"""
PTY Bridge — drives Claude Code CLI through a pseudo-terminal.

Architecture:
  ┌─────────────────┐  PTY master fd   ┌──────────────────┐
  │  PtyBridge      │◄───────────────►│  claude (PID)     │
  │  (reader thread) │  + DA responses  │  (PTY slave)     │
  └────────┬────────┘                  └──────────────────┘
           │ threading.Event
           ▼
  ┌─────────────────┐
  │  bot.py (async)  │
  └─────────────────┘

Key design decisions:
  - Reader thread continuously reads PTY output and responds to DA queries
  - Startup sequence sends Enter to dismiss theme/security/trust dialogs
  - Completion detection: prompt character (❯/▶/>) only — no silence timeout
  - Single-user, asyncio.Lock serializes requests
  - Echo is NOT actively skipped in send(); _clean_output filters echo lines
    via the ^[❯>▶]\s+ pattern. This avoids fragile byte-level echo detection
    that breaks on non-ASCII text, ANSI codes, or timing edge cases.
"""

import asyncio
import logging
import os
import pty
import re
import select
import signal
import subprocess
import threading
import time
from typing import Optional

from ansi_renderer import VirtualScreen
from output_parser import is_prompt_detected, respond_da

logger = logging.getLogger("tg-claude-pty")

# ── Constants ───────────────────────────────────────────────────────────────

STARTUP_SILENCE_TIMEOUT = 10.0  # silence during startup → ready (with >1000 bytes)
START_TIMEOUT = 90.0  # max seconds for full startup (including dialogs + drain)
DEFAULT_RESPONSE_TIMEOUT = 0  # unlimited — wait indefinitely for Claude to finish
ABSOLUTE_MAX_WAIT = 1800.0  # 30 min — hard safety cap, returns whatever is available
DIALOG_ENTER_INTERVAL = 1.5  # seconds between Enter presses during startup

# Startup-phase guard: in --bare mode, Claude outputs spurious prompt
# characters (❯) during settings check, doctor, plugin sync, etc.
# Any ❯/▶/> detected within this window after process start is
# considered startup noise and ignored for completion/prompt-ready.
STARTUP_PHASE_SECS = 45.0  # seconds after process spawn to suppress prompt detection

# Buffer size cap: maximum bytes the raw PTY buffer can hold before
# old data is trimmed.  Prevents unbounded memory growth in long-
# running sessions.  All positional references (_task_send_start_pos,
# _send_prompt_watermark, etc.) are adjusted during trim.
MAX_BUFFER_BYTES = 10 * 1024 * 1024  # 10 MiB

PTY_ROWS = 100
PTY_COLS = 200

# Prompt pattern for completion detection — these characters mark Claude's
# ready-to-accept-input state in the PTY output.
PROMPT_CHARS = frozenset({">", "▶", "❯"})

# Auth prompt patterns for auto-response in reader thread.
# These are checked against buffer tail during response collection.
# IMPORTANT: patterns must be specific enough to avoid false positives
# in normal Claude output. Generic words like "confirm" or "approve"
# are too wide — they appear in everyday conversation and would cause
# spurious y\r writes that derail the response flow.
AUTH_PATTERNS = [
    r"Allow\s+this\s+command",
    r"Allow\s+(read|write|delete|execute|run)\s",
    r"\[y/N\]",
    r"\[Y/n\]",
    r"Type\s+y\s+to\s+approve",
    r"Type\s+y\s+to\s+continue",
    r"Do you want to (continue|proceed|run|allow)",
    r"Approve\s+(bash|shell|command|this|execution)",
    r"Please\s+approve\s+(this|the)",
    r"Authorize\s+(this|the)\s+command",
    r"Authorize\s+command",
]


class PtyBridge:
    """
    Drives Claude Code CLI through a pseudo-terminal.

    Usage:
        bridge = PtyBridge()
        bridge.start()
        response = await bridge.send("Hello")
        await bridge.stop()
    """

    def __init__(self, claude_bin: str = "claude", session_id: str = ""):
        self._claude_bin = claude_bin
        self._session_id = session_id

        # PTY state
        self._master_fd: Optional[int] = None
        self._process: Optional[subprocess.Popen] = None

        # Threading state
        self._running = False
        self._ready = False
        self._reader_thread: Optional[threading.Thread] = None

        # Buffer (accumulates all PTY output)
        self._buffer = bytearray()
        self._buf_lock = threading.Lock()

        # Output burst tracking: detect active subprocess execution.
        # When Claude runs a bash command, the subprocess may produce
        # continuous output bursts. If these bursts suddenly stop, it
        # could mean the subprocess is hung (not Claude finished).
        # We track consecutive reads within 500ms as a "burst" — used
        # for observability logging only. Silence timeout is NOT used
        # for completion; prompt detection is the sole trigger.
        self._output_burst_count = 0
        self._last_output_burst = 0.0

        # Response tracking
        self._expecting_response = False
        self._last_data_time = 0.0
        self._response_event = threading.Event()
        self._send_start_time = 0.0  # Timestamp when send() started
        self._send_start_pos = 0  # Buffer position when send() started

        # Prompt tracking: prevent stale-prompt detection bug
        # When send() starts, we note the current buffer end and stop
        # detecting prompts that were already in the buffer before the
        # new send started.
        self._prompt_seen_pos = 0  # Buffer position of last detected prompt
        self._prompt_seen_lock = threading.Lock()
        self._send_prompt_watermark = 0  # Buffer length at send() start

        # Prompt-ready signal: raised when Claude shows its prompt after
        # completing previous work. Used by _wait_for_prompt_ready() to
        # avoid stacking new input on an unfinished task.
        self._prompt_ready_event = threading.Event()
        self._prompt_ready_event.set()  # Ready initially (startup prompt)

        # Auth prompt auto-response debounce
        self._last_auth_response = 0.0  # timestamp of last auto-y
        self._auth_debounce_secs = 5.0  # min seconds between auto-responses

        # Startup-phase guard: suppress prompt detection during
        # --bare mode startup noise (settings, doctor, plugin sync).
        self._startup_until = 0.0

        # ── Task Mode (non-blocking dispatch) ──
        self._task_mode = False
        self._task_prompt = ""
        self._task_start_time = 0.0
        self._task_completed = threading.Event()
        self._task_result = ""
        self._task_cancelled = threading.Event()
        self._task_send_start_pos = 0

        # Prompt stability tracking (for improved completion detection)
        self._prompt_first_seen = 0.0
        self._prompt_stable_since = 0.0
        self._last_stable_buffer_len = 0

        # Persistent VirtualScreen for diff-based task output extraction
        self._task_screen: Optional[VirtualScreen] = None
        self._task_screen_snapshot: Optional[list] = None

        # claude -p subprocess guard: when True, the reader thread MUST NOT
        # trigger task completion via prompt stability detection. Only the
        # claude -p background thread is allowed to set _task_completed.
        # Prevents race between Path A (reader prompt stability) and
        # Path B (claude -p subprocess) — see send_task().
        self._claude_p_running = False

        # Async bridge (set during start())
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    # ── Properties ──────────────────────────────────────────────────────

    @property
    def is_ready(self) -> bool:
        return self._ready

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def reader_alive(self) -> bool:
        """Check if the reader thread is alive and healthy."""
        t = self._reader_thread
        return t is not None and t.is_alive()

    @property
    def is_prompt_ready(self) -> bool:
        """Check if Claude is at its prompt (ready to accept input)."""
        return self._prompt_ready_event.is_set()

    def _wait_for_prompt_ready(self, timeout: float = 30.0) -> bool:
        """
        Block until Claude shows its prompt (ready for input) or timeout.

        This prevents the "input stacking" bug where new messages are
        written to the PTY while Claude is still executing a previous
        command, causing them to be consumed as part of the command input
        or ignored entirely.

        Returns True if prompt ready within timeout, False if timed out.
        """
        # If already ready, return immediately
        if self._prompt_ready_event.is_set():
            return True

        # Wait for the prompt-ready event
        logger.info("Waiting for Claude prompt (up to %.1fs) before sending...", timeout)
        ready = self._prompt_ready_event.wait(timeout=timeout)
        if ready:
            logger.info("Claude prompt detected, proceeding with send()")
        else:
            logger.warning(
                "Timed out waiting for Claude prompt (%.1fs) — sending anyway",
                timeout,
            )
        return ready

    def _get_readable_buffer_tail(self, max_chars: int = 2000) -> str:
        """Get the last max_chars of decoded buffer content (for fallback)."""
        from output_parser import strip_ansi
        with self._buf_lock:
            tail = bytes(self._buffer[-max_chars * 4:])
        text = tail.decode("utf-8", errors="replace")
        return strip_ansi(text)

    def set_task_mode(self, enabled: bool):
        """Switch between task mode and conversation mode.

        Args:
            enabled: True = task mode, False = conversation mode.
        """
        self._task_mode = enabled
        self._task_completed.clear()
        self._task_cancelled.clear()
        # Reset prompt stability tracking when switching modes
        self._prompt_first_seen = 0.0
        self._prompt_stable_since = 0.0
        self._last_stable_buffer_len = 0
        # Reset send start pos to prevent reader thread from re-triggering
        # completion detection on stale prompt position.
        self._task_send_start_pos = 0
        logger.info("Task mode %s", "enabled" if enabled else "disabled")

    @property
    def task_mode(self) -> bool:
        """Check if currently in task mode."""
        return self._task_mode

    @property
    def task_active(self) -> bool:
        """Check if a task is currently running (task mode + not completed)."""
        return self._task_mode and not self._task_completed.is_set()

    def reset(self):
        """Reset internal state for a fresh session (call after stop)."""
        self._buffer.clear()
        self._ready = False
        self._running = False
        self._expecting_response = False
        self._send_prompt_watermark = 0
        self._output_burst_count = 0
        self._last_output_burst = 0.0
        self._task_mode = False
        self._task_prompt = ""
        self._task_start_time = 0.0
        self._task_completed.clear()
        self._task_result = ""
        self._task_cancelled.clear()
        self._task_send_start_pos = 0
        self._prompt_first_seen = 0.0
        self._prompt_stable_since = 0.0
        self._last_stable_buffer_len = 0
        self._prompt_ready_event.set()  # Reset to ready so next send doesn't block
        self._claude_p_running = False
        # Clear VirtualScreen grid to prevent stale content from
        # previous session leaking into diff-based extraction.
        if self._task_screen is not None:
            self._task_screen._clear_screen()
            self._task_screen._row = 0
            self._task_screen._col = 0

    # ── Start / Stop ────────────────────────────────────────────────────

    def start(self, loop: Optional[asyncio.AbstractEventLoop] = None):
        """
        Start Claude Code in a PTY and handle startup sequence.

        Startup sequence:
          1. Create PTY and spawn Claude
          2. Start reader thread (handles DA queries automatically)
          3. Send Enter repeatedly to dismiss dialogs (theme, security, trust)
          4. Wait for prompt pattern → set ready
        """
        if self._running:
            raise RuntimeError("PtyBridge is already running")

        self._loop = loop or asyncio.get_running_loop()
        self._start_pty()

    def _start_pty(self):
        """Synchronous PTY setup and startup sequence."""
        # Open PTY pair
        master_fd, slave_fd = pty.openpty()
        self._set_pty_size(master_fd)

        # Build claude command with optional session ID
        # Non-bare mode: Claude loads ~/.claude/CLAUDE.md (global dev rules)
        # in addition to --system-prompt-file. The two merge cleanly —
        # /root/tg-claude-pty/.claude/CLAUDE.md sets output format rules, ~/.claude/CLAUDE.md
        # sets coding discipline rules. No conflict.
        # --settings: explicitly point to project-level settings file so
        #   permissions.allow rules are loaded even in --bare mode.
        #   Without this, --bare skips project config entirely and Claude
        #   would lack permissions for files/commands + OAuth session
        #   (showing "Not logged in").
        settings_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            ".claude",
            "settings.local.json",
        )
        cmd = [
            self._claude_bin,
            "--permission-mode", "auto",
            "--settings", settings_path,
            "--system-prompt-file", "/root/tg-claude-pty/.claude/CLAUDE.md",
        ]
        if self._session_id:
            cmd.extend(["--session-id", self._session_id])

        # Strip SDK/agent env vars so Claude starts in interactive CLI mode
        clean_env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith("CLAUDE_CODE_")
            and k not in ("CLAUDECODE", "CLAUDE_AGENT_SDK_VERSION")
        }
        # Start Claude subprocess in isolated working directory.
        # Using /root/chuxi/ ensures Claude never sees pty dev docs.
        chuxi_dir = "/root/chuxi"
        os.makedirs(chuxi_dir, exist_ok=True)
        proc = subprocess.Popen(
            cmd,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
            preexec_fn=os.setsid,
            env=clean_env,
            cwd=chuxi_dir,
        )
        os.close(slave_fd)

        self._master_fd = master_fd
        self._process = proc
        self._running = True
        self._buffer.clear()
        self._response_event.clear()

        # Start reader thread (handles DA queries automatically)
        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader_thread.start()

        # Persistent VirtualScreen for diff-based task output extraction
        self._task_screen = VirtualScreen(rows=PTY_ROWS, cols=PTY_COLS)

        # Startup loop: send Enter to dismiss dialogs, wait for silence
        startup_start = time.monotonic()
        deadline = startup_start + START_TIMEOUT
        last_enter = 0.0
        prev_len = 0
        silent_start = None

        while time.monotonic() < deadline:
            now = time.monotonic()

            if proc.poll() is not None:
                self._running = False
                raise RuntimeError(
                    f"Claude process exited during startup (rc={proc.returncode})"
                )

            with self._buf_lock:
                current_len = len(self._buffer)

            if current_len > prev_len:
                silent_start = None
            elif current_len > 1000 and silent_start is None:
                silent_start = now
            elif current_len > 1000 and silent_start is not None:
                if now - silent_start >= STARTUP_SILENCE_TIMEOUT:
                    self._ready = True
                    break

            prev_len = current_len

            # Send Enter to dismiss dialogs.
            # During early startup (first 20s), also send "q" as
            # fallback for multi-option dialogs like settings issues.
            if now - last_enter >= DIALOG_ENTER_INTERVAL:
                try:
                    if now - startup_start < 20.0:
                        os.write(master_fd, b"q\r")
                    else:
                        os.write(master_fd, b"\r")
                except OSError:
                    break
                last_enter = now

            time.sleep(0.25)

        if not self._ready:
            raise RuntimeError(
                f"Claude did not show prompt within {START_TIMEOUT}s timeout"
            )

        # Post-startup: Claude may still be flushing residual output
        # (settings issue summaries, doctor results).  Wait for the
        # output to settle, then send Enter to get a clean prompt.
        # The silence check above already confirmed ~10s of quiescence,
        # so we just need a brief drain here.
        drain_deadline = time.monotonic() + 5.0
        prev_len = 0
        while time.monotonic() < drain_deadline:
            time.sleep(0.3)
            with self._buf_lock:
                cur_len = len(self._buffer)
            if cur_len > prev_len:
                prev_len = cur_len
                continue
            if cur_len == prev_len and prev_len > 0:
                # Output has settled — send Enter for fresh prompt
                try:
                    os.write(master_fd, b"\r")
                except OSError:
                    break
                time.sleep(1.0)
                break
        # Mark the clean buffer position for future send() calls
        self._send_prompt_watermark = len(self._buffer)

        # ── Startup-phase guard ──
        # Defer prompt detection for STARTUP_PHASE_SECS to let Claude's
        # --bare startup garbage (settings issues, doctor, plugin sync)
        # flush through without triggering false completion events.
        self._startup_until = time.monotonic() + STARTUP_PHASE_SECS
        logger.info(
            "Startup complete; deferring prompt detection for %.0fs "
            "(--bare startup garbage flush)",
            STARTUP_PHASE_SECS,
        )

    # ═══ Send Task (non-blocking) ═══════════════════════════════════════

    def send_task(self, text: str) -> str:
        """
        Submit a task to Claude Code and return immediately.

        Unlike send(), this does NOT wait for a response. It writes the
        prompt to the PTY, records the start position, and returns a
        confirmation string. Callers must monitor _task_completed event
        to know when the task finishes.

        Output extraction uses claude -p --continue (non-interactive,
        pure-text output) instead of scraping the TUI-filled PTY buffer.
        If the subprocess fails, falls back to PTY buffer extraction.

        Args:
            text: The task prompt to send to Claude.

        Returns:
            A confirmation message string.

        Raises:
            RuntimeError: If bridge is not ready.
        """
        if not self._ready:
            raise RuntimeError("PtyBridge is not ready. Call start() first.")

        # Wait for prompt-ready to prevent input stacking.
        # Skip if we're still in startup phase \u2014 Claude may not have
        # signalled prompt-ready yet but is actually at its prompt.
        if not self._prompt_ready_event.is_set() and time.monotonic() >= self._startup_until:
            logger.info("Claude not at prompt \u2014 waiting for it to finish...")
            self._wait_for_prompt_ready(timeout=60.0)

        # Prepare task mode state
        self._task_mode = True
        self._task_prompt = text
        self._task_start_time = time.monotonic()
        self._task_completed.clear()
        self._task_cancelled.clear()
        self._task_result = ""
        self._sent_text = text

        # Record buffer start position
        with self._buf_lock:
            self._task_send_start_pos = len(self._buffer)

        # ── Reset VirtualScreen grid before snapshot ──
        # The persistent _task_screen accumulates grid state from every
        # task. If Claude's ANSI cursor positioning writes to rows that
        # were populated by previous tasks, get_new_text_since() may miss
        # the new content (Bug 1: truncated results) or include stale
        # content from earlier tasks (Bug 2: historical output leaking).
        # Clearing the grid before each snapshot ensures the diff captures
        # only the current task's output from row 0 onward.
        if self._task_screen is not None:
            self._task_screen._clear_screen()
            self._task_screen._row = 0
            self._task_screen._col = 0
        # Snapshot VirtualScreen for diff-based content extraction
        self._task_screen_snapshot = self._task_screen.snapshot() if self._task_screen else None

        # Clear prompt-ready — will be set when prompt reappears
        self._prompt_ready_event.clear()
        self._prompt_first_seen = 0.0
        self._prompt_stable_since = 0.0
        self._last_stable_buffer_len = 0

        # Write to PTY master
        logger.info("send_task: submitting task (%d chars)", len(text))
        os.write(self._master_fd, (text + "\r").encode())

        # \u2500\u2500 Echo Skip Phase \u2500\u2500
        # Wait for the terminal echo to appear, then advance
        # _task_send_start_pos past it. This prevents two problems:
        #   1. Echo lines leaking into the final rendered output
        #   2. The prompty character (\u276f) in the echo line confusing the
        #      reader thread's prompt-detection logic, causing Claude's
        #      real response to be truncated (premature completion).
        echo_skip_deadline = time.monotonic() + 15.0
        while time.monotonic() < echo_skip_deadline:
            if not self._running:
                break
            with self._buf_lock:
                cur_len = len(self._buffer)
            new_bytes_count = cur_len - self._task_send_start_pos
            if new_bytes_count >= len(text.encode("utf-8", errors="replace")) + 1:
                with self._buf_lock:
                    new_bytes = bytes(self._buffer[self._task_send_start_pos:cur_len])
                if self._echo_detected(new_bytes, text):
                    raw_content = new_bytes.decode("utf-8", errors="replace")
                    echo_end_marker = -1
                    for marker in (text + "\r", text + "\n", text):
                        idx = raw_content.find(marker)
                        if idx >= 0:
                            echo_end_marker = idx + len(marker)
                            break
                    if echo_end_marker >= 0:
                        nl_after = raw_content.find("\n", echo_end_marker)
                        if nl_after >= 0:
                            new_pos = self._task_send_start_pos + nl_after + 1
                        else:
                            new_pos = self._task_send_start_pos + echo_end_marker
                        with self._buf_lock:
                            if new_pos <= len(self._buffer):
                                self._task_send_start_pos = new_pos
                        # Reset prompt detection state in reader thread.
                        self._prompt_first_seen = 0.0
                        self._prompt_stable_since = 0.0
                        self._last_stable_buffer_len = 0
                        # Re-snapshot VirtualScreen after echo skip
                        if self._task_screen is not None:
                            self._task_screen_snapshot = self._task_screen.snapshot()
                        # Re-clear task completion after echo skip
                        self._task_completed.clear()
                        logger.debug("send_task: echo skipped for '%s'", text[:50])
                    break
            time.sleep(0.15)

        # ── Launch claude -p --continue in background thread ──
        # Instead of relying on the reader thread's prompt stability
        # detection + VirtualScreen diff (which breaks on TUI-heavy
        # PTY output), we run claude -p --continue as a subprocess.
        # This gives us clean, pure-text output directly from Claude's
        # non-interactive mode, which shares the same session context
        # via --continue (reads ~/.claude/history.jsonl).
        from config import TASK_DEFAULT_TIMEOUT

        claude_bin = self._claude_bin
        # Build claude -p command using the same settings as the PTY session
        settings_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            ".claude",
            "settings.local.json",
        )
        cmd = [
            claude_bin,
            "-p",
            "--continue",
            "--output-format", "text",
            "--permission-mode", "auto",
            "--settings", settings_path,
            "--system-prompt-file", "/root/tg-claude-pty/.claude/CLAUDE.md",
            text,  # prompt as positional argument
        ]
        timeout = TASK_DEFAULT_TIMEOUT
        task_prompt = text
        task_start_pos = self._task_send_start_pos
        master_fd = self._master_fd

        def _run_claude_p_thread():
            """Run claude -p --continue in a thread, capture stdout."""
            chuxi_dir = "/root/chuxi"
            clean_env = {
                k: v
                for k, v in os.environ.items()
                if not k.startswith("CLAUDE_CODE_")
                and k not in ("CLAUDECODE", "CLAUDE_AGENT_SDK_VERSION")
            }
            try:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    cwd=chuxi_dir,
                    env=clean_env,
                )
                if proc.returncode == 0 and proc.stdout.strip():
                    # Success: use subprocess stdout directly
                    result_text = proc.stdout.strip()
                    logger.info(
                        "claude -p completed — %d chars, %d lines",
                        len(result_text),
                        result_text.count("\n") + 1,
                    )
                    # Press Enter on PTY to keep the session alive and
                    # let Claude process the message in interactive mode too.
                    # The PTY write already sent the text; this subprocess
                    # just reaps the answer via --continue. The PTY will
                    # independently finish processing — but since we don't
                    # extract from PTY, that's fine (it just keeps session
                    # context in sync).
                    self._task_result = result_text
                else:
                    # Non-zero exit or empty stdout — fall back to PTY
                    logger.warning(
                        "claude -p failed (rc=%d, stderr=%r) — "
                        "falling back to PTY buffer extraction",
                        proc.returncode,
                        proc.stderr[:200] if proc.stderr else "",
                    )
                    self._task_result = self._extract_task_summary()
            except subprocess.TimeoutExpired:
                logger.warning(
                    "claude -p timed out after %.0fs — "
                    "falling back to PTY buffer extraction",
                    timeout,
                )
                self._task_result = self._extract_task_summary()
            except Exception as e:
                logger.error(
                    "claude -p subprocess error: %s — "
                    "falling back to PTY buffer extraction",
                    e,
                )
                self._task_result = self._extract_task_summary()
            finally:
                self._claude_p_running = False
                self._task_completed.set()

        # ── Disable reader-thread task completion ──
        # The reader thread has its own prompt-stability-based task
        # completion path.  While claude -p is running, we must
        # prevent it from racing ahead and setting _task_completed
        # with stale PTY-buffer content before our subprocess finishes.
        self._claude_p_running = True
        t = threading.Thread(target=_run_claude_p_thread, daemon=True)
        t.start()

        return "\u2705 \u4efb\u52d9\u5df2\u63a5\u6536\uff0c\u5c07\u5728\u80cc\u666f\u57f7\u884c"

    # ═══ Wait for Task Completion (sync helper) ═════════════════════════

    def _wait_for_task_completion(self, timeout: float) -> bool:
        """Block until task completes or timeout. Returns False if timed out.

        Watches _task_completed event with a 1-second polling interval.
        Harcapped at TASK_DEFAULT_TIMEOUT.
        """
        from config import TASK_DEFAULT_TIMEOUT

        start_wait = time.monotonic()
        effective_timeout = TASK_DEFAULT_TIMEOUT if timeout <= 0 else min(timeout, TASK_DEFAULT_TIMEOUT)

        while not self._task_completed.wait(timeout=1.0):
            if not self._running:
                return False
            if time.monotonic() - start_wait >= effective_timeout:
                logger.warning(
                    "Task completion wait timeout (%.0fs) — returning",
                    effective_timeout,
                )
                return False
        return True

    # ═══ Cancel Current Task ════════════════════════════════════════════

    def cancel_current_task(self):
        """
        Cancel the currently running task immediately.

        Uses a phased approach:
        1. Set the _task_cancelled event to signal monitoring loop
        2. Send Ctrl+C (\x03) to the PTY
        3. Wait up to 3s for Claude's prompt to return
        4. If not ready, send Enter and wait 3s more
        5. If still not ready, SIGTERM the Claude process
        6. Set _task_completed to unblock monitoring loop
        7. Switch back to conversation mode
        """
        logger.info("Cancel requested for current task")
        self._task_cancelled.set()

        # Phase 1: Ctrl+C
        try:
            os.write(self._master_fd, b"\x03")
        except OSError:
            pass

        # Phase 2: wait 3s for prompt to return
        time.sleep(3.0)
        if self._prompt_ready_event.is_set():
            logger.info("Task cancelled — prompt returned after Ctrl+C")
            self._task_completed.set()
            self.set_task_mode(False)
            return

        # Phase 3: send Enter and wait 3s more
        try:
            os.write(self._master_fd, b"\r")
        except OSError:
            pass
        time.sleep(3.0)
        if self._prompt_ready_event.is_set():
            logger.info("Task cancelled — prompt returned after Enter")
            self._task_completed.set()
            self.set_task_mode(False)
            return

        # Phase 4: SIGTERM + auto-restart
        proc = self._process
        if proc is not None and proc.returncode is None:
            logger.warning("Task cancel: sending SIGTERM to Claude")
            try:
                os.kill(proc.pid, signal.SIGTERM)
                proc.wait(5.0)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                pass
            self._ready = False
            self._running = False

        self._task_completed.set()
        self.set_task_mode(False)
        logger.info("Task cancelled — SIGTERM sent, triggering auto-restart")

        # Auto-restart the bridge
        try:
            success = self.restart_sync()
            if success:
                logger.info("Bridge auto-restarted after cancel")
                self._ready = True
                self._running = True
                self._prompt_ready_event.set()
            else:
                logger.error("Bridge auto-restart after cancel FAILED")
        except Exception as e:
            logger.error("Bridge auto-restart after cancel raised exception: %s", e)

    def restart_sync(self) -> bool:
        """Restart the bridge synchronously. Returns True if successful.

        Stops the current Claude process (if alive), resets state,
        and starts a fresh session. Safe to call from any thread.

        Returns:
            True if restart succeeded and bridge is ready.
        """
        import signal as _signal

        logger.info("Restarting bridge synchronously...")

        # Stop existing process
        proc = self._process
        if proc is not None and proc.returncode is None:
            try:
                os.kill(proc.pid, _signal.SIGINT)
                proc.wait(timeout=5.0)
            except (ProcessLookupError, subprocess.TimeoutExpired, OSError):
                try:
                    os.kill(proc.pid, _signal.SIGTERM)
                    proc.wait(timeout=3.0)
                except (ProcessLookupError, subprocess.TimeoutExpired, OSError):
                    pass

        self._cleanup()
        self.reset()

        # Start fresh and wait for startup to complete
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self.start(loop)
            loop.close()
            # After start(), the bridge may still be in startup phase.
            # Poll up to START_TIMEOUT + 5s for ready to become True.
            deadline = time.monotonic() + 95.0  # START_TIMEOUT(90) + buffer(5)
            while time.monotonic() < deadline:
                if self._ready:
                    logger.info("Bridge restart successful (ready after %.1fs)",
                                time.monotonic() - deadline + 95.0)
                    return True
                time.sleep(0.5)
            logger.error("Bridge restart timed out waiting for ready state")
            return False
        except Exception as e:
            logger.error("Bridge restart failed: %s", e)
            return False

    # ═══ Extract Task Summary ══════════════════════════════════════════

    def _extract_task_summary(self) -> str:
        """
        Extract a summarized version of the task's output from the buffer.

        Primary: uses VirtualScreen diff (snapshot → current) for clean
        extraction that excludes TUI chrome.  Fallback: raw buffer
        extraction when the diff yields too little content.

        Returns:
            Cleaned summary string. Empty string if no content available.
        """
        from config import TASK_SUMMARY_MAX_LENGTH

        rendered = ""
        if self._task_screen is not None and self._task_screen_snapshot is not None:
            rendered = self._task_screen.get_new_text_since(self._task_screen_snapshot)

        # Clean output
        cleaned = self._clean_output(rendered)

        # ── Fallback: raw buffer extraction ─────────────────────────
        # If the VirtualScreen diff yielded very little content (or empty),
        # fall back to raw PTY buffer extraction.  This handles cases
        # where Claude's ANSI cursor positioning causes VirtualScreen
        # to miss output (e.g., when Claude overwrites the prompt area).
        if (not cleaned or len(cleaned) < 60) and self._task_send_start_pos > 0:
            with self._buf_lock:
                raw_bytes = bytes(self._buffer[self._task_send_start_pos:])
            if raw_bytes:
                from output_parser import extract_content
                raw_text = extract_content(raw_bytes.decode("utf-8", errors="replace"))
                # Strip prompt line (echo) from the beginning
                if self._task_prompt and raw_text.startswith(self._task_prompt[:min(len(self._task_prompt), 20)]):
                    nl = raw_text.find("\n")
                    if nl > 0:
                        raw_text = raw_text[nl + 1:]
                # Re-apply clean_output to the raw fallback
                fallback_cleaned = self._clean_output(raw_text)
                if fallback_cleaned and len(fallback_cleaned) > len(cleaned):
                    logger.info(
                        "_extract_task_summary: fallback extraction yielded "
                        "%d chars (vs %d from VirtualScreen diff)",
                        len(fallback_cleaned), len(cleaned),
                    )
                    cleaned = fallback_cleaned

        if not cleaned:
            return ""

        # Compress if too long
        if len(cleaned) > TASK_SUMMARY_MAX_LENGTH:
            head_ratio = 0.3
            tail_ratio = 0.2
            head_end = int(len(cleaned) * head_ratio)
            tail_start = int(len(cleaned) * (1 - tail_ratio))
            head = cleaned[:head_end].rstrip()
            tail = cleaned[tail_start:].lstrip()
            middle_len = len(cleaned) - head_end - (len(cleaned) - tail_start)
            compressed = (
                f"{head}\n\n...\uff08\u7701\u7565 {middle_len} \u5b57\u5143\uff09...\n\n{tail}"
            )
            return compressed

        return cleaned

    async def stop(self):
        """Stop Claude Code gracefully: SIGINT → SIGTERM → SIGKILL."""
        self._ready = False
        self._running = False

        proc = self._process
        if proc is None or proc.returncode is not None:
            self._cleanup()
            return

        pid = proc.pid

        # SIGINT
        try:
            os.kill(pid, signal.SIGINT)
            await asyncio.get_event_loop().run_in_executor(None, proc.wait, 5.0)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            pass

        if proc.returncode is not None:
            self._cleanup()
            return

        # SIGTERM
        try:
            os.kill(pid, signal.SIGTERM)
            await asyncio.get_event_loop().run_in_executor(None, proc.wait, 5.0)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            pass

        if proc.returncode is not None:
            self._cleanup()
            return

        # SIGKILL
        try:
            os.kill(pid, signal.SIGKILL)
            await asyncio.get_event_loop().run_in_executor(None, proc.wait, 5.0)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            pass

        self._cleanup()

    # ── Send / Receive ──────────────────────────────────────────────────

    def _cleanup(self):
        """Close PTY master fd and clean up process resources."""
        self._ready = False
        self._running = False
        if self._master_fd is not None:
            try:
                os.close(self._master_fd)
            except OSError:
                pass
            self._master_fd = None
        # Reap child process if still alive
        proc = self._process
        if proc is not None and proc.returncode is None:
            try:
                proc.terminate()
                proc.wait(timeout=3.0)
            except (ProcessLookupError, subprocess.TimeoutExpired, OSError):
                try:
                    proc.kill()
                    proc.wait(timeout=2.0)
                except (ProcessLookupError, subprocess.TimeoutExpired, OSError):
                    pass
        # Don't set _process to None — callers may need returncode

    def _trim_buffer_if_needed(self):
        """
        Trim the raw PTY buffer when it exceeds MAX_BUFFER_BYTES.

        Preserves the most recent portion.  Adjusts all position-
        tracking fields (_task_send_start_pos, _send_prompt_watermark,
        _send_start_pos, _prompt_seen_pos) to stay consistent.

        Must be called INSIDE _buf_lock.
        """
        buf_len = len(self._buffer)
        if buf_len <= MAX_BUFFER_BYTES:
            return

        excess = buf_len - MAX_BUFFER_BYTES
        # Keep the last MAX_BUFFER_BYTES — 1 MiB margin to avoid
        # trimming on every append once near the boundary.
        trim_to = MAX_BUFFER_BYTES - (1024 * 1024)  # keep ~9 MiB
        discard = buf_len - trim_to
        if discard <= 0:
            return

        # Trim the prefix
        self._buffer = self._buffer[discard:]
        offset = discard

        # Adjust all positional references
        self._task_send_start_pos = max(0, self._task_send_start_pos - offset)
        self._send_start_pos = self._task_send_start_pos  # approximate
        self._send_prompt_watermark = max(0, self._send_prompt_watermark - offset)
        with self._prompt_seen_lock:
            self._prompt_seen_pos = max(0, self._prompt_seen_pos - offset)

        logger.debug(
            "Buffer trimmed: discarded %d bytes (%.1f MB), "
            "kept %d bytes (%.1f MB)",
            discard, discard / (1024 * 1024),
            len(self._buffer), len(self._buffer) / (1024 * 1024),
        )

    async def send(
        self, text: str, timeout: float = DEFAULT_RESPONSE_TIMEOUT
    ) -> Optional[str]:
        """
        Send text to Claude Code and return the response.

        Echo handling: Echo is NOT actively skipped in send(). The echo
        lines (starting with ❯/▶/>) are filtered by _clean_output via the
        ^[❯>▶]\s+ regex. This avoids fragile byte-level echo detection
        that breaks on non-ASCII text, ANSI escape codes, or timing races.

        Auth prompt handling: quickly scans buffer tail for authorization
        prompts (non-blocking, sub-second window). Relies primarily on
        settings.local.json allow-list; this is the safety net.
        """
        if not self._ready:
            raise RuntimeError("PtyBridge is not ready. Call start() first.")

        # ── Wait for Claude to be ready ────────────────────────────────
        # If Claude is still processing a previous task (no prompt shown),
        # waiting here prevents input stacking. New text wouldn't be
        # processed anyway — it'd either be ignored or consumed as part
        # of the unfinished command.
        if not self._prompt_ready_event.is_set():
            logger.info("Claude not at prompt — waiting for it to finish...")
            loop = asyncio.get_running_loop()
            prompt_ready = await loop.run_in_executor(
                None, self._wait_for_prompt_ready, 60.0
            )
            if not prompt_ready:
                logger.warning("Claude still not at prompt after 60s — sending anyway")

        # Record starting position and time
        with self._buf_lock:
            start_pos = len(self._buffer)
        self._send_start_pos = start_pos
        self._send_start_time = time.monotonic()

        # Set a watermark: the buffer position at send() start.
        # The reader thread will ONLY signal prompt-based completion
        # if the prompt was detected in content written AFTER this
        # marker position. This prevents the stale-prompt race where
        # the old prompt from a previous response immediately triggers
        # completion of the new request.
        self._send_prompt_watermark = start_pos

        # Setup for response tracking
        self._expecting_response = True
        self._last_data_time = time.monotonic()
        # Clear the event INSIDE the lock holder (send), not in
        # _wait_for_response, to avoid the race where the reader
        # thread sets the event between clear() and the PTY write.
        self._response_event.clear()
        # Clear prompt-ready — will be set again when Claude shows prompt
        # after this response completes.
        self._prompt_ready_event.clear()

        # Store sent text for echo detection
        self._sent_text = text

        # Write to PTY master (\r = Enter in raw terminal mode)
        os.write(self._master_fd, (text + "\r").encode())

        # ── Echo Skip Phase ────────────────────────────────────────────
        # Terminal echoes user input. We wait for the echo to appear in
        # the buffer, then advance start_pos past it so the VirtualScreen
        # render doesn't include it in the response.
        # 15-second window: Claude --bare mode may take several seconds
        # to render the input display (especially with large context).
        echo_skip_deadline = time.monotonic() + 15.0
        echo_skipped = False
        while time.monotonic() < echo_skip_deadline:
            if not self._running:
                break
            with self._buf_lock:
                new_bytes = bytes(self._buffer[start_pos:])
            if len(new_bytes) >= len(text.encode("utf-8", errors="replace")) + 1:
                if self._echo_detected(new_bytes, text):
                    # Find echo end in buffer — advance to after the echo line
                    raw_content = new_bytes.decode("utf-8", errors="replace")
                    # Find echo content + following newline
                    echo_end_marker = -1
                    for marker in (text + "\r", text + "\n", text):
                        idx = raw_content.find(marker)
                        if idx >= 0:
                            echo_end_marker = idx + len(marker)
                            break
                    if echo_end_marker >= 0:
                        # Advance to after the echo line's newline
                        nl_after = raw_content.find("\n", echo_end_marker)
                        if nl_after >= 0:
                            start_pos += nl_after + 1
                        else:
                            start_pos += echo_end_marker
                    echo_skipped = True
                    logger.debug("Echo detected and skipped for '%s'", text[:50])
                    break
            time.sleep(0.15)

        # Update send tracking with potentially adjusted start_pos
        self._send_start_pos = start_pos
        self._send_prompt_watermark = start_pos

        # ── Re-clear response event after echo skip ────────────────────
        # The reader thread runs independently and may have detected a
        # prompt character in the echo output (e.g., "❯ 你好") and set
        # _response_event during the echo skip phase. If we proceed to
        # _wait_for_response with the event already set, it returns
        # immediately with an empty response — the user sees only their
        # own text echoed back while Claude is still thinking.
        #
        # Re-clearing here ensures only prompts appearing AFTER this
        # point (Claude's real post-response prompt) trigger completion.
        self._response_event.clear()

        # Auth prompt detection has been moved to the reader thread
        # (_check_and_respond_auth). It runs continuously during
        # response collection, so auth prompts appearing at any point
        # in Claude's execution are handled — not just those visible
        # in the first 15s of send().

        # ── Wait for Response ───────────────────────────────────────────
        loop = asyncio.get_running_loop()
        try:
            completed = await loop.run_in_executor(None, self._wait_for_response, timeout)
        except asyncio.CancelledError:
            self._expecting_response = False
            self._prompt_ready_event.set()  # Unblock prompt waiters on cancel
            raise

        self._expecting_response = False
        # Signal prompt-ready: after send() completes (success or timeout),
        # the reader thread has likely detected Claude's next prompt.
        # Set here as a safety net in case the reader thread hasn't yet.
        self._prompt_ready_event.set()

        if not completed:
            logger.warning("send() timed out or bridge dead — no response available")
            return None

        if not self._running:
            return None

        # ── Render & Clean ──────────────────────────────────────────────
        with self._buf_lock:
            new_bytes = bytes(self._buffer[start_pos:])

        raw = new_bytes.decode("utf-8", errors="replace")
        logger.debug("send() raw (%d b): %s", len(new_bytes), raw[:500])

        screen = VirtualScreen(rows=PTY_ROWS, cols=PTY_COLS)
        rendered = screen.render(raw)

        # Clean up TUI artifacts and normalize formatting
        rendered = self._clean_output(rendered)

        # Safety check: if after cleaning we only got a short status line
        # (like "✻ Churned for 0s"), it means we likely captured too little
        # content (stale-prompt race or similar). Trim the raw content to
        # exclude any residual trash from before send() that VirtualScreen
        # might have rendered, and try again.
        cleaned = rendered.strip()
        # ── Echo Residual Detection ─────────────────────────────────────
        # If the cleaned result looks suspiciously like just the echo of
        # what we sent, try fallback extraction from raw PTY output.
        # Skip for very short messages (<=3 chars): Claude's response to
        # short input may be short and have high similarity to the prompt
        # text itself (e.g. Chinese greeting), causing false-positive echo
        # detection and blank replies.
        if (cleaned and self._sent_text and len(self._sent_text) > 3
                and len(cleaned) <= len(self._sent_text) + 10):
            from difflib import SequenceMatcher
            similarity = SequenceMatcher(None, cleaned, self._sent_text).ratio()
            if similarity > 0.5:
                logger.warning(
                    "Response appears to be echo residual (similarity=%.2f). "
                    "Trying fallback extraction.",
                    similarity,
                )
                from output_parser import extract_content
                fallback = extract_content(raw)
                fallback = re.sub(r"\s*[❯>▶]\s*$", "", fallback).strip()
                # Apply filtering to fallback too — extract_content is bare
                if fallback and len(fallback) > len(cleaned):
                    fallback = self._clean_output(fallback)
                    if fallback.strip():
                        return fallback.strip()

        if cleaned and len(cleaned) < 40 and self._is_status_line_only(cleaned):
            logger.warning(
                "_clean_output produced only status line (%r) — "
                "possible stale-prompt capture. Trying fallback extraction.",
                cleaned,
            )
            # Fallback: use output_parser.extract_content on raw bytes
            from output_parser import extract_content
            fallback = extract_content(raw)
            fallback = re.sub(r"\s*[❯>▶]\s*$", "", fallback).strip()
            # Remove status lines from fallback
            fallback_lines = []
            for fl in fallback.split("\n"):
                fl_stripped = fl.strip()
                if re.search(r"(✻|✶|\*)\s+(Brewed|Cogitated|Churned|Thought|Run|Ran)\s+for", fl_stripped):
                    continue
                if fl_stripped.startswith("✻") and "for" in fl_stripped:
                    continue
                fallback_lines.append(fl)
            fallback = "\n".join(fallback_lines).strip()
            # Apply _clean_output filtering to the fallback result too
            if fallback:
                fallback = self._clean_output(fallback)
                if fallback.strip():
                    return fallback.strip()

        return cleaned

    # ── Reader Thread ───────────────────────────────────────────────────

    def _check_and_respond_auth(self):
        """Check buffer tail for auth prompts and auto-respond with 'y'.

        Called from the reader thread during response collection.
        Includes a 5-second debounce per-request to avoid spamming
        'y' on repeated auth prompts within the same command chain.

        Only active when _expecting_response is True (i.e. between
        send() start and prompt detection).

        Scans only the most recent 1024 bytes of the buffer — enough
        to capture a full auth prompt without re-matching old text.
        """
        now = time.monotonic()
        if now - self._last_auth_response < self._auth_debounce_secs:
            return  # Debounce: don't respond again within 5s

        with self._buf_lock:
            buf_len = len(self._buffer)
            if buf_len == 0:
                return
            scan_start = max(0, buf_len - 1024)
            tail = bytes(self._buffer[scan_start:])
        if not tail:
            return

        tail_text = tail.decode("utf-8", errors="replace")
        if any(re.search(p, tail_text, re.IGNORECASE) for p in AUTH_PATTERNS):
            logger.info("Auth prompt detected in reader thread — auto-responding 'y'")
            try:
                os.write(self._master_fd, b"y\r")
            except OSError:
                pass
            self._last_auth_response = now

    def _reader_loop(self):
        """Continuously read PTY output, respond to DA queries."""
        poll = select.poll()
        poll.register(self._master_fd, select.POLLIN)

        while self._running and self._master_fd is not None:
            try:
                events = poll.poll(250)
            except (ValueError, OSError):
                break

            now = time.monotonic()

            if events:
                try:
                    data = os.read(self._master_fd, 4096)
                except (OSError, ValueError):
                    break

                if not data:
                    logger.warning("EOF on PTY master fd — Claude process exited")
                    break

                # Respond to terminal DA queries
                respond_da(data, self._master_fd)

                # Append to buffer
                with self._buf_lock:
                    self._buffer.extend(data)
                    self._trim_buffer_if_needed()
                self._last_data_time = now

                # Feed to persistent VirtualScreen for diff-based extraction
                if self._task_screen is not None:
                    self._task_screen.feed(data.decode("utf-8", errors="replace"))

                # Track output bursts — consecutive reads within 500ms
                # indicate active output (e.g., subprocess running).
                if now - self._last_output_burst < 0.5:
                    self._output_burst_count += 1
                else:
                    self._output_burst_count = 1
                self._last_output_burst = now

                # ── Auth prompt check (reader thread, continuous) ──────
                # Checks buffer tail for authorization prompts every time
                # new data arrives. Handles auth prompts that appear mid-
                # execution (e.g. 3rd sub-task needs sudo), not just those
                # visible at send() start. 5s debounce prevents spamming.
                # In pure task mode, always checks when a task is active.
                if self._expecting_response or (
                    self._task_send_start_pos > 0 and not self._task_completed.is_set()
                ):
                    self._check_and_respond_auth()

            # ── Response completion detection (send mode) ────────────
            if self._expecting_response and self._last_data_time > 0:
                elapsed = now - self._send_start_time

                if self._output_burst_count >= 3:
                    silence = now - self._last_data_time
                    if silence > 10.0 and self._output_burst_count % 5 == 0:
                        logger.debug(
                            "Burst=%d active, elapsed=%.0fs, silence=%.0fs",
                            self._output_burst_count, elapsed, silence,
                        )

                if elapsed >= 0.5:
                    prompt_detected, prompt_pos = self._check_prompt_detected_with_pos()
                    if prompt_detected and prompt_pos > self._send_prompt_watermark:
                        # Echo-aware prompt filter: don't complete if
                        # the prompt is from an echo line
                        prompt_detected = not self._is_prompt_from_echo(
                            prompt_pos, self._sent_text,
                            watermark=self._send_prompt_watermark,
                        )
                    if prompt_detected and prompt_pos > self._send_prompt_watermark:
                        quiet_needed = 2.0
                        if self._last_data_time > 0:
                            silence = now - self._last_data_time
                            if silence >= quiet_needed:
                                self._response_event.set()

            # ── Heartbeat / stall logging (task mode) ───────────────
            if self._task_send_start_pos > 0 and not self._task_completed.is_set():
                from config import BUFFER_STALL_TIMEOUT, HEARTBEAT_INTERVAL as _cfg_hb

                # Buffer stall detection
                data_silence = now - self._last_data_time
                if data_silence >= BUFFER_STALL_TIMEOUT and self._last_data_time > 0:
                    elapsed = now - self._task_start_time if self._task_start_time > 0 else 0.0
                    logger.warning(
                        "BUFFER STALL: no new data for %.0fs (elapsed=%.0fs) — "
                        "task may be hung, waiting for prompt",
                        data_silence, elapsed,
                    )

                # Periodic heartbeat log
                elapsed = now - self._task_start_time if self._task_start_time > 0 else 0.0
                if elapsed >= _cfg_hb and int(elapsed) % int(_cfg_hb) == 0 and int(elapsed) > 0:
                    with self._buf_lock:
                        buf_growth = len(self._buffer) - self._task_send_start_pos
                    logger.debug(
                        "HEARTBEAT: task running for %.0fs, buffer grown by %d bytes, "
                        "silence=%.0fs, burst_count=%d",
                        elapsed, buf_growth, data_silence, self._output_burst_count,
                    )

            # ── Task completion detection (prompt stability) ──────
            # IMPORTANT: when claude -p is running (send_task mode),
            # we must NOT use prompt stability to detect completion.
            # The claude -p background thread is the sole authority
            # for setting _task_completed.  Allowing the reader to
            # race ahead causes stale/echo PTY-buffer content to be
            # sent to the user before claude -p finishes.
            if (self._task_send_start_pos > 0
                    and not self._task_completed.is_set()
                    and not self._claude_p_running):
                elapsed = now - self._task_start_time if self._task_start_time > 0 else 0.0

                if self._output_burst_count >= 3:
                    silence = now - self._last_data_time
                    if silence > 10.0 and self._output_burst_count % 5 == 0:
                        logger.debug(
                            "Burst=%d active, elapsed=%.0fs, silence=%.0fs",
                            self._output_burst_count, elapsed, silence,
                        )

                # Prompt-based completion with stability tracking
                if elapsed >= 0.5:
                    prompt_detected, prompt_pos = self._check_prompt_detected_with_pos()
                    if prompt_detected and prompt_pos > self._task_send_start_pos:
                        # Echo-aware prompt filter: don't complete if
                        # the prompt line matches echo pattern
                        prompt_detected = not self._is_prompt_from_echo(
                            prompt_pos, self._task_prompt,
                            watermark=self._task_send_start_pos,
                        )
                    if prompt_detected and prompt_pos > self._task_send_start_pos:
                        # Content gate: require at least 150 bytes of
                        # new content after echo region before
                        # considering completion (prevents premature
                        # firing on long Chinese echo lines)
                        with self._buf_lock:
                            new_content_len = len(self._buffer) - self._task_send_start_pos
                        if new_content_len < 150:
                            prompt_detected = False
                        else:
                            if self._prompt_first_seen == 0.0:
                                self._prompt_first_seen = now
                                self._prompt_stable_since = None
                                self._last_stable_buffer_len = len(self._buffer)
                            elif now - self._prompt_first_seen >= 2.0:
                                if self._prompt_stable_since is None:
                                    self._prompt_stable_since = now
                                    self._last_stable_buffer_len = len(self._buffer)
                                cur_len = len(self._buffer)
                                if cur_len > self._last_stable_buffer_len:
                                    self._prompt_stable_since = now
                                    self._last_stable_buffer_len = cur_len
                                if self._prompt_stable_since is not None:
                                    if now - self._prompt_stable_since >= 3.0:
                                        logger.info(
                                            "Task completed — prompt stable for 3.0s after %.0fs",
                                            elapsed,
                                        )
                                        self._task_completed.set()
                                        self._task_result = self._extract_task_summary()
                    else:
                        self._prompt_first_seen = 0.0
                        self._prompt_stable_since = 0.0

            # ── Prompt-ready detection (for input-stacking prevention) ──
            # Set prompt_ready_event when Claude shows its prompt AND we
            # are NOT currently waiting for a send() response. This signals
            # to the next send() caller that Claude is ready to accept input.
            # Startup-phase guard: suppress prompt detection during startup
            # noise window to avoid spurious prompt-ready signals.
            if now < self._startup_until:
                pass  # Still in startup phase — ignore all prompt signals
            elif not self._expecting_response:
                prompt_detected, _ = self._check_prompt_detected_with_pos()
                if prompt_detected:
                    self._prompt_ready_event.set()

        # Reader thread exiting
        self._running = False
        self._ready = False
        self._response_event.set()  # Unblock any waiter

    def _wait_for_response(self, timeout: float):
        """Block until response completion or timeout. Returns False if timed out.

        Completion is triggered by the reader thread detecting Claude's prompt
        (prompt_trigger). Silence timeout is deliberately NOT used here — see
        _reader_loop for rationale. The ABSOLUTE_MAX_WAIT safety net ensures we
        never block forever even if prompt detection fails.

        NOTE: The _response_event is cleared in send() BEFORE the PTY write.
        The reader thread will set it again when the prompt is detected.
        This avoids the race where the reader sets the event before
        _wait_for_response clears it.
        """
        start_wait = time.monotonic()
        # Safety cap: if caller passes 0 (unlimited), use ABSOLUTE_MAX_WAIT.
        # If caller passes a specific timeout, use it but never exceed MAX_WAIT.
        effective_timeout = ABSOLUTE_MAX_WAIT if timeout <= 0 else min(timeout, ABSOLUTE_MAX_WAIT)

        while self._response_event.wait(timeout=1.0) is False:
            # Check if the bridge is still running
            if not self._running:
                return False
            # Check absolute max-wait safety net
            if time.monotonic() - start_wait >= effective_timeout:
                logger.warning(
                    "Absolute max-wait reached (%.0fs) — returning partial response",
                    effective_timeout,
                )
                return False
        return True

    # ── Output Cleaning ─────────────────────────────────────────────────

    @staticmethod
    def _is_status_line_only(text: str) -> bool:
        """Check if stripped text is just a status/duration line."""
        return bool(
            re.search(r"(✻|✶|\*|✽|✢|✦|✧)\s+(Brewed|Cogitated|Churned|Thought|Run|Ran|Worked)\s+for", text)
            or re.match(r"^Found\s+\d+\s+settings\s+issues", text)
            or re.match(r"^⚠️\s+", text)
        )

    # ── Characters of interest for filtering ────────────────────────

    # Box-drawing characters (single, double, heavy, mixed, block elements)
    _BOX_DRAWING_CHARS = frozenset(
        "┌┐└┘├┤┬┴┼│─═╭╮╰╯"   # single + double horizontal
        "╔╗╚╝╠╣╦╩╬"       # double-line box drawing
        "┣┫┳┻╋"           # heavy/vertical variants
        "┏┓┗┛"            # heavy box
        "┃┆┇┈┉┊┋"           # light vertical/dashed border
        "█▀▄▌▐░▒▓"         # block elements
    )

    # Horizontal-rule characters (repeating these = a separator line)
    _HR_CHARS = frozenset("─━═➖‐‑‒–—―▔▀-_=")

    # Purely decorative / TUI ornament characters
    _DECORATIVE_CHARS = frozenset(
        "◆◇▸▹▪▫▴▾◂⬤●○◎◉◈⬡⬢⬣▶▷▲▼◀◁"
        "⏵⏸🔃🔄⏳⏺✅❌⚠️🎯🏃📋📝"
        "⏎↩↵←↑→↓↔↕"
    )

    # ⸻ P0 · Code-block protection ────────────────────────────────────

    @classmethod
    def _clean_output(cls, rendered: str) -> str:
        """Clean up TUI artifacts, protocol sections, and normalize formatting.

        Code blocks (``` ... ```) are extracted BEFORE any filtering and
        reinserted AFTER to prevent damage to indentation, box characters,
        and formatting that legitimately belongs in code output.
        """
        lines = rendered.split("\n")
        _all_lines = list(lines)  # safety fallback copy

        # ═══ Phase 0: code-block extraction ═══
        code_blocks: list[str] = []
        placeholders: list[str] = []
        _in_fence = False
        _fence_lines: list[str] = []
        _non_fence_lines: list[str] = []
        for line in lines:
            if line.startswith("```"):
                if not _in_fence:
                    # Entering code block
                    _in_fence = True
                    _fence_lines = [line]
                else:
                    # Exiting code block
                    _fence_lines.append(line)
                    code_blocks.append("\n".join(_fence_lines))
                    placeholder = f"__CODEBLOCK_{len(placeholders)}__"
                    placeholders.append(placeholder)
                    _non_fence_lines.append(placeholder)
                    _fence_lines = []
                    _in_fence = False
            elif _in_fence:
                _fence_lines.append(line)
            else:
                _non_fence_lines.append(line)
        # Edge case: unclosed fence at end — treat as non-code so we don't lose it
        if _in_fence:
            _non_fence_lines.extend(_fence_lines)

        # Now filter only the non-code-block lines
        lines = _non_fence_lines

        # ═══ Phase 1: protocol / summary section suppression ═══
        filtered: list[str] = []
        in_protocol = False
        suppress_summary = False

        for line in lines:
            stripped = line.strip()

            # ── Spinner/braille ──
            if re.match(r"^[⠁-⣿](\s+\S+){0,4}$", stripped) and len(stripped) < 40:
                continue
            if re.match(r"^Waiting\.\.\.?$", stripped):
                continue
            if re.match(r"^[⠁-⣿]$", stripped):
                continue

            # ── Protocol section ──
            if not in_protocol and re.match(r"^##\s+Protocol", stripped):
                in_protocol = True
                continue
            if in_protocol:
                if re.match(r"^##\s", stripped):
                    in_protocol = False
                    filtered.append(line)
                    continue
                if re.match(r"^──+", line):
                    in_protocol = False
                    continue
                if re.match(r"^\s*(###|\+|─|═|📋|🏃|📝)", line):
                    continue
                if re.search(r"ctrl\+o", stripped) or re.search(r"\(\+\d+\s+lines?\)", stripped):
                    continue
                if re.match(r"^\d+\.\s", stripped) and len(stripped) < 120:
                    continue
                if re.match(r"^\s*\[.*\]\s*$", stripped):
                    continue
                if re.match(r"^\d+\.\s+/(run|edit|web|ask|init)", stripped):
                    continue
                if re.match(r"^\s*(Mode|Status):\s", stripped, re.IGNORECASE):
                    continue
                in_protocol = False

            # ── Summary section ──
            if re.match(r"^##\s+Summary$", stripped):
                suppress_summary = True
                continue
            if suppress_summary:
                if re.match(r"^##\s", stripped):
                    suppress_summary = False
                    filtered.append(line)
                    continue
                if not stripped:
                    continue
                if cls._is_horizontal_rule_line(stripped):
                    continue
                if re.match(r"^[•·●]\s*(Tool|File|Script|Error|User|Exit|Command)", stripped):
                    continue
                if re.match(r"^\d+\s+\w+\s+for\s", stripped):
                    continue
                continue

            # ═══ Phase 2: per-line garbage detection ═══

            # ── Markdown tables ──
            if re.match(r"^\s*\|.*\|\s*$", stripped):
                continue
            if re.match(r"^\s*\|[\-\s:]+\|\s*$", stripped):
                continue

            # ── Unicode table-border lines (│ ┃ ┆ etc.) ──
            if stripped and stripped[0] in "│┃┆┇┈┉┊┋":
                continue

            # ── Horizontal rules (P0: full coverage) ──
            if cls._is_horizontal_rule_line(stripped):
                continue

            # ── Framed lines: ─── content ─── or === content === ──
            # Claude Code's TUI uses hr-like framing around section
            # titles (e.g. "─── Standard Output ───" or "----- stderr -----").
            # These are chrome, not content — filter them out.
            if re.match(r"^[─━═\-]{2,}\s*.+?\s*[─━═\-]{2,}$", stripped):
                continue

            # ── Claude file-reading section headers ────────────────────
            # When Claude reads code (especially multi-file), it emits
            # "--- path/to/file.ext ---" separators and VirtualScreen
            # corruption can produce fragments like "- --- file.ts --" or
            # "/file.ts ---     --- src/components/...".  Strip these.
            if cls._is_file_header_line(stripped):
                continue

            # ── Help/shortcut lines ──
            if re.match(r"^\s*\?\s+for\s+shortcuts", line):
                continue
            if re.match(r"^\s*Press\s+\w+\s+for\s", line):
                continue

            # ── Prompt lines ──
            if re.match(r"^[❯>▶]\s+", line):
                continue
            if re.match(r"^\s*[❯>▶]\s*$", line):
                continue

            # ── ● TUI menu items (not regular bullets) ──
            if re.match(r"^●\s*(Tool|File|Script|Error|User|Exit|Command|Mode|Status)", stripped):
                continue

            # ── Box-drawing / tree-diagram lines (P1: 25% threshold) ──
            if stripped and stripped[0] in "⎿└├│┌┐┘└├┤┬┴┼╭╮╰╯╔╗╚╝╠╣╦╩╬┣┫┳┻╋┏┓┗┛":
                continue
            if len(stripped) > 0:
                bd_ratio = sum(1 for c in stripped if c in cls._BOX_DRAWING_CHARS) / max(len(stripped), 1)
                if bd_ratio > 0.25:
                    continue

            # ── Status text ──
            if stripped in ("Waiting…", "Working…", "Thinking…"):
                continue
            if re.match(r"^(Waiting|Working|Thinking)[…\.]+", stripped):
                continue

            # ── Indented tree structures ──
            if re.match(r"^ {12,}(├|└|│|─)", line):
                continue

            # ── Very-wide blank padding ──
            if re.match(r"^\s{180,}$", line):
                continue

            # ── TUI hints (P1: expanded coverage) ──
            if any(hint in line for hint in (
                "Esc to cancel", "Esc to interrupt", "esc to interrupt",
                "Tab to amend", "ctrl+e to explain", "Return to confirm",
                'type "continue"', "Ctrl+C to cancel",
                "shift+tab to cycle", "Shift+Tab to cycle",
                "press Esc to", "Press Esc to",
                "press Enter to", "Press Enter to",
                "press any key", "Press any key",
                "Esc to", "esc to",
            )):
                continue
            if re.search(r"ctrl\+o", stripped) or re.search(r"\(\+\d+\s+lines?\)", stripped):
                continue

            # ── Claude status lines ──
            if re.search(r"(✻|✶|\*)\s+(Brewed|Cogitated|Churned|Thought|Run|Ran)\s+for", stripped):
                continue
            if stripped.startswith("✻") and ("for" in stripped) and len(stripped) < 40:
                continue

            # ── TUI status bars ──
            # Non-bare mode emits status bar lines like:
            #   ⏵⏵automodeon(shift+tab to cycle)
            #   ⏵⏵automodeon(shift+tabtocycle)  · esc to interrupt
            #   ⏵⏵trusted  · esc to interrupt
            # Match any line starting with ⏵ or ⏸ followed by alphanumeric
            # status text — these are always TUI chrome, never real output.
            if re.match(r"^[⏵⏸]\S*\s*[\(·]", stripped):
                continue
            if re.match(r"^[⏵⏸]\S+$", stripped):
                continue
            if "shift+tab" in stripped.lower():
                continue
            if "esc to interrupt" in stripped.lower():
                continue

            # ── Protocol artifact lines ──
            if re.match(r"^###\s+\d+\.\s", stripped):
                continue
            if re.match(r"^Found\s+\d+\s+settings\s+issues", stripped):
                continue
            if re.match(r"^\s*Exit\s+code:\s+\d+", line):
                continue

            # ── Decorative emoji / unicode lines ──
            if re.match(r"^[🔃🔄⏳⏺✅❌⚠️]\s", line):
                continue
            if re.match(r"^\s*\d+\s+files?", stripped):
                continue

            # ── Pure decorative-character lines ──
            if cls._is_pure_decorative_line(stripped):
                continue
            # Lines starting with just a decorative symbol + minimal text
            if re.match(r"^(▸|▹|▪|▫|◆|◇)\s{0,2}$", stripped):
                continue

            # ── ANSI remnants ──
            if "\x1b" in stripped or "\033" in stripped or "\e" in stripped:
                continue

            # ── Emoji/decorator bullet rows (P2: Unicode-range detection) ──
            if cls._is_decorator_bullet_line(stripped):
                continue

            filtered.append(line)

        rendered = "\n".join(filtered)

        # ═══ Phase 5: post-processing on non-code-block text ═══

        # Space compression (P2: on non-code-block text only)
        rendered = re.sub(r" {2,}", " ", rendered)

        # Trailing prompt cleanup
        rendered = re.sub(r"\s*[❯>▶]\s*$", "", rendered)

        # ● prefix removal (Claude TUI artifact)
        rendered = re.sub(r"^●\s+", "", rendered, flags=re.MULTILINE)

        # Trim
        rendered = rendered.strip()

        # Collapse 3+ consecutive blank lines → 2
        rendered = re.sub(r"\n{4,}", "\n\n\n", rendered)

        # ═══ Reinsert code blocks ═══
        for i, placeholder in enumerate(placeholders):
            if placeholder in rendered:
                rendered = rendered.replace(placeholder, code_blocks[i])
            else:
                # Placeholder may have been absorbed by blank-line collapse
                # or other transforms. Re-append at the end.
                rendered = rendered.rstrip() + "\n\n" + code_blocks[i]

        # ── Safety fallback ──
        if not rendered:
            minimal: list[str] = []
            for line in _all_lines:
                s = line.strip()
                if not s:
                    continue
                if cls._is_horizontal_rule_line(s):
                    continue
                if cls._is_file_header_line(s):
                    continue
                if re.match(r"^[─━═\-]{2,}\s*.+?\s*[─━═\-]{2,}$", s):
                    continue
                if re.match(r"^\s*[❯>▶]\s*$", s):
                    continue
                if re.match(r"^\s*\?\s+for\s+shortcuts", s):
                    continue
                minimal.append(line)
            rendered_lines = "\n".join(minimal)
            rendered = re.sub(r" {2,}", " ", rendered_lines)
            rendered = rendered.strip()

        return rendered

    # ⸻ Helper: horizontal-rule detection ──────────────────────────────

    @classmethod
    def _is_horizontal_rule_line(cls, stripped: str) -> bool:
        """Check if a line is a horizontal rule / separator.

        Covers:
          ---, ___ (3+ consecutive same hr-char, no other content)
          ───, ═══, ━━━ (Unicode box-drawing dashes)
          - - - - -, _ _ _ _ _ (spaced-out dashes)
          ···, •••, ○○○  (3+ consecutive same bullet, no other content)

        Does NOT match:
          - name: value   (YAML — mixed content)
          --flag          (CLI flag — has alphabetic suffix)
          ——              (Chinese em-dash, exactly 2 chars)
        """
        if not stripped:
            return False

        # Bullet-only rows: ···  •••  ○○○  (3+ identical bullets, nothing else)
        # Check BEFORE hr_count since bullets aren't in _HR_CHARS.
        if re.match(r"^([·•●○◉◎])\1{2,}$", stripped):
            return True

        # Quick positive check: must be mostly hr-characters
        hr_count = sum(1 for c in stripped if c in cls._HR_CHARS)
        if hr_count < 3:
            return False

        # Pure hr-character line: ---  ═══  ───  ___  (3+ of the same char)
        if re.match(r"^([_\-=])\1{2,}$", stripped):
            return True
        if re.match(r"^[─━═➖‐‑‒–—―▔▀]{3,}$", stripped):
            return True

        # Spaced hr:  - - - - -   _ _ _ _ _   · · · ·
        if re.match(r"^([_\-─━═]\s+){2,}[_\-─━═]\s*$", stripped):
            return True

        # Spaced multi-char dashes: ---     ---     ---
        if re.match(r"^(\-{2,}\s+){2,}\-{2,}\s*$", stripped):
            return True

        # Catch-all: lines predominantly composed of hr-chars (>50% ratio)
        # with little real content (< 15 non-hr, non-whitespace characters).
        # This catches VirtualScreen corruption artifacts where fragments of
        # file-path headers merge with dashes to produce mangled lines that
        # are mostly separators but don't match any clean pattern.
        non_hr_non_ws = [c for c in stripped if c not in cls._HR_CHARS and c != " "]
        if len(non_hr_non_ws) < 15 and hr_count >= len(stripped) * 0.5:
            return True

        return False

    # ⸻ Helper: file-header-line detection ─────────────────────────────

    # File extensions commonly found in Claude Code file-reading output
    _COMMON_EXTENSIONS = (
        "ts", "tsx", "js", "jsx", "json", "md", "mdx", "py", "rb", "go",
        "rs", "java", "kt", "swift", "c", "cpp", "h", "hpp", "css", "scss",
        "less", "html", "htm", "xml", "svg", "yml", "yaml", "toml", "ini",
        "cfg", "conf", "env", "sh", "bash", "zsh", "fish", "ps1", "bat",
        "sql", "graphql", "gql", "proto", "vue", "svelte", "astro",
        "dockerfile", "makefile", "gitignore", "editorconfig",
    )

    @classmethod
    def _is_file_header_line(cls, stripped: str) -> bool:
        """Detect Claude Code file-reading section headers.

        Claude Code emits these patterns when reading source code:
          --- path/to/file.ts ---
          --- src/components/Button.tsx (lines 1-50) ---

        VirtualScreen corruption (100-row PTY with 200-cols) produces
        truncated / merged variants:
          - --- file.ts --
          --- --- src/components/Button34.tsx
          .tsx ---     --- src/components/Button41
          /components/Button29.tsx ---     --- src
          ton48.tsx ---     --- src/components/But
          - src/components/Button36.tsx --- --
        """
        if not stripped or len(stripped) < 5:
            return False

        ext_pattern = "|".join(cls._COMMON_EXTENSIONS)
        _has_ext = re.search(rf"\.(?:{ext_pattern})\b", stripped, re.IGNORECASE)

        # Pattern A: "--- path.ext ---"  (clean Claude output)
        if re.match(r"^---+\s+\S+\.\S+\s+---+$", stripped):
            return True

        # Pattern B: "--- path.ext (lines N-M) ---"
        if re.match(r"^---+\s+\S+\.\S+\s+\(lines\s+\d+[-–]\d+\)\s+---+$", stripped):
            return True

        # Pattern D (new): line starts with --- and the non-dash content
        # is ONLY path-like fragments (no actual code). This catches
        # severe VirtualScreen corruption where the file extension
        # itself gets mangled (e.g. "--- src/compone  ---").
        if re.match(r"^---+\s", stripped):
            _no_dash_stripped = re.sub(r"[-─═]", "", stripped).strip()
            # Must contain some path-like content (slashes, dots)
            if _no_dash_stripped and ("/" in _no_dash_stripped or "." in _no_dash_stripped):
                # Must NOT contain code keywords
                if not re.search(
                    r'\b(import|export|const|let|var|function|class|return|if|for|while|async|await|yield|throw|try|catch)\b',
                    _no_dash_stripped,
                ):
                    # No long alphanumeric runs (> 30 chars) — these indicate code
                    if not re.search(r'[A-Za-z_][A-Za-z0-9_]{29,}', _no_dash_stripped):
                        # Content after --- is short or path-like
                        content_part = re.sub(r"^---+\s*", "", stripped)
                        if len(content_part) < 80:
                            return True

        # Pattern C: VirtualScreen corruption variants.
        # These always involve file extensions AND --- somewhere.
        if "---" not in stripped:
            return False
        if not _has_ext:
            return False

        # VirtualScreen corruption lines are short and have no
        # meaningful code content --- just file-path bits and dashes.
        # Heuristic: remove dashes, and check if what remains looks
        # like file-path fragments (no code keywords, no long words).
        _no_dash = re.sub(r"[-─═]", "", stripped).strip()
        # Must still have a file extension after dash removal
        if not re.search(rf"\.(?:{ext_pattern})\b", _no_dash, re.IGNORECASE):
            return False
        # No code keywords: import, export, const, function, class, etc
        if re.search(r'\b(import|export|const|let|var|function|class|return|if|for|while)\b', _no_dash):
            return False
        # No long alphanumeric runs (> 20 chars) — these indicate code identifiers
        if re.search(r'[A-Za-z_][A-Za-z0-9_]{19,}', _no_dash):
            return False
        # Ratio: dashes should be a significant portion (>= 15% of the line)
        if len(stripped) < 120 and len(_no_dash) <= len(stripped) * 0.85:
            return True

        return False

    # ⸻ Helper: pure-decorative-line detection ─────────────────────────

    @classmethod
    def _is_pure_decorative_line(cls, stripped: str) -> bool:
        """Check if the entire line is nothing but decorative/ornament chars."""
        if not stripped:
            return False
        for c in stripped:
            if c not in cls._DECORATIVE_CHARS and c not in cls._BOX_DRAWING_CHARS and c != " ":
                return False
        return True

    # ⸻ Helper: decorator-bullet-line detection (P2) ───────────────────

    # Unicode ranges commonly used for TUI decorations / bullets
    _DECORATOR_RANGES = (
        (0x2300, 0x23FF),   # Miscellaneous Technical (⏎ ⏵ ⏸ …)
        (0x2500, 0x257F),   # Box Drawing
        (0x2580, 0x259F),   # Block Elements
        (0x25A0, 0x25FF),   # Geometric Shapes (● ◆ ▶ …)
        (0x2600, 0x26FF),   # Misc Symbols
        (0x2700, 0x27BF),   # Dingbats (✻ ✶ …)
        (0x1F300, 0x1F5FF), # Misc Symbols & Pictographs
        (0x1F600, 0x1F64F), # Emoticons
        (0x1F680, 0x1F6FF), # Transport & Map
        (0x1F900, 0x1F9FF), # Supplemental Symbols
    )

    @classmethod
    def _is_decorator_bullet_line(cls, stripped: str) -> bool:
        """Check if line starts with a TUI-decorator/emoji and has little real text.

        Heuristic: line starts with a char in a known decorative Unicode range,
        followed by very little real content — likely a TUI menu/status item
        rather than real message content.

        ● is excluded from this check because Claude uses ● as a regular
        response bullet in rich output mode (not just TUI menus).
        """
        if not stripped or len(stripped) < 2:
            return False
        first_char = stripped[0]
        # ● (U+25CF) is legitimately used by Claude as a response bullet.
        # Don't filter lines starting with ● via this heuristic.
        if first_char == "●":
            return False
        cp = ord(first_char)
        in_decorator_range = any(lo <= cp <= hi for lo, hi in cls._DECORATOR_RANGES)
        if not in_decorator_range:
            return False
        # Only flag it if it's very short (< 40 chars) with <= 2 words
        if len(stripped) < 40 and len(stripped.split()) <= 2:
            return True
        return False

    # ── Helpers ─────────────────────────────────────────────────────────

    def _get_decoded_text(self) -> str:
        """Get decoded and ANSI-stripped text from buffer."""
        from output_parser import strip_ansi

        with self._buf_lock:
            raw = self._buffer.decode("utf-8", errors="replace")
        return strip_ansi(raw)

    @staticmethod
    def _set_pty_size(fd: int):
        """Set PTY terminal dimensions."""
        import fcntl
        import struct
        import termios

        size = struct.pack("HHHH", PTY_ROWS, PTY_COLS, 0, 0)
        try:
            fcntl.ioctl(fd, termios.TIOCSWINSZ, size)
        except OSError:
            pass

    @staticmethod
    def _echo_detected(new_data: bytes, sent_text: str) -> bool:
        """
        Check if new PTY output contains a terminal echo of the sent text.

        Three strategies, from most to least strict:
          1. Prompt character + sent_text: "❯ hello" (most reliable)
          2. Short-message leniency: messages <=3 chars checked in head
          3. Multi-line matching: each line checked separately
        """
        if not new_data or not sent_text:
            return False

        try:
            decoded = new_data.decode("utf-8", errors="replace")
        except Exception:
            return False

        from output_parser import strip_ansi
        clean = strip_ansi(decoded)

        # Strategy 1: prompt-char + sent_text (most reliable)
        for prompt_char in ("❯", ">", "▶"):
            pattern = re.escape(prompt_char) + r"\s*" + re.escape(sent_text)
            if re.search(pattern, clean, re.DOTALL):
                return True

        # Strategy 2: short messages (<=3 chars) — echo may be embedded
        if len(sent_text) <= 3:
            head = clean[:200]
            if sent_text in head:
                idx = head.find(sent_text)
                if idx == 0:
                    return True
                prefix = head[idx - 1] if idx > 0 else ""
                if prefix in (" ", ">", "❯", "▶", "\n", "\r", "\t"):
                    return True

        # Strategy 3: multi-line sent_text
        if "\n" in sent_text:
            lines = sent_text.split("\n")
            matched_lines = 0
            for line in lines:
                stripped_line = line.strip()
                if not stripped_line:
                    matched_lines += 1
                    continue
                if stripped_line in clean:
                    matched_lines += 1
            if matched_lines > len(lines) * 0.5:
                return True

        return False

    @staticmethod
    def _check_prompt_in_text(text: str) -> bool:
        """
        Check whether text contains a prompt character near the end.

        Delegates to output_parser.is_prompt_detected which handles
        --bare TUI mode correctly (skipping status/decorator lines
        like ⏵⏵automodeon that may appear after the prompt).
        """
        from output_parser import is_prompt_detected
        return is_prompt_detected(text)

    def _check_prompt_detected(self) -> bool:
        """
        Check the latest buffer content for prompt pattern.

        Reads a small tail of the buffer to avoid decoding the entire
        accumulated output on every poll iteration.
        """
        detected, _ = self._check_prompt_detected_with_pos()
        return detected

    def _check_prompt_detected_with_pos(self) -> tuple[bool, int]:
        """
        Check the latest buffer content for prompt pattern, returning
        (detected: bool, byte_position: int).

        Returns (True, position) where position is an estimate of where
        the prompt character sits in the buffer. If no prompt is detected,
        returns (False, 0).
        """
        with self._buf_lock:
            if not self._buffer:
                return False, 0
            buf_len = len(self._buffer)
            tail_bytes = self._buffer[-2048:]
            tail = tail_bytes.decode("utf-8", errors="replace")

        if not self._check_prompt_in_text(tail):
            return False, 0

        # Find the actual byte offset of the prompt character in tail_bytes
        # by searching for the prompt chars (UTF-8 encoded) in the tail
        stripped = tail.rstrip()
        if stripped and (stripped[-1] in PROMPT_CHARS or stripped in PROMPT_CHARS):
            # Find the last occurrence of any prompt char in tail
            detected_idx = -1
            for pc in PROMPT_CHARS:
                encoded = pc.encode("utf-8")
                idx = tail_bytes.rfind(encoded)
                if idx > detected_idx:
                    detected_idx = idx
            if detected_idx >= 0:
                # Position in full buffer = offset of tail start + found index
                prompt_pos = max(0, buf_len - 2048) + detected_idx
                with self._prompt_seen_lock:
                    self._prompt_seen_pos = prompt_pos
                return True, prompt_pos

        # Fallback: approximate
        return True, buf_len

    # ── Echo-aware prompt validation ────────────────────────────────────

    def _is_prompt_from_echo(
        self, prompt_pos: int, sent_text: str, watermark: int = 0
    ) -> bool:
        """
        Check if a detected prompt character (❯/▶/>) is from an echo line.

        When the user's text is echoed back by the terminal, it appears as:
            ❯ <user's sent_text>\n
        The prompt character in this echo line is NOT a real completion
        signal — it's just part of the terminal echo.

        Strategy:
          1. Decode the buffer region from the prompt position backward
             to find the full line.
          2. Check if the line containing the prompt char matches the
             echo pattern: prompt_char + optional whitespace + sent_text.
          3. Also check forward: if the echo line is the FIRST line after
             the watermark, it's likely echo.

        Returns True if the prompt appears to be from an echo line (i.e.,
        should be IGNORED as a completion signal).
        """
        if not sent_text:
            return False

        with self._buf_lock:
            if prompt_pos >= len(self._buffer):
                return False
            # Grab up to 2048 bytes before the prompt position
            start = max(0, prompt_pos - 512)
            end = min(len(self._buffer), prompt_pos + 2048)
            region = bytes(self._buffer[start:end])

        try:
            decoded = region.decode("utf-8", errors="replace")
        except Exception:
            return False

        # Find the line containing the prompt character
        # (search backward from the prompt position within this region)
        prompt_offset_in_region = prompt_pos - start
        # Find beginning of this line
        line_start = decoded.rfind("\n", 0, prompt_offset_in_region)
        if line_start < 0:
            line_start = 0
        else:
            line_start += 1  # skip the newline
        # Find end of this line
        line_end = decoded.find("\n", prompt_offset_in_region)
        if line_end < 0:
            line_end = len(decoded)

        prompt_line = decoded[line_start:line_end].strip()
        if not prompt_line:
            return False

        # Strip ANSI from the line for comparison
        from output_parser import strip_ansi as _strip_ansi
        clean_line = _strip_ansi(prompt_line)

        # Check echo pattern: prompt_char + optional whitespace + sent_text
        # The echo line typically looks like:  ❯ 你好世界
        # or with ANSI:  \x1b[1m\x1b[35m❯\x1b[39m\x1b[22m 你好世界
        for prompt_char in ("❯", ">", "▶"):
            # Pattern: starts with prompt char, then the sent text
            # (allow for ANSI rendering that puts the prompt at column 0
            # or prepended by whitespace)
            if clean_line.startswith(prompt_char):
                after_prompt = clean_line[len(prompt_char):].lstrip()
                # Check if this is the sent text
                if after_prompt == sent_text.strip():
                    logger.debug(
                        "_is_prompt_from_echo: TRUE — prompt line matches echo: %r",
                        clean_line[:80],
                    )
                    return True
                # Also check partial match for long texts that may get
                # truncated by terminal width
                if len(sent_text) > 30 and after_prompt and sent_text.strip().startswith(after_prompt[:20]):
                    logger.debug(
                        "_is_prompt_from_echo: TRUE — partial echo match (long text)",
                    )
                    return True

            # Pattern: whitespace prefix (VirtualScreen rendering may
            # put the prompt at a non-zero column after rendering)
            if clean_line.endswith(sent_text.strip()) and prompt_char in clean_line:
                # Check that the prompt char appears before the sent_text
                pc_idx = clean_line.find(prompt_char)
                sent_idx = clean_line.find(sent_text.strip())
                if 0 <= pc_idx < sent_idx:
                    logger.debug(
                        "_is_prompt_from_echo: TRUE — line ends with sent_text "
                        "and contains prompt char",
                    )
                    return True

        return False

    # _cleanup is defined above (after stop())

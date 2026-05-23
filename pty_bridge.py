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
  - Completion detection: prompt pattern + silence timeout
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

SILENCE_TIMEOUT = 10.0  # seconds of no output before declaring done (raised from 4.0)
MIN_RESPONSE_WAIT = 8.0  # minimum seconds to wait before checking completion (raised from 5.0)
COMMAND_SILENCE_MULTIPLIER = 3.0  # extra silence tolerance for command execution (raised from 2.0)
MAX_SILENCE_TIMEOUT = 30.0  # hard cap for silence timeout even with extreme burst counts
START_TIMEOUT = 60.0  # max seconds for full startup (including dialogs)
DEFAULT_RESPONSE_TIMEOUT = 0  # unlimited — wait indefinitely for Claude to finish
DIALOG_ENTER_INTERVAL = 1.5  # seconds between Enter presses during startup
PTY_ROWS = 100
PTY_COLS = 200

# Prompt pattern for completion detection — these characters mark Claude's
# ready-to-accept-input state in the PTY output.
PROMPT_CHARS = frozenset({">", "▶", "❯"})


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
        # We track consecutive reads within 500ms as a "burst" and
        # use this to extend silence timeout during command execution.
        #
        # Dynamically scales: each burst above 3 adds COMMAND_SILENCE_MULTIPLIER
        # to the effective silence timeout, up to MAX_SILENCE_TIMEOUT.
        # This allows long-running commands (cp, systemctl, npm install) to
        # complete without being cut off by the silence detector.
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

    def reset(self):
        """Reset internal state for a fresh session (call after stop)."""
        self._buffer.clear()
        self._ready = False
        self._running = False
        self._expecting_response = False
        self._send_prompt_watermark = 0
        self._output_burst_count = 0
        self._last_output_burst = 0.0
        self._prompt_ready_event.set()  # Reset to ready so next send doesn't block

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
        # --bare: minimal TUI mode (reduces ANSI clutter in PTY output)
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
            self._claude_bin, "--bare",
            "--settings", settings_path,
            "--system-prompt-file", "/root/.claude/CLAUDE.md",
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
        # Start Claude subprocess
        proc = subprocess.Popen(
            cmd,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
            preexec_fn=os.setsid,
            env=clean_env,
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

        # Startup loop: send Enter to dismiss dialogs, wait for silence
        deadline = time.monotonic() + START_TIMEOUT
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
                if now - silent_start >= SILENCE_TIMEOUT:
                    self._ready = True
                    break

            prev_len = current_len

            # Send Enter to dismiss dialogs
            if now - last_enter >= DIALOG_ENTER_INTERVAL:
                try:
                    os.write(master_fd, b"\r")
                except OSError:
                    break
                last_enter = now

            time.sleep(0.25)

        if not self._ready:
            raise RuntimeError(
                f"Claude did not show prompt within {START_TIMEOUT}s timeout"
            )

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
        # Terminal echoes user input. We wait a brief moment for the echo
        # to appear in the buffer, then advance start_pos past it so the
        # VirtualScreen render doesn't include it in the response.
        echo_skip_deadline = time.monotonic() + 3.0
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

        # ── Auth Prompt Detection (non-blocking) ────────────────────────
        # Quickly scan the buffer tail once for auth prompts, respond if
        # found. This is a brief synchronous scan, not a blocking loop.
        # The allow-list in settings.local.json is the primary mechanism;
        # this is just a safety net for prompts that slipped through.
        auth_deadline = time.monotonic() + 15.0
        while time.monotonic() + 0.3 < auth_deadline:
            if not self._running:
                break
            with self._buf_lock:
                tail = bytes(self._buffer[max(0, len(self._buffer) - 2048):])
            tail_text = tail.decode("utf-8", errors="replace")

            auth_patterns = [
                r"Allow\s+this\s+command",
                r"Type\s+y\s+to\s+approve",
                r"Approve\s+bash\s+command",
                r"Do you want to continue",
                r"\[y/N\]",
                r"\[Y/n\]",
                r"Type\s+y\s+to",
            ]
            if any(re.search(p, tail_text, re.IGNORECASE) for p in auth_patterns):
                logger.info("Auth prompt detected, auto-responding with 'y'")
                os.write(self._master_fd, b"y\r")
                time.sleep(0.5)
                break
            # Exit after first non-empty scan; the loop only retries if
            # the buffer tail was empty — give output more time to arrive.
            if tail_text.strip():
                break
            time.sleep(0.3)

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
        if cleaned and self._sent_text and len(cleaned) <= len(self._sent_text) + 10:
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
                if fallback and len(fallback) > len(cleaned):
                    return fallback

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
            if fallback:
                return fallback

        return cleaned

    # ── Reader Thread ───────────────────────────────────────────────────

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
                self._last_data_time = now

                # Track output bursts — consecutive reads within 500ms
                # indicate active output (e.g., subprocess running).
                if now - self._last_output_burst < 0.5:
                    self._output_burst_count += 1
                else:
                    self._output_burst_count = 1
                self._last_output_burst = now

            # Response completion: one of two conditions
            #    Condition A: silence + minimum wait time elapsed
            #    Condition B: prompt character detected in latest buffer chunk
            #      (only if the prompt is NEW — appeared after send() started)
            if self._expecting_response and self._last_data_time > 0:
                elapsed = now - self._send_start_time
                silence = now - self._last_data_time

                # Condition A: silence-based completion
                # Dynamically scale silence timeout based on output burst count.
                # Each burst beyond 3 indicates more complex subprocess execution.
                # Scale: base + (burst_count - 3) * multiplier, capped at MAX.
                if self._output_burst_count >= 3:
                    scale = 1.0 + (min(self._output_burst_count, 10) - 2) * (COMMAND_SILENCE_MULTIPLIER - 1.0) / 7.0
                    effective_silence = min(SILENCE_TIMEOUT * scale, MAX_SILENCE_TIMEOUT)
                    effective_min_wait = min(MIN_RESPONSE_WAIT * (1.0 + scale * 0.3), MAX_SILENCE_TIMEOUT * 0.8)
                else:
                    effective_silence = SILENCE_TIMEOUT
                    effective_min_wait = MIN_RESPONSE_WAIT

                silence_trigger = elapsed >= effective_min_wait and silence >= effective_silence

                # Condition B: prompt-based completion
                #    Check the last line of current buffer for prompt character
                #    Only accept prompt if it was detected AFTER send() started
                #    (avoids stale-prompt race from previous response)
                prompt_trigger = False
                if elapsed >= 0.5:
                    prompt_detected, prompt_pos = self._check_prompt_detected_with_pos()
                    if prompt_detected and prompt_pos > self._send_prompt_watermark:
                        prompt_trigger = True

                if silence_trigger or prompt_trigger:
                    self._response_event.set()

            # ── Prompt-ready detection (for input-stacking prevention) ──
            # Set prompt_ready_event when Claude shows its prompt AND we
            # are NOT currently waiting for a send() response. This signals
            # to the next send() caller that Claude is ready to accept input.
            if not self._expecting_response:
                prompt_detected, _ = self._check_prompt_detected_with_pos()
                if prompt_detected:
                    self._prompt_ready_event.set()

        # Reader thread exiting
        self._running = False
        self._ready = False
        self._response_event.set()  # Unblock any waiter

    def _wait_for_response(self, timeout: float):
        """Block until response completion or timeout. Returns False if timed out.

        NOTE: The _response_event is cleared in send() BEFORE the PTY write.
        The reader thread will set it again when sufficient response has been
        collected (prompt detected OR silence timeout). This avoids the race
        where the reader sets the event before _wait_for_response clears it.
        """
        start_wait = time.monotonic()
        while self._response_event.wait(timeout=1.0) is False:
            # Check if the bridge is still running
            if not self._running:
                return False
            # Check overall timeout
            if time.monotonic() - start_wait >= timeout:
                logger.warning(
                    "Response timeout after %.1fs (silence-based detection may have failed)",
                    timeout,
                )
                return False
        return True

    # ── Output Cleaning ─────────────────────────────────────────────────

    @staticmethod
    def _is_status_line_only(text: str) -> bool:
        """Check if stripped text is just a status/duration line."""
        return bool(
            re.search(r"(✻|✶|\*)\s+(Brewed|Cogitated|Churned|Thought|Run|Ran)\s+for", text)
            or re.match(r"^Found\s+\d+\s+settings\s+issues", text)
            or re.match(r"^⚠️\s+", text)
        )

    @staticmethod
    def _clean_output(rendered: str) -> str:
        """Clean up TUI artifacts, protocol sections, and normalize formatting."""
        lines = rendered.split("\n")
        # Keep a copy of early-pass lines for the safety fallback below
        _all_lines = list(lines)
        filtered = []

        # Protocol section tracking: skip everything between "## Protocol"
        # and the next top-level section heading.
        in_protocol = False
        # Summary section suppressing: once we hit ## Summary, suppress
        # until next ## heading or end
        suppress_summary = False

        for line in lines:
            stripped = line.strip()

            # ── Detect and skip spinner/braille progress lines ──
            if re.match(r"^[⠁-⣿](\s+\S+){0,4}$", stripped) and len(stripped) < 40:
                continue
            if re.match(r"^Waiting\.\.\.?$", stripped):
                continue
            if re.match(r"^[⠁-⣿]$", stripped):
                continue

            # ── Detect protocol section entry/exit ──
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

                # Skip protocol artifacts
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

                # Non-artifact text — exit protocol filter
                in_protocol = False

            # ── Detect and skip summary section ──
            if re.match(r"^##\s+Summary$", stripped):
                suppress_summary = True
                continue
            if suppress_summary:
                if re.match(r"^##\s", stripped):
                    suppress_summary = False
                    filtered.append(line)
                    continue
                if re.match(r"^[─\-═]{3,}", line) or not stripped:
                    continue
                if re.match(r"^[•·●]\s*(Tool|File|Script|Error|User|Exit|Command)", stripped):
                    continue
                if re.match(r"^\d+\s+\w+\s+for\s", stripped):
                    continue
                continue  # suppress everything until next heading

            # ── Line-level filters ──

            # Table/markdown table lines — Telegram can't render tables
            if re.match(r"^\s*\|.*\|\s*$", stripped):
                continue
            if re.match(r"^\s*\|[\-\s:]+\|\s*$", stripped):
                continue

            # Markdown-style separator lines: --- === ─── ═══ (3+ chars)
            if re.match(r"^[─\-═]{3,}$", stripped):
                continue
            if re.match(r"^={3,}$", stripped):
                continue
            if re.match(r"^[─\-═━]{10,}", line):
                continue
            if re.match(r"^\s*\?\s+for\s+shortcuts", line):
                continue
            if re.match(r"^\s*Press\s+\w+\s+for\s", line):
                continue
            if re.match(r"^[❯>▶]\s+", line):
                continue
            if re.match(r"^\s*[❯>▶]\s*$", line):
                continue
            # Be careful with ● — Claude may use it as a response bullet.
            # Only suppress it if it's clearly a menu/TUI item.
            if re.match(r"^●\s*(Tool|File|Script|Error|User|Exit|Command|Mode|Status)", stripped):
                continue
            if re.match(r"^\s*[⎿└├│┌┐┘└├┤┬┴┼╭╮╰╯]\s", line):
                continue
            # Rows that are mostly box-drawing chars (tree diagrams, etc.)
            if len(stripped) > 0 and sum(1 for c in stripped if c in '┌┐└┘├┤┬┴┼│─═╭╮╰╯') / max(len(stripped), 1) > 0.3:
                continue
            if stripped in ("Waiting…", "Working…", "Thinking…"):
                continue
            if re.match(r"^(Waiting|Working|Thinking)[…\.]+", stripped):
                continue
            # Multi-level indented tree structures (replaced with list)
            if re.match(r"^ {12,}(├|└|│|─)", line):
                continue

            if re.match(r"^\s{180,}$", line):
                continue
            if any(hint in line for hint in (
                "Esc to cancel", "Tab to amend", "ctrl+e to explain", "Return to confirm",
                'type "continue"', "Ctrl+C to cancel",
            )):
                continue
            if re.search(r"ctrl\+o", stripped) or re.search(r"\(\+\d+\s+lines?\)", stripped):
                continue
            # Filter out Claude's status lines (think/reason/run duration)
            if re.search(r"(✻|✶|\*)\s+(Brewed|Cogitated|Churned|Thought|Run|Ran)\s+for", stripped):
                continue
            if stripped.startswith("✻") and ("for" in stripped) and len(stripped) < 40:
                continue
            if re.match(r"^###\s+\d+\.\s", stripped):
                continue
            if re.match(r"^Found\s+\d+\s+settings\s+issues", stripped):
                continue
            if re.match(r"^\s*Exit\s+code:\s+\d+", line):
                continue
            if re.match(r"^[🔃🔄⏳⏺✅❌⚠️]\s", line):
                continue
            if re.match(r"^\s*\d+\s+files?", stripped):
                continue
            # ── Additional decorative unicode filtering ──
            # Strip lines that are PURELY decorative unicode characters
            # (box-drawing, geometric shapes, presentation emoji)
            if re.match(r"^[┏┓┗┛┣┫┳┻╋◆◇▸▹▪▫▴▾◂▸⬤●○◎◉◈⬡⬢⬣▶▷▲▼◀◁]+$", stripped):
                continue
            # Lines starting with decorative triangle/bullet sequences (not content)
            if re.match(r"^(▸|▹|▪|▫|◆|◇)\s{0,2}$", stripped):
                continue
            # ANSI escape code remnants (bare \x1b sequences that survived stripping)
            if "\x1b" in stripped or "\033" in stripped or "\e" in stripped:
                continue
            # Lines that are just repeated decorative characters (not content)
            if re.match(r"^[─━═➖➖‐‑‒–—―▔▀]{3,}$", stripped):
                continue
            if re.match(r"^[·•●○◉◎](\s*[·•●○◉◎]){3,}$", stripped):
                continue

            filtered.append(line)

        rendered = "\n".join(filtered)

        # Compress multiple spaces to one (ANSI cursor artifacts)
        rendered = re.sub(r" {2,}", " ", rendered)

        # Strip trailing prompt characters
        rendered = re.sub(r"\s*[❯>▶]\s*$", "", rendered)

        # Clean up common response line prefixes from Claude Code TUI
        #  "● Hi there" → "Hi there"
        rendered = re.sub(r"^●\s+", "", rendered, flags=re.MULTILINE)

        # Remove leading/trailing blank lines
        rendered = rendered.strip()

        # Collapse 3+ consecutive blank lines to 2
        rendered = re.sub(r"\n{4,}", "\n\n\n", rendered)

        # SAFETY: never return completely empty if we filtered too aggressively.
        # Fall back to a minimal cleanup of the raw rendered text.
        if not rendered:
            # Minimal cleanup: just strip prompt chars and obvious artifacts
            minimal = []
            for line in _all_lines:
                s = line.strip()
                if not s:
                    continue
                if re.match(r"^[─\-═━]{10,}", s):
                    continue
                if re.match(r"^\s*[❯>▶]\s*$", s):
                    continue
                if re.match(r"^\s*\?\s+for\s+shortcuts", s):
                    continue
                minimal.append(line)
            rendered = "\n".join(minimal)
            rendered = re.sub(r" {2,}", " ", rendered)
            rendered = rendered.strip()

        return rendered

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
        Check whether the last significant line of text contains a prompt character.

        Uses the same prompt characters defined in PROMPT_CHARS.
        An empty or whitespace-only buffer means prompt not detected.
        """
        stripped = text.rstrip()
        if not stripped:
            return False
        # Check last line for prompt character at end or as the only content
        last_line = stripped.split("\n")[-1].strip()
        if not last_line:
            return False
        return last_line[-1] in PROMPT_CHARS or last_line in PROMPT_CHARS

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

    def _cleanup(self):
        """Close PTY master fd."""
        self._ready = False
        self._running = False
        if self._master_fd is not None:
            try:
                os.close(self._master_fd)
            except OSError:
                pass
            self._master_fd = None
        self._process = None

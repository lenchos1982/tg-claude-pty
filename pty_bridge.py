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

SILENCE_TIMEOUT = 4.0  # seconds of no output before declaring done
MIN_RESPONSE_WAIT = 5.0  # minimum seconds to wait before checking completion
START_TIMEOUT = 60.0  # max seconds for full startup (including dialogs)
DEFAULT_RESPONSE_TIMEOUT = 0  # unlimited — wait indefinitely for Claude to finish
DIALOG_ENTER_INTERVAL = 1.5  # seconds between Enter presses during startup
PTY_ROWS = 100
PTY_COLS = 200

# ── Stagnation detection ────────────────────────────────────────────────────
# These avoid the spinner-bypass bug where silence timeout never fires.
STAGNATION_GROWTH_THRESHOLD = 128   # bytes: meaningful content growth floor
STAGNATION_GROWTH_TIMEOUT = 15.0    # seconds: no meaningful growth → complete
IDLE_TIMEOUT = 8.0                  # seconds: no PTY events + stagnant buffer → complete

# ── Condition E: Effective content accumulation completion ─────────────────
# Monitors the raw buffer for "effective content bytes" — bytes that are NOT
# spinner ticks, ANSI cursor movements, bare whitespace, or control chars.
# This is more sensitive than raw-byte growth because DeepSeek spinner ticks
# (braille characters) are excluded from the count.
EFFECTIVE_GROWTH_THRESHOLD = 32   # bytes: effective content growth floor
EFFECTIVE_GROWTH_TIMEOUT = 10.0    # seconds: no effective growth → complete

# ── Condition F: Max-wait safety net ───────────────────────────────────────
# Absolute safety net: when SEND_TIMEOUT is reached, return whatever content
# has been collected so far regardless of completion state.
# This is NOT a new constant — it uses the existing SEND_TIMEOUT passed to send().

# ── Reader thread watchdog ─────────────────────────────────────────────────
READER_HEARTBEAT_INTERVAL = 5.0  # seconds between reader heartbeat updates

# Prompt pattern for completion detection — these characters mark Claude's
# ready-to-accept-input state in the PTY output.
PROMPT_CHARS = frozenset({">", "▶", "❯"})

# ── Test/output detection patterns (used in _clean_output and logging) ────
_TRACEBACK_RE = re.compile(r"Traceback\s+\(most recent call last\):")
_BASH_TEST_RE = re.compile(r"(python3|python)\s+-c\s+[\"']")
_ERROR_OUTPUT_RE = re.compile(r"(Error|Exception|KeyError|ValueError|TypeError|AttributeError|ImportError|ModuleNotFoundError):")


def _is_test_or_error_output(text: str) -> bool:
    """Quick heuristic: check if text looks like test output or error traceback."""
    if _TRACEBACK_RE.search(text):
        return True
    if _BASH_TEST_RE.search(text):
        return True
    # Check for common error patterns in first 200 chars
    head = text[:200]
    if _ERROR_OUTPUT_RE.search(head):
        return True
    return False


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

        # Response tracking
        self._expecting_response = False
        self._last_data_time = 0.0
        self._response_event = threading.Event()
        self._send_start_time = 0.0  # Timestamp when send() started
        self._send_start_pos = 0  # Buffer position when send() started
        self._send_timeout_max = float("inf")  # Condition F max-wait deadline

        # Prompt tracking: prevent stale-prompt detection bug
        # When send() starts, we note the current buffer end and stop
        # detecting prompts that were already in the buffer before the
        # new send started.
        self._prompt_seen_pos = 0  # Buffer position of last detected prompt
        self._prompt_seen_lock = threading.Lock()
        self._send_prompt_watermark = 0  # Buffer length at send() start

        # Completion reason tracking: set by reader thread when it signals
        # completion. Used in send() to add context-specific notes.
        self._completion_reason = "unknown"

        # Stagnation detection state (reader thread)
        self._no_pollin_since = 0.0      # timestamp when PTY first stopped yielding data
        self._stagnation_buf_len = 0      # buffer length when stagnation window started
        self._last_growth_time = 0.0      # timestamp of last meaningful (> THRESHOLD) growth
        self._last_growth_buf_len = 0     # buffer length at last meaningful growth event

        # Condition E: Effective content accumulation (reader thread)
        # Tracks growth of "meaningful" content only, excluding spinner ticks
        # and ANSI cursor movements that DeepSeek produces persistently.
        self._last_effective_content_len = 0   # effective bytes at last check
        self._last_effective_growth_time = 0.0 # timestamp of last effective growth

        # Reader thread watchdog / heartbeat
        self._reader_heartbeat_time = 0.0   # last heartbeat timestamp
        self._reader_heartbeat_lock = threading.Lock()

        # Async bridge (set during start())
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    # ── Properties ──────────────────────────────────────────────────────

    @property
    def is_ready(self) -> bool:
        return self._ready

    @property
    def is_running(self) -> bool:
        return self._running

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

        # Disable PTY echo on the master fd.
        # Without this, typed input is echoed back into the PTY output stream,
        # causing the completion detector to see the echo as "valid response"
        # and triggering premature completion before Claude actually replies.
        import termios
        try:
            attrs = termios.tcgetattr(master_fd)
            # c_lflag: local mode flags
            attrs[3] &= ~(termios.ECHO | termios.ECHOE | termios.ECHOK | termios.ECHONL)
            termios.tcsetattr(master_fd, termios.TCSANOW, attrs)
        except OSError:
            logger.warning("Failed to disable PTY echo on master fd %s", master_fd)

        # Build claude command with optional session ID
        # No --bare flag: use normal interactive mode so Claude loads OAuth login
        # The PTY startup loop + reader thread handles dialog dismissal automatically
        cmd = [self._claude_bin, "--settings", os.path.join(os.path.dirname(os.path.abspath(__file__)), ".claude/settings.local.json")]
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
        # Also detects auth/consent prompts and auto-responds (#10, #11)
        deadline = time.monotonic() + START_TIMEOUT
        last_enter = 0.0
        prev_len = 0
        silent_start = None
        _startup_prompt_detected = False

        while time.monotonic() < deadline:
            now = time.monotonic()

            if proc.poll() is not None:
                self._running = False
                raise RuntimeError(
                    f"Claude process exited during startup (rc={proc.returncode})"
                )

            with self._buf_lock:
                current_len = len(self._buffer)

            # ── Detect prompt character in output (#10) ──
            # In addition to the silence+1000-chars check, detect when Claude
            # has reached its prompt character. This handles cases where Claude
            # outputs a prompt without silence (e.g. after dismissing a dialog).
            if not _startup_prompt_detected and current_len > 100:
                with self._buf_lock:
                    _startup_tail = self._buffer[-2048:]
                if _startup_tail:
                    _startup_text = _startup_tail.decode("utf-8", errors="replace")
                    if self._check_prompt_in_text(_startup_text):
                        _startup_prompt_detected = True
                        logger.info("Prompt character detected during startup")

            # Original silence-based detection
            if current_len > prev_len:
                silent_start = None
            elif current_len > 1000 and silent_start is None:
                silent_start = now
            elif current_len > 1000 and silent_start is not None:
                if now - silent_start >= SILENCE_TIMEOUT:
                    self._ready = True
                    break

            # Prompt-based detection: exit startup loop early if prompt detected AND
            # buffer is large enough (avoid false positive on tiny output)
            if _startup_prompt_detected and current_len > 500:
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

    def reset(self):
        """
        Reset all internal state without touching the PTY or process.
        Call AFTER stop() and BEFORE start() to fully reset for a /new session.
        """
        self._ready = False
        self._running = False
        with self._buf_lock:
            self._buffer = bytearray()
        self._send_start_pos = 0
        self._send_prompt_watermark = 0
        self._last_growth_time = 0.0
        self._last_growth_buf_len = 0
        self._last_effective_growth_time = 0.0
        self._last_effective_content_len = 0
        self._no_pollin_since = 0.0
        self._stagnation_buf_len = 0
        self._last_data_time = 0.0
        self._expecting_response = False
        self._response_event.clear()

    # ── Send / Receive ──────────────────────────────────────────────────

    async def send(
        self, text: str, timeout: float = DEFAULT_RESPONSE_TIMEOUT
    ) -> Optional[str]:
        """
        Send text to Claude Code and return the response.

        Coroutine. Writes to PTY master fd, waits for completion event.
        """
        if not self._ready:
            raise RuntimeError("PtyBridge is not ready. Call start() first.")

        # Record starting position and time
        with self._buf_lock:
            start_pos = len(self._buffer)
        self._send_start_pos = start_pos
        self._send_start_time = time.monotonic()
        # Condition F: set max-wait threshold
        # If timeout is 0 (unlimited), use inf; otherwise convert to elapsed-since-start
        self._send_timeout_max = timeout if timeout > 0 else float("inf")

        # Set a watermark: the buffer position at send() start.
        # The reader thread will ONLY signal prompt-based completion
        # if the prompt was detected in content written AFTER this
        # marker position. This prevents the stale-prompt race where
        # the old prompt from a previous response immediately triggers
        # completion of the new request.
        self._send_prompt_watermark = start_pos
        self._completion_reason = "unknown"

        # ── Reset stagnation/effective tracking for fresh response ──
        # Without this, stale state from the previous response causes
        # premature completion: the reader thread sees "no growth" based
        # on _last_growth_time from the last send(), even though the new
        # request has just been written and output hasn't started yet.
        now = time.monotonic()
        self._last_growth_time = 0.0
        self._last_growth_buf_len = 0
        self._last_effective_growth_time = 0.0
        self._last_effective_content_len = 0
        # Reset idle tracking so Condition C doesn't fire on stale no-pollin state
        self._no_pollin_since = 0.0
        self._stagnation_buf_len = start_pos

        # ── Log start state for diagnostics ──
        logger.info(
            "send() start: text_len=%d, buf_start=%d, total_buf=%d",
            len(text), start_pos,
            len(self._buffer) if hasattr(self, '_buffer') else 0
        )

        # Setup for response tracking
        self._expecting_response = True
        self._last_data_time = time.monotonic()
        # Clear the event INSIDE the lock holder (send), not in
        # _wait_for_response, to avoid the race where the reader
        # thread sets the event between clear() and the PTY write.
        self._response_event.clear()

        # Write to PTY master (\r = Enter in raw terminal mode)
        os.write(self._master_fd, (text + "\r").encode())

        # Wait for completion
        loop = asyncio.get_running_loop()
        try:
            completed = await loop.run_in_executor(None, self._wait_for_response, timeout)
        except asyncio.CancelledError:
            self._expecting_response = False
            raise

        self._expecting_response = False

        if not completed:
            logger.warning("send() timed out or bridge dead — no response available")
            return None

        if not self._running:
            return None

        # Render new content via VirtualScreen (preserves cursor-positioned text)
        with self._buf_lock:
            new_bytes = bytes(self._buffer[start_pos:])

        raw = new_bytes.decode("utf-8", errors="replace")
        logger.debug("send() raw (%d b): %s", len(new_bytes), raw[:500])

        screen = VirtualScreen(rows=PTY_ROWS, cols=PTY_COLS)
        rendered = screen.render(raw)

        # Log pre/post clean lengths
        pre_clean_len = len(rendered.strip())

        # Clean up TUI artifacts and normalize formatting
        rendered = self._clean_output(rendered)

        post_clean_len = len(rendered.strip())
        logger.info(
            "send() clean stats: start_pos=%d, new_bytes=%d, "
            "pre_clean=%d, post_clean=%d, total_buf=%d",
            start_pos, len(new_bytes),
            pre_clean_len, post_clean_len,
            len(self._buffer) if hasattr(self, '_buffer') else 0
        )

        # ── Log warning if output looks like test output or traceback ──
        if rendered.strip():
            first_500 = rendered.strip()[:500]
            if _is_test_or_error_output(first_500):
                logger.warning(
                    "send() returning output that looks like test/error content (len=%d): %s",
                    len(rendered.strip()), first_500[:300]
                )

        # Safety check: if after cleaning we only got a short status line
        # (like "✻ Churned for 0s"), it means we likely captured too little
        # content (stale-prompt race or similar). Trim the raw content to
        # exclude any residual trash from before send() that VirtualScreen
        # might have rendered, and try again.
        cleaned = rendered.strip()
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

        # Condition F max-wait: append a note about possible incomplete content
        if self._completion_reason == "max_wait" and cleaned:
            elapsed_min = (time.monotonic() - self._send_start_time) / 60.0
            note = (
                f"\n\n---\n"
                f"⏱️ 已等待 {elapsed_min:.0f} 分鐘，內容可能不完整。"
                f" 如需更完整的回覆，請重發請求。"
            )
            cleaned = cleaned + note

        return cleaned

    # ── Reader Thread ───────────────────────────────────────────────────

    def _reader_loop(self):
        """Continuously read PTY output, respond to DA queries."""
        poll = select.poll()
        poll.register(self._master_fd, select.POLLIN)

        # Effective content tracking: periodically recompute effective bytes
        # from the full buffer (expensive, so rate-limited)
        _effective_check_counter = 0

        while self._running and self._master_fd is not None:
            try:
                # ── Heartbeat ────────────────────────────────────────────────
                with self._reader_heartbeat_lock:
                    self._reader_heartbeat_time = time.monotonic()

                events = poll.poll(250)
            except (ValueError, OSError):
                break
            except Exception as exc:
                logger.error("Reader thread poll error: %s", exc, exc_info=True)
                break

            now = time.monotonic()

            if events:
                # Reset the no-pollin tracker — we're still getting PTY events
                self._no_pollin_since = 0.0
                try:
                    data = os.read(self._master_fd, 4096)
                except (OSError, ValueError):
                    break
                except Exception as exc:
                    logger.error("Reader thread read error: %s", exc, exc_info=True)
                    break

                if not data:
                    logger.warning("EOF on PTY master fd — Claude process exited")
                    break

                # Respond to terminal DA queries (e.g. Device Attributes, CPR)
                try:
                    respond_da(data, self._master_fd)
                except Exception as exc:
                    logger.error("Reader thread respond_da error: %s", exc, exc_info=True)

                # Append to buffer
                with self._buf_lock:
                    old_len = len(self._buffer)
                    self._buffer.extend(data)
                    new_len = len(self._buffer)
                self._last_data_time = now

                # Growth-based stagnation: track when buffer last grew meaningfully
                # This catches spinner-only output where small writes (< threshold)
                # keep coming indefinitely (spinner ticks) but no real content grows.
                if self._expecting_response:
                    bytes_grown = new_len - old_len
                    if bytes_grown >= STAGNATION_GROWTH_THRESHOLD:
                        self._last_growth_time = now
                        with self._buf_lock:
                            self._last_growth_buf_len = new_len

            else:
                # No data available this poll cycle
                # Start/reset no-pollin stagnation tracking
                if self._expecting_response:
                    now = time.monotonic()
                    elapsed = now - self._send_start_time
                    if elapsed >= MIN_RESPONSE_WAIT:
                        if self._no_pollin_since == 0.0:
                            self._no_pollin_since = now
                            with self._buf_lock:
                                self._stagnation_buf_len = len(self._buffer)

            # ── Response completion check ──────────────────────────────────────
            # One of six conditions can signal completion:
            #   A: silence + minimum wait
            #   B: prompt detected (new content after send watermark)
            #   C: idle (no POLLIN events + stagnant buffer)
            #   D: growth-stagnation (no raw-byte growth above threshold)
            #   E: effective-content accumulation (no effective content growth)
            #   F: max-wait timeout (absolute safety net)
            #
            # Conditions A-D are fallbacks from the original code (useful for
            # native Claude CLI). Conditions E and F are the primary DeepSeek
            # fixes: E handles spinner-only output more sensitively, and F
            # guarantees we never hang forever.
            if self._expecting_response and self._last_data_time > 0:
                now = time.monotonic()
                elapsed = now - self._send_start_time
                silence = now - self._last_data_time

                # Condition A: silence-based completion
                silence_trigger = (
                    elapsed >= MIN_RESPONSE_WAIT
                    and silence >= SILENCE_TIMEOUT
                )

                # Condition B: prompt-based completion
                #    Check the last line of current buffer for prompt character
                #    Only accept prompt if it was detected AFTER send() started
                #    (avoids stale-prompt race from previous response)
                prompt_trigger = False
                if elapsed >= 0.5:
                    try:
                        prompt_detected, prompt_pos = self._check_prompt_detected_with_pos()
                        if prompt_detected and prompt_pos > self._send_prompt_watermark:
                            prompt_trigger = True
                    except Exception as exc:
                        logger.error("Reader thread prompt detection error: %s", exc, exc_info=True)

                # Condition C: idle completion (stagnation detection)
                #    Fires when:
                #      1. No POLLIN events for >= IDLE_TIMEOUT seconds
                #         (PTY master fd has no data to read)
                #      2. Buffer size has not changed during that idle window
                #         (output is genuinely stagnant — we didn't miss an event)
                #      3. At least MIN_RESPONSE_WAIT has elapsed since send()
                #    This catches the case where Claude finishes its output,
                #    returns to the prompt, but the prompt is not detected
                #    due to ANSI artifacts.
                idle_trigger = False
                if (
                    elapsed >= MIN_RESPONSE_WAIT
                    and self._no_pollin_since > 0
                ):
                    idle_duration = now - self._no_pollin_since
                    if idle_duration >= IDLE_TIMEOUT:
                        # Double-check buffer hasn't grown
                        with self._buf_lock:
                            current_buf_len = len(self._buffer)
                        if current_buf_len == self._stagnation_buf_len:
                            idle_trigger = True
                            logger.info(
                                "Condition C (idle) triggered after %.1fs of no PTY events "
                                "and stagnant buffer (buf=%d b)",
                                idle_duration, current_buf_len
                            )

                # Condition D: growth-stagnation completion
                #    Fires when Claude's output has slowed to spinner-only garbage.
                #    Even though PTY events keep arriving (spinner ticks),
                #    the buffer has NOT grown by >= STAGNATION_GROWTH_THRESHOLD bytes
                #    for STAGNATION_GROWTH_TIMEOUT seconds. This means the output
                #    is just spinner garbage, not real content.
                growth_trigger = False
                if (
                    elapsed >= MIN_RESPONSE_WAIT
                    and self._last_growth_time > 0
                    and (now - self._last_growth_time) >= STAGNATION_GROWTH_TIMEOUT
                ):
                    # Double-check buffer hasn't grown meaningfully
                    with self._buf_lock:
                        current_buf_len = len(self._buffer)
                    if (current_buf_len - self._last_growth_buf_len) < STAGNATION_GROWTH_THRESHOLD:
                        growth_trigger = True
                        logger.info(
                            "Condition D (growth-stagnation) after %.1fs "
                            "(no raw growth >= %d b in %.1fs, buf=%d b)",
                            elapsed, STAGNATION_GROWTH_THRESHOLD,
                            STAGNATION_GROWTH_TIMEOUT, current_buf_len
                        )

                # ── Condition E: Effective content accumulation ──────────────
                #    Unlike Condition D (raw-byte growth), this measures content
                #    growth excluding spinner ticks, ANSI noise, and bare
                #    whitespace/control chars. More sensitive than D because
                #    DeepSeek spinner ticks are excluded from the byte count.
                #    Rate-limited: recompute effective bytes every ~5 poll cycles
                #    (~1.25s) because it requires full-buffer decode+ANSI-strip.
                effective_trigger = False
                _effective_check_counter += 1
                if (
                    elapsed >= MIN_RESPONSE_WAIT
                    and self._last_effective_growth_time > 0
                    and _effective_check_counter % 5 == 0
                ):
                    with self._buf_lock:
                        eff_len = self._count_effective_content(bytes(self._buffer))
                    prev_eff = self._last_effective_content_len
                    if (eff_len - prev_eff) < EFFECTIVE_GROWTH_THRESHOLD:
                        eff_silent = now - self._last_effective_growth_time
                        if eff_silent >= EFFECTIVE_GROWTH_TIMEOUT:
                            effective_trigger = True
                            logger.info(
                                "Condition E (effective-content) after %.1fs "
                                "(eff_len=%d, growth=%d b in %.1fs)",
                                elapsed, eff_len, eff_len - prev_eff, eff_silent
                            )
                    else:
                        self._last_effective_content_len = eff_len
                        self._last_effective_growth_time = now

                # Initialize effective tracking on first check after send()
                if self._last_effective_growth_time == 0.0 and elapsed >= MIN_RESPONSE_WAIT:
                    with self._buf_lock:
                        self._last_effective_content_len = (
                            self._count_effective_content(bytes(self._buffer))
                        )
                    self._last_effective_growth_time = now

                # ── Condition F: Max-wait safety net ─────────────────────────
                #    Absolute safety net: if SEND_TIMEOUT is reached, signal
                #    completion regardless. This ensures we never hang forever
                #    even if ALL other conditions fail.
                max_wait_trigger = False
                if elapsed >= self._send_timeout_max:
                    max_wait_trigger = True
                    logger.warning(
                        "Condition F (max-wait) after %.1fs — returning "
                        "whatever content has been collected",
                        elapsed
                    )

                if silence_trigger or prompt_trigger or idle_trigger or growth_trigger or effective_trigger or max_wait_trigger:
                    # Record which condition triggered for diagnostic context
                    if max_wait_trigger:
                        self._completion_reason = "max_wait"
                    elif effective_trigger:
                        self._completion_reason = "effective_stagnation"
                    elif growth_trigger:
                        self._completion_reason = "growth_stagnation"
                    elif idle_trigger:
                        self._completion_reason = "idle"
                    elif prompt_trigger:
                        self._completion_reason = "prompt"
                    elif silence_trigger:
                        self._completion_reason = "silence"
                    self._response_event.set()

        # Reader thread exiting
        logger.warning("Reader thread exiting (running=%s, master_fd=%s)",
                       self._running, self._master_fd)
        self._running = False
        self._ready = False
        self._response_event.set()  # Unblock any waiter

    def _wait_for_response(self, timeout: float):
        """Block until response completion or timeout. Returns False if timed out.

        NOTE: The _response_event is cleared in send() BEFORE the PTY write.
        The reader thread will set it again when sufficient response has been
        collected (prompt detected OR silence timeout). This avoids the race
        where the reader sets the event before _wait_for_response clears it.

        timeout=0 means unlimited wait (no deadline).
        """
        start_wait = time.monotonic()
        while self._response_event.wait(timeout=1.0) is False:
            # Check if the bridge is still running
            if not self._running:
                return False
            # Check overall timeout (0 = unlimited)
            if timeout > 0 and (time.monotonic() - start_wait) >= timeout:
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

        # ── Python traceback / test command block filtering ──
        # Some residual PTY content contains "Traceback (most recent call last):"
        # blocks or "python3 -c " test commands that were left in the buffer.
        # These must never leak to the user. We track a "skip block" state that
        # suppresses everything until we're clearly past the error block.
        _skip_block = False       # True while actively skipping a traceback/test block
        _skip_block_depth = 0     # indentation depth of block being skipped

        for line in lines:
            stripped = line.strip()

            # ── Detect start of Python traceback block ──
            if re.search(r"Traceback\s+\(most recent call last\):", stripped):
                _skip_block = True
                _skip_block_depth = len(line) - len(line.lstrip())
                continue

            # ── Detect start of bash test command block ──
            if re.match(r"^(python3|python)\s+-c\s+[\"']", stripped):
                _skip_block = True
                _skip_block_depth = len(line) - len(line.lstrip())
                continue

            # ── Suppress lines inside a skip block ──
            if _skip_block:
                # Exit skip block when we hit a line with LESS indentation than
                # the block start (not a continuation), OR an empty line followed
                # by a non-traceback-looking line in the next iteration.
                # Traceback blocks end at the line with the actual exception name
                # which typically has less indentation than inner frames.
                current_indent = len(line) - len(line.lstrip())
                if current_indent < _skip_block_depth and stripped:
                    _skip_block = False  # Exit skip mode
                else:
                    # Still in the error block — skip this line
                    # Exit also on empty lines that aren't continuation
                    if not stripped:
                        _skip_block = False
                    continue

            # ── Detect and skip spinner/braille progress lines ──
            if re.match(r"^[⠁-⣿](\s+\S+){0,4}$", stripped) and len(stripped) < 40:
                continue
            if re.match(r"^Waiting\.\.\.?$", stripped):
                continue
            if re.match(r"^[⠁-⣿]$", stripped):
                continue

            # ── Detect and skip auth/consent prompt menus ──
            # When Claude asks "Do you want to proceed?" with numbered options,
            # this is not output for the user — it's a CLI interaction.
            # Skip these lines.
            if re.match(r"^Do you want to proceed\?$", stripped, re.IGNORECASE):
                # Track that we're in an auth prompt section
                continue
            if re.match(r"^\d+\.\s+(Yes|No|Allow|Reject|Skip)", stripped, re.IGNORECASE):
                continue
            if re.match(r"^Yes,\s+allow", stripped, re.IGNORECASE):
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

            # ── Filter out terminal echo lines ──
            # The PTY echoes user input back as terminal echo. Common patterns:
            #   1. "❯ <command>" — Claude CLI prompt prefix + command
            #   2. "$ <command>" — raw shell prompt echo (no ❯ prefix)
            #   3. Unquoted command echoed before execution in raw PTY mode
            #
            # These are lines where the PTY echoes the user's input back.
            # We skip lines matching these echo patterns. Length check prevents
            # accidentally filtering actual content.
            if re.match(r"^❯\s+\S", stripped) and len(stripped) < 200:
                continue
            # Filter "$ command" / "# command" shell-prompt echoes
            if re.match(r"^\$\s+\S", stripped) and len(stripped) < 200:
                continue
            if re.match(r"^#\s+\S", stripped) and len(stripped) < 200:
                continue
            # Filter raw bare-command echo lines: short lines that look like
            # a shell command typed at a prompt (starts with common command
            # word, has no output markers). Very conservative to avoid
            # filtering actual content.
            if (re.match(r"^>>?\s+", stripped) or re.match(r"^>\s+", stripped)) and len(stripped) < 200:
                continue

            # ── Filter Claude Code tool invocation blocks ──
            # Claude Code CLI prints tool calls like "Bash(...)" as part of
            # its protocol. These should NOT be shown to the user.
            if re.match(r"^(Bash|Read|Write|Edit|Web|WebSearch|FileEdit|Pattern|Grep|Search|View)\s*\(", stripped):
                continue
            if re.match(r"^\s*Bash\(echo", stripped):
                continue
            # Filter "Contains ..." analysis lines from Claude Code TUI
            if re.match(r"^Contains\s+(simple_expansion|literal|user_input|path|script)", stripped):
                continue
            # Filter question-mark menu shortcuts
            if re.match(r"^\s*\?\s+(Tool|Command|File|Search)", stripped):
                continue

            # ── Line-level filters ──
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
            if re.match(r"^\s*[⎿└├│]\s", line):
                continue
            if stripped in ("Waiting…", "Working…", "Thinking…"):
                continue
            if re.match(r"^(Waiting|Working|Thinking)[…\.]+", stripped):
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
    def _check_prompt_in_text(text: str) -> bool:
        """
        Check whether the last significant line of text contains a prompt character.

        Strips ANSI codes first so that prompt characters hidden behind or before
        escape sequences are correctly detected. Uses PROMPT_CHARS.
        An empty or whitespace-only buffer means prompt not detected.
        """
        # Critically, strip ANSI first — the raw buffer may have ANSI sequences
        # BETWEEN or AFTER the prompt character (e.g. \x1b[?25h\u276f\x1b[?12l),
        # so checking the raw tail would miss the prompt.
        from output_parser import strip_ansi
        clean = strip_ansi(text)
        stripped = clean.rstrip()
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

    # ── Reader thread health ────────────────────────────────────────────

    @property
    def reader_alive(self) -> bool:
        """Check if the reader thread is genuinely alive."""
        thread = self._reader_thread
        if thread is None:
            return False
        if not thread.is_alive():
            return False
        # Check heartbeat: reader should update regularly
        with self._reader_heartbeat_lock:
            hb = self._reader_heartbeat_time
        if hb > 0 and (time.monotonic() - hb) > READER_HEARTBEAT_INTERVAL * 3:
            # Heartbeat stale — reader may be stuck
            logger.warning("Reader thread heartbeat stale (%.1fs since last update)",
                           time.monotonic() - hb)
            return False
        # Check PTY fd
        if self._master_fd is not None:
            try:
                os.fstat(self._master_fd)
            except OSError:
                logger.warning("PTY master fd %s is invalid", self._master_fd)
                return False
        return True

    def _get_readable_buffer_tail(self, max_chars: int = 2000) -> str:
        """
        Get a readable (ANSI-stripped, carriage-return-processed) tail
        of the current buffer for display purposes.
        """
        from output_parser import strip_ansi, process_carriage_returns
        with self._buf_lock:
            raw = bytes(self._buffer)
        if not raw:
            return ""
        # Take the last N bytes
        tail = raw[-min(len(raw), 8192):]
        text = tail.decode("utf-8", errors="replace")
        text = strip_ansi(text)
        text = process_carriage_returns(text)
        # Take last max_chars chars, starting from line boundary
        if len(text) > max_chars:
            idx = max(0, len(text) - max_chars)
            # Try to start at a newline boundary
            nl = text.find("\n", idx)
            if nl >= 0 and nl < len(text) - max_chars // 2:
                idx = nl + 1
            text = text[idx:]
        return text.strip()

    @staticmethod
    def _count_effective_content(buffer_data: bytes) -> int:
        """
        Count bytes of "effective content" in buffer, excluding:
        - Braille spinner characters (U+2801-U+28FF) — DeepSeek spinner ticks
        - ANSI escape sequences (esc, CSI, OSC, etc.)
        - ANSI cursor movement sequences
        - Bare whitespace and control characters

        This is used by Condition E to detect when real output has stopped
        even though spinner/ANSI noise keeps the raw buffer growing.
        """
        from output_parser import strip_ansi

        text = strip_ansi(buffer_data.decode("utf-8", errors="replace"))

        # Count only meaningful characters: non-whitespace, non-control,
        # non-braille-spinner graphemes
        count = 0
        for ch in text:
            cp = ord(ch)
            if cp == 0x2800:
                continue  # Braille blank
            if 0x2801 <= cp <= 0x28FF:
                continue  # Braille spinner characters
            if cp <= 0x1F or cp == 0x7F:
                continue  # Control characters
            if ch.isspace() and ch not in ("\n", "\r"):
                continue  # Non-newline whitespace (indentation artifacts)
            if ch == "\r":
                continue  # Carriage returns (cursor movements)
            count += 1
        return count

    def _cleanup(self):
        """Close PTY master fd and reset all state."""
        self._ready = False
        self._running = False
        if self._master_fd is not None:
            try:
                os.close(self._master_fd)
            except OSError:
                pass
            self._master_fd = None
        self._process = None
        # Clear buffer so stale content from a stopped bridge never leaks
        self._buffer = bytearray()
        # Reset all tracking state
        self._send_start_pos = 0
        self._send_prompt_watermark = 0
        self._last_growth_time = 0.0
        self._last_growth_buf_len = 0
        self._last_effective_growth_time = 0.0
        self._last_effective_content_len = 0
        self._no_pollin_since = 0.0
        self._stagnation_buf_len = 0
        self._last_data_time = 0.0
        self._expecting_response = False
        self._response_event.clear()

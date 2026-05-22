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

        Coroutine. Writes to PTY master fd, waits for completion event.
        """
        if not self._ready:
            raise RuntimeError("PtyBridge is not ready. Call start() first.")

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

        # Clean up TUI artifacts and normalize formatting
        rendered = self._clean_output(rendered)

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

            # Response completion: one of two conditions
            #    Condition A: silence + minimum wait time elapsed
            #    Condition B: prompt character detected in latest buffer chunk
            #      (only if the prompt is NEW — appeared after send() started)
            if self._expecting_response and self._last_data_time > 0:
                elapsed = now - self._send_start_time
                silence = now - self._last_data_time

                # Condition A: silence-based completion
                silence_trigger = elapsed >= MIN_RESPONSE_WAIT and silence >= SILENCE_TIMEOUT

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

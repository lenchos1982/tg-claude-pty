"""
Output parser for Claude Code PTY output.

Handles:
  - ANSI escape sequence stripping
  - Carriage return handling (line overwrites via \r)
  - Completion detection (prompt pattern + silence timeout)
  - Clean content extraction from raw PTY output
  - Spinner/braille character stripping
  - Terminal DA query responses

NOTE: This PTY-based bot does NOT use ACP mode. ACP-format parsing
functions (extract_new_content, _strip_tool_call, _strip_tool_result,
_strip_think_block) have been removed because they are not relevant
here. All communication goes through a real PTY terminal.
"""

import re

# ── ANSI stripping ──────────────────────────────────────────────────────────

_ANSI_RE = re.compile(
    r"""
    \x1b\[[0-9;]*[a-zA-Z]       # CSI sequences: \x1b[31m, \x1b[2K, etc.
    |\x1b\[\?[0-9;]*[a-zA-Z]   # DEC private sequences: \x1b[?25h, \x1b[?12l, etc.
    |\x1b\].*?(?:\x1b\\|\x07)   # OSC sequences: \x1b]0;title\x07
    |\x1b[PX^_].*?(?:\x1b\\|$)  # DCS/SOS/PM/APC sequences
    |\x1b[\\\a78DMNEHc=><]      # Single-char ESC sequences: ST, BEL, IND, RI, NEL, etc.
    """,
    re.VERBOSE,
)

# Braille characters used by terminal spinners (U+2801-U+28FF)
_BRAILLE_RE = re.compile(r"[\u2801-\u28FF]")


def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from text."""
    return _ANSI_RE.sub("", text)


def strip_spinners(text: str) -> str:
    """Remove braille spinner characters that leak through ANSI stripping."""
    return _BRAILLE_RE.sub("", text)


# ── Carriage return handling ────────────────────────────────────────────────


def process_carriage_returns(text: str) -> str:
    """
    Handle carriage returns by keeping the visible content per line.

    In terminal output, \\r moves cursor to start of line,
    and subsequent text overwrites previous content.
    Trailing \\r's are ignored (keep the non-empty segment before them).
    """
    lines = text.split("\n")
    processed = []
    for line in lines:
        if "\r" in line:
            parts = line.split("\r")
            # Keep last non-empty segment (the visible content after overwrites)
            visible = next((p for p in reversed(parts) if p.strip()), "")
            if not visible:
                # If all parts are blank after stripping, keep the last
                visible = parts[-1] if parts else ""
            processed.append(visible)
        else:
            processed.append(line)
    return "\n".join(processed)


# ── Terminal DA queries ────────────────────────────────────────────────────

# Queries that Claude sends to probe terminal capabilities
# Each query -> response mapping (bytes)
DA_QUERIES: dict[bytes, bytes] = {
    b"\x1b[c": b"\x1b[?1;2c",         # Device Attributes → VT100 w/ adv video
    b"\x1b[>0q": b"\x1b[>0;0;0c",     # Request Terminal Params → xterm DA
    b"\x1b[>1q": b"\x1b[>1;0;0c",     # Secondary DA (terminal version query)
    b"\x1b[?6n": b"\x1b[1;1R",        # CPR (Cursor Position Report) → row 1, col 1
}


def respond_da(data: bytes, master_fd: int) -> bool:
    """
    Check raw data for terminal DA queries and respond to master fd.
    Returns True if any response was sent.
    """
    import os

    responded = False
    for query, response in DA_QUERIES.items():
        if query in data:
            try:
                os.write(master_fd, response)
                responded = True
            except OSError:
                pass
    return responded


# ── Prompt detection ────────────────────────────────────────────────────────

# TUI status/decorator patterns that appear on their own line and should
# be skipped when scanning for prompt characters. In --bare TUI mode,
# Claude emits status lines like "⏵⏵automodeon" or "✻ Brewed for 3s"
# that can appear AFTER the ❯ prompt, pushing it to the second-to-last
# line. We strip these decorative lines before checking for prompt chars.
_TUI_STATUS_LINE_RE = re.compile(
    r"""
    ^(?:                        # start of line
        [⏵⏸]\S*$                # TUI mode indicator: ⏵⏵automodeon, ⏸, etc.
        |[✻✶\*]\s+\(?(?:Brewed|Cogitated|Churned|Thought|Run|Ran)\s+for  # status lines
        |[✻✶\*]\s*\(?\d+s.*$   # Throbber duration: ✻ (3s)
        |^\s*$                  # empty/whitespace-only lines
    )
    """,
    re.VERBOSE,
)


def is_prompt_detected(text: str) -> bool:
    """
    Check if text contains a Claude Code prompt character near the end.

    In --bare TUI mode, status/decorator lines (⏵⏵automodeon, ✻ Brewed for 3s)
    can appear AFTER the ❯ prompt. This function scans the last few
    significant lines, skipping known TUI decorators, to find the prompt.

    ANSI escape sequences are stripped before checking, because Claude
    wraps its prompt characters in formatting codes (e.g.,
    \\x1b[1m\\x1b[35m❯\\x1b[39m\\x1b[22m) — the raw text ends with ANSI
    codes and spaces, not the prompt character itself.

    Returns True if any of the last 5 significant (non-TUI) lines contains
    a prompt character (❯, ▶, >) after ANSI stripping.
    """
    if not text:
        return False

    # Strip ANSI first so prompt characters buried in formatting codes
    # become visible at the end of their line.
    text = strip_ansi(text)

    # Scan the last several lines, filtering TUI decorators
    lines = text.split("\n")
    candidate_count = 0
    for line in reversed(lines):
        stripped = line.rstrip()
        if not stripped:
            continue
        # Skip known TUI status/decorator lines
        if _TUI_STATUS_LINE_RE.match(stripped):
            continue
        # This is a significant line — check for prompt character
        candidate_count += 1
        if stripped and stripped[-1] in ">▶❯":
            return True
        # Also check if prompt char appears as standalone on its own line
        # (e.g., whitespace + "❯" + whitespace — common with ANSI rendering)
        if re.search(r"^\s*[❯▶>]\s*$", stripped):
            return True
        if candidate_count >= 5:
            # Looked at 5 significant lines, no prompt found
            return False

    return False


# ── Content extraction ──────────────────────────────────────────────────────


def extract_content(raw_text: str) -> str:
    """Clean raw PTY output into readable text."""
    text = strip_ansi(raw_text)
    text = strip_spinners(text)
    text = process_carriage_returns(text)
    text = text.strip()
    return text




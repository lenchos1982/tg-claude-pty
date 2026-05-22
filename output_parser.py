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


def is_prompt_detected(text: str) -> bool:
    """
    Check if text ends with a Claude Code prompt character.

    Checks the last non-whitespace character for common prompt markers.
    """
    stripped = text.rstrip()
    if not stripped:
        return False
    last_char = stripped[-1]
    return last_char in ">▶❯"


# ── Content extraction ──────────────────────────────────────────────────────


def extract_content(raw_text: str) -> str:
    """Clean raw PTY output into readable text."""
    text = strip_ansi(raw_text)
    text = strip_spinners(text)
    text = process_carriage_returns(text)
    text = text.strip()
    return text




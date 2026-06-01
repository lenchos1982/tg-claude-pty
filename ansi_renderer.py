"""
Virtual terminal renderer for ANSI cursor-positioned output.

Processes ANSI escape sequences to reconstruct the final visible state
of a terminal screen.

Handles:
  - Cursor positioning (CSI H, CSI f, CSI A/B/C/D, CSI G)
  - Line clearing (CSI K, CSI J)
  - SGR (colors, bold, etc.) — stripped
  - Line feeds, carriage returns, tabs, backspaces
  - Scroll regions via newline and CSI S/T
  - OSC (operating system commands) — stripped
  - DCS, SOS, PM, APC — stripped
  - Erase in Display (mode 1, 3)
  - All other ANSI sequences — stripped
"""

import copy
import re
from typing import List

# ── ANSI pattern ────────────────────────────────────────────────────────────

_ANSI_CSI = re.compile(r"\x1b\[([0-9;]*)([a-zA-Z])")

# Braille range: used to detect spinner characters that should be stripped
_BRAILLE_START = 0x2801
_BRAILLE_END = 0x28FF


def _is_braille(ch: str) -> bool:
    """Check if a character is in the Braille Patterns block (spinners)."""
    cp = ord(ch)
    return _BRAILLE_START <= cp <= _BRAILLE_END


# ── VirtualScreen ───────────────────────────────────────────────────────────


class VirtualScreen:
    """
    A simple virtual terminal that processes ANSI codes and builds
    the final visible screen state.

    Unlike a full xterm emulator, this focuses on correct rendering of
    cursor-positioned output (line-by-line overwrites, progress bars,
    menus) that Claude Code CLI produces.
    """

    def __init__(self, rows: int = 100, cols: int = 200):
        self.rows = rows
        self.cols = cols
        self._screen: List[List[str]] = [
            [" "] * cols for _ in range(rows)
        ]
        self._row = 0  # 0-indexed cursor row
        self._col = 0  # 0-indexed cursor column

    def feed(self, text: str):
        """Process text through the virtual terminal."""
        i = 0
        while i < len(text):
            ch = text[i]

            if ch == "\x1b":
                i = self._handle_escape(text, i)
                continue

            elif ch == "\r":
                # Peek next char for CRLF (\r\n) vs CR alone
                if i + 1 < len(text) and text[i + 1] == "\n":
                    # CR+LF: go to start of next line
                    self._col = 0
                    self._row += 1
                    if self._row >= self.rows:
                        self._scroll_up()
                    i += 1  # consume the \n too
                else:
                    # CR only: return to start of current line
                    self._col = 0
            elif ch == "\n":
                self._row += 1
                if self._row >= self.rows:
                    self._scroll_up()
            elif ch == "\a":
                pass  # BEL - ignore
            elif ch == "\b":
                self._col = max(0, self._col - 1)
            elif ch == "\t":
                self._col = (self._col + 8) & ~7
            elif ch == "\x0e" or ch == "\x0f":
                # Shift Out / Shift In — ignore
                pass
            else:
                # Regular character (including line-drawing chars)
                if self._col >= self.cols:
                    self._col = 0
                    self._row += 1
                    if self._row >= self.rows:
                        self._scroll_up()
                        self._row = self.rows - 1
                if self._row >= self.rows:
                    self._scroll_up()
                    self._row = self.rows - 1
                self._screen[self._row][self._col] = ch
                self._col += 1

            i += 1

    def _handle_escape(self, text: str, i: int) -> int:
        """Handle an escape sequence starting at position i. Returns new i."""
        if i + 1 >= len(text):
            return i + 1

        # CSI (Control Sequence Introducer): ESC [
        m = _ANSI_CSI.match(text, i)
        if m:
            self._handle_csi(m)
            return m.end()

        next_ch = text[i + 1]

        if next_ch == "]":
            # OSC (Operating System Command): ESC ] ... ST (ESC \) or BEL
            end = text.find("\x07", i + 2)
            if end == -1:
                end = text.find("\x1b\\", i + 2)
            if end == -1:
                end = len(text)
            else:
                end = end + (2 if text[end] == "\x1b" else 1)
            return end

        elif next_ch in "()":
            # Character set selection (e.g., ESC ( B, ESC ) 0): skip 3 chars
            return i + 3

        elif next_ch == "P":
            # DCS (Device Control String): ESC P ... ST
            end = text.find("\x1b\\", i + 2)
            return end + 2 if end != -1 else len(text)

        elif next_ch in "X^_":
            # SOS/PM/APC: ESC X/^/_ ... ST
            end = text.find("\x1b\\", i + 2)
            return end + 2 if end != -1 else len(text)

        elif next_ch == "\\":
            # ST (String Terminator) alone
            return i + 2

        elif next_ch in "78":
            # ESC 7: Save cursor, ESC 8: Restore cursor
            return i + 2

        elif next_ch == "=":
            # Application Keypad mode — ignore
            return i + 2
        elif next_ch == ">":
            # Normal Keypad mode — ignore
            return i + 2

        elif next_ch == "c":
            # Reset — ignore for now
            return i + 2

        elif next_ch == "D":
            # IND (Index) — move down one line, scroll if needed
            self._row += 1
            if self._row >= self.rows:
                self._scroll_up()
            return i + 2

        elif next_ch == "M":
            # RI (Reverse Index) — move up one line, may need to scroll down
            self._row = max(0, self._row - 1)
            return i + 2

        elif next_ch == "E":
            # NEL (Next Line) — CR + LF
            self._col = 0
            self._row += 1
            if self._row >= self.rows:
                self._scroll_up()
            return i + 2

        elif next_ch == "H":
            # HTS (Horizontal Tab Set) — ignore
            return i + 2

        # Skip any other ESC sequence (2+ chars)
        return i + 2

    def _handle_csi(self, m: re.Match):
        """Handle a CSI (Control Sequence Introducer) sequence."""
        final = m.group(2)
        params_str = m.group(1)
        params = [int(p) for p in params_str.split(";") if p] if params_str else []

        if final == "A":
            # Cursor Up (CUU)
            n = params[0] if params else 1
            self._row = max(0, self._row - n)
        elif final == "B":
            # Cursor Down (CUD)
            n = params[0] if params else 1
            self._row = min(self.rows - 1, self._row + n)
        elif final == "C":
            # Cursor Forward / Right (CUF)
            n = params[0] if params else 1
            self._col = min(self.cols - 1, self._col + n)
        elif final == "D":
            # Cursor Back / Left (CUB)
            n = params[0] if params else 1
            self._col = max(0, self._col - n)
        elif final in ("H", "f"):
            # CUP (Cursor Position) / HVP (Horizontal Vertical Position)
            # Params: row;col (1-based). Default to 1 if omitted.
            row = (params[0] - 1) if len(params) >= 1 and params[0] >= 1 else 0
            col = (params[1] - 1) if len(params) >= 2 and params[1] >= 1 else 0
            self._row = min(self.rows - 1, max(0, row))
            self._col = min(self.cols - 1, max(0, col))
        elif final == "G":
            # CHA (Cursor Horizontal Absolute)
            col = (params[0] - 1) if params and params[0] >= 1 else 0
            self._col = min(self.cols - 1, max(0, col))
        elif final == "K":
            # EL (Erase in Line)
            mode = params[0] if params else 0
            if mode == 0:
                # Clear from cursor to end of line
                for c in range(self._col, self.cols):
                    self._screen[self._row][c] = " "
            elif mode == 1:
                # Clear from start to cursor
                for c in range(0, self._col + 1):
                    self._screen[self._row][c] = " "
            elif mode == 2:
                # Clear entire line
                self._screen[self._row] = [" "] * self.cols
        elif final == "J":
            # ED (Erase in Display)
            mode = params[0] if params else 0
            if mode == 0:
                # Clear from cursor to end of screen
                for r in range(self._row, self.rows):
                    start_col = self._col if r == self._row else 0
                    for c in range(start_col, self.cols):
                        self._screen[r][c] = " "
            elif mode == 1:
                # Clear from beginning to cursor
                for r in range(0, self._row + 1):
                    end_col = self._col if r == self._row else self.cols - 1
                    for c in range(0, end_col + 1):
                        self._screen[r][c] = " "
            elif mode == 2:
                # Clear entire screen
                self._clear_screen()
                self._row = 0
                self._col = 0
            elif mode == 3:
                # Clear scrollback (treat same as clear screen)
                self._clear_screen()
                self._row = 0
                self._col = 0
        elif final == "L":
            # IL (Insert Line): insert n blank lines at cursor row
            n = params[0] if params else 1
            for _ in range(n):
                self._screen.insert(self._row, [" "] * self.cols)
            self._screen = self._screen[:self.rows]
        elif final == "M":
            # DL (Delete Line): delete n lines at cursor row
            n = params[0] if params else 1
            for _ in range(n):
                self._screen.pop(self._row)
            while len(self._screen) < self.rows:
                self._screen.append([" "] * self.cols)
        elif final == "P":
            # DCH (Delete Character): delete n chars at cursor
            n = params[0] if params else 1
            del self._screen[self._row][self._col:self._col + n]
            self._screen[self._row].extend([" "] * n)
            self._screen[self._row] = self._screen[self._row][:self.cols]
        elif final == "@":
            # ICH (Insert Character): insert n blank chars at cursor
            n = params[0] if params else 1
            self._screen[self._row][self._col:self._col] = [" "] * n
            self._screen[self._row] = self._screen[self._row][:self.cols]
        elif final == "S":
            # SU (Scroll Up): scroll up n lines
            n = params[0] if params else 1
            self._scroll_up(n)
        elif final == "T":
            # SD (Scroll Down): scroll down n lines
            n = params[0] if params else 1
            for _ in range(n):
                self._screen.pop()
                self._screen.insert(0, [" "] * self.cols)
            self._row = max(0, self._row + n)
        elif final == "m":
            # SGR (Select Graphic Rendition) — colors, bold, etc.
            # SGR is always stripped for plain-text output.
            pass
        elif final in ("s", "u"):
            # SCOSC (Save Cursor Position) / SCORC (Restore Cursor Position)
            # For simplicity, skip cursor save/restore
            pass
        elif final in ("h", "l"):
            # SM (Set Mode) / RM (Reset Mode)
            # Covers DECSET/DECRST, cursor visibility, wrapping, etc.
            # For plain-text extraction, all modes are irrelevant.
            pass
        elif final == "r":
            # DECSTBM (Set Top and Bottom Margins / Scroll Region)
            # For now, ignore — treat full screen as scrollable.
            pass
        elif final == "n":
            # DSR (Device Status Report) — queries from the terminal
            # e.g. ESC[6n queries cursor position. These are queries,
            # not output; skip.
            pass
        # All other CSI sequences: ignored

    def _clear_screen(self):
        """Reset the entire screen buffer."""
        self._screen = [[" "] * self.cols for _ in range(self.rows)]

    def _scroll_up(self, n: int = 1):
        """Scroll the screen up by n lines."""
        for _ in range(n):
            self._screen.pop(0)
            self._screen.append([" "] * self.cols)
        self._row = max(0, min(self.rows - 1, self._row - n))

    def snapshot(self) -> List[List[str]]:
        """Return a deep copy of the current screen state."""
        return copy.deepcopy(self._screen)

    def get_new_text_since(self, snapshot: List[List[str]]) -> str:
        """Return text from rows added since the given snapshot.

        Finds the last non-empty row in the snapshot, then returns all
        text from that row+1 to the last non-empty row in the current
        screen. If the snapshot is empty or None, returns all text.
        """
        if not snapshot:
            return self.get_text()

        # Find the last non-empty row in the snapshot
        last_snapshot_row = -1
        for r in range(len(snapshot) - 1, -1, -1):
            if any(c != " " for c in snapshot[r]):
                last_snapshot_row = r
                break

        # Collect rows from last_snapshot_row+1 to last non-empty in current screen
        lines = []
        start_row = max(0, last_snapshot_row + 1)
        for r in range(start_row, self.rows):
            line = "".join(self._screen[r]).rstrip()
            lines.append(line)

        # Strip trailing empty lines
        while lines and not lines[-1]:
            lines.pop()

        return "\n".join(lines)

    def get_text(self) -> str:
        """Get the visible text, preserving empty lines for paragraph breaks."""
        lines = []
        for r in range(self.rows):
            line = "".join(self._screen[r]).rstrip()
            lines.append(line)
        # Strip trailing empty lines
        while lines and not lines[-1]:
            lines.pop()
        return "\n".join(lines)

    def get_content_area(self, header_rows: int = 0, footer_rows: int = 0) -> str:
        """
        Get text from the content area (excluding header and footer rows).
        Useful for extracting just the response area from Claude's TUI.
        """
        start = header_rows
        end = self.rows - footer_rows
        lines = []
        for r in range(start, end):
            line = "".join(self._screen[r]).rstrip()
            if line:
                lines.append(line)
        return "\n".join(lines)

    def render(self, text: str) -> str:
        """Process text and return the final visible state."""
        self.feed(text)
        output = self.get_text()

        # Post-process: clean up any remaining bare braille spinner chars
        # that may end up at column 0 of a line
        clean_lines = []
        for line in output.split("\n"):
            # Remove leading braille-only characters
            if line and _is_braille(line[0]):
                line = line[1:]
            clean_lines.append(line)

        return "\n".join(clean_lines)

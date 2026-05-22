"""Environment variable loading and validation for tg-claude-pty."""

import os
import shutil
import sys

# ── Required ─────────────────────────────────────────────────────────────────

TELEGRAM_BOT_TOKEN: str = os.environ.get("TELEGRAM_BOT_TOKEN", "")

# ── Optional with defaults ───────────────────────────────────────────────────

CLAUDE_BIN: str = os.environ.get("CLAUDE_BIN", "claude")
ALLOWED_USER_IDS: set[int] = {
    int(x) for x in os.environ.get("ALLOWED_USER_IDS", "").split(",") if x.strip()
}
SESSION_ID: str = os.environ.get("SESSION_ID", "")

# ── Resolve claude binary path ──────────────────────────────────────────────

# claude is typically installed via npm global (@anthropic-ai/claude-code).
# The npm global bin dir may not be in systemd's PATH.
# If the path isn't fully qualified and isn't found via shutil, try common locations.
if CLAUDE_BIN == "claude" or "/" not in CLAUDE_BIN:
    resolved = shutil.which(CLAUDE_BIN)
    if not resolved:
        # Fallback: common npm global locations
        for candidate in (
            "/usr/local/bin/claude",
            "/usr/bin/claude",
            os.path.expanduser("~/.nvm/versions/node/*/bin/claude"),
            os.path.expanduser("~/.local/bin/claude"),
        ):
            if "*" in candidate:
                import glob as _glob
                matches = _glob.glob(candidate)
                if matches:
                    resolved = matches[0]
                    break
            elif os.path.isfile(candidate):
                resolved = candidate
                break

    if resolved:
        CLAUDE_BIN = resolved

# ── Validation ───────────────────────────────────────────────────────────────

if not TELEGRAM_BOT_TOKEN:
    print("ERROR: Missing required environment variable: TELEGRAM_BOT_TOKEN")
    sys.exit(1)

if not shutil.which(CLAUDE_BIN) and not os.path.isfile(CLAUDE_BIN):
    print(
        f"WARNING: claude binary not found at '{CLAUDE_BIN}'. "
        "Make sure @anthropic-ai/claude-code is installed globally.",
        file=sys.stderr,
    )

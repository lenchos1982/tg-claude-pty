# tg-claude-pty — Telegram ↔ Claude Code PTY Bridge

Standalone, distributable bridge that drives Claude Code CLI through a
pseudo-terminal (PTY). No acpx/OpenClaw dependency, no Docker, no `--print` mode.
Works with any model backend (Anthropic API, Bedrock, Vertex, custom proxy).

## Architecture

```
Telegram BOT ←long polling→ bot.py
  └─ PtyBridge (PTY master fd)
       └─ /usr/bin/claude (full interactive CLI)
            └─ Any model backend
```

## Files

| File | Purpose |
|---|---|
| `bot.py` | Telegram handlers (thin PTB layer) |
| `pty_bridge.py` | PtyBridge class — PTY lifecycle + I/O |
| `output_parser.py` | ANSI stripping + carriage return handling |
| `ansi_renderer.py` | Virtual terminal renderer for ANSI cursor-positioned output |
| `config.py` | Environment variable loading + validation |
| `requirements.txt` | Python dependencies |
| `.env.example` | Template for environment configuration |
| `tg-claude-pty.service` | systemd service unit |

## Prerequisites

- Python 3.10+
- Claude Code CLI installed and configured (`claude` available in PATH)
- A Telegram bot token (from [@BotFather](https://t.me/BotFather))
- Your Telegram user ID (for the whitelist)

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure
cp .env.example .env
# Edit .env with your Telegram bot token and user ID

# 3. Run
python3 bot.py
```

## Deployment (Production)

```bash
# Copy project to deployment directory
sudo mkdir -p /opt/tg-claude-pty
sudo cp -r . /opt/tg-claude-pty/
cd /opt/tg-claude-pty

# Install dependencies
pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Edit .env:
#   TELEGRAM_BOT_TOKEN=<your token>
#   ALLOWED_USER_IDS=<your telegram user id>
#   CLAUDE_BIN=claude          # optional, defaults to claude
#   SESSION_ID=                # optional, for session pinning

# First, test manually
python3 bot.py
# Ctrl+C to stop, then set up systemd

# Install systemd service
sudo cp tg-claude-pty.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tg-claude-pty

# Check status
sudo systemctl status tg-claude-pty

# View logs
sudo journalctl -u tg-claude-pty -f
```

## Model Backend Configuration

The bridge uses whatever backend Claude Code CLI is configured to use.
Configure Claude Code before running the bridge:

```bash
# Set up Claude Code (interactive)
claude

# Or configure via environment variables:
export ANTHROPIC_API_KEY=sk-ant-...          # Anthropic API
# export ANTHROPIC_BASE_URL=https://...       # Custom proxy
# export BEDROCK_AWS_REGION=us-east-1         # AWS Bedrock
# export VERTEX_PROJECT_ID=my-project         # GCP Vertex AI

# These env vars will be inherited by the systemd service
# Add them to the [Service] section of tg-claude-pty.service:
# Environment="ANTHROPIC_API_KEY=sk-ant-..."
```

For systemd, add backend env vars to the service file:

```ini
[Service]
Environment="ANTHROPIC_API_KEY=sk-ant-..."
# Environment="ANTHROPIC_BASE_URL=https://my-proxy.example.com"
```

## Commands

- `/start` — Welcome message
- `/help` — Usage instructions
- `/new` — Reset Claude session (fresh start)
- `/stop` — Restart Claude if stuck

## Troubleshooting

**Claude fails to start:**
- Run `claude` manually in terminal to check if it's configured
- Check logs: `journalctl -u tg-claude-pty -n 50`
- Startup may take up to 60 seconds (dismissing dialogs)

**No response from Claude:**
- Check if the model call is taking longer than the 600s timeout
- Use `/stop` to restart the bridge
- Check `journalctl -u tg-claude-pty -f` for raw output

**Bot not responding to Telegram messages:**
- Verify TELEGRAM_BOT_TOKEN is correct
- Check ALLOWED_USER_IDS includes your user ID
- Ensure the bot is not blocked by a firewall

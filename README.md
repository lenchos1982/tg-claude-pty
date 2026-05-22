# tg-claude-pty — Telegram ↔ Claude Code PTY Bridge

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

> **Turn your Telegram into a full Claude Code terminal.**  
> Send messages from Telegram, get back Claude's responses — no need to keep a terminal open.

---

## Table of Contents

- [1. Project Purpose](#1-project-purpose)
- [2. System Architecture](#2-system-architecture)
- [3. Features](#3-features)
- [4. Prerequisites](#4-prerequisites)
- [5. Installation & Configuration](#5-installation--configuration)
- [6. Usage](#6-usage)
- [7. Security Configuration & Risk Warnings](#7-security-configuration--risk-warnings-️)
- [8. Troubleshooting](#8-troubleshooting)
- [9. File Structure](#9-file-structure)
- [10. Development & Contribution](#10-development--contribution)

---

## 1. Project Purpose

### What is this?

tg-claude-pty is a **standalone, zero-dependency bridge** that connects a Telegram bot to Claude Code CLI via a pseudo-terminal (PTY). You chat with Claude through Telegram — it's like having Claude Code in your pocket.

### What problem does it solve?

Claude Code is an interactive CLI tool (`claude`) by Anthropic. Normally you:

1. Open a terminal
2. Run `claude`
3. Stay at your computer while Claude works
4. Watch the output scroll by

This is fine when you're at a desk, but what if you want to:

- Ask Claude a question while commuting?
- Kick off a long-running task and come back later?
- Let teammates interact with a shared Claude instance?
- Run Claude on a headless server (VPS, homelab, cloud VM)?

**tg-claude-pty solves this.** It runs Claude Code in the background, connects it to a Telegram bot, and lets you interact from anywhere via your phone or desktop Telegram app.

### Why not use acpx / OpenClaw / Docker instead?

| Approach | Pros | Cons |
|----------|------|------|
| **tg-claude-pty** (this project) | Standalone, lightweight, no Docker, no framework dependency, full interactive PTY, AGPL license | Single-user (by design), no multi-bot support |
| **OpenClaw gateway** | Full-featured agent framework, multi-channel | Heavy stack, opinionated, requires OpenClaw ecosystem |
| **acpx** | Claude Code CLI extension | Requires Claude Code to be installed, not standalone |
| **Docker + Claude Code** | Isolated environment | Complex networking for Telegram webhook/PTY, container overhead |
| **Telegram BOT API directly** | Simple HTTP | No interactive session management, no streaming responses |

The key differentiator: **tg-claude-pty drives Claude Code through a real PTY**, not a `--print` mode or REST API wrapper. This means:

- Claude sees a real terminal (terminal DA queries, ANSI codes, cursor positioning)
- Claude's interactive features work normally (editing files, reading, running commands)
- The startup sequence (theme selection, security prompts, trust dialogs) is handled automatically
- Any model backend works — Anthropic API, Bedrock, Vertex AI, or a custom proxy

---

## 2. System Architecture

### High-Level Overview

```
┌──────────────────────────────────────────────────────────┐
│                    Telegram Cloud                         │
│  (Bot API Server / Long Polling)                         │
└────────────────────────┬─────────────────────────────────┘
                         │ HTTP/HTTPS (long polling)
                         ▼
┌──────────────────────────────────────────────────────────┐
│                   Your Server / VPS                       │
│                                                          │
│  ┌──────────────────────────────────────────────────┐   │
│  │              bot.py (Python)                      │   │
│  │  ┌──────────────────────────────────────────────┐ │   │
│  │  │  python-telegram-bot Application              │ │   │
│  │  │  ├── CommandHandler(/start)                   │ │   │
│  │  │  ├── CommandHandler(/help)                    │ │   │
│  │  │  ├── CommandHandler(/new)                     │ │   │
│  │  │  ├── CommandHandler(/stop)                    │ │   │
│  │  │  ├── MessageHandler(text → Claude)            │ │   │
│  │  │  └── MessageHandler(photo → image analysis)   │ │   │
│  │  └──────────────────────────────────────────────┘ │   │
│  └──────────┬───────────────────────────────────────┘   │
│             │ asyncio.Lock (serialized access)           │
│             ▼                                           │
│  ┌──────────────────────────────────────────────────┐   │
│  │              pty_bridge.py                        │   │
│  │  ┌──────────────────────────────────────────────┐ │   │
│  │  │  PtyBridge Class                             │ │   │
│  │  │                                              │ │   │
│  │  │  master_fd ──┬── reader_thread (PTY reader)  │ │   │
│  │  │              │       ├── DA response handler │ │   │
│  │  │              │       ├── Buffer accumulator  │ │   │
│  │  │              │       └── Prompt detector     │ │   │
│  │  │              │                                │ │   │
│  │  │              └── writer (send text to PTY)    │ │   │
│  │  └──────────────────────────────────────────────┘ │   │
│  └──────────┬───────────────────────────────────────┘   │
│             │ PTY (pseudo-terminal)                      │
│             ▼                                           │
│  ┌──────────────────────────────────────────────────┐   │
│  │           Claude Code CLI (subprocess)             │   │
│  │  /usr/bin/claude --bare [--session-id XXX]        │   │
│  │                                                   │   │
│  │  Reads files • Edits code • Runs commands         │   │
│  │  Answers questions • Analyzes images              │   │
│  └──────────────────────────────────────────────────┘   │
│                                                          │
│  ┌──────────────────────────────────────────────────┐   │
│  │            Model Backend (any)                    │   │
│  │  ┌──────────┐  ┌──────────┐  ┌─────────────┐    │   │
│  │  │Anthropic │  │ Bedrock  │  │Vertex AI    │    │   │
│  │  │  API     │  │ (AWS)    │  │ (GCP)       │    │   │
│  │  └──────────┘  └──────────┘  └─────────────┘    │   │
│  │  ┌──────────┐  ┌─────────────────────────────┐  │   │
│  │  │ Custom   │  │ Local / Proxy (open source)  │  │   │
│  │  │ Proxy    │  │ e.g. Ollama, vLLM, litellm   │  │   │
│  │  └──────────┘  └─────────────────────────────┘  │   │
│  └──────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────┘
```

### Data Flow (Single Request/Response Cycle)

```
1. USER                    ──"Write a Python script"──►  Telegram Bot API
                                                          │
2. Telegram Bot API        ──Update (text message)────►  bot.py
                                                          │
3. bot.py (handle_message)                                │
   ├── Check authorization (ALLOWED_USER_IDS)             │
   ├── Send "typing" action to Telegram                   │
   ├── Acquire send_lock (asyncio.Lock)                    │
   ├── Save photo to temp file (if applicable)            │
   └── Call bridge.send(prompt, timeout=600)              │
                                                          │
4. PtyBridge.send(text)                                   │
   ├── Record buffer start position                       │
   ├── Set response watermark (prevent stale-prompt)      │
   ├── Clear response_event                               │
   ├── Write text + \r to master_fd (PTY input)          │
   └── Call _wait_for_response() — blocking thread        │
                                                          │
5. Reader Thread (concurrent)                             │
   ├── Reads PTY output (os.read, 4096 bytes)             │
   ├── Responds to DA queries automatically               │
   ├── Appends data to shared buffer                      │
   ├── Detects prompt characters (>, ▶, ❯)               │
   └── Sets response_event when done                      │
                                                          │
6. _wait_for_response detects event or timeout            │
                                                          │
7. PtyBridge.send()                                       │
   ├── Extract new bytes from buffer (start→end)          │
   ├── Render through VirtualScreen (ANSI cursor)         │
   ├── Clean output (strip TUI artifacts, status lines)   │
   └── Return cleaned text                                │
                                                          │
8. bot.py                                                 │
   ├── Truncate to 4000 chars (Telegram limit)            │
   ├── Format as MarkdownV2                               │
   ├── edit_message_text (replace "⏳ Processing...")     │
   └── Release send_lock                                  │
                                                          │
9. USER                    ◄──"Here's your script..."──  Telegram App
```

### Startup Sequence

```
1. bot.py main()
   ├── Application.builder().token(TELEGRAM_BOT_TOKEN)
   ├── Register command/message handlers
   └── post_init → PtyBridge()
        └── bridge.start(event_loop)
             │
2. PtyBridge._start_pty()
   ├── pty.openpty() → (master_fd, slave_fd)
   ├── fcntl.ioctl → set PTY size (100 rows × 200 cols)
   ├── subprocess.Popen([claude, --bare], stdin=slave_fd, stdout=slave_fd, stderr=slave_fd)
   ├── Close slave_fd (parent only keeps master)
   ├── Start reader_thread (daemon)
   └── Startup dialog loop:
        ├── Send Enter every 1.5s → dismiss theme picker
        ├── Send Enter → dismiss security overview
        ├── Send Enter → dismiss trust-on-first-use dialogs
        └── Wait for prompt pattern + silence timeout
             │
3. Reader thread runs forever:
   ├── poll(master_fd) for input
   ├── os.read → append to buffer
   ├── respond_da() → answer terminal capability queries
   ├── is_prompt_detected() → signal completion
   └── Silence timeout → signal completion
```

### Completion Detection

Two complementary strategies ensure reliable response detection:

1. **Prompt-based detection** — When the reader thread sees a prompt character (`>`, `▶`, `❯`) at the end of the last output line, it signals completion. A watermark mechanism prevents the stale-prompt bug where an old prompt immediately triggers completion of a new request.

2. **Silence-based detection** — After a minimum wait of 5 seconds, if no new output arrives for 4 seconds, completion is signaled. This catches responses that don't end with a clean prompt (e.g., after `--print` mode calls or error states).

---

## 3. Features

- ✅ **Full interactive PTY** — Claude runs in a real pseudo-terminal, just like `claude` in your terminal
- ✅ **Automatic dialog dismissal** — Theme picker, security overview, trust prompts — all handled
- ✅ **Terminal DA response** — The reader thread automatically answers Claude's terminal capability queries
- ✅ **ANSI rendering** — `VirtualScreen` class reconstructs cursor-positioned output into clean text
- ✅ **Image support** — Send photos from Telegram, Claude analyzes them (base64 encoded)
- ✅ **Session persistence** — Optional `SESSION_ID` for maintaining Claude sessions across bot restarts
- ✅ **Model agnostic** — Works with Anthropic API, AWS Bedrock, GCP Vertex, or any custom proxy
- ✅ **Silence + prompt detection** — Dual completion detection for reliability
- ✅ **Systemd integration** — Ready-to-use service unit for production deployment
- ✅ **Single-user authorization** — Whitelist controls which Telegram users can interact
- ✅ **Safe output filtering** — Strips TUI artifacts, status lines, and protocol sections
- ✅ **10-minute timeout** — Handles long-running Claude responses

---

## 4. Prerequisites

| Requirement | Version / Notes |
|-------------|-----------------|
| Python | 3.10+ (tested on 3.11, 3.12) |
| Claude Code CLI | Installed and configured (`claude` in PATH) |
| Telegram Bot Token | From [@BotFather](https://t.me/BotFather) |
| Linux / macOS | Tested on Linux (OCI, Ubuntu, Debian); macOS likely works |
| Node.js | Required for Claude Code CLI (`npm install -g @anthropic-ai/claude-code`) |

---

## 5. Installation & Configuration

### 5.1 Get a Telegram Bot Token

1. Open Telegram and message [@BotFather](https://t.me/BotFather)
2. Send `/newbot` and follow the prompts
3. Save the bot token (looks like `1234567890:ABCdefGHIjklmNOPqrstUVwxyz`)
4. **Keep this token secret** — anyone with it can control your bot

### 5.2 Find Your Telegram User ID

1. Message [@userinfobot](https://t.me/userinfobot) on Telegram
2. It will reply with your user ID (a number like `123456789`)
3. Save this for the `ALLOWED_USER_IDS` configuration

### 5.3 Install Claude Code CLI

```bash
# Install globally via npm
npm install -g @anthropic-ai/claude-code

# Verify installation
claude --version

# Run once to authenticate (you'll need an Anthropic API key)
claude
# Follow the OAuth or API key setup
```

If you're using a custom backend (Bedrock, Vertex, proxy), configure environment variables before this step (see [5.7 Model Backend Configuration](#57-model-backend-configuration)).

### 5.4 Clone & Install the Bridge

```bash
# Clone the repository
git clone https://github.com/lenchos1982/tg-claude-pty.git
cd tg-claude-pty

# Install Python dependencies
pip install -r requirements.txt

# Create environment configuration
cp .env.example .env
```

### 5.5 Configure Environment Variables

Edit `.env` with your actual values:

```bash
# Required: Your Telegram Bot token from @BotFather
TELEGRAM_BOT_TOKEN=1234567890:ABCdefGHIjklmNOPqrstUVwxyz

# Required: Comma-separated Telegram user IDs allowed to use this bot
# Find yours via @userinfobot
ALLOWED_USER_IDS=123456789,987654321

# Optional: Path to claude binary (default: auto-resolved)
# Useful if claude is not in systemd's PATH
# CLAUDE_BIN=/home/claude/.nvm/versions/node/v20.0.0/bin/claude

# Optional: Session ID for persistence across restarts
# Leave empty for auto-generated sessions
# SESSION_ID=my-persistent-session
```

### 5.6 Run & Verify

```bash
# Test the bridge
python3 bot.py

# You should see:
#   INFO:tg-claude-pty:Claude Code PTY bridge ready
#   INFO:tg-claude-pty:tg-claude-pty started (...)
```

Open Telegram, find your bot, and send `/start`. Then try sending a message — you should get a response from Claude.

Press `Ctrl+C` to stop for now.

### 5.7 Model Backend Configuration

tg-claude-pty doesn't configure Claude's model backend — it inherits whatever Claude Code CLI is configured to use. Configure Claude before running the bridge.

**Option A: Anthropic API (default)**

```bash
claude  # Run once to authenticate via OAuth
```

Or use the API key directly:

```bash
export ANTHROPIC_API_KEY=sk-ant-xxxxxxxxxxxx
```

**Option B: AWS Bedrock**

```bash
export BEDROCK_AWS_REGION=us-east-1
export BEDROCK_ACCESS_KEY_ID=AKIAxxxxxxxxxx
export BEDROCK_SECRET_ACCESS_KEY=xxxxxxxxxxxx
```

**Option C: GCP Vertex AI**

```bash
export VERTEX_PROJECT_ID=my-gcp-project
export VERTEX_LOCATION=us-central1
# Also requires gcloud auth or service account key
```

**Option D: Custom Proxy / Open-Source Backend**

```bash
export ANTHROPIC_BASE_URL=https://my-proxy.example.com
export ANTHROPIC_API_KEY=sk-proxy-key-xxxxx
```

This lets you use Claude Code with any OpenAI-compatible or Anthropic-compatible backend, including litellm, Ollama (via proxy), or your own vLLM deployment.

### 5.8 Production Deployment (systemd)

```bash
# Copy project to deployment directory
sudo mkdir -p /opt/tg-claude-pty
sudo cp -r . /opt/tg-claude-pty/
cd /opt/tg-claude-pty

# Install dependencies (as the service user)
pip install -r requirements.txt

# Set up environment
sudo cp .env /opt/tg-claude-pty/.env
sudo chmod 600 /opt/tg-claude-pty/.env  # 🔒 Lock down secrets

# Install systemd service
sudo cp tg-claude-pty.service /etc/systemd/system/

# Optionally create a dedicated user
sudo useradd -r -s /bin/false -m -d /home/claude claude
sudo chown -R claude:claude /opt/tg-claude-pty

# Enable and start
sudo systemctl daemon-reload
sudo systemctl enable --now tg-claude-pty

# Check status
sudo systemctl status tg-claude-pty

# View logs
sudo journalctl -u tg-claude-pty -f
```

For custom model backends, add environment variables to the service file:

```ini
[Service]
Environment="ANTHROPIC_API_KEY=sk-ant-xxxxxxxxxxxx"
# Environment="ANTHROPIC_BASE_URL=https://my-proxy.example.com"
# Environment="BEDROCK_AWS_REGION=us-east-1"
```

Or add them to the `.env` file (recommended for cleanliness).

### 5.9 Configuring Claude Code Behavior

When the bridge starts Claude, it runs it with `--bare` mode, which skips Claude's project initialization. Claude loads its configuration from:

- `CLAUDE.md` in the working directory — project-level instructions and safety rules
- `~/.claude/settings.json` and `~/.claude/settings.local.json` — user-level configuration
- Environment variables — `ANTHROPIC_BASE_URL`, `ANTHROPIC_API_KEY`, etc.

See [Section 7: Security Configuration](#7-security-configuration--risk-warnings-️) for critical guidance on these files.

---

## 6. Usage

### Telegram Commands

| Command | Description |
|---------|-------------|
| `/start` | Show welcome message and available commands |
| `/help` | Show detailed usage instructions |
| `/new` | Reset Claude session (fresh conversation, context cleared) |
| `/stop` | Force-restart Claude (if stuck or unresponsive) |

### Basic Usage

Just send a text message to the bot. It will be forwarded to Claude CLI, and Claude's response will be sent back.

```
You:     What's the current date and time in UTC?

Bot:     ⏳ Processing...
Bot:     The current date and time in UTC is...
```

### Sending Images

Send a photo (as a photo, not a file) with an optional caption. Claude will analyze the image.

```
You:     [📷 photo of a code screenshot]
         What's wrong with this code?

Bot:     ⏳ Processing...
Bot:     Looking at your screenshot, I can see several issues:
         1. You're using a deprecated API...
         2. The variable x is undefined...
```

Long responses are automatically truncated to 4000 characters (Telegram's message limit) with a truncation notice.

### Session Management

- Claude maintains conversation context until you send `/new`
- Use `/new` to start a fresh session (context is lost)
- Use `/stop` if Claude appears stuck (happens rarely, mostly during long tool calls)
- The optional `SESSION_ID` environment variable lets you persist sessions across bot restarts

---

## 7. Security Configuration & Risk Warnings ⚠️

> **⚠️ This is the most important section in this README.**  
> The tg-claude-pty bridge gives a Telegram bot direct access to Claude Code CLI, which can **read files, write files, execute commands, and access your system**. Misconfiguration can have serious consequences.

### 7.1 CLAUDE.md — Your Safety Net

`CLAUDE.md` (in the project's working directory) is the **first line of defense**. It's read by Claude Code CLI at startup and instructs Claude on how to behave.

**What to put in it:**

```markdown
# Safety Rules

## Critical Rules
- NEVER share or expose any API keys, tokens, secrets, or credentials
- NEVER read files outside the project directory
- NEVER expose environment variables in responses
- NEVER execute destructive commands (rm -rf, format, etc.)
- NEVER modify system configuration files

## Allowed Operations
- Read and write files in the project directory
- Execute Python scripts for testing
- Read documentation files
- Answer questions about code
```

**What happens without it:** Claude has no constraints and will happily read your `.env` file (containing the Telegram bot token) and share it in a response. This would give Telegram message readers access to control the bot.

### 7.2 settings.local.json — Command Permission Control ⚠️

Claude Code CLI checks `~/.claude/settings.local.json` for an **allow list** that controls which shell commands Claude can execute.

**Example (safe):**

```json
{
  "permissions": {
    "allow": [
      "git status",
      "git diff",
      "git log",
      "git add -p",
      "git commit",
      "git push",
      "python3 -m pytest",
      "npm run build",
      "ls",
      "cat",
      "grep"
    ]
  }
}
```

**🚫 Dangerous patterns to AVOID:**

```json
{
  "permissions": {
    "allow": [
      "*"           // ❌ Gives Claude full shell access — DANGEROUS
    ]
  }
}
```

```json
{
  "permissions": {
    "allow": [
      "git *"       // ❌ Expansion: allows ANY git command including:
                    //    git push origin :main (delete remote branch)
                    //    git reset --hard HEAD~100 (destroy history)
                    //    git config --global user.email attacker@evil.com
      "gh *"        // ❌ Allows ANY gh command including:
                    //    gh repo delete my-org/production
                    //    gh secret set ... (modify GitHub secrets)
      "npm *"       // ❌ Allows ANY npm command including:
                    //    npm install malicious-package
                    //    npm run deploy:production
      "rm *"        // ❌ Allows ANY file removal
    ]
  }
}
```

**Why specific commands matter:** The allow list is not a sandbox — it's a permission list. `git *` doesn't just mean "useful git commands"; it means **every single git subcommand**, including destructive ones that could destroy your repository or leak secrets.

**Best practice:**
- List only the exact commands you need
- Review and update as requirements change
- Start restrictive and open up only when needed
- Never use wildcard patterns

### 7.3 Cross-Bot Isolation

If you run multiple Telegram bots on the same machine:

```
🚫 BAD:
One server / one .env / one bot.py
→ Claude can read ALL bot tokens from different .env files
→ One compromised Claude session leaks all bots

✅ GOOD:
Separate directories, separate user accounts, separate service units
→ /opt/tg-claude-pty-bot1/ (user: claude1)
→ /opt/tg-claude-pty-bot2/ (user: claude2)
→ Each has its own .env, its own CLAUDE.md, its own filesystem access
→ Claude in bot1 cannot read bot2's token
```

The systemd service file uses `ProtectHome=false` and `ReadWritePaths=/home/claude` by default. For multi-bot setups, restrict each service to its own directory.

### 7.4 Credential Protection

**Never hardcode credentials.** The `.env` file is the only safe place.

```bash
# 🔒 Set proper permissions
chmod 600 /opt/tg-claude-pty/.env
# Readable only by owner (claude user)

# ✅ Good: .gitignore already excludes .env from version control
# ✅ Good: CLAUDE.md should instruct Claude NEVER to read .env
# ❌ Bad: storing token in config.py or bot.py
# ❌ Bad: committing .env to git
# ❌ Bad: putting token in CLAUDE.md or settings.json
```

**.env file examples:**

```bash
# ✅ Safe
TELEGRAM_BOT_TOKEN=1234567890:ABC...

# 🚫 NOT DANGEROUS by itself but bad practice:
TELEGRAM_BOT_TOKEN=1234567890:ABC...  # ← If this file has 644 perms, any user can read it

# 🚫 DANGEROUS (hardcoded in code):
# Don't do this in bot.py:
# TELEGRAM_BOT_TOKEN = "1234567890:ABC..."  # ← Visible in git history forever
```

### 7.5 Principle of Least Privilege

Apply these principles:

| Area | Rule |
|------|------|
| **Bot user** | Run the bridge under a dedicated system user (`claude`), not root |
| **Filesystem** | Restrict Claude to its project directory |
| **Command execution** | Only allow specific, non-destructive commands |
| **Network access** | The bridge needs internet (Telegram API + model API); block unnecessary outbound |
| **Telegram users** | Always use `ALLOWED_USER_IDS` — never leave it empty in production |
| **Session timeout** | Long response timeout (600s) is reasonable; adjust down if needed |

**For extra security with systemd:**

```ini
[Service]
# Recommended hardening
NoNewPrivileges=true
PrivateTmp=true
ReadWritePaths=/opt/tg-claude-pty
ProtectSystem=full
CapabilityBoundingSet=
```

### 7.6 Risk Scenarios

**Scenario 1: Token Leakage**
```
Configuration:
  - ALLOWED_USER_IDS set to [user A]
  - CLAUDE.md has no rule about not reading .env
  - User A asks: "read .env"

Result:
  Claude reads .env and responds with the full bot token.
  This output is visible to user A.
  If user A is untrusted or their Telegram is compromised, the bot is stolen.

Mitigation:
  ✅ Add to CLAUDE.md: "NEVER read .env or expose secrets"
  ✅ Regularly rotate bot tokens
```

**Scenario 2: Command Injection via `git *`**
```
Configuration:
  - settings.local.json has `"allow": ["git *"]`
  - User sends: "push to origin but first delete the main branch remotely"

Result:
  Claude runs: git push origin :main
  Remote repository loses its main branch.
  Team is blocked until a force-push restores it.

Mitigation:
  ✅ Use specific commands: "git push origin main", not "git *"
```

**Scenario 3: Bot Takeover via Unauthorized Access**
```
Configuration:
  - ALLOWED_USER_IDS left empty (allow all)
  - Bot token somehow leaked or discovered
  - Attacker finds the bot on Telegram

Result:
  Attacker sends commands to Claude.
  Claude executes dangerous operations.
  No audit trail of who did what.

Mitigation:
  ✅ Always set ALLOWED_USER_IDS to known, trusted user IDs
  ✅ Never leave it empty in production
```

**Scenario 4: Cross-User Context Leakage**
```
Configuration:
  - Multiple users in ALLOWED_USER_IDS
  - User A asks Claude to read a sensitive file
  - User B later asks a question

Result:
  Claude's response to User B might reference User A's request.
  User B could learn about User A's private conversation.

Mitigation:
  ✅ Understand that this is a single-session bridge
  ✅ Use /new between different users
  ✅ Consider running separate instances for separate users
```

---

## 8. Troubleshooting

### Claude Fails to Start

**Symptoms:**
- "Bridge not running, starting..." message
- "Claude did not show prompt within 60s timeout"
- Bot just says "Sorry, I can't connect to Claude"

**Diagnosis & Fixes:**

1. **Is Claude installed?**
   ```bash
   which claude
   claude --version
   ```
   If not found, install: `npm install -g @anthropic-ai/claude-code`

2. **Is the binary path correct?**
   - For systemd, npm global bin may not be in PATH
   - Set `CLAUDE_BIN` in `.env` to the full path:
     ```bash
     CLAUDE_BIN=/home/claude/.nvm/versions/node/v20.0.0/bin/claude
     ```

3. **Check logs:**
   ```bash
   journalctl -u tg-claude-pty -n 50
   ```

4. **Test manually:**
   ```bash
   # Run as the same user
   sudo -u claude claude --bare
   # Wait 30-60 seconds — does it show a prompt?
   ```

5. **Startup timeout:** The bridge gives Claude up to 60 seconds to start. On slow systems or with large model downloads, this may not be enough. The startup loop sends Enter every 1.5 seconds to dismiss dialogs.

### Bot Not Responding

**Symptoms:**
- Messages sent to the bot get no reply
- No "typing" indicator appears

**Diagnosis & Fixes:**

1. **Is the bot running?**
   ```bash
   sudo systemctl status tg-claude-pty
   ```

2. **Is the token correct?**
   ```bash
   # Check the bot token in .env
   cat /opt/tg-claude-pty/.env | grep TELEGRAM_BOT_TOKEN
   # Verify with:
   curl https://api.telegram.org/bot<YOUR_TOKEN>/getMe
   ```

3. **Is your user ID authorized?**
   ```bash
   cat /opt/tg-claude-pty/.env | grep ALLOWED_USER_IDS
   ```

4. **Network issues:** Ensure the server can reach `api.telegram.org`:
   ```bash
   curl -s https://api.telegram.org/bot<YOUR_TOKEN>/getMe
   ```

### "Not logged in" / "Invalid API key"

**Symptom:** Claude starts but responds with authentication errors.

**Fixes:**

1. **Re-authenticate:**
   ```bash
   claude logout
   claude login  # Follow OAuth prompts
   ```

2. **Check ANTHROPIC_API_KEY:**
   ```bash
   echo $ANTHROPIC_API_KEY
   ```
   If using systemd, ensure the env var is in the service file or `.env`.

3. **Multiple auth methods conflict:** If both OAuth and ANTHROPIC_API_KEY are set, one may override the other. Use one method consistently.

### "Conflict: terminated by other getUpdates request"

**Symptom:** The bot stops responding, logs show a conflict error.

**Cause:** Another instance of the bot is running (using same token). Telegram allows only one long-polling connection per bot.

**Fixes:**
```bash
# Check for other bot processes
ps aux | grep bot.py

# Kill all instances
pkill -f "python3 bot.py"

# Or more forcefully:
killall -9 python3 bot.py

# Restart
sudo systemctl restart tg-claude-pty
```

### PTY Output Incomplete / Missing Content

**Symptom:** Claude's response is cut off, missing middle sections, or just returns a status line like "✻ Churned for 5s".

**Causes & Fixes:**

1. **Stale-prompt race:** The watermark mechanism prevents this, but if you see just a status line, the bridge may have detected the old prompt instead of waiting for new content. Send the message again.

2. **Response too fast:** For very short responses (< 5 seconds), prompt detection may trigger before VirtualScreen has rendered all content. Increase `MIN_RESPONSE_WAIT` in `pty_bridge.py`.

3. **ANSI rendering issues:** Complex Claude TUI output (tables, progress bars, multi-column layouts) may not render well through VirtualScreen. Check `journalctl` for the raw PTY output.

4. **Timeout reached:** The 600-second (10 minute) timeout is generous but long-running Claude operations (large file edits, extended analysis) can exceed it. Increase `SEND_TIMEOUT` in `bot.py` if needed.

### Bot Returns Empty or Gibberish Responses

**Symptom:** Claude responds with strange characters, empty messages, or ANSI escape sequences.

**Fixes:**
1. Use `/stop` to restart the bridge
2. Check logs for raw PTY data
3. If the issue persists, the ANTHROPIC_API_KEY may have expired or the model is returning malformed responses

---

## 9. File Structure

```
tg-claude-pty/
├── bot.py                  # Telegram bot logic
│                           # - PTB application setup (handlers, polling)
│                           # - Authorization check (ALLOWED_USER_IDS)
│                           # - Image download & base64 encoding
│                           # - MarkdownV2 formatting & escaping
│                           # - Response truncation (4000 char limit)
│
├── pty_bridge.py           # PTY bridge — the core engine
│                           # - PtyBridge class: PTY lifecycle
│                           # - PTY creation (pty.openpty)
│                           # - Claude subprocess management
│                           # - Reader thread (continuous PTY read)
│                           # - DA query response (terminal emulation)
│                           # - Startup dialog loop (Enter spam)
│                           # - Completion detection (prompt + silence)
│                           # - Output cleaning (TUI artifact filter)
│
├── output_parser.py        # Low-level PTY output utilities
│                           # - ANSI escape sequence stripping (regex)
│                           # - Braille spinner character removal
│                           # - Carriage return processing (\r)
│                           # - Terminal DA query definitions
│                           # - Prompt detection helpers
│
├── ansi_renderer.py        # Virtual terminal screen renderer
│                           # - VirtualScreen class
│                           # - CSI sequence processing (cursor, erase)
│                           # - Cursor positioning (CUP, CUD, CUF, CUB)
│                           # - Line/screen clearing (EL, ED)
│                           # - Scroll handling (scroll up/down)
│                           # - SGR (color/bold) stripping
│                           # - DCS, OSC, SOS sequence handling
│                           # - Full screen state reconstruction
│
├── config.py               # Environment variable loading
│                           # - TELEGRAM_BOT_TOKEN (required)
│                           # - CLAUDE_BIN (optional, auto-resolved)
│                           # - ALLOWED_USER_IDS (optional whitelist)
│                           # - SESSION_ID (optional persistence)
│                           # - claude binary path resolution
│
├── requirements.txt        # Python dependencies
│                           # - python-telegram-bot >= 20.0
│
├── .env.example            # Template for .env configuration
│                           # - Shows all possible env vars with comments
│                           # - Safe to commit (no real secrets)
│
├── .env                    # Actual environment configuration
│                           # - Listed in .gitignore (never committed)
│                           # - chmod 600 recommended
│                           # - Contains TELEGRAM_BOT_TOKEN and secrets
│
├── CLAUDE.md               # Claude Code instruction file
│                           # - Project-level safety rules
│                           # - Behavioral constraints for Claude
│                           # - Read by Claude CLI on startup
│                           # - Critical for security (see §7)
│
├── tg-claude-pty.service   # systemd service unit
│                           # - Production deployment config
│                           # - Runs as dedicated user (claude)
│                           # - Loads env vars from .env file
│                           # - Includes systemd hardening options
│
├── .gitignore              # Git exclusion rules
│                           # - Excludes .env, __pycache__, *.pyc
│                           # - Excludes IDE configs (.idea/, .vscode/)
│
└── README.md               # This file
```

---

## 10. Development & Contribution

### Development Setup

```bash
# Clone and install
git clone https://github.com/lenchos1982/tg-claude-pty.git
cd tg-claude-pty
pip install -r requirements.txt

# Create .env with a test bot token from @BotFather
cp .env.example .env

# Run in debug mode to see detailed PTY output
python3 bot.py
# Or modify bot.py to set level=logging.DEBUG
```

### Code Structure Notes

- **bot.py** is kept thin — it only handles Telegram protocol, formatting, and authorization. All PTY logic lives in **pty_bridge.py**.
- **pty_bridge.py** contains the main complexity: PTY lifecycle, reader thread, startup loop, completion detection, and output cleaning.
- **output_parser.py** and **ansi_renderer.py** are separate modules because they handle different levels of PTY output processing:
  - `output_parser.py`: Low-level ANSI stripping and carriage return handling
  - `ansi_renderer.py`: Full virtual terminal with cursor positioning

### Key Design Decisions

1. **PTY over --print mode**: Full PTY gives us interactive features (file editing, git integration, image analysis) that `--print` mode cannot provide.

2. **Reader thread over asyncio**: PTY reads are blocking I/O operations. A dedicated reader thread with `select.poll()` is more robust than trying to make PTY async with `asyncio`.

3. **Dual completion detection**: Neither prompt-based nor silence-based detection alone is reliable enough. Using both gives robust response detection across different Claude Code CLI versions and response patterns.

4. **VirtualScreen over raw ANSI stripping**: Simple ANSI stripping loses cursor-positioned output (progress bars, tables, multi-line edits). VirtualScreen reconstructs the final visible state of the terminal.

5. **No Docker**: The project is designed to be minimal and dependency-free. A PTY bridge doesn't benefit from Docker isolation as much as it benefits from direct host filesystem access.

### Testing

For thorough testing, consider:

- Testing with different Claude Code CLI versions
- Testing with different model backends (Anthropic, Bedrock, Vertex)
- Testing with image prompts
- Testing session persistence (SESSION_ID)
- Testing with long-running responses (> 5 minutes)

### Contributing

Contributions are welcome! Areas that could use improvement:

- **Multi-user support** (currently single-session, single-user)
- **Webhook mode** (vs. long polling, for production deployments)
- **Streaming responses** (Telegram supports progressive updates)
- **Token and rate-limit handling**
- **Docker support** (for containerized deployments)
- **Multi-bot management** (running multiple bridge instances)
- **Monitoring endpoints** (health check, metrics)
- **More robust output parsing** (for edge cases in VirtualScreen)

### License

This project is released under the MIT License.

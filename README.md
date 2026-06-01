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
│  │  /usr/bin/claude --permission-mode auto [--session-id XXX]  │   │
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
   ├── subprocess.Popen([claude, --permission-mode auto, --settings ..., --system-prompt-file ...], stdin=slave_fd, stdout=slave_fd, stderr=slave_fd)
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
| Node.js + npm | Required to install Claude Code CLI |
| Claude Code CLI | Installed globally via npm: `npm install -g @anthropic-ai/claude-code` |
| Telegram Bot Token | From [@BotFather](https://t.me/BotFather) |
| Linux / macOS | Tested on Linux (OCI, Ubuntu, Debian); macOS likely works |
| API Key / Backend | Anthropic API key, or DeepSeek API key (ANTHROPIC_BASE_URL + ANTHROPIC_AUTH_TOKEN), or other compatible proxy |

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

### 5.3 Install Node.js & Claude Code CLI

```bash
# Install nvm (recommended for managing Node.js versions)
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.1/install.sh | bash
source ~/.bashrc
nvm install 22

# Install Claude Code CLI globally via npm
npm install -g @anthropic-ai/claude-code

# Verify installation
claude --version
```

> **DeepSeek backend note**: If you're using DeepSeek as your model backend (via `ANTHROPIC_BASE_URL` pointing to DeepSeek's API), you need **Claude Code CLI <= 0.2.6**. Later versions introduced `--thinking` mode and tool-breaking changes that are incompatible with DeepSeek's Anthropic API compatibility layer. Pin the version with `npm install -g @anthropic-ai/claude-code@0.2.6`.

Note: If you're using a custom backend (Bedrock, Vertex, proxy), configure environment variables before this step (see [5.8 Model Backend Configuration](#58-model-backend-configuration)).

### 5.4 Clone & Install the Bridge

```bash
# Clone the repository
git clone <your-repo-url> tg-claude-pty
cd tg-claude-pty

# Install Python dependencies
pip install -r requirements.txt

# Create environment configuration
cp .env.example .env
```

### 5.5 Create the Assistant Identity Directory

tg-claude-pty uses a **separate directory** for the assistant's identity and rules. This keeps the assistant's CLAUDE.md isolated from the PTY project's development files, preventing the assistant from being confused by project documentation.

```bash
# Create the assistant directory (can be anywhere)
mkdir -p <assistant-dir>

# Create the assistant's identity file
cat > <assistant-dir>/CLAUDE.md << 'EOF'
# Assistant Identity

## Identity
You are [Name], an AI assistant communicating via Telegram.

## Behavior Rules

### Output Format
Your messages are sent via Telegram. Use plain text only.

**Forbidden:**
- Markdown tables
- ANSI escape codes / color codes
- Box-drawing characters
- Status emoji (✅❌⚠️)
- Terminal status lines (e.g. "✻ Brewed for 12s")
- Unicode decoration characters (◆▸▹▪▫⏺┏┓┗┛)
- Decorative divider lines (---, ===)

**Required:**
- Plain text with blank line paragraphs
- Simple lists with - or • markers
- Code blocks with ``` (Telegram supports markdown code blocks)
- Concise and direct

### Scope
- Do NOT touch other agent/bot configs (.env, tokens, credentials)
- Do NOT modify this bridge's code unless explicitly asked
EOF

# Create settings symlink so the assistant shares the PTY's permissions
mkdir -p <assistant-dir>/.claude
ln -s <project-dir>/.claude/settings.local.json <assistant-dir>/.claude/settings.local.json
```

### 5.6 Configure Environment Variables

Edit `.env` with your actual values:

```bash
# Required: Your Telegram Bot token from @BotFather
TELEGRAM_BOT_TOKEN=1234567890:ABCdefGHIjklmNOPqrstUVwxyz

# Required: Comma-separated Telegram user IDs allowed to use this bot
# Find yours via @userinfobot
ALLOWED_USER_IDS=123456789,987654321

# Optional: Path to claude binary (default: auto-resolved)
# CLAUDE_BIN=<path-to-claude>

# Optional: Session ID for persistence across restarts
# SESSION_ID=my-persistent-session
```

### 5.7 Run & Verify

```bash
# Test the bridge
python3 bot.py

# You should see:
#   INFO:tg-claude-pty:Claude Code PTY bridge ready
#   INFO:tg-claude-pty:tg-claude-pty started (...)
```

Open Telegram, find your bot, and send `/start`. Then try sending a message — you should get a response from Claude.

Press `Ctrl+C` to stop for now.

### 5.8 Model Backend Configuration

tg-claude-pty doesn't configure Claude's model backend — it inherits whatever Claude Code CLI is configured to use. Configure Claude before running the bridge.

**Option A: Anthropic API (default)**

```bash
claude  # Run once to authenticate via OAuth
```

Or use the API key directly:

```bash
export ANTHROPIC_API_KEY=sk-ant-xxxxxxxxxxxx
```

**Option B: DeepSeek Anthropic-Compatible API**

The bridge runs in non-bare mode, which requires `ANTHROPIC_AUTH_TOKEN` instead of `ANTHROPIC_API_KEY` (see FAQ for why).

```bash
export ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic
export ANTHROPIC_AUTH_TOKEN=sk-your-deepseek-api-key
export ANTHROPIC_MODEL=deepseek-v4-flash
export ANTHROPIC_DEFAULT_HAIKU_MODEL=deepseek-v4-flash
export ANTHROPIC_DEFAULT_SONNET_MODEL=deepseek-v4-flash
```

**Option C: AWS Bedrock**

```bash
export BEDROCK_AWS_REGION=us-east-1
export BEDROCK_ACCESS_KEY_ID=AKIAxxxxxxxxxx
export BEDROCK_SECRET_ACCESS_KEY=xxxxxxxxxxxx
```

**Option D: GCP Vertex AI**

```bash
export VERTEX_PROJECT_ID=my-gcp-project
export VERTEX_LOCATION=us-central1
# Also requires gcloud auth or service account key
```

**Option E: Custom Proxy / Open-Source Backend**

```bash
export ANTHROPIC_BASE_URL=https://my-proxy.example.com
export ANTHROPIC_API_KEY=sk-proxy-key-xxxxx
```

This lets you use Claude Code with any OpenAI-compatible or Anthropic-compatible backend, including litellm, Ollama (via proxy), or your own vLLM deployment.

### 5.9 Production Deployment (systemd)

```bash
# Copy project to deployment directory
sudo mkdir -p <deploy-dir>
sudo cp -r . <deploy-dir>/
cd <deploy-dir>

# Install dependencies (as the service user)
pip install -r requirements.txt

# Set up environment
sudo cp .env <deploy-dir>/.env
sudo chmod 600 <deploy-dir>/.env  # 🔒 Lock down secrets

# Install systemd service
sudo cp tg-claude-pty.service /etc/systemd/system/
```

**Edit the service file** to match your paths:

```bash
sudo vim /etc/systemd/system/tg-claude-pty.service
```

Make sure these values are correct:

```ini
[Service]
WorkingDirectory=<deploy-dir>
EnvironmentFile=<deploy-dir>/.env
Environment=PATH=<your-node-bin-path>:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin
Environment=HOME=<home-dir>
# For DeepSeek or custom proxy:
Environment=ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic
Environment=ANTHROPIC_AUTH_TOKEN=sk-your-key
Environment=ANTHROPIC_MODEL=deepseek-v4-flash
Environment=ANTHROPIC_DEFAULT_HAIKU_MODEL=deepseek-v4-flash
Environment=ANTHROPIC_DEFAULT_SONNET_MODEL=deepseek-v4-flash
```

Then enable and start:

```bash
# Optionally create a dedicated user
sudo useradd -r -s /bin/false -m -d <home-dir> <user>
sudo chown -R <user>:<user> <deploy-dir>

# Enable and start
sudo systemctl daemon-reload
sudo systemctl enable --now tg-claude-pty

# Check status
sudo systemctl status tg-claude-pty

# View logs
sudo journalctl -u tg-claude-pty -f
```

### 5.10 Configuring Claude Code Behavior

tg-claude-pty runs Claude Code in **non-bare mode** so it loads `~/.claude/CLAUDE.md` (global development rules). The assistant's identity rules are loaded separately through `--system-prompt-file` pointing to `<assistant-dir>/CLAUDE.md`. Both files merge cleanly — the global rules govern coding discipline and workflow, while the assistant's rules govern output format and behavior.

The complete launch command the bridge uses:

```bash
claude --permission-mode auto \
  --settings <project-dir>/.claude/settings.local.json \
  --system-prompt-file <assistant-dir>/CLAUDE.md
```

Claude also loads configuration from:
- `~/.claude/settings.json` and `~/.claude/settings.local.json` — user-level configuration
- Environment variables — `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`, etc.

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

2. **Is Node.js installed?** Claude Code requires Node.js:
   ```bash
   node --version  # Should be 18+
   ```
   If missing, install via nvm: `curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.1/install.sh | bash`

3. **Is the binary path correct?**
   - For systemd, npm global bin may not be in PATH
   - Set `CLAUDE_BIN` in `.env` to the full path:
     ```bash
     CLAUDE_BIN=<path-to-claude>
     ```

4. **Check logs:**
   ```bash
   journalctl -u tg-claude-pty -n 50
   ```

5. **Test manually:**
   ```bash
   # Run as the same user
   sudo -u <user> claude --permission-mode auto
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

1. **Re-authenticate (Anthropic API):**
   ```bash
   claude logout
   claude login  # Follow OAuth prompts
   ```

2. **Check your API token environment variable:**
   - For Anthropic API: `ANTHROPIC_API_KEY`
   - For DeepSeek / custom proxy in non-bare mode: `ANTHROPIC_AUTH_TOKEN` (see FAQ)
   ```bash
   echo $ANTHROPIC_AUTH_TOKEN
   # or
   echo $ANTHROPIC_API_KEY
   ```
   If using systemd, ensure the env var is in the service file.

3. **Multiple auth methods conflict:** If both OAuth and ANTHROPIC_API_KEY/ANTHROPIC_AUTH_TOKEN are set, one may override the other. Use one method consistently.

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
<project-dir>/                        # tg-claude-pty project root
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
│
├── .env                    # Actual environment configuration
│                           # - Listed in .gitignore (never committed)
│                           # - chmod 600 recommended
│
├── .claude/
│   └── settings.local.json # Claude Code permissions config
│                           # - Permissions allow list
│                           # - chmod 600 recommended
│
├── tg-claude-pty.service   # systemd service unit template
│                           # - Production deployment config
│                           # - Update paths before use
│
├── .gitignore              # Git exclusion rules
└── README.md               # This file

### Separate Assistant Directory

The assistant's identity file (CLAUDE.md) lives in a **separate directory** — not inside the PTY project:

```
<assistant-dir>/
├── CLAUDE.md                 # Assistant identity rules (loaded via --system-prompt-file)
└── .claude/
    └── settings.local.json   # → symlink to <project-dir>/.claude/settings.local.json
```

### Why Separate Directories?

This architecture keeps the assistant from seeing the PTY project's development documentation in its working directory. The assistant only knows its identity and behavior rules from `<assistant-dir>/CLAUDE.md` — no project architecture docs, no implementation details to confuse it.

The assistant also loads `~/.claude/CLAUDE.md` (global development rules, loaded since the bridge runs in non-bare mode). These two CLAUDE.md files serve different purposes and don't conflict:
- `~/.claude/CLAUDE.md` — coding discipline, git workflow, verification standards
- `<assistant-dir>/CLAUDE.md` — assistant identity, output format, scope limitations

---

## 10. FAQ

### Why no `--bare`?

`--bare` mode disables Claude Code's subagent system, which is needed for complex multi-step tasks (like large refactors or extended research). Running without `--bare` gives the assistant full Claude Code capabilities.

In non-bare mode, Claude also loads `~/.claude/CLAUDE.md` (global development rules), which merges with the assistant-specific rules from `--system-prompt-file`. These serve different purposes and don't conflict.

### Why `ANTHROPIC_AUTH_TOKEN` instead of `ANTHROPIC_API_KEY`?

Claude Code in non-bare mode reads `ANTHROPIC_AUTH_TOKEN` for API authentication and skips OAuth. Using `ANTHROPIC_AUTH_TOKEN` with `ANTHROPIC_BASE_URL` tells Claude to bypass its normal OAuth flow and connect directly to the specified endpoint — which is how we use DeepSeek's Anthropic-compatible API.

If both `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN` are set, the behavior is undefined — use only one.

### What value goes in `ANTHROPIC_AUTH_TOKEN`?

Your DeepSeek API key (format: `sk-...`). Claude Code sends this as the `x-api-key` header to the Anthropic-compatible endpoint.

### Why is the assistant's CLAUDE.md in a separate directory?

To prevent cognitive confusion. If the assistant's working directory contains the PTY project's source code and documentation, it may accidentally interpret project internals as instructions. Keeping the assistant in a separate directory with only its identity rules ensures it focuses on the user's requests.

### Can I use multiple assistants with one bridge?

Not directly — one bridge instance runs one Claude session. For multiple assistants, run multiple bridge instances with different bot tokens and assistant directories.

### What if Claude gets stuck on a subagent task?

Use `/stop` to force-restart Claude, or wait for the timeout (10 minutes by default). Subagent tasks in non-bare mode can run longer than simple responses.

---

## 11. Known Limitations ⚠️

This project works well for its intended use case, but it's important to understand its limitations:

### 11.1 Terminal Echo Artifacts (Echo Bug)

Occasionally, the terminal echo from the PTY is not fully filtered from Claude's responses. You may see parts of your input echoed back in the response. The output cleaning logic (`_clean_output` in `pty_bridge.py`) handles most cases, but edge cases remain — especially with longer prompts or complex command sequences.

### 11.2 Interactive Prompts (Yes/No, Menus)

Claude Code CLI sometimes presents interactive prompts — Yes/No confirmations, numbered menus, or multi-select options. **The PTY bridge cannot automatically respond to these.** If Claude enters an interactive prompt state, the bridge may time out or return an incomplete response.

Mitigation: Use `/stop` to restart Claude, or configure `settings.local.json` to deny/direct commands that trigger interactive flows.

### 11.3 Browser Auto-Open & Async Push Notifications

Features that require opening a browser (OAuth flows, result URLs) or async push from Claude Code CLI are **not supported**. The bridge works synchronously through the PTY — Claude must complete its response before it's sent back to Telegram.

### 11.4 No Subagent Scheduling or Memory Layer

Unlike the OpenClaw ACP framework, this bridge has:
- **No subagent system** — it's single-threaded, single-session
- **No persistent memory layer** — no built-in RAG, vector search, or knowledge base
- **No multi-model orchestration** — it runs whatever Claude Code CLI is configured to use

### 11.5 Single-Session Architecture

The bridge maintains only one Claude session at a time. If multiple Telegram users share a bot (via `ALLOWED_USER_IDS`), they share the same Claude conversation context. There is no per-user session isolation.

### 11.6 No Streaming Responses

Telegram supports progressive message updates, but currently the bridge waits for Claude to finish its entire response before sending it. Long-running Claude operations (file edits, extended analysis) may take several minutes.

---

## 12. Development & Contribution

### Development Setup

```bash
# Clone and install
git clone https://github.com/YOUR_USERNAME/tg-claude-pty.git
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

---

## 2026-06-01 Fix: 核心 Bug 修復

### Bug 1 修復：Echo 截斷正常回覆
- **根因**：`send_task()` 寫入 PTY 後，終端機 echo 行中的 ❯ 被 reader thread 的 prompt
  檢測邏輯誤判為完成訊號，導致任務在 Claude 真正回覆前就提前結束。
- **修復**：
  - 新增 `_is_prompt_from_echo()` 方法，透過比對 prompt 行是否匹配 "❯ + 用戶輸入"
    模式來識別 echo prompt，將其從完成檢測中排除。
  - 新增 content gate：任務完成前要求至少 150 bytes 新內容（防止長中文 echo
    超過 200 bytes 閾值導致漏判）。
  - 取代舊的固定 200-byte 距離閾值 heuristic（對 UTF-8 長中文提示不可靠）。

### Bug 2 修復：歷史回覆疊加到新回覆
- **根因**：`send_task()` 的 VirtualScreen snapshot 在 PTY 寫入前取得，不包含 echo
  內容。reader thread 將 echo 寫入 VirtualScreen 後，snapshot 已過時，導致
  `get_new_text_since()` 可能把舊 session 內容也當作「新增」。
- **修復**：echo skip 成功後立即重新 snapshot VirtualScreen，確保 diff 基線
  在 echo 區域之後。
- **備援機制**：`_extract_task_summary()` 新增 raw buffer fallback — 當
  VirtualScreen diff 結果過短時，改用原始 PTY buffer 提取。

### Bug 3 確認與修復：Buffer 無限增長
- **結論**：屬實。`_buffer` (bytearray) 自 PTY 啟動後從未裁剪。長時間 session
  可增長至數十 MB。
- **修復**：
  - 新增 `MAX_BUFFER_BYTES = 10 MiB` 上限
  - `_trim_buffer_if_needed()` 在 reader thread 每次 append 時檢查並裁剪
    過舊前綴，同時調整所有 positional references。
  - 直接影響 correctness 的 prompt 檢測只掃描最後 2048 bytes，不受裁剪影響。

### Bug 4 分析
- 無法正常收到回覆的問題主要由 Bug 1 導致（echo 觸發 premature completion，
  回傳空白內容）。Bug 1 修復後應已解決。
- 若 VirtualScreen diff 遺漏輸出，raw buffer fallback 機制提供備援路徑。

### 其他修復
- 補回遺失的 `_cleanup()` 方法（舊程式碼多處調用但從未定義，會觸發
  `AttributeError`）。
- 更新 `.gitignore` 排除 backup 目錄和測試腳本。

### 改動檔案
- `pty_bridge.py`：核心修復
- `ansi_renderer.py`：新增 `snapshot()` / `get_new_text_since()`（已存在於
  工作目錄，本次一併提交）
- `README.md`：本文檔
- `.gitignore`：排除規則更新

### License

This project is released under the MIT License. See [LICENSE](LICENSE) for details.

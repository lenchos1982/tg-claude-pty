# tg-claude-pty V2 — Telegram ↔ Claude Code PTY Bridge

> 在 Telegram 上使用 Claude Code CLI，不需要開著終端機。

---

## V2 核心改進

V2 基於 `claude -p --continue` 架構重寫輸出提取層，解決了 V1 的根本問題：

| | V1（PTY 輸出提取） | V2（claude -p --continue） |
|---|---|---|
| 輸出提取 | 從 PTY buffer 解析 ANSI/TUI | `claude -p` stdout 純文字 |
| TUI 相容性 | 依賴版本特定的 box-drawing 過濾 | 與 TUI 無關，所有版本通用 |
| Session 上下文 | PTY 互動 session | `--continue` 自動連接同一 session |

架構：**PTY 進程僅管理 Claude 生命週期**（啟動、輸入寫入、session 保持），實際回覆透過獨立的 `claude -p --continue` 子進程獲取純文字輸出。

---

## 版本需求

建議使用 **Claude Code CLI 2.1.148**：

```bash
npm install -g @anthropic-ai/claude-code@2.1.148
```

也相容 0.2.x 系列。其他版本未經完整測試。

---

## 安裝

### 1. 前置條件

- Python 3.10+
- Node.js 22+
- Claude Code CLI（見上方版本需求）
- Telegram Bot Token（從 [@BotFather](https://t.me/BotFather) 取得）
- Telegram User ID（從 [@userinfobot](https://t.me/userinfobot) 取得）

### 2. 安裝 Claude Code CLI

```bash
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.1/install.sh | bash
source ~/.bashrc
nvm install 22
npm install -g @anthropic-ai/claude-code@2.1.148
claude --version
```

### 3. 安裝 Bridge

```bash
git clone <your-repo-url> tg-claude-pty
cd tg-claude-pty
pip install -r requirements.txt
cp .env.example .env
```

### 4. 建立助手身份目錄

Bridge 使用獨立的助手目錄，將 CLAUDE.md 與專案程式碼隔離：

```bash
mkdir -p /root/chuxi
cat > /root/chuxi/CLAUDE.md << 'EOF'
# 身份與規則

## 輸出格式
你的訊息透過 Telegram 發送，請使用純文字：
- 禁止 Markdown 表格、ANSI 代碼、box-drawing 字元
- 禁止狀態 emoji（✅❌⚠️）和終端機裝飾線
- 段落之間用空行分隔
- 程式碼用 ``` 包裹（Telegram 支援）

## 安全
- 絕不讀取或洩露 .env / token / 密鑰
- 絕不執行破壞性指令
EOF

# 建立 settings 符號連結（共用 PTY 的權限設定）
mkdir -p /root/chuxi/.claude
ln -s $(pwd)/.claude/settings.local.json /root/chuxi/.claude/settings.local.json
```

### 5. 設定環境變數

編輯 `.env`：

```bash
TELEGRAM_BOT_TOKEN=你的_bot_token
ALLOWED_USER_IDS=你的_user_id
```

---

## 執行

```bash
python3 bot.py
```

看到 `Claude Code PTY bridge ready` 就是成功了。在 Telegram 對 bot 發送 `/start` 開始。

### systemd 部署（生產環境）

```bash
sudo cp tg-claude-pty.service /etc/systemd/system/
sudo vim /etc/systemd/system/tg-claude-pty.service  # 修改路徑
sudo systemctl daemon-reload
sudo systemctl enable --now tg-claude-pty
sudo journalctl -u tg-claude-pty -f
```

---

## Telegram 命令

| 命令 | 說明 |
|---|---|
| `/start` | 顯示歡迎訊息 |
| `/help` | 顯示使用說明 |
| `/new` | 重置 session（清除上下文） |
| `/stop` | 強制重啟 Claude（卡住時使用） |
| `/cancel` | 取消目前任務 |

直接發送文字訊息即可與 Claude 對話。發送圖片（非檔案）可讓 Claude 分析圖片內容。

---

## 權限安全 ⚠️

**`settings.local.json`：** 絕不使用萬用字元（`"*"`、`"git *"`）。只列出確切需要的指令：`"git status"`、`"ls"`、`"cat"`。

**`CLAUDE.md`：** 必須聲明「絕不讀取 .env / token / 密鑰」、「絕不執行破壞性指令」。

**多 Bot：** 使用獨立目錄與獨立系統使用者，避免跨 bot 洩漏。

---

## 模型後端設定

Bridge 不直接設定模型——它繼承 Claude Code CLI 的配置。

**Anthropic API（預設）：**
```bash
export ANTHROPIC_API_KEY=sk-ant-xxxxxxxxxxxx
```

**DeepSeek（Anthropic 相容 API）：**
```bash
export ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic
export ANTHROPIC_AUTH_TOKEN=sk-your-deepseek-key
export ANTHROPIC_MODEL=deepseek-v4-flash
export ANTHROPIC_DEFAULT_HAIKU_MODEL=deepseek-v4-flash
export ANTHROPIC_DEFAULT_SONNET_MODEL=deepseek-v4-flash
```

**自訂代理：**
```bash
export ANTHROPIC_BASE_URL=https://your-proxy.example.com
export ANTHROPIC_API_KEY=sk-proxy-key
```

---

## 疑難排解

**Claude 無法啟動：**
```bash
which claude && claude --version    # 確認已安裝
node --version                       # 需要 Node 18+
journalctl -u tg-claude-pty -n 50   # 查看日誌
```

**Bot 沒有回應：**
```bash
sudo systemctl status tg-claude-pty  # 確認執行中
# 檢查 ALLOWED_USER_IDS 是否包含你的 user ID
# 確認 Telegram Bot Token 正確
```

**Claude 卡住：** 發送 `/stop` 強制重啟。

---

## 檔案結構

```
tg-claude-pty/
├── bot.py              # Telegram bot（python-telegram-bot）
├── pty_bridge.py        # PTY 管理 + claude -p 輸出提取
├── output_parser.py     # ANSI 清理、prompt 偵測
├── ansi_renderer.py     # VirtualScreen（PTY fallback 用）
├── config.py            # 環境變數載入
├── run_bot.sh           # 啟動腳本
├── tg-claude-pty.service # systemd 單元
├── .env.example         # 環境變數範本
└── .claude/
    └── settings.local.json  # 指令權限白名單
```

---

## 授權

MIT

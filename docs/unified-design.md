# Unified Design: Output Optimization + Task Mode

> 統籌考慮兩個需求的改動，確保不互相干擾

---

## 目錄

1. [問題背景](#1-問題背景)
2. [核心概念：Mode Flag](#2-核心概念mode-flag)
3. [改動文件清單](#3-改動文件清單)
4. [交互分析：Reader Loop 的雙模行為](#4-交互分析reader-loop-的雙模行為)
5. [統一流程圖](#5-統一流程圖)
6. [代碼改動對照表](#6-代碼改動對照表)
7. [實作優先級](#7-實作優先級)
8. [避免衝突的策略](#8-避免衝突的策略)

---

## 1. 問題背景

### 1.1 現有 Bug：輸出截斷

**症狀**：Claude Code 輸出較長時，PTY bridge 過早截斷，用戶只看到上一個指令最後幾句話。

**根因**：`_reader_loop` 中的 prompt completion detection 存在三類誤判：

1. **Prompt 字符誤判**：Claude 輸出中的 `>`、`->`、`❯` 等聊天字符被當成 prompt 標誌
2. **靜默期誤判**：Claude 思考中 >2 秒的停頓被當成「輸出結束」
3. **TUI 重繪誤判**：Claude 在輸出中頻繁重繪 TUI 時 prompt 字符短暫出現後又消失

**當前代碼**（pty_bridge.py `_reader_loop`）已有 2 秒靜默期過濾，《需要加強 prompt detection 本身》。

### 1.2 新需求：任務模式

**目的**：讓用戶可以提交長時間任務（>30 分鐘），Claude 在背景執行，不輸出中間結果，完成後只發摘要。

**關鍵語義**：
- 任務模式下 reader loop **依然需要 prompt detection**（知道何時完成）
- 但完成後**不立即返回結果給用戶**，而是用於觸發摘要提取
- 任務模式**不保留**對話模式的「即時輸出轉發」特性

### 1.3 兩者關係

```
輸出優化 ←── 改進 prompt detection（更準確判斷完成）
                  │
                  ▼ ┌── 對話模式：完成 = 立即返回全部輸出
  共用改進後的 ─────┤
  prompt detection　 └── 任務模式：完成 = 觸發摘要提取 + 最終返回
```

**核心洞察**：無論哪個模式，prompt detection 的可靠性都是關鍵。輸出優化改進 detection 本身，任務模式只是消費 detection 結果的方式不同。

---

## 2. 核心概念：Mode Flag

### 2.1 _task_mode 定義

在 `PtyBridge.__init__` 新增：

```python
# ── Task Mode ──
self._task_mode = False          # True = 任務模式, False = 對話模式
self._task_prompt = ""           # 當前任務的原始 prompt
self._task_start_time = 0.0     # 任務開始時間戳
self._task_completed = threading.Event()  # 任務完成信號
self._task_result = ""           # 任務結果（完成後寫入）
self._task_cancelled = threading.Event()  # 用戶取消信號
self._task_send_start_pos = 0    # 任務 send() 開始時的 buffer 位置
```

### 2.2 Mode 的生命週期

```
對話模式 ──(/task xxx)──> 任務模式 ──(完成/取消)──> 對話模式
     ^                                                 │
     └─────────────────(/chat 手動)────────────────────┘
```

| 事件 | 轉變 | 觸發方式 |
|------|------|----------|
| 用戶發送 `/task xxx` | 對話 → 任務 | bot.py `/task` handler |
| 任務完成 | 任務 → 對話 | reader thread detect prompt |
| 用戶 `/cancel` | 任務 → 對話 | bot.py `/cancel` handler |
| 用戶 `/chat` | 任務 → 對話 | bot.py `/chat` handler |
| 任務中 `/stop` | 任務 → 對話 | bot.py `/stop` handler |

### 2.3 Mode 對各組件的影響

| 組件 | 對話模式下 | 任務模式下 |
|------|-----------|-----------|
| **reader thread prompt detection** | 檢測到 prompt → set response_event → send() 返回 | 檢測到 prompt → set task_completed → 觸發摘要提取 |
| **send()** | 同步等待 response_event | 提交 prompt → 立即返回 ✅ |
| **_clean_output** | 完整清理 TUI artifacts | 簡化清理 + 壓縮摘要 |
| **handle_message** | 轉發給 Claude | 拒絕：提示任務運行中 |
| **msg.edit_text** | 替換「⏳ Processing...」 | 替換「✅ 任務已接收...」或發送最終結果 |

---

## 3. 改動文件清單

### 3.1 輸出優化（需求 A）—— 僅改 pty_bridge.py

| # | 函數/方法 | 改動 | 說明 |
|---|-----------|------|------|
| A1 | `_reader_loop` | 改進 prompt completion detection | 核心改動，見 4.1 |
| A2 | `_check_prompt_detected_with_pos` | 添加 prompt stability check | 防止短暫出現的 prompt 字符觸發完成 |
| A3 | 新增 `_prompt_is_stable()` | 檢查 prompt 字符是否穩定 | 輸出結束後才認為 prompt 有效 |
| — | output_parser.py | **無需改動** | `is_prompt_detected` 函數本身正確；誤判在 reader loop 層級 |

### 3.2 任務模式（需求 B）—— 改 3 個文件

| # | 文件 | 修改點 | 說明 |
|---|------|--------|------|
| B1 | **config.py** | 新增 `TASK_SUMMARY_MAX_LENGTH` | 摘要最大長度（預設 3000） |
| B2 | **pty_bridge.py** | `__init__` 新增 task mode fields | 見 2.1 |
| B3 | pty_bridge.py | 新增 `set_task_mode(bool)` | 切換模式 |
| B4 | pty_bridge.py | 修改 `reset()` | 同時 reset task mode fields |
| B5 | pty_bridge.py | 新增 `send_task(text)` | 提交任務，立即返回 |
| B6 | pty_bridge.py | 修改 `_reader_loop` prompt detection | 任務模式下改觸發 task_completed |
| B7 | pty_bridge.py | 新增 `_wait_for_task_completion()` | 背景等待任務完成 |
| B8 | pty_bridge.py | 新增 `cancel_current_task()` | 取消任務（Ctrl+C → SIGTERM） |
| B9 | pty_bridge.py | 新增 `_extract_task_summary(raw)` | 從 buffer 提取壓縮摘要 |
| B10 | **bot.py** | 新增 `/task` CommandHandler | 接收任務 |
| B11 | bot.py | 新增 `/cancel` CommandHandler | 取消任務 |
| B12 | bot.py | 新增 `/chat` CommandHandler | 切回對話模式 |
| B13 | bot.py | 修改 `handle_message` | 任務模式下拒絕普通消息 |
| B14 | bot.py | 新增 `_handle_task_completion` (async) | 任務完成後發送結果 |
| B15 | bot.py | 修改 `/stop`, `/new` handler | task mode aware |

### 3.3 共用改動（兩需求都涉及的）

| # | 文件 | 修改點 | 交互原因 |
|---|------|--------|----------|
| C1 | **pty_bridge.py** `_reader_loop` | prompt detection 強化 + mode 分發 | **核心複雜點**，見第四章 |
| C2 | pty_bridge.py `__init__` | 新增 `_prompt_stable_since` | 輸出優化的穩定性檢測欄位 |
| C3 | pty_bridge.py `__init__` | 新增 task mode fields | 任務模式的 flag + event |

### 3.4 不改的文件

| 文件 | 理由 |
|------|------|
| `output_parser.py` | `is_prompt_detected` 本身正確；誤判是 reader loop 層的問題 |
| `ansi_renderer.py` | 渲染邏輯不變；任務模式只是選擇是否渲染 |
| `config.py` | 僅新增一個常數，不改變現有邏輯 |
| `README.md` | 文檔更新，非代碼改動 |

---

## 4. 交互分析：Reader Loop 的雙模行為

### 4.1 現有 prompt detection（有問題）

現有 `_reader_loop` 的關鍵片段：

```python
# 當前代碼（簡化）
if self._expecting_response and elapsed >= 0.5:
    prompt_detected, prompt_pos = self._check_prompt_detected_with_pos()
    if prompt_detected and prompt_pos > self._send_prompt_watermark:
        quiet_needed = 2.0
        if self._last_data_time > 0:
            silence = now - self._last_data_time
            if silence >= quiet_needed:
                self._response_event.set()
```

**問題**：
1. `_check_prompt_detected_with_pos` 只看 tail 2048 bytes
2. 如果最後 2048 bytes 中包含 prompt 字符，就認為完成
3. 2 秒靜默期不夠——Claude 思考、打印 TUI status、生成代碼時的停頓 >2 秒就會誤判
4. VirtualScreen 渲染後的 prompt 字符和原始 buffer 中的 prompt 字符之間有偏移

### 4.2 改進後的 prompt detection（輸出優化）

**策略**：Prompt Stability Check + 更長的穩定期

```python
# 改進後（偽代碼）
if self._expecting_response and elapsed >= 0.5:
    prompt_detected, prompt_pos = self._check_prompt_detected_with_pos()
    if prompt_detected and prompt_pos > self._send_prompt_watermark:
        # ── 新增：Prompt Stability Check ──
        # 持續檢查 prompt 字符是否穩定存在
        # 如果 5 秒內持續檢測到 prompt 字符，才認為是真正的完成
        if not hasattr(self, '_prompt_first_seen'):
            self._prompt_first_seen = now
            self._prompt_stable_since = None
        elif now - self._prompt_first_seen >= 2.0:
            # 2 秒後開始檢查穩定性
            if self._prompt_stable_since is None:
                self._prompt_stable_since = now
            
            # ── 新增：Buffer 增長檢測 ──
            # 如果在穩定期內 buffer 仍有增長，reset 穩定計時器
            # 這能防止 Claude 一邊輸出提示文字一邊出現 prompt 字符的情況
            with self._buf_lock:
                if len(self._buffer) > self._last_stable_buffer_len:
                    self._prompt_stable_since = now  # reset
                self._last_stable_buffer_len = len(self._buffer)
            
            if now - self._prompt_stable_since >= 3.0:
                # 連續 3 秒沒有新數據 + prompt 持續可見 = 真正的完成
                self._response_event.set()
```

**對比**：

| 維度 | 現有 | 改進後 |
|------|------|--------|
| 靜默期 | 2 秒 | 3 秒（從 prompt 首次出現+2秒後開始算） |
| 穩定性檢測 | 無 | 有：2 秒初始化 + 3 秒穩定窗口 |
| Buffer 增長檢測 | 無 | 有：穩定窗口內有新數據則 reset |
| 對 {>→❯} 誤判的容錯 | 低 | 中（仍需 balance） |

### 4.3 任務模式下的 reader loop 行為

```python
# 在改進後的 prompt detection 基礎上，添加 mode 分發
if self._task_mode:
    # 任務模式：觸發 task_completed（不自動設 response_event）
    self._task_completed.set()
else:
    # 對話模式：保持現有行為，設 response_event
    self._response_event.set()
```

**任務模式特有的 reader loop 行為**：

| 狀態 | 行為 |
|------|------|
| 收到新數據 | 正常累積到 buffer（同對話模式） |
| DA queries | 正常回應（同對話模式） |
| Auth prompts | **正常 auto-respond**（同對話模式） |
| Prompt detected | 設 `task_completed`，**不**設 `response_event` |
| _last_data_time 更新 | 正常（同對話模式） |
| 輸出 burst 追蹤 | 正常（同對話模式） |
| Buffer 增長檢測 | 正常（同對話模式） |

### 4.4 send() 的行為差異

```python
async def send(self, text, timeout=0):
    """對話模式 send（不變）"""
    # ... echo skip, wait_for_response ...
    remote_rendered = VirtualScreen.render(raw)
    return self._clean_output(rendered)

async def send_task(self, text, timeout=0):
    """任務模式 send（新增）"""
    # 記錄起始位置
    # 提交 prompt，不清除 response_event（任務模式不用這個）
    # 設 task_send_start_pos
    # 設 _task_mode = True
    # 立即返回
    return "✅ 任務已接收，將在背景執行"
```

### 4.5 摘要提取

```python
def _extract_task_summary(self, raw_text: str) -> str:
    """
    從任務完成的 buffer 內容提取摘要。
    
    Strategy:
    1. VirtualScreen 渲染（同對話模式）
    2. 基本的 TUI artifact 清理
    3. 如果內容 > TASK_SUMMARY_MAX_LENGTH，壓縮：
       - 保留開頭的 context（∼20%）
       - 保留結尾的結果（∼60%）
       - 中間用省略提示替代
    4. 返回最終文本
    """
```

**摘要壓縮策略**：

```
原始輸出：
  ┌──────────────────────────────────────┐
  │ 分析過程...                           │  30%
  ├──────────────────────────────────────┤
  │ 代碼修改...                           │  40%
  ├──────────────────────────────────────┤
  │ 最終結果 + 總結                        │  30%
  └──────────────────────────────────────┘
  
壓縮後（簡化版，保留頭尾）：
  ┌──────────────────────────────────────┐
  │ 分析過程（前 20%）                     │
  │ ... （省略中間，原始 X 字元）...         │
  │ 最終結果（後 60%）                     │
  └──────────────────────────────────────┘
```

---

## 5. 統一流程圖

### 5.1 對話模式流程（改進後）

```
User text ──> bot.py handle_message()
                  │
                  ├── check: task mode? → No
                  │
                  ├── _process_request_in_lock()
                  │       │
                  │       ├── _ensure_bridge_running()
                  │       │
                  │       └── bridge.send(text, timeout)
                  │               │
                  │               ├── wait for prompt_ready
                  │               ├── record start position
                  │               ├── set response_event watermark
                  │               ├── write text+\r to PTY
                  │               │       │
                  │               │       ▼
                  │               │   ┌────────────────────────────────┐
                  │               │   │  Reader Thread (改進後)        │
                  │               │   │                                │
                  │               │   │  ┌──────────────────────────┐  │
                  │               │   │  │ os.read(master_fd)         │  │
                  │               │   │  │   → respond_da()          │  │
                  │               │   │  │   → append to buffer      │  │
                  │               │   │  │   → update _last_data_time│  │
                  │               │   │  │   → auth check            │  │
                  │               │   │  └──────────┬───────────────┘  │
                  │               │   │             │                  │
                  │               │   │             ▼                  │
                  │               │   │  ┌──────────────────────────┐  │
                  │               │   │  │ Prompt Detection          │  │
                  │               │   │  │ (改進後)                 │  │
                  │               │   │  │                           │  │
                  │               │   │  │ 1. check_prompt_in_tail   │  │
                  │               │   │  │ 2. Prompt first seen?      │  │
                  │               │   │  │ 3. 2s init window         │  │
                  │               │   │  │ 4. Stable check (3s)       │  │
                  │               │   │  │ 5. Buffer growth check     │  │
                  │               │   │  │                           │  │
                  │               │   │  │ ┌─ stable 3s + no growth ─→│  │
                  │               │   │  │ │ 對話模式: set response   │  │
                  │               │   │  │ │ 任務模式: set completed  │  │
                  │               │   │  │ └────────────────────────  │  │
                  │               │   │  └──────────────────────────┘  │
                  │               │   └────────────────────────────────┘
                  │               │
                  │               ├── _wait_for_response()
                  │               │   → wait on response_event
                  │               │   → 1800s hard cap
                  │               │
                  │               └── VirtualScreen.render(raw)
                  │                   _clean_output(rendered)
                  │                   return text
                  │
                  ├── _truncate_reply (4096)
                  ├── _format_markdown_v2
                  └── msg.edit_text(result_text)
```

### 5.2 任務模式流程（新）

```
User: /task xxx
        │
        ▼
bot.py task_command()
        │
        ├── check: another task running? → reject
        ├── check: bridge ready?
        │
        └── bridge.send_task(prompt)
                │
                ├── set _task_mode = True
                ├── record task_send_start_pos
                ├── clear _task_completed
                ├── write prompt+\r to PTY
                │       │
                │       ▼
                │   ┌────────────────────────────────┐
                │   │  PTY 寫入完成                  │
                │   └────────┬───────────────────────┘
                │            │
                └──── return "✅ 任務已接收..."
                        │
                        ▼
bot.py: msg.edit_text("✅ 任務已接收，將在背景執行")
        │
        ▼
bot.py: asyncio.create_task(_monitor_task_completion())
        │
        ├── loop: wait on _task_completed (1s check)
        │   ├── _task_cancelled? → break
        │   ├── _running False? → break
        │   ├── elapsed: update "⏳ running..." every 30s
        │   └── task_completed? → proceed
        │
        ├── extract buffer content from task_send_start_pos
        │
        ├── _extract_task_summary(raw)
        │       │
        │       ├── VirtualScreen.render(raw)
        │       ├── _clean_output(rendered) — 簡化版
        │       ├── if > 3000: 壓縮 (頭20% + 尾60%)
        │       └── return text
        │
        ├── set _task_mode = False (自動回對話模式)
        │
        └── msg.edit_text("✅ 任務完成！\n\n[摘要]")
```

### 5.3 任務取消流程

```
User: /cancel
        │
        ▼
bot.py cancel_command()
        │
        ├── check: task running? → No → reply "No active task"
        ├── bridge.cancel_current_task()
        │       │
        │       ├── set _task_cancelled event
        │       ├── os.write(master_fd, b"\x03")  # Ctrl+C
        │       │
        │       ├── wait 3s:
        │       │   ├── check prompt ready? → done
        │       │   └── os.write(master_fd, b"\r")
        │       │
        │       ├── wait 3s more:
        │       │   ├── check prompt ready? → done
        │       │   └── SIGTERM Claude process
        │       │
        │       └── set _task_completed (unblock monitoring loop)
        │
        ├── bridge.set_task_mode(False)
        │
        └── msg.edit_text("🚫 Task cancelled.\nSwitched to chat mode.")
```

### 5.4 任務模式下普通消息

```
User types text while task running
        │
        ▼
bot.py handle_message()
        │
        ├── check: task running? → Yes
        │
        ├── prepare status message:
        │   "⏳ Task running (elapsed: 37s). Use /cancel to stop."
        │
        └── msg.reply_text(status_message)
```

---

## 6. 代碼改動對照表

### 6.1 pty_bridge.py

| 區域 | 行數 | 改動 | 歸屬 |
|------|------|------|------|
| `__init__` | ∼213-225 | 新增 `_task_mode`, `_task_completed`, `_task_cancelled`, `_task_result`, `_task_send_start_pos`, `_task_start_time`, `_task_prompt`, `_prompt_first_seen`, `_prompt_stable_since`, `_last_stable_buffer_len` | 共用 |
| `set_task_mode()` | ∼260 (新) | 新增切換方法 | B |
| `reset()` | ∼230 處 | 重置新增 fields | 共用 |
| `send()` | ∼300 區域 | **無需改動**（對話模式不改行為） | — |
| `send_task()` | ∼480 (新) | 新增任務提交方法（submit + 立即返回） | B |
| `_reader_loop` | ∼520 區域 | **核心改動**：prompt stability + mode dispatch | 共用 |
| `_check_prompt_detected_with_pos` | ∼790 | 新增 stability tracking | A |
| `_wait_for_task_completion` | ∼620 (新) | 背景等待 loop（async compatible） | B |
| `cancel_current_task` | ∼660 (新) | 分階段取消邏輯 | B |
| `_extract_task_summary` | ∼700 (新) | 摘要提取 | B |
| `_cleanup` | ∼830 | 重置 task fields | 共用 |

### 6.2 bot.py

| 區域 | 改動 | 歸屬 |
|------|------|------|
| `main()` | 註冊 `/task`, `/cancel`, `/chat` handlers | B |
| 新增 `task_command()` | 處理 `/task xxx` | B |
| 新增 `cancel_command()` | 處理 `/cancel` | B |
| 新增 `chat_command()` | 處理 `/chat` | B |
| 新增 `_monitor_task_completion()` | 背景監控 + 最終發送 | B |
| 修改 `handle_message()` | 任務模式 dispatch | B |
| 修改 `stop_command()` | task mode aware cancel | B |
| 修改 `new_command()` | task mode aware cancel | B |

### 6.3 config.py

| 改動 | 歸屬 |
|------|------|
| 新增 `TASK_SUMMARY_MAX_LENGTH = int(os.environ.get("TASK_SUMMARY_MAX_LENGTH", "3000"))` | B |

---

## 7. 實作優先級

### 建議順序

```
Phase 1: 輸出優化核心改動（先修 bug）── 3-5 days
Phase 2: 任務模式 MVP ── 2-3 days
Phase 3: 任務模式完整功能 ── 2-3 days
```

### Phase 1：輸出優化（優先修復現有 bug）

| 步驟 | 改動 | 風險 | 預計 |
|------|------|------|------|
| 1.1 | `__init__` 新增 `_prompt_first_seen`, `_prompt_stable_since`, `_last_stable_buffer_len` | 低 | 0.5h |
| 1.2 | `_reader_loop` 新增 prompt stability check | 中（需調整 timeout 參數） | 3h |
| 1.3 | 用 test_pty.py 測試邊界情況：多文件輸出、思考長停頓、A 模式回覆 | 低 | 2h |
| 1.4 | 正式部署測試 2 天 | — | 持續 |

**驗收標準**：
- [ ] 長代碼輸出不再截斷（以前會截的場景全部測試）
- [ ] Claude 思考中停頓不觸發完成
- [ ] 對話模式基本交互不受影響

### Phase 2：任務模式 MVP

| 步驟 | 改動 | 依賴 | 預計 |
|------|------|------|------|
| 2.1 | `__init__` 新增 task mode fields | 1.1 | 0.5h |
| 2.2 | `set_task_mode()`, `reset()` 擴展 | 2.1 | 0.5h |
| 2.3 | `send_task()` | 2.1 | 1h |
| 2.4 | `_reader_loop` mode dispatch | 1.2 + 2.1 | 1h |
| 2.5 | bot.py `/task` handler（簡化版：同步等待） | 2.3, 2.4 | 1h |

**MVP 驗收**：
- [ ] `/task xxx` → ✅ 任務已接收
- [ ] 任務完成後 → 回覆全部結果（尚未摘要）
- [ ] 完成後自動回到對話模式

### Phase 3：任務模式完整功能

| 步驟 | 改動 | 依賴 | 預計 |
|------|------|------|------|
| 3.1 | `_wait_for_task_completion` (async) | 2.1 | 2h |
| 3.2 | `_monitor_task_completion` (bot.py) | 3.1 | 2h |
| 3.3 | `handle_message` task mode dispatch | 2.1 | 0.5h |
| 3.4 | `cancel_current_task()` | 2.1 | 1h |
| 3.5 | bot.py `/cancel`, `/chat` handlers | 3.4 | 0.5h |
| 3.6 | `_extract_task_summary()` | 2.1 | 2h |
| 3.7 | config.py 新增常數 | 無 | 0.25h |
| 3.8 | `/stop`, `/new` task mode aware | 3.4 | 1h |

**完整驗收**：
- [ ] 背景非阻塞等待
- [ ] 摘要提取正確（頭尾保留策略）
- [ ] `/cancel` 分階段取消
- [ ] 任務中普通消息被拒絕
- [ ] `/chat` 切回對話模式
- [ ] `/stop` 在任務模式下正常運作

---

## 8. 避免衝突的策略

### 8.1 潛在衝突點

| 衝突點 | 輸出優化 (A) | 任務模式 (B) | 風險 |
|--------|-------------|-------------|------|
| `_response_event` 使用 | 設 event 觸發返回 | **不設** event（用 task_completed） | 高：如果 B 忘了抑制，任務模式會回退到對話模式行為 |
| `_send_prompt_watermark` | 水印邏輯不變 | 任務模式需要獨立的 `_task_send_start_pos` | 中：共用水印會導致任務結果讀取位置錯誤 |
| reader loop prompt detection | 改進 detection 邏輯 | mode dispatch 疊加在 detection 結果上 | 高：兩處修改都在同一個 if 區塊 |
| `_clean_output` | 過濾邏輯不變 | 任務模式需要簡化版清理 | 低：可通過參數控制 |
| `_wait_for_response` | 只等 `_response_event` | 任務模式不用（用 `_wait_for_task_completion`） | 低：各走各路 |

### 8.2 具體策略

**Strategy 1：Mode 獨立 Event 鏈（最高優先級）**

```
對話模式：prompt → _response_event.set() → send() 收到 → 返回
任務模式：prompt → _task_completed.set() → _monitor 收到 → 摘要 → 返回
```

兩種模式使用**完全獨立**的 Event 鏈。reader loop 中根據 `_task_mode` 決定設哪個 event：

```python
# reader loop 中的模式分發
if prompt_stable:
    if self._task_mode:
        self._task_completed.set()
        logger.info("[Task Mode] Task completed, signal sent")
    else:
        self._response_event.set()
```

**Strategy 2：獨立 Watermark**

```python
# 對話模式（不變）
self._send_prompt_watermark = start_pos  # 用於 _response_event

# 任務模式
self._task_send_start_pos = start_pos     # 用於 task completion 讀取
```

任務模式下 `_send_prompt_watermark` 不設定（對話模式不需要），反之亦然。各自用各自的 watermark。

**Strategy 3：Output Cleaning 分叉**

```python
def _extract_task_summary(self, raw_text: str) -> str:
    screen = VirtualScreen(rows=PTY_ROWS, cols=PTY_COLS)
    rendered = screen.render(raw_text)
    # 用簡化版清理（保留更多原始輸出）
    cleaned = self._clean_output(rendered)
    # 壓縮摘要
    return self._compress_summary(cleaned)
```

不對 `_clean_output` 增加 flag 參數，而是讓 task summary 走獨立路徑，自行調用清理後再壓縮。

**Strategy 4：邏輯隔離**

```
_reader_loop
    ├── Data collection（共用：讀取、DA、buffer、auth）
    ├── Detection（共用：prompt stability check）
    └── Dispatch（模式分叉）
            ├── _task_mode == True
            │    → _task_completed.set()
            └── _task_mode == False
                 → _response_event.set()
```

Detection 和 Dispatch 在 reader loop 中是兩個獨立的階段。輸出優化改 Detection 階段，任務模式加 Dispatch 階段。兩者在代碼中是**順序而不是嵌套**的關係。

### 8.3 衝突矩陣

| A 的改動 | 是否影響 B？ | 防護措施 |
|----------|-------------|----------|
| `__init__` 加 `_prompt_first_seen` | 否（僅 reader 內部使用） | 無需 |
| `__init__` 加 `_prompt_stable_since` | 否（僅 reader 內部使用） | 無需 |
| `__init__` 加 `_last_stable_buffer_len` | 否（僅 reader 內部使用） | 無需 |
| `_reader_loop` detection 強化 | 是（共享同一個 if 區塊） | Strategy 4：Detection → Dispatch 分階段 |

| B 的改動 | 是否影響 A？ | 防護措施 |
|----------|-------------|----------|
| `__init__` 加 task mode fields | 否（獨立命名空間） | 無需 |
| `set_task_mode()` | 否（新方法，不影響舊函數） | 無需 |
| `send_task()` | 否（新方法，不影響 `send()`） | 無需 |
| `_reader_loop` mode dispatch | 是（在 A 的 detection 之後） | Strategy 1：獨立 Event 鏈 |
| `_task_send_start_pos` | 否（獨立於 `_send_prompt_watermark`） | Strategy 2：獨立 Watermark |
| `_extract_task_summary` | 否（獨立路徑，調用 `_clean_output` 但不改它） | Strategy 3：輸出清理分叉 |

### 8.4 開發時的安全檢查

```python
# 在 _reader_loop prompt detection 區塊添加保護性斷言
if self._task_mode:
    # 任務模式不應該使用 response_event
    assert not self._response_event.is_set(), \
        "BUG: task mode should not use response_event"
    
    self._task_completed.set()
else:
    # 對話模式不應該使用 task_completed
    self._response_event.set()
```

在 `send_task()` 和 `_wait_for_task_completion()` 入口也添加 `assert not self._task_mode` / `assert self._task_mode` 來防止誤用。

---

## 附錄 A：變更摘要

| 維度 | 輸出優化 (A) | 任務模式 (B) |
|------|-------------|-------------|
| **文件數** | 1 (`pty_bridge.py`) | 3 (`config.py`, `pty_bridge.py`, `bot.py`) |
| **新增欄位** | 3 | 7 |
| **新增方法** | 0 | 6 |
| **修改方法** | 2 (`__init__`, `_reader_loop`) | 5 (`__init__`, `reset`, `_reader_loop`, +handlers) |
| **核心風險** | Detection 敏感度調教 | Reader loop mode 分叉 |
| **獨立性** | 可先做，不影響現有功能 | 依賴 prompt detection 的改進結果 |

## 附錄 B：配置常量表

```python
# config.py (new constants)
TASK_SUMMARY_MAX_LENGTH: int = int(
    os.environ.get("TASK_SUMMARY_MAX_LENGTH", "3000")
)

# pty_bridge.py (new constants for output optimization)
PROMPT_INIT_WINDOW = 2.0       # prompt 首次出現後 2s 初始化窗口
PROMPT_STABLE_WINDOW = 3.0     # 初始化後 3s 穩定期
TASK_DEFAULT_TIMEOUT = 3600.0  # 任務模式超時（60 分鐘）
```

## 附錄 C：與現有 task-mode-design.md 的關係

本文件與 `/docs/task-mode-design.md` 的關係：

| 方面 | task-mode-design.md | 本文件 (unified-design.md) |
|------|-------------------|--------------------------|
| 範圍 | 只考慮任務模式 | 輸出優化 + 任務模式 + 交互分析 |
| 側重 | 任務
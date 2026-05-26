# 純 Task 模式改造 — 實施任務單

## 任務概述

將 PTY bridge 從即時轉發的對話模式改造為純 task 模式：用戶發送消息後立即收到「任務已接收」確認，Claude 在背景執行，完成後只發送乾淨的摘要結果。

---

## 前置條件

- [ ] 已在楚熙（production）環境中確認動工前各項服務正常運作
- [ ] 已備份以下文件：
  - `/root/tg-claude-pty/bot.py`
  - `/root/tg-claude-pty/pty_bridge.py`
  - `/root/tg-claude-pty/config.py`
  - `/root/tg-claude-pty/output_parser.py`
  - `/root/tg-claude-pty/ansi_renderer.py`
- [ ] 已備份 `/root/tg-claude-pty/docs/unified-design.md`

---

## 需要修改的文件清單

| 文件 | 改動類型 | 改動範圍 | 相依關係 |
|------|----------|----------|----------|
| `config.py` | 修改 | 新增 task mode 配置常數（～5 行，檔尾） | 無（先改） |
| `pty_bridge.py` | 修改 | `__init__` 新增 task mode fields（～10 行） | 依賴 config.py |
| `pty_bridge.py` | 修改 | `reset()` 擴展重置 task fields（～5 行） | 依賴上一行 |
| `pty_bridge.py` | 新增 | `send_task()` 方法 | 依賴 __init__ fields |
| `pty_bridge.py` | 新增 | `cancel_current_task()` 方法 | 依賴 __init__ fields |
| `pty_bridge.py` | 新增 | `_extract_task_summary()` 方法 | 依賴 VirtualScreen + _clean_output |
| `pty_bridge.py` | 修改 | `_reader_loop` prompt detection 區塊：廢除即時轉發，改為累積到 task_buffer | 依賴 task mode fields |
| `pty_bridge.py` | 修改 | `_clean_output` 簡化為一次性清理函數 | 獨立 |
| `pty_bridge.py` | 刪除 | 11 個 helper 方法（逐個確認可刪性） | 依賴 _clean_output 簡化 |
| `bot.py` | 修改 | `handle_message` 改為 task dispatch logic | 依賴 pty_bridge 新方法 |
| `bot.py` | 新增 | `_monitor_task_completion()` async 背景監控 | 依賴 _reader_loop 改造 |
| `bot.py` | 刪除 | 舊的對話模式 handler（send + wait_for_response 路徑） | 依賴 handle_message 改造 |
| `ansi_renderer.py` | 保留/確認 | 確認 VirtualScreen 是否仍被 _extract_task_summary 調用 | 如仍使用則保留 |
| `output_parser.py` | 保留 | 保留 strip_ansi、is_prompt_detected、extract_content | 全被需要 |

---

## 實施步驟

### Step 1: config.py — 新增純 task 模式配置項

**做什麼**：在 config.py 檔尾，`Validation` 區塊之前，新增 3 個 task mode 常量。

**位置**：`/root/tg-claude-pty/config.py`，在 `# ── Validation ──` 之前。

**邏輯說明**：
- `TASK_DEFAULT_TIMEOUT = 3600.0` — 任務最長執行時間（60 分鐘）
- `TASK_SUMMARY_MAX_LENGTH = 3000` — 最終摘要最大字符數
- 均從環境變數讀取，有預設值

**驗證**：
- `python -c "from config import TASK_DEFAULT_TIMEOUT, TASK_SUMMARY_MAX_LENGTH; print('OK:', TASK_DEFAULT_TIMEOUT, TASK_SUMMARY_MAX_LENGTH)"` 應正常輸出

---

### Step 2: pty_bridge.py — 新增 task mode fields + reset() 擴展

**做什麼**：在 `PtyBridge.__init__` 新增 task mode 相關欄位。

**位置**：`pty_bridge.py` `__init__` 方法最後（在 `# Async bridge` 之前），新增以下 fields：

| 欄位 | 類型 | 說明 |
|------|------|------|
| `_task_mode` | `bool` | `True` = 任務模式，`False` = 對話模式 |
| `_task_prompt` | `str` | 當前任務的原始 prompt 文字 |
| `_task_start_time` | `float` | 任務啟動時間戳（monotonic） |
| `_task_completed` | `threading.Event` | 任務完成信號（reader thread set） |
| `_task_result` | `str` | 任務完成後的結果文字 |
| `_task_cancelled` | `threading.Event` | 用戶取消信號 |
| `_task_send_start_pos` | `int` | 任務 send_task() 開始時的 buffer 位置 |
| `_prompt_first_seen` | `float` | Prompt 字符首次出現的時間（prompt stability） |
| `_prompt_stable_since` | `float` | Prompt 穩定開始的時間 |
| `_last_stable_buffer_len` | `int` | 穩定性檢查時的 buffer 長度 |

**同時修改 `reset()`**：
- 重置新加的 task mode fields（_task_mode=False, events cleared）

**同時新增 `set_task_mode(bool)` 方法**：
- 設置 `_task_mode` 值
- 重置 event flags（_task_completed.clear(), _task_cancelled.clear()）

**驗證**：
- 建立 bridge 實例後，`bridge._task_mode` 應為 `False`
- `bridge.set_task_mode(True)` → `bridge._task_mode` 為 `True`
- `bridge.reset()` → `bridge._task_mode` 回到 `False`

---

### Step 3: pty_bridge.py — 新增 send_task() 方法

**做什麼**：新增 `send_task(text)` public 方法。

**位置**：`pty_bridge.py`，在 `send()` 方法之後新增。

**邏輯說明**：
1. 檢查 bridge 是否 ready
2. 設置 `_task_mode = True`
3. 記錄 `_task_send_start_pos = len(buffer)`
4. 清除 `_task_completed` 和 `_task_cancelled` events
5. 記錄 `_task_prompt` 和 `_task_start_time`
6. 寫入 text + `\r` 到 PTY master fd
7. 不等待 response_event，立即返回 `"✅ 任務已接收，將在背景執行"`
8. Echo skip log：記錄 sent_text（單純 log，不需複雜 echo skip）

**注意**：與對話模式 `send()` 不同，`send_task()` 不調用 `_wait_for_response()`、不跑 VirtualScreen render、不調用 `_clean_output`。

**驗證**：
- `result = await bridge.send_task("list files")` → 立即返回字串
- bridge._task_mode 為 True
- bridge._task_start_time > 0

---

### Step 4: pty_bridge.py — 新增 cancel_current_task() 方法

**做什麼**：新增 `cancel_current_task()` public 方法。

**位置**：`pty_bridge.py`，在 `send_task()` 之後新增。

**邏輯說明**：
1. 設置 `_task_cancelled` event
2. 立即發送 Ctrl+C（`b"\x03"`）到 PTY master fd
3. 背景 thread（或同步等待 + 輪詢）：
   - 等待 3 秒，檢查 prompt 是否已回到 ready 狀態
   - 如果沒有，發送 `\r` + 繼續等待 3 秒
   - 如果仍然沒有，發送 SIGTERM kill Claude process
4. 設置 `_task_completed` event（解鎖 monitoring loop）
5. 調用 `set_task_mode(False)` 回到對話模式

**驗證**：
- 呼叫 `bridge.cancel_current_task()` 後，`bridge._task_cancelled.is_set()` 為 True
- 完成後 `bridge._task_mode` 為 False

---

### Step 5: pty_bridge.py — 新增 _extract_task_summary() 方法

**做什麼**：新增 `_extract_task_summary(raw_text)` private 方法。

**位置**：`pty_bridge.py`，在 `cancel_current_task()` 之後新增。

**邏輯說明**：
1. 使用 VirtualScreen（同對話模式）對 raw 渲染
2. 調用 `_clean_output()`（簡化後的版本）清理 TUI artifacts
3. 如果清理後內容 `> TASK_SUMMARY_MAX_LENGTH`：
   - 保留開頭部分（～20%）：任務上下文
   - 保留結尾部分（～60%）：最終結果
   - 中間用省略提示替代 `\n...（省略 X 字元）...\n`
4. 返回最終文字

**驗證**：
- 短內容保持原樣：`summary = bridge._extract_task_summary("Hello world")` → `"Hello world"`
- 長內容被截斷：3000+ 字符的輸入應被壓縮

---

### Step 6: pty_bridge.py — 修改 _reader_loop prompt detection 區塊

**這是整個改造最核心的步驟。**

**做什麼**：修改 `_reader_loop` 中現有的 prompt detection + response completion 邏輯。廢除「即時轉發 + response_event 設法」，改為：
- 所有收到的資料繼續累積到 buffer（同現有邏輯）
- Prompt detection 繼續運作（用於判斷 Claude 完成）
- 但不再設置 `_response_event`（對話模式不再使用）
- 改為累積到一個 independent task buffer 變數
- 檢測到 prompt 完成後 → 設置 `_task_completed` event
- 刪除「對話模式：設 response_event」的邏輯

**位置**：`pty_bridge.py` `_reader_loop` 方法，現有 `# ── Response completion detection ──` 區塊。

**詳細邏輯**：

```
1. 保留現有的 prompt detection 邏輯（_check_prompt_detected_with_pos）
2. 保留 prompt stability check（_prompt_first_seen, _prompt_stable_since）
3. 保留 buffer growth detection
4. 保留所有 data collection 邏輯（DA respond, auth check）
5. 變更觸發：
   舊：if prompt_detected + stable + no growth → _response_event.set()
   新：if prompt_detected + stable + no growth → _task_completed.set()
6. 保留「prompt-ready detection」（_prompt_ready_event），用於輸入防堆疊
7. 保留 output burst tracking（僅 log，不改邏輯）
```

**注意**：
- 不需要 task mode flag 判斷（純 task 模式沒有對話模式分支）
- `_response_event` 可以完全移除或保留但不再被 set
- `_send_prompt_watermark` 可以簡化為 `_task_send_start_pos` 共用

**驗證**：
- 啟動 bridge 後，reader thread 正常運行
- Ctrl+C 測試：發送任意文字後，等 Claude 完成 → `_task_completed` event 被觸發
- 長時間任務（如 `for i in range(100): print(i)`）不會中途截斷

---

### Step 7: pty_bridge.py — 修改 _clean_output：簡化為一次性清理函數

**做什麼**：將 `_clean_output` 從複雜的多階段、多 helper、code-block 保護的版本簡化為一次性清理函數。

**現狀**：
- `_clean_output` 本身很大（～200 行）
- 依賴 11 個 static/class method helper
- 有 code-block extraction / reinsertion 邏輯
- 有 Phase 0-5 的流水線

**改造後**：
- 移除 code-block extraction（task 結果中不需要保護 markdown）
- 移除 Phase 1 protocol/summary suppression（task 結果不會有 protocol section）
- 保留基本的行級過濾：status lines、TUI artifacts、ANSII remnants
- 保留 horizontal rule detection + file header detection（用於清理輸出）
- 保留尾部 prompt 清理（移除結尾的 `❯` / `▶`）
- 保留空白行壓縮
- 改為一個簡單的函數，不需要 class method 調用

**位置**：`pty_bridge.py` `_clean_output` 方法。

**邏輯說明**：
1. 分割 lines
2. 逐行過濾，保留以下過濾器：
   - Spinner/braille lines
   - Horizontal rules
   - File header lines
   - Status lines（✻ Brewed for...）
   - Prompt lines
   - TUI hint lines
   - ANSI remnants
3. Space compression（2+ spaces → 1）
4. Trailing prompt cleanup
5. Trim + collapse blank lines
6. 返回 cleaned text
7. 安全 fallback（如果 filtered 為空）

**驗證**：
- 現有測試 case 仍然通過（可用 test_pty.py 中的測試確認）
- 輸出不再包含 status/status lines
- Claude 的實際代碼/分析輸出不被過濾

---

### Step 8: pty_bridge.py — 清理可刪除的舊函數

**做什麼**：刪除對話模式不再需要的舊函數和 helper。

**清單（逐一確認）：**

| 函數/方法 | 是否刪除 | 理由 |
|-----------|----------|------|
| `send()` | **保留但簡化** | 仍可用於特殊場合（debug/testing），但移除 `_wait_for_response` 調用 |
| `_wait_for_response()` | **刪除** | 不再需要同步等待 response |
| `_wait_for_prompt_ready()` | **保留** | 仍用於 _ensure_bridge_running 檢查 |
| `_is_status_line_only()` | **保留或合併** | 可用於 _extract_task_summary 中 |
| `_BOX_DRAWING_CHARS` | **保留** | 用於 _clean_output |
| `_HR_CHARS` | **保留** | 用於 _clean_output |
| `_DECORATIVE_CHARS` | **保留** | 用於 _clean_output |
| `_check_prompt_in_text()` | **保留** | 用於 _check_prompt_detected_with_pos |
| `_check_prompt_detected_with_pos()` | **保留** | 核心 detection 邏輯 |
| `_check_prompt_detected()` | **可刪除** | send() 的 wrapper，不再需要 |
| `_echo_detected()` | **刪除** | task 模式不需要 echo skip |
| `_get_decoded_text()` | **保留** | 可用於 debug/fallback |
| `_is_horizontal_rule_line()` | **保留** | 用於 _clean_output |
| `_is_file_header_line()` | **保留** | 用於 _clean_output |
| `_is_pure_decorative_line()` | **保留** | 用於 _clean_output |
| `_is_decorator_bullet_line()` | **保留** | 用於 _clean_output |
| `_COMMON_EXTENSIONS` | **保留** | 用於 _is_file_header_line |
| `_DECORATOR_RANGES` | **保留** | 用於 _is_decorator_bullet_line |
| `_response_event` (field) | **刪除** | 對話模式的同步信號 |
| `_expecting_response` (field) | **刪除或改為 `_task_running`** | 改用 `_task_mode == True` 判斷 |
| `_send_start_pos` (field) | **保留** | 改為 task buffer 記錄 |
| `_send_prompt_watermark` (field) | **可刪除** | 任務模式用 `_task_send_start_pos` |
| `_prompt_ready_event` (field) | **保留** | 輸入防堆疊，_ensure_bridge_running 需要 |
| `_sent_text` (field) | **刪除** | 不再需要 echo detection |

**驗證**：
- 刪除後 `import pty_bridge` 不報錯
- `python -c "from pty_bridge import PtyBridge; b = PtyBridge(); print('OK')"` 正常運行

---

### Step 9: bot.py — 新增 task dispatch logic

**做什麼**：改寫 `handle_message`，從「對話模式：send + wait + reply」改為「收到消息 → 立即確認 → 背景監控 → 完成後發送摘要」。

**位置**：`bot.py` `handle_message` 方法。

**詳細邏輯**：

```
handle_message():
    1. 授權檢查（不變）
    2. 提取 prompt / image（不變）
    3. 檢查 bridge 是否 running（_ensure_bridge_running）- 不變
    4. 發送「⏳ 任務已接收」即時回覆
    5. 調用 bridge.send_task(prompt)
    6. 編輯消息為「✅ 任務已接收，正在背景執行」
    7. 啟動背景監控：asyncio.create_task(_monitor_task_completion(msg, prompt))

_monitor_task_completion(msg, prompt):
    1. Loop: 每秒檢查 bridge._task_completed event
       - 同時檢查 _task_cancelled（被 /cancel 觸發）
       - 同時檢查 _running（bridge 是否存活）
    2. 每 30 秒更新消息：⏳ 任務執行中...（已過 N 秒）
    3. 任務完成後：
       a. 從 buffer 提取 task 期間內容（_task_send_start_pos → 當前）
       b. 調用 bridge._extract_task_summary(raw)
       c. 格式化和截斷（_format_markdown_v2 + _truncate_reply）
       d. 編輯消息發送結果
       e. 調用 bridge.set_task_mode(False)
    4. 超時處理：超過 TASK_DEFAULT_TIMEOUT 後發送超時提示
    5. 錯誤處理：bridge 掛掉時發送錯誤消息
```

**驗證**：
- 發送訊息 `list files in /root` → 立即收到「✅ 任務已接收」
- 等 Claude 完成後 → 收到摘要結果
- 發送後立即點 `/cancel` → 任務中斷提示

---

### Step 10: bot.py — 刪除 / 簡化舊的對話模式 handler 和相關代碼

**做什麼**：刪除不再需要的對話模式相關代碼。

**刪除清單：**

| 項目 | 文件位置 | 說明 |
|------|----------|------|
| `_request_lock` | bot.py global | 不再需要序列化鎖（task 模式非阻塞） |
| `_request_processor_task` | bot.py global | 不再需要 |
| `_processor_paused` | bot.py global | 不再需要 |
| `_pause_processor()` | bot.py function | 不再需要（無同步等待） |
| `_resume_processor()` | bot.py function | 不再需要 |
| `_process_request_in_lock()` | bot.py function | 整個函數刪除（核心對話模式邏輯） |
| `SEND_TIMEOUT` | bot.py constant | 不再需要（改用 TASK_DEFAULT_TIMEOUT） |
| `start()` handler 中對話模式相關描述 | bot.py | 更新 /start 回覆文字 |
| `new_command()` | bot.py | **保留但簡化** — 移除鎖相關邏輯 |
| `stop_command()` | bot.py | **保留但簡化** — 移除鎖相關邏輯 |

**保留**：
- `_is_authorized()` — 授權邏輯不變
- `_escape_markdown_v2()` — 格式化工廠
- `_format_markdown_v2()` — 格式化工廠
- `_truncate_reply()` — 截斷邏輯
- `_ensure_bridge_running()` — 健康檢查（但移除對 bridge.send 的依賴）
- `help_command()` — 更新 help 文字
- `error_handler()` — 錯誤處理
- `post_init()` / `post_shutdown()` — lifecycle

**驗證**：
- Bot 正常啟動，不報任何 import/attribute error
- 發送消息後正常走 task dispatch 路徑
- `/new`、`/stop`、`/start`、`/help` 命令正常運行

---

### Step 11: 各文件清理與確認

**做什麼**：清理殘留的 import、常數、註解。

| 文件 | 清理內容 |
|------|----------|
| `bot.py` | 移除不再 import 的 `output_parser`、`strip_ansi`（如果有） |
| `pty_bridge.py` | 移除 `_check_prompt_detected` 方法 |
| `pty_bridge.py` | 移除 `_echo_detected` 方法 |
| `pty_bridge.py` | 更新 docstring 說明現在是純 task 模式 |
| `pty_bridge.py` | 刪除 `# ── Characters of interest for filtering ──` 中的多餘註解 |
| `ansi_renderer.py` | 確認 VirtualScreen 類仍被 _extract_task_summary 調用，保留 |

**驗證**：
- `python -m py_compile bot.py` — 無語法錯誤
- `python -m py_compile pty_bridge.py` — 無語法錯誤

---

### Step 12: 端到端測試驗收

**做什麼**：實際啟動 bot 並進行功能驗收。

**測試清單：**

```
Test 1: 基本任務提交與完成
  → 發送「列出 /root 目錄的檔案」
  → 預期：①立即回復「✅ 任務已接收」②完成後收到目錄列表摘要

Test 2: 長時間任務
  → 發送「用 Python 計算 fibonacci(10000)」
  → 預期：30 秒左右收到進度更新 → 完成後收到結果摘要

Test 3: 任務取消
  → 發送「sleep 60; echo done」
  → 發送 /cancel
  → 預期：任務被中斷，收到「🚫 任務已取消」

Test 4: 多個消息（非阻塞）
  → 發送「sleep 30; echo finished」
  → 在 30 秒內再發送一條消息
  → 預期：第二條消息被拒絕，提示「任務正在執行中」

Test 5: /stop 命令
  → 發送「sleep 60」→ /stop
  → 預期：任務被取消，Claude 重啟

Test 6: 長輸出截斷
  → 發送「for i in range(500): print(f'Line {i}')」
  → 預期：收到摘要結果，長度不超過 4000 字符

Test 7: /start /help 正常
  → 驗證命令正常運行
```

---

## 驗收標準

| # | 驗收項目 | 預期結果 |
|---|---------|----------|
| 1 | 發送一般訊息後 | 立即收到「✅ 任務已接收，正在背景執行」，不阻塞 |
| 2 | 任務完成後 | 收到乾淨的摘要（無 TUI artifacts、無 status lines） |
| 3 | `/cancel` 功能 | 任務被中斷，收到「🚫 任務已取消」 |
| 4 | 長時間任務 | 每 30 秒有進度更新（⏳ 任務執行中... 已過 N 秒） |
| 5 | 任務中超時 | 超過 TASK_DEFAULT_TIMEOUT（3600s）後收到超時提示 |
| 6 | 任務中發送其他消息 | 收到提示「任務正在執行中，請使用 /cancel 取消」 |
| 7 | `/stop` 命令 | 任務取消 + Claude 重啟完成 |
| 8 | 舊的對話模式代碼 | 已完全移除（無 `_request_lock`、`_wait_for_response`、`_response_event` 等） |
| 9 | 舊的 helper 函數 | 11 個可刪除的 helper 已清理 |
| 10 | Bridge 重啟後 | 自動回到對話模式（_task_mode = False） |

---

## 各步驟依賴關係圖

```
Step 1 (config.py)
  │
  ▼
Step 2 (pty_bridge __init__ + reset + set_task_mode)
  │
  ├────┬────────────┬────────────┐
  │    │            │            │
  ▼    ▼            ▼            ▼
Step 3  Step 4     Step 5      Step 6
send_task cancel  _extract     _reader_loop
                  _task_summary 改造
  │    │            │            │
  └────┴────────────┴────────────┘
                  │
                  ▼
             Step 7 (_clean_output 簡化)
                  │
                  ▼
             Step 8 (清理舊 helper)
                  │
                  ▼
             Step 9 (bot.py task dispatch)
                  │
                  ▼
             Step 10 (刪除舊對話 handler)
                  │
                  ▼
             Step 11 (文件清理)
                  │
                  ▼
             Step 12 (測試驗收)
```

---

## 回退方案

### 回退方式

```bash
# 從備份目錄恢復
cd /root/tg-claude-pty

# 恢復 bot.py
cp backup/bot.py.$(日期) bot.py

# 恢復 pty_bridge.py
cp backup/pty_bridge.py.$(日期) pty_bridge.py

# 恢復 config.py
cp backup/config.py.$(日期) config.py

# 重啟服務
systemctl --user restart tg-claude-pty
```

### 備份指令

改造開始前執行：

```bash
cd /root/tg-claude-pty
mkdir -p backup
cp bot.py backup/bot.py.$(date +%Y%m%d_%H%M%S)
cp pty_bridge.py backup/pty_bridge.py.$(date +%Y%m%d_%H%M%S)
cp config.py backup/config.py.$(date +%Y%m%d_%H%M%S)
```

### 回退後驗證

- `systemctl --user status tg-claude-pty` — 服務正常運行
- 發送消息 → 正常收到回覆（回到舊的對話模式）
- `/new`、`/stop` 命令正常運行

### 回退時機判斷

下列任一情況成立時應考慮回退：

1. Bot 無法啟動（`bot.py` import error 或 runtime error）
2. 任務提交後無任何回應（包括即時確認消息）
3. 任務完成後結果為空或嚴重錯誤
4. `_reader_loop` prompt detection 完全失效（Claude 完成後不觸發 task completion）
5. 超過 1 小時仍無法查明原因時

---

## 附錄：受影響的現有功能對照

| 現有功能 | 改造後狀態 | 說明 |
|----------|-----------|------|
| 即時轉發 Claude 輸出 | ❌ 移除 | 不再顯示中間過程 |
| MarkdownV2 格式化 | ✅ 保留 | 最終結果仍需格式化 |
| 圖片分析 | ✅ 保留 | Image → base64 邏輯不變，透過 send_task 執行 |
| Session 持久化 | ✅ 保留 | `--session-id` 參數不變 |
| /new 重置 | ✅ 保留 | 簡化後的 new_command（移除鎖） |
| /stop 重啟 | ✅ 保留 | 簡化後的 stop_command（移除鎖） |
| 授權檢查 | ✅ 保留 | 完全不受影響 |
| 長消息截斷 | ✅ 保留 | 最終結果仍需截斷（4000 chars） |
| 錯誤處理 | ✅ 保留 | error_handler 不變 |
| 自動重連 | ✅ 保留 | _ensure_bridge_running 邏輯不變 |

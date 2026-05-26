# 純 Task 模式改造完成摘要

## 改動總結

將 PTY bridge 從即時轉發的對話模式改造為純 task 模式：用戶發送消息後立即收到「任務已接收」確認，Claude 在背景執行，完成後自動發送乾淨的摘要結果。

### 修改的文件

| 文件 | 改動 | 行數變化 |
|------|------|----------|
| `config.py` | 新增 3 個常數：`TASK_DEFAULT_TIMEOUT`、`TASK_SUMMARY_MAX_LENGTH` | 54 → 68 |
| `pty_bridge.py` | 新增 task mode fields、新方法 + 改造 reader loop | 1475 → 1567 |
| `bot.py` | 改為 pure task dispatch + background monitoring | 560 → 575 |

### 保留的檔案

- `ansi_renderer.py` — `VirtualScreen` 仍被 `_extract_task_summary` 調用
- `output_parser.py` — `strip_ansi`、`is_prompt_detected`、`respond_da`、`extract_content` 均仍被使用

---

## 新增功能

### PtyBridge (pty_bridge.py)

| 項目 | 說明 |
|------|------|
| `__init__` 新增 7 個 task mode fields | `_task_mode`, `_task_prompt`, `_task_start_time`, `_task_completed`, `_task_result`, `_task_cancelled`, `_task_send_start_pos` |
| `__init__` 新增 3 個 prompt stability fields | `_prompt_first_seen`, `_prompt_stable_since`, `_last_stable_buffer_len` |
| `set_task_mode(enabled)` | 切換 task/conversation 模式 |
| `task_mode` / `task_active` properties | 查詢當前模式狀態 |
| `send_task(text)` → `str` | 非阻塞提交任務，立即返回確認 |
| `cancel_current_task()` | 分階段取消（Ctrl+C → Enter → SIGTERM） |
| `_extract_task_summary()` → `str` | VirtualScreen 渲染 + `_clean_output` 清理 + 壓縮摘要 |
| `_wait_for_task_completion(timeout)` | 同步等待 `_task_completed` event |

### bot.py

| 項目 | 說明 |
|------|------|
| `handle_message` → task dispatch | 不再同步等回應，立即 `send_task()` + 背景監控 |
| `_monitor_task_completion()` | background coroutine: 每 1s 檢查完成/取消/超時，每 30s 進度更新 |
| `cancel_command()` | `/cancel` handler 取消當前任務 |
| `post_init` 日誌更新 | "pure task mode" |

---

## 改動細節

### 1. _reader_loop 核心改動

- **任務完成檢測**：廢除 `_expecting_response` + `_response_event`，改用 `_task_completed` event
- **Prompt Stability Check**：新增 3 段式穩定性檢測
  1. Prompt 字符首次出現 → 記錄 `_prompt_first_seen`
  2. 2 秒初始化窗口（忽略短暫 prompt 出現）
  3. 3 秒穩定窗口 + buffer 增長檢測（新數據則 reset 計時器）
  4. 穩定期滿 → `_task_completed.set()` + `_task_result = _extract_task_summary()`
- **Auth 檢查**：改用 `_task_send_start_pos > 0 and not _task_completed.is_set()` 替代舊的 `_expecting_response`
- **Prompt-ready detection**：在 `_task_completed` 後仍正常檢測，確保下次 `send_task()` 可正常提交

### 2. _clean_output 簡化

- 移除：code-block extraction/reinsertion（～80 行）
- 移除：protocol/summary section suppression（～30 行）
- 保留：spinner/braille、horizontal rules、file header lines、TUI hints、status lines、prompt lines、box-drawing、decorative 等所有核心過濾器
- 保留：safety fallback（minimal pass）

### 3. 緩存

- `_wait_for_response()` → 已刪除（不再使用）
- `_response_event` field → 保留但不被 reader loop 使用（僅 `send()` 內部使用）
- `_expecting_response` field → 保留但不被 reader loop 使用（僅 `send()` 內部使用）

### 4. bot.py 重構

- 移除：`_request_lock`（不再需要同步序列化）
- 移除：`_request_processor_task`
- 移除：`_processor_paused`、`_pause_processor()`、`_resume_processor()`
- 移除：`_process_request_in_lock()`（核心對話模式函數）
- 移除：`SEND_TIMEOUT` 常數（改用 config 的 `TASK_DEFAULT_TIMEOUT`）
- 新增：`cancel_command()` handler
- 新增：`_monitor_task_completion()` background coroutine
- Handler 邏輯：所有命令移除鎖相關邏輯

---

## 配置常數

| 常數 | 預設值 | 環境變數 | 說明 |
|------|--------|----------|------|
| `TASK_DEFAULT_TIMEOUT` | 3600.0 | `TASK_DEFAULT_TIMEOUT` | 任務最長執行時間（秒） |
| `TASK_SUMMARY_MAX_LENGTH` | 3000 | `TASK_SUMMARY_MAX_LENGTH` | 最終摘要最大字符數 |
| `PROGRESS_INTERVAL` | 30 | (bot.py 內部常數) | 進度更新間隔（秒） |

---

## 最終狀態

- ✅ 服務正常執行中
- ✅ `systemctl start tg-claude-pty` 無錯誤
- ✅ 所有 `.py` 文件通過 `py_compile` 檢查
- ✅ 備份保存於 `/root/tg-claude-pty/backup-20260526-152709/`

### 使用流程

1. 用戶發送任意訊息 → ✅ 任務已接收
2. Claude 在背景執行 → 每 30 秒進度更新
3. 任務完成 → 自動發送摘要結果
4. 用戶可隨時 `/cancel` 取消當前任務
5. `/stop` / `/new` → 取消任務 + 重啟 Claude

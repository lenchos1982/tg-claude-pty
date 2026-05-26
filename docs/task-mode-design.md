│  msg.edit(text) │     │                     │     │                   │
│                │     │              │     │                     │     │                   │
└────────────────┘     └──────────────┘     └─────────────────────┘     └───────────────────┘
```

### 3.2 對話模式流程（現有，無變更）

```
User text ──> bot.py handle_message ──> _process_request_in_lock()
    └── bridge.send(text, timeout)
            └── write to PTY
            └── wait_for_response()
                    └── reader thread detects prompt ──> signal response_event
            └── VirtualScreen render + _clean_output
            └── return formatted text
    └── msg.edit_text(formatted_text)
```

### 3.3 `/chat` 切回對話模式

```
/chat ──> chat_command()
    ├── check bridge exists + no task running
    ├── bridge.set_task_mode(False)
    │       └── _task_mode = False
    │       └── reset _task_cancelled, _task_completed, _task_result
    └── reply "Switched to Chat Mode"
```

### 3.4 `/cancel` 取消任務

```
/cancel ──> cancel_command()
    ├── check bridge exists + task running
    ├── bridge.cancel_current_task()
    │       └── set _task_cancelled flag
    │       └── os.write(master_fd, b"\x03")  // Ctrl+C
    │       └── bg thread: wait 3s → Enter → SIGTERM if needed
    ├── bridge.set_task_mode(False)
    └── reply "Task cancelled"
```

---

## 4. 邊界情況處理

### 4.1 用戶在任務模式下發普通消息

**場景**：用戶發 `/task xxx` 後，在任務完成前又發了一條消息。

**處理方式**：
1. `handle_message()` 檢查 `bridge.task_status["running"]`
2. 立即回覆提示消息（不 forwarding 給 Claude）：
   - 「⏳ A task is currently running (elapsed: 37s). Use /cancel to stop it.」
3. 消息不排隊，不記錄
4. 如果用戶堅持發送：請他們先 `/cancel`

**理由**：
- 任務模式的設計精神是：Claude 專注執行任務，不受干擾
- 如果將消息堆疊到任務完成後處理，可能導致上下文混亂
- 簡單直接的回覆讓用戶清楚當前狀態

### 4.2 任務模式下 Claude Code 卡住了

**場景**：Claude 正在執行一個長時間任務（如讀取大量文件、編譯大型項目），prompt 一直不出現。

**處理方式**：
1. `_wait_for_task_completion()` 有硬性 timeout（預設 3600s/60min）
2. 超時後設置 `_task_result` + 觸發 `_task_completed`
3. 用戶收到：「⏰ Task timed out after 3600 seconds.」
4. 自動切回對話模式 + 發送 Ctrl+C 嘗試中斷卡住的 Claude
5. 如果 timeout 後 Claude 仍未恢復 prompt，用戶可以 `/stop` 重啟

**監控**：
- 背景 loop 每 30 秒記錄 buffer 增長情況
- 如果 buffer 超過 60 秒無變化，可以在 debug log 中標記「可能卡住」
- 不自動 kill，因為長時間的 compile/build/thinking 也可能輸出停頓

### 4.3 任務模式下用戶想取消任務

**場景**：用戶提交任務後改變主意，想取消。

**處理方式**：見 3.4 節 `/cancel` 流程。

**分階段取消**：
| 階段 | 操作 | 效果 |
|------|------|------|
| 立即（0s） | `_task_cancelled.set()` | 背景 loop 停止等待 |
| 1s | `os.write(b"\x03")` (Ctrl+C) | Claude 收到 SIGINT |
| 4s | `os.write(b"\r")` + 檢查 prompt | 確認是否回到 prompt |
| 6s | `os.kill(pid, SIGTERM)` | 強制終止 Claude 進程 |

**邊界**：
- 取消時 Claude 正在寫文件：寫入可能不完整（如同 Ctrl+C 手動中斷）
- 取消完成後自動切回對話模式，用戶可以 `/new` 重置 session 確保乾淨狀態

### 4.4 Telegram 消息長度限制（4096 字符）

**場景**：任務結果可能很大（代碼生成、分析報告等）。

**處理方式**：

| 層級 | 觸發條件 | 策略 |
|------|----------|------|
| `_compress_to_summary()` | 原始長度 > 3000 | 先壓縮到 3000 字符 |
| `_handle_task_completion()` | 最終消息 > 4000 | 截斷到 3970 + 提示文字 |
| `_handle_task_completion()` | Markdown 解析失敗 | 降級到純文字 |
| `_handle_task_completion()` | 純文字也 > 4000 | 截斷到 4000 |

**設計決策**：
- 截斷時優先保留尾部（結論/結果通常在最後）
- 不在任務模式下分段發送 4096+ 的消息（避免打擾用戶）
- 不發送原始 buffer 的鏈接（無需引入文件儲存）

### 4.5 多個任務排隊

**場景**：用戶發送 `/task A`，在 A 完成前又發送 `/task B`。

**處理方式**：

| 配置值 | 行為 |
|--------|------|
| `TASK_QUEUE_MAX = 0` | 直接拒絕第二次 /task：「A task is already running. Use /cancel first.」 |
| `TASK_QUEUE_MAX = 1` | 允許排 1 個隊列任務。B 提交後：回覆「✅ Task queued (#1). Will start after current task.」 |
| `TASK_QUEUE_MAX = 3` | 同 1，但可排 3 個（+1 正在執行 = 最多 4 個任務） |

**佇列實作建議（簡易版）**：
```python
# 在 PtyBridge 添加
self._task_queue: asyncio.Queue = asyncio.Queue(maxsize=TASK_QUEUE_MAX)
self._task_worker: Optional[asyncio.Task] = None

async def _task_queue_worker(self):
    """背景任務佇列執行器。"""
    while self._running:
        task_prompt, callback_ref = await self._task_queue.get()
        await self._execute_single_task(task_prompt, callback_ref)
```

**設計建議**：初期使用 `TASK_QUEUE_MAX = 0`（不排隊），等實際需求再擴展。排隊機制增加了複雜度，如：
- 排隊中的任務如何取消
- 排隊用戶如何感知狀態變化
- 排隊任務的 timeout 從提交時間還是開始時間算

### 4.6 任務模式下 `/stop` 和 `/new`

**場景**：用戶在任務執行中發送 `/stop` 或 `/new`。

**處理方式**：
```python
async def stop_command(update, context):
    # ... 現有邏輯 ...
    await _pause_processor()
    try:
        if bridge is not None and bridge.task_status["running"]:
            bridge.cancel_current_task()
        # ... 現有 restart 邏輯 ...
        bridge.set_task_mode(False)
    finally:
        _resume_processor()
```

### 4.7 Bridge 重啟後狀態恢復

**場景**：任務執行中 bridge 意外重啟（Claude crash / 網路斷開）。

**處理方式**：
- `_wait_for_task_completion()` 每輪檢查 `self._running`
- 若 `_running=False`，立即設置 `_task_result = "❌ Bridge stopped unexpectedly."`
- 設置 `_task_completed` → bot handler 收到並通知用戶
- 自動切回對話模式
- `_ensure_bridge_running()` 會在下次請求時自動重啟

---

## 5. 實作優先級

### Phase 1 — 核心基礎（建議 0.5 天）

**目標**：任務模式能跑的 MVP

| 順序 | 步驟 | 文件 | 依賴 |
|------|------|------|------|
| 1 | 新增 config.py 配置項 | config.py | 無 |
| 2 | PtyBridge.__init__ 新增 task mode fields | pty_bridge.py | 1 |
| 3 | PtyBridge.set_task_mode() 和 reset() 擴展 | pty_bridge.py | 1 |
| 4 | PtyBridge.send_task() 方法（同步版，返回立即） | pty_bridge.py | 2 |
| 5 | bot.py 新增 /task handler（簡化版：submit + 等待） | bot.py | 3-4 |
| 6 | bot.py main() 註冊 handler | bot.py | 5 |

**MVP 行為驗證**：
- `/task xxx` → ✅ 任務已接收（同步等待）
- 任務完成後 → 回覆最終結果
- 任務完成後自動回到對話模式

### Phase 2 — 背景非阻塞（建議 0.5 天）

**目標**：任務提交後可接收其他消息

| 順序 | 步驟 | 文件 | 依賴 |
|------|------|------|------|
| 7 | PtyBridge._wait_for_task_completion()  async loop | pty_bridge.py | 4 |
| 8 | bot.py _handle_task_completion() 背景 callback | bot.py | 7 |
| 9 | handle_message() task mode dispatch | bot.py | 7 |

**驗證**：
- `/task xxx` → 立即回覆 ✅
- 用戶可發其他消息（收到提示但不上送）
- 任務完成後 → 自動通知

### Phase 3 — 摘要提取（建議 0.5 天）

**目標**：有質量的任務結果

| 順序 | 步驟 | 文件 | 依賴 |
|------|------|------|------|
| 10 | PtyBridge._extract_summary_sync() | pty_bridge.py | 7 |
| 11 | PtyBridge._compress_to_summary() | pty_bridge.py | 10 |
| 12 | 整合到 _wait_for_task_completion() 完成時 | pty_bridge.py | 10-11 |

**驗證**：
- Claude 返回多行代碼 → 摘要只保留關鍵結果
- 過長輸出 → 截斷在 3000 字符內
- 短輸出 → 保持原樣

### Phase 4 — 控制與邊界（建議 0.5 天）

**目標**：完整控制能力

| 順序 | 步驟 | 文件 | 依賴 |
|------|------|------|------|
| 13 | PtyBridge.cancel_current_task() | pty_bridge.py | 7 |
| 14 | bot.py /cancel handler | bot.py | 13 |
| 15 | bot.py /chat handler | bot.py | 2 |
| 16 | `/stop` 和 `/new` 擴展（task mode aware） | bot.py | 13 |
| 17 | Telegram 4096 長度截斷 | bot.py | 10 |

**驗證**：
- `/cancel` → 任務中斷，回到對話模式
- `/chat` → 切換告知
- 任務中 `/stop` → cancel + restart
- 16000 字符結果 → 截斷到 ~4000

### Phase 5 — 日誌與優化（建議 0.5 天）

**目標**：可觀測性與穩定性

| 順序 | 步驟 | 文件 | 依賴 |
|------|------|------|------|
| 18 | task running 日誌（每 30s） | pty_bridge.py | 7 |
| 19 | task elapsed 格式化輸出 | pty_bridge.py | 10 |
| 20 | error handling + 降級策略 | bot.py + pty_bridge.py | 全 |
| 21 | 摘要模式下的 _clean_output 優化 | pty_bridge.py | 10 |
| 22 | 可選：任務排隊 | pty_bridge.py + bot.py | 7 |

### 依賴圖

```
Phase 1 (MVP)
  config (1) ──> PtyBridge fields (2) ──> send_task (4) ──> /task handler (5-6)
                                        └──> set_task_mode (3)
Phase 2 (Async)
  _wait_for_task_completion (7) ──> _handle_task_completion (8)
                                    └──> handle_message dispatch (9)
Phase 3 (Summary)
  _extract_summary_sync (10) ──> _compress_to_summary (11)
                                 └──> integrate (12)
Phase 4 (Control)
  cancel_current_task (13) ──> /cancel handler (14)
                               └──> /stop + /new (16)
                              └──> /chat handler (15)
                              └──> 4096 limit (17)
Phase 5 (Polish)
  logging (18-19) ──> error handling (20) ──> clean_output optimize (21)
                                                └──> queue (22, optional)
```

---

## 附錄：關鍵設計決策摘要

### Decision 1: 非同步 vs 同步等待

**選擇**：非同步（`asyncio.create_task` 背景等待）

**理由**：
- 同步等待會阻塞 Telegram polling，用戶無法在任務執行中使用 `/cancel`
- 非同步讓用戶可以感知任務狀態並控制

### Decision 2: Summary extraction 放在 pty_bridge 還是 bot

**選擇**：`pty_bridge.py`，提供公共 method

**理由**：
- 摘要提取需要訪問 buffer，這是 pty_bridge 的內部狀態
- bot 不應該知道 buffer 細節
- 如果未來加 CLI 接口（非 Telegram），可以直接復用

### Decision 3: 任務完成信號：prompt only vs prompt+keywords

**選擇**：Prompt only（沿用現有 `_check_prompt_detected`）+ 2 秒穩定期

**理由**：
- 現有 prompt detection 已經過大量調試，可靠性高
- 加關鍵詞會增加 false positive 風險（用戶消息中可能包含關鍵詞）
- 2 秒穩定期防止 prompt 殘留誤判（echo skip 階段曾有類似問題）

### Decision 4: 任務模式是否保留 `send()` 接口

**選擇**：保留 `send()`，新增 `send_task()` 作為分支

**理由**：
- `_process_request_in_lock` 現有代碼不需要大改
- 任務模式和對話模式的路徑截然不同，共用一個 `send()` 會增加複雜度
- 清晰的命名讓代碼更容易理解

### Decision 5: 任務排隊：初期不支援

**選擇**：`TASK_QUEUE_MAX = 0` 預設

**理由**：
- 僅單一用戶，排隊需求弱
- 排隊實現增加了複雜度（取消排隊任務、佇列 timeout 等）
- 可以先觀察實際使用情況再決定

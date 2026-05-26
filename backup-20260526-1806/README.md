# PTY Bridge 備份說明

備份時間：2026-05-26 18:06 GMT+8
備份原因：在開始 start() 非阻塞改造前保存當前穩定版本

---

## 當前方案

純 Task 模式。用戶發送任何消息 → 立即回覆「✅ 任務已接收，將在背景執行」→ Claude Code 在背景執行 → 完成後返回結果。

## 已解決的問題

1. **模型名稱 `[1m]` 錯誤**：`deepseek-v4-pro[1m]` 改為 `deepseek-v4-pro`
2. **輸出截斷**：廢除即時輸出轉發，改為任務完成後一次性摘要
3. **輸出內容清理**：`_clean_output` 過濾 ANSI codes、表格、樹狀符號等 Telegram 無法顯示的內容
4. **任務模式 dispatch**：handle_message 從同步 lock 改為背景 task dispatch
5. **`--bare` 模式**：已移除，Claude Code 用正常模式啟動
6. **啟動對話框 dismiss**：startup 階段定期發送 Enter 來關閉可能彈出的對話框

## 已知但未修復的問題

### 高優先
1. **Cancel 後重啟，但新消息無法正常處理**：Cancel 後 Claude Code 會自動重啟，重啟後用戶發消息會顯示「任務已接收」但不會正常輸出
2. **Echo 短中文誤判**：用戶發送短中文詞語（如「你好」）可能被誤判為 echo，導致收到空白回覆（`_echo_detected` 函數存在但未被 bot.py 調用）

### 中優先
3. **`_clean_output` 橫線過濾不完整**：部分分隔線/框線可能未正確過濾，出現在最終摘要中
4. **`start()` 同步阻塞**：Claude Code 啟動期間（最長 90 秒）完全阻塞 Telegram polling，期間無法接收任何消息

### 低優先（可後續處理）
5. **心跳日誌邊界抖動**：`int(elapsed) % int(interval) == 0` 可能錯過觸發點
6. **快速任務延遲偏高**：2s+3s 的穩定窗口對秒回任務有 5 秒固定延遲
7. **舊 `send()` 方法殘留**：不再被使用但仍在代碼中
8. **raw buffer 和 VirtualScreen 渲染不一致**：極端情況下摘要可能包含殘餘內容

## 相關文件

- `config.py`：時間和行為配置（TASK_DEFAULT_TIMEOUT、PROGRESS_INTERVAL 等）
- `pty_bridge.py`：PTY 通訊、Task 模式控制、輸出清理（~1500 行）
- `bot.py`：Telegram bot 邏輯、消息處理、任務監控
- `output_parser.py`：ANSI 清理和輸出提取工具

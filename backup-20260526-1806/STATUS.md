# PTY Bridge 狀態記錄

最後更新：2026-05-26 20:06

## 當前方案
純 Task 模式。發消息 → 「✅ 任務已接收」→ 背景執行 → 完成後回覆。

## 已解決的問題
1. 模型名稱 `[1m]` 錯誤 — 改為 `deepseek-v4-pro`
2. 輸出截斷 — 廢除即時轉發，改為一次性摘要
3. 輸出內容清理 — 過濾 ANSI/表格/樹狀符號
4. 任務模式 dispatch — 從同步 lock 改為背景 task dispatch
5. `--bare` 模式 — 已移除
6. 啟動對話框 dismiss — startup Enter 按鍵
7. Cancel 重啟後消息處理 — 修復競態條件
8. Echo 短中文誤判 — 禁用 echo detection
9. 連續多任務正常
10. `_current_task` 不賦值 — 加 global 聲明
11. `/new` `/stop` bridge=None 崩潰 — 加 None 檢查
12. 代碼塊重插入邏輯 — 修正 placeholder 追回

## 未解決的問題
### Cancel 命令失效
描述：`/cancel` 命令目前無效。
影響：無法取消進行中的任務。

### start() 同步阻塞（中優先）
描述：Claude Code 啟動期間（最長 90 秒）阻塞 Telegram polling。
Claude Code 評估：改為 15 秒短超時 + degraded 模式，30-60 分鐘工作量。

### 其他低優先
- 心跳日誌邊界抖動
- 快速任務延遲偏高（5 秒）
- 舊 send() 方法殘留
- raw buffer 和 VirtualScreen 不一致

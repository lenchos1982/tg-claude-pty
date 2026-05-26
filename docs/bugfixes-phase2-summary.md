# Bugfixes Phase 2 — 交付總結

**日期**: 2026-05-26
**作者**: 阿雕
**服務**: tg-claude-pty (楚熙)

---

## 修改清單

### 1. 修正 echo 短中文誤判

**檔案**: `pty_bridge.py` — `_echo_detected()` 方法

**問題**: 
- Strategy 3（多行匹配）對短行過於寬鬆，中文短詞（如「你好」）必然出現在 Claude 回覆中，>50% 匹配率太容易觸發
- Strategy 2（短消息 ≤3 字符）只用了 `sent_text in head` + 簡單前綴檢查，短中文也很容易誤觸發

**修改**:
- **Strategy 3**: 只計入 ≥4 字符的行的匹配；如果所有非空行都 <4 字符，要求全部出現在 clean 文本中才算數
- **Strategy 2**: 加強前綴檢查——要求短文本前面有 prompt char（❯/▶/>）+ 可選空格，或在行首；不再接受 \n 、\r、\t 等寬泛前綴

### 2. Cancel 後自動重連

**檔案**: `pty_bridge.py` + `bot.py`

**問題**:
- `cancel_current_task()` 在 SIGTERM 後設 `_ready=False; _running=False`，但沒觸發重啟。用戶需要手動 `/stop` 或 `/new`

**修改**:
- **新增 `PtyBridge.restart_sync()`**: 同步重啟方法，kill 舊進程 + cleanup + 重新 `_start_pty()`。安全從任何線程調用
- **`cancel_current_task()`**: SIGTERM 出口新增 `self.restart_sync()` 調用，成功後恢復 `_ready/_running` 狀態並設 `_prompt_ready_event`
- **`_monitor_task_completion()`**: bridge 死亡時自動調用 `_restart_bridge()`，成功後通知用戶重連完成，失敗則提示手動 `/stop`

### 3. `_clean_output` 優化 + 回退保護

**檔案**: `pty_bridge.py` — `_clean_output()` 方法

**審計結論**: 各過濾規則均合理，無明顯誤殺風險。風險最高的是 `_is_horizontal_rule_line()` 中的 catch-all（>50% hr 字符 + <15 非 hr 非空格字符），但其門檻較高，實戰中極難觸發

**修改**:
- **新增回退保護**: 在 filter + post-processing + empty fallback 之後，檢查清理後文本是否 < 原始長度的 20%。若是，回退到 `strip_ansi` + `process_carriage_returns`（跳過所有 heuristic 規則），並記錄 WARNING 日誌

---

## 測試狀態

- ✅ Python 語法檢查: `pty_bridge.py` — 通過
- ✅ Python 語法檢查: `bot.py` — 通過
- ✅ Python 語法檢查: `config.py` — 通過
- ✅ systemd 服務重啟成功
- ✅ Claude Code PTY bridge 初始化成功 (ready 訊息確認)
- ✅ Telegram 消息可送達
- ⏳ 實際任務測試待用戶觸發

## 回滾方式

```bash
# 停止服務
systemctl stop tg-claude-pty

# 恢復備份
cp /root/tg-claude-pty/backup-20260526-152709/pty_bridge.py /root/tg-claude-pty/pty_bridge.py
cp /root/tg-claude-pty/backup-20260526-152709/bot.py /root/tg-claude-pty/bot.py

# 重啟
systemctl start tg-claude-pty
```

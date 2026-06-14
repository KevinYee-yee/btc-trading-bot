# 開發日誌

## 2026-06-14 — 專案啟動

### 完成事項
- 建立專案資料夾結構
- 撰寫 PROJECT_BRIEF.md（專案總覽、架構、風控原則）
- 完成 strategy_A_bollinger_macd.pine（逆勢反轉流 v1.0）
- 完成 strategy_B_ema_rsi.pine（順勢突破流 v1.0）

### 待辦（下一步）
- [ ] 將策略 A 貼入 TradingView，對 BTCUSDT 4H 執行歷史回測
- [ ] 將策略 B 貼入 TradingView，對 BTCUSDT 4H 執行歷史回測
- [ ] 記錄回測結果：淨利、勝率、最大回撤、Sharpe Ratio
- [ ] 根據回測結果決定優先推進哪條策略
- [ ] 建立 TradingView Alert 並設定 Webhook URL（n8n 端）
- [ ] n8n 流程：接收 Webhook → 轉發 Binance API
- [ ] 震撼測試：30–50 USDT 實盤驗證整條鏈路

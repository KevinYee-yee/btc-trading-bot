# AI 自動交易系統 — 專案簡報

**建立日期：** 2026-06-14  
**目標：** BTC/ETH 現貨自動交易，克服人性弱點，長線被動開源 → Tesla Model 3 基金

---

## 核心架構

```
TradingView (Pine Script 策略 + Alert)
        ↓  Webhook JSON
n8n / Make（中繼轉發）
        ↓  REST API
Binance（現貨自動下單）
```

---

## 風控原則（不可違反）

- 僅限 **BTC / ETH 現貨**，禁止槓桿、合約、放空
- 最大回撤控制在 **10% 以內**
- 初期震撼測試金額：**30–50 USDT**
- 確認穩定後進入「每週六覆盤、白天不看盤」節奏

---

## 現況（截至 2026-06-14）

- [x] Binance KYC 身份驗證通過
- [x] 已領取 40 USDT 現貨手續費折抵券（有效期至 2026-06-17）
- [ ] 台灣出入金通道（MAX 交易所）認證中
- [ ] Pine Script 策略腳本開發中
- [ ] Webhook 串接 Binance API

---

## 待開發策略

### 策略 A — 逆勢反轉流（布林通道 + MACD）
- **進場：** 價格跌破布林下軌 + MACD 黃金交叉
- **停利：** 觸及布林上軌
- **停損：** 跌破進場前 3 根 K 線最低點 or 移動停損
- 檔案：`strategy_A_bollinger_macd.pine`

### 策略 B — 順勢突破流（雙 EMA + RSI）
- **進場：** 20 EMA 上穿 50 EMA + RSI 50–70
- **出場：** 移動停損 or 均線死叉
- 檔案：`strategy_B_ema_rsi.pine`

---

## Webhook Alert JSON 格式（串接 Binance 用）

```json
{
  "symbol": "BTCUSDT",
  "side": "BUY",
  "type": "MARKET",
  "quantity": "0.001",
  "strategy": "BB_MACD_v1",
  "price": "{{close}}"
}
```

---

## 資料夾結構

```
AI自動交易系統/
├── PROJECT_BRIEF.md          ← 本檔（專案總覽）
├── strategy_A_bollinger_macd.pine   ← 策略 A 腳本
├── strategy_B_ema_rsi.pine          ← 策略 B 腳本
├── backtest_results/          ← 回測截圖 / 數據記錄
├── webhook_config/            ← n8n / Make 工作流程設定
└── dev_log.md                 ← 開發日誌
```

"""
策略B 歷史回測分析腳本
- 分析期間：2026-06-01 至今
- 目的：診斷7連敗是「市場環境問題」還是「策略邏輯問題」
- 邏輯與 monitor.py 完全一致
"""

import ccxt
import pandas as pd
import numpy as np
from datetime import datetime, timezone

# ── 參數（與 monitor.py 完全一致）─────────────────────
SYMBOL          = "BTC/USDT"
TIMEFRAME       = "15m"
INITIAL_CAPITAL = 1000.0
COMMISSION      = 0.001
RSI_PERIOD      = 9
RSI_BUY         = 40
RSI_SELL        = 62
EMA_FAST        = 13
EMA_SLOW        = 48
COOLDOWN_BARS   = 4     # 出場後冷卻4根K棒（60分鐘）
CIRCUIT_BREAKER = 3     # 連敗幾次觸發熔斷
SL_PCT          = 0.985 # 停損 1.5%
TP_PCT          = 1.010 # 止盈 1.0%
TP_RSI          = 62    # 止盈需同時 RSI>62
HARD_SL         = 0.95  # 硬性停損 5%
EMA_TREND_BARS  = 20    # 趨勢過濾 lookback（根）

START_DATE = "2026-06-01T00:00:00Z"

# ──────────────────────────────────────────────────────
def fetch_data():
    print("📡 從 OKX 抓取 BTC/USDT 15分鐘 K 線（2026-06-01 至今）...")
    exchange = ccxt.okx({"enableRateLimit": True})
    since = exchange.parse8601(START_DATE)
    all_ohlcv = []
    while True:
        ohlcv = exchange.fetch_ohlcv(SYMBOL, TIMEFRAME, since=since, limit=300)
        if not ohlcv:
            break
        all_ohlcv.extend(ohlcv)
        since = ohlcv[-1][0] + 1
        if ohlcv[-1][0] >= exchange.milliseconds():
            break
    df = pd.DataFrame(all_ohlcv, columns=["ts","open","high","low","close","volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df.set_index("ts", inplace=True)
    df = df[~df.index.duplicated()]
    print(f"   取得 {len(df)} 根K棒（{df.index[0]} → {df.index[-1]}）\n")
    return df

def calc_indicators(df):
    # RSI(9)
    delta = df["close"].diff()
    gain  = delta.clip(lower=0).rolling(RSI_PERIOD).mean()
    loss  = (-delta.clip(upper=0)).rolling(RSI_PERIOD).mean()
    df["rsi"] = 100 - (100 / (1 + gain / loss))
    # EMA 13/48
    df["ema_f"] = df["close"].ewm(span=EMA_FAST, adjust=False).mean()
    df["ema_s"] = df["close"].ewm(span=EMA_SLOW, adjust=False).mean()
    df.dropna(inplace=True)
    return df

def run_backtest(df):
    capital = INITIAL_CAPITAL
    position = 0.0
    entry_price = 0.0
    entry_idx = None
    entry_time = None
    last_exit_idx = -999
    consecutive_losses = 0
    trades = []

    for i in range(EMA_TREND_BARS + 1, len(df)):
        row = df.iloc[i]
        price = row["close"]
        ts = df.index[i]

        # ── 有倉位：檢查出場 ──────────────────────────────
        if position > 0:
            exit_reason = None

            if price < entry_price * HARD_SL:
                exit_reason = f"硬性停損5%"
            elif price < entry_price * SL_PCT:
                exit_reason = f"停損1.5%"
            elif row["rsi"] > TP_RSI and price > entry_price * TP_PCT:
                exit_reason = f"止盈（RSI>{TP_RSI}+漲幅≥1%）"

            if exit_reason:
                sell_value = position * price * (1 - COMMISSION)
                cost       = position * entry_price * (1 + COMMISSION)
                pnl        = sell_value - cost
                pnl_pct    = pnl / cost * 100
                capital    = sell_value

                if pnl < 0:
                    consecutive_losses += 1
                else:
                    consecutive_losses = 0

                # 市場環境診斷：進場時 EMA48 斜率
                ema_at_entry    = df["ema_s"].iloc[entry_idx]
                ema_prev_entry  = df["ema_s"].iloc[entry_idx - EMA_TREND_BARS]
                ema_slope_entry = (ema_at_entry - ema_prev_entry) / EMA_TREND_BARS

                # 進場後實際走勢：最低點距離進場
                hold_bars = i - entry_idx
                if hold_bars > 0:
                    actual_low = df["low"].iloc[entry_idx:i+1].min()
                    max_drawdown = (actual_low - entry_price) / entry_price * 100
                else:
                    max_drawdown = 0

                trades.append({
                    "編號":      len(trades) + 1,
                    "進場時間":  entry_time.strftime("%m-%d %H:%M"),
                    "出場時間":  ts.strftime("%m-%d %H:%M"),
                    "持倉K棒":   hold_bars,
                    "進場價":    entry_price,
                    "出場價":    price,
                    "損益%":     pnl_pct,
                    "出場原因":  exit_reason,
                    "進場RSI":   df["rsi"].iloc[entry_idx],
                    "進場EMA斜率": ema_slope_entry,
                    "最大回撤%": max_drawdown,
                    "結果":      "✅ 勝" if pnl > 0 else "❌ 敗",
                    "連敗數":    consecutive_losses if pnl < 0 else 0,
                })

                position = 0.0
                last_exit_idx = i
                continue

        # ── 熔斷檢查 ─────────────────────────────────────
        if consecutive_losses >= CIRCUIT_BREAKER:
            continue

        # ── 冷卻期檢查 ───────────────────────────────────
        if i - last_exit_idx < COOLDOWN_BARS:
            continue

        # ── 無倉位：檢查進場 ──────────────────────────────
        if position == 0:
            rsi = row["rsi"]
            ema_s_now  = df["ema_s"].iloc[i - 1]
            ema_s_prev = df["ema_s"].iloc[i - 1 - EMA_TREND_BARS]
            trend_up   = ema_s_now > ema_s_prev

            if rsi < RSI_BUY and trend_up:
                qty      = capital / price / (1 + COMMISSION)
                position = qty
                entry_price = price
                entry_idx   = i
                entry_time  = ts
                capital     = 0.0

    return trades

def print_report(trades):
    print("=" * 70)
    print("  策略B 回測報告（2026-06-01 至今）")
    print("=" * 70)

    if not trades:
        print("  ⚠️  分析期間內無完成交易")
        return

    wins   = [t for t in trades if t["損益%"] > 0]
    losses = [t for t in trades if t["損益%"] <= 0]
    total_pnl = sum(t["損益%"] for t in trades)

    print(f"\n  總交易：{len(trades)} 筆  |  勝：{len(wins)}  敗：{len(losses)}  "
          f"勝率：{len(wins)/len(trades)*100:.0f}%  累計損益：{total_pnl:+.2f}%\n")

    print(f"  {'編'} {'進場時間':>12} {'出場時間':>12} {'持棒':>4} {'進場價':>9} {'出場價':>9} "
          f"{'損益%':>7} {'進場RSI':>7} {'EMA斜率':>8} {'最大回撤':>8}  出場原因")
    print("  " + "-" * 110)

    for t in trades:
        icon = "✅" if t["損益%"] > 0 else "❌"
        print(f"  {t['編號']:>2} {t['進場時間']:>12} {t['出場時間']:>12} "
              f"{t['持倉K棒']:>4} ${t['進場價']:>8,.0f} ${t['出場價']:>8,.0f} "
              f"{t['損益%']:>+6.2f}% {t['進場RSI']:>7.1f} "
              f"{t['進場EMA斜率']:>+8.3f} {t['最大回撤%']:>+7.2f}%  "
              f"{icon} {t['出場原因']}")

    # ── 診斷分析 ──────────────────────────────────────
    print("\n" + "=" * 70)
    print("  診斷分析")
    print("=" * 70)

    # 1. EMA斜率分析：進場時趨勢是否真的向上？
    loss_slopes  = [t["進場EMA斜率"] for t in losses]
    win_slopes   = [t["進場EMA斜率"] for t in wins]
    print(f"\n  📊 EMA48斜率（趨勢過濾）")
    print(f"     敗場平均：{np.mean(loss_slopes):+.4f}  "
          f"（{'趨勢過濾未能阻擋假訊號' if np.mean(loss_slopes) < 0.5 else '趨勢向上但仍虧損'}）")
    if wins:
        print(f"     勝場平均：{np.mean(win_slopes):+.4f}")

    # 2. 停損觸發分佈
    sl_triggers = [t for t in losses if "停損" in t["出場原因"] or "硬性" in t["出場原因"]]
    tp_triggers = [t for t in wins if "止盈" in t["出場原因"]]
    print(f"\n  📊 出場類型分佈")
    print(f"     停損觸發：{len(sl_triggers)} 筆")
    print(f"     止盈觸發：{len(tp_triggers)} 筆")

    # 3. 最大回撤分析
    print(f"\n  📊 進場後最大回撤")
    for t in trades:
        icon = "✅" if t["損益%"] > 0 else "❌"
        print(f"     {icon} #{t['編號']} 進場後最低跌 {t['最大回撤%']:+.2f}%  "
              f"（停損設在 -1.5%，止盈需 +1.0%）")

    # 4. 結論
    print(f"\n  💡 結論")
    avg_loss_slope = np.mean(loss_slopes) if loss_slopes else 0
    all_sl_triggered = len(sl_triggers) == len(losses)

    if avg_loss_slope < 0.1:
        print("  → EMA斜率偏低，趨勢過濾條件不夠嚴格，仍在偽上升趨勢進場（市場環境問題）")
    else:
        print("  → 進場時趨勢向上，但進場後立即反轉（策略邏輯問題：進場條件不夠嚴格）")

    if all_sl_triggered:
        print("  → 所有敗場均由停損觸發，止盈門檻 1.0% 可能過高，勝場難以實現")
    else:
        print("  → 有非停損觸發的敗場，需進一步分析出場條件")

    loss_avg = np.mean([t["損益%"] for t in losses]) if losses else 0
    win_avg  = np.mean([t["損益%"] for t in wins]) if wins else 0
    print(f"\n  → 平均獲利：{win_avg:+.2f}%  平均虧損：{loss_avg:+.2f}%")
    if wins and losses:
        rr = abs(win_avg / loss_avg)
        print(f"  → 實際 R:R = {rr:.2f}x  "
              f"（需 >{1/max(len(wins)/len(trades),0.001):.1f}x 才能正期望值）")

    print()


if __name__ == "__main__":
    df = fetch_data()
    df = calc_indicators(df)
    trades = run_backtest(df)
    print_report(trades)

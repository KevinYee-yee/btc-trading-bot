"""
AI 自動交易系統 — Python 回測引擎
抓取幣安 BTCUSDT 歷史 4H K 線，模擬策略 A 和策略 B 的交易表現
"""

import ccxt
import pandas as pd
import numpy as np
from datetime import datetime

# ─────────────────────────────────────────────
# 1. 抓取歷史數據（幣安公開 API，不需要金鑰）
# ─────────────────────────────────────────────
def fetch_ohlcv(symbol="BTC/USDT", timeframe="4h", days=365):
    exchange = ccxt.binance({"enableRateLimit": True})
    since = exchange.milliseconds() - days * 24 * 60 * 60 * 1000
    all_ohlcv = []
    while True:
        batch = exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=1000)
        if not batch:
            break
        all_ohlcv += batch
        since = batch[-1][0] + 1
        if len(batch) < 1000:
            break
    df = pd.DataFrame(all_ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df.set_index("timestamp", inplace=True)
    df = df[~df.index.duplicated()]
    return df

# ─────────────────────────────────────────────
# 2. 技術指標計算
# ─────────────────────────────────────────────
def calc_bollinger(df, length=20, mult=2.0):
    df["bb_mid"]   = df["close"].rolling(length).mean()
    df["bb_std"]   = df["close"].rolling(length).std()
    df["bb_upper"] = df["bb_mid"] + mult * df["bb_std"]
    df["bb_lower"] = df["bb_mid"] - mult * df["bb_std"]
    return df

def calc_macd(df, fast=12, slow=26, signal=9):
    ema_fast = df["close"].ewm(span=fast, adjust=False).mean()
    ema_slow = df["close"].ewm(span=slow, adjust=False).mean()
    df["macd"]        = ema_fast - ema_slow
    df["macd_signal"] = df["macd"].ewm(span=signal, adjust=False).mean()
    df["macd_cross"]  = (df["macd"] > df["macd_signal"]) & (df["macd"].shift(1) <= df["macd_signal"].shift(1))
    return df

def calc_ema(df, fast=20, slow=50):
    df["ema_fast"] = df["close"].ewm(span=fast, adjust=False).mean()
    df["ema_slow"] = df["close"].ewm(span=slow, adjust=False).mean()
    df["ema_cross"] = (df["ema_fast"] > df["ema_slow"]) & (df["ema_fast"].shift(1) <= df["ema_slow"].shift(1))
    df["ema_dead"]  = (df["ema_fast"] < df["ema_slow"]) & (df["ema_fast"].shift(1) >= df["ema_slow"].shift(1))
    return df

def calc_rsi(df, length=14):
    delta = df["close"].diff()
    gain  = delta.clip(lower=0).rolling(length).mean()
    loss  = (-delta.clip(upper=0)).rolling(length).mean()
    rs    = gain / loss
    df["rsi"] = 100 - (100 / (1 + rs))
    return df

# ─────────────────────────────────────────────
# 3. 回測引擎
# ─────────────────────────────────────────────
def backtest(df, signals, initial_capital=1000, commission=0.001, trail_pct=None, sl_lookback=3):
    capital    = initial_capital
    position   = 0.0      # 持有 BTC 數量
    entry_price = 0.0
    trail_high  = 0.0
    trades     = []

    for i in range(sl_lookback + 1, len(df)):
        row   = df.iloc[i]
        price = row["close"]
        sig   = signals.iloc[i]

        # 有倉位時的出場判斷
        if position > 0:
            exit_reason = None

            # 移動停損
            if trail_pct:
                trail_high = max(trail_high, price)
                if price <= trail_high * (1 - trail_pct / 100):
                    exit_reason = f"移動停損 (高點{trail_high:.0f}→{price:.0f})"

            # 策略訊號要求出場
            if sig.get("exit"):
                exit_reason = sig["exit_reason"]

            if exit_reason:
                sell_value = position * price * (1 - commission)
                pnl        = sell_value - (position * entry_price * (1 + commission))
                pnl_pct    = pnl / (position * entry_price * (1 + commission)) * 100
                capital   += sell_value
                trades.append({
                    "exit_time":   row.name,
                    "entry_price": entry_price,
                    "exit_price":  price,
                    "pnl":         pnl,
                    "pnl_pct":     pnl_pct,
                    "reason":      exit_reason,
                })
                position   = 0
                entry_price = 0
                trail_high  = 0

        # 無倉位時的進場判斷
        if position == 0 and sig.get("entry"):
            buy_cost   = capital * (1 + commission)
            position   = capital / price / (1 + commission)
            entry_price = price
            trail_high  = price
            capital    = 0

    # 強制平倉（回測結束）
    if position > 0:
        price      = df.iloc[-1]["close"]
        sell_value = position * price * (1 - commission)
        pnl        = sell_value - (position * entry_price * (1 + commission))
        pnl_pct    = pnl / (position * entry_price * (1 + commission)) * 100
        capital   += sell_value
        trades.append({
            "exit_time":   df.index[-1],
            "entry_price": entry_price,
            "exit_price":  price,
            "pnl":         pnl,
            "pnl_pct":     pnl_pct,
            "reason":      "回測結束強制平倉",
        })

    trades_df = pd.DataFrame(trades)
    return trades_df, capital

# ─────────────────────────────────────────────
# 4. 策略 A 訊號產生（布林 + MACD）
# ─────────────────────────────────────────────
def signals_strategy_a(df, sl_lookback=3, macd_window=3):
    sigs = []
    in_trade = False

    for i in range(len(df)):
        row = df.iloc[i]
        sig = {"entry": False, "exit": False, "exit_reason": ""}

        if not in_trade:
            # 進場：價格在布林下軌附近（<= 下軌 * 1.005）
            # 且過去 N 根 K 線內出現 MACD 黃金交叉
            near_lower = row["close"] <= row["bb_lower"] * 1.005
            recent_cross = df["macd_cross"].iloc[max(0, i - macd_window):i + 1].any()
            if near_lower and recent_cross:
                sig["entry"] = True
                in_trade = True
        else:
            # 停利：觸及布林上軌
            if row["close"] >= row["bb_upper"]:
                sig["exit"]        = True
                sig["exit_reason"] = "觸及布林上軌停利"
                in_trade = False
            # 固定停損：跌破前 N 根最低點
            elif i >= sl_lookback:
                recent_low = df["low"].iloc[i - sl_lookback:i].min()
                if row["close"] < recent_low:
                    sig["exit"]        = True
                    sig["exit_reason"] = "跌破近期低點停損"
                    in_trade = False

        sigs.append(sig)

    return pd.DataFrame(sigs, index=df.index)

# ─────────────────────────────────────────────
# 5. 策略 B 訊號產生（雙 EMA + RSI）
# ─────────────────────────────────────────────
def signals_strategy_b(df):
    sigs = []
    in_trade = False

    for i in range(len(df)):
        row = df.iloc[i]
        sig = {"entry": False, "exit": False, "exit_reason": ""}

        if not in_trade:
            # 進場：EMA 黃金交叉 + RSI 50–70
            if row["ema_cross"] and 50 <= row["rsi"] <= 70:
                sig["entry"] = True
                in_trade = True
        else:
            # 出場：EMA 死叉
            if row["ema_dead"]:
                sig["exit"]        = True
                sig["exit_reason"] = "EMA 死叉出場"
                in_trade = False

        sigs.append(sig)

    return pd.DataFrame(sigs, index=df.index)

# ─────────────────────────────────────────────
# 6. 績效統計
# ─────────────────────────────────────────────
def performance(trades_df, final_capital, initial_capital=1000):
    if trades_df.empty:
        print("  ⚠️  無交易記錄")
        return

    total_trades = len(trades_df)
    wins         = trades_df[trades_df["pnl"] > 0]
    losses       = trades_df[trades_df["pnl"] <= 0]
    win_rate     = len(wins) / total_trades * 100
    net_profit   = final_capital - initial_capital
    net_pct      = net_profit / initial_capital * 100

    # 最大回撤計算
    cumulative = [initial_capital]
    cap = initial_capital
    for _, row in trades_df.iterrows():
        cap += row["pnl"]
        cumulative.append(cap)
    peak     = pd.Series(cumulative).cummax()
    drawdown = ((pd.Series(cumulative) - peak) / peak * 100).min()

    avg_win  = wins["pnl_pct"].mean()  if not wins.empty   else 0
    avg_loss = losses["pnl_pct"].mean() if not losses.empty else 0
    profit_factor = (wins["pnl"].sum() / abs(losses["pnl"].sum())) if not losses.empty and losses["pnl"].sum() != 0 else float("inf")

    print(f"  總交易次數：{total_trades}")
    print(f"  勝率：       {win_rate:.1f}%  ({len(wins)}勝 / {len(losses)}敗)")
    print(f"  淨利：       {net_profit:+.2f} USDT  ({net_pct:+.1f}%)")
    print(f"  最大回撤：   {drawdown:.1f}%  {'✅' if abs(drawdown) < 10 else '❌ 超過10%警戒線'}")
    print(f"  Profit Factor：{profit_factor:.2f}  {'✅' if profit_factor > 1.5 else '⚠️ 偏低'}")
    print(f"  平均獲利：   +{avg_win:.1f}%  |  平均虧損：{avg_loss:.1f}%")
    print()
    print("  最近 5 筆交易：")
    for _, t in trades_df.tail(5).iterrows():
        icon = "🟢" if t["pnl"] > 0 else "🔴"
        print(f"  {icon} {t['exit_time'].strftime('%Y-%m-%d')}  {t['entry_price']:.0f}→{t['exit_price']:.0f}  {t['pnl_pct']:+.1f}%  [{t['reason']}]")

# ─────────────────────────────────────────────
# 主程式
# ─────────────────────────────────────────────
if __name__ == "__main__":
    INITIAL_CAPITAL = 1000   # 模擬 1000 USDT

    print("=" * 60)
    print("  AI 自動交易回測引擎")
    print(f"  資產：BTCUSDT 4H  |  回測期間：過去 365 天")
    print(f"  模擬資金：{INITIAL_CAPITAL} USDT  |  手續費：0.1%/筆")
    print("=" * 60)

    print("\n⏳ 正在從幣安抓取歷史數據...")
    df = fetch_ohlcv("BTC/USDT", "4h", days=365)
    print(f"✅ 取得 {len(df)} 根 K 線  ({df.index[0].date()} → {df.index[-1].date()})")

    # 計算指標
    df = calc_bollinger(df)
    df = calc_macd(df)
    df = calc_ema(df)
    df = calc_rsi(df)
    df.dropna(inplace=True)

    # ── 策略 A ──────────────────────────────────
    print("\n" + "─" * 60)
    print("  策略 A：布林通道 + MACD 逆勢反轉")
    print("─" * 60)
    sigs_a   = signals_strategy_a(df)
    trades_a, final_a = backtest(df, sigs_a, INITIAL_CAPITAL, trail_pct=None)
    performance(trades_a, final_a, INITIAL_CAPITAL)

    # ── 策略 A（含移動停損）────────────────────
    print("\n" + "─" * 60)
    print("  策略 A（移動停損 3%）：布林通道 + MACD")
    print("─" * 60)
    trades_a2, final_a2 = backtest(df, sigs_a, INITIAL_CAPITAL, trail_pct=3.0)
    performance(trades_a2, final_a2, INITIAL_CAPITAL)

    # ── 策略 B ──────────────────────────────────
    print("\n" + "─" * 60)
    print("  策略 B：雙 EMA（20/50）+ RSI 順勢突破")
    print("─" * 60)
    sigs_b   = signals_strategy_b(df)
    trades_b, final_b = backtest(df, sigs_b, INITIAL_CAPITAL, trail_pct=3.0)
    performance(trades_b, final_b, INITIAL_CAPITAL)

    # ── 買入持有基準（Buy & Hold）──────────────
    print("\n" + "─" * 60)
    print("  基準比較：買入持有（Buy & Hold）")
    print("─" * 60)
    bh_entry = df["close"].iloc[0]
    bh_exit  = df["close"].iloc[-1]
    bh_pnl   = (bh_exit - bh_entry) / bh_entry * 100
    print(f"  進場價：{bh_entry:.0f}  → 現價：{bh_exit:.0f}")
    print(f"  報酬：  {bh_pnl:+.1f}%  (持有 365 天不動)")

    print("\n" + "=" * 60)
    print("  ✅ 回測完成")
    print("=" * 60)

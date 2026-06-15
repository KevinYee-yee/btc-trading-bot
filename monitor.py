"""
策略 A/B/C/D 即時紙上交易監測器
STRATEGY 環境變數決定執行哪個策略（A/B/C/D）
"""

import ccxt
import pandas as pd
import numpy as np
import json
import csv
import os
import urllib.request
import urllib.parse
from datetime import datetime, timezone

# ─────────────────────────────────────────────
# 設定
# ─────────────────────────────────────────────
STRATEGY        = os.environ.get("STRATEGY", "A")
SYMBOL          = os.environ.get("SYMBOL", "BTC/USDT")
TIMEFRAME       = "15m"
INITIAL_CAPITAL = 1000.0
COMMISSION      = 0.001
SL_LOOKBACK     = 3
MACD_WINDOW     = 3
BB_LENGTH       = 20
BB_MULT         = 2.0
MACD_FAST       = 12
MACD_SLOW       = 26
MACD_SIGNAL_P   = 9

# 策略唯一鍵（含標的前綴）
ASSET     = "ETH" if "ETH" in SYMBOL else "BTC"
STRAT_KEY = f"ETH_{STRATEGY}" if ASSET == "ETH" else STRATEGY

PORTFOLIO_FILE  = "paper_portfolio.json" if STRAT_KEY == "A" else f"paper_portfolio_{STRAT_KEY.lower()}.json"
TRADE_LOG_FILE  = "trade_log.csv"        if STRAT_KEY == "A" else f"trade_log_{STRAT_KEY.lower()}.csv"

STRATEGY_LABEL = {
    "A":     "BTC 策略A：布林+MACD",
    "B":     "BTC 策略B：RSI超賣",
    "C":     "BTC 策略C：EMA交叉",
    "D":     "BTC 策略D：MACD零軸",
    "ETH_B": "ETH 策略B：RSI超賣",
    "ETH_C": "ETH 策略C：EMA交叉",
}

FORCE_TEST = os.environ.get("FORCE_TEST", "")
TG_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TG_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
GS_WEBHOOK = os.environ.get("GS_WEBHOOK", "https://script.google.com/macros/s/AKfycbywcYNXYwDN6Z70F0-1nxVj6f3nzqyyoiugO_Mkiy5LPjXbFb5RP126d79VgqjnWlwJ/exec")

# ─────────────────────────────────────────────
# 工具函數
# ─────────────────────────────────────────────
def notify(msg):
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    try:
        url  = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        data = urllib.parse.urlencode({"chat_id": TG_CHAT_ID, "text": msg, "parse_mode": "HTML"}).encode()
        urllib.request.urlopen(url, data, timeout=10)
    except Exception as e:
        print(f"  ⚠️ Telegram 失敗：{e}")

def sheets_post(payload):
    if not GS_WEBHOOK:
        return
    payload["strategy"] = STRAT_KEY
    try:
        class StopRedirect(urllib.request.HTTPRedirectHandler):
            def http_error_302(self, req, fp, code, msg, headers):
                raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)
        data   = json.dumps(payload).encode("utf-8")
        req    = urllib.request.Request(GS_WEBHOOK, data=data, headers={"Content-Type": "application/json"})
        opener = urllib.request.build_opener(StopRedirect())
        try:
            opener.open(req, timeout=15)
        except urllib.error.HTTPError as e:
            if e.code == 302:
                loc = e.headers.get("Location", "")
                if loc:
                    urllib.request.urlopen(loc, timeout=15)
                    print("  ✅ Google Sheets 已更新")
            else:
                raise
    except Exception as e:
        print(f"  ⚠️ Google Sheets 失敗：{e}")

def load_portfolio():
    if os.path.exists(PORTFOLIO_FILE):
        with open(PORTFOLIO_FILE) as f:
            return json.load(f)
    return {"capital": INITIAL_CAPITAL, "position": 0.0, "entry_price": 0.0,
            "entry_time": "", "last_candle": "", "total_trades": 0,
            "wins": 0, "losses": 0, "total_pnl": 0.0}

def save_portfolio(p):
    with open(PORTFOLIO_FILE, "w") as f:
        json.dump(p, f, indent=2, ensure_ascii=False)

def log_trade(action, price, qty, pnl_pct, reason, portfolio):
    header = not os.path.exists(TRADE_LOG_FILE)
    with open(TRADE_LOG_FILE, "a", newline="") as f:
        w = csv.writer(f)
        if header:
            w.writerow(["time","action","price","qty_btc","pnl_pct","capital_after","reason"])
        w.writerow([
            datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
            action, f"{price:.2f}", f"{qty:.6f}",
            f"{pnl_pct:+.2f}%" if pnl_pct is not None else "",
            f"{portfolio['capital']:.2f}", reason,
        ])

# ─────────────────────────────────────────────
# 抓數據 + 計算所有指標
# ─────────────────────────────────────────────
def fetch_and_calc():
    exchange = ccxt.okx({"enableRateLimit": True})
    ohlcv = exchange.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=120)
    df = pd.DataFrame(ohlcv, columns=["ts","open","high","low","close","volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df.set_index("ts", inplace=True)

    # 布林通道（策略 A 用）
    df["bb_mid"]   = df["close"].rolling(BB_LENGTH).mean()
    df["bb_std"]   = df["close"].rolling(BB_LENGTH).std()
    df["bb_upper"] = df["bb_mid"] + BB_MULT * df["bb_std"]
    df["bb_lower"] = df["bb_mid"] - BB_MULT * df["bb_std"]

    # MACD（策略 A、D 用）
    ema_fast        = df["close"].ewm(span=MACD_FAST, adjust=False).mean()
    ema_slow        = df["close"].ewm(span=MACD_SLOW, adjust=False).mean()
    df["macd"]      = ema_fast - ema_slow
    df["macd_sig"]  = df["macd"].ewm(span=MACD_SIGNAL_P, adjust=False).mean()
    df["macd_cross"] = (df["macd"] > df["macd_sig"]) & (df["macd"].shift(1) <= df["macd_sig"].shift(1))

    # RSI（策略 B 用）
    delta       = df["close"].diff()
    gain        = delta.clip(lower=0).rolling(14).mean()
    loss        = (-delta.clip(upper=0)).rolling(14).mean()
    df["rsi"]   = 100 - (100 / (1 + gain / loss))

    # EMA 快慢線（策略 C 用）
    df["ema9"]  = df["close"].ewm(span=9,  adjust=False).mean()
    df["ema21"] = df["close"].ewm(span=21, adjust=False).mean()

    df.dropna(inplace=True)
    return df

# ─────────────────────────────────────────────
# 各策略訊號邏輯
# ─────────────────────────────────────────────
def get_entry_signal(df, latest):
    """回傳 (should_buy, cond1_str, cond2_str)"""
    price = latest["close"]

    if STRATEGY == "A":
        near_lower   = price <= latest["bb_lower"] * 1.005
        recent_cross = df["macd_cross"].iloc[-(MACD_WINDOW + 2):-1].any()
        return near_lower and recent_cross, "✅" if near_lower else "❌", "✅" if recent_cross else "❌"

    elif STRATEGY == "B":
        rsi = latest["rsi"]
        return rsi < 35, f"RSI {rsi:.1f}", "✅" if rsi < 35 else "❌"

    elif STRATEGY == "C":
        ema9_now, ema21_now   = df["ema9"].iloc[-2], df["ema21"].iloc[-2]
        ema9_prev, ema21_prev = df["ema9"].iloc[-3], df["ema21"].iloc[-3]
        cross_up = (ema9_now > ema21_now) and (ema9_prev <= ema21_prev)
        return cross_up, f"EMA9 {ema9_now:.0f}", f"EMA21 {ema21_now:.0f}"

    elif STRATEGY == "D":
        macd_now  = df["macd"].iloc[-2]
        macd_prev = df["macd"].iloc[-3]
        cross_up  = (macd_now > 0) and (macd_prev <= 0)
        return cross_up, f"MACD {macd_now:.1f}", "✅零軸上" if macd_now > 0 else "❌零軸下"

    return False, "—", "—"


def get_exit_reason(df, latest, portfolio):
    """回傳出場原因字串，無則回傳 None"""
    price       = latest["close"]
    entry_price = portfolio["entry_price"]

    # 10% 硬性停損（所有策略共用）
    if price < entry_price * 0.90:
        return "跌幅超過10%強制停損"

    if STRATEGY == "A":
        recent_low = df["low"].iloc[-(SL_LOOKBACK + 2):-1].min()
        if price >= latest["bb_upper"]:  return "觸及布林上軌停利"
        if price < recent_low:           return "跌破近期低點停損"

    elif STRATEGY == "B":
        rsi = latest["rsi"]
        if rsi > 65:                     return "RSI超買停利"
        if price < entry_price * 0.92:   return "跌幅超過8%停損"

    elif STRATEGY == "C":
        ema9_now, ema21_now   = df["ema9"].iloc[-2], df["ema21"].iloc[-2]
        ema9_prev, ema21_prev = df["ema9"].iloc[-3], df["ema21"].iloc[-3]
        if (ema9_now < ema21_now) and (ema9_prev >= ema21_prev):
            return "EMA死叉賣出"
        if price < entry_price * 0.92:   return "跌幅超過8%停損"

    elif STRATEGY == "D":
        macd_now  = df["macd"].iloc[-2]
        macd_prev = df["macd"].iloc[-3]
        if (macd_now < 0) and (macd_prev >= 0): return "MACD跌破零軸賣出"
        if price < entry_price * 0.92:           return "跌幅超過8%停損"

    return None

# ─────────────────────────────────────────────
# 主邏輯
# ─────────────────────────────────────────────
def run():
    label   = STRATEGY_LABEL.get(STRAT_KEY, f"策略{STRAT_KEY}")
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"\n{'='*55}")
    print(f"  📡 {label}  |  {now_str}")
    print(f"{'='*55}")

    portfolio = load_portfolio()
    df        = fetch_and_calc()
    latest    = df.iloc[-2]
    candle_time = str(latest.name)

    if candle_time == portfolio["last_candle"]:
        print(f"  ⏭  本 K 線已處理過，跳過（{candle_time}）")
        return

    price     = latest["close"]
    bb_upper  = latest.get("bb_upper", 0)
    bb_lower  = latest.get("bb_lower", 0)
    recent_low = df["low"].iloc[-(SL_LOOKBACK + 2):-1].min()

    print(f"  K 線：{candle_time}  |  收盤：${price:,.2f}")

    now_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")

    # ── 強制測試 ────────────────────────────
    if FORCE_TEST == "buy" and portfolio["position"] == 0:
        print("  🧪 強制買入")
        _execute_buy(df, latest, portfolio, price, bb_upper, bb_lower, recent_low,
                     now_time, "強制測試買入", "🧪", "🧪")
        return
    if FORCE_TEST == "sell" and portfolio["position"] > 0:
        print("  🧪 強制賣出")
        _execute_sell(df, latest, portfolio, price, bb_upper, bb_lower, "強制測試賣出", now_time)
        return

    # ── 有倉位：檢查出場 ────────────────────
    if portfolio["position"] > 0:
        reason = get_exit_reason(df, latest, portfolio)
        if reason:
            _execute_sell(df, latest, portfolio, price, bb_upper, bb_lower, reason, now_time)
            return

    # ── 無倉位：檢查進場 ────────────────────
    buy, cond1, cond2 = get_entry_signal(df, latest)
    if portfolio["position"] == 0 and buy:
        _execute_buy(df, latest, portfolio, price, bb_upper, bb_lower, recent_low,
                     now_time, "策略訊號進場", cond1, cond2)
    else:
        print(f"  ⏸  無訊號（{cond1} / {cond2}）")

    portfolio["last_candle"] = candle_time
    save_portfolio(portfolio)

    sheets_post({
        "type": "monitor_log",
        "time": now_time, "price": str(price),
        "bb_upper": str(bb_upper), "bb_lower": str(bb_lower),
        "recent_low": str(recent_low),
        "cond_bb":   cond1, "cond_macd": cond2,
        "signal":    "進場" if (portfolio["position"] > 0) else "無訊號",
        "account_status": "持倉" if portfolio["position"] > 0 else "空倉",
        "portfolio": portfolio,
    })


def _execute_buy(df, latest, portfolio, price, bb_upper, bb_lower, recent_low,
                 now_time, reason, cond1, cond2):
    qty = portfolio["capital"] / price / (1 + COMMISSION)
    portfolio["position"]    = qty
    portfolio["entry_price"] = price
    portfolio["entry_time"]  = str(latest.name)
    portfolio["capital"]     = 0.0
    portfolio["last_candle"] = str(latest.name)
    log_trade("BUY", price, qty, None, reason, portfolio)
    save_portfolio(portfolio)
    sheets_post({"type":"trade","time":now_time,"action":"BUY","price":str(price),
                 "qty":str(qty),"pnl_pct":"","capital_after":"0","reason":reason,
                 "portfolio":portfolio,"bb_upper":str(bb_upper),"bb_lower":str(bb_lower)})
    sheets_post({"type":"monitor_log","time":now_time,"price":str(price),
                 "bb_upper":str(bb_upper),"bb_lower":str(bb_lower),"recent_low":str(recent_low),
                 "cond_bb":cond1,"cond_macd":cond2,"signal":"進場","account_status":"持倉",
                 "portfolio":portfolio})
    notify(f"🔔 <b>進場｜{STRATEGY_LABEL.get(STRATEGY)}</b>\n"
           f"進場價：<b>${price:,.2f}</b>\n"
           f"模擬買入：{qty:.6f} BTC\n"
           f"原因：{reason}")
    print(f"  🔔 買入 {qty:.6f} BTC @ ${price:,.2f}")


def _execute_sell(df, latest, portfolio, price, bb_upper, bb_lower, reason, now_time):
    qty         = portfolio["position"]
    entry_price = portfolio["entry_price"]
    sell_value  = qty * price * (1 - COMMISSION)
    cost        = qty * entry_price * (1 + COMMISSION)
    pnl         = sell_value - cost
    pnl_pct     = pnl / cost * 100
    portfolio["capital"]      += sell_value
    portfolio["position"]      = 0.0
    portfolio["entry_price"]   = 0.0
    portfolio["entry_time"]    = ""
    portfolio["last_candle"]   = str(latest.name)
    portfolio["total_trades"] += 1
    portfolio["total_pnl"]    += pnl
    if pnl > 0: portfolio["wins"]   += 1
    else:        portfolio["losses"] += 1
    log_trade("SELL", price, qty, pnl_pct, reason, portfolio)
    save_portfolio(portfolio)
    sheets_post({"type":"trade","time":now_time,"action":"SELL","price":str(price),
                 "qty":str(qty),"pnl_pct":f"{pnl_pct:+.2f}%","capital_after":str(portfolio["capital"]),
                 "reason":reason,"portfolio":portfolio,"bb_upper":str(bb_upper),"bb_lower":str(bb_lower)})
    icon = "🟢" if pnl > 0 else "🔴"
    notify(f"{icon} <b>出場｜{STRATEGY_LABEL.get(STRATEGY)}</b>\n"
           f"進場：${entry_price:,.2f} → 出場：${price:,.2f}\n"
           f"損益：<b>{pnl_pct:+.2f}%（{pnl:+.2f} USDT）</b>\n"
           f"原因：{reason}")
    print(f"  {icon} 賣出 @ ${price:,.2f}  損益：{pnl_pct:+.2f}%  原因：{reason}")


if __name__ == "__main__":
    run()

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
RSI_PERIOD      = 9
RSI_BUY         = 40
RSI_SELL        = 62
EMA_FAST        = 13
EMA_SLOW        = 48
COOLDOWN_BARS   = 4   # P1：出場後冷卻根數（B/ETH_B 用）
EMA_TREND_BARS  = 48  # 策略B趨勢過濾 lookback（根）：原20根(5h)→48根(12h)，2026-06-21 5人會議

# 策略唯一鍵（含標的前綴）
if "ETH" in SYMBOL:
    ASSET = "ETH"
    STRAT_KEY = f"ETH_{STRATEGY}"
elif "SOL" in SYMBOL:
    ASSET = "SOL"
    STRAT_KEY = f"SOL_{STRATEGY}"
else:
    ASSET = "BTC"
    STRAT_KEY = STRATEGY

PORTFOLIO_FILE  = "paper_portfolio.json" if STRAT_KEY == "A" else f"paper_portfolio_{STRAT_KEY.lower()}.json"
TRADE_LOG_FILE  = "trade_log.csv"        if STRAT_KEY == "A" else f"trade_log_{STRAT_KEY.lower()}.csv"

STRATEGY_LABEL = {
    "A":     "BTC 策略A：布林+MACD+RSI",
    "B":     "BTC 策略B：RSI(9)<40",
    "C":     "BTC 策略C：EMA13/48",
    "D":     "BTC 策略D：MACD信號線",
    "ETH_B": "ETH 策略B：RSI(9)<40",
    "ETH_C": "ETH 策略C：EMA13/48",
    "SOL_B": "SOL 策略B：RSI(9)<40",
    "SOL_C": "SOL 策略C：EMA13/48",
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
        p = json.load(open(PORTFOLIO_FILE))
        p.setdefault("last_exit_candle", "")
        p.setdefault("consecutive_losses", 0)  # 連敗熔斷計數
        return p
    return {"capital": INITIAL_CAPITAL, "position": 0.0, "entry_price": 0.0,
            "entry_time": "", "last_candle": "", "total_trades": 0,
            "wins": 0, "losses": 0, "total_pnl": 0.0,
            "last_exit_candle": "", "consecutive_losses": 0}

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

    # 布林通道（策略 A、C 用）
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

    # RSI(9)（策略 A 確認 + 策略 B 用）
    delta       = df["close"].diff()
    gain        = delta.clip(lower=0).rolling(RSI_PERIOD).mean()
    loss        = (-delta.clip(upper=0)).rolling(RSI_PERIOD).mean()
    df["rsi"]   = 100 - (100 / (1 + gain / loss))

    # EMA 13/48（策略 B 趨勢過濾 + 策略 C 用）
    df["ema_f"] = df["close"].ewm(span=EMA_FAST, adjust=False).mean()
    df["ema_s"] = df["close"].ewm(span=EMA_SLOW, adjust=False).mean()

    # MACD 信號線穿越（策略 D 用）
    df["macd_sig_cross"] = (df["macd"] > df["macd_sig"]) & (df["macd"].shift(1) <= df["macd_sig"].shift(1))
    df["macd_sig_death"] = (df["macd"] < df["macd_sig"]) & (df["macd"].shift(1) >= df["macd_sig"].shift(1))

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
        rsi_ok       = latest["rsi"] < 45
        c1 = "✅" if near_lower else "❌"
        c2 = f"MACD {'✅' if recent_cross else '❌'} RSI {latest['rsi']:.0f}{'✅' if rsi_ok else '❌'}"
        return near_lower and recent_cross and rsi_ok, c1, c2

    elif STRATEGY == "B":
        rsi = latest["rsi"]
        # EMA48 趨勢過濾：20根K棒（5小時）確認上升，避免下跌趨勢接刀
        ema_s_now  = df["ema_s"].iloc[-2]
        ema_s_prev = df["ema_s"].iloc[-EMA_TREND_BARS]
        trend_up   = ema_s_now > ema_s_prev
        ok = rsi < RSI_BUY and trend_up
        return ok, f"RSI(9) {rsi:.1f}", f"{'✅' if rsi < RSI_BUY else '❌'}<{RSI_BUY} EMA48趨勢{'✅' if trend_up else '❌'}"

    elif STRATEGY == "C":
        ef_now,  es_now  = df["ema_f"].iloc[-2], df["ema_s"].iloc[-2]
        ef_prev, es_prev = df["ema_f"].iloc[-3], df["ema_s"].iloc[-3]
        cross_up  = (ef_now > es_now) and (ef_prev <= es_prev)
        # P2：只在布林中軌以下做多，避免在高點買入
        below_mid = price < latest["bb_mid"]
        return cross_up and below_mid, f"EMA{EMA_FAST} {ef_now:.0f}", f"EMA{EMA_SLOW} {es_now:.0f} 中軌{'✅' if below_mid else '❌'}"

    elif STRATEGY == "D":
        macd_now   = df["macd"].iloc[-2]
        sig_cross  = df["macd_sig_cross"].iloc[-2]
        above_zero = macd_now > 0
        return sig_cross and above_zero, f"MACD {macd_now:.1f}", f"{'✅' if sig_cross else '❌'}信號穿越+{'✅' if above_zero else '❌'}零軸上"

    return False, "—", "—"


def get_exit_reason(df, latest, portfolio):
    """回傳出場原因字串，無則回傳 None"""
    price       = latest["close"]
    entry_price = portfolio["entry_price"]

    # 5% 硬性停損（所有策略共用，最後防線）
    if price < entry_price * 0.95:
        return "跌幅超過5%強制停損"

    if STRATEGY == "A":
        if price >= latest["bb_upper"]:   return "觸及布林上軌停利"
        if price < latest["bb_lower"]:    return "跌破布林下軌停損（動態）"

    elif STRATEGY == "B":
        rsi = latest["rsi"]
        # 止盈：需達 +1% 以上才出場（Martin Luk R:R 原則，確保贏面大於手續費）
        if rsi > RSI_SELL and price > entry_price * 1.010:
            return f"RSI(9)>{RSI_SELL}超買出場（獲利≥1%確認）"
        if price < entry_price * 0.985:
            return "跌幅超過1.5%停損"

    elif STRATEGY == "C":
        ef_now,  es_now  = df["ema_f"].iloc[-2], df["ema_s"].iloc[-2]
        ef_prev, es_prev = df["ema_f"].iloc[-3], df["ema_s"].iloc[-3]
        if (ef_now < es_now) and (ef_prev >= es_prev):
            return f"EMA{EMA_FAST}/{EMA_SLOW}死叉賣出"
        # 全域5%停損已在上方處理，此為C策略額外保護（3%）
        if price < entry_price * 0.97:   return "跌幅超過3%停損（策略C）"

    elif STRATEGY == "D":
        sig_death = df["macd_sig_death"].iloc[-2]
        rsi_d = latest["rsi"]
        # 需 MACD死叉 + RSI>55 雙重確認，避免震盪假死叉截斷獲利（2026-06-22 5人會議 3/5）
        if sig_death and rsi_d > 55:     return f"MACD死叉且RSI({rsi_d:.0f})>55賣出"
        # 全域5%停損已在上方處理，此為D策略額外保護（3%）
        if price < entry_price * 0.97:   return "跌幅超過3%停損（策略D）"

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

    # P1：立刻標記此K線已佔用，防止並發重複執行
    portfolio["last_candle"] = candle_time
    save_portfolio(portfolio)

    price     = latest["close"]
    bb_upper  = latest.get("bb_upper", 0)
    bb_lower  = latest.get("bb_lower", 0)
    recent_low = df["low"].iloc[-(SL_LOOKBACK + 2):-1].min()

    print(f"  K 線：{candle_time}  |  收盤：${price:,.2f}")

    now_time = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:00Z")

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

    # ── 連敗熔斷（B策略）─────────────────────
    CIRCUIT_BREAKER = 3
    if STRATEGY == "B" and portfolio.get("consecutive_losses", 0) >= CIRCUIT_BREAKER:
        print(f"  🚫 熔斷中（連敗 {portfolio['consecutive_losses']} 次）跳過進場")
        save_portfolio(portfolio)
        sheets_post({
            "type": "monitor_log", "time": now_time, "price": str(price),
            "bb_upper": str(bb_upper), "bb_lower": str(bb_lower),
            "recent_low": str(recent_low), "cond_bb": "熔斷", "cond_macd": f"連敗{portfolio['consecutive_losses']}次",
            "signal": "熔斷暫停", "account_status": "空倉（熔斷）", "portfolio": portfolio,
        })
        return

    # ── 冷卻期檢查（B/ETH_B + D 用）────────
    in_cooldown = False
    if STRATEGY in ("B", "D"):
        last_exit = portfolio.get("last_exit_candle", "")
        if last_exit:
            try:
                last_ts    = pd.Timestamp(last_exit)
                current_ts = pd.Timestamp(candle_time)
                bars_since = int((current_ts - last_ts).total_seconds() / (15 * 60))
                if bars_since < COOLDOWN_BARS:
                    print(f"  ⏸ 冷卻期中（距上次出場 {bars_since}/{COOLDOWN_BARS} 根K棒）")
                    in_cooldown = True
            except Exception:
                pass

    # ── 無倉位：檢查進場 ────────────────────
    buy, cond1, cond2 = get_entry_signal(df, latest)
    if portfolio["position"] == 0 and buy and not in_cooldown:
        _execute_buy(df, latest, portfolio, price, bb_upper, bb_lower, recent_low,
                     now_time, "策略訊號進場", cond1, cond2)
    else:
        reason_skip = "冷卻期" if in_cooldown else f"{cond1} / {cond2}"
        print(f"  ⏸  無訊號（{reason_skip}）")

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
    notify(f"🔔 <b>進場｜{STRATEGY_LABEL.get(STRAT_KEY)}</b>\n"
           f"進場價：<b>${price:,.2f}</b>\n"
           f"模擬買入：{qty:.6f}\n"
           f"原因：{reason}")
    print(f"  🔔 買入 {qty:.6f} @ ${price:,.2f}")


def _execute_sell(df, latest, portfolio, price, bb_upper, bb_lower, reason, now_time):
    qty         = portfolio["position"]
    entry_price = portfolio["entry_price"]
    sell_value  = qty * price * (1 - COMMISSION)
    cost        = qty * entry_price * (1 + COMMISSION)
    pnl         = sell_value - cost
    pnl_pct     = pnl / cost * 100
    portfolio["capital"]        += sell_value
    portfolio["position"]        = 0.0
    portfolio["entry_price"]     = 0.0
    portfolio["entry_time"]      = ""
    portfolio["last_candle"]     = str(latest.name)
    portfolio["last_exit_candle"] = str(latest.name)  # P1：記錄出場K線供冷卻期用
    portfolio["total_trades"]   += 1
    portfolio["total_pnl"]      += pnl
    if pnl > 0:
        portfolio["wins"]             += 1
        portfolio["consecutive_losses"] = 0
    else:
        portfolio["losses"]           += 1
        portfolio["consecutive_losses"] = portfolio.get("consecutive_losses", 0) + 1
    log_trade("SELL", price, qty, pnl_pct, reason, portfolio)
    save_portfolio(portfolio)
    sheets_post({"type":"trade","time":now_time,"action":"SELL","price":str(price),
                 "qty":str(qty),"pnl_pct":f"{pnl_pct:+.2f}%","capital_after":str(portfolio["capital"]),
                 "reason":reason,"portfolio":portfolio,"bb_upper":str(bb_upper),"bb_lower":str(bb_lower)})
    icon = "🟢" if pnl > 0 else "🔴"
    notify(f"{icon} <b>出場｜{STRATEGY_LABEL.get(STRAT_KEY)}</b>\n"
           f"進場：${entry_price:,.2f} → 出場：${price:,.2f}\n"
           f"損益：<b>{pnl_pct:+.2f}%（{pnl:+.2f} USDT）</b>\n"
           f"原因：{reason}")
    print(f"  {icon} 賣出 @ ${price:,.2f}  損益：{pnl_pct:+.2f}%  原因：{reason}")


if __name__ == "__main__":
    run()

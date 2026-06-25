"""
BTC 做空策略（OKX 永續合約模擬）
進場：EMA13死叉EMA48 + 布林中軌以下 + RSI 40-65（有下跌空間）
出場：止盈-4% / 止損+2% / RSI<25極度超賣 / EMA金叉反轉
R:R = 2:1（止盈是止損的2倍）
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
STRATEGY        = "SHORT"
SYMBOL          = os.environ.get("SYMBOL", "BTC/USDT")
STRAT_KEY       = "BTC_SHORT"
ASSET           = "BTC"
TIMEFRAME       = "15m"
INITIAL_CAPITAL = 1000.0
COMMISSION      = 0.001    # OKX 永續合約 taker fee

# 做空出場條件
SHORT_TP_PCT    = 0.96     # 止盈：跌4%（price <= entry * 0.96）
SHORT_SL_PCT    = 1.02     # 止損：漲2%（price >= entry * 1.02）
RSI_COVER_LEVEL = 25       # RSI超賣覆蓋
COOLDOWN_BARS   = 4        # 出場後冷卻4根K（60分鐘）

# 技術指標參數
BB_LENGTH    = 20
BB_MULT      = 2.0
MACD_FAST    = 12
MACD_SLOW    = 26
MACD_SIGNAL_P = 9
RSI_PERIOD   = 9
EMA_FAST     = 13
EMA_SLOW     = 48
RSI_ENTRY_LOW  = 40   # 進場RSI下限（太低=超賣，不做空）
RSI_ENTRY_HIGH = 65   # 進場RSI上限（在此範圍才有下跌空間）

PORTFOLIO_FILE = "paper_portfolio_short.json"
TRADE_LOG_FILE = "trade_log_short.csv"

FORCE_TEST = os.environ.get("FORCE_TEST", "")
TG_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TG_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
GS_WEBHOOK = os.environ.get("GS_WEBHOOK", "")

EMERGENCY_STOP_FILE = "emergency_stop"

# ─────────────────────────────────────────────
# 工具函數
# ─────────────────────────────────────────────
def notify(msg):
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    try:
        url  = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        data = urllib.parse.urlencode({
            "chat_id": TG_CHAT_ID, "text": msg, "parse_mode": "HTML"
        }).encode()
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
        req    = urllib.request.Request(GS_WEBHOOK, data=data,
                                        headers={"Content-Type": "application/json"})
        opener = urllib.request.build_opener(StopRedirect())
        try:
            opener.open(req, timeout=15)
        except urllib.error.HTTPError as e:
            if e.code == 302:
                loc = e.headers.get("Location", "")
                if loc:
                    urllib.request.urlopen(loc, timeout=15)
            else:
                raise
    except Exception as e:
        print(f"  ⚠️ Google Sheets 失敗：{e}")

def load_portfolio():
    if os.path.exists(PORTFOLIO_FILE):
        p = json.load(open(PORTFOLIO_FILE))
        p.setdefault("last_exit_candle", "")
        p.setdefault("consecutive_losses", 0)
        return p
    return {
        "capital": INITIAL_CAPITAL,
        "short_position": 0.0,   # 持有的空單BTC數量
        "entry_price":    0.0,
        "entry_time":     "",
        "last_candle":    "",
        "last_exit_candle": "",
        "total_trades": 0, "wins": 0, "losses": 0,
        "total_pnl": 0.0, "consecutive_losses": 0,
    }

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
# 抓數據 + 計算指標
# ─────────────────────────────────────────────
def fetch_and_calc():
    exchange = ccxt.okx({"enableRateLimit": True})
    ohlcv = exchange.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=120)
    df = pd.DataFrame(ohlcv, columns=["ts","open","high","low","close","volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df.set_index("ts", inplace=True)

    df["bb_mid"]   = df["close"].rolling(BB_LENGTH).mean()
    df["bb_std"]   = df["close"].rolling(BB_LENGTH).std()
    df["bb_upper"] = df["bb_mid"] + BB_MULT * df["bb_std"]
    df["bb_lower"] = df["bb_mid"] - BB_MULT * df["bb_std"]

    delta       = df["close"].diff()
    gain        = delta.clip(lower=0).rolling(RSI_PERIOD).mean()
    loss        = (-delta.clip(upper=0)).rolling(RSI_PERIOD).mean()
    df["rsi"]   = 100 - (100 / (1 + gain / loss))

    df["ema_f"] = df["close"].ewm(span=EMA_FAST, adjust=False).mean()
    df["ema_s"] = df["close"].ewm(span=EMA_SLOW, adjust=False).mean()

    df.dropna(inplace=True)
    return df

# ─────────────────────────────────────────────
# 做空訊號邏輯
# ─────────────────────────────────────────────
def get_short_entry_signal(df, latest):
    """EMA13死叉EMA48 + 布林中軌以下 + RSI在40-65（有下跌空間）"""
    ef_now  = df["ema_f"].iloc[-2]
    es_now  = df["ema_s"].iloc[-2]
    ef_prev = df["ema_f"].iloc[-3]
    es_prev = df["ema_s"].iloc[-3]

    death_cross  = (ef_now < es_now) and (ef_prev >= es_prev)
    below_mid    = latest["close"] < latest["bb_mid"]
    rsi          = latest["rsi"]
    rsi_momentum = RSI_ENTRY_LOW <= rsi <= RSI_ENTRY_HIGH

    ok = death_cross and below_mid and rsi_momentum
    c1 = f"EMA13:{ef_now:.0f}/EMA48:{es_now:.0f}"
    c2 = (f"死叉{'✅' if death_cross else '❌'} "
          f"中軌下{'✅' if below_mid else '❌'} "
          f"RSI{rsi:.0f}{'✅' if rsi_momentum else '❌'}")
    return ok, c1, c2


def get_short_exit_reason(df, latest, portfolio):
    """回傳覆蓋空頭的原因，無則 None"""
    price       = latest["close"]
    entry_price = portfolio["entry_price"]
    rsi         = latest["rsi"]

    if price <= entry_price * SHORT_TP_PCT:
        return f"跌幅達4%止盈（${price:,.2f}）"

    if price >= entry_price * SHORT_SL_PCT:
        return f"上漲超過2%止損（${price:,.2f}）"

    if rsi < RSI_COVER_LEVEL:
        return f"RSI({rsi:.0f})極度超賣，覆蓋空頭防反彈"

    # EMA金叉反轉：做空方向已反
    ef_now  = df["ema_f"].iloc[-2]
    es_now  = df["ema_s"].iloc[-2]
    ef_prev = df["ema_f"].iloc[-3]
    es_prev = df["ema_s"].iloc[-3]
    if (ef_now > es_now) and (ef_prev <= es_prev):
        return "EMA13/48金叉反轉，覆蓋空頭"

    return None

# ─────────────────────────────────────────────
# 執行做空開倉
# ─────────────────────────────────────────────
def _execute_short(df, latest, portfolio, price, now_time, reason, c1, c2):
    qty = portfolio["capital"] / price / (1 + COMMISSION)
    portfolio["short_position"] = qty
    portfolio["entry_price"]    = price
    portfolio["entry_time"]     = str(latest.name)
    portfolio["capital"]        = 0.0
    portfolio["last_candle"]    = str(latest.name)
    log_trade("SHORT", price, qty, None, reason, portfolio)
    save_portfolio(portfolio)
    sheets_post({"type":"trade","time":now_time,"action":"SHORT","price":str(price),
                 "qty":str(qty),"pnl_pct":"","capital_after":"0","reason":reason,
                 "portfolio":portfolio})
    notify(f"🔻 <b>做空進場｜BTC短策略</b>\n"
           f"進場價：<b>${price:,.2f}</b>\n"
           f"空單數量：{qty:.6f} BTC\n"
           f"止損：${price*SHORT_SL_PCT:,.0f}（+2%）｜止盈：${price*SHORT_TP_PCT:,.0f}（-4%）\n"
           f"原因：{reason}")
    print(f"  🔻 做空 {qty:.6f} BTC @ ${price:,.2f}")

# ─────────────────────────────────────────────
# 執行覆蓋空頭（Cover Short）
# ─────────────────────────────────────────────
def _execute_cover(df, latest, portfolio, price, now_time, reason):
    qty         = portfolio["short_position"]
    entry_price = portfolio["entry_price"]

    # 做空損益 = (進場價 - 覆蓋價) / 進場價（跌了賺，漲了虧）
    buy_cost    = qty * price * (1 + COMMISSION)
    short_proc  = qty * entry_price * (1 - COMMISSION)
    pnl         = short_proc - buy_cost
    pnl_pct     = pnl / (qty * entry_price) * 100

    portfolio["capital"]          += (qty * entry_price + pnl)  # 還原本金+損益
    portfolio["short_position"]    = 0.0
    portfolio["entry_price"]       = 0.0
    portfolio["entry_time"]        = ""
    portfolio["last_candle"]       = str(latest.name)
    portfolio["last_exit_candle"]  = str(latest.name)
    portfolio["total_trades"]     += 1
    portfolio["total_pnl"]        += pnl
    if pnl > 0:
        portfolio["wins"]              += 1
        portfolio["consecutive_losses"] = 0
    else:
        portfolio["losses"]            += 1
        portfolio["consecutive_losses"] = portfolio.get("consecutive_losses", 0) + 1

    log_trade("COVER", price, qty, pnl_pct, reason, portfolio)
    save_portfolio(portfolio)
    sheets_post({"type":"trade","time":now_time,"action":"COVER","price":str(price),
                 "qty":str(qty),"pnl_pct":f"{pnl_pct:+.2f}%",
                 "capital_after":str(portfolio["capital"]),"reason":reason,"portfolio":portfolio})
    icon = "🟢" if pnl > 0 else "🔴"
    notify(f"{icon} <b>覆蓋空頭｜BTC短策略</b>\n"
           f"進場：${entry_price:,.2f} → 覆蓋：${price:,.2f}\n"
           f"損益：<b>{pnl_pct:+.2f}%（{pnl:+.2f} USDT）</b>\n"
           f"原因：{reason}")
    print(f"  {icon} 覆蓋 @ ${price:,.2f}  損益：{pnl_pct:+.2f}%  原因：{reason}")

# ─────────────────────────────────────────────
# 主邏輯
# ─────────────────────────────────────────────
def run():
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"\n{'='*55}")
    print(f"  📡 BTC 做空策略（EMA死叉）  |  {now_str}")
    print(f"{'='*55}")

    if os.path.exists(EMERGENCY_STOP_FILE):
        print("  🛑 緊急停止啟用，本次跳過")
        return

    portfolio   = load_portfolio()
    df          = fetch_and_calc()
    latest      = df.iloc[-2]
    candle_time = str(latest.name)

    if candle_time == portfolio["last_candle"]:
        print(f"  ⏭  本 K 線已處理過，跳過（{candle_time}）")
        return

    portfolio["last_candle"] = candle_time
    save_portfolio(portfolio)

    price    = latest["close"]
    bb_upper = latest.get("bb_upper", 0)
    bb_lower = latest.get("bb_lower", 0)
    now_time = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:00Z")

    print(f"  K 線：{candle_time}  |  收盤：${price:,.2f}")

    # ── 強制測試 ─────────────────────────────
    if FORCE_TEST == "short" and portfolio["short_position"] == 0:
        print("  🧪 強制做空")
        _execute_short(df, latest, portfolio, price, now_time, "強制測試做空", "🧪", "🧪")
        return
    if FORCE_TEST == "cover" and portfolio["short_position"] > 0:
        print("  🧪 強制覆蓋")
        _execute_cover(df, latest, portfolio, price, now_time, "強制測試覆蓋")
        return

    # ── 有空倉：檢查出場 ─────────────────────
    if portfolio["short_position"] > 0:
        reason = get_short_exit_reason(df, latest, portfolio)
        if reason:
            _execute_cover(df, latest, portfolio, price, now_time, reason)
            return
        pnl_now = (portfolio["entry_price"] - price) / portfolio["entry_price"] * 100
        print(f"  📍 持空倉 @ ${portfolio['entry_price']:,.2f}  當前浮動：{pnl_now:+.2f}%")
        save_portfolio(portfolio)
        return

    # ── 冷卻期 ───────────────────────────────
    in_cooldown = False
    last_exit   = portfolio.get("last_exit_candle", "")
    if last_exit:
        try:
            bars_since = int((pd.Timestamp(candle_time) - pd.Timestamp(last_exit)).total_seconds() / 900)
            if bars_since < COOLDOWN_BARS:
                print(f"  ⏸ 冷卻期中（{bars_since}/{COOLDOWN_BARS} 根）")
                in_cooldown = True
        except Exception:
            pass

    # ── 無倉位：檢查進場 ─────────────────────
    signal, c1, c2 = get_short_entry_signal(df, latest)
    if signal and not in_cooldown:
        _execute_short(df, latest, portfolio, price, now_time, "EMA死叉做空", c1, c2)
    else:
        reason_skip = "冷卻期" if in_cooldown else f"{c1} / {c2}"
        print(f"  ⏸  無訊號（{reason_skip}）")
        save_portfolio(portfolio)

    sheets_post({
        "type": "monitor_log", "time": now_time, "price": str(price),
        "bb_upper": str(bb_upper), "bb_lower": str(bb_lower),
        "recent_low": "", "cond_bb": c1, "cond_macd": c2,
        "signal":    "做空" if portfolio["short_position"] > 0 else "無訊號",
        "account_status": "空倉持倉" if portfolio["short_position"] > 0 else "空",
        "portfolio": portfolio,
    })


if __name__ == "__main__":
    run()

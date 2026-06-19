"""
策略 E：BTC 20日線波段策略（無腦戰法改編）
- 日線執行，每天收盤後（00:10 UTC）運行一次
- 進場：MACD零軸上 + 回撤20日SMA ±3% + 放量紅K
- 出場：+30% 賣1/3 → +50% 再賣一半 → 收盤跌破20日SMA 全出
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
STRATEGY        = "BTC_E"
SYMBOL          = "BTC/USDT"
TIMEFRAME       = "1d"
INITIAL_CAPITAL = 1000.0
COMMISSION      = 0.001

PORTFOLIO_FILE  = "paper_portfolio_btc_e.json"
TRADE_LOG_FILE  = "trade_log_btc_e.csv"

# 20日線波段參數
SMA_PERIOD      = 20
MACD_FAST       = 12
MACD_SLOW       = 26
MACD_SIGNAL_P   = 9
VOL_MA_PERIOD   = 20
PULLBACK_RANGE  = 0.03   # 距離20日SMA ±3% 視為「回撤區」

# 三段出場目標
TARGET_1        = 1.30   # +30% 出 1/3
TARGET_2        = 1.50   # +50% 出半數剩餘

FORCE_TEST = os.environ.get("FORCE_TEST", "")
TG_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TG_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
GS_WEBHOOK = os.environ.get("GS_WEBHOOK", "https://script.google.com/macros/s/AKfycbywcYNXYwDN6Z70F0-1nxVj6f3nzqyyoiugO_Mkiy5LPjXbFb5RP126d79VgqjnWlwJ/exec")

LABEL = "BTC 策略E：20日線波段"

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
    payload["strategy"] = STRATEGY
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
        except Exception:
            pass
    except Exception as e:
        print(f"  ⚠️ Google Sheets 失敗：{e}")

def load_portfolio():
    if os.path.exists(PORTFOLIO_FILE):
        p = json.load(open(PORTFOLIO_FILE))
        p.setdefault("stage", 0)         # 0=未出場 1=已出1/3 2=已出再一半
        p.setdefault("initial_qty", 0.0) # 原始買入數量
        p.setdefault("last_candle", "")
        return p
    return {
        "capital": INITIAL_CAPITAL,
        "position": 0.0,
        "entry_price": 0.0,
        "entry_time": "",
        "last_candle": "",
        "total_trades": 0,
        "wins": 0,
        "losses": 0,
        "total_pnl": 0.0,
        "stage": 0,
        "initial_qty": 0.0,
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
    # 取 80 根日線（MACD 需要 35+ 根熱身，SMA 需要 20 根）
    ohlcv = exchange.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=80)
    df = pd.DataFrame(ohlcv, columns=["ts","open","high","low","close","volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df.set_index("ts", inplace=True)

    # 20日SMA
    df["sma20"] = df["close"].rolling(SMA_PERIOD).mean()

    # 成交量SMA
    df["vol_ma"] = df["volume"].rolling(VOL_MA_PERIOD).mean()

    # MACD（日線）
    ema_fast       = df["close"].ewm(span=MACD_FAST, adjust=False).mean()
    ema_slow       = df["close"].ewm(span=MACD_SLOW, adjust=False).mean()
    df["macd"]     = ema_fast - ema_slow
    df["macd_sig"] = df["macd"].ewm(span=MACD_SIGNAL_P, adjust=False).mean()

    df.dropna(inplace=True)
    return df

# ─────────────────────────────────────────────
# 進出場邏輯
# ─────────────────────────────────────────────
def check_entry(df, latest):
    """
    進場條件：
    1. MACD > 0（趨勢確認，對應原策略週線MACD零軸上）
    2. 價格在20日SMA的 [1-PULLBACK_RANGE, 1+PULLBACK_RANGE] 區間（回撤到均線附近）
    3. 當日收紅（close > open）
    4. 成交量 > 20日均量 × 1.1
    """
    price    = latest["close"]
    sma20    = latest["sma20"]
    macd     = latest["macd"]
    vol      = latest["volume"]
    vol_ma   = latest["vol_ma"]
    bullish  = latest["close"] > latest["open"]

    near_sma = sma20 * (1 - PULLBACK_RANGE) <= price <= sma20 * (1 + PULLBACK_RANGE)
    macd_up  = macd > 0
    vol_up   = vol > vol_ma * 1.1

    reasons = []
    if macd_up:  reasons.append(f"MACD✅{macd:.0f}")
    else:         reasons.append(f"MACD❌{macd:.0f}")
    if near_sma: reasons.append(f"近SMA20✅({price:.0f}/{sma20:.0f})")
    else:         reasons.append(f"距SMA20❌({abs(price/sma20-1)*100:.1f}%)")
    if bullish:  reasons.append("紅K✅")
    else:         reasons.append("黑K❌")
    if vol_up:   reasons.append("放量✅")
    else:         reasons.append("縮量❌")

    ok = macd_up and near_sma and bullish and vol_up
    return ok, " ".join(reasons)

def check_exit(latest, portfolio):
    """
    三段出場：
    Stage 0 → 1：價格達 entry × 1.30，賣 1/3 原始倉位
    Stage 1 → 2：價格達 entry × 1.50，再賣一半當前倉位
    Stage any：收盤跌破20日SMA → 全出（止損兼止盈）
    """
    price       = latest["close"]
    sma20       = latest["sma20"]
    entry_price = portfolio["entry_price"]
    stage       = portfolio["stage"]
    initial_qty = portfolio["initial_qty"]
    current_qty = portfolio["position"]

    # 跌破20日SMA → 全出（最高優先）
    if price < sma20:
        return "FULL", current_qty, f"收盤跌破20日SMA（{price:.0f}<{sma20:.0f}）止損出場"

    # +30% 第一段出場
    if stage == 0 and price >= entry_price * TARGET_1:
        sell_qty = initial_qty / 3
        sell_qty = min(sell_qty, current_qty)
        return "PARTIAL_1", sell_qty, f"+30%達標（{price:.0f}），出1/3倉位"

    # +50% 第二段出場
    if stage == 1 and price >= entry_price * TARGET_2:
        sell_qty = current_qty / 2
        return "PARTIAL_2", sell_qty, f"+50%達標（{price:.0f}），再出一半"

    return None, 0, ""

# ─────────────────────────────────────────────
# 執行買入
# ─────────────────────────────────────────────
def execute_buy(latest, portfolio, price, sma20, reason, now_time):
    qty = portfolio["capital"] / price / (1 + COMMISSION)
    portfolio["position"]    = qty
    portfolio["initial_qty"] = qty
    portfolio["entry_price"] = price
    portfolio["entry_time"]  = str(latest.name)
    portfolio["capital"]     = 0.0
    portfolio["stage"]       = 0
    portfolio["last_candle"] = str(latest.name)
    log_trade("BUY", price, qty, None, reason, portfolio)
    save_portfolio(portfolio)
    sheets_post({"type":"trade","time":now_time,"action":"BUY","price":str(price),
                 "qty":str(qty),"pnl_pct":"","capital_after":"0","reason":reason,
                 "portfolio":portfolio,"bb_upper":str(sma20),"bb_lower":str(sma20)})
    notify(f"🔔 <b>進場｜{LABEL}</b>\n"
           f"進場價：<b>${price:,.0f}</b>\n"
           f"數量：{qty:.6f} BTC\n"
           f"20日SMA：${sma20:,.0f}\n"
           f"原因：{reason}")
    print(f"  🔔 買入 {qty:.6f} BTC @ ${price:,.0f}  SMA20=${sma20:,.0f}")

# ─────────────────────────────────────────────
# 執行賣出（支援部分出場）
# ─────────────────────────────────────────────
def execute_sell(portfolio, price, sell_qty, reason, now_time, exit_type):
    entry_price = portfolio["entry_price"]
    sell_value  = sell_qty * price * (1 - COMMISSION)
    cost        = sell_qty * entry_price * (1 + COMMISSION)
    pnl         = sell_value - cost
    pnl_pct     = pnl / cost * 100

    portfolio["capital"]  += sell_value
    portfolio["position"] -= sell_qty
    portfolio["position"]  = max(0.0, portfolio["position"])

    if exit_type == "PARTIAL_1":
        portfolio["stage"] = 1
        is_final = False
    elif exit_type == "PARTIAL_2":
        portfolio["stage"] = 2
        is_final = False
    else:
        # FULL 出場
        portfolio["position"]    = 0.0
        portfolio["entry_price"] = 0.0
        portfolio["entry_time"]  = ""
        portfolio["stage"]       = 0
        portfolio["initial_qty"] = 0.0
        portfolio["total_trades"] += 1
        portfolio["total_pnl"]    += pnl
        if pnl > 0: portfolio["wins"]   += 1
        else:        portfolio["losses"] += 1
        is_final = True

    portfolio["last_candle"] = str(datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    log_trade("SELL", price, sell_qty, pnl_pct, reason, portfolio)
    save_portfolio(portfolio)
    sheets_post({"type":"trade","time":now_time,"action":"SELL","price":str(price),
                 "qty":str(sell_qty),"pnl_pct":f"{pnl_pct:+.2f}%",
                 "capital_after":str(portfolio["capital"]),"reason":reason,
                 "portfolio":portfolio,"bb_upper":"0","bb_lower":"0"})
    icon = "🟢" if pnl > 0 else "🔴"
    stage_tag = {"PARTIAL_1":"[1/3]","PARTIAL_2":"[1/2]","FULL":"[全出]"}.get(exit_type,"")
    notify(f"{icon} <b>出場{stage_tag}｜{LABEL}</b>\n"
           f"進場：${entry_price:,.0f} → 出場：${price:,.0f}\n"
           f"損益：<b>{pnl_pct:+.2f}%（{pnl:+.2f} USDT）</b>\n"
           f"剩餘：{portfolio['position']:.6f} BTC\n"
           f"原因：{reason}")
    print(f"  {icon} 賣出{stage_tag} {sell_qty:.6f} BTC @ ${price:,.0f}  損益：{pnl_pct:+.2f}%")
    if is_final:
        print(f"  📊 本次完整交易結束")

# ─────────────────────────────────────────────
# 主邏輯
# ─────────────────────────────────────────────
def run():
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"\n{'='*55}")
    print(f"  📡 {LABEL}  |  {now_str}")
    print(f"{'='*55}")

    portfolio = load_portfolio()
    df        = fetch_and_calc()
    latest    = df.iloc[-2]   # 已收盤的最後一根日K
    candle_time = str(latest.name)[:10]  # 只取日期

    if candle_time == portfolio.get("last_candle", "")[:10]:
        print(f"  ⏭  今日K線已處理過，跳過（{candle_time}）")
        return

    price = latest["close"]
    sma20 = latest["sma20"]
    macd  = latest["macd"]
    now_time = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:00Z")

    print(f"  日K：{candle_time}  收盤：${price:,.0f}  SMA20：${sma20:,.0f}  MACD：{macd:.0f}")

    # ── 強制測試 ──────────────────────────────
    if FORCE_TEST == "buy" and portfolio["position"] == 0:
        print("  🧪 強制買入")
        execute_buy(latest, portfolio, price, sma20, "強制測試買入", now_time)
        return
    if FORCE_TEST == "sell" and portfolio["position"] > 0:
        print("  🧪 強制賣出")
        execute_sell(portfolio, price, portfolio["position"], "強制測試賣出", now_time, "FULL")
        return

    # ── 有倉位：檢查出場 ──────────────────────
    if portfolio["position"] > 0:
        exit_type, sell_qty, exit_reason = check_exit(latest, portfolio)
        if exit_type:
            execute_sell(portfolio, price, sell_qty, exit_reason, now_time, exit_type)
            # 部分出場後繼續跑（不 return），讓 monitor_log 記錄當前狀態
            if exit_type != "FULL" and portfolio["position"] > 0:
                print(f"  📌 剩餘倉位：{portfolio['position']:.6f} BTC（Stage {portfolio['stage']}）")

    # ── 無倉位：檢查進場 ──────────────────────
    if portfolio["position"] == 0:
        ok, conditions = check_entry(df, latest)
        if ok:
            execute_buy(latest, portfolio, price, sma20, "策略E進場", now_time)
        else:
            print(f"  ⏸  無進場訊號：{conditions}")

    portfolio["last_candle"] = candle_time
    save_portfolio(portfolio)

    # 發送監測日誌
    total_value = portfolio["capital"] + portfolio["position"] * price
    pct = (total_value - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100
    sheets_post({
        "type": "monitor_log",
        "time": now_time, "price": str(price),
        "bb_upper": str(sma20), "bb_lower": str(sma20),
        "recent_low": str(sma20),
        "cond_bb": f"SMA20={sma20:.0f}", "cond_macd": f"MACD={macd:.0f}",
        "signal": "持倉" if portfolio["position"] > 0 else "空倉",
        "account_status": f"Stage{portfolio['stage']}" if portfolio["position"] > 0 else "空倉",
        "portfolio": portfolio,
    })
    print(f"  💼 總資產估值：${total_value:,.2f}（{pct:+.2f}%）")


if __name__ == "__main__":
    run()

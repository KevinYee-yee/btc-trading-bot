"""
策略 A 即時紙上交易監測器
每次執行：抓最新 BTCUSDT 4H 數據 → 檢查訊號 → 更新虛擬帳戶
由 GitHub Actions 每 4 小時自動觸發
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
SYMBOL          = "BTC/USDT"
TIMEFRAME       = "30m"   # 測試模式：改回正式請換成 "4h"
INITIAL_CAPITAL = 1000.0      # 模擬起始資金（USDT）
COMMISSION      = 0.001       # 手續費 0.1%
SL_LOOKBACK     = 3           # 停損回看 K 線數
MACD_WINDOW     = 3           # MACD 黃金交叉容許窗口（根）
BB_LENGTH       = 20
BB_MULT         = 2.0
MACD_FAST       = 12
MACD_SLOW       = 26
MACD_SIGNAL     = 9

PORTFOLIO_FILE  = "paper_portfolio.json"
TRADE_LOG_FILE  = "trade_log.csv"

# Telegram（從環境變數讀取，由 GitHub Secrets 注入）
TG_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TG_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# Google Sheets Web App URL（部署後填入）
GS_WEBHOOK = os.environ.get("GS_WEBHOOK", "")

# ─────────────────────────────────────────────
# 虛擬帳戶：讀取 / 初始化
# ─────────────────────────────────────────────
def notify(msg):
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    try:
        url  = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        data = urllib.parse.urlencode({"chat_id": TG_CHAT_ID, "text": msg, "parse_mode": "HTML"}).encode()
        urllib.request.urlopen(url, data, timeout=10)
    except Exception as e:
        print(f"  ⚠️ Telegram 通知失敗：{e}")

def sheets_post(payload):
    if not GS_WEBHOOK:
        return
    try:
        # Google Apps Script 會 302 redirect，需要先 POST 拿到 Location，再 GET echo URL
        class StopRedirect(urllib.request.HTTPRedirectHandler):
            def http_error_302(self, req, fp, code, msg, headers):
                raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)

        data = json.dumps(payload).encode("utf-8")
        req  = urllib.request.Request(GS_WEBHOOK, data=data,
                                      headers={"Content-Type": "application/json"})
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
        print(f"  ⚠️ Google Sheets 更新失敗：{e}")

def load_portfolio():
    if os.path.exists(PORTFOLIO_FILE):
        with open(PORTFOLIO_FILE) as f:
            return json.load(f)
    return {
        "capital":      INITIAL_CAPITAL,
        "position":     0.0,        # 持有 BTC 數量
        "entry_price":  0.0,
        "entry_time":   "",
        "last_candle":  "",         # 上次處理的 K 線時間（防重複）
        "total_trades": 0,
        "wins":         0,
        "losses":       0,
        "total_pnl":    0.0,
    }

def save_portfolio(p):
    with open(PORTFOLIO_FILE, "w") as f:
        json.dump(p, f, indent=2, ensure_ascii=False)

# ─────────────────────────────────────────────
# 寫入交易紀錄
# ─────────────────────────────────────────────
def log_trade(action, price, qty, pnl_pct, reason, portfolio):
    header = not os.path.exists(TRADE_LOG_FILE)
    with open(TRADE_LOG_FILE, "a", newline="") as f:
        w = csv.writer(f)
        if header:
            w.writerow(["time", "action", "price", "qty_btc", "pnl_pct", "capital_after", "reason"])
        w.writerow([
            datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
            action,
            f"{price:.2f}",
            f"{qty:.6f}",
            f"{pnl_pct:+.2f}%" if pnl_pct is not None else "",
            f"{portfolio['capital']:.2f}",
            reason,
        ])

# ─────────────────────────────────────────────
# 抓數據 + 計算指標
# ─────────────────────────────────────────────
def fetch_and_calc():
    # 使用 OKX：公開 API 不受地區限制（Binance 封鎖美國 IP）
    exchange = ccxt.okx({"enableRateLimit": True})
    ohlcv = exchange.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=100)
    df = pd.DataFrame(ohlcv, columns=["ts", "open", "high", "low", "close", "volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df.set_index("ts", inplace=True)

    # 布林通道
    df["bb_mid"]   = df["close"].rolling(BB_LENGTH).mean()
    df["bb_std"]   = df["close"].rolling(BB_LENGTH).std()
    df["bb_upper"] = df["bb_mid"] + BB_MULT * df["bb_std"]
    df["bb_lower"] = df["bb_mid"] - BB_MULT * df["bb_std"]

    # MACD
    ema_fast       = df["close"].ewm(span=MACD_FAST, adjust=False).mean()
    ema_slow       = df["close"].ewm(span=MACD_SLOW, adjust=False).mean()
    df["macd"]     = ema_fast - ema_slow
    df["macd_sig"] = df["macd"].ewm(span=MACD_SIGNAL, adjust=False).mean()
    df["macd_cross"] = (df["macd"] > df["macd_sig"]) & (df["macd"].shift(1) <= df["macd_sig"].shift(1))

    df.dropna(inplace=True)
    return df

# ─────────────────────────────────────────────
# 主邏輯
# ─────────────────────────────────────────────
def run():
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"\n{'='*55}")
    print(f"  📡 策略 A 紙上交易監測  |  {now_str}")
    print(f"{'='*55}")

    portfolio = load_portfolio()
    df        = fetch_and_calc()

    # 取最新已收盤的 K 線（最後一根可能還在走）
    latest       = df.iloc[-2]
    candle_time  = str(latest.name)

    # 防止同一根 K 線重複執行
    if candle_time == portfolio["last_candle"]:
        print(f"  ⏭  本 K 線已處理過，跳過（{candle_time}）")
        print_status(portfolio, latest)
        return

    price      = latest["close"]
    bb_upper   = latest["bb_upper"]
    bb_lower   = latest["bb_lower"]
    near_lower = price <= bb_lower * 1.005
    # 最近 MACD_WINDOW 根內有黃金交叉
    recent_cross = df["macd_cross"].iloc[-(MACD_WINDOW + 2):-1].any()
    # 停損：最近 SL_LOOKBACK 根最低點
    recent_low   = df["low"].iloc[-(SL_LOOKBACK + 2):-1].min()

    print(f"  K 線時間：{candle_time}")
    print(f"  收盤價：  ${price:,.2f}")
    print(f"  布林上軌：${bb_upper:,.2f}  下軌：${bb_lower:,.2f}")
    print(f"  近期低點（停損線）：${recent_low:,.2f}")

    action_taken = False

    # ── 有倉位：檢查出場 ────────────────────
    if portfolio["position"] > 0:
        qty         = portfolio["position"]
        entry_price = portfolio["entry_price"]
        exit_reason = None

        if price >= bb_upper:
            exit_reason = "觸及布林上軌停利"
        elif price < recent_low:
            exit_reason = "跌破近期低點停損"

        if exit_reason:
            sell_value  = qty * price * (1 - COMMISSION)
            cost        = qty * entry_price * (1 + COMMISSION)
            pnl         = sell_value - cost
            pnl_pct     = pnl / cost * 100
            portfolio["capital"] += sell_value
            portfolio["position"]    = 0.0
            portfolio["entry_price"] = 0.0
            portfolio["entry_time"]  = ""
            portfolio["total_trades"] += 1
            portfolio["total_pnl"]    += pnl
            if pnl > 0:
                portfolio["wins"] += 1
                icon = "🟢"
            else:
                portfolio["losses"] += 1
                icon = "🔴"
            log_trade("SELL", price, qty, pnl_pct, exit_reason, portfolio)
            sheets_post({
                "type":          "trade",
                "time":          datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
                "action":        "SELL",
                "price":         str(price),
                "qty":           str(qty),
                "pnl_pct":       f"{pnl_pct:+.2f}%",
                "capital_after": str(portfolio["capital"]),
                "reason":        exit_reason,
                "portfolio":     portfolio,
                "bb_upper":      str(bb_upper),
                "bb_lower":      str(bb_lower),
            })
            print(f"\n  {icon} 出場！{exit_reason}")
            print(f"     進場：${entry_price:,.2f}  →  出場：${price:,.2f}")
            print(f"     損益：{pnl_pct:+.2f}%  ({pnl:+.2f} USDT)")
            notify(
                f"{icon} <b>出場訊號｜BTC 策略A</b>\n"
                f"原因：{exit_reason}\n"
                f"進場：${entry_price:,.2f} → 出場：${price:,.2f}\n"
                f"損益：<b>{pnl_pct:+.2f}%（{pnl:+.2f} USDT）</b>\n"
                f"帳戶餘額：${portfolio['capital']:,.2f} USDT"
            )
            action_taken = True

    # ── 無倉位：檢查進場 ────────────────────
    if portfolio["position"] == 0:
        if near_lower and recent_cross:
            qty = portfolio["capital"] / price / (1 + COMMISSION)
            portfolio["position"]    = qty
            portfolio["entry_price"] = price
            portfolio["entry_time"]  = candle_time
            portfolio["capital"]     = 0.0
            log_trade("BUY", price, qty, None, "BB下軌+MACD黃金交叉", portfolio)
            sheets_post({
                "type":          "trade",
                "time":          datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
                "action":        "BUY",
                "price":         str(price),
                "qty":           str(qty),
                "pnl_pct":       "",
                "capital_after": "0",
                "reason":        "BB下軌+MACD黃金交叉",
                "portfolio":     portfolio,
                "bb_upper":      str(bb_upper),
                "bb_lower":      str(bb_lower),
            })
            print(f"\n  🔔 進場！布林下軌 + MACD 黃金交叉")
            print(f"     買入 {qty:.6f} BTC @ ${price:,.2f}")
            notify(
                f"🔔 <b>進場訊號｜BTC 策略A</b>\n"
                f"條件：布林下軌 + MACD 黃金交叉\n"
                f"進場價：<b>${price:,.2f}</b>\n"
                f"模擬買入：{qty:.6f} BTC\n"
                f"停損線：${recent_low:,.2f}｜停利：布林上軌 ${bb_upper:,.2f}"
            )
            action_taken = True
        else:
            cond1 = "✅" if near_lower   else "❌"
            cond2 = "✅" if recent_cross else "❌"
            print(f"\n  ⏸  無訊號（布林下軌：{cond1}  MACD交叉：{cond2}）")

    portfolio["last_candle"] = candle_time
    save_portfolio(portfolio)
    print_status(portfolio, latest)

    # ── Google Sheets 監測日誌 ───────────────
    sheets_post({
        "type":           "monitor_log",
        "time":           datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
        "price":          str(price),
        "bb_upper":       str(bb_upper),
        "bb_lower":       str(bb_lower),
        "recent_low":     str(recent_low),
        "cond_bb":        "✅" if near_lower   else "❌",
        "cond_macd":      "✅" if recent_cross else "❌",
        "signal":         "進場" if (near_lower and recent_cross) else "無訊號",
        "account_status": f"持倉" if portfolio["position"] > 0 else "空倉",
        "portfolio":      portfolio,
    })

def print_status(portfolio, latest):
    price = latest["close"]
    cap   = portfolio["capital"]
    pos   = portfolio["position"]

    if pos > 0:
        market_value = pos * price
        unrealized   = market_value - pos * portfolio["entry_price"] * (1 + COMMISSION)
        total_value  = market_value
        print(f"\n  💼 帳戶狀態")
        print(f"     持倉：{pos:.6f} BTC（市值 ${market_value:,.2f}）")
        print(f"     未實現損益：{unrealized:+.2f} USDT")
        print(f"     進場時間：{portfolio['entry_time']}")
    else:
        total_value = cap
        print(f"\n  💼 帳戶狀態")
        print(f"     現金：${cap:,.2f} USDT（空倉中）")

    total_return = (total_value - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100
    wins   = portfolio["wins"]
    losses = portfolio["losses"]
    total  = portfolio["total_trades"]
    wr     = wins / total * 100 if total > 0 else 0

    print(f"     累計報酬：{total_return:+.2f}%")
    print(f"     交易記錄：{total} 筆（{wins}勝 {losses}敗，勝率 {wr:.0f}%）")
    print(f"     累計已實現損益：{portfolio['total_pnl']:+.2f} USDT")
    print()

if __name__ == "__main__":
    run()

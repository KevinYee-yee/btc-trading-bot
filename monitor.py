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
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta

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
RSI_BUY         = float(os.environ.get("RSI_BUY", "40"))
RSI_SELL        = float(os.environ.get("RSI_SELL", "62"))
MIN_PROFIT_PCT  = float(os.environ.get("MIN_PROFIT_PCT", "0"))  # B策略RSI出場最低獲利門檻（A/B測試用）
EMA_FAST        = 13
EMA_SLOW        = 48
COOLDOWN_BARS   = 4   # P1：出場後冷卻根數（B/ETH_B 用）
EMA_TREND_BARS  = 48  # 策略B趨勢過濾 lookback（根）：原20根(5h)→48根(12h)，2026-06-21 5人會議

# 策略唯一鍵（含標的前綴）：BTC 沿用裸鍵，其餘幣種 = 幣名_策略
ASSET = SYMBOL.split("/")[0] if "/" in SYMBOL else "BTC"
STRAT_KEY = STRATEGY if ASSET == "BTC" else f"{ASSET}_{STRATEGY}"

# A/B 測試變體：VARIANT=V2 → 獨立的 STRAT_KEY / portfolio / 交易紀錄
VARIANT = os.environ.get("VARIANT", "")
if VARIANT:
    STRAT_KEY = f"{STRAT_KEY}_{VARIANT}"

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
if VARIANT:
    STRATEGY_LABEL.setdefault(STRAT_KEY, f"{ASSET} 策略{STRATEGY}·{VARIANT}變體")

FORCE_TEST = os.environ.get("FORCE_TEST", "")
TG_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TG_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
GS_WEBHOOK = os.environ.get("GS_WEBHOOK", "https://script.google.com/macros/s/AKfycbywcYNXYwDN6Z70F0-1nxVj6f3nzqyyoiugO_Mkiy5LPjXbFb5RP126d79VgqjnWlwJ/exec")

# ─────────────────────────────────────────────
# 實盤設定（預設關閉，LIVE_TRADE=true 才啟動）
# ─────────────────────────────────────────────
LIVE_TRADE     = os.environ.get("LIVE_TRADE", "false").lower() == "true"
LIVE_CAPITAL   = float(os.environ.get("LIVE_CAPITAL", "100"))
OKX_API_KEY    = os.environ.get("OKX_API_KEY", "")
OKX_SECRET     = os.environ.get("OKX_SECRET", "")
OKX_PASSPHRASE = os.environ.get("OKX_PASSPHRASE", "")

# 實盤用獨立檔案，與模擬完全隔離，避免 GitHub Actions 覆蓋真實倉位
_prefix        = "live" if LIVE_TRADE else "paper"
PORTFOLIO_FILE = f"{_prefix}_portfolio.json" if STRAT_KEY == "A" else f"{_prefix}_portfolio_{STRAT_KEY.lower()}.json"
TRADE_LOG_FILE = f"{_prefix}_trade_log.csv"  if STRAT_KEY == "A" else f"{_prefix}_trade_log_{STRAT_KEY.lower()}.csv"

live_exchange = None
if LIVE_TRADE:
    if not OKX_API_KEY:
        raise RuntimeError("LIVE_TRADE=true 但 OKX_API_KEY 未設定，請加入 GitHub Secrets")
    live_exchange = ccxt.okx({
        "apiKey":          OKX_API_KEY,
        "secret":          OKX_SECRET,
        "password":        OKX_PASSPHRASE,
        "enableRateLimit": True,
    })

# ─────────────────────────────────────────────
# 緊急停止 & 實盤輔助函數（缺口3/4/5/6/7）
# ─────────────────────────────────────────────
EMERGENCY_STOP_FILE = "emergency_stop"

REGIME_GATE = os.environ.get("REGIME_GATE", "true").lower() == "true"  # 實盤情境閘門開關

def _fetch_daily(symbol, limit=80):
    ex = ccxt.okx({"enableRateLimit": True})
    o = ex.fetch_ohlcv(symbol, "1d", limit=limit)
    return pd.DataFrame(o, columns=["ts", "open", "high", "low", "close", "vol"])

def _daily_adx(d, n=14):
    h, l, c = d["high"], d["low"], d["close"]
    up, dn = h.diff(), -l.diff()
    pdm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=d.index)
    mdm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=d.index)
    tr  = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1/n, adjust=False).mean()
    pdi = 100 * pdm.ewm(alpha=1/n, adjust=False).mean() / atr
    mdi = 100 * mdm.ewm(alpha=1/n, adjust=False).mean() / atr
    dx  = 100 * (pdi - mdi).abs() / (pdi + mdi)
    return dx.ewm(alpha=1/n, adjust=False).mean().iloc[-1]

def _regime_gate():
    """情境閘門（外部審查團 P0）：大盤或標的日線轉弱、死盤整時禁止新倉。
    只擋進場，不影響出場。資料取失敗時放行（fail-open），避免誤停交易。"""
    try:
        d = _fetch_daily(SYMBOL)
        ema50 = d["close"].ewm(span=50, adjust=False).mean().iloc[-1]
        c_now, c_prev = d["close"].iloc[-1], d["close"].iloc[-2]
        if c_now < ema50:
            return False, f"{ASSET}日線 {c_now:.2f} < EMA50 {ema50:.2f}（標的轉弱）"
        # 緩衝帶（2026-07-05週會風控決議）：貼線+動能向下=視同關閉，堵「日線未破但已在崩」空窗、防開關震盪
        if c_now < ema50 * 1.02 and c_now < c_prev:
            return False, f"{ASSET}日線 {c_now:.2f} 距EMA50不足2%且動能向下（緩衝帶）"
        adx = _daily_adx(d)
        if adx < 18:
            return False, f"日線ADX {adx:.0f} < 18（死盤整，勝率窪地）"
        # G2閘門（2026-07-05 walk-forward判定）：預設不看BTC，標的自己的天氣自己決定
        # 需要恢復BTC條件時設 GATE_BTC=true
        if ASSET != "BTC" and os.environ.get("GATE_BTC", "false").lower() == "true":
            b = _fetch_daily("BTC/USDT")
            bema = b["close"].ewm(span=50, adjust=False).mean().iloc[-1]
            if b["close"].iloc[-1] < bema:
                return False, f"BTC日線 {b['close'].iloc[-1]:.0f} < EMA50 {bema:.0f}（大盤偏空）"
        return True, f"日線ADX {adx:.0f}，多頭閘門開"
    except Exception as e:
        print(f"  ⚠️ 情境閘門資料取得失敗，本次放行：{e}")
        return True, "gate-error"

def _risk_check_after_sell(portfolio, pnl_pct):
    """實盤風控三件套（外部審查團 P0）：
    ①峰值回撤≤-10% → 停機7天冷靜期 ②滾動10筆淨損≤-4% → 熔斷48h ③單日≤-3% → 當日停單"""
    if not LIVE_TRADE:
        return
    now = datetime.now(timezone.utc)
    rp = portfolio.get("recent_pnls", [])
    rp.append(round(pnl_pct, 3))
    portfolio["recent_pnls"] = rp[-10:]
    today = now.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
    if portfolio.get("day_date") != today:
        portfolio["day_date"], portfolio["day_pnl"] = today, 0.0
    portfolio["day_pnl"] = round(portfolio.get("day_pnl", 0.0) + pnl_pct, 3)
    peak = max(portfolio.get("peak_capital", INITIAL_CAPITAL), portfolio["capital"])
    portfolio["peak_capital"] = peak
    dd = (portfolio["capital"] - peak) / peak * 100

    halt, why = None, ""
    if dd <= -10:
        halt, why = now + timedelta(days=7), f"帳戶自峰值回撤 {dd:.1f}%（≤-10%）→ 全面停機 7 天冷靜期"
    elif len(portfolio["recent_pnls"]) >= 5 and sum(portfolio["recent_pnls"]) <= -4:
        halt, why = now + timedelta(hours=48), f"滾動{len(portfolio['recent_pnls'])}筆淨損 {sum(portfolio['recent_pnls']):.1f}%（≤-4%）→ 熔斷 48 小時"
    elif portfolio["day_pnl"] <= -3:
        halt, why = now + timedelta(hours=12), f"單日虧損 {portfolio['day_pnl']:.1f}%（≤-3%）→ 今日停單"
    if halt:
        portfolio["halt_until"] = halt.isoformat()
        notify(f"🛑 【實盤風控】{STRAT_KEY} {why}\n恢復：{halt.strftime('%m/%d %H:%M')} UTC（台灣+8h）")

def check_emergency_stop():
    """缺口7：emergency_stop 檔案存在時跳過所有操作"""
    if os.path.exists(EMERGENCY_STOP_FILE):
        msg = f"🛑 [{STRAT_KEY}] 緊急停止啟用（emergency_stop 存在），本次跳過"
        print(f"  {msg}")
        return True
    return False

def _live_check_balance():
    """缺口4：進場前確認 OKX USDT 餘額 >= LIVE_CAPITAL"""
    try:
        bal = live_exchange.fetch_balance()
        usdt_free = float(bal.get("USDT", {}).get("free", 0))
        if usdt_free < LIVE_CAPITAL * 0.95:
            notify(f"⚠️ [{STRAT_KEY}] USDT 餘額不足：帳戶 {usdt_free:.2f} < 需要 {LIVE_CAPITAL:.2f}")
            return False
        return True
    except Exception as e:
        notify(f"🚨 [{STRAT_KEY}] 餘額查詢失敗：{e}")
        return False

def _live_get_position_qty():
    """缺口6：從 OKX 讀取當前標的真實持倉量
    用 total 而非 free：掛止損單期間幣會被凍結，free=0 曾導致誤判「止損已觸發」記假出場"""
    try:
        bal = live_exchange.fetch_balance()
        a = bal.get(ASSET, {})
        return float(a.get("total") or 0) or (float(a.get("free") or 0) + float(a.get("used") or 0))
    except Exception as e:
        notify(f"🚨 [{STRAT_KEY}] 持倉查詢失敗：{e}")
        return None

def _live_place_order(side, qty):
    """缺口1/3：送出市價單，失敗通知並回傳 None"""
    try:
        if side == "buy":
            return live_exchange.create_market_buy_order(SYMBOL, qty)
        else:
            return live_exchange.create_market_sell_order(SYMBOL, qty)
    except Exception as e:
        notify(f"🚨 [{STRAT_KEY}] 下單失敗（{side} {qty:.6f} {SYMBOL}）：{e}")
        return None

def _live_maker_buy(qty, ref_price):
    """post-only 限價買入（掛 best bid，省taker費與價差）
    未成交則追最新買一價重掛，最多3輪（約45秒），仍未成交才放棄本次進場"""
    try:
        for attempt in range(3):
            bid = float(live_exchange.fetch_ticker(SYMBOL).get("bid") or 0)
            if bid <= 0:
                bid = ref_price
            order = live_exchange.create_order(SYMBOL, "limit", "buy", qty, bid, {"postOnly": True})
            for _ in range(7):
                time.sleep(2)
                o = live_exchange.fetch_order(order["id"], SYMBOL)
                if o.get("status") == "closed":
                    return o
            try:
                live_exchange.cancel_order(order["id"], SYMBOL)
            except Exception:
                pass
            o = live_exchange.fetch_order(order["id"], SYMBOL)
            if float(o.get("filled") or 0) > 0.0001:
                return o  # 部分成交也接受
            print(f"  🔁 maker 第{attempt+1}輪未成交（掛 ${bid:,.2f}），追價重掛")
        print("  ℹ️ maker 3輪未成交，本次進場放棄（訊號若持續下根K線再試）")
        return None
    except Exception as e:
        notify(f"🚨 [{STRAT_KEY}] maker買入失敗：{e}")
        return None

def _live_place_stop(qty, stop_price):
    """買入後在交易所掛止損市價單（毫秒級觸發，消滅停損穿透）；失敗則退回機器人10分鐘輪詢停損"""
    try:
        # 合約模式帳戶下現貨條件單需明確指定 tdMode=cash（50014 ccy 錯誤的修正）
        o = live_exchange.create_order(SYMBOL, "market", "sell", qty, None,
                                       {"stopLossPrice": stop_price, "tdMode": "cash"})
        print(f"  🛡️ 交易所止損已掛：跌至 ${stop_price:,.2f} 自動賣出")
        return o.get("id") or ""
    except Exception as e:
        print(f"  ⚠️ 交易所止損掛單失敗（退回機器人停損）：{e}")
        notify(f"⚠️ [{STRAT_KEY}] 交易所止損掛單失敗（退回機器人停損）：{e}")
        return ""

def _live_cancel_stop(algo_id):
    """出場前撤掉交易所止損單（避免重複賣出）"""
    for params in ({"stop": True}, {"trigger": True}, {}):
        try:
            live_exchange.cancel_order(algo_id, SYMBOL, params)
            return
        except Exception:
            continue

def _live_fill_price(order, fallback):
    """實盤記帳用實際成交均價；下單回應沒有就查一次訂單，都取不到才退回K線收盤價"""
    try:
        avg = order.get("average") or order.get("price")
        if not avg and order.get("id"):
            time.sleep(1)
            fetched = live_exchange.fetch_order(order["id"], SYMBOL)
            avg = fetched.get("average") or fetched.get("price")
        if avg:
            return float(avg)
    except Exception as e:
        print(f"  ⚠️ 取成交均價失敗，退回K線收盤價：{e}")
    return fallback

def _live_sync_position(portfolio):
    """缺口5：啟動時核對交易所持倉與 JSON 是否一致"""
    exchange_qty = _live_get_position_qty()
    if exchange_qty is None:
        return portfolio
    json_qty  = portfolio.get("position", 0)
    threshold = 0.0001
    if exchange_qty > threshold and json_qty <= threshold:
        notify(f"⚠️ [{STRAT_KEY}] 不一致：交易所有 {exchange_qty:.6f} {ASSET}，JSON 空倉，請手動核對")
    elif exchange_qty <= threshold and json_qty > threshold:
        if portfolio.get("live_algo_id"):
            # 交易所止損單在輪詢間隔內觸發：以止損價完整記帳出場
            entry   = portfolio["entry_price"]
            stop_px = round(entry * 0.985, 4)
            sell_value = json_qty * stop_px * (1 - COMMISSION)
            cost       = json_qty * entry * (1 + COMMISSION)
            pnl        = sell_value - cost
            pnl_pct    = pnl / cost * 100
            portfolio["capital"]           += sell_value
            portfolio["position"]           = 0.0
            portfolio["entry_price"]        = 0.0
            portfolio["entry_time"]         = ""
            portfolio["last_exit_candle"]   = portfolio.get("last_candle", "")
            portfolio["total_trades"]      += 1
            portfolio["losses"]            += 1
            portfolio["total_pnl"]         += pnl
            portfolio["consecutive_losses"] = portfolio.get("consecutive_losses", 0) + 1
            portfolio["live_algo_id"]       = ""
            _risk_check_after_sell(portfolio, pnl_pct)
            log_trade("SELL", stop_px, json_qty, pnl_pct, "交易所止損觸發", portfolio)
            save_portfolio(portfolio)
            notify(f"🔴 【實盤】出場｜{STRATEGY_LABEL.get(STRAT_KEY)}\n"
                   f"進場：${entry:,.2f} → 止損：${stop_px:,.2f}\n"
                   f"損益：{pnl_pct:+.2f}%\n"
                   f"原因：交易所止損單觸發（毫秒級保護）")
        else:
            notify(f"⚠️ [{STRAT_KEY}] 不一致：JSON 有持倉但交易所無 {ASSET}，自動重置 JSON 為空倉")
            portfolio["position"]    = 0.0
            portfolio["entry_price"] = 0.0
            portfolio["entry_time"]  = ""
            save_portfolio(portfolio)
    return portfolio

# ─────────────────────────────────────────────
# 工具函數
# ─────────────────────────────────────────────
def notify(msg):
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    try:
        url  = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        data = urllib.parse.urlencode({"chat_id": TG_CHAT_ID, "text": msg}).encode()
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
        # EMA48 趨勢過濾：確認上升趨勢，避免下跌趨勢接刀
        ema_s_now  = df["ema_s"].iloc[-2]
        ema_s_prev = df["ema_s"].iloc[-EMA_TREND_BARS]
        trend_up   = ema_s_now > ema_s_prev
        # 支撐破位過濾：收盤低於近48根最低點 = 跌破支撐 = 不接刀
        recent_support = df["low"].iloc[-50:-3].min()
        above_support  = latest["close"] > recent_support
        ok = rsi < RSI_BUY and trend_up and above_support
        c2 = (f"{'✅' if rsi < RSI_BUY else '❌'}<{RSI_BUY} "
              f"EMA48{'✅' if trend_up else '❌'} "
              f"支撐{'✅' if above_support else '❌跌破'}")
        return ok, f"RSI(9) {rsi:.1f}", c2

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
        if rsi > RSI_SELL:
            # 變體：未達最低獲利門檻則續抱，避免+0.5%小贏單被摩擦吃光
            if MIN_PROFIT_PCT > 0 and price < entry_price * (1 + MIN_PROFIT_PCT / 100):
                pass
            else:
                return f"RSI(9)>{RSI_SELL:.0f}超買出場"
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

    if check_emergency_stop():
        return

    portfolio = load_portfolio()
    if LIVE_TRADE:
        portfolio = _live_sync_position(portfolio)

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

    # ── 連敗熔斷（所有策略）───────────────────
    CIRCUIT_BREAKER = 3
    if portfolio.get("consecutive_losses", 0) >= CIRCUIT_BREAKER:
        print(f"  🚫 熔斷中（連敗 {portfolio['consecutive_losses']} 次）跳過進場")
        save_portfolio(portfolio)
        sheets_post({
            "type": "monitor_log", "time": now_time, "price": str(price),
            "bb_upper": str(bb_upper), "bb_lower": str(bb_lower),
            "recent_low": str(recent_low), "cond_bb": "熔斷", "cond_macd": f"連敗{portfolio['consecutive_losses']}次",
            "signal": "熔斷暫停", "account_status": "空倉（熔斷）", "portfolio": portfolio,
        })
        return

    # ── 實盤保護（外部審查團P0）：風控停機 + 情境閘門（只擋新倉）──
    if LIVE_TRADE:
        halt_until = portfolio.get("halt_until", "")
        if halt_until and datetime.now(timezone.utc).isoformat() < halt_until:
            print(f"  🛑 風控停機中（至 {halt_until[:16]}）跳過進場")
            save_portfolio(portfolio)
            return
        if REGIME_GATE:
            gate_ok, gate_why = _regime_gate()
            if not gate_ok:
                print(f"  ⛔ 情境閘門關閉：{gate_why}，跳過進場")
                save_portfolio(portfolio)
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
    # 缺口1/2/3/4：實盤下單（先下單成功才更新 portfolio）
    if LIVE_TRADE:
        if not _live_check_balance():
            print("  ❌ 餘額不足，取消本次進場")
            return
        live_qty = round(LIVE_CAPITAL / price, 6)
        order = _live_maker_buy(live_qty, price)
        if order is None:
            print("  ❌ 實盤進場未成交，取消本次進場")
            return
        price = _live_fill_price(order, price)
        print(f"  ✅ 【實盤】買入 {live_qty} {ASSET} @ ${price:,.2f}（實際成交均價）")
        # 交易所級止損：用實際可賣數量（買入手續費以SOL扣）
        time.sleep(1)
        avail = _live_get_position_qty() or 0
        if avail > 0.0001:
            portfolio["live_algo_id"] = _live_place_stop(avail, round(price * 0.985, 2))

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
    label_prefix = "🔴 【實盤】" if LIVE_TRADE else "🔔"
    notify(f"{label_prefix} 進場｜{STRATEGY_LABEL.get(STRAT_KEY)}\n"
           f"進場價：${price:,.2f}\n"
           f"{'實盤' if LIVE_TRADE else '模擬'}買入：{(round(LIVE_CAPITAL/price,6) if LIVE_TRADE else qty):.6f} {ASSET}\n"
           f"原因：{reason}")
    print(f"  🔔 買入 {qty:.6f} @ ${price:,.2f}")


def _execute_sell(df, latest, portfolio, price, bb_upper, bb_lower, reason, now_time):
    # 缺口1/3/6：實盤賣出（先賣成功才更新 portfolio；失敗保留持倉等下次重試）
    if LIVE_TRADE:
        # 先撤交易所止損單，避免與本次賣出重複成交
        if portfolio.get("live_algo_id"):
            _live_cancel_stop(portfolio["live_algo_id"])
            portfolio["live_algo_id"] = ""
        live_qty = _live_get_position_qty()
        if live_qty is None:
            print("  ❌ 無法取得持倉，保留狀態待下次重試")
            return
        if live_qty > 0.0001:
            order = _live_place_order("sell", live_qty)
            if order is None:
                print("  ❌ 實盤賣出失敗，保留持倉狀態待下次重試")
                return
            price = _live_fill_price(order, price)
            print(f"  ✅ 【實盤】賣出 {live_qty} {ASSET} @ ${price:,.2f}（實際成交均價）")
        else:
            notify(f"⚠️ [{STRAT_KEY}] 交易所無 {ASSET} 持倉，跳過真實賣出，更新模擬狀態")

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
    _risk_check_after_sell(portfolio, pnl_pct)
    log_trade("SELL", price, qty, pnl_pct, reason, portfolio)
    save_portfolio(portfolio)
    sheets_post({"type":"trade","time":now_time,"action":"SELL","price":str(price),
                 "qty":str(qty),"pnl_pct":f"{pnl_pct:+.2f}%","capital_after":str(portfolio["capital"]),
                 "reason":reason,"portfolio":portfolio,"bb_upper":str(bb_upper),"bb_lower":str(bb_lower)})
    icon = "🟢" if pnl > 0 else "🔴"
    live_tag = " 【實盤】" if LIVE_TRADE else ""
    notify(f"{icon}{live_tag} 出場｜{STRATEGY_LABEL.get(STRAT_KEY)}\n"
           f"進場：${entry_price:,.2f} → 出場：${price:,.2f}\n"
           f"損益：{pnl_pct:+.2f}%（{pnl:+.2f} USDT）\n"
           f"原因：{reason}")
    print(f"  {icon} 賣出 @ ${price:,.2f}  損益：{pnl_pct:+.2f}%  原因：{reason}")


if __name__ == "__main__":
    run()

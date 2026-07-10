"""
雙獵場週掃描 — 本地 launchd 每週日 10:00 執行
OKX + 幣安 × 趨勢腿T1/回歸腿 × 90天,判準固定,結果發 Telegram
"""
import json, time, hmac, hashlib, urllib.request, urllib.parse

TG_TOKEN   = "8731884089:AAEYJc7S6YoUuNeGjlbSjI5TpNvleQNqnyM"
TG_CHAT_ID = "7898079577"
DAYS = 90
FRICTION = 0.0025

def http(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    return json.load(urllib.request.urlopen(req, timeout=20))

# ── 行情抓取 ──────────────────────────────
def okx_top(n=12):
    d = http("https://www.okx.com/api/v5/market/tickers?instType=SPOT")["data"]
    rows = [t for t in d if t["instId"].endswith("-USDT")
            and not any(s in t["instId"] for s in ("USDC","DAI","TUSD","PAXG","XAUT","STETH","BETH"))]
    rows.sort(key=lambda t: float(t.get("volCcy24h") or 0)*float(t.get("last") or 0), reverse=True)
    return [r["instId"] for r in rows[:n]]

def bn_top(n=15):
    d = http("https://api.binance.com/api/v3/ticker/24hr")
    rows = [t for t in d if t["symbol"].endswith("USDT")
            and not any(s in t["symbol"] for s in ("USDC","FDUSD","DAI","TUSD","PAXG","EUR","BUSD"))]
    rows.sort(key=lambda t: float(t.get("quoteVolume") or 0), reverse=True)
    return [r["symbol"] for r in rows[:n]]

def okx_klines(inst, bar, total):
    out, before = [], ""
    while len(out) < total:
        url = f"https://www.okx.com/api/v5/market/history-candles?instId={inst}&bar={bar}&limit=100" + (f"&after={before}" if before else "")
        d = http(url).get("data", [])
        if not d: break
        out.extend(d); before = d[-1][0]
        time.sleep(0.12)
    out.reverse()
    return [[int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4])] for x in out]

def bn_klines(sym, interval, total):
    out, end = [], ""
    while len(out) < total:
        url = f"https://api.binance.com/api/v3/klines?symbol={sym}&interval={interval}&limit=1000" + (f"&endTime={end}" if end else "")
        d = http(url)
        if not d: break
        out = d + out; end = d[0][0] - 1
        time.sleep(0.1)
    return [[int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4])] for x in out[-total:]]

# ── 指標 ─────────────────────────────────
def ema_series(vals, span):
    k = 2/(span+1); out = [vals[0]]
    for v in vals[1:]:
        out.append(v*k + out[-1]*(1-k))
    return out

def rsi_series(closes, n=9):
    rsi = [None]*len(closes); g = l = 0.0
    for i in range(1, len(closes)):
        ch = closes[i]-closes[i-1]; up = max(ch,0); dn = max(-ch,0)
        if i <= n: g += up; l += dn
        if i == n:
            g /= n; l /= n
            rsi[i] = 100-100/(1+(g/l if l else 99))
        elif i > n:
            g = (g*(n-1)+up)/n; l = (l*(n-1)+dn)/n
            rsi[i] = 100-100/(1+(g/l if l else 99))
    return rsi

# ── 兩套策略回測 ──────────────────────────
def bt_t1(k4h, k1d):
    """趨勢腿:4H收>EMA20進/收<EMA50出/-8%強停;閘門=日線收>EMA50"""
    c4 = [r[4] for r in k4h]
    e20, e50 = ema_series(c4,20), ema_series(c4,50)
    dc = [r[4] for r in k1d]; de50 = ema_series(dc,50)
    dmap = {}
    for i, r in enumerate(k1d):
        day = time.strftime("%Y-%m-%d", time.gmtime(r[0]/1000))
        dmap[day] = dc[i] > de50[i]
    cap, pos, entry, peak_dd, peak = 1.0, 0, 0, 0, 1.0
    trades = []
    d30_start = None
    for i in range(60, len(k4h)):
        ts, c = k4h[i][0], c4[i]
        if pos:
            if c < e50[i] or c < entry*0.92:
                pnl = (c/entry-1) - FRICTION
                cap *= 1+pnl; trades.append((ts, pnl)); pos = 0
                peak = max(peak, cap); peak_dd = min(peak_dd, cap/peak-1)
            continue
        prev_day = time.strftime("%Y-%m-%d", time.gmtime((ts-86400000)/1000))
        if dmap.get(prev_day) and c > e20[i]:
            pos = 1; entry = c
    return summarize(trades, cap, peak_dd, k4h)

def bt_rev(k15, k1d=None):
    """回歸腿:RSI9<40+EMA48升+50根支撐;RSI>70出/-1.5%停/-5%強停;K1冷卻"""
    c = [r[4] for r in k15]; lows = [r[3] for r in k15]
    rsi = rsi_series(c); e48 = ema_series(c,48)
    cap, pos, entry, cd, peak, peak_dd = 1.0, 0, 0, 0, 1.0, 0
    trades = []
    for i in range(60, len(c)):
        if pos:
            r = rsi[i]; ex = None
            if c[i] < entry*0.985: ex = "stop"
            elif c[i] < entry*0.95: ex = "stop"
            elif r and r > 70: ex = "rsi"
            if ex:
                pnl = (c[i]/entry-1) - FRICTION
                cap *= 1+pnl; trades.append((k15[i][0], pnl)); pos = 0
                cd = 16 if ex == "stop" else 4
                peak = max(peak, cap); peak_dd = min(peak_dd, cap/peak-1)
            continue
        if cd: cd -= 1; continue
        r = rsi[i]
        if r and r < 40 and e48[i] > e48[i-48] and c[i] > min(lows[i-50:i]):
            pos = 1; entry = c[i]
    return summarize(trades, cap, peak_dd, k15)

def summarize(trades, cap, mdd, klines):
    cutoff = klines[-1][0] - 30*86400000
    d30 = 1.0
    for ts, pnl in trades:
        if ts >= cutoff: d30 *= 1+pnl
    wins = sum(1 for _, p in trades if p > 0)
    return {"ret": (cap-1)*100, "d30": (d30-1)*100, "mdd": mdd*100,
            "n": len(trades), "wr": wins/len(trades)*100 if trades else 0}

def passes(s):
    return s["ret"] > 5 and s["d30"] >= 0 and s["mdd"] > -25 and s["n"] >= 8

# ── 主流程 ────────────────────────────────
def scan():
    results = []
    for inst in okx_top():
        try:
            k4h = okx_klines(inst, "4H", DAYS*6)
            k1d = okx_klines(inst, "1D", DAYS+60)
            k15 = okx_klines(inst, "15m", DAYS*96)
            for leg, s in (("T1", bt_t1(k4h, k1d)), ("回歸", bt_rev(k15))):
                results.append({"ex": "OKX", "sym": inst.replace("-USDT",""), "leg": leg, **s})
        except Exception as e:
            print(inst, "err", e)
    for sym in bn_top():
        try:
            k4h = bn_klines(sym, "4h", DAYS*6)
            k1d = bn_klines(sym, "1d", DAYS+60)
            k15 = bn_klines(sym, "15m", DAYS*96)
            for leg, s in (("T1", bt_t1(k4h, k1d)), ("回歸", bt_rev(k15))):
                results.append({"ex": "幣安", "sym": sym.replace("USDT",""), "leg": leg, **s})
        except Exception as e:
            print(sym, "err", e)
    return results

def main():
    rs = scan()
    ok = [r for r in rs if passes(r)]
    ok.sort(key=lambda r: r["d30"], reverse=True)
    lines = [f"📡 雙獵場週掃描 {time.strftime('%m/%d')}（90天，判準固定）", ""]
    if ok:
        lines.append("✅ 合格組合（按近30天排序）:")
        for r in ok[:6]:
            lines.append(f"{r['ex']} {r['sym']}×{r['leg']}: {r['ret']:+.0f}% | 30d {r['d30']:+.1f}% | DD {r['mdd']:.0f}% | {r['n']}筆")
        okx_syms = {r["sym"] for r in ok if r["ex"]=="OKX"}
        bn_only = [r for r in ok if r["ex"]=="幣安" and r["sym"] not in okx_syms]
        lines.append("")
        lines.append("🔍 幣安獨有機會: " + ("、".join(f"{r['sym']}×{r['leg']}" for r in bn_only[:3]) if bn_only else "無（遷移觸發未命中）"))
    else:
        lines.append("❌ 全市場0合格——維持現有部署，雨天空手")
    top = sorted(rs, key=lambda r: r["ret"], reverse=True)[:3]
    lines.append("")
    lines.append("榜首（未必合格）: " + " | ".join(f"{r['ex']}{r['sym']}×{r['leg']} {r['ret']:+.0f}%" for r in top))
    msg = "\n".join(lines)
    print(msg)
    data = urllib.parse.urlencode({"chat_id": TG_CHAT_ID, "text": msg[:4000]}).encode()
    urllib.request.urlopen(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data, timeout=15)
    json.dump(rs, open(f"/tmp/weekly_scan_{time.strftime('%m%d')}.json", "w"))

if __name__ == "__main__":
    main()

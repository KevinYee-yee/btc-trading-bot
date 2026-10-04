"""SMC 標的掃描器（2026-10-05）——只篩選、報價位，不下單。

Kevin 手動做 SMC，卡在「找標的」：每次從零翻 8 張圖，大多卡在「位置對、賺賠比不夠」。
這支把三關篩選的前兩關＋賺賠比自動化，每根 4H K 棒收盤後推一次 Telegram：
  ① 日線方向：收盤在 50 日均線上方且均線上升 → 多；反之空；其餘中性（現貨只做多，空/中性跳過）
  ② 4H 位置：找最近一段上漲衝擊（波段低→波段高），價格要在折價區（50% 以下），
     且下方有 POI：未回補的多方 FVG／衝擊前最後一根陰線 OB／≥2 次碰觸的支撐區
  ③ 賺賠比：停損 = POI 下緣 − 0.5 ATR，且距進場至少 1 倍 4H ATR；目標 = 上方最近的流動性（前高）再打 0.2% 折扣；
     扣來回手續費 0.2% 後 ≥2 才算候選
最後一步（1H 止跌 K 棒、要不要進場）留給人判斷。9/30 回測已證明 SMC 機械化下單不及格，這裡只當篩子。
"""
import os, json, urllib.request, urllib.parse
from datetime import datetime, timezone, timedelta

WATCHLIST = [s.strip() for s in os.environ.get(
    "SCAN_LIST", "BTC,ETH,SOL,ZEC,HYPE,OKB,DOGE,XTSLA").split(",") if s.strip()]
TG_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TG_CHAT  = os.environ.get("TELEGRAM_CHAT_ID", "")
STATE    = "scanner_state.json"
FEE_RT   = 0.002          # 現貨來回手續費
MIN_RR   = 2.0
TW       = timezone(timedelta(hours=8))
STOCKS   = {"XTSLA", "XNVDA", "XAAPL", "XSPY", "XQQQ", "XMSTR"}


def get(path):
    req = urllib.request.Request("https://www.okx.com" + path, headers={"User-Agent": "Mozilla/5.0"})
    d = json.loads(urllib.request.urlopen(req, timeout=15).read())
    if d.get("code") != "0":
        raise RuntimeError(d.get("msg"))
    return d["data"]


def candles(inst, bar, limit):
    rows = [r for r in get(f"/api/v5/market/candles?instId={inst}&bar={bar}&limit={limit}") if r[8] == "1"]
    return [dict(t=int(r[0]), o=float(r[1]), h=float(r[2]), l=float(r[3]), c=float(r[4])) for r in reversed(rows)]


def atr(R, n=14):
    trs = [max(R[i]["h"] - R[i]["l"], abs(R[i]["h"] - R[i-1]["c"]), abs(R[i]["l"] - R[i-1]["c"])) for i in range(1, len(R))]
    return sum(trs[-n:]) / n


def swings(R, k=3):
    hi = [i for i in range(k, len(R) - k) if R[i]["h"] == max(x["h"] for x in R[i-k:i+k+1])]
    lo = [i for i in range(k, len(R) - k) if R[i]["l"] == min(x["l"] for x in R[i-k:i+k+1])]
    return hi, lo


def fmt(x):
    return f"{x:,.1f}" if x >= 1000 else (f"{x:.2f}" if x >= 10 else (f"{x:.4f}" if x >= 0.1 else f"{x:.5f}"))


def daily_bias(D):
    c = [x["c"] for x in D]
    if len(c) < 56:
        return "中性", "日線資料不足"
    m = sum(c[-50:]) / 50
    m5 = sum(c[-55:-5]) / 50
    if c[-1] > m and m > m5:
        return "多", f"日線在50日均線({fmt(m)})上方且上升"
    if c[-1] < m and m < m5:
        return "空", f"日線在50日均線({fmt(m)})下方且下降"
    return "中性", f"日線貼著50日均線({fmt(m)})、方向不明"


def find_pois(R, lo_i, hi_i, a):
    """在衝擊段 [lo_i, hi_i] 內找 POI，並排除之後已被跌破/回補的"""
    pois = []
    last = len(R) - 1
    # 多方 FVG：第 i 根高點 < 第 i+2 根低點
    for i in range(lo_i, hi_i - 1):
        top, bot = R[i+2]["l"], R[i]["h"]
        if top > bot and (top - bot) > 0.15 * a:
            after_low = min(x["l"] for x in R[i+3:last+1]) if i + 3 <= last else top
            if after_low > bot:                       # 沒被完全回補
                pois.append(("FVG", bot, min(top, after_low) if after_low < top else top))
    # OB：衝擊起點前最後一根陰線
    for i in range(lo_i, max(lo_i - 6, 0), -1):
        if R[i]["c"] < R[i]["o"]:
            top, bot = R[i]["o"], R[i]["l"]
            if min(x["l"] for x in R[i+1:last+1]) > bot:
                pois.append(("OB", bot, top))
            break
    return pois


def support_zones(R, lows, a):
    pts = sorted((R[i]["l"], i) for i in lows)
    zones, cur = [], []
    for p in pts:
        if cur and p[0] - cur[0][0] > 0.5 * a:
            zones.append(cur); cur = []
        cur.append(p)
    if cur:
        zones.append(cur)
    out = []
    for z in zones:
        if len(z) >= 2:
            lo = min(p for p, _ in z); hi = max(p for p, _ in z)
            if min(x["l"] for x in R[max(i for _, i in z)+1:]) >= lo - 0.2 * a:
                out.append(("支撐", lo, hi))
    return out


def scan(sym):
    inst = f"{sym}-USDT"
    D = candles(inst, "1D", 120)
    R = candles(inst, "4H", 200)
    H1 = candles(inst, "1H", 6)
    px = float(get(f"/api/v5/market/ticker?instId={inst}")[0]["last"])
    a = atr(R)
    bias, why = daily_bias(D)
    res = dict(sym=sym, px=px, bias=bias, why=why, status="❌", note="")
    if bias != "多":
        res["note"] = why + ("，現貨不做空" if bias == "空" else "，等方向明確")
        return res

    his, los = swings(R)
    if not his or not los:
        res["note"] = "4H 結構不清楚"; return res
    hi_i = his[-1]
    prev_lo = [i for i in los if i < hi_i]
    if not prev_lo:
        res["note"] = "4H 找不到衝擊起點"; return res
    lo_i = min(prev_lo[-3:], key=lambda i: R[i]["l"])     # 衝擊段起點取最近 3 個波段低的最低者
    H, L = R[hi_i]["h"], R[lo_i]["l"]
    # 確認後若已創更高點，用最新高點
    H = max(H, max(x["h"] for x in R[hi_i:]))
    eq = (H + L) / 2
    if px < L:
        res["note"] = f"已跌破衝擊起點 {fmt(L)}，結構失效"; return res
    zone_txt = f"4H 區間 {fmt(L)}–{fmt(H)}，50%={fmt(eq)}"

    pois = find_pois(R, lo_i, hi_i, a) + support_zones(R, los, a)
    pois = [p for p in pois if p[2] <= eq * 1.002 and p[1] >= L - 0.5 * a]    # 只要折價區內的
    below = [p for p in pois if p[1] <= px]
    if not below:
        res["note"] = f"{zone_txt}；折價區內沒有可用的 POI" if px <= eq else f"{zone_txt}；價格在溢價區（{fmt(px)}），等回到 {fmt(eq)} 以下"
        res["status"] = "⏳" if px > eq else "❌"
        return res
    kind, pb, pt = max(below, key=lambda p: p[2])           # 最靠近現價的那個
    inside = px <= pt + 0.3 * a
    entry = px if inside else pt
    # 停損要放在「被掃也不該回來」的地方：POI 下緣再留 0.5 ATR，且距進場至少 1 倍 4H ATR
    # （9/30 SMC 回測失敗主因：停損只放掃點下 0.1 ATR，中位數 0.73%，一半 5 小時內被再掃）
    stop = min(pb - 0.5 * a, entry - 1.0 * a)
    tgt = H * 0.998
    risk = (entry - stop) / entry
    rr = ((tgt - entry) / entry - FEE_RT) / (risk + FEE_RT) if risk > 0 else 0
    plan = f"進 {fmt(entry)}｜停 {fmt(stop)}｜目標 {fmt(tgt)}｜賺賠比 {rr:.1f}"
    poi_txt = f"{kind} {fmt(pb)}–{fmt(pt)}"
    res.update(entry=entry, stop=stop, tgt=tgt, rr=rr)

    if rr < 1.5:
        res["note"] = f"{zone_txt}；POI {poi_txt} 但賺賠比只有 {rr:.1f}"; return res
    if inside:
        # 1H 止跌確認（最後一根已收盤 1H）
        k = H1[-1]; body = abs(k["c"] - k["o"]); rng = max(k["h"] - k["l"], 1e-12)
        wick = min(k["o"], k["c"]) - k["l"]
        conf = k["l"] <= pt + 0.3 * a and (wick >= 2 * body or (k["c"] > k["o"] and (k["c"] - k["l"]) / rng >= 0.6))
        res["status"] = "✅" if rr >= MIN_RR else "⚠️"
        res["note"] = (f"到位！POI {poi_txt}\n   {plan}\n   1H {'已出現止跌K棒 👀' if conf else '還沒止跌，等收盤確認'}"
                       + ("" if rr >= MIN_RR else "（賺賠比未達2，謹慎）"))
    else:
        res["status"] = "⏳"
        res["note"] = f"等回到 POI {poi_txt}（設提醒 {fmt(pt)}）\n   {plan}"
    return res


def tg(msg):
    if not TG_TOKEN:
        print(msg); return
    data = urllib.parse.urlencode({"chat_id": TG_CHAT, "text": msg}).encode()
    urllib.request.urlopen(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data, timeout=15)


def main():
    now = datetime.now(timezone.utc)
    candle_key = (now - timedelta(hours=now.hour % 4, minutes=now.minute, seconds=now.second)).strftime("%Y-%m-%dT%H")
    force = os.environ.get("FORCE", "") == "true"
    try:
        st = json.load(open(STATE))
    except Exception:
        st = {}
    if st.get("last") == candle_key and not force:
        print(f"本根 4H（{candle_key}）已推播過，跳過"); return

    rows = []
    for s in WATCHLIST:
        try:
            rows.append(scan(s))
        except Exception as e:
            rows.append(dict(sym=s, status="⚠️", note=f"資料讀取失敗：{e}", px=0, bias=""))
    order = {"✅": 0, "⚠️": 1, "⏳": 2, "❌": 3}
    rows.sort(key=lambda r: order.get(r["status"], 9))
    stamp = now.astimezone(TW).strftime("%m/%d %H:%M")
    lines = []
    for r in rows:
        tag = "（美股代幣：注意財報與開盤跳空）" if r["sym"] in STOCKS and r["status"] in ("✅", "⏳") else ""
        lines.append(f"{r['status']} {r['sym']} {fmt(r['px']) if r['px'] else ''}：{r['note']}{tag}")
    ready = [r for r in rows if r["status"] in ("✅", "⚠️")]
    tail = ""
    crypto_ready = [r for r in ready if r["sym"] not in STOCKS]
    if len(crypto_ready) >= 2:
        tail = "\n\n⚠️ 幣種之間常同漲同跌，多個同時到位時只挑賺賠比最好的一個"
    tg(f"📍 SMC 掃描（{stamp}，4H 收盤）\n✅到位 ⏳等回調 ❌跳過｜只篩選不下單\n\n" + "\n\n".join(lines) + tail
       + "\n\n最後一關（1H 止跌K棒＋自己看圖）由你判斷")
    st["last"] = candle_key
    json.dump(st, open(STATE, "w"))


if __name__ == "__main__":
    main()

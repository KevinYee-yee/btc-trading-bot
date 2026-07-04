"""
每日行動導向報告 v2 — 本地 launchd 執行（台北 09:00）
不只列數據：真實損益、驗證進度、A/B 對比、後續行動
"""

import csv
import json
import subprocess
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta
from pathlib import Path

REPO_DIR   = Path(__file__).parent
TG_TOKEN   = "8731884089:AAEYJc7S6YoUuNeGjlbSjI5TpNvleQNqnyM"
TG_CHAT_ID = "7898079577"

TPE = timezone(timedelta(hours=8))
LIVE_CAPITAL = 45.0
LIVE_START   = datetime(2026, 7, 2, tzinfo=TPE)   # 實盤上線日

# 現役策略（已退役者不顯示）
ACTIVE = {
    "SOL_B":    ("paper_portfolio_sol_b.json",    "主力策略"),
    "SOL_B_V2": ("paper_portfolio_sol_b_v2.json", "A/B變體(+1%出場門檻)"),
    "SOL_B_V3": ("paper_portfolio_sol_b_v3.json", "A/B變體(RSI70出場·回測+14.4%)"),
    "A":        ("paper_portfolio.json",           "BTC觀察中"),
    "ETH_B":    ("paper_portfolio_eth_b.json",     "熔斷待修"),
}


def sh(cmd):
    return subprocess.run(cmd, cwd=REPO_DIR, capture_output=True, text=True).stdout


def load(fname):
    p = REPO_DIR / fname
    if not p.exists():
        return None
    return json.load(open(p))


def live_today_trades():
    """實盤交易紀錄：今日筆數與已實現損益（真實 USDT）"""
    p = REPO_DIR / "live_trade_log_sol_b.csv"
    rows = []
    if p.exists():
        with open(p) as f:
            rows = [r for r in csv.DictReader(f)]
    today = datetime.now(TPE).strftime("%Y-%m-%d")
    n_today, pnl_today = 0, 0.0
    for r in rows:
        try:
            t_tpe = (datetime.strptime(r["time"], "%Y-%m-%d %H:%M")
                     .replace(tzinfo=timezone.utc).astimezone(TPE))
        except Exception:
            continue
        if r["action"] == "SELL" and t_tpe.strftime("%Y-%m-%d") == today:
            n_today += 1
            try:
                pnl_today += float(r["pnl_pct"].replace("%", "").replace("+", "")) / 100 * LIVE_CAPITAL
            except Exception:
                pass
    return n_today, pnl_today


def fmt_paper(key, fname, note):
    p = load(fname)
    if p is None:
        return f"{key}: （尚無資料）{note}"
    t, w = p["total_trades"], p["wins"]
    wr   = f"{w/t*100:.0f}%" if t else "-"
    ret  = p["total_pnl"] / 10  # $1000 基準 → %
    icon = "▲" if ret > 0 else ("▼" if ret < 0 else "─")
    pos  = f"持倉@${p['entry_price']:.0f}" if p.get("position", 0) > 0 else "空倉"
    cb   = " 🔴熔斷" if p.get("consecutive_losses", 0) >= 3 else ""
    return f"{key}: {icon}{ret:+.1f}% | {t}筆 勝{wr} | {pos}{cb}  ← {note}"


def build_actions(live, sol_b, sol_b2):
    acts = []
    lt = live["total_trades"] if live else 0
    lw = live["wins"] if live else 0
    lwr = lw / lt * 100 if lt else 0
    acts.append(f"實盤驗證 {lt}/20 筆（勝率{lwr:.0f}%，≥60%過關→$90全投）")
    if sol_b:
        acts.append(f"SOL_B 紙上 {sol_b['total_trades']}/40 筆（40筆＋月化≥5%→注資1萬TWD）")
    if sol_b2 and sol_b2["total_trades"] >= 5 and sol_b:
        d = sol_b2["total_pnl"] - sol_b["total_pnl"]
        lead = "V2領先" if d > 0 else "原版領先"
        acts.append(f"A/B測試：{lead} {abs(d)/10:.1f}%（V2滿20筆且領先→切換實盤出場邏輯）")
    elif sol_b2 is not None:
        acts.append(f"A/B測試 V2 運行中（{sol_b2['total_trades']}筆，滿5筆開始對比）")
    for key, (fname, _) in ACTIVE.items():
        p = load(fname)
        if p and p.get("consecutive_losses", 0) >= 3:
            acts.append(f"⚠️ {key} 熔斷中（連敗{p['consecutive_losses']}），待策略修正後重啟")
    return acts



def _okx_daily(inst, n=80):
    req = urllib.request.Request(
        f"https://www.okx.com/api/v5/market/candles?instId={inst}&bar=1D&limit={n}",
        headers={"User-Agent": "Mozilla/5.0"})
    d = json.load(urllib.request.urlopen(req, timeout=15))["data"]
    d.reverse()
    return [(float(x[2]), float(x[3]), float(x[4])) for x in d]  # h,l,c


def _ema_last(vals, span):
    k = 2 / (span + 1); e = vals[0]
    for v in vals[1:]:
        e = v * k + e * (1 - k)
    return e


def _adx_last(rows, n=14):
    trs, pdms, mdms = [], [], []
    for i in range(1, len(rows)):
        h, l, c = rows[i]; ph, pl, pc = rows[i-1]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        up, dn = h - ph, pl - l
        pdms.append(up if up > dn and up > 0 else 0)
        mdms.append(dn if dn > up and dn > 0 else 0)
    a, p, m = sum(trs[:n])/n, sum(pdms[:n])/n, sum(mdms[:n])/n
    dxs = []
    for i in range(n, len(trs)):
        a = (a*(n-1)+trs[i])/n; p = (p*(n-1)+pdms[i])/n; m = (m*(n-1)+mdms[i])/n
        P, M = 100*p/a, 100*m/a
        dxs.append(100*abs(P-M)/(P+M) if P+M else 0)
    e = sum(dxs[:n])/n
    for v in dxs[n:]:
        e = (e*(n-1)+v)/n
    return e


def weather_lines():
    """天氣預報：閘門各條件現況與差距"""
    out = ["", "━━ ⛅ 天氣預報（進場閘門）━━"]
    try:
        conds = []
        for name, inst in (("SOL", "SOL-USDT"), ("BTC", "BTC-USDT")):
            rows = _okx_daily(inst)
            closes = [r[2] for r in rows]
            e50 = _ema_last(closes, 50); c = closes[-1]
            dist = (c/e50 - 1) * 100
            ok = c > e50
            conds.append(ok)
            out.append(f"{name}: {'☀️' if ok else '🌧️'} {dist:+.1f}% {'高於' if ok else '低於'}50日線" +
                       ("" if ok else f"（開閘還差 {abs(dist):.1f}%）"))
            if name == "SOL":
                adx = _adx_last(rows)
                conds.append(adx >= 18)
                out.append(f"SOL 趨勢強度 ADX: {adx:.0f}（{'✅≥18' if adx >= 18 else '❌<18 死盤整'}）")
        out.append("→ 閘門：" + ("🟢 開放，機器人可進場" if all(conds) else "🔴 關閉，持有現金等待"))
    except Exception as e:
        out.append(f"（天氣資料取得失敗：{e}）")
    return out


def main():
    print(sh(["git", "pull"]))
    now = datetime.now(TPE)
    day_n = (now - LIVE_START).days + 1

    live = load("live_portfolio_sol_b.json")
    n_today, pnl_today = live_today_trades()

    lines = [f"📊 AI交易日報 {now:%m/%d}（實盤第{day_n}天）", ""]
    lines.append("━━ 💰 實盤 SOL_B（本金$45）━━")
    if live:
        t, w = live["total_trades"], live["wins"]
        wr = f"{w/t*100:.0f}%" if t else "-"
        real_pnl = live["total_pnl"] / 1000 * LIVE_CAPITAL
        pos = f"持倉@${live['entry_price']:.2f}" if live.get("position", 0) > 0 else "空倉"
        lines.append(f"累計: {t}筆 勝{wr} | 真實損益 {real_pnl:+.2f} USDT（{live['total_pnl']/10:+.2f}%）")
        lines.append(f"今日: {n_today}筆 {pnl_today:+.2f} USDT | {pos}")
    else:
        lines.append("（無實盤資料）")

    lines.append("")
    lines.append("━━ 🧪 紙上驗證 ━━")
    for key, (fname, note) in ACTIVE.items():
        lines.append(fmt_paper(key, fname, note))

    for w in weather_lines():
        lines.append(w)

    lines.append("")
    lines.append("━━ ✅ 行動追蹤 ━━")
    sol_b  = load("paper_portfolio_sol_b.json")
    sol_b2 = load("paper_portfolio_sol_b_v2.json")
    for a in build_actions(live, sol_b, sol_b2):
        lines.append(f"• {a}")

    msg = "\n".join(lines)
    print(msg)
    data = urllib.parse.urlencode({"chat_id": TG_CHAT_ID, "text": msg}).encode()
    r = urllib.request.urlopen(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data, timeout=15)
    print(f"Telegram 發送 ({r.status})")


if __name__ == "__main__":
    main()

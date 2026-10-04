"""實盤看門狗 v2（2026-10-04）

起因：GAS 派送器的 GitHub token 10/01 到期，ZEC/HYPE 實盤停擺 3 天沒人發現——
舊看門狗也靠同一個 GAS 觸發，一起死掉就沒人告警。

v2 原則：
  1. 看門狗自己走 GitHub 原生排程（不依賴 GAS），GAS 恢復後也會順便派送它，兩條路有一條活著就行
  2. 用 GitHub API 直接查每個實盤 workflow 最後一次執行時間/結果，不靠心跳檔
  3. 單獨偵測「GAS 派送器是否還活著」（ZEC/HYPE 最後一次 workflow_dispatch 觸發時間）
  4. 對帳：帳本有持倉 → 交易所必須有幣、也必須有止損單；交易所有幣但帳本空倉也要報
  5. MODE=daily 時不論好壞都發一份日報（「沒收到日報」本身就是警訊）

只讀：不下單、不改帳本。
"""
import os, json, urllib.request
from datetime import datetime, timezone, timedelta

REPO     = os.environ.get("GITHUB_REPOSITORY", "KevinYee-yee/btc-trading-bot")
GH_TOKEN = os.environ.get("GH_TOKEN", "")
TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT  = os.environ.get("TG_CHAT_ID", "")
MODE     = os.environ.get("MODE", "alert")          # alert：有問題才發；daily：一律發日報
TW       = timezone(timedelta(hours=8))
NOW      = datetime.now(timezone.utc)

# 名稱、workflow、開關變數、帳本、交易所幣種、容許最久沒跑（分鐘）
# GAS 派送的每 10 分鐘一次；GitHub 原生排程實測會被節流到 1.5~3 小時，所以門檻放寬
STRATS = [
    ("ZEC 趨勢",   "live_zec_t1.yml",       "LIVE_ZEC_T1",       "live_portfolio_zec_t.json",    "ZEC",  60),
    ("HYPE 趨勢",  "live_hype_t1.yml",      "LIVE_HYPE_T1",      "live_portfolio_hype_t.json",   "HYPE", 60),
    ("DOGE 均值",  "live_meanrev_doge.yml", "LIVE_MEANREV_DOGE", "live_portfolio_doge_mr.json",  "DOGE", 300),
    ("BTC 均值",   "live_meanrev_btc.yml",  "LIVE_MEANREV_BTC",  "live_portfolio_mr.json",       "BTC",  300),
    ("NEAR 均值",  "live_meanrev_near.yml", "LIVE_MEANREV_NEAR", "live_portfolio_near_mr.json",  "NEAR", 300),
    ("SOL_B",      "live_sol_b.yml",        "LIVE_SOL_B",        "live_portfolio_sol_b.json",    "SOL",  60),
]
GAS_MAX_SILENCE_MIN = 40   # 超過這麼久沒有任何 workflow_dispatch 觸發 → GAS 派送器可能掛了


def gh(path):
    req = urllib.request.Request(f"https://api.github.com/repos/{REPO}{path}",
                                 headers={"Authorization": f"Bearer {GH_TOKEN}",
                                          "Accept": "application/vnd.github+json"})
    return json.loads(urllib.request.urlopen(req, timeout=20).read())


def tg(msg):
    if not TG_TOKEN or not TG_CHAT:
        print(msg); return
    data = urllib.parse.urlencode({"chat_id": TG_CHAT, "text": msg}).encode()
    urllib.request.urlopen(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data, timeout=15)


def ago(ts):
    t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return int((NOW - t).total_seconds() // 60)


def fmt_min(m):
    return f"{m} 分鐘" if m < 120 else (f"{m/60:.1f} 小時" if m < 2880 else f"{m/1440:.1f} 天")


def check_runs(wf, max_age):
    """回傳 (問題list, 狀態文字, 最後一次dispatch距今分鐘)"""
    runs = gh(f"/actions/workflows/{wf}/runs?per_page=15").get("workflow_runs", [])
    real = [r for r in runs if r.get("conclusion") != "skipped"]
    if not real:
        return [f"從未執行過"], "從未執行", None
    last = real[0]
    age = ago(last["created_at"])
    probs = []
    if age > max_age:
        probs.append(f"已 {fmt_min(age)} 沒執行（門檻 {fmt_min(max_age)}）")
    fails = 0
    for r in real:
        if r.get("status") != "completed":
            continue
        if r.get("conclusion") == "failure":
            fails += 1
        else:
            break
    if fails >= 2:
        probs.append(f"連續 {fails} 次執行失敗：{last['html_url']}")
    disp = [r for r in runs if r.get("event") == "workflow_dispatch"]
    disp_age = ago(disp[0]["created_at"]) if disp else None
    return probs, f"{fmt_min(age)}前執行", disp_age


def load_portfolio(path):
    try:
        return json.load(open(path))
    except Exception:
        return None


def okx():
    k, s, p = (os.environ.get(x, "") for x in ("OKX_API_KEY", "OKX_SECRET", "OKX_PASSPHRASE"))
    if not k:
        return None
    import ccxt
    return ccxt.okx({"apiKey": k, "secret": s, "password": p, "enableRateLimit": True})


def main():
    ex = okx()
    bal, stops = {}, {}
    exch_err = ""
    if ex:
        try:
            b = ex.fetch_balance()
            bal = {c: float((b.get(c) or {}).get("total") or 0) for c in ("ZEC", "HYPE", "DOGE", "BTC", "NEAR", "SOL", "USDT")}
            r = ex.private_get_trade_orders_algo_pending({"ordType": "conditional"})
            for od in r.get("data", []):
                stops.setdefault(od.get("instId", "").split("-")[0], []).append(od.get("slTriggerPx") or od.get("triggerPx"))
        except Exception as e:
            exch_err = f"交易所查詢失敗：{e}"

    problems, lines, gas_ages = [], [], []
    for name, wf, var, pf_file, coin, max_age in STRATS:
        on = os.environ.get(var, "") == "on"
        pf = load_portfolio(pf_file) or {}
        pos = float(pf.get("position") or 0)
        perf = pf.get("perf", {})
        if not on:
            if pos > 0:
                problems.append(f"【{name}】開關已關但帳本還有持倉 {pos:.4g} {coin}，請人工處理")
            lines.append(f"⚪ {name}：關閉")
            continue
        try:
            probs, state, disp_age = check_runs(wf, max_age)
        except Exception as e:
            probs, state, disp_age = [f"GitHub API 查詢失敗：{e}"], "查詢失敗", None
        if wf in ("live_zec_t1.yml", "live_hype_t1.yml"):
            gas_ages.append(disp_age if disp_age is not None else 10**6)

        # 對帳（交易所有讀到才做）
        if ex and not exch_err:
            have = bal.get(coin, 0)
            if pos > 0:
                if have <= 0:
                    probs.append(f"帳本持倉 {pos:.4g} 但交易所沒有 {coin}")
                if not stops.get(coin):
                    probs.append(f"持倉中但交易所【沒有止損單】")
            elif have > 0:
                # 空倉時有手續費零頭屬正常；價值超過 $3 才報（可能是止損觸發沒記帳、或帳本失憶）
                try:
                    val = have * float(ex.fetch_ticker(f"{coin}/USDT")["last"])
                    if val > 3:
                        probs.append(f"帳本空倉但交易所有 {have:.4g} {coin}（約 ${val:.1f}），帳本可能脫鉤")
                except Exception:
                    pass
        halt = pf.get("halt_until", "")
        halt_txt = f"｜🛑停機至{halt[5:16]}" if halt and halt > NOW.isoformat() else ""
        pos_txt = f"持倉 {pos:.4g}@{pf.get('entry_price')}" + (f"｜止損 {stops.get(coin)}" if stops.get(coin) else "") if pos > 0 else "空手"
        perf_txt = f"｜累計 {perf.get('trades',0)}筆 {perf.get('usd',0):+.2f}U" if perf else f"｜歷史 {pf.get('total_trades',0)}筆"
        icon = "🔴" if probs else "🟢"
        lines.append(f"{icon} {name}：{state}｜{pos_txt}{halt_txt}{perf_txt}")
        for p in probs:
            problems.append(f"【{name}】{p}")

    if gas_ages and min(gas_ages) > GAS_MAX_SILENCE_MIN:
        m = min(gas_ages)
        problems.insert(0, "【GAS 派送器】" + (f"已 {fmt_min(m)} 沒有派送" if m < 10**6 else "近期完全沒有派送紀錄")
                        + "。最可能原因：GAS 裡的 GitHub token 到期。處理：GitHub 建新 fine-grained token"
                          "（只開 btc-trading-bot 的 Actions 讀寫）→ GAS 專案 dispatchAll → 指令碼屬性 GH_TOKEN 換掉")

    exp = os.environ.get("GAS_TOKEN_EXPIRES", "")
    if exp:
        try:
            days = (datetime.fromisoformat(exp).replace(tzinfo=TW) - NOW).days
            if days <= 14:
                problems.append(f"【GAS token】{exp} 到期，剩 {days} 天，請提前更換（換完更新 repo 變數 GAS_TOKEN_EXPIRES）")
        except Exception:
            pass
    if exch_err:
        problems.append(exch_err)

    stamp = NOW.astimezone(TW).strftime("%m/%d %H:%M")
    if problems:
        tg(f"🚨【實盤看門狗】{stamp} 發現 {len(problems)} 個問題\n\n" + "\n".join("• " + p for p in problems)
           + "\n\n—— 全部狀態 ——\n" + "\n".join(lines))
    elif MODE == "daily":
        usdt = f"\n💵 主帳戶 USDT：{bal.get('USDT', 0):.2f}" if bal else ""
        tg(f"📋【實盤日報】{stamp} 一切正常\n" + "\n".join(lines) + usdt
           + "\n\n（每天 09:00 左右會收到這份日報；沒收到代表看門狗本身出事）")
    print("\n".join(lines))
    print("問題：", problems or "無")


if __name__ == "__main__":
    import urllib.parse
    main()

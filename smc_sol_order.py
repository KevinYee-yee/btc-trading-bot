"""
SOL 現貨 SMC 夜間單（2026-09-29，Kevin 拍板：主帳號小額 15~50U、只做 SOL 現貨）
由本機監控程式判斷訊號後，用 workflow_dispatch 觸發本腳本下單。
跟 monitor.py 的策略完全獨立，不讀寫任何 portfolio JSON。

ACTION:
  precheck  只讀：列出 USDT / SOL 餘額與 SOL 掛單，不下單
  buy       買入 USDT_AMT 的 SOL，成交後掛 OCO（停損 SL + 止盈 TP）
  sell      撤掉 SOL 所有條件單並市價賣出全部 SOL
"""

import ccxt
import json
import os
import time
import urllib.parse
import urllib.request

SYMBOL  = "SOL/USDT"
INST_ID = "SOL-USDT"
ACTION  = os.environ.get("ACTION", "precheck")
USDT_AMT = float(os.environ.get("USDT_AMT") or 0)
SL = float(os.environ.get("SL") or 0)
TP = float(os.environ.get("TP") or 0)
MIN_RR = 1.2        # 用實際賣一價重算，盈虧比低於此值就放棄（訊號發出後價格已跑掉）
MAX_USDT = 50.0     # Kevin 設定的單筆上限

TG_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TG_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

ex = ccxt.okx({
    "apiKey":   os.environ["OKX_API_KEY"],
    "secret":   os.environ["OKX_SECRET"],
    "password": os.environ["OKX_PASSPHRASE"],
    "enableRateLimit": True,
})


def notify(msg):
    print(msg)
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    try:
        data = urllib.parse.urlencode({"chat_id": TG_CHAT_ID, "text": msg}).encode()
        urllib.request.urlopen(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data, timeout=10)
    except Exception as e:
        print(f"  ⚠️ Telegram 失敗：{e}")


def result(**kw):
    # 本機監控程式從 run log 解析這一行
    print("SMC_RESULT " + json.dumps(kw, ensure_ascii=False))


def balances():
    bal = ex.fetch_balance()
    usdt_free = float(bal.get("USDT", {}).get("free") or 0)
    sol_total = float(bal.get("SOL", {}).get("total") or 0)
    return usdt_free, sol_total


def pending_sol_algos():
    out = []
    for ord_type in ("conditional", "oco"):
        r = ex.private_get_trade_orders_algo_pending({"ordType": ord_type, "instId": INST_ID})
        out += [{"algoId": o["algoId"], "ordType": ord_type,
                 "sl": o.get("slTriggerPx"), "tp": o.get("tpTriggerPx"), "sz": o.get("sz")}
                for o in r.get("data", [])]
    return out


def pending_sol_orders():
    return [{"id": o["id"], "side": o["side"], "price": o["price"], "amount": o["amount"]}
            for o in ex.fetch_open_orders(SYMBOL)]


def cancel_sol_algos():
    for a in pending_sol_algos():
        ex.private_post_trade_cancel_algos([{"algoId": a["algoId"], "instId": INST_ID}])
        print(f"  🧹 撤銷 SOL 條件單 {a['algoId']}（{a['ordType']}）")


def do_precheck():
    usdt_free, sol_total = balances()
    last = float(ex.fetch_ticker(SYMBOL)["last"])
    algos, orders = pending_sol_algos(), pending_sol_orders()
    print(f"USDT 可用 {usdt_free:.2f}｜SOL {sol_total:.6f}（約 {sol_total * last:.2f} USDT）｜SOL 現價 {last}")
    print(f"SOL 條件單 {algos}")
    print(f"SOL 掛單 {orders}")
    result(ok=True, action="precheck", usdt_free=usdt_free, sol=sol_total, last=last,
           algos=algos, orders=orders)


def do_buy():
    if not (0 < USDT_AMT <= MAX_USDT) or not (0 < SL < TP):
        result(ok=False, action="buy", why=f"參數不合法 usdt={USDT_AMT} sl={SL} tp={TP}")
        return
    usdt_free, sol_total = balances()
    ticker = ex.fetch_ticker(SYMBOL)
    ask = float(ticker["ask"] or ticker["last"])

    # 守門：主帳號已經有 SOL 或 SOL 掛單，代表有別的倉位（手動單/其他策略），不疊加
    if sol_total * ask > 3:
        result(ok=False, action="buy", why=f"主帳號已持有 {sol_total:.4f} SOL，不重複進場")
        return
    if pending_sol_algos() or pending_sol_orders():
        result(ok=False, action="buy", why="主帳號已有 SOL 掛單/條件單，不重複進場")
        return
    if usdt_free < USDT_AMT * 1.01:
        result(ok=False, action="buy", why=f"USDT 可用 {usdt_free:.2f} 不足 {USDT_AMT}")
        return
    if not (SL < ask < TP):
        result(ok=False, action="buy", why=f"現價 {ask} 已不在停損 {SL}～止盈 {TP} 之間")
        return
    rr = (TP - ask) / (ask - SL)
    if rr < MIN_RR:
        result(ok=False, action="buy", why=f"以賣一價 {ask} 重算盈虧比 {rr:.2f} < {MIN_RR}，放棄")
        return

    # 可立即成交的限價單（賣一價 +0.2%），比市價單多一層滑價保護
    limit_px = float(ex.price_to_precision(SYMBOL, ask * 1.002))
    qty = float(ex.amount_to_precision(SYMBOL, USDT_AMT / limit_px))
    order = ex.create_order(SYMBOL, "limit", "buy", qty, limit_px)
    filled, avg = 0.0, None
    for _ in range(10):
        time.sleep(1)
        o = ex.fetch_order(order["id"], SYMBOL)
        filled, avg = float(o.get("filled") or 0), o.get("average")
        if o.get("status") == "closed":
            break
    else:
        try:
            ex.cancel_order(order["id"], SYMBOL)
        except Exception:
            pass
    if filled <= 0:
        result(ok=False, action="buy", why="買單 10 秒內未成交，已撤單")
        return
    fill = float(avg or limit_px)

    time.sleep(1)
    _, sol_now = balances()
    sell_qty = float(ex.amount_to_precision(SYMBOL, sol_now))  # 手續費以 SOL 扣，用實際餘額
    protect = "OCO"
    try:
        ex.create_order(SYMBOL, "market", "sell", sell_qty, None,
                        {"stopLossPrice": SL, "takeProfitPrice": TP, "tdMode": "cash"})
    except Exception as e:
        print(f"  ⚠️ OCO 掛單失敗：{e}，改掛單純停損")
        protect = "STOP_ONLY"
        try:
            ex.create_order(SYMBOL, "market", "sell", sell_qty, None,
                            {"stopLossPrice": SL, "tdMode": "cash"})
        except Exception as e2:
            protect = "NONE"
            notify(f"🚨【SOL夜間單】買入成功但停損掛單失敗！請立刻手動處理：{e2}")

    risk = (fill - SL) * sell_qty
    notify(f"🟢【SOL夜間單·實盤】買入 {sell_qty} SOL @ {fill:.2f}（約 {fill * sell_qty:.1f} U）\n"
           f"停損 {SL}｜止盈 {TP}｜盈虧比 {(TP - fill) / (fill - SL):.2f}\n"
           f"最多虧約 {risk:.2f} U｜保護單：{protect}"
           + ("\n⚠️ 只掛到停損，止盈由本機監控程式負責" if protect == "STOP_ONLY" else ""))
    result(ok=True, action="buy", fill=fill, qty=sell_qty, sl=SL, tp=TP, protect=protect)


def do_sell():
    cancel_sol_algos()
    time.sleep(1)
    _, sol_total = balances()
    last = float(ex.fetch_ticker(SYMBOL)["last"])
    if sol_total * last < 1:
        result(ok=True, action="sell", why="沒有可賣的 SOL", sol=sol_total)
        return
    qty = float(ex.amount_to_precision(SYMBOL, sol_total))
    o = ex.create_order(SYMBOL, "market", "sell", qty)
    time.sleep(1)
    o = ex.fetch_order(o["id"], SYMBOL)
    px = float(o.get("average") or last)
    notify(f"⚪【SOL夜間單·實盤】市價賣出 {qty} SOL @ {px:.2f}")
    result(ok=True, action="sell", qty=qty, price=px)


if __name__ == "__main__":
    try:
        {"precheck": do_precheck, "buy": do_buy, "sell": do_sell}[ACTION]()
    except Exception as e:
        notify(f"🚨【SOL夜間單】{ACTION} 執行錯誤：{e}")
        result(ok=False, action=ACTION, why=str(e))
        raise

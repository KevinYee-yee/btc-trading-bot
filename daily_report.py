"""
每日績效報告 — 本地執行版
讀取所有 portfolio JSON，發送 Telegram 通知
設定方式：crontab 每天 01:00 UTC（台北 09:00）執行
"""

import json
import subprocess
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

REPO_DIR   = Path(__file__).parent
TG_TOKEN   = "8731884089:AAEYJc7S6YoUuNeGjlbSjI5TpNvleQNqnyM"
TG_CHAT_ID = "7898079577"

PORTFOLIOS = {
    "A":         "paper_portfolio.json",
    "B":         "paper_portfolio_b.json",
    "C":         "paper_portfolio_c.json",
    "D":         "paper_portfolio_d.json",
    "ETH_B":     "paper_portfolio_eth_b.json",
    "ETH_C":     "paper_portfolio_eth_c.json",
    "SOL_B":     "paper_portfolio_sol_b.json",
    "SOL_C":     "paper_portfolio_sol_c.json",
    "BTC_E":     "paper_portfolio_btc_e.json",
    "BTC_SHORT": "paper_portfolio_short.json",
}


def send_telegram(msg: str):
    data = urllib.parse.urlencode({"chat_id": TG_CHAT_ID, "text": msg}).encode()
    req  = urllib.request.Request(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data=data
    )
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        print(f"Telegram 發送成功 ({resp.status})")
    except Exception as e:
        print(f"Telegram 失敗：{e}")


def fmt_strategy(key: str, p: dict) -> str:
    pnl     = p.get("total_pnl", 0)
    trades  = p.get("total_trades", 0)
    wins    = p.get("wins", 0)
    losses  = p.get("losses", 0)
    pos     = p.get("position", p.get("short_position", 0))
    ep      = p.get("entry_price", 0)
    cl      = p.get("consecutive_losses", 0)

    pnl_pct = pnl / 1000 * 100
    wr      = f"{wins/(wins+losses)*100:.0f}%" if (wins + losses) > 0 else "-"
    status  = f"持倉@${ep:,.0f}" if (pos > 0 and ep > 0) else "空倉"
    breaker = " 🔴熔斷" if cl >= 3 else ""
    arrow   = "▲" if pnl_pct > 0 else ("▼" if pnl_pct < 0 else "─")

    return f"{key}: {arrow}{pnl_pct:+.1f}% | {trades}筆 | 勝{wr} | {status}{breaker}"


def git_pull():
    try:
        result = subprocess.run(
            ["git", "pull", "--rebase", "origin", "main"],
            cwd=REPO_DIR, capture_output=True, text=True, timeout=30
        )
        print(result.stdout.strip())
    except Exception as e:
        print(f"git pull 失敗（略過）：{e}")


def main():
    git_pull()

    now_tw = datetime.now(timezone.utc)
    date_str = now_tw.strftime("%m/%d")

    lines = [f"📊 AI交易每日報告 {date_str} 台北09:00\n"]

    total_pnl = 0.0
    for key, fname in PORTFOLIOS.items():
        fpath = REPO_DIR / fname
        try:
            p = json.loads(fpath.read_text())
            lines.append(fmt_strategy(key, p))
            total_pnl += p.get("total_pnl", 0)
        except FileNotFoundError:
            lines.append(f"{key}: 無資料")

    lines.append(f"\n總損益（10策略合計）：{total_pnl:+.1f} USDT")

    msg = "\n".join(lines)
    print(msg)
    send_telegram(msg)


if __name__ == "__main__":
    main()

"""Winrate tracker — baca trades.jsonl, hitung winrate posisi yang close kena TP/SL."""
import json, sys
from pathlib import Path

TRADES_LOG = Path(__file__).parent / "trades.jsonl"

def main():
    trades = [json.loads(l) for l in TRADES_LOG.open() if l.strip()] if TRADES_LOG.exists() else []
    closed = [t for t in trades if t.get("status") == "CLOSED" and t.get("result") in ("WIN", "LOSS")]
    manual = [t for t in trades if t.get("status") == "CLOSED" and t.get("result") == "MANUAL"]

    print(f"trades.jsonl: {len(trades)} record | CLOSED WIN/LOSS: {len(closed)} | MANUAL(reset): {len(manual)}")
    if not closed:
        print("Belum ada posisi close kena TP/SL — winrate belum bisa dihitung.")
        return

    wins = sum(1 for t in closed if t["result"] == "WIN")
    losses = len(closed) - wins
    pnl = sum(t.get("pnl_usdt", 0) for t in closed)
    wr = wins / len(closed) * 100

    print(f"\nWinrate: {wins}W / {losses}L = {wr:.1f}%")
    print(f"Total PnL (closed TP/SL): {pnl:+.4f} USDT")
    print(f"\n{'ASSET':<10} {'ACTION':<6} {'ENTRY':>10} {'EXIT':>10} {'RESULT':<7} {'PNL':>10}")
    for t in closed:
        print(f"{t['asset']:<10} {t['action']:<6} {t['entry_price']:>10.6g} {t.get('exit_price',0):>10.6g} {t['result']:<7} {t.get('pnl_usdt',0):>+10.4f}")

if __name__ == "__main__":
    main()

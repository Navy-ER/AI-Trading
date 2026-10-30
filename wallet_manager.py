import os
import json
import time
from datetime import datetime

WALLET_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "simulated_wallet.json")
INITIAL_IDR = 250000.0
USDT_RATE = 16000.0
INITIAL_CAPITAL = INITIAL_IDR / USDT_RATE  # 15.625 USDT

def init_wallet(force_reset=False):
    if not os.path.exists(WALLET_FILE) or force_reset:
        data = {
            "initial_idr": INITIAL_IDR,
            "usdt_rate": USDT_RATE,
            "initial_capital": INITIAL_CAPITAL,
            "cash_balance": INITIAL_CAPITAL,
            "realized_pnl": 0.0,
            "total_trades": 0,
            "wins": 0,
            "losses": 0,
            "start_time": datetime.now().isoformat(),
            "target_end_time": datetime.fromtimestamp(time.time() + 48 * 3600).isoformat(),
            "last_updated": datetime.now().isoformat()
        }
        with open(WALLET_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        return data
    try:
        with open(WALLET_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return init_wallet(force_reset=True)

def get_wallet():
    return init_wallet(force_reset=False)

def deduct_for_open(size_usdt):
    w = get_wallet()
    if w["cash_balance"] < size_usdt:
        return False, w["cash_balance"]
    w["cash_balance"] = round(w["cash_balance"] - size_usdt, 4)
    w["last_updated"] = datetime.now().isoformat()
    with open(WALLET_FILE, "w", encoding="utf-8") as f:
        json.dump(w, f, indent=2)
    return True, w["cash_balance"]

def credit_for_close(size_usdt, pnl_usdt, is_win):
    w = get_wallet()
    # Kas dikembalikan: pokok + laba/rugi
    returned = max(0.0, size_usdt + pnl_usdt)
    w["cash_balance"] = round(w["cash_balance"] + returned, 4)
    w["realized_pnl"] = round(w["realized_pnl"] + pnl_usdt, 4)
    w["total_trades"] = w.get("total_trades", 0) + 1
    if is_win:
        w["wins"] = w.get("wins", 0) + 1
    else:
        w["losses"] = w.get("losses", 0) + 1
    w["last_updated"] = datetime.now().isoformat()
    with open(WALLET_FILE, "w", encoding="utf-8") as f:
        json.dump(w, f, indent=2)
    return w

def get_wallet_summary(open_positions):
    w = get_wallet()
    in_trade = sum(p.get("size_usdt", 0.0) for p in open_positions)
    floating = sum(p.get("floating_pnl", 0.0) for p in open_positions)
    equity = w["cash_balance"] + in_trade + floating
    roi_pct = ((equity - w["initial_capital"]) / w["initial_capital"]) * 100
    equity_idr = equity * w["usdt_rate"]
    pnl_idr = (equity - w["initial_capital"]) * w["usdt_rate"]
    return {
        "initial_capital": w["initial_capital"],
        "initial_idr": w["initial_idr"],
        "cash_balance": w["cash_balance"],
        "in_trade": round(in_trade, 4),
        "floating_pnl": round(floating, 4),
        "total_equity": round(equity, 4),
        "equity_idr": round(equity_idr, 0),
        "pnl_idr": round(pnl_idr, 0),
        "roi_pct": round(roi_pct, 2),
        "wins": w.get("wins", 0),
        "losses": w.get("losses", 0),
        "total_trades": w.get("total_trades", 0),
        "start_time": w.get("start_time"),
        "target_end_time": w.get("target_end_time"),
    }

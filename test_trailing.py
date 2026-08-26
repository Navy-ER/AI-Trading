import sys
sys.path.insert(0, "C:/Users/reyvan/pixel-agents")
import bridge

def t(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        raise SystemExit(1)

# BUY: ratchet up, never down
p = {"action": "BUY", "asset": "TESTUSDT", "entry_price": 100.0, "sl": 99.0, "trail_dist": 1.0}
bridge.update_trailing_stop(p, 102.0)
t("BUY best tracked", p["best_price"] == 102.0)
t("BUY SL ratchet", p["sl"] == 101.0)
bridge.update_trailing_stop(p, 103.0)
t("BUY SL ratchet 2", p["sl"] == 102.0)
bridge.update_trailing_stop(p, 101.0)
t("BUY never widen", p["sl"] == 102.0)
t("BUY best stays", p["best_price"] == 103.0)

# SELL: ratchet down, never up
p = {"action": "SELL", "asset": "TESTUSDT", "entry_price": 100.0, "sl": 101.0, "trail_dist": 1.0}
bridge.update_trailing_stop(p, 98.0)
t("SELL best tracked", p["best_price"] == 98.0)
t("SELL SL ratchet", p["sl"] == 99.0)
bridge.update_trailing_stop(p, 97.0)
t("SELL SL ratchet 2", p["sl"] == 98.0)
bridge.update_trailing_stop(p, 99.5)
t("SELL never widen", p["sl"] == 98.0)

# Legacy position tanpa trail_dist -> init dari |sl-entry|
p = {"action": "BUY", "asset": "LEGACY", "entry_price": 10.0, "sl": 9.5}
bridge.update_trailing_stop(p, 10.8)
t("legacy trail init", p["trail_dist"] == 0.5)
t("legacy SL ratchet", p["sl"] == 10.3)

# Toggle off
bridge.TRAIL_ENABLED = False
p = {"action": "BUY", "asset": "OFF", "entry_price": 100.0, "sl": 99.0, "trail_dist": 1.0}
bridge.update_trailing_stop(p, 105.0)
t("disabled no-op", p["sl"] == 99.0 and "best_price" not in p)
bridge.TRAIL_ENABLED = True

# check_open_positions exit label: SL hit dengan pnl > 0 -> WIN
import bridge as b
prices = {"TRAILWIN": 101.5}
pos = {"opened_at": "x", "action": "BUY", "asset": "TRAILWINUSDT", "entry_price": 100.0,
       "tp": 110.0, "sl": 101.0, "trail_dist": 1.0, "size_usdt": 1.2}
import os
open_log = b.OPEN_POS_LOG
tmp = open_log + ".testtmp"
os.replace(open_log, tmp) if os.path.exists(open_log) else None
try:
    b.append_jsonl(open_log, pos)
    # Call 1: price 103 -> SL ratchet ke 102, posisi tetap open
    n, f = b.check_open_positions({"TRAILWIN": 103.0})
    t("trailwin open after ratchet", n == 1)
    # Call 2: price turun ke 102 -> kena SL trailing, profit -> WIN
    n, f = b.check_open_positions({"TRAILWIN": 102.0})
    t("trailwin closed", n == 0)
    trades = b.read_jsonl(b.TRADES_LOG)
    last = trades[-1]
    t("trailwin result WIN", last["result"] == "WIN")
    t("trailwin pnl positive", last["pnl_usdt"] > 0)
finally:
    os.remove(open_log) if os.path.exists(open_log) else None
    os.replace(tmp, open_log) if os.path.exists(tmp) else None
    # bersihin trade test dari trades log
    with open(b.TRADES_LOG, "r") as f:
        lines = [l for l in f if "TRAILWIN" not in l]
    with open(b.TRADES_LOG, "w") as f:
        f.writelines(lines)

print("ALL TESTS PASSED")

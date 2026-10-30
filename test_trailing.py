import sys
sys.path.insert(0, "C:/Users/reyvan/pixel-agents")
import bridge
from unittest.mock import MagicMock, patch
bridge.log = lambda *a, **k: None

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
import tempfile
prices = {"TRAILWIN": 101.5}
pos = {"opened_at": "x", "action": "BUY", "asset": "TRAILWINUSDT", "entry_price": 100.0,
       "tp": 110.0, "sl": 101.0, "trail_dist": 1.0, "size_usdt": 1.2, "mode": "LIVE",
       "quantity": 0.1, "entry_filled_qty": 0.1, "entry_order_id": "entry-1", "sl_order_id": "sl-1"}
import os
with tempfile.TemporaryDirectory() as temp_dir:
    open_log = os.path.join(temp_dir, "open_positions.live.jsonl")
    trades_log = os.path.join(temp_dir, "trades.jsonl")
    b.OPEN_POS_LOGS = {"DRY_RUN": os.path.join(temp_dir, "open_positions.dry_run.jsonl"), "LIVE": open_log}
    b.LEGACY_POSITION_LOGS = []
    b.TRADES_LOG = trades_log
    assert all(os.path.commonpath([temp_dir, path]) == temp_dir for path in b.OPEN_POS_LOGS.values())
    assert os.path.commonpath([temp_dir, b.TRADES_LOG]) == temp_dir
    b.append_jsonl(open_log, pos)
    with patch("execution.trading_mode", return_value="LIVE"), \
         patch.object(b.tko_exec, "get_exchange", return_value=MagicMock()), \
         patch.object(b.tko_exec, "cancel_exchange_order", return_value=True), \
         patch.object(b.tko_exec, "place_stop_loss", return_value="sl-new") as place_sl, \
         patch.object(b.tko_exec, "execute_trade", return_value={"id": "fake-order"}), \
         patch.object(b, "send_discord_webhook"), \
         patch.object(b, "record_realised_loss"):
        # Call 1: price 103 -> SL ratchet ke 102, posisi tetap open
        n, f = b.check_open_positions({"TRAILWIN": 103.0})
        t("trailwin open after ratchet", n == 1)
        t("live exchange stop updated", place_sl.call_count == 1 and place_sl.call_args.args[3] == 0.1)
        # Call 2: stop trigger is not evidence of a stop fill; quarantine for reconciliation.
        n, f = b.check_open_positions({"TRAILWIN": 102.0})
        t("stop trigger quarantined", n == 1)
        open_positions = b.read_jsonl(open_log)
        t("stop trigger requires reconciliation", open_positions[0]["reconciliation_required"] is True)

print("ALL TESTS PASSED")

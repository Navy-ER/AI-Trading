"""Test logika SL/TP ATR(14) di add_position — murni, tanpa network."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bridge
os.environ["TKO_DRY_RUN"] = "true"
bridge.read_position_records = lambda mode=None: []
bridge.LEGACY_POSITION_LOGS = []
assert bridge.read_position_records() == []  # fixture bypasses repository position files
bridge.log = lambda *a, **k: None

F = []
def check(name, got, want):
    ok = got == want
    F.append(ok)
    print(f"{'PASS' if ok else 'FAIL'} {name}: got={got} want={want}")

captured = []
bridge.append_position_record = lambda mode, obj: captured.append(dict(obj, mode=mode)) or True
bridge.append_jsonl = lambda path, obj: None
simulated_fill = {"source": "DRY_RUN_SIMULATOR", "classification": "SIMULATED_FILL",
                  "filled_qty": 0.012, "average_fill_price": 100.0, "quote_qty": 1.2}

# BUY @100, ATR=2 -> TP=103.0 (1.5xATR), SL=98.0 (1.0xATR)
bridge.get_atr = lambda symbol, period=14: 2.0
bridge.add_position({"action": "BUY", "asset": "SOLUSDT"}, 100.0, fill=simulated_fill)
check("buy tp/sl ATR", (captured[-1]["tp"], captured[-1]["sl"]), (103.0, 98.0))

# SELL @100 -> TP=97.0, SL=102.0
bridge.add_position({"action": "SELL", "asset": "SOLUSDT"}, 100.0, fill=simulated_fill)
check("sell tp/sl ATR", (captured[-1]["tp"], captured[-1]["sl"]), (97.0, 102.0))

# LLM kasih TP/SL -> DIABAIKAN, tetap pakai ATR
bridge.add_position({"action": "BUY", "asset": "SOLUSDT", "take_profit": 110.0, "stop_loss": 90.0}, 100.0, fill=simulated_fill)
check("llm tp/sl diabaikan", (captured[-1]["tp"], captured[-1]["sl"]), (103.0, 98.0))

# AI TP/SL ngawur -> tetap pakai ATR (tidak ada validasi lagi)
bridge.get_atr = lambda symbol, period=14: 2.0
bridge.add_position({"action": "BUY", "asset": "SOLUSDT", "take_profit": 0.64, "stop_loss": 0.53}, 100.0, fill=simulated_fill)
check("ai tp/sl ngawur tetap ATR", (captured[-1]["tp"], captured[-1]["sl"]), (103.0, 98.0))

# ATR gagal (None) -> fallback flat +0.8%/-0.5%
bridge.get_atr = lambda symbol, period=14: None
bridge.add_position({"action": "BUY", "asset": "SOLUSDT"}, 100.0, fill=simulated_fill)
check("fallback flat tp/sl", (captured[-1]["tp"], captured[-1]["sl"]), (100.866, 99.566))

print(f"\n{sum(F)}/{len(F)} PASS")
sys.exit(0 if all(F) else 1)

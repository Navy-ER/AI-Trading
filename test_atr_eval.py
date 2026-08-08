"""Test logika SL/TP ATR(14) di add_position — murni, tanpa network."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bridge

F = []
def check(name, got, want):
    ok = got == want
    F.append(ok)
    print(f"{'PASS' if ok else 'FAIL'} {name}: got={got} want={want}")

captured = []
bridge.append_jsonl = lambda path, obj: captured.append(obj)

# BUY @100, ATR=2 -> TP=103.0 (1.5xATR), SL=98.0 (1.0xATR)
bridge.get_atr = lambda symbol, period=14: 2.0
bridge.add_position({"action": "BUY", "asset": "SOLUSDT"}, 100.0)
check("buy tp/sl ATR", (captured[-1]["tp"], captured[-1]["sl"]), (103.0, 98.0))

# SELL @100 -> TP=97.0, SL=102.0
bridge.add_position({"action": "SELL", "asset": "SOLUSDT"}, 100.0)
check("sell tp/sl ATR", (captured[-1]["tp"], captured[-1]["sl"]), (97.0, 102.0))

# LLM kasih TP/SL -> DIABAIKAN, tetap pakai ATR
bridge.add_position({"action": "BUY", "asset": "SOLUSDT", "take_profit": 110.0, "stop_loss": 90.0}, 100.0)
check("llm tp/sl diabaikan", (captured[-1]["tp"], captured[-1]["sl"]), (103.0, 98.0))

# AI TP/SL ngawur -> tetap pakai ATR (tidak ada validasi lagi)
bridge.get_atr = lambda symbol, period=14: 2.0
bridge.add_position({"action": "BUY", "asset": "SOLUSDT", "take_profit": 0.64, "stop_loss": 0.53}, 100.0)
check("ai tp/sl ngawur tetap ATR", (captured[-1]["tp"], captured[-1]["sl"]), (103.0, 98.0))

# ATR gagal (None) -> fallback flat +0.8%/-0.5%
bridge.get_atr = lambda symbol, period=14: None
bridge.add_position({"action": "BUY", "asset": "SOLUSDT"}, 100.0)
check("fallback flat tp/sl", (captured[-1]["tp"], captured[-1]["sl"]), (100.8, 99.5))

print(f"\n{sum(F)}/{len(F)} PASS")
sys.exit(0 if all(F) else 1)

"""Test logika guard execute_tko_trade — tanpa network, tanpa nulis file."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bridge

bridge.TKO_DRY_RUN = True
bridge.TKO_CONFIDENCE_MIN = 80
bridge.log = lambda *a, **k: None
logged = []
bridge.append_jsonl = lambda path, obj: logged.append(obj)

F = []
def check(name, got, want):
    ok = got == want
    F.append(ok)
    print(f"{'PASS' if ok else 'FAIL'} {name}: got={got} want={want}")

def run(sig, price=100.0):
    return bridge.execute_tko_trade(sig, price)

# T1: NEUTRAL -> skip
check("neutral skip", run({"action": "NEUTRAL", "asset": "BTCUSDT", "confidence": "90%"}), None)

# T2: confidence < min -> skip, tidak di-log
n0 = len(logged)
check("conf rendah skip", run({"action": "BUY", "asset": "BTCUSDT", "confidence": "10%"}), None)
check("conf rendah tak di-log", len(logged) == n0, True)

# T3: confidence >= min + dry-run -> dict dry_run True
check("dry-run lolos", run({"action": "BUY", "asset": "BTCUSDT", "confidence": "90%"}), {"action": "BUY", "dry_run": True})

# T4: dry-run nulis trades.jsonl (status DRY_RUN)
check("trades di-log", logged[-1]["status"], "DRY_RUN")

# T5: confidence tanpa angka -> conf 0 -> skip
n1 = len(logged)
check("conf non-angka skip", run({"action": "SELL", "asset": "BTCUSDT", "confidence": "N/A"}), None)
check("conf non-angka tak di-log", len(logged) == n1, True)

# T6: Max Position Guard - 1 posisi per aset
bridge.read_jsonl = lambda path: [{"asset": "ADAUSDT"}, {"asset": "SOLUSDT"}]
check("guard: aset dgn posisi aktif", bridge.has_open_position("ADAUSDT"), True)
check("guard: aset tanpa posisi", bridge.has_open_position("BTCUSDT"), False)

print(f"\n{sum(F)}/{len(F)} PASS")
sys.exit(0 if all(F) else 1)

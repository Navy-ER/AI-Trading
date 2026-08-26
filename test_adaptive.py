"""Test adaptive confidence floor + retry-path fixes."""
import sys
sys.path.insert(0, "C:/Users/reyvan/pixel-agents")
import bridge

bridge.append_jsonl = lambda *a, **k: None
bridge.tko_exec.estimate_position_size = lambda price, conf=0: 1.2

fails = 0

def check(label, got, want):
    global fails
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'} {label}: {got!r}" + ("" if ok else f" != {want!r}"))
    if not ok:
        fails += 1

# ── adaptive_conf_min bands ───────────────────────────────────────────────
check("adx=None", bridge.adaptive_conf_min(None), 80)
check("adx=10 weak", bridge.adaptive_conf_min(10), 80)
check("adx=15 weak", bridge.adaptive_conf_min(15), 80)
check("adx=19.9 weak", bridge.adaptive_conf_min(19.9), 80)
check("adx=20 moderate", bridge.adaptive_conf_min(20), 75)
check("adx=25 moderate", bridge.adaptive_conf_min(25), 75)
check("adx=29.9 moderate", bridge.adaptive_conf_min(29.9), 75)
check("adx=30 strong", bridge.adaptive_conf_min(30), 70)
check("adx=40 strong", bridge.adaptive_conf_min(40), 70)
check("adx=49.9 strong", bridge.adaptive_conf_min(49.9), 70)
check("adx=50 extreme", bridge.adaptive_conf_min(50), 85)
check("adx=65 extreme", bridge.adaptive_conf_min(65), 85)

# ── execute_tko_trade uses dynamic floor ────────────────────────────────────
sig1 = {"action": "BUY", "asset": "XRPUSDT", "confidence": "75%", "adx": 35}
check("conf=75 ADX=35 strong → proceed (floor=70)",
      bridge.execute_tko_trade(sig1, 1.0) is not None, True)

sig2 = {"action": "BUY", "asset": "XRPUSDT", "confidence": "75%", "adx": 15}
check("conf=75 ADX=15 weak → skip (floor=80)",
      bridge.execute_tko_trade(sig2, 1.0), None)

sig3 = {"action": "BUY", "asset": "XRPUSDT", "confidence": "82%", "adx": 55}
check("conf=82 ADX=55 extreme → skip (floor=85)",
      bridge.execute_tko_trade(sig3, 1.0), None)

sig4 = {"action": "BUY", "asset": "XRPUSDT", "confidence": "88%", "adx": 55}
check("conf=88 ADX=55 extreme → proceed (floor=85)",
      bridge.execute_tko_trade(sig4, 1.0) is not None, True)

sig5 = {"action": "BUY", "asset": "XRPUSDT", "confidence": "78%", "adx": None}
check("conf=78 adx=None → skip (floor=80)",
      bridge.execute_tko_trade(sig5, 1.0), None)

sig6 = {"action": "BUY", "asset": "XRPUSDT", "confidence": "80%", "adx": None}
check("conf=80 adx=None → proceed (floor=80)",
      bridge.execute_tko_trade(sig6, 1.0) is not None, True)

# ── dry_run still works ─────────────────────────────────────────────────────
sig7 = {"action": "BUY", "asset": "XRPUSDT", "confidence": "90%", "adx": 30}
result = bridge.execute_tko_trade(sig7, 1.0)
check("dry_run returns dict", isinstance(result, dict), True)
check("dry_run has dry_run=True", result.get("dry_run"), True)

# ── NEUTRAL skipped ─────────────────────────────────────────────────────────
sig8 = {"action": "NEUTRAL", "asset": "XRPUSDT", "confidence": "95%", "adx": 50}
check("NEUTRAL skipped", bridge.execute_tko_trade(sig8, 1.0), None)

print(f"\n{'ALL PASS' if not fails else f'{fails} FAIL'}")
sys.exit(fails)

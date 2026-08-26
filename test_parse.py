# Test _normalize_base + _parse_ai_json dari bridge.py (kasus gagal dari log)
import sys
sys.path.insert(0, "C:/Users/reyvan/pixel-agents")
import bridge

fails = 0

def check(label, got, want):
    global fails
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'} {label}: {got!r}" + ("" if ok else f" != {want!r}"))
    if not ok:
        fails += 1

# _normalize_base — semua bentuk sampah dari log AI
check("MUB-USDT", bridge._normalize_base("MUB-USDT"), "MUB")
check("DOGEUSD", bridge._normalize_base("DOGEUSD"), "DOGE")
check("bnb123", bridge._normalize_base("bnb123"), "BNB")
check("SOLX1024", bridge._normalize_base("SOLX1024"), "SOLX")
check("XRPUSDT", bridge._normalize_base("XRPUSDT"), "XRP")
check("kosong", bridge._normalize_base(""), "")
check("cuma USDT", bridge._normalize_base("USDT"), "")
check("sampah", bridge._normalize_base("###"), "")

# _parse_ai_json — markdown fence (echo contoh JSON), prose, truncated
check("fence", bridge._parse_ai_json('```json\n{"action":"BUY","asset":"MUBUSDT","confidence":"75%","reason":"x"}\n```'),
      {"action": "BUY", "asset": "MUBUSDT", "confidence": "75%", "reason": "x"})
check("prose", bridge._parse_ai_json('Berikut sinyal: {"action":"SELL","asset":"DOGEUSD","entry_zone":0.15} semoga membantu'),
      {"action": "SELL", "asset": "DOGEUSD", "entry_zone": 0.15})
check("full", bridge._parse_ai_json('{"action":"BUY","asset":"XRPUSDT","entry_zoe":1.0649,"take_profit":1.07,"stop_loss":1.06,"rrr":"1:2","confidence":"75%","reason":"alasan"}'),
      {"action": "BUY", "asset": "XRPUSDT", "entry_zoe": 1.0649, "take_profit": 1.07, "stop_loss": 1.06, "rrr": "1:2", "confidence": "75%", "reason": "alasan"})
check("regex fallback", bridge._parse_ai_json('action=BUY asset=MUBUSDT confidence=80% entry=0.5'),
      {"action": "BUY", "asset": "MUBUSDT", "confidence": "80%", "entry_zone": 0.5})
check("echo 2 blok", bridge._parse_ai_json('Contoh benar: {"action":"BUY","asset":"XRPUSDT","confidence":"75%"} dan jawaban: {"action":"SELL","asset":"DOGEUSD","confidence":"80%"}'),
      {"action": "SELL", "asset": "DOGEUSD", "confidence": "80%"})
check("tidak ada json", bridge._parse_ai_json("maaf saya tidak bisa membantu"), None)

print(f"\n{'ALL PASS' if fails == 0 else str(fails) + ' FAIL'}")
sys.exit(1 if fails else 0)

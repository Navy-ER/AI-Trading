"""Smoke test: bridge.py AI signal path (Ollama qwen2.5:1.5b).
Mirrors bridge.py get_ai_signal payload + timeout=(60,180).
Exit 0 = PASS, nonzero = FAIL.
"""
import json
import re
import sys
import time

import requests

URL = "http://127.0.0.1:11434/api/generate"
MODEL = "qwen2.5:1.5b"
TIMEOUT = (60, 180)

prompt = (
    'Kamu analis pasar crypto. Pilih SATU koin dari list: BTCUSDT, SOLUSDT, XRPUSDT, DOGEUSDT. '
    'Output HANYA JSON tanpa markdown: '
    '{"action":"BUY|SELL","asset":"COINUSDT","confidence":"xx%","reason":"singkat"}'
)

payload = {
    "model": MODEL,
    "prompt": prompt,
    "stream": False,
    "keep_alive": "10m",
    "options": {"temperature": 0.3, "num_predict": 150},
}

t0 = time.time()
r = requests.post(URL, json=payload, timeout=TIMEOUT)
elapsed = time.time() - t0
r.raise_for_status()
match = re.search(r"\{.*\}", r.json().get("response", ""), re.DOTALL)
if not match:
    print(f"FAIL: no JSON in response: {r.json().get('response', '')[:200]!r}")
    sys.exit(1)
sig = json.loads(match.group(0))
assert sig.get("action") in ("BUY", "SELL"), f"bad action: {sig}"
assert sig.get("asset", "").endswith("USDT"), f"bad asset: {sig}"
print(f"PASS: model={MODEL} elapsed={elapsed:.1f}s action={sig['action']} "
      f"asset={sig['asset']} conf={sig.get('confidence')}")

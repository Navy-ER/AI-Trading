#!/usr/bin/env python3
"""
Surgical patch for bridge.py - exact string replacements for compressed format
"""
import re

with open("C:/Users/reyvan/pixel-agents/bridge.py", "r", encoding="utf-8") as f:
    content = f.read()

print(f"Original length: {len(content)}")

# ============================================================
# PATCH 1: Fix import execution line (line 4)
# ============================================================
old_import = 'import execution tko_exec\n\n# Constants'
new_import = 'import execution as tko_exec\n\n# Robust HTTP client (handles 502, markdown, JSON extraction)\nfrom robust_http import call_ai_signal, extract_json_from_text\n\n# Constants'
content = content.replace(old_import, new_import)
print(f"After import patch: {len(content)}")

# ============================================================
# PATCH 2: Add OMNIROUTER_URL and AI_MODEL constants (after NINJA_ROUTER_URL)
# ============================================================
old_const = 'NINJA_ROUTER_URL= "http://127.0.0.1:11434"\nDISCORD_WEBHOOK_URL='
new_const = '''# OmniRouter OpenAI-compatible endpoint (Cloudflare Tunnel -> Termux OmniRoute)
OMNIROUTER_URL=os.getenv("OMNIROUTER_URL", "https://your-tunnel.trycloudflare.com/v1/chat/completions")
# Fallback: local Ollama (if tunnel down)
NINJA_ROUTER_URL= "http://127.0.0.1:11434"
AI_MODEL=os.getenv("AI_MODEL", "gratis-3")
DISCORD_WEBHOOK_URL='''
content = content.replace(old_const, new_const)
print(f"After constants patch: {len(content)}")

# ============================================================
# PATCH 3: Replace the HTTP call block in get_ai_signal
# Find the exact block starting from line 356
# ============================================================
# The exact pattern in the file (compressed format)
old_http = ''' url f"{NINJA_ROUTER_URL}/api/generate"
 attempt range(3):
 prompt
 attempt 1 last_asset:
 f"\\nPERINGATAN: {last_asset} SUDAH dipilih pada sinyal terakhir. Pilih koin LAIN selain {last_asset}."
 payload {
 "model": MODEL,
 "prompt": p,
 "stream": False,
 "keep_alive": "10m",
 "options": {"temperature":0.7"top_p":0.9"top_k": 40, "repeat_penalty":1.3"num_predict": 150, "seed":random.randint(1, 99999)}
 }
 requests.post(url, json=payload, timeout=(60, 180))
 r.raise_for_status()
 data r.json()
 content data.get("response", "").strip()
 sig _parse_ai_json(content)
 not sig:
 continue
 base _normalize_base(sig.get("asset", ""))
 not base:
 log("[AI] No valid asset in response, retry")
 continue
 "confidence" not sig not re.search(r"\\d", str(sig.get("confidence", ""))):
 log("[AI] No confidence in signal, retry")
 continue
 base not market_data:
 log(f"[AI] Asset {sig.get('asset')}' not top 20, retrying")
 continue
 last_asset base last_asset:
 log(f"[AI] {base} sama dgn sinyal terakhir, retry rotasi")
 continue
 excluded_assets base excluded_assets:
 log(f"[AI] {base} ada excluded (posisi terbuka/terakhir) retry rotasi")
 continue
 sig["asset"] f"{base}USDT"
 "entry_zoe" sig "entry_zone" not sig:
 sig["entry_zone"] sig.pop("entry_zoe")
 return sig
 return None
 except Exception e:
 log(f"[AI] Error: {e}")
 return None'''

new_http = ''' url = OMNIROUTER_URL
 omni_api_key = os.getenv("OMNIROUTER_API_KEY", "")
 attempt = 0
 while attempt < 3:
     p = prompt
     if attempt == 0 and last_asset:
         p += f"\\nPERINGATAN: {last_asset} SUDAH dipilih pada sinyal terakhir. Pilih koin LAIN selain {last_asset}."
     payload = {
         "model": AI_MODEL,
         "messages": [
             {"role": "user", "content": p},
         ],
         "max_tokens": 500,
         "temperature": 0.7,
         "top_p": 0.9,
     }
     headers = {"Content-Type": "application/json"}
     if omni_api_key:
         headers["Authorization"] = f"Bearer {omni_api_key}"
     try:
         # Use robust HTTP client with 502 handling, JSON extraction, retry
         sig, raw_content = call_ai_signal(url, payload, headers, max_retries=1)
         if not sig:
             log(f"[AI] No valid signal extracted on attempt {attempt + 1}: {raw_content[:200] if raw_content else 'unknown'}")
             attempt += 1
             continue
         base = _normalize_base(sig.get("asset", ""))
         if not base:
             log("[AI] No valid asset in response, retry")
             attempt += 1
             continue
         if "confidence" not in sig or not re.search(r"\\d", str(sig.get("confidence", ""))):
             log("[AI] No confidence in signal, retry")
             attempt += 1
             continue
         if base not in market_data:
             log(f"[AI] Asset {sig.get('asset')} not top 20, retrying")
             attempt += 1
             continue
         if base == last_asset:
             log(f"[AI] {base} sama dengan sinyal terakhir, retry rotasi")
             attempt += 1
             continue
         if excluded_assets and base in excluded_assets:
             log(f"[AI] {base} ada excluded (posisi terbuka/terakhir) retry rotasi")
             attempt += 1
             continue
         sig["asset"] = f"{base}USDT"
         # handle typo entry_zoe
         if "entry_zone" not in sig and "entry_zoe" in sig:
             sig["entry_zone"] = sig.pop("entry_zoe")
         return sig
     except requests.exceptions.Timeout:
         log(f"[AI] Timeout on attempt {attempt + 1}")
         attempt += 1
         continue
     except Exception as e:
         log(f"[AI] Error on attempt {attempt + 1}: {e}")
         attempt += 1
         continue
 return None
 except Exception e:
 log(f"[AI] Error: {e}")
 return None'''

if old_http in content:
    content = content.replace(old_http, new_http)
    print("✓ HTTP block replaced")
else:
    print("✗ HTTP block NOT found exactly - checking variations...")
    # Try without the extra space in "attempt range(3):"
    old_http2 = old_http.replace(' attempt range(3):', ' attemptrange(3):')
    if old_http2 in content:
        content = content.replace(old_http2, new_http)
        print("✓ HTTP block replaced (attemptrange variant)")
    else:
        # Search for partial match
        idx = content.find('url f"{NINJA_ROUTER_URL}/api/generate"')
        if idx >= 0:
            print(f"  Found at position {idx}")
            print(f"  Context: {content[idx:idx+200]}")
        else:
            print("  Not found at all")

# Write back
with open("C:/Users/reyvan/pixel-agents/bridge.py", "w", encoding="utf-8") as f:
    f.write(content)

print(f"Final length: {len(content)}")

# Verify
with open("C:/Users/reyvan/pixel-agents/bridge.py", "r", encoding="utf-8") as f:
    verify = f.read()

checks = [
    ('import execution as tko_exec', 'execution import fixed'),
    ('from robust_http import call_ai_signal', 'robust_http import added'),
    ('OMNIROUTER_URL=os.getenv', 'OMNIROUTER_URL constant added'),
    ('AI_MODEL=os.getenv', 'AI_MODEL constant added'),
    ('call_ai_signal', 'call_ai_signal used'),
    ('while attempt < 3:', 'while loop added'),
    ('sig, raw_content = call_ai_signal', 'robust call used'),
]

print("\nVerification:")
for pattern, desc in checks:
    if pattern in verify:
        print(f"  ✓ {desc}")
    else:
        print(f"  ✗ {desc}")

# Syntax check
import subprocess
result = subprocess.run(['python', '-m', 'py_compile', 'C:/Users/reyvan/pixel-agents/bridge.py'], 
                       capture_output=True, text=True)
if result.returncode == 0:
    print("\n✓ Syntax OK")
else:
    print(f"\n✗ Syntax ERROR:")
    print(result.stderr[:500])
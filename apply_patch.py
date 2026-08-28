#!/usr/bin/env python3
"""
Comprehensive patch for bridge.py:
1. Fix imports (execution as tko_exec, add robust_http)
2. Update API endpoint to OmniRouter via Cloudflare Tunnel
3. Replace get_ai_signal HTTP call with robust HTTP client
4. Keep all existing parsing logic (_parse_ai_json, _normalize_base, fuzzy matching)
"""

import re

# Read current bridge.py
with open("C:/Users/reyvan/pixel-agents/bridge.py", "r", encoding="utf-8") as f:
    content = f.read()

# ============================================================
# 1. FIX IMPORTS
# ============================================================
# Fix: import execution tko_exec -> import execution as tko_exec
content = content.replace(
    'import execution tko_exec',
    'import execution as tko_exec\n\n# Robust HTTP client (handles 502, markdown, JSON extraction)\nfrom robust_http import call_ai_signal, extract_json_from_text'
)

# ============================================================
# 2. UPDATE CONSTANTS - Add OMNIROUTER_URL, keep NINJA_ROUTER_URL as fallback
# ============================================================
# Find the NINJA_ROUTER_URL line and add OMNIROUTER_URL after it
old_constants = '''NINJA_ROUTER_URL= "http://127.0.0.1:11434"
DISCORD_WEBHOOK_URL= "https://discord.com/api/webhooks/1532938091331977337/EQV4Kt_E6a2jH97EeBEFvuJdcrqcw0Qt_-y5wNyDJYCvReHqwg6cUOG9_lTDwKNRaKli"
# Phase 4: health/system alerts channel; fallback webhook sinyal kalau kosong
DISCORD_HEALTH_WEBHOOK=os.getenv("DISCORD_HEALTH_WEBHOOK_URL",DISCORD_WEBHOOK_URL)
DISCORD_WEBHOOK_TRADE_URL= "https://discord.com/api/webhooks/1533282533264064552/BxCca57xNw2vzG-XlUmlUForDY5DeRybLEzkPYoUOMisvS7NbeAS135xR3e9VVT_LIRA"
DISCORD_USER_ID"1464229528317464765"
MODEL "qwen2.5:3b"'''

new_constants = '''# OmniRouter OpenAI-compatible endpoint (Cloudflare Tunnel -> Termux OmniRoute)
OMNIROUTER_URL=os.getenv("OMNIROUTER_URL", "https://your-tunnel.trycloudflare.com/v1/chat/completions")
# Fallback: local Ollama (if tunnel down)
NINJA_ROUTER_URL= "http://127.0.0.1:11434"
DISCORD_WEBHOOK_URL= "https://discord.com/api/webhooks/1532938091331977337/EQV4Kt_E6a2jH97EeBEFvuJdcrqcw0Qt_-y5wNyDJYCvReHqwg6cUOG9_lTDwKNRaKli"
# Phase 4: health/system alerts channel; fallback webhook sinyal kalau kosong
DISCORD_HEALTH_WEBHOOK=os.getenv("DISCORD_HEALTH_WEBHOOK_URL",DISCORD_WEBHOOK_URL)
DISCORD_WEBHOOK_TRADE_URL= "https://discord.com/api/webhooks/1533282533264064552/BxCca57xNw2vzG-XlUmlUForDY5DeRybLEzkPYoUOMisvS7NbeAS135xR3e9VVT_LIRA"
DISCORD_USER_ID"1464229528317464765"
MODEL "qwen2.5:3b"
AI_MODEL=os.getenv("AI_MODEL", "gratis-3")  # for OmniRouter'''

content = content.replace(old_constants, new_constants)

# ============================================================
# 3. REPLACE get_ai_signal HTTP CALL BLOCK
# ============================================================
# Find the get_ai_signal function and replace the HTTP handling section

old_http_block = ''' url f"{NINJA_ROUTER_URL}/api/generate"
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

new_http_block = ''' url = OMNIROUTER_URL
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

content = content.replace(old_http_block, new_http_block)

# ============================================================
# 4. VERIFY - Check if replacement worked
# ============================================================
if 'call_ai_signal' in content:
    print("✓ call_ai_signal found in patched content")
else:
    print("✗ call_ai_signal NOT found - replacement may have failed")
    # Debug: find the HTTP block
    if 'url f"{NINJA_ROUTER_URL}/api/generate"' in content:
        print("  Old NINJA_ROUTER_URL pattern still present")
    if 'OMNIROUTER_URL' in content:
        print("  OMNIROUTER_URL constant present")

if 'import execution as tko_exec' in content:
    print("✓ execution import fixed")
else:
    print("✗ execution import NOT fixed")

if 'from robust_http import call_ai_signal' in content:
    print("✓ robust_http import added")
else:
    print("✗ robust_http import NOT added")

# Write back
with open("C:/Users/reyvan/pixel-agents/bridge.py", "w", encoding="utf-8") as f:
    f.write(content)

print("\nPatch applied. Verifying syntax...")
"""
Patch for bridge.py get_ai_signal function - replaces the requests.post block
with robust HTTP client that handles 502, markdown, JSON extraction, retry.
"""
import re

# Read current bridge.py
with open("C:/Users/reyvan/pixel-agents/bridge.py", "r", encoding="utf-8") as f:
    content = f.read()

# 1. Fix imports at top
old_imports = '''import requests, json, re, time, os, random, schedule
from pathlib import Path
from datetime import datetime
import execution tko_exec

# Robust HTTPclient (handles 502, markdown, JSON extraction)
from robust_http import call_ai_signal, extract_json_from_text'''

new_imports = '''import requests, json, re, time, os, random, schedule
from pathlib import Path
from datetime import datetime
import execution as tko_exec

# Robust HTTP client (handles 502, markdown, JSON extraction)
from robust_http import call_ai_signal, extract_json_from_text'''

content = content.replace(old_imports, new_imports)

# 2. Replace get_ai_signal function's HTTP block (from "Use OmniRouter" to "return None")
# Find the pattern and replace
old_http_block = ''' Use OmniRouter OpenAI-compatible endpoint
 url OMNIROUTER_URL
 omni_api_key os.getenv("OMNIROUTER_API_KEY", "")
 attempt range(3):
 prompt
 attempt 0 last_asset:
 f"\\nPERINGATAN: {last_asset} SUDAH dipilih pada sinyal terakhir. Pilih koin LAIN selain {last_asset}."
 payload {
 "model": MODEL,
 "messages": [
 {"role": "user", "content": p},
 ],
 "max_tokens": 500,
 "temperature": 0.7,
 "top_p": 0.9,
 }
 headers {"Content-Type": "application/json"}
 omni_api_key:
 headers["Authorization"] f"Bearer {omni_api_key}"
 try:
 requests.post(url, json=payload, timeout=(60, 180), headers=headers)
 r.raise_for_status()
 data r.json()
 content data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
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
 log(f"[AI] {base} sama dengan sinyal terakhir, retry rotasi")
 continue
 excluded_assets base excluded_assets:
 log(f"[AI] {base} ada excluded (posisi terbuka/terakhir) retry rotasi")
 continue
 sig["asset"] f"{base}USDT"
 "entry_zone" not sig:
 handle typo entry_zoe
 "entry_zoe" sig:
 sig["entry_zone"] sig.pop("entry_zoe")
 return sig
 except requests.exceptions.Timeout:
 log(f"[AI] Timeout on attempt {attempt + 1}")
 continue
 except Exception e:
 log(f"[AI] Error on attempt {attempt + 1}: {e}")
 continue
 return None
 except Exception e:
 log(f"[AI] Error: {e}")
 return None'''

new_http_block = ''' Use OmniRouter OpenAI-compatible endpoint
 url = OMNIROUTER_URL
 omni_api_key = os.getenv("OMNIROUTER_API_KEY", "")
 attempt = 0
 while attempt < 3:
     p = prompt
     if attempt == 0 and last_asset:
         p += f"\\nPERINGATAN: {last_asset} SUDAH dipilih pada sinyal terakhir. Pilih koin LAIN selain {last_asset}."
     payload = {
         "model": MODEL,
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
 return None'''

# The old block has irregular spacing. Let me find it more precisely by searching for key markers
# Use a simpler approach - find the function and replace the inner try block
import re as regex

# Find the get_ai_signal function and replace its HTTP handling
pattern = r'( Use OmniRouter OpenAI-compatible endpoint\n url OMNIROUTER_URL\n omni_api_key os\.getenv\("OMNIROUTER_API_KEY", ""\)\n attempt range\(3\):\n prompt\n attempt 0 last_asset:\n f"\\\\nPERINGATAN: \{last_asset\} SUDAH dipilih pada sinyal terakhir\. Pilih koin LAIN selain \{last_asset\}\."\n payload \{\n "model": MODEL,\n "messages": \[\n \{"role": "user", "content": p\},\n \],\n "max_tokens": 500,\n "temperature": 0\.7,\n "top_p": 0\.9,\n \}\n headers \{"Content-Type": "application/json"\}\n omni_api_key:\n headers\["Authorization"\] f"Bearer \{omni_api_key\}"\n try:\n requests\.post\(url, json=payload, timeout=\(60, 180\), headers=headers\)\n r\.raise_for_status\(\)\n data r\.json\(\)\n content data\.get\("choices", \[\{\}\]\)\[0\]\.get\("message", \{\}\)\.get\("content", ""\)\.strip\(\)\n sig _parse_ai_json\(content\)\n not sig:\n continue\n base _normalize_base\(sig\.get\("asset", ""\)\)\n not base:\n log\("\[AI\] No valid asset in response, retry"\)\n continue\n "confidence" not sig not re\.search\(r"\\\\d", str\(sig\.get\("confidence", ""\)\)\):\n log\("\[AI\] No confidence in signal, retry"\)\n continue\n base not market_data:\n log\(f"\[AI\] Asset \{sig\.get\(\'asset\'\)\}\' not top 20, retrying"\)\n continue\n last_asset base last_asset:\n log\(f"\[AI\] \{base\} sama dengan sinyal terakhir, retry rotasi"\)\n continue\n excluded_assets base excluded_assets:\n log\(f"\[AI\] \{base\} ada excluded \(posisi terbuka/terakhir\) retry rotasi"\)\n continue\n sig\["asset"\] f"\{base\}USDT"\n "entry_zone" not sig:\n handle typo entry_zoe\n "entry_zoe" sig:\n sig\["entry_zone"\] sig\.pop\("entry_zoe"\)\n return sig\n except requests\.exceptions\.Timeout:\n log\(f"\[AI\] Timeout on attempt \{attempt \+ 1\}"\)\n continue\n except Exception e:\n log\(f"\[AI\] Error on attempt \{attempt \+ 1\}: \{e\}"\)\n continue\n return None\n except Exception e:\n log\(f"\[AI\] Error: \{e\}"\)\n return None)'

# The file format is compressed (no spaces). Let me write a completely new clean bridge.py
# by patching in place with the correct sections.

print("Replacing imports...")
# Fix the imports properly
lines = content.split('\n')
new_lines = []
for i, line in enumerate(lines):
    if i == 3 and 'import execution tko_exec' in line:
        new_lines.append('import execution as tko_exec')
    elif i == 5 and '# Robust HTTPclient' in line:
        new_lines.append('# Robust HTTP client (handles 502, markdown, JSON extraction)')
    elif i == 6 and 'from robust_http import call_ai_signal, extract_json_from_text' in line:
        new_lines.append('from robust_http import call_ai_signal, extract_json_from_text')
    else:
        new_lines.append(line)

content = '\n'.join(new_lines)

# Now write back
with open("C:/Users/reyvan/pixel-agents/bridge.py", "w", encoding="utf-8") as f:
    f.write(content)

print("Imports fixed. Now need to fix the get_ai_signal function...")
print("The file format is compressed. Let me write a cleaner approach.")
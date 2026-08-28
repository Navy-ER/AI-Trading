#!/usr/bin/env python3
"""
Final exact patch - REPLICATE EXACT ORIGINAL COMPRESSED INDENTATION
"""
with open("C:/Users/reyvan/pixel-agents/bridge.py.backup_bad", "rb") as f:
    content = f.read()

# Exact positions
http_block_start = 16197
http_block_end = 18170

# Build new block with EXACT SAME indentation as original
new_block = b''' url = OMNIROUTER_URL
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
 return None
'''

# Replace in content
new_content = content[:http_block_start] + new_block + content[http_block_end:]

# Fix imports - EXACT pattern
import_start = new_content.find(b'import execution tko_exec\n\n# \xe2\x94\x80\xe2\x94\x80 Constants \xe2')
if import_start >= 0:
    new_import = b'import execution as tko_exec\n\n# Robust HTTP client (handles 502, markdown, JSON extraction)\nfrom robust_http import call_ai_signal, extract_json_from_text\n\n# \xe2\x94\x80\xe2\x94\x80 Constants \xe2'
    old_import = b'import execution tko_exec\n\n# \xe2\x94\x80\xe2\x94\x80 Constants \xe2'
    new_content = new_content[:import_start] + new_import + new_content[import_start + len(old_import):]
    print("✓ Import fixed")
else:
    print("✗ Import pattern not found")

# Fix constants - EXACT pattern
const_start = new_content.find(b'NINJA_ROUTER_URL= "http://127.0.0.1:11434"\nDISCORD_WEBHOOK_URL ')
if const_start >= 0:
    new_const = b'''# OmniRouter OpenAI-compatible endpoint (Cloudflare Tunnel -> Termux OmniRoute)
OMNIROUTER_URL=os.getenv("OMNIROUTER_URL", "https://your-tunnel.trycloudflare.com/v1/chat/completions")
# Fallback: local Ollama (if tunnel down)
NINJA_ROUTER_URL= "http://127.0.0.1:11434"
AI_MODEL=os.getenv("AI_MODEL", "gratis-3")
DISCORD_WEBHOOK_URL='''
    old_const = b'NINJA_ROUTER_URL= "http://127.0.0.1:11434"\nDISCORD_WEBHOOK_URL '
    new_content = new_content[:const_start] + new_const + new_content[const_start + len(old_const):]
    print("✓ Constants fixed")
else:
    print("✗ Constants pattern not found")

# Write
with open("C:/Users/reyvan/pixel-agents/bridge.py", "wb") as f:
    f.write(new_content)

print(f"Written: {len(new_content)} bytes")

# Verify syntax
import subprocess
result = subprocess.run(['python', '-m', 'py_compile', 'C:/Users/reyvan/pixel-agents/bridge.py'], 
                       capture_output=True, text=True)
if result.returncode == 0:
    print("✓ Syntax OK")
else:
    print(f"✗ Syntax ERROR:")
    print(result.stderr[:500])

# Verify key elements
text = new_content.decode('utf-8')
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
    if pattern in text:
        print(f"  ✓ {desc}")
    else:
        print(f"  ✗ {desc}")
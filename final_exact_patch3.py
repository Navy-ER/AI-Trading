#!/usr/bin/env python3
"""
Final exact patch using CORRECT byte patterns from backup
"""
with open("C:/Users/reyvan/pixel-agents/bridge.py.backup_bad", "rb") as f:
    content = f.read()

# Exact positions from analysis - CORRECTED
http_block_start = 16197  # Full line: ' url f"{NINJA_ROUTER_URL}/api/generate"\n'
http_block_end = 18170    # After 'return None\n' before 'def_normalize_base'

old_bytes = content[http_block_start:http_block_end]
print(f"Old block length: {len(old_bytes)} bytes")

# Build new block with same formatting (LF, compressed)
new_block = b''' url = OMNIROUTER_URL\n
 omni_api_key = os.getenv("OMNIROUTER_API_KEY", "")\n
 attempt = 0\n
 while attempt < 3:\n
     p = prompt\n
     if attempt == 0 and last_asset:\n
         p += f"\\nPERINGATAN: {last_asset} SUDAH dipilih pada sinyal terakhir. Pilih koin LAIN selain {last_asset}."\n
     payload = {\n
         "model": AI_MODEL,\n
         "messages": [\n
             {"role": "user", "content": p},\n
         ],\n
         "max_tokens": 500,\n
         "temperature": 0.7,\n
         "top_p": 0.9,\n
     }\n
     headers = {"Content-Type": "application/json"}\n
     if omni_api_key:\n
         headers["Authorization"] = f"Bearer {omni_api_key}"\n
     try:\n
         # Use robust HTTP client with 502 handling, JSON extraction, retry\n
         sig, raw_content = call_ai_signal(url, payload, headers, max_retries=1)\n
         if not sig:\n
             log(f"[AI] No valid signal extracted on attempt {attempt + 1}: {raw_content[:200] if raw_content else 'unknown'}")\n
             attempt += 1\n
             continue\n
         base = _normalize_base(sig.get("asset", ""))\n
         if not base:\n
             log("[AI] No valid asset in response, retry")\n
             attempt += 1\n
             continue\n
         if "confidence" not in sig or not re.search(r"\\d", str(sig.get("confidence", ""))):\n
             log("[AI] No confidence in signal, retry")\n
             attempt += 1\n
             continue\n
         if base not in market_data:\n
             log(f"[AI] Asset {sig.get('asset')} not top 20, retrying")\n
             attempt += 1\n
             continue\n
         if base == last_asset:\n
             log(f"[AI] {base} sama dengan sinyal terakhir, retry rotasi")\n
             attempt += 1\n
             continue\n
         if excluded_assets and base in excluded_assets:\n
             log(f"[AI] {base} ada excluded (posisi terbuka/terakhir) retry rotasi")\n
             attempt += 1\n
             continue\n
         sig["asset"] = f"{base}USDT"\n
         # handle typo entry_zoe\n
         if "entry_zone" not in sig and "entry_zoe" in sig:\n
             sig["entry_zone"] = sig.pop("entry_zoe")\n
         return sig\n
     except requests.exceptions.Timeout:\n
         log(f"[AI] Timeout on attempt {attempt + 1}")\n
         attempt += 1\n
         continue\n
     except Exception as e:\n
         log(f"[AI] Error on attempt {attempt + 1}: {e}")\n
         attempt += 1\n
         continue\n
 return None\n
 except Exception e:\n
 log(f"[AI] Error: {e}")\n
 return None'''

# Replace in content
new_content = content[:http_block_start] + new_block + content[http_block_end:]

# Fix imports - EXACT pattern from backup: 'import execution tko_exec\n\n# \xe2\x94\x80\xe2\x94\x80 Constants \xe2'
import_start = new_content.find(b'import execution tko_exec\n\n# \xe2\x94\x80\xe2\x94\x80 Constants \xe2')
if import_start >= 0:
    new_import = b'import execution as tko_exec\n\n# Robust HTTP client (handles 502, markdown, JSON extraction)\nfrom robust_http import call_ai_signal, extract_json_from_text\n\n# \xe2\x94\x80\xe2\x94\x80 Constants \xe2'
    old_import = b'import execution tko_exec\n\n# \xe2\x94\x80\xe2\x94\x80 Constants \xe2'
    new_content = new_content[:import_start] + new_import + new_content[import_start + len(old_import):]
    print("✓ Import fixed")
else:
    print("✗ Import pattern not found")

# Fix constants - EXACT pattern: 'NINJA_ROUTER_URL= "http://127.0.0.1:11434"\nDISCORD_WEBHOOK_URL '
const_start = new_content.find(b'NINJA_ROUTER_URL= "http://127.0.0.1:11434"\nDISCORD_WEBHOOK_URL ')
if const_start >= 0:
    new_const = b'''# OmniRouter OpenAI-compatible endpoint (Cloudflare Tunnel -> Termux OmniRoute)\n
OMNIROUTER_URL=os.getenv("OMNIROUTER_URL", "https://your-tunnel.trycloudflare.com/v1/chat/completions")\n
# Fallback: local Ollama (if tunnel down)\n
NINJA_ROUTER_URL= "http://127.0.0.1:11434"\n
AI_MODEL=os.getenv("AI_MODEL", "gratis-3")\n
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
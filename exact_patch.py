#!/usr/bin/env python3
"""
Patch using exact byte content from backup
"""
with open("C:/Users/reyvan/pixel-agents/bridge.py.backup_bad", "rb") as f:
    content = f.read()

# The HTTP block exact bytes (from offset 16232)
# Let me extract it precisely
http_block_start = content.find(b'api/generate"\r\n')
if http_block_start < 0:
    print("Could not find start")
    exit(1)

# The block ends before "def_normalize_base"
http_block_end = content.find(b'\r\ndef_normalize_base', http_block_start)
if http_block_end < 0:
    print("Could not find end")
    exit(1)

old_bytes = content[http_block_start:http_block_end]
print(f"Old block length: {len(old_bytes)} bytes")
print("--- OLD BLOCK ---")
print(old_bytes.decode('utf-8'))
print("--- END ---")

# Build new block with same formatting (CRLF, compressed)
new_block = b'''api/generate"\r
 url = OMNIROUTER_URL\r
 omni_api_key = os.getenv("OMNIROUTER_API_KEY", "")\r
 attempt = 0\r
 while attempt < 3:\r
     p = prompt\r
     if attempt == 0 and last_asset:\r
         p += f"\\nPERINGATAN: {last_asset} SUDAH dipilih pada sinyal terakhir. Pilih koin LAIN selain {last_asset}."\r
     payload = {\r
         "model": AI_MODEL,\r
         "messages": [\r
             {"role": "user", "content": p},\r
         ],\r
         "max_tokens": 500,\r
         "temperature": 0.7,\r
         "top_p": 0.9,\r
     }\r
     headers = {"Content-Type": "application/json"}\r
     if omni_api_key:\r
         headers["Authorization"] = f"Bearer {omni_api_key}"\r
     try:\r
         # Use robust HTTP client with 502 handling, JSON extraction, retry\r
         sig, raw_content = call_ai_signal(url, payload, headers, max_retries=1)\r
         if not sig:\r
             log(f"[AI] No valid signal extracted on attempt {attempt + 1}: {raw_content[:200] if raw_content else 'unknown'}")\r
             attempt += 1\r
             continue\r
         base = _normalize_base(sig.get("asset", ""))\r
         if not base:\r
             log("[AI] No valid asset in response, retry")\r
             attempt += 1\r
             continue\r
         if "confidence" not in sig or not re.search(r"\\d", str(sig.get("confidence", ""))):\r
             log("[AI] No confidence in signal, retry")\r
             attempt += 1\r
             continue\r
         if base not in market_data:\r
             log(f"[AI] Asset {sig.get('asset')} not top 20, retrying")\r
             attempt += 1\r
             continue\r
         if base == last_asset:\r
             log(f"[AI] {base} sama dengan sinyal terakhir, retry rotasi")\r
             attempt += 1\r
             continue\r
         if excluded_assets and base in excluded_assets:\r
             log(f"[AI] {base} ada excluded (posisi terbuka/terakhir) retry rotasi")\r
             attempt += 1\r
             continue\r
         sig["asset"] = f"{base}USDT"\r
         # handle typo entry_zoe\r
         if "entry_zone" not in sig and "entry_zoe" in sig:\r
             sig["entry_zone"] = sig.pop("entry_zoe")\r
         return sig\r
     except requests.exceptions.Timeout:\r
         log(f"[AI] Timeout on attempt {attempt + 1}")\r
         attempt += 1\r
         continue\r
     except Exception as e:\r
         log(f"[AI] Error on attempt {attempt + 1}: {e}")\r
         attempt += 1\r
         continue\r
 return None\r
 except Exception e:\r
 log(f"[AI] Error: {e}")\r
 return None'''

# Replace in content
new_content = content[:http_block_start] + new_block + content[http_block_end:]

# Fix imports: find "import execution tko_exec\r\n\r\n# Constants"
import_start = new_content.find(b'import execution tko_exec\r\n\r\n# Constants')
if import_start >= 0:
    new_import = b'import execution as tko_exec\r\n\r\n# Robust HTTP client (handles 502, markdown, JSON extraction)\r\nfrom robust_http import call_ai_signal, extract_json_from_text\r\n\r\n# Constants'
    new_content = new_content[:import_start] + new_import + new_content[import_start + len(b'import execution tko_exec\r\n\r\n# Constants'):]
    print("✓ Import fixed")
else:
    print("✗ Import pattern not found")

# Fix constants: find "NINJA_ROUTER_URL= \"http://127.0.0.1:11434\"\r\nDISCORD_WEBHOOK_URL="
const_start = new_content.find(b'NINJA_ROUTER_URL= "http://127.0.0.1:11434"\r\nDISCORD_WEBHOOK_URL=')
if const_start >= 0:
    new_const = b'''# OmniRouter OpenAI-compatible endpoint (Cloudflare Tunnel -> Termux OmniRoute)\r
OMNIROUTER_URL=os.getenv("OMNIROUTER_URL", "https://your-tunnel.trycloudflare.com/v1/chat/completions")\r
# Fallback: local Ollama (if tunnel down)\r
NINJA_ROUTER_URL= "http://127.0.0.1:11434"\r
AI_MODEL=os.getenv("AI_MODEL", "gratis-3")\r
DISCORD_WEBHOOK_URL='''
    old_const = b'NINJA_ROUTER_URL= "http://127.0.0.1:11434"\r\nDISCORD_WEBHOOK_URL='
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
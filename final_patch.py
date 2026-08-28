#!/usr/bin/env python3
"""
Precise patch using exact line-by-line replacement
"""
with open("C:/Users/reyvan/pixel-agents/bridge.py", "r", encoding="utf-8") as f:
    lines = f.readlines()

# Print the exact old block we need to replace
old_lines = lines[355:398]  # from url line to "return None" before except
print("OLD BLOCK:")
for i, line in enumerate(old_lines):
    print(f"{355+i}: {repr(line)}")

print("\n---\n")

# Build new lines
new_lines = [
    ' url = OMNIROUTER_URL\n',
    ' omni_api_key = os.getenv("OMNIROUTER_API_KEY", "")\n',
    ' attempt = 0\n',
    ' while attempt < 3:\n',
    '     p = prompt\n',
    '     if attempt == 0 and last_asset:\n',
    '         p += f"\\nPERINGATAN: {last_asset} SUDAH dipilih pada sinyal terakhir. Pilih koin LAIN selain {last_asset}."\n',
    '     payload = {\n',
    '         "model": AI_MODEL,\n',
    '         "messages": [\n',
    '             {"role": "user", "content": p},\n',
    '         ],\n',
    '         "max_tokens": 500,\n',
    '         "temperature": 0.7,\n',
    '         "top_p": 0.9,\n',
    '     }\n',
    '     headers = {"Content-Type": "application/json"}\n',
    '     if omni_api_key:\n',
    '         headers["Authorization"] = f"Bearer {omni_api_key}"\n',
    '     try:\n',
    '         # Use robust HTTP client with 502 handling, JSON extraction, retry\n',
    '         sig, raw_content = call_ai_signal(url, payload, headers, max_retries=1)\n',
    '         if not sig:\n',
    '             log(f"[AI] No valid signal extracted on attempt {attempt + 1}: {raw_content[:200] if raw_content else \'unknown\'}")\n',
    '             attempt += 1\n',
    '             continue\n',
    '         base = _normalize_base(sig.get("asset", ""))\n',
    '         if not base:\n',
    '             log("[AI] No valid asset in response, retry")\n',
    '             attempt += 1\n',
    '             continue\n',
    '         if "confidence" not in sig or not re.search(r"\\d", str(sig.get("confidence", ""))):\n',
    '             log("[AI] No confidence in signal, retry")\n',
    '             attempt += 1\n',
    '             continue\n',
    '         if base not in market_data:\n',
    '             log(f"[AI] Asset {sig.get(\'asset\')} not top 20, retrying")\n',
    '             attempt += 1\n',
    '             continue\n',
    '         if base == last_asset:\n',
    '             log(f"[AI] {base} sama dengan sinyal terakhir, retry rotasi")\n',
    '             attempt += 1\n',
    '             continue\n',
    '         if excluded_assets and base in excluded_assets:\n',
    '             log(f"[AI] {base} ada excluded (posisi terbuka/terakhir) retry rotasi")\n',
    '             attempt += 1\n',
    '             continue\n',
    '         sig["asset"] = f"{base}USDT"\n',
    '         # handle typo entry_zoe\n',
    '         if "entry_zone" not in sig and "entry_zoe" in sig:\n',
    '             sig["entry_zone"] = sig.pop("entry_zoe")\n',
    '         return sig\n',
    '     except requests.exceptions.Timeout:\n',
    '         log(f"[AI] Timeout on attempt {attempt + 1}")\n',
    '         attempt += 1\n',
    '         continue\n',
    '     except Exception as e:\n',
    '         log(f"[AI] Error on attempt {attempt + 1}: {e}")\n',
    '         attempt += 1\n',
    '         continue\n',
    ' return None\n',
    ' except Exception e:\n',
    ' log(f"[AI] Error: {e}")\n',
    ' return None\n'
]

print("NEW BLOCK (first 5 lines):")
for i, line in enumerate(new_lines[:5]):
    print(f"  {repr(line)}")

# Now do the replacement
new_content_lines = lines[:355] + new_lines + lines[398:]

# Also fix imports and constants
# Fix import line (line 4, index 3)
for i, line in enumerate(new_content_lines):
    if line.strip() == 'import execution tko_exec':
        new_content_lines[i] = 'import execution as tko_exec\n'
        # Insert robust_http import after it
        new_content_lines.insert(i+1, '\n# Robust HTTP client (handles 502, markdown, JSON extraction)\n')
        new_content_lines.insert(i+2, 'from robust_http import call_ai_signal, extract_json_from_text\n')
        break

# Fix constants (NINJA_ROUTER_URL line)
for i, line in enumerate(new_content_lines):
    if line.strip().startswith('NINJA_ROUTER_URL=') and 'http://127.0.0.1:11434' in line:
        # Replace this line and add OMNIROUTER_URL before it
        new_content_lines[i] = '''# OmniRouter OpenAI-compatible endpoint (Cloudflare Tunnel -> Termux OmniRoute)
OMNIROUTER_URL=os.getenv("OMNIROUTER_URL", "https://your-tunnel.trycloudflare.com/v1/chat/completions")
# Fallback: local Ollama (if tunnel down)
NINJA_ROUTER_URL= "http://127.0.0.1:11434"
AI_MODEL=os.getenv("AI_MODEL", "gratis-3")
'''
        break

# Write
with open("C:/Users/reyvan/pixel-agents/bridge.py", "w", encoding="utf-8") as f:
    f.writelines(new_content_lines)

print("\nDone. Verifying...")

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
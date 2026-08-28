import requests, json, re, time, os, schedule
from pathlib import Path
from datetime import datetime
import random
import execution as tko_exec
from robust_http import call_ai_signal, extract_json_from_text

# Constants
OMNIROUTER_URL = os.getenv("OMNIROUTER_URL", "http://127.0.0.1:11434")
AI_MODEL = os.getenv("AI_MODEL", "qwen2.5:1.5b")
DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/1532938091331977337/EQV4Kt_E6a2jH97EeBEFvuJdcrqcw0Qt_-y5wNyDJYCvReHqwg6cUOG9_lTDwKNRaKli"
DISCORD_HEALTH_WEBHOOK = os.getenv("DISCORD_HEALTH_WEBHOOK_URL", DISCORD_WEBHOOK_URL)
DISCORD_WEBHOOK_TRADE_URL = "https://discord.com/api/webhooks/1533282533264064552/BxCca57xNw2vzG-XlUmlUForDY5DeRybLEzkPYoUOMisvS7NbeAS135xR3e9VVT_LIRA"
DISCORD_USER_ID = "1464229528317464765"

PROBLEM_ASSETS = {"SNDK", "SNDKB", "SOL", "GIGGLE", "UNI", "ADA"}

_price_history = {}

INTERVAL = 90
THRESHOLD = 0.5
MAX_OPEN_POSITIONS = int(os.getenv("TKO_MAX_POSITIONS", "5"))
TRAIL_ENABLED = os.getenv("TKO_TRAIL_ENABLED", "1") == "1"
STALE_POSITION_HOURS = 24

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SIGNALS_LOG = os.path.join(BASE_DIR, "signals.jsonl")
TRADES_LOG = os.path.join(BASE_DIR, "trades.jsonl")
OPEN_POS_LOG = os.path.join(BASE_DIR, "open_positions.jsonl")
CIRCUIT_STATE = os.path.join(BASE_DIR, "circuit_state.json")

DAILY_LOSS_LIMIT = float(os.getenv("TKO_DAILY_LOSS_LIMIT", "2.40"))
CIRCUIT_DISABLED = os.getenv("TKO_CIRCUIT_DISABLED", "0") == "1"
EVAL_HOURS = 1
TREND_MIN_ADX = float(os.getenv("TKO_TREND_MIN_ADX", "25"))
LOSS_COOLDOWN_HOURS = float(os.getenv("TKO_LOSS_COOLDOWN_HOURS", "6"))

USDT_TO_IDR = 16000

BRIDGE_LOG = os.path.join(BASE_DIR, "bridge.log")
LOG_MAX_BYTES = 10 * 1024 * 1024

# Helpers
def idr(v):
    return f"{round(v * USDT_TO_IDR):,}".replace(",", ".")

def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}")
    try:
        if os.path.exists(BRIDGE_LOG) and os.path.getsize(BRIDGE_LOG) > LOG_MAX_BYTES:
            os.replace(BRIDGE_LOG, BRIDGE_LOG + ".bak")
        with open(BRIDGE_LOG, "a") as f:
            f.write(f"[{ts}] {msg}\n")
    except OSError:
        pass

def append_jsonl(path, obj):
    with open(path, "a") as f:
        f.write(json.dumps(obj, default=str) + "\n")

def read_jsonl(path):
    items = []
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        items.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    return items

# Daily loss circuit breaker
def circuit_state():
    """Persisted per-day realised loss paused flag. Reset otomatis saat tanggal ganti."""
    st = {"date": datetime.now().strftime("%Y-%m-%d"), "realised_loss": 0.0, "paused": False}
    if os.path.exists(CIRCUIT_STATE):
        try:
            st.update(json.load(open(CIRCUIT_STATE, encoding="utf-8")))
        except Exception:
            pass
    if st.get("date") != datetime.now().strftime("%Y-%m-%d"):
        st = {"date": datetime.now().strftime("%Y-%m-%d"), "realised_loss": 0.0, "paused": False}
    return st

def circuit_paused():
    if CIRCUIT_DISABLED:
        return False
    return circuit_state().get("paused", False)

def record_realised_loss(pnl):
    """Accumulate realised loss hari ini; pause trading kalau lewat limit."""
    if pnl >= 0:
        return
    st = circuit_state()
    st["realised_loss"] = round(st.get("realised_loss", 0.0) + abs(pnl), 4)
    if not st.get("paused") and st["realised_loss"] >= DAILY_LOSS_LIMIT:
        st["paused"] = True
        log(f"[CIRCUIT] Daily realised loss {st['realised_loss']:.4f} USDT limit {DAILY_LOSS_LIMIT} USDT -> TRADING PAUSED until daily reset")
        send_discord_webhook(DISCORD_HEALTH_WEBHOOK, {
            "title": "Circuit Breaker: Trading Paused",
            "color": 0xFF0000,
            "fields": [
                {"name": "Daily Realised Loss", "value": f"-{st['realised_loss']:.4f} USDT", "inline": True},
                {"name": "Limit", "value": f"{DAILY_LOSS_LIMIT} USDT", "inline": True},
                {"name": "Resume", "value": "Otomatis besok (reset harian)", "inline": False},
            ],
        })
    with open(CIRCUIT_STATE, "w", encoding="utf-8") as f:
        json.dump(st, f)

# Market Data
def get_price_range(symbol, since_ts):
    try:
        url = "https://data-api.binance.vision/api/v3/klines"
        params = {"symbol": symbol, "interval": "1h", "startTime": int(since_ts * 1000), "limit": 100}
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        klines = r.json()
        if not klines:
            return None, None
        highs = [float(k[2]) for k in klines]
        lows = [float(k[3]) for k in klines]
        return max(highs), min(lows)
    except Exception as e:
        log(f"[RANGE] Error: {e}")
        return None, None

def get_atr(symbol, period=14):
    try:
        url = "https://data-api.binance.vision/api/v3/klines"
        params = {"symbol": symbol, "interval": "15m", "limit": period + 1}
        r = requests.get(url, params=params, timeout=10)
        if r.status_code == 400:
            log(f"[ATR] {symbol}: HTTP 400 — symbol not listed or delisted")
            return False
        r.raise_for_status()
        klines = r.json()
        if len(klines) < period + 1:
            return None
        trs = []
        for i in range(1, len(klines)):
            h = float(klines[i][2])
            l = float(klines[i][3])
            c_prev = float(klines[i - 1][4])
            trs.append(max(h - l, abs(h - c_prev), abs(l - c_prev)))
        return sum(trs) / len(trs)
    except requests.exceptions.Timeout:
        log(f"[ATR] {symbol}: timeout")
        return None
    except Exception as e:
        log(f"[ATR] {symbol}: {e}")
        return None

def get_rsi(symbol, period=14):
    """Compute RSI 1h klines (recent ~14h). Returns None on error."""
    try:
        url = "https://data-api.binance.vision/api/v3/klines"
        params = {"symbol": symbol, "interval": "1h", "limit": period + 1}
        r = requests.get(url, params=params, timeout=10)
        if r.status_code == 400:
            return None
        r.raise_for_status()
        klines = r.json()
        if len(klines) < period + 1:
            return None
        closes = [float(k[4]) for k in klines]
        gains, losses = 0.0, 0.0
        for i in range(1, len(closes)):
            delta = closes[i] - closes[i - 1]
            if delta > 0:
                gains += delta
            else:
                losses += abs(delta)
        avg_gain = gains / period
        avg_loss = losses / period
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return round(100 - (100 / (1 + rs)), 1)
    except Exception:
        return None

def _adx_components(klines, period=14):
    """Wilder ADX dari klines. Return (adx, plus_di_sum, minus_di_sum) or (None, None, None)."""
    if len(klines) < period + 20:
        return None, None, None
    highs = [float(k[2]) for k in klines]
    lows = [float(k[3]) for k in klines]
    closes = [float(k[4]) for k in klines]
    
    tr = [0.0]
    for i in range(1, len(klines)):
        tr.append(max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        ))
    
    atr = [0.0] * period + [sum(tr[i - period + 1:i + 1]) / period for i in range(period, len(tr))]
    plus_dm = [0.0] * len(tr)
    minus_dm = [0.0] * len(tr)
    
    for i in range(1, len(tr)):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dm[i] = up if up > down and up > 0 else 0.0
        minus_dm[i] = down if down > up and down > 0 else 0.0
    
    plus_di = [0.0] * len(tr)
    minus_di = [0.0] * len(tr)
    
    for i in range(period, len(tr)):
        atr_i = atr[i]
        if atr_i == 0:
            continue
        plus_di[i] = 100 * sum(plus_dm[i - period + 1:i + 1]) / atr_i
        minus_di[i] = 100 * sum(minus_dm[i - period + 1:i + 1]) / atr_i
    
    dx = [0.0] * len(tr)
    for i in range(period, len(tr)):
        pdi = plus_di[i]
        mdi = minus_di[i]
        if pdi + mdi == 0:
            continue
        dx[i] = 100 * abs(pdi - mdi) / (pdi + mdi)
    
    adx = sum(dx[-period:]) / period if len(dx) >= period else 0.0
    return round(adx, 1), sum(plus_di[-period:]), sum(minus_di[-period:])

def get_adx(symbol, period=14):
    """Compute ADX 1h klines. Returns None on error."""
    try:
        url = "https://data-api.binance.vision/api/v3/klines"
        params = {"symbol": symbol, "interval": "1h", "limit": period + 20}
        r = requests.get(url, params=params, timeout=10)
        if r.status_code == 400:
            return None
        r.raise_for_status()
        adx, _, _ = _adx_components(r.json(), period)
        return adx
    except Exception:
        return None

def get_trend_dir(symbol, period=14):
    """Arah tren dari +DI/-DI (1h klines). Return 'UP'/'DOWN'/None (error/equal)."""
    try:
        url = "https://data-api.binance.vision/api/v3/klines"
        params = {"symbol": symbol, "interval": "1h", "limit": period + 20}
        r = requests.get(url, params=params, timeout=10)
        if r.status_code == 400:
            return None
        r.raise_for_status()
        _, pdi, mdi = _adx_components(r.json(), period)
        if pdi is None or pdi == mdi:
            return None
        return "UP" if pdi > mdi else "DOWN"
    except Exception:
        return None

def get_batch_rsi(assets):
    """Fetch RSI multiple symbols concurrently (threaded)."""
    import concurrent.futures
    result = {}
    def _rsi(base):
        sym = f"{base}USDT"
        return base, get_rsi(sym)
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(_rsi, b): b for b in assets}
        for f in concurrent.futures.as_completed(futures, timeout=12):
            try:
                base, rsi = f.result()
                result[base] = rsi
            except Exception:
                pass
    return result

def get_market_data():
    try:
        url = "https://data-api.binance.vision/api/v3/ticker/24hr"
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        tickers = r.json()
        usdt_pairs = [t for t in tickers if t["symbol"].endswith("USDT")]
        usdt_pairs.sort(key=lambda x: float(x.get("quoteVolume", 0)), reverse=True)
        reject_set = {"EUL", "BTC", "ETH", "USDT", "BUSD", "TUSD", "USDC", "USD1", "FDUSD", "PYUSD", "USDE", "DAI"} | PROBLEM_ASSETS
        top20_dict = {}
        for t in usdt_pairs:
            sym = t["symbol"]
            base = sym.replace("USDT", "")
            vol = float(t.get("quoteVolume", 0))
            if base in reject_set or vol < 1_000_000:
                continue
            top20_dict[base] = {
                "symbol": sym,
                "price": float(t["lastPrice"]),
                "change_24h": float(t["priceChangePercent"]),
                "volume_24h": vol,
            }
            if len(top20_dict) >= 20:
                break
        return top20_dict
    except Exception as e:
        log(f"[MARKET] Error: {e}")
        return {}

# Signal
def get_batch_adx(assets):
    """Fetch ADX multiple symbols concurrently (threaded)."""
    import concurrent.futures
    result = {}
    def _adx(base):
        sym = f"{base}USDT"
        return base, get_adx(sym)
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(_adx, b): b for b in assets}
        for f in concurrent.futures.as_completed(futures, timeout=15):
            try:
                base, adx = f.result()
                result[base] = adx
            except Exception:
                pass
    return result

def get_ai_signal(market_data, rsi_data=None, adx_data=None, last_asset="", excluded_assets=None):
    try:
        rsi_data = rsi_data or {}
        adx_data = adx_data or {}

        # Batasi maksimal 5 koin teratas (by volume) untuk prompt ultra-singkat
        top_coins = list(market_data.items())[:5]

        lines = []
        for coin, data in top_coins:
            change = data.get('change_24h', 0)
            vol = data.get('volume_24h', 0) / 1e6
            rsi = rsi_data.get(coin)
            adx = adx_data.get(coin)
            # Format ultra-singkat: XRP:1.42 Chg=+1.4% Vol=243M RSI=24.7 ADX=22
            parts = [f"{coin}:{data['price']:.4g}"]
            parts.append(f"Chg={change:+.1f}%")
            parts.append(f"Vol={vol:.0f}M")
            if rsi is not None:
                parts.append(f"RSI={rsi:.1f}")
            if adx is not None:
                parts.append(f"ADX={adx:.1f}")
            lines.append(" ".join(parts))

        excluded_line = ""
        if excluded_assets:
            excluded_str = ", ".join(excluded_assets)
            excluded_line = f"- ASET SUDAH PUNYA POSISI: {excluded_str}. DILARANG keras memilih aset yang ada list ini. Jika memilihnya, sinyal akan dianggap INVALID.\n- WAJIB pilih koin LAIN yang tidak ada list ASET SUDAH PUNYA POSISI.\n"

        market_text = "\n".join(lines)
        prompt = (
            f"Pilih SATU koin untuk BUY/SELL. JANGAN pilih stablecoin, SOL, UNI, ADA, EUL, GIGGLE. "
            f"JANGAN pilih koin yang sama berturut-turut (Terakhir: {{last_asset}}).\n"
            f"{{excluded_line}}{{market_text}}\n"
            f'Balas HANYA dengan JSON murni tanpa markdown: '
            f'{{\"coin\":\"SYMBOL\", \"action\":\"BUY/SELL\", \"confidence\":\"XX%\", \"reason\":\"alasan\"}}'
        )

        url = OMNIROUTER_URL
        omni_api_key = os.getenv("OMNIROUTER_API_KEY", "")

        for attempt in range(3):
            p = prompt
            if attempt > 0 and last_asset:
                p += f"\nPERINGATAN: {last_asset} SUDAH dipilih pada sinyal terakhir. Pilih koin LAIN selain {last_asset}."

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

            # Use robust HTTP client: handles 502, JSON extraction, retry
            sig, raw_content = call_ai_signal(url, payload, headers, max_retries=1)

            if not sig:
                log(f"[AI] No valid signal extracted on attempt {{attempt + 1}}: {{raw_content[:200] if raw_content else 'unknown'}}")
                continue

            # New format uses 'coin' key, confidence as int
            coin = sig.get("coin") or sig.get("asset", "")
            base = _normalize_base(coin)
            if not base:
                log("[AI] No valid coin in response, retry")
                continue

            confidence = sig.get("confidence")
            if confidence is None or not isinstance(confidence, int):
                log("[AI] No confidence in signal, retry")
                continue

            if base not in market_data:
                log(f"[AI] Asset {{coin}} not top 20, retrying")
                continue

            if base == last_asset and last_asset:
                log(f"[AI] {{base}} sama dengan sinyal terakhir, retry rotasi")
                continue

            if excluded_assets and base in excluded_assets:
                log(f"[AI] {{base}} ada di excluded (posisi terbuka/terakhir), retry rotasi")
                continue

            # Convert to old format for compatibility
            sig["asset"] = f"{{base}}USDT"
            sig["confidence"] = f"{{confidence}}%"

            # handle typo entry_zoe
            if "entry_zone" not in sig and "entry_zoe" in sig:
                sig["entry_zone"] = sig.pop("entry_zoe")

            return sig

        return None
    except Exception as e:
        log(f"[AI] Error: {{e}}")
        return None

def _normalize_base(asset):
    """MUB-USDT / DOGEUSD / bnb123 / SOLX1024 -> MUB / DOGE / BNB / SOLX. Return '' jika tak layak."""
    a = "".join(c for c in str(asset).upper() if c.isalnum())
    for suffix in ("USDT", "USD"):
        if a.endswith(suffix) and len(a) > len(suffix):
            a = a[: -len(suffix)]
            break
    a = re.sub(r"\d+", "", a)
    if a in ("USDT", "USD"):
        return ""
    return a if re.fullmatch(r"[A-Z]{2,10}", a) else ""

def _levenshtein(s1, s2):
    """Hitung jarak edit antar dua string."""
    if len(s1) < len(s2):
        return _levenshtein(s2, s1)
    if len(s2) == 0:
        return len(s1)
    prev_row = list(range(len(s2) + 1))
    for i, c1 in enumerate(s1):
        curr_row = [i + 1]
        for j, c2 in enumerate(s2):
            insertions = prev_row[j + 1] + 1
            deletions = curr_row[j] + 1
            substitutions = prev_row[j] + (c1 != c2)
            curr_row.append(min(insertions, deletions, substitutions))
        prev_row = curr_row
    return prev_row[-1]

def _fuzzy_match(text, targets, max_distance=2):
    """Pilih target dengan jarak edit paling kecil dari text."""
    best = None
    best_dist = max_distance + 1
    text_upper = text.upper()
    for t in targets:
        d = _levenshtein(text_upper, t)
        if d < best_dist:
            best_dist = d
            best = t
    return best

def _parse_ai_json(content):
    """Coba tiap blok {{...}} satu-satu, AMBIL YANG TERAKHIR valid (model suka echo contoh JSON dulu, jawaban asli di blok akhir). Gagal semua: regex fallback per-field. Tambah: fuzzy matching untuk typo action/asset, strict schema validation."""
    found = None
    for block in re.findall(r"\{[^{}]*\}", content):
        try:
            s = json.loads(block)
            if isinstance(s, dict) and s.get("action") and s.get("asset"):
                # Fuzzy match action dan asset pada JSON valid
                raw_action = str(s.get("action", "")).upper()
                fuzzy_action = _fuzzy_match(raw_action, ["BUY", "SELL"], max_distance=1)
                s["action"] = fuzzy_action or raw_action
                
                raw_asset = str(s.get("asset", "")).upper()
                base = _normalize_base(raw_asset)
                if base:
                    s["asset"] = raw_asset  # Keep original format
                else:
                    known_assets = ["BTC", "ETH", "XRP", "SOL", "DOGE", "ADA", "MUB", "ZEC", "XAU", "HEI", "MIRA", "SNXXB", "SPCXB", "CRCLB", "HFT", "SYN", "BABY", "BANK", "RLUSD", "PAXG", "U"]
                    fuzzy_asset = _fuzzy_match(raw_asset, known_assets, max_distance=2)
                    if fuzzy_asset:
                        s["asset"] = raw_asset
                found = s
        except Exception:
            continue
    
    if found:
        return found
    
    s = {}
    # Fuzzy match action: BUY/SELL dengan toleransi typo
    m = re.search(r'"?action"?\s*[:=]\s*"?([A-Z]+)"?', content, re.I)
    if m:
        raw_action = m.group(1).upper()
        fuzzy_action = _fuzzy_match(raw_action, ["BUY", "SELL"], max_distance=1)
        s["action"] = fuzzy_action or raw_action
    
    # Fuzzy match asset: normalisasi fuzzy jika typo
    m = re.search(r'"?asset"?\s*[:=]\s*"?([A-Z0-9\-_]+)"?', content, re.I)
    if m:
        raw_asset = m.group(1).upper()
        base = _normalize_base(raw_asset)
        if base:
            s["asset"] = raw_asset  # Keep original format (MUBUSDT) if valid
        else:
            # normalization failed, try fuzzy match against known assets
            known_assets = ["BTC", "ETH", "XRP", "SOL", "DOGE", "ADA", "MUB", "ZEC", "XAU", "HEI", "MIRA", "SNXXB", "SPCXB", "CRCLB", "HFT", "SYN", "BABY", "BANK", "RLUSD", "PAXG", "U"]
            fuzzy_asset = _fuzzy_match(raw_asset, known_assets, max_distance=2)
            if fuzzy_asset:
                s["asset"] = raw_asset  # Keep original format
    
    m = re.search(r'"?confidence"?\s*[:=]\s*"?\(?([0-9.]+)%?"?\s*"?', content, re.I)
    if m:
        s["confidence"] = m.group(1) + "%"
    
    m = re.search(r'"?entry_?z?o?n?e?"?\s*[:=]\s*"?\(?([0-9.]+)?"?\s*"?', content, re.I)
    if m:
        s["entry_zone"] = float(m.group(1))
    
    # Strict validation: action harus BUY/SELL, asset harus ada
    if s.get("action") not in ("BUY", "SELL"):
        s["action"] = None
    if not s.get("asset"):
        s["asset"] = None
    
    if s.get("action") and s.get("asset"):
        return s
    return None

# Discord
def send_discord_webhook(url, embed, mention=False):
    try:
        payload = {"embeds": [embed]}
        if mention:
            payload["content"] = f"<@{DISCORD_USER_ID}>"
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code in (200, 204):
            return True
        log(f"[DISCORD] HTTP {r.status_code}")
        return False
    except Exception as e:
        log(f"[DISCORD] Error: {e}")
        return False

def make_signal_embed(signal, price):
    action = signal["action"]
    color = 0x00FF00 if action == "BUY" else (0xFF0000 if action == "SELL" else 0x808080)
    
    # Fallback: compute current price when outputs "price" or None
    entry = signal.get("entry_zone")
    tp = signal.get("take_profit")
    sl = signal.get("stop_loss")
    
    if not entry or str(entry).lower() == "price":
        entry = round(price * 1.0024) if action == "BUY" else round(price * 0.9984)
    if not tp or str(tp).lower() == "price":
        tp = round(price * 1.034)
    if not sl or str(sl).lower() == "price":
        sl = round(price * 0.974)
    
    return {
        "title": f"Signal {action} {signal['asset']}",
        "color": color,
        "fields": [
            {"name": "Entry Zone", "value": str(entry), "inline": True},
            {"name": "Take Profit", "value": str(tp), "inline": True},
            {"name": "Stop Loss", "value": str(sl), "inline": True},
            {"name": "R:R Ratio", "value": str(signal.get("rrr", "N/A")), "inline": True},
            {"name": "Confidence", "value": str(signal.get("confidence", "N/A")), "inline": True},
            {"name": "Current Price", "value": str(price), "inline": True},
            {"name": "Reason", "value": signal.get("reason", "N/A"), "inline": False},
        ],
        "footer": {"text": "Ollama AI"}
    }

_down_alerted = False

def alert_down(reason):
    global _down_alerted
    if _down_alerted:
        return
    _down_alerted = True
    embed = {
        "title": "Bridge DOWN",
        "color": 0xFF0000,
        "description": reason,
        "fields": [{"name": "Time", "value": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "inline": False}],
    }
    send_discord_webhook(DISCORD_WEBHOOK_URL, embed, mention=True)
    log(f"[ALERT] {reason}")

# Open Position Tracking
def _validate_price(base, ticker_price):
    """Sanity check: ticker price must within 50% last kline close, within known ranges, within 50% last-known price."""
    global _price_history
    try:
        sym = f"{base}USDT"
        r = requests.get("https://data-api.binance.vision/api/v3/klines",
                         params={"symbol": sym, "interval": "1h", "limit": 24}, timeout=5)
        klines = r.json()
        if not klines:
            log(f"[SANITY] {base}: no kline data, allow")
            return True
        kline_close = float(klines[-1][4])
        if kline_close == 0:
            log(f"[SANITY] {base}: invalid kline close {kline_close}, allow")
            return True
        diff_pct = abs(ticker_price - kline_close) / kline_close * 100
        if diff_pct > 50:
            log(f"[SANITY] {base}: ticker={ticker_price} vs kline={kline_close} diff={diff_pct:.1f}% > 50%, REJECT")
            return False
        
        # 24h median anchor: catches first-touch poison
        closes = sorted(float(k[4]) for k in klines if float(k[4]) > 0)
        if closes:
            median = closes[len(closes) // 2]
            if ticker_price > median * 10 or ticker_price < median / 10:
                log(f"[SANITY] {base}: ticker={ticker_price} vs 24h median={median} out of 10x band, REJECT")
                return False
        
        # Range check: prevent price swaps
        if base in ("ZEC", "SOL") and ticker_price < 10:
            log(f"[SANITY] {base}: price {ticker_price} low (< 10), REJECT")
            return False
        if base == "XAUT" and ticker_price < 100:
            log(f"[SANITY] {base}: price {ticker_price} low (< 100), REJECT")
            return False
        if base == "XRP" and ticker_price > 10:
            log(f"[SANITY] {base}: price {ticker_price} high (> 10), REJECT")
            return False
        
        # Last-known-price guard
        last = _price_history.get(base)
        if last:
            last_price, last_ts = last
            if time.time() - last_ts < 600 and last_price > 0:
                jump_pct = abs(ticker_price - last_price) / last_price * 100
                if jump_pct > 50:
                    log(f"[SANITY] {base}: ticker={ticker_price} vs last-known={last_price} jump={jump_pct:.1f}% > 50%, REJECT")
                    return False
        
        _price_history[base] = (ticker_price, time.time())
        return True
    except Exception as e:
        log(f"[SANITY] {base}: validation error {e}, allow")
        return True

def get_live_price(symbol):
    try:
        r = requests.get("https://data-api.binance.vision/api/v3/ticker/price",
                         params={"symbol": symbol}, timeout=10)
        r.raise_for_status()
        return float(r.json().get("price", 0))
    except Exception as e:
        log(f"[PRICE] {symbol}: {e}")
        return None

def update_trailing_stop(pos, current_price):
    """Ratchet arah profit saat harga bergerak menguntungkan. Jarak trailing = jarak awal (1.0 ATR, disimpan posisi saat open). Mutates pos in-place; hanya ratchet satu arah (never widen)."""
    if not TRAIL_ENABLED:
        return
    action = pos["action"]
    entry = float(pos["entry_price"])
    trail = pos.get("trail_dist")
    if not trail:
        sl = pos.get("sl")
        if not sl:
            return
        trail = pos["trail_dist"] = round(abs(float(sl) - entry), 6)
    
    if action == "BUY":
        best = pos["best_price"] = max(pos.get("best_price", current_price), current_price)
        new_sl = round(best - trail, 6)
        if new_sl > float(pos["sl"]):
            pos["sl"] = new_sl
            log(f"[TRAIL] {pos['asset']} ratchet {new_sl} (best {best})")
    else:
        best = pos["best_price"] = min(pos.get("best_price", current_price), current_price)
        new_sl = round(best + trail, 6)
        if new_sl < float(pos["sl"]):
            pos["sl"] = new_sl
            log(f"[TRAIL] {pos['asset']} ratchet down {new_sl} (best {best})")

def check_open_positions(prices):
    positions = read_jsonl(OPEN_POS_LOG)
    if not positions:
        return 0, 0.0
    
    closed = []
    still_open = []
    total_floating = 0.0
    
    for pos in positions:
        asset = pos["asset"]
        base = asset.replace("USDT", "")
        current_price = prices.get(base)
        if not current_price:
            current_price = get_live_price(asset)
        if not current_price:
            still_open.append(pos)
            continue
        
        entry = pos["entry_price"]
        action = pos["action"]
        size_usdt = pos.get("size_usdt", 0)
        tp = pos.get("tp")
        sl = pos.get("sl")
        closed_flag = False
        
        # Trailing stop: ratchet dulu sebelum cek TP/SL
        update_trailing_stop(pos, current_price)
        
        if action == "BUY":
            pnl_pct = (current_price - entry) / entry * 100
            if current_price >= float(tp):
                pnl = size_usdt * float(tp) / entry - size_usdt
                pos["result"] = "WIN"
                closed_flag = True
            elif current_price <= float(sl):
                pnl = size_usdt * float(sl) / entry - size_usdt
                pos["result"] = "WIN" if pnl > 0 else "LOSS"
                closed_flag = True
            else:
                pnl = size_usdt * pnl_pct / 100
        else:
            pnl_pct = (entry - current_price) / entry * 100
            if current_price <= float(tp):
                pnl = size_usdt * entry / float(tp) - size_usdt
                pos["result"] = "WIN"
                closed_flag = True
            elif current_price >= float(sl):
                pnl = size_usdt * entry / float(sl) - size_usdt
                pos["result"] = "WIN" if pnl > 0 else "LOSS"
                closed_flag = True
            else:
                pnl = size_usdt * pnl_pct / 100
        
        if closed_flag:
            pos["exit_price"] = current_price
            pos["pnl_usdt"] = round(pnl, 4)
            pos["closed_at"] = datetime.now().isoformat()
            closed.append(pos)
            
            # LIVE: tutup posisi exchange cancel order
            if not os.getenv("TKO_DRY_RUN", "true").lower() in ("1", "true", "yes"):
                ex = tko_exec.get_exchange()
                sl_id = pos.get("sl_order_id")
                close_side = "SELL" if pos["action"] == "BUY" else "BUY"
                
                sl_hit = (pos["result"] == "LOSS" and sl_id) or (
                    pos["result"] == "WIN" and sl_id and current_price <= float(pos["sl"]) and pos["action"] == "BUY") or (
                    pos["result"] == "WIN" and sl_id and current_price >= float(pos["sl"]) and pos["action"] == "SELL")
                
                if sl_id and sl_hit:
                    tko_exec.cancel_exchange_order(ex, f"{asset.replace('USDT', '')}/USDT", sl_id)
                    log(f"[EXEC] {asset} closed via exchange SL order, skip close order")
                else:
                    # TP/trailing hit: order harus di-cancel dulu, baru close real
                    tko_exec.cancel_exchange_order(ex, f"{asset.replace('USDT', '')}/USDT", sl_id)
                    tko_exec.execute_trade(close_side, asset, pos.get("size_usdt", 1.2) / current_price)
                
                log(f"[CLOSE] {asset} {pos['result']} {current_price} PnL={pnl:+.4f} USDT (~Rp{idr(pnl)})")
                send_discord_webhook(DISCORD_WEBHOOK_TRADE_URL, {
                    "title": f"Position Closed: {asset}",
                    "color": 0x00FF00 if pos["result"] == "WIN" else 0xFF0000,
                    "fields": [
                        {"name": "Result", "value": pos["result"], "inline": True},
                        {"name": "PnL", "value": f"{pnl:+.4f} USDT", "inline": True},
                        {"name": "Exit Price", "value": str(current_price), "inline": True},
                        {"name": "Closed At", "value": pos["closed_at"], "inline": False},
                    ],
                    "mention": True
                })
        else:
            pos["current_price"] = current_price
            pos["floating_pnl"] = round(pnl, 4)
            pos["floating_pct"] = round(pnl_pct, 2)
            still_open.append(pos)
            total_floating += pnl
    
    with open(OPEN_POS_LOG, "w") as f:
        for p in still_open:
            f.write(json.dumps(p, default=str) + "\n")
    
    for c in closed:
        append_jsonl(TRADES_LOG, {
            "timestamp": c.get("opened_at", ""),
            "closed_at": c["closed_at"],
            "action": c["action"],
            "asset": c["asset"],
            "entry_price": c["entry_price"],
            "exit_price": c["exit_price"],
            "result": c["result"],
            "pnl_usdt": c["pnl_usdt"],
            "size_usdt": c.get("size_usdt", 0),
            "status": "CLOSED",
        })
        record_realised_loss(c.get("pnl_usdt", 0.0))
    
    return len(still_open), round(total_floating, 4)

def add_position(signal, price, size_usdt=None):
    if size_usdt is None:
        size_usdt = tko_exec.estimate_position_size(price)
    
    # ATR API error (HTTP 400/symbol not found) -> skip position, TP/SL risky
    atr = get_atr(signal["asset"])
    if atr is False:
        log(f"[POS] Skipped {signal['asset']} ATR API error (symbol not listed or delisted)")
        return None
    
    if atr:
        if signal["action"] == "BUY":
            tp = round(price + 1.5 * atr, 6)
            sl = round(price - 1.0 * atr, 6)
        else:
            tp = round(price - 1.5 * atr, 6)
            sl = round(price + 1.0 * atr, 6)
    else:
        # Fallback
        if signal["action"] == "BUY":
            tp = round(price * 1.0086, 6)
            sl = round(price * 0.9956, 6)
        else:
            tp = round(price * 0.9926, 6)
            sl = round(price * 1.0056, 6)
    
    pos = {
        "opened_at": datetime.now().isoformat(),
        "action": signal["action"],
        "asset": signal["asset"],
        "entry_price": price,
        "tp": tp,
        "sl": sl,
        "trail_dist": round(abs(sl - price), 6),
        "size_usdt": size_usdt,
        "confidence": signal.get("confidence", "N/A"),
    }
    
    # LIVE: place exchange-side stop-loss
    if not os.getenv("TKO_DRY_RUN", "true").lower() in ("1", "true", "yes"):
        ex = tko_exec.get_exchange()
        if ex is not None:
            sl_qty = size_usdt / price
            sl_id = tko_exec.place_stop_loss(ex, f"{signal['asset'].replace('USDT', '')}/USDT", signal["action"], sl_qty, sl)
            pos["sl_order_id"] = sl_id
    
    append_jsonl(OPEN_POS_LOG, pos)
    log(f"[POS] Opened {signal['action']} {signal['asset']} @ {price} | TP={tp} SL={sl}")

def log_positions_summary(prices):
    positions = read_jsonl(OPEN_POS_LOG)
    if not positions:
        log("[POS] No open positions")
        return
    log(f"[POS] {len(positions)} open positions:")
    total_f = 0.0
    for p in positions:
        base = p["asset"].replace("USDT", "")
        cur = prices.get(base) or get_live_price(p["asset"]) or p["entry_price"]
        entry = p["entry_price"]
        action = p["action"]
        if action == "BUY":
            f_pct = (cur - entry) / entry * 100
        else:
            f_pct = (entry - cur) / entry * 100
        pnl = p.get("size_usdt", 0) * f_pct / 100
        total_f += pnl
        log(f"  {p['asset']} entry={entry} cur={cur:.6f} {f_pct:+.2f}% PnL={pnl:+.4f} USDT (~Rp{idr(pnl)})")
    log(f"[POS] Total floating: {total_f:+.4f} USDT (~Rp{idr(total_f)})")

# Trade Execution
TKO_DRY_RUN = os.getenv("TKO_DRY_RUN", "true").lower() in ("1", "true", "yes")
TKO_CONFIDENCE_MIN = int(os.getenv("TKO_CONFIDENCE_MIN", "80"))

def adaptive_conf_min(adx):
    """Dynamic confidence floor by trend strength (ADX). Strong trend -> trust more (lower floor); extreme ADX > 50 reversal risk, strict again. None -> default."""
    if adx is None:
        return TKO_CONFIDENCE_MIN
    if adx > 50:
        return 85
    if adx > 30:
        return 70
    if adx > 20:
        return 75
    return TKO_CONFIDENCE_MIN

def execute_tko_trade(signal, price):
    if not signal or signal["action"] == "NEUTRAL":
        return None
    conf_str = str(signal.get("confidence", "0%"))
    conf = int(re.search(r'(\d+)', conf_str).group(1)) if re.search(r'(\d+)', conf_str) else 0
    conf_min = adaptive_conf_min(signal.get("adx"))
    if conf < conf_min:
        log(f"[TKO] Confidence {conf}% < floor {conf_min}% (ADX {signal.get('adx')}), skip")
        return None
    
    size_usdt = tko_exec.estimate_position_size(price)
    
    if TKO_DRY_RUN:
        log(f"[TKO] DRY-RUN: {signal['action']} {signal['asset']} {price} ({size_usdt:.2f} USDT)")
        append_jsonl(TRADES_LOG, {
            "timestamp": datetime.now().isoformat(),
            "action": signal["action"], "asset": signal["asset"],
            "price": price, "size_usdt": size_usdt, "status": "DRY_RUN",
        })
        return {"action": signal["action"], "dry_run": True}
    
    # LIVE: real order
    order = tko_exec.execute_trade(signal["action"], signal["asset"], size_usdt, price)
    if order:
        append_jsonl(TRADES_LOG, {
            "timestamp": datetime.now().isoformat(),
            "action": signal["action"], "asset": signal["asset"],
            "price": price, "size_usdt": size_usdt,
            "status": "FILLED", "order_id": order.get("id"),
        })
        return {"action": signal["action"], "dry_run": False, "order_id": order.get("id")}
    
    append_jsonl(TRADES_LOG, {
        "timestamp": datetime.now().isoformat(),
        "action": signal["action"], "asset": signal["asset"],
        "price": price, "size_usdt": size_usdt, "status": "FAILED",
    })
    log(f"[TKO] LIVE order FAILED: {signal['asset']} {price} ({size_usdt:.2f} USDT)")
    embed = {
        "title": "Order FAILED",
        "color": 0xE74C3C,
        "fields": [
            {"name": "Asset", "value": signal["asset"], "inline": True},
            {"name": "Action", "value": signal["action"], "inline": True},
            {"name": "Price", "value": str(price), "inline": True},
            {"name": "Size", "value": f"{size_usdt:.2f} USDT", "inline": True},
            {"name": "Status", "value": "LIVE execution failed", "inline": False},
        ],
        "footer": {"text": "Tokocrypto"},
        "timestamp": datetime.now().isoformat(),
    }
    send_discord_webhook(DISCORD_WEBHOOK_TRADE_URL, embed)
    return None

def send_trade_notification(signal, order, price):
    if not signal:
        return
    action = signal["action"]
    color = 0x00FF00 if action == "BUY" else 0xFF0000
    embed = {
        "title": f"Trade Executed: {signal['asset']}",
        "color": color,
        "fields": [
            {"name": "Action", "value": action, "inline": True},
            {"name": "Asset", "value": signal["asset"], "inline": True},
            {"name": "Price", "value": str(price), "inline": True},
            {"name": "Status", "value": "DRY-RUN" if order and order.get("dry_run") else "LIVE", "inline": True},
            {"name": "Confidence", "value": signal.get("confidence", "N/A"), "inline": True},
        ],
        "footer": {"text": "Tokocrypto"}
    }
    send_discord_webhook(DISCORD_WEBHOOK_TRADE_URL, embed)

# Main Loop
def has_open_position(asset):
    positions = read_jsonl(OPEN_POS_LOG)
    return any(p.get("asset") == asset for p in positions)

def prune_stale_positions():
    """Close positions older than STALE_POSITION_HOURS to prevent zombie slots."""
    positions = read_jsonl(OPEN_POS_LOG)
    if not positions:
        return 0
    now = datetime.now().timestamp()
    stale = []
    remaining = []
    for pos in positions:
        opened_at = pos.get("opened_at", "")
        if opened_at:
            try:
                opened_ts = datetime.fromisoformat(opened_at).timestamp()
                if now - opened_ts > STALE_POSITION_HOURS * 3600:
                    stale.append(pos["asset"])
                    continue
            except (ValueError, TypeError):
                pass
        remaining.append(pos)
    
    if stale:
        with open(OPEN_POS_LOG, "w") as f:
            for p in remaining:
                f.write(json.dumps(p, default=str) + "\n")
        log(f"[STALE] Closed {len(stale)} stale position(s): {', '.join(stale)}")
    return len(stale)

def get_open_assets():
    positions = read_jsonl(OPEN_POS_LOG)
    return list({p.get("asset") for p in positions if p.get("asset")})

def loss_cooldown_active(asset):
    """True jika asset terakhir CLOSE loss dalam LOSS_COOLDOWN_HOURS (anti re-entry coin baru hit)."""
    if LOSS_COOLDOWN_HOURS == 0:
        return False
    cutoff = time.time() - LOSS_COOLDOWN_HOURS * 3600
    for t in reversed(read_jsonl(TRADES_LOG)):
        if t.get("asset") != asset or t.get("status") != "CLOSED":
            continue
        try:
            closed_ts = datetime.fromisoformat(t.get("closed_at", "")).timestamp()
        except Exception:
            closed_ts = float(t.get("timestamp_epoch", 0))
        if closed_ts < cutoff:
            break
        pnl = t.get("pnl_usdt")
        if pnl is not None and pnl < 0:
            return True
    return False

def _filter_signal(signal, adx_indicators, rsi_indicators, prices):
    """Filter signal by ADX strength & RSI alignment. Return signal if passes, None if rejected."""
    asset = signal.get("asset", "")
    base = asset.replace("USDT", "")
    action = signal.get("action", "")
    rsi = rsi_indicators.get(base)
    adx = adx_indicators.get(base)
    
    # ADX minimum check
    ADX_MIN = float(os.getenv("TKO_ADX_MIN", "20"))
    if adx is not None and adx < ADX_MIN:
        log(f"[FILTER] {base} ADX {adx:.1f} < {ADX_MIN} (weak trend), reject")
        return None
    
    # RSI alignment check
    if rsi is not None:
        if action == "BUY" and rsi > 55:
            log(f"[FILTER] {base} BUY RSI {rsi:.1f} > 55 (overbought), reject")
            return None
        if action == "SELL" and rsi < 45:
            log(f"[FILTER] {base} SELL RSI {rsi:.1f} < 45 (oversold), reject")
            return None
    
    return signal

def main_loop():
    global _down_alerted
    main_loop._counter = getattr(main_loop, "_counter", 0) + 1
    
    log("[BRIDGE] Running...")
    
    market_data = get_market_data()
    if not market_data:
        log("[BRIDGE] No market data, skip")
        alert_down("No market data — bridge down?")
        return
    _down_alerted = False
    
    prices = {}
    for base, coin in market_data.items():
        prices[base] = coin["price"]
    
    open_count, floating = check_open_positions(prices)
    if open_count:
        log(f"[POS] {open_count} open floating {floating:+.4f} USDT (~Rp{idr(floating)})")
    
    # Stale position cleanup
    prune_stale_positions()
    
    # Max positions guard
    if open_count >= MAX_OPEN_POSITIONS:
        log(f"[GUARD] Max positions ({MAX_OPEN_POSITIONS}) reached, skip cycle")
        return
    
    # Circuit breaker
    if circuit_paused():
        log("[CIRCUIT] Daily loss limit reached — trading paused until daily reset")
        return
    
    # Log positions summary every 5 cycles
    if main_loop._counter % 5 == 0:
        log_positions_summary(prices)
    
    # Last signal for rotation
    last_sig = read_jsonl(SIGNALS_LOG)
    last_sig = last_sig[-1] if last_sig else None
    last_price = float(last_sig.get("entry_price", 0)) if last_sig else None
    last_asset = last_sig.get("asset", "").replace("USDT", "") if last_sig else ""
    
    # Price stability check
    if last_price and last_asset and last_asset in prices:
        curr = prices[last_asset]
        pct_change = abs(curr - last_price) / last_price * 100
        if not TKO_DRY_RUN and pct_change < THRESHOLD:
            log(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Harga stabil, skip")
            return
    
    open_assets = get_open_assets()
    ADX_MIN = float(os.getenv("TKO_ADX_MIN", "20"))
    
    excluded = [last_asset] if last_asset else []
    if open_assets:
        excluded = list(set(excluded + open_assets))
    
    rsi_indicators = get_batch_rsi(list(market_data.keys()))
    log(f"[RSI] Fetched {len(rsi_indicators)} coins")
    adx_indicators = get_batch_adx(list(market_data.keys()))
    log(f"[ADX] Fetched {len(adx_indicators)} coins")
    
    # ADX filter: skip coins with weak trend before calling AI
    weak_adx = {k for k, v in adx_indicators.items() if v is not None and v < ADX_MIN}
    if weak_adx:
        log(f"[ADX] Weak trend (ADX < {ADX_MIN}): {', '.join(sorted(weak_adx)[:10])}")
    
    signal = get_ai_signal(market_data, rsi_indicators, adx_indicators, last_asset, excluded)
    if not signal:
        # No random fallback = no coinflip
        log("[AI] No valid signal from AI, skip cycle")
        return
    
    signal = _filter_signal(signal, adx_indicators, rsi_indicators, prices)
    if not signal:
        log("[FILTER] Signal rejected by ADX/RSI filter, skip cycle")
        return
    
    asset = signal["asset"]
    base = asset.replace("USDT", "")
    
    price = prices.get(base)
    if not price:
        log(f"[BRIDGE] No price for {asset}")
        return
    
    # Sanity: ticker price must match kline close
    if not _validate_price(base, price):
        log(f"[SANITY] {base} ticker={price} diverges from kline, skip cycle")
        return
    
    max_retries = 3
    for _retry in range(max_retries):
        if not has_open_position(asset):
            break
        excluded.append(asset)
        log(f"[GUARD] {asset} sudah ada posisi terbuka, retry {_retry + 1}/{max_retries}")
        signal = get_ai_signal(market_data, rsi_indicators, adx_indicators, last_asset, excluded)
        if not signal:
            log("[AI] Retry failed, no valid signal, skip cycle")
            return
        asset = signal["asset"]
    
    # Retry loop picked different asset -> recompute base/price/adx
    base = asset.replace("USDT", "")
    signal["adx"] = adx_indicators.get(base)
    price = prices.get(base)
    if not price:
        log(f"[BRIDGE] No price for {asset}")
        return
    
    if has_open_position(asset):
        log(f"[GUARD] {asset} masih sudah terbuka setelah {max_retries} retry, skip")
        log("[COOLDOWN] keras kepala memilih aset dengan posisi, skip 2 cycle (180s)...")
        time.sleep(180)
        return
    
    sig_record = {
        "timestamp": datetime.now().isoformat(),
        "timestamp_epoch": datetime.now().timestamp(),
        "action": signal["action"],
        "asset": asset,
        "entry_price": price,
        "entry_zone": signal.get("entry_zone"),
        "take_profit": signal.get("take_profit"),
        "stop_loss": signal.get("stop_loss"),
        "rrr": signal.get("rrr"),
        "confidence": signal.get("confidence"),
        "reason": signal.get("reason"),
        "price": price,
    }
    append_jsonl(SIGNALS_LOG, sig_record)
    log(f"[{asset}] {signal['action']} {price} (conf {signal.get('confidence')})")
    
    embed = make_signal_embed(signal, price)
    send_discord_webhook(DISCORD_WEBHOOK_URL, embed)
    
    order = execute_tko_trade(signal, price)
    if order:
        send_trade_notification(signal, order, price)
    add_position(signal, price)

if __name__ == "__main__":
    log(f"[BRIDGE] Started. Ollama ({AI_MODEL}) Discord. TKO_DRY_RUN={TKO_DRY_RUN}")
    schedule.every(INTERVAL).seconds.do(main_loop)
    main_loop()
    while True:
        try:
            schedule.run_pending()
        except Exception as e:
            log(f"[BRIDGE] CRASH: {e}")
            alert_down("Bridge crashed: " + str(e))
        time.sleep(1)
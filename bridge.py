import requests, json, re, time, os, schedule, random
from pathlib import Path
from datetime import datetime
import execution as tko_exec

# ── Constants ────────────────────────────────────────────────────────────
NINJA_ROUTER_URL = "http://127.0.0.1:11434"
DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/1532938091331977337/EQV4Kt_E6a2jH97EeBEFvuJdcrqcw0Qt_-y5wNyDJYCvReHqwg6cUOG9_lTDwKNRaKli"
# Phase 4: health/system alerts channel; fallback ke webhook sinyal kalau kosong
DISCORD_HEALTH_WEBHOOK = os.getenv("DISCORD_HEALTH_WEBHOOK_URL", DISCORD_WEBHOOK_URL)
DISCORD_WEBHOOK_TRADE_URL = "https://discord.com/api/webhooks/1533282533264064552/BxCca57xNw2vzG-XlUmlUForDY5DeRybLEzkPYoUOMisvS7NbeAS135xR3e9VVT_LIRA"
DISCORD_USER_ID = "1464229528317464765"
MODEL = "qwen2.5:3b"
# Symbols that frequently fail Binance API (400 / delisted / illiquid) — never trade these
PROBLEM_ASSETS = {"SNDK", "SNDKB", "SOL", "GIGGLE", "UNI", "ADA"}  # SOL/GIGGLE/UNI/ADA: loss konsisten (2026-08-06, 4W/14L -0.115 USDT)
_price_history = {}  # base -> (price, ts): last ACCEPTED ticker price, untuk sanity guard anti price-swap
INTERVAL = 90
THRESHOLD = 0.5
MAX_OPEN_POSITIONS = int(os.getenv("TKO_MAX_POSITIONS", "5"))
TRAIL_ENABLED = os.getenv("TKO_TRAIL_ENABLED", "1") == "1"
STALE_POSITION_HOURS = 24
SIGNALS_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "signals.jsonl")
TRADES_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trades.jsonl")
OPEN_POS_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "open_positions.jsonl")
EVAL_HOURS = 1

# ── Helpers ──────────────────────────────────────────────────────────────
USDT_TO_IDR = 16000  # patokan kurs: 1 USDT = Rp 16.000

def idr(v):
    return f"{round(v * USDT_TO_IDR):,}".replace(",", ".")

BRIDGE_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bridge.log")
LOG_MAX_BYTES = 10 * 1024 * 1024  # rotate di 10MB (plan 8.2)

def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}")
    try:
        if os.path.exists(BRIDGE_LOG) and os.path.getsize(BRIDGE_LOG) > LOG_MAX_BYTES:
            os.replace(BRIDGE_LOG, BRIDGE_LOG + ".bak")
        with open(BRIDGE_LOG, "a") as f:
            f.write(f"[{ts}] {msg}\n")
    except OSError:
        pass  # log failure tidak boleh mematikan bridge

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

# ── Market Data ──────────────────────────────────────────────────────────
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
            return False  # sentinel: API error, do NOT open position
        r.raise_for_status()
        klines = r.json()
        if len(klines) < period + 1:
            return None
        trs = []
        for i in range(1, len(klines)):
            h = float(klines[i][2])
            l = float(klines[i][3])
            c = float(klines[i - 1][4])
            trs.append(max(h - l, abs(h - c), abs(l - c)))
        return sum(trs) / len(trs)
    except requests.exceptions.Timeout:
        log(f"[ATR] {symbol}: timeout")
        return None
    except Exception as e:
        log(f"[ATR] {symbol}: {e}")
        return None

def get_rsi(symbol, period=14):
    """Compute RSI from 1h klines (recent ~14h). Returns None on error."""
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
        gains, losses = 0, 0
        for i in range(1, len(closes)):
            delta = closes[i] - closes[i - 1]
            if delta >= 0:
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

def get_adx(symbol, period=14):
    """Compute ADX from 1h klines. Returns None on error."""
    try:
        url = "https://data-api.binance.vision/api/v3/klines"
        params = {"symbol": symbol, "interval": "1h", "limit": period + 20}
        r = requests.get(url, params=params, timeout=10)
        if r.status_code == 400:
            return None
        r.raise_for_status()
        klines = r.json()
        if len(klines) < period + 20:
            return None
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
        atr = [0.0] * (period) + [sum(tr[i - period + 1:i + 1]) / period for i in range(period, len(tr))]
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
        adx = sum(dx[-period:]) / period if dx else 0.0
        return round(adx, 1)
    except Exception:
        return None

def get_batch_rsi(assets):
    """Fetch RSI for multiple symbols concurrently (threaded)."""
    import concurrent.futures
    result = {}
    def _rsi(base):
        sym = f"{base}USDT"
        r = get_rsi(sym)
        return base, r
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

# ── AI Signal ────────────────────────────────────────────────────────────
def get_batch_adx(assets):
    """Fetch ADX for multiple symbols concurrently (threaded)."""
    import concurrent.futures
    result = {}
    def _adx(base):
        sym = f"{base}USDT"
        adx = get_adx(sym)
        return base, adx
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
        lines = []
        for coin, data in market_data.items():
            change = data.get('change_24h', 0)
            vol = data.get('volume_24h', 0) / 1e6
            rsi = rsi_data.get(coin)
            adx = adx_data.get(coin)
            rsi_str = f" RSI={rsi}" if rsi is not None else ""
            adx_str = f" ADX={adx}" if adx is not None else ""
            lines.append(f"- {coin}: Price={data['price']:.6f} Change={change:+.2f}% Vol={vol:.1f}M{rsi_str}{adx_str}")
        excluded_line = ""
        if excluded_assets:
            excluded_str = ", ".join(excluded_assets)
            excluded_line = f"- ASET SUDAH PUNYA POSISI: {excluded_str}. DILARANG keras memilih aset yang ada di list ini. Jika memilihnya, sinyal akan dianggap INVALID.\n- WAJIB pilih koin LAIN yang tidak ada di list ASET SUDAH PUNYA POSISI.\n"
        market_text = "\n".join(lines)
        prompt = f"""Kamu adalah analis pasar crypto. Pilih SATU koin terbaik dari data di bawah untuk BUY atau SELL.
PERATURAN KRUSIAL:
- WAJIB pilih dari daftar koin di bawah ini (pilih yang PALING JELAS terlihat bullish ATAU bearish)
- TOLAK semua pair berikut: EULUSDT, BTCUSDT, ETHUSDT, USDTUSDC, BUSDUSDT, TUSDUSDT, USDCUSDT, USD1USDT, FDUSDUSDT, PYUSDUSDT, USDEUSDT, DAIUSDT (stablecoin/forex, bukan crypto spekulatif)
- TOLAK pair dengan volume < 1 juta
- Pilih hanya crypto spot USDT pairs
- TIDAK BOLEH memilih pair yang sama 2 kali berturut-turut
- INDIKATOR RSI: RSI < 30 = oversold (mendukung BUY), RSI > 70 = overbought (mendukung SELL). Gunakan RSI sebagai konfirmasi, bukan sinyal tunggal.
- INDIKATOR ADX: ADX < 20 = lemah/tren sideways (hindari entry), ADX >= 20 = tren cukup kuat (izinkan entry). ADX > 50 = tren sangat kuat (hati-hati reversi).
- WAJIB sertakan field "confidence" berisi angka persen 50-95 (contoh "confidence":"75%"). JANGAN pernah menghilangkan atau mengosongkan confidence.
- Sinyal terakhir: {last_asset}. JANGAN pilih {last_asset} kali ini, wajib rotasi ke koin lain
{excluded_line}Data pasar (hanya dari list ini yang boleh dipilih):
{market_text}
Berikan 1 sinyal trading dalam format JSON (HANYA JSON, tanpa markdown):
Contoh output yang BENAR (numeric, bukan string):
{{"action":"BUY","asset":"XRPUSDT","entry_zone":1.0649,"take_profit":1.07000,"stop_loss":1.06000,"rrr":"1:2","confidence":"75%","reason":"alasan singkat"}}

Contoh output yang SALAH (JANGAN tulis seperti ini):
{{"action":"BUY","asset":"XRPUSDT","entry_zone":"price","take_profit":"price","stop_loss":"price"}}  <- SALAH, jangan pakai "price"
Output HANYA JSON tanpa markdown."""
        url = f"{NINJA_ROUTER_URL}/api/generate"
        for attempt in range(3):
            p = prompt
            if attempt >= 1 and last_asset:
                p += f"\nPERINGATAN: {last_asset} SUDAH dipilih pada sinyal terakhir. Pilih koin LAIN selain {last_asset}."
            payload = {
                "model": MODEL,
                "prompt": p,
                "stream": False,
                "keep_alive": "10m",
                "options": {"temperature": 0.7, "top_p": 0.9, "top_k": 40, "repeat_penalty": 1.3, "num_predict": 150, "seed": random.randint(1, 99999)}
            }
            r = requests.post(url, json=payload, timeout=(60, 180))
            r.raise_for_status()
            data = r.json()
            content = data.get("response", "").strip()
            sig = _parse_ai_json(content)
            if not sig:
                continue
            base = _normalize_base(sig.get("asset", ""))
            if not base:
                log("[AI] No valid asset in response, retry")
                continue
            if "confidence" not in sig or not re.search(r"\d", str(sig.get("confidence", ""))):
                log("[AI] No confidence in signal, retry")
                continue
            if base not in market_data:
                log(f"[AI] Asset '{sig.get('asset')}' not in top 20, retrying")
                continue
            if last_asset and base == last_asset:
                log(f"[AI] {base} sama dgn sinyal terakhir, retry rotasi")
                continue
            if excluded_assets and base in excluded_assets:
                log(f"[AI] {base} ada di excluded (posisi terbuka/terakhir), retry rotasi")
                continue
            sig["asset"] = f"{base}USDT"
            if "entry_zoe" in sig and "entry_zone" not in sig:
                sig["entry_zone"] = sig.pop("entry_zoe")
            return sig
        return None
    except Exception as e:
        log(f"[AI] Error: {e}")
        return None

def _normalize_base(asset):
    """MUB-USDT / DOGEUSD / bnb123 / SOLX1024 -> MUB / DOGE / BNB / SOLX. '' jika tak layak."""
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
    """Coba tiap blok {...} satu-satu, AMBIL YANG TERAKHIR valid (model suka
    echo contoh JSON dulu, jawaban asli di blok akhir); gagal semua -> regex fallback per-field.
    Tambah: fuzzy matching untuk typo action/asset, strict schema validation."""
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
                        s["asset"] = raw_asset  # Keep original format
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
    # Fuzzy match asset: normalisasi + fuzzy jika typo
    m = re.search(r'"?asset"?\s*[:=]\s*"?([A-Z0-9\-_]+)"?', content, re.I)
    if m:
        raw_asset = m.group(1).upper()
        # First normalize (strip suffixes, digits)
        base = _normalize_base(raw_asset)
        if base:
            s["asset"] = raw_asset  # Keep original format (MUBUSDT) if valid
        else:
            # If normalization failed, try fuzzy match against known assets
            known_assets = ["BTC", "ETH", "XRP", "SOL", "DOGE", "ADA", "MUB", "ZEC", "XAU", "HEI", "MIRA", "SNXXB", "SPCXB", "CRCLB", "HFT", "SYN", "BABY", "BANK", "RLUSD", "PAXG", "U"]
            fuzzy_asset = _fuzzy_match(raw_asset, known_assets, max_distance=2)
            if fuzzy_asset:
                s["asset"] = raw_asset  # Keep original format
    m = re.search(r'["\']?confidence["\']?\s*[:=]\s*["\']?\(?([0-9.]+)%?"?\s*["\']?', content, re.I)
    if m:
        s["confidence"] = m.group(1) + "%"
    m = re.search(r'["\']?entry_?z?o?n?e?["\']?\s*[:=]\s*["\']?\(?([0-9.]+)?"?\s*["\']?', content, re.I)
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

# ── Discord ──────────────────────────────────────────────────────────────
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
    # Fallback: compute from current price when AI outputs "price" or None
    entry = signal.get("entry_zone")
    tp = signal.get("take_profit")
    sl = signal.get("stop_loss")
    if not entry or str(entry) == "price":
        entry = round(price * 1.002, 4) if action == "BUY" else round(price * 0.998, 4)
    if not tp or str(tp) == "price":
        tp = round(price * 1.03, 4)
    if not sl or str(sl) == "price":
        sl = round(price * 0.97, 4)
    return {
        "title": "Signal {0} {1}".format(action, signal["asset"]),
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

# ── Open Position Tracking ──────────────────────────────────────────────
def _validate_price(base, ticker_price):
    """Sanity check: ticker price must be within 50% of last kline close AND within known ranges AND within 50% of last-known price."""
    global _price_history
    try:
        sym = f"{base}USDT"
        r = requests.get("https://data-api.binance.vision/api/v3/klines",
                         params={"symbol": sym, "interval": "1h", "limit": 24},
                         timeout=5)
        klines = r.json()
        if not klines:
            log(f"[SANITY] {base}: no kline data, allow")
            return True
        kline_close = float(klines[-1][4])
        if kline_close <= 0:
            log(f"[SANITY] {base}: invalid kline close {kline_close}, allow")
            return True
        diff_pct = abs(ticker_price - kline_close) / kline_close * 100
        if diff_pct >= 50:
            log(f"[SANITY] {base}: ticker={ticker_price} vs kline={kline_close} diff={diff_pct:.1f}% > 50%, REJECT")
            return False
        # 24h median anchor: catches first-touch poison (BICO 10.16 vs real ~0.04)
        # even when kline close is ALSO poisoned — median is robust to a few bad candles.
        closes = sorted(float(k[4]) for k in klines if float(k[4]) > 0)
        if closes:
            median = closes[len(closes) // 2]
            if ticker_price > median * 10 or ticker_price < median / 10:
                log(f"[SANITY] {base}: ticker={ticker_price} vs 24h median={median} out of 10x band, REJECT")
                return False
        # Range check: prevent price swaps (e.g., XRP@1.04 assigned to ZEC)
        if base in ("ZEC", "SOL") and ticker_price < 10:
            log(f"[SANITY] {base}: price {ticker_price} too low (< 10), REJECT")
            return False
        if base == "XAUT" and ticker_price < 100:
            log(f"[SANITY] {base}: price {ticker_price} too low (< 100), REJECT")
            return False
        if base == "XRP" and ticker_price > 10:
            log(f"[SANITY] {base}: price {ticker_price} too high (> 10), REJECT")
            return False
        # Last-known-price guard: catches swaps where ticker AND kline are both poisoned
        # (e.g. BICO real ~0.04 got 10.16). Baseline only counts if fresh (< 10 min)
        # so a bot restart after hours of downtime doesn't false-reject real moves.
        last = _price_history.get(base)
        if last:
            last_price, last_ts = last
            if time.time() - last_ts <= 600 and last_price > 0:
                jump_pct = abs(ticker_price - last_price) / last_price * 100
                if jump_pct >= 50:
                    log(f"[SANITY] {base}: ticker={ticker_price} vs last-known={last_price} jump={jump_pct:.1f}% > 50%, REJECT")
                    return False
        _price_history[base] = (ticker_price, time.time())
        return True
    except Exception as e:
        log(f"[SANITY] {base}: validation error {e}, allow")
        return True  # on error, don't block

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
    """Ratchet SL ke arah profit saat harga bergerak menguntungkan.
    Jarak trailing = jarak SL awal (1.0 ATR, disimpan di posisi saat open).
    Mutates pos in place; hanya ratchet satu arah (never widen)."""
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
        best = pos["best_price"] = max(pos.get("best_price") or current_price, current_price)
        new_sl = round(best - trail, 6)
        if new_sl > float(pos["sl"]):
            pos["sl"] = new_sl
            log(f"[TRAIL] {pos['asset']} SL ratchet up → {new_sl} (best {best})")
    else:
        best = pos["best_price"] = min(pos.get("best_price") or current_price, current_price)
        new_sl = round(best + trail, 6)
        if new_sl < float(pos["sl"]):
            pos["sl"] = new_sl
            log(f"[TRAIL] {pos['asset']} SL ratchet down → {new_sl} (best {best})")

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

        # Trailing stop: ratchet SL dulu sebelum cek TP/SL (SL baru dipakai di cek bawah)
        update_trailing_stop(pos, current_price)

        if action == "BUY":
            pnl_pct = (current_price - entry) / entry * 100
            if tp and current_price >= float(tp):
                pnl = size_usdt * float(tp) / entry - size_usdt
                pos["result"] = "WIN"
                closed_flag = True
            elif sl and current_price <= float(sl):
                pnl = size_usdt * float(sl) / entry - size_usdt
                pos["result"] = "WIN" if pnl > 0 else "LOSS"
                closed_flag = True
            else:
                pnl = size_usdt * pnl_pct / 100
        else:
            pnl_pct = (entry - current_price) / entry * 100
            if tp and current_price <= float(tp):
                pnl = size_usdt * entry / float(tp) - size_usdt
                pos["result"] = "WIN"
                closed_flag = True
            elif sl and current_price >= float(sl):
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
            log(f"[CLOSE] {asset} {pos['result']} @ {current_price} | PnL={pnl:+.4f} USDT (~Rp {idr(pnl)})")
            send_discord_webhook(DISCORD_WEBHOOK_TRADE_URL, {
                "title": "Position Closed: " + asset,
                "color": 0x00FF00 if pos["result"] == "WIN" else 0xFF0000,
                "fields": [
                    {"name": "Result", "value": pos["result"], "inline": True},
                    {"name": "PnL", "value": f"{pnl:+.4f} USDT", "inline": True},
                    {"name": "Exit Price", "value": str(current_price), "inline": True},
                    {"name": "Closed At", "value": pos["closed_at"], "inline": False},
                ],
            }, mention=True)
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

    return len(still_open), round(total_floating, 4)

def add_position(signal, price, size_usdt=None):
    if size_usdt is None:
        size_usdt = tko_exec.estimate_position_size(price)
    # ATR API error (HTTP 400/symbol not found) → skip position, no TP/SL = too risky
    atr = get_atr(signal["asset"])
    if atr is False:
        log(f"[POS] Skipped {signal['asset']} — ATR API error (symbol not listed or delisted)")
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
            tp = round(price * 1.008, 6)
            sl = round(price * 0.995, 6)
        else:
            tp = round(price * 0.992, 6)
            sl = round(price * 1.005, 6)
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
    append_jsonl(OPEN_POS_LOG, pos)
    log(f"[POS] Opened {signal['action']} {signal['asset']} @ {price} | TP={tp} SL={sl}")

def log_positions_summary(prices):
    positions = read_jsonl(OPEN_POS_LOG)
    if not positions:
        log("[POS] No open positions")
        return
    log(f"[POS] {len(positions)} open positions:")
    total_f = 0
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
        log(f"  {p['asset']} | entry={entry} cur={cur:.6f} | {f_pct:+.2f}% | PnL={pnl:+.4f} USDT (~Rp {idr(pnl)})")
    log(f"[POS] Total floating: {total_f:+.4f} USDT (~Rp {idr(total_f)})")

# ── Trade Execution ──────────────────────────────────────────────────────
TKO_DRY_RUN = os.getenv("TKO_DRY_RUN", "true").lower() in ("1", "true", "yes")
TKO_CONFIDENCE_MIN = int(os.getenv("TKO_CONFIDENCE_MIN", "80"))

def adaptive_conf_min(adx):
    """Dynamic confidence floor by trend strength (ADX). Strong trend -> trust AI more
    (lower floor); extreme ADX >= 50 -> reversal risk, strict again. None -> default."""
    if adx is None:
        return TKO_CONFIDENCE_MIN
    if adx >= 50:
        return 85
    if adx >= 30:
        return 70
    if adx >= 20:
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
        log(f"[TKO] DRY-RUN: would {signal['action']} {signal['asset']} @ {price} ({size_usdt:.2f} USDT)")
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
    log(f"[TKO] LIVE order FAILED: {signal['asset']} @ {price} ({size_usdt:.2f} USDT)")
    embed = {
        "title": "❌ Order FAILED",
        "color": 0xE74C3C,
        "fields": [
            {"name": "Asset", "value": signal["asset"], "inline": True},
            {"name": "Action", "value": signal["action"], "inline": True},
            {"name": "Price", "value": str(price), "inline": True},
            {"name": "Size", "value": f"{size_usdt:.2f} USDT", "inline": True},
            {"name": "Status", "value": "LIVE - execution failed", "inline": False},
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

# ── Main Loop ────────────────────────────────────────────────────────────
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
                ts = datetime.fromisoformat(opened_at).timestamp()
                if now - ts > STALE_POSITION_HOURS * 3600:
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
    if open_count > 0:
        log(f"[POS] {open_count} open | floating {floating:+.4f} USDT (~Rp {idr(floating)})")

    # Stale position cleanup (positions older than STALE_POSITION_HOURS)
    prune_stale_positions()

    # Max positions guard — stop taking new trades if full
    if open_count >= MAX_OPEN_POSITIONS:
        log(f"[GUARD] Max positions ({MAX_OPEN_POSITIONS}) reached, skip cycle")
        return

    if main_loop._counter % 5 == 0:
        log_positions_summary(prices)

    last_sig = read_jsonl(SIGNALS_LOG)
    last_sig = last_sig[-1] if last_sig else None
    last_price = float(last_sig.get("entry_price", 0)) if last_sig else None
    last_asset = last_sig.get("asset", "").replace("USDT", "") if last_sig else ""
    if last_price and last_asset in prices:
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
    log(f"[RSI] Fetched for {len(rsi_indicators)} coins")
    adx_indicators = get_batch_adx(list(market_data.keys()))
    log(f"[ADX] Fetched for {len(adx_indicators)} coins")
    # ADX filter: skip coins with weak trend before calling AI
    weak_adx = {b for b, v in adx_indicators.items() if v is not None and v < ADX_MIN}
    if weak_adx:
        log(f"[ADX] Weak trend (ADX<{ADX_MIN}): {', '.join(sorted(weak_adx)[:10])}")
    signal = get_ai_signal(market_data, rsi_indicators, adx_indicators, last_asset, excluded)
    if not signal:
        # Forced fallback: pick random valid asset to avoid idle cycles
        candidates = [b for b in market_data if b not in excluded]
        if candidates:
            base = random.choice(candidates)
            price = prices[base]
            action = random.choice(["BUY", "SELL"])
            log(f"[FALLBACK] Random pick {base} {action} @ {price:.6f}")
            signal = {
                "action": action,
                "asset": f"{base}USDT",
                "entry_zone": round(price, 6),
                "take_profit": round(price * 1.015, 6),
                "stop_loss": round(price * 0.985, 6),
                "confidence": "40%",
                "reason": "forced fallback"
            }
        else:
            log("[BRIDGE] No valid candidates, skip cycle")
            return

    asset = signal["asset"]
    base = asset.replace("USDT", "")

    price = prices.get(base)
    if not price:
        log(f"[BRIDGE] No price for {asset}")
        return
    # Sanity: ticker price must match kline close (catches intermittent Binance API glitches)
    if not _validate_price(base, price):
        log(f"[SANITY] {base} ticker={price} diverges from kline, skip cycle")
        return

    max_retries = 3
    for _retry in range(max_retries):
        if not has_open_position(asset):
            break
        excluded.append(asset)
        log(f"[GUARD] {asset} sudah ada posisi terbuka, retry {1 + _retry}/{max_retries}")
        signal = get_ai_signal(market_data, rsi_indicators, adx_indicators, last_asset, excluded)
        if not signal:
            # Fallback on retry failure too
            candidates = [b for b in prices if b not in excluded]
            if candidates:
                base = random.choice(candidates)
                price = prices[base]
                action = random.choice(["BUY", "SELL"])
                log(f"[FALLBACK] Retry random pick {base} {action} @ {price:.6f}")
                signal = {
                    "action": action,
                    "asset": f"{base}USDT",
                    "entry_zone": round(price, 6),
                    "take_profit": round(price * 1.015, 6),
                    "stop_loss": round(price * 0.985, 6),
                    "confidence": "40%",
                    "reason": "forced fallback"
                }
            else:
                log("[BRIDGE] No valid candidates after retries, skip")
                return
        asset = signal["asset"]

    # Retry loop may have picked a different asset — recompute base/price/adx
    base = asset.replace("USDT", "")
    signal["adx"] = adx_indicators.get(base)
    price = prices.get(base)
    if not price:
        log(f"[BRIDGE] No price for {asset}")
        return

    if has_open_position(asset):
        log(f"[GUARD] {asset} masih sudah terbuka setelah {max_retries} retry, skip")
        log(f"[COOLDOWN] AI keras kepala memilih aset dengan posisi, skip 2 cycle (180s)...")
        time.sleep(180)  # skip 2 cycles (2 * INTERVAL) to let positions close
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
    log(f"[{asset}] {signal['action']} @ {price} (conf {signal.get('confidence')})")
    embed = make_signal_embed(signal, price)
    send_discord_webhook(DISCORD_WEBHOOK_URL, embed)
    order = execute_tko_trade(signal, price)
    if order:
        send_trade_notification(signal, order, price)
        add_position(signal, price)

if __name__ == "__main__":
    log(f"[BRIDGE] Started. Ollama ({MODEL}) -> Discord. TKO_DRY_RUN={TKO_DRY_RUN}")
    schedule.every(INTERVAL).seconds.do(main_loop)
    main_loop()
    while True:
        try:
            schedule.run_pending()
        except Exception as e:
            log(f"[BRIDGE] CRASH: {e}")
            alert_down("Bridge crashed: " + str(e))
            time.sleep(INTERVAL)
        time.sleep(1)

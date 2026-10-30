import wallet_manager
import requests, json, re, time, os, schedule, math
from pathlib import Path
from datetime import datetime
import random
import execution as tko_exec
import recovery_policy
import journal_policy
from position_snapshot import read_jsonl_snapshot, write_jsonl_snapshot
from robust_http import call_ai_signal, extract_json_from_text

# Constants
OMNIROUTER_URL=os.getenv("OMNIROUTER_URL", "https://api.groq.com/openai/v1")
AI_MODEL=os.getenv("AI_MODEL", "openai/gpt-oss-20b")
FALLBACK_AI_URL=os.getenv("FALLBACK_AI_URL", "https://openrouter.ai/api/v1")
GEMINI_API_KEY=os.getenv("GEMINI_API_KEY", "")
GEMINI_AI_URL="https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
GEMINI_AI_MODEL=os.getenv("GEMINI_AI_MODEL", "gemini-flash-lite-latest")
FALLBACK_AI_MODEL=os.getenv("FALLBACK_AI_MODEL", "nvidia/nemotron-3.5-lightning:free")
FALLBACK_AI_KEY=os.getenv("FALLBACK_AI_KEY", os.getenv("OPENROUTER_API_KEY", ""))

# Multi-key rotation helpers
_last_groq_idx = 0

# Per-key rate-limit cooldowns: (tier, key index) -> epoch seconds.
# Prevents retrying a 429-limited key on every attempt/cycle.
_key_cooldown = {}
AI_RATE_LIMIT_COOLDOWN = float(os.getenv("TKO_AI_RATE_LIMIT_COOLDOWN", "60"))
AI_RATE_LIMIT_MAX_COOLDOWN = float(os.getenv("TKO_AI_RATE_LIMIT_MAX_COOLDOWN", "300"))


def _key_on_cooldown(tier, idx):
    return time.time() < _key_cooldown.get((tier, idx), 0.0)


def _mark_key_cooldown(tier, idx, err):
    """Put a key on cooldown using Retry-After when the provider sent one."""
    wait = AI_RATE_LIMIT_COOLDOWN
    m = re.search(r"retry_after=(\d+(?:\.\d+)?)", err or "")
    if m:
        wait = float(m.group(1))
    wait = max(5.0, min(wait, AI_RATE_LIMIT_MAX_COOLDOWN))
    _key_cooldown[(tier, idx)] = time.time() + wait
    log(f"[AI] {tier} key_{idx+1} cooldown {wait:.0f}s (HTTP 429)")

def _get_groq_keys():
    """Return list of all Groq API keys (deduplicated, primary first)."""
    keys = []
    primary = os.getenv("OMNIROUTER_API_KEY", os.getenv("GROQ_API_KEY", ""))
    if primary:
        keys.append(primary)
    for i in range(1, 10):
        k = os.getenv(f"GROQ_KEY_{i}", "")
        if k and k not in keys:
            keys.append(k)
    return keys

def _get_gemini_keys():
    """Return list of all Gemini API keys (deduplicated, primary first)."""
    keys = []
    primary = os.getenv("GEMINI_API_KEY", "")
    if primary:
        keys.append(primary)
    for i in range(1, 10):
        k = os.getenv(f"GEMINI_KEY_{i}", "")
        if k and k not in keys:
            keys.append(k)
    return keys
DISCORD_WEBHOOK_URL=os.getenv("DISCORD_WEBHOOK_URL", "")
DISCORD_HEALTH_WEBHOOK=os.getenv("DISCORD_HEALTH_WEBHOOK_URL", DISCORD_WEBHOOK_URL)
DISCORD_WEBHOOK_TRADE_URL=os.getenv("DISCORD_WEBHOOK_TRADE_URL", os.getenv("DISCORD_TRADE_WEBHOOK_URL", os.getenv("DISCORD_WEBHOOK_URL", "")))
DISCORD_USER_ID= os.getenv("DISCORD_USER_ID", "1464229528317464765")

PROBLEM_ASSETS = {"SNDK", "SNDKB", "SOL", "GIGGLE", "UNI", "ADA", "ERA", "QNT", "NIL", "LTC"}

_price_history = {}

INTERVAL = 90
THRESHOLD = 0.5
MAX_OPEN_POSITIONS = int(os.getenv("TKO_MAX_POSITIONS", "5"))
TRAIL_ENABLED = os.getenv("TKO_TRAIL_ENABLED", "1") == "1"
STALE_POSITION_HOURS = 24

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SIGNALS_LOG = os.path.join(BASE_DIR, "signals.jsonl")
TRADES_LOG = os.path.join(BASE_DIR, "trades.jsonl")
OPEN_POS_LOGS = {
    "DRY_RUN": os.path.join(BASE_DIR, "open_positions.dry_run.jsonl"),
    "LIVE": os.path.join(BASE_DIR, "open_positions.live.jsonl"),
}
LEGACY_POSITION_LOGS = [
    os.path.join(BASE_DIR, "open_positions.jsonl"),
    os.path.join(BASE_DIR, "open_ke_positions.jsonl"),
]
CIRCUIT_STATE = os.path.join(BASE_DIR, "circuit_state.json")

DAILY_LOSS_LIMIT = float(os.getenv("TKO_DAILY_LOSS_LIMIT", "2.40"))
CIRCUIT_DISABLED = os.getenv("TKO_CIRCUIT_DISABLED", "0") == "1"
EVAL_HOURS = 1
TREND_MIN_ADX = float(os.getenv("TKO_TREND_MIN_ADX", "25"))
LOSS_COOLDOWN_HOURS = float(os.getenv("TKO_LOSS_COOLDOWN_HOURS", "6"))

# --- Streak breaker: N loss beruntun -> pause entry sementara ---
MAX_CONSECUTIVE_LOSSES = int(os.getenv("TKO_MAX_CONSECUTIVE_LOSSES", "4"))
STREAK_COOLDOWN_HOURS = float(os.getenv("TKO_STREAK_COOLDOWN_HOURS", "2"))
STREAK_STATE = os.path.join(BASE_DIR, "streak_state.json")

# --- Tuning sisi BUY (default = perilaku lama; ubah via .env untuk mengaktifkan) ---
# Riset 106 trade: BUY dengan RSI 60-70 hanya 7% winrate (-0.0976 USDT),
# sedangkan BUY RSI 30-50 = 64-80% winrate. Default 70 mempertahankan perilaku lama.
BUY_RSI_MAX = float(os.getenv("TKO_BUY_RSI_MAX", "70"))
# Riset: BUY dengan ADX 40-50 hanya 10% winrate. Disarankan 45 untuk eksperimen berikutnya.
BUY_ADX_MAX = float(os.getenv("TKO_BUY_ADX_MAX", "65"))
# Alignment arah tren (+DI vs -DI, 1h). 0 = mati (perilaku lama), 1 = aktif.
TREND_ALIGN_ENABLED = os.getenv("TKO_TREND_ALIGN", "0") == "1"

USDT_TO_IDR = 16000

BRIDGE_LOG = os.path.join(BASE_DIR, "bridge.log")
LOG_MAX_BYTES = 10 * 1024 * 1024

# Single-instance guard: cegah systemd + manual + Render jalan bareng.
INSTANCE_LOCK = os.path.join(BASE_DIR, "bridge.lock")
LOCK_EXIT_CODE = 75

# Statistik alasan reject (buat tuning filter pakai data, bukan feeling).
REJECT_STATS_LOG = os.path.join(BASE_DIR, "reject_stats.jsonl")
REJECT_ALERT_STREAK = int(os.getenv("TKO_REJECT_ALERT_STREAK", "6"))
_reject_streak = 0

# Helpers
def idr(v):
    return f"{round(v * USDT_TO_IDR):,}".replace(",", ".")

def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}")
    try:
        if os.path.exists(BRIDGE_LOG) and os.path.getsize(BRIDGE_LOG) > LOG_MAX_BYTES:
            os.replace(BRIDGE_LOG, BRIDGE_LOG + ".bak")
        with open(BRIDGE_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}\n")
    except OSError:
        pass

def append_jsonl(path, obj):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, default=str) + "\n")

def read_jsonl(path):
    items = []
    if not os.path.exists(path):
        return items
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    items.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return items

# --- Reject stats: satu record per cycle, dipakai untuk tuning filter ---
_cycle_rejects = {}

def bump_reject(reason, base=""):
    """Catat satu kandidat yang ditolak (in-memory, di-flush tiap akhir cycle)."""
    entry = _cycle_rejects.setdefault(reason, {"count": 0, "assets": []})
    entry["count"] += 1
    if base and len(entry["assets"]) < 20:
        entry["assets"].append(base)

def _reject(base, reason):
    bump_reject(reason, base)
    return None

def flush_reject_stats():
    """Tulis ringkasan reject cycle ini ke reject_stats.jsonl + log."""
    global _cycle_rejects
    if not _cycle_rejects:
        return
    append_jsonl(REJECT_STATS_LOG, {
        "ts": datetime.now().isoformat(),
        "counts": {k: v["count"] for k, v in _cycle_rejects.items()},
        "assets": {k: v["assets"] for k, v in _cycle_rejects.items()},
    })
    total = sum(v["count"] for v in _cycle_rejects.values())
    detail = " ".join(f"{k}={v['count']}" for k, v in sorted(_cycle_rejects.items()))
    log(f"[STATS] reject cycle: {total} ({detail})")
    _cycle_rejects = {}

def log_reject_summary(hours=24):
    """Agregasi reject dari reject_stats.jsonl selama N jam terakhir."""
    cutoff = time.time() - hours * 3600
    totals = {}
    cycles = 0
    for rec in read_jsonl(REJECT_STATS_LOG):
        try:
            ts = datetime.fromisoformat(rec.get("ts", "")).timestamp()
        except (ValueError, TypeError):
            continue
        if ts < cutoff:
            continue
        cycles += 1
        for reason, count in (rec.get("counts") or {}).items():
            totals[reason] = totals.get(reason, 0) + int(count)
    if not totals:
        return
    top = " ".join(f"{k}={v}" for k, v in sorted(totals.items(), key=lambda kv: -kv[1]))
    log(f"[STATS] reject {hours}h ({cycles} cycle): {sum(totals.values())} kandidat | {top}")
    try:
        if os.path.getsize(REJECT_STATS_LOG) > 2 * 1024 * 1024:
            os.replace(REJECT_STATS_LOG, REJECT_STATS_LOG + ".bak")
    except OSError:
        pass

def acquire_instance_lock():
    """Ambil flock eksklusif. False kalau instance lain sudah memegang lock."""
    try:
        import fcntl
    except ImportError:
        log("[LOCK] fcntl tidak tersedia (non-Linux), lock dilewati")
        return True
    fh = open(INSTANCE_LOCK, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        log("[LOCK] Instance bridge lain sudah jalan, keluar (kode 75)")
        return False
    fh.write(str(os.getpid()))
    fh.flush()
    globals()["_LOCK_FH"] = fh
    log(f"[LOCK] Instance lock acquired (pid {os.getpid()})")
    return True

def position_log_path(mode=None):
    mode = mode if mode is not None else tko_exec.trading_mode()
    return OPEN_POS_LOGS.get(mode)

def legacy_position_data_pending():
    for path in LEGACY_POSITION_LOGS:
        try:
            if os.path.getsize(path) > 0:
                return True
        except FileNotFoundError:
            continue
        except OSError:
            return True
    return False

def read_position_records(mode=None):
    mode = mode if mode is not None else tko_exec.trading_mode()
    path = position_log_path(mode)
    if path is None:
        return None
    if legacy_position_data_pending():
        log(f"[POS] Legacy position data requires manual review; mode {mode} store is blocked")
        return None

    positions = read_jsonl_snapshot(path, mode)
    if positions is None:
        log(f"[POS] Position store unavailable for mode {mode}")
    return positions

def append_position_record(mode, position):
    path = position_log_path(mode)
    if path is None or read_position_records(mode) is None:
        return False
    record = dict(position)
    record["mode"] = mode
    append_jsonl(path, record)
    return True

def write_position_records(mode, positions):
    path = position_log_path(mode)
    if path is None or read_position_records(mode) is None:
        return False
    records = []
    for position in positions:
        record = dict(position)
        record["mode"] = mode
        records.append(record)
    return write_jsonl_snapshot(path, records)


def quarantine_positions_for_unresolved_closes():
    """Persist exact CLOSE-to-entry matches before later position processing."""
    positions = read_position_records("LIVE")
    if positions is None:
        return False
    quarantined, global_block = journal_policy.quarantine_close_positions(
        positions, tko_exec.unresolved_order_intents())
    if quarantined is None or global_block:
        return False
    if quarantined != positions:
        return write_position_records("LIVE", quarantined)
    return True

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
    if not st.get("paused") and st["realised_loss"] > DAILY_LOSS_LIMIT:
        st["paused"] = True
        log(f"[CIRCUIT] Daily realised loss {st['realised_loss']:.4f} USDT > limit {DAILY_LOSS_LIMIT} USDT -- TRADING PAUSED until daily reset")
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

# Streak breaker
def streak_state():
    st = {"consecutive_losses": 0, "paused_until": 0.0, "last_result": None}
    if os.path.exists(STREAK_STATE):
        try:
            with open(STREAK_STATE, encoding="utf-8") as f:
                st.update(json.load(f))
        except Exception:
            pass
    return st

def record_trade_result(pnl):
    """Update streak counter tiap trade close. N loss beruntun -> pause entry sementara.

    Pakai epoch (time.time) supaya tidak ikut masalah timezone tanggal.
    """
    st = streak_state()
    if pnl < 0:
        st["consecutive_losses"] = int(st.get("consecutive_losses", 0)) + 1
        st["last_result"] = "LOSS"
        if st["consecutive_losses"] >= MAX_CONSECUTIVE_LOSSES and not st.get("paused_until"):
            st["paused_until"] = time.time() + STREAK_COOLDOWN_HOURS * 3600
            log(f"[STREAK] {st['consecutive_losses']} loss beruntun (>= {MAX_CONSECUTIVE_LOSSES}) -- entry dihentikan {STREAK_COOLDOWN_HOURS:g} jam")
            send_discord_webhook(DISCORD_HEALTH_WEBHOOK, {
                "title": "Streak Breaker: Entries Paused",
                "color": 0xFFA500,
                "fields": [
                    {"name": "Consecutive Losses", "value": str(st["consecutive_losses"]), "inline": True},
                    {"name": "Pause", "value": f"{STREAK_COOLDOWN_HOURS:g} jam", "inline": True},
                    {"name": "Note", "value": "Posisi aktif tetap dikelola (TP/SL/trailing)", "inline": False},
                ],
            })
    else:
        st["consecutive_losses"] = 0
        st["last_result"] = "WIN"
    try:
        with open(STREAK_STATE, "w", encoding="utf-8") as f:
            json.dump(st, f)
    except OSError:
        pass

def streak_paused():
    """True selama window pause streak breaker aktif. Reset otomatis setelahnya."""
    st = streak_state()
    until = float(st.get("paused_until") or 0)
    if not until:
        return False
    if time.time() < until:
        return True
    st["paused_until"] = 0.0
    st["consecutive_losses"] = 0
    try:
        with open(STREAK_STATE, "w", encoding="utf-8") as f:
            json.dump(st, f)
        log("[STREAK] Window pause selesai -- entry dibuka kembali")
    except OSError:
        pass
    return False

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
            log(f"[ATR] {symbol}: HTTP 400 -- symbol not listed or delisted")
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
    """Wilder ADX from klines. Return (adx, plus_di_sum, minus_di_sum) or (None, None, None)."""
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

    atr = [0.0] * len(tr)
    for i in range(period, len(tr)):
        atr[i] = sum(tr[i - period + 1:i + 1]) / period

    plus_dm = [0.0] * len(tr)
    minus_dm = [0.0] * len(tr)

    for i in range(1, len(tr)):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dm[i] = up if (up > down and up > 0) else 0.0
        minus_dm[i] = down if (down > up and down > 0) else 0.0

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
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as ex:
        futures = {ex.submit(_rsi, b): b for b in assets}
    for f in concurrent.futures.as_completed(futures, timeout=12):
        try:
            base, rsi = f.result()
            result[base] = rsi
        except Exception:
            pass
    return result

def get_market_data():
    """Fetch top 20 USDT pairs by volume with full price data."""
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
            if len(top20_dict) >= 35:
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
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as ex:
        futures = {ex.submit(_adx, b): b for b in assets}
    for f in concurrent.futures.as_completed(futures, timeout=15):
        try:
            base, adx = f.result()
            result[base] = adx
        except Exception:
            pass
    return result

def get_ai_signal(market_data, rsi_data=None, adx_data=None, last_asset="", excluded_assets=None):
    """Get AI trading signal. market_data must have up to 20 coins with full data."""
    try:
        rsi_data = rsi_data or {}
        adx_data = adx_data or {}

        # Use up to 20 coins from market data for comprehensive prompt
        top_coins = list(market_data.items())[:35]

        lines = []
        for coin, data in top_coins:
            change = data.get('change_24h', 0)
            vol = data.get('volume_24h', 0) / 1e6
            rsi = rsi_data.get(coin)
            adx = adx_data.get(coin)
            # Format: XRP:1.42 Chg=+1.4% Vol=243M RSI=24.7 ADX=22
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
            excluded_line = f"- ASET DILARANG DIPILIH (posisi terbuka / baru ditolak): {excluded_str}. DILARANG memilih aset yang ada list ini. Jika memilihnya, sinyal akan dianggap INVALID.\n- WAJIB pilih koin LAIN yang tidak ada list ASET DILARANG.\n"

        market_text = "\n".join(lines)
        prompt = f"""Pilih SATU koin HANYA dari daftar data pasar bawah untuk BUY atau SELL.
DILARANG KERAS memilih BTC, ETH, SOL, UNI, ADA, EUL, GIGGLE, atau stablecoin.
HANYA koin yang ada list bawah ini yang valid.
{excluded_line}{market_text}
Balas HANYA dengan JSON murni tanpa markdown: {{"coin":"SYMBOL", "action":"BUY/SELL", "confidence":80, "reason":"alasan singkat"}}"""

        primary_url = OMNIROUTER_URL.rstrip("/") + "/chat/completions"
        primary_key = os.getenv("OMNIROUTER_API_KEY", os.getenv("GROQ_API_KEY", ""))
        primary_model = AI_MODEL

        fallback_url = FALLBACK_AI_URL.rstrip("/") + "/chat/completions"
        fallback_key = FALLBACK_AI_KEY
        fallback_model = FALLBACK_AI_MODEL

        for attempt in range(3):
            p = prompt
            if attempt > 0 and last_asset:
                p += f"\nPERINGATAN: {last_asset} SUDAH dipilih pada sinyal terakhir. Pilih koin LAIN selain {last_asset}."

            # ======= MULTI-KEY FAILOVER ENGINE =======
            # Tier 1: Groq (round-robin all keys)
            # Tier 2: Gemini (round-robin all keys)
            # Tier 3: OpenRouter (last resort)
            sig = None
            raw_content = None
            base_payload = {
                "model": primary_model,
                "messages": [
                    {"role": "user", "content": p},
                ],
                "max_tokens": 500,
                "temperature": 0.7,
                "top_p": 0.9,
            }
            payload = base_payload

            # --- Tier 1: Groq multi-key (round-robin rotation) ---
            global _last_groq_idx
            groq_keys = _get_groq_keys()
            n_keys = len(groq_keys)
            for step in range(n_keys):
                if sig:
                    break
                idx = (_last_groq_idx + step) % n_keys
                if _key_on_cooldown("Groq", idx):
                    log(f"[AI] Groq key_{idx+1}/{n_keys} cooldown, skip")
                    continue
                gkey = groq_keys[idx]
                log(f"[AI] Groq key_{idx+1}/{n_keys} ({primary_model})...")
                headers = {"Content-Type": "application/json", "Authorization": f"Bearer {gkey}"}
                sig, raw_content = call_ai_signal(primary_url, base_payload, headers, max_retries=1)
                if sig:
                    _last_groq_idx = idx
                    _key_cooldown.pop(("Groq", idx), None)
                else:
                    if "HTTP 429" in (raw_content or ""):
                        _mark_key_cooldown("Groq", idx, raw_content)
                    log(f"[AI] Groq key_{idx+1} gagal, coba key berikutnya...")

            # --- Tier 2: Gemini multi-key ---
            if not sig:
                gemini_keys = _get_gemini_keys()
                for ki, gmkey in enumerate(gemini_keys, 1):
                    if sig:
                        break
                    gi = ki - 1
                    if _key_on_cooldown("Gemini", gi):
                        log(f"[AI] Gemini key_{ki}/{len(gemini_keys)} cooldown, skip")
                        continue
                    log(f"[AI] Gemini key_{ki}/{len(gemini_keys)} ({GEMINI_AI_MODEL})...")
                    gem_headers = {"Content-Type": "application/json", "Authorization": f"Bearer {gmkey}"}
                    gem_payload = dict(base_payload)
                    gem_payload["model"] = GEMINI_AI_MODEL
                    sig, raw_content = call_ai_signal(GEMINI_AI_URL, gem_payload, gem_headers, max_retries=1)
                    if sig:
                        _key_cooldown.pop(("Gemini", gi), None)
                    else:
                        if "HTTP 429" in (raw_content or ""):
                            _mark_key_cooldown("Gemini", gi, raw_content)
                        log(f"[AI] Gemini key_{ki} gagal, coba key berikutnya...")

            # --- Tier 3: OpenRouter (last resort) ---
            if not sig and fallback_key and not _key_on_cooldown("OpenRouter", 0):
                log(f"[AI] Semua Groq+Gemini keys gagal, fallback ke OpenRouter ({fallback_model})...")
                fallback_headers = {
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {fallback_key}",
                    "HTTP-Referer": "https://pixel-agents.local",
                    "X-Title": "Pixel Trading Bot"
                }
                fallback_payload = dict(base_payload)
                fallback_payload["model"] = fallback_model
                sig, raw_content = call_ai_signal(fallback_url, fallback_payload, fallback_headers, max_retries=1)
                if sig:
                    _key_cooldown.pop(("OpenRouter", 0), None)
                elif "HTTP 429" in (raw_content or ""):
                    _mark_key_cooldown("OpenRouter", 0, raw_content)

            if not sig:
                log(f"[AI] No valid signal extracted on attempt {attempt + 1}: {raw_content[:200] if raw_content else 'unknown'}")
                continue

            # New format uses 'coin' key, confidence int
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
                log(f"[AI] Asset {coin} tidak ada di market data, retrying")
                continue

            if base == last_asset and last_asset:
                log(f"[AI] {base} sama dengan sinyal terakhir, retry rotasi")
                continue

            if excluded_assets and base in excluded_assets:
                log(f"[AI] {base} ada excluded (posisi terbuka/terakhir) retry rotasi")
                continue

            # Convert old format for compatibility
            sig["asset"] = f"{base}USDT"
            sig["confidence"] = f"{confidence}%"

            # Handle typo entry_zoe
            if "entry_zone" not in sig and "entry_zoe" in sig:
                sig["entry_zone"] = sig.pop("entry_zoe")

            return sig

        return None
    except Exception as e:
        log(f"[AI] Error: {e}")
        return None

def _normalize_base(asset):
    """MUB-USDT / DOGEUSD / bnb123 / SOLX1024 -> MUB / DOGE / BNB / SOLX. Return '' if not viable."""
    a = "".join(c for c in str(asset).upper() if c.isalnum())
    for suffix in ("USDT", "USD"):
        if a.endswith(suffix) and len(a) > len(suffix):
            a = a[:-len(suffix)]
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
    """Coba tiap blok {...} satu-satu, AMBIL YANG TERAKHIR valid (model suka echo contoh JSON dulu, jawaban asli di blok akhir). Gagal semua: regex fallback per-field. Tambah: fuzzy matching untuk typo action/asset, strict schema validation."""
    found = None
    for block in re.findall(r'\{[^{}]*\}', content):
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
                        s["asset"] = fuzzy_asset

                found = s
        except Exception:
            continue

    if found:
        return found

    # Regex fallback per-field
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
            s["asset"] = raw_asset  # Keep original format (MUBUSDT) valid
        else:
            # Normalization failed, try fuzzy match against known assets
            known_assets = ["BTC", "ETH", "XRP", "SOL", "DOGE", "ADA", "MUB", "ZEC", "XAU", "HEI", "MIRA", "SNXXB", "SPCXB", "CRCLB", "HFT", "SYN", "BABY", "BANK", "RLUSD", "PAXG", "U"]
            fuzzy_asset = _fuzzy_match(raw_asset, known_assets, max_distance=2)
            if fuzzy_asset:
                s["asset"] = fuzzy_asset

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

    # Fallback: compute current price when outputs "price" None
    entry = signal.get("entry_zone")
    tp = signal.get("take_profit")
    sl = signal.get("stop_loss")

    if not entry or str(entry).lower() == "price":
        entry = round(price * 1.0024 if action == "BUY" else round(price * 0.9984))
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

def alert_reject_streak(streak):
    """Alert kalau N cycle berturut-turut tidak ada kandidat yang lolos filter."""
    embed = {
        "title": "Tidak ada kandidat lolos",
        "color": 0xFFA500,
        "description": f"{streak} cycle berturut-turut semua kandidat ditolak filter.",
        "fields": [
            {"name": "Waktu", "value": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "inline": False},
            {"name": "Cek", "value": "ADX/RSI/cooldown terlalu ketat, atau market sideways.", "inline": False},
        ],
    }
    send_discord_webhook(DISCORD_HEALTH_WEBHOOK, embed)
    log(f"[ALERT] {streak} cycle tanpa kandidat lolos")

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
        closes = sorted([float(k[4]) for k in klines if float(k[4]) > 0])
        if closes:
            median = closes[len(closes) // 2]
            if ticker_price > median * 10 or ticker_price < median / 10:
                log(f"[SANITY] {base}: ticker={ticker_price} vs 24h median={median} out of 10x band, REJECT")
                return False

        # Range check: prevent price swaps
        if base in ("ZEC", "SOL") and ticker_price < 10:
            log(f"[SANITY] {base}: price {ticker_price} low (< 10) REJECT")
            return False
        if base == "XAUT" and ticker_price < 100:
            log(f"[SANITY] {base}: price {ticker_price} low (< 100) REJECT")
            return False
        if base == "XRP" and ticker_price > 10:
            log(f"[SANITY] {base}: price {ticker_price} high (> 10) REJECT")
            return False

        # Last-known-price guard
        last = _price_history.get(base)
        if last:
            last_price, last_ts = last
            if time.time() - last_ts > 600 and last_price > 0:
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
    """Ratchet arah profit saat harga bergerak menguntungkan. Jarak trailing = jarak awal (1.0ATR, disimpan posisi saat open). Mutates pos in-place; hanya ratchet satu arah (never widen)."""
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
    mode = tko_exec.trading_mode()
    if mode == "REJECTED":
        log("[POS] Position processing blocked by trading mode")
        return 0, 0.0

    positions = read_position_records(mode)
    if positions is None:
        log(f"[POS] Position store unavailable for mode {mode}; processing skipped")
        return 0, 0.0
    if not positions:
        return 0, 0.0

    closed = []
    still_open = []
    close_orders_to_commit = []
    total_floating = 0.0

    for pos in positions:
        if pos.get("reconciliation_required"):
            still_open.append(pos)
            continue
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
        previous_sl = float(sl)
        update_trailing_stop(pos, current_price)
        if mode == "LIVE" and float(pos.get("sl", previous_sl)) != previous_sl:
            exchange = tko_exec.get_exchange()
            symbol = f"{asset.replace('USDT', '')}/USDT"
            stop_id = pos.get("sl_order_id")
            if (exchange is None or not stop_id
                    or not tko_exec.cancel_exchange_order(exchange, symbol, stop_id)):
                pos["sl"] = previous_sl
                pos["reconciliation_required"] = True
                pos["reconciliation_reason"] = "could not confirm old stop cancellation before trailing-stop update"
                still_open.append(pos)
                continue
            replacement_id = tko_exec.place_stop_loss(
                exchange, symbol, action, float(pos.get("quantity", 0)), float(pos["sl"]),
            )
            if not replacement_id:
                pos["sl_protection_confirmed"] = False
                pos["reconciliation_required"] = True
                pos["reconciliation_reason"] = "replacement trailing stop-loss acceptance unconfirmed"
                still_open.append(pos)
                continue
            pos["sl_order_id"] = replacement_id
            pos["sl_protection_confirmed"] = True
        sl = pos.get("sl")

        if action == "BUY":
            pnl_pct = (current_price - entry) / entry * 100
            if current_price >= float(tp):
                pnl = size_usdt * (float(tp) / entry - 1)
                pos["result"] = "WIN"
                closed_flag = True
            elif current_price <= float(sl):
                pnl = size_usdt * (float(sl) / entry - 1)
                pos["result"] = "WIN" if pnl > 0 else "LOSS"
                closed_flag = True
            else:
                pnl = size_usdt * pnl_pct / 100
        else:
            pnl_pct = (entry - current_price) / entry * 100
            if current_price <= float(tp):
                pnl = size_usdt * (1 - entry / float(tp))
                pos["result"] = "WIN"
                closed_flag = True
            elif current_price >= float(sl):
                pnl = size_usdt * (1 - entry / float(sl))
                pos["result"] = "WIN" if pnl > 0 else "LOSS"
                closed_flag = True
            else:
                pnl = size_usdt * pnl_pct / 100

        if closed_flag:
            if mode == "LIVE":
                sl_id = pos.get("sl_order_id")
                sl_triggered = (action == "BUY" and current_price <= float(sl)) or (
                    action == "SELL" and current_price >= float(sl)
                )
                if sl_triggered:
                    pos["reconciliation_required"] = True
                    pos["reconciliation_reason"] = "stop-loss trigger observed; exchange fill not yet confirmed"
                    still_open.append(pos)
                    continue
                ex = tko_exec.get_exchange()
                symbol = f"{asset.replace('USDT', '')}/USDT"
                if ex is None or (sl_id and not tko_exec.cancel_exchange_order(ex, symbol, sl_id)):
                    pos["reconciliation_required"] = True
                    pos["reconciliation_reason"] = "could not confirm stop-loss cancellation before close"
                    still_open.append(pos)
                    continue
                close_qty = float(pos.get("quantity", 0) or 0)
                close_side = "SELL" if action == "BUY" else "BUY"
                close_fill = tko_exec.execute_trade(
                    close_side, asset, close_qty * current_price, current_price,
                    base_qty=close_qty, operation_type="CLOSE",
                    related_entry_order_id=pos.get("entry_order_id"),
                )
                if not isinstance(close_fill, dict) or close_fill.get("classification") not in {"CONFIRMED_FILL", "PARTIAL_FILL"}:
                    pos["reconciliation_required"] = True
                    pos["reconciliation_reason"] = "close order is pending or ambiguous"
                    still_open.append(pos)
                    continue
                close_decision = recovery_policy.decide_close_recovery(pos, close_fill)
                if close_decision["decision"] not in {
                        "CLOSE_POSITION", "PARTIAL_CLOSE_RECONCILIATION_REQUIRED"}:
                    pos["reconciliation_required"] = True
                    pos["reconciliation_reason"] = close_decision["reason"] or "close evidence is invalid"
                    still_open.append(pos)
                    continue
                closed_qty = float(close_fill.get("filled_qty") or 0)
                close_price = float(close_fill.get("average_fill_price") or 0)
                if closed_qty <= 0 or close_price <= 0 or closed_qty > close_qty * 1.000000001:
                    pos["reconciliation_required"] = True
                    pos["reconciliation_reason"] = "close fill quantity/price failed validation"
                    still_open.append(pos)
                    continue
                pnl = (close_price - entry) * closed_qty if action == "BUY" else (entry - close_price) * closed_qty
                if close_fill.get("client_order_id"):
                    close_orders_to_commit.append(close_fill["client_order_id"])
                pos["exit_price"] = close_price
                pos["exit_order_id"] = close_fill.get("order_id")
                pos["closed_quantity"] = closed_qty
                pos["pnl_usdt"] = round(pnl, 8)
                if closed_qty < close_qty * (1 - 1e-9):
                    remaining_qty = close_qty - closed_qty
                    replacement_sl_id = tko_exec.place_stop_loss(
                        ex, symbol, action, remaining_qty, float(pos.get("sl", sl)),
                    )
                    pos["quantity"] = remaining_qty
                    pos["size_usdt"] = pos["quantity"] * entry
                    pos["sl_order_id"] = replacement_sl_id
                    pos["sl_protection_confirmed"] = bool(replacement_sl_id)
                    pos["reconciliation_required"] = True
                    pos["reconciliation_reason"] = (
                        "partial close fill requires manual reconciliation" if replacement_sl_id else
                        "partial close and replacement stop acceptance are unconfirmed")
                    still_open.append(pos)
                    continue
            if mode != "LIVE":
                pos["exit_price"] = current_price
                pos["pnl_usdt"] = round(pnl, 4)
                # Kembalikan pokok + laba/rugi ke virtual wallet
                is_win = pnl > 0
                wallet_manager.credit_for_close(float(pos.get("size_usdt", 0)), float(pos["pnl_usdt"]), is_win)
            pos["result"] = "WIN" if pnl > 0 else "LOSS"
            pos["closed_at"] = datetime.now().isoformat()
            closed.append(pos)

            # LIVE: tutup posisi exchange - cancel order
            if mode == "LIVE":
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

    if not write_position_records(mode, still_open):
        log(f"[POS] Position store changed or quarantined for mode {mode}; update skipped")
        return 0, 0.0

    for client_order_id in close_orders_to_commit:
        tko_exec.mark_order_position_recorded(client_order_id)

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
        record_trade_result(c.get("pnl_usdt", 0.0))

    return len(still_open), round(total_floating, 4)

def add_position(signal, price, size_usdt=None, fill=None):
    mode = tko_exec.trading_mode()
    if mode == "REJECTED":
        log("[POS] Position creation blocked by trading mode")
        return None
    if read_position_records(mode) is None:
        log(f"[POS] Position store unavailable for mode {mode}; creation skipped")
        return None

    if not isinstance(fill, dict) or fill.get("classification") not in {"CONFIRMED_FILL", "PARTIAL_FILL", "PENDING", "SIMULATED_FILL"}:
        log("[POS] Position requires a validated fill result")
        return None
    if fill.get("source") not in {"LIVE_EXCHANGE", "DRY_RUN_SIMULATOR"}:
        return None
    actual_qty = fill.get("filled_qty")
    actual_price = fill.get("average_fill_price")
    if not isinstance(actual_qty, (int, float)) or actual_qty <= 0 or not isinstance(actual_price, (int, float)) or actual_price <= 0:
        log("[POS] Fill quantity/price invalid; position creation skipped")
        return None
    price = float(actual_price)
    size_usdt = float(fill.get("quote_qty") or actual_qty * actual_price)

    # ATR API error (HTTP 400/symbol not found) - skip position, TP/SL risky
    atr = fill.get("atr_value") if mode == "LIVE" else get_atr(signal["asset"])
    if atr is False:
        log(f"[POS] Skipped {signal['asset']} -- ATR API error (symbol not listed or delisted)")
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
            tp = round(price * 1.00866, 6)
            sl = round(price * 0.99566, 6)
        else:
            tp = round(price * 0.99266, 6)
            sl = round(price * 1.00566, 6)

    pos = {
        "schema_version": 2,
        "opened_at": datetime.now().isoformat(),
        "action": signal["action"],
        "asset": signal["asset"],
        "entry_price": price,
        "tp": tp,
        "sl": sl,
        "trail_dist": round(abs(sl - price), 6),
        "size_usdt": size_usdt,
        "quantity": float(actual_qty),
        "entry_filled_qty": float(actual_qty),
        "fill_source": fill["source"],
        "entry_order_id": fill.get("order_id"),
        "entry_fill_status": fill.get("classification"),
        "pending_order_remainder": max(0.0, float(fill.get("requested_qty") or actual_qty) - float(actual_qty)) if fill.get("classification") == "PENDING" else 0.0,
        "reconciliation_required": fill.get("classification") == "PENDING",
        "reconciliation_reason": "entry order remains pending" if fill.get("classification") == "PENDING" else None,
        "confidence": signal.get("confidence", "N/A"),
        "mode": mode,
    }

    # LIVE: place exchange-side stop-loss
    if mode == "LIVE":
        ex = tko_exec.get_exchange()
        if ex is not None:
            sl_qty = float(actual_qty)
            sl_id = tko_exec.place_stop_loss(ex, f"{signal['asset'].replace('USDT', '')}/USDT", signal["action"], sl_qty, sl)
            if sl_id:
                pos["sl_order_id"] = sl_id
        pos["sl_protection_confirmed"] = bool(pos.get("sl_order_id"))
        if not pos["sl_protection_confirmed"]:
            pos["reconciliation_required"] = True
            pos["reconciliation_reason"] = "exchange stop-loss acceptance not confirmed"

    if not append_position_record(mode, pos):
        log(f"[POS] Position store changed or quarantined for mode {mode}; append skipped")
        return None
    if mode == "LIVE" and fill.get("classification") != "PENDING":
        tko_exec.mark_order_position_recorded(fill.get("client_order_id"))
    log(f"[POS] Opened {signal['action']} {signal['asset']} @ {price} | TP={tp} SL={sl}")
    return pos


def recover_reconciled_positions(exchange):
    """Commit journaled LIVE fills to schema v2; uncertain stop recovery stays quarantined."""
    if tko_exec.trading_mode() != "LIVE":
        return {"recovered": 0, "quarantined": 0}
    positions = read_position_records("LIVE")
    if positions is None:
        return {"recovered": 0, "quarantined": 0}

    recovered = quarantined = 0
    for event in tko_exec.fills_requiring_position_commit():
        client_id = event.get("client_order_id")
        existing = recovery_policy.find_matching_entry_position(event, positions)
        decision = recovery_policy.decide_entry_recovery(
            event,
            existing_client_order_ids={p.get("entry_client_order_id") for p in positions if p.get("entry_client_order_id")},
            existing_order_ids={p.get("entry_order_id") for p in positions if p.get("entry_order_id")},
        )
        if decision["decision"] == "DUPLICATE":
            if existing is None:
                quarantined += 1
                continue
            valid, reason = tko_exec.validate_live_position_orders(exchange, existing)
            if valid:
                # Persist the recovered identity before resolving the journal. This
                # closes the crash window after position append but before journal mark.
                if client_id:
                    existing["entry_client_order_id"] = client_id
                if event.get("order_id"):
                    existing["entry_order_id"] = str(event["order_id"])
                existing["reconciliation_required"] = False
                existing["reconciliation_reason"] = None
                existing["sl_protection_confirmed"] = True
                if write_position_records("LIVE", positions):
                    if client_id and tko_exec.mark_order_position_recorded(client_id):
                        recovered += 1
                    else:
                        existing["reconciliation_required"] = True
                        existing["reconciliation_reason"] = "position saved but journal resolution was not durable"
                        write_position_records("LIVE", positions)
                        quarantined += 1
                else:
                    quarantined += 1
            else:
                existing["reconciliation_required"] = True
                existing["reconciliation_reason"] = "recovered position still unverified: " + str(reason)
                write_position_records("LIVE", positions)
                quarantined += 1
            continue
        if decision["decision"] != "RECOVER_POSITION":
            quarantined += 1
            continue

        filled = decision["filled_qty"]
        fill_price = decision["average_fill_price"]
        action = str(event.get("action", "")).upper()
        asset = event.get("asset")
        order_id = event.get("order_id")
        try:
            filled = float(filled)
            fill_price = float(fill_price)
        except (TypeError, ValueError, OverflowError):
            quarantined += 1
            continue
        if (action not in {"BUY", "SELL"} or not asset or not order_id
                or not math.isfinite(filled) or not math.isfinite(fill_price)
                or filled <= 0 or fill_price <= 0):
            quarantined += 1
            continue

        tp, sl = tko_exec.compute_tp_sl(fill_price, action)
        pos = {
            "schema_version": 2,
            "opened_at": datetime.now().isoformat(),
            "action": action,
            "asset": asset,
            "entry_price": fill_price,
            "tp": tp,
            "sl": sl,
            "trail_dist": round(abs(sl - fill_price), 8),
            "size_usdt": filled * fill_price,
            "quantity": filled,
            "entry_filled_qty": filled,
            "fill_source": "LIVE_EXCHANGE",
            "entry_order_id": str(order_id),
            "entry_client_order_id": client_id,
            "entry_fill_status": event.get("classification"),
            "reconciliation_required": True,
            "reconciliation_reason": "recovered fill requires verified protective stop",
            "mode": "LIVE",
        }

        state = event.get("state")
        if event.get("operation_type") != "ENTRY" or event.get("operation_id") != client_id:
            quarantined += 1
            continue
        if state == "FILL_REQUIRES_POSITION_COMMIT":
            # Persist a one-shot intent first. If the process dies during submission,
            # the next boot quarantines instead of risking a duplicate stop order.
            if not tko_exec._journal_order({**event, "state": "POSITION_RECOVERY_STOP_SUBMITTING"}):
                quarantined += 1
                continue
            stop_id = tko_exec.place_stop_loss(
                exchange, f"{str(asset).replace('USDT', '')}/USDT", action, filled, sl,
            )
            if not stop_id:
                pos["reconciliation_reason"] = "protective stop submission is uncertain; manual exchange reconciliation required"
            else:
                if not tko_exec._journal_order({**event, "sl_order_id": str(stop_id),
                                                 "state": "POSITION_RECOVERY_STOP_RECORDED"}):
                    pos["reconciliation_reason"] = "stop was accepted but journal update failed; manual reconciliation required"
                else:
                    pos["sl_order_id"] = str(stop_id)
        elif state == "POSITION_RECOVERY_STOP_SUBMITTING":
            pos["reconciliation_reason"] = "restart interrupted protective stop submission; manual exchange reconciliation required"
        elif state == "POSITION_RECOVERY_STOP_RECORDED" and event.get("sl_order_id"):
            pos["sl_order_id"] = str(event["sl_order_id"])
        else:
            quarantined += 1
            continue

        if pos.get("sl_order_id"):
            valid, reason = tko_exec.validate_live_position_orders(exchange, pos)
        else:
            valid, reason = False, pos["reconciliation_reason"]
        if valid:
            pos["reconciliation_required"] = False
            pos["reconciliation_reason"] = None
        else:
            pos["reconciliation_reason"] = "recovered entry/stop verification failed: " + str(reason)
        pos["sl_protection_confirmed"] = bool(valid)
        if not append_position_record("LIVE", pos):
            quarantined += 1
            continue
        positions.append(pos)
        if valid:
            if tko_exec.mark_order_position_recorded(client_id):
                recovered += 1
            else:
                pos["reconciliation_required"] = True
                pos["reconciliation_reason"] = "position saved but journal resolution was not durable"
                write_position_records("LIVE", positions)
                quarantined += 1
        else:
            quarantined += 1
    return {"recovered": recovered, "quarantined": quarantined}


def recover_triggered_stop_losses(exchange):
    """Remove only positions whose exchange stop is confirmed fully filled."""
    if tko_exec.trading_mode() != "LIVE":
        return {"closed": 0, "quarantined": 0}
    positions = read_position_records("LIVE")
    if positions is None:
        return {"closed": 0, "quarantined": 0}

    pending = []
    known = set()
    quarantined = 0
    changed = False
    for event in tko_exec.stop_fills_requiring_position_commit():
        event_key = event.get("client_order_id")
        if event_key:
            known.add(event_key)
        matching_position = next((pos for pos in positions
                                  if str(pos.get("sl_order_id")) == str(event.get("stop_order_id"))
                                  and f"{str(pos.get('asset', '')).replace('USDT', '')}/USDT"
                                  == str(event.get("symbol"))), None)
        recovery_position = matching_position or {
            "asset": event.get("position_asset"),
            "action": event.get("position_action"),
            "entry_order_id": event.get("position_entry_order_id"),
            "quantity": event.get("requested_qty"),
            "sl": event.get("trigger_price"),
            "sl_order_id": event.get("stop_order_id"),
        }
        if any(recovery_position.get(field) in (None, "")
               for field in ("asset", "action", "entry_order_id", "quantity", "sl", "sl_order_id")):
            quarantined += 1
            continue
        decision = recovery_policy.reconcile_stop_fill(
            recovery_position, event,
            matching_position.get("stop_reconciliation") if matching_position else None)
        if decision["decision"] == "CLOSE_POSITION":
            pending.append(event)
        elif matching_position:
            matching_position["reconciliation_required"] = True
            matching_position["reconciliation_reason"] = (
                "stop-loss trigger observed; "
                + (decision["reason"] or "stop fill requires reconciliation"))
            matching_position["stop_reconciliation"] = decision["evidence"]
            changed = True
            quarantined += 1
        else:
            quarantined += 1

    for pos in positions:
        reason = str(pos.get("reconciliation_reason") or "")
        if "stop-loss trigger observed" not in reason:
            continue
        stop_id = pos.get("sl_order_id")
        symbol = f"{str(pos.get('asset', '')).replace('USDT', '')}/USDT"
        key = f"stop-loss:{symbol}:{stop_id}" if stop_id else None
        if not key or key in known:
            continue
        result = tko_exec.reconcile_triggered_stop_fill(exchange, pos)
        result_evidence = result.as_dict()
        result_evidence.update({"client_order_id": key, "operation_id": key,
                                "operation_type": "STOP_CLOSE",
                                "related_entry_order_id": pos.get("entry_order_id"),
                                "exchange_client_order_id": result.client_order_id})
        decision = recovery_policy.reconcile_stop_fill(
            pos, result_evidence, pos.get("stop_reconciliation"))
        if decision["decision"] != "CLOSE_POSITION":
            pos["reconciliation_required"] = True
            pos["reconciliation_reason"] = (
                "stop-loss trigger observed; "
                + (decision["reason"] or "stop fill requires reconciliation"))
            pos["stop_reconciliation"] = decision["evidence"]
            changed = True
            quarantined += 1
            continue
        event = {
            **result.as_dict(),
            "client_order_id": key,
            "operation_id": key,
            "operation_type": "STOP_CLOSE",
            "related_entry_order_id": pos.get("entry_order_id"),
            "exchange_client_order_id": result.client_order_id,
            "state": "STOP_FILL_REQUIRES_POSITION_COMMIT",
            "position_asset": pos.get("asset"),
            "position_action": pos.get("action"),
            "position_entry_price": pos.get("entry_price"),
            "position_entry_order_id": pos.get("entry_order_id"),
            "stop_order_id": str(stop_id),
        }
        if not tko_exec._journal_order(event):
            quarantined += 1
            continue
        pending.append(event)
        known.add(key)

    remove_keys = {(str(event.get("symbol")), str(event.get("stop_order_id"))) for event in pending}
    remaining = [pos for pos in positions
                 if (f"{str(pos.get('asset', '')).replace('USDT', '')}/USDT",
                     str(pos.get("sl_order_id"))) not in remove_keys]
    removed_count = len(positions) - len(remaining)
    if (removed_count or changed) and not write_position_records(
            "LIVE", remaining if removed_count else positions):
        return {"closed": 0, "quarantined": quarantined + removed_count}
    committed = 0
    for event in pending:
        if tko_exec.mark_order_position_recorded(event.get("client_order_id")):
            committed += 1
            entry_price = float(event.get("position_entry_price") or 0)
            exit_price = float(event.get("average_fill_price") or 0)
            qty = float(event.get("filled_qty") or 0)
            action = str(event.get("position_action") or "").upper()
            pnl = (exit_price - entry_price) * qty if action == "BUY" else (entry_price - exit_price) * qty
            if entry_price > 0 and exit_price > 0 and qty > 0:
                append_jsonl(TRADES_LOG, {
                    "timestamp": "recovered-stop-fill",
                    "closed_at": datetime.now().isoformat(),
                    "action": action,
                    "asset": event.get("position_asset"),
                    "entry_price": entry_price,
                    "exit_price": exit_price,
                    "quantity": qty,
                    "result": "WIN" if pnl > 0 else "LOSS",
                    "pnl_usdt": round(pnl, 8),
                    "status": "CLOSED_STOP_RECONCILED",
                    "stop_order_id": event.get("stop_order_id"),
                })
                record_realised_loss(pnl)
                record_trade_result(pnl)
        else:
            quarantined += 1
    return {"closed": removed_count, "quarantined": quarantined + max(0, removed_count - committed)}

def log_positions_summary(prices):
    mode = tko_exec.trading_mode()
    positions = read_position_records(mode)
    if positions is None:
        log(f"[POS] Position summary unavailable for mode {mode}")
        return
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
    if mode == "DRY_RUN":
        w = wallet_manager.get_wallet_summary(positions)
        log(f"[WALLET] Modal: {w['initial_capital']:.3f} USDT (Rp{w['initial_idr']:,.0f}) | Kas: {w['cash_balance']:.2f} USDT | In-Trade: {w['in_trade']:.2f} USDT | Equity: {w['total_equity']:.3f} USDT ({w['roi_pct']:+.2f}% | Rp{w['equity_idr']:,.0f})")

# Trade Execution
TKO_CONFIDENCE_MIN = int(os.getenv("TKO_CONFIDENCE_MIN", "80"))

def adaptive_conf_min(adx):
    """Dynamic confidence floor based on trend strength (ADX). Strong trend = trust more (lower floor). Extreme ADX > 50 = reversal risk, strict again. None = default."""
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
    mode = tko_exec.trading_mode()
    if mode == "REJECTED":
        log("[TKO] Trade blocked by trading mode")
        return None
    if mode == "LIVE" and tko_exec.reconciliation_blocks_entries():
        log("[TKO] Unresolved order journal entries; new entries are paused for reconciliation")
        return None
    positions = read_position_records(mode)
    if positions is None:
        log(f"[TKO] Position store unavailable for mode {mode}; trade blocked")
        return None
    if mode == "LIVE" and recovery_policy.positions_block_entries(positions):
        log("[TKO] Position reconciliation required; new entries are paused")
        return None

    conf_str = str(signal.get("confidence", "0%"))
    conf = int(re.search(r'(\d+)', conf_str).group(1) if re.search(r'(\d+)', conf_str) else 0)
    conf_min = adaptive_conf_min(signal.get("adx"))
    if conf < conf_min:
        log(f"[TKO] Confidence {conf}% < floor {conf_min}% (ADX={signal.get('adx')}), skip")
        return None

    size_usdt = tko_exec.estimate_position_size(price)

    if mode == "DRY_RUN":
        ok, remaining_cash = wallet_manager.deduct_for_open(size_usdt)
        if not ok:
            log(f"[TKO] [WALLET] Kas virtual tidak cukup ({remaining_cash:.2f} USDT < {size_usdt:.2f} USDT), skip trade")
            return None
        log(f"[TKO] DRY-RUN: {signal['action']} {signal['asset']} @ {price} ({size_usdt:.2f} USDT) | Kas tersisa: {remaining_cash:.2f} USDT")
        append_jsonl(TRADES_LOG, {
            "timestamp": datetime.now().isoformat(),
            "action": signal["action"], "asset": signal["asset"],
            "price": price, "size_usdt": size_usdt, "status": "DRY_RUN",
        })
        qty = size_usdt / price if price > 0 else 0
        return {
            "source": "DRY_RUN_SIMULATOR", "classification": "SIMULATED_FILL",
            "filled_qty": qty, "average_fill_price": price,
            "quote_qty": size_usdt, "order_id": None,
            "action": signal["action"], "dry_run": True,
        }

    # LIVE: real order
    atr_value = get_atr(signal["asset"])
    if atr_value is False:
        log(f"[TKO] {signal['asset']} ATR unavailable; entry blocked before order submission")
        return None
    order = tko_exec.execute_trade(
        signal["action"], signal["asset"], size_usdt, price,
        operation_type="ENTRY",
    )
    if isinstance(order, dict):
        order["atr_value"] = atr_value
    if isinstance(order, dict) and order.get("classification") in {"CONFIRMED_FILL", "PARTIAL_FILL"}:
        append_jsonl(TRADES_LOG, {
            "timestamp": datetime.now().isoformat(),
            "action": signal["action"], "asset": signal["asset"],
            "price": price, "size_usdt": size_usdt,
            "status": order.get("classification"), "order_id": order.get("order_id"),
            "fill_source": "LIVE_EXCHANGE", "filled_qty": order.get("filled_qty"),
            "average_fill_price": order.get("average_fill_price"),
        })
        return order

    if isinstance(order, dict) and order.get("classification") in {"PENDING", "AMBIGUOUS"}:
        append_jsonl(TRADES_LOG, {
            "timestamp": datetime.now().isoformat(), "action": signal["action"],
            "asset": signal["asset"], "price": price, "size_usdt": size_usdt,
            "status": order["classification"], "order_id": order.get("order_id"),
            "fill_source": "LIVE_EXCHANGE", "reconciliation_required": True,
        })
        if (order.get("classification") == "PENDING"
                and isinstance(order.get("filled_qty"), (int, float)) and order["filled_qty"] > 0
                and isinstance(order.get("average_fill_price"), (int, float)) and order["average_fill_price"] > 0):
            return order
        return None

    if isinstance(order, dict) and order.get("classification") == "NO_FILL_CONFIRMED":
        append_jsonl(TRADES_LOG, {
            "timestamp": datetime.now().isoformat(), "action": signal["action"],
            "asset": signal["asset"], "price": price, "size_usdt": size_usdt,
            "status": "NO_FILL_CONFIRMED", "order_id": order.get("order_id"),
            "fill_source": "LIVE_EXCHANGE",
        })
        return None

    append_jsonl(TRADES_LOG, {
        "timestamp": datetime.now().isoformat(),
        "action": signal["action"], "asset": signal["asset"],
        "price": price, "size_usdt": size_usdt, "status": "FAILED",
    })
    log(f"[TKO] LIVE order FAILED: {signal['asset']} @ {price} ({size_usdt:.2f} USDT)")
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
    mode = tko_exec.trading_mode()
    positions = read_position_records(mode)
    if positions is None:
        return True
    return any(p.get("asset") == asset for p in positions)

def prune_stale_positions():
    """Close positions older than STALE_POSITION_HOURS to prevent zombie slots."""
    mode = tko_exec.trading_mode()
    positions = read_position_records(mode)
    if positions is None:
        log(f"[STALE] Position store unavailable for mode {mode}; pruning skipped")
        return 0
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
                if recovery_policy.should_prune_position(
                        pos, now - opened_ts, STALE_POSITION_HOURS * 3600):
                    stale.append(pos["asset"])
                    continue
            except (ValueError, TypeError):
                pass
        remaining.append(pos)

    if stale:
        if not write_position_records(mode, remaining):
            log(f"[STALE] Position store changed or quarantined for mode {mode}; pruning skipped")
            return 0
        log(f"[STALE] Closed {len(stale)} stale position(s): {', '.join(stale)}")
    return len(stale)

def get_open_assets():
    mode = tko_exec.trading_mode()
    positions = read_position_records(mode)
    if positions is None:
        return []
    return list({p.get("asset") for p in positions if p.get("asset")})

def loss_cooldown_active(asset):
    """True jika asset terakhir CLOSE loss dalam LOSS_COOLDOWN_HOURS (anti re-entry coin baru hit)."""
    if LOSS_COOLDOWN_HOURS <= 0:
        return False
    cutoff = time.time() - LOSS_COOLDOWN_HOURS * 3600
    for t in reversed(read_jsonl(TRADES_LOG)):
        if t.get("asset") != asset or t.get("status") != "CLOSED":
            continue
        try:
            closed_ts = datetime.fromisoformat(t.get("closed_at", "")).timestamp()
        except Exception:
            closed_ts = float(t.get("timestamp_epoch", 0))
        if closed_ts > cutoff:
            break
    else:
        return False
    pnl = t.get("pnl_usdt")
    if pnl is not None and pnl < 0:
        return True
    return False

def loss_cooldown_assets(bases):
    """Batch loss_cooldown_active(): baca TRADES_LOG sekali, balikkan set base yang cooldown."""
    if LOSS_COOLDOWN_HOURS <= 0:
        return set()
    cutoff = time.time() - LOSS_COOLDOWN_HOURS * 3600
    wanted = {f"{b}USDT": b for b in bases}
    result = set()
    for t in reversed(read_jsonl(TRADES_LOG)):
        asset = t.get("asset")
        if asset not in wanted or t.get("status") != "CLOSED":
            continue
        base = wanted.pop(asset)
        try:
            closed_ts = datetime.fromisoformat(t.get("closed_at", "")).timestamp()
        except Exception:
            closed_ts = float(t.get("timestamp_epoch", 0))
        if closed_ts <= cutoff:
            continue
        pnl = t.get("pnl_usdt")
        if pnl is not None and pnl < 0:
            result.add(base)
    return result

def _filter_signal(signal, adx_indicators, rsi_indicators, prices):
    """Filter signal by ADX strength, RSI alignment, and volume/quality sanity. Return signal if passes, None rejected."""
    asset = signal.get("asset", "")
    base = asset.replace("USDT", "")
    action = signal.get("action", "")
    rsi = rsi_indicators.get(base)
    adx = adx_indicators.get(base)

    # 0. Loss cooldown: jangan re-entry coin yang baru saja CLOSE loss
    if loss_cooldown_active(asset):
        log(f"[FILTER] {base} dalam loss cooldown {LOSS_COOLDOWN_HOURS:g} jam, reject")
        return _reject(base, "cooldown")

    # 1. ADX minimum check: ensure sufficient market trend momentum
    ADX_MIN = float(os.getenv("TKO_ADX_MIN", "20"))
    ADX_MAX = float(os.getenv("TKO_ADX_MAX", "65"))
    if adx is not None:
        if adx < ADX_MIN:
            log(f"[FILTER] {base} ADX {adx:.1f} < {ADX_MIN} (weak trend), reject")
            return _reject(base, "adx_low")
        if adx > ADX_MAX:
            log(f"[FILTER] {base} ADX {adx:.1f} > {ADX_MAX:g} (extreme trend climax / exhaustion danger), reject")
            return _reject(base, "adx_high")
        if action == "BUY" and adx > BUY_ADX_MAX:
            log(f"[FILTER] {base} BUY but ADX {adx:.1f} > {BUY_ADX_MAX:g} (late in move), reject")
            return _reject(base, "buy_adx_high")

    # 2. RSI alignment check: avoid buying tops & shorting bottoms
    if rsi is not None:
        if action == "BUY":
            if rsi > BUY_RSI_MAX:
                log(f"[FILTER] {base} BUY but RSI {rsi:.1f} > {BUY_RSI_MAX:g} (overbought), reject")
                return _reject(base, "buy_rsi_high")
            if rsi < 20:
                log(f"[FILTER] {base} BUY but RSI {rsi:.1f} < 20 (falling knife momentum), reject")
                return _reject(base, "buy_rsi_low")
        elif action == "SELL":
            if rsi < 35:
                log(f"[FILTER] {base} SELL but RSI {rsi:.1f} < 35 (oversold), reject")
                return _reject(base, "sell_rsi_low")
            if rsi > 75:
                log(f"[FILTER] {base} SELL but RSI {rsi:.1f} > 75 (parabolic pump squeeze), reject")
                return _reject(base, "sell_rsi_high")

    # 3. Alignment arah tren (opsional, default off)
    if TREND_ALIGN_ENABLED:
        trend = get_trend_dir(asset)
        if trend == "UP" and action == "SELL":
            log(f"[FILTER] {base} SELL but 1h trend UP, reject")
            return _reject(base, "trend_up")
        if trend == "DOWN" and action == "BUY":
            log(f"[FILTER] {base} BUY but 1h trend DOWN, reject")
            return _reject(base, "trend_down")

    return signal

def _main_loop():
    global _down_alerted, _reject_streak
    main_loop._counter = getattr(main_loop, "_counter", 0) + 1

    log("[BRIDGE] Running...")

    if tko_exec.trading_mode() == "LIVE":
        exchange = tko_exec.get_exchange()
        if exchange is not None:
            stats = tko_exec.reconcile_unresolved_order_intents(exchange)
            if stats["checked"]:
                log(f"[RECON] checked={stats['checked']} no_fill={stats['resolved_no_fill']} "
                    f"filled_needs_position={stats['fills_needing_position']} unresolved={stats['unresolved']}")
            recovery = recover_reconciled_positions(exchange)
            if recovery["recovered"] or recovery["quarantined"]:
                log(f"[RECON] recovered_positions={recovery['recovered']} quarantined={recovery['quarantined']}")
            stop_recovery = recover_triggered_stop_losses(exchange)
            if stop_recovery["closed"] or stop_recovery["quarantined"]:
                log(f"[RECON] stop_fills_closed={stop_recovery['closed']} quarantined={stop_recovery['quarantined']}")
            if not quarantine_positions_for_unresolved_closes():
                log("[RECON] Unresolved close relation is ambiguous; entries remain blocked")
            positions = read_position_records("LIVE")
            if positions is not None:
                changed = False
                for pos in positions:
                    valid, reason = tko_exec.validate_live_position_orders(exchange, pos)
                    if not valid and not pos.get("reconciliation_required"):
                        pos["reconciliation_required"] = True
                        pos["reconciliation_reason"] = "exchange order reconciliation: " + str(reason)
                        changed = True
                if changed and not write_position_records("LIVE", positions):
                    log("[RECON] Position store update failed; new entries must remain disabled")

    market_data = get_market_data()
    if not market_data:
        log("[BRIDGE] No market data, skip")
        alert_down("No market data -- bridge down?")
        return
    _down_alerted = False

    prices = {}
    for base, coin in market_data.items():
        prices[base] = coin["price"]

    open_count, floating = check_open_positions(prices)
    if open_count:
        log(f"[POS] {open_count} open, floating {floating:+.4f} USDT (~Rp{idr(floating)})")
        if tko_exec.trading_mode() == "DRY_RUN":
            w = wallet_manager.get_wallet_summary(read_position_records("DRY_RUN") or [])
            log(f"[WALLET] Modal: {w['initial_capital']:.3f} USDT (Rp{w['initial_idr']:,.0f}) | Kas: {w['cash_balance']:.2f} USDT | In-Trade: {w['in_trade']:.2f} USDT | Equity: {w['total_equity']:.3f} USDT ({w['roi_pct']:+.2f}% | Rp{w['equity_idr']:,.0f})")

    # Stale position cleanup
    prune_stale_positions()

    # Max positions guard
    if open_count >= MAX_OPEN_POSITIONS:
        log(f"[GUARD] Max positions ({MAX_OPEN_POSITIONS}) reached, skip cycle")
        return

    # Circuit breaker
    if circuit_paused():
        log("[CIRCUIT] Daily loss limit reached -- trading paused until daily reset")
        return

    # Streak breaker: N loss beruntun -> pause entry (posisi aktif tetap dikelola)
    if streak_paused():
        _st = streak_state()
        _until = datetime.fromtimestamp(float(_st.get("paused_until") or 0)).strftime("%Y-%m-%d %H:%M:%S")
        log(f"[STREAK] {_st.get('consecutive_losses')} loss beruntun -- entry dipause sampai {_until}")
        return

    # Log positions summary every 5 cycles
    if main_loop._counter % 5 == 0:
        log_positions_summary(prices)

    # Last signal rotation
    last_sig = read_jsonl(SIGNALS_LOG)
    last_sig = last_sig[-1] if last_sig else None
    last_price = float(last_sig.get("entry_price", 0)) if last_sig else None
    last_asset = last_sig.get("asset", "").replace("USDT", "") if last_sig else ""

    # Price stability check
    if last_price and last_asset and last_asset in prices:
        curr = prices[last_asset]
        pct_change = abs(curr - last_price) / last_price * 100
        if tko_exec.trading_mode() == "LIVE" and pct_change < THRESHOLD:
            log(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Harga stabil, skip")
            return

    open_assets = get_open_assets()
    ADX_MIN = float(os.getenv("TKO_ADX_MIN", "20"))

    excluded = [last_asset] if last_asset else []
    for oa in open_assets:
        excluded = list(set(excluded + [oa]))

    rsi_indicators = get_batch_rsi(list(market_data.keys()))
    log(f"[RSI] Fetched {len(rsi_indicators)} coins")
    adx_indicators = get_batch_adx(list(market_data.keys()))
    log(f"[ADX] Fetched {len(adx_indicators)} coins")

    # Pre-AI filter: buang kandidat yang sudah pasti ditolak, biar call AI tidak terbuang.
    # (cooldown + ADX di luar band). RSI tetap dicek setelah AI karena tergantung arah.
    ADX_MAX = float(os.getenv("TKO_ADX_MAX", "65"))
    drop = {}
    for b in loss_cooldown_assets(list(market_data.keys())):
        drop[b] = "cooldown"
    for b, adx in adx_indicators.items():
        if adx is None or b in drop:
            continue
        if adx < ADX_MIN:
            drop[b] = "adx_low"
        elif adx > ADX_MAX:
            drop[b] = "adx_high"
    if drop:
        for reason in drop.values():
            bump_reject("pre_" + reason, "")
        log(f"[PRE] Buang {len(drop)} kandidat: " + ", ".join(f"{b}={r}" for b, r in list(drop.items())[:12]))
    market_data = {b: d for b, d in market_data.items() if b not in drop}
    if len(market_data) < 3:
        log(f"[PRE] Cuma {len(market_data)} kandidat layak, skip cycle")
        return

    # Signal retry: kalau kandidat ditolak (filter ADX/RSI, posisi sudah terbuka,
    # harga invalid), langsung minta AI cari koin lain tanpa nunggu INTERVAL.
    signal = None
    max_retries = 5
    for _retry in range(max_retries):
        candidate = get_ai_signal(market_data, rsi_indicators, adx_indicators, last_asset, excluded)
        if not candidate:
            log(f"[AI] No valid signal (attempt {_retry + 1}/{max_retries}), skip cycle")
            return

        base = candidate["asset"].replace("USDT", "")

        if has_open_position(candidate["asset"]):
            excluded.append(base)
            bump_reject("open_position", base)
            log(f"[GUARD] {base} sudah ada posisi terbuka, cari koin lain ({_retry + 1}/{max_retries})")
            continue

        filtered = _filter_signal(candidate, adx_indicators, rsi_indicators, prices)
        if not filtered:
            excluded.append(base)
            log(f"[FILTER] {base} ditolak, cari koin lain ({_retry + 1}/{max_retries})")
            continue

        price = prices.get(base)
        if not price:
            excluded.append(base)
            bump_reject("no_price", base)
            log(f"[BRIDGE] No price for {base}USDT, cari koin lain ({_retry + 1}/{max_retries})")
            continue

        # Sanity: ticker price must match kline close
        if not _validate_price(base, price):
            excluded.append(base)
            bump_reject("price_sanity", base)
            log(f"[SANITY] {base} ticker={price} diverges from kline, cari koin lain ({_retry + 1}/{max_retries})")
            continue

        signal = filtered
        break
    else:
        _reject_streak += 1
        log(f"[FILTER] Semua kandidat ditolak, skip cycle (streak {_reject_streak}/{REJECT_ALERT_STREAK})")
        if _reject_streak >= REJECT_ALERT_STREAK:
            alert_reject_streak(_reject_streak)
            _reject_streak = 0
        return

    _reject_streak = 0
    asset = signal["asset"]
    signal["adx"] = adx_indicators.get(base)

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
    log(f"[{asset}] {signal['action']} @ {price} (conf={signal.get('confidence')})")

    embed = make_signal_embed(signal, price)
    send_discord_webhook(DISCORD_WEBHOOK_URL, embed)

    order = execute_tko_trade(signal, price)
    if order:
        send_trade_notification(signal, order, price)
        add_position(signal, price, fill=order)

def main_loop():
    """Wrapper: flush statistik reject tiap cycle + ringkasan 24h tiap 20 cycle."""
    try:
        _main_loop()
    finally:
        flush_reject_stats()
        if getattr(main_loop, "_counter", 0) % 20 == 0:
            log_reject_summary(24)

if __name__ == "__main__":
    if not acquire_instance_lock():
        raise SystemExit(LOCK_EXIT_CODE)
    log(f"[BRIDGE] Started. Ollama={AI_MODEL} Discord={DISCORD_WEBHOOK_URL[:50]}... TRADING_MODE={tko_exec.trading_mode()}")
    schedule.every(INTERVAL).seconds.do(main_loop)
    main_loop()
    while True:
        try:
            schedule.run_pending()
        except Exception as e:
            log(f"[BRIDGE] CRASH: {e}")
            alert_down("Bridge crashed: " + str(e))
        time.sleep(1)

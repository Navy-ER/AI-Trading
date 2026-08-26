# Full Improvement Plan: Bridge.py Real-Trade Readiness

> Date: 2026-08-07
> Status: PRE-REFACTOR — plan only, no code changes yet
> Target: 0 risk of catastrophic loss on real trade launch

---

## Executive Summary

Bridge.py saat ini: DRY-RUN, 1.2 USDT fixed size, fixed TP/SL 3%/1.5%, tanpa execution handler.
114 trades historis: 47W/67L, net +497 USDT (termasuk BTC sintetik yang harus dikoreksi).

**Blocker utama sebelum real trade:**

1. Tidak ada risk management (no position sizing, no daily limit, no correlation guard)
2. Tidak ada execution handler (ccxt belum terintegrasi)
3. Exit logic terlalu sederhana (no trailing stop, no time exit)
4. Monitoring/alerting minimal
5. BTC sintetik corruption di trades.jsonl perlu dibersihkan dulu

---

## Phase 0: Data Cleanup (PRE-REFACTOR — 1 hari)

**Goal:** Bersihkan data historis sebelum refactor besar.

### 0.1 Koreksi BTC Sintetik

- File: `trades.jsonl`
- Action: Hapus semua entry BTCUSDT yang synthetic (ada tag `⚠️sintetik`)
- Validasi: pastikan 0 duplikat BTC
- Output: trades.jsonl bersih untuk backtest nanti

### 0.2 Audit Lengkap 114 Trades

- Script Python untuk analisis mendalam:
  - Win rate per asset (exclude VOID)
  - Average win vs average loss
  - Maximum drawdown per session
  - Profit factor = gross profit / gross loss
- Output: `audit_report.md`

### 0.3 Backup

```bash
cp trades.jsonl trades.jsonl.bak.$(date +%Y%m%d)
cp open_positions.jsonl open_positions.jsonl.bak.$(date +%Y%m%d)
cp bridge.log bridge.log.bak.$(date +%Y%m%d)
```

---

## Phase 1: Risk Management (HIGH — 2-3 hari)

### 1.1 Dynamic Position Sizing

**File:** `bridge.py` — fungsi `calculate_position_size()`

**Logic:**

```python
def calculate_position_size(asset, base_price, atr, total_capital=50.0):
    """
    Size = (capital * risk_pct) / (entry - stop_loss_price)
    Risk per trade: 1-2% dari total capital
    ATR-based adjustment: high volatility = smaller position
    """
    risk_pct = 0.01  # 1% risk per trade
    atr_mult = 2.0    # SL = 2x ATR

    sl_distance = atr * atr_mult
    if sl_distance < base_price * 0.005:
        sl_distance = base_price * 0.005  # min 0.5% SL

    size_usdt = (total_capital * risk_pct) / sl_distance
    size_usdt = max(1.2, min(size_usdt, 10.0))  # clamp 1.2 - 10 USDT
    return round(size_usdt, 2)
```

**Config via .env:**

```
TKO_RISK_PCT=1.0        # risk per trade (% of capital)
TKO_ATR_MULT=2.0        # SL = ATR * multiplier
TKO_MIN_SIZE_USDT=1.20
TKO_MAX_SIZE_USDT=10.00
TKO_TOTAL_CAPITAL=50.00 # total trading capital
```

### 1.2 Daily Loss Limit (Circuit Breaker)

**File:** `bridge.py` — baru `check_daily_limit()`

**Logic:**

```python
DAILY_LIMIT_USDT = -5.0  # configurable

def check_daily_limit():
    today = datetime.now().strftime('%Y-%m-%d')
    daily_pnl = sum(t['pnl_usdt'] for t in read_jsonl(TRADES_LOG)
                    if t.get('closed_at','')[:10] == today)
    if daily_pnl <= DAILY_LIMIT_USDT:
        log(f"[CIRCUIT] Daily loss {daily_pnl:.4f} <= {DAILY_LIMIT_USDT}, STOPPING")
        return False  # block new trades
    return True
```

**Config:**

```
TKO_DAILY_LOSS_LIMIT=-5.00
```

### 1.3 Correlation Guard

**File:** `bridge.py` — baru `check_correlation()`

**Logic:**

- Maintain list of "families" (sectors)
- Max 2 positions per family
- Families: meme, defi, layer1, gold-pegged, stablecoin-adj, etc.

```python
ASSET_FAMILIES = {
    "meme": {"DOGE", "SHIB", "PEPE", "BONK", "FLOKI"},
    "defi": {"UNI", "AAVE", "LINK", "CRV", "SNX"},
    "layer1": {"ETH", "SOL", "ADA", "DOT", "AVAX"},
    "gold": {"PAXG", "XAUT"},
    "mira": {"MIRA"},
    "snxxb": {"SNXXB"},
    # etc
}

def check_correlation(asset):
    for family, coins in ASSET_FAMILIES.items():
        if asset in coins:
            open_in_family = [p for p in read_jsonl(OPEN_POS_LOG)
                             if p.get('asset','').replace('USDT','') in coins]
            if len(open_in_family) >= 2:
                log(f"[CORR] {asset} in {family}, already {len(open_in_family)} positions, skip")
                return False
    return True
```

### 1.4 Max Open Positions

**Existing:** `TKO_MAX_SLOTS=5` (already in .env)
**Enhancement:** tambahkan check di setiap cycle

---

## Phase 2: Exit Logic (HIGH — 1-2 hari)

> ✅ **COMPLETED 2026-08-08** — trailing stop LIVE (`update_trailing_stop()` di bridge.py, test `test_trailing.py` 13 PASS).
> **Deviasi dari spec:** implementasi pakai jarak FIXED = SL awal (1.0 ATR) dari best price (chandelier-style, aktif sejak open, tanpa trigger 2%), bukan 50%-of-profit. Alasan: risk konsisten, tidak butuh threshold tuning, ratchet otomatis terkunci saat profit. Toggle `TKO_TRAIL_ENABLED` (default 1). Posisi lama tanpa `trail_dist` ke-init dari |sl-entry|. SL-hit dgn pnl>0 dilabel "WIN".

### 2.1 Trailing Stop

**File:** `bridge.py` — modifikasi `check_positions()`

**Logic:**

```python
def update_trailing_stop(pos):
    """Update TP/SL based on price movement"""
    current = get_current_price(pos['asset'])
    entry = pos['entry_price']
    action = pos['action']

    if action == 'BUY':
        # Lock 50% of profit if price > entry * 1.02
        if current > entry * 1.02:
            profit_pct = (current - entry) / entry * 100
            trail_distance = profit_pct * 0.5  # trail at 50% of profit
            new_sl = current * (1 - trail_distance / 100)
            if new_sl > pos['sl']:
                pos['sl'] = new_sl
                pos['trail_active'] = True
    elif action == 'SELL':
        if current < entry * 0.98:
            profit_pct = (entry - current) / entry * 100
            trail_distance = profit_pct * 0.5
            new_sl = current * (1 + trail_distance / 100)
            if new_sl < pos['sl']:
                pos['sl'] = new_sl
                pos['trail_active'] = True
```

### 2.2 Time-Based Exit

**Logic:**

- Position > 4 hours tanpa move → close
- Position > 8 hours → force close

```python
MAX_HOLD_HOURS = 8
PARTIAL_EXIT_HOURS = 4

def check_time_exit(pos):
    opened = datetime.fromisoformat(pos['opened_at'])
    elapsed = (datetime.now() - opened).total_seconds() / 3600
    if elapsed >= MAX_HOLD_HOURS:
        return True  # force close
    if elapsed >= PARTIAL_EXIT_HOURS and pos.get('floating_pnl', 0) > 0:
        return True  # consider exit
    return False
```

### 2.3 Config Exit Parameters

```
TKO_TP_PCT=3           # take profit %
TKO_SL_PCT=1.5         # stop loss % (ATR override)
TKO_TRAIL_TRIGGER=2    # start trailing after 2% profit
TKO_MAX_HOLD_HOURS=8
TKO_PARTIAL_EXIT_HOURS=4
```

---

## Phase 3: Execution Handler (HIGH — 2-3 hari)

### 3.1 ccxt Integration

**File:** baru `execution.py`

**Dependencies:**

```bash
pip install ccxt
```

**Core functions:**

```python
import ccxt

def init_exchange():
    exchange = ccxt.binance({
        'apiKey': os.getenv('TOKOCRYPTO_API_KEY'),
        'secret': os.getenv('TOKOCRYPTO_SECRET_KEY'),
        'enableRateLimit': True,
        'options': {'defaultType': 'spot'},
    })
    # Tokocrypto host override
    exchange.urls['public'] = {'api': 'https://www.tokocrypto.com/api'}
    exchange.urls['private'] = {'api': 'https://www.tokocrypto.com/api'}
    return exchange

def execute_trade(exchange, action, asset, size_usdt, price):
    """
    Execute market order via ccxt
    Returns: order_id or None on error
    """
    try:
        symbol = f"{asset.replace('USDT','')}/USDT"
        side = 'buy' if action == 'BUY' else 'sell'

        # Slippage check
        market_price = exchange.fetch_ticker(symbol)['last']
        slippage = abs(price - market_price) / price * 100
        if slippage > 1.0:  # >1% slippage = skip
            log(f"[EXEC] Slippage {slippage:.2f}% > 1%, skip {asset}")
            return None

        # Calculate quantity
        quantity = size_usdt / price

        # Execute
        order = exchange.create_order(
            symbol=symbol,
            type='market',
            side=side,
            amount=quantity,
        )
        log(f"[EXEC] {action} {asset} {quantity:.6f} @ ~{market_price}")
        return order.get('id')
    except ccxt.InsufficientFunds:
        log(f"[EXEC] Insufficient funds for {asset}")
        return None
    except ccxt.InvalidOrder as e:
        log(f"[EXEC] Invalid order: {e}")
        return None
    except Exception as e:
        log(f"[EXEC] Error: {e}")
        return None
```

### 3.2 Idempotency Guard

**Problem:** retry = double order
**Solution:** track last executed order per asset per cycle

```python
LAST_EXECUTED = {}  # asset -> order_id

def is_duplicate(asset, order_id):
    if LAST_EXECUTED.get(asset) == order_id:
        return True
    LAST_EXECUTED[asset] = order_id
    return False
```

### 3.3 Dry-Run → Real Toggle

**File:** `.env`

```
TKO_DRY_RUN=true     # set false untuk real trade
TKO_SLIPPAGE_MAX=1.0 # max slippage %
TKO_ORDER_TIMEOUT=30 # seconds
```

### 3.4 Execution Test Plan

1. Test dengan small amount (0.1 USDT)
2. Verify order appears in exchange UI
3. Verify position opens in bridge
4. Monitor for 1 hour, check PnL sync
5. Only then scale to full size

---

## Phase 4: Monitoring & Alerting (MEDIUM — 1 hari)

### 4.1 Discord Alert Additions

**File:** `bridge.py` — modifikasi `send_discord_webhook()`

**New alert types:**

```python
ALERT_TYPES = {
    'circuit_breaker': '🚨 Circuit Breaker Triggered',
    'bot_crash': '💀 Bot Crashed',
    'api_error_rate': '⚠️ High API Error Rate',
    'max_drawdown': '📉 Max Drawdown Hit',
    'execution_success': '✅ Order Executed',
    'execution_fail': '❌ Order Failed',
}
```

### 4.2 Health Check Endpoint

**New file:** `health.py`

```python
from fastapi import FastAPI
import psutil

app = FastAPI()

@app.get("/health")
def health():
    return {
        'status': 'ok',
        'uptime': get_uptime(),
        'cpu': psutil.cpu_percent(),
        'memory': psutil.virtual_memory().percent,
        'ollama': check_ollama(),
        'positions': len(read_jsonl(OPEN_POS_LOG)),
    }
```

### 4.3 Config

```
DISCORD_HEALTH_WEBHOOK=https://...
HEALTH_CHECK_INTERVAL=60  # seconds
```

---

## Phase 5: Signal Enhancement (MEDIUM — 2 hari)

### 5.1 Market Regime Filter

**File:** `bridge.py` — baru `get_adx()` dan `is_choppy()`

```python
def get_adx(symbol, period=14):
    """Average Directional Index — trend strength"""
    # Implement using ATR + DI+ - DI-
    # ADX > 25 = trending, < 20 = ranging
    pass

def is_choppy(symbol):
    """Check if market is choppy (avoid trading)"""
    adx = get_adx(symbol)
    return adx < 20  # choppy = low ADX
```

**Config:**

```
TKO_MIN_ADX=20        # skip if ADX < this
TKO_USE_ADX_FILTER=true
```

### 5.2 Adaptive Confidence Threshold

**Current:** hardcoded 80%

**Enhancement:**

```python
def get_adaptive_threshold(market_data, asset):
    """Lower threshold in ranging, higher in trending"""
    adx = get_adx(f"{asset}USDT")
    if adx > 30:  # strong trend
        return 70  # accept lower confidence
    return 80    # default
```

---

## Phase 6: Testing & Validation (MEDIUM — 2 hari)

### 6.1 Unit Tests

**File:** `tests/test_risk.py`

```python
import unittest
from bridge import calculate_position_size, check_daily_limit, check_correlation

class TestRiskManagement(unittest.TestCase):
    def test_position_size_atr(self):
        size = calculate_position_size('BTC', 50000, 500)
        self.assertGreaterEqual(size, 1.2)
        self.assertLessEqual(size, 10.0)

    def test_daily_limit(self):
        # Simulate daily PnL
        pass

    def test_correlation_guard(self):
        # Test family isolation
        pass
```

### 6.2 Integration Tests

**File:** `tests/test_execution.py`

```python
class TestExecution(unittest.TestCase):
    def test_slippage_check(self):
        # Mock price with 2% difference
        pass

    def test_idempotency(self):
        # Verify no double orders
        pass
```

### 6.3 Dry-Run Stress Test

**Duration:** 48 hours
**Metrics to track:**

- Trade frequency
- Win rate
- Max drawdown
- API error rate
- False signal rate

---

## Phase 7: Security Hardening (LOW — 1 hari)

### 7.1 API Key Management

**Current:** plain text in .env
**Improvement:** encrypt with fernet (optional)

```python
from cryptography.fernet import Fernet

def load_encrypted_keys():
    key = os.getenv('ENCRYPTION_KEY')
    f = Fernet(key)
    # Decrypt and load
```

### 7.2 Webhook Security

**Current:** hardcoded URLs
**Improvement:** move to .env, add HMAC verification

```
DISCORD_WEBHOOK_URL=...
DISCORD_TRADE_WEBHOOK_URL=...
```

---

## Phase 8: Logging & Observability (LOW — 1 hari)

### 8.1 Structured Logging

**Current:** plain text
**Improvement:** JSON lines for machine parsing

```python
import logging
import json

class JsonFormatter(logging.Formatter):
    def format(self, record):
        log_obj = {
            'timestamp': self.formatTime(record),
            'level': record.levelname,
            'message': record.getMessage(),
            'module': record.module,
        }
        return json.dumps(log_obj)
```

### 8.2 Log Rotation

```python
from logging.handlers import RotatingFileHandler
handler = RotatingFileHandler('bridge.log', maxBytes=10*1024*1024, backupCount=5)
```

---

## Implementation Order (Prioritized)

```
Day 1: Phase 0 (Data Cleanup)
Day 2-3: Phase 1 (Risk Management)
Day 4-5: Phase 2 (Exit Logic)
Day 6-8: Phase 3 (Execution Handler)
Day 9: Phase 4 (Monitoring)
Day 10-11: Phase 5 (Signal Enhancement)
Day 12-13: Phase 6 (Testing)
Day 14: Phase 7 (Security)
Day 15: Phase 8 (Logging)
Day 16: Final integration test
Day 17: GO LIVE (small capital)
```

---

## Risk Assessment

| Risk                 | Impact | Mitigation                          |
| -------------------- | ------ | ----------------------------------- |
| Execution failure    | High   | Retry + circuit breaker             |
| Slippage loss        | Medium | Slippage guard + skip               |
| Correlated loss      | High   | Family isolation                    |
| Daily limit exceeded | Medium | Hard stop, no recovery              |
| API downtime         | Medium | Fallback to data-api.binance.vision |
| Ollama crash         | Low    | Auto-restart + alert                |

---

## Success Criteria (Go/No-Go)

**Before real trade:**

- [ ] Phase 0-3 complete
- [ ] 48-hour dry-run with no errors
- [ ] Win rate > 40% (current 41.2%)
- [ ] Max drawdown < 10 USDT in dry-run
- [ ] All unit tests passing
- [ ] Execution test with 0.1 USDT successful
- [ ] Circuit breaker tested and working
- [ ] Discord alerts working

**Phase 4-8 can be done after initial real trade launch** (with smaller capital first).

---

## Notes

- BTC sintetik corruption di Phase 0 harus dibersihkan dulu
- Tokocrypto API rate limit: 20 req/sec (ccxt handles this)
- IP public 158.140.166.127 blocked on 3701 → use data-api.binance.vision fallback
- Model: qwen2.5:3b (CPU, num_gpu=0)
- Min order: 1.2 USDT (TOKO requirement)
- Confidence threshold: 80% (tuned from 75%)

---

## Updates to Vault

Setiap selesai phase, update `E:\Project AI\Project AI Agentic.md` dengan:

- Phase yang selesai
- Metrics dry-run
- Issues encountered
- Next phase status

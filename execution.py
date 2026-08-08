"""Real trade execution via ccxt (Binance/Tokocrypto)."""
import os
import re
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass  # os.getenv still works if keys are in process env

import ccxt
from datetime import datetime

TOKOCRYPTO_API_KEY = os.getenv("TOKOCRYPTO_API_KEY", "")
TOKOCRYPTO_SECRET_KEY = os.getenv("TOKOCRYPTO_SECRET_KEY", "")
TKO_MIN_ORDER_USDT = float(os.getenv("TKO_MIN_ORDER_USDT", "1.20"))
TKO_MAX_POSITION_USDT = float(os.getenv("TKO_MAX_POSITION_USDT", "5.00"))
TKO_SL_PCT = float(os.getenv("TKO_SL_PCT", "1.5"))
TKO_TP_PCT = float(os.getenv("TKO_TP_PCT", "3"))

_exchange = None


def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[EXEC] {ts} {msg}", flush=True)


def get_exchange():
    """Singleton ccxt exchange (Tokocrypto-backed Binance)."""
    global _exchange
    if _exchange is not None:
        return _exchange
    if not TOKOCRYPTO_API_KEY or not TOKOCRYPTO_SECRET_KEY:
        log("No API keys — execution disabled")
        return None
    try:
        exchange = ccxt.binance({
            "apiKey": TOKOCRYPTO_API_KEY,
            "secret": TOKOCRYPTO_SECRET_KEY,
            "enableRateLimit": True,
            "options": {"defaultType": "spot"},
        })
        exchange.urls["public"] = {"api": "https://www.tokocrypto.com/api"}
        exchange.urls["private"] = {"api": "https://www.tokocrypto.com/api"}
        # Quick connectivity check
        exchange.load_markets()
        _exchange = exchange
        log("Exchange loaded OK")
        return _exchange
    except Exception as e:
        log(f"Failed to load exchange: {e}")
        return None


def parse_quantity_precision(symbol: str, amount: float) -> float:
    """Round amount to exchange's lot size precision."""
    ex = get_exchange()
    if ex is None:
        return amount
    try:
        market = ex.market(symbol)
        precision = market.get("lot", {}).get("amount", 8) or 8
        return round(amount, precision)
    except Exception:
        return amount


def parse_price_precision(symbol: str, price: float) -> float:
    """Round price to exchange's tick precision."""
    ex = get_exchange()
    if ex is None:
        return price
    try:
        market = ex.market(symbol)
        precision = market.get("price", {}).get("amount", 8) or 8
        return round(price, precision)
    except Exception:
        return price


def execute_trade(action: str, asset: str, size_usdt: float, entry_price: float):
    """
    Place market order. Returns order dict on success, None on failure.
    action: 'BUY' or 'SELL'
    size_usdt: notional in USDT
    entry_price: market price at signal time
    """
    if action not in ("BUY", "SELL"):
        log(f"[EXEC] Invalid action: {action}")
        return None
    if size_usdt < TKO_MIN_ORDER_USDT:
        log(f"[EXEC] Size {size_usdt:.4f} USDT < min {TKO_MIN_ORDER_USDT}, skip")
        return None

    ex = get_exchange()
    if ex is None:
        return None

    symbol = f"{asset.replace('USDT', '')}/USDT"

    # Slippage check vs current market price
    try:
        ticker = ex.fetch_ticker(symbol)
        market_price = ticker.get("last") or ticker.get("ask") or ticker.get("bid")
        if market_price and market_price > 0:
            slippage = abs(entry_price - market_price) / entry_price * 100
            if slippage > 1.0:
                log(f"[EXEC] {asset} slippage {slippage:.2f}% > 1%, skip")
                return None
    except Exception as e:
        log(f"[EXEC] Ticker fetch failed for {asset}: {e}")

    side = "buy" if action == "BUY" else "sell"
    # Clamp size to max BEFORE computing quantity (skip clamping in the early guard)
    if size_usdt > TKO_MAX_POSITION_USDT:
        log(f"[EXEC] Size {size_usdt:.4f} USDT > max {TKO_MAX_POSITION_USDT}, clamp")
        size_usdt = TKO_MAX_POSITION_USDT
    quantity = size_usdt / entry_price
    quantity = parse_quantity_precision(symbol, quantity)
    if quantity <= 0:
        log(f"[EXEC] {asset} quantity {quantity} invalid, skip")
        return None

    try:
        log(f"[EXEC] {action} {asset} qty={quantity:.6f} @ ~{entry_price:.6f} ({size_usdt:.2f} USDT)")
        order = ex.create_order(
            symbol=symbol,
            type="market",
            side=side,
            amount=quantity,
        )
        filled = float(order.get("filled", 0) or 0)
        cost = float(order.get("cost", 0) or 0)
        log(f"[EXEC] {action} {asset} FILLED: qty={filled:.6f} cost={cost:.4f} USDT id={order.get('id', 'N/A')}")
        return order
    except ccxt.InsufficientFunds as e:
        log(f"[EXEC] Insufficient funds for {asset}: {e}")
    except ccxt.InvalidOrder as e:
        log(f"[EXEC] Invalid order {asset}: {e}")
    except ccxt.NetworkError as e:
        log(f"[EXEC] Network error {asset}: {e}")
    except ccxt.ExchangeError as e:
        log(f"[EXEC] Exchange error {asset}: {e}")
    except Exception as e:
        log(f"[EXEC] Unknown error {asset}: {e}")
    return None


def compute_tp_sl(entry_price: float, action: str) -> tuple[float, float]:
    """Compute TP/SL from entry and pct config."""
    if action == "BUY":
        tp = round(entry_price * (1 + TKO_TP_PCT / 100), 8)
        sl = round(entry_price * (1 - TKO_SL_PCT / 100), 8)
    else:
        tp = round(entry_price * (1 - TKO_TP_PCT / 100), 8)
        sl = round(entry_price * (1 + TKO_SL_PCT / 100), 8)
    return tp, sl


def estimate_position_size(base_price: float) -> float:
    """Fixed-size order clamped by min/max env vars."""
    raw = float(os.getenv("TKO_MIN_ORDER_USDT", "1.20"))
    return min(raw, TKO_MAX_POSITION_USDT)

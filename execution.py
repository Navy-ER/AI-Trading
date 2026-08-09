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
TKO_SIZE_LOW = float(os.getenv("TKO_SIZE_LOW", "1.20"))   # confidence < MED
TKO_SIZE_MED = float(os.getenv("TKO_SIZE_MED", "2.00"))   # MED <= conf < HIGH
TKO_SIZE_HIGH = float(os.getenv("TKO_SIZE_HIGH", "3.00")) # conf >= HIGH
TKO_CONFIDENCE_HIGH_TH = float(os.getenv("TKO_CONFIDENCE_HIGH_TH", "80"))
TKO_CONFIDENCE_MED_TH = float(os.getenv("TKO_CONFIDENCE_MED_TH", "60"))
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


def place_stop_loss(exchange, symbol: str, position_side: str, qty: float, sl_price: float):
    """
    Place exchange-side stop-limit order that protects the position even when
    the bridge/PC is offline. Returns order id (str) or None on failure.
    position_side: 'BUY'/'SELL' (the OPEN side). SL order is the opposite side.
    """
    try:
        sl_side = "sell" if position_side == "BUY" else "buy"
        # Limit price: slight buffer beyond trigger so fill is likely once triggered.
        limit_price = sl_price * (0.995 if sl_side == "sell" else 1.005)
        limit_price = parse_price_precision(symbol, limit_price)
        qty = parse_quantity_precision(symbol, qty)
        order = exchange.create_order(
            symbol=symbol,
            type="STOP_LOSS_LIMIT",
            side=sl_side,
            amount=qty,
            price=limit_price,
            params={"stopPrice": sl_price},
        )
        oid = order.get("id")
        log(f"[EXEC-SL] {sl_side} stop-loss {symbol} qty={qty:.6f} trigger={sl_price} limit={limit_price} id={oid}")
        return oid
    except ccxt.InvalidOrder as e:
        log(f"[EXEC-SL] Invalid order {symbol}: {e}")
    except ccxt.NetworkError as e:
        log(f"[EXEC-SL] Network error {symbol}: {e}")
    except ccxt.ExchangeError as e:
        log(f"[EXEC-SL] Exchange error {symbol}: {e}")
    except Exception as e:
        log(f"[EXEC-SL] Unknown error {symbol}: {e}")
    return None


def cancel_exchange_order(exchange, symbol: str, order_id) -> bool:
    """Cancel an exchange order (e.g. orphaned SL when position closed by TP/bridge)."""
    if not order_id:
        return False
    try:
        exchange.cancel_order(order_id, symbol)
        log(f"[EXEC-CANCEL] Cancelled {order_id} {symbol}")
        return True
    except Exception as e:
        log(f"[EXEC-CANCEL] Cancel failed {order_id}: {e}")
        return False




def estimate_position_size(price: float, confidence_pct: int = 0) -> float:
    """Confidence-tier sizing. LOW conf = min, MED = medium, HIGH = high. Clamped env min/max."""
    if confidence_pct >= TKO_CONFIDENCE_HIGH_TH:
        raw = TKO_SIZE_HIGH
    elif confidence_pct >= TKO_CONFIDENCE_MED_TH:
        raw = TKO_SIZE_MED
    else:
        raw = TKO_SIZE_LOW
    return min(max(raw, TKO_MIN_ORDER_USDT), TKO_MAX_POSITION_USDT)


# Back-compat alias without confidence defaults to TKO_MIN_ORDER_USDT (old caller safe)
def estimate_legacy_size(price: float) -> float:
    raw = float(os.getenv("TKO_MIN_ORDER_USDT", "1.20"))
    return min(raw, TKO_MAX_POSITION_USDT)

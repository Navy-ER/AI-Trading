"""Real trade execution via ccxt (Binance/Tokocrypto)."""
import os
import re
import sys
import math
import json
import uuid
from dataclasses import dataclass
from pathlib import Path
import recovery_policy
import journal_policy

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
ORDER_JOURNAL = Path(__file__).parent / "order_reconciliation.jsonl"


def _journal_order(event: dict) -> bool:
    """Durably record order intent/results before trading state is changed."""
    try:
        with ORDER_JOURNAL.open("a", encoding="utf-8") as journal:
            journal.write(json.dumps(event, separators=(",", ":"), default=str) + "\n")
            journal.flush()
            os.fsync(journal.fileno())
        return True
    except OSError as exc:
        log(f"Order journal write failed; order blocked: {exc}")
        return False


def unresolved_order_intents() -> list[dict]:
    """Return latest non-terminal intents. Corrupt journal fails closed."""
    events = []
    if not ORDER_JOURNAL.exists():
        return []
    try:
        with ORDER_JOURNAL.open(encoding="utf-8") as journal:
            for line in journal:
                event = json.loads(line)
                events.append(event)
    except (OSError, json.JSONDecodeError):
        return [{"state": "AMBIGUOUS", "reason": "order journal unreadable"}]
    return journal_policy.index_journal_events(events)


def fills_requiring_position_commit() -> list[dict]:
    """Read the latest reconciled fills that still need a durable position."""
    recovery_states = {"FILL_REQUIRES_POSITION_COMMIT", "POSITION_RECOVERY_STOP_SUBMITTING",
                       "POSITION_RECOVERY_STOP_RECORDED"}
    return [event for event in unresolved_order_intents()
            if event.get("state") in recovery_states and event.get("source") == "LIVE_EXCHANGE"
            and event.get("operation_type") == "ENTRY"
            and event.get("operation_id") == event.get("client_order_id")]


def stop_fills_requiring_position_commit() -> list[dict]:
    return [event for event in unresolved_order_intents()
            if event.get("state") == "STOP_FILL_REQUIRES_POSITION_COMMIT"
            and event.get("source") == "LIVE_EXCHANGE"
            and event.get("operation_type") == "STOP_CLOSE"
            and event.get("operation_id") == event.get("client_order_id")]


def mark_order_position_recorded(client_order_id: str | None) -> bool:
    if not client_order_id:
        return False
    return _journal_order({"client_order_id": client_order_id, "state": "RESOLVED_POSITION_RECORDED"})


def reconciliation_blocks_entries() -> bool:
    return bool(unresolved_order_intents())


def reconcile_unresolved_order_intents(exchange) -> dict:
    """Reconcile journaled orders after restart. Unknowns remain a hard trading lock."""
    checked = resolved_no_fill = fills_needing_position = unresolved = 0
    for intent in unresolved_order_intents():
        if intent.get("state") in {"POSITION_RECOVERY_STOP_SUBMITTING", "POSITION_RECOVERY_STOP_RECORDED",
                                     "STOP_FILL_REQUIRES_POSITION_COMMIT"}:
            # Never resubmit a protective stop after an interrupted recovery attempt.
            fills_needing_position += 1
            continue
        if (intent.get("operation_type") not in recovery_policy.RECOVERY_OPERATIONS
                or intent.get("operation_id") != intent.get("client_order_id")):
            # Old or contradictory records are never guessed to be entries.
            unresolved += 1
            continue
        if intent.get("state") == "CLOSE_FILL_REQUIRES_POSITION_COMMIT":
            # Close replay needs position-level reconciliation; never materialize it as an entry.
            fills_needing_position += 1
            continue
        checked += 1
        if intent.get("reason") == "order journal unreadable":
            unresolved += 1
            continue
        order = reconcile_order(
            exchange, order_id=intent.get("order_id"),
            client_order_id=intent.get("client_order_id", ""),
            symbol=intent.get("symbol", ""), since_ms=intent.get("created_at_ms"),
        )
        if not order:
            unresolved += 1
            continue
        result = normalize_order_result(
            order, symbol=intent["symbol"], side=intent["side"],
            requested_qty=float(intent["requested_qty"]),
        ).as_dict()
        state = "UNRESOLVED"
        if result["classification"] == "NO_FILL_CONFIRMED":
            state = "RESOLVED_NO_FILL"
            resolved_no_fill += 1
        elif result["classification"] in {"CONFIRMED_FILL", "PARTIAL_FILL"}:
            # Only ENTRY can reach entry recovery. CLOSE remains locked for manual position reconciliation.
            state = ("FILL_REQUIRES_POSITION_COMMIT" if intent["operation_type"] == "ENTRY"
                     else "CLOSE_FILL_REQUIRES_POSITION_COMMIT")
            fills_needing_position += 1
        else:
            unresolved += 1
        _journal_order({**intent, **result, "state": state})
    return {"checked": checked, "resolved_no_fill": resolved_no_fill,
            "fills_needing_position": fills_needing_position, "unresolved": unresolved}


def validate_live_position_orders(exchange, position: dict) -> tuple[bool, str | None]:
    """Verify entry and protective stop are still represented by exchange orders."""
    asset = str(position.get("asset", ""))
    symbol = f"{asset.replace('USDT', '')}/USDT"
    entry_id = position.get("entry_order_id")
    stop_id = position.get("sl_order_id")
    if not entry_id or not stop_id:
        return False, "entry or stop-loss order id missing"
    try:
        entry = exchange.fetch_order(str(entry_id), symbol)
        stop = exchange.fetch_order(str(stop_id), symbol)
    except Exception as exc:
        return False, f"exchange order lookup inconclusive ({type(exc).__name__})"
    if not isinstance(entry, dict) or str(entry.get("status", "")).lower() != "closed":
        return False, "entry order is not confirmed closed"
    try:
        entry_filled = float(entry.get("filled")) if entry.get("filled") is not None else None
        expected_entry_qty = float(position.get("entry_filled_qty"))
        stop_filled = float(stop.get("filled")) if stop.get("filled") is not None else None
        stop_amount = float(stop.get("amount")) if stop.get("amount") is not None else None
        position_qty = float(position.get("quantity"))
    except (AttributeError, TypeError, ValueError, OverflowError):
        return False, "stop-loss quantity fields are incomplete"
    if entry_filled is None or abs(entry_filled - expected_entry_qty) > expected_entry_qty * 1e-8:
        return False, "entry fill quantity does not match recorded position"
    expected_stop_side = "sell" if str(position.get("action", "")).upper() == "BUY" else "buy"
    if str(stop.get("symbol", symbol)) != symbol or str(stop.get("side", "")).lower() != expected_stop_side:
        return False, "stop-loss identity or side does not match position"
    try:
        trigger_price = float(stop.get("triggerPrice")) if stop.get("triggerPrice") is not None else None
        expected_trigger = float(position.get("sl"))
    except (TypeError, ValueError, OverflowError):
        return False, "stop-loss trigger price is incomplete"
    if trigger_price is None or abs(trigger_price - expected_trigger) > abs(expected_trigger) * 1e-8:
        return False, "stop-loss trigger price does not match position"
    if str(stop.get("status", "")).lower() != "open" or stop_filled != 0:
        return False, "stop-loss is not open with zero executed quantity"
    if stop_amount is None or position_qty <= 0 or abs(stop_amount - position_qty) > position_qty * 1e-8:
        return False, "stop-loss quantity does not match remaining position"
    return True, None


def reconcile_triggered_stop_fill(exchange, position: dict):
    """Confirm a full stop execution from the exchange before a position is closed locally."""
    asset = str(position.get("asset", ""))
    symbol = f"{asset.replace('USDT', '')}/USDT"
    stop_id = position.get("sl_order_id")
    requested_qty = position.get("quantity")
    expected_side = "sell" if str(position.get("action", "")).upper() == "BUY" else "buy"
    try:
        requested_qty = float(requested_qty)
        expected_trigger = float(position.get("sl"))
    except (TypeError, ValueError, OverflowError):
        requested_qty = 0.0
        expected_trigger = 0.0
    if not stop_id or requested_qty <= 0 or expected_trigger <= 0:
        return FillResult("LIVE_EXCHANGE", "AMBIGUOUS", symbol, expected_side,
                          requested_qty, reason="stop identity or position fields are incomplete")
    try:
        order = exchange.fetch_order(str(stop_id), symbol)
    except Exception as exc:
        return FillResult("LIVE_EXCHANGE", "AMBIGUOUS", symbol, expected_side,
                          requested_qty, order_id=str(stop_id),
                          reason=f"stop lookup inconclusive ({type(exc).__name__})")
    if not isinstance(order, dict) or not order:
        return FillResult("LIVE_EXCHANGE", "AMBIGUOUS", symbol, expected_side,
                          requested_qty, order_id=str(stop_id), reason="empty stop order response")
    result = normalize_order_result(order, symbol=symbol, side=expected_side,
                                    requested_qty=requested_qty)
    evidence = {**result.as_dict(), "symbol": order.get("symbol"),
                "side": order.get("side"), "trigger_price": order.get("triggerPrice"),
                "quantity_unit": "base_asset",
                "client_order_id": order.get("clientOrderId") or order.get("clientOrderID")}
    recovery_id = f"stop-loss:{symbol}:{stop_id}"
    evidence.update({"client_order_id": recovery_id, "operation_id": recovery_id,
                     "operation_type": "STOP_CLOSE",
                     "exchange_client_order_id": order.get("clientOrderId") or order.get("clientOrderID"),
                     "related_entry_order_id": position.get("entry_order_id")})
    decision = recovery_policy.decide_stop_fill(position, evidence)
    if decision["decision"] not in {"CLOSE_POSITION", "PARTIAL_STOP_RECONCILIATION_REQUIRED"}:
        return FillResult("LIVE_EXCHANGE", "AMBIGUOUS", order.get("symbol"), order.get("side"),
                          requested_qty, filled_qty=result.filled_qty,
                          average_fill_price=result.average_fill_price,
                          quote_qty=result.quote_qty, order_id=order.get("id"),
                          exchange_status=result.exchange_status,
                          reason=decision["reason"], trigger_price=order.get("triggerPrice"),
                          quantity_unit="base_asset",
                          client_order_id=order.get("clientOrderId") or order.get("clientOrderID"))
    return FillResult(**{
        **result.as_dict(),
        "symbol": order.get("symbol"),
        "side": order.get("side"),
        "order_id": order.get("id"),
        "trigger_price": order.get("triggerPrice"),
        "quantity_unit": "base_asset",
        "client_order_id": order.get("clientOrderId") or order.get("clientOrderID"),
    })


def reconcile_order(exchange, *, order_id: str | None, client_order_id: str,
                    symbol: str, since_ms: int | None = None):
    """Query a known order, or search recent history by exact client id; never retries."""
    try:
        if order_id:
            return exchange.fetch_order(str(order_id), symbol)
        if not client_order_id:
            return None
        orders = exchange.fetch_orders(symbol, since=since_ms, limit=1000)
        matches = [o for o in orders or [] if isinstance(o, dict)
                   and str(o.get("clientOrderId") or o.get("clientId") or "") == client_order_id]
        return matches[0] if len(matches) == 1 else None
    except Exception as exc:
        log(f"Order reconciliation query inconclusive: {type(exc).__name__}")
        return None


def cancel_and_reconcile_order(exchange, *, order_id: str | None, symbol: str,
                               side: str, requested_qty: float):
    """Try to stop a resting order, then trust only a fresh fetch_order snapshot."""
    if not order_id:
        return None
    try:
        exchange.cancel_order(str(order_id), symbol)
    except Exception:
        pass  # Cancel response is not proof; query current order state below.
    try:
        snapshot = exchange.fetch_order(str(order_id), symbol)
    except Exception as exc:
        log(f"Post-cancel order lookup inconclusive: {type(exc).__name__}")
        return None
    return normalize_order_result(snapshot, symbol=symbol, side=side,
                                  requested_qty=requested_qty)


@dataclass(frozen=True)
class FillResult:
    source: str
    classification: str
    symbol: str
    side: str
    requested_qty: float
    filled_qty: float | None = None
    average_fill_price: float | None = None
    quote_qty: float | None = None
    order_id: str | None = None
    exchange_status: str | None = None
    reason: str | None = None
    trigger_price: float | None = None
    quantity_unit: str | None = None
    client_order_id: str | None = None

    def as_dict(self) -> dict:
        return {key: value for key, value in self.__dict__.items()}


def normalize_order_result(order, *, symbol: str, side: str, requested_qty: float,
                           source: str = "LIVE_EXCHANGE") -> FillResult:
    """Conservatively normalize a CCXT-style response; never infer a fill from success alone."""
    if source != "LIVE_EXCHANGE":
        return FillResult(source, "AMBIGUOUS", symbol, side, requested_qty,
                          reason="non-exchange result cannot establish an exchange fill")
    if not isinstance(order, dict) or not order:
        return FillResult(source, "AMBIGUOUS", symbol, side, requested_qty,
                          reason="empty or malformed order response")

    evidence_error = journal_policy.validate_order_evidence(
        order, symbol=symbol, side=side, requested_qty=requested_qty)

    raw_id = order.get("id")
    order_id = str(raw_id) if raw_id not in (None, "") else None
    raw_status = order.get("status")
    status = str(raw_status).strip().lower() if raw_status is not None else None
    try:
        filled = float(order.get("filled")) if order.get("filled") is not None else None
        average = float(order.get("average")) if order.get("average") is not None else None
        cost = float(order.get("cost")) if order.get("cost") is not None else None
        amount = float(order.get("amount")) if order.get("amount") is not None else requested_qty
    except (TypeError, ValueError, OverflowError):
        return FillResult(source, "AMBIGUOUS", symbol, side, requested_qty,
                          order_id=order_id, exchange_status=status,
                          reason="non-numeric quantity or price field")
    if evidence_error:
        return FillResult(source, "AMBIGUOUS", symbol, side, requested_qty,
                          filled_qty=filled, average_fill_price=average,
                          quote_qty=cost, order_id=order_id, exchange_status=status,
                          reason=evidence_error)

    for value in (filled, average, cost, amount):
        if value is not None and (not math.isfinite(value) or value < 0):
            return FillResult(source, "AMBIGUOUS", symbol, side, requested_qty,
                              order_id=order_id, exchange_status=status,
                              reason="negative or non-finite numeric field")
    if filled is not None and filled > 0 and (average is None or average <= 0):
        if cost is not None and cost > 0:
            average = cost / filled
    if filled is not None and filled > 0 and (average is None or average <= 0):
        return FillResult(source, "AMBIGUOUS", symbol, side, requested_qty,
                          filled_qty=filled, quote_qty=cost, order_id=order_id,
                          exchange_status=status, reason="positive fill lacks a valid fill price")

    mapped = journal_policy.normalize_order_status(status)
    if mapped is None:
        return FillResult(source, "AMBIGUOUS", symbol, side, requested_qty,
                          filled_qty=filled, average_fill_price=average,
                          quote_qty=cost, order_id=order_id, exchange_status=status,
                          reason="unknown or missing order status")
    if mapped == "pending":
        category = "PENDING"
    elif filled is None:
        category = "AMBIGUOUS"
    elif filled == 0:
        category = "NO_FILL_CONFIRMED" if mapped in {"terminal_canceled", "terminal_rejected"} else "AMBIGUOUS"
    elif mapped == "terminal_rejected":
        category = "AMBIGUOUS"
    elif requested_qty <= 0:
        category = "AMBIGUOUS"
    elif filled > requested_qty + requested_qty * 1e-9:
        category = "AMBIGUOUS"
    elif filled + requested_qty * 1e-9 >= requested_qty:
        category = "CONFIRMED_FILL" if mapped in {"terminal_filled", "terminal_canceled"} else "AMBIGUOUS"
    else:
        # CCXT defines `closed` as fully filled. A closed order whose filled
        # amount contradicts the submitted quantity is inconsistent evidence.
        category = "PARTIAL_FILL" if mapped == "terminal_canceled" else "AMBIGUOUS"
    return FillResult(source, category, symbol, side, requested_qty,
                      filled_qty=filled, average_fill_price=average, quote_qty=cost,
                      order_id=order_id, exchange_status=status,
                      reason=None if category != "AMBIGUOUS" else "insufficient evidence to classify order")


def parse_trading_mode(value: str | None) -> str:
    """Pure policy parser; LIVE requires an explicit false value."""
    if value is None:
        return "REJECTED"

    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes"}:
        return "DRY_RUN"
    if normalized in {"0", "false", "no"}:
        return "LIVE"
    return "REJECTED"


def trading_mode() -> str:
    """Return LIVE only for an explicit false value; reject missing/unknown values."""
    return parse_trading_mode(os.getenv("TKO_DRY_RUN"))


def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[EXEC] {ts} {msg}", flush=True)


def get_exchange():
    """Return the native CCXT Tokocrypto adapter for spot trading."""
    global _exchange
    mode = trading_mode()
    if mode != "LIVE":
        log("Exchange access blocked by trading mode")
        return None
    if _exchange is not None:
        return _exchange
    if not TOKOCRYPTO_API_KEY or not TOKOCRYPTO_SECRET_KEY:
        log("No API keys — execution disabled")
        return None
    try:
        exchange = ccxt.tokocrypto({
            "apiKey": TOKOCRYPTO_API_KEY,
            "secret": TOKOCRYPTO_SECRET_KEY,
            "enableRateLimit": True,
        })
        # Quick connectivity check
        exchange.load_markets()
        _exchange = exchange
        log("Exchange loaded OK")
        return _exchange
    except Exception as e:
        log(f"Failed to load exchange: {e}")
        return None


def parse_quantity_precision(symbol: str, amount: float) -> float:
    """Format a base quantity with CCXT's exchange-specific amount precision."""
    ex = get_exchange()
    if ex is None:
        return 0.0
    try:
        return float(ex.amount_to_precision(symbol, amount))
    except Exception:
        return 0.0


def parse_price_precision(symbol: str, price: float) -> float:
    """Format a price with CCXT's exchange-specific price precision."""
    ex = get_exchange()
    if ex is None:
        return 0.0
    try:
        return float(ex.price_to_precision(symbol, price))
    except Exception:
        return 0.0


def execute_trade(action: str, asset: str, size_usdt: float, entry_price: float,
                  base_qty: float | None = None, operation_type: str = "ENTRY",
                  related_entry_order_id: str | None = None):
    """
    Place market order. Returns order dict on success, None on failure.
    action: 'BUY' or 'SELL'
    size_usdt: notional in USDT
    entry_price: market price at signal time
    """
    if trading_mode() != "LIVE":
        log("Market order blocked by trading mode")
        return None

    if operation_type not in {"ENTRY", "CLOSE"}:
        log(f"[EXEC] Invalid recovery operation type: {operation_type}")
        return None

    if action not in ("BUY", "SELL"):
        log(f"[EXEC] Invalid action: {action}")
        return None
    if base_qty is None and size_usdt < TKO_MIN_ORDER_USDT:
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
    if base_qty is None and size_usdt > TKO_MAX_POSITION_USDT:
        log(f"[EXEC] Size {size_usdt:.4f} USDT > max {TKO_MAX_POSITION_USDT}, clamp")
        size_usdt = TKO_MAX_POSITION_USDT
    quantity = float(base_qty) if base_qty is not None else size_usdt / entry_price
    quantity = parse_quantity_precision(symbol, quantity)
    if quantity <= 0:
        log(f"[EXEC] {asset} quantity {quantity} invalid, skip")
        return None

    try:
        client_order_id = uuid.uuid4().hex
        created_at_ms = int(datetime.now().timestamp() * 1000)
        intent = {
            "client_order_id": client_order_id, "state": "SUBMITTING",
            "operation_id": client_order_id, "operation_type": operation_type,
            "related_entry_order_id": related_entry_order_id,
            "created_at_ms": created_at_ms, "symbol": symbol, "side": side,
            "requested_qty": quantity, "asset": asset, "action": action,
            "reference_price": entry_price,
        }
        if not _journal_order(intent):
            return None
        log(f"[EXEC] {action} {asset} qty={quantity:.6f} @ ~{entry_price:.6f} ({size_usdt:.2f} USDT)")
        order = ex.create_order(
            symbol=symbol,
            type="market",
            side=side,
            amount=quantity,
            params={"clientOrderId": client_order_id},
        )
        result = normalize_order_result(
            order, symbol=symbol, side=side, requested_qty=quantity,
        )
        if result.classification == "PENDING" and result.order_id:
            settled = cancel_and_reconcile_order(
                ex, order_id=result.order_id, symbol=symbol, side=side,
                requested_qty=quantity,
            )
            if settled is not None:
                result = settled
        result_dict = result.as_dict()
        result_dict["client_order_id"] = client_order_id
        result_dict["operation_id"] = client_order_id
        result_dict["operation_type"] = operation_type
        result_dict["related_entry_order_id"] = related_entry_order_id
        state = "UNRESOLVED"
        if result.classification == "NO_FILL_CONFIRMED":
            state = "RESOLVED_NO_FILL"
        elif result.classification in {"CONFIRMED_FILL", "PARTIAL_FILL"}:
            state = ("FILL_REQUIRES_POSITION_COMMIT" if operation_type == "ENTRY"
                     else "CLOSE_FILL_REQUIRES_POSITION_COMMIT")
        _journal_order({**intent, **result_dict, "state": state})
        log(f"[EXEC] {action} {asset} order={result.classification} "
            f"qty={result.filled_qty} id={result.order_id or 'N/A'}")
        return result_dict
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
    ambiguous = FillResult(
        "LIVE_EXCHANGE", "AMBIGUOUS", symbol, side, quantity,
        reason="order submission raised an exception; reconcile before retry",
    ).as_dict()
    ambiguous["client_order_id"] = client_order_id if "client_order_id" in locals() else None
    if ambiguous["client_order_id"]:
        _journal_order({**intent, **ambiguous, "state": "UNRESOLVED"})
    return ambiguous


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
    if trading_mode() != "LIVE":
        log("Stop-loss order blocked by trading mode")
        return None

    try:
        sl_side = "sell" if position_side == "BUY" else "buy"
        # Limit price: slight buffer beyond trigger so fill is likely once triggered.
        limit_price = sl_price * (0.995 if sl_side == "sell" else 1.005)
        limit_price = parse_price_precision(symbol, limit_price)
        qty = parse_quantity_precision(symbol, qty)
        if limit_price <= 0 or qty <= 0:
            log(f"[EXEC-SL] Invalid formatted price/quantity for {symbol}")
            return None
        order = exchange.create_order(
            symbol=symbol,
            type="limit",
            side=sl_side,
            amount=qty,
            price=limit_price,
            params={"triggerPrice": sl_price},
        )
        oid = order.get("id")
        status = str(order.get("status", "")).strip().lower() if isinstance(order, dict) else ""
        if not oid or status != "open":
            log(f"[EXEC-SL] Stop-loss acceptance unconfirmed for {symbol}")
            return None
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
    if trading_mode() != "LIVE":
        log("Order cancellation blocked by trading mode")
        return False
    if not order_id:
        return False
    try:
        order = exchange.cancel_order(order_id, symbol)
        if not isinstance(order, dict) or str(order.get("status", "")).strip().lower() not in {"canceled", "cancelled"}:
            log(f"[EXEC-CANCEL] Cancel status unconfirmed for {order_id} {symbol}")
            return False
        try:
            filled = float(order.get("filled")) if order.get("filled") is not None else None
        except (TypeError, ValueError, OverflowError):
            return False
        if filled != 0:
            log(f"[EXEC-CANCEL] Cancel response has fill or missing fill amount for {order_id} {symbol}")
            return False
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

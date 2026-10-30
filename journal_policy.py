"""Pure journal and exchange-evidence validation rules.

This module deliberately has no runtime, exchange, environment, or filesystem imports.
"""
import math


VALID_OPERATIONS = {"ENTRY", "CLOSE", "STOP_CLOSE"}
TERMINAL_STATES = {"RESOLVED_NO_FILL", "RESOLVED_POSITION_RECORDED"}
QUANTITY_TOLERANCE = 1e-8
ORDER_STATUS_MAP = {
    "closed": "terminal_filled", "canceled": "terminal_canceled",
    "cancelled": "terminal_canceled", "expired": "terminal_canceled",
    "rejected": "terminal_rejected", "open": "pending", "pending": "pending",
}


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _matches(left, right, tolerance=QUANTITY_TOLERANCE):
    a, b = _number(left), _number(right)
    return (a is not None and b is not None
            and abs(a - b) <= max(abs(a), abs(b), 1e-12) * tolerance)


def index_journal_events(events):
    """Return latest events, retaining conflicts as unresolved ambiguity."""
    indexed = {}
    for event in events:
        if not isinstance(event, dict) or not event.get("client_order_id"):
            return [{"state": "AMBIGUOUS", "reason": "journal event lacks client id"}]
        key = event["client_order_id"]
        prior = indexed.get(key)
        if event.get("state") in TERMINAL_STATES:
            terminal_conflict = (
                not prior
                or prior.get("journal_conflict")
                or (
                    event.get("operation_type") is not None
                    and event.get("operation_type") != prior.get("operation_type")
                )
                or (
                    event.get("operation_id") is not None
                    and event.get("operation_id") != prior.get("operation_id")
                )
                or (
                    "related_entry_order_id" in event
                    and event.get("related_entry_order_id")
                    != prior.get("related_entry_order_id")
                )
            )
            if terminal_conflict:
                indexed[key] = {
                    "client_order_id": key, "state": "AMBIGUOUS",
                    "journal_conflict": True,
                    "reason": "journal resolution conflicts with prior operation identity",
                }
            else:
                indexed[key] = {**prior, "state": event["state"]}
            continue
        operation = event.get("operation_type")
        operation_id = event.get("operation_id")
        relation = event.get("related_entry_order_id")
        invalid = operation not in VALID_OPERATIONS or operation_id != key
        conflict = bool(prior and (
            prior.get("operation_type") != operation
            or prior.get("operation_id") != operation_id
            or prior.get("related_entry_order_id") != relation
            or prior.get("journal_conflict")
        ))
        if invalid or conflict:
            indexed[key] = {
                "client_order_id": key, "state": "AMBIGUOUS",
                "journal_conflict": True,
                "reason": "journal operation identity is missing or conflicts with prior evidence",
            }
        else:
            indexed[key] = dict(event)
    return [event for event in indexed.values() if event.get("state") not in TERMINAL_STATES]


def validate_order_evidence(order, *, symbol, side, requested_qty):
    """Validate raw exchange fields before a fill classification is trusted."""
    if not isinstance(order, dict) or not order:
        return "empty or malformed order response"
    if not order.get("id"):
        return "order response lacks stable order identity"
    if order.get("symbol") != symbol:
        return "order response symbol does not match request"
    if str(order.get("side") or "").lower() != str(side).lower():
        return "order response side does not match request"
    amount = _number(order.get("amount"))
    requested = _number(requested_qty)
    if amount is None or amount <= 0 or requested is None or requested <= 0:
        return "order response amount or requested quantity is invalid"
    if not _matches(amount, requested):
        return "order response amount does not match requested quantity"
    return None


def normalize_order_status(value):
    if value is None:
        return None
    return ORDER_STATUS_MAP.get(str(value).strip().lower())


def quarantine_close_positions(positions, intents):
    """Return copied positions with only exact related-entry matches quarantined.

    Any unresolved CLOSE lacking one exact entry relation forces a global entry block.
    """
    if not isinstance(positions, list):
        return None, True
    copied = [dict(position) if isinstance(position, dict) else position for position in positions]
    global_block = False
    for intent in intents:
        if not isinstance(intent, dict) or intent.get("operation_type") != "CLOSE":
            continue
        entry_id = intent.get("related_entry_order_id")
        if not entry_id:
            global_block = True
            continue
        matches = [position for position in copied if isinstance(position, dict)
                   and str(position.get("entry_order_id")) == str(entry_id)]
        if len(matches) != 1:
            global_block = True
            continue
        matches[0]["reconciliation_required"] = True
        matches[0]["reconciliation_reason"] = "unresolved close order requires reconciliation"
    return copied, global_block

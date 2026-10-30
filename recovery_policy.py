"""Pure decision rules for reconciling exchange fills with position state.

This module intentionally has no runtime, filesystem, exchange, network, or bot imports.
Inputs are normalized order evidence and position snapshots; callers own all I/O.
"""
import math


RECOVERY_OPERATIONS = {"ENTRY", "CLOSE", "STOP_CLOSE"}


def classify_recovery_operation(record, *, expected_operation_id=None):
    """Validate the explicit operation identity before selecting a recovery path."""
    if not isinstance(record, dict):
        return {"decision": "QUARANTINE", "reason": "recovery record is missing"}
    operation = record.get("operation_type")
    client_id = record.get("client_order_id")
    operation_id = record.get("operation_id")
    if operation not in RECOVERY_OPERATIONS:
        return {"decision": "QUARANTINE", "reason": "operation type is missing or unknown"}
    if not client_id or operation_id != client_id:
        return {"decision": "QUARANTINE", "reason": "operation identity conflicts with order identity"}
    if expected_operation_id is not None and operation_id != expected_operation_id:
        return {"decision": "QUARANTINE", "reason": "operation identity does not match expected order"}
    return {"decision": "ACCEPT", "operation_type": operation}


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _matches_number(left, right, tolerance=1e-8):
    a = _number(left)
    b = _number(right)
    if a is None or b is None:
        return False
    return abs(a - b) <= max(abs(a), abs(b), 1e-12) * tolerance


def find_matching_entry_position(fill, positions):
    """Find an existing position for a fill only when IDs and known fill data agree."""
    if not isinstance(fill, dict) or not isinstance(positions, (list, tuple)):
        return None
    client_id = fill.get("client_order_id")
    order_id = fill.get("order_id")
    matches = []
    for position in positions:
        if not isinstance(position, dict):
            continue
        client_match = bool(client_id and position.get("entry_client_order_id") == client_id)
        order_match = bool(order_id and position.get("entry_order_id") is not None
                           and str(position.get("entry_order_id")) == str(order_id))
        if client_match or order_match:
            matches.append(position)
    if len(matches) != 1:
        return None

    position = matches[0]
    existing_client_id = position.get("entry_client_order_id")
    existing_order_id = position.get("entry_order_id")
    if (client_id and existing_client_id == client_id
            and existing_order_id and order_id
            and str(existing_order_id) != str(order_id)):
        return None
    if existing_client_id and client_id and existing_client_id != client_id:
        return None
    if existing_order_id and order_id and str(existing_order_id) != str(order_id):
        return None

    for fill_key, position_key in (("asset", "asset"), ("action", "action")):
        expected = fill.get(fill_key)
        actual = position.get(position_key)
        if not expected or not actual or str(expected).upper() != str(actual).upper():
            return None
    expected_qty = _number(fill.get("filled_qty"))
    actual_qty = _number(position.get("entry_filled_qty", position.get("quantity")))
    expected_price = _number(fill.get("average_fill_price"))
    actual_price = _number(position.get("entry_price"))
    if (expected_qty is None or actual_qty is None or not _matches_number(expected_qty, actual_qty)
            or expected_price is None or actual_price is None
            or not _matches_number(expected_price, actual_price)):
        return None
    return position


def should_prune_position(position, age_seconds, stale_after_seconds):
    """Quarantined positions are retained until explicitly reconciled."""
    if not isinstance(position, dict) or position.get("reconciliation_required"):
        return False
    age = _number(age_seconds)
    threshold = _number(stale_after_seconds)
    return age is not None and threshold is not None and age > threshold


def positions_block_entries(positions):
    """Fail closed if the position snapshot is malformed or still quarantined."""
    if not isinstance(positions, list):
        return True
    return any(not isinstance(position, dict) or position.get("reconciliation_required")
               for position in positions)


def decide_entry_recovery(fill, *, existing_client_order_ids=(), existing_order_ids=()):
    """Return whether a normalized LIVE fill can be materialized as an open position."""
    if not isinstance(fill, dict) or fill.get("source") != "LIVE_EXCHANGE":
        return {"decision": "QUARANTINE", "reason": "fill is not exchange-sourced"}

    operation = classify_recovery_operation(fill)
    if operation["decision"] != "ACCEPT" or operation["operation_type"] != "ENTRY":
        return {"decision": "QUARANTINE", "reason": operation.get(
            "reason", "non-entry fill cannot recover an entry position")}
    if fill.get("related_entry_order_id") not in (None, ""):
        return {"decision": "QUARANTINE", "reason": "entry operation conflicts with related close-position identity"}

    client_id = fill.get("client_order_id")
    order_id = fill.get("order_id")
    if ((client_id and client_id in set(existing_client_order_ids))
            or (order_id and str(order_id) in {str(item) for item in existing_order_ids if item})):
        return {"decision": "DUPLICATE", "reason": "fill identity already has a position"}

    classification = fill.get("classification")
    if classification in {"PENDING", "AMBIGUOUS", "NO_FILL_CONFIRMED"}:
        return {"decision": "QUARANTINE", "reason": f"entry classification is {classification}"}
    if classification not in {"CONFIRMED_FILL", "PARTIAL_FILL"}:
        return {"decision": "QUARANTINE", "reason": "entry classification is unknown"}

    quantity = _number(fill.get("filled_qty"))
    price = _number(fill.get("average_fill_price"))
    if quantity is None or quantity <= 0 or price is None or price <= 0:
        return {"decision": "QUARANTINE", "reason": "entry quantity or fill price is invalid"}
    if not client_id and not order_id:
        return {"decision": "QUARANTINE", "reason": "entry fill has no stable order identity"}
    return {"decision": "RECOVER_POSITION", "reason": None,
            "identity": str(client_id or order_id), "filled_qty": quantity,
            "average_fill_price": price, "classification": classification}


def decide_close_recovery(position, fill):
    """Validate a CLOSE against the exact open entry identity; never create a position."""
    if not isinstance(position, dict) or not isinstance(fill, dict):
        return {"decision": "QUARANTINE", "reason": "position or close evidence is missing"}
    operation = classify_recovery_operation(fill)
    if operation["decision"] != "ACCEPT" or operation["operation_type"] != "CLOSE":
        return {"decision": "QUARANTINE", "reason": operation.get(
            "reason", "close recovery requires CLOSE operation")}
    entry_order_id = position.get("entry_order_id")
    if (not entry_order_id or fill.get("related_entry_order_id") is None
            or str(fill.get("related_entry_order_id")) != str(entry_order_id)):
        return {"decision": "QUARANTINE", "reason": "close does not identify the related open entry"}
    asset = str(position.get("asset", ""))
    action = str(position.get("action", "")).upper()
    expected_symbol = f"{asset.replace('USDT', '')}/USDT" if asset else ""
    expected_side = "sell" if action == "BUY" else "buy" if action == "SELL" else ""
    if (not fill.get("order_id") or fill.get("symbol") != expected_symbol
            or str(fill.get("side", "")).lower() != expected_side):
        return {"decision": "QUARANTINE", "reason": "close order identity, symbol, or side conflicts"}
    requested = _number(position.get("quantity"))
    requested_fill = _number(fill.get("requested_qty"))
    filled = _number(fill.get("filled_qty"))
    price = _number(fill.get("average_fill_price"))
    if (requested is None or requested <= 0 or requested_fill is None
            or not _matches_number(requested, requested_fill)
            or filled is None or filled <= 0 or filled > requested
            or price is None or price <= 0):
        return {"decision": "QUARANTINE", "reason": "close quantity or price is invalid"}
    if fill.get("classification") == "CONFIRMED_FILL":
        if (str(fill.get("exchange_status", "")).lower() not in {"closed", "canceled", "cancelled", "expired"}
                or not _matches_number(filled, requested)):
            return {"decision": "QUARANTINE", "reason": "full close status and quantity do not agree"}
        return {"decision": "CLOSE_POSITION", "closes_position": True,
                "remaining_qty": 0.0, "reason": None}
    if fill.get("classification") == "PARTIAL_FILL":
        if (str(fill.get("exchange_status", "")).lower() not in {"canceled", "cancelled", "expired"}
                or filled >= requested):
            return {"decision": "QUARANTINE", "reason": "partial close status and quantity do not agree"}
        return {"decision": "PARTIAL_CLOSE_RECONCILIATION_REQUIRED",
                "closes_position": False, "remaining_qty": requested - filled,
                "reason": "partial close requires manual reconciliation"}
    return {"decision": "QUARANTINE", "reason": "close fill is pending, ambiguous, or unknown"}


def reconcile_stop_fill(position, fill, prior_evidence=None):
    """Validate a cumulative stop snapshot; never add repeated snapshots together."""
    position = position if isinstance(position, dict) else {}
    fill = fill if isinstance(fill, dict) else {}
    action = str(position.get("action", "")).upper()
    asset = str(position.get("asset", ""))
    requested_qty = _number(position.get("quantity"))
    filled_qty = _number(fill.get("filled_qty"))
    price = _number(fill.get("average_fill_price"))
    quote_qty = _number(fill.get("quote_qty"))
    trigger = _number(fill.get("trigger_price"))
    status = str(fill.get("exchange_status") or "").strip().lower()
    classification = fill.get("classification")
    order_id = fill.get("order_id")
    quantity_unit = fill.get("quantity_unit")

    evidence = {
        "schema_version": 1,
        "source": fill.get("source"),
        "order_id": str(order_id) if order_id not in (None, "") else None,
        "client_order_id": fill.get("exchange_client_order_id", fill.get("client_order_id")),
        "symbol": fill.get("symbol"),
        "side": str(fill.get("side") or "").lower() or None,
        "requested_qty": requested_qty,
        "cumulative_filled_qty": filled_qty,
        "remaining_qty": None,
        "quantity_unit": quantity_unit,
        "average_fill_price": price,
        "quote_qty": quote_qty,
        "trigger_price": trigger,
        "exchange_status": status or None,
        "classification": classification,
        "reconciliation_required": True,
        "manual_reconciliation_required": True,
        "reason": None,
    }

    def quarantine(reason):
        evidence["reason"] = reason
        if isinstance(prior_evidence, dict):
            evidence["last_valid_cumulative_filled_qty"] = prior_evidence.get(
                "last_valid_cumulative_filled_qty",
                prior_evidence.get("cumulative_filled_qty"),
            )
            evidence["last_valid_remaining_qty"] = prior_evidence.get(
                "last_valid_remaining_qty", prior_evidence.get("remaining_qty"))
        return {"decision": "QUARANTINE", "closes_position": False,
                "remaining_qty": None, "reason": reason, "evidence": evidence}

    expected_symbol = f"{asset.replace('USDT', '')}/USDT" if asset else ""
    expected_side = "sell" if action == "BUY" else "buy" if action == "SELL" else ""
    if not position or not fill or fill.get("source") != "LIVE_EXCHANGE":
        return quarantine("position or LIVE exchange evidence is missing")
    operation = classify_recovery_operation(fill)
    if operation["decision"] != "ACCEPT" or operation["operation_type"] != "STOP_CLOSE":
        return quarantine(operation.get("reason", "stop recovery requires STOP_CLOSE operation"))
    entry_order_id = position.get("entry_order_id")
    if (not entry_order_id or fill.get("related_entry_order_id") is None
            or str(fill.get("related_entry_order_id")) != str(entry_order_id)):
        return quarantine("stop close does not identify the related open entry")
    if (not position.get("sl_order_id") or not order_id
            or str(position.get("sl_order_id")) != str(order_id)
            or fill.get("symbol") != expected_symbol
            or str(fill.get("side") or "").lower() != expected_side):
        return quarantine("stop order identity, symbol, or side does not match")
    expected_client_id = position.get("sl_client_order_id")
    observed_client_id = fill.get("exchange_client_order_id", fill.get("client_order_id"))
    if expected_client_id and observed_client_id != expected_client_id:
        return quarantine("stop client order identity does not match")
    if (requested_qty is None or requested_qty <= 0
            or not _matches_number(fill.get("requested_qty"), requested_qty)):
        return quarantine("initial stop quantity is missing or inconsistent")
    if quantity_unit != "base_asset":
        return quarantine("stop quantity unit is missing or unsupported")
    if trigger is None or trigger <= 0 or not _matches_number(trigger, position.get("sl")):
        return quarantine("stop trigger is missing or does not match position")
    if filled_qty is None or filled_qty < 0 or filled_qty > requested_qty:
        return quarantine("cumulative stop fill quantity is invalid")
    if filled_qty > 0 and (price is None or price <= 0):
        return quarantine("positive stop fill lacks a valid average price")
    if (quote_qty is not None and quote_qty > 0 and filled_qty > 0
            and not _matches_number(quote_qty, filled_qty * price, tolerance=1e-6)):
        return quarantine("stop average price conflicts with normalized quote quantity")

    if isinstance(prior_evidence, dict):
        previous_order = prior_evidence.get("order_id")
        previous_unit = prior_evidence.get("quantity_unit")
        previous_qty = _number(prior_evidence.get("requested_qty"))
        previous_fill = _number(prior_evidence.get(
            "last_valid_cumulative_filled_qty",
            prior_evidence.get("cumulative_filled_qty"),
        ))
        if (str(previous_order) != str(order_id) or previous_unit != quantity_unit
                or previous_qty is None or not _matches_number(previous_qty, requested_qty)
                or previous_fill is None or filled_qty < previous_fill):
            return quarantine("stop fill snapshot conflicts with prior cumulative evidence")

    terminal_full_statuses = {"closed", "canceled", "cancelled", "expired"}
    terminal_partial_statuses = {"canceled", "cancelled", "expired"}
    if classification == "CONFIRMED_FILL":
        if status not in terminal_full_statuses or not _matches_number(filled_qty, requested_qty):
            return quarantine("full fill status and cumulative quantity do not agree")
        evidence.update({"remaining_qty": 0.0, "reconciliation_required": False,
                         "manual_reconciliation_required": False})
        return {"decision": "CLOSE_POSITION", "closes_position": True,
                "remaining_qty": 0.0, "reason": None, "evidence": evidence}

    is_pending = (classification == "PENDING" and status in {"open", "pending"}
                  and filled_qty < requested_qty)
    is_terminal_partial = (classification == "PARTIAL_FILL"
                           and status in terminal_partial_statuses
                           and 0 < filled_qty < requested_qty)
    if is_pending or is_terminal_partial:
        evidence.update({"remaining_qty": requested_qty - filled_qty,
                         "manual_reconciliation_required": False,
                         "reason": "stop order remains pending" if is_pending else
                         "terminal partial stop fill; remaining exposure is quarantined"})
        return {"decision": "PARTIAL_STOP_RECONCILIATION_REQUIRED",
                "closes_position": False, "remaining_qty": evidence["remaining_qty"],
                "reason": evidence["reason"], "evidence": evidence}
    return quarantine("stop status or fill classification is unknown or incomplete")


def decide_stop_fill(position, fill):
    """Compatibility view: only validated full fill authorizes closing."""
    result = reconcile_stop_fill(position, fill)
    return {"decision": result["decision"],
            "closes_position": result["closes_position"],
            "reason": result["reason"]}

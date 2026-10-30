"""Isolated tests for pure recovery decisions; imports no trading/runtime modules."""
import unittest
import json
import tempfile
from pathlib import Path

from recovery_policy import (
    decide_entry_recovery,
    decide_close_recovery,
    decide_stop_fill,
    find_matching_entry_position,
    positions_block_entries,
    reconcile_stop_fill,
    should_prune_position,
)


class EntryRecoveryPolicyTests(unittest.TestCase):
    def setUp(self):
        self.fill = {
            "source": "LIVE_EXCHANGE",
            "operation_type": "ENTRY",
            "operation_id": "client-1",
            "classification": "CONFIRMED_FILL",
            "client_order_id": "client-1",
            "order_id": "entry-1",
            "asset": "BTCUSDT",
            "action": "BUY",
            "entry_order_id": "entry-1",
            "filled_qty": 0.25,
            "average_fill_price": 100.0,
        }

    def test_confirmed_exchange_fill_can_recover_position(self):
        result = decide_entry_recovery(self.fill)
        self.assertEqual(result["decision"], "RECOVER_POSITION")
        self.assertEqual(result["filled_qty"], 0.25)
        self.assertEqual(result["average_fill_price"], 100.0)

    def test_duplicate_client_or_order_identity_is_rejected(self):
        by_client = decide_entry_recovery(self.fill, existing_client_order_ids={"client-1"})
        by_order = decide_entry_recovery(self.fill, existing_order_ids={"entry-1"})
        self.assertEqual(by_client["decision"], "DUPLICATE")
        self.assertEqual(by_order["decision"], "DUPLICATE")

    def test_crash_window_position_matches_by_order_id_and_can_be_backfilled(self):
        existing = {
            "entry_order_id": "entry-1",
            "asset": "BTCUSDT",
            "action": "BUY",
            "quantity": 0.25,
            "entry_price": 100.0,
        }
        self.assertIs(find_matching_entry_position(self.fill, [existing]), existing)
        self.assertEqual(
            decide_entry_recovery(self.fill, existing_order_ids={"entry-1"})["decision"],
            "DUPLICATE",
        )

    def test_existing_order_identity_with_conflicting_fill_is_not_matched(self):
        existing = {
            "entry_order_id": "entry-1",
            "asset": "ETHUSDT",
            "action": "BUY",
            "quantity": 0.25,
            "entry_price": 100.0,
        }
        self.assertIsNone(find_matching_entry_position(self.fill, [existing]))

    def test_dry_run_pending_ambiguous_and_no_fill_are_not_recovered(self):
        variants = [
            {**self.fill, "source": "DRY_RUN_SIMULATOR"},
            {**self.fill, "classification": "PENDING"},
            {**self.fill, "classification": "AMBIGUOUS"},
            {**self.fill, "classification": "NO_FILL_CONFIRMED"},
        ]
        for fill in variants:
            with self.subTest(fill=fill):
                self.assertEqual(decide_entry_recovery(fill)["decision"], "QUARANTINE")

    def test_entry_missing_identity_quantity_or_price_is_not_recovered(self):
        variants = [
            {key: value for key, value in self.fill.items() if key != "order_id" and key != "client_order_id"},
            {**self.fill, "filled_qty": 0},
            {**self.fill, "average_fill_price": float("nan")},
        ]
        for fill in variants:
            with self.subTest(fill=fill):
                self.assertEqual(decide_entry_recovery(fill)["decision"], "QUARANTINE")


class StopRecoveryPolicyTests(unittest.TestCase):
    def setUp(self):
        self.position = {
            "source": "LIVE_EXCHANGE",
            "asset": "BTCUSDT",
            "action": "BUY",
            "entry_order_id": "entry-1",
            "quantity": 0.25,
            "sl": 90.0,
            "sl_order_id": "stop-1",
        }
        self.fill = {
            "source": "LIVE_EXCHANGE",
            "operation_type": "STOP_CLOSE",
            "operation_id": "stop-client-1",
            "client_order_id": "stop-client-1",
            "related_entry_order_id": "entry-1",
            "classification": "CONFIRMED_FILL",
            "symbol": "BTC/USDT",
            "side": "sell",
            "order_id": "stop-1",
            "requested_qty": 0.25,
            "filled_qty": 0.25,
            "average_fill_price": 89.5,
            "quote_qty": 22.375,
            "trigger_price": 90.0,
            "quantity_unit": "base_asset",
            "exchange_status": "closed",
        }

    def test_only_matching_full_stop_fill_can_close(self):
        result = decide_stop_fill(self.position, self.fill)
        self.assertEqual(result["decision"], "CLOSE_POSITION")
        self.assertTrue(result["closes_position"])

    def test_partial_stop_fill_requires_reconciliation_and_stays_open(self):
        fill = {**self.fill, "classification": "PARTIAL_FILL",
                "requested_qty": 0.25, "filled_qty": 0.1,
                "quote_qty": 8.95, "exchange_status": "canceled"}
        result = decide_stop_fill(self.position, fill)
        self.assertEqual(result["decision"], "PARTIAL_STOP_RECONCILIATION_REQUIRED")
        self.assertFalse(result["closes_position"])

    def test_pending_or_ambiguous_stop_never_closes(self):
        for classification in ("PENDING", "AMBIGUOUS"):
            with self.subTest(classification=classification):
                result = decide_stop_fill(
                    self.position, {**self.fill, "classification": classification}
                )
                self.assertFalse(result["closes_position"])

    def test_mismatched_identity_side_symbol_quantity_trigger_and_price_are_rejected(self):
        variants = [
            {**self.fill, "order_id": "other-stop"},
            {**self.fill, "symbol": "ETH/USDT"},
            {**self.fill, "side": "buy"},
            {**self.fill, "requested_qty": 0.2},
            {**self.fill, "filled_qty": 0},
            {**self.fill, "average_fill_price": 0},
            {**self.fill, "quote_qty": 25.0},
            {**self.fill, "trigger_price": 91.0},
            {**self.fill, "exchange_status": "mystery"},
            {**self.fill, "quantity_unit": None},
        ]
        for fill in variants:
            with self.subTest(fill=fill):
                result = decide_stop_fill(self.position, fill)
                self.assertEqual(result["decision"], "QUARANTINE")
                self.assertFalse(result["closes_position"])

    def test_fill_cannot_mark_position_closed_without_full_quantity(self):
        variants = [
            {**self.fill, "classification": "PARTIAL_FILL", "filled_qty": 0.1,
             "quote_qty": 8.95, "exchange_status": "canceled"},
            {**self.fill, "classification": "CONFIRMED_FILL", "filled_qty": 0.1,
             "quote_qty": 8.95},
            {**self.fill, "classification": "PENDING", "exchange_status": "open"},
        ]
        for fill in variants:
            with self.subTest(fill=fill):
                self.assertFalse(decide_stop_fill(self.position, fill)["closes_position"])


class PartialStopReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.position = {
            "asset": "BTCUSDT", "action": "BUY", "quantity": 0.25,
            "entry_order_id": "entry-1",
            "sl": 90.0, "sl_order_id": "stop-1",
            "reconciliation_required": True,
        }
        self.fill = {
            "source": "LIVE_EXCHANGE", "classification": "PARTIAL_FILL",
            "operation_type": "STOP_CLOSE", "operation_id": "stop-client-1",
            "client_order_id": "stop-client-1",
            "related_entry_order_id": "entry-1",
            "order_id": "stop-1", "symbol": "BTC/USDT", "side": "sell",
            "requested_qty": 0.25, "filled_qty": 0.1,
            "average_fill_price": 89.5, "quote_qty": 8.95,
            "trigger_price": 90.0, "exchange_status": "canceled",
            "quantity_unit": "base_asset",
        }

    def test_valid_partial_fill_records_correct_remaining_quantity(self):
        result = reconcile_stop_fill(self.position, self.fill)
        self.assertEqual(result["decision"], "PARTIAL_STOP_RECONCILIATION_REQUIRED")
        self.assertFalse(result["closes_position"])
        self.assertAlmostEqual(result["remaining_qty"], 0.15)
        self.assertEqual(result["evidence"]["order_id"], "stop-1")
        self.assertEqual(result["evidence"]["cumulative_filled_qty"], 0.1)
        self.assertTrue(result["evidence"]["reconciliation_required"])

    def test_reprocessing_same_snapshot_is_idempotent_with_temporary_record(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            record = Path(directory) / "synthetic-stop.json"
            previous = None
            for _ in range(3):
                result = reconcile_stop_fill(self.position, self.fill, previous)
                record.write_text(json.dumps(result["evidence"]), encoding="utf-8")
                previous = json.loads(record.read_text(encoding="utf-8"))
                self.assertAlmostEqual(previous["remaining_qty"], 0.15)
            self.assertEqual(previous["cumulative_filled_qty"], 0.1)

    def test_multiple_cumulative_updates_do_not_double_subtract(self):
        previous = None
        for filled, remaining in ((0.05, 0.20), (0.12, 0.13), (0.2, 0.05)):
            fill = {**self.fill, "filled_qty": filled,
                    "quote_qty": filled * self.fill["average_fill_price"]}
            result = reconcile_stop_fill(self.position, fill, previous)
            self.assertEqual(result["decision"], "PARTIAL_STOP_RECONCILIATION_REQUIRED")
            self.assertAlmostEqual(result["remaining_qty"], remaining)
            previous = result["evidence"]

    def test_regressing_snapshot_does_not_lower_the_validated_fill_high_watermark(self):
        first = reconcile_stop_fill(self.position, self.fill)
        regressed_fill = {**self.fill, "filled_qty": 0.05, "quote_qty": 4.475}
        regressed = reconcile_stop_fill(self.position, regressed_fill, first["evidence"])
        self.assertEqual(regressed["decision"], "QUARANTINE")
        later_stale = {**self.fill, "filled_qty": 0.08, "quote_qty": 7.16}
        still_regressed = reconcile_stop_fill(
            self.position, later_stale, regressed["evidence"])
        self.assertEqual(still_regressed["decision"], "QUARANTINE")
        self.assertEqual(still_regressed["evidence"]["last_valid_cumulative_filled_qty"], 0.1)

    def test_conflicting_identity_or_side_quarantines_without_remaining_guess(self):
        for fill in ({**self.fill, "order_id": "other"},
                     {**self.fill, "side": "buy"}):
            with self.subTest(fill=fill):
                result = reconcile_stop_fill(self.position, fill)
                self.assertEqual(result["decision"], "QUARANTINE")
                self.assertIsNone(result["remaining_qty"])
                self.assertTrue(result["evidence"]["manual_reconciliation_required"])

    def test_invalid_quantities_prices_and_prior_record_fail_closed(self):
        variants = [
            ({**self.fill, "requested_qty": 0.2}, self.position, None),
            ({**self.fill, "filled_qty": float("nan")}, self.position, None),
            ({**self.fill, "average_fill_price": 0}, self.position, None),
            (self.fill, {**self.position, "quantity": float("nan")}, None),
            (self.fill, self.position, {"order_id": "other", "remaining_qty": 0.1,
                                        "requested_qty": 0.25, "quantity_unit": "base_asset",
                                        "cumulative_filled_qty": 0.1}),
        ]
        for fill, position, prior in variants:
            with self.subTest(fill=fill, position=position, prior=prior):
                result = reconcile_stop_fill(position, fill, prior)
                self.assertEqual(result["decision"], "QUARANTINE")
                self.assertIsNone(result["remaining_qty"])

    def test_pending_snapshot_stays_quarantined_and_blocks_entries(self):
        pending = {**self.fill, "classification": "PENDING", "exchange_status": "open"}
        result = reconcile_stop_fill(self.position, pending)
        self.assertFalse(result["closes_position"])
        self.assertTrue(result["evidence"]["reconciliation_required"])
        self.assertTrue(positions_block_entries([self.position]))
        inconsistent_full_pending = {
            **pending, "filled_qty": self.position["quantity"],
            "quote_qty": self.position["quantity"] * pending["average_fill_price"],
        }
        self.assertEqual(
            reconcile_stop_fill(self.position, inconsistent_full_pending)["decision"],
            "QUARANTINE",
        )

    def test_ambiguous_incomplete_and_unknown_records_fail_closed(self):
        variants = [
            {**self.fill, "classification": "AMBIGUOUS"},
            {**self.fill, "trigger_price": None},
            {**self.fill, "exchange_status": "unknown"},
            None,
        ]
        for fill in variants:
            with self.subTest(fill=fill):
                result = reconcile_stop_fill(self.position, fill)
                self.assertEqual(result["decision"], "QUARANTINE")
                self.assertFalse(result["closes_position"])
                self.assertTrue(positions_block_entries([self.position]))

    def test_full_validated_fill_closes_but_partial_does_not(self):
        full = {**self.fill, "classification": "CONFIRMED_FILL", "filled_qty": 0.25,
                "quote_qty": 22.375, "exchange_status": "closed"}
        self.assertTrue(reconcile_stop_fill(self.position, full)["closes_position"])
        self.assertFalse(reconcile_stop_fill(self.position, self.fill)["closes_position"])

    def test_corrupt_position_snapshot_fails_closed_for_entry_guard(self):
        self.assertTrue(positions_block_entries(None))
        self.assertTrue(positions_block_entries([None]))


class OperationRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.position = {
            "entry_order_id": "entry-1", "asset": "BTCUSDT", "action": "BUY",
            "quantity": 0.25, "entry_price": 100.0,
        }
        self.close = {
            "source": "LIVE_EXCHANGE", "operation_type": "CLOSE",
            "operation_id": "close-client-1", "client_order_id": "close-client-1",
            "related_entry_order_id": "entry-1", "classification": "CONFIRMED_FILL",
            "order_id": "close-1", "symbol": "BTC/USDT", "side": "sell",
            "requested_qty": 0.25, "filled_qty": 0.25,
            "average_fill_price": 110.0, "exchange_status": "closed",
        }

    def test_close_and_stop_close_never_recover_as_entry(self):
        for operation_type in ("CLOSE", "STOP_CLOSE"):
            event = {**self.close, "operation_type": operation_type}
            with self.subTest(operation_type=operation_type):
                self.assertEqual(decide_entry_recovery(event)["decision"], "QUARANTINE")

    def test_missing_unknown_or_conflicting_operation_identity_fails_closed(self):
        variants = [
            {key: value for key, value in self.close.items() if key != "operation_type"},
            {**self.close, "operation_type": "EXIT"},
            {**self.close, "operation_type": "ENTRY"},
            {**self.close, "operation_id": "another-client"},
        ]
        for event in variants:
            with self.subTest(event=event):
                self.assertEqual(decide_entry_recovery(event)["decision"], "QUARANTINE")

    def test_close_requires_matching_related_position_and_preserves_on_mismatch(self):
        mismatches = [
            {**self.close, "related_entry_order_id": "other-entry"},
            {**self.close, "symbol": "ETH/USDT"},
            {**self.close, "side": "buy"},
            {**self.close, "operation_id": "wrong-id"},
        ]
        for event in mismatches:
            with self.subTest(event=event):
                result = decide_close_recovery(self.position, event)
                self.assertEqual(result["decision"], "QUARANTINE")
                self.assertEqual(self.position["quantity"], 0.25)

    def test_partial_close_is_quarantined_and_full_close_requires_complete_evidence(self):
        partial = {**self.close, "classification": "PARTIAL_FILL", "exchange_status": "canceled",
                   "filled_qty": 0.1}
        result = decide_close_recovery(self.position, partial)
        self.assertEqual(result["decision"], "PARTIAL_CLOSE_RECONCILIATION_REQUIRED")
        self.assertFalse(result["closes_position"])
        self.assertAlmostEqual(result["remaining_qty"], 0.15)
        for incomplete in (
            {**self.close, "exchange_status": "open"},
            {**self.close, "filled_qty": 0.1},
            {**self.close, "average_fill_price": None},
        ):
            with self.subTest(incomplete=incomplete):
                self.assertEqual(decide_close_recovery(self.position, incomplete)["decision"], "QUARANTINE")
        self.assertEqual(decide_close_recovery(self.position, self.close)["decision"], "CLOSE_POSITION")

    def test_entry_replay_after_position_persist_is_duplicate_not_second_position(self):
        entry = {
            "source": "LIVE_EXCHANGE", "operation_type": "ENTRY", "operation_id": "entry-client-1",
            "client_order_id": "entry-client-1", "classification": "CONFIRMED_FILL",
            "order_id": "entry-1", "asset": "BTCUSDT", "action": "BUY",
            "filled_qty": 0.25, "average_fill_price": 100.0,
        }
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            state_file = Path(directory) / "synthetic-recovery.json"
            position = {"entry_order_id": "entry-1", "asset": "BTCUSDT", "action": "BUY",
                        "entry_filled_qty": 0.25, "quantity": 0.25, "entry_price": 100.0}
            state_file.write_text(json.dumps({"positions": [position], "journal_state": "FILL_REQUIRES_POSITION_COMMIT"}), encoding="utf-8")
            persisted = json.loads(state_file.read_text(encoding="utf-8"))
            result = decide_entry_recovery(
                entry, existing_order_ids={p["entry_order_id"] for p in persisted["positions"]})
            self.assertEqual(result["decision"], "DUPLICATE")
            self.assertEqual(len(persisted["positions"]), 1)
            self.assertEqual(persisted["journal_state"], "FILL_REQUIRES_POSITION_COMMIT")


class PositionPruningPolicyTests(unittest.TestCase):
    def test_old_quarantined_position_is_never_pruned(self):
        position = {"reconciliation_required": True}
        self.assertFalse(should_prune_position(position, 25 * 3600, 24 * 3600))

    def test_old_reconciled_position_can_be_pruned(self):
        position = {"reconciliation_required": False}
        self.assertTrue(should_prune_position(position, 25 * 3600, 24 * 3600))

    def test_invalid_age_or_threshold_does_not_prune(self):
        position = {"reconciliation_required": False}
        self.assertFalse(should_prune_position(position, float("nan"), 24 * 3600))
        self.assertFalse(should_prune_position(position, 25 * 3600, None))


if __name__ == "__main__":
    unittest.main()

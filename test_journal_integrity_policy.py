"""Isolated tests: imports only pure journal and snapshot helpers."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from journal_policy import (
    index_journal_events,
    normalize_order_status,
    quarantine_close_positions,
    validate_order_evidence,
)
from position_snapshot import write_jsonl_snapshot
from position_snapshot import read_jsonl_snapshot
from recovery_policy import decide_close_recovery, decide_entry_recovery, positions_block_entries


def entry_event(**overrides):
    event = {
        "client_order_id": "client-1", "operation_id": "client-1",
        "operation_type": "ENTRY", "related_entry_order_id": None,
        "state": "FILL_REQUIRES_POSITION_COMMIT",
    }
    return {**event, **overrides}


class JournalIntegrityTests(unittest.TestCase):
    def test_valid_entry_without_conflict_remains_recoverable(self):
        self.assertEqual(index_journal_events([entry_event()]), [entry_event()])

    def test_same_id_entry_then_close_becomes_ambiguous(self):
        events = [entry_event(), entry_event(operation_type="CLOSE", related_entry_order_id="entry-1")]
        result = index_journal_events(events)
        self.assertEqual(result[0]["state"], "AMBIGUOUS")
        self.assertTrue(result[0]["journal_conflict"])

    def test_same_id_conflicting_operation_id_becomes_ambiguous(self):
        result = index_journal_events([entry_event(), entry_event(operation_id="other")])
        self.assertEqual(result[0]["state"], "AMBIGUOUS")

    def test_legacy_record_without_operation_is_unresolved(self):
        result = index_journal_events([{"client_order_id": "legacy", "state": "SUBMITTING"}])
        self.assertEqual(result[0]["state"], "AMBIGUOUS")

    def test_terminal_resolution_inherits_prior_identity(self):
        result = index_journal_events([entry_event(), {
            "client_order_id": "client-1", "state": "RESOLVED_POSITION_RECORDED"}])
        self.assertEqual(result, [])

    def test_terminal_resolution_with_conflicting_operation_stays_ambiguous(self):
        result = index_journal_events([entry_event(), {
            "client_order_id": "client-1", "state": "RESOLVED_POSITION_RECORDED",
            "operation_type": "CLOSE", "operation_id": "client-1",
            "related_entry_order_id": "entry-1"}])
        self.assertEqual(result[0]["state"], "AMBIGUOUS")
        self.assertTrue(result[0]["journal_conflict"])

    def test_wrong_raw_order_symbol_side_amount_status_and_identity_fail(self):
        valid = {"id": "order-1", "symbol": "BTC/USDT", "side": "buy", "amount": 0.25}
        self.assertIsNone(validate_order_evidence(valid, symbol="BTC/USDT", side="buy", requested_qty=0.25))
        variants = [
            {**valid, "symbol": "ETH/USDT"}, {**valid, "side": "sell"},
            {**valid, "amount": 0.2}, {key: value for key, value in valid.items() if key != "id"},
        ]
        for order in variants:
            with self.subTest(order=order):
                self.assertIsNotNone(validate_order_evidence(
                    order, symbol="BTC/USDT", side="buy", requested_qty=0.25))
        self.assertEqual(normalize_order_status("pending"), "pending")
        self.assertIsNone(normalize_order_status("unknown"))

    def test_unresolved_close_quarantines_exact_position_and_ambiguous_relation_blocks(self):
        positions = [{"entry_order_id": "entry-1", "quantity": 0.25},
                     {"entry_order_id": "entry-2", "quantity": 0.25}]
        close = entry_event(operation_type="CLOSE", related_entry_order_id="entry-1")
        result, block = quarantine_close_positions(positions, [close])
        self.assertFalse(block)
        self.assertTrue(result[0]["reconciliation_required"])
        self.assertNotIn("reconciliation_required", result[1])
        _, block = quarantine_close_positions(positions, [
            entry_event(operation_type="CLOSE", related_entry_order_id="missing")])
        self.assertTrue(block)
        _, block = quarantine_close_positions(
            [{"entry_order_id": "entry-1"}, {"entry_order_id": "entry-1"}], [close])
        self.assertTrue(block)


class SnapshotTests(unittest.TestCase):
    def test_atomic_snapshot_replaces_complete_file(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / "positions.jsonl"
            path.write_text('{"old":true}\n', encoding="utf-8")
            self.assertTrue(write_jsonl_snapshot(path, [{"new": True}]))
            self.assertEqual(path.read_text(encoding="utf-8"), '{"new": true}\n')

    def test_snapshot_prepare_failure_preserves_target(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / "positions.jsonl"
            before = '{"old":true}\n'
            path.write_text(before, encoding="utf-8")
            circular = {}
            circular["self"] = circular
            self.assertFalse(write_jsonl_snapshot(path, [circular]))
            self.assertEqual(path.read_text(encoding="utf-8"), before)

    def test_snapshot_replace_failure_preserves_target_and_removes_temp_file(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / "positions.jsonl"
            before = '{"old":true}\n'
            path.write_text(before, encoding="utf-8")
            with patch("position_snapshot.os.replace", side_effect=OSError("file locked")):
                self.assertFalse(write_jsonl_snapshot(path, [{"new": True}]))
            self.assertEqual(path.read_text(encoding="utf-8"), before)
            self.assertEqual(list(path.parent.glob(".positions.jsonl.*.tmp")), [])

    def test_snapshot_sync_failure_preserves_target_and_removes_temp_file(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / "positions.jsonl"
            before = '{"old":true}\n'
            path.write_text(before, encoding="utf-8")
            with patch("position_snapshot.os.fsync", side_effect=OSError("sync failed")):
                self.assertFalse(write_jsonl_snapshot(path, [{"new": True}]))
            self.assertEqual(path.read_text(encoding="utf-8"), before)
            self.assertEqual(list(path.parent.glob(".positions.jsonl.*.tmp")), [])

    def test_malformed_snapshot_fails_closed(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / "positions.jsonl"
            path.write_text('{"mode":"LIVE"\n', encoding="utf-8")
            self.assertIsNone(read_jsonl_snapshot(path, "LIVE"))


class SyntheticRestartLifecycleTests(unittest.TestCase):
    def test_restart_replay_and_partial_close_keep_one_quarantined_position(self):
        entry_fill = {
            "source": "LIVE_EXCHANGE", "operation_type": "ENTRY",
            "operation_id": "client-1", "client_order_id": "client-1",
            "classification": "CONFIRMED_FILL", "order_id": "entry-1",
            "asset": "BTCUSDT", "action": "BUY", "filled_qty": 0.25,
            "average_fill_price": 100.0,
        }
        self.assertEqual(decide_entry_recovery(entry_fill)["decision"], "RECOVER_POSITION")

        position = {
            "mode": "LIVE", "entry_client_order_id": "client-1",
            "entry_order_id": "entry-1", "asset": "BTCUSDT", "action": "BUY",
            "quantity": 0.25, "entry_filled_qty": 0.25, "entry_price": 100.0,
        }
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            snapshot = Path(directory) / "positions.jsonl"
            self.assertTrue(write_jsonl_snapshot(snapshot, [position]))
            persisted = read_jsonl_snapshot(snapshot, "LIVE")

        # Simulates restart after position persistence but before journal completion.
        self.assertEqual(index_journal_events([entry_event()]), [entry_event()])
        replay = decide_entry_recovery(entry_fill, existing_order_ids={"entry-1"})
        self.assertEqual(replay["decision"], "DUPLICATE")
        self.assertEqual(len(persisted), 1)

        close_journal = entry_event(
            client_order_id="close-client-1", operation_id="close-client-1",
            operation_type="CLOSE", related_entry_order_id="entry-1",
            state="CLOSE_FILL_REQUIRES_POSITION_COMMIT",
        )
        quarantined, global_block = quarantine_close_positions(persisted, [close_journal])
        self.assertFalse(global_block)
        self.assertTrue(quarantined[0]["reconciliation_required"])
        self.assertTrue(positions_block_entries(quarantined))

        partial_close = {
            "source": "LIVE_EXCHANGE", "operation_type": "CLOSE",
            "operation_id": "close-client-1", "client_order_id": "close-client-1",
            "related_entry_order_id": "entry-1", "classification": "PARTIAL_FILL",
            "order_id": "close-1", "symbol": "BTC/USDT", "side": "sell",
            "requested_qty": 0.25, "filled_qty": 0.1,
            "average_fill_price": 99.0, "exchange_status": "canceled",
        }
        result = decide_close_recovery(quarantined[0], partial_close)
        self.assertEqual(result["decision"], "PARTIAL_CLOSE_RECONCILIATION_REQUIRED")
        self.assertFalse(result["closes_position"])
        self.assertAlmostEqual(result["remaining_qty"], 0.15)


if __name__ == "__main__":
    unittest.main()

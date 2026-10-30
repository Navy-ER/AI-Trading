"""Tests for execution.py — Phase 3 execution handler."""
import unittest
from unittest.mock import patch, MagicMock
import sys, os, json, tempfile
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ["TOKOCRYPTO_API_KEY"] = "test_key"
os.environ["TOKOCRYPTO_SECRET_KEY"] = "test_secret"
os.environ["TKO_DRY_RUN"] = "true"

import execution


class TestExecuteTrade(unittest.TestCase):
    def setUp(self):
        self._trading_mode_patch = patch("execution.trading_mode", return_value="LIVE")
        self._trading_mode_patch.start()
        self.journal_temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.journal_temp.cleanup)
        self._journal_patch = patch.object(execution, "ORDER_JOURNAL", Path(self.journal_temp.name) / "orders.jsonl")
        self._journal_patch.start()
        self.addCleanup(self._journal_patch.stop)

    def tearDown(self):
        self._trading_mode_patch.stop()

    @patch("execution.get_exchange")
    def test_invalid_action_returns_none(self, mock_ex):
        mock_ex.return_value = MagicMock()
        result = execution.execute_trade("HOLD", "BTCUSDT", 1.2, 50000)
        self.assertIsNone(result)

    @patch("execution.get_exchange")
    def test_below_min_order_skip(self, mock_ex):
        mock_ex.return_value = MagicMock()
        result = execution.execute_trade("BUY", "BTCUSDT", 0.5, 50000)
        self.assertIsNone(result)

    @patch("execution.get_exchange")
    def test_above_max_order_clamps(self, mock_ex):
        ex = MagicMock()
        mock_ex.return_value = ex
        ex.fetch_ticker.return_value = {"last": 50000}
        ex.create_order.return_value = {"id": "12345", "status": "closed", "amount": 0.0001,
                                        "filled": 0.0001, "average": 50000.0, "cost": 5.0}
        with patch("execution.parse_quantity_precision", return_value=0.0001):
            result = execution.execute_trade("BUY", "BTCUSDT", 20.0, 50000)
        self.assertIsNotNone(result)
        self.assertEqual(result["order_id"], "12345")

    @patch("execution.get_exchange")
    def test_quantity_is_notional_divided_by_reference_price(self, mock_ex):
        ex = MagicMock()
        mock_ex.return_value = ex
        ex.fetch_ticker.return_value = {"last": 110.0}
        ex.create_order.return_value = {"id": "fake-order", "status": "closed", "amount": 0.1,
                                        "filled": 0.1, "average": 110.0, "cost": 11.0}

        with patch("execution.TKO_MIN_ORDER_USDT", 1.0), \
             patch("execution.TKO_MAX_POSITION_USDT", 20.0), \
             patch("execution.parse_quantity_precision", side_effect=lambda symbol, amount: amount):
            execution.execute_trade("SELL", "BTCUSDT", 11.0, 110.0)

        called = ex.create_order.call_args.kwargs
        self.assertTrue(called["params"]["clientOrderId"])
        ex.create_order.assert_called_once_with(
            symbol="BTC/USDT", type="market", side="sell", amount=0.1,
            params=called["params"],
        )

    @patch("execution.get_exchange")
    def test_slippage_skip(self, mock_ex):
        ex = MagicMock()
        mock_ex.return_value = ex
        ex.fetch_ticker.return_value = {"last": 52500}
        with patch("execution.parse_quantity_precision", return_value=0.000024):
            result = execution.execute_trade("BUY", "BTCUSDT", 1.2, 50000)
        self.assertIsNone(result)

    @patch("execution.get_exchange")
    def test_successful_buy_order(self, mock_ex):
        ex = MagicMock()
        mock_ex.return_value = ex
        ex.fetch_ticker.return_value = {"last": 50000.0}
        ex.create_order.return_value = {"id": "ORDER_001", "status": "closed", "amount": 0.000024,
                                        "filled": 0.000024, "average": 50000.0, "cost": 1.2}
        with patch("execution.parse_quantity_precision", return_value=0.000024):
            result = execution.execute_trade("BUY", "BTCUSDT", 1.2, 50000.0)
        self.assertIsNotNone(result)

    @patch("execution.get_exchange")
    def test_pending_market_order_is_cancelled_then_fetched_before_classification(self, mock_ex):
        ex = MagicMock()
        mock_ex.return_value = ex
        ex.fetch_ticker.return_value = {"last": 50000.0}
        ex.create_order.return_value = {"id": "ORDER_PENDING", "status": "open", "amount": 0.000024,
                                        "filled": 0.00001, "average": 50000.0}
        ex.fetch_order.return_value = {"id": "ORDER_PENDING", "status": "canceled", "amount": 0.000024,
                                       "filled": 0.00001, "average": 50000.0}
        with patch("execution.parse_quantity_precision", return_value=0.000024):
            result = execution.execute_trade("BUY", "BTCUSDT", 1.2, 50000.0)
        self.assertEqual(result["classification"], "PARTIAL_FILL")
        ex.cancel_order.assert_called_once_with("ORDER_PENDING", "BTC/USDT")
        ex.fetch_order.assert_called_once_with("ORDER_PENDING", "BTC/USDT")
        self.assertEqual(result["order_id"], "ORDER_PENDING")
        ex.create_order.assert_called_once()


class TestNormalizeOrderResult(unittest.TestCase):
    def setUp(self):
        self._trading_mode_patch = patch("execution.trading_mode", return_value="LIVE")
        self._trading_mode_patch.start()
        self.journal_temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.journal_temp.cleanup)
        self._journal_patch = patch.object(execution, "ORDER_JOURNAL", Path(self.journal_temp.name) / "orders.jsonl")
        self._journal_patch.start()
        self.addCleanup(self._journal_patch.stop)

    def tearDown(self):
        self._trading_mode_patch.stop()

    def normalize(self, response):
        return execution.normalize_order_result(
            response, symbol="BTC/USDT", side="buy", requested_qty=1.0,
        ).as_dict()

    def test_full_fill_requires_terminal_status_quantity_and_price(self):
        result = self.normalize({"id": "o1", "status": "closed", "amount": 1,
                                 "filled": 1, "average": 100})
        self.assertEqual(result["classification"], "CONFIRMED_FILL")

    def test_partial_fill_is_preserved(self):
        result = self.normalize({"id": "o2", "status": "canceled", "amount": 1,
                                 "filled": 0.4, "average": 101})
        self.assertEqual(result["classification"], "PARTIAL_FILL")
        self.assertEqual(result["filled_qty"], 0.4)

    def test_closed_order_with_partial_quantity_is_ambiguous(self):
        result = self.normalize({"id": "o2b", "status": "closed", "amount": 1,
                                 "filled": 0.4, "average": 101})
        self.assertEqual(result["classification"], "AMBIGUOUS")

    def test_zero_fill_terminal_cancel_is_no_fill(self):
        result = self.normalize({"id": "o3", "status": "canceled", "amount": 1, "filled": 0})
        self.assertEqual(result["classification"], "NO_FILL_CONFIRMED")

    def test_open_order_is_pending_and_carries_partial_fill(self):
        result = self.normalize({"id": "o4", "status": "open", "amount": 1,
                                 "filled": 0.2, "average": 102})
        self.assertEqual(result["classification"], "PENDING")
        self.assertEqual(result["filled_qty"], 0.2)

    def test_unknown_status_empty_response_and_missing_price_are_ambiguous(self):
        self.assertEqual(self.normalize({"id": "o5", "status": "mystery", "filled": 1,
                                         "average": 100})["classification"], "AMBIGUOUS")
        self.assertEqual(self.normalize(None)["classification"], "AMBIGUOUS")
        self.assertEqual(self.normalize({"id": "o6", "status": "closed", "filled": 1})["classification"], "AMBIGUOUS")

    def test_simulator_source_cannot_claim_exchange_fill(self):
        result = execution.normalize_order_result(
            {"id": "sim", "status": "closed", "filled": 1, "average": 100},
            symbol="BTC/USDT", side="buy", requested_qty=1, source="DRY_RUN_SIMULATOR",
        )
        self.assertEqual(result.classification, "AMBIGUOUS")

    @patch("execution.get_exchange")
    def test_successful_sell_order(self, mock_ex):
        ex = MagicMock()
        mock_ex.return_value = ex
        ex.fetch_ticker.return_value = {"last": 50000.0}
        ex.create_order.return_value = {"id": "ORDER_002", "status": "closed", "amount": 0.000024,
                                        "filled": 0.000024, "average": 50000.0, "cost": 1.2}
        with patch("execution.parse_quantity_precision", return_value=0.000024):
            result = execution.execute_trade("SELL", "ETHUSDT", 1.2, 50000.0)
        self.assertIsNotNone(result)

    @patch("execution.get_exchange")
    def test_exception_after_submission_is_ambiguous(self, mock_ex):
        import ccxt
        ex = MagicMock()
        mock_ex.return_value = ex
        ex.fetch_ticker.return_value = {"last": 50000.0}
        ex.create_order.side_effect = ccxt.InsufficientFunds("not enough")
        with patch("execution.parse_quantity_precision", return_value=0.000024):
            result = execution.execute_trade("BUY", "BTCUSDT", 1.2, 50000.0)
        self.assertEqual(result["classification"], "AMBIGUOUS")
        self.assertTrue(result["client_order_id"])
        self.assertTrue(execution.reconciliation_blocks_entries())
        self.assertEqual(ex.create_order.call_args.kwargs["params"]["clientOrderId"], result["client_order_id"])


class TestOrderRecovery(unittest.TestCase):
    def setUp(self):
        self.journal_temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.journal_temp.cleanup)
        self._journal_patch = patch.object(execution, "ORDER_JOURNAL", Path(self.journal_temp.name) / "orders.jsonl")
        self._journal_patch.start()
        self.addCleanup(self._journal_patch.stop)

    def test_exact_client_id_match_recovers_order_without_retry(self):
        execution._journal_order({
            "client_order_id": "unique-1", "state": "UNRESOLVED", "symbol": "BTC/USDT",
            "side": "buy", "requested_qty": 1.0, "created_at_ms": 1000,
        })
        exchange = MagicMock()
        exchange.fetch_orders.return_value = [{
            "id": "o1", "clientOrderId": "unique-1", "symbol": "BTC/USDT", "side": "buy",
            "status": "closed", "amount": 1.0, "filled": 1.0, "average": 10.0,
        }]
        stats = execution.reconcile_unresolved_order_intents(exchange)
        self.assertEqual(stats["fills_needing_position"], 1)
        self.assertTrue(execution.reconciliation_blocks_entries())
        exchange.create_order.assert_not_called()

    def test_no_match_or_duplicate_client_id_stays_locked(self):
        execution._journal_order({
            "client_order_id": "unique-2", "state": "UNRESOLVED", "symbol": "BTC/USDT",
            "side": "buy", "requested_qty": 1.0, "created_at_ms": 1000,
        })
        exchange = MagicMock()
        exchange.fetch_orders.return_value = []
        execution.reconcile_unresolved_order_intents(exchange)
        self.assertTrue(execution.reconciliation_blocks_entries())
        exchange.fetch_orders.return_value = [
            {"clientOrderId": "unique-2"}, {"clientOrderId": "unique-2"},
        ]
        execution.reconcile_unresolved_order_intents(exchange)
        self.assertTrue(execution.reconciliation_blocks_entries())

    def test_live_position_requires_open_unfilled_stop_of_exact_remaining_size(self):
        exchange = MagicMock()
        exchange.fetch_order.side_effect = [
            {"status": "closed", "filled": 0.4},
            {"status": "open", "filled": 0, "amount": 0.4, "symbol": "BTC/USDT",
             "side": "sell", "triggerPrice": 90.0},
        ]
        valid, reason = execution.validate_live_position_orders(exchange, {
            "asset": "BTCUSDT", "action": "BUY", "sl": 90.0,
            "entry_order_id": "e1", "entry_filled_qty": 0.4, "sl_order_id": "s1", "quantity": 0.4,
        })
        self.assertTrue(valid)
        self.assertIsNone(reason)
        exchange.fetch_order.side_effect = [
            {"status": "closed", "filled": 0.4},
            {"status": "closed", "filled": 0.4, "amount": 0.4, "symbol": "BTC/USDT",
             "side": "sell", "triggerPrice": 90.0},
        ]
        valid, reason = execution.validate_live_position_orders(exchange, {
            "asset": "BTCUSDT", "action": "BUY", "sl": 90.0,
            "entry_order_id": "e1", "entry_filled_qty": 0.4, "sl_order_id": "s1", "quantity": 0.4,
        })
        self.assertFalse(valid)
        self.assertIn("stop-loss is not open", reason)


class TestTradingModeGuard(unittest.TestCase):
    def test_mode_parser_has_one_explicit_policy(self):
        for value in ("true", "1", "yes"):
            with self.subTest(value=value):
                self.assertEqual(execution.parse_trading_mode(value), "DRY_RUN")

        for value in ("false", "0", "no"):
            with self.subTest(value=value):
                self.assertEqual(execution.parse_trading_mode(value), "LIVE")

        for value in (None, "", "typo"):
            with self.subTest(value="missing" if value is None else "invalid"):
                self.assertEqual(execution.parse_trading_mode(value), "REJECTED")

    def test_dry_run_blocks_all_order_operations_before_exchange_access(self):
        exchange = MagicMock()
        with patch.dict(os.environ, {"TKO_DRY_RUN": "true"}), \
             patch("execution.get_exchange") as get_exchange:
            self.assertIsNone(execution.execute_trade("BUY", "BTCUSDT", 1.2, 50000))
            self.assertIsNone(execution.place_stop_loss(
                exchange, "BTC/USDT", "BUY", 0.001, 49000
            ))
            self.assertFalse(execution.cancel_exchange_order(
                exchange, "BTC/USDT", "order-id"
            ))

        get_exchange.assert_not_called()
        exchange.create_order.assert_not_called()
        exchange.cancel_order.assert_not_called()

    def test_missing_or_invalid_mode_blocks_exchange_factory(self):
        for mode in (None, "", "typo"):
            with self.subTest(mode="missing" if mode is None else "invalid"):
                with patch("execution.trading_mode", return_value="REJECTED"), \
                     patch("execution._exchange", None), \
                     patch("execution.ccxt.tokocrypto") as exchange_factory:
                    self.assertIsNone(execution.get_exchange())

                exchange_factory.assert_not_called()


class TestTokocryptoOrderFormatting(unittest.TestCase):
    def setUp(self):
        self._trading_mode_patch = patch("execution.trading_mode", return_value="LIVE")
        self._trading_mode_patch.start()

    def tearDown(self):
        self._trading_mode_patch.stop()

    def test_amount_precision_uses_ccxt_formatter(self):
        exchange = MagicMock()
        exchange.amount_to_precision.return_value = "0.0012"
        with patch("execution.get_exchange", return_value=exchange):
            self.assertEqual(execution.parse_quantity_precision("BTC/USDT", 0.001234), 0.0012)
        exchange.amount_to_precision.assert_called_once_with("BTC/USDT", 0.001234)

    def test_stop_order_uses_ccxt_trigger_price_parameter(self):
        exchange = MagicMock()
        exchange.create_order.return_value = {"id": "sl-123", "status": "open"}
        with patch("execution.parse_price_precision", return_value=98.0), \
             patch("execution.parse_quantity_precision", return_value=0.01):
            order_id = execution.place_stop_loss(exchange, "BTC/USDT", "BUY", 0.01, 98.5)
        self.assertEqual(order_id, "sl-123")
        exchange.create_order.assert_called_once_with(
            symbol="BTC/USDT", type="limit", side="sell", amount=0.01,
            price=98.0, params={"triggerPrice": 98.5},
        )

    def test_cancel_requires_terminal_cancel_without_fills(self):
        exchange = MagicMock()
        exchange.cancel_order.return_value = {"id": "sl-123", "status": "canceled", "filled": 0}
        self.assertTrue(execution.cancel_exchange_order(exchange, "BTC/USDT", "sl-123"))
        exchange.cancel_order.return_value = {"id": "sl-123", "status": "closed", "filled": 0.01}
        self.assertFalse(execution.cancel_exchange_order(exchange, "BTC/USDT", "sl-123"))
        exchange.cancel_order.return_value = {"id": "sl-123", "status": "canceled"}
        self.assertFalse(execution.cancel_exchange_order(exchange, "BTC/USDT", "sl-123"))


class IsolatedBridgePositionFixture(unittest.TestCase):
    def setUp(self):
        import bridge

        self.position_temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.position_temp_dir.cleanup)
        root = self.position_temp_dir.name
        stores = {
            "DRY_RUN": os.path.join(root, "open_positions.dry_run.jsonl"),
            "LIVE": os.path.join(root, "open_positions.live.jsonl"),
        }
        fixtures = (
            patch.object(bridge, "OPEN_POS_LOGS", stores),
            patch.object(bridge, "LEGACY_POSITION_LOGS", []),
            patch.object(bridge, "log", lambda *a, **k: None),
        )
        for fixture in fixtures:
            fixture.start()
            self.addCleanup(fixture.stop)
        self.assertTrue(all(os.path.commonpath([root, path]) == root for path in stores.values()))


class TestBridgePositionRecovery(IsolatedBridgePositionFixture):
    def test_reconciled_fill_is_committed_to_schema_v2_only_after_stop_validation(self):
        import bridge

        with patch("execution.trading_mode", return_value="LIVE"), \
             patch.object(bridge.tko_exec, "fills_requiring_position_commit", return_value=[{
                 "client_order_id": "client-1", "state": "FILL_REQUIRES_POSITION_COMMIT",
                 "source": "LIVE_EXCHANGE", "classification": "CONFIRMED_FILL",
                 "asset": "BTCUSDT", "action": "BUY", "symbol": "BTC/USDT",
                 "order_id": "entry-1", "filled_qty": 0.1, "average_fill_price": 100.0,
             }]), \
             patch.object(bridge.tko_exec, "compute_tp_sl", return_value=(110.0, 90.0)), \
             patch.object(bridge.tko_exec, "_journal_order", return_value=True) as journal, \
             patch.object(bridge.tko_exec, "place_stop_loss", return_value="sl-1"), \
             patch.object(bridge.tko_exec, "validate_live_position_orders", return_value=(True, None)), \
             patch.object(bridge.tko_exec, "mark_order_position_recorded", return_value=True) as mark:
            stats = bridge.recover_reconciled_positions(MagicMock())
            positions = bridge.read_position_records("LIVE")

        self.assertEqual(stats, {"recovered": 1, "quarantined": 0})
        self.assertEqual(positions[0]["schema_version"], 2)
        self.assertEqual(positions[0]["fill_source"], "LIVE_EXCHANGE")
        self.assertEqual(positions[0]["entry_client_order_id"], "client-1")
        self.assertFalse(positions[0]["reconciliation_required"])
        self.assertEqual(journal.call_args.args[0]["state"], "POSITION_RECOVERY_STOP_SUBMITTING")
        mark.assert_called_once_with("client-1")

    def test_uncertain_stop_submission_persists_quarantined_position(self):
        import bridge

        with patch("execution.trading_mode", return_value="LIVE"), \
             patch.object(bridge.tko_exec, "fills_requiring_position_commit", return_value=[{
                 "client_order_id": "client-2", "state": "FILL_REQUIRES_POSITION_COMMIT",
                 "source": "LIVE_EXCHANGE", "classification": "CONFIRMED_FILL",
                 "asset": "BTCUSDT", "action": "BUY", "symbol": "BTC/USDT",
                 "order_id": "entry-2", "filled_qty": 0.1, "average_fill_price": 100.0,
             }]), \
             patch.object(bridge.tko_exec, "compute_tp_sl", return_value=(110.0, 90.0)), \
             patch.object(bridge.tko_exec, "_journal_order", return_value=True), \
             patch.object(bridge.tko_exec, "place_stop_loss", return_value=None), \
             patch.object(bridge.tko_exec, "mark_order_position_recorded") as mark:
            stats = bridge.recover_reconciled_positions(MagicMock())
            positions = bridge.read_position_records("LIVE")

        self.assertEqual(stats, {"recovered": 0, "quarantined": 1})
        self.assertTrue(positions[0]["reconciliation_required"])
        self.assertIn("uncertain", positions[0]["reconciliation_reason"])
        mark.assert_not_called()

    def test_triggered_stop_closes_position_only_after_confirmed_full_fill(self):
        import bridge

        position = {
            "schema_version": 2, "asset": "BTCUSDT", "action": "BUY",
            "entry_price": 100.0, "quantity": 0.1, "sl": 90.0,
            "entry_order_id": "entry-3", "sl_order_id": "stop-3", "mode": "LIVE",
            "reconciliation_required": True,
            "reconciliation_reason": "stop-loss trigger observed; exchange fill not yet confirmed",
        }
        bridge.append_position_record("LIVE", position)
        result = MagicMock(classification="CONFIRMED_FILL")
        result.as_dict.return_value = {
            "source": "LIVE_EXCHANGE", "classification": "CONFIRMED_FILL",
            "symbol": "BTC/USDT", "side": "sell", "requested_qty": 0.1,
            "filled_qty": 0.1, "average_fill_price": 89.9, "order_id": "stop-3",
        }
        with patch("execution.trading_mode", return_value="LIVE"), \
             patch.object(bridge.tko_exec, "stop_fills_requiring_position_commit", return_value=[]), \
             patch.object(bridge.tko_exec, "reconcile_triggered_stop_fill", return_value=result), \
             patch.object(bridge.tko_exec, "_journal_order", return_value=True), \
             patch.object(bridge.tko_exec, "mark_order_position_recorded", return_value=True) as mark, \
             patch.object(bridge, "append_jsonl"), \
             patch.object(bridge, "record_realised_loss"):
            stats = bridge.recover_triggered_stop_losses(MagicMock())
            positions = bridge.read_position_records("LIVE")

        self.assertEqual(stats["closed"], 1)
        self.assertEqual(positions, [])
        mark.assert_called_once_with("stop-loss:BTC/USDT:stop-3")


class TestBridgeTradingMode(IsolatedBridgePositionFixture):
    def test_bridge_uses_shared_dry_run_mode(self):
        import bridge

        signal = {"action": "BUY", "asset": "BTCUSDT", "confidence": "90%"}
        with patch.dict(os.environ, {"TKO_DRY_RUN": "true"}), \
             patch.object(bridge, "LEGACY_POSITION_LOGS", []), \
             patch.object(bridge, "TKO_CONFIDENCE_MIN", 80), \
             patch.object(bridge, "append_jsonl") as append_jsonl, \
             patch.object(bridge.tko_exec, "estimate_position_size", return_value=1.2), \
             patch.object(bridge.tko_exec, "get_exchange") as get_exchange:
            result = bridge.execute_tko_trade(signal, 50000)

        self.assertEqual(result["action"], "BUY")
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["source"], "DRY_RUN_SIMULATOR")
        self.assertEqual(result["classification"], "SIMULATED_FILL")
        append_jsonl.assert_called_once()
        get_exchange.assert_not_called()

    def test_bridge_routes_explicit_live_mode_through_execution_layer(self):
        import bridge

        signal = {"action": "BUY", "asset": "BTCUSDT", "confidence": "90%"}
        fake_order = {"source": "LIVE_EXCHANGE", "classification": "CONFIRMED_FILL",
                      "order_id": "fake-order", "requested_qty": 0.000024,
                      "filled_qty": 0.000024, "average_fill_price": 50000.0,
                      "quote_qty": 1.2}
        with patch("execution.trading_mode", return_value="LIVE"), \
             patch.object(bridge, "LEGACY_POSITION_LOGS", []), \
             patch.object(bridge, "TKO_CONFIDENCE_MIN", 80), \
             patch.object(bridge, "get_atr", return_value=None), \
             patch.object(bridge, "append_jsonl"), \
             patch.object(bridge.tko_exec, "estimate_position_size", return_value=1.2), \
             patch.object(bridge.tko_exec, "reconciliation_blocks_entries", return_value=False), \
             patch.object(bridge.tko_exec, "execute_trade", return_value=fake_order) as execute_trade:
            result = bridge.execute_tko_trade(signal, 50000)

        execute_trade.assert_called_once_with("BUY", "BTCUSDT", 1.2, 50000)
        self.assertEqual(result, fake_order)

    def test_rejected_mode_blocks_bridge_trade_and_position_paths(self):
        import bridge

        signal = {"action": "BUY", "asset": "BTCUSDT", "confidence": "90%"}
        with patch("execution.trading_mode", return_value="REJECTED"), \
             patch.object(bridge, "append_jsonl") as append_jsonl, \
             patch.object(bridge, "read_jsonl") as read_jsonl, \
             patch.object(bridge, "get_atr") as get_atr, \
             patch.object(bridge.tko_exec, "execute_trade") as execute_trade, \
             patch.object(bridge.tko_exec, "get_exchange") as get_exchange:
            self.assertIsNone(bridge.execute_tko_trade(signal, 50000))
            self.assertIsNone(bridge.add_position(signal, 50000))
            self.assertEqual(bridge.check_open_positions({}), (0, 0.0))

        append_jsonl.assert_not_called()
        read_jsonl.assert_not_called()
        get_atr.assert_not_called()
        execute_trade.assert_not_called()
        get_exchange.assert_not_called()


class TestBridgeCloseContract(IsolatedBridgePositionFixture):
    def test_close_passes_usdt_notional_and_exit_price(self):
        import bridge

        position = {
            "asset": "BTCUSDT",
            "action": "BUY",
            "entry_price": 100.0,
            "tp": 110.0,
            "sl": 90.0,
            "size_usdt": 11.0,
            "quantity": 0.1,
            "mode": "LIVE",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            live_path = os.path.join(temp_dir, "live.jsonl")
            with open(live_path, "w", encoding="utf-8") as f:
                f.write(json.dumps(position) + "\n")
            with patch("execution.trading_mode", return_value="LIVE"), \
                 patch.object(bridge, "OPEN_POS_LOGS", {"LIVE": live_path, "DRY_RUN": os.path.join(temp_dir, "dry.jsonl")}), \
                 patch.object(bridge, "LEGACY_POSITION_LOGS", []), \
                 patch.object(bridge, "TRAIL_ENABLED", False), \
                 patch.object(bridge, "append_jsonl"), \
                 patch.object(bridge, "record_realised_loss"), \
                 patch.object(bridge, "send_discord_webhook"), \
                 patch.object(bridge.tko_exec, "get_exchange", return_value=MagicMock()), \
                 patch.object(bridge.tko_exec, "cancel_exchange_order"), \
                 patch.object(bridge.tko_exec, "execute_trade", return_value={
                     "classification": "CONFIRMED_FILL", "filled_qty": 0.1,
                     "average_fill_price": 110.0, "order_id": "fake-order",
                 }) as execute_trade:
                bridge.check_open_positions({"BTC": 110.0})

        execute_trade.assert_called_once_with("SELL", "BTCUSDT", 11.0, 110.0, base_qty=0.1)

    def test_partial_close_replaces_stop_for_remaining_quantity(self):
        import bridge
        with tempfile.TemporaryDirectory(dir=".") as temp_dir:
            live_path = os.path.join(temp_dir, "live.jsonl")
            dry_path = os.path.join(temp_dir, "dry.jsonl")
            position = {
                "asset": "BTCUSDT", "action": "BUY", "entry_price": 100.0,
                "tp": 110.0, "sl": 90.0, "trail_dist": 1.0, "size_usdt": 10.0,
                "quantity": 0.1, "entry_order_id": "entry-1", "sl_order_id": "sl-old",
                "mode": "LIVE",
            }
            with open(live_path, "w", encoding="utf-8") as stream:
                stream.write(json.dumps(position) + "\n")
            with patch("execution.trading_mode", return_value="LIVE"), \
                 patch.object(bridge, "OPEN_POS_LOGS", {"LIVE": live_path, "DRY_RUN": dry_path}), \
                 patch.object(bridge, "LEGACY_POSITION_LOGS", []), \
                 patch.object(bridge, "TRAIL_ENABLED", False), \
                 patch.object(bridge, "append_jsonl"), \
                 patch.object(bridge, "record_realised_loss"), \
                 patch.object(bridge.tko_exec, "get_exchange", return_value=MagicMock()), \
                 patch.object(bridge.tko_exec, "cancel_exchange_order", return_value=True), \
                 patch.object(bridge.tko_exec, "execute_trade", return_value={
                     "classification": "PARTIAL_FILL", "filled_qty": 0.04,
                     "average_fill_price": 110.0, "order_id": "exit-1", "client_order_id": "exit-client",
                 }), \
                 patch.object(bridge.tko_exec, "place_stop_loss", return_value="sl-new") as place_sl, \
                 patch.object(bridge.tko_exec, "mark_order_position_recorded"):
                remaining_count, _ = bridge.check_open_positions({"BTC": 110.0})
            self.assertEqual(remaining_count, 1)
            place_sl.assert_called_once()
            self.assertEqual(place_sl.call_args.args[1:3], ("BTC/USDT", "BUY"))
            self.assertAlmostEqual(place_sl.call_args.args[3], 0.06)
            self.assertEqual(place_sl.call_args.args[4], 90.0)
            with patch.object(bridge, "OPEN_POS_LOGS", {"LIVE": live_path, "DRY_RUN": dry_path}), \
                 patch.object(bridge, "LEGACY_POSITION_LOGS", []):
                remaining = bridge.read_position_records("LIVE")[0]
            self.assertAlmostEqual(remaining["quantity"], 0.06)
            self.assertEqual(remaining["sl_order_id"], "sl-new")
            self.assertFalse(remaining["reconciliation_required"])



class TestBridgePositionIsolation(IsolatedBridgePositionFixture):
    def test_modes_use_separate_stores_and_tag_new_records(self):
        import bridge

        with tempfile.TemporaryDirectory() as temp_dir:
            dry_path = os.path.join(temp_dir, "dry.jsonl")
            live_path = os.path.join(temp_dir, "live.jsonl")
            stores = {"DRY_RUN": dry_path, "LIVE": live_path}
            with patch.object(bridge, "OPEN_POS_LOGS", stores), \
                 patch.object(bridge, "LEGACY_POSITION_LOGS", []):
                with patch.dict(os.environ, {"TKO_DRY_RUN": "true"}):
                    self.assertEqual(bridge.position_log_path(), dry_path)
                    self.assertTrue(bridge.append_position_record("DRY_RUN", {"asset": "DRYUSDT"}))
                with patch("execution.trading_mode", return_value="LIVE"):
                    self.assertEqual(bridge.position_log_path(), live_path)
                    self.assertTrue(bridge.append_position_record("LIVE", {"asset": "LIVEUSDT"}))

            with open(dry_path, encoding="utf-8") as f:
                dry_record = json.loads(f.readline())
            with open(live_path, encoding="utf-8") as f:
                live_record = json.loads(f.readline())
            self.assertEqual(dry_record["mode"], "DRY_RUN")
            self.assertEqual(live_record["mode"], "LIVE")
            with patch.object(bridge, "OPEN_POS_LOGS", stores), \
                 patch.object(bridge, "LEGACY_POSITION_LOGS", []):
                with patch.dict(os.environ, {"TKO_DRY_RUN": "true"}):
                    self.assertEqual(bridge.read_position_records(), [dry_record])
                with patch("execution.trading_mode", return_value="LIVE"):
                    self.assertEqual(bridge.read_position_records(), [live_record])

    def test_rejected_mode_does_not_access_position_files(self):
        import bridge

        with patch("execution.trading_mode", return_value="REJECTED"), \
             patch("builtins.open") as open_file, \
             patch.object(bridge, "read_jsonl") as read_jsonl, \
             patch.object(bridge, "append_jsonl") as append_jsonl:
            self.assertIsNone(bridge.position_log_path())
            self.assertIsNone(bridge.read_position_records())
            self.assertEqual(bridge.check_open_positions({}), (0, 0.0))
            self.assertIsNone(bridge.add_position({"action": "BUY", "asset": "BTCUSDT"}, 100.0))

        open_file.assert_not_called()
        read_jsonl.assert_not_called()
        append_jsonl.assert_not_called()

    def test_untagged_legacy_records_are_quarantined_without_rewrite(self):
        import bridge

        with tempfile.TemporaryDirectory() as temp_dir:
            dry_path = os.path.join(temp_dir, "dry.jsonl")
            live_path = os.path.join(temp_dir, "live.jsonl")
            with open(dry_path, "w", encoding="utf-8") as f:
                f.write('{"asset":"LEGACYUSDT"}\n')
            with open(dry_path, "rb") as f:
                before = f.read()

            with patch.object(bridge, "OPEN_POS_LOGS", {"DRY_RUN": dry_path, "LIVE": live_path}), \
                 patch.object(bridge, "LEGACY_POSITION_LOGS", []), \
                 patch.dict(os.environ, {"TKO_DRY_RUN": "true"}):
                self.assertIsNone(bridge.read_position_records())
                self.assertTrue(bridge.has_open_position("LEGACYUSDT"))
                self.assertFalse(bridge.append_position_record("DRY_RUN", {"asset": "NEWUSDT"}))
                with patch.object(bridge, "TKO_CONFIDENCE_MIN", 80), \
                     patch.object(bridge.tko_exec, "execute_trade") as execute_trade, \
                     patch.object(bridge, "append_jsonl") as append_jsonl:
                    self.assertIsNone(bridge.execute_tko_trade(
                        {"action": "BUY", "asset": "NEWUSDT", "confidence": "99%"}, 100.0
                    ))
                    execute_trade.assert_not_called()
                    append_jsonl.assert_not_called()

            with open(dry_path, "rb") as f:
                self.assertEqual(f.read(), before)
            self.assertFalse(os.path.exists(live_path))

    def test_legacy_file_is_not_used_as_a_fallback(self):
        import bridge

        with tempfile.TemporaryDirectory() as temp_dir:
            legacy_path = os.path.join(temp_dir, "open_positions.jsonl")
            live_path = os.path.join(temp_dir, "open_positions.live.jsonl")
            dry_path = os.path.join(temp_dir, "open_positions.dry_run.jsonl")
            with open(legacy_path, "w", encoding="utf-8") as f:
                f.write('{"asset":"LEGACYUSDT"}\n')
            with open(legacy_path, "rb") as f:
                before = f.read()

            with patch.object(bridge, "OPEN_POS_LOGS", {"DRY_RUN": dry_path, "LIVE": live_path}), \
                 patch.object(bridge, "LEGACY_POSITION_LOGS", [legacy_path]), \
                 patch.dict(os.environ, {"TKO_DRY_RUN": "true"}):
                self.assertIsNone(bridge.read_position_records())

            with open(legacy_path, "rb") as f:
                self.assertEqual(f.read(), before)
            self.assertFalse(os.path.exists(dry_path))
            self.assertFalse(os.path.exists(live_path))


class TestEstimateSize(unittest.TestCase):
    def test_returns_min_order(self):
        result = execution.estimate_position_size(100.0)
        self.assertGreater(result, 0)


class TestTpSl(unittest.TestCase):
    def test_buy_tp_above_sl_below(self):
        tp, sl = execution.compute_tp_sl(100.0, "BUY")
        self.assertGreater(tp, 100.0)
        self.assertLess(sl, 100.0)

    def test_sell_tp_below_sl_above(self):
        tp, sl = execution.compute_tp_sl(100.0, "SELL")
        self.assertLess(tp, 100.0)
        self.assertGreater(sl, 100.0)


if __name__ == "__main__":
    unittest.main()

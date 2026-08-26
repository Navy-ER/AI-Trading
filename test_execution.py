"""Tests for execution.py — Phase 3 execution handler."""
import unittest
from unittest.mock import patch, MagicMock
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ["TOKOCRYPTO_API_KEY"] = "test_key"
os.environ["TOKOCRYPTO_SECRET_KEY"] = "test_secret"

import execution


class TestExecuteTrade(unittest.TestCase):
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
        ex.create_order.return_value = {"id": "12345", "filled": 0.0001, "cost": 5.0}
        with patch("execution.parse_quantity_precision", return_value=0.0001):
            result = execution.execute_trade("BUY", "BTCUSDT", 20.0, 50000)
        self.assertIsNotNone(result)
        self.assertEqual(result["id"], "12345")

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
        ex.create_order.return_value = {"id": "ORDER_001", "filled": 0.00024, "cost": 1.2}
        with patch("execution.parse_quantity_precision", return_value=0.000024):
            result = execution.execute_trade("BUY", "BTCUSDT", 1.2, 50000.0)
        self.assertIsNotNone(result)
        self.assertEqual(result["id"], "ORDER_001")
        ex.create_order.assert_called_once()

    @patch("execution.get_exchange")
    def test_successful_sell_order(self, mock_ex):
        ex = MagicMock()
        mock_ex.return_value = ex
        ex.fetch_ticker.return_value = {"last": 50000.0}
        ex.create_order.return_value = {"id": "ORDER_002", "filled": 0.00024, "cost": 1.2}
        with patch("execution.parse_quantity_precision", return_value=0.000024):
            result = execution.execute_trade("SELL", "ETHUSDT", 1.2, 50000.0)
        self.assertIsNotNone(result)

    @patch("execution.get_exchange")
    def test_insufficient_funds_returns_none(self, mock_ex):
        import ccxt
        ex = MagicMock()
        mock_ex.return_value = ex
        ex.fetch_ticker.return_value = {"last": 50000.0}
        ex.create_order.side_effect = ccxt.InsufficientFunds("not enough")
        with patch("execution.parse_quantity_precision", return_value=0.000024):
            result = execution.execute_trade("BUY", "BTCUSDT", 1.2, 50000.0)
        self.assertIsNone(result)


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

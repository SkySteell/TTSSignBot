import json
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import bybit_futures_signal_bot as sb
import bybit_futures_trader as trader
from tg_ui import TgUI


class SignalRiskTests(unittest.TestCase):
    def setUp(self):
        sb.CAPITAL = 150

    def test_new_universe_has_ten_large_assets_plus_kas(self):
        self.assertEqual(len(sb.SYMBOLS), 11)
        self.assertEqual(len(set(sb.SYMBOLS)), 11)
        self.assertIn("KAS/USDT:USDT", sb.SYMBOLS)
        self.assertIn("BTC/USDT:USDT", sb.SYMBOLS)

    def test_position_does_not_exceed_risk_or_leverage(self):
        for dist in (0.1, 0.5, 1.0, 2.0, 4.0):
            with self.subTest(dist=dist):
                pos, reason = sb.size_position(100, dist, 1,
                                               {"step": 0.001, "min_amt": 0.001, "min_cost": 5})
                self.assertIsNotNone(pos, reason)
                self.assertLessEqual(pos["risk"], 1.50 + 1e-6)
                self.assertLessEqual(pos["notional"], 150 * 3 * 0.5 + 1e-6)
                self.assertLessEqual(pos["lev"], 3)
                self.assertLessEqual(pos["margin"], 75 + 1e-6)

    def test_minimum_lot_cannot_break_one_percent_risk(self):
        pos, reason = sb.size_position(100, 3, 1,
                                       {"step": 1, "min_amt": 10, "min_cost": 5})
        self.assertIsNone(pos)
        self.assertIn("минимальн", reason)

    def test_zero_inputs_rejected(self):
        pos, _ = sb.size_position(0, 1, 1, {"step": 0.01})
        self.assertIsNone(pos)


class TradingSafetyTests(unittest.TestCase):
    def setUp(self):
        trader.CAPITAL_CAP = 150

    def base(self):
        return {
            "schema_version": 2, "last": {}, "open": {}, "closed": [],
            "legacy_closed": [], "pending": None,
            "day": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "day_start": 150.0, "peak": 150.0,
            "paused": False, "halted": False, "notified_day": "",
        }

    def test_drawdown_stop(self):
        st = self.base()
        reason = trader.risk_gate(st, 134)
        self.assertIn("просадка", reason)
        self.assertTrue(st["halted"])

    def test_daily_limit(self):
        st = self.base()
        reason = trader.risk_gate(st, 145)
        self.assertIn("дневной", reason)

    def test_pending_order_stops_new_entries(self):
        st = self.base()
        st["pending"] = {"symbol": "BTC/USDT:USDT"}
        self.assertIn("незавершённая", trader.risk_gate(st, 150))

    def test_equity_uses_usdt_equity_not_wallet(self):
        ex = Mock()
        ex.fetch_balance.return_value = {
            "USDT": {"total": 150},
            "info": {"result": {"list": [{"coin": [
                {"coin": "USDT", "walletBalance": "150", "equity": "147.2"},
            ]}]}}
        }
        self.assertAlmostEqual(trader.equity(ex), 147.2)

    def test_missing_equity_fails_closed(self):
        ex = Mock()
        ex.fetch_balance.return_value = {"USDT": {"total": 150}, "info": {}}
        with self.assertRaises(RuntimeError):
            trader.equity(ex)

    def test_hedge_positions_rejected(self):
        ex = Mock()
        ex.fetch_positions.return_value = [{
            "symbol": "BTC/USDT:USDT", "contracts": 0.3,
            "info": {"positionIdx": 1},
        }]
        with self.assertRaises(RuntimeError):
            trader.open_positions(ex)

    def test_unknown_position_halts_bot(self):
        st = self.base()
        ex = Mock()
        with (
            patch.object(trader, "equity", return_value=150),
            patch.object(trader, "open_positions", return_value={
                "BTC/USDT:USDT": {"contracts": 0.1, "side": "long"}}),
            patch.object(trader, "save_state"),
            patch.object(trader.sb, "tg"),
        ):
            trader.run_cycle(ex, st, sb.SYMBOLS)
        self.assertTrue(st["halted"])
        self.assertTrue(st["paused"])

    def test_live_mode_is_unconditionally_disabled(self):
        with patch.object(trader, "MODE", "live"):
            with self.assertRaisesRegex(RuntimeError, "только MODE=demo"):
                trader.make_exchange()

    def test_existing_stop_file_blocks_new_entries(self):
        st = self.base()
        with tempfile.TemporaryDirectory() as td:
            stop = Path(td) / "STOP"
            stop.touch()
            with patch.object(trader, "STOP_FILE", stop):
                self.assertEqual(trader.risk_gate(st, 150), "файл STOP")

    def test_account_margin_isolated_before_leverage(self):
        ex = Mock()
        ex.private_get_v5_account_info.return_value = {
            "retCode": 0, "result": {"marginMode": "ISOLATED_MARGIN"}
        }
        ex.fetch_positions.return_value = [{
            "symbol": "BTC/USDT:USDT", "contracts": 0,
            "leverage": 3, "info": {"positionIdx": 0},
        }]
        trader.ensure_margin_and_leverage(ex, "BTC/USDT:USDT", 3)
        ex.set_leverage.assert_called_once_with(3, "BTC/USDT:USDT")
        ex.set_margin_mode.assert_not_called()

    def test_account_cross_margin_blocks_entry(self):
        ex = Mock()
        ex.private_get_v5_account_info.return_value = {
            "retCode": 0, "result": {"marginMode": "REGULAR_MARGIN"}
        }
        with self.assertRaises(RuntimeError):
            trader.ensure_margin_and_leverage(ex, "BTC/USDT:USDT", 3)
        ex.set_leverage.assert_not_called()

    def test_unknown_create_order_is_not_retried(self):
        ex = Mock()
        ex.fetch_ticker.return_value = {"last": 100}
        ex.amount_to_precision.return_value = "0.1"
        ex.price_to_precision.side_effect = lambda s, px: str(px)
        ex.create_order.side_effect = TimeoutError("timeout")
        sig = {"symbol": "BTC/USDT:USDT", "d": 1, "price": 100,
               "stop": 98, "target": 104, "dist": 2, "qty": 0.1,
               "lev": 1, "side": "LONG"}
        sb.CAPITAL = 150
        with patch.object(trader, "ensure_margin_and_leverage"):
            with self.assertRaises(trader.UnsafeTradeError):
                trader.open_trade(ex, sig)
        ex.create_order.assert_called_once()

    def test_closed_pnl_is_read_from_bybit_not_balance(self):
        ex = Mock()
        ex.market_id.return_value = "ETHUSDT"
        ex.market.return_value = {"precision": {"amount": 0.001}}
        opened = int(time.time() * 1000) - 60_000
        ex.private_get_v5_position_closed_pnl.return_value = {
            "retCode": 0, "result": {"nextPageCursor": "", "list": [{
                "createdTime": str(opened + 30_000),
                "closedSize": "2", "closedPnl": "1.23",
            }]}
        }
        data = trader.fetch_trade_closed_pnl(ex, "ETH/USDT:USDT",
                                             {"opened_ms": opened, "qty": 2.0})
        self.assertEqual(data, (1.23, 2.0))
        ex.fetch_balance.assert_not_called()

    def test_unconfirmed_pnl_is_not_fabricated(self):
        ex = Mock()
        ex.market_id.return_value = "ETHUSDT"
        ex.market.return_value = {"precision": {"amount": 0.001}}
        ex.private_get_v5_position_closed_pnl.return_value = {
            "retCode": 0, "result": {"nextPageCursor": "", "list": []}
        }
        data = trader.fetch_trade_closed_pnl(ex, "ETH/USDT:USDT",
                                             {"opened_ms": int(time.time() * 1000) - 2000,
                                              "qty": 1})
        self.assertIsNone(data)

    def test_old_stats_preserved_separately(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "trader_state.json"
            path.write_text(json.dumps({"closed": [{"r": 0.3}], "open": {}}),
                            encoding="utf-8")
            with patch.object(trader, "STATE_FILE", path):
                st = trader.load_state()
                self.assertEqual(st["legacy_closed"], [{"r": 0.3}])
                self.assertEqual(st["closed"], [])
                trader.save_state(st)
                self.assertEqual(trader.load_state()["schema_version"], 2)


class TelegramAuthorizationTests(unittest.TestCase):
    def create_ui(self):
        ctx = SimpleNamespace(st={"paused": False}, lock=Mock(),
                              save=Mock(), info={}, reasons={}, meta={})
        return TgUI(ctx, "FAKE-TOKEN", "-100123", "42")

    def test_unauthorized_group_member_ignored(self):
        ui = self.create_ui()
        seen = []
        ui.on_text = seen.append
        ui.handle({"message": {"chat": {"id": -100123},
                               "from": {"id": 99},
                               "text": "🛑 Закрыть всё"}})
        self.assertEqual(seen, [])

    def test_owner_can_use_menu(self):
        ui = self.create_ui()
        seen = []
        ui.on_text = seen.append
        ui.handle({"message": {"chat": {"id": -100123},
                               "from": {"id": 42},
                               "text": "/start"}})
        self.assertEqual(seen, ["/start"])

    def test_stale_close_confirmation_refused(self):
        ui = self.create_ui()
        ui.close_token = "abc123"
        ui.close_requested_at = time.time() - 70
        ui.send = Mock()
        ui.close_all = Mock()
        ui.on_action("closeall:abc123")
        ui.close_all.assert_not_called()


if __name__ == "__main__":
    unittest.main()

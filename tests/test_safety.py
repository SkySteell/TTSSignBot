import json
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import ccxt

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



class MonetaryStatisticsTests(unittest.TestCase):
    def test_summary_uses_usdt_instead_of_r(self):
        summary = sb.stats_line([1.20, -0.80, 0.05])
        self.assertIn("итого +0.450 USDT", summary)
        self.assertIn("средний +0.150 USDT", summary)
        self.assertIn("PF 1.56", summary)
        self.assertNotIn("ср.R", summary)
        self.assertNotIn("R,", summary)

    def test_telegram_report_uses_actual_pnl_for_every_pair(self):
        ui = TgUI(
            SimpleNamespace(
                st={"closed": [
                    {"ts": 2000000000, "symbol": "SOL/USDT:USDT", "pnl": 1.210, "r": 9},
                    {"ts": 2000000000, "symbol": "KAS/USDT:USDT", "pnl": -0.700, "r": -9},
                    {"ts": 2000000000, "symbol": "SOL/USDT:USDT", "pnl": 0.250, "r": 3},
                ]},
                lock=__import__("threading").RLock(),
            ),
            "FAKE-TOKEN", "42", "42",
        )
        report = ui.stats_text("all")
        self.assertIn("Общий результат: +0.760 USDT", report)
        self.assertIn("Средний результат: +0.253 USDT", report)
        self.assertIn("Лучшая: +1.210 USDT", report)
        self.assertIn("худшая: -0.700 USDT", report)
        self.assertIn("SOL/USDT:USDT: 2 сд., +1.460 USDT", report)
        self.assertIn("KAS/USDT:USDT: 1 сд., -0.700 USDT", report)
        self.assertIn("Profit Factor: 2.09", report)
        self.assertNotIn("+9.00R", report)
        self.assertNotIn("R", report)

    def test_paper_trade_uses_contract_quantity_in_usdt(self):
        trade = {
            "symbol": "SOL/USDT:USDT", "price": 100, "stop": 98,
            "target": 104, "qty": 0.5, "d": 1,
            "bar_ts": 1000,
        }
        df = sb.pd.DataFrame([{
            "ts": 2000, "low": 95, "high": 105, "close": 99,
        }])
        pnl, why, price = sb.resolve(trade, df)
        self.assertEqual(why, "стоп")
        self.assertEqual(price, 98)
        self.assertAlmostEqual(pnl, -1.0 - (100 * 0.5 * sb.RT_COST))

    def test_legacy_paper_r_history_not_converted_to_money(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "paper_state.json"
            path.write_text(json.dumps({
                "closed": [2.67], "open": [
                    {"symbol": "SOL/USDT:USDT", "price": 100, "bar_ts": 1}
                ],
                "last": {},
            }), encoding="utf-8")
            with patch.object(sb, "STATE_FILE", path):
                st = sb.load_state()
                self.assertEqual(st["closed"], [])
                self.assertEqual(st["legacy_closed"], [2.67])
                self.assertEqual(st["open"], [])
                self.assertEqual(len(st["legacy_open"]), 1)

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

    def test_preflight_timeout_does_not_mark_order_as_pending(self):
        ex = Mock()
        ex.fetch_ticker.side_effect = ccxt.RequestTimeout("ticker timeout")
        sent = []
        sig = {
            "symbol": "BTC/USDT:USDT", "d": 1, "price": 100,
            "stop": 98, "target": 104, "dist": 2, "qty": 0.1,
            "lev": 1, "side": "LONG"
        }
        with self.assertRaises(ccxt.NetworkError):
            trader.open_trade(ex, sig, before_submit=sent.append)
        self.assertEqual(sent, [])
        ex.create_order.assert_not_called()

    def test_intent_persisted_just_before_order(self):
        ex = Mock()
        ex.fetch_ticker.return_value = {"last": 100}
        ex.amount_to_precision.return_value = "0.1"
        ex.price_to_precision.side_effect = lambda sym, px: str(px)
        ex.create_order.side_effect = ccxt.RequestTimeout("order timeout")
        sent = []
        sig = {
            "symbol": "BTC/USDT:USDT", "d": 1, "price": 100,
            "stop": 98, "target": 104, "dist": 2, "qty": 0.1,
            "lev": 1, "side": "LONG"
        }
        sb.CAPITAL = 150
        with patch.object(trader, "ensure_margin_and_leverage"):
            with self.assertRaises(trader.UnsafeTradeError):
                trader.open_trade(ex, sig, before_submit=sent.append)
        self.assertEqual(len(sent), 1)
        ex.create_order.assert_called_once()

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



class ApiRecoveryTests(unittest.TestCase):
    def state(self):
        return {
            "schema_version": 2, "last": {}, "closed": [
                {"symbol": "XMR/USDT:USDT", "pnl": -1.421, "ts": 1794836400}
            ],
            "open": {
                "ZEC/USDT:USDT": {
                    "d": -1, "qty": 0.03, "stop": 934.0,
                    "target": 800.63, "opened_ms": 1794765840000
                }
            }, "pending": None,
            "day": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "day_start": 38271.0, "peak": 38271.0,
            "paused": True, "halted": True, "notified_day": "halted",
        }

    def exchange(self, stop="934", target="800.63"):
        ex = Mock()
        ex.private_get_v5_account_info.return_value = {
            "retCode": 0, "result": {"marginMode": "ISOLATED_MARGIN"}
        }
        ex.fetch_positions.return_value = [{
            "symbol": "ZEC/USDT:USDT", "contracts": 0.03,
            "side": "short", "info": {
                "positionIdx": 0, "stopLoss": stop, "takeProfit": target
            }
        }]
        return ex

    def recovery(self, state, exchange, balance=38269.579):
        with (
            tempfile.TemporaryDirectory() as td,
            patch.object(trader, "STOP_FILE", Path(td) / "STOP"),
            patch.object(trader, "equity", return_value=balance),
            patch.object(trader, "save_state"),
        ):
            return trader.verify_and_resume(exchange, state)

    def test_manual_recovery_verifies_position_protection_and_keeps_history(self):
        st = self.state()
        ok, reason = self.recovery(st, self.exchange())
        self.assertTrue(ok, reason)
        self.assertFalse(st["halted"])
        self.assertFalse(st["paused"])
        self.assertEqual(st["closed"][0]["pnl"], -1.421)

    def test_recovery_rejects_missing_sl(self):
        st = self.state()
        ok, msg = self.recovery(st, self.exchange(stop=""))
        self.assertFalse(ok)
        self.assertIn("нет подтверждённых", msg)
        self.assertTrue(st["halted"])

    def test_recovery_rejects_unknown_positions(self):
        st = self.state()
        ex = self.exchange()
        ex.fetch_positions.return_value.append({
            "symbol": "ETH/USDT:USDT", "contracts": 0.1, "side": "long",
            "info": {"positionIdx": 0, "stopLoss": "1", "takeProfit": "2"}
        })
        ok, msg = self.recovery(st, ex)
        self.assertFalse(ok)
        self.assertIn("биржевые позиции", msg)
        self.assertTrue(st["paused"])

    def test_recovery_rejects_unknown_pending_order(self):
        st = self.state()
        st["pending"] = {"symbol": "BTC/USDT:USDT"}
        ex = self.exchange()
        ok, msg = self.recovery(st, ex)
        self.assertFalse(ok)
        self.assertIn("неразрешённый ордер", msg)
        ex.private_get_v5_account_info.assert_not_called()

    def test_recovery_checks_daily_loss_limit(self):
        st = self.state()
        ok, msg = self.recovery(st, self.exchange(), balance=38265.0)
        self.assertFalse(ok)
        self.assertIn("дневной лимит", msg)
        self.assertTrue(st["halted"])

    def test_recovery_checks_maximum_drawdown(self):
        st = self.state()
        ok, msg = self.recovery(st, self.exchange(), balance=38255.0)
        self.assertFalse(ok)
        self.assertIn("просадка", msg)
        self.assertTrue(st["halted"])

    def test_recovery_rejects_unavailable_api(self):
        st = self.state()
        ex = self.exchange()
        with tempfile.TemporaryDirectory() as td, patch.object(trader, "STOP_FILE", Path(td) / "STOP"):
            with patch.object(trader, "equity", side_effect=ccxt.NetworkError("timeout")):
                ok, msg = trader.verify_and_resume(ex, st)
        self.assertFalse(ok)
        self.assertIn("недоступны", msg)
        self.assertTrue(st["halted"])

    def test_api_network_exception_is_distinct_from_auth_failure(self):
        self.assertIsInstance(ccxt.RequestTimeout("timeout"), ccxt.NetworkError)
        self.assertNotIsInstance(ccxt.AuthenticationError("invalid keys"), ccxt.NetworkError)

    def test_telegram_recovery_button_only_admin(self):
        calls = []
        ctx = SimpleNamespace(
            st={"halted": True, "paused": True}, lock=__import__("threading").RLock(),
            verify_resume=lambda: (calls.append("recovered") or (True, "Проверено")),
            info={}, reasons={}, meta={}, save=Mock(),
        )
        ui = TgUI(ctx, "fake", "-100123", "42")
        ui.send = Mock()
        ui.handle({"message": {"chat": {"id": -100123}, "from": {"id": 99},
                               "text": "🔄 Проверить и возобновить"}})
        self.assertEqual(calls, [])
        ui.handle({"message": {"chat": {"id": -100123}, "from": {"id": 42},
                               "text": "🔄 Проверить и возобновить"}})
        self.assertEqual(calls, ["recovered"])


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

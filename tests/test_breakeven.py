import copy
import errno
import unittest
from unittest.mock import Mock, patch

import ccxt
import bybit_futures_trader as trader


class BreakevenTests(unittest.TestCase):
    sym = "ZEC/USDT:USDT"

    def setup_case(self, d=-1):
        self.trade = {"entry": 100, "stop": 110 if d == -1 else 90,
                      "target": 80 if d == -1 else 120, "qty": 1, "d": d}
        self.st = {"open": {self.sym: self.trade}, "halted": False, "paused": False}
        self.pos = {"side": "short" if d == -1 else "long", "contracts": 1,
                    "markPrice": 90 if d == -1 else 110,
                    "info": {"stopLoss": str(self.trade["stop"]),
                             "takeProfit": str(self.trade["target"]), "positionIdx": "0"}}
        self.ex = Mock(precisionMode=ccxt.TICK_SIZE)
        self.ex.market.return_value = {"precision": {"price": 0.01}}
        self.ex.market_id.return_value = "ZECUSDT"
        self.ex.price_to_precision.side_effect = lambda s, p: f"{p:.2f}"
        self.ex.private_post_v5_position_trading_stop.return_value = {"retCode": 0}
        self.actual = copy.deepcopy(self.pos)
        self.actual["info"]["stopLoss"] = "99.80" if d == -1 else "100.20"

    def run_move(self):
        with (patch.object(trader.sb, "RT_COST", 0.002),
              patch.object(trader, "save_state") as save,
              patch.object(trader, "open_positions", return_value={self.sym: self.actual}),
              patch.object(trader.sb, "tg") as tg):
            trader.move_stop_to_breakeven(self.ex, self.st, self.sym, self.pos)
        return save, tg

    def test_long_and_short_confirm_and_preserve_tp(self):
        for d in (-1, 1):
            with self.subTest(d=d):
                self.setup_case(d)
                save, tg = self.run_move()
                request = self.ex.private_post_v5_position_trading_stop.call_args.args[0]
                self.assertEqual(request["stopLoss"], "99.80" if d == -1 else "100.20")
                self.assertNotIn("takeProfit", request)
                self.assertEqual(request["positionIdx"], 0)
                self.assertTrue(self.trade["breakeven_done"])
                self.assertNotIn("breakeven_pending", self.trade)
                self.assertEqual(self.trade["initial_stop"], 110 if d == -1 else 90)
                self.assertEqual(save.call_count, 2)
                tg.assert_called_once()

    def test_below_one_r_no_request(self):
        self.setup_case()
        self.pos["markPrice"] = 90.01
        self.run_move()
        self.ex.private_post_v5_position_trading_stop.assert_not_called()

    def test_disk_failure_prevents_exchange_write(self):
        self.setup_case()
        with (patch.object(trader.sb, "RT_COST", 0.002),
              patch.object(trader, "save_state", side_effect=OSError(errno.ENOSPC, "full"))):
            with self.assertRaises(OSError):
                trader.move_stop_to_breakeven(self.ex, self.st, self.sym, self.pos)
        self.ex.private_post_v5_position_trading_stop.assert_not_called()

    def test_intent_persisted_before_mutation(self):
        self.setup_case()
        def submit(params):
            self.assertIn("breakeven_pending", self.trade)
            self.assertGreater(save.call_count, 0)
            return {"retCode": 0}
        self.ex.private_post_v5_position_trading_stop.side_effect = submit
        with (patch.object(trader.sb, "RT_COST", 0.002),
              patch.object(trader, "save_state") as save,
              patch.object(trader, "open_positions", return_value={self.sym: self.actual}),
              patch.object(trader.sb, "tg")):
            trader.move_stop_to_breakeven(self.ex, self.st, self.sym, self.pos)

    def test_timeout_reconciles_after_restart_without_resubmit(self):
        self.setup_case()
        self.ex.private_post_v5_position_trading_stop.side_effect = ccxt.RequestTimeout("timeout")
        with self.assertRaises(trader.UnsafeTradeError):
            self.run_move()
        self.st = copy.deepcopy(self.st)
        self.trade = self.st["open"][self.sym]
        self.st["halted"] = self.st["paused"] = True
        self.run_move()
        self.assertEqual(self.ex.private_post_v5_position_trading_stop.call_count, 1)
        self.assertTrue(self.trade["breakeven_done"])
        self.assertTrue(self.st["halted"])

    def test_unconfirmed_stop_or_changed_tp_blocks(self):
        for field, value in (("stopLoss", "110"), ("takeProfit", "79")):
            self.setup_case()
            self.actual["info"][field] = value
            with self.assertRaises(trader.UnsafeTradeError):
                self.run_move()
            self.assertIn("breakeven_pending", self.trade)
            self.assertNotIn("breakeven_done", self.trade)

    def test_pending_not_applied_is_never_blindly_retried(self):
        self.setup_case()
        self.trade["breakeven_pending"] = {"stop": 99.8, "target": 80}
        self.actual = copy.deepcopy(self.pos)
        with self.assertRaises(trader.UnsafeTradeError):
            self.run_move()
        self.ex.private_post_v5_position_trading_stop.assert_not_called()

    def test_already_better_stop_is_not_loosened(self):
        self.setup_case()
        self.pos["info"]["stopLoss"] = "99"
        self.run_move()
        self.ex.private_post_v5_position_trading_stop.assert_not_called()
        self.assertEqual(self.trade["stop"], 99)

    def test_done_or_paused_or_halted_does_not_submit(self):
        for key in ("breakeven_done", "paused", "halted"):
            self.setup_case()
            (self.trade if key == "breakeven_done" else self.st)[key] = True
            self.run_move()
            self.ex.private_post_v5_position_trading_stop.assert_not_called()

    def test_changed_quantity_or_missing_protection_blocks(self):
        for change in ("qty", "sl"):
            self.setup_case()
            if change == "qty":
                self.pos["contracts"] = 0.5
            else:
                self.pos["info"]["stopLoss"] = "0"
            with self.assertRaises(trader.UnsafeTradeError):
                self.run_move()
            self.ex.private_post_v5_position_trading_stop.assert_not_called()

    def test_resume_rejects_unresolved_stop_intent(self):
        self.setup_case()
        self.st["halted"] = True
        self.trade["breakeven_pending"] = {"stop": 99.8, "target": 80}
        ok, _ = trader.verify_and_resume(self.ex, self.st)
        self.assertFalse(ok)
        self.ex.fetch_positions.assert_not_called()

    def test_tick_rounding_towards_profit(self):
        for d in (-1, 1):
            self.setup_case(d)
            self.ex.market.return_value = {"precision": {"price": 0.1}}
            self.actual["info"]["stopLoss"] = "99.7" if d == -1 else "100.3"
            with (patch.object(trader.sb, "RT_COST", 0.00201),
                  patch.object(trader, "save_state"),
                  patch.object(trader, "open_positions", return_value={self.sym: self.actual}),
                  patch.object(trader.sb, "tg")):
                trader.move_stop_to_breakeven(self.ex, self.st, self.sym, self.pos)
            request = self.ex.private_post_v5_position_trading_stop.call_args.args[0]
            self.assertEqual(request["stopLoss"], "99.70" if d == -1 else "100.30")

    def test_manage_open_calls_breakeven_for_existing_trade(self):
        self.setup_case()
        self.trade["opened_ms"] = int(trader.time.time() * 1000)
        with patch.object(trader, "move_stop_to_breakeven") as move:
            trader.manage_open(self.ex, self.st, {self.sym: self.pos})
        move.assert_called_once_with(self.ex, self.st, self.sym, self.pos)

    def test_readback_network_failure_is_unsafe(self):
        self.setup_case()
        with (patch.object(trader.sb, "RT_COST", 0.002),
              patch.object(trader, "save_state"),
              patch.object(trader, "open_positions", side_effect=ccxt.RequestTimeout("read"))):
            with self.assertRaises(trader.UnsafeTradeError):
                trader.move_stop_to_breakeven(self.ex, self.st, self.sym, self.pos)
        self.assertIn("breakeven_pending", self.trade)

    def test_position_closed_during_update_keeps_intent_for_closed_pnl(self):
        self.setup_case()
        with (patch.object(trader.sb, "RT_COST", 0.002),
              patch.object(trader, "save_state"),
              patch.object(trader, "open_positions", return_value={}),
              patch.object(trader.sb, "tg") as tg):
            trader.move_stop_to_breakeven(self.ex, self.st, self.sym, self.pos)
        self.assertIn("breakeven_pending", self.trade)
        self.assertNotIn("breakeven_done", self.trade)
        tg.assert_not_called()



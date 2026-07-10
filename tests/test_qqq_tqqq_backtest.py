import importlib.util
import pathlib
import unittest
from unittest import mock


SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "backtest_qqq_tqqq_ytd.py"
SPEC = importlib.util.spec_from_file_location("qqq_tqqq_backtest", SCRIPT)
backtest = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(backtest)


def flat_prices(n=50, q=100.0, t=30.0):
    prices = {"QQQ": {}, "TQQQ": {}}
    start = backtest.dt.date(2026, 1, 2)
    day = start
    added = 0
    while added < n:
        if day.weekday() < 5:
            d = day.isoformat()
            prices["QQQ"][d] = q
            prices["TQQQ"][d] = t
            added += 1
        day += backtest.dt.timedelta(days=1)
    return prices


def strategy_rows(n=6):
    rows = []
    start = backtest.dt.date(2026, 1, 2)
    tqqq = [18.0, 20.0, 25.0, 22.0, 23.0, 24.0]
    for i in range(n):
        tqqq_close = tqqq[i] if i < len(tqqq) else tqqq[-1] + (i - len(tqqq) + 1) * 0.25
        rows.append({
            "date": (start + backtest.dt.timedelta(days=i)).isoformat(),
            "QQQ": 100.0,
            "TQQQ": tqqq_close,
            "ema8": 100.0,
            "ema13": 99.0,
            "ema21": 98.0,
            "ema34": 97.0,
            "ema55": 96.0,
            "atr14": 1.0,
            "rsi14": 50.0,
            "qqq_5d_pct": None,
            "tqqq_5d_pct": None,
            "ema21_slope_5d": 1.0,
        })
    return rows


def signal(**overrides):
    value = {
        "state": "mixed",
        "stacked": False,
        "trend_break": False,
        "overheat": False,
        "near8": False,
        "near21": False,
    }
    value.update(overrides)
    return value


class QqqTqqqBacktestTests(unittest.TestCase):
    def test_max_drawdown_uses_peak_to_trough(self):
        self.assertAlmostEqual(backtest.max_drawdown([100, 120, 90, 130]), -0.25)

    def test_drawdown_includes_starting_capital_anchor(self):
        self.assertAlmostEqual(backtest.max_drawdown_from_capital([90, 95], 100), -0.10)

    def test_ccs_payoff_is_capped_by_spread_width(self):
        self.assertAlmostEqual(backtest.ccs_payoff_per_contract(90, 91, width=1, credit=0.25), 25.0)
        self.assertAlmostEqual(backtest.ccs_payoff_per_contract(91.5, 91, width=1, credit=0.25), -25.0)
        self.assertAlmostEqual(backtest.ccs_payoff_per_contract(95, 91, width=1, credit=0.25), -75.0)

    def test_overheat_blocks_a_simultaneous_pullback_entry_flag(self):
        row = strategy_rows(1)[0]
        row.update({
            "QQQ": 100.0,
            "ema8": 99.8,
            "ema13": 99.0,
            "ema21": 98.0,
            "ema34": 97.0,
            "atr14": 1.0,
            "qqq_5d_pct": 3.5,
            "tqqq_5d_pct": 8.0,
            "ema21_slope_5d": 1.0,
        })
        result = backtest.classify(row)
        self.assertTrue(result["overheat"])
        self.assertFalse(result["near8"])
        self.assertFalse(result["near21"])
        self.assertEqual(result["state"], "overheat")

    def test_flat_market_fires_no_tactical_trades(self):
        result = backtest.run_backtest(flat_prices(), capital=100000, warmup=5)

        self.assertEqual(result["trades"], [])
        self.assertEqual(result["ccs"], [])
        expected = 30000.0 + 70000.0 / 1.0005
        self.assertAlmostEqual(result["summary"][0]["final_value"], expected)
        self.assertLess(result["summary"][0]["total_return_pct"], 0.0)
        self.assertAlmostEqual(
            result["summary"][0]["max_drawdown_pct"],
            (expected / 100000.0 - 1) * 100,
        )
        self.assertFalse(result["methodology"]["decision_grade"])
        self.assertFalse(result["methodology"]["ccs_included_in_strategy_returns"])

    def test_tactical_signal_fills_at_next_close_with_adverse_slippage(self):
        rows = strategy_rows()
        by_date = {
            rows[0]["date"]: signal(state="ema8", stacked=True, near8=True),
            rows[2]["date"]: signal(state="break", trend_break=True),
        }

        with mock.patch.object(backtest, "classify", side_effect=lambda row, prev=None: by_date.get(row["date"], signal())):
            result = backtest.run_strategy_on_rows(
                rows,
                capital=100000,
                warmup=0,
                config={
                    "core_alloc": 0.0,
                    "ema8_alloc": 0.50,
                    "equity_slippage_bps": 10.0,
                },
            )

        self.assertEqual(len(result["trades"]), 1)
        trade = result["trades"][0]
        self.assertEqual(trade["entry_signal_date"], rows[0]["date"])
        self.assertEqual(trade["entry_date"], rows[1]["date"])
        self.assertEqual(trade["exit_signal_date"], rows[2]["date"])
        self.assertEqual(trade["exit_date"], rows[3]["date"])
        self.assertAlmostEqual(trade["entry_price"], rows[1]["TQQQ"] * 1.001)
        self.assertAlmostEqual(trade["exit_price"], rows[3]["TQQQ"] * 0.999)
        self.assertEqual(trade["fill_timing"], "next_trading_close_after_signal")
        self.assertGreater(result["transaction_costs_dollars"], 0)

    def test_final_session_signal_is_not_backfilled(self):
        rows = strategy_rows()
        last_date = rows[-1]["date"]
        with mock.patch.object(
            backtest,
            "classify",
            side_effect=lambda row, prev=None: (
                signal(state="ema8", stacked=True, near8=True) if row["date"] == last_date else signal()
            ),
        ):
            result = backtest.run_strategy_on_rows(
                rows,
                capital=100000,
                warmup=0,
                config={"core_alloc": 0.0, "ema8_alloc": 0.5},
            )
        self.assertEqual(result["trades"], [])

    def test_partial_and_final_exit_are_one_position_without_pnl_double_count(self):
        rows = strategy_rows()
        by_date = {
            rows[0]["date"]: signal(state="ema8", stacked=True, near8=True),
            rows[2]["date"]: signal(state="overheat", stacked=True, overheat=True),
            rows[3]["date"]: signal(state="break", trend_break=True),
        }
        with mock.patch.object(backtest, "classify", side_effect=lambda row, prev=None: by_date.get(row["date"], signal())):
            result = backtest.run_strategy_on_rows(
                rows,
                capital=100000,
                warmup=0,
                config={
                    "core_alloc": 0.0,
                    "ema8_alloc": 0.5,
                    "overheat_exit": "partial",
                    "overheat_sell_pct": 0.5,
                },
            )

        self.assertEqual([t["status"] for t in result["trades"]], ["partial", "closed"])
        self.assertEqual(result["num_tactical_trades"], 1)
        self.assertAlmostEqual(
            result["tactical_pnl_dollars"],
            result["final_value"] - 100000.0,
        )

    def test_ccs_scenario_uses_next_close_and_skips_incomplete_horizon(self):
        rows = strategy_rows(40)
        states = [signal() for _ in rows]
        # The first signal is intentionally inside the mandatory indicator
        # warmup and must not become a synthetic option trade.
        states[0] = signal(overheat=True)
        states[34] = signal(overheat=True)
        hedges = backtest.simulate_ccs(
            rows,
            states,
            hold_days=2,
            credit=0.25,
            execution_cost_per_contract=4.60,
        )

        self.assertEqual(len(hedges), 1)
        hedge = hedges[0]
        self.assertEqual(hedge["signal_date"], rows[34]["date"])
        self.assertEqual(hedge["entry_date"], rows[35]["date"])
        self.assertEqual(hedge["expiry_date"], rows[37]["date"])
        self.assertEqual(hedge["signal_warmup_sessions"], 34)
        self.assertEqual(hedge["premium_source"], "synthetic_formula_not_historical_option_chain")
        self.assertAlmostEqual(
            hedge["stylized_net_payoff_per_contract"],
            hedge["gross_expiration_payoff_per_contract"] - 4.60,
        )
        self.assertAlmostEqual(hedge["stylized_max_gain_per_contract"], 20.40)
        self.assertAlmostEqual(hedge["stylized_max_loss_per_contract"], 79.60)
        self.assertFalse(hedge["decision_grade"])

    def test_synthetic_ccs_payoff_is_excluded_from_returns(self):
        rows = strategy_rows(40)
        with mock.patch.object(backtest, "classify", return_value=signal(overheat=True)):
            result = backtest.run_strategy_on_rows(
                rows,
                capital=100000,
                warmup=0,
                config={"core_alloc": 0.0, "ccs_mode": "spot_pct", "ccs_hold_days": 2},
            )

        self.assertNotEqual(result["ccs_stylized_payoff_dollars"], 0.0)
        self.assertIsNone(result["ccs_pnl_dollars"])
        self.assertEqual(result["combined_final_value"], result["final_value"])
        self.assertEqual(result["combined_return_pct"], result["total_return_pct"])
        self.assertFalse(result["return_includes_ccs"])

    def test_rsi_does_not_backfill_future_information(self):
        values = list(range(1, 22))
        output = backtest.rsi(values, n=14)
        self.assertTrue(all(v is None for v in output[:14]))
        self.assertEqual(output[14], 100.0)
        self.assertEqual(backtest.rsi([10.0] * 16, n=14)[14], 50.0)

    def test_close_proxy_atr_does_not_insert_a_fake_zero_return(self):
        output = backtest.close_proxy_atr([100.0, 102.0, 105.0], n=2)
        self.assertIsNone(output[0])
        self.assertIsNone(output[1])
        self.assertEqual(output[2], 2.5)

    def test_close_proxy_atr_requires_the_full_seed_window(self):
        output = backtest.close_proxy_atr([100.0, 101.0, 103.0, 106.0], n=3)
        self.assertEqual(output[:3], [None, None, None])
        self.assertEqual(output[3], 2.0)

    def test_ccs_requires_configured_warmup_and_complete_indicators(self):
        rows = strategy_rows(42)
        states = [signal() for _ in rows]
        states[34] = signal(overheat=True)
        states[35] = signal(overheat=True)
        states[36] = signal(overheat=True)
        rows[36]["atr14"] = None
        states[37] = signal(overheat=True)

        hedges = backtest.simulate_ccs(
            rows,
            states,
            hold_days=1,
            credit=0.25,
            warmup=36,
        )

        self.assertEqual([hedge["signal_date"] for hedge in hedges], [rows[37]["date"]])
        self.assertEqual(hedges[0]["signal_warmup_sessions"], 36)

    def test_invalid_price_fails_closed(self):
        prices = flat_prices()
        first_date = next(iter(prices["QQQ"]))
        prices["QQQ"][first_date] = float("nan")
        with self.assertRaisesRegex(ValueError, "positive and finite"):
            backtest.build_rows(prices)

    def test_mismatched_session_coverage_fails_closed(self):
        prices = flat_prices()
        missing_date = next(iter(prices["TQQQ"]))
        del prices["TQQQ"][missing_date]
        with self.assertRaisesRegex(ValueError, "session coverage mismatch"):
            backtest.build_rows(prices)


if __name__ == "__main__":
    unittest.main()

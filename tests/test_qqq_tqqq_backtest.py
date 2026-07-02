import importlib.util
import pathlib
import unittest


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


class QqqTqqqBacktestTests(unittest.TestCase):
    def test_max_drawdown_uses_peak_to_trough(self):
        self.assertAlmostEqual(backtest.max_drawdown([100, 120, 90, 130]), -0.25)

    def test_ccs_payoff_is_capped_by_spread_width(self):
        self.assertAlmostEqual(backtest.ccs_payoff_per_contract(90, 91, width=1, credit=0.25), 25.0)
        self.assertAlmostEqual(backtest.ccs_payoff_per_contract(91.5, 91, width=1, credit=0.25), -25.0)
        self.assertAlmostEqual(backtest.ccs_payoff_per_contract(95, 91, width=1, credit=0.25), -75.0)

    def test_flat_market_fires_no_tactical_trades(self):
        result = backtest.run_backtest(flat_prices(), capital=100000, warmup=5)

        self.assertEqual(result["trades"], [])
        self.assertEqual(result["ccs"], [])
        self.assertAlmostEqual(result["summary"][0]["final_value"], 100000.0)
        self.assertAlmostEqual(result["summary"][0]["total_return_pct"], 0.0)


if __name__ == "__main__":
    unittest.main()

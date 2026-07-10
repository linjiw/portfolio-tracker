"""Unit tests for generate.py core indicator math: _ema, _rsi, compute_fib.

These functions are the foundation of every chart / signal / verdict in the
dashboard but previously had zero direct regression coverage (all other tests
target scripts/). Reference values are computed against the standard
definitions (EMA seeded from first value, Wilder-smoothed RSI).
"""
import math
import csv
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import generate
from generate import _ema, _rsi, compute_fib, MOM_SCALE


def _dates(n):
    """n synthetic ISO dates."""
    return [f"2026-01-{i+1:02d}" if i < 31 else f"2026-02-{i-30:02d}" for i in range(n)]


class EmaTests(unittest.TestCase):
    def test_empty_input(self):
        self.assertEqual(_ema([], 5), [])

    def test_single_value_is_identity(self):
        self.assertEqual(_ema([42.0], 5), [42.0])

    def test_constant_series_stays_constant(self):
        out = _ema([100.0] * 30, 8)
        for v in out:
            self.assertAlmostEqual(v, 100.0, places=12)

    def test_matches_reference_recursion(self):
        """EMA seeded from first value: e = a*v + (1-a)*e."""
        vals = [100, 102, 101, 103, 105, 104, 106]
        n = 5
        a = 2.0 / (n + 1)
        ref, e = [], None
        for v in vals:
            e = v if e is None else a * v + (1 - a) * e
            ref.append(e)
        out = _ema(vals, n)
        self.assertEqual(len(out), len(vals))
        for got, want in zip(out, ref):
            self.assertAlmostEqual(got, want, places=10)

    def test_monotone_rise_ema_lags_below_price(self):
        vals = list(range(100, 130))
        out = _ema(vals, 5)
        # After warm-up the EMA must lag a strictly rising series
        for i in range(5, len(vals)):
            self.assertLess(out[i], vals[i])

    def test_fast_ema_tracks_closer_than_slow(self):
        vals = list(range(100, 140))
        e5, e21 = _ema(vals, 5), _ema(vals, 21)
        self.assertLess(abs(vals[-1] - e5[-1]), abs(vals[-1] - e21[-1]))


class RsiTests(unittest.TestCase):
    def test_short_series_returns_neutral_50(self):
        self.assertEqual(_rsi([1.0] * 10, n=14), [50.0] * 10)

    def test_all_gains_pins_high(self):
        vals = [float(100 + i) for i in range(40)]
        out = _rsi(vals)
        self.assertEqual(out[-1], 100.0)

    def test_all_losses_pins_low(self):
        vals = [float(200 - i) for i in range(40)]
        out = _rsi(vals)
        self.assertLess(out[-1], 1.0)

    def test_bounded_0_100(self):
        vals = [100 + 10 * math.sin(i / 3.0) for i in range(60)]
        for v in _rsi(vals):
            self.assertGreaterEqual(v, 0.0)
            self.assertLessEqual(v, 100.0)

    def test_wilder_smoothing_reference(self):
        """Cross-check against an independent Wilder RSI implementation."""
        vals = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42,
                45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28, 46.00,
                46.03, 46.41, 46.22, 45.64]
        n = 14
        deltas = [vals[i] - vals[i - 1] for i in range(1, len(vals))]
        ag = sum(d for d in deltas[:n] if d > 0) / n
        al = sum(-d for d in deltas[:n] if d < 0) / n
        ref = [None] * len(vals)
        ref[n] = 100 - 100 / (1 + ag / al)
        for i in range(n + 1, len(vals)):
            d = deltas[i - 1]
            ag = (ag * (n - 1) + max(d, 0)) / n
            al = (al * (n - 1) + max(-d, 0)) / n
            ref[i] = 100 - 100 / (1 + ag / al)
        out = _rsi(vals, n)
        for i in range(n, len(vals)):
            self.assertAlmostEqual(out[i], ref[i], places=10)
        # Warm-up values are neutral and never borrow the future day-n RSI.
        for i in range(n):
            self.assertEqual(out[i], 50.0)

    def test_flat_series_neutral(self):
        vals = [100.0] * 20
        out = _rsi(vals)
        self.assertEqual(out, [50.0] * 20)


class ComputeFibTests(unittest.TestCase):
    def test_short_history_returns_none(self):
        items = list(zip(_dates(20), [100.0] * 20))
        self.assertIsNone(compute_fib(items))

    def test_minimum_length_ok(self):
        items = list(zip(_dates(21), [100.0 + i for i in range(21)]))
        self.assertIsNotNone(compute_fib(items))

    def test_output_schema_and_lengths(self):
        n = 60
        px = [100 + 5 * math.sin(i / 4.0) + i * 0.3 for i in range(n)]
        fib = compute_fib(list(zip(_dates(n), px)))
        for key in ("e5", "e8", "e13", "e21", "mom", "rsi", "state",
                    "signals", "resonance", "now"):
            self.assertIn(key, fib)
        for key in ("e5", "e8", "e13", "e21", "mom", "rsi", "state"):
            self.assertEqual(len(fib[key]), n, key)
        now = fib["now"]
        for key in ("state", "label", "mom", "rsi", "res"):
            self.assertIn(key, now)
        self.assertEqual(now["state"], fib["state"][-1])
        self.assertEqual(now["mom"], fib["mom"][-1])

    def test_uptrend_labeled_up(self):
        px = [100.0 * (1.01 ** i) for i in range(60)]
        fib = compute_fib(list(zip(_dates(60), px)))
        self.assertEqual(fib["now"]["state"], "up")
        self.assertEqual(fib["now"]["label"], "多头趋势")
        self.assertGreater(fib["now"]["mom"], 0)

    def test_downtrend_labeled_down(self):
        px = [100.0 * (0.99 ** i) for i in range(60)]
        fib = compute_fib(list(zip(_dates(60), px)))
        self.assertEqual(fib["now"]["state"], "down")
        self.assertEqual(fib["now"]["label"], "空头趋势")
        self.assertLess(fib["now"]["mom"], 0)

    def test_resonance_cannot_fire_during_rsi_warmup(self):
        dates = _dates(60)
        px = [100 + i * 0.6 + 4 * math.sin(i / 2.0) for i in range(60)]
        fib = compute_fib(list(zip(dates, px)))
        index = {day: i for i, day in enumerate(dates)}

        self.assertTrue(all(index[event["date"]] >= 14
                            for event in fib["resonance"]))


class ValuationGuardrailTests(unittest.TestCase):
    def test_price_on_never_uses_a_future_close(self):
        prices = {"AAA": {"2026-01-05": 10.0, "2026-01-07": 12.0}}

        self.assertIsNone(generate.price_on(prices, "AAA", "2026-01-04"))
        self.assertEqual(generate.price_on(prices, "AAA", "2026-01-05"), 10.0)
        self.assertEqual(generate.price_on(prices, "AAA", "2026-01-06"), 10.0)

    def test_price_on_never_carries_an_arbitrarily_stale_close(self):
        prices = {"AAA": {"2020-01-02": 100.0}}

        self.assertIsNone(generate.price_on(prices, "AAA", "2026-07-09"))

    def test_price_on_exact_mode_rejects_a_market_data_hole(self):
        prices = {"AAA": {"2026-01-05": 10.0}}

        self.assertIsNone(generate.price_on(
            prices, "AAA", "2026-01-06", max_age_calendar_days=0))

    def test_trade_inactivity_does_not_truncate_history(self):
        txns = {"AAA": [
            {"date": "2026-01-02"},
            {"date": "2026-04-15"},
        ]}

        self.assertEqual(generate.resolve_history_start(txns), "2026-01-02")
        start, gaps = generate.continuous_start(txns)
        self.assertEqual(start, "2026-01-02")
        self.assertEqual(gaps, [("2026-01-02", "2026-04-15")])

    def test_explicit_history_start_is_validated(self):
        txns = {"AAA": [{"date": "2026-01-02"}, {"date": "2026-04-15"}]}

        self.assertEqual(generate.resolve_history_start(txns, "2026-02-01"), "2026-02-01")
        self.assertEqual(generate.resolve_history_start(txns, "2025-12-01"), "2025-12-01")
        with self.assertRaisesRegex(ValueError, "later than"):
            generate.resolve_history_start(txns, "2026-05-01")
        with self.assertRaisesRegex(ValueError, "YYYY-MM-DD"):
            generate.resolve_history_start(txns, "02/01/2026")


class PortfolioSnapshotParserTests(unittest.TestCase):
    HEADER = [
        "Symbol", "Description", "Quantity", "Last Price", "Current Value",
        "Total Gain/Loss Dollar", "Cost Basis Total", "Account Number", "Type",
    ]

    def _write_snapshot(self, rows):
        tmp = tempfile.TemporaryDirectory()
        path = Path(tmp.name) / "Portfolio_Positions.csv"
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["Broker positions export"])
            writer.writerow(self.HEADER)
            writer.writerows(rows)
            writer.writerow(["Date downloaded Jul-09-2026 4:40 p.m ET"])
        self.addCleanup(tmp.cleanup)
        return path

    def test_header_driven_parser_handles_reordered_columns_and_all_row_types(self):
        path = self._write_snapshot([
            ["QQQ", "Invesco QQQ", "10", "$500", "$5,000", "$1,000",
             "$4,000", "acct-01 / IRA", "Cash"],
            ["SPAXX**", "Money market", "1000", "$1", "$1,000", "--",
             "$1,000", "acct-01 / IRA", "Cash"],
            ["QQQ260717C600", "QQQ Jul call", "-1", "$1.25", "-$125", "$75",
             "$200", "acct-01 / IRA", "Margin"],
            ["Pending Activity", "Pending", "--", "--", "-$50", "--", "--",
             "acct-01 / IRA", "Cash"],
        ])

        positions = generate.parse_portfolio(path)
        extras = generate.parse_account_extras(path)

        self.assertEqual(set(positions), {"QQQ"})
        self.assertEqual(positions["QQQ"]["shares"], 10.0)
        self.assertEqual(positions["QQQ"]["value"], 5000.0)
        self.assertEqual(positions["QQQ"]["cost"], 4000.0)
        self.assertEqual(extras["cashTotal"], 1000.0)
        self.assertEqual(extras["pending"], -50.0)
        self.assertEqual(extras["optMarkNet"], -125.0)
        self.assertEqual(extras["optMarkGross"], 125.0)
        self.assertEqual(extras["equitySharesByAccount"],
                         {"acct-01 / IRA": {"QQQ": 10.0}})
        self.assertEqual(extras["optLegs"][0]["sym"], "-QQQ260717C600")
        self.assertEqual(extras["asOf"], "Jul-09-2026 4:40 p.m ET")

    def test_malformed_required_position_value_fails_closed(self):
        path = self._write_snapshot([
            ["QQQ", "Invesco QQQ", "10", "$500", "not-a-number", "$1,000",
             "$4,000", "acct-01", "Cash"],
        ])

        with self.assertRaisesRegex(ValueError, "invalid position current value"):
            generate.parse_portfolio(path)
        with self.assertRaisesRegex(ValueError, "invalid position current value"):
            generate.parse_account_extras(path)

    def test_multi_account_symbol_uses_aggregate_value_per_share(self):
        path = self._write_snapshot([
            ["QQQ", "Invesco QQQ", "10", "$500", "$5,000", "$1,000",
             "$4,000", "acct-01", "Cash"],
            ["QQQ", "Invesco QQQ", "2", "$550", "$1,100", "$200",
             "$900", "acct-02", "Cash"],
        ])

        position = generate.parse_portfolio(path)["QQQ"]

        self.assertEqual(position["shares"], 12.0)
        self.assertEqual(position["value"], 6100.0)
        self.assertAlmostEqual(position["price"], 6100.0 / 12.0)

    def test_missing_required_column_is_rejected(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "positions.csv"
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["Account Number", "Symbol", "Quantity", "Current Value"])
            writer.writerow(["acct-01", "QQQ", "10", "$5,000"])

        with self.assertRaisesRegex(ValueError, "missing portfolio columns"):
            generate.parse_portfolio(path)

    def test_executable_or_malformed_position_symbol_is_rejected(self):
        path = self._write_snapshot([
            ["BAD' onclick='alert(1)", "Hostile", "10", "$1", "$10", "$0",
             "$10", "acct-01", "Cash"],
        ])

        with self.assertRaisesRegex(ValueError, "invalid equity symbol"):
            generate.parse_portfolio(path)
        with self.assertRaisesRegex(ValueError, "invalid equity symbol"):
            generate.parse_account_extras(path)

    def test_share_class_symbol_is_normalized_without_losing_separator(self):
        path = self._write_snapshot([
            ["brk/b", "Berkshire class B", "2", "$500", "$1,000", "$100",
             "$900", "acct-01", "Cash"],
        ])

        positions = generate.parse_portfolio(path)

        self.assertEqual(set(positions), {"BRK/B"})


class RiskContributionCoverageTests(unittest.TestCase):
    def test_missing_benchmark_is_unavailable_not_fabricated_flat(self):
        dates = _dates(30)
        series = [{"date": d, "ret": i * 0.12 + (0.2 if i % 3 == 0 else 0),
                   "sp500": None}
                  for i, d in enumerate(dates)]
        stocks = [{"sym": "AAA", "held": True, "value": 1000.0,
                   "prices": [(d, 100 + i) for i, d in enumerate(dates)]}]

        risk = generate.compute_risk(series, stocks)

        self.assertEqual(risk["benchmarkQuality"], "unavailable")
        self.assertIsNone(risk["spAnnVol"])
        self.assertIsNone(risk["beta"])
        self.assertTrue(all(row["spuw"] is None for row in risk["uwSeries"]))
        self.assertTrue(all(row["spvol"] is None for row in risk["volSeries"]))

    def test_low_coverage_slice_is_withheld_instead_of_renormalized(self):
        dates = _dates(30)
        series = []
        for i, d in enumerate(dates):
            series.append({"date": d, "ret": i * 0.1, "sp500": i * 0.08})
        aaa = [(d, 100 + i) for i, d in enumerate(dates)]
        bbb = [(d, 50 + i * 0.5) for i, d in enumerate(dates) if i != 10]
        stocks = [
            {"sym": "AAA", "held": True, "value": 1000.0, "prices": aaa},
            {"sym": "BBB", "held": True, "value": 1000.0, "prices": bbb},
        ]

        risk = generate.compute_risk(series, stocks)

        self.assertEqual(risk["contrib"], [])
        self.assertEqual(risk["excluded"], ["AAA", "BBB"])
        self.assertEqual(risk["contribQuality"]["status"],
                         "unavailable-low-market-value-coverage")
        self.assertEqual(risk["contribQuality"]["coverageWeightPct"], 50.0)
        self.assertEqual(risk["contribQuality"]["incompleteSymbols"], ["BBB"])

    def test_high_coverage_exact_slice_is_published_with_explicit_basis(self):
        dates = _dates(35)
        series = [{"date": d, "ret": i * 0.1 + (0.2 if i % 4 == 0 else 0),
                   "sp500": i * 0.08 + (0.1 if i % 5 == 0 else 0)}
                  for i, d in enumerate(dates)]
        stocks = [
            {"sym": "AAA", "held": True, "value": 4900.0,
             "prices": [(d, 100 + i + (1 if i % 3 == 0 else 0)) for i, d in enumerate(dates)]},
            {"sym": "BBB", "held": True, "value": 4900.0,
             "prices": [(d, 80 + i * 0.7 + (0.8 if i % 4 == 0 else 0)) for i, d in enumerate(dates)]},
            {"sym": "NEW", "held": True, "value": 200.0,
             "prices": [(d, 20 + i) for i, d in enumerate(dates[-10:])]},
        ]

        risk = generate.compute_risk(series, stocks)

        self.assertEqual("partial-covered-equity-slice", risk["contribQuality"]["status"])
        self.assertEqual(98.0, risk["contribQuality"]["coverageWeightPct"])
        self.assertEqual(["NEW"], risk["excluded"])
        self.assertEqual({"AAA", "BBB"}, {row["sym"] for row in risk["contrib"]})
        self.assertAlmostEqual(100.0, sum(row["riskPct"] for row in risk["contrib"]), places=1)
        self.assertTrue(all("portfolioWeightPct" in row for row in risk["contrib"]))


class MoneyWeightedReturnTests(unittest.TestCase):
    def test_xirr_does_not_reject_valid_high_annualized_short_period_return(self):
        import datetime
        start = datetime.date(2026, 1, 1)
        end = start + datetime.timedelta(days=30)

        rate = generate.xirr([(start, -100.0), (end, 150.0)])

        self.assertIsNotNone(rate)
        self.assertGreater(rate, 10.0)
        period_return = (1.0 + rate) ** (30 / 365.0) - 1.0
        self.assertAlmostEqual(period_return, 0.5, places=6)

    def test_flat_series_is_range(self):
        # constant series: all EMAs equal (no strict stack), band = 0 < 0.8%
        # of price => range. (Alternating ±noise actually stacks the EMAs by
        # response amplitude each tick; drift stacks them too — stack order is
        # checked FIRST by design, so "flat" here means truly flat.)
        px = [100.0] * 60
        fib = compute_fib(list(zip(_dates(60), px)))
        self.assertEqual(fib["now"]["state"], "range")

    def test_momentum_bounded(self):
        px = [100.0 * (1.05 ** i) for i in range(40)]  # violent rally
        fib = compute_fib(list(zip(_dates(40), px)))
        for m in fib["mom"]:
            self.assertGreaterEqual(m, -100.0)
            self.assertLessEqual(m, 100.0)

    def test_momentum_formula(self):
        n = 50
        px = [100 + i * 0.5 for i in range(n)]
        fib = compute_fib(list(zip(_dates(n), px)))
        e5, e21 = _ema(px, 5), _ema(px, 21)
        sp = (e5[-1] - e21[-1]) / e21[-1]
        want = round(100 * math.tanh(sp / MOM_SCALE), 1)
        self.assertEqual(fib["now"]["mom"], want)

    def test_golden_cross_detected(self):
        # 30 days down then 30 days sharply up => EMA5 crosses above EMA13
        px = [100.0 - i * 0.8 for i in range(30)] + \
             [76.0 + i * 1.5 for i in range(30)]
        fib = compute_fib(list(zip(_dates(60), px)))
        types = [s["type"] for s in fib["signals"]]
        self.assertIn("golden", types)

    def test_death_cross_detected(self):
        px = [100.0 + i * 0.8 for i in range(30)] + \
             [124.0 - i * 1.5 for i in range(30)]
        fib = compute_fib(list(zip(_dates(60), px)))
        types = [s["type"] for s in fib["signals"]]
        self.assertIn("death", types)

    def test_signal_prices_rounded_2dp(self):
        px = [100.0 - i * 0.8 for i in range(30)] + \
             [76.0 + i * 1.5 for i in range(30)]
        fib = compute_fib(list(zip(_dates(60), px)))
        for s in fib["signals"]:
            self.assertEqual(s["price"], round(s["price"], 2))
        for arr in ("e5", "e8", "e13", "e21"):
            for v in fib[arr]:
                self.assertEqual(v, round(v, 2))

    def test_resonance_entries_only_on_state_change(self):
        px = [100.0 - i * 0.8 for i in range(30)] + \
             [76.0 + i * 1.5 for i in range(40)]
        fib = compute_fib(list(zip(_dates(70), px)))
        # no two consecutive resonance entries of the same type
        prev = None
        for r in fib["resonance"]:
            self.assertIn(r["type"], ("bull", "bear"))
            self.assertNotEqual(r["type"], prev)
            prev = r["type"]

    def test_states_are_valid_labels(self):
        px = [100 + 8 * math.sin(i / 5.0) for i in range(80)]
        fib = compute_fib(list(zip(_dates(80), px)))
        self.assertTrue(set(fib["state"]) <= {"up", "down", "range", "mixed"})


if __name__ == "__main__":
    unittest.main()

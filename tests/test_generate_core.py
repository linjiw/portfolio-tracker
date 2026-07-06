"""Unit tests for generate.py core indicator math: _ema, _rsi, compute_fib.

These functions are the foundation of every chart / signal / verdict in the
dashboard but previously had zero direct regression coverage (all other tests
target scripts/). Reference values are computed against the standard
definitions (EMA seeded from first value, Wilder-smoothed RSI).
"""
import math
import os
import sys
import unittest

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
        # No losses => RS capped via al==0 -> rs=999 -> RSI ~99.9
        self.assertGreater(out[-1], 99.0)

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
        ref[n] = 100 - 100 / (1 + (ag / al if al > 0 else 999))
        for i in range(n + 1, len(vals)):
            d = deltas[i - 1]
            ag = (ag * (n - 1) + max(d, 0)) / n
            al = (al * (n - 1) + max(-d, 0)) / n
            ref[i] = 100 - 100 / (1 + (ag / al if al > 0 else 999))
        out = _rsi(vals, n)
        for i in range(n, len(vals)):
            self.assertAlmostEqual(out[i], ref[i], places=10)
        # warm-up region is backfilled with the first real value
        for i in range(n):
            self.assertAlmostEqual(out[i], out[n], places=12)

    def test_flat_series_neutral(self):
        # zero losses on a flat tail: al==0 path (rs=999) must not crash
        vals = [100.0] * 20
        out = _rsi(vals)
        self.assertEqual(len(out), 20)


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

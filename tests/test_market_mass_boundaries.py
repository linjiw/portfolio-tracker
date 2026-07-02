import importlib.util
import datetime as dt
import math
import pathlib
import unittest


SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "market_mass_boundaries.py"
SPEC = importlib.util.spec_from_file_location("market_mass_boundaries", SCRIPT)
mmb = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mmb)


def synthetic_rows(n=320, detached=False, diffuse=False):
    rows = []
    start = dt.date(2025, 1, 2)
    for i in range(n):
        if diffuse:
            price = 80.0 + i * 0.18 + 4.0 * math.sin(i / 3.0)
            volume = 1_000_000
        else:
            price = 100.0 + 2.0 * math.sin(i / 5.0) + 0.7 * math.sin(i / 2.0)
            volume = 1_000_000 * (1.0 + 2.0 * max(0.0, 1.0 - abs(price - 100.0) / 3.0))
        rows.append({
            "date": (start + dt.timedelta(days=i)).isoformat(),
            "open": price,
            "high": price * 1.006,
            "low": price * 0.994,
            "close": price,
            "volume": volume,
            "dollar_volume": price * volume,
        })

    if detached:
        for j in range(20):
            price = 104.0 + j * 1.7
            volume = 700_000
            rows.append({
                "date": (start + dt.timedelta(days=n + j)).isoformat(),
                "open": price,
                "high": price * 1.006,
                "low": price * 0.994,
                "close": price,
                "volume": volume,
                "dollar_volume": price * volume,
            })
    return rows


class MarketMassBoundaryTests(unittest.TestCase):
    def test_centered_market_has_active_center(self):
        state = mmb.analyze_state(synthetic_rows(), {}, {}, lookback=252, half_life=63)
        center = state["center"]

        self.assertEqual(center["regime"], "active_center")
        self.assertGreaterEqual(center["quality_score"], 70)
        self.assertAlmostEqual(center["center_price"], 100.0, delta=1.5)
        self.assertTrue(center["active_center"])

    def test_detached_market_keeps_escape_side_volatility_based(self):
        state = mmb.analyze_state(synthetic_rows(detached=True), {}, {}, lookback=252, half_life=63)
        center = state["center"]
        band = mmb.boundary_rows(state, [1], [0.68])[0]

        self.assertEqual(center["regime"], "detached_from_mass")
        self.assertGreater(center["distance_z"], 2.5)
        self.assertEqual(band["upper_center_weight"], 0.0)
        self.assertAlmostEqual(band["upper_boundary"], band["vol_upper"])
        self.assertGreater(band["upper_boundary"], center["current_price"])

    def test_boundary_rows_are_zones_around_current_price(self):
        state = mmb.analyze_state(synthetic_rows(), {}, {}, lookback=252, half_life=63)
        bands = mmb.boundary_rows(state, [1, 5, 21], [0.68, 0.80])

        self.assertEqual(len(bands), 6)
        for band in bands:
            self.assertIn("gravity_boundary_weight", band)
            self.assertIn("ou_lower", band)
            self.assertIn("ou_upper", band)
            self.assertIn("profile_lower", band)
            self.assertIn("profile_upper", band)
            self.assertEqual(band["gravity_boundary_weight"], 0.0)
            self.assertLessEqual(band["lower_boundary"], state["center"]["current_price"] + 1e-9)
            self.assertGreaterEqual(band["upper_boundary"], state["center"]["current_price"] - 1e-9)
            self.assertLess(band["lower_zone_low"], band["lower_boundary"])
            self.assertGreater(band["lower_zone_high"], band["lower_boundary"])
            self.assertLess(band["upper_zone_low"], band["upper_boundary"])
            self.assertGreater(band["upper_zone_high"], band["upper_boundary"])

    def test_reference_gravity_metrics_are_reported(self):
        state = mmb.analyze_state(synthetic_rows(), {}, {}, lookback=252, half_life=63)
        gravity = state["center"]["gravity"]
        nodes = state["center"]["profile_nodes"]

        self.assertGreater(gravity["ou_samples"], 30)
        self.assertIsNotNone(gravity["kappa"])
        self.assertIn(gravity["gravity_regime"], {
            "centered_mean_reverting",
            "mixed_gravity",
            "levitating_thin_or_momentum",
        })
        self.assertGreaterEqual(gravity["gravity_score"], 0.0)
        self.assertLessEqual(gravity["gravity_score"], 100.0)
        self.assertGreaterEqual(gravity["levitation_score"], 0.0)
        self.assertLessEqual(gravity["levitation_score"], 100.0)
        self.assertIn("support", nodes)
        self.assertIn("resistance", nodes)

    def test_ou_hybrid_boundary_model_must_be_explicit(self):
        state = mmb.analyze_state(synthetic_rows(), {}, {}, lookback=252, half_life=63)
        default_band = mmb.boundary_rows(state, [5], [0.80])[0]
        hybrid_band = mmb.boundary_rows(state, [5], [0.80], boundary_model="ou_hybrid")[0]

        self.assertEqual(default_band["gravity_boundary_weight"], 0.0)
        self.assertGreaterEqual(hybrid_band["gravity_boundary_weight"], 0.0)
        self.assertIn("ou_lower", hybrid_band)

    def test_absorption_changes_mass_for_wide_ranges(self):
        rows = synthetic_rows(40)
        tight = dict(rows[-2])
        wide = dict(rows[-1])
        tight.update({"high": tight["close"] * 1.001, "low": tight["close"] * 0.999})
        wide.update({"high": wide["close"] * 1.08, "low": wide["close"] * 0.92})

        masses, _, _, absorptions = mmb.compute_masses([tight, wide], half_life=100)

        self.assertGreater(absorptions[0], absorptions[1])
        self.assertGreater(masses[0], masses[1])

    def test_backtest_calibration_reports_coverage_and_multipliers(self):
        rows = synthetic_rows(360)
        calibration = mmb.backtest_calibration(
            rows,
            vol_series={},
            fallback_vol_series={},
            lookback=126,
            half_life=42,
            horizons=[1, 5],
            confidences=[0.68, 0.80],
            max_evals=100,
        )

        self.assertTrue(calibration["available"])
        self.assertGreater(calibration["rows_tested"], 0)
        self.assertEqual(len(calibration["by_band"]), 4)
        for row in calibration["by_band"]:
            self.assertGreater(row["samples"], 0)
            self.assertGreaterEqual(row["close_coverage"], 0.0)
            self.assertLessEqual(row["close_coverage"], 1.0)
            self.assertGreaterEqual(row["suggested_width_multiplier"], 1.0)

    def test_fallback_implied_vol_blends_with_realized_vol(self):
        rows = synthetic_rows()
        state = mmb.analyze_state(
            rows,
            vol_series={},
            fallback_vol_series={mmb.date_key(rows[-1]["date"]): 20.0},
            lookback=252,
            half_life=63,
        )

        vol = state["volatility"]
        self.assertEqual(vol["implied_vol_source"], "fallback")
        self.assertAlmostEqual(vol["implied_vol"], 0.20)
        self.assertGreater(vol["annual_vol_used"], 0.0)

    def test_gravity_profile_sets_lookback_and_half_life(self):
        args = mmb.parse_args(["--gravity-profile", "swing"])

        self.assertEqual(args.lookback, 84)
        self.assertEqual(args.half_life, 21)


if __name__ == "__main__":
    unittest.main()

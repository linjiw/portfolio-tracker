import csv
import importlib.util
import datetime as dt
import math
import pathlib
import tempfile
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

    def test_missing_volume_penalizes_center_confidence(self):
        complete = synthetic_rows()
        missing = [dict(row, dollar_volume=0.0, volume=0.0) for row in complete]

        complete_center = mmb.score_center(complete, half_life=63)
        missing_center = mmb.score_center(missing, half_life=63)

        self.assertEqual(complete_center["volume_coverage_ratio"], 1.0)
        self.assertEqual(missing_center["volume_coverage_ratio"], 0.0)
        self.assertLess(missing_center["quality_score"], complete_center["quality_score"])

    def test_volume_proxy_merge_rejects_internal_calendar_hole(self):
        price = synthetic_rows(10)
        volume = [dict(row) for row in price]
        volume.pop(5)

        with self.assertRaisesRegex(ValueError, "calendars differ"):
            mmb.merge_price_and_volume_rows(price, volume)

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
        self.assertTrue(calibration["point_in_time_forecasts"])
        self.assertEqual(calibration["evaluation"], "in_sample_empirical_path_coverage")
        self.assertIn("empirical target quantile", calibration["multiplier_method"])
        for row in calibration["by_band"]:
            self.assertGreater(row["samples"], 0)
            self.assertGreaterEqual(row["close_coverage"], 0.0)
            self.assertLessEqual(row["close_coverage"], 1.0)
            self.assertGreaterEqual(row["suggested_width_multiplier"], 1.0)
            self.assertIn("uncapped_width_multiplier", row)

    def test_small_calibration_sample_is_reported_but_not_applied(self):
        rows = synthetic_rows(45)
        calibration = mmb.backtest_calibration(
            rows, {}, {}, lookback=30, half_life=10,
            horizons=[1], confidences=[0.80], max_evals=100,
        )
        state = mmb.analyze_state(rows, {}, {}, lookback=30, half_life=10)
        band = mmb.boundary_rows(
            state, [1], [0.80], calibration=calibration["multipliers"],
            apply_calibration=True,
        )[0]

        self.assertTrue(calibration["available"])
        self.assertFalse(calibration["by_band"][0]["reliable"])
        self.assertEqual(band["calibration_multiplier"], 1.0)

    def test_stale_implied_vol_is_excluded_instead_of_blended_indefinitely(self):
        rows = synthetic_rows()
        as_of = dt.date.fromisoformat(mmb.date_key(rows[-1]["date"]))
        stale_date = (as_of - dt.timedelta(days=30)).isoformat()
        fresh_date = (as_of - dt.timedelta(days=1)).isoformat()

        vol = mmb.volatility_context(
            rows,
            vol_series={stale_date: 80.0},
            fallback_vol_series={fresh_date: 20.0},
            max_implied_vol_age_days=7,
        )

        self.assertTrue(vol["primary_vol_stale"])
        self.assertFalse(vol["fallback_vol_stale"])
        self.assertEqual(vol["implied_vol_source"], "fallback")
        self.assertAlmostEqual(vol["implied_vol"], 0.20)
        self.assertEqual(vol["primary_vol_observation_date"], stale_date)

    def test_ohlc_parser_rejects_impossible_ranges_and_marks_imputation(self):
        self.assertIsNone(mmb._row_from_mapping({
            "Date": "2026-01-02", "Open": 100, "High": 99,
            "Low": 98, "Close": 100, "Volume": 10,
        }))
        row = mmb._row_from_mapping({
            "Date": "2026-01-02", "Close": 100, "Volume": 10,
        })
        self.assertTrue(row["ohlc_imputed"])

    def test_local_ohlcv_input_fails_closed_on_incomplete_bar(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "bad.csv"
            path.write_text("Date,Close,Volume\n2026-01-02,100,10\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "incomplete OHLC row"):
                mmb.read_ohlcv_csv(path)

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
        self.assertGreaterEqual(vol["annual_vol_used"], vol["realized_vol_21d"])
        self.assertEqual(vol["annual_vol_blend_method"], "max_realized_or_fallback_proxy")

    def test_gravity_profile_sets_lookback_and_half_life(self):
        args = mmb.parse_args(["--gravity-profile", "swing"])

        self.assertEqual(args.lookback, 84)
        self.assertEqual(args.half_life, 21)

    def test_boundary_csv_is_atomically_private(self):
        state = mmb.analyze_state(synthetic_rows(), {}, {}, lookback=252, half_life=63)
        bands = mmb.boundary_rows(state, [5], [0.80])
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "runtime" / "boundaries.csv"
            mmb.write_boundaries_csv(path, bands)

            with path.open(newline="", encoding="utf-8") as handle:
                rows_written = list(csv.DictReader(handle))
            self.assertEqual(len(rows_written), 1)
            self.assertEqual(rows_written[0]["horizon_days"], "5.0")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)


if __name__ == "__main__":
    unittest.main()

import importlib.util
import datetime as dt
import json
import pathlib
import tempfile
import unittest
from unittest import mock

import numpy as np
import pandas as pd


SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "usd_liquidity_score.py"
SPEC = importlib.util.spec_from_file_location("usd_liquidity_score", SCRIPT)
uls = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(uls)


def synthetic_frame(n=420):
    dates = pd.date_range("2024-01-01", periods=n, freq="D")
    x = np.arange(n, dtype=float)
    return pd.DataFrame(
        {
            "fed_assets_mn": 6_500_000 + x * 20,
            "tga_mn": 750_000 + np.sin(x / 20) * 25_000,
            "rrp_bn": 100 - x * 0.05,
            "reserves_mn": 3_000_000 + np.cos(x / 30) * 40_000,
            "sofr": 4.30 + np.sin(x / 50) * 0.05,
            "iorb": 4.35,
            "ust2y": 4.00 + np.sin(x / 35) * 0.15,
            "ust10y": 4.25 + np.cos(x / 40) * 0.12,
            "broad_usd": 120 + np.sin(x / 45),
            "vix": 18 + np.cos(x / 17) * 2,
            "hy_oas": 3.0 + np.sin(x / 25) * 0.2,
        },
        index=dates,
    )


class UsdLiquidityScoreTests(unittest.TestCase):
    def test_load_all_series_excludes_future_dated_observations(self):
        def fake(series_id, name, base_url=None):
            return pd.DataFrame(
                {name: [1.0, 2.0]},
                index=pd.to_datetime(["2026-07-09", "2026-07-10"]),
            )

        with mock.patch.object(uls, "load_fred_series", side_effect=fake):
            data, dates = uls.load_all_series(
                series={"TEST": "value"}, as_of=dt.date(2026, 7, 9))

        self.assertEqual(data.index[-1].date(), dt.date(2026, 7, 9))
        self.assertEqual(dates["TEST"], "2026-07-09")

    def test_net_liquidity_formula_and_units(self):
        df = synthetic_frame(10)
        features = uls.build_features(df)

        row = features.iloc[0]
        expected = 6500.0 - 750.0 - 100.0
        self.assertAlmostEqual(row["fed_assets_bn"], 6500.0)
        self.assertAlmostEqual(row["tga_bn"], 750.0)
        self.assertAlmostEqual(row["net_liquidity_bn"], expected)

    def test_latest_document_has_score_and_regime(self):
        doc = uls.latest_document(synthetic_frame(), {"WALCL": "2025-02-23"})

        self.assertGreaterEqual(doc["score"], 0)
        self.assertLessEqual(doc["score"], 100)
        self.assertIn(doc["regime"]["label"], {"TAILWIND", "NEUTRAL_EASING", "NEUTRAL_TIGHT", "TIGHT_RISK", "STRESS"})
        self.assertIn("netLiquidityBn", doc["metrics"])
        self.assertIn("net_liq_flow", doc["components"])
        self.assertIn("sourceLagDays", doc)
        self.assertFalse(doc["decisionGrade"])
        self.assertFalse(doc["backtestEligible"])

    def test_four_week_change_uses_calendar_days_on_business_index(self):
        dates = pd.bdate_range("2026-01-01", periods=50)
        df = synthetic_frame(50)
        df.index = dates
        features = uls.build_features(df)
        last = features.iloc[-1]
        target = dates[-1] - pd.Timedelta(days=28)
        prior_pos = dates.searchsorted(target, side="right") - 1
        expected = features.iloc[-1]["net_liquidity_bn"] - features.iloc[prior_pos]["net_liquidity_bn"]

        self.assertAlmostEqual(last["net_liq_4w_chg"], expected)

    def test_classify_score_thresholds(self):
        self.assertEqual(uls.classify_score(65)["label"], "TAILWIND")
        self.assertEqual(uls.classify_score(55)["label"], "NEUTRAL_EASING")
        self.assertEqual(uls.classify_score(45)["label"], "NEUTRAL_TIGHT")
        self.assertEqual(uls.classify_score(35)["label"], "TIGHT_RISK")
        self.assertEqual(uls.classify_score(25)["label"], "STRESS")

    def test_report_renders_key_sections(self):
        doc = uls.latest_document(synthetic_frame(), {"WALCL": "2025-02-23"})
        report = uls.render_report(doc)

        self.assertIn("USD Liquidity Composite", report)
        self.assertIn("Core Metrics", report)
        self.assertIn("Component Z-Scores", report)

    def test_stale_or_missing_sources_block_portfolio_interpretation(self):
        requested = dt.date(2026, 7, 10)
        source_dates = {series_id: "2026-06-01" for series_id in uls.SERIES}

        doc = uls.latest_document(
            synthetic_frame(), source_dates, requested_as_of=requested)

        self.assertEqual(doc["dataFreshness"]["status"], "BLOCK")
        self.assertEqual(set(doc["dataFreshness"]["staleSources"]), set(uls.SERIES))
        self.assertTrue(doc["interpretation"]["portfolioAction"].startswith("DATA BLOCK"))

    def test_mixed_frequency_lag_thresholds_allow_current_weekly_and_daily_series(self):
        requested = dt.date(2026, 7, 10)
        frame = synthetic_frame()
        frame.index = pd.date_range(end=requested, periods=len(frame), freq="D")
        source_dates = {
            series_id: ("2026-07-01" if series_id in {"WALCL", "WDTGAL", "WRBWFRBL"}
                        else "2026-07-07")
            for series_id in uls.SERIES
        }

        doc = uls.latest_document(
            frame, source_dates, requested_as_of=requested)

        self.assertEqual(doc["dataFreshness"]["status"], "PASS")
        self.assertEqual(doc["dataFreshness"]["staleSources"], [])

    def test_outputs_are_strict_atomic_and_private(self):
        doc = uls.latest_document(synthetic_frame(), {"WALCL": "2025-02-23"})
        with tempfile.TemporaryDirectory() as td:
            out_json = pathlib.Path(td) / "runtime" / "liquidity.json"
            out_md = pathlib.Path(td) / "runtime" / "liquidity.md"
            uls.write_outputs(doc, out_json, out_md)
            original_json = out_json.read_text(encoding="utf-8")
            original_md = out_md.read_text(encoding="utf-8")

            self.assertEqual(json.loads(original_json)["score"], doc["score"])
            self.assertEqual(out_json.stat().st_mode & 0o777, 0o600)
            self.assertEqual(out_md.stat().st_mode & 0o777, 0o600)
            self.assertEqual(out_json.parent.stat().st_mode & 0o777, 0o700)

            invalid = dict(doc, invalid=float("nan"))
            with self.assertRaises(ValueError):
                uls.write_outputs(invalid, out_json, out_md)
            self.assertEqual(out_json.read_text(encoding="utf-8"), original_json)
            self.assertEqual(out_md.read_text(encoding="utf-8"), original_md)


if __name__ == "__main__":
    unittest.main()

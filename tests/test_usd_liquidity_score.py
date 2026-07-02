import importlib.util
import pathlib
import unittest

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


if __name__ == "__main__":
    unittest.main()

import datetime as dt
import importlib.util
import json
import math
import pathlib
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
DASHBOARD_SCRIPT = ROOT / "scripts" / "market_mass_dashboard.py"
GENERATE_SCRIPT = ROOT / "generate.py"

SPEC = importlib.util.spec_from_file_location("market_mass_dashboard", DASHBOARD_SCRIPT)
mmd = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = mmd
SPEC.loader.exec_module(mmd)

GEN_SPEC = importlib.util.spec_from_file_location("generate", GENERATE_SCRIPT)
generate = importlib.util.module_from_spec(GEN_SPEC)
sys.modules[GEN_SPEC.name] = generate
GEN_SPEC.loader.exec_module(generate)


def synthetic_rows(n=190):
    rows = []
    start = dt.date(2025, 1, 2)
    for i in range(n):
        price = 100.0 + 2.0 * math.sin(i / 6.0) + 0.03 * i
        volume = 1_000_000 * (1.0 + 0.4 * math.sin(i / 11.0))
        rows.append({
            "date": (start + dt.timedelta(days=i)).isoformat(),
            "open": price * 0.998,
            "high": price * 1.006,
            "low": price * 0.994,
            "close": price,
            "volume": volume,
            "dollar_volume": price * volume,
            "volume_proxy_close": price,
            "volume_proxy_volume": volume,
        })
    return rows


def default_profile(history_bars=30):
    return {
        "gravityProfile": "swing",
        "lookback": 84,
        "halfLife": 21,
        "historyBars": history_bars,
        "defaultHorizonDays": 5,
        "defaultConfidence": 0.80,
        "boundaryModel": "mass_vol",
        "horizons": [1, 5],
        "confidences": [0.68, 0.80],
    }


class MarketMassDashboardTests(unittest.TestCase):
    def test_symbol_config_selects_qqq_and_voo_volatility_proxies(self):
        qqq = mmd.build_symbol_config("QQQ", {"QQQ"})
        voo = mmd.build_symbol_config("VOO", set())
        ndx = mmd.build_symbol_config("^NDX", set())
        gspc = mmd.build_symbol_config("^GSPC", set())
        stock = mmd.build_symbol_config("NVDA", {"NVDA"})

        self.assertEqual(qqq.vol_ticker, "^VXN")
        self.assertEqual(qqq.fallback_vol_ticker, "^VIX")
        self.assertIn("holding", qqq.roles)
        self.assertIn("anchor", qqq.roles)

        self.assertEqual(voo.vol_ticker, "^VIX")
        self.assertIsNone(voo.fallback_vol_ticker)
        self.assertIn("anchor", voo.roles)

        self.assertIn("reference", ndx.roles)
        self.assertEqual(ndx.volume_ticker, "QQQ")
        self.assertEqual(ndx.price_ticker, "^NDX")
        self.assertIn("reference", gspc.roles)
        self.assertEqual(gspc.volume_ticker, "SPY")
        self.assertEqual(gspc.price_ticker, "^GSPC")

        self.assertIsNone(stock.vol_ticker)
        self.assertEqual(stock.fallback_vol_ticker, "^VIX")
        self.assertTrue(any("broad-market ^VIX proxy" in w for w in stock.warnings))

    def test_build_symbol_payload_contains_current_boundaries_and_history(self):
        rows = synthetic_rows()
        config = mmd.build_symbol_config("QQQ", {"QQQ"})
        profile = default_profile()
        generated_at = dt.datetime.combine(dt.date.fromisoformat(rows[-1]["date"]), dt.time(21), tzinfo=dt.timezone.utc)

        payload = mmd.build_symbol_payload(
            config,
            rows,
            "synthetic",
            vol_series={rows[-1]["date"]: 22.0},
            vol_source="synthetic VXN",
            fallback_vol_series={rows[-1]["date"]: 18.0},
            fallback_vol_source="synthetic VIX",
            profile=profile,
            generated_at=generated_at,
        )

        self.assertEqual(payload["priceTicker"], "QQQ")
        for key in ("current", "history", "boundaries", "warnings", "role", "priceTicker"):
            self.assertIn(key, payload)
        self.assertIn("current_price", payload["current"])
        self.assertIn("selected_boundary", payload["current"])
        self.assertEqual(payload["current"]["selected_boundary"]["horizon_days"], 5)
        self.assertIn("pyramid", payload)
        self.assertIn("massHealth", payload["pyramid"])
        self.assertIn(payload["pyramid"]["massHealth"]["label"], {
            "coherent_mass",
            "working_mass",
            "fragile_or_transition",
            "low_friction_or_no_mass",
        })
        self.assertIn(payload["pyramid"]["massHealth"]["frictionLabel"], {
            "strong_friction",
            "friction_present",
            "weak_friction",
            "low_friction",
            "low_friction_escape_risk",
        })
        self.assertIn("agreement", payload["pyramid"])
        self.assertGreaterEqual(payload["pyramid"]["agreement"]["centerSpreadPct"], 0)
        self.assertGreaterEqual(payload["pyramid"]["agreement"]["centerDisagreementZ"], 0)
        self.assertEqual(set(payload["pyramid"]["profiles"]), {"tactical", "swing", "structural"})
        self.assertEqual(len(payload["history"]), 30)
        self.assertEqual(payload["history"], sorted(payload["history"], key=lambda r: r["date"]))
        self.assertIn("lower_boundary", payload["history"][-1])
        self.assertIn("upper_boundary", payload["history"][-1])
        self.assertFalse(payload["stale"])
        self.assertEqual(payload["priceAsOf"], rows[-1]["date"])
        self.assertEqual(payload["massAsOf"], rows[-1]["date"])
        self.assertIn(payload["dashboardConfidence"]["label"], {"high", "medium", "low"})

    def test_stale_symbol_payload_reports_freshness_and_confidence_reasons(self):
        rows = synthetic_rows()
        config = mmd.build_symbol_config("QQQ", {"QQQ"})
        generated_at = dt.datetime.combine(
            dt.date.fromisoformat(rows[-1]["date"]) + dt.timedelta(days=10),
            dt.time(21),
            tzinfo=dt.timezone.utc,
        )

        payload = mmd.build_symbol_payload(
            config,
            rows,
            "synthetic",
            vol_series={rows[-1]["date"]: 22.0},
            profile=default_profile(history_bars=10),
            generated_at=generated_at,
            max_stale_calendar_days=4,
        )

        self.assertTrue(payload["stale"])
        self.assertIn("calendar days old", payload["staleReason"])
        self.assertTrue(any("stale price data" in r for r in payload["dashboardConfidence"]["reasons"]))

    def test_dashboard_payload_shape(self):
        generated_at = dt.datetime(2026, 6, 27, tzinfo=dt.timezone.utc)
        payload = mmd.build_dashboard_payload(
            {"QQQ": {"current": {"current_price": 100}}},
            {"gravityProfile": "swing", "lookback": 84, "halfLife": 21},
            generated_at=generated_at,
            universe_mode="anchor_first",
            portfolio_path="/tmp/portfolio.csv",
        )

        self.assertEqual(payload["generatedAt"], generated_at.isoformat())
        self.assertEqual(payload["universeMode"], "anchor_first")
        self.assertEqual(payload["portfolioPath"], "/tmp/portfolio.csv")
        self.assertEqual(payload["profile"]["gravityProfile"], "swing")
        self.assertIn("QQQ", payload["symbols"])
        self.assertIn("Probabilistic research model", payload["disclaimer"])

    def test_resolve_symbols_supports_anchor_first_mode(self):
        args = mmd.parse_args(["--anchor-only"])
        symbols, _held, _portfolio, _warnings, mode = mmd.resolve_symbols(args)

        self.assertEqual(mode, "anchor_first")
        self.assertIn("QQQ", symbols)
        self.assertIn("VOO", symbols)
        self.assertIn("^NDX", symbols)
        self.assertIn("^GSPC", symbols)

    def test_parse_pyramid_profiles_dedupes_and_validates(self):
        self.assertEqual(mmd.parse_pyramid_profiles("tactical,swing,tactical"), ["tactical", "swing"])
        with self.assertRaises(mmd.argparse.ArgumentTypeError):
            mmd.parse_pyramid_profiles("bad_profile")

    def test_rendered_dashboard_contains_market_mass_hooks(self):
        html = generate.render_html({
            "summary": {},
            "stocks": [],
            "options": [],
            "series": [],
            "marketMass": {"generatedAt": "now", "symbols": {"QQQ": {}}},
        })

        self.assertIn("重心边界", html)
        self.assertIn("Boundary Table", html)
        self.assertIn("marketMass", html)
        self.assertIn("Dashboard confidence", html)
        self.assertIn("Mass health", html)
        self.assertIn("Pyramid mass check", html)

    def test_generate_loader_handles_missing_malformed_and_valid_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(generate.load_market_mass_dashboard(tmp))

            path = pathlib.Path(tmp) / "market_mass_dashboard.json"
            path.write_text("{bad json", encoding="utf-8")
            self.assertIsNone(generate.load_market_mass_dashboard(tmp))

            good = {"generatedAt": "now", "symbols": {"QQQ": {}}}
            path.write_text(json.dumps(good), encoding="utf-8")
            self.assertEqual(generate.load_market_mass_dashboard(tmp), good)


if __name__ == "__main__":
    unittest.main()

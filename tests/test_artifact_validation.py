import json
import tempfile
import unittest
from pathlib import Path

from scripts import artifact_validation as artifacts


class ArtifactValidationTests(unittest.TestCase):
    def test_current_schema_requires_timezone_aware_generated_timestamp(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "financial_status.json"
            path.write_text(json.dumps({
                "schemaVersion": 1, "generatedAt": "2026-07-09T12:00:00",
                "asOfDate": "2026-07-09", "scores": [], "summary": {},
                "researchOnly": True, "decisionGrade": False,
            }), encoding="utf-8")

            doc, health = artifacts.load_artifact(td, "financialStatus", "2026-07-09")

            self.assertIsNone(doc)
            self.assertEqual(health["status"], "invalid")
            self.assertIn("UTC offset", health["reason"])

    def test_missing_and_wrong_schema_are_not_embedded(self):
        with tempfile.TemporaryDirectory() as td:
            doc, health = artifacts.load_artifact(td, "financialStatus", "2026-07-09")
            self.assertIsNone(doc)
            self.assertEqual(health["status"], "missing")

            path = Path(td) / "financial_status.json"
            path.write_text(json.dumps({
                "schemaVersion": 99, "generatedAt": "2026-07-09T20:00:00Z",
                "asOfDate": "2026-07-09", "scores": [], "summary": {},
                "researchOnly": True, "decisionGrade": False,
            }), encoding="utf-8")
            doc, health = artifacts.load_artifact(td, "financialStatus", "2026-07-09")
            self.assertIsNone(doc)
            self.assertEqual(health["status"], "invalid")
            self.assertIn("unsupported schemaVersion", health["reason"])

    def test_nonfinite_and_future_artifacts_are_blocked(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "close_vs_intraday.json"
            path.write_text(
                '{"schemaVersion":1,"generatedAt":"2026-07-10T00:00:00Z",'
                '"asOf":"2026-07-10","windows":{},"bad":NaN}', encoding="utf-8")
            doc, health = artifacts.load_artifact(td, "closeVsIntraday", "2026-07-09")
            self.assertIsNone(doc)
            self.assertEqual(health["status"], "invalid")

            path.write_text(json.dumps({
                "schemaVersion": 1, "generatedAt": "2026-07-10T00:00:00Z",
                "asOf": "2026-07-10", "windows": {},
                "researchOnly": True, "decisionGrade": False,
            }), encoding="utf-8")
            doc, health = artifacts.load_artifact(td, "closeVsIntraday", "2026-07-09")
            self.assertIsNone(doc)
            self.assertIn("after the portfolio price date", health["reason"])

    def test_stale_artifact_remains_visible_but_not_decision_grade(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "close_vs_intraday.json"
            path.write_text(json.dumps({
                "schemaVersion": 1, "generatedAt": "2026-06-01T00:00:00Z",
                "asOf": "2026-06-01", "windows": {}, "stocks": [1, 2, 3],
                "researchOnly": True, "decisionGrade": False,
            }), encoding="utf-8")
            doc, health = artifacts.load_artifact(td, "closeVsIntraday", "2026-07-09")
            self.assertIsNotNone(doc)
            self.assertEqual(health["status"], "stale")
            self.assertFalse(health["decisionGrade"])
            self.assertNotIn("stocks", doc)

    def test_ai_watchlist_compactor_removes_duplicate_research_queue(self):
        row = {"ticker": "QQQ", "finalScore": 80, "dataQuarantined": False}
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "ai_watchlist.json"
            path.write_text(json.dumps({
                "schemaVersion": 1, "generatedAt": "2026-07-09T12:00:00-07:00",
                "marketDataAsOf": "2026-07-09", "scores": [row], "modelCard": {},
                "researchOnly": True, "decisionGrade": False,
                "queues": {"researchQueue": [row], "entryQueue": [row]},
                "summary": {"researchQueue": [row] * 20},
            }), encoding="utf-8")
            doc, health = artifacts.load_artifact(td, "aiWatchlist", "2026-07-09")
        self.assertEqual(health["status"], "fresh")
        self.assertNotIn("researchQueue", doc["queues"])
        self.assertEqual(doc["queues"]["entryQueue"], [row])
        self.assertEqual(len(doc["summary"]["researchQueue"]), 10)

    def test_market_mass_compactor_redacts_path_and_downsamples_history(self):
        history = [{"date": f"2026-01-{(i % 28) + 1:02d}", "close": i + 1,
                    "regime": "a" if i < 65 else "b"} for i in range(130)]
        item = {
            "priceAsOf": "2026-07-09", "stale": False,
            "freshness": {"generatedAt": "2026-07-09T20:00:00Z"},
            "dashboardConfidence": {"label": "high", "components": {"private": 1}},
            "current": {"current_price": 100, "center_price": 99, "quality_score": 80,
                        "distance_z": 0.1, "regime": "active_center",
                        "volatility_build_up_score": 20,
                        "selected_boundary": {"lower_boundary": 90, "upper_boundary": 110},
                        "profile_nodes": {"unused": True}},
            "history": history,
            "pyramid": {"profiles": {}, "agreement": {}, "massHealth": {}},
        }
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "market_mass_dashboard.json"
            path.write_text(json.dumps({
                "schemaVersion": 1, "generatedAt": "2026-07-09T20:00:00Z",
                "researchOnly": True, "decisionGrade": False,
                "profile": {}, "portfolioPath": "/private/broker.csv", "symbols": {"QQQ": item},
            }), encoding="utf-8")
            doc, health = artifacts.load_artifact(td, "marketMass", "2026-07-09")
        self.assertEqual(health["status"], "fresh")
        self.assertNotIn("portfolioPath", doc)
        self.assertNotIn("profile_nodes", doc["symbols"]["QQQ"]["current"])
        self.assertLess(len(doc["symbols"]["QQQ"]["history"]), len(history))
        self.assertIn("b", {row["regime"] for row in doc["symbols"]["QQQ"]["history"]})

    def test_market_mass_unavailable_symbol_is_provisional_not_stale(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "market_mass_dashboard.json"
            path.write_text(json.dumps({
                "schemaVersion": 1, "generatedAt": "2026-07-09T20:00:00Z",
                "researchOnly": True, "decisionGrade": False, "profile": {},
                "symbols": {
                    "QQQ": {"priceAsOf": "2026-07-09", "current": {"current_price": 1},
                            "history": [], "stale": False},
                    "NEW": {"current": None, "history": [], "stale": False,
                            "dataStatus": "unavailable"},
                },
            }), encoding="utf-8")

            _, health = artifacts.load_artifact(td, "marketMass", "2026-07-09")

        self.assertEqual("provisional", health["status"])
        self.assertEqual(0, health["staleSymbolCount"])
        self.assertEqual(1, health["unavailableSymbolCount"])
        self.assertIn("insufficient-history", health["reason"])

    def test_counterfactual_compactor_removes_internal_ids_and_redundant_values(self):
        raw = {"aggregate": {"score": 50}, "mode": "test", "events": [{
            "id": "one", "legIds": ["private-internal-id"],
            "legs": [{"sym": "QQQ"}] * 12,
            "series": [{"date": f"2026-01-{(i % 28) + 1:02d}", "actual": 100 + i,
                        "actualPct": i, "alt": 100, "altPct": 0, "delta": i} for i in range(60)],
        }]}
        compact = artifacts.compact_counterfactual(raw)
        event = compact["events"][0]
        self.assertNotIn("legIds", event)
        self.assertEqual(len(event["legs"]), 10)
        self.assertLessEqual(len(event["series"]), 48)
        self.assertEqual(set(event["series"][0]), {"date", "actualPct", "altPct"})

    def test_insufficient_aics_history_is_provisional(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "aics.json"
            path.write_text(json.dumps({
                "schemaVersion": 1, "generatedAt": "2026-07-09T12:00:00-07:00",
                "marketDataAsOf": "2026-07-09", "scores": [], "modelCard": {},
                "researchOnly": True, "decisionGrade": False,
                "backtest": {"historyValidation": {"status": "insufficient_history"}},
            }), encoding="utf-8")
            doc, health = artifacts.load_artifact(td, "aics", "2026-07-09")
        self.assertIsNotNone(doc)
        self.assertEqual(health["status"], "provisional")
        self.assertFalse(health["decisionGrade"])
        self.assertIn("insufficient_history", health["reason"])

    def test_momentum_artifact_is_validated_as_fresh_but_never_decision_grade(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "momentum_top3" / "momentum_top3.json"
            path.parent.mkdir()
            path.write_text(json.dumps({
                "schemaVersion": 2,
                "generated_at": "2026-07-09T12:00:00-07:00",
                "price_freshness": {"as_of": "2026-07-09"},
                "window": {"start": "2018-07-09", "end": "2026-07-09"},
                "strategies": [],
                "researchOnly": True,
                "decisionGrade": False,
                "methodology": {
                    "fillTiming": "next trading session close",
                    "initialCapitalAnchor": True,
                    "currentUniverseSurvivorship": True,
                    "inSampleStrategySelection": True,
                    "outOfSampleValidation": False,
                },
            }), encoding="utf-8")

            doc, health = artifacts.load_artifact(td, "momentumTop3", "2026-07-09")

        self.assertIsNotNone(doc)
        self.assertEqual(health["status"], "fresh")
        self.assertFalse(health["decisionGrade"])
        self.assertIn("research-only", health["reason"])

    def test_semi_leverage_artifact_is_validated_as_research_only(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "semi_leverage_tracker.json"
            path.write_text(json.dumps({
                "schemaVersion": 2,
                "generatedAt": "2026-07-09T20:00:00Z",
                "asOf": "2026-07-09",
                "researchOnly": True,
                "decisionGrade": False,
                "korea": {},
                "unitedStates": {},
                "prices": {},
                "analysis": {},
            }), encoding="utf-8")

            doc, health = artifacts.load_artifact(td, "semiLeverage", "2026-07-09")

        self.assertIsNotNone(doc)
        self.assertEqual("fresh", health["status"])
        self.assertFalse(health["decisionGrade"])
        self.assertIn("research-only", health["reason"])

    def test_fresh_artifact_requires_an_explicit_true_decision_grade_contract(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "financial_status.json"
            path.write_text(json.dumps({
                "schemaVersion": 1, "generatedAt": "2026-07-09T20:00:00Z",
                "asOfDate": "2026-07-09", "scores": [], "summary": {},
                "researchOnly": False, "decisionGrade": True,
            }), encoding="utf-8")
            _, health = artifacts.load_artifact(td, "financialStatus", "2026-07-09")
            self.assertTrue(health["decisionGrade"])

            path.write_text(json.dumps({
                "schemaVersion": 1, "generatedAt": "2026-07-09T20:00:00Z",
                "asOfDate": "2026-07-09", "scores": [], "summary": {},
                "researchOnly": True, "decisionGrade": False,
            }), encoding="utf-8")
            _, health = artifacts.load_artifact(td, "financialStatus", "2026-07-09")
            self.assertFalse(health["decisionGrade"])
            self.assertIn("research-only", health["reason"])

    def test_momentum_artifact_rejects_old_or_decision_grade_contracts(self):
        base = {
            "generated_at": "2026-07-09T12:00:00-07:00",
            "price_freshness": {"as_of": "2026-07-09"},
            "window": {}, "strategies": [],
            "researchOnly": True, "decisionGrade": False,
            "methodology": {
                "fillTiming": "next trading session close",
                "initialCapitalAnchor": True,
                "currentUniverseSurvivorship": True,
                "inSampleStrategySelection": True,
                "outOfSampleValidation": False,
            },
        }
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "momentum_top3" / "momentum_top3.json"
            path.parent.mkdir()
            path.write_text(json.dumps({**base, "schemaVersion": 1}), encoding="utf-8")
            doc, health = artifacts.load_artifact(td, "momentumTop3", "2026-07-09")
            self.assertIsNone(doc)
            self.assertIn("unsupported schemaVersion", health["reason"])

            path.write_text(json.dumps({**base, "schemaVersion": 2, "decisionGrade": True}), encoding="utf-8")
            doc, health = artifacts.load_artifact(td, "momentumTop3", "2026-07-09")
            self.assertIsNone(doc)
            self.assertIn("research-only", health["reason"])

    def test_memory_flow_is_visible_but_research_only_when_direct_feeds_are_missing(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "memory_flow.json"
            path.write_text(json.dumps({
                "schemaVersion": 1,
                "generatedAt": "2026-07-09T20:00:00-07:00",
                "asOf": "2026-07-09",
                "decisionGrade": False,
                "researchOnly": True,
                "hypotheses": [],
                "symbols": {},
                "methodology": {},
                "dataGaps": [{"id": "dealer_inventory"}],
            }), encoding="utf-8")

            doc, health = artifacts.load_artifact(td, "memoryFlow", "2026-07-09")

        self.assertIsNotNone(doc)
        self.assertEqual("provisional", health["status"])
        self.assertFalse(health["decisionGrade"])
        self.assertIn("research-only", health["reason"])


if __name__ == "__main__":
    unittest.main()

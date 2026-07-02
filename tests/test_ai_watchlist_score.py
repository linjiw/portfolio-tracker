import importlib.util
import pathlib
import sys
import unittest


SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "ai_watchlist_score.py"
SPEC = importlib.util.spec_from_file_location("ai_watchlist_score", SCRIPT)
watch = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = watch
SPEC.loader.exec_module(watch)


class AiWatchlistScoreTests(unittest.TestCase):
    def test_universe_contains_research_names(self):
        rows = watch.load_universe(watch.DEFAULT_UNIVERSE)
        tickers = {row["ticker"] for row in rows}

        for ticker in {"QCOM", "2454.TW", "ARM", "SONY", "AMBA", "SYNA", "MRVL", "ALAB", "CRDO", "VRT", "CDNS", "SNPS", "APP", "009150.KS", "4062.T", "2802.T", "ON", "LSCC", "CGNX", "TTD"}:
            self.assertIn(ticker, tickers)

    def test_structural_score_uses_watchlist_weights(self):
        factors = {
            "bottleneckFit": 100,
            "proofPoints": 80,
            "monetizationPath": 60,
            "underappreciation": 40,
            "executionRiskControl": 20,
        }

        self.assertEqual(watch.structural_score(factors), 68)

    def test_entry_gate_routes_strong_broken_trend_to_wait_reset(self):
        gate, note, reasons = watch.entry_gate_for(
            setup_score=84,
            structural=86,
            risk_score=65,
            metrics={"available": True, "vs200": -6.0, "vs50": -2.0, "rsi14": 45.0},
            dq_score=100,
            dq_reasons=[],
            portfolio_penalty=0,
            portfolio_reasons=[],
        )

        self.assertEqual(gate, "WAIT_RESET")
        self.assertIn("Trend repair", note)
        self.assertEqual(reasons[0]["rule"], "trend_break")

    def test_tier_one_research_gate_builds_model(self):
        gate, tone, note, reasons = watch.research_gate_for(
            priority_score=81,
            structural=74,
            evidence=82,
            tier="T1 immediate deep dive",
            dq_score=100,
            dq_reasons=[],
        )

        self.assertEqual(gate, "BUILD_MODEL")
        self.assertEqual(tone, "good")
        self.assertIn("one-page", note)
        self.assertEqual(reasons[0]["rule"], "tier1_priority")

    def test_build_scores_structural_only_has_required_fields(self):
        universe = watch.load_universe(watch.DEFAULT_UNIVERSE)
        rows, latest = watch.build_scores(universe, prices=None, exposures={}, profiles={})

        self.assertIsNone(latest)
        self.assertGreaterEqual(len(rows), 60)
        self.assertTrue(all("finalScore" in row for row in rows))
        self.assertTrue(all("entryGate" in row for row in rows))
        self.assertTrue(all("evidenceScore" in row for row in rows))
        self.assertTrue(all("evidenceLedger" in row for row in rows))
        self.assertTrue(all("evidenceAudit" in row for row in rows))
        self.assertTrue(all("entryDiagnostics" in row for row in rows))
        self.assertTrue(all("crowdingRisk" in row for row in rows))
        self.assertTrue(all("instrumentMaster" in row for row in rows))
        self.assertTrue(all("actionTier" in row for row in rows))
        self.assertTrue(all("modelWorkstream" in row for row in rows))
        self.assertTrue(all("queue" in row for row in rows))
        self.assertTrue(all("clusterRole" in row for row in rows))
        self.assertTrue(all("priorityTier" in row for row in rows))
        self.assertTrue(all("stage" in row for row in rows))
        self.assertTrue(all("direction" in row for row in rows))
        self.assertTrue(all("catalysts" in row for row in rows))
        self.assertTrue(all("rebuttalChecks" in row for row in rows))
        self.assertTrue(all("universePercentile" in row for row in rows))
        self.assertTrue(all("bucketPercentile" in row for row in rows))
        self.assertTrue(all("directionRelativeScore" in row for row in rows))
        self.assertTrue(all(row["gate"] == "DATA_REVIEW" for row in rows))
        self.assertTrue(all(row["entryGate"] == "BLOCK_DATA" for row in rows))
        self.assertTrue(all(row["setupFrozen"] for row in rows))
        self.assertTrue(all(row["queue"] == "Data Review Queue" for row in rows))
        self.assertTrue(all(row["actionTier"] == "DQ Data Quarantine" for row in rows))
        self.assertTrue(all((row["evidenceLedger"][0] or {}).get("sourceDate") for row in rows))

    def test_summary_counts_all_rows(self):
        universe = watch.load_universe(watch.DEFAULT_UNIVERSE)
        rows, _ = watch.build_scores(universe, prices=None, exposures={}, profiles={})
        summary = watch.summarize(rows)

        self.assertEqual(sum(summary["gateCounts"].values()), len(rows))
        self.assertEqual(sum(summary["entryGateCounts"].values()), len(rows))
        self.assertEqual(summary["queueCounts"]["dataReview"], len(rows))
        self.assertEqual(summary["queueCounts"]["research"], 0)
        self.assertEqual(summary["queueCounts"]["entry"], 0)
        self.assertTrue(summary["byBucket"])
        self.assertTrue(summary["byStage"])
        self.assertTrue(summary["byDirection"])
        self.assertTrue(summary["byPriorityTier"])

    def test_queue_payload_separates_quarantine(self):
        universe = watch.load_universe(watch.DEFAULT_UNIVERSE)
        rows, _ = watch.build_scores(universe, prices=None, exposures={}, profiles={})
        queues = watch.queue_payload(rows)

        self.assertEqual(len(queues["dataReviewQueue"]), len(rows))
        self.assertFalse(queues["researchQueue"])
        self.assertFalse(queues["entryQueue"])

    def test_model_card_tracks_audit_coverage(self):
        universe = watch.load_universe(watch.DEFAULT_UNIVERSE)
        rows, latest = watch.build_scores(universe, prices=None, exposures={}, profiles={})
        card = watch.model_card(rows, latest, watch.DEFAULT_UNIVERSE)

        self.assertEqual(card["modelVersion"], "0.3.1")
        self.assertEqual(card["dataQuarantineCount"], len(rows))
        self.assertEqual(card["ledgerSchemaCoverage"], 100.0)
        self.assertEqual(card["sourceUrlCoverage"], 0.0)
        self.assertEqual(card["sourceUrlAttachedCount"], 0)
        self.assertEqual(card["sourceUrlRequiredCount"], len(rows))
        self.assertGreater(card["evidenceConfidenceCappedCount"], 0)
        self.assertEqual(card["instrumentMasterCoverage"], 100.0)

    def test_direction_relative_score_requires_three_peers(self):
        universe = watch.load_universe(watch.DEFAULT_UNIVERSE)
        rows, _ = watch.build_scores(universe, prices=None, exposures={}, profiles={})
        by_ticker = {row["ticker"]: row for row in rows}

        self.assertEqual(by_ticker["2454.TW"]["directionPeerGroupStatus"], "singleton_no_peer_group")
        self.assertIsNone(by_ticker["2454.TW"]["directionRelativeScore"])
        self.assertEqual(by_ticker["2454.TW"]["directionRankLabel"], "Singleton / no peer group")
        self.assertEqual(by_ticker["QCOM"]["directionPeerGroupStatus"], "peer_percentile")

    def test_verify_before_entry_blocks_entry_without_quarantine(self):
        metrics = {"available": True, "priceTo252dMedian": 4.5, "ret3m": 40.0, "marketCapUsd": 10_000_000_000}
        reasons = [{"rule": "price_band_outlier", "detail": "Price is 4.50x trailing median."}]
        severity = watch.data_quality_severity(metrics, 82, reasons)
        crowd = watch.crowding_risk(metrics, "T2 quality discovered", "DEEP_DIVE")
        eligibility = watch.entry_eligibility(severity, crowd, metrics)
        gate, note, _ = watch.entry_gate_for(
            setup_score=80,
            structural=80,
            risk_score=70,
            metrics={**metrics, "vs50": 5.0, "vs200": 20.0, "rsi14": 55.0},
            dq_score=82,
            dq_reasons=reasons,
            portfolio_penalty=0,
            portfolio_reasons=[],
            dq_severity=severity,
            action_ready_allowed=eligibility["actionReadyAllowed"],
        )

        self.assertEqual(severity, "verify_before_entry")
        self.assertFalse(eligibility["entryAllowed"])
        self.assertEqual(gate, "VERIFY_DATA")
        self.assertIn("Verify", note)


if __name__ == "__main__":
    unittest.main()

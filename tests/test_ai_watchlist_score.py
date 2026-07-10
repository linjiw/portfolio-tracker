import importlib.util
import json
import pathlib
import sys
import tempfile
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

    def test_universe_rejects_invalid_factor_and_unknown_categories(self):
        base = {
            "companyId": "bad",
            "ticker": "BAD",
            "name": "Bad Co",
            "bucket": "Other",
            "factors": {
                "bottleneckFit": 101,
                "proofPoints": 50,
                "monetizationPath": 50,
                "underappreciation": 50,
                "executionRiskControl": 50,
            },
            "priorityTier": "T1 immediate deep dive",
            "evidenceLevel": "credible_product",
        }
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "universe.json"
            path.write_text(json.dumps([base]), encoding="utf-8")
            with self.assertRaises(ValueError):
                watch.load_universe(path)

            base["factors"]["bottleneckFit"] = 50
            base["evidenceLevel"] = "made_up_evidence"
            path.write_text(json.dumps([base]), encoding="utf-8")
            with self.assertRaises(ValueError):
                watch.load_universe(path)

            base["evidenceLevel"] = "credible_product"
            base["aliases"] = "NOT_A_LIST"
            path.write_text(json.dumps([base]), encoding="utf-8")
            with self.assertRaises(ValueError):
                watch.load_universe(path)

    def test_unsourced_evidence_label_is_numerically_capped(self):
        item = {
            "ticker": "AAA",
            "thesis": "Claim",
            "evidenceLevel": "major_customer_deployment",
            "conviction": "high",
        }
        ledger = watch.normalize_evidence_ledger(item)
        assessment = watch.evidence_assessment(ledger, item["evidenceLevel"], "2026-07-09")

        self.assertEqual(assessment["rawScore"], 92)
        self.assertEqual(assessment["effectiveScore"], 64)
        self.assertTrue(assessment["scoreCapApplied"])
        self.assertFalse(assessment["sourceComplete"])
        self.assertFalse(assessment["decisionGrade"])

    def test_expired_evidence_is_capped_even_with_valid_url(self):
        ledger = [
            {
                "sourceUrl": "https://example.com/filing",
                "sourceDate": "2025-01-01",
                "expiresAfter": "2025-12-31",
                "needsRefresh": False,
                "verified": True,
            }
        ]
        assessment = watch.evidence_assessment(ledger, "revenue_guided", "2026-07-09")
        self.assertEqual(assessment["effectiveScore"], 54)
        self.assertEqual(assessment["expiredCount"], 1)
        self.assertFalse(assessment["decisionGrade"])

    def test_research_score_components_reconcile_without_rounding_drift(self):
        factors = {"underappreciation": 77, "proofPoints": 83}
        components = watch.research_priority_components(81, 64, 72, factors, "T1 immediate deep dive")
        expected = 81 * 0.48 + 64 * 0.20 + 77 * 0.17 + 83 * 0.10 + 72 * 0.05 + 6
        self.assertEqual(components["rawScore"], round(expected, 3))
        self.assertEqual(components["finalScore"], round(expected))

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

        self.assertEqual(card["modelVersion"], "0.4.0")
        self.assertEqual(card["dataQuarantineCount"], len(rows))
        self.assertEqual(card["ledgerSchemaCoverage"], 100.0)
        self.assertEqual(card["sourceUrlCoverage"], 0.0)
        self.assertEqual(card["sourceUrlAttachedCount"], 0)
        self.assertEqual(card["sourceUrlRequiredCount"], len(rows))
        self.assertGreater(card["evidenceConfidenceCappedCount"], 0)
        self.assertGreater(card["evidenceScoreCappedCount"], 0)
        self.assertLess(card["evidenceScoreCappedCount"], len(rows))
        self.assertEqual(card["evidenceDecisionGradeCount"], 0)
        self.assertEqual(card["instrumentMasterCoverage"], 100.0)

    def test_instrument_master_preserves_market_cap_point_in_time_audit(self):
        metrics = watch.aiq.market_profile_fields({
            "currency": "USD",
            "marketCapUsd": 10_000_000_000,
            "marketCapAsOf": "2026-07-10",
            "marketCapAsOfSource": "fetch_date_fallback",
            "marketCapQuoteAsOf": "2026-07-09",
            "marketCapDecisionAsOf": "2026-07-09",
            "marketCapAlignmentReason": "historical_share_count_unverified",
            "marketCapFetchedAt": "2026-07-10T08:00:00-07:00",
            "marketCapPointInTimeCompatible": False,
            "shareCountPointInTimeVerified": False,
        })

        master = watch.instrument_master({"ticker": "AAA"}, metrics, "verify_before_entry", [])

        self.assertEqual(master["marketCapUsd"], 10_000_000_000)
        self.assertIsNone(master["scoringMarketCapUsd"])
        self.assertEqual(master["marketCapQuoteAsOf"], "2026-07-09")
        self.assertEqual(master["marketCapAlignmentReason"], "historical_share_count_unverified")
        self.assertFalse(master["marketCapPointInTimeCompatible"])

    def test_direction_relative_score_requires_three_peers(self):
        universe = watch.load_universe(watch.DEFAULT_UNIVERSE)
        rows, _ = watch.build_scores(universe, prices=None, exposures={}, profiles={})
        by_ticker = {row["ticker"]: row for row in rows}

        self.assertEqual(by_ticker["2454.TW"]["directionPeerGroupStatus"], "data_quarantined_not_ranked")
        self.assertIsNone(by_ticker["2454.TW"]["directionRelativeScore"])
        self.assertEqual(by_ticker["2454.TW"]["directionRankLabel"], "Data review / not ranked")
        self.assertEqual(by_ticker["QCOM"]["directionPeerGroupStatus"], "data_quarantined_not_ranked")

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

    def test_limited_history_blocks_entry_review(self):
        metrics = {
            "available": True,
            "historyBars": 35,
            "vs50": None,
            "vs200": None,
            "marketCapUsd": 10_000_000_000,
        }
        reasons = [{"rule": "limited_price_history", "detail": "Only 35 bars."}]
        severity = watch.data_quality_severity(metrics, 80, reasons)
        eligibility = watch.entry_eligibility(severity, {}, metrics)
        self.assertEqual(severity, "verify_before_entry")
        self.assertFalse(eligibility["entryAllowed"])
        self.assertIn("50/200DMA history incomplete", eligibility["actionReadyBlockers"])

    def test_price_position_is_not_mislabeled_as_fundamental_valuation(self):
        diagnostics = watch.entry_diagnostics(
            {"priceTo252dMedian": 1.9, "medianLookbackBars": 200, "ret3m": 10, "vol1y": 30},
            "clean",
        )
        self.assertEqual(diagnostics["valuationBand"], "not_measured")
        self.assertEqual(diagnostics["pricePositionBand"], "above_trailing_median")
        self.assertIn("not fundamental valuation", diagnostics["valuationDataStatus"])
        self.assertEqual(diagnostics["postEarningsGapRisk"], "not_measured")
        self.assertEqual(diagnostics["priceVolatilityRisk"], "medium")


if __name__ == "__main__":
    unittest.main()

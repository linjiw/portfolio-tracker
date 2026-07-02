import importlib.util
import pathlib
import sys
import unittest


SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "ai_semi_quant.py"
SPEC = importlib.util.spec_from_file_location("ai_semi_quant", SCRIPT)
aiq = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = aiq
SPEC.loader.exec_module(aiq)


class AiSemiQuantTests(unittest.TestCase):
    def test_structural_score_excludes_torque_overlay(self):
        factors = {
            "pricingPower": 100,
            "profitElasticity": 80,
            "capexConversion": 60,
            "valuationGrowth": 40,
            "sizeGrowthTorque": 80,
        }

        self.assertEqual(aiq.structural_score(factors), 72)

        factors["sizeGrowthTorque"] = 10
        self.assertEqual(aiq.structural_score(factors), 72)

    def test_size_growth_torque_rewards_smaller_market_cap_with_quality(self):
        company = next(c for c in aiq.UNIVERSE if c.ticker == "AMKR")

        small = aiq.size_growth_torque(company, 12_000_000_000)
        mega = aiq.size_growth_torque(company, 1_800_000_000_000)

        self.assertGreater(small, mega)

    def test_torque_overlay_is_bonus_not_structural_score(self):
        company = next(c for c in aiq.UNIVERSE if c.ticker == "AMKR")
        factors = dict(company.factors)
        factors["sizeGrowthTorque"] = 92

        adjusted, bonus, fragility = aiq.torque_overlay(
            company,
            factors,
            {"available": True, "marketCapUsd": 12_000_000_000, "vol1y": 35, "vs50": 5, "vs200": 8, "ret3m": 20, "rsi14": 55},
        )

        self.assertGreaterEqual(bonus, 1)
        self.assertGreaterEqual(adjusted, aiq.structural_score(factors) - fragility)

    def test_research_gate_blocks_broken_trend(self):
        gate, tone, note, reasons = aiq.research_gate(
            final_score=91,
            standalone_score=91,
            base_score=90,
            trend_score=70,
            risk_score=65,
            metrics={"available": True, "vs200": -5.0, "vs50": -2.0, "rsi14": 45.0},
            factors={"valuationGrowth": 80},
            dq_score=100,
            dq_reasons=[],
            portfolio_penalty=0,
            penalty_reasons=[],
        )

        self.assertEqual(gate, "WATCH_RESET")
        self.assertEqual(tone, "caution")
        self.assertIn("trend repair", note)
        self.assertTrue(reasons)

    def test_structural_only_build_has_required_sections(self):
        rows, latest = aiq.build_scores(prices=None, exposures={})
        doc = {
            "generatedAt": "2026-06-22T00:00:00-07:00",
            "marketDataAsOf": latest,
            "scores": rows,
            "summary": aiq.summarize(rows),
            "modelCard": aiq.model_card(rows, latest),
            "capitalWaterfall": list(aiq.CAPITAL_WATERFALL),
            "sources": list(aiq.SOURCE_LINKS),
        }
        report = aiq.render_report(doc)

        self.assertGreaterEqual(len(rows), 10)
        self.assertIn("TSM", {row["ticker"] for row in rows})
        self.assertTrue(all("universePercentile" in row for row in rows))
        self.assertTrue(all("torqueAdjustedScore" in row for row in rows))
        self.assertIn("AI-SemiQuant Reference Report", report)
        self.assertIn("Capital Waterfall", report)

    def test_small_peer_group_percentile_display_is_not_rank_like(self):
        rows, _ = aiq.build_scores(prices=None, exposures={})
        tsm = next(row for row in rows if row["ticker"] == "TSM")

        self.assertEqual(tsm["peerGroupSize"], 1)
        self.assertEqual(tsm["peerPercentileDisplay"], "N/A · n=1")

    def test_allow_dd_reason_includes_peer_confirmation_when_available(self):
        rows = [
            {
                "ticker": "A",
                "finalScore": 69,
                "strategicRankScore": 83,
                "tacticalScore": 50,
                "peerGroup": "HBM Equipment",
                "gate": "ALLOW_DD",
                "gateReasons": [{"rule": "dd_candidate", "detail": "old reason"}],
            },
            {"ticker": "B", "finalScore": 60, "strategicRankScore": 65, "tacticalScore": 40, "peerGroup": "HBM Equipment", "gate": "BLOCK", "gateReasons": []},
            {"ticker": "C", "finalScore": 62, "strategicRankScore": 66, "tacticalScore": 42, "peerGroup": "HBM Equipment", "gate": "BLOCK", "gateReasons": []},
            {"ticker": "D", "finalScore": 64, "strategicRankScore": 67, "tacticalScore": 44, "peerGroup": "HBM Equipment", "gate": "BLOCK", "gateReasons": []},
        ]

        aiq.annotate_percentiles(rows)
        detail = rows[0]["gateReasons"][0]["detail"]

        self.assertIn("ALLOW_DD because strategic score 83 >= 82", detail)
        self.assertIn("peer percentile P88 >= 70", detail)
        self.assertIn("below WATCH threshold", detail)

    def test_model_card_lists_named_data_quality_flags(self):
        rows = [
            {
                "ticker": "MU",
                "name": "Micron",
                "gate": "WATCH_RESET",
                "trendScore": 70,
                "market": {"available": True, "marketCapUsd": 1},
                "dataQualitySeverity": "soft_review",
                "dataQualityReasons": [{"rule": "price_band_outlier", "detail": "price 4.4x 252D median"}],
            },
            {
                "ticker": "WYN",
                "name": "Wiwynn",
                "gate": "DATA_REVIEW",
                "trendScore": 40,
                "market": {"available": True, "marketCapUsd": 1},
                "dataQualitySeverity": "hard_review",
                "dataQualityReasons": [{"rule": "daily_return_outlier", "detail": "1D return 210%"}],
            },
        ]

        card = aiq.model_card(rows, latest_date="2026-06-22")

        self.assertEqual(card["softDataReviewCount"], 1)
        self.assertEqual(card["softDataFlags"][0]["ticker"], "MU")
        self.assertEqual(card["softDataFlags"][0]["rule"], "price_band_outlier")
        self.assertEqual(card["hardDataFlags"][0]["ticker"], "WYN")

    def test_non_usd_local_price_display_does_not_use_usd_symbol(self):
        self.assertEqual(aiq.fmt_local_money(349500, "KRW"), "₩349,500")
        self.assertEqual(aiq.fmt_local_money(4865, "TWD"), "NT$4,865")
        self.assertEqual(aiq.fmt_local_money(309.8, "EUR"), "€309.80")

    def test_data_quality_missing_fx_gets_review_reason(self):
        score, reasons = aiq.data_quality(
            {
                "available": True,
                "currency": "KRW",
                "priceUsd": None,
                "marketCapUsd": 100_000_000_000,
                "ret1d": 0.5,
                "ret5d": 1.2,
                "ret3m": 20.0,
            }
        )

        self.assertLess(score, 80)
        self.assertTrue(any(r["rule"] == "fx_conversion_missing" for r in reasons))

    def test_gate_always_has_reason_for_block(self):
        gate, tone, note, reasons = aiq.research_gate(
            final_score=58,
            standalone_score=58,
            base_score=68,
            trend_score=70,
            risk_score=20,
            metrics={"available": True, "vs200": 5.0, "vs50": 12.0, "rsi14": 55.0},
            factors={"valuationGrowth": 70},
            dq_score=100,
            dq_reasons=[],
            portfolio_penalty=0,
            penalty_reasons=[],
        )

        self.assertEqual(gate, "BLOCK")
        self.assertTrue(reasons)
        self.assertEqual(reasons[0]["rule"], "risk_floor")

    def test_portfolio_concentration_gets_portfolio_block(self):
        gate, tone, note, reasons = aiq.research_gate(
            final_score=76,
            standalone_score=88,
            base_score=86,
            trend_score=70,
            risk_score=65,
            metrics={"available": True, "vs200": 8.0, "vs50": 12.0, "rsi14": 55.0},
            factors={"valuationGrowth": 80},
            dq_score=100,
            dq_reasons=[],
            portfolio_penalty=12,
            penalty_reasons=[{"rule": "portfolio_concentration", "detail": "Existing weight 22.99% is above the cap."}],
        )

        self.assertEqual(gate, "PORTFOLIO_BLOCK")
        self.assertEqual(tone, "avoid")
        self.assertTrue(reasons)

    def test_data_quality_flags_five_day_and_price_band(self):
        score, reasons = aiq.data_quality(
            {
                "available": True,
                "currency": "USD",
                "priceUsd": 120,
                "marketCapUsd": 100_000_000_000,
                "ret1d": 2.0,
                "ret5d": 88.0,
                "ret3m": 120.0,
                "priceTo252dMedian": 3.4,
            }
        )

        self.assertLess(score, 80)
        self.assertTrue(any(r["rule"] == "five_day_return_outlier" for r in reasons))
        self.assertTrue(any(r["rule"] == "price_band_outlier" for r in reasons))

    def test_capital_flow_edges_reference_existing_companies(self):
        company_ids = {c.companyId for c in aiq.UNIVERSE}
        allowed_external = {"hyperscaler_capex", "hbm_makers", "advanced_packaging_shortage", "ai_rack_demand"}

        for edge in aiq.CAPITAL_FLOW_EDGES:
            self.assertIn(edge["target"], company_ids)
            self.assertTrue(edge["source"] in company_ids or edge["source"] in allowed_external)
            self.assertGreaterEqual(edge["weight"], 0)
            self.assertLessEqual(edge["weight"], 100)


if __name__ == "__main__":
    unittest.main()

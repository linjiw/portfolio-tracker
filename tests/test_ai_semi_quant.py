import importlib.util
import pathlib
import sys
import unittest

import pandas as pd


SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "ai_semi_quant.py"
SPEC = importlib.util.spec_from_file_location("ai_semi_quant", SCRIPT)
aiq = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = aiq
SPEC.loader.exec_module(aiq)


class AiSemiQuantTests(unittest.TestCase):
    def test_percentile_rank_has_no_phantom_tie_for_off_grid_value(self):
        self.assertEqual(aiq.pct_rank(50, [0, 100]), 50.0)

    def test_market_overlay_is_truncated_to_dashboard_price_date(self):
        frame = pd.DataFrame(
            {"AAA": [100.0, 999.0]},
            index=pd.to_datetime(["2026-07-09", "2026-07-10"]),
        )

        aligned = aiq.truncate_market_data(frame, "2026-07-09")

        self.assertEqual(list(aligned.index.strftime("%Y-%m-%d")), ["2026-07-09"])
        self.assertEqual(aiq.dashboard_market_as_of(
            {"summary": {"priceAsOf": "2026-07-09"}}), "2026-07-09")

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

    def test_structural_score_rejects_missing_nonfinite_and_out_of_range_factors(self):
        valid = {
            "pricingPower": 80,
            "profitElasticity": 70,
            "capexConversion": 60,
            "valuationGrowth": 50,
        }
        for bad in (
            {**valid, "pricingPower": float("nan")},
            {**valid, "pricingPower": 101},
            {**valid, "pricingPower": True},
        ):
            with self.assertRaises(ValueError):
                aiq.structural_score(bad)
        with self.assertRaises(ValueError):
            aiq.structural_score({"pricingPower": 80})

    def test_non_usd_technical_returns_use_historical_fx(self):
        index = pd.bdate_range("2026-01-01", periods=70)
        local = pd.Series([1_000.0] * 70, index=index)
        usd_krw = pd.Series([1_000.0] * 7 + [2_000.0] * 63, index=index)

        metrics = aiq.market_metrics(
            local,
            profile={"currency": "KRW", "fxMode": "divide"},
            fx_series=usd_krw,
            reference_date=str(index[-1].date()),
            reference_index=index,
        )

        self.assertTrue(metrics["analysisAvailable"])
        self.assertTrue(metrics["fxAdjustedReturns"])
        self.assertEqual(metrics["priceLocal"], 1000.0)
        self.assertEqual(metrics["priceUsd"], 0.5)
        self.assertEqual(metrics["ret3m"], -50.0)
        self.assertEqual(metrics["returnBasis"], "USD adjusted close including FX")

    def test_non_usd_missing_fx_is_not_silently_ranked_as_local_return(self):
        index = pd.bdate_range("2026-01-01", periods=70)
        metrics = aiq.market_metrics(
            pd.Series(range(100, 170), index=index, dtype=float),
            profile={"currency": "KRW", "fxMode": "divide", "marketCapUsd": 10_000_000_000},
            reference_date=str(index[-1].date()),
            reference_index=index,
        )

        score, reasons = aiq.data_quality(metrics)
        self.assertFalse(metrics["analysisAvailable"])
        self.assertIsNone(metrics["ret3m"])
        self.assertTrue(any(reason["rule"] == "fx_conversion_missing" for reason in reasons))
        self.assertLess(score, 100)
        self.assertEqual(aiq.market_overlay(metrics), (35, 45, 40, 8))

    def test_short_history_does_not_receive_full_trend_regime_credit(self):
        metrics = {
            "available": True,
            "analysisAvailable": True,
            "historyBars": 35,
            "ret3m": None,
            "ret6m": None,
            "ret1y": None,
            "vs50": None,
            "vs200": None,
            "drawdownMax": -5,
            "currency": "USD",
            "priceUsd": 100,
            "marketCapUsd": 1_000_000_000,
        }
        self.assertLess(aiq.relative_momentum_score(metrics, [metrics]), 60)
        score, reasons = aiq.data_quality(metrics)
        self.assertEqual(score, 80)
        self.assertTrue(any(reason["rule"] == "limited_price_history" for reason in reasons))

    def test_stale_cross_market_series_fails_closed(self):
        metrics = {
            "available": True,
            "analysisAvailable": True,
            "currency": "USD",
            "priceUsd": 100,
            "marketCapUsd": 1_000_000_000,
            "staleTradingBars": 3,
        }
        score, reasons = aiq.data_quality(metrics)
        self.assertLess(score, 50)
        self.assertTrue(any(reason["rule"] == "stale_price_history" for reason in reasons))

    def test_stale_fx_conversion_fails_closed(self):
        metrics = {
            "available": True,
            "analysisAvailable": True,
            "currency": "KRW",
            "priceUsd": 1.0,
            "marketCapUsd": 1_000_000_000,
            "priceFxLagDays": 7,
        }
        score, reasons = aiq.data_quality(metrics)
        self.assertLess(score, 50)
        self.assertTrue(any(reason["rule"] == "stale_fx_history" for reason in reasons))

    def test_future_market_cap_is_display_only_for_scoring(self):
        fields = aiq.market_profile_fields(
            {"marketCapUsd": 50_000_000_000, "marketCapPointInTimeCompatible": False}
        )
        self.assertEqual(fields["marketCapUsd"], 50_000_000_000)
        self.assertIsNone(fields["scoringMarketCapUsd"])

    def test_market_cap_quote_date_cannot_relabel_current_cap_as_historical(self):
        prices = pd.DataFrame(
            {"AAA": [99.0, 100.0]},
            index=pd.to_datetime(["2026-07-08", "2026-07-09"]),
        )
        profiles = {
            "AAA": {
                "currency": "USD",
                "marketCapUsd": 10_000_000_000,
                "marketCapAsOf": "2026-07-10",
                "marketCapAsOfSource": "fetch_date_fallback",
                "marketCapFetchedAt": "2026-07-10T08:00:00-07:00",
                "marketCapPointInTimeCompatible": False,
                "shareCountPointInTimeVerified": False,
            }
        }
        aligned = aiq.align_market_profile_as_of(profiles, prices, "2026-07-09")
        self.assertEqual(aligned["AAA"]["marketCapAsOf"], "2026-07-10")
        self.assertEqual(aligned["AAA"]["marketCapAsOfSource"], "fetch_date_fallback")
        self.assertEqual(aligned["AAA"]["marketCapQuoteAsOf"], "2026-07-09")
        self.assertFalse(aligned["AAA"]["marketCapPointInTimeCompatible"])
        self.assertIn("historical_share_count_unverified", aligned["AAA"]["marketCapAlignmentReason"])

        future_prices = pd.concat(
            [prices, pd.DataFrame({"AAA": [101.0]}, index=pd.to_datetime(["2026-07-10"]))]
        )
        aligned = aiq.align_market_profile_as_of(profiles, future_prices, "2026-07-09")
        self.assertFalse(aligned["AAA"]["marketCapPointInTimeCompatible"])
        self.assertIn("quote_as_of_after_decision_or_missing", aligned["AAA"]["marketCapAlignmentReason"])

    def test_verified_historical_share_count_can_support_historical_cap(self):
        prices = pd.DataFrame(
            {"AAA": [99.0, 100.0]},
            index=pd.to_datetime(["2026-07-08", "2026-07-09"]),
        )
        profiles = {
            "AAA": {
                "currency": "USD",
                "marketCapUsd": 10_000_000_000,
                "marketCapAsOf": "2026-07-09",
                "marketCapAsOfSource": "verified_historical_shares_x_close",
                "marketCapFetchedAt": "2026-07-10T08:00:00-07:00",
                "shareCountPointInTimeVerified": True,
            }
        }

        aligned = aiq.align_market_profile_as_of(profiles, prices, "2026-07-09")

        self.assertTrue(aligned["AAA"]["marketCapPointInTimeCompatible"])
        self.assertEqual(aligned["AAA"]["marketCapAlignmentReason"], "point_in_time_compatible")

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

        self.assertEqual(tsm["peerGroupSize"], 0)
        self.assertEqual(tsm["peerPercentileDisplay"], "DATA_REVIEW · not ranked")
        self.assertEqual(tsm["peerStrategicPercentileDisplay"], "N/A · n=1")

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

    def test_london_pence_quotes_apply_one_hundredth_unit_scale(self):
        self.assertEqual(aiq.normalize_currency("GBp"), "GBX")
        self.assertAlmostEqual(aiq.convert_amount_to_usd(250, "GBp", 1.25, "multiply"), 3.125)
        cap, _, _ = aiq.convert_market_cap_to_usd(
            10_000_000_000,
            "GBP",
            {"GBP": {"rate": 1.25, "mode": "multiply", "source": "GBPUSD=X"}},
        )
        self.assertEqual(cap, 12_500_000_000)
        fields = aiq.market_profile_fields({"currency": "GBp", "marketCapCurrency": "GBP"})
        self.assertEqual(fields["priceCurrency"], "GBX")
        self.assertEqual(fields["marketCapCurrency"], "GBP")
        fast_cap, scale = aiq.normalize_market_cap_value(16_800_000_000_000, "GBp", "yahoo_fast_info")
        info_cap, info_scale = aiq.normalize_market_cap_value(168_000_000_000, "GBp", "yahoo_quote_summary")
        self.assertEqual((fast_cap, scale), (168_000_000_000, 0.01))
        self.assertEqual((info_cap, info_scale), (168_000_000_000, 1.0))

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

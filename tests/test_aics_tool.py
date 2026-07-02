import importlib.util
import pathlib
import sys
import types
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "aics_tool.py"
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location("aics_tool", SCRIPT)
aics = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = aics
SPEC.loader.exec_module(aics)


class AicsToolTests(unittest.TestCase):
    def build_doc(self, previous=None, history_snapshots=None):
        args = types.SimpleNamespace(
            dashboard=str(ROOT / "output" / "portfolio_dashboard.html"),
            period="2y",
            no_fetch=True,
            history=str(ROOT / "output" / "_missing_aics_history_for_tests.jsonl"),
            out_json=str(ROOT / "output" / "_missing_aics_for_tests.json"),
            generated_at="2026-06-21T09:00:00-07:00",
        )
        return aics.build_aics_document(args, previous=previous, history_snapshots=history_snapshots)

    def test_structural_aics_document_has_required_contract(self):
        doc = self.build_doc()

        self.assertEqual(doc["schemaVersion"], 1)
        self.assertEqual(doc["title"], "AICS")
        self.assertGreaterEqual(len(doc["scores"]), 15)
        self.assertGreaterEqual(len(doc["relationshipEdges"]), 10)
        self.assertIn("capitalFlow", doc)
        self.assertIn("portfolioOverlay", doc)
        self.assertIn("scenarioModel", doc)
        self.assertIn("scenarioResults", doc)
        self.assertIn("returnAttribution", doc)
        self.assertIn("returnAttributionSummary", doc)
        self.assertIn("modelCard", doc)

    def test_scores_are_clamped_and_explainable(self):
        doc = self.build_doc()

        for row in doc["scores"]:
            self.assertGreaterEqual(row["finalInvestmentScore"], 0)
            self.assertLessEqual(row["finalInvestmentScore"], 100)
            self.assertTrue(row["gate"])
            self.assertTrue(row["gateReasons"], row["ticker"])
            self.assertIn("bottleneckPowerScore", row)
            self.assertIn("industrialCapitalFlowScore", row)
            self.assertIn("financialCapitalFlowScore", row)
            self.assertIn("companyQualityRank", row)
            self.assertIn("stockAttractivenessRank", row)

    def test_relationship_edges_have_factor_decomposition(self):
        doc = self.build_doc()
        company_ids = {row["companyId"] for row in doc["scores"]}
        external = set(aics.EXTERNAL_NODE_NAMES)

        for edge in doc["relationshipEdges"]:
            self.assertTrue(edge["sourceCompanyId"] in company_ids or edge["sourceCompanyId"] in external)
            self.assertIn(edge["targetCompanyId"], company_ids)
            self.assertGreaterEqual(edge["edgeWeight"], 0)
            self.assertLessEqual(edge["edgeWeight"], 100)
            for key in (
                "revenueCorrelation",
                "technicalDependency",
                "substitutionDifficulty",
                "orderVisibility",
                "capacityTightness",
            ):
                self.assertGreaterEqual(edge[key], 0)
                self.assertLessEqual(edge[key], 100)

    def test_scenarios_and_report_are_renderable(self):
        doc = self.build_doc()
        report = aics.render_report(doc)

        self.assertEqual({s["id"] for s in doc["scenarioResults"]}, {"base", "bull", "bear"})
        self.assertTrue(all(s["winners"] for s in doc["scenarioResults"]))
        self.assertTrue(all(s["losers"] for s in doc["scenarioResults"]))
        self.assertIn("# AICS Report", report)
        self.assertIn("not investment advice", report.lower())
        self.assertIn("Scenario Lab", report)
        self.assertIn("History Snapshot Validation", report)

    def test_scenario_model_has_controls_and_sensitivities(self):
        doc = self.build_doc()
        model = doc["scenarioModel"]

        self.assertEqual(
            {
                "aiCapexGrowth",
                "cowosCapacity",
                "hbmAsp",
                "samsungSf2Yield",
                "intel18aWins",
                "exportControls",
                "usdFx",
            },
            {control["key"] for control in model["controls"]},
        )
        self.assertEqual(len(model["companySensitivities"]), len(doc["scores"]))
        self.assertTrue(model["defaultRun"]["winners"])
        self.assertTrue(model["defaultRun"]["losers"])
        self.assertIn("portfolioImpact", model["defaultRun"])

        sample = model["companySensitivities"][0]
        self.assertIn("baseScore", sample)
        self.assertIn("portfolioWeightPct", sample)
        self.assertIn("aiCapexGrowth", sample["sensitivities"])

    def test_custom_scenario_impacts_are_deterministic(self):
        rows = [
            {
                "companyId": "mem",
                "ticker": "MEM",
                "name": "Memory Co",
                "peerGroup": "Memory",
                "valueChainRole": "HBM supplier",
                "finalInvestmentScore": 70,
                "riskScore": 60,
                "gate": "WATCH",
                "portfolio": {"weightPct": 10},
            },
            {
                "companyId": "gpu",
                "ticker": "GPU",
                "name": "GPU Co",
                "peerGroup": "AI Accelerator",
                "valueChainRole": "AI accelerator",
                "finalInvestmentScore": 80,
                "riskScore": 60,
                "gate": "ALLOW_DD",
                "portfolio": {"weightPct": 30},
            },
        ]
        assumptions = {
            "aiCapexGrowth": "up40",
            "cowosCapacity": "tight",
            "hbmAsp": "up50",
            "exportControls": "stable",
        }

        first = aics.evaluate_custom_scenario(rows, assumptions)
        second = aics.evaluate_custom_scenario(rows, assumptions)
        bear = aics.evaluate_custom_scenario(
            rows,
            {
                "aiCapexGrowth": "down10",
                "cowosCapacity": "oversupply",
                "hbmAsp": "down20",
                "exportControls": "severe",
            },
        )

        self.assertEqual(first, second)
        self.assertGreater(first["portfolioImpact"]["scoreDelta"], bear["portfolioImpact"]["scoreDelta"])
        self.assertEqual(first["winners"][0]["ticker"], "MEM")
        self.assertIn(first["winners"][0]["newGate"], {"WATCH", "ALLOW_DD"})

    def test_alert_center_covers_design_plan_categories(self):
        rows = [
            {
                "companyId": "flow",
                "ticker": "FLOW",
                "name": "Flow Co",
                "peerGroup": "HBM / Memory",
                "valueChainRole": "HBM supplier",
                "finalInvestmentScore": 82,
                "scoreChange1W": -12,
                "riskScore": 48,
                "riskScoreChange1W": -15,
                "industrialCapitalFlowScore": 85,
                "industrialCapitalFlowChange1W": 40,
                "financialCapitalFlowScore": 72,
                "financialCapitalFlowChange1W": 28,
                "valuationScore": 96,
                "growthRealizationScore": 45,
                "gate": "WATCH",
                "riskBreakdown": {"geopolitical": "medium-high"},
                "dataQualitySeverity": "clean",
                "dataQualityReasons": [],
            },
            {
                "companyId": "equip",
                "ticker": "EQP",
                "name": "Equipment Co",
                "peerGroup": "Equipment",
                "valueChainRole": "WFE",
                "finalInvestmentScore": 77,
                "scoreChange1W": 11,
                "riskScore": 62,
                "industrialCapitalFlowScore": 88,
                "financialCapitalFlowScore": 55,
                "valuationScore": 80,
                "growthRealizationScore": 80,
                "gate": "WATCH",
                "riskBreakdown": {"geopolitical": "medium"},
                "dataQualitySeverity": "clean",
                "dataQualityReasons": [],
            },
            {
                "companyId": "dq",
                "ticker": "DQ",
                "name": "Data Quality Co",
                "peerGroup": "AI Server ODM",
                "valueChainRole": "server",
                "finalInvestmentScore": 40,
                "riskScore": 50,
                "industrialCapitalFlowScore": 55,
                "financialCapitalFlowScore": 52,
                "valuationScore": 60,
                "growthRealizationScore": 60,
                "gate": "DATA_REVIEW",
                "riskBreakdown": {"geopolitical": "high"},
                "dataQualitySeverity": "hard_review",
                "dataQualityReasons": [{"detail": "synthetic data anomaly"}],
            },
        ]
        edges = [
            {
                "sourceName": "Cloud Capex",
                "targetName": "Foundry Co",
                "relationshipType": "advanced_logic_cowos",
                "edgeWeight": 90,
                "edgeWeightChange1Run": 15,
                "capacityTightness": 90,
            }
        ]
        attribution = [{"ticker": "FLOW", "qualityFlag": "low_quality_rally", "totalReturn": 35}]

        alerts = aics.alerts(rows, {"concentrationWarning": True, "totalAicsWeightPct": 42.5}, edges, attribution)
        alert_types = {row["type"] for row in alerts}

        self.assertTrue(
            {
                "portfolio_concentration",
                "data_quality",
                "score_down",
                "score_up",
                "risk_deterioration",
                "capital_flow_turn_positive",
                "valuation_percentile",
                "low_quality_rally",
                "edge_weight_change",
                "industry_threshold",
            }.issubset(alert_types)
        )
        self.assertIn("cowos", {row.get("subtype") for row in alerts})
        self.assertIn("hbm_dram", {row.get("subtype") for row in alerts})
        self.assertIn("wfe", {row.get("subtype") for row in alerts})

    def test_return_attribution_summary_contract(self):
        attribution = [
            {
                "ticker": "AAA",
                "available": True,
                "totalReturn": 30,
                "earningsRevisionContribution": 5,
                "valuationMultipleContribution": 10,
                "capitalFlowMomentumContribution": 12,
                "fxDividendResidualContribution": 3,
                "qualityFlag": "low_quality_rally",
            },
            {
                "ticker": "BBB",
                "available": True,
                "totalReturn": -10,
                "earningsRevisionContribution": -3,
                "valuationMultipleContribution": -4,
                "capitalFlowMomentumContribution": -2,
                "fxDividendResidualContribution": -1,
                "qualityFlag": "normal",
            },
        ]

        summary = aics.return_attribution_summary(attribution)

        self.assertEqual(summary["availableCount"], 2)
        self.assertEqual(summary["topTotalReturn"][0]["ticker"], "AAA")
        self.assertEqual(summary["worstTotalReturn"][0]["ticker"], "BBB")
        self.assertEqual(summary["lowQualityRallies"][0]["ticker"], "AAA")
        self.assertEqual(summary["averageContribution"]["capitalFlowMomentumContribution"], 5)

    def test_previous_snapshot_drives_score_delta(self):
        previous = {
            "scores": [
                {"companyId": "tsmc", "ticker": "TSM", "finalInvestmentScore": 10, "riskScore": 99},
            ]
        }
        doc = self.build_doc(previous=previous)
        tsm = next(row for row in doc["scores"] if row["companyId"] == "tsmc")

        self.assertIsNotNone(tsm["scoreChange1Run"])
        self.assertEqual(tsm["riskScoreChange1Run"], tsm["riskScore"] - 99)

    def test_history_snapshots_drive_one_week_and_one_month_deltas(self):
        history = [
            {
                "generatedAt": "2026-05-20T09:00:00-07:00",
                "scores": [{"companyId": "tsmc", "ticker": "TSM", "finalInvestmentScore": 31, "riskScore": 80}],
            },
            {
                "generatedAt": "2026-06-10T09:00:00-07:00",
                "scores": [{"companyId": "tsmc", "ticker": "TSM", "finalInvestmentScore": 41, "riskScore": 70}],
            },
            {
                "generatedAt": "2026-06-20T09:00:00-07:00",
                "scores": [{"companyId": "tsmc", "ticker": "TSM", "finalInvestmentScore": 51, "riskScore": 60}],
            },
        ]
        doc = self.build_doc(previous=None)
        current = next(row for row in doc["scores"] if row["companyId"] == "tsmc")

        historical = self.build_doc(history_snapshots=history)
        tsm = next(row for row in historical["scores"] if row["companyId"] == "tsmc")

        self.assertEqual(tsm["scoreChange1Run"], current["finalInvestmentScore"] - 51)
        self.assertEqual(tsm["scoreChange1W"], current["finalInvestmentScore"] - 41)
        self.assertEqual(tsm["scoreChange1M"], current["finalInvestmentScore"] - 31)
        self.assertEqual(historical["modelCard"]["history"]["snapshotsRead"], 3)
        self.assertEqual(historical["modelCard"]["history"]["oneWeekSnapshotAt"], "2026-06-10T09:00:00-07:00")
        self.assertEqual(historical["modelCard"]["history"]["oneMonthSnapshotAt"], "2026-05-20T09:00:00-07:00")

    def test_model_card_requirements_prove_basic_validity(self):
        doc = self.build_doc()
        req = doc["modelCard"]["requirements"]

        self.assertTrue(req["scoresClamped"])
        self.assertTrue(req["allRowsHaveGateReasons"])
        self.assertTrue(req["allEdgesWeighted"])
        self.assertGreaterEqual(doc["modelCard"]["edgeCount"], 10)

    def test_static_backtest_calculates_equal_weight_basket_returns(self):
        rows = [
            {
                "ticker": "AAA",
                "finalInvestmentScore": 90,
                "bottleneckPowerScore": 95,
                "industrialCapitalFlowScore": 90,
                "financialCapitalFlowScore": 80,
                "momentumScore": 70,
                "gate": "ALLOW_DD",
                "market": {"ret1m": 10, "ret3m": 30, "ret6m": 60, "ret1y": 120},
            },
            {
                "ticker": "BBB",
                "finalInvestmentScore": 80,
                "bottleneckPowerScore": 75,
                "industrialCapitalFlowScore": 70,
                "financialCapitalFlowScore": 60,
                "momentumScore": 50,
                "gate": "WATCH",
                "market": {"ret1m": 0, "ret3m": 10, "ret6m": 20, "ret1y": 40},
            },
        ]

        backtest = aics.static_basket_backtest(rows)
        top = next(b for b in backtest["baskets"] if b["name"] == "Top Final Score")

        self.assertEqual(backtest["status"], "available")
        self.assertEqual(top["members"], ["AAA", "BBB"])
        self.assertEqual(top["windows"]["1M"]["returnPct"], 5.0)
        self.assertEqual(top["windows"]["3M"]["returnPct"], 20.0)

    def test_aics_document_exposes_backtest_baskets(self):
        doc = self.build_doc()
        backtest = doc["backtest"]

        self.assertIn("baskets", backtest)
        self.assertIn("historyValidation", backtest)
        self.assertEqual(
            {"Top Final Score", "Top Bottleneck Power", "Top Capital Flow", "Investable Tactical"},
            {b["name"] for b in backtest["baskets"]},
        )
        self.assertIn("topCapitalFlowBasket", backtest["currentCrossSection"])

    def test_historical_snapshot_backtest_uses_start_snapshot_selection(self):
        start = {
            "generatedAt": "2026-01-01T09:00:00-08:00",
            "benchmarks": [{"ticker": "SOXX", "price": 100}, {"ticker": "SMH", "price": 100}],
            "scores": [
                {
                    "ticker": "AAA",
                    "companyId": "aaa",
                    "finalInvestmentScore": 90,
                    "bottleneckPowerScore": 80,
                    "industrialCapitalFlowScore": 70,
                    "financialCapitalFlowScore": 60,
                    "valuationScore": 75,
                    "riskScore": 65,
                    "region": "US",
                    "peerGroup": "AI Accelerator / ASIC",
                    "market": {"priceUsd": 100},
                },
                {
                    "ticker": "BBB",
                    "companyId": "bbb",
                    "finalInvestmentScore": 70,
                    "bottleneckPowerScore": 95,
                    "industrialCapitalFlowScore": 80,
                    "financialCapitalFlowScore": 55,
                    "valuationScore": 90,
                    "riskScore": 45,
                    "region": "US",
                    "peerGroup": "Equipment",
                    "market": {"priceUsd": 100},
                },
                {
                    "ticker": "CCC",
                    "companyId": "ccc",
                    "finalInvestmentScore": 80,
                    "bottleneckPowerScore": 75,
                    "industrialCapitalFlowScore": 85,
                    "financialCapitalFlowScore": 65,
                    "valuationScore": 70,
                    "riskScore": 70,
                    "region": "Taiwan",
                    "peerGroup": "Foundry / Manufacturing",
                    "market": {"priceUsd": 100},
                },
            ],
        }
        end = {
            "generatedAt": "2026-02-01T09:00:00-08:00",
            "benchmarks": [{"ticker": "SOXX", "price": 102}, {"ticker": "SMH", "price": 104}],
            "scores": [
                {"ticker": "AAA", "companyId": "aaa", "market": {"priceUsd": 110}},
                {"ticker": "BBB", "companyId": "bbb", "market": {"priceUsd": 90}},
                {"ticker": "CCC", "companyId": "ccc", "market": {"priceUsd": 105}},
            ],
        }

        backtest = aics.historical_snapshot_backtest([start, end], basket_size=2, transaction_cost_bps=10)
        top = next(row for row in backtest["baskets"] if row["name"] == "Top Final Score")

        self.assertEqual(backtest["status"], "available")
        self.assertEqual(backtest["evaluatedPairs"], 1)
        self.assertEqual(backtest["rules"]["transactionCostBps"], 10)
        self.assertEqual(top["latestMembers"], ["AAA", "CCC"])
        self.assertEqual(top["avgGrossReturnPct"], 7.5)
        self.assertEqual(top["avgNetReturnPct"], 7.4)
        self.assertGreater(top["avgExcessVsUniversePct"], 0)
        self.assertEqual(top["avgExcessVsSOXXPct"], 5.4)
        self.assertEqual(top["avgExcessVsSMHPct"], 3.4)
        self.assertIn("latestRegionExposure", top)

    def test_historical_snapshot_backtest_adds_calendar_rebalance_modes(self):
        def snap(iso, prices, bench):
            base_scores = [
                ("AAA", "aaa", 92, 80, 70, 60, 75, 65, "US", "AI Accelerator / ASIC"),
                ("BBB", "bbb", 84, 96, 82, 65, 82, 60, "US", "Equipment"),
                ("CCC", "ccc", 76, 72, 88, 70, 70, 72, "Taiwan", "Foundry / Manufacturing"),
            ]
            return {
                "generatedAt": iso,
                "benchmarks": [{"ticker": "SOXX", "price": bench[0]}, {"ticker": "SMH", "price": bench[1]}],
                "scores": [
                    {
                        "ticker": ticker,
                        "companyId": company_id,
                        "finalInvestmentScore": final_score,
                        "bottleneckPowerScore": bottleneck,
                        "industrialCapitalFlowScore": industrial,
                        "financialCapitalFlowScore": financial,
                        "valuationScore": valuation,
                        "riskScore": risk,
                        "region": region,
                        "peerGroup": peer_group,
                        "market": {"priceUsd": prices[ticker]},
                    }
                    for ticker, company_id, final_score, bottleneck, industrial, financial, valuation, risk, region, peer_group in base_scores
                ],
            }

        snapshots = [
            snap("2026-01-02T09:00:00-08:00", {"AAA": 100, "BBB": 100, "CCC": 100}, (100, 100)),
            snap("2026-02-03T09:00:00-08:00", {"AAA": 110, "BBB": 95, "CCC": 102}, (103, 104)),
            snap("2026-04-02T09:00:00-07:00", {"AAA": 121, "BBB": 100, "CCC": 112}, (106, 108)),
            snap("2026-07-01T09:00:00-07:00", {"AAA": 115, "BBB": 105, "CCC": 120}, (109, 111)),
        ]

        backtest = aics.historical_snapshot_backtest(snapshots, basket_size=2, transaction_cost_bps=10)
        calendar = backtest["calendarValidation"]
        monthly = calendar["monthly"]
        quarterly = calendar["quarterly"]
        monthly_top = next(row for row in monthly["baskets"] if row["name"] == "Top Final Score")
        quarterly_top = next(row for row in quarterly["baskets"] if row["name"] == "Top Final Score")

        self.assertEqual(monthly["selectedPeriods"], ["2026-01", "2026-02", "2026-04", "2026-07"])
        self.assertEqual(quarterly["selectedPeriods"], ["2026-Q1", "2026-Q2", "2026-Q3"])
        self.assertEqual(monthly["evaluatedPairs"], 3)
        self.assertEqual(quarterly["evaluatedPairs"], 2)
        self.assertEqual(monthly["rules"]["rebalance"], "first saved snapshot per monthly calendar period")
        self.assertGreater(monthly_top["rebalanceCount"], 0)
        self.assertGreater(quarterly_top["rebalanceCount"], 0)
        self.assertIsNotNone(monthly_top["avgNetReturnPct"])
        self.assertIsNotNone(quarterly_top["avgExcessVsSOXXPct"])

    def test_compact_snapshot_persists_benchmarks(self):
        snapshot = aics.compact_score_snapshot(
            "2026-01-01T09:00:00-08:00",
            "2026-01-01",
            [{"ticker": "AAA", "companyId": "aaa", "market": {"priceUsd": 12.5}}],
            [{"ticker": "SOXX", "price": 100}],
        )

        self.assertEqual(snapshot["benchmarks"][0]["ticker"], "SOXX")
        self.assertEqual(snapshot["scores"][0]["market"]["priceUsd"], 12.5)

    def test_generate_py_has_aics_route_and_loader(self):
        text = (ROOT / "generate.py").read_text(encoding="utf-8")

        self.assertIn("def load_aics_payload", text)
        self.assertIn('payload["aics"] = load_aics_payload', text)
        self.assertIn("function aicsToolTab()", text)
        self.assertIn("function aicsScenarioRender()", text)
        self.assertIn("function aicsScoreboardRender()", text)
        self.assertIn("'aics'", text)
        self.assertIn("'aiwatch'", text)
        self.assertIn("data-aics-scen", text)
        self.assertIn('id="aics-score-sort"', text)
        for sort_key in ("final", "delta", "flow", "bottleneck", "risk", "portfolio", "peer"):
            self.assertIn(f'value="{sort_key}"', text)
        self.assertIn("aics-attribution", text)
        self.assertIn("returnAttributionSummary", text)
        self.assertIn("historyValidation", text)
        self.assertIn("calendarValidation", text)
        self.assertIn('data-seg="aics"', text)
        self.assertIn("back.baskets", text)
        self.assertIn("AICS产业链", text)


if __name__ == "__main__":
    unittest.main()

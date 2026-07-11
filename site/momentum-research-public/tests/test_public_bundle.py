"""Privacy and schema gates for the static public momentum research bundle."""
from __future__ import annotations

import json
import math
import re
import unittest
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DATA_PATH = ROOT / "data" / "study.json"
SCHEMA_PATH = ROOT / "public-study.schema.json"
WORKFLOW_PATH = ROOT / ".github-workflow-template" / "pages.yml"

TOP_LEVEL_KEYS = {
    "schemaVersion",
    "dataStatus",
    "siteTitle",
    "generatedDate",
    "notice",
    "strategies",
    "methodology",
    "limitations",
}
STRATEGY_KEYS = {
    "id",
    "name",
    "family",
    "summary",
    "exposureUnit",
    "metrics",
    "currentModels",
    "currentSnapshot",
    "series",
    "qualityLedger",
}
METRIC_KEYS = {"cagr", "maxDrawdown", "calmar", "annualTurnover", "status"}
MODEL_KEYS = {
    "track",
    "label",
    "asOf",
    "riskyAsset",
    "targetExposure",
    "cashExposure",
    "action",
    "gate",
    "note",
}
SNAPSHOT_KEYS = {
    "asOf",
    "mode",
    "newCapitalGate",
    "action",
    "basketBasis",
    "modelBasket",
    "cashWeight",
    "nextTrigger",
    "riskTrigger",
    "note",
}
POINT_KEYS = {"date", "nav", "drawdown", "exposure", "decision"}
DECISION_KEYS = {"kind", "action", "regime", "reason", "targetExposure", "modelBasket"}
DECISION_KINDS = {
    "ENTER",
    "ADD",
    "HOLD",
    "REDUCE",
    "EXIT",
    "REBALANCE",
    "BLOCK",
    "REFERENCE",
}
BASKET_KEYS = {"asset", "weight"}
QUALITY_KEYS = {"rank", "asset", "momentumReturn", "status", "evidence"}
QUALITY_STATUSES = {"PASS", "WATCH", "BLOCK_DECISION_GRADE"}
METRIC_STATUSES = {"采用", "影子", "基准", "非决策级代理", "研究中"}
EXPECTED_STRATEGY_IDS = [
    "spmo-production",
    "spmo-shadow-10m",
    "spy-benchmark",
    "qqq-benchmark",
    "top3-11m-proxy",
    "top5-11m-proxy",
]
FORBIDDEN_KEYS = {
    "account",
    "accountid",
    "accountnumber",
    "position",
    "positions",
    "holding",
    "holdings",
    "quantity",
    "shares",
    "transaction",
    "transactions",
    "costbasis",
    "marketvalue",
    "cashflow",
    "broker",
    "email",
    "filepath",
    "absolutepath",
}
ABSOLUTE_PATH = re.compile(r"(?:/Users/|/home/|file://|[A-Za-z]:\\)")
EMAIL = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
OFFICIAL_LINK_PREFIXES = (
    "https://www.sec.gov/",
    "https://www.nasdaq.com/",
    "https://ir.nasdaq.com/",
    "https://www.spglobal.com/",
    "https://www.crsp.org/",
)


class ResourceParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.references: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for key, value in attrs:
            if key in {"href", "src"} and value is not None:
                self.references.append((tag, value))


def walk(value: Any, path: str = "study"):
    yield path, value
    if isinstance(value, dict):
        for key, child in value.items():
            yield from walk(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from walk(child, f"{path}[{index}]")


class PublicBundleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.study = json.loads(DATA_PATH.read_text(encoding="utf-8"))
        cls.schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    def test_required_files_are_regular_files(self) -> None:
        required = [
            ROOT / "index.html",
            ROOT / "styles.css",
            ROOT / "app.js",
            DATA_PATH,
            SCHEMA_PATH,
            ROOT / "README.md",
            WORKFLOW_PATH,
        ]
        for path in required:
            self.assertTrue(path.is_file(), path)
            self.assertFalse(path.is_symlink(), path)

    def test_top_level_and_strategy_allowlists(self) -> None:
        self.assertEqual(set(self.study), TOP_LEVEL_KEYS)
        self.assertEqual(self.study["schemaVersion"], 1)
        self.assertIn(self.study["dataStatus"], {"placeholder", "public-research"})
        self.assertGreaterEqual(len(self.study["strategies"]), 1)
        seen_ids: set[str] = set()
        for strategy in self.study["strategies"]:
            self.assertEqual(set(strategy), STRATEGY_KEYS)
            self.assertRegex(strategy["id"], r"^[a-z0-9][a-z0-9_-]{1,48}$")
            self.assertIn(strategy["exposureUnit"], {"percent", "multiplier"})
            self.assertNotIn(strategy["id"], seen_ids)
            seen_ids.add(strategy["id"])
            self.assertEqual(set(strategy["metrics"]), METRIC_KEYS)
            self.assertIn(strategy["metrics"]["status"], METRIC_STATUSES)

    def test_release_snapshot_is_real_and_covers_the_ten_year_window(self) -> None:
        self.assertEqual(self.study["dataStatus"], "public-research")
        self.assertEqual(
            [strategy["id"] for strategy in self.study["strategies"]],
            EXPECTED_STRATEGY_IDS,
        )
        self.assertEqual(
            [strategy["exposureUnit"] for strategy in self.study["strategies"]],
            ["percent", "multiplier", "percent", "percent", "percent", "percent"],
        )
        for strategy in self.study["strategies"]:
            series = strategy["series"]
            self.assertGreater(len(series), 400)
            start = date.fromisoformat(series[0]["date"])
            end = date.fromisoformat(series[-1]["date"])
            self.assertGreaterEqual((end - start).days, 3648)
            self.assertEqual(end.isoformat(), "2026-07-10")
            years = (end - start).days / 365.25
            endpoint_cagr = (series[-1]["nav"] / series[0]["nav"]) ** (1 / years) - 1
            metrics = strategy["metrics"]
            self.assertAlmostEqual(metrics["cagr"], endpoint_cagr, delta=2e-7)
            self.assertAlmostEqual(
                metrics["maxDrawdown"],
                min(point["drawdown"] for point in series),
                delta=1e-8,
            )
            self.assertTrue(math.isfinite(metrics["calmar"]))
            self.assertAlmostEqual(
                metrics["calmar"],
                metrics["cagr"] / abs(metrics["maxDrawdown"]),
                delta=1e-6,
            )
        self.assertEqual(self.study["strategies"][-2]["metrics"]["status"], "非决策级代理")
        self.assertEqual(self.study["strategies"][-1]["metrics"]["status"], "非决策级代理")

    def test_no_forbidden_keys_or_private_values(self) -> None:
        for path, value in walk(self.study):
            if isinstance(value, dict):
                for key in value:
                    normalized = re.sub(r"[^a-z0-9]", "", key.lower())
                    self.assertNotIn(normalized, FORBIDDEN_KEYS, path)
            if isinstance(value, str):
                self.assertIsNone(ABSOLUTE_PATH.search(value), path)
                self.assertIsNone(EMAIL.search(value), path)

    def test_dual_model_tracks_are_explicit_and_bounded(self) -> None:
        for strategy in self.study["strategies"]:
            models = strategy["currentModels"]
            self.assertEqual(len(models), 2)
            self.assertEqual(
                {model["track"] for model in models},
                {"existing-sleeve", "new-capital"},
            )
            for model in models:
                self.assertEqual(set(model), MODEL_KEYS)
                self.assertIn(model["gate"], {"ALLOW", "WATCH", "BLOCK"})
                self.assertGreaterEqual(model["targetExposure"], 0)
                self.assertLessEqual(model["targetExposure"], 1.25)
                self.assertGreaterEqual(model["cashExposure"], 0)
                self.assertLessEqual(model["cashExposure"], 1)
                self.assertLessEqual(
                    model["targetExposure"] + model["cashExposure"],
                    1.250001,
                )

    def test_current_snapshots_make_each_strategy_immediately_actionable(self) -> None:
        expected_modes = [
            "PRODUCTION_HOLD",
            "SHADOW_ONLY",
            "BENCHMARK",
            "BENCHMARK",
            "RESEARCH_BLOCKED",
            "RESEARCH_BLOCKED",
        ]
        expected_gates = ["BLOCK", "BLOCK", "NA", "NA", "BLOCK", "BLOCK"]
        expected_bases = [
            "account-target",
            "shadow-account-target",
            "benchmark",
            "benchmark",
            "research-sleeve",
            "research-sleeve",
        ]
        expected_assets = [
            ["SPMO"],
            ["SPMO"],
            ["SPY"],
            ["QQQ"],
            ["SNDK", "MU", "WDC"],
            ["SNDK", "MU", "WDC", "LITE", "INTC"],
        ]
        expected_cash = [0.92, 0.90, 0.0, 0.0, 0.0, 0.0]

        for index, strategy in enumerate(self.study["strategies"]):
            snapshot = strategy["currentSnapshot"]
            self.assertEqual(set(snapshot), SNAPSHOT_KEYS)
            self.assertEqual(snapshot["mode"], expected_modes[index])
            self.assertEqual(snapshot["newCapitalGate"], expected_gates[index])
            self.assertEqual(snapshot["basketBasis"], expected_bases[index])
            self.assertEqual(
                [item["asset"] for item in snapshot["modelBasket"]],
                expected_assets[index],
            )
            self.assertAlmostEqual(snapshot["cashWeight"], expected_cash[index])
            self.assertAlmostEqual(
                sum(item["weight"] for item in snapshot["modelBasket"])
                + snapshot["cashWeight"],
                1.0,
                places=8,
            )
            for field in ("action", "nextTrigger", "riskTrigger", "note"):
                self.assertTrue(snapshot[field])

        production = self.study["strategies"][0]["currentSnapshot"]
        self.assertEqual(production["modelBasket"], [{"asset": "SPMO", "weight": 0.08}])
        self.assertGreaterEqual(
            len(re.findall(r"\d+\.\d{2}", production["nextTrigger"])),
            2,
        )
        self.assertGreaterEqual(
            len(re.findall(r"\d+\.\d{2}", production["riskTrigger"])),
            2,
        )
        shadow = self.study["strategies"][1]["currentSnapshot"]
        self.assertEqual(shadow["modelBasket"], [{"asset": "SPMO", "weight": 0.1}])
        for benchmark in self.study["strategies"][2:4]:
            benchmark_track = benchmark["currentModels"][0]
            self.assertEqual(benchmark_track["targetExposure"], 1.0)
            self.assertEqual(benchmark_track["cashExposure"], 0.0)
            self.assertEqual(
                benchmark_track["riskyAsset"],
                benchmark["currentSnapshot"]["modelBasket"][0]["asset"],
            )

    def test_series_and_decisions_use_observable_fields(self) -> None:
        for strategy in self.study["strategies"]:
            series = strategy["series"]
            self.assertGreaterEqual(len(series), 8)
            dates: list[str] = []
            for point in series:
                self.assertEqual(set(point), POINT_KEYS)
                dates.append(point["date"])
                self.assertGreater(point["nav"], 0)
                self.assertGreaterEqual(point["drawdown"], -1)
                self.assertLessEqual(point["drawdown"], 0)
                self.assertGreaterEqual(point["exposure"], 0)
                self.assertLessEqual(point["exposure"], 2)
                decision = point["decision"]
                if decision is not None:
                    self.assertEqual(set(decision), DECISION_KEYS)
                    self.assertIn(decision["kind"], DECISION_KINDS)
                    self.assertNotIn("confidence", decision)
                    self.assertGreaterEqual(decision["targetExposure"], 0)
                    self.assertLessEqual(decision["targetExposure"], 1.25)
                    self.assertLessEqual(len(decision["modelBasket"]), 5)
                    total_weight = 0.0
                    for item in decision["modelBasket"]:
                        self.assertEqual(set(item), BASKET_KEYS)
                        self.assertRegex(item["asset"], r"^[A-Z0-9.^-]{1,16}$")
                        self.assertGreater(item["weight"], 0)
                        self.assertLessEqual(item["weight"], 1.25)
                        total_weight += item["weight"]
                    self.assertLessEqual(total_weight, 1.250001)
            self.assertEqual(dates, sorted(dates))
            self.assertEqual(len(dates), len(set(dates)))

    def test_decision_kinds_encode_material_changes(self) -> None:
        all_decisions = [
            [point["decision"] for point in strategy["series"] if point["decision"]]
            for strategy in self.study["strategies"]
        ]

        for decision in all_decisions[0]:
            expected = "BLOCK" if decision["action"] == "DEFEND REVIEW" else "HOLD"
            self.assertEqual(decision["kind"], expected)
            if expected == "BLOCK":
                self.assertEqual(decision["targetExposure"], 1.0)
                self.assertIn("未在该代理路径中执行减仓", decision["reason"])

        previous_target = None
        for decision in all_decisions[1]:
            if previous_target is None:
                expected = "ENTER"
            elif decision["targetExposure"] > previous_target + 1e-12:
                expected = "ADD"
            elif decision["targetExposure"] < previous_target - 1e-12:
                expected = "REDUCE"
            else:
                expected = "HOLD"
            self.assertEqual(decision["kind"], expected)
            previous_target = decision["targetExposure"]

        for decisions in all_decisions[2:4]:
            self.assertEqual({row["kind"] for row in decisions}, {"REFERENCE"})

        for decisions in all_decisions[4:]:
            previous_basket = None
            for decision in decisions:
                basket = tuple(
                    sorted((item["asset"], item["weight"]) for item in decision["modelBasket"])
                )
                expected = "REBALANCE" if previous_basket is None or basket != previous_basket else "HOLD"
                self.assertEqual(decision["kind"], expected)
                previous_basket = basket

    def test_top3_top5_ledger_is_11m_and_not_subjective_score(self) -> None:
        expected = {
            "top3": ["SNDK", "MU", "WDC"],
            "top5": ["SNDK", "MU", "WDC", "LITE", "INTC"],
        }
        for strategy in self.study["strategies"]:
            ledger = strategy["qualityLedger"]
            self.assertEqual(set(ledger), {"top3", "top5"})
            for name, assets in expected.items():
                rows = ledger[name]
                self.assertEqual([row["asset"] for row in rows], assets)
                for index, row in enumerate(rows, start=1):
                    self.assertEqual(set(row), QUALITY_KEYS)
                    self.assertNotIn("score", row)
                    self.assertEqual(row["rank"], index)
                    self.assertGreaterEqual(row["momentumReturn"], -1)
                    self.assertLessEqual(row["momentumReturn"], 1000)
                    self.assertIn(row["status"], QUALITY_STATUSES)

    def test_schema_closes_every_object(self) -> None:
        for path, value in walk(self.schema, "schema"):
            if isinstance(value, dict) and value.get("type") == "object":
                self.assertIs(value.get("additionalProperties"), False, path)
        quality = self.schema["$defs"]["qualityItem"]
        self.assertIn("momentumReturn", quality["properties"])
        self.assertNotIn("score", quality["properties"])
        decision = self.schema["$defs"]["decision"]
        self.assertIn("kind", decision["properties"])
        self.assertIn("targetExposure", decision["properties"])
        self.assertIn("modelBasket", decision["properties"])
        self.assertNotIn("confidence", decision["properties"])
        snapshot = self.schema["$defs"]["currentSnapshot"]
        self.assertEqual(
            snapshot["properties"]["mode"]["enum"],
            ["PRODUCTION_HOLD", "SHADOW_ONLY", "BENCHMARK", "RESEARCH_BLOCKED"],
        )
        self.assertEqual(
            snapshot["properties"]["newCapitalGate"]["enum"],
            ["ALLOW", "WATCH", "BLOCK", "NA"],
        )
        self.assertEqual(
            snapshot["properties"]["basketBasis"]["enum"],
            ["account-target", "shadow-account-target", "benchmark", "research-sleeve"],
        )

    def test_runtime_resources_are_relative_and_local(self) -> None:
        parser = ResourceParser()
        parser.feed((ROOT / "index.html").read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(parser.references), 3)
        for tag, reference in parser.references:
            if tag == "a" and reference.startswith(OFFICIAL_LINK_PREFIXES):
                continue
            self.assertTrue(
                reference.startswith("./") or reference.startswith("#"),
                f"{tag} has non-relative resource {reference}",
            )
        css = (ROOT / "styles.css").read_text(encoding="utf-8")
        self.assertNotRegex(css, r"url\(\s*['\"]?https?://")
        app = (ROOT / "app.js").read_text(encoding="utf-8")
        self.assertIn('new URL("./data/study.json", document.baseURI)', app)
        self.assertIn("fetch(DATA_URL", app)
        self.assertNotRegex(app, r"fetch\(\s*['\"]https?://")

    def test_fast_decision_dashboard_contract_is_present(self) -> None:
        html = (ROOT / "index.html").read_text(encoding="utf-8")
        app = (ROOT / "app.js").read_text(encoding="utf-8")
        css = (ROOT / "styles.css").read_text(encoding="utf-8")
        for element_id in ("now", "overview-grid", "decision-shortcuts", "decision-kind"):
            self.assertIn(f'id="{element_id}"', html)
        for token in (
            "MODE_LABELS",
            "BASKET_BASIS_LABELS",
            "KIND_LABELS",
            "renderOverview",
            "kindForSnapshot",
            'setAttribute("aria-current", "date")',
            "模型总权重必须为100%",
        ):
            self.assertIn(token, app)
        for selector in (
            ".overview-grid",
            ".decision-shortcuts",
            ".decision-marker",
            ".model-track .detail-list",
        ):
            self.assertIn(selector, css)

    def test_workflow_stages_only_explicit_public_files(self) -> None:
        workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
        self.assertIn("workflow_dispatch", workflow)
        self.assertIn("contents: read", workflow)
        self.assertIn("pages: write", workflow)
        self.assertIn("id-token: write", workflow)
        self.assertIn("actions/upload-pages-artifact@v4", workflow)
        self.assertIn('public_dir="$RUNNER_TEMP/momentum-public-site"', workflow)
        self.assertIn("path: ${{ runner.temp }}/momentum-public-site", workflow)
        self.assertNotIn("output/", workflow)
        self.assertNotIn("outputs/", workflow)
        self.assertNotIn("path: .\n", workflow)
        for public_file in [
            "index.html",
            "styles.css",
            "app.js",
            "public-study.schema.json",
            "data/study.json",
        ]:
            self.assertIn(f"site/momentum-research-public/{public_file}", workflow)


if __name__ == "__main__":
    unittest.main()

"""Contract, determinism, and privacy tests for the public study builder."""
from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

import pytest

from scripts import build_public_momentum_site_data as public_data


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "site" / "momentum-research-public" / "public-study.schema.json"

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
PRIVATE_VALUE = re.compile(
    r"(?:/Users/|/home/|file://|[A-Za-z]:\\|[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,})",
    re.IGNORECASE,
)


@pytest.fixture(scope="module")
def study() -> dict[str, Any]:
    return public_data.build_public_study()


def walk(value: Any, path: str = "study"):
    yield path, value
    if isinstance(value, dict):
        for key, child in value.items():
            yield from walk(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from walk(child, f"{path}[{index}]")


def test_public_contract_is_exact_and_has_six_strategies(study):
    assert set(study) == TOP_LEVEL_KEYS
    assert study["schemaVersion"] == 1
    assert study["dataStatus"] == "public-research"
    assert tuple(row["id"] for row in study["strategies"]) == public_data.PUBLIC_STRATEGY_IDS
    for strategy in study["strategies"]:
        assert set(strategy) == STRATEGY_KEYS
        assert {row["track"] for row in strategy["currentModels"]} == {
            "existing-sleeve",
            "new-capital",
        }
    assert [row["exposureUnit"] for row in study["strategies"]] == [
        "percent",
        "multiplier",
        "percent",
        "percent",
        "percent",
        "percent",
    ]
    assert [row["metrics"]["status"] for row in study["strategies"]] == [
        "采用",
        "影子",
        "基准",
        "基准",
        "非决策级代理",
        "非决策级代理",
    ]


def test_no_forbidden_keys_paths_email_or_nonfinite_values(study):
    for path, value in walk(study):
        if isinstance(value, dict):
            for key in value:
                normalized = re.sub(r"[^a-z0-9]", "", key.lower())
                assert normalized not in FORBIDDEN_KEYS, path
        elif isinstance(value, str):
            assert not PRIVATE_VALUE.search(value), path
        elif isinstance(value, float):
            assert math.isfinite(value), path
    encoded = json.dumps(study, ensure_ascii=False, allow_nan=False)
    for private_token in (
        "PRIVATE_ACCOUNT_SENTINEL",
        "DO_NOT_PUBLISH_REAL_SHARES",
        "DO_NOT_PUBLISH_COST_BASIS",
    ):
        assert private_token not in encoded


def test_schema_validation_when_jsonschema_is_available(study):
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(
        schema,
        format_checker=jsonschema.FormatChecker(),
    )
    errors = sorted(validator.iter_errors(study), key=lambda error: list(error.path))
    assert not errors, "\n".join(error.message for error in errors[:20])


def test_quality_ledger_uses_current_11m_candidates_and_global_block(study):
    expected = {
        "top3": ["SNDK", "MU", "WDC"],
        "top5": ["SNDK", "MU", "WDC", "LITE", "INTC"],
    }
    for strategy in study["strategies"]:
        for name, assets in expected.items():
            rows = strategy["qualityLedger"][name]
            assert [row["asset"] for row in rows] == assets
            assert [row["rank"] for row in rows] == list(range(1, len(rows) + 1))
            assert all("momentumReturn" in row and "score" not in row for row in rows)
    assert "BLOCK_DECISION_GRADE" in study["notice"]
    assert all(
        model["targetExposure"] == 0 and model["gate"] == "BLOCK"
        for strategy in study["strategies"][-2:]
        for model in strategy["currentModels"]
    )


def test_spmo_public_models_match_the_explicit_two_track_decision(study):
    production = study["strategies"][0]
    existing, new_capital = production["currentModels"]
    assert existing["track"] == "existing-sleeve"
    assert existing["targetExposure"] == pytest.approx(0.08)
    assert existing["cashExposure"] == pytest.approx(0.92)
    assert new_capital["track"] == "new-capital"
    assert new_capital["targetExposure"] == 0
    assert new_capital["cashExposure"] == 1
    assert new_capital["gate"] == "BLOCK"


def test_current_snapshots_are_complete_and_match_model_baskets(study):
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
    expected_baskets = [
        [("SPMO", 0.08)],
        [("SPMO", 0.10)],
        [("SPY", 1.0)],
        [("QQQ", 1.0)],
        [("SNDK", 1 / 3), ("MU", 1 / 3), ("WDC", 1 / 3)],
        [
            ("SNDK", 0.2),
            ("MU", 0.2),
            ("WDC", 0.2),
            ("LITE", 0.2),
            ("INTC", 0.2),
        ],
    ]
    expected_cash = [0.92, 0.90, 0.0, 0.0, 0.0, 0.0]

    for index, strategy in enumerate(study["strategies"]):
        snapshot = strategy["currentSnapshot"]
        assert set(snapshot) == SNAPSHOT_KEYS
        assert snapshot["mode"] == expected_modes[index]
        assert snapshot["newCapitalGate"] == expected_gates[index]
        assert snapshot["basketBasis"] == expected_bases[index]
        assert snapshot["cashWeight"] == pytest.approx(expected_cash[index])
        assert [item["asset"] for item in snapshot["modelBasket"]] == [
            asset for asset, _ in expected_baskets[index]
        ]
        assert [item["weight"] for item in snapshot["modelBasket"]] == pytest.approx(
            [weight for _, weight in expected_baskets[index]], abs=1e-9
        )
        assert sum(item["weight"] for item in snapshot["modelBasket"]) + snapshot[
            "cashWeight"
        ] == pytest.approx(1.0, abs=1e-9)
        assert all(snapshot[field] for field in ("action", "nextTrigger", "riskTrigger", "note"))

    source = json.loads(public_data.DEFAULT_SPMO_ARTIFACT.read_text(encoding="utf-8"))
    production_snapshot = study["strategies"][0]["currentSnapshot"]
    assert production_snapshot["newCapitalGate"] == public_data._spmo_new_capital_gate(
        source
    )
    levels = source["levels"]
    for name in ("buyStop", "buyLimit"):
        assert f"{float(levels[name]):.2f}" in production_snapshot["nextTrigger"]
    for name in ("invalidationClose", "movingStop3Atr"):
        assert f"{float(levels[name]):.2f}" in production_snapshot["riskTrigger"]

    for benchmark in study["strategies"][2:4]:
        benchmark_track = benchmark["currentModels"][0]
        assert benchmark_track["targetExposure"] == pytest.approx(1.0)
        assert benchmark_track["cashExposure"] == pytest.approx(0.0)
        assert benchmark_track["riskyAsset"] == benchmark["currentSnapshot"]["modelBasket"][0]["asset"]


def test_metrics_and_series_are_real_deterministic_replays(study):
    for strategy in study["strategies"]:
        metrics = strategy["metrics"]
        assert metrics["cagr"] != 0
        assert metrics["maxDrawdown"] < 0
        assert math.isfinite(metrics["calmar"])
        assert metrics["annualTurnover"] >= 0
        series = strategy["series"]
        assert len(series) > 400
        assert [row["date"] for row in series] == sorted(row["date"] for row in series)
        assert any(row["decision"] is not None for row in series)
        assert len({row["nav"] for row in series}) > 100
        span_days = (
            public_data.pd.Timestamp(series[-1]["date"])
            - public_data.pd.Timestamp(series[0]["date"])
        ).days
        assert 3648 <= span_days <= 3660
        years = span_days / 365.25
        endpoint_cagr = (series[-1]["nav"] / series[0]["nav"]) ** (1 / years) - 1
        assert metrics["cagr"] == pytest.approx(endpoint_cagr, abs=2e-7)
        assert metrics["maxDrawdown"] == pytest.approx(
            min(row["drawdown"] for row in series), abs=1e-8
        )
        expected_calmar = metrics["cagr"] / abs(metrics["maxDrawdown"])
        assert metrics["calmar"] == pytest.approx(expected_calmar, abs=1e-6)
    first = json.dumps(study, ensure_ascii=False, sort_keys=True, allow_nan=False)
    second = json.dumps(
        public_data.build_public_study(),
        ensure_ascii=False,
        sort_keys=True,
        allow_nan=False,
    )
    assert first == second


def test_weekly_downsample_preserves_exact_monthly_decision_dates(study):
    for strategy in study["strategies"]:
        decision_dates = [
            row["date"] for row in strategy["series"] if row["decision"] is not None
        ]
        assert len(decision_dates) >= 100
        assert len(decision_dates) == len(set(decision_dates))
        assert all(date.endswith(tuple(f"-{day:02d}" for day in range(20, 32))) for date in decision_dates)
        for point in strategy["series"]:
            decision = point["decision"]
            if decision is None:
                continue
            assert set(decision) == DECISION_KEYS
            assert decision["kind"] in DECISION_KINDS
            assert 1 <= len(decision["modelBasket"]) <= 5
            assert sum(item["weight"] for item in decision["modelBasket"]) == pytest.approx(
                decision["targetExposure"]
            )


def test_decision_kinds_are_deterministic_by_strategy_family(study):
    kinds = [
        [
            point["decision"]["kind"]
            for point in strategy["series"]
            if point["decision"] is not None
        ]
        for strategy in study["strategies"]
    ]
    assert set(kinds[0]) <= {"HOLD", "BLOCK"}
    assert {"HOLD", "BLOCK"} <= set(kinds[0])
    assert kinds[1][0] == "ENTER"
    assert set(kinds[1]) <= {"ENTER", "ADD", "HOLD", "REDUCE"}
    assert {"ENTER", "ADD", "REDUCE"} <= set(kinds[1])
    assert set(kinds[2]) == {"REFERENCE"}
    assert set(kinds[3]) == {"REFERENCE"}
    assert kinds[4][0] == "REBALANCE"
    assert kinds[5][0] == "REBALANCE"
    assert set(kinds[4]) == {"REBALANCE", "HOLD"}
    assert set(kinds[5]) == {"REBALANCE", "HOLD"}


def test_top_proxy_baskets_show_exact_monthly_assets_and_weights(study):
    top3, top5 = study["strategies"][-2:]
    for strategy, expected_count in ((top3, 3), (top5, 5)):
        decisions = [
            point["decision"] for point in strategy["series"] if point["decision"] is not None
        ]
        assert len(decisions) >= 100
        assert all(len(decision["modelBasket"]) == expected_count for decision in decisions)
        assert all(
            sum(item["weight"] for item in decision["modelBasket"]) == pytest.approx(1.0)
            for decision in decisions
        )


def test_top_proxy_endpoints_match_the_corrected_source_artifact(study):
    source = json.loads(public_data.DEFAULT_TOP3_ARTIFACT.read_text(encoding="utf-8"))
    corrected = {
        row["id"]: row["metrics"]["total_x"]
        for row in source["strategies"]
        if row["id"] in {"RET_11M_top3", "RET_11M_top5"}
    }
    public = {row["id"]: row for row in study["strategies"]}
    for source_id, public_id in (
        ("RET_11M_top3", "top3-11m-proxy"),
        ("RET_11M_top5", "top5-11m-proxy"),
    ):
        series = public[public_id]["series"]
        endpoint = series[-1]["nav"] / series[0]["nav"]
        assert endpoint == pytest.approx(corrected[source_id], abs=0.011)


def test_atomic_writer_emits_exact_payload(tmp_path, monkeypatch, study):
    target = tmp_path / "public" / "study.json"
    monkeypatch.setattr(public_data, "build_public_study", lambda: study)
    written, returned = public_data.write_public_study(target)
    assert written == target
    assert returned == study
    assert json.loads(target.read_text(encoding="utf-8")) == study
    assert not list(target.parent.glob(".study.json.tmp.*"))

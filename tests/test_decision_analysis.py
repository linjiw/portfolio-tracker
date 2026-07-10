import importlib.util
import datetime as dt
import pathlib
import sys

import numpy as np
import pandas as pd
import pytest


SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "decision_analysis.py"
SPEC = importlib.util.spec_from_file_location("decision_analysis", SCRIPT)
decision = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = decision
SPEC.loader.exec_module(decision)


def test_dashboard_context_derives_cash_concentration_and_market_gate():
    payload = {
        "summary": {"marketValue": 1000, "cashTotal": 50, "pendingTotal": -10, "accountNetWorth": 1040},
        "account": {"cashTotal": 50, "pending": -10, "netWorthWhole": 1040},
        "stocks": [
            {"sym": "AAA", "held": True, "value": 600},
            {"sym": "BBB", "held": True, "value": 400},
        ],
        "qqqTqqq": {"state": {"code": "break", "label": "Trend repair"}},
    }

    out = decision.dashboard_context(payload)

    assert out["heldSymbols"] == ["AAA", "BBB"]
    assert out["cash"]["withPending"] == 40
    assert out["cash"]["top4Pct"] == 100
    assert out["marketGate"] == "break"


def test_placeholder_detection_preserves_only_real_curated_text():
    assert decision.placeholder("（待补：一句话建议）")
    assert decision.placeholder([])
    assert not decision.placeholder("Keep risk below the written cap.")


def test_curated_text_requires_opt_in_and_records_stale_provenance():
    base = {"asOf": "2026-07-09", "thesis": "new quant thesis", "rules": ["new"],
            "recommendation": "new quant recommendation"}
    previous = {"asOf": "2026-07-08", "generatedAt": "2026-07-08T13:00:00-07:00",
                "thesis": "manual thesis", "rules": ["manual rule"],
                "recommendation": "manual recommendation"}

    without = decision.carry_forward_curated_text(dict(base), previous, enabled=False)
    assert without["thesis"] == "new quant thesis"
    assert not without["curatedTextProvenance"]["carriedForward"]

    with_carry = decision.carry_forward_curated_text(dict(base), previous, enabled=True)
    assert with_carry["thesis"] == "manual thesis"
    assert with_carry["curatedTextProvenance"]["carriedForward"]
    assert with_carry["curatedTextProvenance"]["requiresReview"]
    assert set(with_carry["curatedTextProvenance"]["keys"]) == {
        "thesis", "rules", "recommendation"}


def test_wilder_rsi_has_warmup_and_flat_series_is_neutral():
    values = pd.Series([100.0] * 20)
    result = decision.rsi(values)

    assert result.iloc[:14].isna().all()
    assert result.iloc[14:].eq(50.0).all()


def test_symbol_validation_rejects_executable_or_malformed_text():
    assert decision.canonical_symbol(" brk.b ") == "BRK.B"
    for value in ("BAD<script>", "../../QQQ", "QQQ;rm", "QQ Q"):
        with pytest.raises(ValueError):
            decision.canonical_symbol(value)


def test_compute_blocks_stale_or_short_history_and_handles_single_symbol_layout():
    index = pd.bdate_range("2026-01-02", periods=120)
    raw = pd.DataFrame({
        "Open": np.linspace(100, 120, len(index)),
        "High": np.linspace(101, 121, len(index)),
        "Low": np.linspace(99, 119, len(index)),
        "Close": np.linspace(100, 120, len(index)),
        "Volume": 1_000,
    }, index=index)

    candidates, _, _ = decision.compute(
        ["QQQ"], [], today=dt.date(2026, 7, 10), downloader=lambda *_a, **_k: raw)

    assert len(candidates) == 1
    assert candidates[0]["verdict"] == "BLOCK_DATA"
    assert candidates[0]["dataDecisionGrade"] is False
    assert candidates[0]["historyBars"] == 120


def test_compute_requires_minimum_pairwise_correlation_overlap():
    index = pd.bdate_range(end="2026-07-09", periods=300)
    qqq = np.linspace(100, 200, len(index))
    raw = pd.concat({
        "Close": pd.DataFrame({"AAA": qqq, "BBB": qqq * 1.01}, index=index)
    }, axis=1)

    candidates, _, _ = decision.compute(
        ["AAA"], ["BBB"], today=dt.date(2026, 7, 10), downloader=lambda *_a, **_k: raw)

    assert candidates[0]["dataDecisionGrade"] is True
    assert candidates[0]["correlationPairs"] == 1
    assert candidates[0]["minCorrelationObservations"] >= decision.MIN_CORRELATION_BARS

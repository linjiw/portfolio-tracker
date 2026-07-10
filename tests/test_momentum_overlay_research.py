"""Deterministic tests for the ten-year momentum overlay research engine."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import momentum_overlay_research as research


def business_prices(start="2022-01-03", periods=320, growth=0.001):
    index = pd.bdate_range(start, periods=periods)
    price = pd.Series(100.0 * np.cumprod(np.full(periods, 1.0 + growth)), index=index)
    return price


def context_for(series):
    return {
        "close": {"SPY": series, "QQQ": series, "SPMO": series},
        "atr": {"SPMO": pd.Series(1.0, index=series.index)},
    }


def test_market_validation_allows_zero_tbill_yield():
    index = pd.bdate_range("2015-01-02", periods=600)
    frame = pd.DataFrame(index=index)
    for ticker in research.MARKET_TICKERS:
        frame[f"Close__{ticker}"] = 0.0 if ticker == "^IRX" else 100.0
    assert research.validate_market_frame(frame) == []


def test_cash_return_uses_prior_yield_and_calendar_days():
    index = pd.to_datetime(["2026-01-02", "2026-01-05", "2026-01-06"])
    frame = pd.DataFrame({"Close__^IRX": [3.65, 7.30, 7.30]}, index=index)
    result = research.cash_returns_from_irx(frame, index)
    assert result.iloc[0] == 0.0
    assert result.iloc[1] == pytest.approx((1.0 + 0.0365) ** (3 / 365.25) - 1.0)
    assert result.iloc[2] == pytest.approx((1.0 + 0.0730) ** (1 / 365.25) - 1.0)


def test_signal_at_close_fills_at_next_close_and_pays_cost():
    index = pd.bdate_range("2026-01-02", periods=5)
    base = pd.Series([100.0, 100.0, 120.0, 120.0, 120.0], index=index)
    cash = pd.Series(0.0, index=index)
    result = research.simulate_overlay(
        base,
        cash,
        {"id": "baseline", "family": "buy_hold"},
        context_for(base),
        start=index[1],
        end=index[-1],
        cost_bps=10,
    )
    # The seed-close instruction fills on index[1], so the strategy cannot
    # capture the seed -> fill return.  It then captures index[1] -> index[2].
    assert result.equity.loc[index[1]] == pytest.approx(0.999)
    assert result.equity.loc[index[2]] == pytest.approx(0.999 * 1.2)
    assert result.events[0]["date"] == str(index[1].date())


def test_fixed_stop_exits_only_after_next_close():
    index = pd.bdate_range("2026-01-02", periods=8)
    base = pd.Series([100, 100, 100, 88, 80, 80, 80, 80], index=index, dtype=float)
    cash = pd.Series(0.0, index=index)
    result = research.simulate_overlay(
        base,
        cash,
        {
            "id": "stop10",
            "family": "fixed_stop",
            "hard_stop": 0.10,
            "cooldown": 20,
            "breakout": 0,
        },
        context_for(base),
        start=index[1],
        end=index[-1],
        cost_bps=0,
    )
    exits = [event for event in result.events if event["reason"] == "fixed_stop"]
    assert exits[0]["date"] == str(index[4].date())
    # The -12% close triggered the instruction, but the old position still
    # bears the following close's 88 -> 80 move before exiting.
    assert result.equity.loc[index[4]] == pytest.approx(0.8)
    assert result.exposure.loc[index[4]] == 0.0


def test_monthly_sma_does_not_update_before_completed_month_end():
    index = pd.bdate_range("2024-01-02", "2025-04-04")
    base = pd.Series(np.linspace(100, 150, len(index)), index=index)
    signal = research.monthly_signal(base, 6, "sma")
    june = base.loc["2024-06"]
    assert not signal.loc[june.index[:-1]].any()
    # The first six completed month observations can only update state on the
    # final session of June, after which the state is carried into July.
    assert bool(signal.loc["2024-07-01"])


def test_monthly_sma_signal_updates_on_month_end_close():
    index = pd.bdate_range("2024-01-02", "2025-04-04")
    base = pd.Series(np.linspace(100, 150, len(index)), index=index)
    signal = research.monthly_signal(base, 6, "sma")
    june_end = base.loc["2024-06"].index[-1]
    assert bool(signal.loc[june_end])
    assert bool(signal.loc["2024-07-01"])


def test_final_incomplete_month_does_not_create_month_end_signal():
    index = pd.bdate_range("2024-01-02", "2024-07-10")
    base = pd.Series(100.0, index=index)
    base.loc["2024-07"] = 200.0
    signal = research.monthly_signal(base, 3, "sma")
    # July has no later-month observation proving completion, so the July jump
    # must not become an actionable month-end signal.
    assert not signal.loc["2024-07"].any()


def test_top3_positive_filter_leaves_failed_slots_in_cash(monkeypatch):
    index = pd.bdate_range("2024-01-02", "2025-06-04")
    prices = pd.DataFrame(
        {
            "AAA": np.linspace(100, 120, len(index)),
            "BBB": np.linspace(100, 90, len(index)),
            "CCC": np.linspace(100, 80, len(index)),
            "SPY": np.linspace(100, 110, len(index)),
            "QQQ": np.linspace(100, 110, len(index)),
        },
        index=index,
    )
    monkeypatch.setattr(
        research,
        "top3_scores",
        lambda *_args, **_kwargs: pd.Series({"AAA": 0.20, "BBB": -0.05, "CCC": -0.10}),
    )
    cash = pd.Series(0.0, index=index)
    result = research.run_top3_portfolio(
        prices,
        {"SPY": prices["SPY"], "QQQ": prices["QQQ"]},
        cash,
        {
            "id": "positive",
            "family": "top3_portfolio",
            "score_mode": "11m",
            "positive_only": True,
        },
        start=pd.Timestamp("2025-01-02"),
        end=index[-1],
        cost_bps=0,
        coverage=0.0,
    )
    monthly_events = [event for event in result.events if event["reason"] == "monthly_rebalance"]
    assert all(event["risk_weight"] == pytest.approx(1 / 3) for event in monthly_events)
    assert monthly_events[-1]["holdings"] == ["AAA"]


def test_top3_baseline_matches_existing_engine_with_explicit_no_warm_start():
    rng = np.random.default_rng(8)
    index = pd.bdate_range("2022-01-03", "2025-01-03")
    prices = pd.DataFrame(
        {
            "AAA": 100 * np.exp(np.linspace(0, 1.0, len(index)) + rng.normal(0, 0.002, len(index))),
            "BBB": 100 * np.exp(np.linspace(0, 0.5, len(index)) + rng.normal(0, 0.002, len(index))),
            "CCC": 100 * np.exp(np.linspace(0, -0.2, len(index)) + rng.normal(0, 0.002, len(index))),
            "SPY": np.linspace(100, 120, len(index)),
            "QQQ": np.linspace(100, 130, len(index)),
        },
        index=index,
    )
    cash = pd.Series(0.0, index=index)
    start = pd.Timestamp("2023-03-01")
    new = research.run_top3_portfolio(
        prices,
        {"SPY": prices["SPY"], "QQQ": prices["QQQ"]},
        cash,
        {"id": "baseline", "family": "top3_portfolio", "score_mode": "11m"},
        start=start,
        end=index[-1],
        cost_bps=10,
        coverage=0.95,
        warmup_months=0,
    )
    old, _ = research.mt.run_backtest(
        prices,
        11,
        3,
        start,
        index[-1],
        0.001,
        coverage=0.95,
        exclude=("SPY", "QQQ"),
        warm_start_months=0,
    )
    aligned = pd.concat([new.equity.rename("new"), old.rename("old")], axis=1).dropna()
    assert aligned.iloc[-1]["new"] == pytest.approx(aligned.iloc[-1]["old"], rel=1e-10)


def test_top3_midmonth_evaluation_inherits_prior_month_book(monkeypatch):
    index = pd.bdate_range("2022-01-03", "2025-01-31")
    prices = pd.DataFrame(
        {
            "AAA": np.linspace(100, 180, len(index)),
            "BBB": np.linspace(100, 120, len(index)),
            "CCC": np.linspace(100, 90, len(index)),
            "SPY": np.linspace(100, 140, len(index)),
            "QQQ": np.linspace(100, 150, len(index)),
        },
        index=index,
    )
    monkeypatch.setattr(
        research,
        "top3_scores",
        lambda *_args, **_kwargs: pd.Series({"AAA": 0.3, "BBB": 0.2, "CCC": 0.1}),
    )
    start = pd.Timestamp("2024-07-15")
    result = research.run_top3_portfolio(
        prices,
        {"SPY": prices["SPY"], "QQQ": prices["QQQ"]},
        pd.Series(0.0, index=index),
        {"id": "baseline", "family": "top3_portfolio", "score_mode": "11m"},
        start=start,
        end=index[-1],
        cost_bps=0,
        coverage=0.0,
    )
    first = result.exposure.loc[result.exposure.index >= start].iloc[0]
    assert first == pytest.approx(1.0)


def test_candidate_selection_never_reads_holdout_score():
    base_fold = {"cagr": 0.10, "max_drawdown": -0.20, "sharpe": 0.5, "exposure": 1.0}
    good_fold = {"cagr": 0.11, "max_drawdown": -0.15, "sharpe": 0.7, "exposure": 0.8}
    bad_fold = {"cagr": 0.05, "max_drawdown": -0.30, "sharpe": 0.1, "exposure": 0.8}
    huge_holdout = {"cagr": 9.0, "max_drawdown": -0.01, "sharpe": 20.0, "exposure": 1.0}
    metrics = {
        "baseline": {"train": base_fold, "validation": base_fold, "holdout": base_fold},
        "stable": {"train": good_fold, "validation": good_fold, "holdout": base_fold},
        "overfit": {"train": bad_fold, "validation": bad_fold, "holdout": huge_holdout},
    }
    variants = {
        "baseline": {"complexity": 0, "family": "baseline"},
        "stable": {"complexity": 1, "family": "stable_family"},
        "overfit": {"complexity": 1, "family": "overfit_family"},
    }
    selected, _ = research.choose_candidate(metrics, variants, baseline_id="baseline")
    assert selected == "stable"


def test_performance_includes_initial_anchor_cost():
    index = pd.bdate_range("2026-01-02", periods=4)
    path = research.PathResult(
        equity=pd.Series([1.0, 0.9, 0.9, 0.9], index=index),
        exposure=pd.Series([0.0, 1.0, 1.0, 1.0], index=index),
        turnover=pd.Series([0.0, 1.0, 0.0, 0.0], index=index),
        events=[],
        variant={},
    )
    metrics = research.performance_metrics(
        path,
        pd.Series(0.0, index=index),
        index[1],
        index[-1],
    )
    assert metrics["total_x"] == pytest.approx(0.9)
    assert metrics["max_drawdown"] == pytest.approx(-0.1)


def test_cost_restress_changes_only_turnover_dates():
    index = pd.bdate_range("2026-01-02", periods=4)
    path = research.PathResult(
        equity=pd.Series([1.0, 0.999, 1.0989, 1.0989], index=index),
        exposure=pd.Series([0.0, 1.0, 1.0, 1.0], index=index),
        turnover=pd.Series([0.0, 1.0, 0.0, 0.0], index=index),
        events=[],
        variant={},
    )
    stressed = research.restress_transaction_costs(
        path, original_cost_bps=10, stressed_cost_bps=20
    )
    assert stressed.equity.iloc[1] == pytest.approx(0.998)
    assert stressed.equity.iloc[2] / stressed.equity.iloc[1] == pytest.approx(1.1)


def test_partial_trend_gate_keeps_half_risk_instead_of_exiting():
    index = pd.bdate_range("2026-01-02", periods=8)
    base = pd.Series([100, 101, 102, 90, 89, 88, 87, 86], index=index, dtype=float)
    result = research.simulate_overlay(
        base,
        pd.Series(0.0, index=index),
        {
            "id": "sma2_half",
            "family": "sma",
            "window": 2,
            "risk_off_weight": 0.5,
        },
        context_for(base),
        start=index[2],
        end=index[-1],
        cost_bps=0,
    )
    # The last close drifts after the most recent fill; the following close
    # would rebalance back to 50%.
    assert 0.49 < result.exposure.iloc[-1] < 0.5
    assert any(event["to"] == pytest.approx(0.5) for event in result.events)


def test_overlay_indicators_use_pre_window_warmup():
    index = pd.bdate_range("2024-01-02", periods=360)
    base = pd.Series(np.linspace(100, 200, len(index)), index=index)
    start = index[260]
    result = research.simulate_overlay(
        base,
        pd.Series(0.0, index=index),
        {"id": "sma200", "family": "sma", "window": 200},
        context_for(base),
        start=start,
        end=index[-1],
        cost_bps=0,
    )
    assert result.events[0]["date"] == str(start.date())
    assert result.events[0]["to"] == pytest.approx(1.0)


def test_partial_target_rebalances_from_drift_and_records_turnover():
    index = pd.bdate_range("2026-01-02", periods=6)
    base = pd.Series([100, 100, 100, 110, 110, 110], index=index, dtype=float)
    result = research.simulate_overlay(
        base,
        pd.Series(0.0, index=index),
        {
            "id": "half",
            "family": "sma",
            "window": 2,
            "risk_on_weight": 0.5,
            "risk_off_weight": 0.5,
        },
        context_for(base),
        start=index[2],
        end=index[-1],
        cost_bps=0,
    )
    rebalances = [event for event in result.events if event["date"] == str(index[4].date())]
    assert rebalances
    assert rebalances[0]["from"] == pytest.approx(0.52381, rel=1e-4)
    assert rebalances[0]["to"] == pytest.approx(0.5)


def test_experimental_leverage_is_capped_and_pays_funding_spread():
    index = pd.bdate_range("2026-01-02", periods=8)
    base = pd.Series(np.linspace(100, 107, len(index)), index=index)
    common = {
        "id": "on125",
        "family": "sma",
        "window": 2,
        "risk_on_weight": 1.25,
        "risk_off_weight": 0.5,
        "max_exposure": 1.25,
    }
    cash = pd.Series(0.0, index=index)
    free = research.simulate_overlay(
        base, cash, {**common, "borrow_spread": 0.0}, context_for(base),
        start=index[2], end=index[-1], cost_bps=0,
    )
    funded = research.simulate_overlay(
        base, cash, {**common, "borrow_spread": 0.03}, context_for(base),
        start=index[2], end=index[-1], cost_bps=0,
    )
    assert funded.exposure.max() == pytest.approx(1.25)
    assert funded.equity.iloc[-1] < free.equity.iloc[-1]


def test_bootstrap_labels_identical_paths_instead_of_inventing_a_win_rate():
    index = pd.bdate_range("2024-01-02", periods=100)
    equity = pd.Series(np.cumprod(np.full(len(index), 1.001)), index=index)
    path = research.PathResult(
        equity=equity,
        exposure=pd.Series(1.0, index=index),
        turnover=pd.Series(0.0, index=index),
        events=[],
        variant={},
    )
    result = research.paired_block_bootstrap(
        path, path, index[1], index[-1], samples=20, block=10, seed=1
    )
    assert result["samples"] == 0
    assert "identical" in result["reason"]
    assert result["p_both"] is None

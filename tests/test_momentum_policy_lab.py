"""Tests for the cross-asset momentum policy lab."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import momentum_policy_lab as lab


def simple_frame(index):
    values = np.linspace(100, 150, len(index))
    return pd.DataFrame(
        {
            "SPY": values,
            "QQQ": values * 1.1,
            "PDP": values * 0.9,
            "MTUM": values * 1.05,
            "MMTM": values * 0.95,
            "SPMO": values * 1.2,
            "QMOM": values * 0.8,
            "^IRX": 4.0,
        },
        index=index,
    )


def test_trend_votes_use_only_completed_months():
    index = pd.bdate_range("2023-01-02", "2024-07-10")
    price = pd.Series(np.linspace(100, 130, len(index)), index=index)
    price.loc["2024-07"] = 50.0
    votes = lab.trend_votes(price, (6, 10, 12))
    assert votes.loc["2024-07"].nunique() == 1
    assert votes.loc["2024-07"].iloc[0] == votes.loc["2024-06"].iloc[-1]


def test_fast_brake_requires_reclaim_before_release():
    index = pd.bdate_range("2023-01-02", periods=360)
    values = np.concatenate([
        np.linspace(100, 140, 250),
        np.linspace(140, 112, 30),
        np.linspace(112, 116, 30),
        np.linspace(116, 150, 50),
    ])
    price = pd.Series(values, index=index)
    brake = lab.brake_state(
        price, drawdown_trigger=0.10, vol_quantile=0.75, reclaim_days=20
    )
    assert brake.iloc[280]
    assert not bool(brake.iloc[-1])


def test_target_simulation_fills_next_close():
    index = pd.bdate_range("2026-01-02", periods=6)
    price = pd.Series([100, 100, 120, 120, 120, 120], index=index, dtype=float)
    target = pd.Series(1.0, index=index)
    rebalance = pd.Series(False, index=index)
    result = lab.simulate_targets(
        price,
        pd.Series(0.0, index=index),
        target,
        rebalance,
        {"id": "baseline", "max_exposure": 1.0},
        start=index[1],
        end=index[-1],
        cost_bps=10,
        funding_spread=0.03,
    )
    assert result.equity.loc[index[1]] == pytest.approx(0.999)
    assert result.equity.loc[index[2]] == pytest.approx(0.999 * 1.2)


def test_non_rebalance_day_drifts_partial_weight_without_free_reset():
    index = pd.bdate_range("2026-01-02", periods=6)
    price = pd.Series([100, 100, 100, 110, 110, 110], index=index, dtype=float)
    target = pd.Series(0.5, index=index)
    rebalance = pd.Series(False, index=index)
    result = lab.simulate_targets(
        price,
        pd.Series(0.0, index=index),
        target,
        rebalance,
        {"id": "half", "max_exposure": 1.0},
        start=index[2],
        end=index[-1],
        cost_bps=0,
        funding_spread=0.03,
    )
    assert result.exposure.loc[index[3]] == pytest.approx(0.55 / 1.05)
    assert len(result.events) == 1  # initial fill only


def test_scheduled_rebalance_pays_for_drifted_weight():
    index = pd.bdate_range("2026-01-02", periods=7)
    price = pd.Series([100, 100, 100, 110, 110, 110, 110], index=index, dtype=float)
    target = pd.Series(0.5, index=index)
    rebalance = pd.Series(False, index=index)
    rebalance.iloc[3] = True  # signal after the jump; fill on index[4]
    result = lab.simulate_targets(
        price,
        pd.Series(0.0, index=index),
        target,
        rebalance,
        {"id": "half", "max_exposure": 1.0},
        start=index[2],
        end=index[-1],
        cost_bps=0,
        funding_spread=0.03,
    )
    event = next(event for event in result.events if event["date"] == str(index[4].date()))
    assert event["from"] == pytest.approx(0.55 / 1.05, rel=1e-5)
    assert event["to"] == pytest.approx(0.5)


def test_candidate_ranking_does_not_read_holdout():
    assets = ("A", "B")
    base = {"cagr": 0.10, "max_drawdown": -0.20, "sharpe": 0.5}
    good = {"cagr": 0.11, "max_drawdown": -0.15, "sharpe": 0.7}
    bad = {"cagr": 0.05, "max_drawdown": -0.30, "sharpe": 0.1}
    huge = {"cagr": 9.0, "max_drawdown": -0.01, "sharpe": 20.0}
    metrics = {
        "baseline": {
            asset: {"train": base, "validation": base, "holdout": base}
            for asset in assets
        },
        "stable1": {
            asset: {"train": good, "validation": good, "holdout": base}
            for asset in assets
        },
        "stable2": {
            asset: {"train": good, "validation": good, "holdout": base}
            for asset in assets
        },
        "overfit": {
            asset: {"train": bad, "validation": bad, "holdout": huge}
            for asset in assets
        },
    }
    specs = {
        "baseline": {"family": "baseline", "complexity": 0},
        "stable1": {"family": "stable", "complexity": 1},
        "stable2": {"family": "stable", "complexity": 1},
        "overfit": {"family": "overfit", "complexity": 1},
    }
    ranking = lab.rank_candidates(metrics, specs)
    assert ranking[0]["id"] in {"stable1", "stable2"}
    assert ranking[0]["eligible"]


def test_validate_prices_accepts_zero_tbill_yield():
    index = pd.bdate_range("2010-01-04", periods=2600)
    frame = simple_frame(index)
    frame["^IRX"] = 0.0
    assert lab.validate_prices(frame) == []

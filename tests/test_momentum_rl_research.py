"""Tests for the conservative time-series RL environment."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import momentum_rl_research as rl


def feature_frame(index, *, baseline=1.0, strong=True, rebalance=True):
    return pd.DataFrame(
        {
            "vote_bucket": 3 if strong else 1,
            "vol_bucket": 0 if strong else 2,
            "above_sma200": 1 if strong else 0,
            "market_dd_bucket": 0 if strong else 2,
            "market_drawdown": 0.0 if strong else -0.15,
            "baseline_target": baseline,
            "baseline_rebalance": rebalance,
            "rv63": 0.15,
        },
        index=index,
    )


def test_environment_observes_close_then_fills_at_next_close():
    index = pd.bdate_range("2026-01-02", periods=6)
    price = pd.Series([100, 100, 120, 120, 120, 120], index=index, dtype=float)
    env = rl.MomentumAllocationEnv(
        price,
        pd.Series(0.0, index=index),
        feature_frame(index),
        cost_bps=10,
    )
    env.reset(index[1], index[-1])
    first = env.step(rl.BASELINE_ACTION)  # seed->start is still held in cash
    assert first.info["date"] == str(index[1].date())
    assert env.equity == pytest.approx(0.999)
    second = env.step(4)
    assert env.equity == pytest.approx(0.999 * 1.2)
    assert second.info["turnover"] == pytest.approx(0.0)


def test_environment_rebalances_from_drifted_weight():
    index = pd.bdate_range("2026-01-02", periods=6)
    price = pd.Series([100, 100, 110, 110, 110, 110], index=index, dtype=float)
    env = rl.MomentumAllocationEnv(
        price,
        pd.Series(0.0, index=index),
        feature_frame(index, baseline=0.5),
        cost_bps=0,
    )
    env.reset(index[1], index[-1])
    env.step(rl.BASELINE_ACTION)  # fill continuous 50% on index[1]
    result = env.step(2)  # 10% risky jump, then restore 50%
    assert result.info["pretrade_exposure"] == pytest.approx(0.55 / 1.05)
    assert result.info["turnover"] == pytest.approx(0.55 / 1.05 - 0.5)


def test_leverage_is_only_allowed_in_strong_state_and_adds_one_step():
    index = pd.bdate_range("2026-01-02", periods=4)
    price = pd.Series(100.0, index=index)
    strong = rl.MomentumAllocationEnv(
        price, pd.Series(0.0, index=index), feature_frame(index, strong=True)
    )
    strong.reset(index[1], index[-1])
    # The deterministic 100% safety anchor is always reachable, including
    # from reset.  Other exploratory increases remain limited to +25 points.
    assert strong.allowed_actions() == [0, 1, rl.HOLD_ACTION, rl.BASELINE_ACTION]
    strong.step(rl.BASELINE_ACTION)
    assert strong.allowed_actions() == [
        0, 1, 2, 3, 4, 5, rl.HOLD_ACTION, rl.BASELINE_ACTION
    ]

    weak = rl.MomentumAllocationEnv(
        price, pd.Series(0.0, index=index), feature_frame(index, strong=False)
    )
    weak.reset(index[1], index[-1])
    assert all(
        action >= len(rl.ACTIONS) or rl.ACTIONS[action] <= 1.0
        for action in weak.allowed_actions()
    )


def test_nondecision_days_only_allow_true_hold():
    index = pd.bdate_range("2026-01-02", periods=5)
    features = feature_frame(index, baseline=0.73, rebalance=False)
    env = rl.MomentumAllocationEnv(
        pd.Series([100, 100, 110, 110, 110], index=index, dtype=float),
        pd.Series(0.0, index=index),
        features,
        cost_bps=10,
    )
    env.reset(index[1], index[-1])
    first = env.step(rl.BASELINE_ACTION)
    assert first.info["target_exposure"] == pytest.approx(0.73)
    assert env.allowed_actions() == [rl.HOLD_ACTION]
    held = env.step(rl.HOLD_ACTION)
    assert held.info["turnover"] == pytest.approx(0.0)
    assert held.info["target_exposure"] == pytest.approx(0.73 * 1.1 / 1.073)


def test_drawdown_penalty_only_charges_new_maximum_drawdown():
    index = pd.bdate_range("2026-01-02", periods=6)
    price = pd.Series([100, 100, 90, 90, 90, 90], index=index, dtype=float)
    env = rl.MomentumAllocationEnv(
        price,
        pd.Series(0.0, index=index),
        feature_frame(index),
        cost_bps=0,
        drawdown_penalty=3.0,
    )
    env.reset(index[1], index[-1])
    env.step(rl.BASELINE_ACTION)
    loss = env.step(4)
    flat = env.step(4)
    assert loss.reward < math_log(0.9)
    assert flat.reward == pytest.approx(0.0)


def math_log(value):
    import math

    return math.log(value)


def test_low_support_policy_falls_back_to_deterministic_action():
    index = pd.bdate_range("2026-01-02", periods=5)
    env = rl.MomentumAllocationEnv(
        pd.Series(100.0, index=index),
        pd.Series(0.0, index=index),
        feature_frame(index, baseline=0.75),
    )
    env.reset(index[1], index[-1])
    agent = rl.ConservativeTabularAgent(alpha=0.05, gamma=0.99, nmin=20, epochs=2, seed=7)
    assert agent.action(env) == rl.BASELINE_ACTION


def test_running_max_drawdown_is_in_state():
    index = pd.bdate_range("2026-01-02", periods=5)
    env = rl.MomentumAllocationEnv(
        pd.Series(100.0, index=index),
        pd.Series(0.0, index=index),
        feature_frame(index),
    )
    env.reset(index[1], index[-1])
    assert env.state()[5] == 0
    env.max_drawdown = 0.12
    assert env.state()[5] == 2


def test_supported_actions_require_unique_date_support_for_baseline_and_candidate():
    agent = rl.ConservativeTabularAgent(
        alpha=0.05, gamma=0.99, nmin=2, epochs=2, seed=7
    )
    state = (3, 0, 1, 0, 0, 0, 4, 1, 1, 3)
    allowed = [0, 1, rl.HOLD_ACTION, rl.BASELINE_ACTION]
    agent.action_counts[state][0] = 10
    agent.action_counts[state][rl.BASELINE_ACTION] = 1
    assert agent.supported_actions(state, allowed, rl.BASELINE_ACTION) == []
    agent.action_counts[state][rl.BASELINE_ACTION] = 2
    agent.action_counts[state][1] = 1
    assert agent.supported_actions(state, allowed, rl.BASELINE_ACTION) == [
        0, rl.BASELINE_ACTION
    ]


def test_training_support_counts_unique_dates_not_replayed_epochs():
    index = pd.bdate_range("2025-01-02", periods=35)
    env = rl.MomentumAllocationEnv(
        pd.Series(np.linspace(100, 105, len(index)), index=index),
        pd.Series(0.0, index=index),
        feature_frame(index, baseline=0.75, rebalance=True),
        cost_bps=0,
    )
    agent = rl.ConservativeTabularAgent(
        alpha=0.05, gamma=0.99, nmin=2, epochs=4, seed=7
    )
    agent.train({"TEST": env}, {"TEST": (index[1], index[-1])})
    assert agent.action_counts
    assert max(counts.max() for counts in agent.action_counts.values()) <= len(index)
    assert max(agent.support.values()) <= len(index)


def test_dynamic_baseline_path_matches_policy_lab_exactly():
    index = pd.bdate_range("2026-01-02", periods=9)
    price = pd.Series([100, 101, 99, 103, 104, 102, 106, 105, 108], index=index, dtype=float)
    cash = pd.Series([0.0, 0.0001, 0.0001, 0.0001, 0.0001, 0.0001, 0.0001, 0.0001, 0.0001], index=index)
    target = pd.Series([0.63, 0.63, 0.81, 0.81, 0.47, 0.47, 0.92, 0.92, 0.92], index=index)
    rebalance = pd.Series(
        [False, True, False, False, True, False, True, False, False],
        index=index,
    )
    features = feature_frame(index, baseline=1.0, rebalance=False)
    features["baseline_target"] = target
    features["baseline_rebalance"] = rebalance
    env = rl.MomentumAllocationEnv(
        price, cash, features, cost_bps=10, funding_spread=0.03
    )
    actual = rl.evaluate_policy(
        env, index[1], index[-1], lambda current: current.baseline_action(), "baseline"
    )
    expected = rl.lab.simulate_targets(
        price,
        cash,
        target,
        rebalance,
        {"id": "baseline_parity", "max_exposure": 1.25},
        start=index[1],
        end=index[-1],
        cost_bps=10,
        funding_spread=0.03,
    )
    pd.testing.assert_series_equal(actual.equity, expected.equity, check_freq=False)
    pd.testing.assert_series_equal(actual.exposure, expected.exposure, check_freq=False)
    pd.testing.assert_series_equal(actual.turnover, expected.turnover, check_freq=False)


def test_continuous_baseline_score_slice_and_metrics_match_full_lab_path():
    index = pd.bdate_range("2025-01-02", periods=14)
    price = pd.Series(
        [100, 101, 103, 102, 105, 104, 106, 108, 107, 110, 109, 111, 113, 112],
        index=index,
        dtype=float,
    )
    cash = pd.Series(0.0001, index=index)
    target = pd.Series(
        [0.55, 0.55, 0.55, 0.78, 0.78, 0.78, 0.42, 0.42, 0.42, 0.88, 0.88, 0.88, 0.63, 0.63],
        index=index,
    )
    rebalance = pd.Series(
        [False, False, True, False, False, True, False, False, True, False, False, True, False, False],
        index=index,
    )
    features = feature_frame(index, baseline=1.0, rebalance=False)
    features["baseline_target"] = target
    features["baseline_rebalance"] = rebalance
    warmup_start = index[1]
    score_start = index[7]
    env = rl.MomentumAllocationEnv(price, cash, features, cost_bps=10)
    actual = rl.evaluate_policy_with_warmup(
        env,
        warmup_start,
        score_start,
        index[-1],
        lambda current: current.baseline_action(),
        "continuous_baseline",
        switch_at_start=False,
    )
    expected = rl.lab.simulate_targets(
        price,
        cash,
        target,
        rebalance,
        {"id": "full_baseline", "max_exposure": 1.25},
        start=warmup_start,
        end=index[-1],
        cost_bps=10,
        funding_spread=0.03,
    )
    for field in ("equity", "exposure", "turnover"):
        actual_slice = rl.core.segment_series(getattr(actual, field), score_start, index[-1])
        expected_slice = rl.core.segment_series(getattr(expected, field), score_start, index[-1])
        pd.testing.assert_series_equal(actual_slice, expected_slice, check_freq=False)
    assert rl.core.performance_metrics(
        actual, cash, score_start, index[-1]
    ) == rl.core.performance_metrics(expected, cash, score_start, index[-1])


def test_challenger_handoff_cost_is_scored_at_boundary_close():
    index = pd.bdate_range("2025-01-02", periods=8)
    price = pd.Series(100.0, index=index)
    cash = pd.Series(0.0, index=index)
    features = feature_frame(index, baseline=0.50, rebalance=False)
    env = rl.MomentumAllocationEnv(price, cash, features, cost_bps=10)
    score_start = index[4]
    path = rl.evaluate_policy_with_warmup(
        env,
        index[1],
        score_start,
        index[-1],
        lambda current: 3 if current.decision_day() else rl.HOLD_ACTION,
        "challenger",
        switch_at_start=True,
    )
    assert path.turnover.at[score_start] == pytest.approx(0.25)
    metrics = rl.core.performance_metrics(path, cash, score_start, index[-1])
    assert metrics["total_x"] == pytest.approx(1.0 - 0.25 * 10 / 10000, abs=1e-6)


def test_path_result_has_anchor_and_daily_turnover():
    index = pd.bdate_range("2026-01-02", periods=5)
    env = rl.MomentumAllocationEnv(
        pd.Series([100, 100, 101, 102, 103], index=index, dtype=float),
        pd.Series(0.0, index=index),
        feature_frame(index),
    )
    rl.evaluate_policy(
        env, index[1], index[-1], lambda _env: rl.BASELINE_ACTION, "test"
    )
    path = env.path_result("test")
    assert path.equity.iloc[0] == 1.0
    assert len(path.equity) == len(path.turnover) == len(path.exposure)
    assert path.turnover.iloc[1] > 0

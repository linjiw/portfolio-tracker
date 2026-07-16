#!/usr/bin/env python3
"""Conservative tabular time-series RL research for momentum exposure.

The environment uses the same close-t signal / close-t+1 fill, drifted risky
weight, T-bill cash, leverage funding and turnover costs as the deterministic
policy lab.  It excludes the survivor-biased Top-3 data.  A small tabular
Q-learning policy may deviate from the deterministic cross-asset baseline only
when the historical market-state support and unique-date state-action support
exceed pre-registered minimums; otherwise it falls back to that baseline.

This is a research framework, not a live trading agent or a formal safe-RL
guarantee.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

try:
    from scripts.artifact_io import atomic_write_json, atomic_write_text
    from scripts import momentum_overlay_research as core
    from scripts import momentum_policy_lab as lab
except ModuleNotFoundError:
    from artifact_io import atomic_write_json, atomic_write_text
    import momentum_overlay_research as core
    import momentum_policy_lab as lab


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "output" / "momentum_rl_research"
DEFAULT_POLICY_LAB = lab.DEFAULT_OUT / "momentum_policy_lab.json"
ACTIONS = np.asarray([0.0, 0.25, 0.50, 0.75, 1.00, 1.25], dtype=float)
HOLD_ACTION = len(ACTIONS)
BASELINE_ACTION = len(ACTIONS) + 1
N_ACTIONS = len(ACTIONS) + 2
TRAIN_END = pd.Timestamp("2021-12-31")
VALIDATION_START = pd.Timestamp("2022-01-01")
VALIDATION_END = pd.Timestamp("2023-12-31")
REPLAY_START = pd.Timestamp("2024-01-01")
EPS = 1e-12


def safe_float(value: Any, digits: int = 8) -> float | None:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return round(value, digits) if math.isfinite(value) else None


def json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    if isinstance(value, (pd.Timestamp, dt.date, dt.datetime)):
        return value.isoformat()
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        return safe_float(value)
    if isinstance(value, (np.bool_,)):
        return bool(value)
    return value


def nearest_action(exposure: float) -> int:
    return int(np.argmin(np.abs(ACTIONS - float(exposure))))


def action_name(action: int) -> str:
    if action == HOLD_ACTION:
        return "HOLD"
    if action == BASELINE_ACTION:
        return "BASELINE"
    return f"TARGET_{ACTIONS[int(action)]:.2f}"


def drawdown_bucket(value: float) -> int:
    magnitude = abs(min(0.0, float(value)))
    return 0 if magnitude < 0.05 else (1 if magnitude < 0.10 else 2)


def build_features(
    price: pd.Series,
    frame: pd.DataFrame,
    baseline_spec: dict[str, Any],
) -> pd.DataFrame:
    price = price.dropna().sort_index()
    index = price.index
    votes = lab.trend_votes(price, tuple(baseline_spec.get("months", (6, 10, 12))))
    vote_bucket = (votes * 3).round().clip(0, 3).astype(int)
    rv63 = lab.realized_vol(price, "63")
    q33 = rv63.rolling(756, min_periods=252).quantile(1 / 3)
    q67 = rv63.rolling(756, min_periods=252).quantile(2 / 3)
    vol_bucket = pd.Series(1, index=index, dtype=int)
    vol_bucket.loc[rv63 < q33] = 0
    vol_bucket.loc[rv63 > q67] = 2
    sma200 = price.rolling(200, min_periods=200).mean()
    trend = (price > sma200).fillna(False).astype(int)
    market_dd = price / price.rolling(126, min_periods=63).max() - 1.0
    market_dd_bucket = market_dd.fillna(0.0).map(drawdown_bucket).astype(int)
    target, diagnostics = lab.target_exposure(price, frame, baseline_spec)
    features = pd.DataFrame({
        "vote_bucket": vote_bucket,
        "vol_bucket": vol_bucket,
        "above_sma200": trend,
        "market_dd_bucket": market_dd_bucket,
        "market_drawdown": market_dd.fillna(0.0),
        "baseline_target": target,
        "baseline_rebalance": diagnostics["rebalance"].astype(bool),
        "rv63": rv63,
    }, index=index)
    return features.replace([np.inf, -np.inf], np.nan).ffill().fillna(0.0)


@dataclass
class StepResult:
    state: tuple[int, ...]
    reward: float
    done: bool
    info: dict[str, Any]


@dataclass(frozen=True)
class Transition:
    state: tuple[int, ...]
    action: int
    reward: float
    next_state: tuple[int, ...]
    next_allowed: tuple[int, ...]
    next_baseline: int
    done: bool


class MomentumAllocationEnv:
    """Historical replay environment with exact next-close execution."""

    def __init__(
        self,
        price: pd.Series,
        cash_returns: pd.Series,
        features: pd.DataFrame,
        *,
        cost_bps: float = 10.0,
        funding_spread: float = 0.03,
        drawdown_penalty: float = 1.0,
    ):
        self.price = price.dropna().sort_index()
        self.features = features.reindex(self.price.index).ffill().fillna(0.0)
        self.cash = cash_returns.reindex(self.price.index).fillna(0.0)
        self.cost = cost_bps / 10000.0
        self.funding_spread = float(funding_spread)
        self.drawdown_penalty = float(drawdown_penalty)
        self.dates = self.price.index
        self.i = 0
        self.end_i = 0
        self.exposure = 0.0
        self.equity = 1.0
        self.peak = 1.0
        self.max_drawdown = 0.0
        self.initial_decision = True
        self.forced_decision = False
        self.equity_points: list[float] = []
        self.exposure_points: list[float] = []
        self.turnover_points: list[float] = []
        self.point_dates: list[pd.Timestamp] = []

    def reset(self, start: pd.Timestamp, end: pd.Timestamp) -> tuple[int, ...]:
        starts = np.flatnonzero(self.dates >= start)
        ends = np.flatnonzero(self.dates <= end)
        if not len(starts) or not len(ends) or starts[0] == 0:
            raise ValueError("environment window lacks seed/start/end observations")
        self.i = int(starts[0]) - 1
        self.end_i = int(ends[-1])
        if self.end_i <= self.i:
            raise ValueError("environment window is empty")
        self.exposure = 0.0
        self.equity = 1.0
        self.peak = 1.0
        self.max_drawdown = 0.0
        # simulate_targets seeds a pending baseline target from the close
        # immediately before the evaluation window. Treat that seed close as
        # an initialization decision even when it is not a scheduled rebalance
        # day so deterministic baseline paths are exactly comparable.
        self.initial_decision = True
        self.forced_decision = False
        self.equity_points = [1.0]
        self.exposure_points = [0.0]
        self.turnover_points = [0.0]
        self.point_dates = [self.dates[self.i]]
        return self.state()

    def portfolio_drawdown(self) -> float:
        return self.equity / self.peak - 1.0

    def state(self) -> tuple[int, ...]:
        row = self.features.iloc[self.i]
        return (
            int(row["vote_bucket"]),
            int(row["vol_bucket"]),
            int(row["above_sma200"]),
            int(row["market_dd_bucket"]),
            drawdown_bucket(self.portfolio_drawdown()),
            drawdown_bucket(-self.max_drawdown),
            nearest_action(self.exposure),
            int(self.decision_day()),
            int(self.scheduled_baseline_decision()),
            nearest_action(float(row["baseline_target"])),
        )

    def support_key(self) -> tuple[int, ...]:
        state = self.state()
        # Decision availability and the dynamic baseline target are part of
        # support: a HOLD-only day is not a scheduled decision state.
        return (*state[:4], state[7], state[8], state[9])

    def decision_day(self) -> bool:
        if self.initial_decision or self.forced_decision:
            return True
        return bool(self.features.iloc[self.i].get("baseline_rebalance", False))

    def scheduled_baseline_decision(self) -> bool:
        """Whether the original policy itself would submit a target today."""
        if self.initial_decision:
            return True
        return bool(self.features.iloc[self.i].get("baseline_rebalance", False))

    def force_decision_once(self) -> None:
        """Open the action set for a challenger hand-off at a score boundary."""
        self.forced_decision = True

    def baseline_action(self) -> int:
        return BASELINE_ACTION if self.scheduled_baseline_decision() else HOLD_ACTION

    def action_exposure_hint(self, action: int) -> float:
        """Resolve an action for reporting/voting before next-close drift."""
        if action == HOLD_ACTION:
            return float(self.exposure)
        if action == BASELINE_ACTION:
            return float(self.features.iloc[self.i]["baseline_target"])
        return float(ACTIONS[int(action)])

    def allowed_actions(self) -> list[int]:
        if not self.decision_day():
            return [HOLD_ACTION]
        state = self.state()
        vote, vol, trend, market_dd, _portfolio_dd, _running_mdd, current = state[:7]
        allowed = [action for action in range(len(ACTIONS)) if action <= current + 1]
        # The deterministic policy is the safety anchor, so it must always be
        # reachable.  The +25 percentage-point rule constrains exploratory RL
        # deviations; it must not prevent initialization or a safety fallback
        # from moving directly to the pre-registered baseline exposure.
        baseline = self.baseline_action()
        allowed.extend((HOLD_ACTION, baseline))
        strong = vote == 3 and vol < 2 and trend == 1 and market_dd == 0
        if not strong:
            allowed = [
                action for action in allowed
                if action >= len(ACTIONS) or ACTIONS[action] <= 1.0
            ]
        return sorted(set(allowed))

    def step(self, action: int) -> StepResult:
        if self.i >= self.end_i:
            raise RuntimeError("episode already complete")
        if action not in self.allowed_actions():
            raise ValueError(f"action {action} violates monotonic/leverage constraints")
        today = self.dates[self.i]
        next_i = self.i + 1
        next_day = self.dates[next_i]
        risky_return = float(self.price.iloc[next_i] / self.price.iloc[self.i] - 1.0)
        financing = float(self.cash.iloc[next_i])
        if self.exposure > 1.0:
            days = max(1, int((next_day - today).days))
            financing += (1.0 + self.funding_spread) ** (days / 365.25) - 1.0
        pre_factor = (
            self.exposure * (1.0 + risky_return)
            + (1.0 - self.exposure) * (1.0 + financing)
        )
        if pre_factor <= 0:
            raise RuntimeError("portfolio exhausted")
        pretrade_exposure = self.exposure * (1.0 + risky_return) / pre_factor
        if action == HOLD_ACTION:
            target = pretrade_exposure
        elif action == BASELINE_ACTION:
            target = float(self.features.iloc[self.i]["baseline_target"])
        else:
            target = float(ACTIONS[action])
        turnover = abs(target - pretrade_exposure)
        net_factor = pre_factor * max(0.0, 1.0 - turnover * self.cost)
        old_mdd = self.max_drawdown
        self.equity *= net_factor
        self.exposure = target
        self.peak = max(self.peak, self.equity)
        current_dd = max(0.0, 1.0 - self.equity / self.peak)
        self.max_drawdown = max(self.max_drawdown, current_dd)
        reward = math.log(max(net_factor, EPS)) - self.drawdown_penalty * (
            self.max_drawdown ** 2 - old_mdd ** 2
        )
        self.initial_decision = False
        self.forced_decision = False
        self.i = next_i
        self.point_dates.append(next_day)
        self.equity_points.append(self.equity)
        self.exposure_points.append(self.exposure)
        self.turnover_points.append(turnover)
        done = self.i >= self.end_i
        return StepResult(
            state=self.state(),
            reward=reward,
            done=done,
            info={
                "date": str(next_day.date()),
                "pretrade_exposure": pretrade_exposure,
                "target_exposure": target,
                "action": action_name(action),
                "turnover": turnover,
                "net_factor": net_factor,
                "drawdown": current_dd,
                "max_drawdown": self.max_drawdown,
            },
        )

    def path_result(self, policy_id: str) -> core.PathResult:
        return core.PathResult(
            equity=pd.Series(self.equity_points, index=self.point_dates, dtype=float),
            exposure=pd.Series(self.exposure_points, index=self.point_dates, dtype=float),
            turnover=pd.Series(self.turnover_points, index=self.point_dates, dtype=float),
            events=[],
            variant={"id": policy_id},
        )


class ConservativeTabularAgent:
    def __init__(
        self,
        *,
        alpha: float,
        gamma: float,
        nmin: int,
        epochs: int,
        seed: int,
    ):
        self.alpha = float(alpha)
        self.gamma = float(gamma)
        self.nmin = int(nmin)
        self.epochs = int(epochs)
        self.seed = int(seed)
        self.q: dict[tuple[int, ...], np.ndarray] = defaultdict(
            lambda: np.zeros(N_ACTIONS, dtype=float)
        )
        self.action_counts: dict[tuple[int, ...], np.ndarray] = defaultdict(
            lambda: np.zeros(N_ACTIONS, dtype=int)
        )
        self.support: Counter[tuple[int, ...]] = Counter()
        self._action_dates: dict[tuple[tuple[int, ...], int], set[pd.Timestamp]] = defaultdict(set)
        self._support_dates: dict[tuple[int, ...], set[pd.Timestamp]] = defaultdict(set)

    def action_support(self, state: tuple[int, ...], action: int) -> int:
        return int(self.action_counts[state][action])

    def supported_actions(
        self,
        state: tuple[int, ...],
        allowed: list[int] | tuple[int, ...],
        baseline: int,
    ) -> list[int]:
        """Return backed actions only when the dynamic baseline is also backed."""
        if baseline not in allowed or self.action_support(state, baseline) < self.nmin:
            return []
        return [
            action for action in allowed
            if self.action_support(state, action) >= self.nmin
        ]

    def _record_market_support(self, env: MomentumAllocationEnv) -> None:
        day = pd.Timestamp(env.dates[env.i]).normalize()
        self._support_dates[env.support_key()].add(day)

    def _record_transition_support(
        self,
        state: tuple[int, ...],
        action: int,
        day: pd.Timestamp,
    ) -> None:
        # Replaying an epoch, or seeing the same regime in several correlated
        # ETFs on one date, must not manufacture additional support.
        self._action_dates[(state, action)].add(pd.Timestamp(day).normalize())

    def train(
        self,
        environments: dict[str, MomentumAllocationEnv],
        windows: dict[str, tuple[pd.Timestamp, pd.Timestamp]],
    ) -> None:
        rng = np.random.default_rng(self.seed)
        transitions: list[Transition] = []
        transition_keys: set[tuple[Any, ...]] = set()
        assets = sorted(environments)

        def collect_episode(asset: str, *, baseline_only: bool) -> None:
            env = environments[asset]
            state = env.reset(*windows[asset])
            while True:
                self._record_market_support(env)
                if env.i >= env.end_i:
                    break
                day = pd.Timestamp(env.dates[env.i])
                allowed = env.allowed_actions()
                baseline = env.baseline_action()
                if len(allowed) == 1:
                    action = allowed[0]
                elif baseline_only or rng.random() < 0.55:
                    action = baseline
                elif HOLD_ACTION in allowed and rng.random() < 0.35:
                    action = HOLD_ACTION
                else:
                    action = int(rng.choice(allowed))
                result = env.step(action)
                self._record_transition_support(state, action, day)
                next_allowed = tuple(env.allowed_actions()) if not result.done else ()
                next_baseline = env.baseline_action() if not result.done else HOLD_ACTION
                key = (asset, day.normalize(), state, action, result.state)
                if key not in transition_keys:
                    transition_keys.add(key)
                    transitions.append(Transition(
                        state=state,
                        action=action,
                        reward=result.reward,
                        next_state=result.state,
                        next_allowed=next_allowed,
                        next_baseline=next_baseline,
                        done=result.done,
                    ))
                state = result.state
                if result.done:
                    self._record_market_support(env)
                    break

        # One exact baseline trajectory guarantees that the safety anchor is in
        # the fixed batch. Additional policy-independent exploratory rollouts
        # cover alternative internal drawdown/exposure states.
        for asset in assets:
            collect_episode(asset, baseline_only=True)
        for _epoch in range(self.epochs):
            shuffled = assets.copy()
            rng.shuffle(shuffled)
            for asset in shuffled:
                collect_episode(asset, baseline_only=False)

        self.support = Counter({
            key: len(days) for key, days in self._support_dates.items()
        })
        for (state, action), days in self._action_dates.items():
            self.action_counts[state][action] = len(days)

        # With the unique-date support set frozen, every Bellman backup is
        # constrained to actions for which both the candidate and dynamic
        # baseline have sufficient support. No unsupported max is permitted.
        for _epoch in range(self.epochs):
            order = rng.permutation(len(transitions))
            for transition_i in order:
                transition = transitions[int(transition_i)]
                target = transition.reward
                if not transition.done:
                    backed = self.supported_actions(
                        transition.next_state,
                        transition.next_allowed,
                        transition.next_baseline,
                    )
                    if backed:
                        target += self.gamma * max(
                            self.q[transition.next_state][action]
                            for action in backed
                        )
                self.q[transition.state][transition.action] += self.alpha * (
                    target - self.q[transition.state][transition.action]
                )

    def action(self, env: MomentumAllocationEnv) -> int:
        state = env.state()
        baseline = env.baseline_action()
        allowed = env.allowed_actions()
        if allowed == [HOLD_ACTION]:
            return HOLD_ACTION
        if self.support[env.support_key()] < self.nmin:
            return baseline
        supported = self.supported_actions(state, allowed, baseline)
        if not supported:
            return baseline
        best = max(supported, key=lambda candidate: self.q[state][candidate])
        baseline_q = self.q[state][baseline]
        return best if self.q[state][best] > baseline_q + 1e-6 else baseline


def evaluate_policy(
    env: MomentumAllocationEnv,
    start: pd.Timestamp,
    end: pd.Timestamp,
    action_fn: Callable[[MomentumAllocationEnv], int],
    policy_id: str,
) -> core.PathResult:
    env.reset(start, end)
    while True:
        result = env.step(int(action_fn(env)))
        if result.done:
            break
    return env.path_result(policy_id)


def evaluate_policy_with_warmup(
    env: MomentumAllocationEnv,
    warmup_start: pd.Timestamp,
    score_start: pd.Timestamp,
    end: pd.Timestamp,
    action_fn: Callable[[MomentumAllocationEnv], int],
    policy_id: str,
    *,
    switch_at_start: bool,
) -> core.PathResult:
    """Run continuous baseline history, then optionally hand off challenger.

    The hand-off action is submitted at the close immediately before the score
    start and fills at the score-start close, so its turnover cost is included
    in the scored slice. A baseline comparator never forces a hand-off and
    therefore remains identical to lab.simulate_targets from warmup onward.
    """
    env.reset(warmup_start, end)
    score_positions = np.flatnonzero(env.dates >= score_start)
    if not len(score_positions) or score_positions[0] == 0:
        raise ValueError("score window lacks a pre-start hand-off close")
    handoff_i = int(score_positions[0]) - 1
    while True:
        if env.i < handoff_i or not switch_at_start:
            action = env.baseline_action()
        else:
            if env.i == handoff_i:
                env.force_decision_once()
            action = int(action_fn(env))
        result = env.step(action)
        if result.done:
            break
    return env.path_result(policy_id)


def ensemble_action(agents: list[ConservativeTabularAgent], env: MomentumAllocationEnv) -> int:
    actions = [agent.action(env) for agent in agents]
    ranked = sorted(actions, key=lambda action: env.action_exposure_hint(action))
    return int(ranked[len(ranked) // 2])


def utility(candidate: dict[str, Any], baseline: dict[str, Any]) -> float:
    return (
        1.25 * (candidate["cagr"] - baseline["cagr"])
        + 0.75 * (abs(baseline["max_drawdown"]) - abs(candidate["max_drawdown"]))
        + 0.05 * ((candidate.get("sharpe") or 0.0) - (baseline.get("sharpe") or 0.0))
    )


def training_windows(
    features: dict[str, pd.DataFrame],
    *,
    end: pd.Timestamp,
    mode: str,
) -> dict[str, tuple[pd.Timestamp, pd.Timestamp]]:
    windows = {}
    for asset, feature in features.items():
        first = feature.index[feature.index >= feature.index[0] + pd.DateOffset(months=13)][0]
        start = first if mode == "expanding" else max(first, end - pd.DateOffset(years=5))
        windows[asset] = (start, end)
    return windows


def make_environments(
    frame: pd.DataFrame,
    features: dict[str, pd.DataFrame],
    cash: pd.Series,
    *,
    cost_bps: float,
    funding_spread: float,
    eta: float,
) -> dict[str, MomentumAllocationEnv]:
    return {
        asset: MomentumAllocationEnv(
            frame[asset].dropna(),
            cash,
            features[asset],
            cost_bps=cost_bps,
            funding_spread=funding_spread,
            drawdown_penalty=eta,
        )
        for asset in lab.RISK_ASSETS
    }


def metrics_for_policy(
    environments: dict[str, MomentumAllocationEnv],
    cash: pd.Series,
    *,
    start: pd.Timestamp,
    end: pd.Timestamp,
    action_fn: Callable[[MomentumAllocationEnv], int],
    policy_id: str,
    switch_at_start: bool,
) -> dict[str, dict[str, Any]]:
    output = {}
    for asset, env in environments.items():
        eligible = env.features.index[
            env.features.index >= env.features.index[0] + pd.DateOffset(months=13)
        ]
        if not len(eligible):
            raise ValueError(f"{asset} lacks a 13-month continuous warm-up start")
        warmup_start = eligible[0]
        score_start = max(start, warmup_start)
        path = evaluate_policy_with_warmup(
            env,
            warmup_start,
            score_start,
            end,
            action_fn,
            policy_id,
            switch_at_start=switch_at_start,
        )
        output[asset] = core.performance_metrics(path, cash, score_start, end)
    return output


def build_study(
    frame: pd.DataFrame,
    baseline_spec: dict[str, Any],
    *,
    cost_bps: float,
    funding_spread: float,
    epochs: int,
) -> dict[str, Any]:
    end = min(frame[ticker].last_valid_index() for ticker in lab.DOWNLOAD_TICKERS)
    cash = lab.cash_returns(frame, frame.index[frame.index <= end])
    features = {
        asset: build_features(frame[asset].dropna().loc[:end], frame, baseline_spec)
        for asset in lab.RISK_ASSETS
    }
    seeds = (7, 17, 29)
    grid = [
        {"mode": mode, "eta": eta, "nmin": nmin}
        for mode in ("expanding", "rolling5")
        for eta in (1.0, 3.0)
        for nmin in (20, 40)
    ]
    validation_results = []
    for config in grid:
        agents = []
        for seed in seeds:
            envs = make_environments(
                frame, features, cash,
                cost_bps=cost_bps, funding_spread=funding_spread, eta=config["eta"],
            )
            agent = ConservativeTabularAgent(
                alpha=0.05, gamma=0.99, nmin=config["nmin"], epochs=epochs, seed=seed
            )
            agent.train(envs, training_windows(features, end=TRAIN_END, mode=config["mode"]))
            agents.append(agent)
        eval_envs = make_environments(
            frame, features, cash,
            cost_bps=cost_bps, funding_spread=funding_spread, eta=config["eta"],
        )
        rl_metrics = metrics_for_policy(
            eval_envs, cash,
            start=VALIDATION_START, end=VALIDATION_END,
            action_fn=lambda env, agents=agents: ensemble_action(agents, env),
            policy_id="rl_validation",
            switch_at_start=True,
        )
        baseline_envs = make_environments(
            frame, features, cash,
            cost_bps=cost_bps, funding_spread=funding_spread, eta=config["eta"],
        )
        baseline_metrics = metrics_for_policy(
            baseline_envs, cash,
            start=VALIDATION_START, end=VALIDATION_END,
            action_fn=lambda env: env.baseline_action(),
            policy_id="deterministic_baseline",
            switch_at_start=False,
        )
        utilities = [utility(rl_metrics[a], baseline_metrics[a]) for a in lab.RISK_ASSETS]
        validation_results.append({
            **config,
            "score": float(np.median(utilities) + 0.5 * np.quantile(utilities, 0.25)),
            "positive_asset_rate": float(np.mean(np.asarray(utilities) >= 0)),
            "metrics": {"rl": rl_metrics, "baseline": baseline_metrics},
        })
    validation_results.sort(key=lambda row: (-row["score"], -row["positive_asset_rate"], row["mode"], row["eta"], row["nmin"]))
    chosen = validation_results[0]

    final_agents = []
    for seed in seeds:
        envs = make_environments(
            frame, features, cash,
            cost_bps=cost_bps, funding_spread=funding_spread, eta=chosen["eta"],
        )
        agent = ConservativeTabularAgent(
            alpha=0.05, gamma=0.99, nmin=chosen["nmin"], epochs=epochs, seed=seed
        )
        agent.train(envs, training_windows(features, end=VALIDATION_END, mode=chosen["mode"]))
        final_agents.append(agent)

    replay_envs = make_environments(
        frame, features, cash,
        cost_bps=cost_bps, funding_spread=funding_spread, eta=chosen["eta"],
    )
    replay_rl = metrics_for_policy(
        replay_envs, cash,
        start=REPLAY_START, end=end,
        action_fn=lambda env: ensemble_action(final_agents, env),
        policy_id="rl_replay",
        switch_at_start=True,
    )
    baseline_envs = make_environments(
        frame, features, cash,
        cost_bps=cost_bps, funding_spread=funding_spread, eta=chosen["eta"],
    )
    replay_baseline = metrics_for_policy(
        baseline_envs, cash,
        start=REPLAY_START, end=end,
        action_fn=lambda env: env.baseline_action(),
        policy_id="deterministic_replay",
        switch_at_start=False,
    )
    replay_rows = []
    for asset in lab.RISK_ASSETS:
        rl = replay_rl[asset]
        baseline = replay_baseline[asset]
        replay_rows.append({
            "asset": asset,
            "rl": rl,
            "baseline": baseline,
            "dominates": bool(
                rl["cagr"] >= baseline["cagr"]
                and abs(rl["max_drawdown"]) < abs(baseline["max_drawdown"])
            ),
            "strict": bool(
                rl["cagr"] >= baseline["cagr"]
                and abs(rl["max_drawdown"]) <= 0.85 * abs(baseline["max_drawdown"])
            ),
        })

    # metrics_for_policy leaves every environment at the end of the complete
    # replay. Preserve that continuous exposure/drawdown history for current
    # diagnostics; a short reset would create a different policy state.
    current = {}
    for asset, env in replay_envs.items():
        rl_action = ensemble_action(final_agents, env)
        baseline_action = env.baseline_action()
        current[asset] = {
            "date": str(env.dates[env.i].date()),
            "replay_start": str(REPLAY_START.date()),
            "rl_action": action_name(rl_action),
            "rl_target_exposure_hint": env.action_exposure_hint(rl_action),
            "baseline_action": action_name(baseline_action),
            "baseline_target_exposure_hint": env.action_exposure_hint(baseline_action),
            "filled_exposure": env.exposure,
            "portfolio_drawdown": env.portfolio_drawdown(),
            "running_max_drawdown": env.max_drawdown,
            "state": list(env.state()),
            "support": min(agent.support[env.support_key()] for agent in final_agents),
            "rl_action_support": min(
                agent.action_support(env.state(), rl_action)
                for agent in final_agents
            ),
            "baseline_action_support": min(
                agent.action_support(env.state(), baseline_action)
                for agent in final_agents
            ),
        }

    strict_count = sum(row["strict"] for row in replay_rows)
    historical_gate = (
        "PAPER_CANDIDATE" if strict_count >= 4 else "FAIL_TO_QUALIFY"
    )
    decision = "INCONCLUSIVE_RESEARCH"
    return {
        "schemaVersion": 1,
        "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "researchOnly": True,
        "decisionGrade": False,
        "decision": decision,
        "reason": (
            "Historical replay cannot establish safe policy improvement; "
            "a prospective shadow period is required."
        ),
        "actions": ACTIONS.tolist(),
        "special_actions": {
            str(HOLD_ACTION): "HOLD",
            str(BASELINE_ACTION): "dynamic continuous BASELINE",
        },
        "baseline_spec": baseline_spec,
        "protocol": {
            "train_end": str(TRAIN_END.date()),
            "validation": [str(VALIDATION_START.date()), str(VALIDATION_END.date())],
            "replay": [str(REPLAY_START.date()), str(end.date())],
            "replay_is_untouched": False,
            "seeds": list(seeds),
            "epochs": epochs,
            "alpha": 0.05,
            "gamma": 0.99,
            "cost_bps": cost_bps,
            "funding_spread": funding_spread,
            "top3_in_training": False,
            "decision_schedule": "policy-lab rebalance calendar; HOLD-only otherwise",
            "score_boundary": (
                "continuous dynamic-baseline warm-up from each asset's eligible start; "
                "challenger hand-off is submitted at the prior close and its turnover "
                "cost is included in the first scored return"
            ),
            "baseline_comparator": (
                "uninterrupted policy-lab dynamic baseline; metrics are a slice of "
                "the full continuous path"
            ),
            "support_unit": "unique calendar dates per state-action; duplicate epochs/assets do not add support",
            "bellman_backup": "supported actions only, with supported dynamic baseline required",
            "fallback": "dynamic deterministic baseline when either baseline or candidate support is insufficient",
        },
        "hyperparameter_trials": json_safe(validation_results),
        "chosen_config": {key: chosen[key] for key in ("mode", "eta", "nmin", "score", "positive_asset_rate")},
        "replay": json_safe(replay_rows),
        "strict_replay_assets": strict_count,
        "historical_gate": historical_gate,
        "current": json_safe(current),
        "limitations": [
            "This is a support-constrained tabular engineering baseline, not SPIBB and not a formal safe-RL guarantee.",
            "Highly correlated ETF histories do not create independent samples.",
            "The 2024+ replay was exposed by prior deterministic research.",
            "Unique-date support reduces replay inflation but does not make correlated dates or assets independent.",
            "A prospective shadow period is required before any live use.",
        ],
    }


def pct(value: Any) -> str:
    return "—" if value is None else f"{float(value) * 100:.1f}%"


def render_report(payload: dict[str, Any]) -> str:
    lines = [
        "# 时序强化学习动量环境与保守基准",
        "",
        f"生成：{payload['generated_at']}  ",
        f"决定：**{payload['decision']}** — {payload['reason']}",
        f"历史门槛：**{payload['historical_gate']}**（仅供研究，不是 safe-RL 结论）",
        "",
        "## 环境",
        "",
        "- 动作：0 / 25% / 50% / 75% / 100% / 125% 目标、HOLD、动态连续 BASELINE。",
        "- 非 policy-lab 再平衡日只能 HOLD；初始化与再平衡日才允许选择目标。",
        "- 每段评分前先从资产可用起点连续运行普通基线；challenger 在评分起点前一收盘接管，首日计入切换成本。",
        "- baseline comparator 不在评分边界重置，指标是原 policy-lab 完整连续路径的切片。",
        "- 收盘 t 观察并选择；旧仓承受 t→t+1 收益；t+1 收盘漂移后成交并付成本。",
        "- 状态：6/10/12月趋势票、63日波动状态、SMA200、市场回撤、组合回撤、历史 MDD、当前仓位及决策状态。",
        "- 奖励：净 log return − η×新增最大回撤平方；Bellman backup 只使用有唯一日期支持且 baseline 同样有支持的动作。",
        "- Top‑3 因 point-in-time 偏差完全排除。",
        "",
        "## 选择的 RL 配置",
        "",
        "```json",
        json.dumps(payload["chosen_config"], ensure_ascii=False, indent=2),
        "```",
        "",
        "## 2024+ replay",
        "",
        "| 资产 | 普通基线 CAGR / MDD | RL CAGR / MDD | 同时占优 | 严格通过 |",
        "|---|---:|---:|---|---|",
    ]
    for row in payload["replay"]:
        lines.append(
            f"| {row['asset']} | {pct(row['baseline']['cagr'])} / {pct(row['baseline']['max_drawdown'])} | "
            f"{pct(row['rl']['cagr'])} / {pct(row['rl']['max_drawdown'])} | "
            f"{'是' if row['dominates'] else '否'} | {'是' if row['strict'] else '否'} |"
        )
    lines.extend([
        "",
        f"严格通过：**{payload['strict_replay_assets']}/{len(payload['replay'])}**。",
        "",
        "## 当前动作对照",
        "",
        "| 资产 | RL / 目标提示 | 普通基线 / 目标提示 | 状态支持数 | 状态 |",
        "|---|---:|---:|---:|---|",
    ])
    for asset, row in payload["current"].items():
        lines.append(
            f"| {asset} | {row['rl_action']} / {pct(row['rl_target_exposure_hint'])} | "
            f"{row['baseline_action']} / {pct(row['baseline_target_exposure_hint'])} | "
            f"{row['support']} | `{row['state']}` |"
        )
    lines.extend([
        "",
        "## 接受纪律",
        "",
        "本实验无论历史门槛是否通过都保持 INCONCLUSIVE_RESEARCH。历史通过最多成为 paper-trade 候选，不能宣称安全策略改进；当前 replay 已被看过，不能替代未来 6–12 个月 prospective shadow test。",
        "",
    ])
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--policy-lab", type=Path, default=DEFAULT_POLICY_LAB)
    parser.add_argument("--cache", type=Path, default=lab.DEFAULT_CACHE)
    parser.add_argument("--cost-bps", type=float, default=10.0)
    parser.add_argument("--funding-spread", type=float, default=0.03)
    parser.add_argument("--epochs", type=int, default=15)
    args = parser.parse_args()
    if args.epochs <= 0 or args.cost_bps < 0 or args.funding_spread < 0:
        parser.error("epochs must be positive; costs must be non-negative")
    frame = lab.read_cache(args.cache)
    problems = lab.validate_prices(frame)
    if problems:
        raise RuntimeError("extended cache invalid: " + "; ".join(problems))
    document = json.loads(args.policy_lab.read_text(encoding="utf-8"))
    baseline_spec = document["favorite_spec"]
    payload = build_study(
        frame,
        baseline_spec,
        cost_bps=args.cost_bps,
        funding_spread=args.funding_spread,
        epochs=args.epochs,
    )
    args.out_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(args.out_dir / "momentum_rl_research.json", json_safe(payload))
    atomic_write_text(args.out_dir / "momentum_rl_research.md", render_report(payload))
    print(f"wrote {args.out_dir / 'momentum_rl_research.json'}")
    print(f"wrote {args.out_dir / 'momentum_rl_research.md'}")
    print(
        f"decision {payload['decision']} | gate {payload['historical_gate']} | "
        f"strict {payload['strict_replay_assets']}/{len(lab.RISK_ASSETS)}"
    )


if __name__ == "__main__":
    main()

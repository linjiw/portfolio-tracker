#!/usr/bin/env python3
"""Final validation of one unified monthly asymmetric trend policy.

The research grid is deliberately small and interpretable.  For each ETF, a
completed month-end close is compared with its trailing 6-, 10-, or 12-month
simple average.  The policy targets ``risk_on`` above the average and
``risk_off`` otherwise.  That target is filled at the following session's
close, pays one-way turnover cost, and then drifts until the next rebalance.

Parameter selection uses only observations through 2023.  The 2024+ segment is
reported as a fixed replay because prior studies in this repository have
already exposed it.  A separate annual pseudo-OOS diagnostic uses a rolling
five-calendar-year train, the prior-year validation period, and a 21-session
embargo before each test year.  Top-3 constituent strategies are intentionally
out of scope.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import platform
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

try:
    from scripts.artifact_io import atomic_write_json, atomic_write_text
except ModuleNotFoundError:  # direct ``python scripts/...`` execution
    from artifact_io import atomic_write_json, atomic_write_text


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CACHE = ROOT / "output" / "momentum_policy_lab" / "extended_prices.csv.gz"
DEFAULT_OUT = ROOT / "output" / "momentum_final_candidate"

RISK_ASSETS = ("SPY", "QQQ", "PDP", "MTUM", "MMTM", "SPMO", "QMOM")
REQUIRED_COLUMNS = (*RISK_ASSETS, "^IRX")
MONTH_GRID = (6, 10, 12)
RISK_ON_GRID = (1.0, 1.15, 1.25)
RISK_OFF_GRID = (0.0, 0.25, 0.5)
FOCUS_ID = "m10_on125_off50"
BASELINE_ID = "buy_hold"

TRAIN_END = pd.Timestamp("2021-12-31")
VALIDATION_START = pd.Timestamp("2022-01-01")
VALIDATION_END = pd.Timestamp("2023-12-31")
REPLAY_START = pd.Timestamp("2024-01-01")
EMBARGO_SESSIONS = 21
TRADING_DAYS = 252.0
EPS = 1e-12


@dataclass
class Simulation:
    equity: pd.Series
    exposure: pd.Series
    turnover: pd.Series
    events: list[dict[str, Any]]
    spec: dict[str, Any]


def _safe_float(value: Any, digits: int = 8) -> float | None:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return round(value, digits) if math.isfinite(value) else None


def json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if isinstance(value, (pd.Timestamp, dt.datetime, dt.date)):
        return value.isoformat()
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        return _safe_float(value)
    if isinstance(value, (np.bool_,)):
        return bool(value)
    return value


def sha256_path(path: Path) -> str | None:
    if not path.exists():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------- data layer
def validate_extended_prices(frame: pd.DataFrame) -> list[str]:
    errors: list[str] = []
    if frame.empty or len(frame) < 2500:
        return ["extended price cache is empty or too short"]
    if frame.index.has_duplicates or not frame.index.is_monotonic_increasing:
        errors.append("price dates must be unique and increasing")
    for ticker in REQUIRED_COLUMNS:
        if ticker not in frame:
            errors.append(f"missing {ticker}")
            continue
        series = pd.to_numeric(frame[ticker], errors="coerce").dropna()
        if len(series) < 1000:
            errors.append(f"{ticker} has insufficient observations")
        if ticker != "^IRX" and (series <= 0).any():
            errors.append(f"{ticker} contains non-positive prices")
    if all(ticker in frame for ticker in REQUIRED_COLUMNS):
        common_end = min(frame[ticker].last_valid_index() for ticker in REQUIRED_COLUMNS)
        if common_end < REPLAY_START:
            errors.append("cache does not reach the fixed 2024+ replay")
    return errors


def read_extended_cache(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"extended price cache not found: {path}")
    frame = pd.read_csv(path, index_col=0, parse_dates=True, compression="gzip")
    frame.index = pd.to_datetime(frame.index, errors="coerce")
    frame = frame.loc[~frame.index.isna()].sort_index()
    for column in frame.columns:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    problems = validate_extended_prices(frame)
    if problems:
        raise ValueError("; ".join(problems))
    return frame


def cash_returns(frame: pd.DataFrame, index: pd.DatetimeIndex) -> pd.Series:
    """Prior-observation 13-week T-bill proxy, compounded by calendar days."""
    annual_pct = frame["^IRX"].reindex(index).ffill().shift(1).fillna(0.0)
    days = index.to_series().diff().dt.days.fillna(1.0).clip(lower=1.0)
    annual = (annual_pct / 100.0).clip(lower=-0.99)
    values = np.power(1.0 + annual, days / 365.25) - 1.0
    return pd.Series(values, index=index, dtype=float).fillna(0.0)


# -------------------------------------------------------------- policy grid
def candidate_id(months: int, risk_on: float, risk_off: float) -> str:
    return (
        f"m{int(months)}_on{int(round(risk_on * 100))}_"
        f"off{int(round(risk_off * 100))}"
    )


def candidate_grid() -> list[dict[str, Any]]:
    return [
        {
            "id": candidate_id(months, risk_on, risk_off),
            "months": months,
            "risk_on": risk_on,
            "risk_off": risk_off,
            "max_exposure": risk_on,
        }
        for months in MONTH_GRID
        for risk_on in RISK_ON_GRID
        for risk_off in RISK_OFF_GRID
    ]


def baseline_spec() -> dict[str, Any]:
    return {
        "id": BASELINE_ID,
        "months": None,
        "risk_on": 1.0,
        "risk_off": 1.0,
        "max_exposure": 1.0,
    }


def focus_neighbor_ids() -> list[str]:
    """Focus plus immediate one-axis grid neighbors (the local platform)."""
    focus = (MONTH_GRID.index(10), RISK_ON_GRID.index(1.25), RISK_OFF_GRID.index(0.5))
    output = [FOCUS_ID]
    for axis, values in enumerate((MONTH_GRID, RISK_ON_GRID, RISK_OFF_GRID)):
        for step in (-1, 1):
            location = list(focus)
            location[axis] += step
            if 0 <= location[axis] < len(values):
                output.append(candidate_id(
                    MONTH_GRID[location[0]],
                    RISK_ON_GRID[location[1]],
                    RISK_OFF_GRID[location[2]],
                ))
    return output


def monthly_asymmetric_target(
    price: pd.Series,
    spec: dict[str, Any],
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Return daily target, trend state, and completed month-end signals."""
    price = price.dropna().sort_index()
    if spec["id"] == BASELINE_ID:
        target = pd.Series(1.0, index=price.index, dtype=float)
        state = pd.Series(True, index=price.index, dtype=bool)
        return target, state, pd.Series(False, index=price.index, dtype=bool)

    periods = price.index.to_period("M")
    month_close = price.groupby(periods).last()
    months = int(spec["months"])
    month_average = month_close.rolling(months, min_periods=months).mean()
    month_state = month_close > month_average

    observed_month_ends = price.groupby(periods).tail(1).index
    # The final data month is not complete unless a later-month bar exists.
    completed = observed_month_ends[
        observed_month_ends.to_period("M") < price.index[-1].to_period("M")
    ]
    updates = pd.Series(np.nan, index=price.index, dtype=float)
    updates.loc[completed] = month_state.reindex(completed.to_period("M")).astype(float).values
    state = updates.ffill().fillna(0.0).astype(bool)
    target = pd.Series(float(spec["risk_off"]), index=price.index, dtype=float)
    target = target.where(~state, float(spec["risk_on"]))
    return target, state, updates.notna()


def asset_start(price: pd.Series) -> pd.Timestamp:
    """Uniform 13-month warm-up so every grid member has ample history."""
    price = price.dropna().sort_index()
    raw_start = price.index[0]
    eligible = price.index[price.index >= raw_start + pd.DateOffset(months=13)]
    if not len(eligible):
        raise ValueError("asset lacks a 13-month warm-up")
    return eligible[0]


# ------------------------------------------------------------ execution layer
def simulate(
    price: pd.Series,
    cash: pd.Series,
    target: pd.Series,
    rebalance_signal: pd.Series,
    spec: dict[str, Any],
    *,
    start: pd.Timestamp,
    end: pd.Timestamp,
    cost_bps: float,
    funding_spread: float,
) -> Simulation:
    """Close-t signal / next-close fill with drifting risky exposure."""
    if cost_bps < 0 or funding_spread < 0:
        raise ValueError("cost and funding spread must be non-negative")
    price = price.dropna().sort_index()
    dates = price.index[price.index <= end]
    starts = np.flatnonzero(dates >= start)
    if not len(starts) or starts[0] == 0:
        raise ValueError("simulation needs one pre-start observation")
    dates = dates[int(starts[0]) - 1:]
    price = price.reindex(dates)
    target = target.reindex(dates).ffill().fillna(float(spec.get("risk_off", 0.0)))
    rebalance_signal = rebalance_signal.reindex(dates).fillna(False).astype(bool)
    cash = cash.reindex(dates).fillna(0.0)

    equity = 1.0
    exposure = 0.0
    pending: float | None = float(target.iloc[0])
    cost_rate = cost_bps / 10000.0
    equity_values = [equity]
    exposures = [exposure]
    turnovers = [0.0]
    events: list[dict[str, Any]] = []

    for i in range(1, len(dates)):
        day, prior = dates[i], dates[i - 1]
        risky_return = float(price.at[day] / price.at[prior] - 1.0)
        financing_return = float(cash.at[day])
        if exposure > 1.0 + EPS:
            calendar_days = max(1, int((day - prior).days))
            spread_return = (1.0 + funding_spread) ** (calendar_days / 365.25) - 1.0
            financing_return = (1.0 + financing_return) * (1.0 + spread_return) - 1.0

        portfolio_factor = (
            exposure * (1.0 + risky_return)
            + (1.0 - exposure) * (1.0 + financing_return)
        )
        if portfolio_factor <= 0:
            raise ValueError(f"portfolio equity became non-positive on {day.date()}")
        equity *= portfolio_factor
        exposure = exposure * (1.0 + risky_return) / portfolio_factor

        turnover = 0.0
        if pending is not None:
            desired = float(np.clip(pending, 0.0, float(spec["max_exposure"])))
            turnover = abs(desired - exposure)
            if turnover > EPS:
                equity *= max(0.0, 1.0 - turnover * cost_rate)
                events.append({
                    "date": str(day.date()),
                    "from": _safe_float(exposure, 6),
                    "to": _safe_float(desired, 6),
                    "turnover": _safe_float(turnover, 6),
                })
                exposure = desired
            pending = None

        if bool(rebalance_signal.at[day]):
            pending = float(target.at[day])

        equity_values.append(equity)
        exposures.append(exposure)
        turnovers.append(turnover)

    return Simulation(
        equity=pd.Series(equity_values, index=dates, dtype=float),
        exposure=pd.Series(exposures, index=dates, dtype=float),
        turnover=pd.Series(turnovers, index=dates, dtype=float),
        events=events,
        spec=spec,
    )


# -------------------------------------------------------------- performance
def segment(series: pd.Series, start: pd.Timestamp, end: pd.Timestamp) -> pd.Series:
    series = series.dropna().sort_index()
    prior = series.loc[series.index < start].tail(1)
    within = series.loc[(series.index >= start) & (series.index <= end)]
    return pd.concat([prior, within])[lambda value: ~value.index.duplicated(keep="last")]


def performance(
    path: Simulation,
    cash: pd.Series,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> dict[str, Any]:
    equity = segment(path.equity, start, end)
    empty_keys = (
        "total_x", "cagr", "max_drawdown", "sharpe", "calmar",
        "annual_vol", "exposure", "annual_turnover",
    )
    if len(equity) < 2 or equity.iloc[0] <= 0:
        return {key: None for key in empty_keys}
    returns = equity.pct_change().dropna()
    years = max((equity.index[-1] - equity.index[0]).days / 365.25, 1.0 / TRADING_DAYS)
    total_x = float(equity.iloc[-1] / equity.iloc[0])
    cagr = total_x ** (1.0 / years) - 1.0
    drawdown = equity / equity.cummax() - 1.0
    max_drawdown = float(drawdown.min())
    annual_vol = float(returns.std(ddof=1) * math.sqrt(TRADING_DAYS)) if len(returns) > 1 else 0.0
    excess = returns - cash.reindex(returns.index).fillna(0.0)
    sharpe = (
        float(excess.mean() / returns.std(ddof=1) * math.sqrt(TRADING_DAYS))
        if len(returns) > 1 and returns.std(ddof=1) > 0
        else None
    )
    exposure = path.exposure.reindex(returns.index).fillna(0.0)
    turnover = path.turnover.reindex(returns.index).fillna(0.0)
    return {
        "total_x": _safe_float(total_x, 6),
        "cagr": _safe_float(cagr),
        "max_drawdown": _safe_float(max_drawdown),
        "sharpe": _safe_float(sharpe, 6),
        "calmar": _safe_float(cagr / abs(max_drawdown), 6) if max_drawdown < 0 else None,
        "annual_vol": _safe_float(annual_vol),
        "exposure": _safe_float(float(exposure.mean()), 6),
        "annual_turnover": _safe_float(float(turnover.sum()) / years, 6),
        "observations": int(len(returns)),
        "start": str(equity.index[0].date()),
        "end": str(equity.index[-1].date()),
    }


def utility(candidate: dict[str, Any], baseline: dict[str, Any]) -> float:
    if candidate.get("cagr") is None or baseline.get("cagr") is None:
        return -1e9
    cagr_gain = candidate["cagr"] - baseline["cagr"]
    drawdown_gain = abs(baseline["max_drawdown"]) - abs(candidate["max_drawdown"])
    sharpe_gain = (candidate.get("sharpe") or 0.0) - (baseline.get("sharpe") or 0.0)
    return float(1.25 * cagr_gain + 0.75 * drawdown_gain + 0.05 * sharpe_gain)


def dominates(candidate: dict[str, Any], baseline: dict[str, Any]) -> bool:
    return bool(
        candidate.get("cagr") is not None
        and candidate["cagr"] >= baseline["cagr"]
        and abs(candidate["max_drawdown"]) < abs(baseline["max_drawdown"])
    )


def strict(candidate: dict[str, Any], baseline: dict[str, Any]) -> bool:
    return bool(
        candidate.get("cagr") is not None
        and candidate["cagr"] >= baseline["cagr"]
        and abs(candidate["max_drawdown"]) <= 0.85 * abs(baseline["max_drawdown"])
    )


# ----------------------------------------------------------- development rank
def rank_development(
    metric_book: dict[str, dict[str, dict[str, dict[str, Any]]]],
    specs: dict[str, dict[str, Any]],
    assets: list[str] | tuple[str, ...] = RISK_ASSETS,
) -> list[dict[str, Any]]:
    """Rank only train/validation keys; replay content is deliberately ignored."""
    rows: list[dict[str, Any]] = []
    for candidate in candidate_grid():
        candidate_id_value = candidate["id"]
        scores = []
        strict_assets = 0
        for asset in assets:
            folds = metric_book[candidate_id_value][asset]
            baseline = metric_book[BASELINE_ID][asset]
            train_score = utility(folds["train"], baseline["train"])
            validation_score = utility(folds["validation"], baseline["validation"])
            scores.append(min(train_score, validation_score))
            if strict(folds["train"], baseline["train"]) and strict(
                folds["validation"], baseline["validation"]
            ):
                strict_assets += 1
        values = np.asarray(scores, dtype=float)
        score = float(np.median(values) + 0.5 * np.quantile(values, 0.25))
        positive_rate = float(np.mean(values >= 0.0))
        rows.append({
            "id": candidate_id_value,
            "score": score,
            "median_asset_score": float(np.median(values)),
            "p25_asset_score": float(np.quantile(values, 0.25)),
            "positive_asset_rate": positive_rate,
            "strict_assets": strict_assets,
            "eligible": bool(score > 0 and positive_rate >= 2.0 / 3.0),
            "spec": specs[candidate_id_value],
        })
    rows.sort(key=lambda row: (not row["eligible"], -row["score"], row["id"]))
    for rank, row in enumerate(rows, 1):
        row["rank"] = rank
    return rows


def development_selection(ranking: list[dict[str, Any]]) -> str:
    return next((row["id"] for row in ranking if row["eligible"]), BASELINE_ID)


def build_paths(
    frame: pd.DataFrame,
    specs: dict[str, dict[str, Any]],
    cash: pd.Series,
    *,
    end: pd.Timestamp,
    cost_bps: float,
    funding_spread: float,
) -> tuple[
    dict[str, dict[str, Simulation]],
    dict[str, dict[str, dict[str, pd.Series]]],
    dict[str, pd.Timestamp],
]:
    paths: dict[str, dict[str, Simulation]] = {asset: {} for asset in RISK_ASSETS}
    diagnostics: dict[str, dict[str, dict[str, pd.Series]]] = {
        asset: {} for asset in RISK_ASSETS
    }
    starts: dict[str, pd.Timestamp] = {}
    for asset in RISK_ASSETS:
        price = frame[asset].dropna().loc[:end]
        starts[asset] = asset_start(price)
        for candidate_id_value, spec in specs.items():
            target, state, rebalance = monthly_asymmetric_target(price, spec)
            paths[asset][candidate_id_value] = simulate(
                price,
                cash,
                target,
                rebalance,
                spec,
                start=starts[asset],
                end=end,
                cost_bps=cost_bps,
                funding_spread=funding_spread,
            )
            diagnostics[asset][candidate_id_value] = {
                "target": target,
                "state": state,
                "rebalance": rebalance,
            }
    return paths, diagnostics, starts


def build_metric_book(
    paths: dict[str, dict[str, Simulation]],
    cash: pd.Series,
    starts: dict[str, pd.Timestamp],
    *,
    end: pd.Timestamp,
) -> dict[str, dict[str, dict[str, dict[str, Any]]]]:
    metric_book: dict[str, dict[str, dict[str, dict[str, Any]]]] = {
        candidate_id_value: {} for candidate_id_value in next(iter(paths.values()))
    }
    for candidate_id_value in metric_book:
        for asset in RISK_ASSETS:
            start = starts[asset]
            path = paths[asset][candidate_id_value]
            metric_book[candidate_id_value][asset] = {
                "train": performance(path, cash, start, min(TRAIN_END, end)),
                "validation": performance(
                    path,
                    cash,
                    max(VALIDATION_START, start),
                    min(VALIDATION_END, end),
                ),
                "replay": performance(path, cash, max(REPLAY_START, start), end),
            }
    return metric_book


def build_three_segment_diagnostics(
    metric_book: dict[str, dict[str, dict[str, dict[str, Any]]]],
    candidate_id_value: str = FOCUS_ID,
) -> dict[str, dict[str, dict[str, Any]]]:
    """Expose the candidate versus baseline in every predeclared segment."""
    output: dict[str, dict[str, dict[str, Any]]] = {}
    for asset in RISK_ASSETS:
        output[asset] = {}
        for fold in ("train", "validation", "replay"):
            candidate = metric_book[candidate_id_value][asset][fold]
            baseline = metric_book[BASELINE_ID][asset][fold]
            output[asset][fold] = {
                "baseline": baseline,
                "candidate": candidate,
                "utility": utility(candidate, baseline),
                "dominates": dominates(candidate, baseline),
            }
    return output


# --------------------------------------------------------- annual pseudo-OOS
def annual_fold_schedule(
    frame: pd.DataFrame,
    starts: dict[str, pd.Timestamp],
    *,
    end: pd.Timestamp,
) -> list[dict[str, Any]]:
    calendar = frame["SPY"].dropna().index
    folds: list[dict[str, Any]] = []
    for year in range(2006, end.year + 1):
        test_start = pd.Timestamp(year=year, month=1, day=1)
        test_end = min(end, pd.Timestamp(year=year, month=12, day=31))
        prior_sessions = calendar[calendar < test_start]
        if len(prior_sessions) <= EMBARGO_SESSIONS:
            continue
        validation_end = prior_sessions[-(EMBARGO_SESSIONS + 1)]
        validation_start = pd.Timestamp(year=year - 1, month=1, day=1)
        train_start = pd.Timestamp(year=year - 6, month=1, day=1)
        train_end = pd.Timestamp(year=year - 2, month=12, day=31)
        eligible_assets = [
            asset for asset in RISK_ASSETS
            if starts[asset] <= train_start
            and frame[asset].last_valid_index() >= test_start
        ]
        embargo = calendar[(calendar > validation_end) & (calendar < test_start)]
        if (
            len(eligible_assets) < 2
            or validation_end < validation_start
            or len(embargo) != EMBARGO_SESSIONS
        ):
            continue
        folds.append({
            "year": year,
            "train": [str(train_start.date()), str(train_end.date())],
            "validation": [str(validation_start.date()), str(validation_end.date())],
            "embargo_sessions": int(len(embargo)),
            "test": [str(test_start.date()), str(test_end.date())],
            "eligible_assets": eligible_assets,
        })
    return folds


def annual_pseudo_oos(
    frame: pd.DataFrame,
    paths: dict[str, dict[str, Simulation]],
    diagnostics: dict[str, dict[str, dict[str, pd.Series]]],
    specs: dict[str, dict[str, Any]],
    starts: dict[str, pd.Timestamp],
    cash: pd.Series,
    *,
    end: pd.Timestamp,
    cost_bps: float,
    funding_spread: float,
) -> dict[str, Any]:
    folds = annual_fold_schedule(frame, starts, end=end)
    for fold in folds:
        fold_metrics: dict[str, dict[str, dict[str, dict[str, Any]]]] = {
            candidate_id_value: {} for candidate_id_value in specs
        }
        train_start, train_end = map(pd.Timestamp, fold["train"])
        validation_start, validation_end = map(pd.Timestamp, fold["validation"])
        for candidate_id_value in specs:
            for asset in fold["eligible_assets"]:
                path = paths[asset][candidate_id_value]
                fold_metrics[candidate_id_value][asset] = {
                    "train": performance(path, cash, train_start, train_end),
                    "validation": performance(path, cash, validation_start, validation_end),
                }
        ranking = rank_development(fold_metrics, specs, fold["eligible_assets"])
        selected = development_selection(ranking)
        fold["selected_id"] = selected
        fold["selected_score"] = next(
            (row["score"] for row in ranking if row["id"] == selected), 0.0
        )

    asset_results: dict[str, Any] = {}
    for asset in RISK_ASSETS:
        asset_folds = [fold for fold in folds if asset in fold["eligible_assets"]]
        if not asset_folds:
            continue
        price = frame[asset].dropna().loc[:end]
        first_test = pd.Timestamp(asset_folds[0]["test"][0])
        last_test = pd.Timestamp(asset_folds[-1]["test"][1])
        target = pd.Series(np.nan, index=price.index, dtype=float)
        rebalance = pd.Series(False, index=price.index, dtype=bool)
        for fold in asset_folds:
            selected = fold["selected_id"]
            test_start, test_end = map(pd.Timestamp, fold["test"])
            mask = (price.index >= test_start) & (price.index <= test_end)
            source = diagnostics[asset][selected]
            target.loc[mask] = source["target"].reindex(price.index[mask]).values
            rebalance.loc[mask] = source["rebalance"].reindex(
                price.index[mask]
            ).fillna(False).values
            prior = price.index[price.index < test_start]
            if len(prior):
                boundary = prior[-1]
                target.at[boundary] = float(source["target"].at[boundary])
                rebalance.at[boundary] = True
        target = target.ffill().fillna(0.0)
        meta_spec = {
            "id": "annual_pseudo_oos",
            "risk_off": 0.0,
            "max_exposure": max(RISK_ON_GRID),
        }
        dynamic = simulate(
            price,
            cash,
            target,
            rebalance,
            meta_spec,
            start=first_test,
            end=last_test,
            cost_bps=cost_bps,
            funding_spread=funding_spread,
        )
        overall_dynamic = performance(dynamic, cash, first_test, last_test)
        overall_baseline = performance(paths[asset][BASELINE_ID], cash, first_test, last_test)
        overall_focus = performance(paths[asset][FOCUS_ID], cash, first_test, last_test)
        pre_end = min(last_test, VALIDATION_END)
        replay_start = max(first_test, REPLAY_START)
        asset_results[asset] = {
            "start": str(first_test.date()),
            "end": str(last_test.date()),
            "dynamic": overall_dynamic,
            "focus_fixed": overall_focus,
            "baseline": overall_baseline,
            "dynamic_dominates": dominates(overall_dynamic, overall_baseline),
            "focus_dominates": dominates(overall_focus, overall_baseline),
            "pre_2024": {
                "dynamic": performance(dynamic, cash, first_test, pre_end),
                "focus_fixed": performance(paths[asset][FOCUS_ID], cash, first_test, pre_end),
                "baseline": performance(paths[asset][BASELINE_ID], cash, first_test, pre_end),
            } if first_test <= pre_end else None,
            "replay_2024_plus": {
                "dynamic": performance(dynamic, cash, replay_start, last_test),
                "focus_fixed": performance(paths[asset][FOCUS_ID], cash, replay_start, last_test),
                "baseline": performance(paths[asset][BASELINE_ID], cash, replay_start, last_test),
            } if replay_start <= last_test else None,
        }

    frequency: dict[str, int] = {}
    pre_frequency: dict[str, int] = {}
    for fold in folds:
        selected = fold["selected_id"]
        frequency[selected] = frequency.get(selected, 0) + 1
        if fold["year"] <= 2023:
            pre_frequency[selected] = pre_frequency.get(selected, 0) + 1
    sorter = lambda item: (-item[1], item[0])
    return {
        "note": (
            "Pseudo-OOS, not untouched: every test year uses a rolling five-calendar-year "
            "train, prior-year validation ending before a 21-session embargo, then that "
            "year's test. 2024+ remains labeled replay."
        ),
        "folds": folds,
        "selection_frequency": dict(sorted(frequency.items(), key=sorter)),
        "pre_2024_selection_frequency": dict(sorted(pre_frequency.items(), key=sorter)),
        "assets": asset_results,
        "dynamic_dominates_assets": sum(
            row["dynamic_dominates"] for row in asset_results.values()
        ),
        "focus_dominates_assets": sum(
            row["focus_dominates"] for row in asset_results.values()
        ),
    }


# ------------------------------------------------------------- replay/stress
def aggregate_replay(rows: list[dict[str, Any]]) -> dict[str, Any]:
    cagr_deltas = [row["strategy"]["cagr"] - row["baseline"]["cagr"] for row in rows]
    drawdown_improvements = [
        abs(row["baseline"]["max_drawdown"]) - abs(row["strategy"]["max_drawdown"])
        for row in rows
    ]
    return {
        "dominates_assets": sum(row["dominates"] for row in rows),
        "strict_assets": sum(row["strict"] for row in rows),
        "median_cagr_delta": float(np.median(cagr_deltas)),
        "median_drawdown_improvement": float(np.median(drawdown_improvements)),
        "worst_cagr_delta": float(np.min(cagr_deltas)),
    }


def evaluate_replay_candidate(
    candidate_id_value: str,
    frame: pd.DataFrame,
    specs: dict[str, dict[str, Any]],
    paths: dict[str, dict[str, Simulation]],
    diagnostics: dict[str, dict[str, dict[str, pd.Series]]],
    starts: dict[str, pd.Timestamp],
    cash: pd.Series,
    *,
    end: pd.Timestamp,
    cost_bps: float,
    funding_spread: float,
    stress_cost_bps: float,
    stress_funding_spread: float,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for asset in RISK_ASSETS:
        start = max(REPLAY_START, starts[asset])
        price = frame[asset].dropna().loc[:end]
        source = diagnostics[asset][candidate_id_value]
        cost_stress_path = simulate(
            price,
            cash,
            source["target"],
            source["rebalance"],
            specs[candidate_id_value],
            start=starts[asset],
            end=end,
            cost_bps=stress_cost_bps,
            funding_spread=funding_spread,
        )
        funding_stress_path = simulate(
            price,
            cash,
            source["target"],
            source["rebalance"],
            specs[candidate_id_value],
            start=starts[asset],
            end=end,
            cost_bps=cost_bps,
            funding_spread=stress_funding_spread,
        )
        baseline = performance(paths[asset][BASELINE_ID], cash, start, end)
        strategy = performance(paths[asset][candidate_id_value], cash, start, end)
        doubled_cost = performance(cost_stress_path, cash, start, end)
        funding_stress = performance(funding_stress_path, cash, start, end)
        rows.append({
            "asset": asset,
            "baseline": baseline,
            "strategy": strategy,
            "doubled_cost": doubled_cost,
            "funding_plus_6pct": funding_stress,
            "dominates": dominates(strategy, baseline),
            "strict": strict(strategy, baseline),
        })
    return {
        "candidate_id": candidate_id_value,
        "rows": rows,
        "aggregate": aggregate_replay(rows),
    }


# --------------------------------------------------------------- orchestration
def build_payload(
    frame: pd.DataFrame,
    *,
    cache_path: Path,
    cost_bps: float = 10.0,
    funding_spread: float = 0.03,
    stress_cost_bps: float = 20.0,
    stress_funding_spread: float = 0.06,
) -> dict[str, Any]:
    problems = validate_extended_prices(frame)
    if problems:
        raise ValueError("; ".join(problems))
    if stress_cost_bps < cost_bps or stress_funding_spread < funding_spread:
        raise ValueError("stress assumptions must not be cheaper than base assumptions")
    end = min(frame[ticker].last_valid_index() for ticker in REQUIRED_COLUMNS)
    index = frame.index[frame.index <= end]
    cash = cash_returns(frame, index)
    specs = {spec["id"]: spec for spec in [baseline_spec(), *candidate_grid()]}
    paths, diagnostics, starts = build_paths(
        frame,
        specs,
        cash,
        end=end,
        cost_bps=cost_bps,
        funding_spread=funding_spread,
    )
    metric_book = build_metric_book(paths, cash, starts, end=end)
    ranking = rank_development(metric_book, specs)
    selected_id = development_selection(ranking)
    ranking_by_id = {row["id"]: row for row in ranking}

    platform_ids = focus_neighbor_ids()
    stress_ids = list(dict.fromkeys([selected_id, *platform_ids]))
    replay = {
        candidate_id_value: evaluate_replay_candidate(
            candidate_id_value,
            frame,
            specs,
            paths,
            diagnostics,
            starts,
            cash,
            end=end,
            cost_bps=cost_bps,
            funding_spread=funding_spread,
            stress_cost_bps=stress_cost_bps,
            stress_funding_spread=stress_funding_spread,
        )
        for candidate_id_value in stress_ids
    }
    platform_rows = [
        {
            **{key: value for key, value in ranking_by_id[candidate_id_value].items() if key != "spec"},
            "replay": replay[candidate_id_value]["aggregate"],
        }
        for candidate_id_value in platform_ids
    ]
    focus_rank = ranking_by_id[FOCUS_ID]
    platform_positive = sum(row["score"] > 0 for row in platform_rows)
    focus_status = (
        "DEVELOPMENT_SUPPORTED"
        if focus_rank["eligible"] and platform_positive >= 3
        else "CHALLENGER_NOT_DEVELOPMENT_SELECTED"
    )

    pseudo_oos = annual_pseudo_oos(
        frame,
        paths,
        diagnostics,
        specs,
        starts,
        cash,
        end=end,
        cost_bps=cost_bps,
        funding_spread=funding_spread,
    )

    current: dict[str, Any] = {}
    for asset in RISK_ASSETS:
        day = frame[asset].dropna().index[-1]
        focus_diag = diagnostics[asset][FOCUS_ID]
        selected_diag = diagnostics[asset][selected_id]
        current[asset] = {
            "as_of": str(day.date()),
            "price": _safe_float(frame.at[day, asset], 4),
            "focus_trend_on": bool(focus_diag["state"].at[day]),
            "focus_target": _safe_float(focus_diag["target"].at[day], 4),
            "focus_filled_exposure": _safe_float(paths[asset][FOCUS_ID].exposure.at[day], 4),
            "selected_target": _safe_float(selected_diag["target"].at[day], 4),
            "selected_filled_exposure": _safe_float(paths[asset][selected_id].exposure.at[day], 4),
        }

    return {
        "schemaVersion": 1,
        "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "researchOnly": True,
        "decisionGrade": False,
        "data": {
            "source": "existing extended Yahoo-adjusted market-price cache",
            "as_of": str(end.date()),
            "cache_path": str(cache_path),
            "cache_sha256": sha256_path(cache_path),
            "coverage": {
                ticker: {
                    "start": str(frame[ticker].first_valid_index().date()),
                    "end": str(frame[ticker].last_valid_index().date()),
                    "observations": int(frame[ticker].notna().sum()),
                }
                for ticker in REQUIRED_COLUMNS
            },
            "python": platform.python_version(),
            "pandas": pd.__version__,
            "numpy": np.__version__,
        },
        "protocol": {
            "assets": list(RISK_ASSETS),
            "top3_excluded": True,
            "grid": {
                "months": list(MONTH_GRID),
                "risk_on": list(RISK_ON_GRID),
                "risk_off": list(RISK_OFF_GRID),
                "candidate_count": len(candidate_grid()),
            },
            "signal": "completed month-end close versus trailing monthly SMA",
            "fill": "next session close",
            "between_rebalances": "risky and cash weights drift",
            "cost_bps_per_one_way_turnover": cost_bps,
            "cash": "prior-observation 13-week T-bill proxy",
            "funding_spread_over_tbill": funding_spread,
            "stress_cost_bps": stress_cost_bps,
            "stress_funding_spread_over_tbill": stress_funding_spread,
            "train_end": str(TRAIN_END.date()),
            "validation": [str(VALIDATION_START.date()), str(VALIDATION_END.date())],
            "replay_start": str(REPLAY_START.date()),
            "replay_used_for_selection": False,
            "replay_label": "fixed replay, not untouched holdout",
        },
        "development": {
            "selection_rule": (
                "median plus 0.5 times lower-quartile of each asset's worse "
                "train/validation utility; score>0 and >=2/3 assets non-negative"
            ),
            "selected_id": selected_id,
            "selected_spec": specs[selected_id],
            "ranking": json_safe(ranking),
        },
        "focus_candidate": {
            "id": FOCUS_ID,
            "spec": specs[FOCUS_ID],
            "status": focus_status,
            "development_rank": focus_rank["rank"],
            "development_score": focus_rank["score"],
            "development_eligible": focus_rank["eligible"],
            "neighbor_ids": platform_ids[1:],
            "platform_positive_members": platform_positive,
            "platform_size": len(platform_ids),
            "platform": json_safe(platform_rows),
        },
        "focus_three_segments": json_safe(
            build_three_segment_diagnostics(metric_book, FOCUS_ID)
        ),
        "replay_2024_plus": json_safe(replay),
        "pseudo_oos": json_safe(pseudo_oos),
        "current": json_safe(current),
        "limitations": [
            "2024+ is a replay because prior repository reports already exposed it.",
            "Yahoo-adjusted market-price bars are research proxies, not official ETF NAV total returns.",
            "Adjusted OHLC does not validate intraday stops or executable stop fills.",
            "The ETF panel shares one US equity path and is not seven independent macro samples.",
            "Leverage ignores taxes, margin calls, broker-specific haircuts, and nonlinear market impact.",
            "Pseudo-OOS is a chronology audit, not a new untouched experiment.",
        ],
    }


def pct(value: Any) -> str:
    return "—" if value is None else f"{float(value) * 100:.1f}%"


def render_report(payload: dict[str, Any]) -> str:
    development = payload["development"]
    focus = payload["focus_candidate"]
    selected = development["selected_id"]
    replay = payload["replay_2024_plus"]
    lines = [
        "# 最终候选验证：统一月频非对称趋势",
        "",
        f"生成：{payload['generated_at']}  ",
        f"数据截止：{payload['data']['as_of']}  ",
        "数据为 Yahoo 调整后市场价格回报代理；2024+ 是固定 replay，不是 untouched holdout。",
        "",
        "## 结论先行",
        "",
        f"- 仅使用 2023 年及以前数据，开发期选中：**`{selected}`**。",
        f"- 预指定重点 `10m / on 1.25 / off 0.50` 的开发排名："
        f"**{focus['development_rank']}/27**，状态：**{focus['status']}**。",
        f"- 重点候选邻域中开发分为正：**{focus['platform_positive_members']}/{focus['platform_size']}**。",
        f"- 开发选中策略在 2024+ 同时提高 CAGR 且降低回撤："
        f"**{replay[selected]['aggregate']['dominates_assets']}/7**。",
        "- Top‑3 完全排除；所有 ETF 共用同一参数和成交语义。",
        "",
        "## 预注册协议",
        "",
        "- 月末收盘观察价格与 6/10/12 月 SMA；下一交易日收盘调仓。",
        "- 风险开启仓位：1.00/1.15/1.25；风险关闭仓位：0/0.25/0.50。",
        "- 仓位在月度调仓之间自然漂移；单边换手 10bp。",
        "- 现金赚取前一观测的 13 周 T-bill；超过 100% 的融资支付 T-bill+3%。",
        "- 压力测试分别为 20bp 单边成本、T-bill+6% 融资。",
        "",
        "## 重点候选三段复验",
        "",
        "每格为 Buy&Hold CAGR / MDD → 重点候选 CAGR / MDD；train 与 validation 用于开发，2024+ 只作固定 replay。",
        "",
        "| 资产 | Train | Validation | 2024+ replay |",
        "|---|---:|---:|---:|",
    ]
    for asset, folds in payload["focus_three_segments"].items():
        cells = []
        for fold in ("train", "validation", "replay"):
            row = folds[fold]
            cells.append(
                f"{pct(row['baseline']['cagr'])} / {pct(row['baseline']['max_drawdown'])} → "
                f"{pct(row['candidate']['cagr'])} / {pct(row['candidate']['max_drawdown'])}"
            )
        lines.append(f"| {asset} | {cells[0]} | {cells[1]} | {cells[2]} |")

    lines.extend([
        "",
        "## 开发期排名（不看 2024+）",
        "",
        "| 排名 | 参数 | 分数 | 正效用资产率 | train+validation 严格资产 | 通过 |",
        "|---:|---|---:|---:|---:|---|",
    ])
    for row in development["ranking"][:12]:
        lines.append(
            f"| {row['rank']} | `{row['id']}` | {row['score']:.4f} | "
            f"{pct(row['positive_asset_rate'])} | {row['strict_assets']} | "
            f"{'PASS' if row['eligible'] else 'FAIL'} |"
        )

    lines.extend([
        "",
        "## 开发选中策略：2024+ 固定 replay 与压力",
        "",
        "| 资产 | Buy&Hold CAGR / MDD | 开发选中 CAGR / MDD | 20bp CAGR | 融资+6% CAGR | 同时占优 |",
        "|---|---:|---:|---:|---:|---|",
    ])
    for row in replay[selected]["rows"]:
        lines.append(
            f"| {row['asset']} | {pct(row['baseline']['cagr'])} / {pct(row['baseline']['max_drawdown'])} | "
            f"{pct(row['strategy']['cagr'])} / {pct(row['strategy']['max_drawdown'])} | "
            f"{pct(row['doubled_cost']['cagr'])} | {pct(row['funding_plus_6pct']['cagr'])} | "
            f"{'是' if row['dominates'] else '否'} |"
        )
    selected_aggregate = replay[selected]["aggregate"]
    lines.extend([
        "",
        f"开发选中策略同时占优：**{selected_aggregate['dominates_assets']}/7**；"
        f"中位 CAGR 差：**{pct(selected_aggregate['median_cagr_delta'])}**；"
        f"中位 MDD 改善：**{pct(selected_aggregate['median_drawdown_improvement'])}**。",
        "",
        "## 重点候选：2024+ 固定 replay 与压力",
        "",
        "| 资产 | Buy&Hold CAGR / MDD | 重点候选 CAGR / MDD | 20bp CAGR / MDD | 融资+6% CAGR / MDD | 同时占优 |",
        "|---|---:|---:|---:|---:|---|",
    ])
    for row in replay[FOCUS_ID]["rows"]:
        lines.append(
            f"| {row['asset']} | {pct(row['baseline']['cagr'])} / {pct(row['baseline']['max_drawdown'])} | "
            f"{pct(row['strategy']['cagr'])} / {pct(row['strategy']['max_drawdown'])} | "
            f"{pct(row['doubled_cost']['cagr'])} / {pct(row['doubled_cost']['max_drawdown'])} | "
            f"{pct(row['funding_plus_6pct']['cagr'])} / {pct(row['funding_plus_6pct']['max_drawdown'])} | "
            f"{'是' if row['dominates'] else '否'} |"
        )
    aggregate = replay[FOCUS_ID]["aggregate"]
    lines.extend([
        "",
        f"重点候选同时提高 CAGR 且降低回撤：**{aggregate['dominates_assets']}/7**；"
        f"严格通过：**{aggregate['strict_assets']}/7**；中位 CAGR 差："
        f"**{pct(aggregate['median_cagr_delta'])}**。",
        "",
        "## 重点候选邻居平台",
        "",
        "相邻定义为只沿 months、risk_on 或 risk_off 一个轴移动一个网格点。",
        "",
        "| 参数 | 开发排名 | 开发分 | 正效用资产率 | replay 同时占优资产 | 中位 CAGR 差 | 中位 MDD 改善 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ])
    for row in focus["platform"]:
        lines.append(
            f"| `{row['id']}` | {row['rank']} | {row['score']:.4f} | "
            f"{pct(row['positive_asset_rate'])} | {row['replay']['dominates_assets']} | "
            f"{pct(row['replay']['median_cagr_delta'])} | "
            f"{pct(row['replay']['median_drawdown_improvement'])} |"
        )

    pseudo = payload["pseudo_oos"]
    frequencies = ", ".join(
        f"`{candidate}`×{count}"
        for candidate, count in list(pseudo["selection_frequency"].items())[:8]
    )
    lines.extend([
        "",
        "## 年度 rolling pseudo-OOS",
        "",
        pseudo["note"],
        "",
        f"年度选择频率：{frequencies or '无'}。",
        "",
        f"动态年度选择同时占优：**{pseudo['dynamic_dominates_assets']}/{len(pseudo['assets'])}**；"
        f"固定重点候选同时占优：**{pseudo['focus_dominates_assets']}/{len(pseudo['assets'])}**。",
        "",
        "| 资产 | 区间 | Buy&Hold CAGR / MDD | 动态年度选择 CAGR / MDD | 固定重点 CAGR / MDD |",
        "|---|---|---:|---:|---:|",
    ])
    for asset, row in pseudo["assets"].items():
        lines.append(
            f"| {asset} | {row['start']}→{row['end']} | "
            f"{pct(row['baseline']['cagr'])} / {pct(row['baseline']['max_drawdown'])} | "
            f"{pct(row['dynamic']['cagr'])} / {pct(row['dynamic']['max_drawdown'])} | "
            f"{pct(row['focus_fixed']['cagr'])} / {pct(row['focus_fixed']['max_drawdown'])} |"
        )

    lines.extend([
        "",
        "## 当前研究状态",
        "",
        "当前目标是最后一个已完成月末的状态参考；实际仓位会在调仓后漂移。",
        "",
        "| 资产 | 重点趋势 | 重点目标 | 重点已漂移仓位 | 开发选中目标 |",
        "|---|---|---:|---:|---:|",
    ])
    for asset, state in payload["current"].items():
        lines.append(
            f"| {asset} | {'ON' if state['focus_trend_on'] else 'OFF'} | "
            f"{pct(state['focus_target'])} | {pct(state['focus_filled_exposure'])} | "
            f"{pct(state['selected_target'])} |"
        )
    lines.extend([
        "",
        "## 解释边界",
        "",
        "- 这是一项参数很少的可解释规则研究，不是交易指令。",
        "- 调整后 OHLC 不能验证盘中 stop；本研究只使用月末收盘确认和下一收盘成交。",
        "- 七个 ETF 共处同一美国市场，跨资产结果不能当作七个独立宏观样本。",
        "- 2024+ 已在先前报告中出现，因此所有相关数字都明确叫 replay。",
        "",
    ])
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--cost-bps", type=float, default=10.0)
    parser.add_argument("--funding-spread", type=float, default=0.03)
    parser.add_argument("--stress-cost-bps", type=float, default=20.0)
    parser.add_argument("--stress-funding-spread", type=float, default=0.06)
    args = parser.parse_args()

    frame = read_extended_cache(args.cache)
    payload = build_payload(
        frame,
        cache_path=args.cache,
        cost_bps=args.cost_bps,
        funding_spread=args.funding_spread,
        stress_cost_bps=args.stress_cost_bps,
        stress_funding_spread=args.stress_funding_spread,
    )
    args.out_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.out_dir / "momentum_final_candidate.json"
    report_path = args.out_dir / "momentum_final_candidate.md"
    atomic_write_json(json_path, json_safe(payload))
    atomic_write_text(report_path, render_report(payload))
    print(f"wrote {json_path}")
    print(f"wrote {report_path}")
    print(
        f"development selected {payload['development']['selected_id']} | "
        f"focus rank {payload['focus_candidate']['development_rank']}/27 | "
        f"focus replay dominates "
        f"{payload['replay_2024_plus'][FOCUS_ID]['aggregate']['dominates_assets']}/7"
    )


if __name__ == "__main__":
    main()

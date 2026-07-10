#!/usr/bin/env python3
"""Cross-asset momentum policy lab with a fixed 2024+ replay segment.

This second-stage study searches for one interpretable allocation policy across
SPY, QQQ and four momentum ETFs.  It deliberately avoids per-asset tuning.  A
policy combines:

* 6/10/12-month trend votes;
* optional 21/63-session realized-volatility scaling;
* an optional fast drawdown/high-volatility brake;
* optional parent-market risk caps; and
* a hard 0..1.25 exposure range with explicit funding spread.

Every close-t target fills at close t+1.  The 2024+ segment is not used by the
ranking function, but prior reports already exposed it, so it is labeled replay
rather than untouched holdout.  Generated artifacts are research-only.
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import io
import json
import math
import platform
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

try:
    from scripts.artifact_io import atomic_write_bytes, atomic_write_json, atomic_write_text
    from scripts import momentum_overlay_research as core
except ModuleNotFoundError:  # direct execution
    from artifact_io import atomic_write_bytes, atomic_write_json, atomic_write_text
    import momentum_overlay_research as core


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "output" / "momentum_policy_lab"
DEFAULT_CACHE = DEFAULT_OUT / "extended_prices.csv.gz"
RISK_ASSETS = ("SPY", "QQQ", "PDP", "MTUM", "MMTM", "SPMO", "QMOM")
DOWNLOAD_TICKERS = (*RISK_ASSETS, "^IRX")
TRAIN_END = pd.Timestamp("2021-12-31")
VALIDATION_START = pd.Timestamp("2022-01-01")
VALIDATION_END = pd.Timestamp("2023-12-31")
HOLDOUT_START = pd.Timestamp("2024-01-01")
TRADING_DAYS = 252.0
EPS = 1e-12


def sha256_path(path: Path) -> str | None:
    if not path.exists():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_float(value: Any, digits: int = 8) -> float | None:
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
        return _safe_float(value)
    if isinstance(value, (np.bool_,)):
        return bool(value)
    return value


# ---------------------------------------------------------------- data layer
def flatten_download(raw: pd.DataFrame) -> pd.DataFrame:
    if raw.empty or not isinstance(raw.columns, pd.MultiIndex):
        return pd.DataFrame()
    output = {}
    for ticker in DOWNLOAD_TICKERS:
        try:
            output[ticker] = pd.to_numeric(raw[("Close", ticker)], errors="coerce")
        except KeyError:
            continue
    frame = pd.DataFrame(output)
    frame.index = pd.to_datetime(frame.index, errors="coerce")
    if getattr(frame.index, "tz", None) is not None:
        frame.index = frame.index.tz_convert(None)
    return frame.loc[~frame.index.isna()].sort_index().dropna(how="all")


def validate_prices(frame: pd.DataFrame) -> list[str]:
    errors = []
    if frame.empty or len(frame) < 2500:
        return ["extended price frame is empty or too short"]
    if frame.index.has_duplicates or not frame.index.is_monotonic_increasing:
        errors.append("dates must be ordered and unique")
    for ticker in DOWNLOAD_TICKERS:
        if ticker not in frame:
            errors.append(f"missing {ticker}")
            continue
        series = frame[ticker].dropna()
        if len(series) < 1000:
            errors.append(f"{ticker} has insufficient observations")
        if ticker != "^IRX" and (series <= 0).any():
            errors.append(f"{ticker} contains non-positive adjusted closes")
    return errors


def read_cache(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path, index_col=0, parse_dates=True, compression="gzip")
    frame.index = pd.to_datetime(frame.index)
    return frame.sort_index()


def write_cache(frame: pd.DataFrame, path: Path) -> None:
    buffer = io.StringIO()
    frame.to_csv(buffer)
    atomic_write_bytes(path, gzip.compress(buffer.getvalue().encode("utf-8"), 6))


def load_prices(cache: Path, *, no_fetch: bool) -> tuple[pd.DataFrame, str, str | None]:
    cached = None
    cache_error = None
    if cache.exists():
        try:
            candidate = read_cache(cache)
            problems = validate_prices(candidate)
            if problems:
                cache_error = "; ".join(problems)
            else:
                cached = candidate
        except Exception as exc:  # pragma: no cover - corrupt runtime artifact
            cache_error = f"{type(exc).__name__}: {exc}"
    if no_fetch:
        if cached is None:
            raise RuntimeError(f"--no-fetch requires a valid extended cache: {cache_error}")
        return cached, "extended_price_cache", cache_error

    import yfinance as yf

    try:
        raw = yf.download(
            list(DOWNLOAD_TICKERS),
            start="1993-01-01",
            end=str((pd.Timestamp(dt.date.today()) + pd.Timedelta(days=1)).date()),
            auto_adjust=True,
            actions=False,
            progress=False,
            threads=False,
        )
        frame = flatten_download(raw)
        problems = validate_prices(frame)
        if problems:
            raise ValueError("; ".join(problems))
        write_cache(frame, cache)
        round_trip = read_cache(cache)
        problems = validate_prices(round_trip)
        if problems:
            raise ValueError("cache round-trip failed: " + "; ".join(problems))
        return round_trip, "yfinance_live_download", None
    except Exception as exc:
        if cached is None:
            raise RuntimeError(f"extended download failed without fallback: {exc}") from exc
        return cached, "extended_price_cache_fallback", f"{type(exc).__name__}: {exc}"


def cash_returns(frame: pd.DataFrame, index: pd.DatetimeIndex) -> pd.Series:
    annual_pct = frame["^IRX"].reindex(index).ffill().shift(1).fillna(0.0)
    days = index.to_series().diff().dt.days.fillna(1.0).clip(lower=1.0)
    return pd.Series(
        np.power(1.0 + (annual_pct / 100.0).clip(lower=-0.99), days / 365.25) - 1.0,
        index=index,
        dtype=float,
    ).fillna(0.0)


# ------------------------------------------------------------ policy signals
def trend_votes(price: pd.Series, months: tuple[int, ...]) -> pd.Series:
    votes = sum(core.monthly_signal(price, n, "sma").astype(float) for n in months)
    return votes / float(len(months))


def realized_vol(price: pd.Series, mode: str) -> pd.Series:
    returns = price.pct_change(fill_method=None)
    v21 = returns.rolling(21, min_periods=21).std() * math.sqrt(TRADING_DAYS)
    v63 = returns.rolling(63, min_periods=63).std() * math.sqrt(TRADING_DAYS)
    if mode == "21":
        return v21
    if mode == "63":
        return v63
    if mode == "blend":
        return 0.5 * v21 + 0.5 * v63
    raise ValueError(mode)


def brake_state(
    price: pd.Series,
    *,
    drawdown_trigger: float,
    vol_quantile: float,
    reclaim_days: int,
) -> pd.Series:
    """Asymmetric daily stress brake with close-confirmed reclaim."""
    ema21 = price.ewm(span=21, adjust=False, min_periods=21).mean()
    ema50 = price.ewm(span=50, adjust=False, min_periods=50).mean()
    vol21 = price.pct_change(fill_method=None).rolling(21, min_periods=21).std() * math.sqrt(TRADING_DAYS)
    high_vol = vol21 > vol21.expanding(min_periods=252).quantile(vol_quantile)
    drawdown = price / price.rolling(126, min_periods=63).max() - 1.0
    prior_high = price.rolling(reclaim_days, min_periods=reclaim_days).max().shift(1)
    trigger = (drawdown <= -drawdown_trigger) | ((price < ema50) & high_vol)
    reclaim = (price > ema21) & (price > prior_high)
    active = False
    output = []
    for day in price.index:
        if not active and bool(trigger.get(day, False)):
            active = True
        elif active and bool(reclaim.get(day, False)):
            active = False
        output.append(active)
    return pd.Series(output, index=price.index, dtype=bool)


def parent_stress(frame: pd.DataFrame, index: pd.DatetimeIndex) -> pd.Series:
    spy = frame["SPY"].reindex(index).ffill()
    qqq = frame["QQQ"].reindex(index).ffill()
    spy200 = spy.rolling(200, min_periods=200).mean()
    qqq200 = qqq.rolling(200, min_periods=200).mean()
    return ((spy < spy200) & (qqq < qqq200)).fillna(False)


def target_exposure(
    price: pd.Series,
    frame: pd.DataFrame,
    spec: dict[str, Any],
) -> tuple[pd.Series, dict[str, pd.Series]]:
    index = price.index
    if spec["family"] == "baseline":
        ones = pd.Series(1.0, index=index)
        return ones, {
            "target": ones,
            "votes": ones,
            "vol": pd.Series(np.nan, index=index),
            "brake": pd.Series(False, index=index),
            "parent": pd.Series(False, index=index),
            "rebalance": pd.Series(False, index=index),
        }

    months = tuple(spec.get("months", (6, 10, 12)))
    votes = trend_votes(price, months)
    floor = float(spec["floor"])
    ceiling = float(spec["ceiling"])
    target = floor + (ceiling - floor) * votes
    vol = pd.Series(np.nan, index=index)
    if spec.get("target_vol"):
        vol = realized_vol(price, spec.get("vol_mode", "blend"))
        scaler = (float(spec["target_vol"]) / vol.replace(0, np.nan)).clip(
            float(spec.get("scale_min", 0.75)), float(spec.get("scale_max", 1.25))
        )
        target = target * scaler.fillna(1.0)

    brake = pd.Series(False, index=index)
    if spec.get("brake"):
        brake = brake_state(
            price,
            drawdown_trigger=float(spec["drawdown_trigger"]),
            vol_quantile=float(spec.get("vol_quantile", 0.75)),
            reclaim_days=int(spec.get("reclaim_days", 20)),
        )
        target = target.where(~brake, np.minimum(target, float(spec["brake_cap"])))

    parent = pd.Series(False, index=index)
    if spec.get("parent_cap") is not None:
        parent = parent_stress(frame, index)
        target = target.where(~parent, np.minimum(target, float(spec["parent_cap"])))

    maximum = float(spec.get("max_exposure", ceiling))
    target = target.clip(0.0, maximum).fillna(floor)
    periods = index.to_period("M")
    monthly = pd.Series(
        periods != pd.Series(periods, index=index).shift(-1).to_numpy(),
        index=index,
        dtype=bool,
    )
    weeks = index.to_period("W")
    weekly = pd.Series(
        weeks != pd.Series(weeks, index=index).shift(-1).to_numpy(),
        index=index,
        dtype=bool,
    )
    rebalance = weekly if spec.get("target_vol") else monthly
    if spec.get("brake"):
        rebalance = rebalance | brake.ne(brake.shift(1)).fillna(False)
    if spec.get("parent_cap") is not None:
        rebalance = rebalance | parent.ne(parent.shift(1)).fillna(False)
    return target, {
        "target": target,
        "votes": votes,
        "vol": vol,
        "brake": brake,
        "parent": parent,
        "rebalance": rebalance.fillna(False),
    }


# ------------------------------------------------------------ execution layer
def simulate_targets(
    price: pd.Series,
    cash: pd.Series,
    targets: pd.Series,
    rebalance_signal: pd.Series,
    spec: dict[str, Any],
    *,
    start: pd.Timestamp,
    end: pd.Timestamp,
    cost_bps: float,
    funding_spread: float,
) -> core.PathResult:
    price = price.dropna().sort_index()
    dates = price.index[price.index <= end]
    starts = np.flatnonzero(dates >= start)
    if not len(starts) or starts[0] == 0:
        raise ValueError("target simulation needs a pre-start signal observation")
    dates = dates[int(starts[0]) - 1:]
    price = price.reindex(dates)
    target = targets.reindex(dates).ffill().fillna(0.0)
    cash = cash.reindex(dates).fillna(0.0)
    equity = 1.0
    exposure = 0.0
    pending: float | None = float(target.iloc[0])
    rebalance_signal = rebalance_signal.reindex(dates).fillna(False).astype(bool)
    cost = cost_bps / 10000.0
    eq_values = [equity]
    exposures = [exposure]
    turnovers = [0.0]
    events: list[dict[str, Any]] = []

    for i in range(1, len(dates)):
        day, prior = dates[i], dates[i - 1]
        risky_return = float(price.at[day] / price.at[prior] - 1.0)
        financing = float(cash.at[day])
        if exposure > 1.0:
            calendar_days = max(1, int((day - prior).days))
            financing += (1.0 + funding_spread) ** (calendar_days / 365.25) - 1.0
        portfolio_factor = (
            exposure * (1.0 + risky_return)
            + (1.0 - exposure) * (1.0 + financing)
        )
        if portfolio_factor <= 0:
            raise ValueError(f"portfolio equity became non-positive on {day.date()}")
        equity *= portfolio_factor
        exposure = exposure * (1.0 + risky_return) / portfolio_factor

        turnover = 0.0
        if pending is not None:
            desired = min(float(spec.get("max_exposure", 1.0)), max(0.0, pending))
            turnover = abs(desired - exposure)
            if turnover > EPS:
                equity *= max(0.0, 1.0 - turnover * cost)
                events.append({
                    "date": str(day.date()),
                    "from": round(exposure, 6),
                    "to": round(desired, 6),
                    "turnover": round(turnover, 6),
                })
                exposure = desired
            pending = None
        if bool(rebalance_signal.at[day]):
            pending = float(target.at[day])
        eq_values.append(equity)
        exposures.append(exposure)
        turnovers.append(turnover)

    return core.PathResult(
        equity=pd.Series(eq_values, index=dates, dtype=float),
        exposure=pd.Series(exposures, index=dates, dtype=float),
        turnover=pd.Series(turnovers, index=dates, dtype=float),
        events=events,
        variant=spec,
    )


# --------------------------------------------------------------- candidates
def candidates() -> list[dict[str, Any]]:
    result = [{"id": "baseline", "family": "baseline", "max_exposure": 1.0, "complexity": 0}]
    month_sets = ((6, 10, 12), (6, 9, 12), (8, 10, 12))
    for months in month_sets:
        tag = "_".join(map(str, months))
        for floor in (0.25, 0.5):
            for ceiling in (1.0, 1.25):
                result.append({
                    "id": f"vote_{tag}_f{int(floor*100)}_c{int(ceiling*100)}",
                    "family": "vote", "months": months, "floor": floor,
                    "ceiling": ceiling, "max_exposure": ceiling, "complexity": 2,
                })
    for floor in (0.25, 0.5):
        for ceiling in (1.0, 1.25):
            for target_vol in (0.15, 0.18, 0.21):
                for mode in ("63", "blend"):
                    result.append({
                        "id": f"vote_vol_f{int(floor*100)}_c{int(ceiling*100)}_t{int(target_vol*100)}_{mode}",
                        "family": "vote_vol", "months": (6, 10, 12),
                        "floor": floor, "ceiling": ceiling, "max_exposure": ceiling,
                        "target_vol": target_vol, "vol_mode": mode,
                        "scale_min": 0.75, "scale_max": 1.25, "complexity": 4,
                    })
    for floor in (0.25, 0.5):
        for ceiling in (1.0, 1.25):
            for target_vol in (0.18, 0.21):
                for drawdown in (0.08, 0.10, 0.12):
                    for brake_cap in (0.25, 0.5):
                        result.append({
                            "id": (
                                f"vote_brake_f{int(floor*100)}_c{int(ceiling*100)}_"
                                f"t{int(target_vol*100)}_dd{int(drawdown*100)}_cap{int(brake_cap*100)}"
                            ),
                            "family": "vote_vol_brake", "months": (6, 10, 12),
                            "floor": floor, "ceiling": ceiling, "max_exposure": ceiling,
                            "target_vol": target_vol, "vol_mode": "blend",
                            "scale_min": 0.75, "scale_max": 1.25,
                            "brake": True, "drawdown_trigger": drawdown,
                            "brake_cap": brake_cap, "vol_quantile": 0.75,
                            "reclaim_days": 20, "complexity": 6,
                        })
    for floor in (0.25, 0.5):
        for ceiling in (1.0, 1.25):
            result.append({
                "id": f"vote_parent_f{int(floor*100)}_c{int(ceiling*100)}",
                "family": "vote_parent", "months": (6, 10, 12),
                "floor": floor, "ceiling": ceiling, "max_exposure": ceiling,
                "parent_cap": 0.5, "complexity": 3,
            })
    return result


def asset_start(series: pd.Series) -> pd.Timestamp:
    raw_start = series.first_valid_index()
    eligible = series.dropna().index[series.dropna().index >= raw_start + pd.DateOffset(months=13)]
    if not len(eligible):
        raise ValueError("asset lacks 13-month warmup")
    return eligible[0]


def utility(candidate: dict[str, Any], baseline: dict[str, Any]) -> float:
    if candidate.get("cagr") is None:
        return -1e9
    cagr_gain = candidate["cagr"] - baseline["cagr"]
    dd_gain = abs(baseline["max_drawdown"]) - abs(candidate["max_drawdown"])
    sharpe_gain = (candidate.get("sharpe") or 0.0) - (baseline.get("sharpe") or 0.0)
    return float(1.25 * cagr_gain + 0.75 * dd_gain + 0.05 * sharpe_gain)


def rank_candidates(
    metrics: dict[str, dict[str, dict[str, dict[str, Any]]]],
    specs: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    rows = []
    for candidate_id, by_asset in metrics.items():
        if candidate_id == "baseline":
            continue
        asset_scores = []
        strict_assets = 0
        for asset, folds in by_asset.items():
            base = metrics["baseline"][asset]
            train_u = utility(folds["train"], base["train"])
            validation_u = utility(folds["validation"], base["validation"])
            robust = min(train_u, validation_u)
            asset_scores.append(robust)
            if all(
                folds[fold]["cagr"] >= base[fold]["cagr"]
                and abs(folds[fold]["max_drawdown"]) <= 0.85 * abs(base[fold]["max_drawdown"])
                for fold in ("train", "validation")
            ):
                strict_assets += 1
        scores = np.asarray(asset_scores, dtype=float)
        score = float(np.median(scores) + 0.5 * np.quantile(scores, 0.25))
        positive_rate = float(np.mean(scores >= 0))
        rows.append({
            "id": candidate_id,
            "family": specs[candidate_id]["family"],
            "score": score,
            "positive_asset_rate": positive_rate,
            "strict_development_assets": strict_assets,
            "median_asset_score": float(np.median(scores)),
            "p25_asset_score": float(np.quantile(scores, 0.25)),
            "complexity": int(specs[candidate_id].get("complexity", 1)),
        })

    family_rows: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        family_rows.setdefault(row["family"], []).append(row)
    for row in rows:
        stable = sum(
            peer["score"] > 0 and peer["positive_asset_rate"] >= 2 / 3
            for peer in family_rows[row["family"]]
        )
        row["stable_family_members"] = stable
        row["eligible"] = bool(
            row["score"] > 0
            and row["positive_asset_rate"] >= 2 / 3
            and stable >= 2
        )
    rows.sort(key=lambda row: (not row["eligible"], -row["score"], row["complexity"], row["id"]))
    return rows


def annual_walk_forward(
    frame: pd.DataFrame,
    paths: dict[str, dict[str, core.PathResult]],
    diagnostics: dict[str, dict[str, dict[str, pd.Series]]],
    specs: dict[str, dict[str, Any]],
    cash: pd.Series,
    *,
    end: pd.Timestamp,
    cost_bps: float,
    funding_spread: float,
) -> dict[str, Any]:
    """Nested annual pseudo-OOS selection with a 21-session embargo."""
    starts = {asset: asset_start(frame[asset].dropna()) for asset in RISK_ASSETS}
    calendar = frame["SPY"].dropna().index
    folds = []
    for year in range(2006, end.year + 1):
        test_start = pd.Timestamp(year=year, month=1, day=1)
        test_end = min(end, pd.Timestamp(year=year, month=12, day=31))
        prior_sessions = calendar[calendar < test_start]
        if len(prior_sessions) < 22:
            continue
        validation_end = prior_sessions[-22]
        validation_start = pd.Timestamp(year=year - 1, month=1, day=1)
        train_end = validation_start - pd.Timedelta(days=1)
        train_start = pd.Timestamp(year=year - 6, month=1, day=1)
        eligible_assets = [
            asset for asset in RISK_ASSETS
            if starts[asset] <= train_start
            and frame[asset].dropna().index[-1] >= test_start
        ]
        if len(eligible_assets) < 2 or validation_end <= validation_start:
            continue

        fold_metrics: dict[str, dict[str, dict[str, dict[str, Any]]]] = {
            candidate_id: {} for candidate_id in specs
        }
        for candidate_id in specs:
            for asset in eligible_assets:
                path = paths[asset][candidate_id]
                fold_metrics[candidate_id][asset] = {
                    "train": core.performance_metrics(
                        path, cash, max(train_start, starts[asset]), train_end
                    ),
                    "validation": core.performance_metrics(
                        path, cash, validation_start, validation_end
                    ),
                }
        ranking = rank_candidates(fold_metrics, specs)
        selected = next((row["id"] for row in ranking if row["eligible"]), "baseline")
        folds.append({
            "year": year,
            "train": [str(train_start.date()), str(train_end.date())],
            "validation": [str(validation_start.date()), str(validation_end.date())],
            "embargo_sessions": 21,
            "test": [str(test_start.date()), str(test_end.date())],
            "eligible_assets": eligible_assets,
            "selected_id": selected,
            "selected_score": next(
                (row["score"] for row in ranking if row["id"] == selected), 0.0
            ),
        })

    oos_assets = {}
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
            test_start = pd.Timestamp(fold["test"][0])
            test_end = pd.Timestamp(fold["test"][1])
            mask = (price.index >= test_start) & (price.index <= test_end)
            selected_diagnostics = diagnostics[asset][selected]
            target.loc[mask] = selected_diagnostics["target"].reindex(price.index[mask]).values
            rebalance.loc[mask] = selected_diagnostics["rebalance"].reindex(
                price.index[mask]
            ).fillna(False).values
            prior = price.index[price.index < test_start]
            if len(prior):
                boundary = prior[-1]
                target.at[boundary] = float(selected_diagnostics["target"].at[boundary])
                rebalance.at[boundary] = True
        target = target.ffill().fillna(0.0)
        meta = simulate_targets(
            price,
            cash,
            target,
            rebalance,
            {"id": "nested_walk_forward", "max_exposure": 1.25},
            start=first_test,
            end=last_test,
            cost_bps=cost_bps,
            funding_spread=funding_spread,
        )
        baseline_target = pd.Series(1.0, index=price.index)
        baseline = simulate_targets(
            price,
            cash,
            baseline_target,
            pd.Series(False, index=price.index),
            {"id": "walk_forward_baseline", "max_exposure": 1.0},
            start=first_test,
            end=last_test,
            cost_bps=cost_bps,
            funding_spread=funding_spread,
        )
        meta_metrics = core.performance_metrics(meta, cash, first_test, last_test)
        base_metrics = core.performance_metrics(baseline, cash, first_test, last_test)
        oos_assets[asset] = {
            "start": str(first_test.date()),
            "end": str(last_test.date()),
            "strategy": meta_metrics,
            "baseline": base_metrics,
            "dominates": bool(
                meta_metrics["cagr"] >= base_metrics["cagr"]
                and abs(meta_metrics["max_drawdown"]) < abs(base_metrics["max_drawdown"])
            ),
            "strict": bool(
                meta_metrics["cagr"] >= base_metrics["cagr"]
                and abs(meta_metrics["max_drawdown"]) <= 0.85 * abs(base_metrics["max_drawdown"])
            ),
        }

    counts: dict[str, int] = {}
    for fold in folds:
        counts[fold["selected_id"]] = counts.get(fold["selected_id"], 0) + 1
    return {
        "folds": folds,
        "selection_frequency": dict(sorted(counts.items(), key=lambda item: (-item[1], item[0]))),
        "assets": oos_assets,
        "strict_assets": sum(row["strict"] for row in oos_assets.values()),
        "note": (
            "Pseudo-OOS only: prior reports exposed later data. Each annual selection uses a rolling "
            "five-year train, prior-year validation ending before a 21-session embargo, then one-year test."
        ),
    }


def crisis_windows(end: pd.Timestamp) -> dict[str, tuple[pd.Timestamp, pd.Timestamp]]:
    return {
        "dotcom": (pd.Timestamp("2000-03-01"), pd.Timestamp("2002-10-31")),
        "gfc": (pd.Timestamp("2007-10-01"), pd.Timestamp("2009-03-31")),
        "covid": (pd.Timestamp("2020-02-01"), pd.Timestamp("2020-05-31")),
        "2022_bear": (pd.Timestamp("2022-01-01"), pd.Timestamp("2022-12-31")),
        "holdout": (HOLDOUT_START, end),
    }


def current_policy_state(
    asset: str,
    path: core.PathResult,
    diagnostics: dict[str, pd.Series],
    price: pd.Series,
) -> dict[str, Any]:
    day = price.dropna().index[-1]
    return {
        "asset": asset,
        "as_of": str(day.date()),
        "price": _safe_float(price.at[day], 4),
        "target_exposure_next_close": _safe_float(path.variant.get("_latest_target"), 4),
        "current_filled_exposure": _safe_float(path.exposure.get(day), 4),
        "trend_vote": _safe_float(diagnostics["votes"].get(day), 4),
        "realized_vol": _safe_float(diagnostics["vol"].get(day), 6),
        "fast_brake": bool(diagnostics["brake"].get(day, False)),
        "parent_stress": bool(diagnostics.get("parent", pd.Series(dtype=bool)).get(day, False)),
    }


def build_payload(
    frame: pd.DataFrame,
    *,
    source: str,
    warning: str | None,
    cost_bps: float,
    funding_spread: float,
) -> tuple[dict[str, Any], dict[str, dict[str, core.PathResult]]]:
    end = min(frame[ticker].last_valid_index() for ticker in DOWNLOAD_TICKERS)
    index = frame.index[frame.index <= end]
    cash = cash_returns(frame, index)
    specs_list = candidates()
    specs = {spec["id"]: spec for spec in specs_list}
    paths: dict[str, dict[str, core.PathResult]] = {ticker: {} for ticker in RISK_ASSETS}
    diagnostics_by_asset: dict[str, dict[str, dict[str, pd.Series]]] = {
        ticker: {} for ticker in RISK_ASSETS
    }
    metrics: dict[str, dict[str, dict[str, dict[str, Any]]]] = {
        spec["id"]: {} for spec in specs_list
    }

    for asset in RISK_ASSETS:
        price = frame[asset].dropna().loc[:end]
        start = asset_start(price)
        for spec in specs_list:
            target, diagnostics = target_exposure(price, frame, spec)
            run_spec = {**spec, "_latest_target": float(target.iloc[-1])}
            path = simulate_targets(
                price,
                cash,
                target,
                diagnostics["rebalance"],
                run_spec,
                start=start,
                end=end,
                cost_bps=cost_bps,
                funding_spread=funding_spread,
            )
            paths[asset][spec["id"]] = path
            diagnostics_by_asset[asset][spec["id"]] = diagnostics
            metrics[spec["id"]][asset] = {
                "train": core.performance_metrics(path, cash, start, min(TRAIN_END, end)),
                "validation": core.performance_metrics(
                    path, cash, max(VALIDATION_START, start), min(VALIDATION_END, end)
                ),
                "holdout": core.performance_metrics(
                    path, cash, max(HOLDOUT_START, start), end
                ),
                "full": core.performance_metrics(path, cash, start, end),
            }

    ranking = rank_candidates(metrics, specs)
    favorite_id = next((row["id"] for row in ranking if row["eligible"]), "baseline")
    favorite_spec = specs[favorite_id]
    walk_forward = annual_walk_forward(
        frame,
        paths,
        diagnostics_by_asset,
        specs,
        cash,
        end=end,
        cost_bps=cost_bps,
        funding_spread=funding_spread,
    )

    holdout_rows = []
    strict_holdout_assets = 0
    for asset in RISK_ASSETS:
        base = metrics["baseline"][asset]["holdout"]
        favorite = metrics[favorite_id][asset]["holdout"]
        dominates = bool(
            favorite["cagr"] >= base["cagr"]
            and abs(favorite["max_drawdown"]) < abs(base["max_drawdown"])
        )
        strict = bool(
            favorite["cagr"] >= base["cagr"]
            and abs(favorite["max_drawdown"]) <= 0.85 * abs(base["max_drawdown"])
        )
        strict_holdout_assets += strict
        holdout_rows.append({
            "asset": asset,
            "baseline": base,
            "favorite": favorite,
            "dominates": dominates,
            "strict": strict,
        })

    crises = {}
    for name, (window_start, window_end) in crisis_windows(end).items():
        crises[name] = {}
        for asset in RISK_ASSETS:
            if frame[asset].first_valid_index() > window_end:
                continue
            start = max(window_start, asset_start(frame[asset].dropna()))
            if start >= window_end:
                continue
            crises[name][asset] = {
                "baseline": core.performance_metrics(paths[asset]["baseline"], cash, start, window_end),
                "favorite": core.performance_metrics(paths[asset][favorite_id], cash, start, window_end),
            }

    current = {}
    for asset in RISK_ASSETS:
        current[asset] = current_policy_state(
            asset,
            paths[asset][favorite_id],
            diagnostics_by_asset[asset][favorite_id],
            frame[asset].dropna(),
        )

    payload = {
        "schemaVersion": 1,
        "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "researchOnly": True,
        "decisionGrade": False,
        "data": {
            "source": source,
            "warning": warning,
            "as_of": str(end.date()),
            "cache_sha256": sha256_path(DEFAULT_CACHE),
            "coverage": {
                ticker: {
                    "start": str(frame[ticker].first_valid_index().date()),
                    "end": str(frame[ticker].last_valid_index().date()),
                    "observations": int(frame[ticker].notna().sum()),
                }
                for ticker in DOWNLOAD_TICKERS
            },
            "python": platform.python_version(),
            "pandas": pd.__version__,
            "numpy": np.__version__,
        },
        "protocol": {
            "train_end": str(TRAIN_END.date()),
            "validation": [str(VALIDATION_START.date()), str(VALIDATION_END.date())],
            "holdout_start": str(HOLDOUT_START.date()),
            "signal": "completed close",
            "fill": "next session close",
            "cost_bps": cost_bps,
            "funding_spread_over_tbill": funding_spread,
            "selection": "cross-asset median plus lower-quartile development utility",
            "holdout_used_for_current_run_ranking": False,
            "prospective_untouched_holdout": False,
        },
        "candidate_count": len(specs_list),
        "favorite_id": favorite_id,
        "favorite_spec": favorite_spec,
        "ranking": json_safe(ranking[:25]),
        "metrics": json_safe(metrics),
        "holdout": json_safe(holdout_rows),
        "strict_holdout_assets": strict_holdout_assets,
        "crises": json_safe(crises),
        "current": json_safe(current),
        "walk_forward": json_safe(walk_forward),
        "limitations": [
            "ETF adjusted histories may be revised by the data provider.",
            "ETF proxy histories are live fund returns, not the same index methodology as SPMO.",
            "The 2024+ holdout is short and contains few independent crash regimes.",
            "1.25 exposure assumes financing at T-bill plus the configured spread and ignores taxes/margin calls.",
        ],
    }
    return payload, paths


def pct(value: Any) -> str:
    return "—" if value is None else f"{float(value) * 100:.1f}%"


def render_report(payload: dict[str, Any]) -> str:
    spec = payload["favorite_spec"]
    lines = [
        "# 跨资产动量策略实验室",
        "",
        f"生成：{payload['generated_at']}  ",
        f"数据截止：{payload['data']['as_of']}  ",
        f"候选策略：{payload['candidate_count']}；固定 replay 段自 {payload['protocol']['holdout_start']}。",
        "",
        "## 开发期选出的统一策略",
        "",
        f"**`{payload['favorite_id']}`**",
        "",
        "```json",
        json.dumps(spec, ensure_ascii=False, indent=2),
        "```",
        "",
        f"它在 2024+ replay 严格通过的资产数：**{payload['strict_holdout_assets']}/{len(RISK_ASSETS)}**。",
        "",
        "## 2024+ 固定 replay（已被先前研究查看，不再声称 untouched）",
        "",
        "| 资产 | 基准 CAGR / MDD | 统一策略 CAGR / MDD | 暴露 | 同时占优 | 严格通过 |",
        "|---|---:|---:|---:|---|---|",
    ]
    for row in payload["holdout"]:
        lines.append(
            f"| {row['asset']} | {pct(row['baseline']['cagr'])} / {pct(row['baseline']['max_drawdown'])} | "
            f"{pct(row['favorite']['cagr'])} / {pct(row['favorite']['max_drawdown'])} | "
            f"{pct(row['favorite']['exposure'])} | {'是' if row['dominates'] else '否'} | "
            f"{'是' if row['strict'] else '否'} |"
        )
    lines.extend([
        "",
        "## 年度嵌套 pseudo-OOS",
        "",
        payload["walk_forward"]["note"],
        "",
        f"严格通过资产：**{payload['walk_forward']['strict_assets']}/{len(payload['walk_forward']['assets'])}**。",
        "",
        "| 资产 | 区间 | 基准 CAGR / MDD | 动态年度策略 CAGR / MDD | 同时占优 |",
        "|---|---|---:|---:|---|",
    ])
    for asset, row in payload["walk_forward"]["assets"].items():
        lines.append(
            f"| {asset} | {row['start']}→{row['end']} | "
            f"{pct(row['baseline']['cagr'])} / {pct(row['baseline']['max_drawdown'])} | "
            f"{pct(row['strategy']['cagr'])} / {pct(row['strategy']['max_drawdown'])} | "
            f"{'是' if row['dominates'] else '否'} |"
        )
    frequencies = ", ".join(
        f"`{candidate}`×{count}"
        for candidate, count in list(payload["walk_forward"]["selection_frequency"].items())[:6]
    )
    lines.append(f"\n年度选择频率：{frequencies or '无'}。")
    lines.extend([
        "",
        "## 当前目标仓位",
        "",
        "目标在本日收盘观察，模型成交口径为下一交易日收盘。",
        "",
        "| 资产 | 趋势投票 | 实现波动 | 快速刹车 | 当前已成交 | 下一收盘目标 |",
        "|---|---:|---:|---|---:|---:|",
    ])
    for asset, state in payload["current"].items():
        lines.append(
            f"| {asset} | {pct(state['trend_vote'])} | {pct(state['realized_vol'])} | "
            f"{'ON' if state['fast_brake'] else 'OFF'} | {pct(state['current_filled_exposure'])} | "
            f"{pct(state['target_exposure_next_close'])} |"
        )
    lines.extend([
        "",
        "## 前十名开发期排名",
        "",
        "| 排名 | 策略 | 家族 | 分数 | 正效用资产率 | 严格开发资产数 | 参数平台 |",
        "|---:|---|---|---:|---:|---:|---|",
    ])
    for i, row in enumerate(payload["ranking"][:10], 1):
        lines.append(
            f"| {i} | `{row['id']}` | {row['family']} | {row['score']:.4f} | "
            f"{pct(row['positive_asset_rate'])} | {row['strict_development_assets']} | "
            f"{'PASS' if row['eligible'] else 'FAIL'} |"
        )
    lines.extend([
        "",
        "## 解释",
        "",
        "- 趋势投票不是预测顶部或底部；它把单一 10 个月参数改成 6/10/12 个月的稳定集成。",
        "- 波动缩放只改变仓位，不改变趋势方向；融资成本已按 T-bill 加点扣除。",
        "- 快速刹车在 126 日回撤或高波动跌破 EMA50 时降低暴露，必须收回 EMA21 和前高才解除。",
        "- 所有资产共享同一参数，SPMO 不享受单独调参，因此更难但也更可信。",
        "",
        "## 限制与 RL 门槛",
        "",
        "普通策略只有在冻结样本和危机窗口都稳定后才能成为 RL 基线。RL 环境必须使用相同 next-close、成本、现金和融资语义；若不能在多资产 walk-forward 中击败本页统一策略，就保留可解释规则。",
        "",
    ])
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--no-fetch", action="store_true")
    parser.add_argument("--cost-bps", type=float, default=10.0)
    parser.add_argument("--funding-spread", type=float, default=0.03)
    args = parser.parse_args()
    if args.cost_bps < 0 or args.funding_spread < 0:
        parser.error("cost and funding spread must be non-negative")
    frame, source, warning = load_prices(args.cache, no_fetch=args.no_fetch)
    payload, _ = build_payload(
        frame,
        source=source,
        warning=warning,
        cost_bps=args.cost_bps,
        funding_spread=args.funding_spread,
    )
    args.out_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(args.out_dir / "momentum_policy_lab.json", json_safe(payload))
    atomic_write_text(args.out_dir / "momentum_policy_lab.md", render_report(payload))
    print(f"wrote {args.out_dir / 'momentum_policy_lab.json'}")
    print(f"wrote {args.out_dir / 'momentum_policy_lab.md'}")
    print(
        f"favorite {payload['favorite_id']} | strict holdout "
        f"{payload['strict_holdout_assets']}/{len(RISK_ASSETS)}"
    )


if __name__ == "__main__":
    main()

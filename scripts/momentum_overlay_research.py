#!/usr/bin/env python3
"""Ten-year momentum overlay research for SPY, QQQ, SPMO and 11M Top-3.

The study is deliberately conservative:

* adjusted close/OHLC observations are visible only after that close;
* an order caused by close ``t`` fills at close ``t+1``;
* every change in risky exposure pays the configured per-side cost;
* idle capital earns the prior-observation 13-week T-bill proxy;
* parameters are ranked on chronological development folds and reported on a
  fixed 2024+ replay segment (it has been viewed in prior research and is no
  longer an untouched holdout);
* all Top-3 conclusions remain research-only because the local universe is
  reconstructed from today's index members.

The default output directory is ``output/momentum_overlay_research``.  The
script writes a strict JSON artifact and a Chinese Markdown report.
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
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version as package_version
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

try:
    from scripts.artifact_io import atomic_write_bytes, atomic_write_json, atomic_write_text
    from scripts import momentum_top3 as mt
except ModuleNotFoundError:  # direct ``python scripts/...`` execution
    from artifact_io import atomic_write_bytes, atomic_write_json, atomic_write_text
    import momentum_top3 as mt


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "output" / "momentum_overlay_research"
DEFAULT_MARKET_CACHE = DEFAULT_OUT / "market_ohlc.csv.gz"
DEFAULT_TOP3_CACHE = ROOT / "output" / "momentum_prices.csv.gz"
MARKET_TICKERS = ("SPY", "QQQ", "SPMO", "^IRX")
MARKET_FIELDS = ("Close", "High", "Low")
TRADING_DAYS = 252.0
DEFAULT_COST_BPS = 10.0
EPS = 1e-12


@dataclass
class PathResult:
    equity: pd.Series
    exposure: pd.Series
    turnover: pd.Series
    events: list[dict[str, Any]]
    variant: dict[str, Any]


def _clean_float(value: Any, digits: int = 8) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return round(number, digits)


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, (pd.Timestamp, dt.date, dt.datetime)):
        return value.isoformat()
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        return _clean_float(value)
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


def installed_version(package: str) -> str | None:
    try:
        return package_version(package)
    except PackageNotFoundError:
        return None


# ---------------------------------------------------------------- data layer
def _flatten_market_download(raw: pd.DataFrame) -> pd.DataFrame:
    if raw.empty:
        return pd.DataFrame()
    if not isinstance(raw.columns, pd.MultiIndex):
        if len(MARKET_TICKERS) != 1:
            raise ValueError("market download does not have ticker-aware columns")
        raw = pd.concat({MARKET_TICKERS[0]: raw}, axis=1).swaplevel(axis=1)
    columns: dict[str, pd.Series] = {}
    for field in MARKET_FIELDS:
        for ticker in MARKET_TICKERS:
            try:
                series = raw[(field, ticker)]
            except KeyError:
                continue
            columns[f"{field}__{ticker}"] = pd.to_numeric(series, errors="coerce")
    frame = pd.DataFrame(columns)
    frame.index = pd.to_datetime(frame.index, errors="coerce")
    if getattr(frame.index, "tz", None) is not None:
        frame.index = frame.index.tz_convert(None)
    return frame.loc[~frame.index.isna()].sort_index().dropna(how="all")


def validate_market_frame(frame: pd.DataFrame, *, end: pd.Timestamp | None = None) -> list[str]:
    errors: list[str] = []
    if frame.empty or len(frame) < 500:
        return ["market frame is empty or too short"]
    if frame.index.has_duplicates or not frame.index.is_monotonic_increasing:
        errors.append("market dates must be ordered and unique")
    for ticker in MARKET_TICKERS:
        column = f"Close__{ticker}"
        if column not in frame:
            errors.append(f"missing {column}")
            continue
        series = pd.to_numeric(frame[column], errors="coerce").dropna()
        if series.empty:
            errors.append(f"invalid close history for {ticker}")
        elif ticker == "^IRX":
            # A cash yield may legitimately be zero (and was briefly quoted
            # slightly below zero); it is not an asset price.
            if (series <= -100).any():
                errors.append("invalid annualized yield history for ^IRX")
        elif (series <= 0).any():
            errors.append(f"invalid close history for {ticker}")
        if end is not None and ticker != "^IRX" and series.index[-1] < end:
            errors.append(f"{ticker} ends before requested date {end.date()}")
    spmo = frame.get("Close__SPMO", pd.Series(dtype=float)).dropna()
    if not spmo.empty and spmo.index[0] > pd.Timestamp("2015-10-31"):
        errors.append("SPMO history begins unexpectedly late")
    return errors


def read_market_cache(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path, index_col=0, parse_dates=True, compression="gzip")
    frame.index = pd.to_datetime(frame.index)
    return frame.sort_index()


def write_market_cache(frame: pd.DataFrame, path: Path) -> None:
    buffer = io.StringIO()
    frame.to_csv(buffer)
    payload = gzip.compress(buffer.getvalue().encode("utf-8"), compresslevel=6)
    atomic_write_bytes(path, payload)


def load_market_data(
    start: pd.Timestamp,
    end: pd.Timestamp,
    *,
    cache_path: Path = DEFAULT_MARKET_CACHE,
    no_fetch: bool = False,
) -> tuple[pd.DataFrame, str, str | None]:
    cached = None
    cache_error = None
    if cache_path.exists():
        try:
            candidate = read_market_cache(cache_path)
            problems = validate_market_frame(candidate)
            if problems:
                cache_error = "; ".join(problems)
            else:
                cached = candidate
        except Exception as exc:  # pragma: no cover - corrupt local runtime file
            cache_error = f"{type(exc).__name__}: {exc}"
    if no_fetch:
        if cached is None:
            raise RuntimeError(f"--no-fetch requires a valid cache: {cache_error or 'missing'}")
        return cached, "market_cache", cache_error

    import yfinance as yf

    try:
        raw = yf.download(
            list(MARKET_TICKERS),
            start=str(start.date()),
            end=str((end + pd.Timedelta(days=1)).date()),
            auto_adjust=True,
            actions=False,
            progress=False,
            threads=False,
        )
        frame = _flatten_market_download(raw)
        problems = validate_market_frame(frame, end=end)
        if problems:
            raise ValueError("; ".join(problems))
        write_market_cache(frame, cache_path)
        round_trip = read_market_cache(cache_path)
        problems = validate_market_frame(round_trip, end=end)
        if problems:
            raise ValueError("cache round-trip failed: " + "; ".join(problems))
        return round_trip, "yfinance_live_download", None
    except Exception as exc:
        if cached is None:
            raise RuntimeError(f"market download failed and cache is unusable: {exc}") from exc
        warning = f"download failed; reused cache: {type(exc).__name__}: {exc}"
        return cached, "market_cache_fallback", warning


def market_close(frame: pd.DataFrame, ticker: str) -> pd.Series:
    return pd.to_numeric(frame[f"Close__{ticker}"], errors="coerce").dropna().sort_index()


def cash_returns_from_irx(frame: pd.DataFrame, index: pd.DatetimeIndex) -> pd.Series:
    """Turn the prior-observation 13-week T-bill quote into close-to-close cash returns."""
    annual_pct = market_close(frame, "^IRX").reindex(index).ffill().shift(1).fillna(0.0)
    days = pd.Series(index.to_series().diff().dt.days.fillna(1.0).values, index=index)
    annual = annual_pct / 100.0
    values = np.power(1.0 + annual.clip(lower=-0.99), days / 365.25) - 1.0
    return pd.Series(values, index=index, dtype=float).fillna(0.0)


def wilder_atr(frame: pd.DataFrame, ticker: str, n: int = 14) -> pd.Series:
    high = pd.to_numeric(frame[f"High__{ticker}"], errors="coerce")
    low = pd.to_numeric(frame[f"Low__{ticker}"], errors="coerce")
    close = pd.to_numeric(frame[f"Close__{ticker}"], errors="coerce")
    prev = close.shift(1)
    tr = pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()


# ----------------------------------------------------------- signal utilities
def monthly_observation(series: pd.Series) -> pd.Series:
    return series.groupby(series.index.to_period("M")).transform("last")


def monthly_signal(series: pd.Series, months: int, kind: str) -> pd.Series:
    monthly = series.groupby(series.index.to_period("M")).last()
    if kind == "sma":
        state = monthly > monthly.rolling(months, min_periods=months).mean()
    elif kind == "absolute":
        state = monthly / monthly.shift(months) > 1.0
    else:  # pragma: no cover - guarded by candidate construction
        raise ValueError(kind)
    # A month-end observation may only affect an order after that completed
    # close.  The final observed data month is conservatively incomplete unless
    # a later-month observation exists; this prevents an intramonth refresh
    # from manufacturing an actionable month-end target.
    month_end = series.groupby(series.index.to_period("M")).tail(1).index
    month_end = month_end[month_end.to_period("M") < series.index[-1].to_period("M")]
    updates = pd.Series(np.nan, index=series.index, dtype=float)
    updates.loc[month_end] = state.reindex(month_end.to_period("M")).astype(float).values
    return updates.ffill().fillna(0.0).astype(bool)


def hysteresis_state(
    price: pd.Series,
    average: pd.Series,
    *,
    entry_buffer: float,
    exit_buffer: float,
    confirm: int,
) -> pd.Series:
    state = False
    above_count = below_count = 0
    output = []
    for day in price.index:
        p, ma = price.at[day], average.at[day]
        if pd.isna(p) or pd.isna(ma):
            output.append(state)
            continue
        above_count = above_count + 1 if p > ma * (1.0 + entry_buffer) else 0
        below_count = below_count + 1 if p < ma * (1.0 - exit_buffer) else 0
        if not state and above_count >= confirm:
            state = True
            below_count = 0
        elif state and below_count >= confirm:
            state = False
            above_count = 0
        output.append(state)
    return pd.Series(output, index=price.index, dtype=bool)


def _source_price(variant: dict[str, Any], base: pd.Series, context: dict[str, Any]) -> pd.Series:
    source = variant.get("source", "self")
    if source == "self":
        return base
    if source in {"SPY", "QQQ", "SPMO"}:
        return context["close"][source].reindex(base.index).ffill()
    raise ValueError(f"unknown signal source {source}")


def build_gate(
    variant: dict[str, Any],
    base: pd.Series,
    context: dict[str, Any],
) -> dict[str, pd.Series]:
    family = variant["family"]
    ref = _source_price(variant, base, context).reindex(base.index).ffill()
    ones = pd.Series(True, index=base.index)
    zeros = pd.Series(False, index=base.index)
    raw = pd.Series(1.0, index=base.index)
    threshold = pd.Series(np.nan, index=base.index)

    if family == "buy_hold":
        state = ones
    elif family == "sma":
        threshold = ref.rolling(variant["window"], min_periods=variant["window"]).mean()
        state = ref > threshold
    elif family == "ema":
        threshold = ref.ewm(span=variant["window"], adjust=False, min_periods=variant["window"]).mean()
        state = ref > threshold
    elif family == "absolute":
        threshold = ref.shift(variant["window"])
        state = ref > threshold
    elif family == "dual_sma":
        fast = ref.rolling(variant["fast"], min_periods=variant["fast"]).mean()
        threshold = ref.rolling(variant["slow"], min_periods=variant["slow"]).mean()
        state = fast > threshold
    elif family == "monthly_sma":
        state = monthly_signal(ref, variant["months"], "sma")
        month_close = ref.groupby(ref.index.to_period("M")).last()
        month_level = month_close.rolling(
            variant["months"], min_periods=variant["months"]
        ).mean()
        month_end = ref.groupby(ref.index.to_period("M")).tail(1).index
        month_end = month_end[month_end.to_period("M") < ref.index[-1].to_period("M")]
        threshold.loc[month_end] = month_level.reindex(month_end.to_period("M")).values
        threshold = threshold.ffill()
    elif family == "monthly_absolute":
        state = monthly_signal(ref, variant["months"], "absolute")
        month_close = ref.groupby(ref.index.to_period("M")).last()
        month_level = month_close.shift(variant["months"])
        month_end = ref.groupby(ref.index.to_period("M")).tail(1).index
        month_end = month_end[month_end.to_period("M") < ref.index[-1].to_period("M")]
        threshold.loc[month_end] = month_level.reindex(month_end.to_period("M")).values
        threshold = threshold.ffill()
    elif family == "hysteresis":
        threshold = ref.rolling(variant["window"], min_periods=variant["window"]).mean()
        state = hysteresis_state(
            ref,
            threshold,
            entry_buffer=variant["entry_buffer"],
            exit_buffer=variant["exit_buffer"],
            confirm=variant["confirm"],
        )
    elif family == "dual_market_sma":
        spy = context["close"]["SPY"].reindex(base.index).ffill()
        qqq = context["close"]["QQQ"].reindex(base.index).ffill()
        n = variant["window"]
        state = (spy > spy.rolling(n, min_periods=n).mean()) & (
            qqq > qqq.rolling(n, min_periods=n).mean()
        )
    elif family in {"vol_target", "sma_vol"}:
        realized = base.pct_change(fill_method=None).rolling(
            variant["vol_window"], min_periods=variant["vol_window"]
        ).std() * math.sqrt(TRADING_DAYS)
        raw = (variant["target_vol"] / realized.replace(0, np.nan)).clip(
            float(variant.get("min_exposure", 0.0)),
            float(variant.get("max_exposure", 1.0)),
        ).fillna(float(variant.get("min_exposure", 0.0)))
        if family == "sma_vol":
            if variant.get("months"):
                state = monthly_signal(ref, variant["months"], "sma")
                month_close = ref.groupby(ref.index.to_period("M")).last()
                month_level = month_close.rolling(
                    variant["months"], min_periods=variant["months"]
                ).mean()
                month_end = ref.groupby(ref.index.to_period("M")).tail(1).index
                month_end = month_end[
                    month_end.to_period("M") < ref.index[-1].to_period("M")
                ]
                threshold.loc[month_end] = month_level.reindex(month_end.to_period("M")).values
                threshold = threshold.ffill()
            else:
                threshold = ref.rolling(variant["window"], min_periods=variant["window"]).mean()
                state = ref > threshold
            raw = raw.where(state, 0.0)
        else:
            state = raw > 0
    elif family == "panic_gate":
        market = context["close"][variant.get("source", "SPY")].reindex(base.index).ffill()
        ret = market / market.shift(variant["market_window"]) - 1.0
        vol = market.pct_change(fill_method=None).rolling(126, min_periods=126).std() * math.sqrt(TRADING_DAYS)
        high_vol = vol > vol.expanding(min_periods=252).quantile(0.75)
        panic = (ret < 0) & high_vol
        raw = pd.Series(1.0, index=base.index).where(~panic, variant["panic_weight"])
        state = raw > 0
    elif family == "spmo_structural":
        spmo = context["close"]["SPMO"].reindex(base.index).ffill()
        spy = context["close"]["SPY"].reindex(base.index).ffill()
        qqq = context["close"]["QQQ"].reindex(base.index).ffill()
        e21 = spmo.ewm(span=21, adjust=False, min_periods=21).mean()
        e50 = spmo.ewm(span=50, adjust=False, min_periods=50).mean()
        e200 = spmo.ewm(span=200, adjust=False, min_periods=200).mean()
        q21 = qqq.ewm(span=21, adjust=False, min_periods=21).mean()
        s50 = spy.ewm(span=50, adjust=False, min_periods=50).mean()
        atr = context["atr"]["SPMO"].reindex(base.index)
        two_below = (spmo < e21) & (spmo.shift(1) < e21.shift(1))
        rs21 = spmo.pct_change(21) > spy.pct_change(21)
        rs63 = spmo.pct_change(63) > spy.pct_change(63)
        hard_hit = spmo.pct_change() <= -0.04
        stretched = (spmo - e21) / atr > 1.75
        strict = variant.get("strict", False)
        entry = (
            (spmo > e50)
            & ~two_below
            & rs21
            & rs63
            & (qqq > q21)
            & (spy > s50)
            & ~hard_hit
            & ~stretched
        )
        if strict:
            entry &= (spmo > e21) & (spmo > e200) & (e21 > e21.shift(5)) & (e50 > e50.shift(10))
        exit_required = (spmo < e50) | two_below
        return {
            "entry": entry.fillna(False),
            "exit": exit_required.fillna(False),
            "raw": raw,
            "ref": spmo,
            "threshold": e50,
            "atr": atr,
        }
    else:  # stop/take-profit candidates use an optional trend window
        trend_window = variant.get("trend_window")
        if trend_window:
            threshold = ref.rolling(trend_window, min_periods=trend_window).mean()
            state = ref > threshold
        else:
            state = ones

    state = state.fillna(False).astype(bool)
    if variant.get("risk_off_weight") is not None:
        # A partial-risk regime is a sizing rule, not a full exit.  It keeps a
        # core sleeve while the trend gate is closed and restores 100% only
        # after the same close-confirmed repair signal.
        raw = pd.Series(float(variant.get("risk_on_weight", 1.0)), index=base.index).where(
            state, float(variant["risk_off_weight"])
        )
        entry_state = pd.Series(True, index=base.index)
        exit_state = pd.Series(False, index=base.index)
    else:
        entry_state = state
        exit_state = (~state).fillna(True)
    return {
        "entry": entry_state,
        "exit": exit_state,
        "raw": raw.fillna(0.0).clip(0.0, float(variant.get("max_exposure", 1.0))),
        "ref": ref,
        "threshold": threshold,
        "atr": pd.Series(np.nan, index=base.index),
    }


def is_rebalance_day(day: pd.Timestamp, next_day: pd.Timestamp | None, mode: str) -> bool:
    if mode == "daily":
        return True
    if mode == "weekly":
        return next_day is None or day.to_period("W") != next_day.to_period("W")
    if mode == "monthly":
        return next_day is None or day.to_period("M") != next_day.to_period("M")
    raise ValueError(mode)


def simulate_overlay(
    base: pd.Series,
    cash_returns: pd.Series,
    variant: dict[str, Any],
    context: dict[str, Any],
    *,
    start: pd.Timestamp,
    end: pd.Timestamp,
    cost_bps: float,
) -> PathResult:
    full_base = base.dropna().sort_index()
    # Indicators must see the full pre-window history.  Building the gate only
    # after slicing to one seed session silently gives SMA/EMA/volatility rules
    # an empty warm-up and can delay entry by hundreds of sessions.
    gate = build_gate(variant, full_base, context)
    dates = full_base.index[full_base.index <= end]
    starts = np.flatnonzero(dates >= start)
    if not len(starts) or starts[0] == 0:
        raise ValueError("overlay needs at least one pre-start signal observation")
    seed_i = int(starts[0]) - 1
    dates = dates[seed_i:]
    base = full_base.reindex(dates).ffill()
    cash = cash_returns.reindex(dates).fillna(0.0)
    gate = {
        key: series.reindex(dates)
        for key, series in gate.items()
    }
    cost_rate = float(cost_bps) / 10000.0

    equity = 1.0
    position = 0.0
    pending: tuple[float, str] | None = None
    entry_price = peak_price = None
    atr_stop = None
    armed = False
    cooldown_until = -1
    events: list[dict[str, Any]] = []
    eq_values = [equity]
    exposures = [position]
    turnovers = [0.0]

    # The observation immediately before the evaluation start is a valid
    # completed-close signal.  Queue it now so the first in-window session is
    # the fill close; otherwise every generic path would silently start one
    # session late.
    seed_day = dates[0]
    seed_entry = bool(gate["entry"].get(seed_day, False))
    seed_raw = float(gate["raw"].get(seed_day, 0.0))
    seed_breakout = int(variant.get("breakout", 0))
    seed_breakout_ok = True
    if seed_breakout:
        prior_high = gate["ref"].rolling(
            seed_breakout, min_periods=seed_breakout
        ).max().shift(1).get(seed_day)
        seed_breakout_ok = (
            pd.notna(prior_high)
            and float(gate["ref"].at[seed_day]) > float(prior_high)
        )
    if seed_entry and seed_breakout_ok and seed_raw > EPS:
        pending = (seed_raw, "initial_entry")

    for i in range(1, len(dates)):
        day, previous = dates[i], dates[i - 1]
        base_return = float(base.at[day] / base.at[previous] - 1.0)
        cash_return = float(cash.at[day])
        financing_return = cash_return
        if position > 1.0:
            calendar_days = max(1, int((day - previous).days))
            financing_return += (
                (1.0 + float(variant.get("borrow_spread", 0.03)))
                ** (calendar_days / 365.25)
                - 1.0
            )
        portfolio_factor = (
            position * (1.0 + base_return)
            + (1.0 - position) * (1.0 + financing_return)
        )
        if portfolio_factor <= 0:
            raise ValueError(f"portfolio equity became non-positive on {day.date()}")
        equity *= portfolio_factor
        # Risky and cash sleeves drift before the close-t order executes.  Any
        # return to a fixed target therefore requires observable turnover and
        # pays cost; there is no free continuous rebalancing.
        position = position * (1.0 + base_return) / portfolio_factor
        daily_turnover = 0.0

        if pending is not None:
            desired, reason = pending
            desired = min(
                float(variant.get("max_exposure", 1.0)), max(0.0, float(desired))
            )
            daily_turnover = abs(desired - position)
            if daily_turnover > EPS:
                equity *= max(0.0, 1.0 - daily_turnover * cost_rate)
                events.append({
                    "date": str(day.date()),
                    "from": round(position, 6),
                    "to": round(desired, 6),
                    "turnover": round(daily_turnover, 6),
                    "reason": reason,
                })
                opened = position <= EPS and desired > EPS
                closed = position > EPS and desired <= EPS
                position = desired
                if opened:
                    entry_price = peak_price = float(base.at[day])
                    atr = gate["atr"].get(day)
                    atr_stop = (
                        float(base.at[day]) - variant["atr_mult"] * float(atr)
                        if variant.get("atr_mult") and pd.notna(atr)
                        else None
                    )
                    armed = False
                elif closed:
                    entry_price = peak_price = atr_stop = None
                    armed = False
            pending = None

        price = float(base.at[day])
        if position > EPS:
            peak_price = max(float(peak_price or price), price)
            if variant.get("atr_mult"):
                atr = gate["atr"].get(day)
                if pd.notna(atr):
                    candidate_stop = price - variant["atr_mult"] * float(atr)
                    atr_stop = candidate_stop if atr_stop is None else max(atr_stop, candidate_stop)

        raw = float(gate["raw"].get(day, 0.0))
        entry_allowed = bool(gate["entry"].get(day, False))
        exit_required = bool(gate["exit"].get(day, True))
        desired = position
        reason = "hold"
        stop_triggered = False

        if position > EPS:
            gain = price / float(entry_price or price) - 1.0
            peak_drawdown = price / float(peak_price or price) - 1.0
            if variant.get("arm_profit") is not None and gain >= variant["arm_profit"]:
                armed = True
            if variant.get("hard_stop") is not None and gain <= -variant["hard_stop"]:
                stop_triggered, reason = True, "fixed_stop"
            elif variant.get("trailing_stop") is not None and (
                variant.get("arm_profit") is None or armed
            ) and peak_drawdown <= -variant["trailing_stop"]:
                stop_triggered, reason = True, "trailing_stop"
            elif variant.get("profit_target") is not None and gain >= variant["profit_target"]:
                stop_triggered, reason = True, "fixed_take_profit"
            elif atr_stop is not None and price <= atr_stop:
                stop_triggered, reason = True, "atr_trailing_stop"

            if stop_triggered:
                desired = 0.0
                cooldown_until = i + int(variant.get("cooldown", 0))
            elif exit_required:
                desired, reason = 0.0, "trend_exit"
            elif variant["family"] == "spmo_structural":
                desired, reason = 1.0, "structural_hold"
            else:
                desired, reason = raw, "risk_target"
        else:
            breakout = int(variant.get("breakout", 0))
            breakout_ok = True
            if breakout:
                prior_high = gate["ref"].rolling(breakout, min_periods=breakout).max().shift(1).get(day)
                breakout_ok = pd.notna(prior_high) and float(gate["ref"].at[day]) > float(prior_high)
            if i >= cooldown_until and entry_allowed and breakout_ok:
                desired, reason = raw, "entry_or_reclaim"
            else:
                desired, reason = 0.0, "cash_wait"

        next_day = dates[i + 1] if i + 1 < len(dates) else None
        rebalance = is_rebalance_day(day, next_day, variant.get("rebalance", "daily"))
        urgent = stop_triggered or exit_required or (position <= EPS and desired > EPS)
        band = float(variant.get("rebalance_band", 0.0))
        if (rebalance or urgent) and abs(desired - position) >= max(band, EPS):
            pending = (desired, reason)

        eq_values.append(equity)
        exposures.append(position)
        turnovers.append(daily_turnover)

    return PathResult(
        equity=pd.Series(eq_values, index=dates, dtype=float),
        exposure=pd.Series(exposures, index=dates, dtype=float),
        turnover=pd.Series(turnovers, index=dates, dtype=float),
        events=events,
        variant=variant,
    )


# ----------------------------------------------------- Top-3 portfolio layer
def _asof_row(hist: pd.DataFrame, months: int) -> pd.Series:
    anchor = hist.index[-1] - pd.DateOffset(months=months)
    eligible = hist.index[hist.index <= anchor]
    return hist.loc[eligible[-1]] if len(eligible) else hist.iloc[0] * np.nan


def top3_scores(hist: pd.DataFrame, mode: str, coverage: float, exclude: tuple[str, ...]) -> pd.Series:
    columns = [c for c in mt.valid_columns(hist, months=13, coverage=coverage) if c not in exclude]
    prices = hist[columns]
    if mode == "11m":
        return (prices.iloc[-1] / _asof_row(prices, 11) - 1.0).dropna()
    if mode == "12_1":
        return (_asof_row(prices, 1) / _asof_row(prices, 12) - 1.0).dropna()
    raise ValueError(mode)


def _market_monthly_gate(market: pd.Series, signal_day: pd.Timestamp, months: int = 10) -> bool:
    hist = market.loc[:signal_day].dropna()
    month_closes = hist.groupby(hist.index.to_period("M")).last()
    if len(month_closes) < months:
        return False
    return bool(month_closes.iloc[-1] > month_closes.iloc[-months:].mean())


def run_top3_portfolio(
    prices: pd.DataFrame,
    market: dict[str, pd.Series],
    cash_returns: pd.Series,
    variant: dict[str, Any],
    *,
    start: pd.Timestamp,
    end: pd.Timestamp,
    cost_bps: float,
    coverage: float = 0.95,
    warmup_months: int = 14,
) -> PathResult:
    prices = mt.normalize_price_frame(prices)
    all_dates = prices.index[(prices.index <= end)]
    simulation_start = start - pd.DateOffset(months=max(0, int(warmup_months)))
    starts = np.flatnonzero(all_dates >= simulation_start)
    if not len(starts):
        raise ValueError("Top-3 research has no simulation observations")
    seed_i = max(0, int(starts[0]) - 1)
    dates = all_dates[seed_i:]
    cash = cash_returns.reindex(dates).fillna(0.0)
    exclude = tuple(t for t in ("SPY", "QQQ") if t in prices.columns)
    cost_rate = cost_bps / 10000.0
    top_n = int(variant.get("top_n", 3))

    equity = 1.0
    weights = pd.Series(dtype=float)
    cash_weight = 1.0
    last_prices = pd.Series(dtype=float)
    entry_prices: dict[str, float] = {}
    pending_target: tuple[pd.Series, str] | None = None
    pending_stops: set[str] = set()
    events: list[dict[str, Any]] = []
    eq_values = [equity]
    exposures = [0.0]
    turnovers = [0.0]

    for i in range(1, len(dates)):
        day = dates[i]
        daily_turnover = 0.0
        if len(weights):
            asset_returns = pd.Series(0.0, index=weights.index, dtype=float)
            for symbol in weights.index:
                current = prices.at[day, symbol] if symbol in prices else np.nan
                prior = last_prices.get(symbol)
                if pd.notna(current) and pd.notna(prior) and float(prior) > 0:
                    asset_returns.at[symbol] = float(current) / float(prior) - 1.0
                    last_prices.at[symbol] = float(current)
            portfolio_return = float((weights * asset_returns).sum()) + cash_weight * float(cash.at[day])
            equity *= 1.0 + portfolio_return
            grown = weights * (1.0 + asset_returns)
            grown_cash = cash_weight * (1.0 + float(cash.at[day]))
            total = float(grown.sum()) + grown_cash
            weights = grown / total if total > 0 else pd.Series(dtype=float)
            cash_weight = grown_cash / total if total > 0 else 1.0
        else:
            equity *= 1.0 + float(cash.at[day])

        target = None
        target_reason = None
        if pending_target is not None:
            target, target_reason = pending_target
        elif pending_stops:
            target = weights.drop(labels=list(pending_stops), errors="ignore")
            target_reason = "position_stop"
        pending_target = None
        pending_stops = set()

        if target is not None:
            required = weights.index.union(target.index)
            valid = all(
                symbol in prices.columns
                and pd.notna(prices.at[day, symbol])
                and float(prices.at[day, symbol]) > 0
                for symbol in required
            )
            if valid:
                prior_entry_prices = dict(entry_prices)
                pre = weights.reindex(required, fill_value=0.0)
                post = target.reindex(required, fill_value=0.0)
                gross = float((post - pre).abs().sum())
                daily_turnover = gross
                equity *= max(0.0, 1.0 - gross * cost_rate)
                events.append({
                    "date": str(day.date()),
                    "reason": target_reason,
                    "gross_turnover": round(gross, 6),
                    "holdings": list(target.index),
                    "risk_weight": round(float(target.sum()), 6),
                })
                old_symbols = set(weights.index)
                weights = target[target > EPS].copy()
                cash_weight = max(0.0, 1.0 - float(weights.sum()))
                last_prices = pd.Series(
                    {symbol: float(prices.at[day, symbol]) for symbol in weights.index}, dtype=float
                )
                if target_reason == "monthly_rebalance":
                    # A retained holding keeps its lifecycle entry basis.  Only
                    # a newly opened/reopened name receives a fresh stop basis;
                    # monthly equal-weight maintenance is not a new position.
                    entry_prices = {
                        symbol: prior_entry_prices.get(symbol, float(prices.at[day, symbol]))
                        for symbol in weights.index
                    }
                else:
                    entry_prices = {
                        symbol: entry_prices.get(symbol, float(prices.at[day, symbol]))
                        for symbol in weights.index
                    }
                for symbol in set(weights.index) - old_symbols:
                    entry_prices[symbol] = float(prices.at[day, symbol])
            else:
                events.append({"date": str(day.date()), "reason": "skipped_missing_close"})

        fixed_stop = variant.get("position_stop")
        if fixed_stop and len(weights):
            for symbol in weights.index:
                current = prices.at[day, symbol]
                entry = entry_prices.get(symbol)
                if pd.notna(current) and entry and float(current) / entry - 1.0 <= -fixed_stop:
                    pending_stops.add(symbol)

        next_day = dates[i + 1] if i + 1 < len(dates) else None
        if next_day is not None and day.to_period("M") != next_day.to_period("M"):
            scores = top3_scores(prices.loc[:day], variant.get("score_mode", "11m"), coverage, exclude)
            ranked = scores.sort_values(ascending=False, kind="mergesort")
            if variant.get("positive_only"):
                ranked = ranked[ranked > 0]

            selected: list[str] = []
            stopped_today = set(pending_stops)
            buffer_n = int(variant.get("rank_buffer", top_n))
            if buffer_n > top_n:
                buffer = set(ranked.head(buffer_n).index)
                selected.extend([
                    s for s in weights.index if s in buffer and s not in stopped_today
                ][:top_n])
            for symbol in ranked.index:
                if symbol not in selected and symbol not in stopped_today:
                    selected.append(symbol)
                if len(selected) >= top_n:
                    break

            risk_weight = 1.0
            gate_source = variant.get("market_gate")
            if gate_source and not _market_monthly_gate(market[gate_source], day, variant.get("gate_months", 10)):
                risk_weight = float(variant.get("risk_off_weight", 0.0))
            if variant.get("panic_gate"):
                source = market[variant.get("panic_source", "SPY")].loc[:day].dropna()
                ret_window = int(variant.get("panic_window", 504))
                market_return = source.iloc[-1] / source.iloc[-ret_window] - 1.0 if len(source) > ret_window else np.nan
                vol = source.pct_change().rolling(126).std() * math.sqrt(TRADING_DAYS)
                threshold = vol.expanding(min_periods=252).quantile(0.75)
                if pd.notna(market_return) and market_return < 0 and bool(vol.iloc[-1] > threshold.iloc[-1]):
                    risk_weight = min(risk_weight, float(variant.get("panic_weight", 0.0)))

            slot_weight = risk_weight / top_n
            pending_target = (
                pd.Series(slot_weight, index=selected, dtype=float),
                "monthly_rebalance",
            )

        eq_values.append(equity)
        exposures.append(float(weights.sum()))
        turnovers.append(daily_turnover)

    return PathResult(
        equity=pd.Series(eq_values, index=dates, dtype=float),
        exposure=pd.Series(exposures, index=dates, dtype=float),
        turnover=pd.Series(turnovers, index=dates, dtype=float),
        events=events,
        variant=variant,
    )


# -------------------------------------------------------------- performance
def segment_series(series: pd.Series, start: pd.Timestamp, end: pd.Timestamp) -> pd.Series:
    series = series.dropna().sort_index()
    prior = series.loc[series.index < start]
    within = series.loc[(series.index >= start) & (series.index <= end)]
    if len(prior):
        within = pd.concat([prior.tail(1), within])
    return within[~within.index.duplicated(keep="last")]


def performance_metrics(
    path: PathResult,
    cash_returns: pd.Series,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> dict[str, Any]:
    eq = segment_series(path.equity, start, end)
    if len(eq) < 2 or eq.iloc[0] <= 0:
        return {key: None for key in (
            "total_x", "cagr", "max_drawdown", "sharpe", "sortino", "calmar",
            "annual_vol", "ulcer", "cvar_95", "worst_day", "exposure", "annual_turnover",
        )}
    returns = eq.pct_change().dropna()
    years = max((eq.index[-1] - eq.index[0]).days / 365.25, 1.0 / TRADING_DAYS)
    total_x = float(eq.iloc[-1] / eq.iloc[0])
    cagr = total_x ** (1.0 / years) - 1.0
    drawdown = eq / eq.cummax() - 1.0
    max_dd = float(drawdown.min())
    annual_vol = float(returns.std(ddof=1) * math.sqrt(TRADING_DAYS)) if len(returns) > 1 else 0.0
    rf = cash_returns.reindex(returns.index).fillna(0.0)
    excess = returns - rf
    sharpe = float(excess.mean() / returns.std(ddof=1) * math.sqrt(TRADING_DAYS)) if returns.std(ddof=1) > 0 else None
    downside = returns[returns < 0]
    sortino = (
        float(excess.mean() / downside.std(ddof=1) * math.sqrt(TRADING_DAYS))
        if len(downside) > 1 and downside.std(ddof=1) > 0
        else None
    )
    tail_n = max(1, int(math.ceil(len(returns) * 0.05)))
    cvar = float(returns.nsmallest(tail_n).mean())
    exposure = path.exposure.reindex(returns.index).fillna(0.0)
    turnover = path.turnover.reindex(returns.index).fillna(0.0)
    return {
        "total_x": _clean_float(total_x, 6),
        "cagr": _clean_float(cagr, 8),
        "max_drawdown": _clean_float(max_dd, 8),
        "sharpe": _clean_float(sharpe, 6),
        "sortino": _clean_float(sortino, 6),
        "calmar": _clean_float(cagr / abs(max_dd), 6) if max_dd < 0 else None,
        "annual_vol": _clean_float(annual_vol, 8),
        "ulcer": _clean_float(math.sqrt(float((drawdown.pow(2)).mean())), 8),
        "cvar_95": _clean_float(cvar, 8),
        "worst_day": _clean_float(float(returns.min()), 8),
        "exposure": _clean_float(float(exposure.mean()), 6),
        "annual_turnover": _clean_float(float(turnover.sum()) / years, 6),
        "observations": int(len(returns)),
        "start": str(eq.index[0].date()),
        "end": str(eq.index[-1].date()),
    }


def fold_utility(candidate: dict[str, Any], baseline: dict[str, Any]) -> float:
    if candidate.get("cagr") is None or baseline.get("cagr") is None:
        return -1e9
    cagr_gain = candidate["cagr"] - baseline["cagr"]
    dd_gain = abs(baseline["max_drawdown"]) - abs(candidate["max_drawdown"])
    sharpe_gain = (candidate.get("sharpe") or 0.0) - (baseline.get("sharpe") or 0.0)
    return float(cagr_gain + 0.75 * dd_gain + 0.05 * sharpe_gain)


def choose_candidate(
    metrics: dict[str, dict[str, dict[str, Any]]],
    variants: dict[str, dict[str, Any]],
    *,
    baseline_id: str,
) -> tuple[str, list[dict[str, Any]]]:
    ranked = []
    for candidate_id, folds in metrics.items():
        if candidate_id == baseline_id:
            continue
        if variants[candidate_id].get("experimental_leverage"):
            continue
        utilities = [
            fold_utility(folds[fold], metrics[baseline_id][fold])
            for fold in ("train", "validation")
        ]
        if min((folds[fold].get("exposure") or 0.0) for fold in ("train", "validation")) < 0.05:
            continue
        robust = min(utilities) + 0.25 * float(np.mean(utilities))
        ranked.append({
            "id": candidate_id,
            "robust_score": robust,
            "train_utility": utilities[0],
            "validation_utility": utilities[1],
            "complexity": int(variants[candidate_id].get("complexity", 1)),
            "family": variants[candidate_id].get("family", "unknown"),
        })
    family_rows: dict[str, list[dict[str, Any]]] = {}
    for row in ranked:
        family_rows.setdefault(row["family"], []).append(row)
    for row in ranked:
        peers = family_rows[row["family"]]
        stable_peers = sum(
            peer["train_utility"] >= 0 and peer["validation_utility"] >= 0
            for peer in peers
        )
        # A broad parameter plateau is stronger evidence than one isolated
        # winning cell.  Top-3 portfolio variants are discrete rule ablations,
        # not adjacent cells, so the neighbor requirement does not apply.
        row["stable_family_members"] = stable_peers
        row["plateau_pass"] = bool(
            row["train_utility"] >= 0
            and row["validation_utility"] >= 0
            and (
                len(peers) == 1
                or stable_peers >= 2
                or row["family"] == "top3_portfolio"
            )
        )
    ranked.sort(
        key=lambda row: (
            not row["plateau_pass"],
            -row["robust_score"],
            row["complexity"],
            row["id"],
        )
    )
    eligible = [row for row in ranked if row["plateau_pass"]]
    return (eligible[0]["id"] if eligible else baseline_id), ranked


def variant_category(variant: dict[str, Any]) -> str:
    family = variant.get("family", "unknown")
    if variant.get("experimental_leverage"):
        return "levered_risk_managed"
    if family == "top3_portfolio":
        if variant.get("position_stop"):
            return "top3_position_stop"
        if variant.get("positive_only") and variant.get("market_gate"):
            return "top3_absolute_plus_market"
        if variant.get("positive_only"):
            return "top3_positive_slots"
        if variant.get("score_mode") == "12_1":
            return "top3_12_1"
        if variant.get("rank_buffer"):
            return "top3_rank_buffer"
        if variant.get("market_gate"):
            return "top3_market_gate"
        if variant.get("panic_gate"):
            return "top3_panic_gate"
        return "baseline"
    if family in {"vol_target", "sma_vol"}:
        return "volatility_target"
    if family in {"fixed_stop"}:
        return "fixed_stop"
    if family in {"trailing_stop", "armed_trailing"}:
        return "trailing_stop"
    if family == "take_profit":
        return "fixed_take_profit"
    if family == "panic_gate":
        return "panic_gate"
    if family == "spmo_structural":
        return "spmo_structural_gate"
    if family == "dual_market_sma" or variant.get("source") in {"SPY", "QQQ"}:
        return "parent_market_gate"
    if variant.get("breakout"):
        return "buy_stop_reclaim"
    if variant.get("risk_off_weight") is not None:
        return "trend_to_partial"
    if family in {
        "sma", "ema", "absolute", "dual_sma", "monthly_sma",
        "monthly_absolute", "hysteresis",
    }:
        return "trend_to_cash"
    return family


def ablation_summary(
    metrics: dict[str, dict[str, dict[str, Any]]],
    variants: dict[str, dict[str, Any]],
    *,
    baseline_id: str,
) -> list[dict[str, Any]]:
    best: dict[str, dict[str, Any]] = {}
    for candidate_id, variant in variants.items():
        if candidate_id == baseline_id:
            continue
        category = variant_category(variant)
        utilities = [
            fold_utility(metrics[candidate_id][fold], metrics[baseline_id][fold])
            for fold in ("train", "validation")
        ]
        robust = min(utilities) + 0.25 * float(np.mean(utilities))
        row = {
            "category": category,
            "id": candidate_id,
            "development_score": robust,
            "train_utility": utilities[0],
            "validation_utility": utilities[1],
            "full": metrics[candidate_id]["full"],
            "holdout": metrics[candidate_id]["holdout"],
        }
        if category not in best or robust > best[category]["development_score"]:
            best[category] = row
    return sorted(best.values(), key=lambda row: row["category"])


def calendar_stability(
    candidate: PathResult,
    baseline: PathResult,
    cash_returns: pd.Series,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> dict[str, Any]:
    rows = []
    for year in range(start.year + 1, end.year + 1):
        year_start = max(start, pd.Timestamp(year=year, month=1, day=1))
        year_end = min(end, pd.Timestamp(year=year, month=12, day=31))
        c = performance_metrics(candidate, cash_returns, year_start, year_end)
        b = performance_metrics(baseline, cash_returns, year_start, year_end)
        if c.get("observations", 0) < 20 or b.get("observations", 0) < 20:
            continue
        return_win = c["cagr"] > b["cagr"]
        drawdown_win = abs(c["max_drawdown"]) < abs(b["max_drawdown"])
        rows.append({
            "year": year,
            "candidate_cagr": c["cagr"],
            "baseline_cagr": b["cagr"],
            "candidate_max_drawdown": c["max_drawdown"],
            "baseline_max_drawdown": b["max_drawdown"],
            "return_win": return_win,
            "drawdown_win": drawdown_win,
            "both": return_win and drawdown_win,
        })
    total = len(rows)
    return {
        "years": rows,
        "count": total,
        "return_win_rate": sum(row["return_win"] for row in rows) / total if total else None,
        "drawdown_win_rate": sum(row["drawdown_win"] for row in rows) / total if total else None,
        "both_win_rate": sum(row["both"] for row in rows) / total if total else None,
    }


def paired_block_bootstrap(
    candidate: PathResult,
    baseline: PathResult,
    start: pd.Timestamp,
    end: pd.Timestamp,
    *,
    samples: int,
    block: int,
    seed: int,
) -> dict[str, Any]:
    c = segment_series(candidate.equity, start, end).pct_change().dropna()
    b = segment_series(baseline.equity, start, end).pct_change().dropna()
    aligned = pd.concat([c.rename("c"), b.rename("b")], axis=1).dropna()
    if len(aligned) < block * 3 or samples <= 0:
        return {"samples": 0, "reason": "insufficient observations or disabled"}
    if np.allclose(aligned["c"].to_numpy(), aligned["b"].to_numpy(), rtol=0.0, atol=1e-12):
        return {
            "samples": 0,
            "reason": "candidate and baseline paths are identical over the evaluation segment",
            "p_higher_cagr": None,
            "p_shallower_drawdown": None,
            "p_both": None,
        }
    values = aligned.to_numpy()
    n = len(values)
    rng = np.random.default_rng(seed)
    cagr_deltas = []
    return_wins = drawdown_wins = both = 0
    for _ in range(samples):
        sampled = []
        while len(sampled) < n:
            start_i = int(rng.integers(0, n))
            sampled.extend(values[(start_i + np.arange(block)) % n].tolist())
        sample = np.asarray(sampled[:n], dtype=float)
        ceq = np.cumprod(1.0 + sample[:, 0])
        beq = np.cumprod(1.0 + sample[:, 1])
        years = n / TRADING_DAYS
        ccagr = ceq[-1] ** (1.0 / years) - 1.0
        bcagr = beq[-1] ** (1.0 / years) - 1.0
        cdd = float(np.min(ceq / np.maximum.accumulate(ceq) - 1.0))
        bdd = float(np.min(beq / np.maximum.accumulate(beq) - 1.0))
        rw = ccagr > bcagr
        dw = abs(cdd) < abs(bdd)
        return_wins += rw
        drawdown_wins += dw
        both += rw and dw
        cagr_deltas.append(ccagr - bcagr)
    return {
        "samples": samples,
        "block_sessions": block,
        "p_higher_cagr": round(return_wins / samples, 4),
        "p_shallower_drawdown": round(drawdown_wins / samples, 4),
        "p_both": round(both / samples, 4),
        "cagr_delta_95pct": [
            round(float(np.quantile(cagr_deltas, 0.025)), 6),
            round(float(np.quantile(cagr_deltas, 0.975)), 6),
        ],
    }


def restress_transaction_costs(
    path: PathResult,
    *,
    original_cost_bps: float,
    stressed_cost_bps: float,
) -> PathResult:
    """Reprice observed turnover at another cost without changing signals.

    This isolates implementation-cost sensitivity.  It intentionally does not
    assume that a different cost would have changed the strategy's signals.
    """
    equity = path.equity.sort_index()
    returns = equity.pct_change().fillna(0.0)
    turnover = path.turnover.reindex(equity.index).fillna(0.0)
    old = original_cost_bps / 10000.0
    new = stressed_cost_bps / 10000.0
    denominator = (1.0 - turnover * old).clip(lower=EPS)
    cost_ratio = (1.0 - turnover * new).clip(lower=0.0) / denominator
    stressed_factors = (1.0 + returns) * cost_ratio
    stressed_factors.iloc[0] = 1.0
    stressed_equity = stressed_factors.cumprod() * float(equity.iloc[0])
    return PathResult(
        equity=stressed_equity,
        exposure=path.exposure.copy(),
        turnover=path.turnover.copy(),
        events=path.events,
        variant=path.variant,
    )


# --------------------------------------------------------------- candidates
def generic_variants() -> list[dict[str, Any]]:
    variants: list[dict[str, Any]] = [
        {"id": "baseline", "family": "buy_hold", "complexity": 0},
    ]
    for months in (6, 10, 12):
        variants.append({
            "id": f"monthly_sma_{months}m", "family": "monthly_sma", "months": months,
            "rebalance": "monthly", "complexity": 1,
        })
        variants.append({
            "id": f"monthly_sma_{months}m_half",
            "family": "monthly_sma", "months": months,
            "risk_off_weight": 0.5, "rebalance": "monthly", "complexity": 2,
        })
        variants.append({
            "id": f"monthly_sma_{months}m_on125_off50",
            "family": "monthly_sma", "months": months,
            "risk_on_weight": 1.25, "risk_off_weight": 0.5,
            "max_exposure": 1.25, "borrow_spread": 0.03,
            "rebalance": "monthly", "complexity": 4,
            "experimental_leverage": True,
        })
        variants.append({
            "id": f"monthly_abs_{months}m", "family": "monthly_absolute", "months": months,
            "rebalance": "monthly", "complexity": 1,
        })
    for window in (100, 150, 200, 250):
        variants.append({"id": f"sma_{window}", "family": "sma", "window": window, "complexity": 1})
        if window != 250:
            variants.append({
                "id": f"sma_{window}_half", "family": "sma", "window": window,
                "risk_off_weight": 0.5, "complexity": 2,
            })
            variants.append({
                "id": f"sma_{window}_on125_off50", "family": "sma", "window": window,
                "risk_on_weight": 1.25, "risk_off_weight": 0.5,
                "max_exposure": 1.25, "borrow_spread": 0.03,
                "complexity": 4, "experimental_leverage": True,
            })
    for window in (50, 100, 150, 200):
        variants.append({"id": f"ema_{window}", "family": "ema", "window": window, "complexity": 1})
    for window in (63, 126, 189, 252):
        variants.append({"id": f"abs_{window}d", "family": "absolute", "window": window, "complexity": 1})
    for fast, slow in ((20, 100), (50, 150), (50, 200), (100, 200)):
        variants.append({
            "id": f"dual_sma_{fast}_{slow}", "family": "dual_sma", "fast": fast, "slow": slow,
            "complexity": 2,
        })
    for window in (100, 150, 200):
        for buffer in (0.01, 0.02):
            for confirm in (2, 5):
                variants.append({
                    "id": f"hyst_sma{window}_b{int(buffer*100)}_c{confirm}",
                    "family": "hysteresis", "window": window,
                    "entry_buffer": buffer, "exit_buffer": buffer, "confirm": confirm,
                    "complexity": 3,
                })
    for vol_window in (21, 63, 126):
        for target in (0.12, 0.15, 0.18):
            variants.append({
                "id": f"vol{vol_window}_target{int(target*100)}",
                "family": "vol_target", "vol_window": vol_window, "target_vol": target,
                "rebalance": "weekly", "rebalance_band": 0.05, "complexity": 2,
            })
            if vol_window in (21, 63):
                variants.append({
                    "id": f"vol{vol_window}_target{int(target*100)}_floor50",
                    "family": "vol_target", "vol_window": vol_window,
                    "target_vol": target, "min_exposure": 0.5,
                    "rebalance": "weekly", "rebalance_band": 0.05, "complexity": 3,
                })
                if target in (0.15, 0.18):
                    variants.append({
                        "id": f"vol{vol_window}_target{int(target*100)}_max125",
                        "family": "vol_target", "vol_window": vol_window,
                        "target_vol": target, "max_exposure": 1.25,
                        "borrow_spread": 0.03, "rebalance": "weekly",
                        "rebalance_band": 0.05, "complexity": 4,
                        "experimental_leverage": True,
                    })
    for months in (10, 12):
        for vol_window in (63, 126):
            for target in (0.12, 0.15, 0.18):
                variants.append({
                    "id": f"m{months}sma_vol{vol_window}_t{int(target*100)}",
                    "family": "sma_vol", "months": months,
                    "vol_window": vol_window, "target_vol": target,
                    "rebalance": "weekly", "rebalance_band": 0.05, "complexity": 3,
                })
    for stop in (0.10, 0.15, 0.20):
        variants.append({
            "id": f"fixed_stop{int(stop*100)}_next_month",
            "family": "fixed_stop", "hard_stop": stop, "cooldown": 20, "breakout": 20,
            "complexity": 2,
        })
    for stop in (0.05, 0.10, 0.20):
        variants.append({
            "id": f"trailing{int(stop*100)}_reclaim20",
            "family": "trailing_stop", "trailing_stop": stop, "cooldown": 5, "breakout": 20,
            "complexity": 2,
        })
    for arm in (0.10, 0.20, 0.30):
        for trail in (0.08, 0.10, 0.15):
            variants.append({
                "id": f"armed{int(arm*100)}_trail{int(trail*100)}",
                "family": "armed_trailing", "arm_profit": arm, "trailing_stop": trail,
                "cooldown": 5, "breakout": 20, "complexity": 3,
            })
    for target in (0.20, 0.30, 0.50):
        variants.append({
            "id": f"take_profit{int(target*100)}", "family": "take_profit",
            "profit_target": target, "cooldown": 5, "breakout": 20, "complexity": 2,
        })
    for window in (100, 200):
        for breakout in (5, 20, 55):
            variants.append({
                "id": f"sma{window}_reclaim{breakout}", "family": "sma", "window": window,
                "breakout": breakout, "complexity": 2,
            })
    return variants


def top3_portfolio_variants() -> list[dict[str, Any]]:
    variants = [
        {"id": "baseline", "family": "top3_portfolio", "score_mode": "11m", "complexity": 0},
        {"id": "top3_positive_slots", "family": "top3_portfolio", "score_mode": "11m", "positive_only": True, "complexity": 1},
        {"id": "top3_12_1", "family": "top3_portfolio", "score_mode": "12_1", "complexity": 1},
        {"id": "top3_rank_buffer5", "family": "top3_portfolio", "score_mode": "11m", "rank_buffer": 5, "complexity": 1},
        {"id": "top3_rank_buffer6", "family": "top3_portfolio", "score_mode": "11m", "rank_buffer": 6, "complexity": 1},
    ]
    for source in ("SPY", "QQQ"):
        for months in (6, 10, 12):
            variants.append({
                "id": f"top3_{source.lower()}_{months}m_gate",
                "family": "top3_portfolio", "score_mode": "11m", "market_gate": source,
                "gate_months": months, "complexity": 2,
            })
            variants.append({
                "id": f"top3_{source.lower()}_{months}m_half",
                "family": "top3_portfolio", "score_mode": "11m",
                "market_gate": source, "gate_months": months,
                "risk_off_weight": 0.5, "complexity": 3,
            })
    variants.extend([
        {
            "id": "top3_positive_spy10m", "family": "top3_portfolio", "score_mode": "11m",
            "positive_only": True, "market_gate": "SPY", "gate_months": 10, "complexity": 3,
        },
        {
            "id": "top3_positive_qqq10m", "family": "top3_portfolio", "score_mode": "11m",
            "positive_only": True, "market_gate": "QQQ", "gate_months": 10, "complexity": 3,
        },
        {
            "id": "top3_panic_half", "family": "top3_portfolio", "score_mode": "11m",
            "panic_gate": True, "panic_source": "SPY", "panic_window": 504,
            "panic_weight": 0.5, "complexity": 3,
        },
    ])
    for stop in (0.10, 0.15, 0.20):
        variants.append({
            "id": f"top3_position_stop{int(stop*100)}", "family": "top3_portfolio",
            "score_mode": "11m", "position_stop": stop, "complexity": 2,
        })
    return variants


def spmo_special_variants() -> list[dict[str, Any]]:
    variants = []
    for strict in (False, True):
        for atr_mult in (None, 3.0):
            suffix = "strict" if strict else "core"
            if atr_mult:
                suffix += "_3atr"
            variants.append({
                "id": f"spmo_structural_{suffix}", "family": "spmo_structural",
                "strict": strict, "atr_mult": atr_mult, "complexity": 5 if strict else 4,
            })
    for window in (100, 200):
        variants.append({
            "id": f"spmo_spy_sma{window}", "family": "sma", "source": "SPY",
            "window": window, "complexity": 2,
        })
    return variants


def top3_market_overlay_variants() -> list[dict[str, Any]]:
    variants = []
    for source in ("SPY", "QQQ"):
        for months in (10, 12):
            variants.append({
                "id": f"overlay_{source.lower()}_{months}m",
                "family": "monthly_sma", "source": source, "months": months,
                "rebalance": "monthly", "complexity": 2,
            })
    variants.append({
        "id": "overlay_spy_qqq_sma200", "family": "dual_market_sma", "window": 200,
        "complexity": 3,
    })
    for weight in (0.0, 0.5):
        variants.append({
            "id": f"overlay_panic_{int(weight*100)}", "family": "panic_gate", "source": "SPY",
            "market_window": 504, "panic_weight": weight, "complexity": 3,
        })
    return variants


# ----------------------------------------------------------- report helpers
def pct(value: Any, digits: int = 1) -> str:
    return "—" if value is None else f"{float(value) * 100:.{digits}f}%"


def num(value: Any, digits: int = 2) -> str:
    return "—" if value is None else f"{float(value):.{digits}f}"


def current_levels(
    asset: str,
    selected: PathResult,
    base: pd.Series,
    context: dict[str, Any],
) -> dict[str, Any]:
    variant = selected.variant
    gate = build_gate(variant, base, context)
    day = base.dropna().index[-1]
    price = float(base.at[day])
    threshold = gate["threshold"].get(day)
    result = {
        "as_of": str(day.date()),
        "asset": asset,
        "variant": variant["id"],
        "price_or_shadow_nav": _clean_float(price, 4),
        "current_exposure": _clean_float(selected.exposure.reindex(base.index).ffill().get(day), 4),
        "trend_threshold": _clean_float(threshold, 4),
        "signal_source": variant.get("source", "self"),
        "execution_note": "close-confirmed signal; modeled fill is next session close",
    }
    if variant.get("family") in {"vol_target", "sma_vol"}:
        vol_window = int(variant["vol_window"])
        realized = base.pct_change(fill_method=None).rolling(
            vol_window, min_periods=vol_window
        ).std().iloc[-1] * math.sqrt(TRADING_DAYS)
        result.update({
            "realized_vol": _clean_float(realized, 6),
            "target_vol": _clean_float(variant["target_vol"], 6),
            "vol_window": vol_window,
            "min_exposure": _clean_float(variant.get("min_exposure", 0.0), 4),
            "max_exposure": _clean_float(variant.get("max_exposure", 1.0), 4),
        })
    if pd.notna(threshold):
        result["buy_stop"] = _clean_float(
            float(threshold) * (1.0 + float(variant.get("entry_buffer", 0.0))), 4
        )
        result["invalidation_close"] = _clean_float(
            float(threshold) * (1.0 - float(variant.get("exit_buffer", 0.0))), 4
        )
    breakout = int(variant.get("breakout", 0))
    if breakout:
        breakout_level = gate["ref"].rolling(
            breakout, min_periods=breakout
        ).max().shift(1).get(day)
        result["buy_stop_close_proxy"] = _clean_float(breakout_level, 4)
        valid_levels = [
            value for value in (result.get("buy_stop"), result.get("buy_stop_close_proxy"))
            if value is not None
        ]
        if valid_levels:
            result["buy_stop"] = max(valid_levels)
    exposure = selected.exposure.reindex(base.index).ffill().get(day)
    if exposure is not None and exposure > EPS:
        active = selected.exposure.reindex(base.index).ffill().fillna(0.0) > EPS
        inactive_dates = active.index[(active.index < day) & ~active]
        active_start = inactive_dates[-1] if len(inactive_dates) else active.index[0]
        active_prices = base.loc[(base.index > active_start) & (base.index <= day)]
        if len(active_prices):
            entry_price = float(active_prices.iloc[0])
            peak_price = float(active_prices.max())
            result["active_entry_price"] = _clean_float(entry_price, 4)
            result["active_peak_price"] = _clean_float(peak_price, 4)
            if variant.get("hard_stop") is not None:
                result["fixed_stop"] = _clean_float(
                    entry_price * (1.0 - variant["hard_stop"]), 4
                )
            if variant.get("trailing_stop") is not None:
                result["moving_stop"] = _clean_float(
                    peak_price * (1.0 - variant["trailing_stop"]), 4
                )
            if variant.get("profit_target") is not None:
                result["profit_target"] = _clean_float(
                    entry_price * (1.0 + variant["profit_target"]), 4
                )
    if variant.get("family") == "spmo_structural":
        spmo = context["close"]["SPMO"]
        e21 = spmo.ewm(span=21, adjust=False, min_periods=21).mean().iloc[-1]
        e50 = spmo.ewm(span=50, adjust=False, min_periods=50).mean().iloc[-1]
        atr = context["atr"]["SPMO"].iloc[-1]
        result.update({
            "buy_stop": _clean_float(max(spmo.iloc[-1] + 0.10 * atr, e21 + 0.20 * atr), 4),
            "buy_limit_ceiling": _clean_float(max(spmo.iloc[-1] + 0.10 * atr, e21 + 0.20 * atr) + 0.10 * atr, 4),
            "invalidation_close": _clean_float(e50 - 0.20 * atr, 4),
            "raw_3atr_stop": _clean_float(spmo.iloc[-1] - 3.0 * atr, 4),
            "ema21": _clean_float(e21, 4),
            "ema50": _clean_float(e50, 4),
            "atr14": _clean_float(atr, 4),
        })
    return result


def spmo_reference_levels(context: dict[str, Any]) -> dict[str, Any]:
    spmo = context["close"]["SPMO"].dropna()
    day = spmo.index[-1]
    e21 = spmo.ewm(span=21, adjust=False, min_periods=21).mean().at[day]
    e50 = spmo.ewm(span=50, adjust=False, min_periods=50).mean().at[day]
    atr = context["atr"]["SPMO"].at[day]
    buy_stop = max(spmo.at[day] + 0.10 * atr, e21 + 0.20 * atr)
    return {
        "as_of": str(day.date()),
        "price": _clean_float(spmo.at[day], 4),
        "ema21": _clean_float(e21, 4),
        "ema50": _clean_float(e50, 4),
        "atr14": _clean_float(atr, 4),
        "buy_stop": _clean_float(buy_stop, 4),
        "buy_limit_ceiling": _clean_float(buy_stop + 0.10 * atr, 4),
        "invalidation_close": _clean_float(e50 - 0.20 * atr, 4),
        "raw_3atr_stop": _clean_float(spmo.at[day] - 3.0 * atr, 4),
        "note": "structural SPMO sleeve formula; persisted lifecycle stop may only ratchet upward",
    }


def top3_reference_signal(prices: pd.DataFrame) -> dict[str, Any]:
    signal_day = mt.latest_completed_month_signal_date(prices)
    if signal_day is None:
        return {"as_of": None, "holdings": []}
    scores = mt.momentum_scores(
        prices.loc[:signal_day], 11, coverage=0.95, exclude=("SPY", "QQQ")
    ).sort_values(ascending=False, kind="mergesort").head(3)
    future = prices.index[prices.index > signal_day]
    return {
        "as_of": str(signal_day.date()),
        "effective_from": str(future[0].date()) if len(future) else None,
        "holdings": [
            {"ticker": str(ticker), "lookback_return": _clean_float(value, 6), "weight": 1 / 3}
            for ticker, value in scores.items()
        ],
        "warning": "current-universe research signal; not decision-grade",
    }


def render_report(payload: dict[str, Any]) -> str:
    lines = [
        "# 动量策略辅助规则：10 年研究回测",
        "",
        f"生成时间：{payload['generated_at']}  ",
        f"共同评价窗：{payload['window']['start']} → {payload['window']['end']}  ",
        f"数据截止：{payload['data']['as_of']}；价格源：{payload['data']['market_source']}。",
        "",
        "> 结论是研究证据，不是交易指令。ETF 使用 Yahoo 调整后市场价格回报代理，不是发行人官方 NAV total return；Top‑3 使用今天的指数成分重建历史，存在严重幸存者偏差，因此永久标记为非 decision-grade。",
        "",
        "## 先看结论",
        "",
    ]
    verdict = payload["verdict"]
    lines.extend([
        f"- 简单规则阶段：**{verdict['simple_rules_status']}**。",
        f"- 严格标准是在固定 2024+ replay 同时做到 CAGR 不低于基准、最大回撤至少缩小 15%。达到该标准的资产数：**{verdict['strict_pass_count']}/{verdict['asset_count']}**。",
        f"- 强化学习：**{verdict['rl_status']}**。{verdict['rl_reason']}",
        "- 固定止盈只作为反证组；如果它输给不止盈，含义是继续让右尾赢家奔跑，而不是继续微调止盈点。",
    ])
    levered_dominators = [
        row for row in verdict.get("levered_findings", []) if row.get("holdout_dominates")
    ]
    if levered_dominators:
        for row in levered_dominators:
            lines.append(
                f"- 杠杆隔离实验：`{row['id']}` 在 {row['asset']} 2024+ replay 同时提高收益并减小回撤，"
                f"但{'达到' if row['holdout_strict_15pct'] else '未达到'} 15% 回撤改善硬门槛；"
                "该实验含 1.25x 上限和 T-bill+3% 融资成本，不纳入无杠杆主排名。"
            )
    lines.extend([
        "",
        "## 固定 replay 结果（2024+；已被先前研究查看）",
        "",
        "| 策略 | 开发期选中规则 | 基准 CAGR | 规则 CAGR | 基准 MDD | 规则 MDD | Calmar | 暴露 | 严格通过 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---|",
    ])
    for asset, result in payload["assets"].items():
        base = result["metrics"][result["baseline_id"]]["holdout"]
        chosen = result["metrics"][result["selected_id"]]["holdout"]
        lines.append(
            f"| {asset} | `{result['selected_id']}` | {pct(base['cagr'])} | {pct(chosen['cagr'])} | "
            f"{pct(base['max_drawdown'])} | {pct(chosen['max_drawdown'])} | {num(chosen['calmar'])} | "
            f"{pct(chosen['exposure'])} | {'是' if result['strict_holdout_pass'] else '否'} |"
        )
    lines.extend([
        "",
        "本次参数只按 train + validation 排序，2024+ 没有参与本次重排；但该区间已被先前研究查看，不能再称为 untouched holdout。",
        "",
        "## 全 10 年收益 / 回撤 Pareto 对照",
        "",
        "| 策略 | 基准 CAGR / MDD | 选中规则 CAGR / MDD | Sharpe | 年换手 | 年度双胜率 | Block bootstrap 同时胜率 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ])
    for asset, result in payload["assets"].items():
        base = result["metrics"][result["baseline_id"]]["full"]
        chosen = result["metrics"][result["selected_id"]]["full"]
        boot = result["bootstrap"]
        stability = result["calendar_stability"]
        lines.append(
            f"| {asset} | {pct(base['cagr'])} / {pct(base['max_drawdown'])} | "
            f"{pct(chosen['cagr'])} / {pct(chosen['max_drawdown'])} | {num(chosen['sharpe'])} | "
            f"{num(chosen['annual_turnover'])}x | {pct(stability.get('both_win_rate'))} | "
            f"{pct(boot.get('p_both'))} |"
        )
    lines.extend([
        "",
        "## 双倍交易成本压力测试（全 10 年）",
        "",
        "| 策略 | 20bp/边基准 CAGR | 20bp/边规则 CAGR | 规则 MDD |",
        "|---|---:|---:|---:|",
    ])
    for asset, result in payload["assets"].items():
        stress = result["cost_sensitivity_bps"]["20"]
        lines.append(
            f"| {asset} | {pct(stress['baseline']['cagr'])} | {pct(stress['selected']['cagr'])} | "
            f"{pct(stress['selected']['max_drawdown'])} |"
        )
    lines.extend([
        "",
        "## 基础规则消融（每类只显示开发期最稳的一组）",
        "",
        "| 策略 | 规则类别 | 参数 | 开发分 | 全期 CAGR / MDD | Replay CAGR / MDD |",
        "|---|---|---|---:|---:|---:|",
    ])
    for asset, result in payload["assets"].items():
        for row in result["ablation"]:
            lines.append(
                f"| {asset} | {row['category']} | `{row['id']}` | {row['development_score']:.4f} | "
                f"{pct(row['full']['cagr'])} / {pct(row['full']['max_drawdown'])} | "
                f"{pct(row['holdout']['cagr'])} / {pct(row['holdout']['max_drawdown'])} |"
            )
    lines.extend([
        "",
        "## 每组开发期前五名与 2024+ replay 核验",
        "",
    ])
    for asset, result in payload["assets"].items():
        lines.extend([
            f"### {asset}",
            "",
            "| 排名 | 规则 | 稳健分 | Replay CAGR | Replay MDD | 暴露 |",
            "|---:|---|---:|---:|---:|---:|",
        ])
        for rank, row in enumerate(result["ranking"][:5], 1):
            metric = result["metrics"][row["id"]]["holdout"]
            lines.append(
                f"| {rank} | `{row['id']}` | {row['robust_score']:.4f} | {pct(metric['cagr'])} | "
                f"{pct(metric['max_drawdown'])} | {pct(metric['exposure'])} |"
            )
        lines.append("")
    lines.extend([
        "## 当前研究点位（仅用于把规则翻译成可执行语言）",
        "",
        "这些点位以最后一个调整后收盘计算；收盘确认后，回测假设下一交易日收盘成交。它们不是盘中 stop order 的历史成交验证。",
        "",
        "| 策略 | as of | 当前暴露 | 趋势阈值 | buy-stop/突破代理 | invalidation |",
        "|---|---|---:|---:|---:|---:|",
    ])
    for asset, level in payload["levels"].items():
        lines.append(
            f"| {asset} | {level.get('as_of')} | {pct(level.get('current_exposure'))} | "
            f"{num(level.get('trend_threshold'), 4)} | {num(level.get('buy_stop') or level.get('buy_stop_close_proxy'), 4)} | "
            f"{num(level.get('moving_stop') or level.get('invalidation_close') or level.get('fixed_stop'), 4)} |"
        )
    for asset, level in payload["levels"].items():
        if level.get("target_vol") is not None:
            lines.append(
                f"- {asset} 波动目标：{level['vol_window']} 日实现波动 {pct(level['realized_vol'])}，"
                f"目标 {pct(level['target_vol'])}，因此当前研究暴露 {pct(level['current_exposure'])}。"
            )
    spmo_ref = payload["spmo_reference_levels"]
    lines.append(
        f"- SPMO 结构参考（{spmo_ref['as_of']}）：buy-stop {num(spmo_ref['buy_stop'], 4)}，"
        f"stop-limit 上限 {num(spmo_ref['buy_limit_ceiling'], 4)}，"
        f"EMA50 invalidation {num(spmo_ref['invalidation_close'], 4)}，"
        f"原始 3ATR stop {num(spmo_ref['raw_3atr_stop'], 4)}；实际持仓生命周期止损只能上移。"
    )
    top3_ref = payload["top3_reference_signal"]
    holdings = ", ".join(
        f"{row['ticker']}({pct(row['lookback_return'])})" for row in top3_ref.get("holdings", [])
    ) or "无"
    lines.append(
        f"- Top‑3 当前完成月信号（{top3_ref.get('as_of')}，{top3_ref.get('effective_from')} 起生效）："
        f"{holdings}；每只 1/3。该信号仍受当前成分股幸存者偏差污染。"
    )
    lines.extend([
        "",
        "## 实验设计与防止过拟合",
        "",
        f"- Train：{payload['splits']['train']['start']} → {payload['splits']['train']['end']}。",
        f"- Validation：{payload['splits']['validation']['start']} → {payload['splits']['validation']['end']}。",
        f"- Fixed replay（非 untouched）：{payload['splits']['holdout']['start']} → {payload['splits']['holdout']['end']}。",
        f"- 成交成本：每边 {payload['assumptions']['cost_bps']:.0f}bp；现金使用上一观测的 ^IRX 13-week T-bill proxy。",
        "- 开发期目标不是最高收益，而是 CAGR 增量 + 0.75×回撤改善 + 0.05×Sharpe 改善，并使用 train/validation 两段中较差者主导排名。",
        "- 当前平台检查要求同族至少两个成员在 train 与 validation 为非负；它不是严格的参数邻接检验，因此只作为探索性过滤。",
        "- 1.25x 隔离实验按负现金支付 ^IRX + 3% 年化融资差，不参与无杠杆主排名；真实保证金、税务和强平风险会更差。",
        "- 20 日 block bootstrap 只保留短期局部相关性；它是探索性诊断，不能覆盖 6–12 个月趋势状态、参数搜索偏差或独立市场样本不足。",
        "- 真正 buy-stop/盘中止损需要 next-open/high/low 的跳空与同日触发顺序；本轮统一采用更可比的 close-confirmation → next-close。",
        "",
        "## 证据基础",
        "",
        "- 10 个月绝对趋势门与 Top‑3/现金框架：[Faber 原始论文](https://mebfaber.com/wp-content/uploads/2016/05/SSRN-id962461.pdf)。",
        "- 波动率管理：[Moreira–Muir](https://www.nber.org/papers/w22208)、[Barroso–Santa-Clara](https://www.sciencedirect.com/science/article/pii/S0304405X14002566)。",
        "- 动量崩盘的市场状态：[Daniel–Moskowitz](https://www.nber.org/papers/w20439)、[Cooper et al.](https://www.rogutierrez.net/files/States_and_Momentum.pdf)。",
        "- Stop-loss 的条件性而非普适性：[Kaminski–Lo](https://www.sciencedirect.com/science/article/abs/pii/S138641811300030X)。",
        "- SPMO 的 12-1、波动调整与半年再平衡：[S&P Momentum Index methodology](https://www.spglobal.com/spdji/en/documents/methodologies/methodology-sp-momentum-indices.pdf)。",
        "",
        "## 重要限制",
        "",
        "1. Top‑3 股票池是今天的 SPX/NDX/DJIA 并集，遗漏退市与被剔除公司；其收益、排名与 overlay 提升都不能视为真实样本外证据。",
        "2. 十年只有约 120 次月度信号，且只有少数危机；单市场深度强化学习会非常容易记住路径。",
        "3. yfinance/Yahoo 的 auto-adjusted OHLC 是按调整因子缩放的市场价格代理；actions=False 未保存企业行动账本，历史也可能被数据商修订。JSON 的输入哈希只能识别本次快照。",
        "4. SPMO ETF 2015 年 10 月才有真实交易历史；本研究没有把更早的假设指数回填混入 ETF 回测。",
        "",
        "## 下一阶段门槛",
        "",
        "只有取得 point-in-time 成分股、退市股和一致企业行动数据后，才值得把 Top‑3 扩展为 nested walk-forward 或强化学习环境。若进入 RL，动作应先限制为 0/25/50/75/100% 暴露，奖励为净 log return − 换手成本 − drawdown/CVaR 惩罚，并用多个独立市场与完全冻结外层测试。",
        "",
    ])
    return "\n".join(lines)


# ------------------------------------------------------------------- driver
def build_study(
    market_frame: pd.DataFrame,
    top3_prices: pd.DataFrame,
    *,
    market_source: str,
    market_warning: str | None,
    years: int,
    cost_bps: float,
    bootstrap_samples: int,
    market_cache_path: Path = DEFAULT_MARKET_CACHE,
    top3_cache_path: Path = DEFAULT_TOP3_CACHE,
) -> tuple[dict[str, Any], dict[str, dict[str, PathResult]]]:
    closes = {ticker: market_close(market_frame, ticker) for ticker in ("SPY", "QQQ", "SPMO")}
    common_end = min(series.index[-1] for series in closes.values())
    common_end = min(common_end, top3_prices.index[-1])
    requested_start = common_end - pd.DateOffset(years=years)
    common_index = closes["SPY"].index.intersection(closes["QQQ"].index).intersection(closes["SPMO"].index)
    start_candidates = common_index[common_index >= requested_start]
    if not len(start_candidates):
        raise ValueError("no common evaluation start")
    common_start = start_candidates[0]

    master_index = market_frame.index[market_frame.index <= common_end]
    cash_returns = cash_returns_from_irx(market_frame, master_index)
    context = {
        "close": {ticker: closes[ticker].reindex(master_index).ffill() for ticker in closes},
        "atr": {"SPMO": wilder_atr(market_frame, "SPMO").reindex(master_index)},
    }

    train_end = pd.Timestamp("2021-12-31")
    validation_start = pd.Timestamp("2022-01-01")
    validation_end = pd.Timestamp("2023-12-31")
    holdout_start = pd.Timestamp("2024-01-01")
    if common_start >= train_end or common_end <= holdout_start:
        train_end = common_start + (common_end - common_start) * 0.55
        validation_start = train_end + pd.Timedelta(days=1)
        validation_end = common_start + (common_end - common_start) * 0.75
        holdout_start = validation_end + pd.Timedelta(days=1)
    splits = {
        "train": (common_start, train_end),
        "validation": (validation_start, validation_end),
        "holdout": (holdout_start, common_end),
        "full": (common_start, common_end),
    }

    paths: dict[str, dict[str, PathResult]] = {}
    variant_maps: dict[str, dict[str, dict[str, Any]]] = {}
    generic = generic_variants()
    for asset in ("SPY", "QQQ", "SPMO"):
        base = closes[asset].reindex(master_index).dropna()
        asset_variants = list(generic)
        if asset == "SPMO":
            asset_variants += spmo_special_variants()
        asset_paths = {}
        variants = {}
        for variant in asset_variants:
            variants[variant["id"]] = variant
            asset_paths[variant["id"]] = simulate_overlay(
                base, cash_returns, variant, context,
                start=common_start, end=common_end, cost_bps=cost_bps,
            )
        paths[asset] = asset_paths
        variant_maps[asset] = variants

    # Build Top-3 paths directly so positive-slot, rank-buffer and position-stop
    # experiments operate on constituent weights rather than a synthetic NAV.
    top3_paths = {}
    top3_variants = {}
    market_map = {ticker: context["close"][ticker] for ticker in ("SPY", "QQQ")}
    for variant in top3_portfolio_variants():
        top3_variants[variant["id"]] = variant
        top3_paths[variant["id"]] = run_top3_portfolio(
            top3_prices, market_map, cash_returns, variant,
            start=common_start, end=common_end, cost_bps=cost_bps,
        )
    top3_base = top3_paths["baseline"].equity
    for variant in generic_variants()[1:] + top3_market_overlay_variants():
        candidate_id = "nav_" + variant["id"]
        adjusted = {**variant, "id": candidate_id, "complexity": variant.get("complexity", 1) + 1}
        top3_variants[candidate_id] = adjusted
        top3_paths[candidate_id] = simulate_overlay(
            top3_base, cash_returns, adjusted, context,
            start=common_start, end=common_end, cost_bps=cost_bps,
        )
    paths["TOP3_11M"] = top3_paths
    variant_maps["TOP3_11M"] = top3_variants

    assets_payload: dict[str, Any] = {}
    levels: dict[str, Any] = {}
    strict_passes = 0
    for asset, candidates in paths.items():
        metrics: dict[str, dict[str, dict[str, Any]]] = {}
        for candidate_id, path in candidates.items():
            metrics[candidate_id] = {
                fold: performance_metrics(path, cash_returns, *bounds)
                for fold, bounds in splits.items()
            }
        selected_id, ranking = choose_candidate(metrics, variant_maps[asset], baseline_id="baseline")
        selected_holdout = metrics[selected_id]["holdout"]
        baseline_holdout = metrics["baseline"]["holdout"]
        strict = bool(
            selected_holdout["cagr"] >= baseline_holdout["cagr"]
            and abs(selected_holdout["max_drawdown"]) <= 0.85 * abs(baseline_holdout["max_drawdown"])
        )
        strict_passes += strict
        seed = int(hashlib.sha256(asset.encode("utf-8")).hexdigest()[:8], 16)
        bootstrap = paired_block_bootstrap(
            candidates[selected_id], candidates["baseline"], *splits["holdout"],
            samples=bootstrap_samples, block=20, seed=seed,
        )
        stability = calendar_stability(
            candidates[selected_id], candidates["baseline"], cash_returns,
            common_start, common_end,
        )
        selected_for_cost = candidates[selected_id]
        if asset == "TOP3_11M" and selected_id.startswith("nav_"):
            # The NAV overlay pays its own allocation changes on top of the
            # underlying monthly Top-3 turnover already embedded in the NAV.
            selected_for_cost = PathResult(
                equity=selected_for_cost.equity,
                exposure=selected_for_cost.exposure,
                turnover=(
                    selected_for_cost.turnover
                    + candidates["baseline"].turnover.reindex(
                        selected_for_cost.turnover.index
                    ).fillna(0.0)
                ),
                events=selected_for_cost.events,
                variant=selected_for_cost.variant,
            )
        cost_sensitivity = {}
        for stressed_bps in (5.0, cost_bps, 20.0):
            stressed_selected = restress_transaction_costs(
                selected_for_cost,
                original_cost_bps=cost_bps,
                stressed_cost_bps=stressed_bps,
            )
            stressed_baseline = restress_transaction_costs(
                candidates["baseline"],
                original_cost_bps=cost_bps,
                stressed_cost_bps=stressed_bps,
            )
            cost_sensitivity[str(int(stressed_bps))] = {
                "selected": performance_metrics(
                    stressed_selected, cash_returns, *splits["full"]
                ),
                "baseline": performance_metrics(
                    stressed_baseline, cash_returns, *splits["full"]
                ),
            }
        assets_payload[asset] = {
            "baseline_id": "baseline",
            "selected_id": selected_id,
            "selected_variant": variant_maps[asset][selected_id],
            "strict_holdout_pass": strict,
            "ranking": [_json_safe(row) for row in ranking[:20]],
            "metrics": _json_safe(metrics),
            "bootstrap": bootstrap,
            "calendar_stability": _json_safe(stability),
            "ablation": _json_safe(
                ablation_summary(metrics, variant_maps[asset], baseline_id="baseline")
            ),
            "cost_sensitivity_bps": _json_safe(cost_sensitivity),
            "candidate_count": len(candidates),
        }
        base_for_level = closes[asset].reindex(master_index).dropna() if asset != "TOP3_11M" else top3_base
        levels[asset] = current_levels(asset, candidates[selected_id], base_for_level, context)

    levered_findings = []
    for asset, result in assets_payload.items():
        rows = [row for row in result["ablation"] if row["category"] == "levered_risk_managed"]
        if not rows:
            continue
        row = rows[0]
        base_full = result["metrics"]["baseline"]["full"]
        base_holdout = result["metrics"]["baseline"]["holdout"]
        levered_findings.append({
            "asset": asset,
            "id": row["id"],
            "full_dominates": bool(
                row["full"]["cagr"] >= base_full["cagr"]
                and abs(row["full"]["max_drawdown"]) < abs(base_full["max_drawdown"])
            ),
            "holdout_dominates": bool(
                row["holdout"]["cagr"] >= base_holdout["cagr"]
                and abs(row["holdout"]["max_drawdown"]) < abs(base_holdout["max_drawdown"])
            ),
            "holdout_strict_15pct": bool(
                row["holdout"]["cagr"] >= base_holdout["cagr"]
                and abs(row["holdout"]["max_drawdown"]) <= 0.85 * abs(base_holdout["max_drawdown"])
            ),
            "full": row["full"],
            "holdout": row["holdout"],
        })

    simple_status = (
        "存在可继续验证的无杠杆简单规则"
        if strict_passes
        else "未找到跨固定 replay 严格占优的无杠杆简单规则"
    )
    if strict_passes >= 2:
        rl_status = "暂不进入"
        rl_reason = "简单、可解释的规则已经达到部分严格门槛，先做更长历史与 point-in-time 复核更划算。"
    else:
        rl_status = "仍不建议立即进入"
        rl_reason = "失败更可能来自样本短和 Top-3 数据偏差；在修复 point-in-time 数据前，RL 只会放大过拟合。"

    payload = {
        "schemaVersion": 1,
        "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "researchOnly": True,
        "decisionGrade": False,
        "window": {"start": str(common_start.date()), "end": str(common_end.date()), "years": years},
        "splits": {
            key: {"start": str(bounds[0].date()), "end": str(bounds[1].date())}
            for key, bounds in splits.items() if key != "full"
        },
        "assumptions": {
            "signal": "completed close",
            "fill": "next session close",
            "cost_bps": cost_bps,
            "cash": "prior-observation ^IRX 13-week T-bill proxy",
            "adjusted_prices": True,
            "main_selection_leverage_cap": 1.0,
            "experimental_leverage_cap": 1.25,
            "selection": "train/validation worst-fold robust utility; 2024+ excluded from this run's ranking but previously exposed",
        },
        "data": {
            "as_of": str(common_end.date()),
            "market_source": market_source,
            "market_warning": market_warning,
            "market_cache_sha256": sha256_path(market_cache_path),
            "top3_cache_sha256": sha256_path(top3_cache_path),
            "top3_rows": int(top3_prices.shape[0]),
            "top3_columns": int(top3_prices.shape[1]),
            "top3_current_universe_survivorship": True,
            "python": platform.python_version(),
            "pandas": pd.__version__,
            "numpy": np.__version__,
            "yfinance": installed_version("yfinance"),
        },
        "verdict": {
            "simple_rules_status": simple_status,
            "strict_pass_count": strict_passes,
            "asset_count": len(paths),
            "strict_rule": "fixed replay CAGR >= baseline and absolute max drawdown <= 85% of baseline",
            "rl_status": rl_status,
            "rl_reason": rl_reason,
            "levered_findings": levered_findings,
        },
        "assets": assets_payload,
        "levels": _json_safe(levels),
        "spmo_reference_levels": _json_safe(spmo_reference_levels(context)),
        "top3_reference_signal": _json_safe(top3_reference_signal(top3_prices)),
        "limitations": [
            "Top-3 uses today's SPX/NDX/DJIA members and is survivor-biased.",
            "Displayed parameters share a ten-year history and are not independent trials.",
            "Close-confirmation/next-close execution does not prove intraday stop fills.",
            "SPMO uses live ETF history only; no hypothetical pre-inception index backfill.",
        ],
    }
    return payload, paths


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--years", type=int, default=10)
    parser.add_argument("--cost-bps", type=float, default=DEFAULT_COST_BPS)
    parser.add_argument("--bootstrap-samples", type=int, default=500)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--market-cache", type=Path, default=DEFAULT_MARKET_CACHE)
    parser.add_argument("--top3-cache", type=Path, default=DEFAULT_TOP3_CACHE)
    parser.add_argument("--no-fetch", action="store_true")
    args = parser.parse_args()
    if args.years < 5:
        parser.error("--years must be at least 5")
    if args.cost_bps < 0 or args.bootstrap_samples < 0:
        parser.error("cost and bootstrap sample count must be non-negative")

    # Fetch ample history for monthly trend, 24-month panic state and warmup.
    today = pd.Timestamp(dt.date.today())
    fetch_start = today - pd.DateOffset(years=max(args.years + 3, 13))
    market, source, warning = load_market_data(
        fetch_start,
        today,
        cache_path=args.market_cache,
        no_fetch=args.no_fetch,
    )
    top3_prices = mt.read_price_cache(args.top3_cache)
    payload, _ = build_study(
        market,
        top3_prices,
        market_source=source,
        market_warning=warning,
        years=args.years,
        cost_bps=args.cost_bps,
        bootstrap_samples=args.bootstrap_samples,
        market_cache_path=args.market_cache,
        top3_cache_path=args.top3_cache,
    )
    args.out_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(args.out_dir / "momentum_overlay_research.json", _json_safe(payload))
    atomic_write_text(args.out_dir / "momentum_overlay_research.md", render_report(payload))
    print(f"wrote {args.out_dir / 'momentum_overlay_research.json'}")
    print(f"wrote {args.out_dir / 'momentum_overlay_research.md'}")
    for asset, result in payload["assets"].items():
        holdout = result["metrics"][result["selected_id"]]["holdout"]
        print(
            f"{asset}: {result['selected_id']} | holdout CAGR {pct(holdout['cagr'])} "
            f"MDD {pct(holdout['max_drawdown'])} | strict={result['strict_holdout_pass']}"
        )


if __name__ == "__main__":
    main()

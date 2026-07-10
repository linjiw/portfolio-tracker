#!/usr/bin/env python3
"""Momentum Top-3 sleeve: signals + 8y backtest + dashboard artifact.

Implements three strategies selected from the July-2026 40-variant sweep
(see ~/.hermes skill quant-momentum-research / references/findings-2026-07.md):

  1. RET_11M_top3  — 11-month total return, top-3 equal weight (flagship)
  2. RET_9M_top3   —  9-month total return, top-3 equal weight (balanced)
  3. RET_5M_top5   —  5-month total return, top-5 equal weight (breadth variant)

Design rules (no lookahead): signals are computed at month-end close and filled
at the next session's close. The prior portfolio earns the signal-to-fill close
return; only then are turnover and per-side costs applied. Benchmarks SPY/QQQ
ride along.

HONESTY NOTE: the universe is *today's* index membership and these variants
were chosen in-sample from a 40-variant sweep. Neither absolute performance nor
cross-strategy ranking is decision-grade or out-of-sample evidence. This is a
research diagnostic, not a trading signal.

Outputs (under --out-dir, default output/momentum_top3/):
  momentum_top3.json  — metrics, weekly equity curves, current signals
  momentum_top3.html  — standalone dark-theme dashboard (no external deps)

Usage:
  python3 scripts/momentum_top3.py              # fetch prices (cached)
  python3 scripts/momentum_top3.py --no-fetch   # offline, reuse cache
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import io
import json
import math
import os
import sys
import time
from contextlib import contextmanager
from html.parser import HTMLParser
from pathlib import Path

import numpy as np
import pandas as pd

try:
    from scripts.artifact_io import atomic_write_json, atomic_write_text
except ModuleNotFoundError:  # direct ``python scripts/momentum_top3.py`` execution
    from artifact_io import atomic_write_json, atomic_write_text

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "data" / "momentum_config.json"
DEFAULT_OUT = ROOT / "output" / "momentum_top3"
PRICE_CACHE = ROOT / "output" / "momentum_prices.csv.gz"
CACHE_LOCK = ROOT / "output" / ".momentum_prices.lock"
try:
    pd.tseries.frequencies.to_offset("ME")
    MONTH_END_FREQ = "ME"
except ValueError:
    MONTH_END_FREQ = "M"

WIKI = {
    "SPX": ("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies", "Symbol"),
    "NDX": ("https://en.wikipedia.org/wiki/Nasdaq-100", "Ticker"),
    "DJIA": ("https://en.wikipedia.org/wiki/Dow_Jones_Industrial_Average", "Symbol"),
}
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) portfolio-tracker/1.0"}


# ---------------------------------------------------------------- data layer
class _WikiTableParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tables = []
        self._table = None
        self._row = None
        self._cell = None

    def handle_starttag(self, tag, attrs):
        if tag == "table" and self._table is None:
            self._table = []
        elif tag == "tr" and self._table is not None:
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell = []

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag):
        if tag in {"td", "th"} and self._cell is not None and self._row is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None and self._table is not None:
            if any(self._row):
                self._table.append(self._row)
            self._row = None
        elif tag == "table" and self._table is not None:
            if self._table:
                self.tables.append(self._table)
            self._table = None


def _tickers_from_html_table(html, column):
    """Small stdlib fallback for Wikipedia constituents when pandas lacks lxml."""
    parser = _WikiTableParser()
    parser.feed(html)
    tickers = set()
    for table in parser.tables:
        if len(table) <= 20:
            continue
        header_idx = None
        col_idx = None
        for i, row in enumerate(table[:5]):
            normalized = [c.strip() for c in row]
            if column in normalized:
                header_idx = i
                col_idx = normalized.index(column)
                break
        if header_idx is None or col_idx is None:
            continue
        for row in table[header_idx + 1:]:
            if col_idx < len(row):
                ticker = row[col_idx].strip().replace(".", "-")
                if ticker and ticker.lower() != "nan":
                    tickers.add(ticker)
        if tickers:
            break
    return tickers


def fetch_universe(indices):
    import requests

    tickers = set()
    for idx in indices:
        url, col = WIKI[idx]
        r = requests.get(url, headers=UA, timeout=30)
        r.raise_for_status()
        found = set()
        try:
            for tbl in pd.read_html(io.StringIO(r.text)):
                cols = [str(c) for c in tbl.columns]
                if col in cols and len(tbl) > 20:
                    found = {str(t).strip().replace(".", "-") for t in tbl[col]}
                    break
        except ImportError:
            found = _tickers_from_html_table(r.text, col)
        if not found:
            found = _tickers_from_html_table(r.text, col)
        if not found:
            raise RuntimeError(f"Could not parse {idx} constituents from {url}")
        tickers |= found
    return sorted(t for t in tickers if t and t != "nan")


def normalize_price_frame(prices):
    """Normalize provider/cache output without inventing any price observations."""
    if isinstance(prices, pd.Series):
        prices = prices.to_frame()
    if not isinstance(prices, pd.DataFrame):
        return pd.DataFrame()
    attrs = dict(prices.attrs)
    frame = prices.copy()
    frame.columns = [str(column).strip() for column in frame.columns]
    frame.index = pd.to_datetime(frame.index, errors="coerce")
    frame = frame.loc[~frame.index.isna()]
    if getattr(frame.index, "tz", None) is not None:
        frame.index = frame.index.tz_convert(None)
    frame = frame.loc[~frame.index.duplicated(keep="last")].sort_index()
    frame = frame.apply(pd.to_numeric, errors="coerce")
    frame = frame.replace([np.inf, -np.inf], np.nan)
    frame = frame.dropna(axis=0, how="all").dropna(axis=1, how="all")
    frame.attrs.update(attrs)
    return frame


def read_price_cache(path=None):
    path = PRICE_CACHE if path is None else Path(path)
    return normalize_price_frame(
        pd.read_csv(path, index_col=0, parse_dates=True, compression="gzip")
    )


def price_frame_errors(prices, cfg, expected_tickers=None, enforce_cache_floor=False):
    """Return reasons a price frame is unsafe for signals/cache publication."""
    px = normalize_price_frame(prices)
    errors = []
    if px.empty or len(px.index) < 2:
        return ["price frame is empty or has fewer than two observations"]
    if px.columns.duplicated().any():
        errors.append("duplicate ticker columns")
        return errors
    if not px.index.is_monotonic_increasing or px.index.has_duplicates:
        errors.append("price index is not strictly ordered and unique")

    benchmarks = [str(ticker) for ticker in cfg["backtest"]["benchmarks"]]
    end = px.index[-1]
    min_span_days = max(30, int(float(cfg["backtest"]["years"]) * 365.25 * 0.90))
    for benchmark in benchmarks:
        if benchmark not in px.columns:
            errors.append(f"missing benchmark {benchmark}")
            continue
        series = px[benchmark].dropna()
        if series.empty:
            errors.append(f"benchmark {benchmark} has no prices")
            continue
        if series.last_valid_index() != end:
            errors.append(f"benchmark {benchmark} is stale at frame end {end.date()}")
        if (series <= 0).any():
            errors.append(f"benchmark {benchmark} contains non-positive prices")
        if (series.index[-1] - series.index[0]).days < min_span_days:
            errors.append(f"benchmark {benchmark} history is shorter than the configured window")

    universe = [column for column in px.columns if column not in set(benchmarks)]
    if enforce_cache_floor:
        minimum = int(cfg["universe"].get("min_cache_members", 25))
        if len(universe) < minimum:
            errors.append(f"universe has {len(universe)} members; requires at least {minimum}")

    if expected_tickers:
        expected = {str(ticker) for ticker in expected_tickers}
        present = {column for column in px.columns if px[column].notna().any()}
        fresh = {column for column in present if px[column].last_valid_index() == end}
        minimum_download = float(cfg["universe"].get("min_download_fraction", 0.90))
        minimum_fresh = float(cfg["universe"].get("min_fresh_fraction", 0.80))
        download_ratio = len(present & expected) / max(len(expected), 1)
        fresh_ratio = len(fresh & expected) / max(len(expected), 1)
        if download_ratio < minimum_download:
            errors.append(
                f"download coverage {download_ratio:.1%} is below {minimum_download:.1%}"
            )
        if fresh_ratio < minimum_fresh:
            errors.append(f"fresh-symbol coverage {fresh_ratio:.1%} is below {minimum_fresh:.1%}")

    fresh_universe = [
        ticker for ticker in universe
        if px[ticker].last_valid_index() == end
    ]
    maximum_top_n = max((int(s["top_n"]) for s in cfg.get("strategies", [])), default=1)
    if len(fresh_universe) < maximum_top_n:
        errors.append(
            f"only {len(fresh_universe)} current universe members; strategy requires {maximum_top_n}"
        )
    return errors


def _validated_cache(cfg, path=None):
    path = PRICE_CACHE if path is None else Path(path)
    if not path.exists():
        return None, "cache missing"
    try:
        cached = read_price_cache(path)
    except Exception as exc:
        return None, f"cache unreadable: {type(exc).__name__}: {exc}"
    errors = price_frame_errors(cached, cfg, enforce_cache_floor=True)
    if errors:
        return None, "; ".join(errors)
    return cached, None


@contextmanager
def cache_publish_lock(path=CACHE_LOCK):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a+", encoding="utf-8") as handle:
        try:
            path.chmod(0o600)
        except OSError:
            pass
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def publish_price_cache(prices, cfg, expected_tickers, path=None, lock_path=None):
    """Validate, round-trip, and atomically publish without downgrading newer data."""
    path = PRICE_CACHE if path is None else Path(path)
    lock_path = Path(lock_path) if lock_path else path.with_name(f".{path.name}.lock")
    candidate = normalize_price_frame(prices)
    errors = price_frame_errors(
        candidate,
        cfg,
        expected_tickers=expected_tickers,
        enforce_cache_floor=True,
    )
    if errors:
        raise ValueError("price cache candidate rejected: " + "; ".join(errors))

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}.{time.time_ns()}")
    try:
        candidate.to_csv(temporary, compression="gzip")
        round_trip = read_price_cache(temporary)
        round_trip_errors = price_frame_errors(
            round_trip,
            cfg,
            expected_tickers=expected_tickers,
            enforce_cache_floor=True,
        )
        if round_trip_errors:
            raise ValueError("round-trip cache validation failed: " + "; ".join(round_trip_errors))
        temporary.chmod(0o600)
        with cache_publish_lock(lock_path):
            existing, _ = _validated_cache(cfg, path)
            if existing is not None and existing.index[-1] > round_trip.index[-1]:
                existing.attrs["universeSource"] = "newer_price_cache"
                existing.attrs["universeWarning"] = "newer validated cache preserved over older download"
                return existing
            os.replace(temporary, path)
        return round_trip
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _fallback_cache(cached, reason):
    if cached is None:
        raise RuntimeError(reason)
    cached.attrs["universeSource"] = "price_cache_fallback"
    cached.attrs["universeWarning"] = reason
    print(f"· {reason}; preserved validated last-known-good cache", file=sys.stderr)
    return cached


def load_prices(cfg, no_fetch=False):
    cached, cache_error = _validated_cache(cfg)
    if no_fetch:
        if cached is None:
            raise RuntimeError(f"--no-fetch requires a validated cache at {PRICE_CACHE}: {cache_error}")
        cached.attrs["universeSource"] = "price_cache"
        cached.attrs["universeWarning"] = None
        return cached

    import yfinance as yf

    universe_source = "live_index_constituents"
    universe_warning = None
    try:
        tickers = fetch_universe(cfg["universe"]["indices"])
    except Exception as exc:
        if cached is None:
            raise RuntimeError(
                f"constituent refresh failed ({exc}) and no validated cache is available ({cache_error})"
            ) from exc
        benchmarks = set(cfg["backtest"]["benchmarks"])
        tickers = [str(column) for column in cached.columns if str(column) not in benchmarks]
        universe_source = "cached_constituents_live_prices"
        universe_warning = f"constituent refresh failed; reused cached universe: {exc}"
        print(f"· {universe_warning}", file=sys.stderr)
    tickers = sorted(set(tickers) | set(cfg["backtest"]["benchmarks"]))
    end = dt.date.today() + dt.timedelta(days=1)
    start = end - dt.timedelta(days=int(365.25 * (cfg["backtest"]["years"] + 3.2)))
    try:
        downloaded = yf.download(
            tickers,
            start=str(start),
            end=str(end),
            auto_adjust=True,
            threads=True,
            progress=False,
        )
        px = normalize_price_frame(downloaded["Close"])
    except Exception as exc:
        return _fallback_cache(cached, f"price download failed: {type(exc).__name__}: {exc}")

    errors = price_frame_errors(
        px,
        cfg,
        expected_tickers=tickers,
        enforce_cache_floor=True,
    )
    if errors:
        return _fallback_cache(cached, "price download rejected: " + "; ".join(errors))

    px.attrs["universeSource"] = universe_source
    px.attrs["universeWarning"] = universe_warning
    published = publish_price_cache(px, cfg, tickers)
    if published.attrs.get("universeSource") == "newer_price_cache":
        return published
    published.attrs["universeSource"] = universe_source
    published.attrs["universeWarning"] = universe_warning
    return published


# ------------------------------------------------------------- signal layer
def asof_price(hist, months):
    ts = hist.index[-1] - pd.DateOffset(months=months)
    # Use the last observable close on or before the calendar anchor. Choosing
    # the first close after a weekend/holiday silently shortens the lookback.
    idx = hist.index[hist.index <= ts]
    if not len(idx):
        return hist.iloc[0] * np.nan
    return hist.loc[idx[-1]]


def valid_columns(hist, months=13, coverage=0.95):
    w = hist.loc[hist.index >= hist.index[-1] - pd.DateOffset(months=months)]
    return w.columns[w.notna().sum() >= int(len(w) * coverage)]


def momentum_scores(hist, lookback_months, coverage=0.95, exclude=()):
    cols = [c for c in valid_columns(hist, coverage=coverage) if c not in exclude]
    h = hist[cols]
    return (h.iloc[-1] / asof_price(h, lookback_months) - 1).dropna()


# ----------------------------------------------------------- backtest layer
def month_end_index(px):
    return px.groupby(px.index.to_period("M")).tail(1).index


def latest_completed_month_signal_date(px):
    """Return the last observed close in a month strictly before the data month."""
    if px.empty:
        return None
    end_period = px.index[-1].to_period("M")
    candidates = month_end_index(px)
    candidates = candidates[candidates.to_period("M") < end_period]
    return candidates[-1] if len(candidates) else None


def weekday_age(end, today=None):
    today = pd.Timestamp(today or dt.date.today()).date()
    start = pd.Timestamp(end).date()
    if start > today:
        return -1
    age = 0
    cursor = start + dt.timedelta(days=1)
    while cursor <= today:
        age += cursor.weekday() < 5
        cursor += dt.timedelta(days=1)
    return age


def run_backtest(px, lookback_months, top_n, start, end, cost_per_side,
                 coverage=0.95, exclude=(), return_diagnostics=False):
    """Monthly close-to-close simulation with conservative next-close fills.

    A month-end signal is not executable at the close that created it. The
    existing portfolio earns the next session's close-to-close return, drifts
    with that return, and is rebalanced at that next close. Turnover and costs
    are calculated from those pre-trade drifted weights. The returned equity
    curve includes a 1.0 initial-capital anchor.
    """
    px = normalize_price_frame(px)
    window = px.index[(px.index >= pd.Timestamp(start)) & (px.index <= pd.Timestamp(end))]
    diagnostics = {
        "anchorDate": None,
        "signalTiming": "month-end close",
        "fillTiming": "next session close",
        "fillDayExposure": "prior weights through fill close",
        "returnContinuity": (
            "held positions use cumulative returns from their last observed close after a price gap; "
            "rebalances require valid closes for every held and target symbol"
        ),
        "fills": [],
        "skippedFills": [],
        "priceGapObservations": [],
        "cumulativeGapReturns": [],
        "unresolvedPriceGaps": [],
    }
    if not len(window):
        empty = pd.Series(dtype=float)
        return (empty, 0.0, diagnostics) if return_diagnostics else (empty, 0.0)

    anchor = window[0]
    last_date = window[-1]
    diagnostics["anchorDate"] = str(anchor.date())
    signal_dates = month_end_index(px)
    signal_dates = signal_dates[(signal_dates >= anchor) & (signal_dates <= last_date)]
    fill_schedule = {}
    for signal_date in signal_dates:
        future = px.index[(px.index > signal_date) & (px.index <= last_date)]
        if not len(future):
            continue
        fill_date = future[0]
        scores = momentum_scores(
            px.loc[:signal_date], lookback_months,
            coverage=coverage, exclude=exclude,
        )
        top = scores.sort_values(ascending=False, kind="mergesort").head(top_n)
        target = (pd.Series(1 / len(top), index=top.index, dtype=float)
                  if len(top) else pd.Series(dtype=float))
        fill_schedule[fill_date] = {"signalDate": signal_date, "target": target}

    equity_value = 1.0
    equity_values = [equity_value]
    equity_dates = [anchor]
    current_w = pd.Series(dtype=float)
    last_mark_prices = pd.Series(dtype=float)
    last_mark_dates = {}
    open_gaps = {}
    turnover_sum = 0.0
    executed = 0
    for day in window[1:]:
        if len(current_w):
            held_returns = pd.Series(0.0, index=current_w.index, dtype=float)
            for symbol in current_w.index:
                current_close = px.at[day, symbol] if symbol in px.columns else np.nan
                observed = pd.notna(current_close) and np.isfinite(current_close) and float(current_close) > 0
                if not observed:
                    if symbol not in open_gaps:
                        open_gaps[symbol] = {
                            "symbol": str(symbol),
                            "lastObservedDate": (
                                str(last_mark_dates[symbol].date()) if symbol in last_mark_dates else None
                            ),
                            "firstMissingDate": str(day.date()),
                        }
                    diagnostics["priceGapObservations"].append({
                        "symbol": str(symbol),
                        "date": str(day.date()),
                        "lastObservedDate": open_gaps[symbol]["lastObservedDate"],
                    })
                    continue

                prior_close = last_mark_prices.get(symbol)
                if pd.isna(prior_close) or not np.isfinite(prior_close) or float(prior_close) <= 0:
                    raise ValueError(f"missing valid prior mark for held symbol {symbol} on {day.date()}")
                cumulative_return = float(current_close) / float(prior_close) - 1.0
                held_returns.at[symbol] = cumulative_return
                if symbol in open_gaps:
                    gap = open_gaps.pop(symbol)
                    diagnostics["cumulativeGapReturns"].append({
                        **gap,
                        "recoveredDate": str(day.date()),
                        "cumulativeReturnPct": round(cumulative_return * 100.0, 8),
                    })
                last_mark_prices.at[symbol] = float(current_close)
                last_mark_dates[symbol] = day

            portfolio_return = float((current_w * held_returns).sum())
            equity_value *= 1.0 + portfolio_return
            grown = current_w * (1.0 + held_returns)
            gross = float(grown.sum())
            current_w = grown / gross if gross > 0 else pd.Series(dtype=float)

        scheduled = fill_schedule.get(day)
        if scheduled is not None:
            target = scheduled["target"]
            required_quotes = current_w.index.union(target.index)
            missing_quotes = sorted(
                symbol for symbol in required_quotes
                if (
                    symbol not in px.columns
                    or pd.isna(px.at[day, symbol])
                    or not np.isfinite(px.at[day, symbol])
                    or float(px.at[day, symbol]) <= 0
                )
            )
            if missing_quotes:
                diagnostics["skippedFills"].append({
                    "signalDate": str(scheduled["signalDate"].date()),
                    "fillDate": str(day.date()),
                    "reason": "missing close for " + ", ".join(missing_quotes),
                })
            else:
                both = target.index.union(current_w.index)
                pretrade = current_w.reindex(both, fill_value=0.0)
                posttrade = target.reindex(both, fill_value=0.0)
                gross_traded = float((posttrade - pretrade).abs().sum())
                one_way_turnover = gross_traded / 2.0
                cost_rate = gross_traded * float(cost_per_side)
                equity_before_cost = equity_value
                equity_value *= max(0.0, 1.0 - cost_rate)
                turnover_sum += one_way_turnover
                executed += 1
                diagnostics["fills"].append({
                    "signalDate": str(scheduled["signalDate"].date()),
                    "fillDate": str(day.date()),
                    "preTradeWeights": {str(k): round(float(v), 10) for k, v in pretrade.items()},
                    "targetWeights": {str(k): round(float(v), 10) for k, v in posttrade.items()},
                    "oneWayTurnover": one_way_turnover,
                    "grossTraded": gross_traded,
                    "costRate": cost_rate,
                    "equityBeforeCost": equity_before_cost,
                    "equityAfterCost": equity_value,
                })
                current_w = target.copy()
                last_mark_prices = pd.Series(
                    {symbol: float(px.at[day, symbol]) for symbol in target.index},
                    dtype=float,
                )
                last_mark_dates = {symbol: day for symbol in target.index}
                open_gaps = {symbol: gap for symbol, gap in open_gaps.items() if symbol in target.index}

        equity_values.append(equity_value)
        equity_dates.append(day)

    ser = pd.Series(equity_values, index=pd.DatetimeIndex(equity_dates), dtype=float)
    ser = ser[~ser.index.duplicated(keep="last")]
    if open_gaps:
        diagnostics["unresolvedPriceGaps"] = sorted(open_gaps.values(), key=lambda row: row["symbol"])
        first_unresolved = min(pd.Timestamp(gap["firstMissingDate"]) for gap in open_gaps.values())
        ser = ser.loc[ser.index < first_unresolved]
        diagnostics["evaluationTruncatedBefore"] = str(first_unresolved.date())
    average_turnover = turnover_sum / max(executed, 1)
    diagnostics["executedRebalances"] = executed
    diagnostics["averageOneWayTurnover"] = average_turnover
    if return_diagnostics:
        return ser, average_turnover, diagnostics
    return ser, average_turnover


def perf_metrics(eq):
    if len(eq) < 2:
        return {"total_x": None, "cagr_pct": None, "max_dd_pct": None, "sharpe": None}
    eq = eq.dropna().sort_index()
    if len(eq) < 2 or float(eq.iloc[0]) <= 0:
        return {"total_x": None, "cagr_pct": None, "max_dd_pct": None, "sharpe": None}
    r = eq.pct_change().dropna()
    yrs = max((eq.index[-1] - eq.index[0]).days / 365.25, 1e-9)
    total_x = float(eq.iloc[-1] / eq.iloc[0])
    cagr = total_x ** (1 / yrs) - 1
    dd = float((eq / eq.cummax() - 1).min())
    sharpe = float(r.mean() / r.std() * math.sqrt(252)) if r.std() > 0 else None
    return {"total_x": round(total_x, 2),
            "cagr_pct": round(cagr * 100, 1),
            "max_dd_pct": round(dd * 100, 1),
            "sharpe": round(sharpe, 2) if sharpe is not None else None}


def cycle_stats(eq, months_back=13):
    """周期位置: 基于策略净值的 MTD/1m/3m、距高点回撤、逐月收益、阶段判定."""
    if len(eq) < 2:
        return {"mtd_pct": None, "r1m_pct": None, "r3m_pct": None,
                "dd_from_peak_pct": None, "peak_date": None, "phase": "早期",
                "monthly_returns": []}
    end = eq.index[-1]

    def ret_since(t0, include_t0=False):
        # MTD 口径: 基准=t0(月初)前最后一个收盘, 即上月末; 滚动 1m/3m 口径: 基准=t0 当日或之前最后收盘
        cutoff = eq.index <= t0 if include_t0 else eq.index < t0
        prior, sub = eq.loc[cutoff], eq.loc[~cutoff]
        if len(prior):
            base = float(prior.iloc[-1])
        elif len(sub) > 1:
            base, sub = float(sub.iloc[0]), sub.iloc[1:]
        else:
            return None
        if not len(sub) or base <= 0:
            return None
        return round(float(sub.iloc[-1]) / base * 100 - 100, 1)

    mtd = ret_since(end.replace(day=1))
    r1m = ret_since(end - pd.DateOffset(months=1), include_t0=True)
    r3m = ret_since(end - pd.DateOffset(months=3), include_t0=True)
    peak = eq.cummax()
    dd = round(float(eq.iloc[-1] / peak.iloc[-1] - 1) * 100, 1)
    peak_date = str(eq.idxmax().date())
    if dd <= -20:   phase = "深回撤"
    elif dd <= -8:  phase = "回吐中"
    elif (r3m or 0) >= 30: phase = "成熟"
    else:           phase = "早期"
    mr = eq.resample(MONTH_END_FREQ).last().pct_change().dropna().tail(months_back)
    monthly = [[p.strftime("%Y-%m"), round(float(v)*100, 1)] for p, v in mr.items()]
    if monthly and end < mr.index[-1]:      # 数据未到月末 → 末根为进行中的部分月
        monthly[-1][0] += "*"
    return {"mtd_pct": mtd, "r1m_pct": r1m, "r3m_pct": r3m,
            "dd_from_peak_pct": dd, "peak_date": peak_date, "phase": phase,
            "monthly_returns": monthly}


def live_plan_status(plan, strategy_eq):
    """分批建仓进度: 已部署比例、下一批、入场以来策略表现(镜像参考)."""
    done = [t for t in plan["tranches"] if t.get("executed")]
    pending = [t for t in plan["tranches"] if not t.get("executed")]
    deployed = round(sum(t["fraction"] for t in done) * 100, 1)
    since = None
    t0 = pd.Timestamp(plan["started"], tz=strategy_eq.index.tz)  # 兼容 tz-aware 价格索引
    sub = strategy_eq.loc[strategy_eq.index >= t0]
    if len(sub) >= 1:
        since = round(float(sub.iloc[-1] / sub.iloc[0] - 1) * 100, 1)
    return {"account_label": plan.get("account_label", ""),
            "strategy_id": plan["strategy_id"], "started": plan["started"],
            "deployed_pct": deployed,
            "tranches": plan["tranches"],
            "next_tranche": min(pending, key=lambda t: t["target_month_end"]) if pending else None,
            "since_entry_pct": since,
            "note": plan.get("note", "")}


def weekly_points(eq):
    w = eq.groupby(eq.index.to_period("W")).last()
    return [[str(p.end_time.date()), round(float(v), 4)] for p, v in w.items()]


# ------------------------------------------------------------ artifact layer
def build_payload(cfg, px):
    px = normalize_price_frame(px)
    validation_errors = price_frame_errors(px, cfg)
    if validation_errors:
        raise ValueError("price frame rejected: " + "; ".join(validation_errors))
    bench = cfg["backtest"]["benchmarks"]
    end = px.index[-1]
    start = end - pd.DateOffset(years=cfg["backtest"]["years"])
    cost = cfg["backtest"]["cost_per_side"]
    coverage = cfg["universe"].get("min_history_coverage", 0.95)
    fresh_columns = [col for col in px.columns
                     if px[col].last_valid_index() is not None and px[col].last_valid_index() == end]
    stale_columns = sorted(set(px.columns) - set(fresh_columns))
    data_age_weekdays = weekday_age(end)
    current_price_status = (
        "PASS" if 0 <= data_age_weekdays <= 2 else "BLOCK_STALE_OR_FUTURE"
    )

    payload = {
        "schemaVersion": 2,
        "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "window": {"start": str(start.date()), "end": str(end.date()),
                   "years": cfg["backtest"]["years"]},
        "universe_size": int(px.shape[1] - len(bench)),
        "universe_source": px.attrs.get("universeSource", "provided_prices"),
        "universe_warning": px.attrs.get("universeWarning"),
        "price_freshness": {"as_of": str(end.date()),
                            "fresh_count": len(fresh_columns),
                            "stale_symbols": stale_columns,
                            "age_weekdays": data_age_weekdays,
                            "status": current_price_status},
        "cost_per_side": cost,
        "researchOnly": True,
        "decisionGrade": False,
        "decisionGradeReason": (
            "Current-universe survivorship and in-sample selection from the same "
            "40-variant sweep prevent decision-grade performance claims."
        ),
        "methodology": {
            "signalTiming": "month-end close",
            "fillTiming": "next trading session close",
            "fillDayExposure": "prior weights earn the signal-to-fill close return",
            "turnoverTiming": "after fill-day return, from drifted pre-trade weights to target weights",
            "costModel": "gross traded notional multiplied by configured per-side cost at fill close",
            "missingHeldClosePolicy": "carry the last mark, then book the cumulative return when a valid close resumes",
            "missingRebalanceClosePolicy": "skip the rebalance when any held or target close is invalid",
            "unresolvedHeldClosePolicy": "truncate performance before the first unresolved held-price gap",
            "initialCapitalAnchor": True,
            "initialization": "cash from the anchor until the first post-anchor signal fills",
            "adjustedPrices": True,
            "currentUniverseSurvivorship": True,
            "inSampleStrategySelection": True,
            "strategySelection": "three variants selected from a 40-variant sweep using overlapping history",
            "outOfSampleValidation": False,
        },
        "disclaimer": (
            "Research only. Current-universe survivorship and in-sample strategy "
            "selection can materially overstate both performance and ranking stability."
        ),
        "strategies": [], "benchmarks": {},
    }
    for b in bench:
        eq = px[b].loc[px.index >= start].dropna()
        eq = eq / eq.iloc[0]
        payload["benchmarks"][b] = {"metrics": perf_metrics(eq),
                                    "equity_weekly": weekly_points(eq),
                                    "cycle": cycle_stats(eq)}
    eq_by_id = {}
    for s in cfg["strategies"]:
        eq, turn, execution = run_backtest(
            px, s["lookback_months"], s["top_n"], start, end, cost,
            coverage, exclude=bench, return_diagnostics=True,
        )
        signal_px = px[fresh_columns]
        latest_scores = momentum_scores(
            signal_px, s["lookback_months"], coverage=coverage, exclude=bench
        )
        latest_top = latest_scores.sort_values(ascending=False, kind="mergesort").head(s["top_n"])
        latest_weight_pct = round(100 / len(latest_top), 1) if len(latest_top) else None
        completed_signal_date = latest_completed_month_signal_date(px)
        if completed_signal_date is not None:
            completed_scores = momentum_scores(
                px.loc[:completed_signal_date], s["lookback_months"],
                coverage=coverage, exclude=bench,
            )
            active_top = completed_scores.sort_values(
                ascending=False, kind="mergesort"
            ).head(s["top_n"])
            future_dates = px.index[px.index > completed_signal_date]
            effective_from = future_dates[0] if len(future_dates) else None
        else:
            active_top = pd.Series(dtype=float)
            effective_from = None
        active_weight_pct = round(100 / len(active_top), 1) if len(active_top) else None
        active_stale = sorted(set(active_top.index) - set(fresh_columns))
        eq_by_id[s["id"]] = eq
        payload["strategies"].append({
            **{k: s[k] for k in ("id", "label", "lookback_months", "top_n",
                                 "weighting", "role", "thesis")},
            "metrics": {**perf_metrics(eq),
                        "monthly_turnover": round(float(turn), 2)},
            "equity_weekly": weekly_points(eq),
            "cycle": cycle_stats(eq),
            "execution": {
                "anchor_date": execution["anchorDate"],
                "executed_rebalances": execution["executedRebalances"],
                "skipped_rebalances": len(execution["skippedFills"]),
                "first_fill_date": execution["fills"][0]["fillDate"] if execution["fills"] else None,
                "last_fill_date": execution["fills"][-1]["fillDate"] if execution["fills"] else None,
            },
            "current_signal": {
                "as_of": str(completed_signal_date.date()) if completed_signal_date is not None else None,
                "effective_from": str(effective_from.date()) if effective_from is not None else None,
                "timing": "last completed month-end close; target becomes active at next session close",
                "status": (
                    "BLOCK_NO_COMPLETED_SIGNAL" if active_top.empty
                    else ("BLOCK_PRICE_STALE" if active_stale else "PASS")
                ),
                "stale_target_symbols": active_stale,
                "holdings": [{"ticker": t, "lookback_return_pct": round(v * 100, 1),
                              "weight_pct": active_weight_pct}
                             for t, v in active_top.items()],
            },
            "latest_rank_snapshot": {
                "as_of": str(end.date()),
                "actionable": False,
                "reason": "Intramonth research ranking; the strategy forms targets only from a completed month-end close and fills next session close.",
                "holdings": [{"ticker": t, "lookback_return_pct": round(v * 100, 1),
                              "weight_pct": latest_weight_pct}
                             for t, v in latest_top.items()],
            },
        })
    lp = cfg.get("live_plan")
    if lp and lp.get("strategy_id") in eq_by_id:
        payload["live_plan"] = live_plan_status(lp, eq_by_id[lp["strategy_id"]])
    return payload


def render_html(payload):
    # The payload lives in executable JavaScript, so escape every character
    # that can terminate the surrounding script or create HTML markup before
    # JavaScript gets a chance to render it.
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    data = (data.replace("&", "\\u0026")
                .replace("<", "\\u003c")
                .replace(">", "\\u003e")
                .replace("\u2028", "\\u2028")
                .replace("\u2029", "\\u2029"))
    return """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>动量 Top-3 策略舱 · Momentum Sleeve</title><style>
:root{--bg:#0b0f14;--card:#121821;--ink:#e6edf3;--mut:#8b98a5;--line:#1f2937;
--g:#3fb950;--r:#f85149;--a1:#58a6ff;--a2:#d29922;--a3:#bc8cff;--b1:#484f58;--b2:#6e7681}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:14px/1.5 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif;padding:18px}
h1{font-size:20px;margin:0 0 2px}.sub{color:var(--mut);font-size:12px;margin-bottom:14px}
.warn{background:#2d1a06;border:1px solid #6a4a12;color:#e3b341;border-radius:8px;
padding:8px 12px;font-size:12px;margin-bottom:16px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:12px;margin-bottom:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px}
.card h2{font-size:15px;margin:0 0 2px}.role{font-size:11px;color:var(--mut);margin-bottom:8px}
.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:6px;margin:10px 0}
.kpi{background:#0d1420;border-radius:6px;padding:6px 8px;text-align:center}
.kpi b{display:block;font-size:16px}.kpi span{font-size:10px;color:var(--mut)}
.pos b{color:var(--g)}.neg b{color:var(--r)}
table{width:100%;border-collapse:collapse;font-size:12px}
th,td{padding:4px 6px;text-align:right;border-bottom:1px solid var(--line)}
th:first-child,td:first-child{text-align:left}th{color:var(--mut);font-weight:500}
.thesis{font-size:11px;color:var(--mut);margin-top:8px;border-top:1px dashed var(--line);padding-top:8px}
#chartcard{margin-bottom:16px}#legend{display:flex;flex-wrap:wrap;gap:12px;font-size:12px;margin-bottom:6px}
#legend span{display:inline-flex;align-items:center;gap:5px}
#legend i{width:14px;height:3px;border-radius:2px;display:inline-block}
svg text{fill:var(--mut);font-size:10px}.hold{font-weight:600}
.cmp th,.cmp td{font-variant-numeric:tabular-nums}
.cycle{font-size:12px;margin:8px 0;color:var(--ink)}
.phase{border:1px solid;border-radius:10px;padding:1px 8px;font-size:11px;margin-right:6px}
.bars{display:flex;gap:3px;align-items:flex-start;height:72px;margin:6px 0}
.bar{flex:1;text-align:center}.bar i{display:block;width:100%;border-radius:2px}
.bar s{text-decoration:none;font-size:9px;color:var(--mut)}
#plancard{margin-bottom:16px}
.progress{height:10px;background:#0d1420;border-radius:5px;overflow:hidden;margin:8px 0}
.progress i{display:block;height:100%;background:var(--a1);border-radius:5px}
.next td{background:#0d1f33}
.mirror{font-size:11px;color:var(--a2);margin-top:6px}
</style></head><body>
<h1>动量 Top-3 策略舱</h1>
<div class="sub" id="sub"></div>
<div class="warn">⚠️ <b>仅研究 / Decision Grade: NO</b>。宇宙使用今日指数成分，且三组参数来自同一段历史上的 40 变体筛选；绝对收益和策略排名都不是样本外证据。</div>
<div class="warn" id="datawarn"></div>
<div class="card" id="chartcard"><h2>8年净值曲线(对数轴)</h2><div id="legend"></div><div id="chart"></div></div>
<div id="planmount"></div>
<div class="grid" id="cards"></div>
<div class="card"><h2>策略对比总表</h2><table class="cmp" id="cmp"></table></div>
<script>
const D=__DATA__;
const C={RET_11M_top3:'var(--a1)',RET_9M_top3:'var(--a2)',RET_5M_top5:'var(--a3)',SPY:'var(--b1)',QQQ:'var(--b2)'};
function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
document.getElementById('sub').textContent=`窗口 ${D.window.start} → ${D.window.end} · 宇宙 ${D.universe_size} 只 (SPX∪NDX∪DJIA) · 月末收盘信号/下一交易日收盘成交/成交日沿用旧权重 · 单边成本 ${(D.cost_per_side*1e4).toFixed(0)}bp · 生成于 ${D.generated_at}`;
document.getElementById('datawarn').textContent=`价格数据 ${D.price_freshness.status} · as of ${D.price_freshness.as_of} · 工作日龄 ${D.price_freshness.age_weekdays} · stale symbols ${D.price_freshness.stale_symbols.length}`;
function fmt(x){return x==null?'—':x}
function series(){const out=[];for(const s of D.strategies)out.push({id:s.id,pts:s.equity_weekly});
for(const[b,v]of Object.entries(D.benchmarks))out.push({id:b,pts:v.equity_weekly});return out}
(function chart(){const S=series(),W=Math.min(document.body.clientWidth-60,1100),H=340,P={l:46,r:8,t:8,b:22};
let ymin=1e9,ymax=-1e9,xmin=null,xmax=null;
for(const s of S)for(const[d,v]of s.pts){ymin=Math.min(ymin,v);ymax=Math.max(ymax,v);
if(!xmin||d<xmin)xmin=d;if(!xmax||d>xmax)xmax=d}
const x0=new Date(xmin).getTime(),x1=new Date(xmax).getTime();
const ly0=Math.log10(ymin),ly1=Math.log10(ymax);
const X=d=>P.l+(new Date(d).getTime()-x0)/(x1-x0)*(W-P.l-P.r);
const Y=v=>P.t+(1-(Math.log10(v)-ly0)/(ly1-ly0))*(H-P.t-P.b);
let g='';const ticks=[0.5,1,2,5,10,20,50,100,200,400];
for(const t of ticks){if(t<ymin||t>ymax)continue;
g+=`<line x1="${P.l}" x2="${W-P.r}" y1="${Y(t)}" y2="${Y(t)}" stroke="#1f2937" stroke-width="1"/><text x="4" y="${Y(t)+3}">${t}x</text>`}
for(let yr=new Date(xmin).getFullYear()+1;yr<=new Date(xmax).getFullYear();yr++){
const xx=X(yr+'-01-01');g+=`<line x1="${xx}" x2="${xx}" y1="${P.t}" y2="${H-P.b}" stroke="#141c26"/><text x="${xx-12}" y="${H-6}">${yr}</text>`}
let paths='';for(const s of S){const p=s.pts.map((q,i)=>(i?'L':'M')+X(q[0]).toFixed(1)+','+Y(q[1]).toFixed(1)).join('');
paths+=`<path d="${p}" fill="none" stroke="${C[s.id]||'#999'}" stroke-width="${s.id in D.benchmarks?1.2:2}" opacity="${s.id in D.benchmarks?0.75:1}"/>`}
document.getElementById('chart').innerHTML=`<svg width="${W}" height="${H}">${g}${paths}</svg>`;
document.getElementById('legend').innerHTML=S.map(s=>{const lab=(D.strategies.find(t=>t.id===s.id)||{}).label||s.id+' (基准)';
return `<span><i style="background:${C[s.id]||'var(--mut)'}"></i>${esc(lab)} · ${s.pts[s.pts.length-1][1].toFixed(1)}x</span>`}).join('')})();
document.getElementById('cards').innerHTML=D.strategies.map(s=>{const m=s.metrics;
const cy=s.cycle;let cyc='';
if(cy){const phC={'早期':'var(--g)','成熟':'var(--a2)','回吐中':'var(--r)','深回撤':'var(--r)'}[cy.phase]||'var(--mut)';
cyc=`<div class="cycle"><span class="phase" style="border-color:${phC};color:${phC}">${esc(cy.phase)}</span>
 MTD ${fmt(cy.mtd_pct)}% · 1m ${fmt(cy.r1m_pct)}% · 3m ${fmt(cy.r3m_pct)}% · 距高点 <b style="color:var(--r)">${cy.dd_from_peak_pct}%</b> (峰值 ${esc(cy.peak_date)})</div>
<div class="bars">${cy.monthly_returns.map(([mo,v])=>{const h=Math.min(Math.abs(v),60)*0.5;
return `<div class="bar" title="${esc(mo)}: ${v}%"><i style="height:${h}px;background:${v>=0?'var(--g)':'var(--r)'};margin-top:${v>=0?30-h:30}px"></i><s>${esc(String(mo).slice(5))}</s></div>`}).join('')}</div>`}
return `<div class="card"><h2 style="color:${C[s.id]||'var(--mut)'}">${esc(s.label)}</h2>
<div class="role">${esc(s.id)} · 回看 ${s.lookback_months} 月 · Top${s.top_n} 等权 · 月调仓 · ${esc(s.role)}</div>
<div class="kpis">
<div class="kpi pos"><b>${fmt(m.total_x)}x</b><span>8年总倍数</span></div>
<div class="kpi pos"><b>${fmt(m.cagr_pct)}%</b><span>CAGR</span></div>
<div class="kpi neg"><b>${fmt(m.max_dd_pct)}%</b><span>最大回撤</span></div>
<div class="kpi"><b>${fmt(m.sharpe)}</b><span>Sharpe</span></div></div>
${cyc}
	<table><tr><th>已完成月末目标 (${esc(s.current_signal.as_of)})</th><th>回看收益</th><th>权重</th></tr>
	${s.current_signal.holdings.map(h=>`<tr><td class="hold">${esc(h.ticker)}</td><td>${h.lookback_return_pct>=0?'+':''}${h.lookback_return_pct}%</td><td>${h.weight_pct}%</td></tr>`).join('')}</table>
	<div class="role">生效: ${esc(s.current_signal.effective_from||'—')} · 数据门禁: ${esc(s.current_signal.status)}</div>
<div class="thesis">${esc(s.thesis)}</div></div>`}).join('');
(function plan(){const p=D.live_plan;if(!p)return;
const nx=p.next_tranche;
document.getElementById('planmount').innerHTML=`<div class="card" id="plancard">
<h2>分批建仓进度 · ${esc(p.account_label)} (${esc(p.strategy_id)})</h2>
<div class="role">开始 ${esc(p.started)} · ${esc(p.note)}</div>
<div class="progress"><i style="width:${p.deployed_pct}%"></i></div>
<div style="font-size:12px;margin-bottom:8px">已部署 <b>${p.deployed_pct}%</b>${nx?` · 下一批: 第${nx.seq}批 目标 <b style="color:var(--a1)">${esc(nx.target_month_end)}</b> (${(nx.fraction*100).toFixed(1)}%)`:' · 全部完成'}</div>
<table><tr><th>批次</th><th>目标月末</th><th>份额</th><th>状态</th><th>实际日期</th></tr>
${p.tranches.map(t=>`<tr class="${nx&&t.seq===nx.seq?'next':''}"><td>第${t.seq}批</td><td>${esc(t.target_month_end)}</td><td>${(t.fraction*100).toFixed(1)}%</td><td>${t.executed?'✅ 已执行':'⏳ 待执行'}</td><td>${esc(t.date||'—')}</td></tr>`).join('')}</table>
<div class="mirror">入场以来策略表现: <b>${fmt(p.since_entry_pct)}%</b>(策略净值镜像,非账户实际;含滑点/时点差)</div></div>`})();
(function cmp(){const rows=[...D.strategies.map(s=>({n:s.label,m:{...s.metrics}})),
...Object.entries(D.benchmarks).map(([b,v])=>({n:b+'(基准)',m:{...v.metrics,monthly_turnover:0}}))];
document.getElementById('cmp').innerHTML='<tr><th>策略</th><th>总倍数</th><th>CAGR</th><th>最大回撤</th><th>Sharpe</th><th>月换手</th></tr>'+
rows.map(r=>`<tr><td>${esc(r.n)}</td><td>${fmt(r.m.total_x)}x</td><td>${fmt(r.m.cagr_pct)}%</td><td>${fmt(r.m.max_dd_pct)}%</td><td>${fmt(r.m.sharpe)}</td><td>${(r.m.monthly_turnover*100).toFixed(0)}%</td></tr>`).join('')})();
</script></body></html>""".replace("__DATA__", data)


def main():
    ap = argparse.ArgumentParser(description="Momentum Top-3 sleeve artifact")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT))
    ap.add_argument("--no-fetch", action="store_true",
                    help="reuse cached prices (offline)")
    args = ap.parse_args()

    cfg = json.loads(Path(args.config).read_text())
    px = load_prices(cfg, no_fetch=args.no_fetch)
    payload = build_payload(cfg, px)

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_write_json(out / "momentum_top3.json", payload, indent=1)
    atomic_write_text(out / "momentum_top3.html", render_html(payload))
    for s in payload["strategies"]:
        h = ", ".join(x["ticker"] for x in s["current_signal"]["holdings"])
        m = s["metrics"]
        print(f"{s['id']:14s} {m['total_x']:>7}x  CAGR {m['cagr_pct']:>5}%  "
              f"DD {m['max_dd_pct']:>6}%  Sharpe {m['sharpe']}  -> {h}")
    print(f"wrote {out/'momentum_top3.json'} and .html")


if __name__ == "__main__":
    main()

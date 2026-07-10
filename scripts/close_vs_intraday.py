#!/usr/bin/env python3
"""Compare portfolio closes with opens and intraday reference prices.

The producer is deliberately separate from ``generate.py``.  It reads the
current held-stock universe and market-value weights from the embedded
dashboard payload, downloads daily and regular-session 60-minute bars, and
writes a small optional research artifact for 1M/2M/3M windows.

The 60-minute VWAP is an approximation: each bar contributes
``((high + low + close) / 3) * volume``.  Portfolio history is also a
*current-book lens*: current dashboard weights are re-normalized among symbols
with data on each session.  It is not a reconstruction of historical holdings.

Examples::

    python3 scripts/close_vs_intraday.py
    python3 scripts/close_vs_intraday.py --as-of 2026-07-09
    python3 scripts/close_vs_intraday.py --no-fetch

``--no-fetch`` requires the raw-bar cache and recomputes the report from that
cache.  Live runs merge fresh bars into the cache atomically, so a partial
Yahoo response cannot erase previously cached history.
"""

from __future__ import annotations

import argparse
import calendar
import datetime as dt
import json
import math
import re
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

try:
    from scripts.artifact_io import atomic_write_json
    from scripts.dashboard_payload import read_dashboard_payload as _read_dashboard_payload
except ModuleNotFoundError:
    from artifact_io import atomic_write_json
    from dashboard_payload import read_dashboard_payload as _read_dashboard_payload

import pandas as pd

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python 3.8 fallback
    ZoneInfo = None


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DASHBOARD = ROOT / "output" / "portfolio_dashboard.html"
DEFAULT_OUT = ROOT / "output" / "close_vs_intraday.json"
DEFAULT_CACHE = ROOT / "output" / "close_vs_intraday_cache.json"
ET = ZoneInfo("America/New_York") if ZoneInfo else dt.timezone(dt.timedelta(hours=-5))

WINDOW_MONTHS = (("1M", 1), ("2M", 2), ("3M", 3))
MIN_COVERAGE_PCT = 80.0
MAX_DATA_LAG_WEEKDAYS = 2
_SYMBOL_RE = re.compile(r"^[A-Z0-9^][A-Z0-9.^=/\-]{0,19}$")

DAILY_BOOLEAN_METRICS = {
    "closeBelowOpen": "closeBelowOpenPct",
    "closeBelowRangeMidpoint": "closeBelowRangeMidpointPct",
}
DAILY_AVERAGE_METRICS = {
    "closeLocationPct": "avgCloseLocationPct",
    "openToClosePct": "avgOpenToClosePct",
    "highToClosePct": "avgHighToClosePct",
}
INTRADAY_BOOLEAN_METRICS = {
    "closeBelowVwap": "closeBelowVwapPct",
    "closeBelowMidSession": "closeBelowMidSessionPct",
    "closingBarRed": "closingBarRedPct",
    "peakBefore14": "peakBefore14Pct",
}
INTRADAY_AVERAGE_METRICS = {
    "closeVsVwapPct": "avgCloseVsVwapPct",
    "closeVsMidSessionPct": "avgCloseVsMidSessionPct",
    "closingBarReturnPct": "avgClosingBarReturnPct",
}


def _finite_number(value: Any) -> float | None:
    """Return a finite Python float, otherwise ``None``."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _round(value: Any, digits: int = 4) -> float | None:
    number = _finite_number(value)
    return round(number, digits) if number is not None else None


def sanitize_json(value: Any) -> Any:
    """Recursively convert pandas/numpy values and reject non-finite numbers."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, (dt.date, dt.datetime, pd.Timestamp)):
        return value.isoformat()
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    # numpy integer/float scalars expose item(), but importing numpy solely for
    # isinstance checks is unnecessary.
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return sanitize_json(item())
        except (TypeError, ValueError):
            pass
    if isinstance(value, Mapping):
        return {str(key): sanitize_json(item_value) for key, item_value in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [sanitize_json(item_value) for item_value in value]
    return str(value)


def atomic_json(path: str | Path, payload: Any) -> None:
    """Write sanitized strict JSON atomically with private permissions."""
    atomic_write_json(path, sanitize_json(payload))


def read_dashboard(path: str | Path) -> dict[str, Any]:
    return _read_dashboard_payload(path)


def subtract_months(day: dt.date, months: int) -> dt.date:
    """Calendar-month subtraction with end-of-month clamping."""
    absolute_month = day.year * 12 + day.month - 1 - months
    year, month_zero = divmod(absolute_month, 12)
    month = month_zero + 1
    last_day = calendar.monthrange(year, month)[1]
    return dt.date(year, month, min(day.day, last_day))


def dashboard_context(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Extract current held symbols and normalized current market-value weights."""
    held = []
    for row in payload.get("stocks") or []:
        if not isinstance(row, Mapping) or not row.get("held") or not row.get("sym"):
            continue
        symbol = str(row["sym"]).strip().upper()
        if not _SYMBOL_RE.fullmatch(symbol) or ".." in symbol or "//" in symbol:
            raise ValueError(f"dashboard contains invalid held symbol: {symbol!r}")
        value = _finite_number(row.get("value"))
        if value is None or value <= 0:
            raise ValueError(f"dashboard held value must be positive for {symbol}")
        held.append({"symbol": symbol, "value": value})
    # Stable de-duplication protects against malformed dashboards without
    # silently double-weighting a name.
    by_symbol: dict[str, float] = {}
    for row in held:
        by_symbol[row["symbol"]] = by_symbol.get(row["symbol"], 0.0) + row["value"]
    if not by_symbol:
        raise ValueError("dashboard has no held stocks")
    total = sum(by_symbol.values())
    if total > 0:
        weights = {symbol: value / total for symbol, value in by_symbol.items()}
    else:
        weights = {symbol: 1.0 / len(by_symbol) for symbol in by_symbol}
    summary = payload.get("summary") or {}
    return {
        "symbols": sorted(by_symbol),
        "values": by_symbol,
        "weights": weights,
        "marketValue": total,
        "dashboardPriceAsOf": summary.get("priceAsOf"),
        "dashboardGeneratedAt": summary.get("generatedAt"),
    }


def _default_downloader(
    symbols: list[str], *, start: str, end: str, interval: str
) -> pd.DataFrame:
    import warnings

    warnings.filterwarnings("ignore")
    import yfinance as yf

    cache_dir = ROOT / "output" / "yfinance_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    setter = getattr(yf, "set_tz_cache_location", None)
    if setter:
        setter(str(cache_dir))
    return yf.download(
        symbols,
        start=start,
        end=end,
        interval=interval,
        auto_adjust=True,
        group_by="ticker",
        prepost=False,
        progress=False,
        threads=True,
    )


Downloader = Callable[..., Any]


def _symbol_frame(raw: Any, symbol: str, symbol_count: int) -> pd.DataFrame | None:
    """Extract one symbol from yfinance output or an injected frame mapping."""
    if isinstance(raw, Mapping):
        frame = raw.get(symbol)
        return frame.copy() if isinstance(frame, pd.DataFrame) else None
    if not isinstance(raw, pd.DataFrame) or raw.empty:
        return None
    if isinstance(raw.columns, pd.MultiIndex):
        level0 = set(map(str, raw.columns.get_level_values(0)))
        level1 = set(map(str, raw.columns.get_level_values(1)))
        if symbol in level0:
            frame = raw[symbol]
        elif symbol in level1:
            frame = raw.xs(symbol, axis=1, level=1)
        else:
            return None
        return frame.copy()
    return raw.copy() if symbol_count == 1 else None


def _column_map(frame: pd.DataFrame) -> dict[str, Any]:
    return {str(column).strip().lower(): column for column in frame.columns}


def _normalize_daily(raw: Any, symbols: Iterable[str]) -> dict[str, list[dict[str, Any]]]:
    symbols = list(symbols)
    output: dict[str, list[dict[str, Any]]] = {}
    for symbol in symbols:
        frame = _symbol_frame(raw, symbol, len(symbols))
        if frame is None or frame.empty:
            continue
        columns = _column_map(frame)
        if not all(key in columns for key in ("open", "high", "low", "close")):
            continue
        rows = []
        for index, row in frame.iterrows():
            values = {key: _finite_number(row[columns[key]]) for key in ("open", "high", "low", "close")}
            if any(values[key] is None for key in values):
                continue
            timestamp = pd.Timestamp(index)
            rows.append(
                {
                    "date": timestamp.date().isoformat(),
                    **{key: round(float(value), 8) for key, value in values.items()},
                    "volume": _round(row[columns["volume"]], 4) if "volume" in columns else None,
                }
            )
        if rows:
            output[symbol] = _dedupe_rows(rows, "date")
    return output


def _as_eastern(timestamp: Any) -> pd.Timestamp:
    value = pd.Timestamp(timestamp)
    if value.tzinfo is None:
        return value.tz_localize(ET)
    return value.tz_convert(ET)


def _regular_session(timestamp: pd.Timestamp) -> bool:
    wall = timestamp.time().replace(tzinfo=None)
    return dt.time(9, 30) <= wall < dt.time(16, 0)


def _normalize_intraday(raw: Any, symbols: Iterable[str]) -> dict[str, list[dict[str, Any]]]:
    symbols = list(symbols)
    output: dict[str, list[dict[str, Any]]] = {}
    for symbol in symbols:
        frame = _symbol_frame(raw, symbol, len(symbols))
        if frame is None or frame.empty:
            continue
        columns = _column_map(frame)
        if not all(key in columns for key in ("open", "high", "low", "close")):
            continue
        rows = []
        for index, row in frame.iterrows():
            timestamp = _as_eastern(index)
            if not _regular_session(timestamp):
                continue
            values = {key: _finite_number(row[columns[key]]) for key in ("open", "high", "low", "close")}
            if any(values[key] is None for key in values):
                continue
            rows.append(
                {
                    "timestamp": timestamp.isoformat(),
                    "date": timestamp.date().isoformat(),
                    **{key: round(float(value), 8) for key, value in values.items()},
                    "volume": max(_finite_number(row[columns["volume"]]) or 0.0, 0.0)
                    if "volume" in columns
                    else 0.0,
                }
            )
        if rows:
            output[symbol] = _dedupe_rows(rows, "timestamp")
    return output


def _dedupe_rows(rows: Iterable[Mapping[str, Any]], key: str) -> list[dict[str, Any]]:
    by_key = {str(row[key]): dict(row) for row in rows if row.get(key)}
    return [by_key[item_key] for item_key in sorted(by_key)]


def fetch_market_data(
    symbols: Iterable[str],
    start: dt.date,
    end: dt.date,
    downloader: Downloader | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Fetch and normalize daily/60-minute data.

    ``downloader`` is injectable and receives ``symbols``, ``start``, ``end``
    and ``interval``.  Daily and intraday failures are isolated so one useful
    half of a Yahoo response can still be cached and reported.
    """
    symbols = sorted(set(symbols))
    download = downloader or _default_downloader
    result: dict[str, Any] = {"daily": {}, "intraday60m": {}}
    errors = []
    for interval, key, normalizer in (
        ("1d", "daily", _normalize_daily),
        ("60m", "intraday60m", _normalize_intraday),
    ):
        try:
            raw = download(symbols, start=start.isoformat(), end=end.isoformat(), interval=interval)
            result[key] = normalizer(raw, symbols)
        except Exception as exc:  # network/provider failures fall back to cache in run()
            errors.append(f"{interval}: {type(exc).__name__}: {exc}")
    return result, errors


def _merge_series(
    old: Mapping[str, Any] | None,
    fresh: Mapping[str, Any],
    row_key: str,
) -> dict[str, list[dict[str, Any]]]:
    merged: dict[str, list[dict[str, Any]]] = {}
    symbols = set((old or {}).keys()) | set(fresh.keys())
    for symbol in symbols:
        rows = list((old or {}).get(symbol) or []) + list(fresh.get(symbol) or [])
        merged[symbol] = _dedupe_rows(rows, row_key)
    return merged


def merge_cache(old: Mapping[str, Any] | None, fresh: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "daily": _merge_series((old or {}).get("daily"), fresh.get("daily") or {}, "date"),
        "intraday60m": _merge_series(
            (old or {}).get("intraday60m"), fresh.get("intraday60m") or {}, "timestamp"
        ),
    }


def load_cache(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    if not source.exists():
        raise FileNotFoundError(f"raw-bar cache not found: {source}")
    doc = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or not isinstance(doc.get("daily"), dict) or not isinstance(
        doc.get("intraday60m"), dict
    ):
        raise ValueError(f"invalid raw-bar cache: {source}")
    return doc


def daily_observations(
    rows: Iterable[Mapping[str, Any]], cutoff: dt.date, as_of: dt.date
) -> list[dict[str, Any]]:
    output = []
    for row in rows:
        try:
            day = dt.date.fromisoformat(str(row.get("date")))
        except (TypeError, ValueError):
            continue
        if day < cutoff or day > as_of:
            continue
        open_price = _finite_number(row.get("open"))
        high = _finite_number(row.get("high"))
        low = _finite_number(row.get("low"))
        close = _finite_number(row.get("close"))
        if None in (open_price, high, low, close):
            continue
        range_size = high - low
        output.append(
            {
                "date": day.isoformat(),
                "closeBelowOpen": close < open_price,
                "closeBelowRangeMidpoint": close < (high + low) / 2.0,
                "closeLocationPct": ((close - low) / range_size * 100.0) if range_size > 0 else None,
                "openToClosePct": ((close / open_price - 1.0) * 100.0) if open_price else None,
                "highToClosePct": ((close / high - 1.0) * 100.0) if high else None,
            }
        )
    return output


def intraday_observations(
    rows: Iterable[Mapping[str, Any]], cutoff: dt.date, as_of: dt.date
) -> list[dict[str, Any]]:
    by_day: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        try:
            timestamp = _as_eastern(row.get("timestamp"))
        except (TypeError, ValueError):
            continue
        day = timestamp.date()
        if day < cutoff or day > as_of or not _regular_session(timestamp):
            continue
        normalized = dict(row)
        normalized["_timestamp"] = timestamp
        by_day.setdefault(day.isoformat(), []).append(normalized)

    output = []
    for day, day_rows in sorted(by_day.items()):
        day_rows.sort(key=lambda item: item["_timestamp"])
        # Four bars retains scheduled US half-days but drops very early partial
        # sessions from an accidental intraday invocation.
        if len(day_rows) < 4:
            continue
        valid = []
        for row in day_rows:
            values = {key: _finite_number(row.get(key)) for key in ("open", "high", "low", "close")}
            if any(values[key] is None for key in values):
                continue
            values["volume"] = max(_finite_number(row.get("volume")) or 0.0, 0.0)
            values["timestamp"] = row["_timestamp"]
            valid.append(values)
        if len(valid) < 4:
            continue
        close = valid[-1]["close"]
        volume_total = sum(row["volume"] for row in valid)
        vwap = None
        if volume_total > 0:
            vwap = sum(
                ((row["high"] + row["low"] + row["close"]) / 3.0) * row["volume"]
                for row in valid
            ) / volume_total
        middle = valid[(len(valid) - 1) // 2]["close"]
        last = valid[-1]
        peak = max(valid, key=lambda item: (item["high"], -item["timestamp"].value))
        output.append(
            {
                "date": day,
                "barCount": len(valid),
                "closeBelowVwap": (close < vwap) if vwap else None,
                "closeVsVwapPct": ((close / vwap - 1.0) * 100.0) if vwap else None,
                "closeBelowMidSession": close < middle,
                "closeVsMidSessionPct": ((close / middle - 1.0) * 100.0) if middle else None,
                "closingBarRed": last["close"] < last["open"],
                "closingBarReturnPct": ((last["close"] / last["open"] - 1.0) * 100.0)
                if last["open"]
                else None,
                "peakBefore14": peak["timestamp"].time().replace(tzinfo=None) < dt.time(14, 0),
            }
        )
    return output


def aggregate_weighted(
    observations: Mapping[str, list[Mapping[str, Any]]],
    weights: Mapping[str, float],
    boolean_metrics: Mapping[str, str],
    average_metrics: Mapping[str, str],
) -> dict[str, Any]:
    """Aggregate symbol-day observations with per-session re-normalization."""
    by_symbol_date = {
        symbol: {str(row["date"]): row for row in rows if row.get("date")}
        for symbol, rows in observations.items()
    }
    dates = sorted({day for rows in by_symbol_date.values() for day in rows})
    positive_weights = {symbol: max(float(weight), 0.0) for symbol, weight in weights.items()}
    total_weight = sum(positive_weights.values())
    if total_weight <= 0 and positive_weights:
        positive_weights = {symbol: 1.0 for symbol in positive_weights}
        total_weight = len(positive_weights)

    result: dict[str, Any] = {
        "sessions": len(dates),
        "symbolDays": sum(len(rows) for rows in by_symbol_date.values()),
        "firstDate": dates[0] if dates else None,
        "latestDate": dates[-1] if dates else None,
        "averageCoveragePct": None,
        "metricSessions": {},
    }
    coverage = []
    metrics = {**boolean_metrics, **average_metrics}
    daily_values: dict[str, list[float]] = {metric: [] for metric in metrics}
    for day in dates:
        available_symbols = [symbol for symbol in positive_weights if day in by_symbol_date.get(symbol, {})]
        if total_weight > 0:
            coverage.append(sum(positive_weights[symbol] for symbol in available_symbols) / total_weight * 100.0)
        for metric in metrics:
            available = []
            for symbol, weight in positive_weights.items():
                row = by_symbol_date.get(symbol, {}).get(day)
                if row is None or row.get(metric) is None:
                    continue
                raw_value = row[metric]
                value = float(bool(raw_value)) if metric in boolean_metrics else _finite_number(raw_value)
                if value is not None:
                    available.append((weight, value))
            denominator = sum(weight for weight, _ in available)
            if denominator > 0:
                daily_values[metric].append(sum(weight * value for weight, value in available) / denominator)

    result["averageCoveragePct"] = _round(sum(coverage) / len(coverage), 2) if coverage else None
    for metric, output_name in metrics.items():
        values = daily_values[metric]
        result[output_name] = _round(sum(values) / len(values) * (100.0 if metric in boolean_metrics else 1.0), 3) if values else None
        result["metricSessions"][output_name] = len(values)
    # Distinguish two related questions.  closeBelowVwapPct is the average
    # current-weighted *share of names* below VWAP.  This composite metric first
    # forms one current-weighted close-vs-VWAP return for each session, then asks
    # on what fraction of sessions that whole-book composite was below zero.
    if "closeVsVwapPct" in daily_values:
        composite = daily_values["closeVsVwapPct"]
        result["compositeCloseBelowVwapPct"] = (
            _round(sum(value < 0 for value in composite) / len(composite) * 100.0, 3)
            if composite
            else None
        )
        result["metricSessions"]["compositeCloseBelowVwapPct"] = len(composite)
    return result


def _window_summary(
    daily_by_symbol: Mapping[str, list[Mapping[str, Any]]],
    intraday_by_symbol: Mapping[str, list[Mapping[str, Any]]],
    weights: Mapping[str, float],
    cutoff: dt.date,
    as_of: dt.date,
) -> dict[str, Any]:
    daily = {
        symbol: daily_observations(rows, cutoff, as_of)
        for symbol, rows in daily_by_symbol.items()
        if symbol in weights
    }
    intraday = {
        symbol: intraday_observations(rows, cutoff, as_of)
        for symbol, rows in intraday_by_symbol.items()
        if symbol in weights
    }
    return {
        "cutoff": cutoff.isoformat(),
        "daily": aggregate_weighted(
            daily, weights, DAILY_BOOLEAN_METRICS, DAILY_AVERAGE_METRICS
        ),
        "intraday60m": aggregate_weighted(
            intraday, weights, INTRADAY_BOOLEAN_METRICS, INTRADAY_AVERAGE_METRICS
        ),
    }


def _windows(
    daily: Mapping[str, list[Mapping[str, Any]]],
    intraday: Mapping[str, list[Mapping[str, Any]]],
    weights: Mapping[str, float],
    as_of: dt.date,
) -> dict[str, Any]:
    return {
        label: _window_summary(daily, intraday, weights, subtract_months(as_of, months), as_of)
        for label, months in WINDOW_MONTHS
    }


def _latest_date(rows_by_symbol: Mapping[str, list[Mapping[str, Any]]], field: str) -> str | None:
    values = [str(row[field]) for rows in rows_by_symbol.values() for row in rows if row.get(field)]
    return max(values) if values else None


def _weekday_lag(latest: str | dt.date | None, target: dt.date) -> int | None:
    if not latest:
        return None
    try:
        start = latest if isinstance(latest, dt.date) else dt.date.fromisoformat(str(latest)[:10])
    except ValueError:
        return None
    if start > target:
        return -1
    lag = 0
    cursor = start + dt.timedelta(days=1)
    while cursor <= target:
        lag += cursor.weekday() < 5
        cursor += dt.timedelta(days=1)
    return lag


def _ui_metrics(
    window: Mapping[str, Any], *, min_sessions: int, data_cutoff: dt.date
) -> dict[str, Any]:
    """Flatten a detailed window into the stable dashboard-card contract."""
    daily = window.get("daily") or {}
    intraday = window.get("intraday60m") or {}
    close_below_vwap = intraday.get("closeBelowVwapPct")
    composite_below_vwap = intraday.get("compositeCloseBelowVwapPct")
    close_vs_vwap = intraday.get("avgCloseVsVwapPct")
    primary_below_vwap = composite_below_vwap if composite_below_vwap is not None else close_below_vwap
    quality_reasons = []
    sessions = int(intraday.get("sessions") or 0)
    coverage = _finite_number(intraday.get("averageCoveragePct"))
    latest_date = intraday.get("latestDate")
    lag = _weekday_lag(latest_date, data_cutoff)
    if primary_below_vwap is None or close_vs_vwap is None:
        quality_reasons.append("required VWAP metrics are missing")
    if sessions < min_sessions:
        quality_reasons.append(f"only {sessions} intraday sessions; requires {min_sessions}")
    if coverage is None or coverage < MIN_COVERAGE_PCT:
        quality_reasons.append(
            f"coverage {coverage if coverage is not None else 'missing'}% is below {MIN_COVERAGE_PCT:.0f}%"
        )
    if lag is None or lag < 0 or lag > MAX_DATA_LAG_WEEKDAYS:
        quality_reasons.append(f"latest intraday session {latest_date or 'missing'} is not current")
    if quality_reasons:
        verdict = "DATA_INSUFFICIENT"
    elif primary_below_vwap > 55 and close_vs_vwap < 0:
        verdict = "SUPPORTS_LOWER_CLOSE"
    elif primary_below_vwap < 45 and close_vs_vwap > 0:
        verdict = "REJECTS_LOWER_CLOSE"
    else:
        verdict = "MIXED"
    high_to_close = daily.get("avgHighToClosePct")
    return {
        "sessions": daily.get("sessions", 0),
        "intradaySessions": sessions,
        "intradayLatestDate": latest_date,
        "intradayLagWeekdays": lag,
        "minimumRequiredSessions": min_sessions,
        "dataQualityStatus": "PASS" if not quality_reasons else "BLOCK",
        "dataQualityReasons": quality_reasons,
        "coveragePct": intraday.get("averageCoveragePct"),
        "dailyCoveragePct": daily.get("averageCoveragePct"),
        "intradayCoveragePct": intraday.get("averageCoveragePct"),
        "closeBelowOpenPct": daily.get("closeBelowOpenPct"),
        "closeBelowRangeMidPct": daily.get("closeBelowRangeMidpointPct"),
        "avgCloseLocationPct": daily.get("avgCloseLocationPct"),
        "avgOpenToClosePct": daily.get("avgOpenToClosePct"),
        "avgHighToCloseGivebackPct": _round(-high_to_close, 3) if high_to_close is not None else None,
        "closeBelowVwapPct": close_below_vwap,
        "compositeCloseBelowVwapPct": composite_below_vwap,
        "avgCloseVsVwapPct": close_vs_vwap,
        "closeBelowMidSessionPct": intraday.get("closeBelowMidSessionPct"),
        "avgCloseVsMidSessionPct": intraday.get("avgCloseVsMidSessionPct"),
        "closingBarRedPct": intraday.get("closingBarRedPct"),
        "avgClosingBarReturnPct": intraday.get("avgClosingBarReturnPct"),
        "peakBefore14Pct": intraday.get("peakBefore14Pct"),
        "verdict": verdict,
    }


def build_report(
    payload: Mapping[str, Any],
    raw_cache: Mapping[str, Any],
    as_of: dt.date,
    *,
    fetch_mode: str,
    fetch_errors: Iterable[str] = (),
    generated_at: dt.datetime | None = None,
) -> dict[str, Any]:
    context = dashboard_context(payload)
    symbols = context["symbols"]
    weights = context["weights"]
    equal_weights = {symbol: 1.0 / len(symbols) for symbol in symbols}
    daily = raw_cache.get("daily") or {}
    intraday = raw_cache.get("intraday60m") or {}
    benchmark = "QQQ"
    benchmark_weights = {benchmark: 1.0}
    generated_at = generated_at or dt.datetime.now(dt.timezone.utc)
    if generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=dt.timezone.utc)
    generated_et = generated_at.astimezone(ET)
    if as_of > generated_et.date():
        raise ValueError(f"analysis as-of {as_of} is in the future relative to {generated_et.date()}")
    # Yahoo can expose a still-forming daily candle and enough 60-minute bars to
    # resemble a US half-day.  Do not admit the current session before a short
    # post-close finalization buffer.
    data_cutoff = as_of
    forming_session_excluded = False
    if as_of == generated_et.date() and generated_et.time().replace(tzinfo=None) < dt.time(16, 15):
        data_cutoff = as_of - dt.timedelta(days=1)
        forming_session_excluded = True
    oldest_cutoff = subtract_months(data_cutoff, 3)
    missing_daily = [
        symbol
        for symbol in symbols
        if not daily_observations(daily.get(symbol) or [], oldest_cutoff, data_cutoff)
    ]
    missing_intraday = [
        symbol
        for symbol in symbols
        if not intraday_observations(intraday.get(symbol) or [], oldest_cutoff, data_cutoff)
    ]

    portfolio_weighted_windows = _windows(daily, intraday, weights, data_cutoff)
    portfolio_equal_windows = _windows(daily, intraday, equal_weights, data_cutoff)
    benchmark_windows = _windows(daily, intraday, benchmark_weights, data_cutoff)
    symbol_windows: dict[str, dict[str, Any]] = {}
    stocks = []
    for symbol in sorted(symbols, key=lambda item: (-weights[item], item)):
        symbol_windows[symbol] = _windows(daily, intraday, {symbol: 1.0}, data_cutoff)
        stocks.append(
            {
                "symbol": symbol,
                "currentValue": _round(context["values"][symbol], 2),
                "currentWeightPct": _round(weights[symbol] * 100.0, 3),
                "dailyAvailable": symbol not in missing_daily,
                "intradayAvailable": symbol not in missing_intraday,
                "windows": symbol_windows[symbol],
            }
        )

    # Compact contract consumed by the dashboard card.  Detailed blocks
    # remain below for auditability and alternative weighting views.
    ui_windows = {}
    for label, months in WINDOW_MONTHS:
        minimum_sessions = 15 * months
        ui_windows[label] = {
            "months": months,
            "startDate": subtract_months(data_cutoff, months).isoformat(),
            "endDate": data_cutoff.isoformat(),
            "sessions": portfolio_weighted_windows[label]["daily"].get("sessions", 0),
            "portfolio": _ui_metrics(
                portfolio_weighted_windows[label], min_sessions=minimum_sessions, data_cutoff=data_cutoff
            ),
            "qqq": _ui_metrics(
                benchmark_windows[label], min_sessions=minimum_sessions, data_cutoff=data_cutoff
            ),
            "symbols": {
                symbol: _ui_metrics(
                    symbol_windows[symbol][label], min_sessions=minimum_sessions, data_cutoff=data_cutoff
                )
                for symbol in symbols
            },
        }

    report = {
        "schemaVersion": 1,
        "generatedAt": generated_at.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "asOf": as_of.isoformat(),
        "effectiveDataCutoff": data_cutoff.isoformat(),
        "formingSessionExcluded": forming_session_excluded,
        "researchOnly": True,
        "decisionGrade": False,
        "decisionGradeReason": "Descriptive current-book sample using approximate 60-minute VWAP; not a forecast or execution signal.",
        "source": "Yahoo Finance via yfinance; adjusted daily and regular-session 60-minute bars",
        "fetchMode": fetch_mode,
        "fetchErrors": list(fetch_errors),
        "windows": ui_windows,
        "dataFreshness": {
            "dailyLatest": _latest_date(daily, "date"),
            "intradayLatestTimestamp": _latest_date(intraday, "timestamp"),
            "dashboardPriceAsOf": context["dashboardPriceAsOf"],
            "dashboardGeneratedAt": context["dashboardGeneratedAt"],
        },
        "methodology": {
            "windows": {
                label: {
                    "months": months,
                    "cutoff": subtract_months(data_cutoff, months).isoformat(),
                    "inclusiveThrough": data_cutoff.isoformat(),
                }
                for label, months in WINDOW_MONTHS
            },
            "daily": {
                "closeBelowOpen": "close < open",
                "closeBelowRangeMidpoint": "close < (high + low) / 2",
                "closeLocationPct": "100 * (close - low) / (high - low)",
            },
            "intraday60m": {
                "session": "regular US session bars with timestamps from 09:30 through before 16:00 America/New_York",
                "vwapProxy": "sum(((high+low+close)/3)*volume) / sum(volume) across 60-minute bars; not exchange tick VWAP",
                "midSessionProxy": "close of the middle regular-session 60-minute bar",
                "closingBar": "open-to-close return of the final available regular-session bucket; Yahoo's last 15:30 bucket is commonly only 30 minutes, so this is not labeled last-hour return",
                "aggregation": "closeBelowVwapPct is the session-average current-weighted share of available names below VWAP; compositeCloseBelowVwapPct is the fraction of sessions where the current-weighted mean close-vs-VWAP return is below zero",
                "minimumBars": 4,
            },
            "portfolioWeighting": "Current dashboard market-value weights, re-normalized by metric on each session; equal-weight lens also included",
            "limitations": [
                "Current-book survivorship/current-weight lens; not historical holdings reconstruction.",
                "Free Yahoo data can be delayed, adjusted, missing, or revised.",
                "60-minute typical-price VWAP is an approximation, not tick-level consolidated VWAP.",
                "Descriptive sample only; percentages are not forecasts or trading signals.",
            ],
        },
        "universe": {
            "benchmark": benchmark,
            "heldCount": len(symbols),
            "heldSymbols": symbols,
            "currentMarketValue": _round(context["marketValue"], 2),
            "currentWeightsPct": {
                symbol: _round(weights[symbol] * 100.0, 4) for symbol in symbols
            },
            "missingDaily": missing_daily,
            "missingIntraday60m": missing_intraday,
        },
        "portfolio": {
            "currentValueWeighted": {"windows": portfolio_weighted_windows},
            "equalWeighted": {"windows": portfolio_equal_windows},
        },
        "benchmark": {
            "symbol": benchmark,
            "dailyAvailable": bool(daily_observations(daily.get(benchmark) or [], oldest_cutoff, as_of)),
            "intradayAvailable": bool(
                intraday_observations(intraday.get(benchmark) or [], oldest_cutoff, as_of)
            ),
            "windows": benchmark_windows,
        },
        "stocks": stocks,
    }
    # Fail close here rather than emitting invalid JavaScript JSON later.
    json.dumps(sanitize_json(report), allow_nan=False)
    return sanitize_json(report)


def run(
    dashboard: str | Path = DEFAULT_DASHBOARD,
    *,
    as_of: str | dt.date | None = None,
    out: str | Path = DEFAULT_OUT,
    cache: str | Path = DEFAULT_CACHE,
    no_fetch: bool = False,
    downloader: Downloader | None = None,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    payload = read_dashboard(dashboard)
    context = dashboard_context(payload)
    if isinstance(as_of, dt.date):
        target = as_of
    else:
        as_of_text = as_of or context.get("dashboardPriceAsOf") or dt.date.today().isoformat()
        target = dt.date.fromisoformat(str(as_of_text))
    cache_path = Path(cache)
    fetch_errors: list[str] = []
    if no_fetch:
        raw = load_cache(cache_path)
        mode = "cache-only"
    else:
        try:
            old = load_cache(cache_path)
        except FileNotFoundError:
            old = None
        except (ValueError, json.JSONDecodeError) as exc:
            old = None
            fetch_errors.append(f"ignored invalid cache: {type(exc).__name__}: {exc}")
        start = subtract_months(target, 3) - dt.timedelta(days=7)
        end = target + dt.timedelta(days=1)
        fresh, errors = fetch_market_data(
            set(context["symbols"]) | {"QQQ"}, start, end, downloader=downloader
        )
        fetch_errors.extend(errors)
        has_fresh = any(fresh.get(kind) for kind in ("daily", "intraday60m"))
        if not has_fresh and old is None:
            detail = "; ".join(fetch_errors) or "provider returned no usable bars"
            raise RuntimeError(f"market-data fetch produced no usable data: {detail}")
        raw = merge_cache(old, fresh) if has_fresh else old
        mode = ("live-partial" if fetch_errors else "live") if has_fresh else "cache-fallback"
        attempt_time = (now or dt.datetime.now(dt.timezone.utc)).isoformat()
        raw["fetchAttemptedAt"] = attempt_time
        if has_fresh:
            raw["fetchedAt"] = attempt_time
        raw["requestedAsOf"] = target.isoformat()
        raw["requestedSymbols"] = sorted(set(context["symbols"]) | {"QQQ"})
        atomic_json(cache_path, raw)
    report = build_report(
        payload,
        raw,
        target,
        fetch_mode=mode,
        fetch_errors=fetch_errors,
        generated_at=now,
    )
    atomic_json(out, report)
    return report


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Analyze 1M/2M/3M portfolio closes versus open, range midpoint, and intraday proxies."
    )
    ap.add_argument("--dashboard", default=str(DEFAULT_DASHBOARD))
    ap.add_argument("--as-of", help="inclusive analysis date YYYY-MM-DD; defaults to dashboard priceAsOf")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--cache", default=str(DEFAULT_CACHE), help="raw daily/60m JSON cache")
    ap.add_argument("--no-fetch", action="store_true", help="recompute using only the raw-bar cache")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    report = run(
        args.dashboard,
        as_of=args.as_of,
        out=args.out,
        cache=args.cache,
        no_fetch=args.no_fetch,
    )
    weighted = report["portfolio"]["currentValueWeighted"]["windows"]
    print(f"✓ close-vs-intraday artifact: {Path(args.out).resolve()}")
    print(
        "  "
        + " · ".join(
            f"{label} close<VWAP {window['intraday60m'].get('closeBelowVwapPct')}%"
            for label, window in weighted.items()
        )
    )
    missing = report["universe"]["missingIntraday60m"]
    if missing:
        print(f"  intraday missing: {', '.join(missing)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

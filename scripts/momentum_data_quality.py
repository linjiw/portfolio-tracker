#!/usr/bin/env python3
"""Independent identity, corporate-action, and price-quality gate for momentum.

This module does not alter the Top-3 engine.  It audits the adjusted-close
cache, optionally refreshes raw/adjusted/action fields from yfinance, joins a
small official-event ledger, and emits per-security quality plus current
signal eligibility.  The global decision-grade gate remains blocked until
point-in-time index membership and delisting returns are complete.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

try:
    from scripts.artifact_io import atomic_write_json, atomic_write_text
except ModuleNotFoundError:  # direct ``python scripts/...`` execution
    from artifact_io import atomic_write_json, atomic_write_text


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PRICE_CACHE = ROOT / "output" / "momentum_prices.csv.gz"
DEFAULT_EVENTS = ROOT / "data" / "momentum_security_events.json"
DEFAULT_OUT = ROOT / "output" / "momentum_data_quality"
DEFAULT_PROVIDER_CACHE = DEFAULT_OUT / "provider_history.csv.gz"

BENCHMARKS = ("SPY", "QQQ")
PROVIDER_COLUMNS = (
    "raw_close", "adj_close", "dividends", "stock_splits",
)
SEVERITY_ORDER = {"INFO": 0, "WATCH": 1, "BLOCK": 2}
GATE_ORDER = {"PASS": 0, "WATCH": 1, "BLOCK": 2}
EPS = 1e-12


def safe_float(value: Any, digits: int = 10) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(number, digits) if math.isfinite(number) else None


def json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if isinstance(value, (pd.Timestamp, dt.datetime, dt.date)):
        return value.isoformat()
    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, (np.floating, float)):
        return safe_float(value)
    return value


def load_event_registry(path: Path = DEFAULT_EVENTS) -> dict[str, Any]:
    with Path(path).open(encoding="utf-8") as handle:
        registry = json.load(handle)
    if registry.get("schemaVersion") != 1:
        raise ValueError("unsupported momentum security-event schema")
    if registry.get("decisionGradeGate") != "BLOCK_DECISION_GRADE":
        raise ValueError("event registry must preserve BLOCK_DECISION_GRADE")
    if not isinstance(registry.get("securities"), dict):
        raise ValueError("event registry has no securities map")
    if not isinstance(registry.get("events"), list):
        raise ValueError("event registry has no events list")
    return registry


def read_price_cache(path: Path = DEFAULT_PRICE_CACHE) -> pd.DataFrame:
    frame = pd.read_csv(path, index_col=0, parse_dates=True, compression="gzip")
    frame.index = pd.to_datetime(frame.index, errors="coerce")
    frame = frame.loc[~frame.index.isna()].sort_index()
    frame = frame.loc[~frame.index.duplicated(keep="last")]
    frame.columns = [str(column).strip() for column in frame.columns]
    frame = frame.apply(pd.to_numeric, errors="coerce")
    frame = frame.replace([np.inf, -np.inf], np.nan).dropna(how="all")
    if frame.empty or frame.index.has_duplicates:
        raise ValueError("momentum price cache is empty or malformed")
    if any(benchmark not in frame for benchmark in BENCHMARKS):
        raise ValueError("momentum price cache lacks SPY/QQQ calendar anchors")
    return frame


def normalize_provider_long(frame: pd.DataFrame) -> pd.DataFrame:
    if frame is None or frame.empty:
        index = pd.MultiIndex.from_arrays([[], []], names=["date", "ticker"])
        return pd.DataFrame(index=index, columns=PROVIDER_COLUMNS, dtype=float)
    output = frame.copy()
    if not isinstance(output.index, pd.MultiIndex):
        if not {"date", "ticker"}.issubset(output.columns):
            raise ValueError("provider cache requires date and ticker")
        output["date"] = pd.to_datetime(output["date"], errors="coerce")
        output["ticker"] = output["ticker"].astype(str).str.strip()
        output = output.dropna(subset=["date"]).set_index(["date", "ticker"])
    else:
        dates = pd.to_datetime(output.index.get_level_values(0), errors="coerce")
        tickers = output.index.get_level_values(1).astype(str)
        output.index = pd.MultiIndex.from_arrays(
            [dates, tickers], names=["date", "ticker"]
        )
        output = output.loc[~output.index.get_level_values(0).isna()]
    for column in PROVIDER_COLUMNS:
        if column not in output:
            output[column] = 0.0 if column in {"dividends", "stock_splits"} else np.nan
        output[column] = pd.to_numeric(output[column], errors="coerce")
    output = output.loc[:, PROVIDER_COLUMNS]
    meaningful = (
        output["raw_close"].notna()
        | output["adj_close"].notna()
        | output["dividends"].fillna(0.0).ne(0.0)
        | output["stock_splits"].fillna(0.0).ne(0.0)
    )
    output = output.loc[meaningful]
    output = output.loc[~output.index.duplicated(keep="last")].sort_index()
    return output


def read_provider_cache(path: Path = DEFAULT_PROVIDER_CACHE) -> pd.DataFrame:
    if not Path(path).exists():
        return normalize_provider_long(pd.DataFrame())
    frame = pd.read_csv(path, compression="gzip")
    return normalize_provider_long(frame)


def _field_series(downloaded: pd.DataFrame, ticker: str, field: str) -> pd.Series:
    if downloaded.empty:
        return pd.Series(dtype=float)
    if isinstance(downloaded.columns, pd.MultiIndex):
        first = downloaded.columns.get_level_values(0)
        second = downloaded.columns.get_level_values(1)
        if field in first and ticker in second:
            value = downloaded[field][ticker]
        elif ticker in first and field in second:
            value = downloaded[ticker][field]
        else:
            return pd.Series(dtype=float)
    elif field in downloaded:
        value = downloaded[field]
    else:
        return pd.Series(dtype=float)
    return pd.to_numeric(value, errors="coerce")


def provider_download_to_long(
    downloaded: pd.DataFrame,
    tickers: Iterable[str],
) -> pd.DataFrame:
    rows: list[pd.DataFrame] = []
    mapping = {
        "Close": "raw_close",
        "Adj Close": "adj_close",
        "Dividends": "dividends",
        "Stock Splits": "stock_splits",
    }
    for ticker in tickers:
        data = pd.DataFrame(index=pd.to_datetime(downloaded.index, errors="coerce"))
        for field, output in mapping.items():
            series = _field_series(downloaded, ticker, field)
            data[output] = series.reindex(downloaded.index).to_numpy() if len(series) else np.nan
        if data[["raw_close", "adj_close"]].dropna(how="all").empty:
            continue
        data["dividends"] = data["dividends"].fillna(0.0)
        data["stock_splits"] = data["stock_splits"].fillna(0.0)
        data["ticker"] = ticker
        data.index.name = "date"
        rows.append(data.reset_index())
    if not rows:
        return normalize_provider_long(pd.DataFrame())
    return normalize_provider_long(pd.concat(rows, ignore_index=True))


def fetch_provider_history(
    tickers: Iterable[str],
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> pd.DataFrame:
    import yfinance as yf

    symbols = sorted(set(str(ticker) for ticker in tickers))
    if not symbols:
        return normalize_provider_long(pd.DataFrame())
    downloaded = yf.download(
        symbols,
        start=str(pd.Timestamp(start).date()),
        end=str((pd.Timestamp(end) + pd.Timedelta(days=1)).date()),
        auto_adjust=False,
        actions=True,
        group_by="column",
        threads=True,
        progress=False,
    )
    return provider_download_to_long(downloaded, symbols)


def merge_provider_cache(existing: pd.DataFrame, update: pd.DataFrame) -> pd.DataFrame:
    combined = pd.concat([normalize_provider_long(existing), normalize_provider_long(update)])
    return combined.loc[~combined.index.duplicated(keep="last")].sort_index()


def write_provider_cache(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    output = normalize_provider_long(frame).reset_index()
    output.to_csv(path, index=False, compression="gzip")
    path.chmod(0o600)


def latest_completed_month_signal_date(prices: pd.DataFrame) -> pd.Timestamp:
    month_end = prices.groupby(prices.index.to_period("M")).tail(1).index
    completed = month_end[
        month_end.to_period("M") < prices.index[-1].to_period("M")
    ]
    if not len(completed):
        raise ValueError("no completed month in price cache")
    return completed[-1]


def asof_price(frame: pd.DataFrame, months: int) -> pd.Series:
    anchor = frame.index[-1] - pd.DateOffset(months=months)
    eligible = frame.index[frame.index <= anchor]
    if not len(eligible):
        return frame.iloc[0] * np.nan
    return frame.loc[eligible[-1]]


def momentum_scores(
    prices: pd.DataFrame,
    signal_date: pd.Timestamp,
    months: int,
    *,
    coverage: float = 0.95,
    exclude: Iterable[str] = BENCHMARKS,
) -> pd.Series:
    history = prices.loc[:signal_date]
    coverage_window = history.loc[
        history.index >= history.index[-1] - pd.DateOffset(months=13)
    ]
    minimum = int(len(coverage_window) * coverage)
    columns = coverage_window.columns[coverage_window.notna().sum() >= minimum]
    columns = [column for column in columns if column not in set(exclude)]
    selected = history[columns]
    scores = selected.iloc[-1] / asof_price(selected, months) - 1.0
    return scores.replace([np.inf, -np.inf], np.nan).dropna().sort_values(
        ascending=False, kind="mergesort"
    )


def flag(
    code: str,
    severity: str,
    detail: str,
    *,
    date: pd.Timestamp | str | None = None,
    event_id: str | None = None,
) -> dict[str, Any]:
    output: dict[str, Any] = {"code": code, "severity": severity, "detail": detail}
    if date is not None:
        output["date"] = str(pd.Timestamp(date).date())
    if event_id:
        output["eventId"] = event_id
    return output


def ticker_events(registry: dict[str, Any], ticker: str) -> list[dict[str, Any]]:
    return [
        event for event in registry.get("events", [])
        if ticker in {event.get("parentTicker"), event.get("childTicker")}
    ]


def event_near_date(
    events: Iterable[dict[str, Any]],
    date: pd.Timestamp,
    tolerance_days: int = 4,
) -> dict[str, Any] | None:
    dates = ("distributionDate", "regularWayDate", "effectiveDate")
    for event in events:
        for key in dates:
            if event.get(key):
                distance = abs((pd.Timestamp(event[key]) - pd.Timestamp(date)).days)
                if distance <= tolerance_days:
                    return event
    return None


def non_integer_split(value: float, tolerance: float = 1e-3) -> bool:
    value = float(value)
    if value <= 0:
        return False
    if abs(value - round(value)) <= tolerance:
        return False
    inverse = 1.0 / value
    return abs(inverse - round(inverse)) > tolerance


def missing_run_stats(series: pd.Series, calendar: pd.DatetimeIndex) -> dict[str, Any]:
    valid = series.dropna()
    if len(valid) < 2:
        return {"count": 0, "max_run": 0, "dates": []}
    expected = calendar[(calendar >= valid.index[0]) & (calendar <= valid.index[-1])]
    missing = expected.difference(valid.index)
    missing_set = set(missing)
    max_run = run = 0
    for day in expected:
        if day in missing_set:
            run += 1
            max_run = max(max_run, run)
        else:
            run = 0
    return {
        "count": int(len(missing)),
        "max_run": int(max_run),
        "dates": [str(day.date()) for day in missing[:10]],
    }


def longest_flat_run(series: pd.Series, tolerance: float = 1e-12) -> dict[str, Any]:
    values = series.dropna().sort_index()
    if len(values) < 2:
        return {"sessions": 0, "start": None, "end": None}
    best = (1, values.index[0], values.index[0])
    run = 1
    start = values.index[0]
    prior = float(values.iloc[0])
    for day, value in values.iloc[1:].items():
        if math.isclose(float(value), prior, rel_tol=tolerance, abs_tol=tolerance):
            run += 1
        else:
            if run > best[0]:
                best = (run, start, prior_day)
            run = 1
            start = day
        prior = float(value)
        prior_day = day
    if run > best[0]:
        best = (run, start, values.index[-1])
    return {
        "sessions": int(best[0]),
        "start": str(pd.Timestamp(best[1]).date()),
        "end": str(pd.Timestamp(best[2]).date()),
    }


def provider_for_ticker(provider: pd.DataFrame, ticker: str) -> pd.DataFrame:
    provider = normalize_provider_long(provider)
    tickers = provider.index.get_level_values("ticker")
    if ticker not in set(tickers):
        return pd.DataFrame(columns=PROVIDER_COLUMNS)
    output = provider.xs(ticker, level="ticker").copy()
    output.index = pd.to_datetime(output.index)
    return output.sort_index()


def action_flags(
    ticker: str,
    provider: pd.DataFrame,
    registry: dict[str, Any],
    *,
    large_dividend_ratio: float = 0.10,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    history = provider_for_ticker(provider, ticker)
    events = ticker_events(registry, ticker)
    flags: list[dict[str, Any]] = []
    actions: list[dict[str, Any]] = []
    if history.empty:
        return flags, actions
    prior_close = history["raw_close"].shift(1)
    for day, row in history.iterrows():
        dividend_value = row.get("dividends")
        split_value = row.get("stock_splits")
        dividend = 0.0 if pd.isna(dividend_value) else float(dividend_value)
        split = 0.0 if pd.isna(split_value) else float(split_value)
        if not dividend and not split:
            continue
        event = event_near_date(events, day)
        item = {
            "date": str(day.date()),
            "dividend": safe_float(dividend, 6),
            "stockSplit": safe_float(split, 6),
            "eventId": event.get("id") if event else None,
        }
        actions.append(item)
        if split and non_integer_split(split):
            code = "FRACTIONAL_SPLIT_FACTOR"
            detail = f"provider stock-split factor {split:.6g} is non-integer"
            if event:
                detail += f"; mapped to official complex event {event['id']}"
            flags.append(flag(
                code,
                "WATCH" if event else "BLOCK",
                detail,
                date=day,
                event_id=event.get("id") if event else None,
            ))
        previous = float(prior_close.at[day]) if pd.notna(prior_close.at[day]) else None
        ratio = dividend / previous if previous and previous > 0 else None
        if ratio is not None and ratio >= large_dividend_ratio:
            detail = f"distribution {dividend:.6g} is {ratio:.1%} of prior raw close"
            if event:
                detail += f"; mapped to official complex event {event['id']}"
            flags.append(flag(
                "LARGE_DISTRIBUTION",
                "WATCH" if event else "BLOCK",
                detail,
                date=day,
                event_id=event.get("id") if event else None,
            ))
    return flags, actions


def price_flags(
    ticker: str,
    adjusted: pd.Series,
    calendar: pd.DatetimeIndex,
    provider: pd.DataFrame,
    registry: dict[str, Any],
    *,
    jump_threshold: float = 0.50,
    flat_sessions: int = 5,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    series = pd.to_numeric(adjusted, errors="coerce").replace(
        [np.inf, -np.inf], np.nan
    ).sort_index()
    valid = series.dropna()
    flags: list[dict[str, Any]] = []
    diagnostics: dict[str, Any] = {
        "observations": int(len(valid)),
        "first": str(valid.index[0].date()) if len(valid) else None,
        "last": str(valid.index[-1].date()) if len(valid) else None,
    }
    if valid.empty:
        return [flag("NO_PRICE_HISTORY", "BLOCK", "no valid adjusted close")], diagnostics
    if (valid <= 0).any():
        flags.append(flag("NON_POSITIVE_PRICE", "BLOCK", "adjusted close contains non-positive values"))

    gaps = missing_run_stats(series, calendar)
    diagnostics["internalGaps"] = gaps
    if gaps["count"]:
        severity = "BLOCK" if gaps["max_run"] >= 20 else "WATCH"
        flags.append(flag(
            "INTERNAL_GAP",
            severity,
            f"{gaps['count']} missing calendar sessions; max run {gaps['max_run']}",
            date=gaps["dates"][0],
        ))

    flat = longest_flat_run(valid)
    diagnostics["longestFlatRun"] = flat
    if flat["sessions"] >= flat_sessions:
        flags.append(flag(
            "FLAT_RUN",
            "WATCH",
            f"unchanged adjusted close for {flat['sessions']} observed sessions",
            date=flat["start"],
        ))

    events = ticker_events(registry, ticker)
    provider_history = provider_for_ticker(provider, ticker)
    action_dates = set()
    if not provider_history.empty:
        action_mask = (
            provider_history["dividends"].fillna(0.0).ne(0.0)
            | provider_history["stock_splits"].fillna(0.0).ne(0.0)
        )
        action_dates = set(provider_history.index[action_mask])
    daily = valid.pct_change().replace([np.inf, -np.inf], np.nan).dropna()
    large = daily[daily.abs() >= jump_threshold]
    diagnostics["largestAbsDailyReturn"] = safe_float(daily.abs().max()) if len(daily) else None
    diagnostics["largeMoveCount"] = int(len(large))
    for day, value in large.items():
        provider_explains = any(abs((day - action).days) <= 4 for action in action_dates)
        event = event_near_date(events, day)
        if provider_explains or event:
            continue
        flags.append(flag(
            "UNEXPLAINED_JUMP",
            "BLOCK" if abs(float(value)) >= 0.75 else "WATCH",
            f"adjusted close moved {float(value):+.1%} without a mapped action",
            date=day,
        ))

    if not provider_history.empty:
        joined = pd.concat(
            [valid.rename("cache"), provider_history["adj_close"].rename("provider")],
            axis=1,
            join="inner",
        ).dropna()
        if len(joined):
            relative = (joined["cache"] / joined["provider"] - 1.0).abs()
            diagnostics["providerAlignment"] = {
                "observations": int(len(relative)),
                "medianAbsRelativeDifference": safe_float(relative.median()),
                "maxAbsRelativeDifference": safe_float(relative.max()),
            }
            maximum = float(relative.max())
            if maximum > 0.01:
                flags.append(flag(
                    "PROVIDER_ADJUSTED_MISMATCH",
                    "BLOCK" if maximum > 0.10 else "WATCH",
                    f"cache/provider adjusted-close max difference {maximum:.2%}",
                    date=relative.idxmax(),
                ))
    return flags, diagnostics


def worst_gate(flags: Iterable[dict[str, Any]]) -> str:
    severity = max((SEVERITY_ORDER[row["severity"]] for row in flags), default=0)
    return {0: "PASS", 1: "WATCH", 2: "BLOCK"}[severity]


def flag_in_window(row: dict[str, Any], start: pd.Timestamp, end: pd.Timestamp) -> bool:
    if not row.get("date"):
        return True
    day = pd.Timestamp(row["date"])
    return start <= day <= end


def signal_eligibility(
    ticker: str,
    score: float | None,
    signal_date: pd.Timestamp,
    anchor_date: pd.Timestamp,
    cache_end: pd.Timestamp,
    security: dict[str, Any],
    flags: list[dict[str, Any]],
    provider_covered: bool,
) -> dict[str, Any]:
    reasons: list[str] = []
    gate = "PASS"

    def raise_gate(next_gate: str, reason: str) -> None:
        nonlocal gate
        if GATE_ORDER[next_gate] > GATE_ORDER[gate]:
            gate = next_gate
        reasons.append(reason)

    if score is None or not math.isfinite(float(score)):
        raise_gate("BLOCK", "momentum score is unavailable")
    if security.get("identityStatus") != "VERIFIED" or not security.get("cik"):
        raise_gate("BLOCK", "security identity is not pinned to a verified CIK")
    if not security.get("currentMembershipIndex"):
        raise_gate("BLOCK", "current SPX/NDX/DJIA membership is not officially verified")
    membership = security.get("currentMembershipStart")
    if membership and signal_date < pd.Timestamp(membership):
        raise_gate("BLOCK", f"not an eligible current-universe member until {membership}")
    regular = security.get("regularWayStart")
    if regular and anchor_date < pd.Timestamp(regular):
        raise_gate("BLOCK", f"lookback anchor predates regular-way trading {regular}")
    if not provider_covered:
        raise_gate("WATCH", "raw/adjusted/action provider history unavailable")
    for row in flags:
        if not flag_in_window(row, anchor_date, signal_date):
            continue
        if row["severity"] == "BLOCK":
            raise_gate("BLOCK", row["code"])
        elif row["severity"] == "WATCH":
            raise_gate("WATCH", row["code"])
    if not reasons:
        reasons.append("identity, current membership, lookback history, and provider actions pass")
    return {
        "gate": gate,
        "reasons": list(dict.fromkeys(reasons)),
        "anchorDate": str(anchor_date.date()),
        "signalDate": str(signal_date.date()),
        "cacheAsOf": str(cache_end.date()),
    }


def candidate_universe(
    prices: pd.DataFrame,
    signal_date: pd.Timestamp,
) -> dict[str, pd.Series]:
    return {
        "11m": momentum_scores(prices, signal_date, 11),
        "5m": momentum_scores(prices, signal_date, 5),
    }


def audit_security(
    ticker: str,
    prices: pd.DataFrame,
    provider: pd.DataFrame,
    registry: dict[str, Any],
    scores: dict[str, pd.Series],
    signal_date: pd.Timestamp,
) -> dict[str, Any]:
    calendar = prices["SPY"].dropna().index
    security = registry["securities"].get(ticker, {})
    price_quality_flags, diagnostics = price_flags(
        ticker, prices[ticker], calendar, provider, registry
    )
    corporate_flags, actions = action_flags(ticker, provider, registry)
    flags = sorted(
        [*price_quality_flags, *corporate_flags],
        key=lambda row: (-SEVERITY_ORDER[row["severity"]], row.get("date", ""), row["code"]),
    )
    provider_history = provider_for_ticker(provider, ticker)
    provider_covered = not provider_history[["raw_close", "adj_close"]].dropna(how="all").empty
    if security.get("identityStatus") != "VERIFIED":
        flags.append(flag(
            "IDENTITY_NOT_PINNED",
            "WATCH",
            "no verified CIK/share-class identity in the event registry",
        ))
    valid = prices[ticker].dropna()
    if len(valid) and valid.index[-1] < prices.index[-1]:
        flags.append(flag(
            "STALE_AT_CACHE_END",
            "BLOCK",
            f"last adjusted close {valid.index[-1].date()} precedes cache end {prices.index[-1].date()}",
            date=valid.index[-1],
        ))
    eligibility: dict[str, Any] = {}
    for label, months in (("11m", 11), ("5m", 5)):
        anchor = signal_date - pd.DateOffset(months=months)
        eligible_days = prices.index[prices.index <= anchor]
        anchor_day = eligible_days[-1] if len(eligible_days) else prices.index[0]
        score = scores[label].get(ticker)
        eligibility[label] = signal_eligibility(
            ticker,
            float(score) if score is not None else None,
            signal_date,
            anchor_day,
            prices.index[-1],
            security,
            flags,
            provider_covered,
        )
    return {
        "ticker": ticker,
        "identity": {
            "name": security.get("name"),
            "cik": security.get("cik"),
            "compositeFigi": security.get("compositeFigi"),
            "shareClassFigi": security.get("shareClassFigi"),
            "status": security.get("identityStatus", "UNVERIFIED"),
            "regularWayStart": security.get("regularWayStart"),
        },
        "membership": {
            "index": security.get("currentMembershipIndex"),
            "start": security.get("currentMembershipStart"),
            "asOf": security.get("currentMembershipAsOf"),
            "verified": bool(
                security.get("currentMembershipIndex")
                and (
                    security.get("currentMembershipStart")
                    or security.get("currentMembershipAsOf")
                )
            ),
        },
        "officialSources": security.get("officialSources", []),
        "providerCovered": provider_covered,
        "providerRange": {
            "start": str(provider_history.index[0].date()) if len(provider_history) else None,
            "end": str(provider_history.index[-1].date()) if len(provider_history) else None,
        },
        "actions": actions,
        "knownEvents": [event["id"] for event in ticker_events(registry, ticker)],
        "priceDiagnostics": diagnostics,
        "qualityFlags": flags,
        "historyQualityGate": worst_gate(flags),
        "signalEligibility": eligibility,
    }


def rank_rows(
    scores: pd.Series,
    records: dict[str, dict[str, Any]],
    label: str,
    count: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for rank, (ticker, score) in enumerate(scores.head(count).items(), 1):
        record = records[ticker]
        rows.append({
            "rank": rank,
            "ticker": ticker,
            "momentumReturn": float(score),
            "gate": record["signalEligibility"][label]["gate"],
            "gateReasons": record["signalEligibility"][label]["reasons"],
            "cik": record["identity"]["cik"],
            "officialSources": record["officialSources"],
        })
    return rows


def build_payload(
    prices: pd.DataFrame,
    provider: pd.DataFrame,
    registry: dict[str, Any],
    *,
    price_cache_path: Path,
    provider_cache_path: Path,
) -> dict[str, Any]:
    signal_date = latest_completed_month_signal_date(prices)
    scores = candidate_universe(prices, signal_date)
    records = {
        ticker: audit_security(ticker, prices, provider, registry, scores, signal_date)
        for ticker in prices.columns
        if ticker not in BENCHMARKS
    }
    top = {
        "11mTop3": rank_rows(scores["11m"], records, "11m", 3),
        "11mTop5": rank_rows(scores["11m"], records, "11m", 5),
        "5mTop5": rank_rows(scores["5m"], records, "5m", 5),
    }
    gate_counts: dict[str, int] = {"PASS": 0, "WATCH": 0, "BLOCK": 0}
    for record in records.values():
        gate_counts[record["historyQualityGate"]] += 1
    return json_safe({
        "schemaVersion": 1,
        "generatedAt": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "researchOnly": True,
        "decisionGrade": False,
        "decisionGradeGate": "BLOCK_DECISION_GRADE",
        "decisionGradeReasons": registry["decisionGradeReasons"],
        "data": {
            "priceCache": str(price_cache_path),
            "providerCache": str(provider_cache_path),
            "cacheStart": str(prices.index[0].date()),
            "cacheAsOf": str(prices.index[-1].date()),
            "completedSignalDate": str(signal_date.date()),
            "securityCount": len(records),
            "providerCoveredCount": sum(row["providerCovered"] for row in records.values()),
        },
        "protocol": {
            "identityKey": "CIK plus share class/FIGI and dated ticker alias; ticker alone is forbidden",
            "actionChecks": [
                "fractional split factor", "large distribution", "unexplained jump",
                "flat run", "internal gap", "cache/provider adjusted mismatch",
            ],
            "signalGateIsWindowScoped": True,
            "historyGateMayRemainWatchWhenCurrentLookbackPasses": True,
        },
        "knownTickerReuse": registry.get("knownTickerReuse", []),
        "eventLedger": registry.get("events", []),
        "historyGateCounts": gate_counts,
        "currentCandidates": top,
        "securities": records,
    })


def pct(value: Any) -> str:
    return "—" if value is None else f"{float(value) * 100:.1f}%"


def render_report(payload: dict[str, Any]) -> str:
    lines = [
        "# Momentum 数据质量门",
        "",
        f"生成：{payload['generatedAt']}  ",
        f"价格截止：{payload['data']['cacheAsOf']}  ",
        f"已完成月末信号：{payload['data']['completedSignalDate']}  ",
        "",
        "## 总门禁",
        "",
        "**BLOCK_DECISION_GRADE** — 单只证券 PASS 不会解除全局阻断。",
        "",
    ]
    lines.extend(f"- {reason}" for reason in payload["decisionGradeReasons"])
    lines.extend([
        "",
        "## 当前 11M Top3 / Top5",
        "",
        "| Rank | Ticker | 11M | Gate | CIK | 主要原因 |",
        "|---:|---|---:|---|---|---|",
    ])
    for row in payload["currentCandidates"]["11mTop5"]:
        lines.append(
            f"| {row['rank']} | {row['ticker']} | {pct(row['momentumReturn'])} | "
            f"{row['gate']} | {row['cik'] or '—'} | {'; '.join(row['gateReasons'])} |"
        )
    lines.extend([
        "",
        "## 当前 5M Top5",
        "",
        "| Rank | Ticker | 5M | Gate | CIK | 主要原因 |",
        "|---:|---|---:|---|---|---|",
    ])
    for row in payload["currentCandidates"]["5mTop5"]:
        lines.append(
            f"| {row['rank']} | {row['ticker']} | {pct(row['momentumReturn'])} | "
            f"{row['gate']} | {row['cik'] or '—'} | {'; '.join(row['gateReasons'])} |"
        )
    candidate_tickers = []
    for group in ("11mTop5", "5mTop5"):
        candidate_tickers.extend(
            row["ticker"] for row in payload["currentCandidates"][group]
        )
    lines.extend([
        "",
        "## 当前候选官方来源",
        "",
    ])
    for ticker in dict.fromkeys(candidate_tickers):
        sources = payload["securities"][ticker]["officialSources"]
        links = ", ".join(
            f"[{number}]({url})" for number, url in enumerate(sources, 1)
        )
        lines.append(f"- **{ticker}**: {links or '未登记'}")
    lines.extend([
        "",
        "## 已锁定复杂事件",
        "",
        "| Event | Type | Parent → Child | Distribution / regular-way | Ratio |",
        "|---|---|---|---|---:|",
    ])
    for event in payload["eventLedger"]:
        lines.append(
            f"| {event['id']} | {event['type']} | {event.get('parentTicker')} → "
            f"{event.get('childTicker')} | {event.get('distributionDate')} / "
            f"{event.get('regularWayDate')} | {event.get('childSharesPerParentShare')} |"
        )
    lines.extend([
        "",
        "## 解释",
        "",
        "- `historyQualityGate` 检查完整本地历史；`signalEligibility` 只检查当前动量回看窗口。",
        "- 因此 WDC 的 2025-02 分拆可令历史为 WATCH，但 2025-07 之后开始的 11M 窗口仍可 PASS。",
        "- 非整数 split 字段可能是供应商对分拆的连续性因子，不得自动解释为法定拆股。",
        "- 未登记身份的证券保留 WATCH；若进入候选榜则因缺少 CIK 锁定而 BLOCK。",
        "",
    ])
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--price-cache", type=Path, default=DEFAULT_PRICE_CACHE)
    parser.add_argument("--events", type=Path, default=DEFAULT_EVENTS)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--provider-cache", type=Path, default=DEFAULT_PROVIDER_CACHE)
    parser.add_argument("--no-fetch", action="store_true")
    parser.add_argument(
        "--full-provider",
        action="store_true",
        help="fetch raw/action fields for every cached security instead of event/candidate names",
    )
    args = parser.parse_args()

    prices = read_price_cache(args.price_cache)
    registry = load_event_registry(args.events)
    signal_date = latest_completed_month_signal_date(prices)
    scores = candidate_universe(prices, signal_date)
    selected = set(registry["securities"])
    selected.update(scores["11m"].head(10).index)
    selected.update(scores["5m"].head(10).index)
    selected &= set(prices.columns)
    requested = sorted(set(prices.columns) - set(BENCHMARKS)) if args.full_provider else sorted(selected)

    provider = read_provider_cache(args.provider_cache)
    if not args.no_fetch:
        update = fetch_provider_history(requested, prices.index[0], prices.index[-1])
        provider = merge_provider_cache(provider, update)
        write_provider_cache(args.provider_cache, provider)

    payload = build_payload(
        prices,
        provider,
        registry,
        price_cache_path=args.price_cache,
        provider_cache_path=args.provider_cache,
    )
    args.out_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    json_path = args.out_dir / "momentum_data_quality.json"
    md_path = args.out_dir / "momentum_data_quality.md"
    atomic_write_json(json_path, payload)
    atomic_write_text(md_path, render_report(payload))
    print(f"wrote {json_path}")
    print(f"wrote {md_path}")
    print(
        "decision grade BLOCK | 11m Top3 "
        + ", ".join(
            f"{row['ticker']}:{row['gate']}"
            for row in payload["currentCandidates"]["11mTop3"]
        )
    )


if __name__ == "__main__":
    main()

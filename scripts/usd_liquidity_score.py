#!/usr/bin/env python3
"""USD liquidity composite score.

Pulls public FRED CSV series, builds a repeatable 0-100 liquidity regime score,
and writes JSON/Markdown artifacts for portfolio risk context.
"""
from __future__ import annotations

import argparse
import datetime as dt
from pathlib import Path
from typing import Dict, Iterable, Optional

import numpy as np
import pandas as pd

try:
    from scripts.artifact_io import atomic_write_json, atomic_write_text
except ImportError:  # direct `python scripts/usd_liquidity_score.py`
    from artifact_io import atomic_write_json, atomic_write_text


ROOT = Path(__file__).resolve().parents[1]
OUT_JSON = ROOT / "output" / "usd_liquidity_score.json"
OUT_MD = ROOT / "output" / "usd_liquidity_report.md"
BASE_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id="
MODEL_VERSION = "0.2"
SOURCE_MAX_LAG_DAYS = {
    "WALCL": 14,
    "WDTGAL": 14,
    "WRBWFRBL": 14,
    "RRPONTSYD": 7,
    "SOFR": 7,
    "IORB": 7,
    "DGS2": 7,
    "DGS10": 7,
    "DTWEXBGS": 7,
    "VIXCLS": 7,
    "BAMLH0A0HYM2": 7,
}

SERIES = {
    "WALCL": "fed_assets_mn",
    "WDTGAL": "tga_mn",
    "RRPONTSYD": "rrp_bn",
    "WRBWFRBL": "reserves_mn",
    "SOFR": "sofr",
    "IORB": "iorb",
    "DGS2": "ust2y",
    "DGS10": "ust10y",
    "DTWEXBGS": "broad_usd",
    "VIXCLS": "vix",
    "BAMLH0A0HYM2": "hy_oas",
}

WEIGHTS = {
    "net_liq_flow": 0.20,
    "net_liq_13w": 0.15,
    "reserves_flow": 0.15,
    "tga_drain": 0.10,
    "funding_pressure": 0.15,
    "usd_pressure": 0.10,
    "rate_pressure": 0.07,
    "vix_pressure": 0.04,
    "credit_pressure": 0.04,
}


def rn(value, digits=2):
    if value is None:
        return None
    try:
        if not np.isfinite(float(value)):
            return None
        return round(float(value), digits)
    except Exception:
        return None


def load_fred_series(series_id: str, name: str, base_url: str = BASE_URL) -> pd.DataFrame:
    df = pd.read_csv(base_url + series_id)
    if len(df.columns) != 2:
        raise ValueError(f"unexpected FRED CSV shape for {series_id}: {df.columns}")
    df.columns = ["date", name]
    df["date"] = pd.to_datetime(df["date"])
    df[name] = pd.to_numeric(df[name].replace(".", np.nan), errors="coerce")
    return df.set_index("date").sort_index()


def latest_observation_dates(frames: Dict[str, pd.DataFrame]) -> Dict[str, str]:
    out = {}
    for series_id, frame in frames.items():
        values = frame.dropna()
        if not values.empty:
            out[series_id] = values.index[-1].date().isoformat()
    return out


def load_all_series(series=SERIES, base_url: str = BASE_URL, as_of: Optional[dt.date] = None):
    cutoff = pd.Timestamp(as_of or dt.date.today())
    frames = {
        sid: load_fred_series(sid, name, base_url=base_url).loc[:cutoff]
        for sid, name in series.items()
    }
    return pd.concat(frames.values(), axis=1).sort_index().ffill(), latest_observation_dates(frames)


def rolling_z(s: pd.Series, window: int = 756, min_periods: int = 252) -> pd.Series:
    mean = s.rolling(window, min_periods=min_periods).mean()
    std = s.rolling(window, min_periods=min_periods).std()
    return (s - mean) / std.replace(0, np.nan)


def calendar_lag(series: pd.Series, days: int) -> pd.Series:
    """Value observed on or immediately before ``days`` calendar days ago."""
    idx = series.index
    values = []
    for stamp in idx:
        target = stamp - pd.Timedelta(days=days)
        pos = idx.searchsorted(target, side="right") - 1
        values.append(series.iloc[pos] if pos >= 0 else np.nan)
    return pd.Series(values, index=idx, dtype=float)


def build_features(data: pd.DataFrame) -> pd.DataFrame:
    data = data.copy()
    data["fed_assets_bn"] = data["fed_assets_mn"] / 1000.0
    data["tga_bn"] = data["tga_mn"] / 1000.0
    data["reserves_bn"] = data["reserves_mn"] / 1000.0
    data["net_liquidity_bn"] = data["fed_assets_bn"] - data["tga_bn"] - data["rrp_bn"]

    lag4 = lambda s: calendar_lag(s, 28)
    lag13 = lambda s: calendar_lag(s, 91)
    data["net_liq_4w_chg"] = data["net_liquidity_bn"] - lag4(data["net_liquidity_bn"])
    data["net_liq_13w_chg"] = data["net_liquidity_bn"] - lag13(data["net_liquidity_bn"])
    data["reserves_4w_chg"] = data["reserves_bn"] - lag4(data["reserves_bn"])
    data["tga_4w_chg"] = data["tga_bn"] - lag4(data["tga_bn"])
    data["sofr_iorb_bp"] = (data["sofr"] - data["iorb"]) * 100.0
    data["usd_4w_chg_pct"] = (data["broad_usd"] / lag4(data["broad_usd"]) - 1.0) * 100.0
    data["ust2y_4w_chg_bp"] = (data["ust2y"] - lag4(data["ust2y"])) * 100.0
    data["ust10y_4w_chg_bp"] = (data["ust10y"] - lag4(data["ust10y"])) * 100.0
    data["vix_4w_chg"] = data["vix"] - lag4(data["vix"])
    data["hy_oas_4w_chg_bp"] = (data["hy_oas"] - lag4(data["hy_oas"])) * 100.0
    return data


def score_components(data: pd.DataFrame) -> pd.DataFrame:
    components = pd.DataFrame(index=data.index)
    components["net_liq_flow"] = rolling_z(data["net_liq_4w_chg"])
    components["net_liq_13w"] = rolling_z(data["net_liq_13w_chg"])
    components["reserves_flow"] = rolling_z(data["reserves_4w_chg"])
    components["tga_drain"] = -rolling_z(data["tga_4w_chg"])
    components["funding_pressure"] = -rolling_z(data["sofr_iorb_bp"])
    components["usd_pressure"] = -rolling_z(data["usd_4w_chg_pct"])
    components["rate_pressure"] = -rolling_z(data["ust2y_4w_chg_bp"])
    components["vix_pressure"] = -rolling_z(data["vix_4w_chg"])
    components["credit_pressure"] = -rolling_z(data["hy_oas_4w_chg_bp"])
    return components


def attach_score(data: pd.DataFrame) -> pd.DataFrame:
    data = build_features(data)
    components = score_components(data)
    raw = sum(components[key] * weight for key, weight in WEIGHTS.items())
    data["usd_liquidity_raw"] = raw
    data["usd_liquidity_score"] = (50.0 + 10.0 * raw).clip(0, 100)
    for key in WEIGHTS:
        data[f"component_{key}"] = components[key]
    return data


def classify_score(score: Optional[float]) -> Dict[str, str]:
    if score is None:
        return {"label": "UNKNOWN", "tone": "neutral", "meaning": "Not enough data."}
    if score >= 60:
        return {"label": "TAILWIND", "tone": "positive", "meaning": "Liquidity is a tailwind; risk appetite can broaden."}
    if score >= 50:
        return {"label": "NEUTRAL_EASING", "tone": "positive", "meaning": "Neutral-to-easy; leaders can broaden if price confirms."}
    if score >= 40:
        return {"label": "NEUTRAL_TIGHT", "tone": "warning", "meaning": "Neutral-to-tight; only high-conviction EPS themes deserve aggression."}
    if score >= 30:
        return {"label": "TIGHT_RISK", "tone": "negative", "meaning": "Liquidity is tight; reduce chase and favor hedges/stops."}
    return {"label": "STRESS", "tone": "negative", "meaning": "Systemic stress regime; capital preservation first."}


def score_delta(row, data: pd.DataFrame, days: int):
    target = row.name - pd.Timedelta(days=days)
    prior = data.loc[:target, "usd_liquidity_score"].dropna()
    if prior.empty:
        return None
    prev = prior.iloc[-1]
    cur = row.get("usd_liquidity_score")
    if pd.isna(prev) or pd.isna(cur):
        return None
    return float(cur - prev)


def latest_document(
    data: pd.DataFrame,
    source_dates: Dict[str, str],
    requested_as_of: Optional[dt.date] = None,
) -> Dict:
    scored = attach_score(data).dropna(subset=["usd_liquidity_score"])
    if scored.empty:
        raise RuntimeError("not enough data to compute USD liquidity score")
    row = scored.iloc[-1]
    regime = classify_score(rn(row.get("usd_liquidity_score"), 1))
    components = {
        key: {
            "z": rn(row.get(f"component_{key}"), 3),
            "weight": WEIGHTS[key],
            "weightedContribution": rn((row.get(f"component_{key}") or 0.0) * WEIGHTS[key], 3),
        }
        for key in WEIGHTS
    }
    metrics = {
        "netLiquidityBn": rn(row.get("net_liquidity_bn"), 1),
        "fedAssetsBn": rn(row.get("fed_assets_bn"), 1),
        "tgaBn": rn(row.get("tga_bn"), 1),
        "rrpBn": rn(row.get("rrp_bn"), 2),
        "reservesBn": rn(row.get("reserves_bn"), 1),
        "netLiquidity4wChangeBn": rn(row.get("net_liq_4w_chg"), 1),
        "netLiquidity13wChangeBn": rn(row.get("net_liq_13w_chg"), 1),
        "reserves4wChangeBn": rn(row.get("reserves_4w_chg"), 1),
        "tga4wChangeBn": rn(row.get("tga_4w_chg"), 1),
        "sofrIorbBp": rn(row.get("sofr_iorb_bp"), 1),
        "broadUsd": rn(row.get("broad_usd"), 4),
        "broadUsd4wChangePct": rn(row.get("usd_4w_chg_pct"), 2),
        "ust2y": rn(row.get("ust2y"), 3),
        "ust10y": rn(row.get("ust10y"), 3),
        "ust2y4wChangeBp": rn(row.get("ust2y_4w_chg_bp"), 1),
        "vix": rn(row.get("vix"), 2),
        "vix4wChange": rn(row.get("vix_4w_chg"), 2),
        "hyOas": rn(row.get("hy_oas"), 2),
        "hyOas4wChangeBp": rn(row.get("hy_oas_4w_chg_bp"), 1),
    }
    as_of = row.name.date()
    freshness_reference = requested_as_of or as_of
    source_lags = {}
    for series_id, source_date in source_dates.items():
        try:
            source_lags[series_id] = (freshness_reference - dt.date.fromisoformat(source_date)).days
        except (TypeError, ValueError):
            source_lags[series_id] = None
    missing_sources = sorted(set(SERIES) - set(source_dates))
    stale_sources = sorted(
        series_id
        for series_id, lag in source_lags.items()
        if (
            lag is None or lag < 0 or lag > SOURCE_MAX_LAG_DAYS.get(series_id, 7)
            or (parse_iso_date(source_dates.get(series_id)) or dt.date.max) > as_of
        )
    )
    freshness_reasons = []
    if missing_sources:
        freshness_reasons.append("missing source dates: " + ", ".join(missing_sources))
    if stale_sources:
        freshness_reasons.append("stale/future source dates: " + ", ".join(stale_sources))
    score_row_lag = (freshness_reference - as_of).days
    if score_row_lag < 0 or score_row_lag > 7:
        freshness_reasons.append(
            f"composite score row {as_of.isoformat()} is not current for {freshness_reference.isoformat()}"
        )
    freshness_status = "PASS" if not freshness_reasons else "BLOCK"
    interpretation = interpret(regime["label"], metrics)
    if freshness_reasons:
        interpretation["portfolioAction"] = (
            "DATA BLOCK: refresh the listed FRED series before using this regime for portfolio decisions."
        )
    valid_source_dates = [value for value in source_dates.values() if parse_iso_date(value)]
    return {
        "modelVersion": MODEL_VERSION,
        "generatedAt": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "asOf": as_of.isoformat(),
        "requestedAsOf": freshness_reference.isoformat(),
        "researchOnly": True,
        "decisionGrade": False,
        "backtestEligible": False,
        "decisionGradeReason": "Heuristic mixed-frequency composite; FRED observation dates do not provide vintage/release-time point-in-time data.",
        "score": rn(row.get("usd_liquidity_score"), 1),
        "rawScore": rn(row.get("usd_liquidity_raw"), 3),
        "regime": regime,
        "scoreDeltas": {
            "1w": rn(score_delta(row, scored, 7), 1),
            "4w": rn(score_delta(row, scored, 28), 1),
            "13w": rn(score_delta(row, scored, 91), 1),
        },
        "metrics": metrics,
        "components": components,
        "sourceDates": source_dates,
        "sourceLagDays": source_lags,
        "oldestSourceDate": min(valid_source_dates) if valid_source_dates else None,
        "mixedSourceDates": len(set(source_dates.values())) > 1,
        "dataFreshness": {
            "status": freshness_status,
            "referenceDate": freshness_reference.isoformat(),
            "scoreRowLagDays": score_row_lag,
            "missingSources": missing_sources,
            "staleSources": stale_sources,
            "reasons": freshness_reasons,
            "maxLagDaysBySource": SOURCE_MAX_LAG_DAYS,
        },
        "sources": {series_id: f"{BASE_URL}{series_id}" for series_id in SERIES},
        "interpretation": interpretation,
    }


def parse_iso_date(value):
    try:
        return dt.date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def interpret(label: str, metrics: Dict) -> Dict[str, str]:
    if label in {"STRESS", "TIGHT_RISK"}:
        action = "Defense first: no broad chase; hedge high-beta exposure and require reclaim levels."
    elif label == "NEUTRAL_TIGHT":
        action = "Selective offense only: hard EPS upgrade themes can work, but use position caps and stops."
    else:
        action = "Liquidity supports broader risk appetite if price and breadth confirm."

    semi = (
        "Semiconductor leadership is more credible when score is above 55; below 50, treat semis as a narrow "
        "relative-strength pocket that can unwind if crowded positioning breaks."
    )
    plumbing = "Funding pipes are not stressed while SOFR-IORB stays below roughly +5bp."
    if metrics.get("sofrIorbBp") is not None and metrics["sofrIorbBp"] > 5:
        plumbing = "SOFR-IORB is above +5bp; funding plumbing deserves stress-level attention."
    return {"portfolioAction": action, "semiReadthrough": semi, "fundingReadthrough": plumbing}


def fmt(value, suffix="", digits=1):
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:,.{digits}f}{suffix}"
    return f"{value}{suffix}"


def render_report(doc: Dict) -> str:
    m = doc["metrics"]
    lines = [
        "# USD Liquidity Composite",
        "",
        f"Generated: {doc['generatedAt']}",
        f"Data as of: {doc['asOf']}",
        f"Data freshness: **{doc['dataFreshness']['status']}**"
        + (f" · {'; '.join(doc['dataFreshness']['reasons'])}" if doc['dataFreshness']['reasons'] else ""),
        f"Score: **{doc['score']}/100** · Regime: **{doc['regime']['label']}**",
        f"Meaning: {doc['regime']['meaning']}",
        "",
        "## Core Metrics",
        "| Metric | Value |",
        "|---|---:|",
        f"| Net dollar liquidity | ${fmt(m.get('netLiquidityBn'))}B |",
        f"| Fed assets | ${fmt(m.get('fedAssetsBn'))}B |",
        f"| Treasury General Account | ${fmt(m.get('tgaBn'))}B |",
        f"| ON RRP | ${fmt(m.get('rrpBn'), digits=2)}B |",
        f"| Reserve balances | ${fmt(m.get('reservesBn'))}B |",
        f"| 4W net-liquidity change | {fmt(m.get('netLiquidity4wChangeBn'))}B |",
        f"| 4W reserve change | {fmt(m.get('reserves4wChangeBn'))}B |",
        f"| SOFR - IORB | {fmt(m.get('sofrIorbBp'))} bp |",
        f"| Broad USD | {fmt(m.get('broadUsd'), digits=4)} |",
        f"| 2Y / 10Y Treasury | {fmt(m.get('ust2y'), '%', 3)} / {fmt(m.get('ust10y'), '%', 3)} |",
        f"| VIX | {fmt(m.get('vix'), digits=2)} |",
        f"| HY OAS | {fmt(m.get('hyOas'), '%', 2)} |",
        "",
        "## Component Z-Scores",
        "| Component | Weight | Z | Contribution |",
        "|---|---:|---:|---:|",
    ]
    for key, row in doc["components"].items():
        lines.append(f"| {key} | {row['weight']:.0%} | {fmt(row.get('z'), digits=3)} | {fmt(row.get('weightedContribution'), digits=3)} |")
    lines += [
        "",
        "## Interpretation",
        f"- Portfolio action: {doc['interpretation']['portfolioAction']}",
        f"- Semi read-through: {doc['interpretation']['semiReadthrough']}",
        f"- Funding read-through: {doc['interpretation']['fundingReadthrough']}",
        "",
        "## Source Dates",
    ]
    for series_id, date in doc.get("sourceDates", {}).items():
        lines.append(f"- {series_id}: {date}")
    return "\n".join(lines) + "\n"


def write_outputs(doc: Dict, out_json=OUT_JSON, out_md=OUT_MD) -> None:
    out_json = Path(out_json)
    out_md = Path(out_md)
    atomic_write_json(out_json, doc)
    atomic_write_text(out_md, render_report(doc))


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Build USD Liquidity Composite from FRED data.")
    parser.add_argument("--out-json", default=str(OUT_JSON))
    parser.add_argument("--out-md", default=str(OUT_MD))
    parser.add_argument("--as-of", type=dt.date.fromisoformat, default=dt.date.today(),
                        help="ignore observations dated after this YYYY-MM-DD cutoff")
    args = parser.parse_args(argv)

    if args.as_of > dt.date.today():
        raise SystemExit(f"--as-of {args.as_of} is in the future")

    data, source_dates = load_all_series(as_of=args.as_of)
    doc = latest_document(data, source_dates, requested_as_of=args.as_of)
    write_outputs(doc, args.out_json, args.out_md)
    print(f"USD liquidity {doc['score']}/100 {doc['regime']['label']} as of {doc['asOf']}")
    print(Path(args.out_json).resolve())
    print(Path(args.out_md).resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

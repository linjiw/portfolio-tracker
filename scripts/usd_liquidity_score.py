#!/usr/bin/env python3
"""USD liquidity composite score.

Pulls public FRED CSV series, builds a repeatable 0-100 liquidity regime score,
and writes JSON/Markdown artifacts for portfolio risk context.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
from typing import Dict, Iterable, Optional

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
OUT_JSON = ROOT / "output" / "usd_liquidity_score.json"
OUT_MD = ROOT / "output" / "usd_liquidity_report.md"
BASE_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id="
MODEL_VERSION = "0.1"

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


def load_all_series(series=SERIES, base_url: str = BASE_URL):
    frames = {sid: load_fred_series(sid, name, base_url=base_url) for sid, name in series.items()}
    return pd.concat(frames.values(), axis=1).sort_index().ffill(), latest_observation_dates(frames)


def rolling_z(s: pd.Series, window: int = 756, min_periods: int = 252) -> pd.Series:
    mean = s.rolling(window, min_periods=min_periods).mean()
    std = s.rolling(window, min_periods=min_periods).std()
    return (s - mean) / std.replace(0, np.nan)


def build_features(data: pd.DataFrame) -> pd.DataFrame:
    data = data.copy()
    data["fed_assets_bn"] = data["fed_assets_mn"] / 1000.0
    data["tga_bn"] = data["tga_mn"] / 1000.0
    data["reserves_bn"] = data["reserves_mn"] / 1000.0
    data["net_liquidity_bn"] = data["fed_assets_bn"] - data["tga_bn"] - data["rrp_bn"]

    data["net_liq_4w_chg"] = data["net_liquidity_bn"] - data["net_liquidity_bn"].shift(28)
    data["net_liq_13w_chg"] = data["net_liquidity_bn"] - data["net_liquidity_bn"].shift(91)
    data["reserves_4w_chg"] = data["reserves_bn"] - data["reserves_bn"].shift(28)
    data["tga_4w_chg"] = data["tga_bn"] - data["tga_bn"].shift(28)
    data["sofr_iorb_bp"] = (data["sofr"] - data["iorb"]) * 100.0
    data["usd_4w_chg_pct"] = data["broad_usd"].pct_change(28) * 100.0
    data["ust2y_4w_chg_bp"] = (data["ust2y"] - data["ust2y"].shift(28)) * 100.0
    data["ust10y_4w_chg_bp"] = (data["ust10y"] - data["ust10y"].shift(28)) * 100.0
    data["vix_4w_chg"] = data["vix"] - data["vix"].shift(28)
    data["hy_oas_4w_chg_bp"] = (data["hy_oas"] - data["hy_oas"].shift(28)) * 100.0
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
    loc = data.index.get_loc(row.name)
    if not isinstance(loc, int) or loc < days:
        return None
    prev = data.iloc[loc - days].get("usd_liquidity_score")
    cur = row.get("usd_liquidity_score")
    if pd.isna(prev) or pd.isna(cur):
        return None
    return float(cur - prev)


def latest_document(data: pd.DataFrame, source_dates: Dict[str, str]) -> Dict:
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
    return {
        "modelVersion": MODEL_VERSION,
        "generatedAt": dt.datetime.now().isoformat(timespec="seconds"),
        "asOf": row.name.date().isoformat(),
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
        "sources": {series_id: f"{BASE_URL}{series_id}" for series_id in SERIES},
        "interpretation": interpret(regime["label"], metrics),
    }


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
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")
    out_md.write_text(render_report(doc), encoding="utf-8")


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Build USD Liquidity Composite from FRED data.")
    parser.add_argument("--out-json", default=str(OUT_JSON))
    parser.add_argument("--out-md", default=str(OUT_MD))
    args = parser.parse_args(argv)

    data, source_dates = load_all_series()
    doc = latest_document(data, source_dates)
    write_outputs(doc, args.out_json, args.out_md)
    print(f"USD liquidity {doc['score']}/100 {doc['regime']['label']} as of {doc['asOf']}")
    print(Path(args.out_json).resolve())
    print(Path(args.out_md).resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

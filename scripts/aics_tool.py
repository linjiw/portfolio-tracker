#!/usr/bin/env python3
"""AICS producer for the portfolio dashboard.

AICS = AI Semiconductor Capital Flow & Scoring System.

This script keeps the existing AI-SemiQuant engine as the calculation core and
emits a richer product-facing contract for the new AICS dashboard tab:

- company/security universe;
- component scores and research gates;
- factor-decomposed supply-chain relationship edges;
- capital-flow, scenario, alert, portfolio-overlay, and model-card sections;
- a Markdown report for review outside the dashboard.

It is intentionally JSON-first so the static `generate.py` dashboard can render
the tab without a server.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Dict, Iterable, List, Optional, Tuple

import ai_semi_quant as aiq


ROOT = Path(__file__).resolve().parents[1]
OUT_JSON = ROOT / "output" / "aics.json"
OUT_MD = ROOT / "output" / "aics_report.md"
HISTORY = ROOT / "output" / "aics_history.jsonl"
DASHBOARD = ROOT / "output" / "portfolio_dashboard.html"
MODEL_VERSION = "0.1"
HISTORY_TRANSACTION_COST_BPS = 10

EDGE_COMPONENT_PROFILES = {
    "direct_platform_spend": (92, 88, 82, 85, 78),
    "custom_asic_spend": (88, 86, 84, 82, 74),
    "second_source_accelerator": (72, 70, 66, 62, 66),
    "advanced_logic_cowos": (94, 96, 92, 88, 92),
    "custom_asic_foundry": (86, 90, 86, 82, 84),
    "hbm_supply": (84, 92, 86, 82, 88),
    "advanced_node_capex": (80, 90, 88, 84, 78),
    "wfe_capex": (72, 78, 72, 70, 66),
    "yield_control_capex": (74, 82, 78, 70, 70),
    "hbm_bonder_capex": (72, 84, 80, 70, 78),
    "cowos_overflow": (70, 82, 76, 66, 82),
    "server_odm": (66, 64, 58, 60, 62),
}

EXTERNAL_NODE_NAMES = {
    "hyperscaler_capex": "Hyperscaler Capex",
    "hbm_makers": "HBM Makers",
    "advanced_packaging_shortage": "Advanced Packaging Shortage",
    "ai_rack_demand": "AI Rack Demand",
}

SCENARIOS = (
    {
        "id": "base",
        "name": "Base Case",
        "description": "AI expansion continues while CoWoS/HBM bottlenecks gradually ease.",
        "assumptions": {
            "aiCapexGrowth": "+20%",
            "cowosCapacity": "balanced-tight",
            "hbmAsp": "+10%",
            "exportControls": "stable",
        },
    },
    {
        "id": "bull",
        "name": "Bull Case",
        "description": "AI capex beats expectations and HBM/CoWoS remain scarce.",
        "assumptions": {
            "aiCapexGrowth": "+40%",
            "cowosCapacity": "tight",
            "hbmAsp": "+30%",
            "exportControls": "stable",
        },
    },
    {
        "id": "bear",
        "name": "Bear Case",
        "description": "Capex slows, memory pricing weakens, and valuation multiples compress.",
        "assumptions": {
            "aiCapexGrowth": "-10%",
            "cowosCapacity": "oversupply-risk",
            "hbmAsp": "-20%",
            "exportControls": "tighter",
        },
    },
)

SCENARIO_CONTROLS = (
    {
        "key": "aiCapexGrowth",
        "label": "AI capex growth",
        "default": "base",
        "options": (
            {"id": "down10", "label": "-10%", "scoreImpact": -6, "revenueImpact": -8, "marginImpact": -1.2, "epsImpact": -7, "valuationImpact": -5},
            {"id": "base", "label": "base", "scoreImpact": 0, "revenueImpact": 0, "marginImpact": 0, "epsImpact": 0, "valuationImpact": 0},
            {"id": "up20", "label": "+20%", "scoreImpact": 4, "revenueImpact": 8, "marginImpact": 1.0, "epsImpact": 7, "valuationImpact": 4},
            {"id": "up40", "label": "+40%", "scoreImpact": 8, "revenueImpact": 16, "marginImpact": 2.0, "epsImpact": 14, "valuationImpact": 8},
        ),
    },
    {
        "key": "cowosCapacity",
        "label": "CoWoS capacity",
        "default": "balanced",
        "options": (
            {"id": "tight", "label": "tight", "scoreImpact": 6, "revenueImpact": 5, "marginImpact": 1.6, "epsImpact": 7, "valuationImpact": 5},
            {"id": "balanced", "label": "balanced", "scoreImpact": 0, "revenueImpact": 0, "marginImpact": 0, "epsImpact": 0, "valuationImpact": 0},
            {"id": "oversupply", "label": "oversupply", "scoreImpact": -5, "revenueImpact": -4, "marginImpact": -1.5, "epsImpact": -6, "valuationImpact": -4},
        ),
    },
    {
        "key": "hbmAsp",
        "label": "HBM ASP",
        "default": "base",
        "options": (
            {"id": "down20", "label": "-20%", "scoreImpact": -8, "revenueImpact": -6, "marginImpact": -2.4, "epsImpact": -9, "valuationImpact": -5},
            {"id": "base", "label": "base", "scoreImpact": 0, "revenueImpact": 0, "marginImpact": 0, "epsImpact": 0, "valuationImpact": 0},
            {"id": "up20", "label": "+20%", "scoreImpact": 5, "revenueImpact": 7, "marginImpact": 1.8, "epsImpact": 8, "valuationImpact": 4},
            {"id": "up50", "label": "+50%", "scoreImpact": 10, "revenueImpact": 14, "marginImpact": 3.8, "epsImpact": 16, "valuationImpact": 8},
        ),
    },
    {
        "key": "samsungSf2Yield",
        "label": "Samsung SF2 yield",
        "default": "normal",
        "options": (
            {"id": "failed", "label": "failed", "scoreImpact": -5, "revenueImpact": -4, "marginImpact": -1.2, "epsImpact": -5, "valuationImpact": -3},
            {"id": "normal", "label": "normal", "scoreImpact": 0, "revenueImpact": 0, "marginImpact": 0, "epsImpact": 0, "valuationImpact": 0},
            {"id": "successful", "label": "successful", "scoreImpact": 7, "revenueImpact": 6, "marginImpact": 1.4, "epsImpact": 7, "valuationImpact": 5},
        ),
    },
    {
        "key": "intel18aWins",
        "label": "Intel 18A customer wins",
        "default": "medium",
        "options": (
            {"id": "low", "label": "low", "scoreImpact": -5, "revenueImpact": -4, "marginImpact": -1.0, "epsImpact": -5, "valuationImpact": -4},
            {"id": "medium", "label": "medium", "scoreImpact": 0, "revenueImpact": 0, "marginImpact": 0, "epsImpact": 0, "valuationImpact": 0},
            {"id": "high", "label": "high", "scoreImpact": 8, "revenueImpact": 7, "marginImpact": 1.6, "epsImpact": 8, "valuationImpact": 6},
        ),
    },
    {
        "key": "exportControls",
        "label": "Export controls",
        "default": "stable",
        "options": (
            {"id": "stable", "label": "stable", "scoreImpact": 0, "revenueImpact": 0, "marginImpact": 0, "epsImpact": 0, "valuationImpact": 0},
            {"id": "tighter", "label": "tighter", "scoreImpact": -5, "revenueImpact": -6, "marginImpact": -1.2, "epsImpact": -6, "valuationImpact": -4},
            {"id": "severe", "label": "severe", "scoreImpact": -10, "revenueImpact": -12, "marginImpact": -2.4, "epsImpact": -13, "valuationImpact": -8},
        ),
    },
    {
        "key": "usdFx",
        "label": "USD / TWD / KRW / EUR FX",
        "default": "base",
        "options": (
            {"id": "base", "label": "base", "scoreImpact": 0, "revenueImpact": 0, "marginImpact": 0, "epsImpact": 0, "valuationImpact": 0},
            {"id": "strongUsd", "label": "strong USD", "scoreImpact": -3, "revenueImpact": -2, "marginImpact": -0.6, "epsImpact": -3, "valuationImpact": -2},
            {"id": "weakUsd", "label": "weak USD", "scoreImpact": 3, "revenueImpact": 2, "marginImpact": 0.6, "epsImpact": 3, "valuationImpact": 2},
        ),
    },
)

SCENARIO_PRESET_VALUES = {
    "base": {
        "aiCapexGrowth": "up20",
        "cowosCapacity": "balanced",
        "hbmAsp": "up20",
        "samsungSf2Yield": "normal",
        "intel18aWins": "medium",
        "exportControls": "stable",
        "usdFx": "base",
    },
    "bull": {
        "aiCapexGrowth": "up40",
        "cowosCapacity": "tight",
        "hbmAsp": "up50",
        "samsungSf2Yield": "successful",
        "intel18aWins": "high",
        "exportControls": "stable",
        "usdFx": "weakUsd",
    },
    "bear": {
        "aiCapexGrowth": "down10",
        "cowosCapacity": "oversupply",
        "hbmAsp": "down20",
        "samsungSf2Yield": "failed",
        "intel18aWins": "low",
        "exportControls": "tighter",
        "usdFx": "strongUsd",
    },
}


def clamp(value: Optional[float], lo: float = 0.0, hi: float = 100.0) -> float:
    try:
        if value is None or not math.isfinite(float(value)):
            return lo
        return max(lo, min(hi, float(value)))
    except Exception:
        return lo


def rn(value: Optional[float], digits: int = 2) -> Optional[float]:
    if value is None:
        return None
    try:
        if not math.isfinite(float(value)):
            return None
        return round(float(value), digits)
    except Exception:
        return None


def pct_delta(now: Optional[float], before: Optional[float]) -> Optional[float]:
    if now is None or before is None:
        return None
    return rn(float(now) - float(before), 2)


def read_json(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def read_latest_history(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    latest = None
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                latest = json.loads(line)
            except json.JSONDecodeError:
                continue
    except OSError:
        return None
    return latest


def parse_snapshot_time(snapshot: Optional[dict]) -> Optional[dt.datetime]:
    if not snapshot:
        return None
    raw = snapshot.get("generatedAt") or snapshot.get("marketDataAsOf")
    if not raw:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.datetime.now().astimezone().tzinfo)
    return parsed


def read_history_snapshots(path: Path) -> List[dict]:
    if not path.exists():
        return []
    snapshots = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                snap = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(snap, dict):
                snapshots.append(snap)
    except OSError:
        return []
    snapshots.sort(key=lambda s: parse_snapshot_time(s) or dt.datetime.min.replace(tzinfo=dt.timezone.utc))
    return snapshots


def latest_snapshot_before(snapshots: Iterable[dict], now: dt.datetime) -> Optional[dict]:
    candidates = []
    for snap in snapshots:
        when = parse_snapshot_time(snap)
        if when and when < now:
            candidates.append((when, snap))
    if not candidates:
        return None
    return max(candidates, key=lambda x: x[0])[1]


def history_snapshot_on_or_before(snapshots: Iterable[dict], now: dt.datetime, days_back: int) -> Optional[dict]:
    target = now - dt.timedelta(days=days_back)
    candidates = []
    for snap in snapshots:
        when = parse_snapshot_time(snap)
        if when and when <= target:
            candidates.append((when, snap))
    if not candidates:
        return None
    return max(candidates, key=lambda x: x[0])[1]


def benchmark_snapshot_rows(benchmark_prices) -> List[dict]:
    rows = []
    if benchmark_prices is None:
        return rows
    for ticker in ("SOXX", "SMH"):
        try:
            if ticker not in benchmark_prices.columns:
                continue
            series = benchmark_prices[ticker].dropna()
            if len(series) == 0:
                continue
            last = float(series.iloc[-1])
            rows.append(
                {
                    "ticker": ticker,
                    "date": str(series.index[-1].date()) if hasattr(series.index[-1], "date") else str(series.index[-1])[:10],
                    "price": rn(last, 4),
                    "currency": "USD",
                }
            )
        except Exception:
            continue
    return rows


def compact_score_snapshot(generated_at: Optional[str], market_data_as_of: Optional[str], scores: List[dict], benchmarks: Optional[List[dict]] = None) -> dict:
    return {
        "generatedAt": generated_at,
        "marketDataAsOf": market_data_as_of,
        "benchmarks": list(benchmarks or []),
        "scores": [
            {
                "companyId": r.get("companyId"),
                "ticker": r.get("ticker"),
                "name": r.get("name"),
                "region": r.get("region"),
                "peerGroup": r.get("peerGroup"),
                "valueChainRole": r.get("valueChainRole"),
                "finalInvestmentScore": r.get("finalInvestmentScore"),
                "bottleneckPowerScore": r.get("bottleneckPowerScore"),
                "industrialCapitalFlowScore": r.get("industrialCapitalFlowScore"),
                "financialCapitalFlowScore": r.get("financialCapitalFlowScore"),
                "valuationScore": r.get("valuationScore"),
                "riskScore": r.get("riskScore"),
                "gate": r.get("gate"),
                "market": {
                    "date": (r.get("market") or {}).get("date"),
                    "priceUsd": (r.get("market") or {}).get("priceUsd"),
                    "price": (r.get("market") or {}).get("price"),
                    "currency": (r.get("market") or {}).get("currency"),
                    "ret1m": (r.get("market") or {}).get("ret1m"),
                    "ret3m": (r.get("market") or {}).get("ret3m"),
                    "ret6m": (r.get("market") or {}).get("ret6m"),
                    "ret1y": (r.get("market") or {}).get("ret1y"),
                },
            }
            for r in scores
        ],
    }


def append_history(path: Path, doc: dict) -> None:
    snapshot = compact_score_snapshot(doc.get("generatedAt"), doc.get("marketDataAsOf"), doc.get("scores", []), doc.get("benchmarkSnapshot") or [])
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")) + "\n")


def previous_score_maps(previous: Optional[dict]) -> Dict[str, dict]:
    out = {}
    for row in (previous or {}).get("scores") or []:
        cid = row.get("companyId") or row.get("company_id")
        ticker = row.get("ticker")
        key = cid or ticker
        if key:
            out[key] = row
    return out


def previous_edge_maps(previous: Optional[dict]) -> Dict[Tuple[str, str, str], dict]:
    out = {}
    for edge in (previous or {}).get("relationshipEdges") or []:
        key = (
            edge.get("sourceCompanyId") or edge.get("source"),
            edge.get("targetCompanyId") or edge.get("target"),
            edge.get("relationshipType") or edge.get("edgeType"),
        )
        if all(key):
            out[key] = edge
    return out


def component_scores(row: dict) -> dict:
    factors = row.get("factors") or {}
    market = row.get("market") or {}
    bottleneck = round(
        clamp(
            0.45 * factors.get("pricingPower", 50)
            + 0.30 * factors.get("capexConversion", 50)
            + 0.15 * row.get("structuralScore", 50)
            + 0.10 * factors.get("profitElasticity", 50)
        )
    )
    growth = round(clamp(0.58 * factors.get("valuationGrowth", 50) + 0.42 * factors.get("profitElasticity", 50)))
    profit_quality = round(clamp(0.70 * factors.get("profitElasticity", 50) + 0.30 * factors.get("pricingPower", 50)))
    industrial_flow = round(clamp(0.72 * factors.get("capexConversion", 50) + 0.28 * bottleneck))
    financial_flow = round(
        clamp(
            0.55 * row.get("tacticalScore", 50)
            + 0.25 * row.get("trendScore", 50)
            + 0.20 * (50 + min(max(market.get("ret3m") or 0, -80), 120) * 0.35)
        )
    )
    valuation = round(clamp(factors.get("valuationGrowth", 50)))
    momentum = round(clamp(row.get("trendScore", 50)))
    resilience = round(clamp(row.get("riskScore", 50)))
    return {
        "bottleneckPowerScore": bottleneck,
        "growthRealizationScore": growth,
        "profitQualityScore": profit_quality,
        "industrialCapitalFlowScore": industrial_flow,
        "financialCapitalFlowScore": financial_flow,
        "valuationScore": valuation,
        "momentumScore": momentum,
        "balanceSheetResilienceScore": resilience,
    }


def top_drivers(row: dict, comps: dict) -> Tuple[List[str], List[str]]:
    positives = []
    negatives = []
    gate_reasons = row.get("gateReasons") or []
    risk_breakdown = row.get("riskBreakdown") or {}
    if comps["bottleneckPowerScore"] >= 85:
        positives.append("High bottleneck power")
    if comps["industrialCapitalFlowScore"] >= 82:
        positives.append("Strong capex conversion")
    if row.get("portfolioPenalty", 0) > 0:
        negatives.append("Portfolio concentration penalty")
    if row.get("riskScore", 0) < 50:
        negatives.append("Weak risk/resilience score")
    if row.get("dataQualitySeverity") != "clean":
        negatives.append("Data quality review flag")
    if risk_breakdown.get("geopolitical") == "high":
        negatives.append("High geopolitical exposure")
    if gate_reasons:
        negatives.append(gate_reasons[0].get("rule", "gate constraint"))
    return positives[:4], negatives[:4]


def universe_row(row: dict) -> dict:
    market = row.get("market") or {}
    return {
        "companyId": row.get("companyId"),
        "name": row.get("name"),
        "ticker": row.get("ticker"),
        "aliases": list(row.get("aliases") or []),
        "market": row.get("market"),
        "region": row.get("region"),
        "country": row.get("region"),
        "valueChainRole": row.get("node"),
        "role": row.get("role"),
        "peerGroup": row.get("peerGroup"),
        "currency": market.get("currency") or market.get("marketCapCurrency") or "USD",
        "primaryListing": row.get("ticker"),
        "security": {
            "ticker": row.get("ticker"),
            "currency": market.get("currency") or "USD",
            "listingType": "ADR/local normalized" if row.get("aliases") else "primary",
            "marketCapUsd": market.get("marketCapUsd"),
            "liquidityBucket": market.get("marketCapBucket"),
        },
        "coreOrExplainer": "core",
    }


def rank_maps(rows: List[dict]) -> Tuple[Dict[str, int], Dict[str, int]]:
    quality = {
        r["companyId"]: i + 1
        for i, r in enumerate(sorted(rows, key=lambda x: (-x.get("structuralScore", 0), -x.get("finalScore", 0), x.get("ticker", ""))))
    }
    attractiveness = {
        r["companyId"]: i + 1
        for i, r in enumerate(sorted(rows, key=lambda x: (-x.get("finalScore", 0), -x.get("tacticalScore", 0), x.get("ticker", ""))))
    }
    return quality, attractiveness


def score_row(
    row: dict,
    quality_rank: int,
    attractiveness_rank: int,
    previous: Optional[dict],
    previous_1w: Optional[dict] = None,
    previous_1m: Optional[dict] = None,
) -> dict:
    comps = component_scores(row)
    positives, negatives = top_drivers(row, comps)
    prev_score = None
    prev_risk = None
    if previous:
        prev_score = previous.get("finalInvestmentScore", previous.get("finalScore"))
        prev_risk = previous.get("riskScore")
    prev_1w_score = previous_1w.get("finalInvestmentScore", previous_1w.get("finalScore")) if previous_1w else None
    prev_1m_score = previous_1m.get("finalInvestmentScore", previous_1m.get("finalScore")) if previous_1m else None
    prev_1w_risk = previous_1w.get("riskScore") if previous_1w else None
    prev_1m_risk = previous_1m.get("riskScore") if previous_1m else None
    prev_industrial_flow = previous.get("industrialCapitalFlowScore") if previous else None
    prev_financial_flow = previous.get("financialCapitalFlowScore") if previous else None
    prev_1w_industrial_flow = previous_1w.get("industrialCapitalFlowScore") if previous_1w else None
    prev_1w_financial_flow = previous_1w.get("financialCapitalFlowScore") if previous_1w else None
    prev_1m_industrial_flow = previous_1m.get("industrialCapitalFlowScore") if previous_1m else None
    prev_1m_financial_flow = previous_1m.get("financialCapitalFlowScore") if previous_1m else None
    market = row.get("market") or {}
    return {
        "date": market.get("date"),
        "companyId": row.get("companyId"),
        "ticker": row.get("ticker"),
        "name": row.get("name"),
        "region": row.get("region"),
        "peerGroup": row.get("peerGroup"),
        "valueChainRole": row.get("node"),
        "role": row.get("role"),
        "thesis": row.get("thesis"),
        **comps,
        "baseScore": row.get("torqueAdjustedScore"),
        "riskPenalty": row.get("riskPenalty"),
        "portfolioPenalty": row.get("portfolioPenalty"),
        "catalystBonus": row.get("torqueBonus"),
        "structuralScore": row.get("structuralScore"),
        "trendScore": row.get("trendScore"),
        "riskScore": row.get("riskScore"),
        "tacticalScore": row.get("tacticalScore"),
        "finalInvestmentScore": row.get("finalScore"),
        "companyQualityRank": quality_rank,
        "stockAttractivenessRank": attractiveness_rank,
        "gate": row.get("gate"),
        "gateFamily": row.get("gateFamily"),
        "gateNote": row.get("gateNote"),
        "gateReasons": row.get("gateReasons") or [],
        "topPositiveDrivers": positives,
        "topNegativeDrivers": negatives,
        "sourceConfidence": row.get("dataQualityScore"),
        "dataQualityScore": row.get("dataQualityScore"),
        "dataQualitySeverity": row.get("dataQualitySeverity"),
        "dataQualityReasons": row.get("dataQualityReasons") or [],
        "riskBreakdown": row.get("riskBreakdown") or {},
        "market": market,
        "portfolio": row.get("portfolio") or {},
        "factors": row.get("factors") or {},
        "peerGroupSize": row.get("peerGroupSize"),
        "universePercentile": row.get("universePercentile"),
        "peerPercentile": row.get("peerPercentile"),
        "peerPercentileDisplay": row.get("peerPercentileDisplay"),
        "scoreChange1Run": pct_delta(row.get("finalScore"), prev_score),
        "riskScoreChange1Run": pct_delta(row.get("riskScore"), prev_risk),
        "scoreChange1W": pct_delta(row.get("finalScore"), prev_1w_score),
        "scoreChange1M": pct_delta(row.get("finalScore"), prev_1m_score),
        "riskScoreChange1W": pct_delta(row.get("riskScore"), prev_1w_risk),
        "riskScoreChange1M": pct_delta(row.get("riskScore"), prev_1m_risk),
        "industrialCapitalFlowChange1Run": pct_delta(comps["industrialCapitalFlowScore"], prev_industrial_flow),
        "industrialCapitalFlowChange1W": pct_delta(comps["industrialCapitalFlowScore"], prev_1w_industrial_flow),
        "industrialCapitalFlowChange1M": pct_delta(comps["industrialCapitalFlowScore"], prev_1m_industrial_flow),
        "financialCapitalFlowChange1Run": pct_delta(comps["financialCapitalFlowScore"], prev_financial_flow),
        "financialCapitalFlowChange1W": pct_delta(comps["financialCapitalFlowScore"], prev_1w_financial_flow),
        "financialCapitalFlowChange1M": pct_delta(comps["financialCapitalFlowScore"], prev_1m_financial_flow),
    }


def relationship_edges(base_rows: List[dict], previous_edges: Optional[Dict[Tuple[str, str, str], dict]] = None) -> List[dict]:
    company_by_id = {r.get("companyId"): r for r in base_rows}
    edges = []
    for edge in aiq.CAPITAL_FLOW_EDGES:
        profile = EDGE_COMPONENT_PROFILES.get(edge["edgeType"], (edge["weight"],) * 5)
        source_id = edge["source"]
        target_id = edge["target"]
        source = company_by_id.get(source_id)
        target = company_by_id.get(target_id)
        previous = (previous_edges or {}).get((source_id, target_id, edge["edgeType"])) or {}
        edges.append(
            {
                "date": None,
                "sourceCompanyId": source_id,
                "targetCompanyId": target_id,
                "sourceName": (source or {}).get("name") or EXTERNAL_NODE_NAMES.get(source_id, source_id),
                "targetName": (target or {}).get("name") or target_id,
                "sourceTicker": (source or {}).get("ticker"),
                "targetTicker": (target or {}).get("ticker"),
                "relationshipType": edge["edgeType"],
                "edgeWeight": edge["weight"],
                "revenueCorrelation": profile[0],
                "technicalDependency": profile[1],
                "substitutionDifficulty": profile[2],
                "orderVisibility": profile[3],
                "capacityTightness": profile[4],
                "evidenceType": "curated_prior",
                "sourceConfidence": round(edge.get("confidence", 0.5) * 100),
                "edgeWeightChange1Run": pct_delta(edge["weight"], previous.get("edgeWeight")),
            }
        )
    return edges


def capital_flow_summary(scores: List[dict], edges: List[dict]) -> dict:
    by_role = {}
    for row in scores:
        role = row.get("peerGroup") or "Other"
        bucket = by_role.setdefault(role, {"role": role, "count": 0, "avgIndustrialFlow": 0.0, "avgFinancialFlow": 0.0, "leaders": []})
        bucket["count"] += 1
        bucket["avgIndustrialFlow"] += row.get("industrialCapitalFlowScore") or 0
        bucket["avgFinancialFlow"] += row.get("financialCapitalFlowScore") or 0
        if len(bucket["leaders"]) < 3:
            bucket["leaders"].append(row["ticker"])
    for bucket in by_role.values():
        bucket["avgIndustrialFlow"] = rn(bucket["avgIndustrialFlow"] / bucket["count"], 1)
        bucket["avgFinancialFlow"] = rn(bucket["avgFinancialFlow"] / bucket["count"], 1)
    return {
        "industrial": sorted(by_role.values(), key=lambda x: -(x["avgIndustrialFlow"] or 0)),
        "financial": sorted(by_role.values(), key=lambda x: -(x["avgFinancialFlow"] or 0)),
        "strongestEdges": sorted(edges, key=lambda e: (-e["edgeWeight"], -e["sourceConfidence"]))[:8],
        "crowdingWatch": [
            {
                "ticker": r["ticker"],
                "score": r["financialCapitalFlowScore"],
                "riskScore": r["riskScore"],
                "gate": r["gate"],
            }
            for r in scores
            if (r.get("financialCapitalFlowScore") or 0) >= 78 and (r.get("riskScore") or 100) < 60
        ][:8],
    }


def portfolio_overlay(scores: List[dict]) -> dict:
    held = [r for r in scores if (r.get("portfolio") or {}).get("held")]
    total_weight = sum((r.get("portfolio") or {}).get("weightPct") or 0 for r in held)
    by_role = {}
    for row in held:
        role = row.get("peerGroup") or "Other"
        by_role[role] = by_role.get(role, 0.0) + ((row.get("portfolio") or {}).get("weightPct") or 0.0)
    return {
        "heldCount": len(held),
        "totalAicsWeightPct": rn(total_weight, 2),
        "byRole": [{"role": k, "weightPct": rn(v, 2)} for k, v in sorted(by_role.items(), key=lambda kv: -kv[1])],
        "largestHeld": [
            {
                "ticker": r["ticker"],
                "weightPct": rn((r.get("portfolio") or {}).get("weightPct"), 2),
                "gate": r["gate"],
                "finalInvestmentScore": r["finalInvestmentScore"],
            }
            for r in sorted(held, key=lambda x: -((x.get("portfolio") or {}).get("weightPct") or 0))[:8]
        ],
        "concentrationWarning": total_weight >= 35,
        "note": "AICS overlap uses dashboard holdings matched to core AI semiconductor tickers and aliases.",
    }


def scenario_control_map() -> Dict[str, dict]:
    return {control["key"]: control for control in SCENARIO_CONTROLS}


def default_scenario_assumptions() -> Dict[str, str]:
    return {control["key"]: control["default"] for control in SCENARIO_CONTROLS}


def scenario_option(control_key: str, option_id: Optional[str]) -> dict:
    control = scenario_control_map().get(control_key) or {}
    options = {option["id"]: option for option in control.get("options") or ()}
    return options.get(option_id) or options.get(control.get("default")) or {}


def scenario_assumptions_for(scenario_id: str) -> Dict[str, str]:
    assumptions = default_scenario_assumptions()
    assumptions.update(SCENARIO_PRESET_VALUES.get(scenario_id) or {})
    return assumptions


def scenario_sensitivities(row: dict) -> Dict[str, float]:
    peer = (row.get("peerGroup") or "").lower()
    role = (row.get("valueChainRole") or "").lower()
    ticker = (row.get("ticker") or "").upper()
    company_id = (row.get("companyId") or "").lower()
    sens = {control["key"]: 0.0 for control in SCENARIO_CONTROLS}
    sens.update({"aiCapexGrowth": 0.25, "exportControls": 0.10})

    if "accelerator" in peer or "asic" in peer or "networking" in role:
        sens.update({"aiCapexGrowth": 0.90, "cowosCapacity": -0.35, "hbmAsp": -0.20, "exportControls": 0.45})
    if "foundry" in peer:
        sens.update({"aiCapexGrowth": 0.65, "cowosCapacity": 0.75, "exportControls": 0.35})
    if "memory" in peer:
        sens.update({"aiCapexGrowth": 0.55, "cowosCapacity": 0.15, "hbmAsp": 1.00, "exportControls": 0.25})
    if "packaging" in peer:
        sens.update({"aiCapexGrowth": 0.55, "cowosCapacity": 1.00, "exportControls": 0.15})
    if "equipment" in peer:
        sens.update({"aiCapexGrowth": 0.45, "exportControls": 0.60, "samsungSf2Yield": 0.15, "intel18aWins": 0.20})
    if "server odm" in peer:
        sens.update({"aiCapexGrowth": 0.50, "cowosCapacity": -0.15, "hbmAsp": -0.20, "exportControls": 0.25})

    if ticker in {"005930.KS", "SSNLF"} or "samsung" in company_id:
        sens["samsungSf2Yield"] = 1.00
        sens["usdFx"] = 0.35
    if ticker in {"TSM", "2330.TW"} or "tsmc" in company_id:
        sens["samsungSf2Yield"] = -0.20
        sens["intel18aWins"] = -0.15
        sens["usdFx"] = 0.30
    if ticker == "INTC" or "intel" in company_id:
        sens["intel18aWins"] = 1.00
    if ticker in {"AMD", "AVGO", "NVDA", "QCOM"}:
        sens["samsungSf2Yield"] = max(sens["samsungSf2Yield"], 0.10)
        sens["intel18aWins"] = max(sens["intel18aWins"], 0.05)
    if ticker in {"ASML", "ASML.AS"} or "asml" in company_id:
        sens["usdFx"] = 0.35
        sens["exportControls"] = 0.70
    if ticker.endswith(".TW") or ticker.endswith(".KS") or ticker.endswith(".AS") or ticker.endswith(".T"):
        sens["usdFx"] = max(sens["usdFx"], 0.30)

    return {key: rn(value, 2) for key, value in sens.items()}


def scenario_gate(row: dict, scenario_score: float) -> str:
    current = row.get("gate") or "WATCH"
    if current in ("DATA_REVIEW", "PORTFOLIO_BLOCK"):
        return current
    risk = row.get("riskScore") or 0
    if risk < 35:
        return "BLOCK"
    if scenario_score >= 82 and risk >= 45:
        return "ALLOW_DD"
    if scenario_score >= 70:
        return "WATCH"
    if scenario_score >= 62:
        return "WATCH_RESET"
    return "BLOCK"


def evaluate_custom_scenario(scores: List[dict], assumptions: Optional[Dict[str, str]] = None) -> dict:
    selected = default_scenario_assumptions()
    selected.update(assumptions or {})
    rows = []
    portfolio_weight = 0.0
    portfolio_score = 0.0
    portfolio_revenue = 0.0
    portfolio_eps = 0.0
    portfolio_valuation = 0.0
    held_rows = []

    for row in scores:
        sensitivities = scenario_sensitivities(row)
        impacts = {"scoreDelta": 0.0, "revenueImpactPct": 0.0, "marginImpactPct": 0.0, "epsImpactPct": 0.0, "valuationImpactPct": 0.0}
        for key, sensitivity in sensitivities.items():
            option = scenario_option(key, selected.get(key))
            impacts["scoreDelta"] += sensitivity * (option.get("scoreImpact") or 0)
            impacts["revenueImpactPct"] += sensitivity * (option.get("revenueImpact") or 0)
            impacts["marginImpactPct"] += sensitivity * (option.get("marginImpact") or 0)
            impacts["epsImpactPct"] += sensitivity * (option.get("epsImpact") or 0)
            impacts["valuationImpactPct"] += sensitivity * (option.get("valuationImpact") or 0)

        base_score = row.get("finalInvestmentScore") or 0
        scenario_score = round(clamp(base_score + impacts["scoreDelta"]))
        out = {
            "companyId": row.get("companyId"),
            "ticker": row["ticker"],
            "name": row.get("name"),
            "baseScore": base_score,
            "scoreDelta": rn(impacts["scoreDelta"], 1),
            "scenarioScore": scenario_score,
            "revenueImpactPct": rn(impacts["revenueImpactPct"], 1),
            "marginImpactPct": rn(impacts["marginImpactPct"], 1),
            "epsImpactPct": rn(impacts["epsImpactPct"], 1),
            "valuationImpactPct": rn(impacts["valuationImpactPct"], 1),
            "gate": row.get("gate"),
            "newGate": scenario_gate(row, scenario_score),
            "portfolioWeightPct": rn((row.get("portfolio") or {}).get("weightPct"), 2),
        }
        rows.append(out)

        weight = (row.get("portfolio") or {}).get("weightPct") or 0.0
        if weight > 0:
            portfolio_weight += weight
            portfolio_score += impacts["scoreDelta"] * weight
            portfolio_revenue += impacts["revenueImpactPct"] * weight
            portfolio_eps += impacts["epsImpactPct"] * weight
            portfolio_valuation += impacts["valuationImpactPct"] * weight
            held_rows.append({"ticker": row["ticker"], "weightPct": rn(weight, 2), "scoreDelta": out["scoreDelta"], "newGate": out["newGate"]})

    winners = sorted(rows, key=lambda x: (-(x["scoreDelta"] or 0), -(x["scenarioScore"] or 0), x["ticker"]))[:6]
    losers = sorted(rows, key=lambda x: ((x["scoreDelta"] or 0), (x["scenarioScore"] or 0), x["ticker"]))[:6]
    denom = portfolio_weight or 1.0
    return {
        "assumptions": selected,
        "rows": rows,
        "winners": winners,
        "losers": losers,
        "portfolioImpact": {
            "heldWeightPct": rn(portfolio_weight, 2),
            "scoreDelta": rn(portfolio_score / denom, 2) if portfolio_weight else None,
            "revenueImpactPct": rn(portfolio_revenue / denom, 2) if portfolio_weight else None,
            "epsImpactPct": rn(portfolio_eps / denom, 2) if portfolio_weight else None,
            "valuationImpactPct": rn(portfolio_valuation / denom, 2) if portfolio_weight else None,
            "heldRows": sorted(held_rows, key=lambda x: -((x.get("weightPct") or 0)))[:8],
            "method": "Portfolio impact is current-weighted average factor sensitivity, not a return forecast.",
        },
        "method": "Scenario impacts multiply selected assumption shocks by curated company sensitivity coefficients.",
    }


def scenario_model(scores: List[dict]) -> dict:
    default_assumptions = default_scenario_assumptions()
    return {
        "version": MODEL_VERSION,
        "method": "Deterministic assumption-shock model using curated sensitivity coefficients. Revenue, margin, EPS, valuation, and score outputs are directional MVP proxies.",
        "controls": list(SCENARIO_CONTROLS),
        "defaultAssumptions": default_assumptions,
        "outputs": ["revenueImpactPct", "marginImpactPct", "epsImpactPct", "scoreDelta", "valuationImpactPct", "portfolioImpact", "newGate", "winners", "losers"],
        "companySensitivities": [
            {
                "companyId": row.get("companyId"),
                "ticker": row["ticker"],
                "baseScore": row.get("finalInvestmentScore"),
                "portfolioWeightPct": rn((row.get("portfolio") or {}).get("weightPct"), 2),
                "sensitivities": scenario_sensitivities(row),
            }
            for row in scores
        ],
        "defaultRun": evaluate_custom_scenario(scores, default_assumptions),
    }


def scenario_results(scores: List[dict]) -> List[dict]:
    results = []
    for scenario in SCENARIOS:
        assumptions = scenario_assumptions_for(scenario["id"])
        run = evaluate_custom_scenario(scores, assumptions)
        results.append({**scenario, "controlValues": assumptions, "winners": run["winners"], "losers": run["losers"], "portfolioImpact": run["portfolioImpact"]})
    return results


def return_attribution(scores: List[dict]) -> List[dict]:
    rows = []
    for row in scores:
        market = row.get("market") or {}
        ret = market.get("ret3m")
        if ret is None:
            rows.append({"ticker": row["ticker"], "window": "3M", "available": False})
            continue
        growth_weight = clamp(row.get("growthRealizationScore"), 0, 100) / 100
        valuation_weight = clamp(row.get("valuationScore"), 0, 100) / 100
        flow_weight = clamp(row.get("financialCapitalFlowScore"), 0, 100) / 100
        raw = [0.30 + growth_weight * 0.20, 0.18 + valuation_weight * 0.15, 0.26 + flow_weight * 0.20]
        denom = sum(raw)
        earnings = ret * raw[0] / denom
        multiple = ret * raw[1] / denom
        flow = ret * raw[2] / denom
        residual = ret - earnings - multiple - flow
        rows.append(
            {
                "ticker": row["ticker"],
                "window": "3M",
                "available": True,
                "totalReturn": rn(ret),
                "earningsRevisionContribution": rn(earnings),
                "valuationMultipleContribution": rn(multiple),
                "capitalFlowMomentumContribution": rn(flow),
                "fxDividendResidualContribution": rn(residual),
                "qualityFlag": "low_quality_rally"
                if ret > 20 and row.get("valuationScore", 0) < 55 and row.get("growthRealizationScore", 0) < 70
                else "normal",
                "method": "proxy decomposition from growth, valuation, and financial-flow scores; replace with estimate data when available",
            }
        )
    return rows


def return_attribution_summary(attribution: List[dict]) -> dict:
    available = [row for row in attribution if row.get("available")]

    def avg_key(key: str) -> Optional[float]:
        vals = [row.get(key) for row in available if row.get(key) is not None]
        return rn(sum(vals) / len(vals), 2) if vals else None

    return {
        "window": "3M",
        "availableCount": len(available),
        "lowQualityRallies": [
            {
                "ticker": row["ticker"],
                "totalReturn": row.get("totalReturn"),
                "earningsRevisionContribution": row.get("earningsRevisionContribution"),
                "valuationMultipleContribution": row.get("valuationMultipleContribution"),
                "capitalFlowMomentumContribution": row.get("capitalFlowMomentumContribution"),
            }
            for row in sorted(
                [r for r in available if r.get("qualityFlag") == "low_quality_rally"],
                key=lambda r: (-(r.get("totalReturn") or 0), r["ticker"]),
            )[:8]
        ],
        "topTotalReturn": sorted(available, key=lambda r: (-(r.get("totalReturn") or 0), r["ticker"]))[:8],
        "worstTotalReturn": sorted(available, key=lambda r: ((r.get("totalReturn") or 0), r["ticker"]))[:8],
        "largestResiduals": sorted(available, key=lambda r: (-abs(r.get("fxDividendResidualContribution") or 0), r["ticker"]))[:8],
        "averageContribution": {
            "earningsRevisionContribution": avg_key("earningsRevisionContribution"),
            "valuationMultipleContribution": avg_key("valuationMultipleContribution"),
            "capitalFlowMomentumContribution": avg_key("capitalFlowMomentumContribution"),
            "fxDividendResidualContribution": avg_key("fxDividendResidualContribution"),
        },
        "method": "3M proxy attribution. Replace with EPS-estimate, multiple, FX, ADR/local, and dividend feeds as data quality improves.",
    }


def first_available_delta(row: dict, keys: Tuple[str, ...]) -> Tuple[Optional[float], Optional[str]]:
    for key in keys:
        value = row.get(key)
        if value is not None:
            return value, key
    return None, None


def delta_label(key: Optional[str]) -> str:
    return {
        "scoreChange1W": "one week",
        "scoreChange1M": "one month",
        "scoreChange1Run": "the prior AICS run",
        "riskScoreChange1W": "one week",
        "riskScoreChange1M": "one month",
        "riskScoreChange1Run": "the prior AICS run",
        "industrialCapitalFlowChange1W": "one week",
        "industrialCapitalFlowChange1M": "one month",
        "industrialCapitalFlowChange1Run": "the prior AICS run",
        "financialCapitalFlowChange1W": "one week",
        "financialCapitalFlowChange1M": "one month",
        "financialCapitalFlowChange1Run": "the prior AICS run",
    }.get(key or "", "the selected comparison window")


def percentile_threshold(values: List[float], percentile: float) -> Optional[float]:
    cleaned = sorted(float(v) for v in values if v is not None and math.isfinite(float(v)))
    if not cleaned:
        return None
    if len(cleaned) == 1:
        return cleaned[0]
    idx = min(len(cleaned) - 1, max(0, math.ceil((percentile / 100.0) * len(cleaned)) - 1))
    return cleaned[idx]


def flow_turn_alert(row: dict) -> Optional[dict]:
    candidates = (
        ("financialCapitalFlowScore", ("financialCapitalFlowChange1W", "financialCapitalFlowChange1M", "financialCapitalFlowChange1Run"), "financial capital-flow"),
        ("industrialCapitalFlowScore", ("industrialCapitalFlowChange1W", "industrialCapitalFlowChange1M", "industrialCapitalFlowChange1Run"), "industrial capital-flow"),
    )
    for score_key, delta_keys, label in candidates:
        delta, delta_key = first_available_delta(row, delta_keys)
        current = row.get(score_key)
        if current is None or delta is None:
            continue
        previous = current - delta
        if previous < 50 <= current or (previous < 60 <= current and delta >= 8):
            return {
                "type": "capital_flow_turn_positive",
                "severity": "medium",
                "ticker": row["ticker"],
                "title": f"{row['ticker']} {label} turned positive",
                "detail": f"{label.capitalize()} moved from {previous:.1f} to {current:.1f} versus {delta_label(delta_key)}.",
                "metric": score_key,
                "value": rn(current, 1),
                "threshold": 50,
            }
    return None


def industry_threshold_alerts(scores: List[dict], edges: List[dict]) -> List[dict]:
    out = []
    rows_by_peer: Dict[str, List[dict]] = {}
    for row in scores:
        rows_by_peer.setdefault(row.get("peerGroup") or "Other", []).append(row)

    def avg(rows: List[dict], key: str) -> Optional[float]:
        vals = [r.get(key) for r in rows if r.get(key) is not None]
        return rn(sum(vals) / len(vals), 1) if vals else None

    cowos_edges = [e for e in edges if e.get("relationshipType") in ("advanced_logic_cowos", "cowos_overflow")]
    tight_cowos = [e for e in cowos_edges if (e.get("capacityTightness") or 0) >= 82 and (e.get("edgeWeight") or 0) >= 70]
    if tight_cowos:
        out.append(
            {
                "type": "industry_threshold",
                "subtype": "cowos",
                "severity": "medium",
                "title": "CoWoS bottleneck remains active",
                "detail": f"{len(tight_cowos)} advanced-packaging edges have capacity tightness above threshold.",
                "metric": "capacityTightness",
                "value": max(e.get("capacityTightness") or 0 for e in tight_cowos),
                "threshold": 82,
            }
        )

    memory_rows = [r for peer, rows in rows_by_peer.items() if "memory" in peer.lower() for r in rows]
    memory_flow = avg(memory_rows, "industrialCapitalFlowScore")
    if memory_flow is not None and memory_flow >= 80:
        out.append(
            {
                "type": "industry_threshold",
                "subtype": "hbm_dram",
                "severity": "medium",
                "title": "HBM / DRAM capital-flow threshold triggered",
                "detail": f"Memory peer-group average industrial flow is {memory_flow}.",
                "metric": "memoryAvgIndustrialFlow",
                "value": memory_flow,
                "threshold": 80,
            }
        )

    equipment_rows = [r for peer, rows in rows_by_peer.items() if "equipment" in peer.lower() for r in rows]
    equipment_flow = avg(equipment_rows, "industrialCapitalFlowScore")
    if equipment_flow is not None and equipment_flow >= 82:
        out.append(
            {
                "type": "industry_threshold",
                "subtype": "wfe",
                "severity": "medium",
                "title": "WFE capex threshold triggered",
                "detail": f"Equipment peer-group average industrial flow is {equipment_flow}.",
                "metric": "equipmentAvgIndustrialFlow",
                "value": equipment_flow,
                "threshold": 82,
            }
        )

    export_rows = [
        r
        for r in scores
        if (r.get("riskBreakdown") or {}).get("geopolitical") in ("high", "medium-high")
        and (scenario_sensitivities(r).get("exportControls") or 0) >= 0.35
    ]
    if export_rows:
        out.append(
            {
                "type": "industry_threshold",
                "subtype": "export_controls",
                "severity": "medium",
                "title": "Export-control sensitivity threshold triggered",
                "detail": "High geopolitical exposure intersects with export-control sensitivity for "
                + ", ".join(r["ticker"] for r in export_rows[:6])
                + ".",
                "metric": "exportControlSensitiveNames",
                "value": len(export_rows),
                "threshold": 1,
            }
        )
    return out


def alerts(scores: List[dict], overlay: dict, edges: Optional[List[dict]] = None, attribution: Optional[List[dict]] = None) -> List[dict]:
    out = []
    if overlay.get("concentrationWarning"):
        out.append(
            {
                "type": "portfolio_concentration",
                "severity": "high",
                "title": "AICS exposure is concentrated",
                "detail": f"Core AI semiconductor exposure is {overlay.get('totalAicsWeightPct')}% of the portfolio.",
            }
        )
    for row in scores:
        if row.get("dataQualitySeverity") == "hard_review" or row.get("gate") == "DATA_REVIEW":
            out.append(
                {
                    "type": "data_quality",
                    "severity": "high",
                    "ticker": row["ticker"],
                    "title": f"{row['ticker']} needs data review",
                    "detail": ((row.get("dataQualityReasons") or [{}])[0]).get("detail", "Data quality score is below threshold."),
                }
            )
        score_delta, score_key = first_available_delta(row, ("scoreChange1W", "scoreChange1M", "scoreChange1Run"))
        risk_delta, risk_key = first_available_delta(row, ("riskScoreChange1W", "riskScoreChange1M", "riskScoreChange1Run"))
        if score_delta is not None and score_delta >= 10:
            out.append(
                {
                    "type": "score_up",
                    "severity": "medium",
                    "ticker": row["ticker"],
                    "title": f"{row['ticker']} score improved",
                    "detail": f"Final investment score improved {score_delta:+.1f} points versus {delta_label(score_key)}.",
                    "metric": "finalInvestmentScore",
                    "value": row.get("finalInvestmentScore"),
                    "threshold": "+10",
                }
            )
        if score_delta is not None and score_delta <= -10:
            out.append(
                {
                    "type": "score_down",
                    "severity": "medium",
                    "ticker": row["ticker"],
                    "title": f"{row['ticker']} score deteriorated",
                    "detail": f"Final investment score changed {score_delta:+.1f} points versus {delta_label(score_key)}.",
                    "metric": "finalInvestmentScore",
                    "value": row.get("finalInvestmentScore"),
                    "threshold": "-10",
                }
            )
        if risk_delta is not None and risk_delta <= -10:
            out.append(
                {
                    "type": "risk_deterioration",
                    "severity": "medium",
                    "ticker": row["ticker"],
                    "title": f"{row['ticker']} risk score deteriorated",
                    "detail": f"Risk score changed {risk_delta:+.1f} points versus {delta_label(risk_key)}.",
                    "metric": "riskScore",
                    "value": row.get("riskScore"),
                    "threshold": "-10",
                }
            )
        flow_alert = flow_turn_alert(row)
        if flow_alert:
            out.append(flow_alert)
        if row.get("gate") == "WATCH_RESET":
            reason = ((row.get("gateReasons") or [{}])[0]).get("detail", row.get("gateNote", "Wait for reset."))
            out.append(
                {
                    "type": "wait_for_reset",
                    "severity": "medium",
                    "ticker": row["ticker"],
                    "title": f"{row['ticker']} requires reset",
                    "detail": reason,
                }
            )

    valuation_threshold = percentile_threshold([r.get("valuationScore") for r in scores], 95)
    if valuation_threshold is not None:
        valuation_leaders = [r for r in scores if (r.get("valuationScore") or 0) >= valuation_threshold and (r.get("valuationScore") or 0) >= 90]
        for row in sorted(valuation_leaders, key=lambda r: (-(r.get("valuationScore") or 0), r["ticker"]))[:3]:
            out.append(
                {
                    "type": "valuation_percentile",
                    "severity": "low",
                    "ticker": row["ticker"],
                    "title": f"{row['ticker']} valuation score is in the top percentile",
                    "detail": f"Valuation score {row.get('valuationScore')} is at or above the current 95th-percentile threshold {valuation_threshold:.1f}.",
                    "metric": "valuationScore",
                    "value": row.get("valuationScore"),
                    "threshold": rn(valuation_threshold, 1),
                }
            )

    attribution_by_ticker = {row.get("ticker"): row for row in attribution or []}
    for row in scores:
        attr = attribution_by_ticker.get(row["ticker"]) or {}
        if attr.get("qualityFlag") == "low_quality_rally":
            out.append(
                {
                    "type": "low_quality_rally",
                    "severity": "medium",
                    "ticker": row["ticker"],
                    "title": f"{row['ticker']} rally quality is weak",
                    "detail": "3M price return is positive while growth/valuation support is weak in the current proxy model.",
                    "metric": "returnAttribution.qualityFlag",
                    "value": attr.get("totalReturn"),
                    "threshold": "low_quality_rally",
                }
            )

    for edge in edges or []:
        change = edge.get("edgeWeightChange1Run")
        if change is not None and abs(change) >= 10:
            out.append(
                {
                    "type": "edge_weight_change",
                    "severity": "medium",
                    "title": f"{edge.get('sourceName')} → {edge.get('targetName')} edge changed",
                    "detail": f"Edge weight changed {change:+.1f} points versus the prior AICS run.",
                    "metric": "edgeWeight",
                    "value": edge.get("edgeWeight"),
                    "threshold": "±10",
                }
            )

    out.extend(industry_threshold_alerts(scores, edges or []))
    priority = {"high": 0, "medium": 1, "low": 2}
    return sorted(out, key=lambda a: (priority.get(a.get("severity"), 3), a.get("type") or "", a.get("ticker") or "", a.get("title") or ""))[:32]


def score_deltas(scores: List[dict]) -> List[dict]:
    return [
        {
            "companyId": r["companyId"],
            "ticker": r["ticker"],
            "scoreChange1Run": r.get("scoreChange1Run"),
            "scoreChange1W": r.get("scoreChange1W"),
            "scoreChange1M": r.get("scoreChange1M"),
            "riskScoreChange1Run": r.get("riskScoreChange1Run"),
            "riskScoreChange1W": r.get("riskScoreChange1W"),
            "riskScoreChange1M": r.get("riskScoreChange1M"),
            "industrialCapitalFlowChange1Run": r.get("industrialCapitalFlowChange1Run"),
            "industrialCapitalFlowChange1W": r.get("industrialCapitalFlowChange1W"),
            "industrialCapitalFlowChange1M": r.get("industrialCapitalFlowChange1M"),
            "financialCapitalFlowChange1Run": r.get("financialCapitalFlowChange1Run"),
            "financialCapitalFlowChange1W": r.get("financialCapitalFlowChange1W"),
            "financialCapitalFlowChange1M": r.get("financialCapitalFlowChange1M"),
        }
        for r in scores
    ]


BACKTEST_WINDOWS = (
    ("1M", "ret1m"),
    ("3M", "ret3m"),
    ("6M", "ret6m"),
    ("1Y", "ret1y"),
)


def avg_window_return(rows: List[dict], return_key: str) -> Optional[float]:
    vals = []
    for row in rows:
        market = row.get("market") or {}
        value = market.get(return_key)
        if value is not None:
            try:
                vals.append(float(value))
            except Exception:
                pass
    if not vals:
        return None
    return rn(sum(vals) / len(vals), 2)


def benchmark_window_returns(price_data) -> Dict[str, Dict[str, Optional[float]]]:
    out: Dict[str, Dict[str, Optional[float]]] = {}
    if price_data is None:
        return out
    day_map = {"1M": 21, "3M": 63, "6M": 126, "1Y": 252}
    for ticker in ("SOXX", "SMH"):
        if ticker not in getattr(price_data, "columns", []):
            continue
        series = price_data[ticker].dropna()
        out[ticker] = {}
        for label, days in day_map.items():
            out[ticker][label] = rn(aiq.pct_change(series, days), 2)
    return out


def basket_result(name: str, rows: List[dict], benchmark_returns: Dict[str, Dict[str, Optional[float]]]) -> dict:
    windows = {}
    for label, key in BACKTEST_WINDOWS:
        ret = avg_window_return(rows, key)
        windows[label] = {
            "returnPct": ret,
            "memberCount": sum(1 for row in rows if (row.get("market") or {}).get(key) is not None),
            "excessVsSOXX": rn(ret - benchmark_returns.get("SOXX", {}).get(label), 2)
            if ret is not None and benchmark_returns.get("SOXX", {}).get(label) is not None
            else None,
            "excessVsSMH": rn(ret - benchmark_returns.get("SMH", {}).get(label), 2)
            if ret is not None and benchmark_returns.get("SMH", {}).get(label) is not None
            else None,
        }
    return {
        "name": name,
        "members": [r["ticker"] for r in rows],
        "memberCount": len(rows),
        "windows": windows,
    }


def snapshot_row_price(row: dict) -> Optional[float]:
    market = row.get("market") or {}
    for key in ("priceUsd", "price", "close", "last"):
        value = market.get(key, row.get(key))
        try:
            if value is not None and math.isfinite(float(value)) and float(value) > 0:
                return float(value)
        except Exception:
            pass
    return None


def snapshot_rows(snapshot: dict) -> List[dict]:
    rows = []
    for row in snapshot.get("scores") or []:
        if isinstance(row, dict) and row.get("ticker"):
            rows.append(row)
    return rows


def snapshot_row_maps(snapshot: dict) -> Dict[str, dict]:
    out = {}
    for row in snapshot_rows(snapshot):
        if row.get("ticker"):
            out[row["ticker"]] = row
        if row.get("companyId"):
            out[row["companyId"]] = row
    return out


def benchmark_price_map(snapshot: dict) -> Dict[str, float]:
    out = {}
    for row in snapshot.get("benchmarks") or []:
        ticker = row.get("ticker")
        try:
            price = float(row.get("price"))
        except Exception:
            continue
        if ticker and math.isfinite(price) and price > 0:
            out[ticker] = price
    return out


def benchmark_pair_returns(start: dict, end: dict) -> Dict[str, float]:
    start_prices = benchmark_price_map(start)
    end_prices = benchmark_price_map(end)
    out = {}
    for ticker, start_price in start_prices.items():
        end_price = end_prices.get(ticker)
        if end_price:
            out[ticker] = rn((end_price / start_price - 1) * 100, 2)
    return out


def historical_basket_specs() -> List[Tuple[str, Callable[[List[dict]], List[dict]]]]:
    return [
        ("Top Final Score", lambda rows: sorted(rows, key=lambda r: (-(r.get("finalInvestmentScore") or 0), r.get("ticker", "")))),
        ("Top Bottleneck Power", lambda rows: sorted(rows, key=lambda r: (-(r.get("bottleneckPowerScore") or 0), r.get("ticker", "")))),
        (
            "Improving Capital Flow",
            lambda rows: sorted(
                rows,
                key=lambda r: (
                    -((r.get("industrialCapitalFlowChange1Run") or 0) + (r.get("financialCapitalFlowChange1Run") or 0)),
                    -((r.get("industrialCapitalFlowScore") or 0) + (r.get("financialCapitalFlowScore") or 0)),
                    r.get("ticker", ""),
                ),
            ),
        ),
        (
            "Valuation Discipline",
            lambda rows: sorted(
                [r for r in rows if (r.get("valuationScore") or 100) <= 85] or rows,
                key=lambda r: (-(r.get("finalInvestmentScore") or 0), (r.get("valuationScore") or 100), r.get("ticker", "")),
            ),
        ),
        (
            "Risk Resilience",
            lambda rows: sorted(
                [r for r in rows if (r.get("riskScore") or 0) >= 55] or rows,
                key=lambda r: (-(r.get("riskScore") or 0), -(r.get("finalInvestmentScore") or 0), r.get("ticker", "")),
            ),
        ),
    ]


def annualization_factor(day_counts: List[int]) -> Optional[float]:
    if not day_counts:
        return None
    avg_days = sum(day_counts) / len(day_counts)
    if avg_days <= 0:
        return None
    return 365.25 / avg_days


def max_drawdown_from_returns(returns: List[float]) -> Optional[float]:
    if not returns:
        return None
    equity = 1.0
    peak = 1.0
    max_dd = 0.0
    for ret in returns:
        equity *= 1 + ret / 100.0
        peak = max(peak, equity)
        dd = equity / peak - 1
        max_dd = min(max_dd, dd)
    return rn(max_dd * 100, 2)


def ratio_from_returns(returns: List[float], day_counts: List[int], downside_only: bool = False) -> Optional[float]:
    if len(returns) < 2:
        return None
    mean = sum(returns) / len(returns)
    if downside_only:
        deviations = [min(0.0, r) for r in returns]
        denom = math.sqrt(sum(d * d for d in deviations) / len(deviations))
    else:
        denom = math.sqrt(sum((r - mean) ** 2 for r in returns) / (len(returns) - 1))
    ann = annualization_factor(day_counts)
    if denom <= 0 or ann is None:
        return None
    return rn((mean / denom) * math.sqrt(ann), 2)


def cagr_from_returns(returns: List[float], day_counts: List[int]) -> Optional[float]:
    if not returns or not day_counts:
        return None
    total_days = sum(day_counts)
    if total_days <= 0:
        return None
    equity = 1.0
    for ret in returns:
        equity *= 1 + ret / 100.0
    if equity <= 0:
        return None
    return rn((equity ** (365.25 / total_days) - 1) * 100, 2)


def avg_factor_profile(rows: List[dict]) -> dict:
    keys = (
        "finalInvestmentScore",
        "bottleneckPowerScore",
        "industrialCapitalFlowScore",
        "financialCapitalFlowScore",
        "valuationScore",
        "riskScore",
    )
    out = {}
    for key in keys:
        vals = [r.get(key) for r in rows if r.get(key) is not None]
        out[key] = rn(sum(vals) / len(vals), 1) if vals else None
    return out


def exposure_counts(rows: List[dict], key: str) -> List[dict]:
    counts = {}
    for row in rows:
        value = row.get(key) or "Unknown"
        counts[value] = counts.get(value, 0) + 1
    return [{"name": k, "count": v} for k, v in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]


def ordered_backtest_snapshots(snapshots: List[dict], current_snapshot: Optional[dict] = None) -> List[dict]:
    ordered = [s for s in snapshots if parse_snapshot_time(s)]
    if current_snapshot and parse_snapshot_time(current_snapshot):
        current_time = parse_snapshot_time(current_snapshot)
        if not any(parse_snapshot_time(s) == current_time for s in ordered):
            ordered.append(current_snapshot)
    ordered.sort(key=lambda s: parse_snapshot_time(s) or dt.datetime.min.replace(tzinfo=dt.timezone.utc))
    return ordered


def snapshot_period_key(snapshot: dict, frequency: str) -> Optional[str]:
    parsed = parse_snapshot_time(snapshot)
    if not parsed:
        return None
    if frequency == "monthly":
        return f"{parsed.year:04d}-{parsed.month:02d}"
    if frequency == "quarterly":
        quarter = ((parsed.month - 1) // 3) + 1
        return f"{parsed.year:04d}-Q{quarter}"
    return None


def calendar_rebalance_snapshots(ordered: List[dict], frequency: str) -> List[dict]:
    selected = []
    seen_periods = set()
    for snapshot in ordered:
        period = snapshot_period_key(snapshot, frequency)
        if not period or period in seen_periods:
            continue
        selected.append(snapshot)
        seen_periods.add(period)
    return selected


def historical_snapshot_backtest_core(
    ordered: List[dict],
    basket_size: int = 5,
    transaction_cost_bps: float = HISTORY_TRANSACTION_COST_BPS,
    method: str = "No-lookahead snapshot validation: baskets are selected from each saved AICS snapshot and measured against the next later snapshot price.",
    rebalance_rule: str = "one rebalance per saved snapshot pair",
) -> dict:
    basket_samples = {name: [] for name, _ in historical_basket_specs()}
    prior_members: Dict[str, set] = {}
    skipped_pairs = 0
    evaluated_pairs = 0

    for start, end in zip(ordered, ordered[1:]):
        start_time = parse_snapshot_time(start)
        end_time = parse_snapshot_time(end)
        if not start_time or not end_time or end_time <= start_time:
            skipped_pairs += 1
            continue
        start_rows = snapshot_rows(start)
        end_map = snapshot_row_maps(end)
        universe_returns = []
        row_returns = {}
        for row in start_rows:
            end_row = end_map.get(row.get("ticker")) or end_map.get(row.get("companyId"))
            start_price = snapshot_row_price(row)
            end_price = snapshot_row_price(end_row or {})
            if start_price and end_price:
                ret = rn((end_price / start_price - 1) * 100, 2)
                row_returns[row["ticker"]] = ret
                universe_returns.append(ret)
        if len(universe_returns) < 2:
            skipped_pairs += 1
            continue
        evaluated_pairs += 1
        universe_avg = sum(universe_returns) / len(universe_returns)
        benchmark_returns = benchmark_pair_returns(start, end)
        days = max(1, (end_time - start_time).days)
        for name, selector in historical_basket_specs():
            selected = selector(start_rows)[:basket_size]
            selected_returns = [row_returns[r["ticker"]] for r in selected if r.get("ticker") in row_returns]
            if not selected_returns:
                continue
            members = [r["ticker"] for r in selected if r.get("ticker") in row_returns]
            current_members = set(members)
            previous_members = prior_members.get(name)
            turnover = None if previous_members is None else rn((1 - len(previous_members & current_members) / max(len(current_members), 1)) * 100, 2)
            prior_members[name] = current_members
            ret = rn(sum(selected_returns) / len(selected_returns), 2)
            assumed_turnover = 100.0 if turnover is None else turnover
            transaction_cost_pct = rn(float(transaction_cost_bps) * assumed_turnover / 10000.0, 4)
            net_ret = rn(ret - transaction_cost_pct, 2)
            basket_samples[name].append(
                {
                    "start": start.get("generatedAt") or start.get("marketDataAsOf"),
                    "end": end.get("generatedAt") or end.get("marketDataAsOf"),
                    "days": days,
                    "members": members,
                    "grossReturnPct": ret,
                    "returnPct": net_ret,
                    "netReturnPct": net_ret,
                    "transactionCostPct": transaction_cost_pct,
                    "transactionCostBps": transaction_cost_bps,
                    "universeReturnPct": rn(universe_avg, 2),
                    "excessVsUniversePct": rn(net_ret - universe_avg, 2),
                    "benchmarkReturns": benchmark_returns,
                    "excessVsSOXXPct": rn(net_ret - benchmark_returns.get("SOXX"), 2) if benchmark_returns.get("SOXX") is not None else None,
                    "excessVsSMHPct": rn(net_ret - benchmark_returns.get("SMH"), 2) if benchmark_returns.get("SMH") is not None else None,
                    "hit": net_ret > universe_avg,
                    "turnoverPct": turnover,
                    "factorProfile": avg_factor_profile(selected),
                    "regionExposure": exposure_counts(selected, "region"),
                    "valueChainExposure": exposure_counts(selected, "peerGroup"),
                    "worstMember": min(
                        [{"ticker": r["ticker"], "returnPct": row_returns[r["ticker"]]} for r in selected if r.get("ticker") in row_returns],
                        key=lambda x: x["returnPct"],
                    ),
                }
            )

    baskets = []
    for name, samples in basket_samples.items():
        returns = [s["returnPct"] for s in samples if s.get("returnPct") is not None]
        gross_returns = [s["grossReturnPct"] for s in samples if s.get("grossReturnPct") is not None]
        excess = [s["excessVsUniversePct"] for s in samples if s.get("excessVsUniversePct") is not None]
        excess_soxx = [s["excessVsSOXXPct"] for s in samples if s.get("excessVsSOXXPct") is not None]
        excess_smh = [s["excessVsSMHPct"] for s in samples if s.get("excessVsSMHPct") is not None]
        costs = [s["transactionCostPct"] for s in samples if s.get("transactionCostPct") is not None]
        day_counts = [s["days"] for s in samples if s.get("days")]
        turnovers = [s["turnoverPct"] for s in samples if s.get("turnoverPct") is not None]
        baskets.append(
            {
                "name": name,
                "rebalanceCount": len(samples),
                "observationCount": sum(len(s.get("members") or []) for s in samples),
                "avgGrossReturnPct": rn(sum(gross_returns) / len(gross_returns), 2) if gross_returns else None,
                "avgReturnPct": rn(sum(returns) / len(returns), 2) if returns else None,
                "avgNetReturnPct": rn(sum(returns) / len(returns), 2) if returns else None,
                "avgTransactionCostPct": rn(sum(costs) / len(costs), 4) if costs else None,
                "avgExcessVsUniversePct": rn(sum(excess) / len(excess), 2) if excess else None,
                "avgExcessVsSOXXPct": rn(sum(excess_soxx) / len(excess_soxx), 2) if excess_soxx else None,
                "avgExcessVsSMHPct": rn(sum(excess_smh) / len(excess_smh), 2) if excess_smh else None,
                "hitRatePct": rn(100 * sum(1 for s in samples if s.get("hit")) / len(samples), 1) if samples else None,
                "turnoverPct": rn(sum(turnovers) / len(turnovers), 2) if turnovers else None,
                "cagrPct": cagr_from_returns(returns, day_counts),
                "maxDrawdownPct": max_drawdown_from_returns(returns),
                "sharpe": ratio_from_returns(returns, day_counts),
                "sortino": ratio_from_returns(returns, day_counts, downside_only=True),
                "latestMembers": samples[-1]["members"] if samples else [],
                "latestRegionExposure": samples[-1]["regionExposure"] if samples else [],
                "latestValueChainExposure": samples[-1]["valueChainExposure"] if samples else [],
                "avgStartFactorProfile": samples[-1]["factorProfile"] if samples else {},
                "drawdownAttribution": sorted(
                    [s["worstMember"] for s in samples if s.get("worstMember")],
                    key=lambda x: x["returnPct"],
                )[:5],
                "samples": samples[-6:],
            }
        )

    enough = any((b.get("rebalanceCount") or 0) > 0 for b in baskets)
    return {
        "status": "available" if enough else "not_enough_price_snapshots",
        "method": method,
        "snapshotCount": len(ordered),
        "evaluatedPairs": evaluated_pairs,
        "skippedPairs": skipped_pairs,
        "basketSize": basket_size,
        "benchmark": "Equal-weight AICS universe over the same snapshot pair, plus SOXX/SMH where both snapshots persist benchmark prices.",
        "rules": {
            "rebalance": rebalance_rule,
            "lookahead": "selection uses start snapshot only",
            "publicationLag": "snapshot timestamp is treated as the publication timestamp; fundamentals enter the test only after they are saved in a snapshot",
            "transactionCostBps": transaction_cost_bps,
            "fx": "uses priceUsd when present; otherwise local price fallback",
            "adrLocalDedup": "MVP universe is companyId-based so ADR/local listings do not create duplicate basket members",
            "liquidity": "inherits current AICS universe filters",
        },
        "plannedMetrics": ["CAGR", "Max Drawdown", "Sharpe", "Sortino", "Hit Rate", "Turnover", "Excess Return", "Factor Profile", "Region Exposure", "Value-Chain Exposure", "Drawdown Attribution"],
        "baskets": baskets,
    }


def calendar_rebalance_validations(
    ordered: List[dict],
    basket_size: int = 5,
    transaction_cost_bps: float = HISTORY_TRANSACTION_COST_BPS,
) -> dict:
    validations = {}
    for frequency, label in (("monthly", "monthly"), ("quarterly", "quarterly")):
        selected = calendar_rebalance_snapshots(ordered, frequency)
        periods = [snapshot_period_key(snapshot, frequency) for snapshot in selected]
        validation = historical_snapshot_backtest_core(
            selected,
            basket_size=basket_size,
            transaction_cost_bps=transaction_cost_bps,
            method=(
                f"No-lookahead {label} calendar rebalance validation: select the first saved AICS snapshot "
                f"inside each {label} period and measure against the next selected period snapshot."
            ),
            rebalance_rule=f"first saved snapshot per {label} calendar period",
        )
        validation["frequency"] = frequency
        validation["selectedPeriods"] = [p for p in periods if p]
        validations[frequency] = validation
    return validations


def historical_snapshot_backtest(
    snapshots: List[dict],
    current_snapshot: Optional[dict] = None,
    basket_size: int = 5,
    transaction_cost_bps: float = HISTORY_TRANSACTION_COST_BPS,
) -> dict:
    ordered = ordered_backtest_snapshots(snapshots, current_snapshot)
    validation = historical_snapshot_backtest_core(
        ordered,
        basket_size=basket_size,
        transaction_cost_bps=transaction_cost_bps,
    )
    validation["calendarValidation"] = calendar_rebalance_validations(
        ordered,
        basket_size=basket_size,
        transaction_cost_bps=transaction_cost_bps,
    )
    return validation


def static_basket_backtest(scores: List[dict], benchmark_prices=None, history_snapshots: Optional[List[dict]] = None, current_snapshot: Optional[dict] = None) -> dict:
    benchmark_returns = benchmark_window_returns(benchmark_prices)
    investable = [r for r in scores if r.get("gate") not in ("BLOCK", "DATA_REVIEW", "PORTFOLIO_BLOCK")]
    baskets = [
        basket_result("Top Final Score", scores[:5], benchmark_returns),
        basket_result("Top Bottleneck Power", sorted(scores, key=lambda r: -r.get("bottleneckPowerScore", 0))[:5], benchmark_returns),
        basket_result(
            "Top Capital Flow",
            sorted(scores, key=lambda r: -(r.get("industrialCapitalFlowScore", 0) + r.get("financialCapitalFlowScore", 0)))[:5],
            benchmark_returns,
        ),
        basket_result(
            "Investable Tactical",
            sorted(investable, key=lambda r: (-r.get("financialCapitalFlowScore", 0), -r.get("momentumScore", 0), r.get("ticker", "")))[:5],
            benchmark_returns,
        ),
    ]
    any_returns = any(
        window.get("returnPct") is not None
        for basket in baskets
        for window in (basket.get("windows") or {}).values()
    )
    return {
        "status": "available" if any_returns else "not_enough_market_history",
        "method": "Static equal-weight basket backtest from current AICS ranks using trailing market returns. This is not a point-in-time rebalance test.",
        "benchmarkReturns": benchmark_returns,
        "baskets": baskets,
        "plannedMetrics": ["CAGR", "Max Drawdown", "Sharpe", "Sortino", "Hit Rate", "Turnover", "Excess Return"],
        "currentCrossSection": {
            "topFinalScoreBasket": baskets[0]["members"],
            "topBottleneckBasket": baskets[1]["members"],
            "topCapitalFlowBasket": baskets[2]["members"],
            "investableTacticalBasket": baskets[3]["members"],
        },
        "historyValidation": historical_snapshot_backtest(history_snapshots or [], current_snapshot),
        "note": "This validates current ranks against recent returns. History validation adds saved-snapshot and calendar rebalance checks once enough price-bearing AICS snapshots exist.",
    }


def backtest_stub(scores: List[dict]) -> dict:
    return {
        "status": "not_enough_score_history",
        "benchmarkCandidates": ["SOXX", "SMH", "Nasdaq 100", "Global AI Semiconductor Basket"],
        "plannedMetrics": ["CAGR", "Max Drawdown", "Sharpe", "Sortino", "Hit Rate", "Turnover", "Excess Return"],
        "currentCrossSection": {
            "topFinalScoreBasket": [r["ticker"] for r in scores[:5]],
            "topBottleneckBasket": [r["ticker"] for r in sorted(scores, key=lambda x: -x["bottleneckPowerScore"])[:5]],
        },
        "note": "Backtest requires persisted historical AICS snapshots; the producer now writes output/aics_history.jsonl.",
    }


def model_card(base: dict, scores: List[dict], edges: List[dict], previous: Optional[dict], history_meta: Optional[dict] = None) -> dict:
    base_card = dict(base.get("modelCard") or {})
    return {
        **base_card,
        "aicsModelVersion": MODEL_VERSION,
        "baseAiSemiQuantVersion": base_card.get("modelVersion"),
        "schemaVersion": 1,
        "edgeCount": len(edges),
        "scoreSaturationCount": sum(1 for r in scores if r.get("finalInvestmentScore") == 100),
        "sourceConfidenceDistribution": {
            "clean": sum(1 for r in scores if r.get("dataQualitySeverity") == "clean"),
            "soft_review": sum(1 for r in scores if r.get("dataQualitySeverity") == "soft_review"),
            "hard_review": sum(1 for r in scores if r.get("dataQualitySeverity") == "hard_review"),
        },
        "hasPreviousSnapshot": bool(previous),
        "history": history_meta or {},
        "requirements": {
            "scoresClamped": all(0 <= (r.get("finalInvestmentScore") or 0) <= 100 for r in scores),
            "allRowsHaveGateReasons": all(r.get("gateReasons") for r in scores),
            "allEdgesWeighted": all(0 <= e.get("edgeWeight", -1) <= 100 for e in edges),
        },
    }


def build_aics_document(args, previous: Optional[dict] = None, history_snapshots: Optional[List[dict]] = None) -> dict:
    base_args = SimpleNamespace(
        dashboard=getattr(args, "dashboard", str(DASHBOARD)),
        period=getattr(args, "period", "2y"),
        no_fetch=getattr(args, "no_fetch", False),
    )
    base = aiq.build_document(base_args)
    base_rows = base["scores"]
    generated_at_override = getattr(args, "generated_at", None)
    if generated_at_override:
        if isinstance(generated_at_override, dt.datetime):
            generated_at = generated_at_override
        else:
            generated_at = dt.datetime.fromisoformat(str(generated_at_override).replace("Z", "+00:00"))
        if generated_at.tzinfo is None:
            generated_at = generated_at.replace(tzinfo=dt.datetime.now().astimezone().tzinfo)
    else:
        generated_at = dt.datetime.now().astimezone()
    history_path = Path(getattr(args, "history", HISTORY))
    out_json_path = Path(getattr(args, "out_json", OUT_JSON))
    snapshots = list(history_snapshots) if history_snapshots is not None else read_history_snapshots(history_path)
    previous_output = read_json(out_json_path)
    previous_run = previous or latest_snapshot_before(snapshots, generated_at) or previous_output
    validation_snapshots = list(snapshots)
    previous_output_time = parse_snapshot_time(previous_output)
    if previous_output and previous_output_time and previous_output_time < generated_at:
        if not any(parse_snapshot_time(s) == previous_output_time for s in validation_snapshots):
            validation_snapshots.append(previous_output)
    previous_1w = history_snapshot_on_or_before(snapshots, generated_at, 7)
    previous_1m = history_snapshot_on_or_before(snapshots, generated_at, 30)
    previous_rows = previous_score_maps(previous_run)
    previous_edges = previous_edge_maps(previous_run)
    previous_1w_rows = previous_score_maps(previous_1w)
    previous_1m_rows = previous_score_maps(previous_1m)
    history_meta = {
        "historyPath": str(history_path),
        "snapshotsRead": len(snapshots),
        "previousRunAt": (previous_run or {}).get("generatedAt"),
        "oneWeekSnapshotAt": (previous_1w or {}).get("generatedAt"),
        "oneMonthSnapshotAt": (previous_1m or {}).get("generatedAt"),
    }
    quality_ranks, attractiveness_ranks = rank_maps(base_rows)
    scores = []
    for row in base_rows:
        key = row.get("companyId") or row.get("ticker")
        scores.append(
            score_row(
                row,
                quality_rank=quality_ranks.get(row["companyId"], 0),
                attractiveness_rank=attractiveness_ranks.get(row["companyId"], 0),
                previous=previous_rows.get(key) or previous_rows.get(row.get("ticker")),
                previous_1w=previous_1w_rows.get(key) or previous_1w_rows.get(row.get("ticker")),
                previous_1m=previous_1m_rows.get(key) or previous_1m_rows.get(row.get("ticker")),
            )
        )
    scores.sort(key=lambda r: (-r["finalInvestmentScore"], r["stockAttractivenessRank"], r["ticker"]))
    edges = relationship_edges(base_rows, previous_edges)
    overlay = portfolio_overlay(scores)
    attribution = return_attribution(scores)
    generated_at_iso = generated_at.isoformat(timespec="seconds")
    benchmark_prices = aiq.load_market_data(("SOXX", "SMH"), getattr(args, "period", "2y"), no_fetch=getattr(args, "no_fetch", False))
    benchmark_snapshot = benchmark_snapshot_rows(benchmark_prices)
    current_snapshot = compact_score_snapshot(generated_at_iso, base.get("marketDataAsOf"), scores, benchmark_snapshot)
    doc = {
        "schemaVersion": 1,
        "toolVersion": MODEL_VERSION,
        "generatedAt": generated_at_iso,
        "marketDataAsOf": base.get("marketDataAsOf"),
        "title": "AICS",
        "subtitle": "AI Semiconductor Capital Flow & Scoring System",
        "method": "AICS contract built from AI-SemiQuant structural scores, Yahoo-style market overlay, curated supply-chain edges, and portfolio exposure.",
        "universe": [universe_row(r) for r in base_rows],
        "scores": scores,
        "scoreDeltas": score_deltas(scores),
        "relationshipEdges": edges,
        "capitalWaterfall": base.get("capitalWaterfall") or [],
        "capitalFlow": capital_flow_summary(scores, edges),
        "returnAttribution": attribution,
        "returnAttributionSummary": return_attribution_summary(attribution),
        "scenarioModel": scenario_model(scores),
        "scenarioResults": scenario_results(scores),
        "alerts": alerts(scores, overlay, edges, attribution),
        "portfolioOverlay": overlay,
        "benchmarkSnapshot": benchmark_snapshot,
        "backtest": static_basket_backtest(scores, benchmark_prices, validation_snapshots, current_snapshot),
        "modelCard": model_card(base, scores, edges, previous_run, history_meta),
        "sources": base.get("sources") or [],
        "disclaimer": "Research framework only. Not investment advice, not a recommendation, and not a guarantee of future return.",
    }
    return doc


def fmt_score(value: Optional[float]) -> str:
    return "-" if value is None else f"{float(value):.0f}"


def fmt_delta(value: Optional[float]) -> str:
    return "-" if value is None else f"{float(value):+g}"


def render_report(doc: dict) -> str:
    lines = [
        "# AICS Report",
        "",
        f"Generated: {doc.get('generatedAt')}",
        f"Market data as of: {doc.get('marketDataAsOf') or 'unavailable'}",
        "",
        doc.get("disclaimer", ""),
        "",
        "## Cockpit",
        "",
        f"- Universe: {len(doc.get('universe') or [])} core companies",
        f"- Relationship edges: {len(doc.get('relationshipEdges') or [])}",
        f"- AICS portfolio overlap: {doc.get('portfolioOverlay', {}).get('totalAicsWeightPct')}%",
        f"- Alerts: {len(doc.get('alerts') or [])}",
        "",
        "## Top Scoreboard",
        "",
        "| Rank | Ticker | Company | Gate | Final | Bottleneck | Growth | Industrial Flow | Financial Flow | Risk | Main Reason |",
        "| ---: | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for i, row in enumerate((doc.get("scores") or [])[:15], 1):
        reason = ((row.get("gateReasons") or [{}])[0]).get("detail", "-")
        lines.append(
            f"| {i} | {row['ticker']} | {row['name']} | {row['gate']} | {fmt_score(row.get('finalInvestmentScore'))} | "
            f"{fmt_score(row.get('bottleneckPowerScore'))} | {fmt_score(row.get('growthRealizationScore'))} | "
            f"{fmt_score(row.get('industrialCapitalFlowScore'))} | {fmt_score(row.get('financialCapitalFlowScore'))} | "
            f"{fmt_score(row.get('riskScore'))} | {reason} |"
        )
    lines.extend(["", "## Capital Waterfall", "", "| Stage | Beneficiaries | Leading KPIs |", "| --- | --- | --- |"])
    for stage in doc.get("capitalWaterfall") or []:
        lines.append(f"| {stage['stage']} | {', '.join(stage['beneficiaries'])} | {', '.join(stage['leadingKpis'])} |")
    lines.extend(["", "## Scenario Lab", ""])
    for scenario in doc.get("scenarioResults") or []:
        winners = ", ".join(f"{x['ticker']} ({fmt_delta(x.get('scoreDelta'))})" for x in scenario.get("winners", [])[:4])
        losers = ", ".join(f"{x['ticker']} ({fmt_delta(x.get('scoreDelta'))})" for x in scenario.get("losers", [])[:4])
        lines.append(f"- **{scenario['name']}**: winners {winners}; losers {losers}.")
    lines.extend(
        [
            "",
            "## Static Basket Backtest",
            "",
            (doc.get("backtest") or {}).get("method", ""),
            "",
            "| Basket | Members | 1M | 3M | 6M | 1Y |",
            "| --- | --- | ---: | ---: | ---: | ---: |",
        ]
    )
    for basket in (doc.get("backtest") or {}).get("baskets") or []:
        windows = basket.get("windows") or {}
        lines.append(
            f"| {basket.get('name')} | {', '.join(basket.get('members') or [])} | "
            f"{windows.get('1M', {}).get('returnPct', '-')} | "
            f"{windows.get('3M', {}).get('returnPct', '-')} | "
            f"{windows.get('6M', {}).get('returnPct', '-')} | "
            f"{windows.get('1Y', {}).get('returnPct', '-')} |"
        )
    history_validation = ((doc.get("backtest") or {}).get("historyValidation") or {})
    lines.extend(
        [
            "",
            "## History Snapshot Validation",
            "",
            f"Status: {history_validation.get('status', '-')}. Snapshots: {history_validation.get('snapshotCount', 0)}; evaluated pairs: {history_validation.get('evaluatedPairs', 0)}; transaction cost: {(history_validation.get('rules') or {}).get('transactionCostBps', 0)} bps.",
            "",
            "| Basket | Rebalances | Gross Return | Net Return | Excess Universe | Excess SOXX | Excess SMH | CAGR | Max Drawdown | Hit Rate | Turnover |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for basket in history_validation.get("baskets") or []:
        lines.append(
            f"| {basket.get('name')} | {basket.get('rebalanceCount', 0)} | {basket.get('avgGrossReturnPct', '-')} | "
            f"{basket.get('avgNetReturnPct', basket.get('avgReturnPct', '-'))} | {basket.get('avgExcessVsUniversePct', '-')} | "
            f"{basket.get('avgExcessVsSOXXPct', '-')} | {basket.get('avgExcessVsSMHPct', '-')} | {basket.get('cagrPct', '-')} | "
            f"{basket.get('maxDrawdownPct', '-')} | {basket.get('hitRatePct', '-')} | {basket.get('turnoverPct', '-')} |"
        )
    lines.extend(
        [
            "",
            "## Calendar Rebalance Validation",
            "",
            "| Mode | Status | Period Snapshots | Evaluated Pairs | Top Basket Net Return | Excess Universe | CAGR | Max Drawdown |",
            "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for mode in ("monthly", "quarterly"):
        validation = ((history_validation.get("calendarValidation") or {}).get(mode) or {})
        baskets = validation.get("baskets") or []
        top_basket = next((b for b in baskets if (b.get("rebalanceCount") or 0) > 0), baskets[0] if baskets else {})
        lines.append(
            f"| {mode.title()} | {validation.get('status', '-')} | {validation.get('snapshotCount', 0)} | "
            f"{validation.get('evaluatedPairs', 0)} | {top_basket.get('avgNetReturnPct', top_basket.get('avgReturnPct', '-'))} | "
            f"{top_basket.get('avgExcessVsUniversePct', '-')} | {top_basket.get('cagrPct', '-')} | {top_basket.get('maxDrawdownPct', '-')} |"
        )
    lines.extend(["", "## Alerts", ""])
    if doc.get("alerts"):
        for alert in doc["alerts"]:
            ticker = f" `{alert.get('ticker')}`" if alert.get("ticker") else ""
            lines.append(f"- {alert['severity'].upper()}{ticker}: {alert['title']} - {alert['detail']}")
    else:
        lines.append("- No AICS alerts on this run.")
    lines.extend(["", "## Model Card", ""])
    card = doc.get("modelCard") or {}
    history = card.get("history") or {}
    lines.extend(
        [
            f"- AICS model version: {card.get('aicsModelVersion')}",
            f"- Base AI-SemiQuant version: {card.get('baseAiSemiQuantVersion')}",
            f"- Missing price count: {card.get('missingPriceCount')}",
            f"- DATA_REVIEW count: {card.get('dataReviewCount')}",
            f"- Score saturation count: {card.get('scoreSaturationCount')}",
            f"- History snapshots read: {history.get('snapshotsRead', 0)}",
            f"- Previous run snapshot: {history.get('previousRunAt') or '-'}",
            f"- 1W comparison snapshot: {history.get('oneWeekSnapshotAt') or '-'}",
            f"- 1M comparison snapshot: {history.get('oneMonthSnapshotAt') or '-'}",
            f"- Requirements: {json.dumps(card.get('requirements'), ensure_ascii=False)}",
        ]
    )
    lines.extend(["", "## Sources", ""])
    for src in doc.get("sources") or []:
        lines.append(f"- [{src['name']}]({src['url']}) - {src['use']}")
    lines.extend(
        [
            "",
            "## Regeneration",
            "",
            "```bash",
            "python3 scripts/aics_tool.py",
            "python3 generate.py --no-fetch",
            "```",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate AICS JSON and Markdown dashboard artifacts.")
    ap.add_argument("--out-json", default=str(OUT_JSON))
    ap.add_argument("--out-md", default=str(OUT_MD))
    ap.add_argument("--history", default=str(HISTORY))
    ap.add_argument("--dashboard", default=str(DASHBOARD), help="portfolio_dashboard.html for exposure overlay")
    ap.add_argument("--period", default="2y", help="Yahoo Finance history period")
    ap.add_argument("--no-fetch", action="store_true", help="skip Yahoo fetch and emit structural-only scores")
    ap.add_argument("--no-history", action="store_true", help="do not append to output/aics_history.jsonl")
    args = ap.parse_args()

    doc = build_aics_document(args)
    out_json = Path(args.out_json)
    out_md = Path(args.out_md)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_report(doc), encoding="utf-8")
    if not args.no_history:
        append_history(Path(args.history), doc)
    print(f"✓ wrote {out_json}")
    print(f"✓ wrote {out_md}")
    print(
        f"  {len(doc['scores'])} companies · {len(doc['relationshipEdges'])} edges · "
        f"{len(doc['alerts'])} alerts · AICS exposure {doc['portfolioOverlay'].get('totalAicsWeightPct')}%"
    )


if __name__ == "__main__":
    main()

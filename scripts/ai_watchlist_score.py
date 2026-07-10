#!/usr/bin/env python3
"""Score the AI old-capability re-rating watchlist.

The watchlist is intentionally broader than the existing AI-SemiQuant
semiconductor module. It covers companies whose old capabilities may become
new AI bottlenecks: mobile SoCs, edge sensing, ASIC services, optics, power,
cooling, EDA, HBM, and selected AI application monetization names.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import importlib.util
import io
import json
import math
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse

try:
    from scripts.artifact_io import atomic_write_json, atomic_write_text
except ModuleNotFoundError:  # direct ``python scripts/ai_watchlist_score.py``
    from artifact_io import atomic_write_json, atomic_write_text


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_UNIVERSE = ROOT / "data" / "ai_watchlist_universe.json"
OUT_JSON = ROOT / "output" / "ai_watchlist.json"
OUT_MD = ROOT / "output" / "ai_watchlist_report.md"
OUT_CSV = ROOT / "output" / "ai_watchlist.csv"
DASHBOARD = ROOT / "output" / "portfolio_dashboard.html"

AIQ_PATH = ROOT / "scripts" / "ai_semi_quant.py"
SPEC = importlib.util.spec_from_file_location("ai_semi_quant", AIQ_PATH)
aiq = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = aiq
SPEC.loader.exec_module(aiq)

MODEL_VERSION = "0.4.0"
BENCHMARK = "SPY"
DEFAULT_RESEARCH_SOURCE_DATE = "2026-06-21"
FACTOR_WEIGHTS = {
    "bottleneckFit": 0.28,
    "proofPoints": 0.24,
    "monetizationPath": 0.18,
    "underappreciation": 0.18,
    "executionRiskControl": 0.12,
}
FACTOR_LABELS = {
    "bottleneckFit": "Old capability -> AI bottleneck fit",
    "proofPoints": "Real proof points / design wins / revenue guide",
    "monetizationPath": "Ability to capture economics",
    "underappreciation": "Old label still hides the new AI role",
    "executionRiskControl": "Execution, customer, cycle, and balance-sheet risk control",
}
GATE_ORDER = (
    "BUILD_MODEL",
    "DEEP_DIVE",
    "WATCH_EVIDENCE",
    "TRACK_CATALYST",
    "WATCH_RESET",
    "SPECULATIVE",
    "DATA_REVIEW",
    "BLOCK",
)
ENTRY_GATE_ORDER = ("ACTION_READY", "ALLOW_REVIEW", "VERIFY_DATA", "WAIT_RESET", "WAIT_SETUP", "PORTFOLIO_BLOCK", "BLOCK_DATA", "BLOCK")
TIER_ORDER = {
    "T1 immediate deep dive": 1,
    "T2 quality discovered": 2,
    "T3 high-beta option": 3,
    "T4 long-cycle watch": 4,
    "T5 reference / low AI purity": 5,
}
TIER_BONUS = {
    "T1 immediate deep dive": 6,
    "T2 quality discovered": 3,
    "T3 high-beta option": 0,
    "T4 long-cycle watch": -2,
    "T5 reference / low AI purity": -5,
}
EVIDENCE_LEVEL_SCORES = {
    "major_customer_deployment": 92,
    "major_customer_contract": 90,
    "revenue_guided": 86,
    "capex_expansion": 82,
    "customer_design_win": 74,
    "credible_product": 64,
    "early_optional": 54,
    "application_uncertain": 44,
}
EVIDENCE_CONFIDENCE_LEVELS = {"low", "low-medium", "medium-low", "medium", "medium-high", "high"}
FRAMEWORK_STAGES = (
    "Stage 1/2 - compute foundation",
    "Stage 2 - AI factory bottlenecks",
    "Stage 3 - edge and physical AI",
    "Stage 3 - AI application monetization",
)
ACTION_TIER_LABELS = {
    "T1 immediate deep dive": "T1 Build Model",
    "T2 quality discovered": "T2 Monitor Valuation",
    "T3 high-beta option": "T3 Evidence Upgrade",
    "T4 long-cycle watch": "T4 Long Cycle Watch",
    "T5 reference / low AI purity": "T5 Reference",
}
SUPPLY_CHAIN_DIRECTIONS = (
    "Advanced packaging",
    "substrates",
    "materials",
    "ASIC design services",
)
CORRELATION_BUCKETS = (
    "AI_factory_capex",
    "physical_ai_edge",
    "ai_application_monetization",
    "non_ai_or_other",
)


def clamp(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return aiq.clamp(value, lo, hi)


def rn(value: Optional[float], digits: int = 2) -> Optional[float]:
    return aiq.rn(value, digits)


def finite(value) -> bool:
    return aiq.finite(value)


def load_universe(path: Path) -> List[dict]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"{path} must contain a JSON list")
    seen = set()
    seen_company_ids = set()
    out = []
    for i, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            raise ValueError(f"watchlist row {i} is not an object")
        ticker = str(item.get("ticker") or "").strip()
        cid = str(item.get("companyId") or "").strip()
        factors = item.get("factors") or {}
        if not isinstance(factors, dict):
            raise ValueError(f"{ticker or f'row {i}'} factors must be an object")
        missing = [k for k in FACTOR_WEIGHTS if k not in factors]
        if not ticker or not cid:
            raise ValueError(f"watchlist row {i} needs companyId and ticker")
        if not str(item.get("name") or "").strip() or not str(item.get("bucket") or "").strip():
            raise ValueError(f"{ticker} needs name and bucket")
        if ticker.upper() in seen:
            raise ValueError(f"duplicate ticker in watchlist: {ticker}")
        if cid.lower() in seen_company_ids:
            raise ValueError(f"duplicate companyId in watchlist: {cid}")
        if missing:
            raise ValueError(f"{ticker} missing factors: {', '.join(missing)}")
        for key in FACTOR_WEIGHTS:
            aiq.validated_factor(factors[key], f"{ticker}.factors.{key}")
        tier = item.get("priorityTier") or "T4 long-cycle watch"
        evidence_level = item.get("evidenceLevel") or "early_optional"
        if tier not in TIER_ORDER:
            raise ValueError(f"{ticker} has unknown priorityTier: {tier}")
        if evidence_level not in EVIDENCE_LEVEL_SCORES:
            raise ValueError(f"{ticker} has unknown evidenceLevel: {evidence_level}")
        conviction = str(item.get("conviction") or "medium").lower()
        if conviction not in EVIDENCE_CONFIDENCE_LEVELS:
            raise ValueError(f"{ticker} has unknown conviction: {conviction}")
        list_fields = (
            "aliases",
            "riskFlags",
            "watchKpis",
            "catalysts",
            "rebuttalChecks",
            "researchQuestions",
            "sourceReportTags",
            "evidenceLedger",
        )
        for field in list_fields:
            value = item.get(field)
            if value is not None and not isinstance(value, list):
                raise ValueError(f"{ticker}.{field} must be a list")
            if field != "evidenceLedger" and value is not None and any(not isinstance(entry, str) for entry in value):
                raise ValueError(f"{ticker}.{field} entries must be strings")
            if field == "evidenceLedger" and value is not None and any(not isinstance(entry, dict) for entry in value):
                raise ValueError(f"{ticker}.evidenceLedger entries must be objects")
        for entry in item.get("evidenceLedger") or []:
            confidence = str(entry.get("rawConfidence") or entry.get("confidence") or conviction).lower()
            if confidence not in EVIDENCE_CONFIDENCE_LEVELS:
                raise ValueError(f"{ticker} has unknown evidence confidence: {confidence}")
        seen.add(ticker.upper())
        seen_company_ids.add(cid.lower())
        item = dict(item)
        item["aliases"] = list(item.get("aliases") or [])
        item["riskFlags"] = list(item.get("riskFlags") or [])
        item["watchKpis"] = list(item.get("watchKpis") or [])
        item["catalysts"] = list(item.get("catalysts") or item["watchKpis"][:3])
        item["rebuttalChecks"] = list(item.get("rebuttalChecks") or [])
        item["researchQuestions"] = list(item.get("researchQuestions") or [])
        item["sourceReportTags"] = list(item.get("sourceReportTags") or [])
        item["evidenceLedger"] = normalize_evidence_ledger(item)
        item.setdefault("stage", "Stage 3 - edge and physical AI")
        item.setdefault("direction", item.get("bucket") or "Other")
        item.setdefault("priorityTier", "T4 long-cycle watch")
        item.setdefault("evidenceLevel", "early_optional")
        item.setdefault("conviction", "medium")
        out.append(item)
    symbol_owner = {}
    for item in out:
        for symbol in (item["ticker"], *(item.get("aliases") or [])):
            normalized = str(symbol or "").strip().upper()
            if not normalized:
                raise ValueError(f"{item['ticker']} contains an empty instrument symbol")
            owner = symbol_owner.get(normalized)
            if owner and owner != item["companyId"]:
                raise ValueError(f"instrument symbol {normalized} is assigned to both {owner} and {item['companyId']}")
            symbol_owner[normalized] = item["companyId"]
    return out


def structural_score(factors: Dict[str, int]) -> int:
    missing = [key for key in FACTOR_WEIGHTS if key not in factors]
    if missing:
        raise ValueError(f"missing watchlist factors: {', '.join(missing)}")
    score = sum(aiq.validated_factor(factors[key], key) * weight for key, weight in FACTOR_WEIGHTS.items())
    return int(round(clamp(score)))


def gate_family(gate: str) -> str:
    if gate in ("BUILD_MODEL", "DEEP_DIVE"):
        return "RESEARCH"
    if gate in ("WATCH_EVIDENCE", "TRACK_CATALYST", "WATCH_RESET", "SPECULATIVE"):
        return "WATCH"
    if gate == "DATA_REVIEW":
        return "DATA_REVIEW"
    return "BLOCK"


def score_band(score: int) -> str:
    if score >= 85:
        return "A"
    if score >= 75:
        return "B"
    if score >= 65:
        return "C"
    if score >= 55:
        return "D"
    return "F"


def evidence_score(level: Optional[str]) -> int:
    if level not in EVIDENCE_LEVEL_SCORES:
        raise ValueError(f"unknown evidence level: {level}")
    return EVIDENCE_LEVEL_SCORES[level]


def tier_rank(tier: Optional[str]) -> int:
    return TIER_ORDER.get(tier or "", 9)


def action_tier(tier: str, hard_review: bool = False) -> str:
    if hard_review:
        return "DQ Data Quarantine"
    return ACTION_TIER_LABELS.get(tier, "T5 Reference")


def normalize_evidence_ledger(item: dict) -> List[dict]:
    """Return audit-ready evidence entries even when the universe only has tags."""
    raw = item.get("evidenceLedger") or []
    ledger = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        source_url = entry.get("sourceUrl")
        raw_confidence = str(entry.get("rawConfidence") or entry.get("confidence") or item.get("conviction") or "medium").lower()
        confidence = capped_evidence_confidence(raw_confidence, source_url)
        ledger.append(
            {
                "claim": entry.get("claim") or item.get("thesis") or item.get("aiBottleneck") or "Research claim needs wording.",
                "sourceType": entry.get("sourceType") or "internal_research_report",
                "sourceTypeStatus": "explicit" if entry.get("sourceType") else "default_internal_research",
                "sourceUrl": source_url,
                "sourceDate": entry.get("sourceDate") or DEFAULT_RESEARCH_SOURCE_DATE,
                "sourceDateStatus": "explicit" if entry.get("sourceDate") else "default_research_import_date",
                "rawConfidence": raw_confidence,
                "confidence": confidence,
                "confidenceCapped": confidence != raw_confidence,
                "financialLink": entry.get("financialLink") or item.get("evidenceLevel") or "early_optional",
                "financialLinkStatus": "categorical evidence-to-financials linkage; not a URL",
                "expiresAfter": entry.get("expiresAfter") or "2026-12-31",
                "expiryStatus": "explicit" if entry.get("expiresAfter") else "default_review_deadline",
                "needsRefresh": bool(entry.get("needsRefresh", False)),
                "verified": bool(entry.get("verified", False)),
            }
        )
    if ledger:
        return ledger

    tags = item.get("sourceReportTags") or []
    source_type = "internal_research_report"
    level = item.get("evidenceLevel") or "early_optional"
    if level in ("major_customer_deployment", "major_customer_contract", "revenue_guided"):
        source_type = "company_or_primary_report"
    elif level in ("capex_expansion", "customer_design_win"):
        source_type = "capex_or_supply_chain_report"
    claim = item.get("thesis") or item.get("aiBottleneck") or f"{item.get('ticker')} research thesis"
    if tags:
        claim = f"{claim} Evidence tags: {', '.join(tags[:3])}."
    source_url = item.get("sourceUrl")
    raw_confidence = str(item.get("conviction") or "medium").lower()
    confidence = capped_evidence_confidence(raw_confidence, source_url)
    return [
        {
            "claim": claim,
            "sourceType": source_type,
            "sourceTypeStatus": "inferred_from_evidence_level",
            "sourceUrl": source_url,
            "sourceDate": DEFAULT_RESEARCH_SOURCE_DATE,
            "sourceDateStatus": "default_research_import_date",
            "rawConfidence": raw_confidence,
            "confidence": confidence,
            "confidenceCapped": confidence != raw_confidence,
            "financialLink": level,
            "financialLinkStatus": "categorical evidence-to-financials linkage; not a URL",
            "expiresAfter": "2026-12-31",
            "expiryStatus": "default_review_deadline",
            "needsRefresh": False,
            "verified": False,
            "sourceStatus": "attached" if valid_source_url(source_url) else "needs_external_url",
        }
    ]


def capped_evidence_confidence(confidence: str, source_url: Optional[str]) -> str:
    if valid_source_url(source_url):
        return confidence
    order = {"low": 1, "low-medium": 1.5, "medium-low": 1.5, "medium": 2, "medium-high": 3, "high": 4}
    if order.get(str(confidence).lower(), 2) > order["medium"]:
        return "medium"
    return confidence


def valid_source_url(value: Optional[str]) -> bool:
    try:
        parsed = urlparse(str(value or "").strip())
        return parsed.scheme in ("http", "https") and bool(parsed.netloc)
    except Exception:
        return False


def _iso_date(value: Optional[str]) -> Optional[dt.date]:
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def evidence_assessment(ledger: List[dict], level: str, as_of: Optional[str] = None) -> dict:
    """Turn a qualitative evidence label into an auditable effective score."""
    raw_score = evidence_score(level)
    reference = _iso_date(as_of) or dt.datetime.now().astimezone().date()
    valid_urls = sum(1 for entry in ledger if valid_source_url(entry.get("sourceUrl")))
    verified = sum(1 for entry in ledger if entry.get("verified"))
    default_dates = sum(1 for entry in ledger if entry.get("sourceDateStatus") == "default_research_import_date")
    future_dated = sum(
        1 for entry in ledger
        if _iso_date(entry.get("sourceDate")) and _iso_date(entry.get("sourceDate")) > reference
    )
    expired = sum(
        1 for entry in ledger
        if _iso_date(entry.get("expiresAfter")) and _iso_date(entry.get("expiresAfter")) < reference
    )
    invalid_dates = sum(
        1 for entry in ledger
        if not _iso_date(entry.get("sourceDate"))
        or not _iso_date(entry.get("expiresAfter"))
        or (
            _iso_date(entry.get("sourceDate"))
            and _iso_date(entry.get("expiresAfter"))
            and _iso_date(entry.get("expiresAfter")) < _iso_date(entry.get("sourceDate"))
        )
    )
    refresh = sum(1 for entry in ledger if entry.get("needsRefresh"))
    effective = raw_score
    reasons = []
    if valid_urls < len(ledger):
        effective = min(effective, EVIDENCE_LEVEL_SCORES["credible_product"])
        reasons.append(f"{len(ledger) - valid_urls} claim(s) lack a valid http(s) source URL; evidence capped at medium confidence")
    if future_dated:
        effective = min(effective, EVIDENCE_LEVEL_SCORES["application_uncertain"])
        reasons.append(f"{future_dated} evidence claim(s) have a future source date")
    if invalid_dates:
        effective = min(effective, EVIDENCE_LEVEL_SCORES["early_optional"])
        reasons.append(f"{invalid_dates} evidence claim(s) have invalid dates")
    if expired or refresh:
        effective = min(effective, EVIDENCE_LEVEL_SCORES["early_optional"])
        reasons.append(f"{expired} expired and {refresh} refresh-required claim(s)")
    return {
        "rawScore": raw_score,
        "effectiveScore": int(effective),
        "scoreCapApplied": effective < raw_score,
        "sourceComplete": bool(ledger and valid_urls == len(ledger)),
        "decisionGrade": bool(ledger and verified == len(ledger) and valid_urls == len(ledger) and not default_dates and not future_dated and not invalid_dates and not expired and not refresh),
        "asOf": reference.isoformat(),
        "validSourceUrlCount": valid_urls,
        "verifiedClaimCount": verified,
        "defaultSourceDateCount": default_dates,
        "futureSourceDateCount": future_dated,
        "expiredCount": expired,
        "invalidDateCount": invalid_dates,
        "needsRefreshCount": refresh,
        "reasons": reasons,
    }


def evidence_audit(ledger: List[dict], as_of: Optional[str] = None) -> dict:
    claim_count = len(ledger)
    source_urls = sum(1 for e in ledger if valid_source_url(e.get("sourceUrl")))
    reference = _iso_date(as_of) or dt.datetime.now().astimezone().date()
    return {
        "claimCount": claim_count,
        "highConfidenceClaims": sum(1 for e in ledger if str(e.get("confidence", "")).lower() == "high"),
        "needsRefreshCount": sum(1 for e in ledger if e.get("needsRefresh")),
        "missingSourceUrlCount": sum(1 for e in ledger if not valid_source_url(e.get("sourceUrl"))),
        "sourceUrlCount": source_urls,
        "sourceUrlCoverage": rn(source_urls / claim_count * 100.0, 1) if claim_count else None,
        "confidenceCappedCount": sum(1 for e in ledger if e.get("confidenceCapped")),
        "oldestSourceDate": min((e.get("sourceDate") for e in ledger if e.get("sourceDate")), default=None),
        "defaultSourceDateCount": sum(1 for e in ledger if e.get("sourceDateStatus") == "default_research_import_date"),
        "futureSourceDateCount": sum(1 for e in ledger if _iso_date(e.get("sourceDate")) and _iso_date(e.get("sourceDate")) > reference),
        "expiredCount": sum(1 for e in ledger if _iso_date(e.get("expiresAfter")) and _iso_date(e.get("expiresAfter")) < reference),
        "asOf": reference.isoformat(),
    }


def model_workstream(item: dict, hard_review: bool = False) -> str:
    if hard_review:
        return "Data Cleaning"
    direction = item.get("direction") or item.get("bucket") or ""
    if item.get("priorityTier") == "T1 immediate deep dive":
        if any(token.lower() in direction.lower() for token in SUPPLY_CHAIN_DIRECTIONS):
            return "Supply Chain Verification"
        return "One-Page Financial Model"
    if item.get("priorityTier") == "T2 quality discovered":
        return "Valuation Monitor"
    if item.get("priorityTier") == "T3 high-beta option":
        return "Evidence Upgrade"
    return "Quarterly Catalyst Tracking"


def crowding_risk(metrics: dict, priority_tier: str, gate: str) -> dict:
    ret3 = metrics.get("ret3m")
    vs50 = metrics.get("vs50")
    vs200 = metrics.get("vs200")
    overext = aiq.overextension_penalty(metrics)
    if gate == "DATA_REVIEW":
        extension = "unknown"
    elif overext >= 18 or (ret3 is not None and ret3 >= 100) or (vs50 is not None and vs50 >= 35):
        extension = "high"
    elif (ret3 is not None and ret3 >= 50) or (vs50 is not None and vs50 >= 20) or (vs200 is not None and vs200 >= 70):
        extension = "medium"
    else:
        extension = "low"
    narrative = "high" if priority_tier in ("T1 immediate deep dive", "T2 quality discovered") and extension in ("high", "medium") else "medium"
    if priority_tier in ("T4 long-cycle watch", "T5 reference / low AI purity"):
        narrative = "low"
    price_expectation_proxy = "high" if extension == "high" else ("medium" if extension == "medium" else "unknown" if extension == "unknown" else "low")
    return {
        "priceExtension": extension,
        "narrativeHeat": narrative,
        "ownershipRisk": "unknown",
        "earningsExpectationRisk": "not_measured",
        "priceExpectationProxy": price_expectation_proxy,
        "method": "price-extension/narrative heuristic; no ownership or earnings-consensus feed",
        "overextensionPenalty": overext,
    }


def entry_diagnostics(metrics: dict, dq_severity: str, benchmark_metrics: Optional[dict] = None) -> dict:
    rel3m = None
    if metrics.get("ret3m") is not None and benchmark_metrics and benchmark_metrics.get("ret3m") is not None:
        rel3m = rn(float(metrics["ret3m"]) - float(benchmark_metrics["ret3m"]), 2)
    price_ratio = metrics.get("priceTo252dMedian")
    if dq_severity == "hard_review":
        price_position_band = "data_blocked"
    elif price_ratio is None:
        price_position_band = "unknown"
    elif price_ratio >= 1.8:
        price_position_band = "above_trailing_median"
    elif price_ratio <= 0.8:
        price_position_band = "below_trailing_median"
    else:
        price_position_band = "near_trailing_median"
    ret3 = metrics.get("ret3m")
    vol = metrics.get("vol1y")
    price_volatility_risk = "unknown"
    if dq_severity != "hard_review":
        price_volatility_risk = "high" if (ret3 is not None and abs(ret3) >= 80) or (vol is not None and vol >= 65) else "medium"
    return {
        "distanceTo50DMA": metrics.get("vs50"),
        "distanceTo200DMA": metrics.get("vs200"),
        "threeMonthReturn": ret3,
        "relativeStrengthVsBenchmark3M": rel3m,
        "nextEarningsDate": None,
        "earningsCalendarStatus": "unavailable",
        "postEarningsGapRisk": "not_measured",
        "priceVolatilityRisk": price_volatility_risk,
        "valuationBand": "not_measured",
        "valuationDataStatus": "unavailable; price position is not fundamental valuation",
        "pricePositionBand": price_position_band,
        "pricePositionLookbackBars": metrics.get("medianLookbackBars"),
        "dataConfidence": dq_severity,
    }


def instrument_master(item: dict, metrics: dict, dq_severity: str, dq_reasons: List[dict]) -> dict:
    split_status = "needs_manual_review" if dq_reasons else "not_reviewed"
    return {
        "primaryTicker": item.get("ticker"),
        "yahooTicker": item.get("ticker"),
        "aliases": item.get("aliases") or [],
        "listingCurrency": metrics.get("currency"),
        "fxRate": metrics.get("marketCapFxRate"),
        "fxSource": metrics.get("marketCapFxSource"),
        "fxMode": metrics.get("fxMode"),
        "returnFxRate": metrics.get("priceFxRate"),
        "returnFxAsOf": metrics.get("priceFxAsOf"),
        "returnFxSource": metrics.get("priceFxSource"),
        "marketCapUsd": metrics.get("marketCapUsd"),
        "scoringMarketCapUsd": metrics.get("scoringMarketCapUsd"),
        "marketCapAsOf": metrics.get("marketCapAsOf"),
        "marketCapAsOfSource": metrics.get("marketCapAsOfSource"),
        "marketCapQuoteAsOf": metrics.get("marketCapQuoteAsOf"),
        "marketCapDecisionAsOf": metrics.get("marketCapDecisionAsOf"),
        "marketCapAlignmentReason": metrics.get("marketCapAlignmentReason"),
        "marketCapFetchedAt": metrics.get("marketCapFetchedAt"),
        "marketCapPointInTimeCompatible": metrics.get("marketCapPointInTimeCompatible"),
        "marketCapSource": metrics.get("marketCapSource") or ("Yahoo profile normalized to USD" if metrics.get("marketCapUsd") is not None else "missing"),
        "marketCapUnitScale": metrics.get("marketCapUnitScale"),
        "returnBasis": metrics.get("returnBasis"),
        "splitHistoryStatus": split_status,
        "shareCountSource": "Yahoo market cap profile",
        "shareCountPointInTimeVerified": metrics.get("shareCountPointInTimeVerified"),
        "dataConfidence": dq_severity,
    }


def data_quality_severity(metrics: dict, dq_score: int, dq_reasons: List[dict]) -> str:
    rules = {(reason.get("rule") or "") for reason in dq_reasons}
    if not metrics.get("available") or dq_score < 50 or "future_price_date" in rules or "stale_price_history" in rules or "stale_fx_history" in rules:
        return "hard_review"
    ret3 = metrics.get("ret3m")
    price_ratio = metrics.get("priceTo252dMedian")
    if ret3 is not None and abs(float(ret3)) > 250:
        return "hard_review"
    if price_ratio is not None and float(price_ratio) > 6.0:
        return "hard_review"
    if price_ratio is not None and 4.0 <= float(price_ratio) <= 6.0:
        return "verify_before_entry"
    if any(rule in ("fx_conversion_missing", "market_cap_missing", "market_cap_future_as_of", "lagged_price_history", "lagged_fx_history", "limited_price_history") for rule in rules):
        return "verify_before_entry"
    if dq_reasons:
        return "soft_review"
    return "clean"


def entry_eligibility(data_severity: str, crowd: dict, metrics: dict) -> dict:
    blockers = []
    action_ready_blockers = []
    if data_severity == "hard_review":
        blockers.append("hard data review")
    if data_severity == "verify_before_entry":
        blockers.append("verify price/FX/split data before entry review")
    if crowd.get("priceExtension") == "high" or crowd.get("narrativeHeat") == "high":
        action_ready_blockers.append("crowding heat")
    if (metrics.get("vs50") or 0.0) > 20:
        action_ready_blockers.append("price more than 20% above 50DMA")
    if metrics.get("marketCapUsd") is None:
        action_ready_blockers.append("market-cap size data missing")
    if metrics.get("vs50") is None or metrics.get("vs200") is None:
        action_ready_blockers.append("50/200DMA history incomplete")
    return {
        "entryAllowed": not blockers,
        "actionReadyAllowed": not blockers and not action_ready_blockers,
        "blockers": blockers,
        "actionReadyBlockers": action_ready_blockers,
    }


def adjust_entry_score(raw_entry_score: int, data_severity: str, crowd: dict, metrics: dict) -> int:
    if data_severity == "hard_review":
        return 0
    score = raw_entry_score
    if data_severity == "verify_before_entry":
        score = min(score, 64)
    if crowd.get("priceExtension") == "high" or crowd.get("narrativeHeat") == "high":
        score = min(score, 66)
    elif crowd.get("priceExtension") == "medium":
        score = min(score, 72)
    if (metrics.get("vs50") or 0.0) > 20:
        score = min(score, 72)
    return int(round(clamp(score)))


def infer_thesis_tags(item: dict) -> List[str]:
    text = " ".join(str(item.get(k) or "") for k in ("stage", "direction", "bucket", "aiBottleneck", "thesis")).lower()
    tags = []
    rules = [
        ("hyperscaler_ai_capex", ("hyperscaler", "data center", "ai factory", "hbm", "custom asic", "networking", "eda", "advanced manufacturing", "advanced packaging", "substrate", "power / cooling", "cooling", "interconnect", "foundry", "cowos")),
        ("advanced_packaging", ("advanced manufacturing", "advanced packaging", "packaging", "substrate", "abf", "osat", "fcbga", "package")),
        ("custom_asic", ("custom asic", "asic", "soc", "eda", "ip", "cpu ip")),
        ("data_center_power", ("power", "cooling", "data-center", "data center", "electrical")),
        ("physical_ai_edge", ("physical ai", "edge", "sensor", "auto", "industrial", "robotics", "machine vision")),
        ("ai_application_monetization", ("advertising", "application monetization", "content creation", "interactive worlds")),
    ]
    for tag, needles in rules:
        if any(needle in text for needle in needles):
            tags.append(tag)
    return tags or ["non_ai_or_other"]


def portfolio_correlation_bucket(item: dict, thesis_tags: List[str]) -> str:
    stage = item.get("stage") or ""
    if "ai_application_monetization" in thesis_tags or "application monetization" in stage.lower():
        return "ai_application_monetization"
    if any(tag in thesis_tags for tag in ("hyperscaler_ai_capex", "advanced_packaging", "custom_asic", "data_center_power")):
        return "AI_factory_capex"
    if "physical_ai_edge" in thesis_tags or "edge and physical ai" in stage.lower():
        return "physical_ai_edge"
    return "non_ai_or_other"


def research_priority_components(structural: int, evidence: int, setup: int, factors: Dict[str, int], tier: str) -> dict:
    hidden = factors.get("underappreciation", 50)
    proof = factors.get("proofPoints", 50)
    raw_components = {
        "structural": structural * 0.48,
        "evidence": evidence * 0.20,
        "underappreciationOverlay": hidden * 0.17,
        "proofPointsOverlay": proof * 0.10,
        "setup": setup * 0.05,
    }
    tier_bonus = TIER_BONUS.get(tier, -2)
    before_tier = sum(raw_components.values())
    raw_score = before_tier + tier_bonus
    return {
        **{key: rn(value, 3) for key, value in raw_components.items()},
        "tierBonus": tier_bonus,
        "beforeTierBonus": rn(before_tier, 3),
        "rawScore": rn(raw_score, 3),
        "finalScore": int(round(clamp(raw_score))),
        "clamped": raw_score < 0 or raw_score > 100,
    }


def research_priority_score(structural: int, evidence: int, setup: int, factors: Dict[str, int], tier: str) -> int:
    return research_priority_components(structural, evidence, setup, factors, tier)["finalScore"]


def research_action(tier: str, gate: str) -> str:
    if gate == "DATA_REVIEW":
        return "verify price/FX/split data before ranking"
    if gate == "BUILD_MODEL":
        return "build one-page deep-dive model"
    if tier == "T1 immediate deep dive":
        return "deep dive now: segment revenue, catalysts, and rebuttals"
    if tier == "T2 quality discovered":
        return "track valuation, durability, and pullback levels"
    if tier == "T3 high-beta option":
        return "watch evidence and size only after proof improves"
    if tier == "T4 long-cycle watch":
        return "track catalysts quarterly; do not force timing"
    return "keep as reference"


def fetch_prices(tickers: Iterable[str], period: str, no_fetch: bool):
    if no_fetch:
        return None
    all_tickers = sorted(set(tickers) | {BENCHMARK})
    return aiq.load_market_data(all_tickers, period=period, no_fetch=False)


def exposure_for(item: dict, exposures: Dict[str, dict]) -> dict:
    for symbol in [item["ticker"], *(item.get("aliases") or [])]:
        exp = exposures.get(symbol.upper())
        if exp:
            return exp
    return {"held": False, "value": 0.0, "weightPct": 0.0, "unrealPct": None}


def research_gate_for(
    priority_score: int,
    structural: int,
    evidence: int,
    tier: str,
    dq_score: int,
    dq_reasons: List[dict],
    dq_severity: Optional[str] = None,
) -> Tuple[str, str, str, List[dict]]:
    reasons: List[dict] = []
    dq_severity = dq_severity or ("hard_review" if dq_score < 50 else ("soft_review" if dq_reasons else "clean"))
    if dq_severity == "hard_review":
        return "DATA_REVIEW", "caution", "Price or FX data needs review before ranking.", dq_reasons[:3]
    if dq_reasons:
        reasons.extend(dq_reasons[:2])
    if tier == "T1 immediate deep dive" and structural >= 60 and evidence >= 54:
        reasons.append({"rule": "tier1_priority", "detail": f"{tier}; priority {priority_score}, structural {structural}, evidence {evidence}."})
        return "BUILD_MODEL", "good", "Build the one-page deep-dive model first.", reasons[:3]
    if priority_score >= 78 and structural >= 72:
        reasons.append({"rule": "deep_dive", "detail": f"Priority {priority_score}, structural {structural}, evidence {evidence}."})
        return "DEEP_DIVE", "good", "Strong research candidate; validate financial evidence and valuation.", reasons[:3]
    if tier == "T2 quality discovered" and structural >= 70:
        reasons.append({"rule": "quality_discovered", "detail": f"Quality is high but likely partly discovered; priority {priority_score}."})
        return "DEEP_DIVE", "good", "Track durability and price discipline; not a hidden small-cap screen.", reasons[:3]
    if tier == "T3 high-beta option":
        reasons.append({"rule": "high_beta_option", "detail": f"High-upside but fragile candidate; evidence score {evidence}."})
        return "WATCH_EVIDENCE", "caution", "Watch for proof-point upgrades before sizing.", reasons[:3]
    if tier == "T4 long-cycle watch":
        reasons.append({"rule": "long_cycle", "detail": "Likely physical-AI or infrastructure monitor with slower realization."})
        return "TRACK_CATALYST", "caution", "Track catalysts quarterly; timing is not urgent.", reasons[:3]
    if structural >= 58:
        reasons.append({"rule": "speculative", "detail": f"Structural score {structural}; evidence {evidence} still needs improvement."})
        return "SPECULATIVE", "caution", "Keep as optionality, not a core deep-dive name.", reasons[:3]
    reasons.append({"rule": "score_below_threshold", "detail": f"Priority score {priority_score} is below the watch threshold."})
    return "BLOCK", "avoid", "Not enough evidence for the watchlist under this model.", reasons[:3]


def entry_gate_for(
    setup_score: int,
    structural: int,
    risk_score: int,
    metrics: dict,
    dq_score: int,
    dq_reasons: List[dict],
    portfolio_penalty: int,
    portfolio_reasons: List[dict],
    dq_severity: Optional[str] = None,
    action_ready_allowed: bool = True,
) -> Tuple[str, str, List[dict]]:
    reasons: List[dict] = []
    vs200 = metrics.get("vs200")
    vs50 = metrics.get("vs50") or 0.0
    rsi14 = metrics.get("rsi14") or 50.0
    overext = aiq.overextension_penalty(metrics)
    dq_severity = dq_severity or ("hard_review" if dq_score < 50 else ("soft_review" if dq_reasons else "clean"))
    if dq_severity == "hard_review":
        return "BLOCK_DATA", "Hard data issue blocks any entry judgment.", dq_reasons[:3]
    if dq_severity == "verify_before_entry":
        reasons.extend(dq_reasons[:3])
        if not reasons:
            reasons.append({"rule": "verify_before_entry", "detail": "Data confidence allows research but blocks entry review."})
        return "VERIFY_DATA", "Verify price/FX/split data before entry review.", reasons[:3]
    if portfolio_penalty >= 12:
        reasons.extend(portfolio_reasons)
        return "PORTFOLIO_BLOCK", "Existing portfolio concentration blocks new adds.", reasons[:3]
    if risk_score < 35:
        reasons.append({"rule": "risk_floor", "detail": f"Risk score {risk_score} is below 35."})
        return "WAIT_RESET", "Risk score is below the required floor.", reasons[:3]
    if vs200 is not None and vs200 < -3:
        reasons.append({"rule": "trend_break", "detail": f"Price is {vs200:.2f}% below the 200DMA."})
        return "WAIT_RESET", "Trend repair is required before any entry plan.", reasons[:3]
    if overext >= 18:
        reasons.append({"rule": "overextension", "detail": f"Overextension penalty {overext}; vs50={vs50:.2f}%, RSI={rsi14:.1f}."})
        return "WAIT_RESET", "Strong thesis can be researched, but price is stretched.", reasons[:3]
    if setup_score >= 84 and structural >= 75 and risk_score >= 55 and overext < 10 and action_ready_allowed:
        reasons.append({"rule": "action_ready", "detail": f"Setup {setup_score}, structural {structural}, risk {risk_score}; still requires manual valuation and sizing."})
        return "ACTION_READY", "Setup is ready for manual valuation, sizing, and invalidation review.", reasons[:3]
    if setup_score >= 70 and structural >= 65:
        reasons.append({"rule": "setup_review", "detail": f"Setup {setup_score}, structural {structural}, risk {risk_score}."})
        return "ALLOW_REVIEW", "Setup can be reviewed with sizing and invalidation.", reasons[:3]
    if dq_reasons:
        reasons.extend(dq_reasons[:2])
    reasons.append({"rule": "setup_not_ready", "detail": f"Setup score {setup_score} is below 70."})
    return "WAIT_SETUP", "Setup is not strong enough for new capital yet.", reasons[:3]


def percentile(value: Optional[float], values: Iterable[Optional[float]]) -> Optional[int]:
    if not finite(value):
        return None
    return int(round(clamp(aiq.pct_rank(value, values))))


def cluster_role(row: dict) -> str:
    if row.get("dataQuarantined"):
        return "Quarantine"
    direction = (row.get("direction") or row.get("bucket") or "").lower()
    rank = row.get("directionRank") or 99
    if row.get("priorityTier") == "T3 high-beta option":
        return "Option"
    if any(token in direction for token in ("substrate", "material", "optics", "connector", "power / cooling")):
        return "Supplier"
    if rank == 1:
        return "Leader"
    if rank <= 3:
        return "Challenger"
    if row.get("priorityTier") in ("T4 long-cycle watch", "T5 reference / low AI purity"):
        return "Derivative Play"
    return "Challenger"


def assign_queue_ranks(rows: List[dict]) -> None:
    research_rank = 0
    entry_rank = 0
    data_rank = 0
    for row in rows:
        for key in ("researchQueueRank", "entryQueueRank", "dataReviewQueueRank"):
            row[key] = None
        if row.get("dataQuarantined"):
            data_rank += 1
            row["dataReviewQueueRank"] = data_rank
        else:
            research_rank += 1
            row["researchQueueRank"] = research_rank
            if row.get("entryGate") in ("ACTION_READY", "ALLOW_REVIEW"):
                entry_rank += 1
                row["entryQueueRank"] = entry_rank


def build_scores(
    universe: List[dict],
    prices,
    exposures: Optional[Dict[str, dict]] = None,
    profiles: Optional[Dict[str, dict]] = None,
    reference_date: Optional[str] = None,
) -> Tuple[List[dict], Optional[str]]:
    exposures = exposures or {}
    profiles = profiles or {}
    benchmark = None
    benchmark_metrics = None
    if prices is not None and BENCHMARK in getattr(prices, "columns", []):
        benchmark = prices[BENCHMARK]
    reference_date = reference_date or aiq._reference_date_from_prices(prices, BENCHMARK)
    reference_index = getattr(benchmark, "index", None) if benchmark is not None else getattr(prices, "index", None)
    if benchmark is not None:
        benchmark_metrics = aiq.market_metrics(
            benchmark,
            profile={"currency": "USD"},
            reference_date=reference_date,
            reference_index=reference_index,
        )

    metrics_by_ticker: Dict[str, dict] = {}
    latest_date: Optional[str] = None
    for item in universe:
        ticker = item["ticker"]
        profile = {
            "ticker": ticker,
            "currency": aiq.infer_ticker_currency(ticker),
            **(profiles.get(ticker) or {}),
        }
        metrics = {"available": False, **aiq.market_profile_fields(profile)}
        if prices is not None and ticker in getattr(prices, "columns", []):
            currency = (profile.get("currency") or aiq.infer_ticker_currency(ticker)).upper()
            fx_ticker = (aiq.FX_SPECS.get(currency) or (None, None))[0]
            metrics = aiq.market_metrics(
                prices[ticker],
                benchmark=benchmark,
                profile=profile,
                fx_series=aiq._series_column(prices, fx_ticker),
                reference_date=reference_date,
                reference_index=reference_index,
            )
            latest_date = max(latest_date or metrics.get("date"), metrics.get("date") or latest_date)
        metrics_by_ticker[ticker] = metrics

    peer_metrics = list(metrics_by_ticker.values())
    rows = []
    for item in universe:
        metrics = metrics_by_ticker[item["ticker"]]
        factors = {k: float(v) for k, v in (item.get("factors") or {}).items()}
        structural = structural_score(factors)
        trend, risk, tactical, risk_penalty = aiq.market_overlay(metrics, peer_metrics)
        exp = exposure_for(item, exposures)
        p_penalty, p_reasons = aiq.portfolio_penalty(exp)
        dq_score, dq_reasons = aiq.data_quality(metrics)
        dq_severity = data_quality_severity(metrics, dq_score, dq_reasons)
        hard_review = dq_severity == "hard_review"
        evidence_ledger = normalize_evidence_ledger(item)
        evidence_eval = evidence_assessment(evidence_ledger, item.get("evidenceLevel"), reference_date)
        raw_evidence_score = evidence_eval["rawScore"]
        ev_score = evidence_eval["effectiveScore"]
        raw_setup = int(round(clamp(structural * 0.55 + tactical * 0.30 + risk * 0.15 - risk_penalty)))
        raw_entry_score = int(round(clamp(raw_setup - p_penalty)))
        crowd = crowding_risk(metrics, item.get("priorityTier") or "", "DATA_REVIEW" if hard_review else "")
        eligibility = entry_eligibility(dq_severity, crowd, metrics)
        setup = 0 if hard_review else raw_setup
        entry_score = adjust_entry_score(raw_entry_score, dq_severity, crowd, metrics)
        priority_components = research_priority_components(structural, ev_score, entry_score, factors, item.get("priorityTier") or "")
        final = priority_components["finalScore"]
        gate, tone, gate_note, gate_reasons = research_gate_for(
            final,
            structural,
            ev_score,
            item.get("priorityTier") or "",
            dq_score,
            dq_reasons,
            dq_severity,
        )
        crowd = crowding_risk(metrics, item.get("priorityTier") or "", gate)
        eligibility = entry_eligibility(dq_severity, crowd, metrics)
        entry_score = adjust_entry_score(raw_entry_score, dq_severity, crowd, metrics)
        priority_components = research_priority_components(structural, ev_score, entry_score, factors, item.get("priorityTier") or "")
        final = priority_components["finalScore"]
        entry_gate, entry_note, entry_reasons = entry_gate_for(
            entry_score,
            structural,
            risk,
            metrics,
            dq_score,
            dq_reasons,
            p_penalty,
            p_reasons,
            dq_severity,
            eligibility.get("actionReadyAllowed", True),
        )
        queue = "Data Review Queue" if hard_review else "Research Queue"
        if not hard_review and entry_gate in ("ACTION_READY", "ALLOW_REVIEW"):
            queue = "Entry Queue"
        thesis_tags = infer_thesis_tags(item)
        correlation_bucket = portfolio_correlation_bucket(item, thesis_tags)
        row = {
            **item,
            "modelVersion": MODEL_VERSION,
            "factorWeights": FACTOR_WEIGHTS,
            "actionTier": action_tier(item.get("priorityTier") or "", hard_review),
            "modelWorkstream": model_workstream(item, hard_review),
            "queue": queue,
            "dataQuarantined": hard_review,
            "rawEvidenceScore": raw_evidence_score,
            "evidenceScore": ev_score,
            "evidenceAssessment": evidence_eval,
            "evidenceLedger": evidence_ledger,
            "evidenceAudit": evidence_audit(evidence_ledger, reference_date),
            "structuralScore": structural,
            "trendScore": trend,
            "riskScore": risk,
            "tacticalScore": tactical,
            "riskPenalty": risk_penalty,
            "portfolioPenalty": p_penalty,
            "setupScore": setup,
            "entryScore": entry_score,
            "rawSetupScore": raw_setup,
            "rawEntryScore": raw_entry_score,
            "setupFrozen": hard_review,
            "entryEligibility": eligibility,
            "standaloneScore": setup,
            "finalScore": final,
            "researchPriorityScore": final,
            "researchScoreComponents": priority_components,
            "scoreBand": score_band(final),
            "gate": gate,
            "gateFamily": gate_family(gate),
            "tone": tone,
            "gateNote": gate_note,
            "gateReasons": gate_reasons,
            "entryGate": entry_gate,
            "entryGateNote": entry_note,
            "entryGateReasons": entry_reasons,
            "researchAction": research_action(item.get("priorityTier") or "", gate),
            "dataQualityScore": dq_score,
            "dataQualitySeverity": dq_severity,
            "dataQualityReasons": dq_reasons,
            "entryDiagnostics": entry_diagnostics(metrics, dq_severity, benchmark_metrics),
            "crowdingRisk": crowd,
            "instrumentMaster": instrument_master(item, metrics, dq_severity, dq_reasons),
            "thesisTags": thesis_tags,
            "portfolioCorrelationBucket": correlation_bucket,
            "portfolioCorrelationMethod": "qualitative thesis-tag overlap; not a measured return correlation",
            "market": metrics,
            "portfolio": exp,
        }
        rows.append(row)

    ranked_rows = [r for r in rows if not r.get("dataQuarantined")]
    final_values = [r["finalScore"] for r in ranked_rows]
    structural_values = [r["structuralScore"] for r in rows]
    tactical_values = [r["tacticalScore"] for r in ranked_rows]
    by_bucket: Dict[str, List[dict]] = {}
    by_direction: Dict[str, List[dict]] = {}
    for row in rows:
        by_bucket.setdefault(row["bucket"], []).append(row)
        by_direction.setdefault(row.get("direction") or row["bucket"], []).append(row)
    for row in rows:
        is_ranked = not row.get("dataQuarantined")
        peers = [p for p in by_bucket.get(row["bucket"], []) if not p.get("dataQuarantined")]
        direction_peers = [p for p in by_direction.get(row.get("direction") or row["bucket"], []) if not p.get("dataQuarantined")]
        direction_sorted = sorted(direction_peers, key=lambda r: (-r["finalScore"], -r["structuralScore"], r["ticker"]))
        row["universePercentile"] = percentile(row["finalScore"], final_values) if is_ranked else None
        row["structuralPercentile"] = percentile(row["structuralScore"], structural_values)
        row["tacticalPercentile"] = percentile(row["tacticalScore"], tactical_values) if is_ranked else None
        row["bucketSize"] = len(peers)
        row["bucketPercentile"] = percentile(row["finalScore"], [p["finalScore"] for p in peers]) if is_ranked else None
        row["directionSize"] = len(direction_peers)
        row["directionRank"] = next((i + 1 for i, peer in enumerate(direction_sorted) if peer["ticker"] == row["ticker"]), None)
        if not is_ranked:
            row["directionRelativeScore"] = None
            row["directionPeerGroupStatus"] = "data_quarantined_not_ranked"
            row["directionRankLabel"] = "Data review / not ranked"
        elif len(direction_peers) >= 3:
            row["directionRelativeScore"] = percentile(row["finalScore"], [p["finalScore"] for p in direction_peers])
            row["directionPeerGroupStatus"] = "peer_percentile"
            row["directionRankLabel"] = f"{row['directionRank']}/{len(direction_peers)}"
        elif len(direction_peers) == 2:
            row["directionRelativeScore"] = None
            row["directionPeerGroupStatus"] = "two_name_rank_only"
            row["directionRankLabel"] = f"{row['directionRank']}/2 rank only"
        else:
            row["directionRelativeScore"] = None
            row["directionPeerGroupStatus"] = "singleton_no_peer_group"
            row["directionRankLabel"] = "Singleton / no peer group"
        row["clusterRole"] = cluster_role(row)

    rows.sort(key=lambda r: (1 if r.get("dataQuarantined") else 0, tier_rank(r.get("priorityTier")), -r["finalScore"], -r["structuralScore"], r["ticker"]))
    assign_queue_ranks(rows)
    return rows, latest_date


def summarize(rows: List[dict]) -> dict:
    def grouped(key: str, score_key: str = "finalScore") -> List[dict]:
        groups: Dict[str, dict] = {}
        for row in rows:
            name = row.get(key) or "Other"
            group = groups.setdefault(name, {key: name, "count": 0, "avgFinal": 0.0, "avgStructural": 0.0, "leaders": []})
            group["count"] += 1
            group["avgFinal"] += row[score_key]
            group["avgStructural"] += row["structuralScore"]
            if len(group["leaders"]) < 4:
                group["leaders"].append(row["ticker"])
        for group in groups.values():
            group["avgFinal"] = rn(group["avgFinal"] / group["count"], 1)
            group["avgStructural"] = rn(group["avgStructural"] / group["count"], 1)
        return sorted(groups.values(), key=lambda b: (-b["avgFinal"], b[key]))

    gate_counts = {g: sum(1 for r in rows if r["gate"] == g) for g in GATE_ORDER}
    entry_gate_counts = {g: sum(1 for r in rows if r["entryGate"] == g) for g in ENTRY_GATE_ORDER}
    family_counts = {g: sum(1 for r in rows if r["gateFamily"] == g) for g in ("RESEARCH", "WATCH", "BLOCK", "DATA_REVIEW")}
    research_rows = [r for r in rows if not r.get("dataQuarantined")]
    data_rows = [r for r in rows if r.get("dataQuarantined")]
    entry_rows = [r for r in research_rows if r.get("entryGate") in ("ACTION_READY", "ALLOW_REVIEW")]
    tactical = [
        r
        for r in sorted(research_rows, key=lambda x: (-x["entryScore"], -x["tacticalScore"], x["ticker"]))
        if r["entryGate"] not in ("BLOCK", "PORTFOLIO_BLOCK", "BLOCK_DATA", "VERIFY_DATA")
    ][:8]
    deep_dive = [r for r in research_rows if r["priorityTier"] == "T1 immediate deep dive"][:12]
    return {
        "leaders": compact_rows(research_rows[:10]),
        "researchQueue": compact_rows(research_rows[:20]),
        "entryQueue": compact_rows(entry_rows[:12]),
        "dataReviewQueue": compact_rows(data_rows[:12]),
        "buildModel": compact_rows([r for r in research_rows if r["gate"] == "BUILD_MODEL"][:12]),
        "immediateDeepDive": compact_rows(deep_dive),
        "supplyChainVerify": compact_rows([r for r in research_rows if r.get("modelWorkstream") == "Supply Chain Verification"][:12]),
        "onePageModels": compact_rows([r for r in research_rows if r.get("modelWorkstream") == "One-Page Financial Model"][:12]),
        "evidenceUpgrade": compact_rows([r for r in research_rows if r.get("modelWorkstream") == "Evidence Upgrade"][:12]),
        "deepDive": compact_rows([r for r in research_rows if r["gate"] == "DEEP_DIVE"][:10]),
        "watchReset": compact_rows([r for r in research_rows if r["entryGate"] in ("WAIT_RESET", "PORTFOLIO_BLOCK")][:10]),
        "tacticalLeaders": compact_rows(tactical),
        "byBucket": grouped("bucket"),
        "byStage": grouped("stage"),
        "byDirection": grouped("direction"),
        "byPriorityTier": sorted(grouped("priorityTier"), key=lambda b: TIER_ORDER.get(b["priorityTier"], 9)),
        "gateCounts": gate_counts,
        "entryGateCounts": entry_gate_counts,
        "gateFamilyCounts": family_counts,
        "queueCounts": {
            "research": len(research_rows),
            "entry": len(entry_rows),
            "dataReview": len(data_rows),
        },
    }


def compact_rows(rows: List[dict]) -> List[dict]:
    return [
        {
            "ticker": r["ticker"],
            "name": r["name"],
            "bucket": r["bucket"],
            "stage": r.get("stage"),
            "direction": r.get("direction"),
            "priorityTier": r.get("priorityTier"),
            "actionTier": r.get("actionTier"),
            "modelWorkstream": r.get("modelWorkstream"),
            "queue": r.get("queue"),
            "clusterRole": r.get("clusterRole"),
            "portfolioCorrelationBucket": r.get("portfolioCorrelationBucket"),
            "finalScore": r["finalScore"],
            "structuralScore": r["structuralScore"],
            "setupScore": r["setupScore"],
            "entryScore": r.get("entryScore"),
            "tacticalScore": r["tacticalScore"],
            "gate": r["gate"],
            "entryGate": r.get("entryGate"),
            "directionRank": r.get("directionRank"),
            "directionSize": r.get("directionSize"),
            "directionRelativeScore": r.get("directionRelativeScore"),
            "directionPeerGroupStatus": r.get("directionPeerGroupStatus"),
            "directionRankLabel": r.get("directionRankLabel"),
            "percentile": r.get("universePercentile"),
            "dataQualitySeverity": r.get("dataQualitySeverity"),
        }
        for r in rows
    ]


def portfolio_thesis_exposure(rows: List[dict]) -> dict:
    buckets = {bucket: 0.0 for bucket in CORRELATION_BUCKETS}
    held = []
    for row in rows:
        weight = ((row.get("portfolio") or {}).get("weightPct") or 0.0)
        if weight <= 0:
            continue
        bucket = row.get("portfolioCorrelationBucket") or "non_ai_or_other"
        if bucket not in buckets:
            buckets[bucket] = 0.0
        buckets[bucket] += float(weight)
        held.append({"ticker": row["ticker"], "bucket": bucket, "weightPct": rn(weight, 2)})
    total = sum(buckets.values())
    return {
        "byBucket": [{"bucket": k, "weightPct": rn(v, 2)} for k, v in sorted(buckets.items(), key=lambda x: (-x[1], x[0]))],
        "totalMappedWeightPct": rn(total, 2),
        "heldTickers": sorted(held, key=lambda x: (-x["weightPct"], x["ticker"]))[:20],
    }


def model_card(rows: List[dict], latest_date: Optional[str], universe_path: Path) -> dict:
    flags = []
    for row in rows:
        for reason in row.get("dataQualityReasons") or []:
            flags.append({"ticker": row["ticker"], "name": row["name"], "severity": row["dataQualitySeverity"], **reason})
    research_rows = [r for r in rows if not r.get("dataQuarantined")]
    data_rows = [r for r in rows if r.get("dataQuarantined")]
    entry_rows = [r for r in research_rows if r.get("entryGate") in ("ACTION_READY", "ALLOW_REVIEW")]
    ledgers = [r.get("evidenceLedger") or [] for r in rows]
    total_claims = sum(len(ledger) for ledger in ledgers)
    attached_urls = sum(1 for ledger in ledgers for entry in ledger if valid_source_url(entry.get("sourceUrl")))
    evidence_decision_grade = sum(1 for r in rows if (r.get("evidenceAssessment") or {}).get("decisionGrade"))
    return {
        "modelVersion": MODEL_VERSION,
        "dataDate": latest_date,
        "universeFile": universe_path.name,
        "universePathIncluded": False,
        "universeSize": len(rows),
        "factorWeights": FACTOR_WEIGHTS,
        "effectiveResearchWeightsBeforeTierBonus": {
            "bottleneckFit": 0.1344,
            "proofPoints": 0.2152,
            "monetizationPath": 0.0864,
            "underappreciation": 0.2564,
            "executionRiskControl": 0.0576,
            "evidence": 0.20,
            "setup": 0.05,
        },
        "priorityTierBonusIsAdditive": True,
        "frameworkStages": list(FRAMEWORK_STAGES),
        "evidenceLevelScores": EVIDENCE_LEVEL_SCORES,
        "priorityTierOrder": TIER_ORDER,
        "missingPriceCount": sum(1 for r in rows if not (r.get("market") or {}).get("available")),
        "missingMarketCapCount": sum(1 for r in rows if (r.get("market") or {}).get("marketCapUsd") is None),
        "dataReviewCount": sum(1 for r in rows if r.get("gate") == "DATA_REVIEW"),
        "dataQuarantineCount": len(data_rows),
        "researchQueueCount": len(research_rows),
        "entryQueueCount": len(entry_rows),
        "softDataReviewCount": sum(1 for r in rows if r.get("dataQualitySeverity") == "soft_review"),
        "hardDataReviewCount": sum(1 for r in rows if r.get("dataQualitySeverity") == "hard_review"),
        "ledgerSchemaCoverage": rn(sum(1 for ledger in ledgers if ledger) / len(rows) * 100.0, 1) if rows else None,
        "sourceUrlCoverage": rn(attached_urls / total_claims * 100.0, 1) if total_claims else None,
        "sourceUrlAttachedCount": attached_urls,
        "sourceUrlRequiredCount": total_claims,
        "evidenceLedgerCoverage": rn(sum(1 for ledger in ledgers if ledger) / len(rows) * 100.0, 1) if rows else None,
        "evidenceLedgerMissingUrlCount": sum((r.get("evidenceAudit") or {}).get("missingSourceUrlCount", 0) for r in rows),
        "evidenceLedgerRefreshCount": sum((r.get("evidenceAudit") or {}).get("needsRefreshCount", 0) for r in rows),
        "evidenceDefaultSourceDateCount": sum((r.get("evidenceAudit") or {}).get("defaultSourceDateCount", 0) for r in rows),
        "evidenceConfidenceCappedCount": sum((r.get("evidenceAudit") or {}).get("confidenceCappedCount", 0) for r in rows),
        "evidenceScoreCappedCount": sum(1 for r in rows if (r.get("evidenceAssessment") or {}).get("scoreCapApplied")),
        "evidenceDecisionGradeCount": evidence_decision_grade,
        "evidenceDecisionGradePct": rn(evidence_decision_grade / len(rows) * 100.0, 1) if rows else None,
        "instrumentMasterCoverage": rn(sum(1 for r in rows if r.get("instrumentMaster")) / len(rows) * 100.0, 1) if rows else None,
        "portfolioThesisExposure": portfolio_thesis_exposure(rows),
        "portfolioCorrelationSemantics": "Buckets are qualitative thesis-overlap labels, not Pearson/Spearman return correlations.",
        "valuationSemantics": "No fundamental valuation feed is present; pricePositionBand is only price versus its trailing median.",
        "earningsAndOwnershipSemantics": "No earnings-calendar, consensus-expectation, ownership, or observed gap feed is present; related fields are withheld and priceVolatilityRisk is only a proxy.",
        "benchmark": BENCHMARK,
        "thresholds": {
            "buildModelTier": "T1 immediate deep dive",
            "deepDiveStructural": 80,
            "watchEvidenceTier": "T3 high-beta option",
            "riskFloor": 35,
            "actionReadySetup": 84,
            "allowReviewSetup": 70,
            "portfolioBlockPenalty": 12,
        },
        "privateOrUnpricedNamesOmitted": ["ByteDance"],
        "dataQualityFlags": flags[:12],
    }


def fmt_cap(value: Optional[float]) -> str:
    if value is None:
        return "-"
    value = float(value)
    if value >= 1e12:
        return f"${value / 1e12:.2f}T"
    if value >= 1e9:
        return f"${value / 1e9:.1f}B"
    if value >= 1e6:
        return f"${value / 1e6:.0f}M"
    return f"${value:,.0f}"


def pct(value: Optional[float]) -> str:
    if value is None:
        return "-"
    return f"{float(value):+.2f}%"


def direction_score_display(row: dict) -> str:
    if row.get("directionRelativeScore") is None:
        return "N/A"
    return str(row.get("directionRelativeScore"))


def render_report(doc: dict) -> str:
    rows = doc["scores"]
    research_rows = [r for r in rows if not r.get("dataQuarantined")]
    data_rows = [r for r in rows if r.get("dataQuarantined")]
    entry_rows = [r for r in research_rows if r.get("entryGate") in ("ACTION_READY", "ALLOW_REVIEW")]
    summary = doc["summary"]
    model = doc["modelCard"]
    generated = doc["generatedAt"]
    lines = [
        "# AI Old-Capability Watchlist",
        "",
        f"Generated: {generated}",
        f"Market data as of: {doc.get('marketDataAsOf') or 'unavailable'}",
        "",
        "Purpose: rank companies where an old label may hide a new AI bottleneck. This is a research workflow, not investment advice or an order recommendation.",
        "",
        "## Score Model",
        "",
        "Final score is a research-priority score. Market setup is shown separately so a good thesis does not get confused with an immediate entry.",
        "",
        "- Structural score = 28% bottleneck fit + 24% proof points + 18% monetization path + 18% underappreciation + 12% execution-risk control.",
        "- Research-priority score blends structural score, evidence ladder, old-label underappreciation, proof points, current setup, and report-defined priority tier.",
        "- Entry/setup gate uses Yahoo adjusted closes for trend, volatility, moving-average position, drawdown, overextension, and portfolio concentration.",
        "- Hard data review is a quarantine fuse: those rows are removed from the main research queue until price/FX/split/share-count data is checked.",
        "- Direction-relative score is shown only for directions with at least three names; singleton directions show N/A.",
        "- Evidence audit separates ledger schema coverage from source URL coverage so populated slots are not mistaken for cited evidence.",
        "- Evidence labels without a valid source URL are retained as priors but capped at 64/100 in the research-priority calculation; expired, future-dated, or refresh-required claims are capped further.",
        "- Research-priority effective factor weights are disclosed in the model card because proof points and underappreciation are intentionally emphasized both inside and outside the structural score.",
        "- Gates are research workflow labels, not buy/sell recommendations. Any real trade still needs market regime, sizing, invalidation, and concentration checks.",
        "",
        "| Factor | Weight | Meaning |",
        "| --- | ---: | --- |",
    ]
    for key, weight in FACTOR_WEIGHTS.items():
        lines.append(f"| {FACTOR_LABELS[key]} | {weight:.0%} | {key} |")

    lines.extend(
        [
            "",
            "## Research Queue",
            "",
            "| Rank | Ticker | Company | Action tier | Workstream | Direction role | Research gate | Entry gate | Research | Dir score | Setup | Evidence | Structural | Price | 3M | vs50 | Mkt cap | Main reason |",
            "| ---: | --- | --- | --- | --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | --- | ---: | ---: | ---: | --- |",
        ]
    )
    for i, row in enumerate(research_rows[:24], 1):
        market = row.get("market") or {}
        reason = (row.get("gateReasons") or [{}])[0].get("detail", row.get("gateNote", "-"))
        lines.append(
            f"| {i} | {row['ticker']} | {row['name']} | {row.get('actionTier', row.get('priorityTier', '-'))} | {row.get('modelWorkstream', '-')} | "
            f"{row.get('clusterRole', '-')} {row.get('directionRankLabel') or '-'} | {row['gate']} | {row.get('entryGate', '-')} | "
            f"{row['finalScore']} | {direction_score_display(row)} | {row.get('entryScore', '-')} | {row.get('evidenceScore', '-')} (raw {row.get('rawEvidenceScore', '-')}) | {row['structuralScore']} | "
            f"{market.get('displayPrice') or '-'} | "
            f"{pct(market.get('ret3m'))} | {pct(market.get('vs50'))} | {fmt_cap(market.get('marketCapUsd'))} | {reason} |"
        )

    lines.extend(
        [
            "",
            "## Entry Queue",
            "",
            "| Rank | Ticker | Company | Entry gate | Entry score | 50DMA | 200DMA | 3M | Rel 3M vs SPY | Price vs trailing median | Price-volatility proxy |",
            "| ---: | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | --- | --- |",
        ]
    )
    for i, row in enumerate(entry_rows[:12], 1):
        diag = row.get("entryDiagnostics") or {}
        lines.append(
            f"| {i} | {row['ticker']} | {row['name']} | {row.get('entryGate')} | {row.get('entryScore')} | "
            f"{pct(diag.get('distanceTo50DMA'))} | {pct(diag.get('distanceTo200DMA'))} | {pct(diag.get('threeMonthReturn'))} | "
            f"{pct(diag.get('relativeStrengthVsBenchmark3M'))} | {diag.get('pricePositionBand') or '-'} | {diag.get('priceVolatilityRisk') or '-'} |"
        )

    lines.extend(
        [
            "",
            "## Data Quarantine",
            "",
            "| Ticker | Company | Severity | Research score | Entry gate | Issue | Required check |",
            "| --- | --- | --- | ---: | --- | --- | --- |",
        ]
    )
    for row in data_rows[:24]:
        reason = (row.get("dataQualityReasons") or [{}])[0]
        lines.append(
            f"| {row['ticker']} | {row['name']} | {row.get('dataQualitySeverity')} | {row.get('finalScore')} | {row.get('entryGate')} | "
            f"{reason.get('rule', '-')} | {reason.get('detail', row.get('entryGateNote', '-'))} |"
        )

    lines.extend(
        [
            "",
            "## Priority Tiers",
            "",
            "| Tier | Count | Avg priority | Avg structural | Leaders |",
            "| --- | ---: | ---: | ---: | --- |",
        ]
    )
    for tier in summary.get("byPriorityTier") or []:
        lines.append(
            f"| {tier['priorityTier']} | {tier['count']} | {tier['avgFinal']} | {tier['avgStructural']} | {', '.join(tier['leaders'])} |"
        )

    lines.extend(
        [
            "",
            "## Framework Stages",
            "",
            "| Stage | Count | Avg priority | Avg structural | Leaders |",
            "| --- | ---: | ---: | ---: | --- |",
        ]
    )
    for stage in summary.get("byStage") or []:
        lines.append(
            f"| {stage['stage']} | {stage['count']} | {stage['avgFinal']} | {stage['avgStructural']} | {', '.join(stage['leaders'])} |"
        )

    lines.extend(
        [
            "",
            "## Research Directions",
            "",
            "| Direction | Count | Avg priority | Avg structural | Leaders |",
            "| --- | ---: | ---: | ---: | --- |",
        ]
    )
    for bucket in summary.get("byDirection") or []:
        lines.append(
            f"| {bucket['direction']} | {bucket['count']} | {bucket['avgFinal']} | {bucket['avgStructural']} | {', '.join(bucket['leaders'])} |"
        )

    lines.extend(
        [
            "",
            "## Gate Summary",
            "",
            f"- BUILD_MODEL: {model_count(summary, 'BUILD_MODEL')}",
            f"- DEEP_DIVE: {model_count(summary, 'DEEP_DIVE')}",
            f"- WATCH_EVIDENCE / TRACK_CATALYST / SPECULATIVE: {model_count(summary, 'WATCH_EVIDENCE') + model_count(summary, 'TRACK_CATALYST') + model_count(summary, 'SPECULATIVE')}",
            f"- BLOCK / DATA_REVIEW: {model_count(summary, 'BLOCK') + model_count(summary, 'DATA_REVIEW')}",
            f"- Entry ALLOW_REVIEW: {(summary.get('entryGateCounts') or {}).get('ALLOW_REVIEW', 0)}",
            f"- Entry WAIT_RESET / WAIT_SETUP / PORTFOLIO_BLOCK: {(summary.get('entryGateCounts') or {}).get('WAIT_RESET', 0) + (summary.get('entryGateCounts') or {}).get('WAIT_SETUP', 0) + (summary.get('entryGateCounts') or {}).get('PORTFOLIO_BLOCK', 0)}",
            "",
            "## Model Card",
            "",
            f"- Model version: {model.get('modelVersion')}",
            f"- Universe size: {model.get('universeSize')}",
            f"- Benchmark: {model.get('benchmark')}",
            f"- Missing price count: {model.get('missingPriceCount')}",
            f"- Missing market-cap count: {model.get('missingMarketCapCount')}",
            f"- DATA_REVIEW count: {model.get('dataReviewCount')}",
            f"- Data quarantine count: {model.get('dataQuarantineCount')}",
            f"- Research queue count: {model.get('researchQueueCount')}",
            f"- Entry queue count: {model.get('entryQueueCount')}",
            f"- Ledger schema coverage: {model.get('ledgerSchemaCoverage')}%",
            f"- Source URL coverage: {model.get('sourceUrlCoverage')}% ({model.get('sourceUrlAttachedCount')}/{model.get('sourceUrlRequiredCount')})",
            f"- Evidence confidence capped by missing URLs: {model.get('evidenceConfidenceCappedCount')}",
            f"- Evidence numerical score caps: {model.get('evidenceScoreCappedCount')}; decision-grade evidence rows: {model.get('evidenceDecisionGradeCount')}/{model.get('universeSize')}",
            f"- Evidence claims using the import-date fallback instead of an explicit source date: {model.get('evidenceDefaultSourceDateCount')}",
            f"- Instrument master coverage: {model.get('instrumentMasterCoverage')}%",
            f"- Universe file: `{model.get('universeFile')}` (absolute path omitted)",
            "- Private/unpriced names omitted: " + ", ".join(model.get("privateOrUnpricedNamesOmitted") or []),
            "",
            "## KPI Checklist",
            "",
        ]
    )
    for row in research_rows[:16]:
        kpis = ", ".join(row.get("watchKpis") or [])
        catalysts = "; catalysts: " + ", ".join(row.get("catalysts") or []) if row.get("catalysts") else ""
        rebuttals = "; rebuttals: " + ", ".join((row.get("rebuttalChecks") or [])[:2]) if row.get("rebuttalChecks") else ""
        lines.append(f"- {row['ticker']}: {kpis or 'review latest earnings and revenue guide'}{catalysts}{rebuttals}")

    lines.extend(["", "## Evidence Ledger Sample", ""])
    for row in research_rows[:12]:
        ledger = row.get("evidenceLedger") or []
        first = ledger[0] if ledger else {}
        lines.append(
            f"- {row['ticker']}: {first.get('sourceType', '-')} · {first.get('sourceDate', '-')} · "
            f"{first.get('confidence', '-')} · URL={'yes' if valid_source_url(first.get('sourceUrl')) else 'no'} · financial linkage={first.get('financialLink', '-')}"
        )

    thesis = model.get("portfolioThesisExposure") or {}
    if thesis.get("byBucket"):
        lines.extend(["", "## Portfolio Thesis Exposure", "", "| Bucket | Portfolio weight |", "| --- | ---: |"])
        for item in thesis.get("byBucket") or []:
            lines.append(f"| {item.get('bucket')} | {item.get('weightPct')}% |")

    flags = model.get("dataQualityFlags") or []
    if flags:
        lines.extend(["", "## Data Quality Flags", ""])
        for flag in flags:
            lines.append(f"- {flag.get('severity', '-')}: {flag.get('ticker')} {flag.get('rule')} - {flag.get('detail')}")

    return "\n".join(lines) + "\n"


def model_count(summary: dict, gate: str) -> int:
    return int((summary.get("gateCounts") or {}).get(gate, 0))


def queue_payload(rows: List[dict]) -> dict:
    research = [r for r in rows if not r.get("dataQuarantined")]
    entry = [r for r in research if r.get("entryGate") in ("ACTION_READY", "ALLOW_REVIEW")]
    quarantine = [r for r in rows if r.get("dataQuarantined")]
    return {
        "researchQueue": research,
        "entryQueue": entry,
        "dataReviewQueue": quarantine,
    }


def write_csv(rows: List[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "rank",
        "dataReviewRank",
        "ticker",
        "name",
        "stage",
        "direction",
        "priorityTier",
        "actionTier",
        "modelWorkstream",
        "queue",
        "clusterRole",
        "directionRank",
        "directionSize",
        "directionRelativeScore",
        "directionPeerGroupStatus",
        "directionRankLabel",
        "portfolioCorrelationBucket",
        "thesisTags",
        "bucket",
        "gate",
        "entryGate",
        "finalScore",
        "entryScore",
        "rawEvidenceScore",
        "evidenceScore",
        "evidenceDecisionGrade",
        "universePercentile",
        "structuralScore",
        "setupScore",
        "tacticalScore",
        "trendScore",
        "riskScore",
        "price",
        "currency",
        "ret3m",
        "vs50",
        "marketCapUsd",
        "marketCapPointInTimeCompatible",
        "returnBasis",
        "dataQualitySeverity",
        "setupFrozen",
        "entryAllowed",
        "actionReadyAllowed",
        "distanceTo50DMA",
        "distanceTo200DMA",
        "relativeStrengthVsBenchmark3M",
        "valuationBand",
        "pricePositionBand",
        "priceExtension",
        "narrativeHeat",
        "portfolioWeightPct",
        "researchAction",
        "evidenceClaims",
        "sourceUrlCoverage",
        "missingEvidenceUrls",
        "confidenceCapped",
        "catalysts",
        "rebuttalChecks",
        "mainReason",
    ]
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fields)
    writer.writeheader()
    for row in rows:
            market = row.get("market") or {}
            portfolio = row.get("portfolio") or {}
            diag = row.get("entryDiagnostics") or {}
            crowd = row.get("crowdingRisk") or {}
            audit = row.get("evidenceAudit") or {}
            elig = row.get("entryEligibility") or {}
            reason = (row.get("gateReasons") or [{}])[0].get("detail", row.get("gateNote", ""))
            writer.writerow(
                {
                    "rank": row.get("researchQueueRank"),
                    "dataReviewRank": row.get("dataReviewQueueRank"),
                    "ticker": row["ticker"],
                    "name": row["name"],
                    "stage": row.get("stage"),
                    "direction": row.get("direction"),
                    "priorityTier": row.get("priorityTier"),
                    "actionTier": row.get("actionTier"),
                    "modelWorkstream": row.get("modelWorkstream"),
                    "queue": row.get("queue"),
                    "clusterRole": row.get("clusterRole"),
                    "directionRank": row.get("directionRank"),
                    "directionSize": row.get("directionSize"),
                    "directionRelativeScore": row.get("directionRelativeScore"),
                    "directionPeerGroupStatus": row.get("directionPeerGroupStatus"),
                    "directionRankLabel": row.get("directionRankLabel"),
                    "portfolioCorrelationBucket": row.get("portfolioCorrelationBucket"),
                    "thesisTags": " | ".join(row.get("thesisTags") or []),
                    "bucket": row["bucket"],
                    "gate": row["gate"],
                    "entryGate": row.get("entryGate"),
                    "finalScore": row["finalScore"],
                    "entryScore": row.get("entryScore"),
                    "rawEvidenceScore": row.get("rawEvidenceScore"),
                    "evidenceScore": row.get("evidenceScore"),
                    "evidenceDecisionGrade": (row.get("evidenceAssessment") or {}).get("decisionGrade"),
                    "universePercentile": row.get("universePercentile"),
                    "structuralScore": row["structuralScore"],
                    "setupScore": row.get("setupScore"),
                    "tacticalScore": row["tacticalScore"],
                    "trendScore": row["trendScore"],
                    "riskScore": row["riskScore"],
                    "price": market.get("priceLocal"),
                    "currency": market.get("currency"),
                    "ret3m": market.get("ret3m"),
                    "vs50": market.get("vs50"),
                    "marketCapUsd": market.get("marketCapUsd"),
                    "marketCapPointInTimeCompatible": market.get("marketCapPointInTimeCompatible"),
                    "returnBasis": market.get("returnBasis"),
                    "dataQualitySeverity": row.get("dataQualitySeverity"),
                    "setupFrozen": row.get("setupFrozen"),
                    "entryAllowed": elig.get("entryAllowed"),
                    "actionReadyAllowed": elig.get("actionReadyAllowed"),
                    "distanceTo50DMA": diag.get("distanceTo50DMA"),
                    "distanceTo200DMA": diag.get("distanceTo200DMA"),
                    "relativeStrengthVsBenchmark3M": diag.get("relativeStrengthVsBenchmark3M"),
                    "valuationBand": diag.get("valuationBand"),
                    "pricePositionBand": diag.get("pricePositionBand"),
                    "priceExtension": crowd.get("priceExtension"),
                    "narrativeHeat": crowd.get("narrativeHeat"),
                    "portfolioWeightPct": portfolio.get("weightPct"),
                    "researchAction": row.get("researchAction"),
                    "evidenceClaims": audit.get("claimCount"),
                    "sourceUrlCoverage": audit.get("sourceUrlCoverage"),
                    "missingEvidenceUrls": audit.get("missingSourceUrlCount"),
                    "confidenceCapped": audit.get("confidenceCappedCount"),
                    "catalysts": " | ".join(row.get("catalysts") or []),
                    "rebuttalChecks": " | ".join(row.get("rebuttalChecks") or []),
                    "mainReason": reason,
                }
            )
    atomic_write_text(path, buffer.getvalue())


def build_document(args) -> dict:
    universe_path = Path(args.universe).expanduser().resolve()
    universe = load_universe(universe_path)
    tickers = [item["ticker"] for item in universe]
    payload = aiq.read_dashboard_payload(Path(args.dashboard))
    target_as_of = aiq.dashboard_market_as_of(payload)
    profiles = aiq.load_market_profiles(
        tickers,
        no_fetch=args.no_fetch,
        market_data_as_of=target_as_of,
    )
    market_tickers = [*tickers, *aiq.required_fx_tickers(profiles, tickers)]
    raw_prices = fetch_prices(market_tickers, period=args.period, no_fetch=args.no_fetch)
    profiles = aiq.align_market_profile_as_of(profiles, raw_prices, target_as_of)
    raw_rows = len(raw_prices) if raw_prices is not None else 0
    prices = aiq.truncate_market_data(raw_prices, target_as_of)
    aligned_rows = len(prices) if prices is not None else 0
    exposures = aiq.portfolio_exposure(payload)
    rows, latest = build_scores(
        universe,
        prices=prices,
        exposures=exposures,
        profiles=profiles,
        reference_date=target_as_of,
    )
    generated = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    return {
        "schemaVersion": 1,
        "researchOnly": True,
        "decisionGrade": False,
        "title": "AI Old-Capability Watchlist",
        "subtitle": "Companies whose old capabilities may become new AI bottlenecks",
        "generatedAt": generated,
        "marketDataAsOf": latest,
        "dataAlignment": {
            "portfolioPriceAsOf": target_as_of,
            "futureCalendarRowsExcluded": max(0, raw_rows - aligned_rows),
            "policy": "market overlays do not use closes after the dashboard price date; non-USD technical returns include backward-filled daily FX",
        },
        "modelVersion": MODEL_VERSION,
        "factorWeights": FACTOR_WEIGHTS,
        "factorLabels": FACTOR_LABELS,
        "scores": rows,
        "queues": queue_payload(rows),
        "summary": summarize(rows),
        "modelCard": model_card(rows, latest, universe_path),
        "disclaimer": "Research framework only; not investment advice, not an order recommendation.",
    }


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Refresh and score the AI old-capability watchlist.")
    ap.add_argument("--universe", default=str(DEFAULT_UNIVERSE), help="watchlist JSON file")
    ap.add_argument("--period", default="2y", help="Yahoo Finance history window")
    ap.add_argument("--dashboard", default=str(DASHBOARD), help="portfolio dashboard HTML for exposure overlay")
    ap.add_argument("--out-json", default=str(OUT_JSON))
    ap.add_argument("--out-md", default=str(OUT_MD))
    ap.add_argument("--out-csv", default=str(OUT_CSV))
    ap.add_argument("--no-fetch", action="store_true", help="skip Yahoo fetch and produce structural/data-review output")
    args = ap.parse_args(argv)

    doc = build_document(args)
    out_json = Path(args.out_json)
    out_md = Path(args.out_md)
    out_csv = Path(args.out_csv)
    atomic_write_json(out_json, doc)
    atomic_write_text(out_md, render_report(doc))
    write_csv(doc["scores"], out_csv)

    ranked = [row for row in doc["scores"] if not row.get("dataQuarantined")]
    display_rows = ranked[:5] if ranked else (doc.get("queues") or {}).get("dataReviewQueue", [])[:5]
    leaders = ", ".join(f"{r['ticker']} {r['finalScore']} {r['gate']}" for r in display_rows)
    print(f"AI watchlist written: {out_json}")
    print(f"Report: {out_md}")
    print(f"CSV: {out_csv}")
    print(f"Market data as of: {doc.get('marketDataAsOf') or 'unavailable'}")
    print(f"{'Top ranked 5' if ranked else 'Data-review priority 5'}: {leaders}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

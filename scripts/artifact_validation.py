"""Validation, provenance, freshness, and presentation compaction for artifacts.

The dashboard consumes optional research JSON produced by several independent
jobs.  A syntactically valid JSON object is not enough for decision support:
the producer schema, as-of date, and relationship to the portfolio snapshot
must also be known.  This module keeps those checks outside the renderer and
returns a compact, browser-facing document plus an explicit health record.
"""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import math
from pathlib import Path
from typing import Any


ARTIFACT_SPECS = {
    "decision": {
        "path": "decision_analysis.json", "versions": {1}, "legacy": True,
        "required": {"generatedAt": str, "asOf": str, "analysisStatus": str,
                     "candidates": list, "rules": list,
                     "researchOnly": bool, "decisionGrade": bool},
        "asOf": ("asOf",), "generatedAt": ("generatedAt",), "maxAgeDays": 5,
    },
    "aiSemiQuant": {
        "path": "ai_semi_quant.json", "versions": {3},
        "required": {"generatedAt": str, "scores": list, "modelCard": dict,
                     "researchOnly": bool, "decisionGrade": bool},
        "asOf": ("marketDataAsOf",), "generatedAt": ("generatedAt",), "maxAgeDays": 14,
    },
    "aiWatchlist": {
        "path": "ai_watchlist.json", "versions": {1}, "legacy": True,
        "required": {"generatedAt": str, "scores": list, "modelCard": dict,
                     "researchOnly": bool, "decisionGrade": bool},
        "asOf": ("marketDataAsOf",), "generatedAt": ("generatedAt",), "maxAgeDays": 14,
    },
    "aics": {
        "path": "aics.json", "versions": {1},
        "required": {"generatedAt": str, "scores": list, "modelCard": dict,
                     "researchOnly": bool, "decisionGrade": bool},
        "asOf": ("marketDataAsOf",), "generatedAt": ("generatedAt",), "maxAgeDays": 14,
    },
    "marketMass": {
        "path": "market_mass_dashboard.json", "versions": {1}, "legacy": True,
        "required": {"generatedAt": str, "symbols": dict, "profile": dict,
                     "researchOnly": bool, "decisionGrade": bool},
        "asOf": ("symbols", "QQQ", "priceAsOf"), "generatedAt": ("generatedAt",),
        "maxAgeDays": 5,
    },
    "momentumTop3": {
        "path": "momentum_top3/momentum_top3.json", "versions": {2},
        "required": {"generated_at": str, "strategies": list, "window": dict,
                     "researchOnly": bool, "decisionGrade": bool, "methodology": dict},
        "asOf": ("price_freshness", "as_of"), "generatedAt": ("generated_at",),
        "maxAgeDays": 10,
    },
    "financialStatus": {
        "path": "financial_status.json", "versions": {1},
        "required": {"generatedAt": str, "scores": list, "summary": dict,
                     "researchOnly": bool, "decisionGrade": bool},
        "asOf": ("asOfDate",), "generatedAt": ("generatedAt",), "maxAgeDays": 14,
    },
    "closeVsIntraday": {
        "path": "close_vs_intraday.json", "versions": {1},
        "required": {"generatedAt": str, "asOf": str, "windows": dict,
                     "researchOnly": bool, "decisionGrade": bool},
        "asOf": ("asOf",), "generatedAt": ("generatedAt",), "maxAgeDays": 5,
    },
    "memoryFlow": {
        "path": "memory_flow.json", "versions": {1},
        "required": {"generatedAt": str, "asOf": str, "decisionGrade": bool,
                     "hypotheses": list, "symbols": dict, "methodology": dict,
                     "dataGaps": list, "researchOnly": bool},
        "asOf": ("asOf",), "generatedAt": ("generatedAt",), "maxAgeDays": 3,
    },
}


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant {value}")


def _path_value(doc: Any, path: tuple[str, ...]) -> Any:
    value = doc
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _date(value: Any) -> dt.date | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    try:
        return dt.date.fromisoformat(text[:10])
    except ValueError:
        return None


def _timestamp(value: Any) -> dt.datetime | None:
    """Parse an unambiguous ISO timestamp; current schemas require an offset."""
    if value in (None, ""):
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _finite_tree(value: Any) -> bool:
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, dict):
        return all(_finite_tree(v) for v in value.values())
    if isinstance(value, list):
        return all(_finite_tree(v) for v in value)
    return True


def _sample_series(rows: list, max_points: int = 72) -> list:
    """Evenly sample a chart series while retaining regime transition edges."""
    if len(rows) <= max_points:
        return rows
    indexes = {0, len(rows) - 1}
    for i in range(max_points):
        indexes.add(round(i * (len(rows) - 1) / (max_points - 1)))
    for i in range(1, len(rows)):
        if isinstance(rows[i], dict) and isinstance(rows[i - 1], dict):
            if rows[i].get("regime") != rows[i - 1].get("regime"):
                indexes.update((i - 1, i))
    return [rows[i] for i in sorted(indexes)]


def compact_market_mass(doc: dict) -> dict:
    """Remove private provenance and ship only fields rendered by the dashboard."""
    top = {key: copy.deepcopy(doc.get(key)) for key in
           ("schemaVersion", "generatedAt", "researchOnly", "decisionGrade",
            "profile", "universeMode", "warnings", "disclaimer")
           if key in doc}
    top["symbols"] = {}
    current_keys = {
        "as_of", "current_price", "center_price", "quality_score", "distance_z", "regime",
        "volatility_build_up_score", "volatility", "selected_boundary",
    }
    profile_keys = {
        "profile", "lookback", "halfLife", "reliability", "center_price", "quality_score",
        "stability_score", "volatility_build_up_score", "distance_z", "gravity_score",
        "levitation_score", "regime",
    }
    for symbol, raw in (doc.get("symbols") or {}).items():
        if not isinstance(raw, dict):
            continue
        item = {key: copy.deepcopy(raw.get(key)) for key in (
            "role", "priceTicker", "volumeTicker", "volTicker", "fallbackVolTicker", "asOf",
            "priceAsOf", "massAsOf", "historyEnd", "dataStatus", "stale", "staleReason", "warnings",
        ) if key in raw}
        freshness = raw.get("freshness") or {}
        item["freshness"] = {key: freshness.get(key) for key in
                             ("generatedAt", "priceAsOf", "ageCalendarDays", "stale", "staleReason")
                             if key in freshness}
        confidence = raw.get("dashboardConfidence") or {}
        item["dashboardConfidence"] = {key: copy.deepcopy(confidence.get(key)) for key in
                                       ("score", "label", "reasons") if key in confidence}
        current = raw.get("current") or {}
        item["current"] = {key: copy.deepcopy(value) for key, value in current.items()
                           if key in current_keys}
        selected = item["current"].get("selected_boundary")
        item["boundaries"] = [copy.deepcopy(selected)] if isinstance(selected, dict) else []
        item["history"] = _sample_series(copy.deepcopy(raw.get("history") or []))
        pyramid = raw.get("pyramid") or {}
        profiles = {}
        for name, profile in (pyramid.get("profiles") or {}).items():
            if isinstance(profile, dict):
                profiles[name] = {key: copy.deepcopy(value) for key, value in profile.items()
                                  if key in profile_keys}
        item["pyramid"] = {
            "profiles": profiles,
            "agreement": copy.deepcopy(pyramid.get("agreement") or {}),
            "massHealth": {
                key: copy.deepcopy(value)
                for key, value in (pyramid.get("massHealth") or {}).items()
                if key in {"score", "label", "frictionLabel", "reasons"}
            },
        }
        top["symbols"][symbol] = item
    return top


def compact_ai_watchlist(doc: dict) -> dict:
    compact = copy.deepcopy(doc)
    queues = compact.get("queues") or {}
    # The score table already contains the complete ranked research queue.  The
    # original artifact duplicated every large row a second time here.
    queues.pop("researchQueue", None)
    compact["queues"] = queues
    summary = compact.get("summary") or {}
    if isinstance(summary.get("researchQueue"), list):
        summary["researchQueue"] = summary["researchQueue"][:10]
    compact["summary"] = summary
    return compact


def compact_close_vs_intraday(doc: dict) -> dict:
    keep = ("schemaVersion", "generatedAt", "asOf", "researchOnly", "decisionGrade",
            "decisionGradeReason", "source", "fetchMode", "fetchErrors",
            "windows", "dataFreshness", "methodology", "universe")
    return {key: copy.deepcopy(doc.get(key)) for key in keep if key in doc}


def compact_counterfactual(doc: Any) -> Any:
    """Create a presentation DTO without mutating the calculation result."""
    if not isinstance(doc, dict):
        return doc
    compact = {"aggregate": copy.deepcopy(doc.get("aggregate") or {}),
               "mode": doc.get("mode"), "events": []}
    for raw in doc.get("events") or []:
        if not isinstance(raw, dict):
            continue
        event = {key: copy.deepcopy(value) for key, value in raw.items()
                 if key not in {"legIds", "series", "legs"}}
        event["legs"] = copy.deepcopy((raw.get("legs") or [])[:10])
        event["series"] = [
            {key: row.get(key) for key in ("date", "actualPct", "altPct")}
            for row in _sample_series(raw.get("series") or [], max_points=48)
            if isinstance(row, dict)
        ]
        compact["events"].append(event)
    return compact


COMPACTORS = {
    "marketMass": compact_market_mass,
    "aiWatchlist": compact_ai_watchlist,
    "closeVsIntraday": compact_close_vs_intraday,
}


def load_artifact(out_dir: str | Path, key: str, reference_date: str | dt.date | None = None):
    """Return ``(presentation_document, health)`` for one configured artifact."""
    if key not in ARTIFACT_SPECS:
        raise KeyError(f"unknown artifact {key!r}")
    spec = ARTIFACT_SPECS[key]
    path = Path(out_dir) / spec["path"]
    health = {
        "status": "missing", "decisionGrade": False, "reason": "artifact not found",
        "source": spec["path"], "schemaVersion": None, "schemaValidated": False,
        "asOf": None, "generatedAt": None, "ageCalendarDays": None,
    }
    if not path.is_file():
        return None, health
    try:
        raw_bytes = path.read_bytes()
        doc = json.loads(raw_bytes.decode("utf-8-sig"), parse_constant=_reject_constant)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        health.update(status="invalid", reason=f"unreadable JSON: {exc}")
        return None, health
    health.update(bytes=len(raw_bytes), sha256=hashlib.sha256(raw_bytes).hexdigest())
    if not isinstance(doc, dict):
        health.update(status="invalid", reason="top-level value must be an object")
        return None, health
    if not _finite_tree(doc):
        health.update(status="invalid", reason="artifact contains a non-finite number")
        return None, health

    for field, expected_type in spec["required"].items():
        if not isinstance(doc.get(field), expected_type):
            health.update(status="invalid", reason=f"missing or invalid required field {field}")
            return None, health

    version = doc.get("schemaVersion")
    health["schemaVersion"] = version
    legacy = version is None and spec.get("legacy", False)
    if version is not None and version not in spec["versions"]:
        health.update(status="invalid", reason=f"unsupported schemaVersion {version}")
        return None, health
    if version is None and not legacy:
        health.update(status="invalid", reason="schemaVersion is required")
        return None, health
    health["schemaValidated"] = not legacy

    as_of_raw = _path_value(doc, spec["asOf"])
    generated_raw = _path_value(doc, spec["generatedAt"])
    as_of = _date(as_of_raw)
    generated = _timestamp(generated_raw)
    health["asOf"] = str(as_of_raw) if as_of_raw not in (None, "") else None
    health["generatedAt"] = str(generated_raw) if generated_raw not in (None, "") else None
    if not legacy and generated is None:
        health.update(status="invalid", reason="generatedAt must be an ISO timestamp with a UTC offset")
        return None, health
    reference = reference_date if isinstance(reference_date, dt.date) else _date(reference_date)
    status, reasons = ("provisional", ["legacy artifact lacks schemaVersion; regenerate"] if legacy else [])
    if as_of is None:
        reasons.append("artifact has no valid as-of date")
    elif reference is not None:
        age = (reference - as_of).days
        health["ageCalendarDays"] = age
        if age < 0:
            health.update(status="invalid", reason="artifact is from after the portfolio price date")
            return None, health
        if age > spec["maxAgeDays"]:
            status = "stale"
            reasons.append(f"artifact is {age} calendar days behind the portfolio price date")
        elif not legacy:
            status = "fresh"
    elif not legacy and as_of is not None:
        status = "fresh"

    explicit_claim = doc.get("decisionGrade")
    model_claim = (doc.get("modelCard") or {}).get("decisionGrade")
    validation_decision_grade = bool(
        explicit_claim is True and model_claim is not False
        and doc.get("researchOnly") is not True
    )
    if not validation_decision_grade:
        reasons.append(
            "producer marks artifact research-only/non-decision-grade"
            if explicit_claim is False or model_claim is False or doc.get("researchOnly") is True
            else "producer has no explicit decision-grade contract; failed closed"
        )
    if key == "marketMass":
        stale_symbols = sorted(symbol for symbol, item in (doc.get("symbols") or {}).items()
                               if isinstance(item, dict) and item.get("stale"))
        unavailable_symbols = sorted(
            symbol for symbol, item in (doc.get("symbols") or {}).items()
            if isinstance(item, dict) and not isinstance(item.get("current"), dict)
        )
        health["staleSymbolCount"] = len(stale_symbols)
        health["unavailableSymbolCount"] = len(unavailable_symbols)
        if stale_symbols:
            status = "stale"
            reasons.append(f"{len(stale_symbols)} market-mass symbols are stale")
        elif unavailable_symbols:
            if status == "fresh":
                status = "provisional"
            reasons.append(f"{len(unavailable_symbols)} market-mass symbols are unavailable/insufficient-history")
    elif key == "closeVsIntraday" and doc.get("fetchErrors"):
        health["fetchErrorCount"] = len(doc["fetchErrors"])
        if status == "fresh":
            status = "provisional"
        reasons.append("one or more close/intraday fetches failed")
    elif key == "aics":
        validation = (((doc.get("backtest") or {}).get("historyValidation") or {}).get("status"))
        health["validationStatus"] = validation
        if validation and validation not in {"validated", "sufficient_history"}:
            validation_decision_grade = False
            if status == "fresh":
                status = "provisional"
            reasons.append(f"AICS history validation is {validation}")
    elif key == "momentumTop3":
        methodology = doc.get("methodology") or {}
        research_contract = (
            doc.get("researchOnly") is True
            and doc.get("decisionGrade") is False
            and methodology.get("currentUniverseSurvivorship") is True
            and methodology.get("inSampleStrategySelection") is True
            and methodology.get("outOfSampleValidation") is False
            and methodology.get("initialCapitalAnchor") is True
            and methodology.get("fillTiming") == "next trading session close"
        )
        if not research_contract:
            health.update(
                status="invalid",
                decisionGrade=False,
                reason="momentum artifact is missing the required research-only execution/bias contract",
            )
            return None, health
        validation_decision_grade = False
        reasons.append("research-only: current-universe survivorship and in-sample strategy selection")
    elif key == "financialStatus":
        health["dataReviewCount"] = ((doc.get("counts") or {}).get("dataReview") or 0)
    elif key == "memoryFlow" and doc.get("decisionGrade") is not True:
        validation_decision_grade = False
        if status == "fresh":
            status = "provisional"
        reasons.append("research-only: direct leverage-clearing and signed dealer inventory feeds are incomplete")

    health["status"] = status
    health["decisionGrade"] = status == "fresh" and validation_decision_grade
    health["reason"] = "; ".join(dict.fromkeys(reasons)) if reasons else "validated and current"
    try:
        presentation = COMPACTORS.get(key, lambda value: value)(doc)
        health["presentationBytes"] = len(json.dumps(
            presentation, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8"))
    except (TypeError, ValueError) as exc:
        health.update(status="invalid", decisionGrade=False, reason=f"presentation compaction failed: {exc}")
        return None, health
    return presentation, health

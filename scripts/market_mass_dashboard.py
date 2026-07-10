#!/usr/bin/env python3
"""Build the market-mass dashboard artifact.

This producer converts the existing market-mass boundary engine into one
dashboard-friendly JSON file. It is research output only; boundary zones are
probabilistic and are not guaranteed support or resistance.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import glob
import json
import math
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    from scripts.artifact_io import atomic_write_json
except ModuleNotFoundError:  # direct ``python scripts/market_mass_dashboard.py``
    from artifact_io import atomic_write_json


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import market_mass_boundaries as mmb  # noqa: E402


DEFAULT_OUT = ROOT / "output" / "market_mass_dashboard.json"
DEFAULT_HISTORY_BARS = 130
DEFAULT_DASHBOARD_HORIZON = 5
DEFAULT_DASHBOARD_CONFIDENCE = 0.80
DEFAULT_REFERENCE_LANES = ("^NDX", "^GSPC")
FORCED_ANCHORS = ("QQQ", "VOO")
DEFAULT_MAX_STALE_CALENDAR_DAYS = 4
DEFAULT_PYRAMID_PROFILES = ("tactical", "swing", "structural")


@dataclass(frozen=True)
class SymbolConfig:
    symbol: str
    price_ticker: str
    volume_ticker: str | None
    vol_ticker: str | None
    fallback_vol_ticker: str | None
    roles: tuple[str, ...]
    warnings: tuple[str, ...] = ()


def _strict_portfolio_number(value, field, row_number):
    text = str(value or "").strip()
    if text in ("", "--"):
        return None
    cleaned = text.replace(",", "").replace("$", "").replace("+", "")
    negative = cleaned.startswith("(") and cleaned.endswith(")")
    if negative:
        cleaned = cleaned[1:-1]
    try:
        parsed = float(cleaned)
    except ValueError as exc:
        raise ValueError(f"portfolio row {row_number}: invalid {field}") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"portfolio row {row_number}: non-finite {field}")
    return -parsed if negative else parsed


def newest(pattern, input_dir):
    paths = glob.glob(os.path.join(input_dir, pattern))
    return max(paths, key=os.path.getmtime) if paths else None


def parse_portfolio_symbols(path):
    """Return equity symbols from a Fidelity Portfolio Positions CSV."""
    symbols = set()
    if not path or not os.path.exists(path):
        return symbols
    with open(path, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.reader(fh))
    header_idx = next(
        (
            idx for idx, row in enumerate(rows)
            if {"Account Number", "Symbol", "Quantity", "Current Value"}.issubset(
                {cell.strip() for cell in row}
            )
        ),
        None,
    )
    if header_idx is None:
        return symbols
    header = [cell.strip() for cell in rows[header_idx]]
    index = {name: pos for pos, name in enumerate(header)}
    for row_number, raw in enumerate(rows[header_idx + 1 :], start=header_idx + 2):
        if not any(str(cell).strip() for cell in raw):
            continue
        cell = lambda name: raw[index[name]].strip() if index[name] < len(raw) else ""
        if not cell("Account Number"):
            continue
        symbol = cell("Symbol").upper()
        canonical_option = re.fullmatch(
            r"[A-Z0-9.]{1,10}\d{6}[CP]\d+(?:\.\d+)?",
            symbol.replace(" ", "").lstrip("-"),
        )
        if (
            not symbol
            or symbol.endswith("**")
            or symbol.startswith("-")
            or symbol == "PENDING ACTIVITY"
            or canonical_option
        ):
            continue
        shares = _strict_portfolio_number(cell("Quantity"), "Quantity", row_number)
        value = _strict_portfolio_number(cell("Current Value"), "Current Value", row_number)
        if (
            (abs(shares or 0.0) > 0 or abs(value or 0.0) > 0)
            and re.fullmatch(r"[A-Z][A-Z0-9./^-]*", symbol)
        ):
            symbols.add(symbol)
    return symbols


def parse_symbol_list(text):
    if not text:
        return []
    return [s.strip().upper() for s in text.split(",") if s.strip()]


def parse_pyramid_profiles(text):
    profiles = [s.strip().lower() for s in str(text or "").split(",") if s.strip()]
    if not profiles:
        return list(DEFAULT_PYRAMID_PROFILES)
    bad = [p for p in profiles if p not in mmb.GRAVITY_PROFILES]
    if bad:
        allowed = ", ".join(sorted(mmb.GRAVITY_PROFILES))
        raise argparse.ArgumentTypeError(f"unknown pyramid profile(s): {', '.join(bad)}; allowed: {allowed}")
    return list(dict.fromkeys(profiles))


def _date_from_key(value):
    return dt.date.fromisoformat(mmb.date_key(value))


def _label_from_score(score):
    if score >= 0.75:
        return "high"
    if score >= 0.50:
        return "medium"
    return "low"


def build_symbol_config(symbol, held_symbols=None):
    """Choose data proxies for one dashboard symbol."""
    held = {s.upper() for s in (held_symbols or set())}
    sym = symbol.strip().upper()
    roles = []
    warnings = []
    volume_ticker = None
    vol_ticker = None
    fallback_vol_ticker = None

    if sym in held:
        roles.append("holding")
    if sym in FORCED_ANCHORS:
        roles.append("anchor")
    if sym in DEFAULT_REFERENCE_LANES:
        roles.append("reference")

    if sym in {"QQQ", "TQQQ"}:
        vol_ticker = "^VXN"
        fallback_vol_ticker = "^VIX"
    elif sym in {"^NDX", "NDX"}:
        volume_ticker = "QQQ"
        vol_ticker = "^VXN"
        fallback_vol_ticker = "^VIX"
        warnings.append("NDX is an index reference lane; QQQ volume is used only as a tradable participation proxy.")
    elif sym in {"VOO", "SPY", "^GSPC", "GSPC"}:
        if sym in {"^GSPC", "GSPC"}:
            volume_ticker = "SPY"
            warnings.append("S&P 500 is an index reference lane; SPY volume is used only as a tradable participation proxy.")
        vol_ticker = "^VIX"
    else:
        fallback_vol_ticker = "^VIX"
        warnings.append("Single-stock dashboard view uses realized volatility plus broad-market ^VIX proxy; no single-name option IV is included.")

    if not roles:
        roles.append("universe")

    return SymbolConfig(
        symbol=sym,
        price_ticker=sym,
        volume_ticker=volume_ticker,
        vol_ticker=vol_ticker,
        fallback_vol_ticker=fallback_vol_ticker,
        roles=tuple(dict.fromkeys(roles)),
        warnings=tuple(warnings),
    )


def dashboard_profile(args):
    lookback = args.lookback
    half_life = args.half_life
    if args.gravity_profile:
        lookback, half_life = mmb.GRAVITY_PROFILES[args.gravity_profile]
    return {
        "gravityProfile": args.gravity_profile or "custom",
        "lookback": int(lookback),
        "halfLife": float(half_life),
        "historyBars": int(args.history_bars),
        "defaultHorizonDays": int(args.default_horizon),
        "defaultConfidence": float(args.default_confidence),
        "boundaryModel": args.boundary_model,
        "horizons": list(args.horizons),
        "confidences": list(args.confidences),
        "pyramidProfiles": list(args.pyramid_profiles),
    }


def _selected_boundary(boundaries, horizon, confidence):
    best = None
    best_key = None
    for row in boundaries:
        key = (abs(int(row["horizon_days"]) - int(horizon)), abs(float(row["confidence"]) - float(confidence)))
        if best is None or key < best_key:
            best = row
            best_key = key
    return best or {}


def _compact_current(state):
    center = mmb.compact_center(state["center"])
    build = state["volatility_build_up"]
    return {
        "as_of": state["as_of"],
        **center,
        "volatility_build_up_score": build.get("score"),
        "volatility_build_up": build,
        "volatility": state["volatility"],
    }


def _history_row(state, boundary):
    center = state["center"]
    build = state["volatility_build_up"]
    row = {
        "date": mmb.date_key(state["as_of"]),
        "close": center.get("current_price"),
        "center": center.get("center_price"),
        "quality": center.get("quality_score"),
        "buildUp": build.get("score"),
        "distanceZ": center.get("distance_z"),
        "regime": center.get("regime"),
    }
    for key in (
        "lower_boundary",
        "upper_boundary",
        "lower_zone_low",
        "lower_zone_high",
        "upper_zone_low",
        "upper_zone_high",
    ):
        row[key] = boundary.get(key)
    return row


def freshness_metadata(state, market_rows, generated_at, max_stale_calendar_days=DEFAULT_MAX_STALE_CALENDAR_DAYS):
    price_as_of = mmb.date_key(market_rows[-1]["date"])
    mass_as_of = mmb.date_key(state["as_of"])
    if generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=dt.timezone.utc)
    generated_date = generated_at.astimezone(ZoneInfo("America/New_York")).date()
    price_date = _date_from_key(price_as_of)
    age_days = (generated_date - price_date).days
    stale = age_days > max_stale_calendar_days
    stale_reason = None
    if age_days < 0:
        stale = True
        stale_reason = f"price date {price_as_of} is after generated date {generated_date.isoformat()}"
    elif stale:
        stale_reason = f"price data is {age_days} calendar days old"
    return {
        "asOf": mass_as_of,
        "priceAsOf": price_as_of,
        "massAsOf": mass_as_of,
        "historyEnd": price_as_of,
        "generatedAt": generated_at.isoformat(),
        "ageCalendarDays": age_days,
        "stale": stale,
        "staleReason": stale_reason,
    }


def dashboard_confidence(config, state, freshness, warnings):
    center = state["center"]
    vol = state["volatility"]
    quality = max(0.0, min(100.0, center.get("quality_score") or 0.0))
    quality_score = quality / 100.0
    fresh_score = 0.35 if freshness.get("stale") else 1.0

    roles = set(config.roles)
    sym = config.symbol
    if "reference" in roles and config.volume_ticker:
        volume_score = 0.75
        volume_reason = f"reference lane uses {config.volume_ticker} volume proxy"
    elif sym in {"QQQ", "VOO", "SPY", "TQQQ"}:
        volume_score = 1.0
        volume_reason = "direct ETF price and volume"
    else:
        volume_score = 0.85
        volume_reason = "direct symbol volume"

    implied_vol = vol.get("implied_vol")
    if implied_vol is not None and config.vol_ticker:
        vol_score = 1.0
        vol_reason = f"valid volatility proxy {config.vol_ticker}"
    elif implied_vol is not None and config.fallback_vol_ticker:
        vol_score = 0.65
        vol_reason = f"market volatility proxy {config.fallback_vol_ticker}"
    else:
        vol_score = 0.35
        vol_reason = "realized volatility only"

    if "reference" in roles:
        instrument_score = 0.78
        instrument_reason = "index reference lane, not a tradable strike surface"
    elif sym in {"QQQ", "VOO", "SPY"}:
        instrument_score = 1.0
        instrument_reason = "tradable ETF anchor"
    elif sym == "TQQQ":
        instrument_score = 0.72
        instrument_reason = "leveraged ETF boundary inherits path-dependence risk"
    else:
        instrument_score = 0.58
        instrument_reason = "single-stock idiosyncratic gap risk"

    raw = (
        0.35 * quality_score
        + 0.20 * fresh_score
        + 0.18 * vol_score
        + 0.14 * volume_score
        + 0.13 * instrument_score
    )
    penalty = min(0.15, 0.025 * len(warnings))
    score = max(0.0, min(1.0, raw - penalty))

    reasons = [
        "fresh price data" if not freshness.get("stale") else f"stale price data: {freshness.get('staleReason')}",
        volume_reason,
        vol_reason,
        "high center quality" if quality >= 70 else ("usable center quality" if quality >= 55 else "weak center quality"),
        instrument_reason,
    ]
    if warnings:
        reasons.append(f"{len(warnings)} dashboard warning(s)")

    return {
        "score": round(score, 4),
        "label": _label_from_score(score),
        "reasons": reasons,
        "components": {
            "centerQuality": round(quality_score, 4),
            "freshness": round(fresh_score, 4),
            "volatilityProxy": round(vol_score, 4),
            "volumeProxy": round(volume_score, 4),
            "instrument": round(instrument_score, 4),
            "warningPenalty": round(penalty, 4),
        },
    }


def _weighted_average(items, value_key, weight_key="weight", default=None):
    total = 0.0
    weighted = 0.0
    for item in items:
        value = item.get(value_key)
        weight = item.get(weight_key, 0.0)
        if value is None or weight <= 0:
            continue
        weighted += float(value) * float(weight)
        total += float(weight)
    if total <= 0:
        vals = [float(item[value_key]) for item in items if item.get(value_key) is not None]
        return sum(vals) / len(vals) if vals else default
    return weighted / total


def _profile_reliability(center, build):
    quality = center.get("quality_score") or 0.0
    center_weight = center.get("center_weight") or 0.0
    gravity = center.get("gravity") or {}
    gravity_score = gravity.get("gravity_score")
    levitation_score = gravity.get("levitation_score")
    build_score = build.get("score") or 0.0

    gravity_norm = mmb.clamp((gravity_score if gravity_score is not None else 50.0) / 100.0)
    levitation_norm = mmb.clamp((levitation_score if levitation_score is not None else 50.0) / 100.0)
    score = (
        0.43 * mmb.clamp(quality / 100.0)
        + 0.19 * mmb.clamp(center_weight / 0.85)
        + 0.18 * gravity_norm
        + 0.12 * (1.0 - mmb.clamp(build_score / 100.0))
        + 0.08 * (1.0 - levitation_norm)
    )
    regime = center.get("regime")
    if regime == "detached_from_mass":
        score -= 0.14
    elif regime == "no_reliable_center":
        score -= 0.18
    elif regime == "transition_weak_center":
        score -= 0.06
    if abs(center.get("distance_z") or 0.0) > 3.25:
        score -= 0.06
    return mmb.clamp(score)


def _profile_summary(profile_name, state, boundary):
    center = state["center"]
    build = state["volatility_build_up"]
    gravity = center.get("gravity") or {}
    components = center.get("quality_components") or {}
    return {
        "profile": profile_name,
        "lookback": int(mmb.GRAVITY_PROFILES[profile_name][0]),
        "halfLife": float(mmb.GRAVITY_PROFILES[profile_name][1]),
        "reliability": _profile_reliability(center, build),
        "current_price": center.get("current_price"),
        "center_price": center.get("center_price"),
        "quality_score": center.get("quality_score"),
        "quality_components": components,
        "distance_z": center.get("distance_z"),
        "center_shift_z": center.get("center_shift_z"),
        "mass_sigma_pct": center.get("mass_sigma_pct"),
        "mass_quantity_ratio": center.get("mass_quantity_ratio"),
        "center_weight": center.get("center_weight"),
        "volatility_build_up_score": build.get("score"),
        "gravity_score": gravity.get("gravity_score"),
        "levitation_score": gravity.get("levitation_score"),
        "gravity_regime": gravity.get("gravity_regime"),
        "mean_reversion_score": gravity.get("mean_reversion_score"),
        "vol_stability_score": gravity.get("vol_stability_score"),
        "thinness_score": gravity.get("thinness_score"),
        "stability_score": components.get("stability"),
        "regime": center.get("regime"),
        "selected_boundary": boundary,
    }


def _pyramid_agreement(raw_profiles):
    available = [p for p in raw_profiles if p.get("center_log") is not None]
    if not available:
        return {
            "available": False,
            "profileCount": 0,
            "usableCount": 0,
            "activeCount": 0,
            "detachedCount": 0,
            "noCenterCount": 0,
            "centerSpreadPct": None,
            "centerDisagreementZ": None,
            "agreementScore": 0.0,
        }

    weights = [max(float(p.get("reliability") or 0.0), 0.05) for p in available]
    total_weight = sum(weights)
    mean_log = sum(w * p["center_log"] for w, p in zip(weights, available)) / max(total_weight, 1e-12)
    variance = sum(w * (p["center_log"] - mean_log) ** 2 for w, p in zip(weights, available)) / max(total_weight, 1e-12)
    center_std = math.sqrt(max(variance, 0.0))
    avg_sigma = sum(w * max(float(p.get("mass_sigma") or 0.0), 1e-6) for w, p in zip(weights, available)) / max(total_weight, 1e-12)
    logs = [p["center_log"] for p in available]
    center_disagreement_z = center_std / max(avg_sigma, 1e-6)
    center_spread_pct = (math.exp(max(logs) - min(logs)) - 1.0) * 100.0
    regimes = [p.get("regime") for p in available]

    return {
        "available": True,
        "profileCount": len(available),
        "usableCount": sum(1 for p in available if p.get("usable_center")),
        "activeCount": sum(1 for p in available if p.get("active_center")),
        "detachedCount": regimes.count("detached_from_mass"),
        "noCenterCount": regimes.count("no_reliable_center"),
        "transitionCount": regimes.count("transition_weak_center"),
        "centerSpreadPct": center_spread_pct,
        "centerDisagreementZ": center_disagreement_z,
        "agreementScore": (1.0 - mmb.norm01(center_disagreement_z, 0.35, 1.25)) * 100.0,
        "weightedCenterPrice": math.exp(mean_log),
        "averageMassSigmaPct": (math.exp(avg_sigma) - 1.0) * 100.0,
    }


def _mass_health(raw_profiles, agreement):
    if not raw_profiles:
        return {
            "score": 0.0,
            "label": "low_friction_or_no_mass",
            "frictionLabel": "low_friction",
            "reasons": ["no pyramid profiles available"],
            "components": {},
        }

    weighted = []
    for p in raw_profiles:
        weight = max(float(p.get("reliability") or 0.0), 0.05)
        weighted.append({
            **p,
            "weight": weight,
            "quality_norm": mmb.clamp((p.get("quality_score") or 0.0) / 100.0),
            "gravity_norm": mmb.clamp((p.get("gravity_score") if p.get("gravity_score") is not None else 50.0) / 100.0),
            "build_norm": mmb.clamp((p.get("build_up_score") or 0.0) / 100.0),
            "levitation_norm": mmb.clamp((p.get("levitation_score") if p.get("levitation_score") is not None else 50.0) / 100.0),
            "center_weight_norm": mmb.clamp((p.get("center_weight") or 0.0) / 0.85),
        })

    profile_count = max(len(weighted), 1)
    quality = _weighted_average(weighted, "quality_norm", default=0.0)
    gravity = _weighted_average(weighted, "gravity_norm", default=0.0)
    build = _weighted_average(weighted, "build_norm", default=1.0)
    levitation = _weighted_average(weighted, "levitation_norm", default=1.0)
    center_weight = _weighted_average(weighted, "center_weight_norm", default=0.0)
    agreement_score = mmb.clamp((agreement.get("agreementScore") or 0.0) / 100.0)
    active_ratio = agreement.get("activeCount", 0) / profile_count
    usable_ratio = agreement.get("usableCount", 0) / profile_count
    detached_ratio = agreement.get("detachedCount", 0) / profile_count
    no_center_ratio = agreement.get("noCenterCount", 0) / profile_count

    raw = 100.0 * (
        0.25 * quality
        + 0.18 * gravity
        + 0.19 * agreement_score
        + 0.12 * active_ratio
        + 0.10 * usable_ratio
        + 0.09 * (1.0 - build)
        + 0.07 * center_weight
    )
    raw -= 16.0 * mmb.norm01(levitation, 0.55, 0.85)
    raw -= 12.0 * detached_ratio
    raw -= 9.0 * no_center_ratio
    score = mmb.clamp(raw, 0.0, 100.0)

    center_spread_pct = agreement.get("centerSpreadPct")
    coherent_ready = (
        score >= 78.0
        and agreement_score >= 0.70
        and (center_spread_pct is None or center_spread_pct <= 7.0)
        and detached_ratio == 0
        and no_center_ratio == 0
    )

    if coherent_ready:
        label = "coherent_mass"
    elif score >= 60.0:
        label = "working_mass"
    elif score >= 45.0:
        label = "fragile_or_transition"
    else:
        label = "low_friction_or_no_mass"

    disagreement_z = agreement.get("centerDisagreementZ")
    if (
        agreement.get("detachedCount", 0) > 0
        or levitation >= 0.65
        or (disagreement_z is not None and disagreement_z >= 1.25)
    ):
        friction = "low_friction_escape_risk"
    elif coherent_ready:
        friction = "strong_friction"
    elif score >= 60.0:
        friction = "friction_present"
    elif score >= 45.0:
        friction = "weak_friction"
    else:
        friction = "low_friction"

    reasons = []
    if disagreement_z is None:
        reasons.append("profile agreement unavailable")
    elif disagreement_z <= 0.45:
        reasons.append("tactical/swing/structural centers agree")
    elif disagreement_z <= 1.0:
        reasons.append("profile centers are moderately separated")
    else:
        reasons.append("profile centers disagree; one-timescale center may be misleading")

    if quality >= 0.70:
        reasons.append("weighted center quality is strong")
    elif quality >= 0.55:
        reasons.append("weighted center quality is usable")
    else:
        reasons.append("weighted center quality is weak")

    if levitation >= 0.65:
        reasons.append("levitation is high: price may be moving with limited mean-reversion friction")
    elif gravity >= 0.60:
        reasons.append("gravity score supports mean-reversion friction")

    if build >= 0.70:
        reasons.append("volatility build-up is elevated")
    elif build <= 0.45:
        reasons.append("volatility build-up is contained")

    if detached_ratio > 0:
        reasons.append("at least one profile is detached from its mass center")
    if no_center_ratio > 0:
        reasons.append("at least one profile has no reliable center")

    return {
        "score": score,
        "label": label,
        "frictionLabel": friction,
        "reasons": reasons,
        "components": {
            "quality": quality * 100.0,
            "gravity": gravity * 100.0,
            "agreement": agreement_score * 100.0,
            "activeRatio": active_ratio * 100.0,
            "usableRatio": usable_ratio * 100.0,
            "volatilityBuildUp": build * 100.0,
            "levitation": levitation * 100.0,
            "centerWeight": center_weight * 100.0,
            "detachedRatio": detached_ratio * 100.0,
            "noCenterRatio": no_center_ratio * 100.0,
        },
    }


def build_pyramid_payload(market_rows, vol_series, fallback_vol_series, profile):
    profiles = profile.get("pyramidProfiles") or DEFAULT_PYRAMID_PROFILES
    profile_payloads = {}
    raw_profiles = []
    errors = []
    for profile_name in profiles:
        if profile_name not in mmb.GRAVITY_PROFILES:
            errors.append(f"Skipped unknown pyramid profile {profile_name}")
            continue
        lookback, half_life = mmb.GRAVITY_PROFILES[profile_name]
        try:
            state = mmb.analyze_state(
                market_rows,
                vol_series=vol_series or {},
                fallback_vol_series=fallback_vol_series or {},
                lookback=lookback,
                half_life=half_life,
            )
            boundaries = mmb.boundary_rows(
                state,
                [profile["defaultHorizonDays"]],
                [profile["defaultConfidence"]],
                boundary_model=profile["boundaryModel"],
            )
        except Exception as exc:
            errors.append(f"Pyramid profile {profile_name} failed: {type(exc).__name__}: {exc}")
            continue

        boundary = boundaries[0] if boundaries else {}
        summary = _profile_summary(profile_name, state, boundary)
        profile_payloads[profile_name] = summary
        center = state["center"]
        gravity = center.get("gravity") or {}
        raw_profiles.append({
            "name": profile_name,
            "center_log": center.get("center_log"),
            "mass_sigma": center.get("mass_sigma"),
            "quality_score": center.get("quality_score"),
            "build_up_score": state["volatility_build_up"].get("score"),
            "gravity_score": gravity.get("gravity_score"),
            "levitation_score": gravity.get("levitation_score"),
            "center_weight": center.get("center_weight"),
            "distance_z": center.get("distance_z"),
            "regime": center.get("regime"),
            "active_center": center.get("active_center"),
            "usable_center": center.get("usable_center"),
            "reliability": summary["reliability"],
        })

    agreement = _pyramid_agreement(raw_profiles)
    health = _mass_health(raw_profiles, agreement)
    return {
        "profiles": profile_payloads,
        "agreement": agreement,
        "massHealth": health,
        "warnings": errors,
    }


def build_symbol_payload(
    config,
    market_rows,
    price_source,
    vol_series=None,
    vol_source=None,
    vol_error=None,
    fallback_vol_series=None,
    fallback_vol_source=None,
    fallback_vol_error=None,
    profile=None,
    generated_at=None,
    max_stale_calendar_days=DEFAULT_MAX_STALE_CALENDAR_DAYS,
):
    """Build one symbol entry from already-fetched rows.

    This function is intentionally pure enough for unit tests: callers can pass
    synthetic OHLCV and volatility series without touching the network.
    """
    profile = profile or {
        "lookback": 84,
        "halfLife": 21,
        "historyBars": DEFAULT_HISTORY_BARS,
        "defaultHorizonDays": DEFAULT_DASHBOARD_HORIZON,
        "defaultConfidence": DEFAULT_DASHBOARD_CONFIDENCE,
        "boundaryModel": "mass_vol",
        "horizons": list(mmb.DEFAULT_HORIZONS),
        "confidences": list(mmb.DEFAULT_CONFIDENCES),
    }
    warnings = list(config.warnings)
    if vol_error:
        warnings.append(f"Primary volatility fetch issue: {vol_error}")
    if fallback_vol_error:
        warnings.append(f"Fallback volatility fetch issue: {fallback_vol_error}")

    state = mmb.analyze_state(
        market_rows,
        vol_series=vol_series or {},
        fallback_vol_series=fallback_vol_series or {},
        lookback=profile["lookback"],
        half_life=profile["halfLife"],
    )
    boundaries = mmb.boundary_rows(
        state,
        profile["horizons"],
        profile["confidences"],
        boundary_model=profile["boundaryModel"],
    )
    generated_at = generated_at or dt.datetime.now(dt.timezone.utc)
    freshness = freshness_metadata(
        state,
        market_rows,
        generated_at,
        max_stale_calendar_days=max_stale_calendar_days,
    )
    if freshness["stale"]:
        warnings.append(f"Stale market-mass data: {freshness['staleReason']}")

    history = []
    start = max(profile["lookback"] - 1, len(market_rows) - profile["historyBars"])
    for idx in range(start, len(market_rows)):
        rows = market_rows[: idx + 1]
        try:
            hist_state = mmb.analyze_state(
                rows,
                vol_series=vol_series or {},
                fallback_vol_series=fallback_vol_series or {},
                lookback=profile["lookback"],
                half_life=profile["halfLife"],
            )
            hist_boundaries = mmb.boundary_rows(
                hist_state,
                [profile["defaultHorizonDays"]],
                [profile["defaultConfidence"]],
                boundary_model=profile["boundaryModel"],
            )
        except Exception as exc:
            warnings.append(f"Skipped historical state {mmb.date_key(rows[-1]['date'])}: {type(exc).__name__}")
            continue
        history.append(_history_row(hist_state, hist_boundaries[0]))

    current = _compact_current(state)
    current["selected_boundary"] = _selected_boundary(
        boundaries,
        profile["defaultHorizonDays"],
        profile["defaultConfidence"],
    )
    pyramid = build_pyramid_payload(
        market_rows,
        vol_series=vol_series or {},
        fallback_vol_series=fallback_vol_series or {},
        profile=profile,
    )
    warnings.extend(pyramid.get("warnings") or [])

    if current["volatility"].get("implied_vol") is None:
        warnings.append("No implied volatility series was available; boundaries rely on realized volatility.")
    if (current.get("volume_coverage_ratio") or 0.0) < 0.80:
        warnings.append("Mass-window dollar-volume coverage is below 80%; center quality is penalized.")
    if current["volatility"].get("primary_vol_stale"):
        warnings.append("Primary volatility observation was stale and excluded from the boundary blend.")
    if current["volatility"].get("fallback_vol_stale"):
        warnings.append("Fallback volatility observation was stale and excluded from the boundary blend.")
    if (
        current["volatility"].get("primary_vol_invalid")
        or current["volatility"].get("fallback_vol_invalid")
    ):
        warnings.append("A non-positive volatility observation was excluded from the boundary blend.")
    if "Stooq daily CSV fallback" in str(price_source):
        warnings.append("Stooq fallback adjustment policy was not independently verified; long-horizon levels are provisional.")
    warnings = sorted(set(warnings))
    confidence = dashboard_confidence(config, state, freshness, warnings)

    return mmb.rounded_for_json({
        "role": list(config.roles),
        "priceTicker": config.price_ticker,
        "volumeTicker": config.volume_ticker,
        "volTicker": config.vol_ticker,
        "fallbackVolTicker": config.fallback_vol_ticker,
        "asOf": freshness["asOf"],
        "priceAsOf": freshness["priceAsOf"],
        "massAsOf": freshness["massAsOf"],
        "historyEnd": freshness["historyEnd"],
        "stale": freshness["stale"],
        "staleReason": freshness["staleReason"],
        "dataStatus": "available",
        "freshness": freshness,
        "dashboardConfidence": confidence,
        "validation": {
            "decisionGrade": False,
            "researchOnly": True,
            "fresh": not freshness["stale"],
            "impliedVolAvailable": current["volatility"].get("implied_vol") is not None,
            # volatility_context only exposes a selected implied value after
            # its freshness/validity gates; excluded primary data may coexist
            # with a valid fallback.
            "impliedVolFresh": current["volatility"].get("implied_vol") is not None,
            "scope": "probabilistic boundary visualization; not an executable options signal",
            "adjustedPricePolicy": (
                "yfinance_auto_adjust_true"
                if "Yahoo Finance" in str(price_source)
                else "source_adjustment_not_independently_verified"
            ),
        },
        "current": current,
        "boundaries": boundaries,
        "history": history[-profile["historyBars"] :],
        "pyramid": pyramid,
        "warnings": warnings,
        "sources": {
            "price": price_source,
            "primaryVolatility": vol_source,
            "fallbackVolatility": fallback_vol_source,
        },
        "data": {
            "rows": len(market_rows),
            "firstDate": market_rows[0]["date"],
            "lastDate": market_rows[-1]["date"],
        },
    })


def fetch_symbol_payload(
    config,
    profile,
    period,
    interval,
    stooq_fallback=True,
    generated_at=None,
    max_stale_calendar_days=DEFAULT_MAX_STALE_CALENDAR_DAYS,
):
    price_rows, price_source = mmb.fetch_price_rows(
        config.price_ticker,
        period=period,
        interval=interval,
        stooq_fallback=stooq_fallback,
    )
    volume_rows = None
    if config.volume_ticker and config.volume_ticker != config.price_ticker:
        volume_rows, volume_source = mmb.fetch_price_rows(
            config.volume_ticker,
            period=period,
            interval=interval,
            stooq_fallback=stooq_fallback,
        )
        price_source = f"{price_source}; volume proxy {config.volume_ticker} from {volume_source}"

    market_rows = mmb.merge_price_and_volume_rows(price_rows, volume_rows)
    vol_series, vol_source, vol_error = mmb.fetch_vol_series(
        config.vol_ticker,
        period=period,
        interval=interval,
    )
    fallback_series, fallback_source, fallback_error = mmb.fetch_vol_series(
        config.fallback_vol_ticker,
        period=period,
        interval=interval,
    )
    return build_symbol_payload(
        config,
        market_rows,
        price_source,
        vol_series=vol_series,
        vol_source=vol_source,
        vol_error=vol_error,
        fallback_vol_series=fallback_series,
        fallback_vol_source=fallback_source,
        fallback_vol_error=fallback_error,
        profile=profile,
        generated_at=generated_at,
        max_stale_calendar_days=max_stale_calendar_days,
    )


def build_dashboard_payload(symbol_payloads, profile, warnings=None, generated_at=None, universe_mode=None, portfolio_path=None):
    generated_at = generated_at or dt.datetime.now(dt.timezone.utc)
    return {
        "schemaVersion": 1,
        "researchOnly": True,
        "decisionGrade": False,
        "generatedAt": generated_at.isoformat(),
        "profile": profile,
        "universeMode": universe_mode,
        "portfolioSourcePresent": bool(portfolio_path),
        "symbols": symbol_payloads,
        "warnings": warnings or [],
        "validation": {
            "decisionGrade": False,
            "researchOnly": True,
            "failedSymbols": sorted(
                symbol for symbol, payload in symbol_payloads.items()
                if not payload.get("current")
            ),
            "limitations": [
                "Broad volatility proxies do not reproduce single-name option surfaces.",
                "Boundary confidence is a model-health score, not a probability of trade profit.",
                "Fresh current chains, liquidity, portfolio overlap, and max-risk review are required separately.",
            ],
        },
        "disclaimer": "Probabilistic research model only; boundaries are not guaranteed support/resistance and are not financial advice.",
    }


def symbol_sort_key(symbol):
    priority = {"QQQ": 0, "^NDX": 1, "VOO": 2, "^GSPC": 3}
    return (priority.get(symbol, 10), symbol)


def resolve_symbols(args):
    warnings = []
    portfolio = args.portfolio or newest("Portfolio_Positions*.csv", args.input_dir)
    held = parse_portfolio_symbols(portfolio)
    if not portfolio and not args.symbols and not args.anchor_only:
        warnings.append("No Portfolio_Positions CSV found; using forced anchors only.")

    if args.anchor_only:
        symbols = set(FORCED_ANCHORS)
        universe_mode = "anchor_first"
    elif args.symbols:
        symbols = set(parse_symbol_list(args.symbols))
        universe_mode = "explicit_symbols"
    else:
        symbols = set(held)
        universe_mode = "holdings_plus_anchors"
    if not args.no_force_anchors:
        symbols.update(FORCED_ANCHORS)
    if not args.no_reference_lanes:
        symbols.update(DEFAULT_REFERENCE_LANES)
    symbols = {s for s in symbols if s}
    ordered = sorted(symbols, key=symbol_sort_key)
    if args.max_symbols:
        ordered = ordered[: args.max_symbols]
    return ordered, held, portfolio, warnings, universe_mode


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="Build market-mass visualization JSON for the portfolio dashboard.")
    ap.add_argument("--portfolio", help="Portfolio Positions CSV. Defaults to newest in --input-dir.")
    ap.add_argument("--input-dir", default=os.path.join(os.path.expanduser("~"), "Downloads"))
    ap.add_argument("--symbols", help="Comma-separated symbols to cover. Anchors are still included unless --no-force-anchors.")
    ap.add_argument("--anchor-only", action="store_true", help="Generate QQQ/VOO plus reference lanes instead of all holdings.")
    ap.add_argument("--period", default="5y")
    ap.add_argument("--interval", choices=["1d"], default="1d",
                    help="Daily bars only; model annualization and horizons use trading-day units.")
    ap.add_argument("--gravity-profile", choices=sorted(mmb.GRAVITY_PROFILES), default="swing")
    ap.add_argument("--pyramid-profiles", type=parse_pyramid_profiles, default=list(DEFAULT_PYRAMID_PROFILES), help="Comma-separated multi-timescale profiles for mass-health scoring.")
    ap.add_argument("--lookback", type=int, default=84)
    ap.add_argument("--half-life", type=float, default=21)
    ap.add_argument("--history-bars", type=int, default=DEFAULT_HISTORY_BARS)
    ap.add_argument("--horizons", type=lambda s: mmb.parse_num_list(s, int), default=list(mmb.DEFAULT_HORIZONS))
    ap.add_argument("--confidences", type=lambda s: mmb.parse_num_list(s, float), default=list(mmb.DEFAULT_CONFIDENCES))
    ap.add_argument("--default-horizon", type=int, default=DEFAULT_DASHBOARD_HORIZON)
    ap.add_argument("--default-confidence", type=float, default=DEFAULT_DASHBOARD_CONFIDENCE)
    ap.add_argument("--boundary-model", choices=["mass_vol", "ou_hybrid"], default="mass_vol")
    ap.add_argument("--no-reference-lanes", action="store_true")
    ap.add_argument("--no-force-anchors", action="store_true")
    ap.add_argument("--no-stooq-fallback", action="store_true")
    ap.add_argument("--max-stale-calendar-days", type=int, default=DEFAULT_MAX_STALE_CALENDAR_DAYS)
    ap.add_argument("--max-symbols", type=int, default=None, help="Optional safety cap for slow first runs.")
    ap.add_argument("--out-json", default=str(DEFAULT_OUT))
    args = ap.parse_args(argv)
    checks = (
        (args.lookback >= 30, "--lookback must be at least 30"),
        (math.isfinite(args.half_life) and args.half_life > 0, "--half-life must be positive and finite"),
        (args.history_bars > 0, "--history-bars must be positive"),
        (args.default_horizon > 0, "--default-horizon must be positive"),
        (0 < args.default_confidence < 1, "--default-confidence must be between 0 and 1"),
        (args.max_stale_calendar_days >= 0, "--max-stale-calendar-days cannot be negative"),
    )
    for valid, message in checks:
        if not valid:
            ap.error(message)
    if any(horizon <= 0 for horizon in args.horizons):
        ap.error("all --horizons must be positive")
    if any(not 0 < confidence < 1 for confidence in args.confidences):
        ap.error("all --confidences must be between 0 and 1")
    return args


def main(argv=None):
    args = parse_args(argv)
    profile = dashboard_profile(args)
    symbols, held, portfolio, warnings, universe_mode = resolve_symbols(args)
    generated_at = dt.datetime.now(dt.timezone.utc)
    payloads = {}
    print(f"· market-mass dashboard universe: {len(symbols)} symbols ({universe_mode})")
    if portfolio:
        print(f"· portfolio symbols from: {portfolio}")

    for symbol in symbols:
        config = build_symbol_config(symbol, held)
        try:
            print(f"  - {symbol}: fetching {config.price_ticker}")
            payloads[symbol] = fetch_symbol_payload(
                config,
                profile,
                period=args.period,
                interval=args.interval,
                stooq_fallback=not args.no_stooq_fallback,
                generated_at=generated_at,
                max_stale_calendar_days=args.max_stale_calendar_days,
            )
        except Exception as exc:
            payloads[symbol] = {
                "role": list(config.roles),
                "priceTicker": config.price_ticker,
                "volumeTicker": config.volume_ticker,
                "volTicker": config.vol_ticker,
                "fallbackVolTicker": config.fallback_vol_ticker,
                "current": None,
                "boundaries": [],
                "history": [],
                "stale": False,
                "staleReason": None,
                "dataStatus": "unavailable",
                "validation": {
                    "decisionGrade": False,
                    "researchOnly": True,
                    "fresh": False,
                    "scope": "unavailable",
                },
                "warnings": [*config.warnings, f"Market-mass build failed: {type(exc).__name__}: {exc}"],
                "sources": {},
                "data": {},
            }

    out = Path(args.out_json)
    atomic_write_json(
        out,
        build_dashboard_payload(
            payloads,
            profile,
            warnings,
            generated_at=generated_at,
            universe_mode=universe_mode,
            portfolio_path=portfolio,
        ),
    )
    print(f"✓ wrote {out}")


if __name__ == "__main__":
    main()

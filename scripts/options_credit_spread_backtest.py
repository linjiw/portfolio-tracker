#!/usr/bin/env python3
"""Guarded user entry point for the market-mass credit-spread backtester.

The numerical engine lives in :mod:`market_mass_credit_spread_backtest`.  This
wrapper deliberately does not fork that implementation.  It adds two things a
personal decision tool needs at the public CLI boundary:

* fail-closed checks for timing/fill combinations that would otherwise look
  historical while using information unavailable at the claimed entry; and
* a post-run validity audit that makes synthetic pricing, session-count expiry,
  capital overlap, costs, missing data, and calibration limits machine-readable.

Every artifact remains research-only and ``decisionGrade: false``.  The audit
does not turn a model backtest into an executable options signal.
"""

from __future__ import annotations

import collections
import datetime as dt
import importlib.util
import json
import math
from pathlib import Path
import sys

try:
    from scripts.artifact_io import atomic_write_json, atomic_write_text
except ImportError:  # direct ``python scripts/options_credit_spread_backtest.py``
    from artifact_io import atomic_write_json, atomic_write_text


CORE_PATH = Path(__file__).with_name("market_mass_credit_spread_backtest.py")
WRAPPER_AUDIT_VERSION = 1

# These opt-ins are intentionally wrapper-only and are stripped before the
# delegated parser sees argv.  A waiver permits a sensitivity run; it does not
# improve the resulting decision grade.
WRAPPER_FLAGS = {
    "--allow-same-close-sensitivity": "allow_same_close_sensitivity",
    "--allow-session-expiry-sensitivity": "allow_session_expiry_sensitivity",
    "--allow-overlapping-capital-sensitivity": "allow_overlapping_capital_sensitivity",
    "--allow-timestamp-mismatch-sensitivity": "allow_timestamp_mismatch_sensitivity",
}


def load_core_module():
    """Load the canonical engine without executing its CLI block."""
    spec = importlib.util.spec_from_file_location(
        "_ptrak_market_mass_credit_spread_backtest",
        CORE_PATH,
    )
    if spec is None or spec.loader is None:  # pragma: no cover - environment failure
        raise RuntimeError(f"cannot load credit-spread engine: {CORE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def split_wrapper_flags(argv):
    """Return delegated argv and explicit research-sensitivity waivers."""
    delegated = []
    waivers = {name: False for name in WRAPPER_FLAGS.values()}
    for token in list(argv or []):
        key = WRAPPER_FLAGS.get(token)
        if key:
            waivers[key] = True
        else:
            delegated.append(token)
    return delegated, waivers


def _issue(code, severity, message, *, waived=False):
    return {
        "code": code,
        "severity": severity,
        "waivedForSensitivity": bool(waived),
        "message": message,
    }


def preflight_issues(args, waivers=None):
    """Audit parsed core arguments before network access or output mutation."""
    waivers = dict(waivers or {})
    issues = []

    def value(name, default=None):
        return getattr(args, name, default)

    def guarded(code, unsafe, waiver, message):
        if not unsafe:
            return
        allowed = bool(waivers.get(waiver))
        issues.append(
            _issue(code, "warning" if allowed else "block", message, waived=allowed)
        )

    signal_lag = int(value("signal_lag_bars", 1) or 0)
    guarded(
        "same_close_signal_sensitivity",
        signal_lag < 1,
        "allow_same_close_sensitivity",
        "signal_lag_bars < 1 can use the same close that the strategy claims to trade",
    )

    interval = str(value("interval", "1d") or "1d").lower()
    if interval != "1d":
        issues.append(
            _issue(
                "non_daily_interval",
                "block",
                "the engine's hold-bars, weather, and expiry proxy are daily-session models; interval must be 1d",
            )
        )

    fill_model = str(value("entry_fill_model", "synthetic") or "synthetic")
    snapshots = value("use_option_snapshots")
    if fill_model != "synthetic" and not snapshots:
        issues.append(
            _issue(
                "snapshot_fill_without_snapshots",
                "block",
                "a snapshot entry-fill model requires --use-option-snapshots; otherwise credits are synthetic",
            )
        )
    if snapshots and fill_model == "synthetic":
        issues.append(
            _issue(
                "snapshots_with_synthetic_fill",
                "block",
                "--use-option-snapshots requires an explicit snapshot entry-fill model",
            )
        )
    if value("require_snapshot_fills", False) and not snapshots:
        issues.append(
            _issue(
                "required_snapshots_missing",
                "block",
                "--require-snapshot-fills has no meaning without --use-option-snapshots",
            )
        )
    if float(value("min_snapshot_fill_coverage", 0.0) or 0.0) > 0 and not snapshots:
        issues.append(
            _issue(
                "snapshot_coverage_without_snapshots",
                "block",
                "positive snapshot coverage requires archived point-in-time option chains",
            )
        )

    timestamp_policy = str(value("entry_timestamp_policy", "same_day_close"))
    guarded(
        "same_day_open_uses_close_spot",
        timestamp_policy == "same_day_open",
        "allow_timestamp_mismatch_sensitivity",
        "same_day_open snapshot matching is incompatible with strike selection from that day's closing spot",
    )
    guarded(
        "next_open_uses_prior_close_spot",
        timestamp_policy == "next_open",
        "allow_timestamp_mismatch_sensitivity",
        "next_open snapshot matching still selects strikes and reports entry spot from the prior daily close",
    )
    guarded(
        "timestamp_policy_ignored_by_synthetic_pricing",
        fill_model == "synthetic" and timestamp_policy != "same_day_close",
        "allow_timestamp_mismatch_sensitivity",
        "entry timestamp policy affects snapshot lookup only; synthetic pricing always uses entry-close spot",
    )

    entry_weekday = int(value("entry_weekday", 0))
    hold_bars = int(value("hold_bars", 4))
    expiry_mode = str(value("expiry_mode", "weekly_friday"))
    if not 0 <= entry_weekday <= 4:
        issues.append(
            _issue("invalid_entry_weekday", "block", "entry_weekday must be 0..4")
        )
    if hold_bars <= 0:
        issues.append(
            _issue("invalid_hold_bars", "block", "hold_bars must be positive")
        )
    if 0 <= entry_weekday <= 4 and hold_bars > 0 and expiry_mode == "session_count":
        expected_to_friday = 4 - entry_weekday
        guarded(
            "session_expiry_not_weekly_friday",
            hold_bars != expected_to_friday,
            "allow_session_expiry_sensitivity",
            f"hold_bars={hold_bars} from weekday {entry_weekday} is not the normal Friday session offset {expected_to_friday}",
        )
    guarded(
        "potential_overlapping_capital",
        bool(value("allow_overlapping_positions", False)),
        "allow_overlapping_capital_sensitivity",
        "overlapping positions disable the core's default active-capital reservation guard",
    )

    numeric_rules = (
        ("capital", value("capital", 0), lambda x: x > 0, "capital must be > 0"),
        (
            "lookback",
            value("lookback", 0),
            lambda x: x >= 20,
            "lookback must be >= 20 daily sessions",
        ),
        ("half_life", value("half_life", 0), lambda x: x > 0, "half_life must be > 0"),
        (
            "confidence",
            value("confidence", 0),
            lambda x: 0 < x < 1,
            "confidence must be between 0 and 1",
        ),
        ("width", value("width", 0), lambda x: x > 0, "spread width must be > 0"),
        (
            "strike_increment",
            value("strike_increment", 0),
            lambda x: x > 0,
            "strike increment must be > 0",
        ),
        (
            "risk_per_trade_pct",
            value("risk_per_trade_pct", 0),
            lambda x: 0 < x <= 1,
            "risk_per_trade_pct must be in (0, 1]",
        ),
        (
            "max_contracts",
            value("max_contracts", 0),
            lambda x: x >= 1,
            "max_contracts must be >= 1",
        ),
        (
            "profit_take_pct",
            value("profit_take_pct", 0),
            lambda x: 0 < x <= 1,
            "profit_take_pct must be in (0, 1]",
        ),
        (
            "stop_loss_multiple",
            value("stop_loss_multiple", 0),
            lambda x: x > 0,
            "stop_loss_multiple must be > 0",
        ),
        (
            "slippage_per_spread",
            value("slippage_per_spread", 0),
            lambda x: x >= 0,
            "slippage must be >= 0",
        ),
        (
            "exit_slippage_per_spread",
            value("exit_slippage_per_spread", 0.02),
            lambda x: x >= 0,
            "exit slippage must be >= 0",
        ),
        (
            "dividend_yield",
            value("dividend_yield", 0.0),
            lambda x: -0.25 < x < 1.0,
            "dividend yield must be between -0.25 and 1.0",
        ),
        (
            "put_iv_multiplier",
            value("put_iv_multiplier", 1.0),
            lambda x: x > 0,
            "put IV multiplier must be > 0",
        ),
        (
            "call_iv_multiplier",
            value("call_iv_multiplier", 1.0),
            lambda x: x > 0,
            "call IV multiplier must be > 0",
        ),
        (
            "commission_per_option_contract",
            value("commission_per_option_contract", 0),
            lambda x: x >= 0,
            "commission must be >= 0",
        ),
        (
            "snapshot_max_age_days",
            value("snapshot_max_age_days", 0),
            lambda x: x >= 0,
            "snapshot age days must be >= 0",
        ),
        (
            "snapshot_strike_tolerance",
            value("snapshot_strike_tolerance", 0.01),
            lambda x: x >= 0,
            "snapshot strike tolerance must be >= 0",
        ),
        (
            "snapshot_fill_edge",
            value("snapshot_fill_edge", 0.25),
            lambda x: 0 <= x <= 1,
            "snapshot fill edge must be in [0, 1]",
        ),
        (
            "min_snapshot_fill_coverage",
            value("min_snapshot_fill_coverage", 0),
            lambda x: 0 <= x <= 100,
            "snapshot fill coverage must be in [0, 100] percent",
        ),
        (
            "credit_haircut_pct",
            value("credit_haircut_pct", 0),
            lambda x: 0 <= x < 100,
            "credit haircut must be in [0, 100) percent",
        ),
        (
            "max_short_delta",
            value("max_short_delta", 0.35),
            lambda x: 0 < x <= 1,
            "maximum short delta must be in (0, 1]",
        ),
        (
            "risk_free_rate",
            value("risk_free_rate", 0.04),
            lambda x: -0.25 < x < 1,
            "risk-free rate must be between -0.25 and 1.0",
        ),
    )
    for code, raw, predicate, message in numeric_rules:
        try:
            valid = math.isfinite(float(raw)) and predicate(float(raw))
        except (TypeError, ValueError):
            valid = False
        if not valid:
            issues.append(_issue(f"invalid_{code}", "block", message))

    max_age_minutes = value("snapshot_max_age_minutes")
    if max_age_minutes is not None:
        try:
            valid_age = (
                math.isfinite(float(max_age_minutes)) and float(max_age_minutes) >= 0
            )
        except (TypeError, ValueError):
            valid_age = False
        if not valid_age:
            issues.append(
                _issue(
                    "invalid_snapshot_max_age_minutes",
                    "block",
                    "snapshot age minutes must be >= 0",
                )
            )
    elif snapshots:
        issues.append(
            _issue(
                "unbounded_intraday_snapshot_age",
                "warning",
                "snapshot replay has no intraday age cap; same-date quotes may be far from the claimed entry timestamp",
            )
        )

    input_csv = value("input_csv")
    if input_csv and not Path(input_csv).expanduser().exists():
        issues.append(
            _issue(
                "missing_input_csv", "block", f"input CSV does not exist: {input_csv}"
            )
        )
    if snapshots and not Path(str(snapshots)).expanduser().exists():
        issues.append(
            _issue(
                "missing_option_snapshots",
                "block",
                f"option snapshot path does not exist: {snapshots}",
            )
        )
    return issues


def _date(value):
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _capital_overlap_metrics(trades):
    intervals = []
    invalid = 0
    for trade in trades:
        start, end = _date(trade.get("entry_date")), _date(trade.get("exit_date"))
        if start is None or end is None or end < start:
            invalid += 1
            continue
        intervals.append((start, end, trade))
    intervals.sort(key=lambda row: (row[0], row[1]))
    active = []
    strict_pairs = same_day_reuse = 0
    peak = 0
    for start, end, trade in intervals:
        active = [row for row in active if row[1] >= start]
        for _, other_end, _ in active:
            if other_end > start:
                strict_pairs += 1
            else:
                same_day_reuse += 1
        active.append((start, end, trade))
        peak = max(peak, len(active))
    return {
        "invalidIntervals": invalid,
        "strictOverlapPairs": strict_pairs,
        "sameDayExitEntryReusePairs": same_day_reuse,
        "peakConcurrentPositionsInclusive": peak,
        "capitalReservationMode": "core default blocks new entries through the active trade's exit session; overlap audit verifies exported intervals",
    }


def _economics_metrics(trades, commission_per_option_contract=None, tolerance=0.02):
    risk_identity_mismatches = max_loss_mismatches = max_gain_mismatches = 0
    return_denominator_mismatches = pnl_bound_mismatches = 0
    invalid_rows = 0
    embedded_round_trip_cost = 0.0
    expected_round_trip_cost = 0.0
    checked = 0
    for trade in trades:
        try:
            width = float(trade["width"])
            credit = float(trade["credit_per_share"])
            contracts = int(trade["contracts"])
            risk_per_contract = float(trade["risk_per_contract"])
            max_loss = float(trade["max_loss_dollars"])
            max_gain = float(trade["max_gain_dollars"])
            pnl = float(trade["pnl_dollars"])
        except (KeyError, TypeError, ValueError):
            invalid_rows += 1
            continue
        values = (width, credit, risk_per_contract, max_loss, max_gain, pnl)
        if (
            not all(math.isfinite(item) for item in values)
            or width <= 0
            or contracts <= 0
            or credit < 0
            or credit >= width
            or risk_per_contract <= 0
            or max_loss <= 0
            or max_gain <= 0
        ):
            invalid_rows += 1
            continue
        checked += 1
        expected_risk = max(width - credit, 0.0) * 100.0
        if abs(risk_per_contract - expected_risk) > tolerance:
            risk_identity_mismatches += 1
        inferred_cost = max_loss - risk_per_contract * contracts
        embedded_round_trip_cost += max(inferred_cost, 0.0)
        if commission_per_option_contract is None:
            expected_cost = inferred_cost
        else:
            option_contract_sides = (
                8.0 if str(trade.get("side") or "").upper() == "IC" else 4.0
            )
            expected_cost = (
                float(commission_per_option_contract)
                * option_contract_sides
                * contracts
            )
        expected_round_trip_cost += expected_cost
        # Exported credits have four decimals and dollar values have two.  Scale
        # the comparison tolerance so valid multi-contract rows do not fail on
        # serialization rounding alone.
        money_tolerance = tolerance + 0.006 * max(contracts, 1)
        expected_loss = risk_per_contract * contracts + expected_cost
        if abs(max_loss - expected_loss) > money_tolerance:
            max_loss_mismatches += 1
        expected_gain = credit * 100.0 * contracts - expected_cost
        if abs(max_gain - expected_gain) > money_tolerance:
            max_gain_mismatches += 1
        if pnl < -max_loss - money_tolerance or pnl > max_gain + money_tolerance:
            pnl_bound_mismatches += 1
        reported_ror = trade.get("return_on_risk_pct")
        expected_ror = pnl / max_loss * 100.0 if max_loss else None
        try:
            ror_mismatch = (
                expected_ror is None
                or reported_ror is None
                or not math.isfinite(float(reported_ror))
                or abs(float(reported_ror) - expected_ror) > 0.03
            )
        except (TypeError, ValueError):
            ror_mismatch = True
        if ror_mismatch:
            return_denominator_mismatches += 1
    return {
        "tradesChecked": checked,
        "invalidEconomicRowCount": invalid_rows,
        "riskIdentityMismatchCount": risk_identity_mismatches,
        "maxLossIdentityMismatchCount": max_loss_mismatches,
        "maxGainIdentityMismatchCount": max_gain_mismatches,
        "pnlOutsideDefinedRiskBoundsCount": pnl_bound_mismatches,
        "returnOnRiskDenominatorMismatchCount": return_denominator_mismatches,
        "embeddedRoundTripCommissionDollars": round(embedded_round_trip_cost, 2),
        "expectedRoundTripCommissionDollars": round(expected_round_trip_cost, 2),
        "returnOnRiskDenominator": "max loss including modeled round-trip commissions",
        "positionSizingDenominator": "max spread loss before commissions",
    }


def _summary_consistency_metrics(trades, summary, initial_capital, tolerance=0.05):
    """Recompute headline aggregates from exported trades and compare them."""
    pnl_values = []
    risk_values = []
    wins = 0
    invalid_trade_rows = 0
    for trade in trades:
        try:
            pnl = float(trade["pnl_dollars"])
            risk = float(trade["max_loss_dollars"])
        except (KeyError, TypeError, ValueError):
            invalid_trade_rows += 1
            continue
        if not math.isfinite(pnl) or not math.isfinite(risk):
            invalid_trade_rows += 1
            continue
        pnl_values.append(pnl)
        risk_values.append(risk)
        wins += int(bool(trade.get("win", pnl > 0)))

    total_pnl = sum(pnl_values)
    total_risk = sum(risk_values)
    losses = len(trades) - wins
    expected = {
        "trades": len(trades),
        "wins": wins,
        "losses": losses,
        "total_pnl_dollars": total_pnl,
        "final_equity": float(initial_capital) + total_pnl,
        "total_risk_dollars": total_risk,
        "total_return_pct": (
            total_pnl / float(initial_capital) * 100.0 if initial_capital else None
        ),
        "return_on_total_risk_pct": (
            total_pnl / total_risk * 100.0 if total_risk else None
        ),
    }
    integer_fields = {"trades", "wins", "losses"}
    mismatches = []
    checked = 0
    for field, expected_value in expected.items():
        if field not in summary or summary.get(field) is None or expected_value is None:
            continue
        checked += 1
        try:
            reported = float(summary[field])
            delta = abs(reported - float(expected_value))
            allowed = 0.0 if field in integer_fields else tolerance
            mismatch = not math.isfinite(reported) or delta > allowed
        except (TypeError, ValueError):
            reported = summary.get(field)
            mismatch = True
        if mismatch:
            mismatches.append(
                {
                    "field": field,
                    "reported": reported,
                    "recomputed": round(float(expected_value), 6),
                }
            )
    return {
        "fieldsChecked": checked,
        "mismatchCount": len(mismatches),
        "mismatches": mismatches,
        "invalidTradeRows": invalid_trade_rows,
    }


def _calendar_metrics(trades):
    valid_expiries = []
    before_entry = invalid_dates = adjusted = unexpected_nonfriday = 0
    for trade in trades:
        entry, expiry = _date(trade.get("entry_date")), _date(trade.get("expiry_date"))
        if entry is None or expiry is None:
            invalid_dates += 1
            continue
        if expiry < entry:
            before_entry += 1
        valid_expiries.append(expiry)
        expected_adjustment = bool(
            trade.get("expiry_mode") == "weekly_friday"
            and trade.get("expiry_calendar_adjusted")
            and _date(trade.get("expiry_target_date")) is not None
            and expiry < _date(trade.get("expiry_target_date"))
        )
        if expected_adjustment:
            adjusted += 1
        elif expiry.weekday() != 4:
            unexpected_nonfriday += 1
    friday = sum(1 for expiry in valid_expiries if expiry.weekday() == 4)
    return {
        "expiryModel": "next Friday with prior-observed-session adjustment; exchange option calendar not loaded",
        "validExpiryDates": len(valid_expiries),
        "fridayExpiryCount": friday,
        "nonFridayExpiryCount": len(valid_expiries) - friday,
        "calendarAdjustedExpiryCount": adjusted,
        "unexpectedNonFridayExpiryCount": unexpected_nonfriday,
        "expiryBeforeEntryCount": before_entry,
        "invalidEntryOrExpiryDateCount": invalid_dates,
        "holidayAndEarlyCloseCalendarChecked": False,
    }


def build_validity_audit(payload, args, waivers=None, issues=None):
    """Build machine-readable quantitative caveats from one completed run."""
    payload = payload or {}
    baseline = payload.get("baseline") or {}
    trades = list(baseline.get("trades") or [])
    summary = baseline.get("summary") or {}
    skipped = list(baseline.get("skipped") or [])
    issues = list(issues if issues is not None else preflight_issues(args, waivers))
    overlap = _capital_overlap_metrics(trades)
    economics = _economics_metrics(
        trades,
        commission_per_option_contract=getattr(
            args, "commission_per_option_contract", None
        ),
    )
    summary_consistency = _summary_consistency_metrics(
        trades,
        summary,
        getattr(args, "capital", 0.0),
    )
    calendar = _calendar_metrics(trades)

    skipped_counts = collections.Counter(
        str(row.get("reason") or "unknown") for row in skipped
    )
    context_errors = sum(
        count
        for reason, count in skipped_counts.items()
        if reason.startswith("context_error")
    )
    missing_expiry = sum(
        count for reason, count in skipped_counts.items() if "missing_expiry" in reason
    )
    snapshot_rejections = len(baseline.get("snapshot_rejections") or [])
    snapshot_mode_requested = (
        str(getattr(args, "entry_fill_model", "synthetic")) != "synthetic"
    )
    snapshot_coverage_gate = summary.get("snapshot_coverage_gate_pass")
    snapshot_coverage_failure = (
        snapshot_mode_requested and snapshot_coverage_gate is not True
    )

    snapshot_entries = sum(
        1
        for trade in trades
        if str(trade.get("entry_credit_source") or "").startswith("snapshot:")
    )
    synthetic_entries = len(trades) - snapshot_entries
    pricing_class = (
        "archived_snapshot_entry_modeled_exit"
        if snapshot_entries and not synthetic_entries
        else (
            "mixed_snapshot_and_synthetic_entry_modeled_exit"
            if snapshot_entries
            else "historical_underlying_synthetic_option_entry_and_exit"
        )
    )

    blocking_findings = (
        economics["invalidEconomicRowCount"]
        + economics["riskIdentityMismatchCount"]
        + economics["maxLossIdentityMismatchCount"]
        + economics["maxGainIdentityMismatchCount"]
        + economics["pnlOutsideDefinedRiskBoundsCount"]
        + economics["returnOnRiskDenominatorMismatchCount"]
        + overlap["invalidIntervals"]
        + overlap["strictOverlapPairs"]
        + overlap["sameDayExitEntryReusePairs"]
        + calendar["unexpectedNonFridayExpiryCount"]
        + calendar["expiryBeforeEntryCount"]
        + calendar["invalidEntryOrExpiryDateCount"]
        + summary_consistency["mismatchCount"]
        + summary_consistency["invalidTradeRows"]
        + int(snapshot_coverage_failure)
    )
    research_label = "BLOCK" if not trades or blocking_findings else "WATCH"
    sweep = payload.get("sweep")
    return {
        "schemaVersion": WRAPPER_AUDIT_VERSION,
        "decisionGrade": False,
        "liveDecisionLabel": "BLOCK",
        "researchUseLabel": research_label,
        "preflightIssues": issues,
        "waivers": dict(waivers or {}),
        "signalAndFill": {
            "signalLagBars": getattr(args, "signal_lag_bars", None),
            "sameBarLookaheadProtected": (getattr(args, "signal_lag_bars", 0) or 0)
            >= 1,
            "entryTimestampPolicy": getattr(args, "entry_timestamp_policy", None),
            "timestampPolicyAppliesToSyntheticPricing": False,
            "entryPricingClassification": pricing_class,
            "observedUnderlyingHistory": True,
            "snapshotEntryCount": snapshot_entries,
            "syntheticEntryCount": synthetic_entries,
            "historicalOptionExitCount": 0,
            "exitMarksModeled": True,
        },
        "calendar": calendar,
        "economics": economics,
        "summaryConsistency": summary_consistency,
        "executionCosts": {
            "commissionPerOptionContract": getattr(
                args, "commission_per_option_contract", None
            ),
            "syntheticEntrySlippagePerShare": getattr(
                args, "slippage_per_spread", None
            ),
            "creditHaircutPct": getattr(args, "credit_haircut_pct", None),
            "exitSlippagePerVertical": getattr(
                args, "exit_slippage_per_spread", None
            ),
            "exitSlippageModeled": getattr(
                args, "exit_slippage_per_spread", None
            ) is not None,
            "marketImpactModeled": False,
            "assignmentExerciseFeesModeled": False,
        },
        "valuationModel": {
            "syntheticOptionModel": "Black-Scholes European-style theoretical value",
            "dividendYieldModeled": True,
            "dividendYieldInput": getattr(args, "dividend_yield", None),
            "exitVolatilityPath": "dated broad volatility proxy is re-estimated at modeled exit closes; entry proxy is the fallback",
            "putIvMultiplier": getattr(args, "put_iv_multiplier", None),
            "callIvMultiplier": getattr(args, "call_iv_multiplier", None),
            "volatilitySurfaceAndSkewModeled": "constant wing multipliers only; no historical surface",
            "observedExitBidAsk": False,
            "intradayThresholdOrderingKnown": False,
        },
        "capital": overlap,
        "missingData": {
            "skippedCandidateCount": len(skipped),
            "contextErrorCount": context_errors,
            "missingExpiryCount": missing_expiry,
            "snapshotRejectionCount": snapshot_rejections,
            "snapshotModeRequested": snapshot_mode_requested,
            "snapshotCoverageGatePass": snapshot_coverage_gate,
            "snapshotCoverageGateFailure": snapshot_coverage_failure,
            "syntheticFallbackTradeCount": summary.get(
                "trades_falling_back_to_synthetic"
            ),
            "topSkipReasons": [
                {"reason": reason, "count": count}
                for reason, count in skipped_counts.most_common(10)
            ],
        },
        "calibration": {
            "gridSweepRun": bool(sweep),
            "chronologicalHoldoutReported": bool(
                sweep and (sweep.get("top_test_from_train") or [])
            ),
            "nestedWalkForward": bool(
                (sweep or {}).get("rolling_walk_forward", {}).get(
                    "nested_model_selection"
                )
            ),
            "leakageCaveat": (
                "each calendar test window selects parameters only from its preceding training window; candidate-grid design and repeated research use can still overfit"
                if (sweep or {}).get("rolling_walk_forward", {}).get(
                    "nested_model_selection"
                )
                else "rolling calendar results are not nested model selection and may reuse configurations selected with later data"
                if sweep
                else "baseline parameters are not independently calibrated within this run; prior human/model selection may embed research reuse"
            ),
        },
        "sample": {
            "tradeCount": len(trades),
            "summaryTradeCount": summary.get("trades"),
            "pricingSource": summary.get("pricing_source"),
        },
        "limitations": [
            "weekly expiry targets Friday and uses the prior observed session when Friday is absent; no authoritative exchange holiday calendar is loaded",
            "entry chains may be archived, but every exit and intraperiod stop/profit mark is model-priced",
            "daily OHLC cannot prove intraday ordering of touch, profit-take, and stop events",
            "entry timestamp policy governs snapshot matching only; synthetic entries remain close-based theoretical marks",
            "synthetic exits include a fixed slippage input but omit observed bid/ask, market impact, early exercise, assignment/margin path, and taxes",
            "Black-Scholes marks include a constant dividend yield, dated broad volatility proxies, and fixed wing IV multipliers, not a historical option surface",
            "max loss and contract sizing include modeled round-trip commissions",
            "annualized return spans the eligible evaluation window rather than time-weighted deployed capital",
            "profit factor is null when no modeled losing trades exist rather than using a numeric sentinel",
            "a backtest WATCH label is research triage only; it is never an ALLOW trade decision",
        ],
    }


def _audit_report_markdown(audit):
    sig = audit["signalAndFill"]
    cal = audit["calendar"]
    cap = audit["capital"]
    econ = audit["economics"]
    totals = audit["summaryConsistency"]
    missing = audit["missingData"]
    return "\n".join(
        [
            "",
            "## Guarded Wrapper Validity Audit",
            "",
            f"- Decision grade: **false**; live decision label: **{audit['liveDecisionLabel']}**; research use: **{audit['researchUseLabel']}**.",
            f"- Option pricing classification: `{sig['entryPricingClassification']}`; historical option exits: {sig['historicalOptionExitCount']}.",
            f"- Signal lag: {sig['signalLagBars']} completed bars; same-bar protection: {sig['sameBarLookaheadProtected']}.",
            f"- Expiry contract: next-Friday/prior-session mapping; adjusted expiries: {cal['calendarAdjustedExpiryCount']}; unexpected non-Friday expiries: {cal['unexpectedNonFridayExpiryCount']}/{cal['validExpiryDates']}; authoritative holiday calendar checked: false.",
            f"- Capital overlap: {cap['strictOverlapPairs']} strict pairs; same-day exit/entry reuse: {cap['sameDayExitEntryReusePairs']}; peak concurrent: {cap['peakConcurrentPositionsInclusive']}.",
            f"- Economics identity mismatches: invalid rows {econ['invalidEconomicRowCount']}, risk {econ['riskIdentityMismatchCount']}, max loss {econ['maxLossIdentityMismatchCount']}, max gain {econ['maxGainIdentityMismatchCount']}, P&L bounds {econ['pnlOutsideDefinedRiskBoundsCount']}, return denominator {econ['returnOnRiskDenominatorMismatchCount']}.",
            f"- Headline aggregate reconciliation: {totals['mismatchCount']} mismatches across {totals['fieldsChecked']} checked fields; invalid trade rows: {totals['invalidTradeRows']}.",
            f"- Missing-data evidence: {missing['skippedCandidateCount']} skipped candidates, {missing['contextErrorCount']} context errors, {missing['missingExpiryCount']} missing-expiry skips, {missing['snapshotRejectionCount']} snapshot rejections; snapshot coverage gate: {missing['snapshotCoverageGatePass']}.",
            "- This audit is a research guardrail, not evidence that modeled fills are executable.",
            "",
        ]
    )


def annotate_outputs(out_dir, args, waivers=None, issues=None):
    """Merge wrapper audit metadata into the strict JSON and Markdown output."""
    out_dir = Path(out_dir)
    summary_path = out_dir / "summary.json"
    if not summary_path.exists():
        raise RuntimeError(f"delegated backtest did not create {summary_path}")
    with summary_path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    audit = build_validity_audit(payload, args, waivers=waivers, issues=issues)
    validation = payload.setdefault("validation", {})
    validation["decisionGrade"] = False
    validation["researchOnly"] = True
    validation["wrapperAudit"] = audit
    payload["disclaimer"] = (
        "Research only. Historical underlying with synthetic or snapshot-entry/model-exit options economics; "
        "not financial advice or a live trading signal."
    )
    atomic_write_json(summary_path, payload)

    report_path = out_dir / "report.md"
    report = report_path.read_text(encoding="utf-8") if report_path.exists() else ""
    marker = "## Guarded Wrapper Validity Audit"
    if marker in report:
        report = report.split(marker, 1)[0].rstrip() + "\n"
    atomic_write_text(
        report_path, report.rstrip() + "\n" + _audit_report_markdown(audit)
    )
    return audit


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    delegated, waivers = split_wrapper_flags(argv)
    if waivers.get("allow_timestamp_mismatch_sensitivity"):
        delegated.append("--allow-timestamp-mismatch-sensitivity")
    core = load_core_module()
    args = core.parse_args(delegated)
    issues = preflight_issues(args, waivers)
    blockers = [row for row in issues if row["severity"] == "block"]
    if blockers:
        details = "\n".join(f"  - {row['code']}: {row['message']}" for row in blockers)
        raise SystemExit(
            "Guarded options wrapper blocked quantitatively invalid/ambiguous settings:\n"
            + details
            + "\nUse an explicit --allow-...-sensitivity flag only for labeled research sensitivity runs."
        )
    result = core.main(delegated)
    if result not in (None, 0):
        return int(result)
    audit = annotate_outputs(args.out_dir, args, waivers=waivers, issues=issues)
    print(
        "Guarded validity audit: "
        f"decisionGrade=false live={audit['liveDecisionLabel']} research={audit['researchUseLabel']} "
        f"pricing={audit['signalAndFill']['entryPricingClassification']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""SPMO momentum sleeve analysis.

The sleeve follows SPMO's idea: buy after momentum is visible, not before it.
It uses a market gate (QQQ weather), an asset trend gate (SPMO trend), relative
strength, and moving take-profit. It is decision support, not trading advice.
"""
import argparse
import datetime as dt
import hashlib
import json
import math
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    from scripts.artifact_io import append_csv_row_private, atomic_write_json, atomic_write_text, ensure_private_directory
    from scripts.dashboard_payload import read_dashboard_payload as _read_dashboard_payload
except ModuleNotFoundError:  # direct ``python scripts/spmo_momentum_sleeve.py``
    from artifact_io import append_csv_row_private, atomic_write_json, atomic_write_text, ensure_private_directory
    from dashboard_payload import read_dashboard_payload as _read_dashboard_payload


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DASHBOARD = ROOT / "output" / "portfolio_dashboard.html"
DEFAULT_OUT = ROOT / "output" / "spmo_momentum"
DEFAULT_SENTINEL = ROOT / "output" / "market_sentinel" / "latest_snapshot.json"
BENCHMARKS = ("SPY", "QQQ", "VOO")
BASE_SLEEVE_CAP_PCT = 8.0
MAX_SLEEVE_CAP_PCT = 12.0
ET = ZoneInfo("America/New_York")
MAX_CLOSED_SESSION_LAG_WEEKDAYS = 2


def _to_float(value):
    try:
        if value is None:
            return None
        if isinstance(value, float) and math.isnan(value):
            return None
        return float(value)
    except Exception:
        return None


def _round(value, digits=2):
    value = _to_float(value)
    return None if value is None else round(value, digits)


def _pct(new, old):
    new = _to_float(new)
    old = _to_float(old)
    if new is None or old in (None, 0):
        return None
    return (new / old - 1.0) * 100.0


def ema(values, n):
    alpha = 2.0 / (n + 1.0)
    cur = None
    out = []
    for raw in values:
        value = _to_float(raw)
        if value is None:
            out.append(cur)
            continue
        cur = value if cur is None else alpha * value + (1 - alpha) * cur
        out.append(cur)
    return out


def atr(rows, n=14):
    trs = []
    prev = None
    for row in rows:
        high, low, close = row["high"], row["low"], row["close"]
        tr = high - low if prev is None else max(high - low, abs(high - prev), abs(low - prev))
        trs.append(tr)
        prev = close
    out, cur = [], None
    for i, tr in enumerate(trs):
        if i < n - 1:
            out.append(None)
        elif i == n - 1:
            cur = sum(trs[:n]) / n
            out.append(cur)
        else:
            cur = (cur * (n - 1) + tr) / n
            out.append(cur)
    return out


def read_dashboard_payload(path=DEFAULT_DASHBOARD):
    return _read_dashboard_payload(path)


def read_json(path):
    path = Path(path)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _parse_date(value):
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _parse_timestamp(value):
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=ET)


def _previous_weekday(day):
    cursor = day - dt.timedelta(days=1)
    while cursor.weekday() >= 5:
        cursor -= dt.timedelta(days=1)
    return cursor


def last_closed_session_reference(now=None):
    now = now or dt.datetime.now(tz=ET)
    if now.tzinfo is None:
        now = now.replace(tzinfo=ET)
    now_et = now.astimezone(ET)
    day = now_et.date()
    if day.weekday() >= 5:
        return _previous_weekday(day)
    if now_et.time().replace(tzinfo=None) < dt.time(16, 15):
        return _previous_weekday(day)
    return day


def _weekday_age(start, end):
    if start > end:
        return -1
    age = 0
    cursor = start + dt.timedelta(days=1)
    while cursor <= end:
        age += cursor.weekday() < 5
        cursor += dt.timedelta(days=1)
    return age


def closed_session_freshness(value, now=None):
    market_date = _parse_date(value)
    expected = last_closed_session_reference(now)
    if market_date is None:
        return {"fresh": False, "marketDate": None, "expectedLastClosedSession": expected.isoformat(),
                "ageWeekdays": None, "reason": "date_missing_or_invalid"}
    age = _weekday_age(market_date, expected)
    if market_date.weekday() >= 5:
        reason = "date_not_weekday_session"
    elif age < 0:
        reason = "date_after_last_closed_session"
    elif age > MAX_CLOSED_SESSION_LAG_WEEKDAYS:
        reason = "last_closed_session_stale"
    else:
        reason = None
    return {
        "fresh": reason is None,
        "marketDate": market_date.isoformat(),
        "expectedLastClosedSession": expected.isoformat(),
        "ageWeekdays": age,
        "reason": reason,
    }


def _position_is_active(document):
    position = (document or {}).get("position") or {}
    shares = _to_float(position.get("shares"))
    return bool(shares is not None and shares > 0)


def _new_lifecycle_id(result, reason):
    position = (result or {}).get("position") or {}
    seed = "|".join(
        str(value)
        for value in (
            "SPMO",
            (result or {}).get("asOf"),
            position.get("shares"),
            position.get("avg"),
            reason,
        )
    )
    return "spmo-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]


def apply_persisted_moving_stop(result, previous=None, *, reset_stop=False, reset_reason=None):
    """Ratchet the saved 3xATR stop upward and flag a close through it.

    The freshly calculated ATR stop is still retained for auditability, but an
    existing position is managed with the higher of the new calculation and
    the last valid saved stop.  This prevents a volatility expansion or price
    decline from quietly loosening risk after the stop was committed.  Stop
    persistence is scoped to an observed position lifecycle: flat -> active or
    an explicit reset starts a new lifecycle rather than inheriting an old
    position's stop.
    """
    result = result if isinstance(result, dict) else {}
    previous = previous if isinstance(previous, dict) else {}
    if reset_stop and not str(reset_reason or "").strip():
        raise ValueError("reset_stop requires a non-empty reset_reason")

    levels = result.setdefault("levels", {})
    previous_levels = previous.get("levels") or {}
    raw_stop = _to_float(levels.get("movingStop3Atr"))
    prior_stop = _to_float(previous_levels.get("movingStop3Atr"))
    current_active = _position_is_active(result)
    previous_active = _position_is_active(previous)
    previous_lifecycle = previous.get("positionLifecycle") or {}
    previous_id = previous_lifecycle.get("id")

    reset_applied = False
    lifecycle_reason = None
    if not current_active:
        status = "FLAT"
        lifecycle_id = None
        reset_applied = bool(previous_active or previous_id)
        lifecycle_reason = "position_not_held"
    elif reset_stop:
        status = "MANUAL_RESET"
        lifecycle_reason = str(reset_reason).strip()
        lifecycle_id = _new_lifecycle_id(result, lifecycle_reason)
        reset_applied = True
    elif not previous_active:
        status = "OPENED_OR_RESTARTED"
        lifecycle_reason = "first_active_snapshot" if not previous else "reopened_after_flat_snapshot"
        lifecycle_id = _new_lifecycle_id(result, lifecycle_reason)
        reset_applied = bool(previous)
    else:
        status = "CONTINUING"
        lifecycle_reason = "continuous_active_snapshots"
        lifecycle_id = previous_id or _new_lifecycle_id(previous, "legacy_active_snapshot")

    eligible_prior = prior_stop if current_active and status == "CONTINUING" else None
    valid = (
        [value for value in (raw_stop, eligible_prior) if value is not None and value > 0]
        if current_active
        else []
    )
    effective_stop = max(valid) if valid else None
    price = _to_float(result.get("price"))
    breached = bool(current_active and effective_stop is not None and price is not None and price <= effective_stop)

    levels["movingStop3AtrRaw"] = _round(raw_stop)
    levels["priorMovingStop3Atr"] = _round(prior_stop) if previous_active else None
    levels["movingStop3Atr"] = _round(effective_stop)
    if not current_active:
        levels["movingStopSource"] = "inactive_no_position"
    elif status != "CONTINUING":
        levels["movingStopSource"] = "current_3atr_lifecycle_start"
    elif eligible_prior is not None and (raw_stop is None or eligible_prior > raw_stop):
        levels["movingStopSource"] = "prior_saved_floor"
    else:
        levels["movingStopSource"] = "current_3atr"
    levels["movingStopBreached"] = breached
    levels["movingStopBreachedAsOf"] = result.get("asOf") if breached else None

    position = result.get("position") or {}
    result["positionLifecycle"] = {
        "active": current_active,
        "status": status,
        "id": lifecycle_id,
        "previousId": previous_id,
        "resetApplied": reset_applied,
        "resetReason": lifecycle_reason if reset_applied else None,
        "continuityReason": lifecycle_reason if not reset_applied else None,
        "observedAsOf": result.get("asOf"),
        "observedShares": _round(position.get("shares"), 6),
        "observedAverageCost": _round(position.get("avg"), 6),
        "provenance": "dashboard aggregate SPMO holding snapshot",
        "limitation": "A close-and-reopen between saved snapshots cannot be inferred; use --reset-stop with a reason when that occurs.",
    }

    gates = result.setdefault("gates", {})
    gates["movingStopBreached"] = breached
    actions = result.setdefault("actions", {})
    if not current_active:
        actions["existingSleeve"] = "NO_ACTIVE_POSITION: no persisted stop is in force."
        actions["movingTakeProfit"] = "No active SPMO holding; a future entry starts a new stop lifecycle."
    elif breached:
        actions["existingSleeve"] = (
            f"STOP_BREACHED: SPMO {result.get('price')} closed at/below the ratcheted "
            f"moving stop {levels.get('movingStop3Atr')}; defend/trim per the written plan."
        )
        actions["movingTakeProfit"] = (
            f"Ratcheted stop {levels.get('movingStop3Atr')} was breached; do not lower it to keep the position."
        )
    else:
        actions["movingTakeProfit"] = (
            f"Use ratcheted {levels.get('movingStop3Atr')} as the 3xATR reference; never move it down."
        )
    return result


def sentinel_context_for_payload(payload, sentinel, now=None):
    """Return an aligned market gate or an explicit fail-closed data gate."""
    def blocked(status, reason):
        context = {"status": status, "aligned": False, "reason": reason}
        return (
            {"intradayLabel": "BLOCK_DATA", "flags": {}, "_marketGateContext": context},
            {"label": "BLOCK_DATA", "_marketGateContext": context},
        )

    if not isinstance(sentinel, dict) or not sentinel:
        return blocked("MISSING_SENTINEL", "saved market sentinel is missing or unreadable")
    freshness = sentinel.get("dataFreshness") or {}
    summary = (payload or {}).get("summary") or {}
    if not summary.get("generatedAt") or not summary.get("priceAsOf"):
        return blocked("INCOMPLETE_DASHBOARD_FRESHNESS", "dashboard freshness keys are missing")
    dashboard_session = closed_session_freshness(summary.get("priceAsOf"), now=now)
    if not dashboard_session.get("fresh"):
        return blocked(
            "STALE_OR_FUTURE_DASHBOARD",
            "dashboard price date is not a current last-closed-session input: "
            + str(dashboard_session.get("reason")),
        )
    if not freshness.get("dashboardGeneratedAt") or not freshness.get("dashboardPriceAsOf"):
        return blocked("INCOMPLETE_SENTINEL", "sentinel dashboard freshness keys are missing")
    if (
        freshness.get("dashboardGeneratedAt") != summary.get("generatedAt")
        or freshness.get("dashboardPriceAsOf") != summary.get("priceAsOf")
    ):
        return blocked("MISALIGNED_SENTINEL", "sentinel was built from a different dashboard snapshot")
    sentinel_ran_at = _parse_timestamp(sentinel.get("ranAt"))
    now_value = now or dt.datetime.now(tz=ET)
    if now_value.tzinfo is None:
        now_value = now_value.replace(tzinfo=ET)
    if sentinel_ran_at is None:
        return blocked("INCOMPLETE_SENTINEL", "sentinel ranAt timestamp is missing or invalid")
    sentinel_age_hours = (
        now_value.astimezone(dt.timezone.utc) - sentinel_ran_at.astimezone(dt.timezone.utc)
    ).total_seconds() / 3600.0
    if sentinel_age_hours < -0.1 or sentinel_age_hours > 36:
        return blocked("STALE_OR_FUTURE_SENTINEL", "sentinel ranAt timestamp is stale or future-dated")
    agents = sentinel.get("agents") or {}
    technical = agents.get("technical")
    decision = agents.get("decision")
    if not isinstance(technical, dict) or not isinstance(decision, dict):
        return blocked("INCOMPLETE_SENTINEL", "sentinel technical/decision agents are missing")
    if technical.get("intradayLabel") not in {"ALLOW", "WATCH", "BLOCK"}:
        return blocked("INCOMPLETE_SENTINEL", "sentinel technical label is missing or unknown")
    if decision.get("label") not in {"ALLOW", "WATCH", "BLOCK"}:
        return blocked("INCOMPLETE_SENTINEL", "sentinel decision label is missing or unknown")

    technical = dict(technical)
    decision = dict(decision)
    hard_gates = technical.get("intradayHardGates") or {}
    intraday = technical.get("intradayData") or {}
    stale_or_locked = bool(
        hard_gates.get("prohibitAllow")
        or (hard_gates.get("available") and hard_gates.get("fresh") is False)
        or (intraday.get("available") and intraday.get("fresh") is False)
    )
    context = {
        "status": "STALE_OR_LOCKED" if stale_or_locked else "ALIGNED",
        "aligned": True,
        "reason": "intraday market gate is stale or prohibits ALLOW" if stale_or_locked else None,
    }
    technical["_marketGateContext"] = context
    decision["_marketGateContext"] = context
    return technical, decision


def _df_to_rows(df):
    if df is None or df.empty:
        return []
    rows = []
    for idx, row in df.iterrows():
        close = _to_float(row.get("Close"))
        high = _to_float(row.get("High"))
        low = _to_float(row.get("Low"))
        open_ = _to_float(row.get("Open"))
        if close is None or high is None or low is None or open_ is None:
            continue
        rows.append({
            "date": idx.date().isoformat() if hasattr(idx, "date") else str(idx)[:10],
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": _to_float(row.get("Volume")) or 0.0,
        })
    return rows


def fetch_daily_rows(symbol, period="18mo"):
    import warnings
    warnings.filterwarnings("ignore")
    import yfinance as yf

    # Adjust the entire OHLC series for distributions/splits so trend, ATR, and
    # relative-strength gates do not interpret an ETF dividend as a drawdown.
    df = yf.Ticker(symbol).history(period=period, interval="1d", auto_adjust=True)
    return _df_to_rows(df)


def add_features(rows):
    closes = [r["close"] for r in rows]
    e21 = ema(closes, 21)
    e50 = ema(closes, 50)
    e200 = ema(closes, 200)
    a14 = atr(rows, 14)
    for i, row in enumerate(rows):
        row["ema21"] = e21[i]
        row["ema50"] = e50[i]
        row["ema200"] = e200[i]
        row["atr14"] = a14[i]
    return rows


def return_n(rows, bars):
    if len(rows) <= bars:
        return None
    return _pct(rows[-1]["close"], rows[-1 - bars]["close"])


def momentum_12_1(rows):
    """Approximate S&P momentum: 12-month price change excluding latest month."""
    if len(rows) >= 253:
        return _pct(rows[-22]["close"], rows[-253]["close"])
    if len(rows) >= 190:
        return _pct(rows[-22]["close"], rows[-190]["close"])
    return None


def relative_strength(spmo_rows, bench_rows):
    # Compare the same exchange sessions on both sides.  Independent N-row
    # lookbacks silently compare different start dates when either feed has a
    # missing bar.
    spmo_by_date = {row.get("date"): row for row in spmo_rows if row.get("date")}
    bench_by_date = {row.get("date"): row for row in bench_rows if row.get("date")}
    common_dates = sorted(set(spmo_by_date) & set(bench_by_date))
    result = {}
    for bars, label in ((21, "rel21"), (63, "rel63"), (126, "rel126")):
        if len(common_dates) <= bars:
            result[label] = None
            continue
        start, end = common_dates[-1 - bars], common_dates[-1]
        s_ret = _pct(spmo_by_date[end]["close"], spmo_by_date[start]["close"])
        b_ret = _pct(bench_by_date[end]["close"], bench_by_date[start]["close"])
        result[label] = _round(s_ret - b_ret, 2) if s_ret is not None and b_ret is not None else None
    return result


def _market_gate(technical, decision, *, required=True):
    if not required:
        return {
            "block": True,
            "watch": False,
            "dataValid": False,
            "status": "EXPLICITLY_BYPASSED",
            "reasons": ["market sentinel bypassed; diagnostic result cannot authorize a new add"],
        }

    technical = technical if isinstance(technical, dict) else {}
    decision = decision if isinstance(decision, dict) else {}
    t_label = technical.get("intradayLabel")
    d_label = decision.get("label")
    context = technical.get("_marketGateContext") or decision.get("_marketGateContext") or {}
    context_status = context.get("status") or "IN_PROCESS"
    reasons = []
    data_invalid = context_status in {
        "MISSING_SENTINEL",
        "MISALIGNED_SENTINEL",
        "INCOMPLETE_SENTINEL",
        "INCOMPLETE_DASHBOARD_FRESHNESS",
        "STALE_OR_FUTURE_DASHBOARD",
        "STALE_OR_FUTURE_SENTINEL",
        "STALE_OR_LOCKED",
        "EXPLICITLY_BYPASSED",
    } or context.get("aligned") is False
    if context.get("reason"):
        reasons.append(str(context["reason"]))
    if not t_label or not d_label:
        data_invalid = True
        reasons.append("market technical/decision context is missing")
    if t_label not in {"ALLOW", "WATCH", "BLOCK", "BLOCK_DATA"}:
        data_invalid = True
        reasons.append(f"unknown market technical label {t_label!r}")
    if d_label not in {"ALLOW", "WATCH", "BLOCK", "BLOCK_DATA"}:
        data_invalid = True
        reasons.append(f"unknown market decision label {d_label!r}")

    hard_gates = technical.get("intradayHardGates") or {}
    intraday = technical.get("intradayData") or {}
    if hard_gates.get("prohibitAllow"):
        data_invalid = True
        reasons.append("intraday hard gates prohibit ALLOW")
    if intraday.get("available") and intraday.get("fresh") is False:
        data_invalid = True
        reasons.append("intraday market context is stale")

    flags = technical.get("flags") or {}
    blocked_label = t_label in {"BLOCK", "BLOCK_DATA"} or d_label in {"BLOCK", "BLOCK_DATA"}
    blocked_structure = bool(flags.get("belowEma21"))
    watched = t_label == "WATCH" or d_label == "WATCH"
    if blocked_structure:
        reasons.append("market sentinel/重心 blocked")
    if t_label in {"BLOCK", "BLOCK_DATA"}:
        reasons.append(f"market technical label {t_label}")
    if d_label in {"BLOCK", "BLOCK_DATA"}:
        reasons.append(f"market decision {d_label}")
    if watched:
        reasons.append("market sentinel is WATCH")
    return {
        "block": bool(data_invalid or blocked_label or blocked_structure),
        "watch": bool(watched and not (data_invalid or blocked_label or blocked_structure)),
        "dataValid": not data_invalid,
        "status": context_status,
        "technicalLabel": t_label,
        "decisionLabel": d_label,
        "reasons": sorted(set(reasons)),
    }


def score_spmo(
    spmo_rows,
    benchmark_map,
    payload,
    technical=None,
    decision=None,
    *,
    market_gate_required=True,
    price_basis="caller-provided OHLC; adjustment provenance not supplied",
    now=None,
):
    if len(spmo_rows) < 60:
        return {"available": False, "reason": "not enough SPMO rows"}

    add_features(spmo_rows)
    for rows in benchmark_map.values():
        add_features(rows)
    last = spmo_rows[-1]
    prev = spmo_rows[-2] if len(spmo_rows) >= 2 else None
    close = last["close"]
    atr14 = last.get("atr14") or 0
    ema21 = last.get("ema21")
    ema50 = last.get("ema50")
    ema200 = last.get("ema200")

    spy_rs = relative_strength(spmo_rows, benchmark_map.get("SPY", [])) if benchmark_map.get("SPY") else {}
    qqq_rs = relative_strength(spmo_rows, benchmark_map.get("QQQ", [])) if benchmark_map.get("QQQ") else {}
    voo_rs = relative_strength(spmo_rows, benchmark_map.get("VOO", [])) if benchmark_map.get("VOO") else {}
    rs_votes = []
    for rs in (spy_rs, qqq_rs, voo_rs):
        for key in ("rel21", "rel63"):
            val = rs.get(key)
            if val is not None:
                rs_votes.append(val > 0)
    rel_score = sum(1 for x in rs_votes if x)
    spy_hard_rs = (spy_rs.get("rel21") is not None and spy_rs.get("rel21") > 0
                   and spy_rs.get("rel63") is not None and spy_rs.get("rel63") > 0)

    above21 = ema21 is not None and close > ema21
    above50 = ema50 is not None and close > ema50
    above200 = ema200 is not None and close > ema200
    slope21 = None
    slope50 = None
    if len(spmo_rows) >= 6 and spmo_rows[-6].get("ema21"):
        slope21 = close_or_delta(last.get("ema21"), spmo_rows[-6].get("ema21"))
    if len(spmo_rows) >= 11 and spmo_rows[-11].get("ema50"):
        slope50 = close_or_delta(last.get("ema50"), spmo_rows[-11].get("ema50"))
    two_below_ema21 = False
    if len(spmo_rows) >= 2 and ema21 is not None and spmo_rows[-2].get("ema21") is not None:
        two_below_ema21 = spmo_rows[-1]["close"] < spmo_rows[-1]["ema21"] and spmo_rows[-2]["close"] < spmo_rows[-2]["ema21"]

    abs_score = 0
    abs_score += 2 if above21 else -2
    abs_score += 2 if above50 else -2
    abs_score += 1 if above200 else 0
    abs_score += 1 if (slope21 is not None and slope21 > 0) else -1
    abs_score += 1 if (slope50 is not None and slope50 > 0) else 0

    market = _market_gate(technical, decision, required=market_gate_required)
    market_block = market["block"]
    market_watch = market["watch"]
    reasons = list(market["reasons"])
    q_latest = ((payload or {}).get("qqqTqqq") or {}).get("latest") or {}
    q_close = _to_float(q_latest.get("qqq"))
    q_ema21 = _to_float(q_latest.get("ema21"))
    if q_close is None or q_ema21 is None:
        market_block = True
        market["dataValid"] = False
        market["status"] = "MISSING_QQQ_GATE"
        reasons.append("dashboard QQQ/EMA21 market gate is missing")
    elif q_close < q_ema21:
        market_block = True
        reasons.append("QQQ below EMA21")
    spmo_as_of = last.get("date")
    benchmark_as_of = {
        symbol: rows[-1].get("date") if rows else None
        for symbol, rows in benchmark_map.items()
    }
    missing_or_misaligned = [
        symbol
        for symbol in BENCHMARKS
        if not benchmark_map.get(symbol)
        or len(benchmark_map.get(symbol) or []) < 64
        or benchmark_as_of.get(symbol) != spmo_as_of
    ]
    if missing_or_misaligned:
        market_block = True
        market["dataValid"] = False
        market["status"] = "BENCHMARK_DATA_MISALIGNED"
        reasons.append("missing/misaligned benchmark data: " + ", ".join(missing_or_misaligned))
    summary = (payload or {}).get("summary") or {}
    dashboard_as_of = summary.get("priceAsOf")
    qqq_as_of = q_latest.get("date")
    absolute_freshness = closed_session_freshness(spmo_as_of, now=now)
    alignment_values = {
        "SPMO": spmo_as_of,
        "dashboard": dashboard_as_of,
        "dashboardQQQ": qqq_as_of,
        **benchmark_as_of,
    }
    date_alignment_pass = bool(
        spmo_as_of and all(value == spmo_as_of for value in alignment_values.values())
    )
    if not absolute_freshness.get("fresh"):
        market_block = True
        market["dataValid"] = False
        market["status"] = "STALE_OR_FUTURE_PRICE_DATA"
        reasons.append("SPMO last-closed-session freshness failed: " + str(absolute_freshness.get("reason")))
    if not date_alignment_pass:
        market_block = True
        market["dataValid"] = False
        market["status"] = "SPMO_QQQ_DASHBOARD_DATE_MISALIGNED"
        reasons.append("SPMO/benchmark/QQQ/dashboard dates are not identical")
    spy_latest = (benchmark_map.get("SPY") or [{}])[-1] if benchmark_map.get("SPY") else {}
    spy_market_broken = bool(spy_latest.get("close") and spy_latest.get("ema50") and spy_latest["close"] < spy_latest["ema50"])
    if spy_market_broken:
        market_watch = True
        reasons.append("SPY below EMA50")

    one_day = _pct(close, prev["close"] if prev else None)
    hard_hit = one_day is not None and one_day <= -4.0
    if hard_hit:
        market_watch = True
        reasons.append("SPMO had a large one-day drawdown")
    stretched = bool(ema21 and atr14 and (close - ema21) / atr14 > 1.75)
    if stretched:
        market_watch = True
        reasons.append("SPMO stretched above EMA21")

    sleeve_state = "trend"
    if two_below_ema21 or not above50:
        sleeve_state = "broken_or_chop"
    elif abs_score < 1:
        sleeve_state = "broken_or_chop"
    elif not spy_hard_rs or rel_score < max(2, len(rs_votes) // 2):
        sleeve_state = "absolute_only"
    elif hard_hit:
        sleeve_state = "trend_under_stress"
    elif stretched:
        sleeve_state = "overextended"

    if market_block:
        label = "BLOCK"
        action = "No new SPMO add. Keep existing sleeve only if it respects moving-stop; wait for QQQ reclaim."
    elif market_watch:
        label = "WATCH"
        action = "Market gate is WATCH. Existing trend may be managed, but no new SPMO add until market ALLOW."
    elif sleeve_state == "trend" and abs_score >= 4 and rel_score >= max(3, len(rs_votes) // 2):
        label = "ALLOW"
        action = "Momentum confirmed. Add only by tranches after buy-stop/reclaim, not at market after a crash bar."
    elif sleeve_state in ("absolute_only", "trend_under_stress", "overextended") or market_watch:
        label = "WATCH"
        action = "Trend exists but confirmation is incomplete. Wait for SPMO reclaim/relative strength repair."
    else:
        label = "BLOCK"
        action = "SPMO trend is not clean enough for momentum adds."

    buy_stop = None
    buy_limit = None
    if ema21 and atr14:
        buy_stop = max(close + 0.10 * atr14, ema21 + 0.20 * atr14)
        buy_limit = buy_stop + 0.10 * atr14
    invalidation = (ema50 - 0.20 * atr14) if ema50 and atr14 else None
    moving_stop = (close - 3.0 * atr14) if atr14 else None

    holding = None
    for stock in (payload or {}).get("stocks", []):
        if stock.get("sym") == "SPMO" and stock.get("held"):
            holding = stock
            break
    weight = None
    if holding and summary.get("marketValue"):
        weight = holding.get("value", 0) / summary["marketValue"] * 100

    levels = {
        "buyStop": _round(buy_stop),
        "buyLimit": _round(buy_limit),
        "invalidationClose": _round(invalidation),
        "movingStop3Atr": _round(moving_stop),
        "ema21Zone": [_round(ema21 - 0.5 * atr14), _round(ema21 + 0.5 * atr14)] if ema21 and atr14 else None,
    }
    actions = action_plan(
        label=label,
        sleeve_state=sleeve_state,
        above50=above50,
        two_below_ema21=two_below_ema21,
        hard_hit=hard_hit,
        stretched=stretched,
        weight=weight,
        levels=levels,
    )

    return {
        "available": True,
        "asOf": last["date"],
        "label": label,
        "action": action,
        "sleeveState": sleeve_state,
        "reasons": sorted(set(reasons)),
        "price": _round(close),
        "oneDayPct": _round(one_day, 2),
        "ema21": _round(ema21),
        "ema50": _round(ema50),
        "ema200": _round(ema200),
        "atr14": _round(atr14),
        "distEma21Atr": _round((close - ema21) / atr14, 2) if ema21 and atr14 else None,
        "absTrendScore": abs_score,
        "relativeVotes": {"positive": rel_score, "total": len(rs_votes)},
        "gates": {
            "aboveEma21": above21,
            "aboveEma50": above50,
            "aboveEma200": above200,
            "ema21Slope5": _round(slope21, 4),
            "ema50Slope10": _round(slope50, 4),
            "twoBelowEma21": two_below_ema21,
            "spmoVsSpy21And63Positive": spy_hard_rs,
            "stretchedAboveEma21": stretched,
            "hardHitDay": hard_hit,
            "marketBlock": market_block,
            "marketWatch": market_watch,
            "marketDataValid": market["dataValid"],
            "marketGateStatus": market["status"],
            "marketTechnicalLabel": market.get("technicalLabel"),
            "marketDecisionLabel": market.get("decisionLabel"),
            "spyBelowEma50": spy_market_broken,
        },
        "dataFreshness": {
            "priceBasis": price_basis,
            "spmoAsOf": spmo_as_of,
            "benchmarkAsOf": benchmark_as_of,
            "dashboardAsOf": dashboard_as_of,
            "dashboardQqqAsOf": qqq_as_of,
            "dateAlignmentPass": date_alignment_pass,
            "absoluteSession": absolute_freshness,
            "status": "PASS" if absolute_freshness.get("fresh") and date_alignment_pass else "BLOCK",
        },
        "spmoMomentum12Minus1": _round(momentum_12_1(spmo_rows), 2),
        "returns": {
            "spmo21": _round(return_n(spmo_rows, 21), 2),
            "spmo63": _round(return_n(spmo_rows, 63), 2),
            "spmo126": _round(return_n(spmo_rows, 126), 2),
        },
        "relativeStrength": {
            "vsSPY": spy_rs,
            "vsQQQ": qqq_rs,
            "vsVOO": voo_rs,
        },
        "levels": levels,
        "position": {
            "shares": holding.get("shares") if holding else None,
            "value": holding.get("value") if holding else None,
            "weightPct": _round(weight, 1),
            "avg": holding.get("avg") if holding else None,
            "unrealPct": holding.get("unrealPct") if holding else None,
        },
        "actions": actions,
        "tranchePlan": tranche_plan(levels),
        "journalChecklist": journal_checklist(),
        "reviewers": reviewer_notes(label, sleeve_state, weight, market_block, rel_score, len(rs_votes), hard_hit, stretched),
    }


def close_or_delta(new, old):
    new = _to_float(new)
    old = _to_float(old)
    if new is None or old is None:
        return None
    return new - old


def action_plan(label, sleeve_state, above50, two_below_ema21, hard_hit, stretched, weight, levels):
    cap = BASE_SLEEVE_CAP_PCT
    if label == "ALLOW" and sleeve_state == "trend":
        cap = MAX_SLEEVE_CAP_PCT
    if hard_hit or stretched or label == "WATCH":
        cap = min(cap, BASE_SLEEVE_CAP_PCT)
    current = weight or 0.0
    max_add = max(0.0, cap - current)
    if label == "BLOCK":
        max_add = 0.0
    elif label == "WATCH":
        max_add = 0.0

    if label == "ALLOW":
        new_add = "ALLOW tranche; never market-buy a vertical spike"
    elif label == "WATCH":
        new_add = "WATCH only; wait for reclaim/retest confirmation"
    else:
        new_add = "BLOCK new add"

    if not above50 or two_below_ema21:
        existing = "TRIM/DEFEND: SPMO sleeve trend has lost its intermediate support"
    elif hard_hit:
        existing = "HOLD_WITH_STOP: existing sleeve survived EMA50 but crash bar requires no add"
    else:
        existing = "HOLD_WITH_MOVING_STOP: let winner run while stop keeps moving up"

    return {
        "newAdd": new_add,
        "existingSleeve": existing,
        "maxSleevePct": _round(cap, 1),
        "maxNewAddPct": _round(max_add, 1),
        "stop": f"Close below {levels.get('invalidationClose')} shifts to defend/trim.",
        "movingTakeProfit": f"Use {levels.get('movingStop3Atr')} as 3xATR reference; never move it down.",
    }


def tranche_plan(levels):
    return [
        {
            "tranche": "1/3",
            "trigger": f"SPMO buy-stop/reclaim above {levels.get('buyStop')} with QQQ/sentinel explicitly ALLOW.",
            "risk": f"Initial invalidation close {levels.get('invalidationClose')}.",
        },
        {
            "tranche": "1/3",
            "trigger": "Retest holds above EMA21/EMA50 after reclaim; no lower low on 15m/30m structure.",
            "risk": "If reclaim fails the same day, do not add second tranche.",
        },
        {
            "tranche": "1/3",
            "trigger": "Follow-through close with SPMO/SPY relative strength still positive.",
            "risk": "If position is stretched > 1.75 ATR above EMA21, skip add and move stop instead.",
        },
    ]


def journal_checklist():
    return [
        "Regime label and data date are fresh.",
        "QQQ/sentinel market gate is explicitly ALLOW (WATCH/missing/stale/misaligned all prohibit a new add).",
        "SPMO is above EMA50 and not two closes below EMA21.",
        "SPMO/SPY 21d and 63d relative strength are positive.",
        "Entry uses buy-stop/reclaim or retest, not FOMO at market.",
        "Position size stays under sleeve cap and portfolio concentration cap.",
        "Invalidation, moving take-profit, and review date are written before entry.",
    ]


def reviewer_notes(label, sleeve_state, weight, market_block, rel_score, total_votes, hard_hit=False, stretched=False):
    dalio = []
    quant = []
    execution = []
    if market_block:
        dalio.append("Separate desire from evidence: the system is not allowed to add while the market weather gate is closed.")
    if weight is not None and weight >= 8:
        dalio.append("Risk first: SPMO is already meaningful; next add must improve balance, not just express conviction.")
    else:
        dalio.append("Use a small sleeve: momentum works by repeated rule-following, not one heroic entry.")
    if label == "ALLOW":
        dalio.append("Let profits run, but pre-commit the moving stop before entry.")
    if label == "BLOCK":
        dalio.append("A blocked add is not a bearish prediction; it is a rule saying evidence has not repaired yet.")
    quant.append("Avoid single-signal entries; require absolute trend plus relative strength plus market gate.")
    if total_votes and rel_score < max(3, total_votes // 2):
        quant.append("Relative strength is not broad enough; this is an absolute-trend hold, not a fresh add.")
    if sleeve_state == "trend_under_stress":
        quant.append("Crash-bar adds create bad slippage; use buy-stop/reclaim to avoid catching a falling factor.")
    if stretched:
        quant.append("Overextended momentum is a no-chase zone; future return/risk improves after retest or consolidation.")
    execution.append("New entries should be stop-triggered or retest-triggered; existing winners are managed by moving stops.")
    execution.append("If an add requires an immediate rebound to feel safe, the size or trigger is wrong.")
    return {"principlesInvestor": dalio, "quantReviewer": quant, "executionDesk": execution}


def run_once(payload=None, technical=None, decision=None, *, market_gate_required=True, now=None):
    payload = payload or read_dashboard_payload(DEFAULT_DASHBOARD)
    spmo_rows = fetch_daily_rows("SPMO")
    bench = {symbol: fetch_daily_rows(symbol) for symbol in BENCHMARKS}
    return score_spmo(
        spmo_rows,
        bench,
        payload,
        technical=technical,
        decision=decision,
        market_gate_required=market_gate_required,
        price_basis="split-and-distribution-adjusted OHLC (yfinance auto_adjust=True)",
        now=now,
    )


def format_report(result):
    lines = [
        "# SPMO Momentum Sleeve",
        "",
        f"As of: {result.get('asOf')}  ",
        f"Decision: **{result.get('label')}** - {result.get('action')}",
        "",
        "## Evidence",
        f"- Price: {result.get('price')} | EMA21 {result.get('ema21')} | EMA50 {result.get('ema50')} | ATR14 {result.get('atr14')}",
        f"- Abs trend score: {result.get('absTrendScore')} | Relative votes: {result.get('relativeVotes')}",
        f"- 12-1 momentum proxy: {result.get('spmoMomentum12Minus1')}% | returns {result.get('returns')}",
        f"- Relative strength: {result.get('relativeStrength')}",
        f"- Gates: {result.get('gates')}",
        "",
        "## Levels",
        f"- Buy-stop/reclaim: {result.get('levels', {}).get('buyStop')} / limit {result.get('levels', {}).get('buyLimit')}",
        f"- Invalidation close: {result.get('levels', {}).get('invalidationClose')}",
        f"- Moving stop guide: {result.get('levels', {}).get('movingStop3Atr')} "
        f"(raw {result.get('levels', {}).get('movingStop3AtrRaw')}, "
        f"source {result.get('levels', {}).get('movingStopSource')})",
        f"- Moving stop breached: {result.get('levels', {}).get('movingStopBreached')}",
        f"- Position lifecycle: {result.get('positionLifecycle')}",
        "",
        "## Actions",
        f"- New add: {result.get('actions', {}).get('newAdd')}",
        f"- Existing sleeve: {result.get('actions', {}).get('existingSleeve')}",
        f"- Max sleeve/add: {result.get('actions', {}).get('maxSleevePct')}% / {result.get('actions', {}).get('maxNewAddPct')}%",
        "",
        "## Tranche Plan",
    ]
    for item in result.get("tranchePlan") or []:
        lines.append(f"- {item['tranche']}: {item['trigger']} Risk: {item['risk']}")
    lines += [
        "",
        "## Journal Checklist",
    ]
    for item in result.get("journalChecklist") or []:
        lines.append(f"- {item}")
    lines += [
        "",
        "## Reviewers",
    ]
    reviewers = result.get("reviewers") or {}
    for name, notes in reviewers.items():
        lines.append(f"### {name}")
        for note in notes:
            lines.append(f"- {note}")
    return "\n".join(lines) + "\n"


def write_outputs(result, out_dir=DEFAULT_OUT, *, reset_stop=False, reset_reason=None):
    out_dir = Path(out_dir)
    ensure_private_directory(out_dir)
    latest_path = out_dir / "latest_spmo_momentum.json"
    apply_persisted_moving_stop(
        result,
        read_json(latest_path),
        reset_stop=reset_stop,
        reset_reason=reset_reason,
    )
    atomic_write_json(latest_path, result)
    atomic_write_text(out_dir / "latest_spmo_momentum.md", format_report(result))
    log = out_dir / "spmo_momentum_log.csv"
    row = {
        "ranAt": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "asOf": result.get("asOf"),
        "label": result.get("label"),
        "price": result.get("price"),
        "absTrendScore": result.get("absTrendScore"),
        "relativePositive": (result.get("relativeVotes") or {}).get("positive"),
        "relativeTotal": (result.get("relativeVotes") or {}).get("total"),
        "buyStop": (result.get("levels") or {}).get("buyStop"),
        "invalidationClose": (result.get("levels") or {}).get("invalidationClose"),
    }
    append_csv_row_private(log, row, list(row.keys()))


def main():
    parser = argparse.ArgumentParser(description="Analyze SPMO momentum sleeve.")
    parser.add_argument("--dashboard", default=str(DEFAULT_DASHBOARD))
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT))
    parser.add_argument("--sentinel", default=str(DEFAULT_SENTINEL),
                        help="latest market sentinel snapshot used for the QQQ/market gate")
    parser.add_argument("--ignore-sentinel", action="store_true",
                        help="diagnostic asset-only run; fail-closed output cannot authorize a new add")
    parser.add_argument("--reset-stop", action="store_true",
                        help="start a new persisted-stop lifecycle for a known close/re-entry")
    parser.add_argument("--reset-reason", default=None,
                        help="required audit reason when --reset-stop is used")
    args = parser.parse_args()
    if args.reset_stop and not str(args.reset_reason or "").strip():
        parser.error("--reset-stop requires --reset-reason")
    payload = read_dashboard_payload(args.dashboard)
    technical = decision = None
    if not args.ignore_sentinel:
        technical, decision = sentinel_context_for_payload(payload, read_json(args.sentinel))
    result = run_once(
        payload=payload,
        technical=technical,
        decision=decision,
        market_gate_required=not args.ignore_sentinel,
    )
    write_outputs(result, args.out_dir, reset_stop=args.reset_stop, reset_reason=args.reset_reason)
    print(f"{result.get('asOf')} SPMO {result.get('label')} {result.get('action')}")
    print(Path(args.out_dir).resolve() / "latest_spmo_momentum.md")


if __name__ == "__main__":
    main()

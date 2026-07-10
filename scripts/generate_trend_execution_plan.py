#!/usr/bin/env python3
"""Generate a portfolio-wide trend execution plan.

The report is intentionally rule-based: it turns the current dashboard payload
into reclaim buy-stop-limit levels, pullback zones, invalidation closes, moving
take-profit guides, and a recent-sell review.
"""
import argparse
import datetime as dt
import hashlib
import json
import math
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    from scripts.artifact_io import atomic_write_csv, atomic_write_json, atomic_write_text
    from scripts.dashboard_payload import read_dashboard_payload as _read_dashboard_payload
except ImportError:  # direct `python scripts/generate_trend_execution_plan.py`
    from artifact_io import atomic_write_csv, atomic_write_json, atomic_write_text
    from dashboard_payload import read_dashboard_payload as _read_dashboard_payload


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DASHBOARD = ROOT / "output" / "portfolio_dashboard.html"
DEFAULT_SENTINEL = ROOT / "output" / "market_sentinel" / "latest_snapshot.json"
DEFAULT_SPMO = ROOT / "output" / "spmo_momentum" / "latest_spmo_momentum.json"
DEFAULT_OUT = ROOT / "output" / "trend_execution"
CORE_ETFS = {"VOO", "SPY"}
BROAD_TACTICAL = {"QQQ", "SPMO"}
LEVERAGED = {"TQQQ"}
HIGH_BETA_THEMES = {"半导体", "杠杆", "互联网/通信"}
ET = ZoneInfo("America/New_York")
MAX_CLOSED_SESSION_LAG_WEEKDAYS = 2
MAX_SENTINEL_AGE_HOURS = 36


def _to_float(value):
    try:
        if value is None or isinstance(value, bool):
            return None
        value = float(value)
        if not math.isfinite(value):
            return None
        return value
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
    return parsed if parsed.tzinfo else None


def _previous_weekday(day):
    cursor = day - dt.timedelta(days=1)
    while cursor.weekday() >= 5:
        cursor -= dt.timedelta(days=1)
    return cursor


def last_closed_session_reference(now=None):
    """Return the latest session that should have a completed daily close.

    The weekday model intentionally allows a small lag below so US exchange
    holidays do not create false freshness failures without a calendar feed.
    """
    now = now or dt.datetime.now(tz=ET)
    if now.tzinfo is None:
        now = now.replace(tzinfo=ET)
    now_et = now.astimezone(ET)
    if now_et.date().weekday() >= 5:
        return _previous_weekday(now_et.date())
    if now_et.time().replace(tzinfo=None) < dt.time(16, 15):
        return _previous_weekday(now_et.date())
    return now_et.date()


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
        return {
            "fresh": False,
            "marketDate": None,
            "expectedLastClosedSession": expected.isoformat(),
            "ageWeekdays": None,
            "reason": "date_missing_or_invalid",
        }
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


def ema(values, n):
    """Conventional seeded EMA; values are unavailable until ``n`` observations."""
    alpha = 2.0 / (n + 1.0)
    cur = None
    warmup = []
    out = []
    for raw in values:
        value = _to_float(raw)
        if value is None:
            warmup = []
            cur = None
            out.append(None)
            continue
        if cur is None:
            warmup.append(value)
            if len(warmup) < n:
                out.append(None)
                continue
            cur = sum(warmup[-n:]) / n
        else:
            cur = alpha * value + (1 - alpha) * cur
        out.append(cur)
    return out


def atr(rows, n=14):
    trs = []
    prev_close = None
    for row in rows:
        high = _to_float(row.get("high"))
        low = _to_float(row.get("low"))
        close = _to_float(row.get("close"))
        if high is None or low is None or close is None:
            continue
        if prev_close is None:
            tr = high - low
        else:
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
        prev_close = close

    out = []
    cur = None
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


def _df_to_rows(df):
    rows = []
    if df is None or getattr(df, "empty", True):
        return rows
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
        })
    return rows


def fetch_ohlc(symbols, period="18mo"):
    import warnings
    warnings.filterwarnings("ignore")
    import yfinance as yf

    result = {}
    for symbol in symbols:
        try:
            # Trend/ATR levels span many months and therefore require adjusted
            # OHLC so splits and distributions do not become false signals.
            result[symbol] = _df_to_rows(yf.Ticker(symbol).history(
                period=period, interval="1d", auto_adjust=True))
        except Exception:
            result[symbol] = []
    return result


def close_only_rows(stock):
    return [
        {"date": d, "open": p, "high": p, "low": p, "close": p}
        for d, p in (stock.get("prices") or [])
        if _to_float(p) is not None
    ]


def feature_rows(rows):
    # De-duplicate and order before rolling calculations. Provider revisions for
    # the same session keep the last supplied row.
    by_date = {str(row.get("date")): dict(row) for row in (rows or []) if row.get("date")}
    rows = [by_date[key] for key in sorted(by_date)]
    closes = [_to_float(r.get("close")) for r in rows]
    e21 = ema(closes, 21)
    e50 = ema(closes, 50)
    e200 = ema(closes, 200)
    a14 = atr(rows, 14)
    # atr() skips malformed rows; in normal rows lengths match. Fall back to a
    # close-to-close proxy if only close data is available.
    if len(a14) != len(rows) or not any(x for x in a14 if x):
        diffs = [0.0]
        for prev, cur in zip(closes, closes[1:]):
            diffs.append(abs(cur - prev) if cur is not None and prev is not None else 0.0)
        a14 = ema(diffs, 14)
    for i, row in enumerate(rows):
        row["ema21"] = e21[i] if i < len(e21) else None
        row["ema50"] = e50[i] if i < len(e50) else None
        row["ema200"] = e200[i] if i < len(e200) else None
        row["atr14"] = a14[i] if i < len(a14) else None
    return rows


def return_n(rows, bars):
    if len(rows) <= bars:
        return None
    return _pct(rows[-1].get("close"), rows[-1 - bars].get("close"))


def slope(row_now, row_prev, key):
    if not row_now or not row_prev:
        return None
    return close_delta(row_now.get(key), row_prev.get(key))


def close_delta(new, old):
    new = _to_float(new)
    old = _to_float(old)
    if new is None or old is None:
        return None
    return new - old


def market_context(payload, sentinel, now=None):
    now = now or dt.datetime.now(tz=ET)
    if now.tzinfo is None:
        now = now.replace(tzinfo=ET)
    decision = ((sentinel.get("agents") or {}).get("decision") or {})
    q = ((payload.get("qqqTqqq") or {}).get("latest") or {})
    q_close = _to_float(q.get("qqq"))
    q_ema21 = _to_float(q.get("ema21"))
    summary = payload.get("summary") or {}
    dashboard_as_of = summary.get("priceAsOf")
    sentinel_as_of = (sentinel.get("dataFreshness") or {}).get("dashboardPriceAsOf")
    sentinel_ran_at = sentinel.get("ranAt")
    dashboard_generated_at = summary.get("generatedAt")
    sentinel_dashboard_generated_at = (sentinel.get("dataFreshness") or {}).get(
        "dashboardGeneratedAt"
    )
    qqq_as_of = q.get("date")
    absolute_session = closed_session_freshness(dashboard_as_of, now=now)
    snapshot_aligned = bool(
        dashboard_as_of
        and sentinel_as_of == dashboard_as_of
        and dashboard_generated_at
        and sentinel_dashboard_generated_at == dashboard_generated_at
    )
    qqq_aligned = bool(qqq_as_of and qqq_as_of == dashboard_as_of)
    sentinel_time = _parse_timestamp(sentinel_ran_at)
    dashboard_time = _parse_timestamp(dashboard_generated_at)
    sentinel_age_hours = None
    snapshot_lag_hours = None
    if sentinel_time is not None:
        sentinel_age_hours = (
            now.astimezone(dt.timezone.utc) - sentinel_time.astimezone(dt.timezone.utc)
        ).total_seconds() / 3600.0
    if sentinel_time is not None and dashboard_time is not None:
        snapshot_lag_hours = (
            sentinel_time.astimezone(dt.timezone.utc)
            - dashboard_time.astimezone(dt.timezone.utc)
        ).total_seconds() / 3600.0
    sentinel_current = bool(
        absolute_session.get("fresh")
        and snapshot_aligned
        and qqq_aligned
        and sentinel_age_hours is not None
        and -0.1 <= sentinel_age_hours <= MAX_SENTINEL_AGE_HOURS
        and snapshot_lag_hours is not None
        and 0 <= snapshot_lag_hours <= 12
    )
    data_issues = []
    if not absolute_session.get("fresh"):
        data_issues.append(
            "dashboard_session_stale_or_future:"
            + str(absolute_session.get("reason"))
        )
    if not snapshot_aligned:
        data_issues.append("sentinel_dashboard_snapshot_misaligned")
    if not qqq_aligned:
        data_issues.append("dashboard_QQQ_date_misaligned_or_missing")
    if sentinel_age_hours is None:
        data_issues.append("sentinel_timestamp_missing_or_invalid")
    elif sentinel_age_hours < -0.1:
        data_issues.append("sentinel_timestamp_in_future")
    elif sentinel_age_hours > MAX_SENTINEL_AGE_HOURS:
        data_issues.append("sentinel_timestamp_stale")
    if snapshot_lag_hours is None:
        data_issues.append("dashboard_or_sentinel_timestamp_missing_timezone")
    elif snapshot_lag_hours < 0 or snapshot_lag_hours > 12:
        data_issues.append("sentinel_dashboard_timestamp_lag_invalid")
    label = decision.get("label")
    if label not in {"ALLOW", "WATCH", "BLOCK"} or not sentinel_current:
        label = "BLOCK_DATA"
    return {
        "label": label,
        "primaryAction": (
            decision.get("primaryAction") if sentinel_current
            else ((payload.get("qqqTqqq") or {}).get("decisionPanel") or {}).get("doNow")
        ),
        "qqqClose": q_close,
        "qqqEma21": q_ema21,
        "qqqAtr14": _to_float(q.get("atr14")),
        "qqqBelowEma21": q_close is not None and q_ema21 is not None and q_close < q_ema21,
        "dataStatus": "PASS" if sentinel_current else "BLOCK",
        "dataIssues": data_issues,
        "dashboardSession": absolute_session,
        "dashboardQqqPriceAsOf": qqq_as_of,
        "dashboardQqqAligned": qqq_aligned,
        "sentinelSnapshotAligned": snapshot_aligned,
        "sentinelCurrent": sentinel_current,
        "sentinelAgeHours": _round(sentinel_age_hours, 2),
        "sentinelDashboardLagHours": _round(snapshot_lag_hours, 2),
        "sentinelRanAt": sentinel_ran_at,
        "sentinelDashboardPriceAsOf": sentinel_as_of,
        "sentinelDashboardGeneratedAt": sentinel_dashboard_generated_at,
    }


def concentration_context(payload):
    alloc = payload.get("alloc") or {}
    themes = {row.get("theme"): row for row in alloc.get("byTheme") or []}
    return {
        "semisRiskPct": _to_float((themes.get("半导体") or {}).get("riskPct")) or 0.0,
        "semisWeightPct": _to_float((themes.get("半导体") or {}).get("weightPct")) or 0.0,
        "largestTheme": (alloc.get("largestTheme") or {}).get("theme"),
        "topWeight": ((payload.get("behavior") or {}).get("stats") or {}).get("topWeight"),
        "top5Weight": ((payload.get("behavior") or {}).get("stats") or {}).get("top5Weight"),
    }


def choose_entry_label(stock, features, market, concentration, rs_spy):
    sym = stock.get("sym")
    theme = stock.get("theme") or ""
    last = features[-1] if features else {}
    close = _to_float(last.get("close"))
    ema21 = _to_float(last.get("ema21"))
    ema50 = _to_float(last.get("ema50"))
    atr14 = _to_float(last.get("atr14")) or 0.0
    day = _to_float(stock.get("dayChangePct"))
    fib_state = ((stock.get("fib") or {}).get("now") or {}).get("state")
    current_weight = _to_float(stock.get("weightPct")) or 0.0

    blocked_by = []
    data_issues = list(stock.get("_dataIssues") or [])
    if data_issues:
        return "BLOCK_DATA", data_issues
    if market.get("label") == "BLOCK" and (sym in BROAD_TACTICAL or sym in LEVERAGED or theme in HIGH_BETA_THEMES):
        blocked_by.append("market_gate_BLOCK")
    if sym == "SPMO" and market.get("label") == "BLOCK":
        blocked_by.append("SPMO_requires_QQQ_reclaim")
    if sym == "TQQQ" and market.get("qqqBelowEma21"):
        blocked_by.append("TQQQ_requires_QQQ_EMA21_reclaim")
    if theme == "半导体" and concentration.get("semisRiskPct", 0) >= 50:
        blocked_by.append("semiconductor_risk_concentration")
    if fib_state == "down":
        blocked_by.append("local_downtrend")
    if close is not None and ema50 is not None and close < ema50:
        blocked_by.append("below_EMA50")
    if sym == "NVDA" and current_weight >= 20:
        blocked_by.append("single_name_concentration")

    hard_hit = day is not None and day <= -8.0
    stretched = close is not None and ema21 is not None and atr14 and (close - ema21) / atr14 > 1.75
    trend_ok = close is not None and ema21 is not None and ema50 is not None and close > ema21 and close > ema50
    rs_ok = (rs_spy.get("rs21") or -999) > 0 and (rs_spy.get("rs63") or -999) > 0

    if blocked_by:
        return "BLOCK_ADD", blocked_by
    if hard_hit:
        return "WATCH_REPAIR", ["hard_hit_wait_reclaim"]
    if stretched:
        return "WATCH_NO_CHASE", ["stretched_above_EMA21"]
    if market.get("label") == "ALLOW" and trend_ok and rs_ok:
        return "ALLOW_TRANCHE", []
    if trend_ok:
        return "WATCH_TREND", ["market_or_RS_confirmation_incomplete"]
    return "WATCH_RECLAIM", ["trend_not_clean"]


def level_plan(stock, features, spy_features, market, concentration, spmo):
    sym = stock.get("sym")
    last = features[-1] if features else {}
    prev = features[-2] if len(features) >= 2 else {}
    close = _to_float(last.get("close")) or _to_float(stock.get("curPrice"))
    ema21 = _to_float(last.get("ema21"))
    ema50 = _to_float(last.get("ema50"))
    ema200 = _to_float(last.get("ema200"))
    atr14 = _to_float(last.get("atr14")) or (close * 0.03 if close else None)
    if not close or not atr14:
        atr14 = None

    if sym == "SPMO" and (spmo.get("levels") or {}).get("buyStop"):
        buy_stop = _to_float(spmo["levels"].get("buyStop"))
        buy_limit = _to_float(spmo["levels"].get("buyLimit"))
        invalidation = _to_float(spmo["levels"].get("invalidationClose"))
        moving_stop = _to_float(spmo["levels"].get("movingStop3Atr"))
    else:
        if close is not None and ema21 is not None and close < ema21:
            reclaim_anchor = ema21
        else:
            anchors = [x for x in [ema21, _to_float(prev.get("high")), close] if x is not None]
            reclaim_anchor = max(anchors) if anchors else None
        buy_stop = reclaim_anchor + (0.10 * atr14 if atr14 else 0) if reclaim_anchor is not None else None
        buy_limit = buy_stop + (0.25 * atr14 if atr14 else 0) if buy_stop is not None else None
        invalidation = (ema50 - 0.25 * atr14) if ema50 and atr14 else (close - 2.0 * atr14 if close and atr14 else None)
        recent_closes = [_to_float(r.get("close")) for r in features[-20:]]
        recent_closes = [value for value in recent_closes if value is not None]
        close20 = max(recent_closes) if recent_closes else None
        trail_by_high = close20 - 3.0 * atr14 if close20 and atr14 else None
        trail_by_ema = ema21 - 1.0 * atr14 if ema21 and atr14 else None
        moving_stop = max(x for x in [trail_by_high, trail_by_ema, invalidation] if x is not None) if close else None

    pullback_zone = None
    if close and ema21 and ema50 and atr14 and close > ema21 > ema50:
        pullback_zone = [_round(ema21 - 0.30 * atr14), _round(ema21 + 0.20 * atr14)]

    stock_r21, spy_r21 = return_n(features, 21), return_n(spy_features, 21)
    stock_r63, spy_r63 = return_n(features, 63), return_n(spy_features, 63)
    rs_spy = {
        "rs21": _round(stock_r21 - spy_r21, 2)
        if stock_r21 is not None and spy_r21 is not None else None,
        "rs63": _round(stock_r63 - spy_r63, 2)
        if stock_r63 is not None and spy_r63 is not None else None,
    }
    stock = dict(stock)
    summary_mv = _to_float(stock.get("_marketValue")) or 0.0
    stock_value = _to_float(stock.get("value")) or 0.0
    stock["weightPct"] = stock_value / summary_mv * 100 if summary_mv else 0.0
    expected_as_of = stock.get("_expectedAsOf")
    latest_feature_date = str(last.get("date")) if last.get("date") else None
    data_issues = list(stock.get("_globalDataIssues") or [])
    if len(features) < 50 or ema21 is None or ema50 is None or atr14 is None:
        data_issues.append("insufficient_price_history")
    if expected_as_of and latest_feature_date != expected_as_of:
        data_issues.append("price_history_not_aligned_to_dashboard")
    stock["_dataIssues"] = data_issues
    entry_label, blocked_by = choose_entry_label(stock, features, market, concentration, rs_spy)
    stop_status = "OK"
    if close and moving_stop and close < moving_stop:
        stop_status = "TRAIL_BREACHED"
    if close and invalidation and close < invalidation:
        stop_status = "INVALIDATION_BREACHED"
    existing_action = manage_existing_action(entry_label, stop_status, blocked_by)

    current_weight = stock["weightPct"]
    max_weight = max_weight_for(stock)
    proposed = 0.0
    if entry_label == "ALLOW_TRANCHE":
        proposed = max(0.0, min(1.0, max_weight - current_weight))

    return {
        "Symbol": sym,
        "Theme": stock.get("theme") or "—",
        "Price": _round(close),
        "Shares": _round(stock.get("shares"), 3),
        "WeightPct": _round(current_weight, 2),
        "UnrealPct": _round(stock.get("unrealPct"), 2),
        "DayPct": _round(stock.get("dayChangePct"), 2),
        "Fib": ((stock.get("fib") or {}).get("now") or {}).get("label") or "—",
        "EMA21": _round(ema21),
        "EMA50": _round(ema50),
        "EMA200": _round(ema200),
        "ATR14": _round(atr14),
        "HistoryBars": len(features),
        "PriceAsOf": latest_feature_date,
        "DataQuality": "PASS" if not data_issues else "BLOCK",
        "DistEMA21ATR": _round((close - ema21) / atr14, 2) if close and ema21 and atr14 else None,
        "RS21vsSPY": rs_spy.get("rs21"),
        "RS63vsSPY": rs_spy.get("rs63"),
        "EntryDecision": entry_label,
        "ExistingAction": existing_action,
        "BuyStop": _round(buy_stop),
        "BuyLimit": _round(buy_limit),
        "PullbackZone": pullback_zone,
        "InvalidationClose": _round(invalidation),
        "MovingStopCandidate": _round(moving_stop),
        "PriorMovingStop": None,
        "MovingStop": _round(moving_stop),
        "MovingStopSource": "current_candidate_unpersisted",
        "MovingStopBreached": stop_status in {"TRAIL_BREACHED", "INVALIDATION_BREACHED"},
        "StopLifecycle": None,
        "StopStatus": stop_status,
        "MaxWeightPct": _round(max_weight, 1),
        "ProposedTranchePct": _round(proposed, 2),
        "BlockedBy": ";".join(blocked_by),
    }


def manage_existing_action(entry_label, stop_status, blocked_by):
    blocked = set(blocked_by or [])
    if entry_label == "BLOCK_DATA":
        return "DATA REVIEW: do not change exposure from this artifact"
    if stop_status == "INVALIDATION_BREACHED":
        return "DEFEND/TRIM: invalidation already breached"
    if stop_status == "TRAIL_BREACHED":
        return "REVIEW/TRIM: moving stop already breached"
    if "local_downtrend" in blocked or "below_EMA50" in blocked:
        return "DEFEND: no add; reduce if rebound fails"
    if entry_label == "BLOCK_ADD":
        return "HOLD_WITH_STOP: no new add"
    if entry_label.startswith("WATCH"):
        return "HOLD/WAIT: require repair"
    return "ALLOW_SMALL_TRANCHE: if risk budget fits"


def parse_stop_resets(values):
    """Parse repeated ``SYMBOL=REASON`` reset declarations.

    A reason is mandatory because a reset is the only operation allowed to
    lower a previously persisted stop for a continuously observed holding.
    """
    resets = {}
    for raw in values or []:
        if "=" not in str(raw):
            raise ValueError("stop reset must use SYMBOL=REASON")
        symbol, reason = str(raw).split("=", 1)
        symbol = symbol.strip().upper()
        reason = reason.strip()
        if not symbol or not reason:
            raise ValueError("stop reset requires a non-empty symbol and reason")
        if symbol in resets:
            raise ValueError(f"duplicate stop reset for {symbol}")
        resets[symbol] = reason
    return resets


def _normalize_stop_resets(resets):
    if resets is None:
        return {}
    if isinstance(resets, (list, tuple)):
        return parse_stop_resets(resets)
    if not isinstance(resets, dict):
        raise ValueError("stop resets must be a mapping or SYMBOL=REASON list")
    normalized = {}
    for raw_symbol, raw_reason in resets.items():
        symbol = str(raw_symbol or "").strip().upper()
        reason = str(raw_reason or "").strip()
        if not symbol or not reason:
            raise ValueError("stop reset requires a non-empty symbol and reason")
        if symbol in normalized:
            raise ValueError(f"duplicate stop reset for {symbol}")
        normalized[symbol] = reason
    return normalized


def _level_position_active(row):
    shares = _to_float((row or {}).get("Shares"))
    return bool(shares is not None and shares > 0)


def _stop_lifecycle_id(row, status, previous_id=None, reason=None):
    seed = "|".join(
        str(value)
        for value in (
            "trend-stop-v1",
            (row or {}).get("Symbol"),
            (row or {}).get("PriceAsOf"),
            (row or {}).get("Shares"),
            (row or {}).get("MovingStopCandidate"),
            status,
            previous_id,
            reason,
        )
    )
    return "trend-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def apply_persisted_moving_stops(plan, previous=None, resets=None):
    """Apply a per-holding ratchet against the previously saved artifact.

    Continuous holdings use ``max(previous effective stop, new candidate)``.
    A flat snapshot naturally ends continuity because the symbol disappears
    from the held-level table. A close-and-reopen between snapshots is not
    inferable, so lowering that stop requires an explicit reasoned reset.
    """
    if not isinstance(plan, dict):
        raise ValueError("trend execution plan must be an object")
    previous = previous if isinstance(previous, dict) else {}
    reset_map = _normalize_stop_resets(resets)
    levels = plan.get("levels")
    if not isinstance(levels, list):
        raise ValueError("trend execution plan levels must be a list")
    current_by_symbol = {
        str(row.get("Symbol") or "").upper(): row
        for row in levels
        if isinstance(row, dict) and row.get("Symbol")
    }
    unknown = sorted(set(reset_map) - set(current_by_symbol))
    if unknown:
        raise ValueError("stop reset symbol is not an active plan holding: " + ", ".join(unknown))
    inactive = sorted(
        symbol for symbol in reset_map if not _level_position_active(current_by_symbol[symbol])
    )
    if inactive:
        raise ValueError("stop reset requires positive current shares: " + ", ".join(inactive))

    previous_by_symbol = {
        str(row.get("Symbol") or "").upper(): row
        for row in (previous.get("levels") or [])
        if isinstance(row, dict) and row.get("Symbol")
    }
    resets_applied = []
    for symbol, row in current_by_symbol.items():
        prior = previous_by_symbol.get(symbol) or {}
        candidate = _to_float(
            row.get("MovingStopCandidate")
            if "MovingStopCandidate" in row
            else row.get("MovingStop")
        )
        prior_stop = _to_float(prior.get("MovingStop"))
        current_active = _level_position_active(row)
        previous_active = _level_position_active(prior)
        prior_lifecycle = prior.get("StopLifecycle") or {}
        previous_id = prior_lifecycle.get("id")
        if previous_active and not previous_id:
            previous_id = _stop_lifecycle_id(prior, "LEGACY_ACTIVE")
        reset_reason = reset_map.get(symbol)

        if not current_active:
            status = "FLAT"
            lifecycle_id = None
            effective = None
            source = "inactive_no_position"
        elif reset_reason:
            status = "MANUAL_RESET"
            lifecycle_id = _stop_lifecycle_id(
                row, status, previous_id=previous_id, reason=reset_reason
            )
            effective = candidate
            source = "manual_reset_current_candidate"
        elif previous_active:
            status = "CONTINUING"
            lifecycle_id = previous_id
            candidates = [value for value in (candidate, prior_stop) if value is not None]
            effective = max(candidates) if candidates else None
            source = (
                "prior_saved_floor"
                if prior_stop is not None and (candidate is None or prior_stop > candidate)
                else "current_candidate"
            )
        else:
            status = "OPENED_OR_RESTARTED"
            lifecycle_id = _stop_lifecycle_id(row, status)
            effective = candidate
            source = "current_candidate_lifecycle_start"

        price = _to_float(row.get("Price"))
        invalidation = _to_float(row.get("InvalidationClose"))
        stop_breached = bool(
            current_active and price is not None and effective is not None and price <= effective
        )
        if current_active and price is not None and invalidation is not None and price < invalidation:
            stop_status = "INVALIDATION_BREACHED"
        elif stop_breached:
            stop_status = "TRAIL_BREACHED"
        elif effective is None:
            stop_status = "UNAVAILABLE"
        else:
            stop_status = "OK"

        row["MovingStopCandidate"] = _round(candidate)
        row["PriorMovingStop"] = _round(prior_stop) if previous_active else None
        row["MovingStop"] = _round(effective)
        row["MovingStopSource"] = source
        row["MovingStopBreached"] = stop_breached
        row["StopStatus"] = stop_status
        blocked_by = [
            item for item in str(row.get("BlockedBy") or "").split(";") if item
        ]
        row["ExistingAction"] = manage_existing_action(
            row.get("EntryDecision"), stop_status, blocked_by
        )
        lifecycle = {
            "active": current_active,
            "status": status,
            "id": lifecycle_id,
            "previousId": previous_id,
            "resetApplied": bool(reset_reason),
            "resetReason": reset_reason,
            "observedAsOf": row.get("PriceAsOf"),
            "observedShares": row.get("Shares"),
            "provenance": "current dashboard holding joined to the prior saved trend-plan row by Symbol",
            "limitation": (
                "A close-and-reopen between saved snapshots cannot be inferred; "
                "use --reset-stop SYMBOL=REASON when that occurs."
            ),
        }
        row["StopLifecycle"] = lifecycle
        if reset_reason:
            resets_applied.append({
                "symbol": symbol,
                "reason": reset_reason,
                "previousLifecycleId": previous_id,
                "newLifecycleId": lifecycle_id,
            })

    try:
        prior_schema = int(plan.get("schemaVersion") or 0)
    except (TypeError, ValueError):
        prior_schema = 0
    plan["schemaVersion"] = max(3, prior_schema)
    plan["stopLifecyclePolicy"] = {
        "schemaVersion": 1,
        "rule": "continuous active holding effective stop = max(previous effective stop, current candidate)",
        "loweringRequiresExplicitReset": True,
        "resetInterface": "--reset-stop SYMBOL=REASON (repeatable)",
        "previousArtifactGeneratedAt": previous.get("generatedAt"),
        "resetsApplied": resets_applied,
        "continuityKey": "Symbol plus positive Shares in consecutive saved artifacts",
        "limitation": (
            "Position changes within the interval between saved artifacts are not observable; "
            "record an explicit reset after a close-and-reopen."
        ),
    }
    return plan


def max_weight_for(stock):
    sym = stock.get("sym")
    theme = stock.get("theme") or ""
    if sym in CORE_ETFS:
        return 30.0
    if sym == "QQQ":
        return 18.0
    if sym == "SPMO":
        return 8.0
    if sym in LEVERAGED:
        return 1.0
    if sym == "NVDA":
        return 20.0
    if theme == "半导体":
        return 4.0
    if theme == "互联网/通信":
        return 18.0 if sym == "GOOGL" else 3.0
    return 3.0


def aggregate_recent_sells(payload, since):
    rows = []
    for stock in payload.get("stocks") or []:
        sells = [t for t in stock.get("txns") or [] if t.get("side") == "SELL" and t.get("date", "") >= since]
        if not sells:
            continue
        qty = sum(_to_float(t.get("qty")) or 0.0 for t in sells)
        proceeds = sum(_to_float(t.get("amount")) or 0.0 for t in sells)
        realized = sum(_to_float(t.get("realized")) or 0.0 for t in sells if _to_float(t.get("realized")) is not None)
        avg_px = proceeds / qty if qty else None
        rows.append({
            "Symbol": stock.get("sym"),
            "HeldNow": bool(stock.get("held")),
            "SellCount": len(sells),
            "QtySold": _round(qty, 3),
            "AvgSell": _round(avg_px),
            "Proceeds": _round(proceeds),
            "RealizedWindow": _round(realized),
            "RemainingShares": _round(stock.get("shares"), 3),
            "LastSellDate": max(t.get("date") for t in sells),
        })
    rows.sort(key=lambda r: (r["LastSellDate"], r["Proceeds"] or 0), reverse=True)
    return rows


def build_plan(payload, sentinel, spmo, ohlc_map=None, no_fetch=False, now=None):
    now = now or dt.datetime.now(tz=ET)
    if now.tzinfo is None:
        now = now.replace(tzinfo=ET)
    summary = payload.get("summary") or {}
    held = [dict(s) for s in payload.get("stocks") or [] if s.get("held")]
    market_value = _to_float(summary.get("marketValue")) or 0.0
    dashboard_as_of = summary.get("priceAsOf")
    try:
        as_of_date = dt.date.fromisoformat(str(dashboard_as_of))
    except (TypeError, ValueError) as exc:
        raise ValueError("dashboard summary.priceAsOf must be an ISO date") from exc
    for stock in held:
        stock["_marketValue"] = market_value
        stock["_expectedAsOf"] = dashboard_as_of

    symbols = [s.get("sym") for s in held if s.get("sym")]
    if ohlc_map is None:
        ohlc_map = {} if no_fetch else fetch_ohlc(sorted(set(symbols + ["SPY"])))
    stock_by_sym = {s.get("sym"): s for s in payload.get("stocks") or []}
    features = {}
    price_sources = {}
    for symbol in set(symbols + ["SPY"]):
        provider_rows = ohlc_map.get(symbol) or []
        rows = provider_rows or close_only_rows(stock_by_sym.get(symbol, {}))
        filtered = []
        for row in rows:
            try:
                row_date = dt.date.fromisoformat(str(row.get("date"))[:10])
            except (TypeError, ValueError):
                continue
            if row_date <= as_of_date:
                filtered.append(row)
        rows = filtered
        price_sources[symbol] = (
            "yahoo_adjusted_ohlc" if provider_rows
            else ("dashboard_adjusted_close_fallback" if rows else "missing")
        )
        features[symbol] = feature_rows(rows)

    market = market_context(payload, sentinel, now=now)
    global_data_issues = list(market.get("dataIssues") or [])
    for stock in held:
        stock["_globalDataIssues"] = global_data_issues
    concentration = concentration_context(payload)
    spy_features = features.get("SPY") or []
    rows = []
    for stock in held:
        spmo_aligned = spmo if spmo.get("asOf") == dashboard_as_of else {}
        row = level_plan(
            stock, features.get(stock["sym"]) or [], spy_features,
            market, concentration, spmo_aligned,
        )
        row["PriceSource"] = price_sources.get(stock["sym"], "missing")
        rows.append(row)
    rows.sort(key=lambda r: r["WeightPct"] or 0, reverse=True)
    since = (dt.date.fromisoformat(summary.get("priceAsOf") or summary.get("dateRange", ["2026-01-01"])[-1]) - dt.timedelta(days=7)).isoformat()
    all_level_data_pass = all(row.get("DataQuality") == "PASS" for row in rows)
    freshness_pass = bool(market.get("sentinelCurrent") and all_level_data_pass)
    return {
        "generatedAt": now.astimezone().isoformat(timespec="seconds"),
        "schemaVersion": 3,
        "researchOnly": True,
        "decisionGrade": False,
        "decisionGradeReason": "Heuristic levels require current aligned price history, a current market gate, sizing review, and manual execution validation.",
        "dataFreshness": {
            "status": "PASS" if freshness_pass else "BLOCK",
            "issues": sorted(set(
                global_data_issues
                + [
                    f"{row.get('Symbol')}:{item}"
                    for row in rows
                    for item in str(row.get("BlockedBy") or "").split(";")
                    if row.get("EntryDecision") == "BLOCK_DATA" and item
                ]
            )),
            "dashboardPriceAsOf": summary.get("priceAsOf"),
            "dashboardGeneratedAt": summary.get("generatedAt"),
            "absoluteSession": market.get("dashboardSession"),
            "sentinelCurrent": market.get("sentinelCurrent"),
            "sentinelAgeHours": market.get("sentinelAgeHours"),
            "priceSource": "Yahoo Finance OHLC via yfinance" if not no_fetch else "dashboard close cache",
            "priceSourceBySymbol": price_sources,
            "spmoAligned": spmo.get("asOf") == dashboard_as_of,
        },
        "market": market,
        "concentration": concentration,
        "rules": execution_rules(),
        "levels": rows,
        "recentSellsSince": since,
        "recentSells": aggregate_recent_sells(payload, since),
    }


def execution_rules():
    return [
        "Market gate first: when market label is BLOCK, no new high-beta/TQQQ/Call/SPMO offense.",
        "Buy-stop-limit is for reclaim only: trigger above EMA21/prior-high repair, limit caps slippage, never convert to market.",
        "Pullback-limit is only valid after trend is confirmed; two closes below EMA21 or below EMA50 cancels it.",
        "Move stops up only: use highest-close/ATR or EMA21/ATR references; never lower a stop to avoid discipline.",
        "Core and tactical are separate: tactical can be stopped/re-entered; core is reduced only by thesis, concentration, or risk-budget rules.",
    ]


def fmt_money(value):
    value = _to_float(value)
    return "—" if value is None else f"${value:,.2f}"


def fmt_pct(value):
    value = _to_float(value)
    return "—" if value is None else f"{value:+.2f}%"


def fmt_num(value, digits=2):
    value = _to_float(value)
    return "—" if value is None else f"{value:,.{digits}f}"


def format_report(plan):
    m = plan["market"]
    c = plan["concentration"]
    lines = [
        "# Trend Execution Plan",
        "",
        f"Generated: {plan['generatedAt']}",
        f"Data: **{plan['dataFreshness'].get('status', 'UNKNOWN')}** · dashboard prices as of {plan['dataFreshness'].get('dashboardPriceAsOf')} · source {plan['dataFreshness'].get('priceSource')}",
        f"Market gate: **{m.get('label')}** · QQQ {fmt_money(m.get('qqqClose'))} vs EMA21 {fmt_money(m.get('qqqEma21'))}",
        f"Concentration: semis {fmt_pct(c.get('semisWeightPct'))} weight / {fmt_pct(c.get('semisRiskPct'))} risk; top holding {fmt_pct(c.get('topWeight'))}, top five {fmt_pct(c.get('top5Weight'))}",
        "",
        "## Rules",
    ]
    for rule in plan["rules"]:
        lines.append(f"- {rule}")
    lines += [
        "",
        "## Current Levels",
        "| Symbol | Decision | Existing | Price | EMA21 | EMA50 | ATR | RS21/SPY | Buy-stop / limit | Pullback zone | Invalidation | Moving stop (candidate) | Lifecycle | Stop | Blocked by |",
        "|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|---|",
    ]
    for row in plan["levels"]:
        zone = row.get("PullbackZone")
        zone_text = "—" if not zone else f"{fmt_money(zone[0])}-{fmt_money(zone[1])}"
        lines.append(
            f"| {row['Symbol']} | {row['EntryDecision']} | {row['ExistingAction']} | {fmt_money(row['Price'])} | {fmt_money(row['EMA21'])} | "
            f"{fmt_money(row['EMA50'])} | {fmt_money(row['ATR14'])} | {fmt_pct(row['RS21vsSPY'])} | "
            f"{fmt_money(row['BuyStop'])} / {fmt_money(row['BuyLimit'])} | {zone_text} | "
            f"{fmt_money(row['InvalidationClose'])} | {fmt_money(row['MovingStop'])} ({fmt_money(row.get('MovingStopCandidate'))}) | "
            f"{(row.get('StopLifecycle') or {}).get('status', '—')} | {row['StopStatus']} | {row['BlockedBy'] or '—'} |"
        )
    lines += [
        "",
        f"## Recent Sells Since {plan['recentSellsSince']}",
        "| Symbol | Qty sold | Avg sell | Proceeds | Realized(window) | Remaining | Note |",
        "|---|---:|---:|---:|---:|---:|---|",
    ]
    if not plan["recentSells"]:
        lines.append("| — | — | — | — | — | — | no recent sells |")
    for row in plan["recentSells"]:
        note = "still held, use reclaim plan" if row["HeldNow"] else "exited; only re-enter on trigger"
        lines.append(
            f"| {row['Symbol']} | {fmt_num(row['QtySold'], 3)} | {fmt_money(row['AvgSell'])} | "
            f"{fmt_money(row['Proceeds'])} | {fmt_money(row['RealizedWindow'])} | "
            f"{fmt_num(row['RemainingShares'], 3)} | {note} |"
        )
    return "\n".join(lines) + "\n"


def write_outputs(plan, out_dir=DEFAULT_OUT, *, resets=None):
    out_dir = Path(out_dir)
    latest_path = out_dir / "latest_trend_execution_plan.json"
    previous = {}
    if latest_path.exists():
        try:
            previous = json.loads(latest_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ValueError(
                "cannot safely apply moving-stop ratchets because the prior trend artifact is unreadable"
            ) from exc
        if not isinstance(previous, dict):
            raise ValueError("prior trend artifact must contain a JSON object")
    apply_persisted_moving_stops(plan, previous, resets=resets)
    atomic_write_json(latest_path, plan)
    atomic_write_text(out_dir / "latest_trend_execution_plan.md", format_report(plan))

    levels_path = out_dir / "latest_trend_levels.csv"
    if plan["levels"]:
        atomic_write_csv(levels_path, plan["levels"], list(plan["levels"][0].keys()))
    else:
        atomic_write_text(levels_path, "")

    recent_sells_path = out_dir / "latest_recent_sells.csv"
    if plan["recentSells"]:
        atomic_write_csv(
            recent_sells_path,
            plan["recentSells"],
            list(plan["recentSells"][0].keys()),
        )
    else:
        atomic_write_text(recent_sells_path, "")


def main():
    parser = argparse.ArgumentParser(description="Generate trend-following execution plan for current holdings.")
    parser.add_argument("--dashboard", default=str(DEFAULT_DASHBOARD))
    parser.add_argument("--sentinel", default=str(DEFAULT_SENTINEL))
    parser.add_argument("--spmo", default=str(DEFAULT_SPMO))
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT))
    parser.add_argument("--no-fetch", action="store_true", help="Use dashboard close cache instead of fetching OHLC.")
    parser.add_argument(
        "--reset-stop",
        action="append",
        default=[],
        metavar="SYMBOL=REASON",
        help=(
            "Start a new saved stop lifecycle for SYMBOL. Repeatable; a non-empty "
            "reason is required and recorded in the artifact."
        ),
    )
    args = parser.parse_args()

    payload = read_dashboard_payload(args.dashboard)
    plan = build_plan(payload, read_json(args.sentinel), read_json(args.spmo), no_fetch=args.no_fetch)
    try:
        resets = parse_stop_resets(args.reset_stop)
        write_outputs(plan, args.out_dir, resets=resets)
    except ValueError as exc:
        parser.error(str(exc))
    print(f"{plan['dataFreshness'].get('dashboardPriceAsOf')} trend execution {plan['market'].get('label')} -> {len(plan['levels'])} held levels")
    print(Path(args.out_dir).resolve() / "latest_trend_execution_plan.md")


if __name__ == "__main__":
    main()

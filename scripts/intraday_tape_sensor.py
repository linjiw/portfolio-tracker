#!/usr/bin/env python3
"""Deterministic L1/L4a sensor for the Codex intraday tape Judge.

Outputs:
  output/intraday_tape/observation.json
  output/intraday_tape/gates.json
  output/intraday_tape/state.json

This script does not produce trading advice. Codex is the Judge layer.
"""

import argparse
import datetime as dt
import fcntl
import json
import math
import os
from pathlib import Path
from zoneinfo import ZoneInfo

import yfinance as yf

try:
    from scripts.artifact_io import atomic_write_json, ensure_private_directory
except ModuleNotFoundError:  # direct ``python scripts/intraday_tape_sensor.py``
    from artifact_io import atomic_write_json, ensure_private_directory


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output" / "intraday_tape"
PT = ZoneInfo("America/Los_Angeles")
ET = ZoneInfo("America/New_York")
EVENT_CALENDAR_PATH = OUT / "events.json"
INTERVALS = {"1m": "7d", "5m": "30d", "15m": "60d", "30m": "60d"}
INTERVAL_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "30m": 30}
REQUIRED_FRESH_INTERVALS = ("5m", "15m")
MIN_VOLUME_BASELINE_DAYS = 3
DAILY_SETTLEMENT_DELAY_MINUTES = 15


def now_et():
    return dt.datetime.now(tz=ET)


def _nth_weekday(year, month, weekday, occurrence):
    first = dt.date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + dt.timedelta(days=offset + 7 * (occurrence - 1))


def _last_weekday(year, month, weekday):
    if month == 12:
        last = dt.date(year + 1, 1, 1) - dt.timedelta(days=1)
    else:
        last = dt.date(year, month + 1, 1) - dt.timedelta(days=1)
    return last - dt.timedelta(days=(last.weekday() - weekday) % 7)


def _easter_sunday(year):
    """Gregorian Easter (Meeus/Jones/Butcher), used for Good Friday."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = (h + l - 7 * m + 114) % 31 + 1
    return dt.date(year, month, day)


def _observed(date):
    if date.weekday() == 5:
        return date - dt.timedelta(days=1)
    if date.weekday() == 6:
        return date + dt.timedelta(days=1)
    return date


def us_equity_holidays(year):
    """Deterministic standard US equity closures for the requested year."""
    holidays = {}
    for base_year in (year, year + 1):
        for base, name in (
            (dt.date(base_year, 1, 1), "New Year's Day"),
            (dt.date(base_year, 7, 4), "Independence Day"),
            (dt.date(base_year, 12, 25), "Christmas Day"),
        ):
            observed = _observed(base)
            if observed.year == year:
                holidays[observed] = name
    holidays[_nth_weekday(year, 1, 0, 3)] = "Martin Luther King Jr. Day"
    holidays[_nth_weekday(year, 2, 0, 3)] = "Washington's Birthday"
    holidays[_easter_sunday(year) - dt.timedelta(days=2)] = "Good Friday"
    holidays[_last_weekday(year, 5, 0)] = "Memorial Day"
    if year >= 2022:
        holidays[_observed(dt.date(year, 6, 19))] = "Juneteenth"
    holidays[_nth_weekday(year, 9, 0, 1)] = "Labor Day"
    holidays[_nth_weekday(year, 11, 3, 4)] = "Thanksgiving Day"
    return holidays


def market_session(date):
    """Return standard NYSE/Nasdaq session bounds, including common early closes.

    Unscheduled exchange closures remain the responsibility of the required
    event calendar, but weekends, standard holidays, and recurring 13:00 ET
    early closes are deterministic here.
    """
    if date.weekday() >= 5:
        return {"date": date.isoformat(), "open": False, "reason": "weekend", "calendar": "us_equity_v1"}
    holiday = us_equity_holidays(date.year).get(date)
    if holiday:
        return {"date": date.isoformat(), "open": False, "reason": holiday, "calendar": "us_equity_v1"}

    close_time = dt.time(16, 0)
    reason = "regular_session"
    thanksgiving = _nth_weekday(date.year, 11, 3, 4)
    if date == thanksgiving + dt.timedelta(days=1):
        close_time, reason = dt.time(13, 0), "day_after_thanksgiving_early_close"
    elif date.month == 7 and date.day == 3 and date.weekday() <= 3:
        close_time, reason = dt.time(13, 0), "pre_independence_day_early_close"
    elif date.month == 12 and date.day == 24 and date.weekday() <= 3:
        close_time, reason = dt.time(13, 0), "christmas_eve_early_close"

    opened = dt.datetime.combine(date, dt.time(9, 30), tzinfo=ET)
    closed = dt.datetime.combine(date, close_time, tzinfo=ET)
    return {
        "date": date.isoformat(), "open": True,
        "openAt": opened.isoformat(timespec="minutes"),
        "closeAt": closed.isoformat(timespec="minutes"),
        "newEntryStart": (opened + dt.timedelta(minutes=15)).isoformat(timespec="minutes"),
        "newEntryEnd": (closed - dt.timedelta(minutes=90)).isoformat(timespec="minutes"),
        "reason": reason, "earlyClose": close_time != dt.time(16, 0),
        "calendar": "us_equity_v1",
    }


def atomic_json(path, payload):
    """Strict, crash-safe, private publication for runtime JSON."""
    atomic_write_json(path, payload)


def acquire_lock(path):
    """Return an exclusive non-blocking process lock, or ``None`` if busy."""
    path = Path(path)
    ensure_private_directory(path.parent)
    handle = path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    handle.seek(0)
    handle.truncate()
    handle.write(f"pid={os.getpid()} started={now_et().isoformat()}\n")
    handle.flush()
    os.fsync(handle.fileno())
    path.chmod(0o600)
    return handle


def release_lock(handle):
    if handle is None:
        return
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def parse_timestamp(value):
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    # Event and market-data timestamps must be explicit.  Guessing a timezone
    # around DST boundaries can move a hard gate by an hour.
    return parsed if parsed.tzinfo is not None else None


def load_strict_json(path):
    def reject_constant(value):
        raise ValueError(f"non-finite JSON constant {value}")
    return json.loads(Path(path).read_text(encoding="utf-8"), parse_constant=reject_constant)


def load_event_calendar(path=EVENT_CALENDAR_PATH, now=None):
    """Load a user-maintained, validity-bounded binary-event calendar.

    The safe default is unavailable, not an evergreen hard-coded event list.
    A valid file is an object with ``schemaVersion: 1``, ``validThrough``
    (YYYY-MM-DD), and ``events`` containing timezone-aware ``t`` timestamps.
    An empty event list is valid when explicitly covered by ``validThrough``.
    """
    current = (now or now_et()).astimezone(ET)
    path = Path(path)
    base = {"path": str(path), "available": False, "fresh": False, "events": []}
    if not path.exists():
        return {**base, "reason": "event_calendar_missing"}
    try:
        doc = load_strict_json(path)
    except Exception as exc:
        return {**base, "reason": f"event_calendar_invalid_json:{type(exc).__name__}"}
    if not isinstance(doc, dict) or doc.get("schemaVersion") != 1:
        return {**base, "reason": "event_calendar_schema_mismatch"}
    try:
        valid_through = dt.date.fromisoformat(str(doc["validThrough"]))
    except (KeyError, TypeError, ValueError):
        return {**base, "reason": "event_calendar_valid_through_invalid"}
    raw_events = doc.get("events")
    if not isinstance(raw_events, list):
        return {**base, "reason": "event_calendar_events_not_list"}
    events = []
    for index, event in enumerate(raw_events):
        if not isinstance(event, dict) or not str(event.get("name") or "").strip():
            return {**base, "reason": f"event_calendar_event_invalid:{index}"}
        event_time = parse_timestamp(event.get("t"))
        if event_time is None:
            return {**base, "reason": f"event_calendar_timestamp_invalid:{index}"}
        events.append({**event, "t": event_time.isoformat(timespec="seconds")})
    fresh = current.date() <= valid_through
    return {
        **base,
        "available": True,
        "fresh": fresh,
        "reason": None if fresh else "event_calendar_expired",
        "schemaVersion": 1,
        "generatedAt": doc.get("generatedAt"),
        "validThrough": valid_through.isoformat(),
        "events": events if fresh else [],
        "eventCount": len(events),
    }


def observation_freshness(observation, now=None):
    """Require current-session, recently closed 5m and 15m evidence bars."""
    current = (now or now_et()).astimezone(ET)
    checks = {}
    for interval in REQUIRED_FRESH_INTERVALS:
        summary = (observation.get("intervals") or {}).get(interval) or {}
        last_closed = summary.get("last_closed") or {}
        started = parse_timestamp(last_closed.get("t"))
        minutes = INTERVAL_MINUTES[interval]
        if not summary.get("available") or started is None:
            checks[interval] = {"fresh": False, "reason": "missing_closed_bar"}
            continue
        started = started.astimezone(ET)
        ended = started + dt.timedelta(minutes=minutes)
        age = (current.astimezone(dt.timezone.utc) - ended.astimezone(dt.timezone.utc)).total_seconds() / 60.0
        same_session = started.date() == current.date()
        # At a polling boundary the newest interval may still be in the
        # provider's 30-second settlement grace, so one interval plus five
        # minutes is tolerated. More than one missing closed bar is stale.
        max_age = minutes + 5
        fresh = same_session and -2.0 <= age <= max_age
        if not same_session:
            reason = "different_et_session_date"
        elif age < -2.0:
            reason = "bar_end_in_future"
        elif age > max_age:
            reason = "bar_stale"
        else:
            reason = None
        checks[interval] = {
            "fresh": fresh,
            "reason": reason,
            "barStart": started.isoformat(timespec="minutes"),
            "barEnd": ended.isoformat(timespec="minutes"),
            "ageMinutes": round(age, 1),
            "maxAgeMinutes": max_age,
        }
    fresh = all(checks.get(interval, {}).get("fresh") for interval in REQUIRED_FRESH_INTERVALS)
    return {
        "fresh": fresh,
        "reason": None if fresh else "required_5m_15m_bars_missing_stale_or_wrong_session",
        "checkedAt": current.isoformat(timespec="seconds"),
        "intervals": checks,
    }


def to_float(value):
    try:
        if value is None:
            return None
        converted = float(value)
        return converted if math.isfinite(converted) else None
    except Exception:
        return None


def rnd(value, digits=2):
    value = to_float(value)
    return None if value is None else round(value, digits)


def mean(values):
    vals = [to_float(v) for v in values]
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else None


def ema(values, n):
    alpha = 2.0 / (n + 1)
    out, cur = [], None
    for raw in values:
        v = to_float(raw)
        if v is None:
            out.append(cur)
            continue
        cur = v if cur is None else alpha * v + (1 - alpha) * cur
        out.append(cur)
    return out


def true_ranges(rows):
    out, prev = [], None
    for row in rows:
        high, low, close = row["high"], row["low"], row["close"]
        tr = high - low if prev is None else max(high - low, abs(high - prev), abs(low - prev))
        out.append(tr)
        prev = close
    return out


def wilder_values(values, n):
    """Wilder-smoothed values, unavailable until a full ``n``-row seed."""
    out = [None] * len(values)
    if len(values) < n:
        return out
    seed = mean(values[:n])
    if seed is None:
        return out
    out[n - 1] = seed
    cur = seed
    for index in range(n, len(values)):
        value = to_float(values[index])
        if value is None:
            out[index] = None
            continue
        cur = (cur * (n - 1) + value) / n
        out[index] = cur
    return out


def flatten_download(df, symbol):
    if df is None or df.empty:
        return df
    try:
        import pandas as pd
        if isinstance(df.columns, pd.MultiIndex):
            for level in reversed(range(df.columns.nlevels)):
                vals = [str(x) for x in df.columns.get_level_values(level)]
                if symbol in vals:
                    return df.xs(symbol, axis=1, level=level)
            return df.droplevel(-1, axis=1)
    except Exception:
        pass
    return df


def df_to_rows(df, symbol):
    df = flatten_download(df, symbol)
    if df is None or df.empty:
        return []
    colmap = {str(c).strip().lower(): c for c in df.columns}
    if any(name not in colmap for name in ("open", "high", "low", "close")):
        return []
    rows = []
    for idx, row in df.iterrows():
        close = to_float(row[colmap.get("close")])
        high = to_float(row[colmap.get("high")])
        low = to_float(row[colmap.get("low")])
        open_ = to_float(row[colmap.get("open")])
        if (close is None or high is None or low is None or open_ is None or
                min(open_, high, low, close) <= 0 or high < max(open_, close, low) or
                low > min(open_, close, high)):
            continue
        vol_col = colmap.get("volume")
        ts = idx.to_pydatetime() if hasattr(idx, "to_pydatetime") else idx
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=ET)
        volume = to_float(row[vol_col]) if vol_col is not None else 0.0
        rows.append({
            "t": ts.astimezone(ET).isoformat(timespec="minutes"),
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": max(volume or 0.0, 0.0),
        })
    by_time = {row["t"]: row for row in rows}
    return [by_time[key] for key in sorted(by_time)]


def parse_t(value):
    return dt.datetime.fromisoformat(value)


def session_date(row):
    return row["t"][:10]


def tod_key(row):
    ts = parse_t(row["t"])
    return ts.strftime("%H%M")


def group_sessions(rows):
    sessions = {}
    for row in rows:
        sessions.setdefault(session_date(row), []).append(row)
    return sessions


def add_features(rows):
    closes = [r["close"] for r in rows]
    for name, n in (("ema5", 5), ("ema8", 8), ("ema13", 13), ("ema21", 21), ("ema34", 34)):
        vals = ema(closes, n)
        for i, row in enumerate(rows):
            row[name] = vals[i]
    cur_date = None
    cum_pv = 0.0
    cum_vol = 0.0
    for row in rows:
        if session_date(row) != cur_date:
            cur_date = session_date(row)
            cum_pv = 0.0
            cum_vol = 0.0
        typical = (row["high"] + row["low"] + row["close"]) / 3.0
        vol = max(row.get("volume") or 0.0, 0.0)
        if vol:
            cum_pv += typical * vol
            cum_vol += vol
        row["vwap"] = cum_pv / cum_vol if cum_vol else typical
    return rows


def split_closed_forming(rows, interval, now=None):
    if not rows:
        return [], None
    minutes = INTERVAL_MINUTES[interval]
    current = (now or now_et()).astimezone(ET)
    last = rows[-1]
    last_start = parse_t(last["t"])
    bar_end = last_start + dt.timedelta(minutes=minutes)
    if current < bar_end + dt.timedelta(seconds=30):
        age = max(0, int((current - last_start).total_seconds()))
        forming = {**last, "age_sec": age, "vol_so_far": last.get("volume")}
        return rows[:-1], forming
    return rows, None


def fetch_intraday(symbol, now=None):
    out = {}
    for interval, period in INTERVALS.items():
        df = yf.download(symbol, period=period, interval=interval, auto_adjust=True, prepost=False, progress=False, threads=False)
        rows = add_features(df_to_rows(df, symbol))
        closed, forming = split_closed_forming(rows, interval, now=now)
        out[interval] = {"closed": closed, "forming": forming}
    return out


def completed_daily_rows(rows, now=None):
    """Exclude today's daily candle until the regular close has settled."""
    current = (now or now_et()).astimezone(ET)
    settled = current.replace(hour=16, minute=0, second=0, microsecond=0) + dt.timedelta(
        minutes=DAILY_SETTLEMENT_DELAY_MINUTES
    )
    if current < settled:
        return [row for row in rows if session_date(row) != current.date().isoformat()]
    return rows


def fetch_daily(symbol, now=None):
    # Adjust historical OHLC for splits/distributions so long-window EMA/ATR
    # and fib levels remain on today's tradable price scale.
    df = yf.download(symbol, period="9mo", interval="1d", auto_adjust=True, progress=False, threads=False)
    rows = completed_daily_rows(df_to_rows(df, symbol), now=now)
    if not rows:
        return []
    closes = [r["close"] for r in rows]
    for name, n in (("ema8", 8), ("ema13", 13), ("ema21", 21), ("ema34", 34), ("ema55", 55)):
        vals = ema(closes, n)
        for i, row in enumerate(rows):
            row[name] = vals[i]
    trs = true_ranges(rows)
    atr = wilder_values(trs, 14)
    for i, row in enumerate(rows):
        row["atr14"] = atr[i]
    return rows


def session_rows(rows):
    if not rows:
        return []
    last_date = session_date(rows[-1])
    return [r for r in rows if session_date(r) == last_date]


def prior_session(rows):
    sessions = group_sessions(rows)
    dates = sorted(sessions)
    return sessions[dates[-2]] if len(dates) >= 2 else []


def same_tod_baseline(rows, current_row, lookback=10):
    key = tod_key(current_row)
    sessions = group_sessions(rows)
    cur_date = session_date(current_row)
    vals = []
    cum_vals = []
    for date in sorted(sessions):
        if date >= cur_date:
            continue
        day = sessions[date]
        matches = [r for r in day if tod_key(r) == key]
        if matches:
            slot_volume = to_float(matches[-1].get("volume"))
            if slot_volume is None or slot_volume <= 0:
                continue
            vals.append(slot_volume)
            cutoff = parse_t(matches[-1]["t"]).time()
            cum_vals.append(sum(
                max(to_float(r.get("volume")) or 0.0, 0.0)
                for r in day if parse_t(r["t"]).time() <= cutoff
            ))
    vals = vals[-lookback:]
    cum_vals = cum_vals[-lookback:]
    today = [r for r in rows if session_date(r) == cur_date and parse_t(r["t"]) <= parse_t(current_row["t"])]
    current_cum = sum(max(to_float(r.get("volume")) or 0.0, 0.0) for r in today)
    current_volume = to_float(current_row.get("volume"))
    base = mean(vals)
    cum_base = mean(cum_vals)
    sufficient = len(vals) >= MIN_VOLUME_BASELINE_DAYS and len(cum_vals) >= MIN_VOLUME_BASELINE_DAYS
    return {
        "tod": key,
        "baseline": rnd(base, 0),
        "rvol": rnd(current_volume / base, 2) if sufficient and base and current_volume and current_volume > 0 else None,
        "cum_baseline": rnd(cum_base, 0),
        "cum_rvol": rnd(current_cum / cum_base, 2) if sufficient and cum_base else None,
        "sample_days": len(vals),
        "min_sample_days": MIN_VOLUME_BASELINE_DAYS,
        "sufficient": sufficient,
    }


def interval_summary(rows, interval):
    sess = session_rows(rows)
    if not sess:
        return {"available": False, "interval": interval}
    last = sess[-1]
    mins = INTERVAL_MINUTES[interval]
    n45 = max(1, math.ceil(45 / mins))
    recent = sess[-n45:]
    prior = sess[-2 * n45:-n45] if len(sess) >= 2 * n45 else []
    vals = [last.get(k) for k in ("ema5", "ema8", "ema13", "ema21", "ema34") if last.get(k) is not None]
    atr20_series = wilder_values(true_ranges(rows), 20)
    tr20 = atr20_series[-1] if atr20_series else None
    mean20 = mean([r["close"] for r in sess[-20:]]) if len(sess) >= 20 else None
    vol = same_tod_baseline(rows, last)
    return {
        "available": True,
        "interval": interval,
        "last_closed": last,
        "session_date": session_date(last),
        "bars_today": len(sess),
        "day_high": rnd(max(r["high"] for r in sess)),
        "day_low": rnd(min(r["low"] for r in sess)),
        "recent45_high": rnd(max(r["high"] for r in recent)),
        "recent45_low": rnd(min(r["low"] for r in recent)),
        "prior45_high": rnd(max((r["high"] for r in prior), default=None)),
        "prior45_low": rnd(min((r["low"] for r in prior), default=None)),
        "fresh_low45": None if not prior else min(r["low"] for r in recent) < min(r["low"] for r in prior) - 0.01,
        "fresh_high45": None if not prior else max(r["high"] for r in recent) > max(r["high"] for r in prior) + 0.01,
        "vwap": rnd(last.get("vwap")),
        "fma_band": {"bottom": rnd(min(vals) if vals else None), "top": rnd(max(vals) if vals else None)},
        "ema": {k: rnd(last.get(k)) for k in ("ema5", "ema8", "ema13", "ema21", "ema34")},
        "range_position_pct": rnd((last["close"] - min(r["low"] for r in sess)) / (max(r["high"] for r in sess) - min(r["low"] for r in sess)) * 100, 1) if max(r["high"] for r in sess) > min(r["low"] for r in sess) else None,
        "mean20": rnd(mean20),
        "atr20": rnd(tr20),
        "atr20_method": "Wilder true range over closed bars",
        "extension_atr20": rnd(abs(last["close"] - mean20) / tr20, 2) if mean20 and tr20 else None,
        "move_direction": "up" if mean20 and last["close"] > mean20 else "down",
        "volume": vol,
    }


def fibs_macro(daily_rows):
    recent = daily_rows[-180:] if len(daily_rows) > 180 else daily_rows
    lo = min(r["low"] for r in recent)
    hi = max(r["high"] for r in recent)
    return {
        "swing": [rnd(lo), rnd(hi)],
        "levels": {
            "fib_macro_382": rnd(hi - (hi - lo) * 0.382),
            "fib_macro_500": rnd(hi - (hi - lo) * 0.500),
            "fib_macro_618": rnd(hi - (hi - lo) * 0.618),
        },
    }


def fibs_active(rows5):
    rows = rows5[-390:] if len(rows5) > 390 else rows5
    if not rows:
        return {"swing": [], "levels": {}}
    hi_idx, hi_row = max(enumerate(rows), key=lambda x: x[1]["high"])
    lo_idx, lo_row = min(enumerate(rows), key=lambda x: x[1]["low"])
    hi = hi_row["high"]
    lo = lo_row["low"]
    if hi_idx < lo_idx:
        return {
            "direction": "down",
            "swing": [rnd(hi), rnd(lo)],
            "levels": {
                "fib_active_236": rnd(lo + (hi - lo) * 0.236),
                "fib_active_382": rnd(lo + (hi - lo) * 0.382),
                "fib_active_500": rnd(lo + (hi - lo) * 0.500),
                "fib_active_618": rnd(lo + (hi - lo) * 0.618),
            },
        }
    return {
        "direction": "up",
        "swing": [rnd(lo), rnd(hi)],
        "levels": {
            "fib_active_236": rnd(hi - (hi - lo) * 0.236),
            "fib_active_382": rnd(hi - (hi - lo) * 0.382),
            "fib_active_500": rnd(hi - (hi - lo) * 0.500),
            "fib_active_618": rnd(hi - (hi - lo) * 0.618),
        },
    }


def add_candidate(candidates, px, taxonomy, factor):
    px = to_float(px)
    if px is None or px <= 0:
        return
    candidates.append({"px": px, "taxonomy": taxonomy, "factor": factor})


def build_levels(observation, state):
    candidates = []
    macro = observation["fibs"]["macro"]["levels"]
    active = observation["fibs"]["active"]["levels"]
    for k, px in macro.items():
        add_candidate(candidates, px, "fib_macro", k)
    for k, px in active.items():
        add_candidate(candidates, px, "fib_active", k)
    for k, px in observation.get("prior_day", {}).items():
        add_candidate(candidates, px, "prior_day_HLC", k)
    for label, summary in observation["intervals"].items():
        if not summary.get("available"):
            continue
        add_candidate(candidates, summary.get("vwap"), "vwap", f"{label}_vwap")
        add_candidate(candidates, summary.get("fma_band", {}).get("bottom"), "fma_band", f"{label}_fma_bottom")
        add_candidate(candidates, summary.get("fma_band", {}).get("top"), "fma_band", f"{label}_fma_top")
        add_candidate(candidates, summary.get("day_high"), "failed_high_low", f"{label}_day_high")
        add_candidate(candidates, summary.get("day_low"), "failed_high_low", f"{label}_day_low")
        add_candidate(candidates, summary.get("recent45_high"), "failed_high_low", f"{label}_recent45_high")
        add_candidate(candidates, summary.get("recent45_low"), "failed_high_low", f"{label}_recent45_low")
    current = observation["price"]["last"]
    if current:
        base = round(current / 5) * 5
        for px in (base - 5, base, base + 5):
            add_candidate(candidates, px, "round_number", f"round_{px}")

    tolerance = max(0.25, (current or 0) * 0.0015)
    clusters = []
    for cand in sorted(candidates, key=lambda x: x["px"]):
        for cluster in clusters:
            if abs(cand["px"] - cluster["px"]) <= tolerance:
                cluster["raw"].append(cand["px"])
                cluster["px"] = sum(cluster["raw"]) / len(cluster["raw"])
                cluster["items"].append(cand)
                break
        else:
            clusters.append({"px": cand["px"], "raw": [cand["px"]], "items": [cand]})

    state_levels = {f"{to_float(x.get('px')):.2f}": x for x in state.get("levels", []) if to_float(x.get("px")) is not None}
    levels = []
    for cluster in clusters:
        tax = []
        factors = []
        for item in cluster["items"]:
            if item["taxonomy"] not in tax:
                tax.append(item["taxonomy"])
            if item["factor"] not in factors:
                factors.append(item["factor"])
        key = f"{cluster['px']:.2f}"
        old = state_levels.get(key) or {}
        touched = current is not None and abs(cluster["px"] - current) <= tolerance
        tests = int(old.get("tests") or 0) + (1 if touched and old.get("last_tested_bar") != observation["price"].get("last_bar_time") else 0)
        levels.append({
            "px": rnd(cluster["px"]),
            "taxonomy": tax,
            "factors": factors,
            "confluence": len(tax),
            "main_battlefield": len(tax) >= 3,
            "tests": tests,
            "last_tested_bar": observation["price"].get("last_bar_time") if touched else old.get("last_tested_bar"),
        })
    return sorted(levels, key=lambda x: abs((x["px"] or 0) - (current or 0)))


def load_state(path, symbol, now=None):
    if path.exists():
        state = load_strict_json(path)
        if not isinstance(state, dict):
            raise ValueError("intraday tape state must be a JSON object")
        if state.get("schemaVersion") not in (None, 1):
            raise ValueError("unsupported intraday tape state schemaVersion")
    else:
        state = {}
    today = (now or now_et()).astimezone(ET).date().isoformat()
    if state.get("session_date") != today:
        if state:
            archive = path.with_name(f"state.{state.get('session_date', 'unknown')}.json")
            if not archive.exists():
                atomic_json(archive, state)
        state = {
            "schemaVersion": 1,
            "session_date": today,
            "instrument": symbol,
            "events": [],
            "swings": {},
            "levels": [],
            "vol_baseline_tod": {},
            "scenarios": [],
            "positions": [],
            "verdict_history": [],
            "burden_of_proof": "bears",
            "last_verdict_hash": "",
        }
    state.setdefault("events", [])
    state.setdefault("schemaVersion", 1)
    state.setdefault("levels", [])
    state.setdefault("positions", [])
    state.setdefault("verdict_history", [])
    return state


def read_positions(out_dir=OUT):
    out = []
    manual = Path(out_dir) / "positions.json"
    if manual.exists():
        data = load_strict_json(manual)
        positions = data if isinstance(data, list) else data.get("positions") if isinstance(data, dict) else None
        if not isinstance(positions, list):
            raise ValueError("positions.json must be a list or an object containing a positions list")
        out.extend(positions)
    sentinel = ROOT / "output" / "market_sentinel" / "latest_snapshot.json"
    if sentinel.exists():
        snap = load_strict_json(sentinel)
        if not isinstance(snap, dict):
            raise ValueError("market sentinel snapshot must be a JSON object")
        spreads = ((snap.get("agents") or {}).get("portfolio") or {}).get("optionSpreads") or []
        if not isinstance(spreads, list):
            raise ValueError("market sentinel optionSpreads must be a list")
        out.extend(spreads)
    deduped = []
    seen = set()
    for position in out:
        if not isinstance(position, dict):
            raise ValueError("each intraday position must be a JSON object")
        key = json.dumps(position, sort_keys=True, ensure_ascii=False, allow_nan=False)
        if key not in seen:
            seen.add(key)
            deduped.append(position)
    return deduped


def build_gates(observation, state, event_calendar=None, now=None):
    current = (now or now_et()).astimezone(ET)
    event_calendar = event_calendar or {
        "available": False,
        "fresh": False,
        "reason": "event_calendar_not_loaded",
        "events": [],
    }
    session = market_session(current.date())
    triggered = []
    action_locks = []
    score_cap = None
    data_freshness = observation.get("data_freshness") or observation_freshness(observation, now=current)
    if not data_freshness.get("fresh"):
        triggered.append({
            "gate": "G0",
            "name": "market_data_freshness_gate",
            "reason": data_freshness.get("reason") or "market data missing/stale",
            "details": data_freshness.get("intervals") or {},
        })
        action_locks.append("数据缺失或过期；不开新仓")
        score_cap = "BLOCK_DATA"
    if not session.get("open"):
        triggered.append({
            "gate": "G1", "name": "time_gate",
            "reason": f"US equity market closed: {session.get('reason')}",
        })
        action_locks.append("不开新仓")
    else:
        entry_start = parse_timestamp(session.get("newEntryStart"))
        entry_end = parse_timestamp(session.get("newEntryEnd"))
        if current < entry_start or current > entry_end:
            triggered.append({
                "gate": "G1", "name": "time_gate",
                "reason": (
                    f"outside session-aware new-entry window "
                    f"{entry_start.strftime('%H:%M')}-{entry_end.strftime('%H:%M')} ET"
                ),
            })
            action_locks.append("不开新仓")
    if not event_calendar.get("available") or not event_calendar.get("fresh"):
        triggered.append({
            "gate": "G2_DATA",
            "name": "event_calendar_gate",
            "reason": event_calendar.get("reason") or "event calendar unavailable",
        })
        action_locks.append("事件日历缺失或过期；不开新仓")
        score_cap = "BLOCK_DATA"
    active_events = []
    last_fifteen_start = parse_timestamp(
        (((observation.get("intervals") or {}).get("15m") or {}).get("last_closed") or {}).get("t")
    )
    if last_fifteen_start is not None:
        last_fifteen_start = last_fifteen_start.astimezone(ET)
    for event in event_calendar.get("events", []):
        try:
            event_t = dt.datetime.fromisoformat(event["t"]).astimezone(ET)
        except Exception:
            continue
        delta_min = (event_t - current).total_seconds() / 60.0
        if -15 <= delta_min <= 60:
            active_events.append({
                **event, "minutes_away": round(delta_min, 1),
                "status": "event_window",
            })
        elif delta_min < -15 and not (
            last_fifteen_start is not None
            and last_fifteen_start >= event_t
            and current >= last_fifteen_start + dt.timedelta(minutes=15, seconds=30)
        ):
            # An off-quarter-hour release can leave the first nominal 15m bar
            # mostly pre-event. Keep management-only until a bar that starts
            # after the release has actually closed.
            active_events.append({
                **event, "minutes_away": round(delta_min, 1),
                "status": "awaiting_post_event_closed_15m",
            })
    if active_events:
        triggered.append({"gate": "G2", "name": "event_gate", "events": active_events})
        action_locks.append("不开新仓")
    if data_freshness.get("fresh"):
        fifteen = observation["intervals"].get("15m") or {}
        vol = fifteen.get("volume") or {}
        if not vol.get("sufficient") or vol.get("rvol") is None or vol.get("cum_rvol") is None:
            triggered.append({
                "gate": "G3_DATA",
                "name": "volume_baseline_gate",
                "reason": (
                    f"15m same-time volume baseline requires {MIN_VOLUME_BASELINE_DAYS} prior sessions; "
                    f"found {int(vol.get('sample_days') or 0)}"
                ),
            })
            if score_cap is None:
                score_cap = "观察"
        if (vol.get("rvol") is not None and vol.get("rvol") < 1.0 and
                vol.get("cum_rvol") is not None and vol.get("cum_rvol") < 1.0):
            triggered.append({"gate": "G3", "name": "vacuum_gate", "reason": "15m rvol < 1.0 and cum_rvol < 1.0"})
            if score_cap is None:
                score_cap = "观察"
    next_events = []
    for event in event_calendar.get("events", []):
        try:
            event_t = dt.datetime.fromisoformat(event["t"]).astimezone(ET)
            if event_t >= current:
                next_events.append({**event, "hours_away": round((event_t - current).total_seconds() / 3600.0, 1)})
        except Exception:
            pass
    next_events.sort(key=lambda x: x["hours_away"])
    if data_freshness.get("fresh"):
        five = observation["intervals"].get("5m") or {}
        ext = five.get("extension_atr20")
        if ext is not None and ext > 1.5:
            triggered.append({
                "gate": "G5",
                "name": "chase_gate",
                "reason": "|close - mean20| > 1.5 * ATR20",
                "blocked_direction": five.get("move_direction"),
            })
    return {
        "run_id": observation.get("run_id"),
        "generated_at": current.isoformat(timespec="seconds"),
        "triggered": triggered,
        "action_lock": sorted(set(action_locks)),
        "score_cap": score_cap,
        "prohibit_allow": bool(action_locks) or any(x["gate"] in {"G0", "G2_DATA", "G3_DATA", "G3"} for x in triggered),
        "data_freshness": data_freshness,
        "event_calendar": {
            key: event_calendar.get(key)
            for key in ("path", "available", "fresh", "reason", "generatedAt", "validThrough", "eventCount")
        },
        "market_session": session,
        "cross_event_dte_policy": {
            "gate": "G4",
            "next_binary_event": next_events[0] if next_events else None,
            "calendar_fresh": bool(event_calendar.get("fresh")),
            "calendar_valid_through": event_calendar.get("validThrough"),
            "rule": (
                "Reject any option expiry beyond calendar_valid_through because event coverage is unknown. "
                "If a covered expiry crosses the next binary event, halve size or reject; "
                "0DTE/day-before crossing is rejected."
            ),
        },
        "llm_may_not_override": True,
    }


def main():
    ap = argparse.ArgumentParser(description="Build intraday tape observation and gates for Codex Judge.")
    ap.add_argument("--symbol", default="QQQ")
    ap.add_argument("--out-dir", default=str(OUT))
    ap.add_argument("--events-file", default=str(EVENT_CALENDAR_PATH),
                    help="validated event calendar JSON; missing/expired calendar blocks new entries")
    args = ap.parse_args()

    out = Path(args.out_dir)
    ensure_private_directory(out)
    lock = acquire_lock(out / ".sensor.lock")
    if lock is None:
        raise SystemExit("another intraday sensor run is active")
    try:
        return run_sensor(args, out)
    finally:
        release_lock(lock)


def run_sensor(args, out):
    state_path = out / "state.json"
    current = now_et()
    state = load_state(state_path, args.symbol, now=current)
    run_id = current.strftime("%Y%m%dT%H%M%S%f%z")
    event_calendar = load_event_calendar(args.events_file, now=current)
    state["events"] = list(event_calendar.get("events") or [])
    state["event_calendar"] = {
        key: event_calendar.get(key)
        for key in ("path", "available", "fresh", "reason", "generatedAt", "validThrough", "eventCount")
    }
    state["sensor_run_id"] = run_id

    intraday = fetch_intraday(args.symbol, now=current)
    daily = fetch_daily(args.symbol, now=current)
    summaries = {k: interval_summary(v["closed"], k) for k, v in intraday.items()}
    five_rows = intraday["5m"]["closed"]
    prior = prior_session(five_rows)
    prior_day = {
        "prior_day_high": rnd(max((r["high"] for r in prior), default=None)),
        "prior_day_low": rnd(min((r["low"] for r in prior), default=None)),
        "prior_day_close": rnd(prior[-1]["close"]) if prior else None,
    }
    price_candidates = [
        summary["last_closed"]
        for summary in summaries.values()
        if summary.get("available") and parse_timestamp((summary.get("last_closed") or {}).get("t"))
    ]
    price_row = max(price_candidates, key=lambda row: parse_timestamp(row["t"]).timestamp(), default=None)
    observation = {
        "schemaVersion": 1,
        "run_id": run_id,
        "generated_at": current.isoformat(timespec="seconds"),
        "symbol": args.symbol,
        "source": "Yahoo Finance via yfinance; adjusted-to-current-scale closed bars only",
        "price": {
            "last": rnd(price_row["close"] if price_row else None),
            "last_bar_time": price_row["t"] if price_row else None,
        },
        "forming": {k: v["forming"] for k, v in intraday.items()},
        "intervals": summaries,
        "prior_day": prior_day,
        "daily": daily[-1] if daily else {},
        "daily_price_basis": "split_and_distribution_adjusted_OHLC; current scale",
        "fibs": {
            "macro": fibs_macro(daily) if daily else {"swing": [], "levels": {}},
            "active": fibs_active(five_rows),
        },
        "positions": read_positions(out),
    }
    observation["data_freshness"] = observation_freshness(observation, now=current)
    observation["levels"] = build_levels(observation, state)
    state["swings"] = {
        "macro": observation["fibs"]["macro"].get("swing"),
        "active": observation["fibs"]["active"].get("swing"),
    }
    state["levels"] = observation["levels"]
    state["positions"] = observation["positions"]
    for summary in summaries.values():
        vol = summary.get("volume") or {}
        if vol.get("tod") and vol.get("baseline"):
            state.setdefault("vol_baseline_tod", {})[vol["tod"]] = vol["baseline"]

    gates = build_gates(observation, state, event_calendar=event_calendar, now=current)
    gates["schemaVersion"] = 1
    atomic_json(state_path, state)
    atomic_json(out / "observation.json", observation)
    # Publish gates last: readers either see the previous complete decision or
    # the new complete decision, never a half-written hard-gate document.
    atomic_json(out / "gates.json", gates)
    print(json.dumps({
        "observation": str(out / "observation.json"),
        "gates": str(out / "gates.json"),
        "state": str(state_path),
        "last": observation["price"]["last"],
        "triggered_gates": [g["gate"] for g in gates["triggered"]],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()

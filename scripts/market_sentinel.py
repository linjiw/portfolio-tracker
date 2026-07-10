#!/usr/bin/env python3
"""Daily market sentinel for QQQ/TQQQ teacher-style decision support.

This script is intentionally deterministic. It splits the workflow into
"agents" that can run unattended: price, news, mood, technical, leaders,
portfolio, and decision. The output is a timestamped snapshot plus an optional
Telegram alert.
"""
import argparse
import csv
import datetime as dt
import fcntl
import html
import json
import math
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

try:
    from scripts.artifact_io import (
        append_csv_row_private,
        atomic_write_csv,
        atomic_write_json,
        atomic_write_text,
        ensure_private_directory,
    )
    from scripts.dashboard_payload import read_dashboard_payload as _read_dashboard_payload
except ImportError:  # direct `python scripts/market_sentinel.py`
    from artifact_io import (
        append_csv_row_private,
        atomic_write_csv,
        atomic_write_json,
        atomic_write_text,
        ensure_private_directory,
    )
    from dashboard_payload import read_dashboard_payload as _read_dashboard_payload

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - Python 3.8 fallback
    ZoneInfo = None


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DASHBOARD = ROOT / "output" / "portfolio_dashboard.html"
DEFAULT_OUT = ROOT / "output" / "market_sentinel"
INTRADAY_OUT = ROOT / "output" / "intraday_qqq"
INTRADAY_TAPE_GATES = ROOT / "output" / "intraday_tape" / "gates.json"
INTRADAY_FRESH_MINUTES = 20
DAILY_QUOTE_MAX_AGE_WEEKDAYS = 2

PT = ZoneInfo("America/Los_Angeles") if ZoneInfo else dt.timezone(dt.timedelta(hours=-7))
ET_TZ = ZoneInfo("America/New_York") if ZoneInfo else dt.timezone(dt.timedelta(hours=-4))

INDEX_SYMBOLS = ["QQQ", "TQQQ", "^IXIC", "^NDX", "^VIX", "^TNX", "USO"]
LEADER_SYMBOLS = ["SMH", "NVDA", "AVGO", "MU", "AMD", "MRVL"]

NEWS_QUERIES = [
    (
        "Macro/Fed",
        "CPI OR PCE OR Fed OR FOMC OR payrolls OR jobs OR Treasury yields OR inflation",
    ),
    (
        "Oil/Risk",
        "oil prices OR crude OR Middle East OR risk off OR volatility OR VIX",
    ),
    (
        "AI/Semis",
        "Nasdaq OR QQQ OR semiconductors OR chips OR AI stocks OR Nvidia OR Broadcom",
    ),
]

KEYWORDS = {
    "macro": ("cpi", "pce", "inflation", "payroll", "jobs", "employment", "wage", "fed", "fomc", "rate", "yield"),
    "hawkish": ("hawkish", "hot", "strong jobs", "higher for longer", "rate hike", "yields rise", "sticky"),
    "dovish": ("cool", "weak", "soft", "rate cut", "cuts", "slows", "eases"),
    "oil_risk": ("oil", "crude", "middle east", "geopolitical", "risk-off", "risk off"),
    "ai_semis": ("semiconductor", "chip", "chips", "ai", "nvidia", "broadcom", "amd", "micron", "marvell"),
}

JUNE_PLAYBOOK = {
    "thesisDate": "2026-06-10",
    # This was a dated tactical note, not a permanent market regime.  Keeping an
    # explicit expiry prevents an old manual thesis from silently capping every
    # future decision at WATCH.
    "validThrough": "2026-06-30",
    "positioning": (
        "COT/manual note: leveraged funds were deeply net short S&P futures as of 2026-06-02; "
        "basis-trade caveat applies, but four-week short-leg speed argues active pressure."
    ),
    "supply": (
        "Manual June thesis: equity/IPO supply is a live amplifier; good-news rejection means "
        "distribution until tape proves otherwise."
    ),
    "events": [
        {
            "date": "2026-06-11",
            "timeET": "08:30",
            "name": "May PPI",
            "protocol": "Data score and tape score separate; if good data is rejected, distribution signal strengthens.",
        },
        {
            "date": "2026-06-11",
            "timeET": "13:00",
            "name": "30Y Treasury auction",
            "protocol": "Watch yields/TLT/DXY into 14:30 ET; equity break after strong auction still means supply digestion failed.",
        },
        {
            "date": "2026-06-12",
            "timeET": "10:00",
            "name": "UMich preliminary sentiment",
            "protocol": "Inflation expectations matter more than headline sentiment for Nasdaq multiple risk.",
        },
        {
            "date": "2026-06-16",
            "timeET": "09:30",
            "name": "FOMC meeting starts",
            "protocol": "Avoid adding short-dated directional risk into the event without defined max loss.",
        },
        {
            "date": "2026-06-17",
            "timeET": "14:00",
            "name": "FOMC + SEP/dot plot",
            "protocol": "Direction ticket; wait for first 15-30m tape confirmation after statement/press conference.",
        },
        {
            "date": "2026-06-18",
            "timeET": "16:00",
            "name": "June options/futures expiration pulled forward before Juneteenth",
            "protocol": "Gamma/roll flows can distort late-day levels; do not treat one expiry move as clean trend proof.",
        },
        {
            "date": "2026-06-19",
            "timeET": "09:30",
            "name": "NYSE/Nasdaq closed for Juneteenth",
            "protocol": "Holiday liquidity; avoid leaving short-gamma positions that depend on Friday management.",
        },
    ],
}


def _now_pt():
    return dt.datetime.now(tz=PT)


def acquire_lock(path):
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
    handle.write(f"pid={os.getpid()}\n")
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


def _parse_timestamp(value, default_tz=ET_TZ):
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=default_tz)
    except Exception:
        return None


def _timestamp_freshness(value, now=None, max_age_minutes=INTRADAY_FRESH_MINUTES):
    now = now or _now_pt()
    if now.tzinfo is None:
        now = now.replace(tzinfo=PT)
    timestamp = _parse_timestamp(value)
    if timestamp is None:
        return {"fresh": False, "ageMinutes": None, "reason": "timestamp_missing_or_invalid"}
    age_minutes = (now.astimezone(dt.timezone.utc) - timestamp.astimezone(dt.timezone.utc)).total_seconds() / 60.0
    same_session_date = timestamp.astimezone(ET_TZ).date() == now.astimezone(ET_TZ).date()
    fresh = same_session_date and -2.0 <= age_minutes <= max_age_minutes
    reason = None
    if not same_session_date:
        reason = "different_et_session_date"
    elif age_minutes < -2.0:
        reason = "timestamp_in_future"
    elif age_minutes > max_age_minutes:
        reason = "stale"
    return {"fresh": fresh, "ageMinutes": round(age_minutes, 1), "reason": reason}


def _weekday_age(start, end):
    """Weekdays after ``start`` through ``end``; negative means future data."""
    if start > end:
        return -1
    age = 0
    cursor = start + dt.timedelta(days=1)
    while cursor <= end:
        age += cursor.weekday() < 5
        cursor += dt.timedelta(days=1)
    return age


def daily_quote_freshness(quote, now=None, max_age_weekdays=DAILY_QUOTE_MAX_AGE_WEEKDAYS):
    """Validate a daily/current-session quote against the current ET session.

    Daily Yahoo rows do not carry a release timestamp, so the hard boundary is
    their market date. Current-session 1m quotes additionally validate their
    observation timestamp. A small weekday allowance covers weekends and US
    exchange holidays without allowing arbitrarily old aligned snapshots.
    """
    now = now or _now_pt()
    if now.tzinfo is None:
        now = now.replace(tzinfo=PT)
    now_et = now.astimezone(ET_TZ)
    raw_date = (quote or {}).get("date")
    try:
        market_date = dt.date.fromisoformat(str(raw_date)[:10])
    except (TypeError, ValueError):
        return {
            "fresh": False, "marketDate": None, "ageWeekdays": None,
            "reason": "quote_date_missing_or_invalid",
        }
    age = _weekday_age(market_date, now_et.date())
    reason = None
    fresh = True
    if market_date.weekday() >= 5:
        fresh, reason = False, "quote_date_not_weekday_session"
    elif age < 0:
        fresh, reason = False, "quote_date_in_future"
    elif age > max_age_weekdays:
        fresh, reason = False, "quote_stale"

    observed_at = (quote or {}).get("observedAt")
    observation = None
    if observed_at:
        observation = _timestamp_freshness(observed_at, now=now, max_age_minutes=30)
        if not observation.get("fresh"):
            fresh = False
            reason = "intraday_observation_" + str(observation.get("reason") or "stale")
    return {
        "fresh": fresh,
        "marketDate": market_date.isoformat(),
        "ageWeekdays": age,
        "reason": reason,
        "observedAt": observed_at,
        "observationFreshness": observation,
    }


def _closed_bar_freshness(value, interval_minutes, now=None):
    """Freshness measured from a candle's close, not its opening timestamp."""
    now = now or _now_pt()
    if now.tzinfo is None:
        now = now.replace(tzinfo=PT)
    started = _parse_timestamp(value)
    if started is None:
        return {"fresh": False, "ageMinutes": None, "reason": "timestamp_missing_or_invalid"}
    started = started.astimezone(ET_TZ)
    ended = started + dt.timedelta(minutes=interval_minutes)
    age_minutes = (
        now.astimezone(dt.timezone.utc) - ended.astimezone(dt.timezone.utc)
    ).total_seconds() / 60.0
    same_session_date = started.date() == now.astimezone(ET_TZ).date()
    max_age = interval_minutes + 5
    fresh = same_session_date and -2.0 <= age_minutes <= max_age
    if not same_session_date:
        reason = "different_et_session_date"
    elif age_minutes < -2.0:
        reason = "bar_end_in_future"
    elif age_minutes > max_age:
        reason = "bar_stale"
    else:
        reason = None
    return {
        "fresh": fresh,
        "ageMinutes": round(age_minutes, 1),
        "reason": reason,
        "barStart": started.isoformat(timespec="minutes"),
        "barEnd": ended.isoformat(timespec="minutes"),
        "maxAgeMinutes": max_age,
    }


def read_intraday_tape_gates(path=INTRADAY_TAPE_GATES, now=None, max_age_minutes=INTRADAY_FRESH_MINUTES):
    """Read deterministic hard gates, activating them only while fresh."""
    path = Path(path)
    if not path.exists():
        return {"available": False, "fresh": False, "path": str(path), "reason": "file_missing"}
    try:
        def reject_constant(value):
            raise ValueError(f"non-finite JSON constant {value}")
        raw = json.loads(path.read_text(encoding="utf-8"), parse_constant=reject_constant)
    except Exception as exc:
        return {
            "available": False,
            "fresh": False,
            "path": str(path),
            "reason": f"invalid_json: {type(exc).__name__}",
        }
    if not isinstance(raw, dict) or raw.get("schemaVersion") != 1:
        return {
            "available": False, "fresh": False, "path": str(path),
            "reason": "schema_mismatch",
        }
    if not isinstance(raw.get("run_id"), str) or not raw["run_id"].strip():
        return {
            "available": False, "fresh": False, "path": str(path),
            "reason": "run_id_missing",
        }
    if not isinstance(raw.get("prohibit_allow"), bool):
        return {
            "available": False, "fresh": False, "path": str(path),
            "reason": "prohibit_allow_not_boolean",
        }
    if not isinstance(raw.get("action_lock"), list) or not all(
        isinstance(item, str) for item in raw["action_lock"]
    ):
        return {
            "available": False, "fresh": False, "path": str(path),
            "reason": "action_lock_schema_invalid",
        }
    if not isinstance(raw.get("triggered"), list) or not all(
        isinstance(item, dict) for item in raw["triggered"]
    ):
        return {
            "available": False, "fresh": False, "path": str(path),
            "reason": "triggered_schema_invalid",
        }
    if raw.get("score_cap") not in (None, "观察", "WATCH", "BLOCK_DATA"):
        return {
            "available": False, "fresh": False, "path": str(path),
            "reason": "score_cap_invalid",
        }
    if raw.get("llm_may_not_override") is not True:
        return {
            "available": False, "fresh": False, "path": str(path),
            "reason": "binding_flag_missing",
        }
    try:
        generated = dt.datetime.fromisoformat(str(raw.get("generated_at")).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        generated = None
    if generated is None or generated.tzinfo is None:
        return {
            "available": False, "fresh": False, "path": str(path),
            "reason": "generated_at_timezone_missing_or_invalid",
        }
    freshness = _timestamp_freshness(raw.get("generated_at"), now=now, max_age_minutes=max_age_minutes)
    return {
        "available": True,
        "fresh": freshness["fresh"],
        "path": str(path),
        "generatedAt": raw.get("generated_at"),
        "runId": raw.get("run_id"),
        "ageMinutes": freshness["ageMinutes"],
        "reason": freshness["reason"],
        "prohibitAllow": bool(raw.get("prohibit_allow")),
        "actionLocks": list(raw.get("action_lock") or []),
        "scoreCap": raw.get("score_cap"),
        "triggered": list(raw.get("triggered") or []),
        "llmMayNotOverride": bool(raw.get("llm_may_not_override")),
    }


def intraday_snapshot_status(snapshot, now=None, max_age_minutes=INTRADAY_FRESH_MINUTES):
    snapshot = snapshot or {}
    signal = snapshot.get("signal") or {}
    summaries = signal.get("summaries") or {}
    five = summaries.get("5m") or {}
    fifteen = summaries.get("15m") or {}
    has_signal = signal.get("label") in ("ALLOW", "WATCH", "BLOCK")
    has_price = _to_float(signal.get("last")) is not None
    has_required_bars = bool(five.get("available") and fifteen.get("available"))
    freshness = _timestamp_freshness(snapshot.get("pulledAt"), now=now, max_age_minutes=max_age_minutes)
    bar_checks = {}
    for label, summary, interval_minutes in (("5m", five, 5), ("15m", fifteen, 15)):
        # A freshly written snapshot can still contain Friday's bars on a Monday
        # holiday or after a provider/cache failure.  Validate the evidence time,
        # not merely the file-write time.
        bar_checks[label] = _closed_bar_freshness(
            summary.get("lastTime"), interval_minutes, now=now,
        )
    bars_fresh = has_required_bars and all(item.get("fresh") for item in bar_checks.values())
    available = bool(has_signal and has_price and has_required_bars)
    reason = freshness.get("reason")
    if not available:
        reason = "missing_signal_price_or_5m_15m_bars"
    elif not bars_fresh:
        stale_labels = [label for label, item in bar_checks.items() if not item.get("fresh")]
        reason = "stale_or_wrong_session_bars:" + ",".join(stale_labels)
    return {
        "available": available,
        "fresh": bool(available and freshness.get("fresh") and bars_fresh),
        "pulledAt": snapshot.get("pulledAt"),
        "ageMinutes": freshness.get("ageMinutes"),
        "bars": bar_checks,
        "reason": reason,
    }


def _to_float(value):
    try:
        if value is None:
            return None
        converted = float(value)
        return converted if math.isfinite(converted) else None
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


def _fmt(value, digits=2, suffix=""):
    value = _to_float(value)
    if value is None:
        return "—"
    return f"{value:.{digits}f}{suffix}"


def _signed(value, digits=2, suffix="%"):
    value = _to_float(value)
    if value is None:
        return "—"
    return f"{value:+.{digits}f}{suffix}"


def read_dashboard_payload(path=DEFAULT_DASHBOARD):
    return _read_dashboard_payload(path)


def run_price_refresh(input_dir, no_fetch=False):
    cmd = [
        sys.executable,
        str(ROOT / "scripts" / "refresh_latest_prices.py"),
        "--input-dir",
        input_dir,
        "--trigger",
        "market-sentinel",
    ]
    if no_fetch:
        cmd.append("--no-fetch")
    proc = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True)
    return {
        "cmd": cmd,
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdoutTail": proc.stdout[-1200:],
        "stderrTail": proc.stderr[-1200:],
    }


def fetch_symbol_snapshot(symbol):
    import warnings
    warnings.filterwarnings("ignore")
    import yfinance as yf

    hist = yf.Ticker(symbol).history(period="10d", interval="1d", auto_adjust=False)
    if hist is None or hist.empty:
        return {"symbol": symbol, "available": False, "reason": "no daily history"}

    closes = hist["Close"].dropna()
    if closes.empty:
        return {"symbol": symbol, "available": False, "reason": "no close prices"}

    last = closes.iloc[-1]
    prev = closes.iloc[-2] if len(closes) >= 2 else None
    base5 = closes.iloc[-6] if len(closes) >= 6 else None
    idx = closes.index[-1]
    date = idx.date().isoformat() if hasattr(idx, "date") else str(idx)[:10]

    # Yahoo's daily endpoint can lag after the close. Prefer the latest regular-session
    # intraday mark when it is newer than, or equal to, the latest daily row.
    try:
        intra = yf.Ticker(symbol).history(period="1d", interval="1m", auto_adjust=False, prepost=False)
        if intra is not None and not intra.empty and "Close" in intra:
            intra_close = intra["Close"].dropna()
            if not intra_close.empty:
                intra_last = _to_float(intra_close.iloc[-1])
                intra_idx = intra_close.index[-1]
                intra_date = intra_idx.date().isoformat() if hasattr(intra_idx, "date") else str(intra_idx)[:10]
                if intra_last is not None and intra_date >= date:
                    prev_for_ret = last if intra_date > date else prev
                    base_for_ret5 = closes.iloc[-5] if intra_date > date and len(closes) >= 5 else base5
                    return {
                        "symbol": symbol,
                        "available": True,
                        "date": intra_date,
                        "observedAt": intra_idx.isoformat() if hasattr(intra_idx, "isoformat") else str(intra_idx),
                        "last": _round(intra_last),
                        "ret1": _round(_pct(intra_last, prev_for_ret), 2) if prev_for_ret is not None else None,
                        "ret5": _round(_pct(intra_last, base_for_ret5), 2) if base_for_ret5 is not None else None,
                        "source": "Yahoo 1m regular-session",
                    }
    except Exception:
        pass

    return {
        "symbol": symbol,
        "available": True,
        "date": date,
        "last": _round(last),
        "ret1": _round(_pct(last, prev), 2) if prev is not None else None,
        "ret5": _round(_pct(last, base5), 2) if base5 is not None else None,
        "source": "Yahoo daily",
    }


def price_agent(symbols=None):
    symbols = symbols or list(dict.fromkeys(INDEX_SYMBOLS + LEADER_SYMBOLS))
    quotes = {}
    errors = {}
    for symbol in symbols:
        try:
            snap = fetch_symbol_snapshot(symbol)
            quotes[symbol] = snap
        except Exception as exc:
            errors[symbol] = f"{type(exc).__name__}: {exc}"
            quotes[symbol] = {"symbol": symbol, "available": False, "reason": errors[symbol]}
    return {"name": "price_agent", "quotes": quotes, "errors": errors}


def google_news_rss_url(query, days=2):
    q = f"({query}) when:{days}d"
    return "https://news.google.com/rss/search?" + urllib.parse.urlencode({
        "q": q,
        "hl": "en-US",
        "gl": "US",
        "ceid": "US:en",
    })


def fetch_rss_items(url, label, limit=8, timeout=10):
    req = urllib.request.Request(url, headers={"User-Agent": "portfolio-tracker-market-sentinel/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    root = ET.fromstring(raw)
    items = []
    for item in root.findall(".//item")[:limit]:
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        source = (item.findtext("source") or label).strip()
        pub = (item.findtext("pubDate") or "").strip()
        if title:
            items.append({"label": label, "title": title, "source": source, "published": pub, "link": link})
    return items


def news_agent(max_per_query=5, days=2):
    items = []
    errors = {}
    for label, query in NEWS_QUERIES:
        url = google_news_rss_url(query, days=days)
        try:
            items.extend(fetch_rss_items(url, label, limit=max_per_query))
        except Exception as exc:
            errors[label] = f"{type(exc).__name__}: {exc}"

    seen = set()
    unique = []
    for item in items:
        key = re.sub(r"\s+", " ", item["title"].lower())
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)

    counts = {k: 0 for k in KEYWORDS}
    for item in unique:
        text = item["title"].lower()
        for bucket, words in KEYWORDS.items():
            if any(w in text for w in words):
                counts[bucket] += 1
    return {
        "name": "news_agent",
        "lookbackDays": days,
        "headlineCount": len(unique),
        "keywordCounts": counts,
        "headlines": unique[:12],
        "errors": errors,
    }


def event_datetime_pt(event):
    raw = f"{event['date']}T{event.get('timeET', '09:30')}:00"
    return dt.datetime.fromisoformat(raw).replace(tzinfo=ET_TZ).astimezone(PT)


def june_playbook_agent(now=None):
    now = now or _now_pt()
    today = now.astimezone(PT).date()
    thesis_date = dt.date.fromisoformat(JUNE_PLAYBOOK["thesisDate"])
    valid_through = dt.date.fromisoformat(JUNE_PLAYBOOK["validThrough"])
    playbook_active = thesis_date <= today <= valid_through
    upcoming = []
    active = []
    for event in JUNE_PLAYBOOK["events"] if playbook_active else []:
        event_pt = event_datetime_pt(event)
        delta_hours = (event_pt - now).total_seconds() / 3600.0
        row = {**event, "timePT": event_pt.isoformat(timespec="minutes"), "hoursAway": round(delta_hours, 1)}
        if -1.0 <= delta_hours <= 24.0:
            active.append(row)
        if delta_hours >= -1.0:
            upcoming.append(row)
    upcoming.sort(key=lambda x: x["hoursAway"])
    active.sort(key=lambda x: x["hoursAway"])
    return {
        "name": "june_playbook_agent",
        "thesisDate": JUNE_PLAYBOOK["thesisDate"],
        "validThrough": JUNE_PLAYBOOK["validThrough"],
        "active": playbook_active,
        "expired": today > valid_through,
        "status": "active" if playbook_active else ("expired" if today > valid_through else "not_started"),
        "positioning": JUNE_PLAYBOOK["positioning"],
        "supply": JUNE_PLAYBOOK["supply"],
        "activeEvents": active[:3],
        "nextEvents": upcoming[:5],
        "slowState": "trend_down_but_short_crowded" if playbook_active else None,
        "spreadBias": (
            "Primary discipline: no bottom call from one bounce. Prefer defined-risk spreads; "
            "call debit only after reclaim, put debit only after failed bounce, CCS only as long-exposure hedge."
        ) if playbook_active else None,
    }


def refresh_intraday_agent(refresh=True):
    if refresh:
        sys.path.insert(0, str(ROOT / "scripts"))
        try:
            import monitor_qqq_intraday
            snap = monitor_qqq_intraday.run_once("QQQ", INTRADAY_OUT)
            return {"name": "intraday_agent", "ok": True, "snapshot": snap, "source": "fresh"}
        except Exception as exc:
            return {"name": "intraday_agent", "ok": False, "error": f"{type(exc).__name__}: {exc}", "snapshot": read_latest_intraday()}
    return {"name": "intraday_agent", "ok": True, "snapshot": read_latest_intraday(), "source": "cached"}


def read_latest_intraday():
    path = INTRADAY_OUT / "latest_signal.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def portfolio_agent(payload):
    summary = payload.get("summary", {})
    alloc = payload.get("alloc", {})
    risk = payload.get("risk", {})
    q = payload.get("qqqTqqq", {})
    largest_theme = alloc.get("largestTheme") or (alloc.get("byTheme") or [{}])[0]
    risk_contrib = risk.get("contrib") or []
    return {
        "name": "portfolio_agent",
        "summary": {
            "priceAsOf": summary.get("priceAsOf"),
            "generatedAt": summary.get("generatedAt"),
            "accountNetWorth": summary.get("accountNetWorth"),
            "marketValue": summary.get("marketValue"),
            "cashTotal": summary.get("cashTotal"),
            "optPctEquity": summary.get("optPctEquity"),
        },
        "largestTheme": largest_theme,
        "topRisk": risk_contrib[:6],
        "qqqHolding": (q.get("holdings") or {}).get("QQQ"),
        "tqqqHolding": (q.get("holdings") or {}).get("TQQQ"),
        "optionSpreads": q.get("optionSpreads") or [],
    }


def technical_agent(payload, intraday_snapshot, intraday_hard_gates=None, now=None):
    q = payload.get("qqqTqqq", {})
    latest = q.get("latest") or {}
    daily_state = q.get("state") or {}
    intraday_signal = ((intraday_snapshot or {}).get("signal") or {})
    intraday_status = intraday_snapshot_status(intraday_snapshot, now=now)
    hard_gates = intraday_hard_gates or {
        "available": False,
        "fresh": False,
        "reason": "not_loaded",
    }
    summaries = intraday_signal.get("summaries") or {}
    five = summaries.get("5m") or {}
    fifteen = summaries.get("15m") or {}

    intraday_close = _to_float(intraday_signal.get("last")) if intraday_status.get("fresh") else None
    close = intraday_close if intraday_close is not None else _to_float(latest.get("qqq"))
    ema21 = _to_float(latest.get("ema21"))
    ema8 = _to_float(latest.get("ema8"))
    ema34 = _to_float(latest.get("ema34"))
    atr = _to_float(latest.get("atr14"))
    daily_context_complete = all(value is not None and value > 0 for value in (close, ema8, ema21, ema34, atr))

    below_ema21 = close is not None and ema21 is not None and close < ema21
    below_ema34 = close is not None and ema34 is not None and close < ema34
    ema8_under_ema21 = ema8 is not None and ema21 is not None and ema8 < ema21
    overheat = False
    if close is not None and ema8 is not None and atr:
        overheat = close > ema8 + 1.5 * atr

    if below_ema34:
        regime = "trend_break_ema34_test"
    elif below_ema21 or ema8_under_ema21:
        regime = "below_ema21_watch"
    elif overheat:
        regime = "overheat"
    elif close is not None and ema8 is not None and atr and abs(close - ema8) <= 0.5 * atr:
        regime = "ema8_pullback"
    elif close is not None and ema21 is not None and atr and abs(close - ema21) <= 0.5 * atr:
        regime = "ema21_pullback"
    else:
        regime = "trend_or_mixed"

    return {
        "name": "technical_agent",
        "regime": regime,
        "dashboardState": daily_state,
        "latest": latest,
        "currentPrice": close,
        "currentPriceSource": "fresh_closed_intraday" if intraday_close is not None else "dashboard_daily_context",
        "intradayLabel": intraday_signal.get("label"),
        "intradayScore": intraday_signal.get("score"),
        "intradayMaxScore": intraday_signal.get("maxScore"),
        "intradayData": intraday_status,
        "intradayHardGates": hard_gates,
        "fiveMinute": {
            "close": five.get("close"),
            "vwap": five.get("vwap"),
            "aboveVwap": five.get("aboveVwap"),
            "ema21": five.get("ema21"),
        },
        "fifteenMinute": {
            "close": fifteen.get("close"),
            "ema8": fifteen.get("ema8"),
            "ema8Slope45m": fifteen.get("ema8Slope45m"),
        },
        "flags": {
            "belowEma21": below_ema21,
            "belowEma34": below_ema34,
            "ema8UnderEma21": ema8_under_ema21,
            "overheat": overheat,
            "twoBelowEma21": bool(latest.get("twoBelowEma21")),
            "dailyContextComplete": daily_context_complete,
        },
    }


def leaders_agent(quotes, now=None):
    leaders = []
    rejected = {}
    for symbol in LEADER_SYMBOLS:
        q = quotes.get(symbol) or {}
        freshness = daily_quote_freshness(q, now=now)
        if q.get("available") and freshness.get("fresh"):
            leaders.append({
                "symbol": symbol, "last": q.get("last"), "ret1": q.get("ret1"),
                "ret5": q.get("ret5"), "date": q.get("date"),
            })
        else:
            rejected[symbol] = freshness.get("reason") or q.get("reason") or "quote_unavailable"
    weak = [x for x in leaders if _to_float(x.get("ret1")) is not None and x["ret1"] <= -2.5]
    strong = [x for x in leaders if _to_float(x.get("ret1")) is not None and x["ret1"] >= 1.0]
    qqq_quote = quotes.get("QQQ") or {}
    smh_quote = quotes.get("SMH") or {}
    qqq_ret = _to_float(qqq_quote.get("ret1")) if daily_quote_freshness(qqq_quote, now=now).get("fresh") else None
    smh_ret = _to_float(smh_quote.get("ret1")) if daily_quote_freshness(smh_quote, now=now).get("fresh") else None
    smh_underperforms = qqq_ret is not None and smh_ret is not None and smh_ret < qqq_ret - 1.5
    coverage_pass = len(leaders) >= math.ceil(len(LEADER_SYMBOLS) * 0.8)
    if not coverage_pass:
        state = "data_block"
    elif len(weak) >= 3 or smh_underperforms:
        state = "leader_break"
    elif len(strong) >= 3 and not smh_underperforms:
        state = "leader_repair"
    else:
        state = "mixed"
    return {
        "name": "leaders_agent",
        "state": state,
        "leaders": leaders,
        "weakCount": len(weak),
        "strongCount": len(strong),
        "smhUnderperformsQqq": smh_underperforms,
        "dataStatus": "PASS" if coverage_pass else "BLOCK",
        "freshCount": len(leaders),
        "requiredFreshCount": math.ceil(len(LEADER_SYMBOLS) * 0.8),
        "rejectedQuotes": rejected,
    }


def mood_agent(quotes, news, now=None):
    required_symbols = ("QQQ", "^VIX", "^TNX")
    freshness = {
        symbol: daily_quote_freshness(quotes.get(symbol) or {}, now=now)
        for symbol in (*required_symbols, "USO")
    }
    usable = {
        symbol: (quotes.get(symbol) or {}) if freshness[symbol].get("fresh") else {}
        for symbol in freshness
    }
    qqq, vix, tnx, uso = (usable["QQQ"], usable["^VIX"], usable["^TNX"], usable["USO"])
    qqq_ret = _to_float(qqq.get("ret1"))
    vix_ret = _to_float(vix.get("ret1"))
    tnx_ret = _to_float(tnx.get("ret1"))
    uso_ret = _to_float(uso.get("ret1"))
    counts = news.get("keywordCounts") or {}

    rate_pressure = (tnx_ret is not None and tnx_ret > 0.7) or counts.get("hawkish", 0) >= 2
    vol_shock = (vix_ret is not None and vix_ret > 12.0)
    oil_risk = (uso_ret is not None and uso_ret > 2.0) or counts.get("oil_risk", 0) >= 2
    semis_news = counts.get("ai_semis", 0) >= 3

    data_pass = all(freshness[symbol].get("fresh") for symbol in required_symbols)
    if not data_pass:
        mood = "data_insufficient"
        reason = "QQQ/VIX/TNX quote dates are missing, stale, or future-dated; price-based mood is disabled."
    elif qqq_ret is not None and qqq_ret < -1.0 and rate_pressure:
        mood = "good_news_is_bad_news"
        reason = "QQQ 下跌同时利率/鹰派关键词偏强，市场在按更高贴现率交易。"
    elif qqq_ret is not None and qqq_ret < -1.0 and vol_shock:
        mood = "bad_news_is_bad_news"
        reason = "VIX 快速上行且 QQQ 下跌，风险控制比抄底更重要。"
    elif qqq_ret is not None and qqq_ret > 0.7 and not rate_pressure:
        mood = "good_news_is_good_news"
        reason = "指数上涨且利率压力不明显，风险偏好在修复。"
    elif qqq_ret is not None and qqq_ret > 0 and counts.get("dovish", 0) >= 2:
        mood = "bad_news_is_good_news"
        reason = "弱数据/降息叙事支持反弹，但仍需看技术重心。"
    else:
        mood = "mixed"
        reason = "新闻和价格信号混杂，等待 QQQ 重心和 leader 确认。"

    return {
        "name": "mood_agent",
        "mood": mood,
        "reason": reason,
        "flags": {
            "ratePressure": rate_pressure,
            "volShock": vol_shock,
            "oilRisk": oil_risk,
            "semisNewsHeavy": semis_news,
        },
        "inputs": {
            "qqqRet1": qqq_ret,
            "vixRet1": vix_ret,
            "tnxRet1": tnx_ret,
            "usoRet1": uso_ret,
            "newsKeywordCounts": counts,
        },
        "dataStatus": "PASS" if data_pass else "BLOCK",
        "quoteFreshness": freshness,
    }


def compute_levels(technical, quotes):
    latest = technical.get("latest") or {}
    qqq = quotes.get("QQQ") or {}
    tqqq = quotes.get("TQQQ") or {}
    close = None
    if technical.get("currentPriceSource") == "fresh_closed_intraday":
        close = _to_float(technical.get("currentPrice"))
    if close is None:
        close = _to_float(qqq.get("last"))
    if close is None:
        close = _to_float(latest.get("qqq"))
    current = close
    ema8 = _to_float(latest.get("ema8"))
    ema21 = _to_float(latest.get("ema21"))
    ema34 = _to_float(latest.get("ema34"))
    atr = _to_float(latest.get("atr14")) or 0
    tqqq_spot = _to_float(tqqq.get("last"))
    if tqqq_spot is None:
        tqqq_spot = _to_float(latest.get("tqqq"))

    reclaim_stop = None
    reclaim_limit = None
    if ema21 and atr:
        base = ema21 + 0.20 * atr
        if current is not None and current > ema21:
            base = max(base, current + 0.10 * atr)
        reclaim_stop = _round(base)
        reclaim_limit = _round(base + 0.10 * atr)

    ema21_zone = None
    if ema21 and atr:
        ema21_zone = [_round(ema21 - 0.5 * atr), _round(ema21 + 0.5 * atr)]
    ema8_zone = None
    if ema8 and atr:
        ema8_zone = [_round(ema8 - 0.5 * atr), _round(ema8 + 0.5 * atr)]

    invalidation = None
    if ema34 and atr:
        invalidation = _round(ema34 - 0.20 * atr)
    moving_take_profit = None
    if current and atr:
        moving_take_profit = _round(current - 3.0 * atr)

    tqqq_ccs = None
    if tqqq_spot:
        tqqq_ccs = {
            "spot": _round(tqqq_spot),
            "underlyingPressureZone": [_round(tqqq_spot * 1.03), _round(tqqq_spot * 1.06)],
            "strikeSelectionStatus": "requires_live_option_chain",
            "note": "这是 TQQQ 标的价格压力区，不是可执行期权行权价。只有 QQQ 重新过热/冲高停顿，并由实时期权链验证 delta、买卖价差、credit 与 max loss 后才可选 strike。",
        }

    return {
        "reclaimBuyStop": reclaim_stop,
        "reclaimLimit": reclaim_limit,
        "ema8Zone": ema8_zone,
        "ema21Zone": ema21_zone,
        "ema34": _round(ema34),
        "invalidationClose": invalidation,
        "current3AtrReference": moving_take_profit,
        "current3AtrReferenceIsRatcheted": False,
        "tqqqCcsGuide": tqqq_ccs,
    }


def decision_agent(technical, leaders, mood, portfolio, quotes, playbook=None):
    flags = technical.get("flags") or {}
    mood_flags = mood.get("flags") or {}
    intraday_label = technical.get("intradayLabel")
    intraday_data = technical.get("intradayData") or {}
    hard_gates = technical.get("intradayHardGates") or {}
    leader_state = leaders.get("state")
    playbook = playbook or {}

    blockers = []
    watch = []
    allow = []

    if flags.get("belowEma21"):
        blockers.append("QQQ 在 EMA21 下方，老师框架下不加 TQQQ/Call。")
    if flags.get("belowEma34"):
        blockers.append("QQQ 已触及/跌破 EMA34 区域，不能当普通 EMA8 回踩。")
    if flags.get("twoBelowEma21") or flags.get("ema8UnderEma21"):
        blockers.append("趋势结构开始破坏，需要先等重新站回。")
    if intraday_label == "BLOCK":
        blockers.append("日内重心 BLOCK，5m/15m/30m 未修复。")
    if intraday_label == "WATCH":
        watch.append("日内重心仅为 WATCH，上层决策不得提升为 ALLOW。")
    if not flags.get("dailyContextComplete"):
        watch.append("完成日线 QQQ/EMA8/21/34/ATR 天气上下文不完整，本轮禁用 ALLOW。")
    if not intraday_data.get("available") or not intraday_data.get("fresh"):
        detail = intraday_data.get("reason") or "unavailable"
        watch.append(f"日内数据缺失或过期（{detail}），本轮禁用 ALLOW。")
    if hard_gates.get("fresh") and hard_gates.get("prohibitAllow"):
        gate_names = "/".join(
            str(item.get("gate")) for item in hard_gates.get("triggered") or [] if item.get("gate")
        ) or "hard gate"
        watch.append(f"日内硬门禁 {gate_names} 设置 prohibit_allow，本轮最高只能 WATCH。")
    if hard_gates.get("fresh") and hard_gates.get("actionLocks"):
        locks = " / ".join(str(item) for item in hard_gates.get("actionLocks") or [])
        blockers.append(f"日内硬门禁锁定操作：{locks}。")
    if not hard_gates.get("available") or not hard_gates.get("fresh"):
        detail = hard_gates.get("reason") or "unavailable"
        watch.append(f"日内硬门禁数据缺失或过期（{detail}），本轮禁用 ALLOW。")
    if leader_state == "leader_break":
        blockers.append("SMH/NVDA/AVGO/MU/AMD/MRVL 仍偏破位，leader 没有带头修复。")
    if leaders.get("dataStatus") != "PASS":
        watch.append("Leader 报价缺失、过期或来自未来日期，本轮禁用 ALLOW。")
    if mood.get("dataStatus") != "PASS":
        watch.append("QQQ/VIX/TNX 情绪报价缺失、过期或来自未来日期，本轮禁用 ALLOW。")
    if mood_flags.get("volShock"):
        watch.append("VIX/波动率冲击偏强，short premium 仓位只允许小且定义风险。")
    if mood_flags.get("ratePressure"):
        watch.append("利率/Fed 压力仍在，反弹可能先被估值压制。")
    largest_theme_risk = (portfolio.get("largestTheme") or {}).get("riskPct")
    if largest_theme_risk is not None and largest_theme_risk >= 50:
        watch.append("组合半导体/AI 风险集中，任何加仓都要按总 beta 算。")
    if playbook.get("active") and playbook.get("slowState") == "trend_down_but_short_crowded":
        watch.append("六月慢变量：趋势破位但空头拥挤，追低卖put/call都容易被逼空或gamma反打。")
    if playbook.get("active") and playbook.get("activeEvents"):
        names = " / ".join(e.get("name", "") for e in playbook["activeEvents"][:2])
        watch.append(f"事件窗口：{names}；事件前后优先收窄风险，等tape卷确认。")

    intraday_usable = bool(intraday_data.get("available") and intraday_data.get("fresh"))
    if not blockers and intraday_usable and leader_state == "leader_repair" and intraday_label == "ALLOW" and flags.get("dailyContextComplete"):
        allow.append("可观察小仓分批：必须用 buy-stop/reclaim + 明确止损。")
    elif not blockers:
        watch.append("可观察但不追：等 QQQ EMA21/EMA8 与 leader 同步修复。")

    if blockers:
        label = "BLOCK"
        primary = "停止新增 TQQQ/Call；先管理已有仓和定义风险 spread，等 reclaim。"
    elif watch:
        label = "WATCH"
        primary = "等待下一根 15m/30m 和日线 EMA21 确认；WATCH 不是入场许可，只管理已有风险或等待明确触发。"
    else:
        label = "ALLOW"
        primary = "允许小仓按计划进入，但必须移动止盈，不追高。"

    levels = compute_levels(technical, quotes)
    ccs = levels.get("tqqqCcsGuide")
    if ccs:
        def has_long(holding):
            holding = holding or {}
            return any(
                (_to_float(holding.get(key)) or 0) > 0
                for key in ("value", "marketValue", "shares", "quantity", "qty")
            )

        verified_long = has_long(portfolio.get("qqqHolding")) or has_long(portfolio.get("tqqqHolding"))
        ccs["verifiedLongExposure"] = verified_long
        ccs["role"] = "hedge" if verified_long else "directional_short_premium_not_hedge"
        if not verified_long:
            ccs["note"] = "未验证 QQQ/TQQQ 多头暴露；该 CCS 不得称为 hedge，只能视为方向性 short-premium 候选。"
    return {
        "name": "decision_agent",
        "label": label,
        "primaryAction": primary,
        "blockers": blockers,
        "watchItems": watch,
        "allowItems": allow,
        "levels": levels,
        "teacherRead": f"{technical.get('regime')} -> {label}: {primary}",
    }


def spmo_momentum_agent(payload, technical, decision):
    """Attach the SPMO momentum sleeve as a portfolio-level sub-signal."""
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import spmo_momentum_sleeve

        result = spmo_momentum_sleeve.run_once(
            payload=payload,
            technical=technical,
            decision=decision,
        )
        spmo_momentum_sleeve.write_outputs(result)
        result["name"] = "spmo_momentum_agent"
        return result
    except Exception as exc:
        return {
            "name": "spmo_momentum_agent",
            "available": False,
            "label": "BLOCK_DATA",
            "reason": f"{type(exc).__name__}: {exc}",
        }


def build_snapshot(args):
    now = _now_pt()
    refresh = None
    if args.refresh_dashboard:
        refresh = run_price_refresh(args.input_dir, args.no_fetch)
        if not refresh["ok"]:
            raise RuntimeError(f"price refresh failed: {refresh['stderrTail'] or refresh['stdoutTail']}")

    payload = read_dashboard_payload(args.dashboard)
    prices = price_agent()
    news = {"name": "news_agent", "headlineCount": 0, "keywordCounts": {}, "headlines": [], "errors": {}}
    if not args.no_news:
        news = news_agent(max_per_query=args.max_headlines_per_query, days=args.news_days)
    intraday = refresh_intraday_agent(refresh=args.refresh_intraday)
    hard_gates = read_intraday_tape_gates(getattr(args, "intraday_gates", INTRADAY_TAPE_GATES))
    portfolio = portfolio_agent(payload)
    technical = technical_agent(payload, intraday.get("snapshot"), intraday_hard_gates=hard_gates, now=now)
    leaders = leaders_agent(prices["quotes"], now=now)
    mood = mood_agent(prices["quotes"], news, now=now)
    playbook = june_playbook_agent(now=now)
    decision = decision_agent(technical, leaders, mood, portfolio, prices["quotes"], playbook=playbook)
    spmo = spmo_momentum_agent(payload, technical, decision)

    return {
        "ranAt": now.isoformat(timespec="seconds"),
        "dataFreshness": {
            "dashboardPriceAsOf": portfolio["summary"].get("priceAsOf"),
            "dashboardGeneratedAt": portfolio["summary"].get("generatedAt"),
            "priceSource": "Yahoo Finance via yfinance",
            "newsSource": "Google News RSS queries",
            "intradaySource": intraday.get("source") or "fresh-attempt",
            "intradayTapeGates": {
                "generatedAt": hard_gates.get("generatedAt"),
                "ageMinutes": hard_gates.get("ageMinutes"),
                "fresh": hard_gates.get("fresh"),
            },
        },
        "refresh": refresh,
        "agents": {
            "price": prices,
            "news": news,
            "intraday": intraday,
            "intradayHardGates": hard_gates,
            "portfolio": portfolio,
            "technical": technical,
            "leaders": leaders,
            "mood": mood,
            "junePlaybook": playbook,
            "decision": decision,
            "spmoMomentum": spmo,
        },
    }


def format_leaders(leaders):
    rows = []
    for item in leaders.get("leaders") or []:
        rows.append(f"{item['symbol']} {_signed(item.get('ret1'))}")
    return ", ".join(rows) or "—"


def format_headlines(news, limit=4):
    out = []
    for item in (news.get("headlines") or [])[:limit]:
        title = item.get("title", "")
        source = item.get("source") or item.get("label") or "news"
        out.append(f"{title} ({source})")
    return out


def format_telegram_message(snapshot):
    a = snapshot["agents"]
    decision = a["decision"]
    technical = a["technical"]
    mood = a["mood"]
    portfolio = a["portfolio"]
    prices = a["price"]["quotes"]
    leaders = a["leaders"]
    news = a["news"]
    spmo = a.get("spmoMomentum") or {}
    playbook = a.get("junePlaybook") or {}
    levels = decision["levels"]
    latest = technical.get("latest") or {}
    intraday_data = technical.get("intradayData") or {}
    hard_gates = technical.get("intradayHardGates") or {}

    label_icon = {"ALLOW": "🟢", "WATCH": "🟡", "BLOCK": "🔴"}.get(decision["label"], "⚪")
    qqq = prices.get("QQQ") or {}
    tqqq = prices.get("TQQQ") or {}
    vix = prices.get("^VIX") or {}
    tnx = prices.get("^TNX") or {}
    theme = portfolio.get("largestTheme") or {}

    lines = [
        f"{label_icon} <b>Market Sentinel · {html.escape(decision['label'])}</b>",
        f"<code>{html.escape(snapshot['ranAt'])}</code>",
        "",
        f"<b>QQQ</b> <code>{_fmt(qqq.get('last'))}</code> {_signed(qqq.get('ret1'))} · "
        f"<b>TQQQ</b> <code>{_fmt(tqqq.get('last'))}</code> {_signed(tqqq.get('ret1'))}",
        f"EMA: 8 <code>{_fmt(latest.get('ema8'))}</code> / 21 <code>{_fmt(latest.get('ema21'))}</code> / 34 <code>{_fmt(latest.get('ema34'))}</code> · ATR <code>{_fmt(latest.get('atr14'))}</code>",
        f"重心: <b>{html.escape(str(technical.get('intradayLabel') or '—'))}</b> score "
        f"<code>{technical.get('intradayScore')}/{technical.get('intradayMaxScore')}</code> · regime <code>{html.escape(str(technical.get('regime')))}</code>",
        f"Tape freshness: <code>{'fresh' if intraday_data.get('fresh') else 'missing/stale'}</code>"
        f" · hard gates <code>{'active' if hard_gates.get('fresh') else 'inactive/stale'}</code>"
        f" · prohibit_allow <code>{bool(hard_gates.get('fresh') and hard_gates.get('prohibitAllow'))}</code>",
        f"VIX <code>{_fmt(vix.get('last'))}</code> {_signed(vix.get('ret1'))} · 10Y <code>{_fmt(tnx.get('last'))}</code> {_signed(tnx.get('ret1'))}",
        "",
        f"<b>Mood</b>: {html.escape(mood.get('mood', 'mixed'))} · {html.escape(mood.get('reason', ''))}",
        f"<b>Leaders</b>: {html.escape(leaders.get('state', 'mixed'))} · {html.escape(format_leaders(leaders))}",
        f"<b>Portfolio</b>: {html.escape(str(theme.get('theme', '—')))} { _fmt(theme.get('weightPct'), 1, '%') }资金 / { _fmt(theme.get('riskPct'), 1, '%') }风险 · cash <code>${_fmt(portfolio['summary'].get('cashTotal'))}</code>",
        "",
        (f"<b>Dated playbook</b>: {html.escape(str(playbook.get('slowState', '—')))} · "
         f"{html.escape(str(playbook.get('spreadBias', '')))}"
         if playbook.get("active") else
         f"<b>Dated playbook</b>: inactive ({html.escape(str(playbook.get('status', 'unavailable')))}) · "
         f"valid through {html.escape(str(playbook.get('validThrough', '—')))}"),
        f"<b>Decision</b>: {html.escape(decision.get('primaryAction', ''))}",
    ]

    next_events = playbook.get("activeEvents") or playbook.get("nextEvents") or []
    if next_events:
        lines.append(
            "<b>Event tape</b>: "
            + html.escape("；".join(
                f"{e.get('name')} {e.get('timeET')}ET ({e.get('hoursAway')}h): {e.get('protocol')}"
                for e in next_events[:2]
            ))
        )

    if spmo.get("available"):
        spmo_levels = spmo.get("levels") or {}
        spmo_pos = spmo.get("position") or {}
        lines.append(
            f"<b>SPMO sleeve</b>: {html.escape(str(spmo.get('label')))} · "
            f"price <code>{_fmt(spmo.get('price'))}</code> · wt <code>{_fmt(spmo_pos.get('weightPct'), 1, '%')}</code> · "
            f"buy-stop <code>{_fmt(spmo_levels.get('buyStop'))}</code> · invalidation <code>{_fmt(spmo_levels.get('invalidationClose'))}</code>"
        )
    elif spmo:
        lines.append(f"<b>SPMO sleeve</b>: {html.escape(str(spmo.get('label', 'BLOCK_DATA')))} · {html.escape(str(spmo.get('reason', '')))}")

    if decision.get("blockers"):
        lines.append("BLOCK: " + html.escape("；".join(decision["blockers"][:3])))
    if decision.get("watchItems"):
        lines.append("WATCH: " + html.escape("；".join(decision["watchItems"][:3])))

    lines += [
        "",
        "<b>Levels</b>",
        f"Reclaim buy-stop: <code>{_fmt(levels.get('reclaimBuyStop'))}</code> / limit <code>{_fmt(levels.get('reclaimLimit'))}</code>",
        f"EMA21 zone: <code>{levels.get('ema21Zone')}</code> · EMA34/invalidation: <code>{_fmt(levels.get('ema34'))}</code> / <code>{_fmt(levels.get('invalidationClose'))}</code>",
        f"Current 3xATR reference: <code>{_fmt(levels.get('current3AtrReference'))}</code> · not ratcheted; never lower an already-saved stop",
    ]
    ccs = levels.get("tqqqCcsGuide")
    if ccs:
        role = "hedge" if ccs.get("role") == "hedge" else "directional (not hedge)"
        role_rule = "verified against existing long exposure" if ccs.get("role") == "hedge" else "no verified long exposure; never label as hedge"
        lines.append(
            f"TQQQ CCS {role} watch: underlying pressure <code>{ccs['underlyingPressureZone']}</code> · "
            f"strikes require live chain/delta/credit/max-loss · {role_rule}"
        )

    headlines = format_headlines(news)
    if headlines:
        lines += ["", "<b>Headlines</b>"]
        for idx, title in enumerate(headlines, 1):
            lines.append(f"{idx}. {html.escape(title)}")
    elif news.get("errors"):
        lines.append("News errors: " + html.escape(json.dumps(news.get("errors"), ensure_ascii=False)))

    lines.append("")
    lines.append("技术/流程参考，不是自动交易指令。")
    return "\n".join(lines)


def write_outputs(snapshot, out_dir):
    atomic_write_json(out_dir / "latest_snapshot.json", snapshot)
    msg = format_telegram_message(snapshot)
    atomic_write_text(out_dir / "latest_message.html", msg)

    log_path = out_dir / "market_sentinel_log.csv"
    decision = snapshot["agents"]["decision"]
    technical = snapshot["agents"]["technical"]
    row = {
        "ranAt": snapshot["ranAt"],
        "label": decision.get("label"),
        "regime": technical.get("regime"),
        "intradayLabel": technical.get("intradayLabel"),
        "mood": snapshot["agents"]["mood"].get("mood"),
        "leaderState": snapshot["agents"]["leaders"].get("state"),
        "spmoLabel": (snapshot["agents"].get("spmoMomentum") or {}).get("label"),
        "spmoPrice": (snapshot["agents"].get("spmoMomentum") or {}).get("price"),
        "qqq": (snapshot["agents"]["price"]["quotes"].get("QQQ") or {}).get("last"),
        "qqqRet1": (snapshot["agents"]["price"]["quotes"].get("QQQ") or {}).get("ret1"),
        "primaryAction": decision.get("primaryAction"),
    }
    append_schema_csv(log_path, row)
    return msg


def append_schema_csv(path, row):
    fieldnames = list(row.keys())
    existing = []
    if path.exists():
        with path.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames:
                for old in reader:
                    recovered = {key: old.get(key, "") for key in fieldnames}
                    extra = old.get(None) or []
                    if extra and old.get("qqq") in ("ALLOW", "WATCH", "BLOCK", "BLOCK_DATA"):
                        recovered["spmoLabel"] = old.get("qqq", "")
                        recovered["spmoPrice"] = old.get("qqqRet1", "")
                        recovered["qqq"] = old.get("primaryAction", "")
                        recovered["qqqRet1"] = extra[0] if len(extra) >= 1 else ""
                        recovered["primaryAction"] = extra[1] if len(extra) >= 2 else ""
                    existing.append(recovered)
    needs_rewrite = not path.exists()
    if path.exists():
        with path.open(newline="", encoding="utf-8") as f:
            header = next(csv.reader(f), [])
        needs_rewrite = header != fieldnames

    if needs_rewrite:
        atomic_write_csv(path, [*existing, row], fieldnames)
    else:
        append_csv_row_private(path, row, fieldnames)


def send_telegram(message):
    sys.path.insert(0, str(ROOT / "scripts"))
    from telegram_notifier import send_message

    return send_message(message)


def main():
    ap = argparse.ArgumentParser(description="Run a QQQ/TQQQ market sentinel and optionally push Telegram.")
    ap.add_argument("--dashboard", default=str(DEFAULT_DASHBOARD))
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT))
    ap.add_argument("--input-dir", default=str(Path.home() / "Downloads"))
    ap.add_argument("--refresh-dashboard", action="store_true", help="run latest price refresh before analysis")
    ap.add_argument("--refresh-intraday", action="store_true", help="pull fresh QQQ 1m/5m/15m/30m before analysis")
    ap.add_argument("--intraday-gates", default=str(INTRADAY_TAPE_GATES),
                    help="deterministic intraday hard-gate JSON; only fresh gates are enforced")
    ap.add_argument("--no-fetch", action="store_true", help="pass --no-fetch to dashboard refresh")
    ap.add_argument("--no-news", action="store_true")
    ap.add_argument("--news-days", type=int, default=2)
    ap.add_argument("--max-headlines-per-query", type=int, default=5)
    ap.add_argument("--telegram", action="store_true")
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    lock = acquire_lock(out_dir / ".market_sentinel.lock")
    if lock is None:
        print("market sentinel skipped: previous run still active")
        return 75
    try:
        return run_cli(args, out_dir)
    finally:
        release_lock(lock)


def run_cli(args, out_dir):

    snapshot = build_snapshot(args)
    msg = write_outputs(snapshot, out_dir)
    if args.telegram:
        ok = send_telegram(msg)
        print(f"telegram={'sent' if ok else 'failed'}")
        if not ok:
            return 1
    decision = snapshot["agents"]["decision"]
    print(f"{snapshot['ranAt']} {decision['label']} {decision['teacherRead']}")
    print(out_dir.resolve() / "latest_snapshot.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

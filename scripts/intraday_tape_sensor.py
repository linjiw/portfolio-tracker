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
import json
import math
from pathlib import Path

import yfinance as yf


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output" / "intraday_tape"
PT = dt.timezone(dt.timedelta(hours=-7), name="America/Los_Angeles")
ET = dt.timezone(dt.timedelta(hours=-4), name="America/New_York")
INTERVALS = {"1m": "7d", "5m": "30d", "15m": "60d", "30m": "60d"}
INTERVAL_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "30m": 30}
DEFAULT_EVENTS = [
    {"t": "2026-06-12T10:00:00-04:00", "name": "UMich", "status": "pending"},
    {"t": "2026-06-17T14:00:00-04:00", "name": "FOMC dots", "status": "pending"},
    {"t": "2026-06-18T16:00:00-04:00", "name": "June quarterly expiration", "status": "pending"},
]


def now_et():
    return dt.datetime.now(tz=ET)


def to_float(value):
    try:
        if value is None or (isinstance(value, float) and math.isnan(value)):
            return None
        return float(value)
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
    rows = []
    for idx, row in df.iterrows():
        close = to_float(row[colmap.get("close")])
        high = to_float(row[colmap.get("high")])
        low = to_float(row[colmap.get("low")])
        open_ = to_float(row[colmap.get("open")])
        if close is None or high is None or low is None or open_ is None:
            continue
        vol_col = colmap.get("volume")
        ts = idx.to_pydatetime() if hasattr(idx, "to_pydatetime") else idx
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=ET)
        rows.append({
            "t": ts.astimezone(ET).isoformat(timespec="minutes"),
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": to_float(row[vol_col]) if vol_col is not None else 0.0,
        })
    return rows


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


def split_closed_forming(rows, interval):
    if not rows:
        return [], None
    minutes = INTERVAL_MINUTES[interval]
    current = now_et()
    last = rows[-1]
    last_start = parse_t(last["t"])
    bar_end = last_start + dt.timedelta(minutes=minutes)
    if current < bar_end + dt.timedelta(seconds=30):
        age = max(0, int((current - last_start).total_seconds()))
        forming = {**last, "age_sec": age, "vol_so_far": last.get("volume")}
        return rows[:-1], forming
    return rows, None


def fetch_intraday(symbol):
    out = {}
    for interval, period in INTERVALS.items():
        df = yf.download(symbol, period=period, interval=interval, auto_adjust=False, prepost=False, progress=False, threads=False)
        rows = add_features(df_to_rows(df, symbol))
        closed, forming = split_closed_forming(rows, interval)
        out[interval] = {"closed": closed, "forming": forming}
    return out


def fetch_daily(symbol):
    df = yf.download(symbol, period="9mo", interval="1d", auto_adjust=False, progress=False, threads=False)
    rows = df_to_rows(df, symbol)
    if not rows:
        return []
    closes = [r["close"] for r in rows]
    for name, n in (("ema8", 8), ("ema13", 13), ("ema21", 21), ("ema34", 34), ("ema55", 55)):
        vals = ema(closes, n)
        for i, row in enumerate(rows):
            row[name] = vals[i]
    trs = true_ranges(rows)
    atr = ema(trs, 14)
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
            vals.append(matches[-1].get("volume") or 0.0)
            cutoff = parse_t(matches[-1]["t"]).time()
            cum_vals.append(sum((r.get("volume") or 0.0) for r in day if parse_t(r["t"]).time() <= cutoff))
    vals = vals[-lookback:]
    cum_vals = cum_vals[-lookback:]
    today = [r for r in rows if session_date(r) == cur_date and parse_t(r["t"]) <= parse_t(current_row["t"])]
    current_cum = sum((r.get("volume") or 0.0) for r in today)
    base = mean(vals)
    cum_base = mean(cum_vals)
    return {
        "tod": key,
        "baseline": rnd(base, 0),
        "rvol": rnd((current_row.get("volume") or 0.0) / base, 2) if base else None,
        "cum_baseline": rnd(cum_base, 0),
        "cum_rvol": rnd(current_cum / cum_base, 2) if cum_base else None,
        "sample_days": len(vals),
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
    tr20 = mean(true_ranges(sess[-20:]))
    mean20 = mean([r["close"] for r in sess[-20:]])
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
        "fresh_low45": bool(prior and min(r["low"] for r in recent) < min(r["low"] for r in prior) - 0.01),
        "fresh_high45": bool(prior and max(r["high"] for r in recent) > max(r["high"] for r in prior) + 0.01),
        "vwap": rnd(last.get("vwap")),
        "fma_band": {"bottom": rnd(min(vals) if vals else None), "top": rnd(max(vals) if vals else None)},
        "ema": {k: rnd(last.get(k)) for k in ("ema5", "ema8", "ema13", "ema21", "ema34")},
        "range_position_pct": rnd((last["close"] - min(r["low"] for r in sess)) / (max(r["high"] for r in sess) - min(r["low"] for r in sess)) * 100, 1) if max(r["high"] for r in sess) > min(r["low"] for r in sess) else None,
        "mean20": rnd(mean20),
        "atr20": rnd(tr20),
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


def load_state(path, symbol):
    if path.exists():
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            state = {}
    else:
        state = {}
    today = now_et().date().isoformat()
    if state.get("session_date") != today:
        if state:
            archive = path.with_name(f"state.{state.get('session_date', 'unknown')}.json")
            if not archive.exists():
                archive.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
        state = {
            "session_date": today,
            "instrument": symbol,
            "events": DEFAULT_EVENTS,
            "swings": {},
            "levels": [],
            "vol_baseline_tod": {},
            "scenarios": [],
            "positions": [],
            "verdict_history": [],
            "burden_of_proof": "bears",
            "last_verdict_hash": "",
        }
    state.setdefault("events", DEFAULT_EVENTS)
    state.setdefault("levels", [])
    state.setdefault("positions", [])
    state.setdefault("verdict_history", [])
    return state


def read_positions():
    out = []
    manual = OUT / "positions.json"
    if manual.exists():
        try:
            data = json.loads(manual.read_text(encoding="utf-8"))
            out.extend(data if isinstance(data, list) else data.get("positions", []))
        except Exception:
            pass
    sentinel = ROOT / "output" / "market_sentinel" / "latest_snapshot.json"
    if sentinel.exists():
        try:
            snap = json.loads(sentinel.read_text(encoding="utf-8"))
            out.extend(((snap.get("agents") or {}).get("portfolio") or {}).get("optionSpreads") or [])
        except Exception:
            pass
    return out


def build_gates(observation, state):
    current = now_et()
    minute = current.hour * 60 + current.minute
    triggered = []
    action_locks = []
    score_cap = None
    if minute < 9 * 60 + 45 or minute > 14 * 60 + 30:
        triggered.append({"gate": "G1", "name": "time_gate", "reason": "now < 09:45 ET or now > 14:30 ET"})
        action_locks.append("不开新仓")
    active_events = []
    for event in state.get("events", []):
        try:
            event_t = dt.datetime.fromisoformat(event["t"]).astimezone(ET)
        except Exception:
            continue
        delta_min = (event_t - current).total_seconds() / 60.0
        if -15 <= delta_min <= 60:
            active_events.append({**event, "minutes_away": round(delta_min, 1)})
    if active_events:
        triggered.append({"gate": "G2", "name": "event_gate", "events": active_events})
        action_locks.append("不开新仓")
    fifteen = observation["intervals"].get("15m") or {}
    vol = fifteen.get("volume") or {}
    if (vol.get("rvol") is not None and vol.get("rvol") < 1.0 and
            vol.get("cum_rvol") is not None and vol.get("cum_rvol") < 1.0):
        triggered.append({"gate": "G3", "name": "vacuum_gate", "reason": "15m rvol < 1.0 and cum_rvol < 1.0"})
        score_cap = "观察"
    next_events = []
    for event in state.get("events", []):
        try:
            event_t = dt.datetime.fromisoformat(event["t"]).astimezone(ET)
            if event_t >= current:
                next_events.append({**event, "hours_away": round((event_t - current).total_seconds() / 3600.0, 1)})
        except Exception:
            pass
    next_events.sort(key=lambda x: x["hours_away"])
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
        "generated_at": current.isoformat(timespec="seconds"),
        "triggered": triggered,
        "action_lock": sorted(set(action_locks)),
        "score_cap": score_cap,
        "prohibit_allow": any(x["gate"] == "G3" for x in triggered),
        "cross_event_dte_policy": {
            "gate": "G4",
            "next_binary_event": next_events[0] if next_events else None,
            "rule": "If recommended expiry crosses the next binary event, halve size or reject; 0DTE/day-before crossing is rejected.",
        },
        "llm_may_not_override": True,
    }


def main():
    ap = argparse.ArgumentParser(description="Build intraday tape observation and gates for Codex Judge.")
    ap.add_argument("--symbol", default="QQQ")
    ap.add_argument("--out-dir", default=str(OUT))
    args = ap.parse_args()

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    state_path = out / "state.json"
    state = load_state(state_path, args.symbol)

    intraday = fetch_intraday(args.symbol)
    daily = fetch_daily(args.symbol)
    summaries = {k: interval_summary(v["closed"], k) for k, v in intraday.items()}
    five_rows = intraday["5m"]["closed"]
    prior = prior_session(five_rows)
    prior_day = {
        "prior_day_high": rnd(max((r["high"] for r in prior), default=None)),
        "prior_day_low": rnd(min((r["low"] for r in prior), default=None)),
        "prior_day_close": rnd(prior[-1]["close"]) if prior else None,
    }
    price_row = next((summaries[k]["last_closed"] for k in ("1m", "5m", "15m") if summaries.get(k, {}).get("available")), None)
    observation = {
        "generated_at": now_et().isoformat(timespec="seconds"),
        "symbol": args.symbol,
        "source": "Yahoo Finance via yfinance; closed bars only",
        "price": {
            "last": rnd(price_row["close"] if price_row else None),
            "last_bar_time": price_row["t"] if price_row else None,
        },
        "forming": {k: v["forming"] for k, v in intraday.items()},
        "intervals": summaries,
        "prior_day": prior_day,
        "daily": daily[-1] if daily else {},
        "fibs": {
            "macro": fibs_macro(daily) if daily else {"swing": [], "levels": {}},
            "active": fibs_active(five_rows),
        },
        "positions": read_positions(),
    }
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

    gates = build_gates(observation, state)
    state_path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    (out / "observation.json").write_text(json.dumps(observation, indent=2, ensure_ascii=False), encoding="utf-8")
    (out / "gates.json").write_text(json.dumps(gates, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({
        "observation": str(out / "observation.json"),
        "gates": str(out / "gates.json"),
        "state": str(state_path),
        "last": observation["price"]["last"],
        "triggered_gates": [g["gate"] for g in gates["triggered"]],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()

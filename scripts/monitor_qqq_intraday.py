#!/usr/bin/env python3
"""Intraday QQQ monitor for teacher-style close-before-entry decisions.

The job pulls QQQ 1m/5m/15m/30m candles, keeps the latest CSV snapshots, and
scores whether intraday "center of gravity" has stopped falling. It is a
technical monitor, not trading advice.
"""
import argparse
import csv
import datetime as dt
import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "output" / "intraday_qqq"
INTERVALS = ("1m", "5m", "15m", "30m")
INTERVAL_PERIODS = {"1m": "1d", "5m": "5d", "15m": "5d", "30m": "1mo"}
PT = dt.timezone(dt.timedelta(hours=-7), name="America/Los_Angeles")
ET = dt.timezone(dt.timedelta(hours=-4), name="America/New_York")
TELEGRAM_CHAT_CACHE = "telegram_chat_id.txt"
STATE_FILE_NAME = "state.json"


def _to_float(value):
    try:
        if value is None or (isinstance(value, float) and math.isnan(value)):
            return None
        return float(value)
    except Exception:
        return None


def _round(value, digits=2):
    value = _to_float(value)
    return None if value is None else round(value, digits)


def _avg(values):
    vals = [_to_float(v) for v in values]
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else None


def _session_date(row):
    return str(row.get("time", ""))[:10]


def _session_time(row):
    try:
        return dt.datetime.fromisoformat(row["time"]).timetz().replace(tzinfo=None)
    except Exception:
        return None


def current_session_rows(rows):
    if not rows:
        return []
    last_date = _session_date(rows[-1])
    return [r for r in rows if _session_date(r) == last_date]


def grouped_sessions(rows):
    out = {}
    for row in rows:
        out.setdefault(_session_date(row), []).append(row)
    return out


def ema_values(values, n):
    alpha = 2.0 / (n + 1)
    out, cur = [], None
    for raw in values:
        v = _to_float(raw)
        if v is None:
            out.append(cur)
            continue
        cur = v if cur is None else alpha * v + (1 - alpha) * cur
        out.append(cur)
    return out


def rsi_values(values, n=14):
    if not values:
        return []
    gains, losses, out = [], [], [None]
    prev = _to_float(values[0])
    avg_gain = avg_loss = None
    for raw in values[1:]:
        cur = _to_float(raw)
        if cur is None or prev is None:
            out.append(None)
            prev = cur
            continue
        delta = cur - prev
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))
        if len(gains) < n:
            out.append(None)
        elif len(gains) == n:
            avg_gain = sum(gains[-n:]) / n
            avg_loss = sum(losses[-n:]) / n
            out.append(100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss))
        else:
            avg_gain = (avg_gain * (n - 1) + gains[-1]) / n
            avg_loss = (avg_loss * (n - 1) + losses[-1]) / n
            out.append(100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss))
        prev = cur
    return out


def atr14_rows(rows):
    trs = []
    prev_close = None
    for row in rows:
        high, low, close = row["high"], row["low"], row["close"]
        if prev_close is None:
            tr = high - low
        else:
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
        prev_close = close

    out, cur = [], None
    for i, tr in enumerate(trs):
        if i < 13:
            out.append(None)
        elif i == 13:
            cur = sum(trs[:14]) / 14.0
            out.append(cur)
        else:
            cur = (cur * 13 + tr) / 14.0
            out.append(cur)
    return out


def _flatten_download(df, symbol):
    if df is None or df.empty:
        return df
    try:
        import pandas as pd
    except Exception:
        return df
    if isinstance(df.columns, pd.MultiIndex):
        levels = list(range(df.columns.nlevels))
        for level in reversed(levels):
            vals = [str(x) for x in df.columns.get_level_values(level)]
            if symbol in vals:
                return df.xs(symbol, axis=1, level=level)
        return df.droplevel(-1, axis=1)
    return df


def _df_to_rows(df, symbol):
    df = _flatten_download(df, symbol)
    if df is None or df.empty:
        return []

    colmap = {str(c).strip().lower(): c for c in df.columns}
    required = ["open", "high", "low", "close"]
    if any(k not in colmap for k in required):
        return []

    rows = []
    for idx, row in df.iterrows():
        close = _to_float(row[colmap["close"]])
        high = _to_float(row[colmap["high"]])
        low = _to_float(row[colmap["low"]])
        open_ = _to_float(row[colmap["open"]])
        if close is None or high is None or low is None or open_ is None:
            continue
        vol_col = colmap.get("volume")
        volume = _to_float(row[vol_col]) if vol_col is not None else 0.0
        ts = idx.to_pydatetime() if hasattr(idx, "to_pydatetime") else idx
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=ET)
        rows.append({
            "time": ts.astimezone(ET).isoformat(timespec="minutes"),
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume or 0.0,
        })
    return rows


def add_intraday_features(rows):
    closes = [r["close"] for r in rows]
    ema5 = ema_values(closes, 5)
    ema8 = ema_values(closes, 8)
    ema13 = ema_values(closes, 13)
    ema21 = ema_values(closes, 21)
    ema34 = ema_values(closes, 34)
    cum_pv = 0.0
    cum_vol = 0.0
    cur_date = None
    for i, row in enumerate(rows):
        row["ema5"] = ema5[i]
        row["ema8"] = ema8[i]
        row["ema13"] = ema13[i]
        row["ema21"] = ema21[i]
        row["ema34"] = ema34[i]
        row_date = _session_date(row)
        if row_date != cur_date:
            cur_date = row_date
            cum_pv = 0.0
            cum_vol = 0.0
        typical = (row["high"] + row["low"] + row["close"]) / 3.0
        vol = max(row.get("volume") or 0.0, 0.0)
        if vol:
            cum_pv += typical * vol
            cum_vol += vol
        row["vwap"] = (cum_pv / cum_vol) if cum_vol else typical
    return rows


def fetch_intraday(symbol, intervals=INTERVALS):
    import warnings
    warnings.filterwarnings("ignore")
    import yfinance as yf

    out = {}
    for interval in intervals:
        df = yf.download(
            symbol,
            period=INTERVAL_PERIODS.get(interval, "1d"),
            interval=interval,
            auto_adjust=False,
            prepost=False,
            progress=False,
            threads=False,
        )
        out[interval] = add_intraday_features(_df_to_rows(df, symbol))
    return out


def fetch_daily_context(symbol):
    import warnings
    warnings.filterwarnings("ignore")
    import yfinance as yf

    df = yf.download(
        symbol,
        period="6mo",
        interval="1d",
        auto_adjust=False,
        progress=False,
        threads=False,
    )
    rows = _df_to_rows(df, symbol)
    if len(rows) < 34:
        return {"available": False, "reason": "not enough daily rows"}
    closes = [r["close"] for r in rows]
    for name, n in (("ema8", 8), ("ema13", 13), ("ema21", 21), ("ema34", 34), ("ema55", 55)):
        vals = ema_values(closes, n)
        for i, row in enumerate(rows):
            row[name] = vals[i]
    atr = atr14_rows(rows)
    rsi = rsi_values(closes)
    for i, row in enumerate(rows):
        row["atr14"] = atr[i]
        row["rsi14"] = rsi[i] if i < len(rsi) else None
    last = rows[-1]
    prev = rows[-2]
    recent = rows[-126:] if len(rows) >= 126 else rows
    swing_high_row = max(recent, key=lambda r: r["high"])
    swing_low_row = min(recent, key=lambda r: r["low"])
    swing_high = swing_high_row["high"]
    swing_low = swing_low_row["low"]
    pullback_fibs = {}
    if swing_high > swing_low:
        for name, ratio in (("fib23", 0.236), ("fib38", 0.382), ("fib50", 0.500), ("fib62", 0.618)):
            pullback_fibs[name] = _round(swing_high - (swing_high - swing_low) * ratio)
    return {
        "available": True,
        "date": last["time"][:10],
        "close": _round(last["close"]),
        "prevClose": _round(prev["close"]),
        "ema8": _round(last["ema8"]),
        "ema13": _round(last["ema13"]),
        "ema21": _round(last["ema21"]),
        "ema34": _round(last["ema34"]),
        "ema55": _round(last["ema55"]),
        "atr14": _round(last["atr14"]),
        "rsi14": _round(last["rsi14"], 1),
        "ema21Slope5": _round((last["ema21"] / rows[-6]["ema21"] - 1) * 100, 2) if len(rows) >= 6 and rows[-6].get("ema21") else None,
        "swingWindowDays": len(recent),
        "swingHigh": _round(swing_high),
        "swingLow": _round(swing_low),
        "pullbackFibs": pullback_fibs,
    }


def interval_minutes(interval):
    return int(interval[:-1]) if interval.endswith("m") else 1


def volume_context(rows, session, interval):
    if not session:
        return {}
    last = session[-1]
    last_vol = _to_float(last.get("volume")) or 0.0
    sample = [r.get("volume") for r in session[-21:-1]]
    if len([v for v in sample if _to_float(v)]) < 5:
        sample = [r.get("volume") for r in rows[-61:-1]]
    vma20 = _avg(sample)

    minutes = interval_minutes(interval)
    recent_n = max(2, int(math.ceil(15 / minutes)))
    recent_avg = _avg([r.get("volume") for r in session[-recent_n:]])
    prior_avg = _avg([r.get("volume") for r in session[-(recent_n + 20):-recent_n]])

    cur_time = _session_time(last)
    current_cum = sum((_to_float(r.get("volume")) or 0.0) for r in session)
    prior_cums = []
    for date, day_rows in grouped_sessions(rows).items():
        if date == _session_date(last) or not cur_time:
            continue
        prior_cum = 0.0
        for row in day_rows:
            row_time = _session_time(row)
            if row_time and row_time <= cur_time:
                prior_cum += _to_float(row.get("volume")) or 0.0
        if prior_cum:
            prior_cums.append(prior_cum)
    cum_base = _avg(prior_cums[-10:])
    return {
        "lastVolume": _round(last_vol, 0),
        "vma20": _round(vma20, 0),
        "volumeRatio": _round(last_vol / vma20, 2) if vma20 else None,
        "recentVolumeRatio": _round(recent_avg / prior_avg, 2) if prior_avg else None,
        "cumulativeVolume": _round(current_cum, 0),
        "cumulativeVolumeRatio": _round(current_cum / cum_base, 2) if cum_base else None,
    }


def fma_context(last):
    vals = {
        "ema5": _to_float(last.get("ema5")),
        "ema8": _to_float(last.get("ema8")),
        "ema13": _to_float(last.get("ema13")),
        "ema21": _to_float(last.get("ema21")),
        "ema34": _to_float(last.get("ema34")),
    }
    usable = [v for v in vals.values() if v is not None]
    close = _to_float(last.get("close"))
    top = max(usable) if usable else None
    bottom = min(usable) if usable else None
    if close is None or top is None or bottom is None:
        position = "unknown"
    elif close > top:
        position = "above_band"
    elif close < bottom:
        position = "below_band"
    else:
        position = "inside_band"
    bull_stack = all(vals[a] is not None and vals[b] is not None and vals[a] > vals[b]
                     for a, b in (("ema5", "ema8"), ("ema8", "ema13"), ("ema13", "ema21"), ("ema21", "ema34")))
    bear_stack = all(vals[a] is not None and vals[b] is not None and vals[a] < vals[b]
                     for a, b in (("ema5", "ema8"), ("ema8", "ema13"), ("ema13", "ema21"), ("ema21", "ema34")))
    return {
        **{k: _round(v) for k, v in vals.items()},
        "fmaTop": _round(top),
        "fmaBottom": _round(bottom),
        "fmaPosition": position,
        "fmaBullStack": bull_stack,
        "fmaBearStack": bear_stack,
    }


def volume_price_verdict(summary):
    ratio = _to_float(summary.get("volumeRatio"))
    if ratio is None:
        return "量能基线不足"
    fresh_low = summary.get("freshLow45m")
    fresh_high = summary.get("freshHigh45m")
    pos = _to_float(summary.get("rangePositionPct"))
    last_bar_pos = _to_float(summary.get("lastBarPositionPct"))
    if ratio < 1.0:
        return "真空漂移，突破/跌破打折"
    if ratio >= 2.0 and fresh_low and (pos is None or pos <= 25):
        return "放量新低延续，重心仍下移"
    if ratio >= 1.5 and fresh_low and last_bar_pos is not None and last_bar_pos >= 55:
        return "放量扫低收回，承接首证"
    if ratio >= 1.5 and not fresh_low and not fresh_high:
        return "放量重叠争夺，看15-30分钟能否横住"
    if ratio >= 1.5 and fresh_high:
        return "放量上冲，需确认是否能留在供给区上方"
    return "普通量，方向信息有限"


def summarize_interval(rows, interval):
    if not rows:
        return {"available": False, "interval": interval}
    session = current_session_rows(rows)
    if not session:
        return {"available": False, "interval": interval, "reason": "no current session rows"}
    last = session[-1]
    minutes = interval_minutes(interval)
    bars_45m = max(1, int(math.ceil(45 / minutes)))
    recent = session[-bars_45m:]
    prior = session[-2 * bars_45m:-bars_45m] if len(session) >= 2 * bars_45m else []
    day_high = max(r["high"] for r in session)
    day_low = min(r["low"] for r in session)
    recent_low = min(r["low"] for r in recent)
    recent_high = max(r["high"] for r in recent)
    prior_low = min((r["low"] for r in prior), default=None)
    prior_high = max((r["high"] for r in prior), default=None)
    ema8_start = session[-min(len(session), bars_45m)]["ema8"]
    ema21_start = session[-min(len(session), bars_45m)]["ema21"]
    close = last["close"]
    vwap = last.get("vwap")
    bar_range = last["high"] - last["low"]
    out = {
        "available": True,
        "interval": interval,
        "bars": len(session),
        "sourceBars": len(rows),
        "sessionDate": _session_date(last),
        "lastTime": last["time"],
        "close": _round(close),
        "open": _round(last["open"]),
        "high": _round(last["high"]),
        "low": _round(last["low"]),
        "dayHigh": _round(day_high),
        "dayLow": _round(day_low),
        "recent45mLow": _round(recent_low),
        "prior45mLow": _round(prior_low),
        "recent45mHigh": _round(recent_high),
        "prior45mHigh": _round(prior_high),
        "noNewLow45m": None if prior_low is None else recent_low >= prior_low - 0.01,
        "freshLow45m": None if prior_low is None else recent_low < prior_low - 0.01,
        "freshHigh45m": None if prior_high is None else recent_high > prior_high + 0.01,
        "ema8": _round(last.get("ema8")),
        "ema21": _round(last.get("ema21")),
        "ema8Slope45m": _round(last["ema8"] - ema8_start),
        "ema21Slope45m": _round(last["ema21"] - ema21_start),
        "vwap": _round(vwap),
        "distVwapPct": _round((close / vwap - 1) * 100, 2) if vwap else None,
        "aboveVwap": None if vwap is None else close >= vwap,
        "aboveEma8": close >= last["ema8"] if last.get("ema8") is not None else None,
        "aboveEma21": close >= last["ema21"] if last.get("ema21") is not None else None,
        "ema8AboveEma21": last["ema8"] >= last["ema21"] if last.get("ema8") is not None and last.get("ema21") is not None else None,
        "rangePositionPct": _round((close - day_low) / (day_high - day_low) * 100, 1) if day_high > day_low else None,
        "lastBarPositionPct": _round((close - last["low"]) / bar_range * 100, 1) if bar_range > 0 else None,
    }
    out.update(fma_context(last))
    out.update(volume_context(rows, session, interval))
    out["volumeVerdict"] = volume_price_verdict(out)
    return out


def add_level(candidates, px, factor):
    px = _to_float(px)
    if px is None or px <= 0:
        return
    candidates.append({"px": px, "factors": [factor]})


def previous_session_levels(rows):
    sessions = grouped_sessions(rows)
    dates = sorted(d for d in sessions if d)
    if len(dates) < 2:
        return {}
    prev = sessions[dates[-2]]
    return {
        "prevHigh": max(r["high"] for r in prev),
        "prevLow": min(r["low"] for r in prev),
        "prevClose": prev[-1]["close"],
    }


def cluster_levels(candidates, current):
    if not candidates or current is None:
        return []
    tolerance = max(0.35, current * 0.0012)
    clusters = []
    for cand in sorted(candidates, key=lambda x: x["px"]):
        for cluster in clusters:
            if abs(cand["px"] - cluster["px"]) <= tolerance:
                cluster["raw"].append(cand["px"])
                cluster["px"] = sum(cluster["raw"]) / len(cluster["raw"])
                cluster["factors"].extend(cand["factors"])
                break
        else:
            clusters.append({"px": cand["px"], "raw": [cand["px"]], "factors": list(cand["factors"])})
    out = []
    for cluster in clusters:
        factors = []
        for factor in cluster["factors"]:
            if factor not in factors:
                factors.append(factor)
        out.append({
            "px": _round(cluster["px"]),
            "factorCount": len(factors),
            "factors": factors,
        })
    return out


def build_level_map(intraday, daily, summaries):
    five_rows = intraday.get("5m") or []
    fifteen = summaries.get("15m") or {}
    five = summaries.get("5m") or {}
    thirty = summaries.get("30m") or {}
    current = five.get("close") or fifteen.get("close") or daily.get("close")
    candidates = []

    for name, px in previous_session_levels(five_rows).items():
        add_level(candidates, px, name)
    for label, summary in (("5m", five), ("15m", fifteen), ("30m", thirty)):
        add_level(candidates, summary.get("dayHigh"), f"{label}_dayHigh")
        add_level(candidates, summary.get("dayLow"), f"{label}_dayLow")
        add_level(candidates, summary.get("recent45mLow"), f"{label}_recentLow")
        add_level(candidates, summary.get("recent45mHigh"), f"{label}_recentHigh")
        add_level(candidates, summary.get("vwap"), f"{label}_vwap")
        add_level(candidates, summary.get("fmaTop"), f"{label}_fmaTop")
        add_level(candidates, summary.get("fmaBottom"), f"{label}_fmaBottom")
        add_level(candidates, summary.get("ema21"), f"{label}_ema21")
    if daily.get("available"):
        for name in ("ema8", "ema13", "ema21", "ema34", "ema55"):
            add_level(candidates, daily.get(name), f"daily_{name}")
        for name, px in (daily.get("pullbackFibs") or {}).items():
            add_level(candidates, px, f"daily_{name}")

    levels = cluster_levels(candidates, current)
    above = [x for x in levels if current is not None and x["px"] >= current]
    below = [x for x in levels if current is not None and x["px"] <= current]
    battlefields = sorted(
        [x for x in levels if x["factorCount"] >= 2],
        key=lambda x: abs(x["px"] - current) if current is not None else 0,
    )[:5]
    nearest_above = min(above, key=lambda x: x["px"] - current) if above and current is not None else None
    nearest_below = max(below, key=lambda x: x["px"]) if below else None
    return {
        "current": _round(current),
        "nearestAbove": nearest_above,
        "nearestBelow": nearest_below,
        "battlefields": battlefields,
        "all": levels,
    }


def fmt_level(level):
    if not level:
        return "—"
    factors = "/".join(level.get("factors", [])[:3])
    tests = level.get("tests")
    test_txt = f", test {tests}" if tests is not None else ""
    return f"{fmt_num(level.get('px'))} ({level.get('factorCount', 0)}成分{test_txt}: {factors})"


def build_scenarios(label, summaries, levels):
    five = summaries.get("5m") or {}
    fifteen = summaries.get("15m") or {}
    thirty = summaries.get("30m") or {}
    above = levels.get("nearestAbove") or {}
    below = levels.get("nearestBelow") or {}
    scenarios = []
    if label == "BLOCK":
        scenarios.append({
            "branch": "bear_continuation",
            "trigger": f"5m/15m 放量跌破 {fmt_num(below.get('px'))} 且收不回 VWAP/FMA",
            "invalid": f"15m 实体收复 {fmt_num(fifteen.get('fmaTop'))} 并站上 VWAP {fmt_num(five.get('vwap'))}",
            "tactic": "不追空；等缩量反弹到上方墙再评估 put debit spread/已有多头 CCS。",
        })
        scenarios.append({
            "branch": "base_attempt",
            "trigger": "至少45分钟不再新低 + 5m收回VWAP/EMA21 + 15m EMA8斜率转正",
            "invalid": f"重新跌破 {fmt_num(five.get('recent45mLow'))} 且量能>=1.5x",
            "tactic": "只从 WATCH 开始，不直接从 BLOCK 跳到重仓进攻。",
        })
    elif label == "WATCH":
        scenarios.append({
            "branch": "reclaim_watch",
            "trigger": f"15m/30m 站稳 {fmt_num(above.get('px'))}，且5m量能不是缩量真空",
            "invalid": f"跌回 {fmt_num(five.get('vwap'))} 下方或45分钟新低",
            "tactic": "只允许小仓 defined-risk call debit spread，先定最大亏损。",
        })
    else:
        scenarios.append({
            "branch": "controlled_reclaim",
            "trigger": f"回踩不破 {fmt_num(five.get('vwap'))}/{fmt_num(fifteen.get('ema8'))}",
            "invalid": f"30m 收回失败并跌回区间低位 {fmt_num(thirty.get('dayLow'))}",
            "tactic": "允许小仓分批；盈利后移动止盈，不追高。",
        })
    return scenarios


def build_spread_plan(label, summaries, levels, daily):
    five = summaries.get("5m") or {}
    fifteen = summaries.get("15m") or {}
    above = levels.get("nearestAbove") or {}
    below = levels.get("nearestBelow") or {}
    daily_ema21 = daily.get("ema21")
    plans = []
    if label == "BLOCK":
        plans.append({
            "status": "BLOCK",
            "structure": "新增 TQQQ / long calls / call debit spread",
            "rule": f"QQQ 未站回日线EMA21 {fmt_num(daily_ema21)} 前不做进攻。",
        })
        plans.append({
            "status": "WATCH",
            "structure": "put debit spread",
            "rule": f"反弹到上方 {fmt_level(above)} 后缩量上影/拒绝，再看14-30DTE；debit <= 宽度25-30%。",
        })
        plans.append({
            "status": "WATCH",
            "structure": "call credit spread",
            "rule": "只有已有多头需要对冲，且价格反弹到2+成分阻力后才考虑；裸空方向仓不叫hedge。",
        })
    elif label == "WATCH":
        plans.append({
            "status": "WATCH",
            "structure": "call debit spread",
            "rule": f"触发线是15m站稳 {fmt_num(fifteen.get('fmaTop'))} + 5m VWAP {fmt_num(five.get('vwap'))}；否则空手。",
        })
        plans.append({
            "status": "WATCH",
            "structure": "put credit spread",
            "rule": f"只在下方 {fmt_level(below)} 放量扫低收回后评估，short腿放在支撑墙之后。",
        })
    else:
        plans.append({
            "status": "ALLOW",
            "structure": "small call debit spread",
            "rule": "14-30DTE，近ATM long call + 上方压力short call；亏损预设，盈利后移动止盈。",
        })
        plans.append({
            "status": "WATCH",
            "structure": "CCS hedge",
            "rule": f"若冲到上方 {fmt_level(above)} 后滞涨，可用小宽度CCS给已有多头降波动。",
        })
    return plans


def evaluate_signal(intraday, daily):
    summaries = {k: summarize_interval(v, k) for k, v in intraday.items()}
    one = summaries.get("1m", {})
    five = summaries.get("5m", {})
    fifteen = summaries.get("15m", {})
    thirty = summaries.get("30m", {})
    last = one.get("close") or five.get("close") or daily.get("close")

    checks = []

    def add(name, ok, weight, detail):
        checks.append({"name": name, "ok": ok, "weight": weight, "detail": detail})

    daily_hold = None
    if daily.get("available") and last is not None and daily.get("ema21") is not None:
        daily_hold = last >= daily["ema21"]
        add("日线 EMA21 守住", daily_hold, 2, f"QQQ {last:.2f} vs EMA21 {daily['ema21']:.2f}")
    else:
        add("日线 EMA21 守住", None, 0, "daily context missing")

    add("5m 收回 VWAP", five.get("aboveVwap"), 1, f"5m close {five.get('close')} vs VWAP {five.get('vwap')}")
    add("5m 收回 EMA21", five.get("aboveEma21"), 1, f"5m close {five.get('close')} vs EMA21 {five.get('ema21')}")
    add("5m EMA8>EMA21", five.get("ema8AboveEma21"), 1, f"5m EMA8 {five.get('ema8')} / EMA21 {five.get('ema21')}")
    add("15m 收回 EMA8", fifteen.get("aboveEma8"), 1, f"15m close {fifteen.get('close')} vs EMA8 {fifteen.get('ema8')}")
    add("15m EMA8 斜率转正", (fifteen.get("ema8Slope45m") or 0) >= 0, 1, f"15m EMA8 45m slope {fifteen.get('ema8Slope45m')}")
    add("45m 不再创新低", one.get("noNewLow45m") if one.get("available") else five.get("noNewLow45m"), 1,
        f"recent low {one.get('recent45mLow') or five.get('recent45mLow')} vs prior {one.get('prior45mLow') or five.get('prior45mLow')}")
    add("30m 收盘不在低位", (thirty.get("rangePositionPct") or 0) >= 35, 1, f"30m range position {thirty.get('rangePositionPct')}%")
    heavy_new_low = (
        five.get("freshLow45m") is True
        and (_to_float(five.get("volumeRatio")) or 0) >= 1.5
        and (_to_float(five.get("rangePositionPct")) or 0) <= 35
    )
    add("5m 量价不再下移", not heavy_new_low, 1, f"5m vol {five.get('volumeRatio')}x: {five.get('volumeVerdict')}")

    score = 0
    max_score = 0
    for c in checks:
        if c["ok"] is None:
            continue
        max_score += c["weight"]
        score += c["weight"] if c["ok"] else -min(c["weight"], 1)

    if daily_hold is False:
        label = "BLOCK"
        action = "日线 EMA21 未守住，老师框架下先不加 TQQQ/Call，等重新站回。"
    elif score >= 6:
        label = "ALLOW"
        action = "可以进入收盘前小仓观察区：只考虑分批、定义止损、移动止盈。"
    elif score >= 3:
        label = "WATCH"
        action = "等待下一轮 15m 确认：需要 5m/15m 继续站回，且不再创新低。"
    else:
        label = "BLOCK"
        action = "重心仍不稳，不做进攻；先看是否继续失守 EMA21/VWAP。"

    level_map = build_level_map(intraday, daily, summaries)
    scenarios = build_scenarios(label, summaries, level_map)
    spread_plan = build_spread_plan(label, summaries, level_map, daily)

    return {
        "label": label,
        "score": score,
        "maxScore": max_score,
        "last": last,
        "checks": checks,
        "summaries": summaries,
        "action": action,
        "teacherRead": f"QQQ intraday {label}: {action}",
        "levelMap": level_map,
        "scenarios": scenarios,
        "spreadPlan": spread_plan,
    }


def fmt_num(value, digits=2):
    value = _to_float(value)
    if value is None:
        return "—"
    return f"{value:.{digits}f}"


def fmt_bool(value):
    if value is True:
        return "YES"
    if value is False:
        return "NO"
    return "—"


def load_state(out_dir):
    path = Path(out_dir) / STATE_FILE_NAME
    if not path.exists():
        return {"levels": {}, "lastLabel": None}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"levels": {}, "lastLabel": None}


def save_state(out_dir, state):
    path = Path(out_dir) / STATE_FILE_NAME
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def annotate_level_tests(signal, state, pulled_at):
    levels = signal.get("levelMap") or {}
    five = (signal.get("summaries") or {}).get("5m") or {}
    bar_id = five.get("lastTime") or pulled_at
    bar_high = _to_float(five.get("high"))
    bar_low = _to_float(five.get("low"))
    current = _to_float(levels.get("current"))
    tolerance = max(0.35, (current or 0) * 0.0012)
    state.setdefault("levels", {})

    def touch(level):
        px = _to_float((level or {}).get("px"))
        if px is None:
            return
        hit = False
        if bar_high is not None and bar_low is not None and bar_low - tolerance <= px <= bar_high + tolerance:
            hit = True
        if current is not None and abs(current - px) <= tolerance:
            hit = True
        key = f"{px:.1f}"
        info = state["levels"].setdefault(key, {"tests": 0, "lastTestedAt": None, "lastTestedBar": None})
        if hit and info.get("lastTestedBar") is None and info.get("lastTestedAt"):
            info["lastTestedBar"] = bar_id
        if hit and info.get("lastTestedBar") != bar_id:
            info["tests"] = int(info.get("tests") or 0) + 1
            info["lastTestedAt"] = pulled_at
            info["lastTestedBar"] = bar_id
        level["tests"] = int(info.get("tests") or 0)

    for key in ("nearestAbove", "nearestBelow"):
        touch(levels.get(key))
    for level in levels.get("battlefields") or []:
        touch(level)
    for level in levels.get("all") or []:
        px = _to_float(level.get("px"))
        key = f"{px:.1f}" if px is not None else None
        if key and key in state["levels"]:
            level["tests"] = int(state["levels"][key].get("tests") or 0)

    state["lastLabel"] = signal.get("label")
    state["lastPulledAt"] = pulled_at
    return state


def fmt_volume(summary):
    return (
        f"vol {fmt_num(summary.get('volumeRatio'))}x / cum {fmt_num(summary.get('cumulativeVolumeRatio'))}x"
        f" -> {summary.get('volumeVerdict', '—')}"
    )


def format_telegram_message(snapshot):
    s = snapshot["signal"]
    daily = snapshot.get("daily", {})
    five = s["summaries"].get("5m", {})
    fifteen = s["summaries"].get("15m", {})
    thirty = s["summaries"].get("30m", {})
    one = s["summaries"].get("1m", {})
    levels = s.get("levelMap") or {}
    scenarios = s.get("scenarios") or []
    spread_plan = s.get("spreadPlan") or []
    checks = [c for c in s.get("checks", []) if c.get("ok") is not None]
    passed = sum(1 for c in checks if c.get("ok") is True)
    failed = sum(1 for c in checks if c.get("ok") is False)
    first_scenario = scenarios[0] if scenarios else {}
    first_spread = spread_plan[0] if spread_plan else {}
    lines = [
        f"QQQ 15m monitor · {snapshot['pulledAt']}",
        f"Signal: {s['label']} · score {s['score']}/{s['maxScore']} · QQQ {fmt_num(s.get('last'))}",
        f"Data: {snapshot.get('dataMode')} · last bar {five.get('lastTime') or one.get('lastTime')}",
        "",
        "重心定义: 日线EMA21 + 5m VWAP/EMA21 + 15m FMA/斜率 + 45m no-new-low + 30m位置 + 量能。",
        f"Checks: pass {passed} / fail {failed}",
        "",
        f"Daily: EMA8/21/34/55 {fmt_num(daily.get('ema8'))}/{fmt_num(daily.get('ema21'))}/{fmt_num(daily.get('ema34'))}/{fmt_num(daily.get('ema55'))}, ATR {fmt_num(daily.get('atr14'))}, RSI {fmt_num(daily.get('rsi14'), 1)}",
        f"Swing fib supports: 38/50/62 {fmt_num((daily.get('pullbackFibs') or {}).get('fib38'))}/{fmt_num((daily.get('pullbackFibs') or {}).get('fib50'))}/{fmt_num((daily.get('pullbackFibs') or {}).get('fib62'))}",
        f"Levels: 上方 {fmt_level(levels.get('nearestAbove'))} / 下方 {fmt_level(levels.get('nearestBelow'))}",
        f"5m: close {fmt_num(five.get('close'))}, VWAP {fmt_num(five.get('vwap'))}, FMA {fmt_num(five.get('fmaBottom'))}-{fmt_num(five.get('fmaTop'))}, {fmt_volume(five)}",
        f"15m: close {fmt_num(fifteen.get('close'))}, FMA {fmt_num(fifteen.get('fmaBottom'))}-{fmt_num(fifteen.get('fmaTop'))}, slope8 {fmt_num(fifteen.get('ema8Slope45m'))}, {fmt_volume(fifteen)}",
        f"45m: recent low {fmt_num(one.get('recent45mLow'))}, prior low {fmt_num(one.get('prior45mLow'))}, no-new-low {fmt_bool(one.get('noNewLow45m'))}",
        f"30m: range position {fmt_num(thirty.get('rangePositionPct'), 1)}%, FMA {fmt_num(thirty.get('fmaBottom'))}-{fmt_num(thirty.get('fmaTop'))}, {fmt_volume(thirty)}",
        "",
        f"Scenario: {first_scenario.get('branch', '—')} · trigger: {first_scenario.get('trigger', '—')} · invalid: {first_scenario.get('invalid', '—')}",
        f"Spread: {first_spread.get('status', '—')} {first_spread.get('structure', '')} · {first_spread.get('rule', '')}",
        f"Teacher action: {s['action']}",
        "Not financial advice.",
    ]
    return "\n".join(lines)


def telegram_request(token, method, payload=None, timeout=15):
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = None
    headers = {}
    if payload is not None:
        data = urllib.parse.urlencode(payload).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if payload is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8")
    parsed = json.loads(body)
    if not parsed.get("ok"):
        raise RuntimeError(f"Telegram API {method} failed: {parsed}")
    return parsed


def discover_telegram_chat_id(token):
    updates = telegram_request(token, "getUpdates", timeout=15).get("result", [])
    for update in reversed(updates):
        msg = update.get("message") or update.get("edited_message") or update.get("channel_post")
        if msg and msg.get("chat", {}).get("id") is not None:
            return str(msg["chat"]["id"])
    return None


def resolve_telegram_chat_id(token, explicit_chat_id, out_dir, auto_discover):
    if explicit_chat_id:
        return str(explicit_chat_id)
    cache = out_dir / TELEGRAM_CHAT_CACHE
    if cache.exists():
        cached = cache.read_text().strip()
        if cached:
            return cached
    if not auto_discover:
        return None
    chat_id = discover_telegram_chat_id(token)
    if chat_id:
        cache.write_text(chat_id + "\n")
    return chat_id


def send_telegram_message(token, chat_id, text):
    return telegram_request(token, "sendMessage", {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": "true",
    })


def send_telegram_message_from_config(text):
    try:
        from telegram_notifier import send_message
    except Exception as exc:
        raise RuntimeError(f"telegram_notifier unavailable: {exc}") from exc
    ok = send_message(text)
    if not ok:
        raise RuntimeError("telegram_notifier send failed; check ~/.config/ptrak/telegram.json")
    return True


def append_telegram_log(out_dir, snapshot, ok, detail):
    path = out_dir / "telegram_push_log.csv"
    first = not path.exists()
    fields = ["pushedAt", "pulledAt", "label", "last", "ok", "detail"]
    row = {
        "pushedAt": dt.datetime.now(tz=PT).isoformat(timespec="seconds"),
        "pulledAt": snapshot.get("pulledAt"),
        "label": snapshot.get("signal", {}).get("label"),
        "last": snapshot.get("signal", {}).get("last"),
        "ok": ok,
        "detail": detail,
    }
    with path.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if first:
            w.writeheader()
        w.writerow(row)


def write_csv(path, rows):
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = ["time", "open", "high", "low", "close", "volume", "ema5", "ema8", "ema13", "ema21", "ema34", "vwap"]
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k) for k in fields})


def append_history(path, snapshot):
    first = not path.exists()
    fields = [
        "pulledAt", "label", "score", "maxScore", "last", "dailyEma21",
        "fiveClose", "fiveVwap", "fiveEma21", "fifteenClose", "fifteenEma8",
        "fiveVolumeRatio", "fifteenVolumeRatio", "thirtyVolumeRatio",
        "nearestAbove", "nearestBelow", "noNewLow45m", "action",
    ]
    s = snapshot["signal"]
    five = s["summaries"].get("5m", {})
    fifteen = s["summaries"].get("15m", {})
    one = s["summaries"].get("1m", {})
    row = {
        "pulledAt": snapshot["pulledAt"],
        "label": s["label"],
        "score": s["score"],
        "maxScore": s["maxScore"],
        "last": s["last"],
        "dailyEma21": snapshot["daily"].get("ema21"),
        "fiveClose": five.get("close"),
        "fiveVwap": five.get("vwap"),
        "fiveEma21": five.get("ema21"),
        "fifteenClose": fifteen.get("close"),
        "fifteenEma8": fifteen.get("ema8"),
        "fiveVolumeRatio": five.get("volumeRatio"),
        "fifteenVolumeRatio": fifteen.get("volumeRatio"),
        "thirtyVolumeRatio": (s["summaries"].get("30m") or {}).get("volumeRatio"),
        "nearestAbove": ((s.get("levelMap") or {}).get("nearestAbove") or {}).get("px"),
        "nearestBelow": ((s.get("levelMap") or {}).get("nearestBelow") or {}).get("px"),
        "noNewLow45m": one.get("noNewLow45m"),
        "action": s["action"],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if first:
            w.writeheader()
        w.writerow(row)


def run_once(symbol, out_dir):
    out_dir.mkdir(parents=True, exist_ok=True)
    pulled_at = dt.datetime.now(tz=PT).isoformat(timespec="seconds")
    intraday = fetch_intraday(symbol)
    daily = fetch_daily_context(symbol)
    signal = evaluate_signal(intraday, daily)
    state = load_state(out_dir)
    state = annotate_level_tests(signal, state, pulled_at)
    save_state(out_dir, state)
    for interval, rows in intraday.items():
        write_csv(out_dir / f"{symbol}_{interval}.csv", rows)
    snapshot = {
        "symbol": symbol,
        "pulledAt": pulled_at,
        "dataMode": "Yahoo Finance intraday candles",
        "daily": daily,
        "signal": signal,
    }
    (out_dir / "latest_signal.json").write_text(json.dumps(snapshot, indent=2, ensure_ascii=False))
    append_history(out_dir / "signal_history.csv", snapshot)
    return snapshot


def seconds_until_close(now=None):
    now = now or dt.datetime.now(tz=PT)
    close = now.replace(hour=13, minute=5, second=0, microsecond=0)
    return (close - now).total_seconds()


def is_market_monitor_window(now=None):
    now = now or dt.datetime.now(tz=PT)
    minute = now.hour * 60 + now.minute
    return now.weekday() < 5 and (6 * 60 + 30) <= minute <= (13 * 60 + 5)


def main():
    ap = argparse.ArgumentParser(description="Pull QQQ 1m/5m/15m/30m candles and score intraday stability.")
    ap.add_argument("--symbol", default="QQQ")
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT))
    ap.add_argument("--loop", action="store_true", help="run every --poll-seconds until stopped")
    ap.add_argument("--poll-seconds", type=int, default=900)
    ap.add_argument("--until-close", action="store_true", help="stop after 13:05 PT")
    ap.add_argument("--market-hours-only", action="store_true", help="exit 0 outside the weekday 06:30-13:05 PT monitor window")
    ap.add_argument("--telegram", action="store_true", help="send each analysis snapshot to Telegram")
    ap.add_argument("--telegram-token-env", default="TELEGRAM_BOT_TOKEN")
    ap.add_argument("--telegram-chat-id-env", default="TELEGRAM_CHAT_ID")
    ap.add_argument("--telegram-chat-id")
    ap.add_argument("--telegram-auto-chat-id", action="store_true",
                    help="discover chat id from Telegram getUpdates and cache it after the user messages the bot")
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    if args.market_hours_only and not is_market_monitor_window():
        print(f"{dt.datetime.now(tz=PT).isoformat(timespec='seconds')} skip outside monitor window", flush=True)
        return
    while True:
        try:
            snap = run_once(args.symbol, out_dir)
            s = snap["signal"]
            print(f"{snap['pulledAt']} {args.symbol} {s['label']} score={s['score']}/{s['maxScore']} last={s['last']} action={s['action']}", flush=True)
            if args.telegram:
                token = os.environ.get(args.telegram_token_env)
                chat_id = args.telegram_chat_id or os.environ.get(args.telegram_chat_id_env)
                if token:
                    chat_id = resolve_telegram_chat_id(token, chat_id, out_dir, args.telegram_auto_chat_id)
                    if not chat_id:
                        raise RuntimeError("Telegram chat_id not found; send any message to the bot or set TELEGRAM_CHAT_ID")
                    send_telegram_message(token, chat_id, format_telegram_message(snap))
                    append_telegram_log(out_dir, snap, True, "sent-env")
                    print(f"{snap['pulledAt']} telegram sent chat_id={chat_id}", flush=True)
                else:
                    send_telegram_message_from_config(format_telegram_message(snap))
                    append_telegram_log(out_dir, snap, True, "sent-config")
                    print(f"{snap['pulledAt']} telegram sent via config", flush=True)
        except Exception as exc:
            print(f"{dt.datetime.now(tz=PT).isoformat(timespec='seconds')} ERROR {exc}", file=sys.stderr, flush=True)
            try:
                append_telegram_log(out_dir, snap if "snap" in locals() else {"pulledAt": None, "signal": {}}, False, str(exc))
            except Exception:
                pass
        if not args.loop:
            break
        if args.until_close and seconds_until_close() <= 0:
            print("market-close stop reached", flush=True)
            break
        sleep_for = args.poll_seconds
        if args.until_close:
            sleep_for = max(1, min(sleep_for, int(seconds_until_close())))
        time.sleep(sleep_for)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
QQQ intraday "重心稳住" monitor.

Pulls 1/5/15/30-min bars and reports across timeframes:
  - VWAP (the literal center-of-gravity)
  - distance from VWAP
  - recent-bars low vs prior low (is the floor holding?)
  - higher-lows / lower-highs count (constructive base or breakdown continuation?)
  - range contraction (volatility squeeze?)
  - EMA8/EMA21 slope (drift up/down)

Verdict per timeframe:
  STABILIZING — center holding, no new lows, range tightening, drift flat-to-up
  STILL FALLING — fresh lows, lower highs, expanding range
  UNCLEAR — mixed signals

This is technical observation, not investment advice.
"""

import warnings
warnings.filterwarnings("ignore")

import sys
import json
import datetime
import os
from pathlib import Path
from zoneinfo import ZoneInfo

import yfinance as yf

SYMBOL = "QQQ"
INTERVALS = [
    ("1m", "1d"),    # last 1 day of 1-min bars
    ("5m", "5d"),    # last 5 days of 5-min — gives prior-day VWAP context too
    ("15m", "5d"),
    ("30m", "1mo"),
]
INTERVAL_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "30m": 30}

LOG_FILE = Path(__file__).parent.parent / "output" / "qqq_intraday_log.jsonl"
ET = ZoneInfo("America/New_York")

# Telegram notifier is best-effort: if config missing or network down, monitor still prints to stdout
sys.path.insert(0, str(Path(__file__).parent))
try:
    from telegram_notifier import send_message as _tg_send
except Exception:
    _tg_send = None
try:
    from scripts.intraday_tape_sensor import market_session as _market_session
except Exception:
    try:
        from intraday_tape_sensor import market_session as _market_session
    except Exception:
        _market_session = None


def vwap(df):
    """Session VWAP — typical price weighted by volume, reset per session day."""
    tp = (df["High"] + df["Low"] + df["Close"]) / 3.0
    tpv = tp * df["Volume"]
    # group by session day so VWAP resets each day
    grp = df.groupby(df.index.date)
    return (tpv.groupby(df.index.date).cumsum() / df["Volume"].groupby(df.index.date).cumsum())


def ema(series, span):
    return series.ewm(span=span, adjust=False).mean()


def closed_dataframe(df, label, now=None):
    """Normalize to New York time and remove the provider's forming candle."""
    if df is None or df.empty:
        return df
    current = (now or datetime.datetime.now(ET)).astimezone(ET)
    normalized = df.copy()
    if normalized.index.tz is None:
        normalized.index = normalized.index.tz_localize(ET)
    else:
        normalized.index = normalized.index.tz_convert(ET)
    started = normalized.index[-1].to_pydatetime()
    ended = started + datetime.timedelta(minutes=INTERVAL_MINUTES[label])
    if current < ended + datetime.timedelta(seconds=30):
        normalized = normalized.iloc[:-1]
    return normalized


def analyze_timeframe(df, label, now=None):
    """Return dict of stability metrics for one timeframe."""
    if df is None or len(df) < 10:
        return {"label": label, "ok": False, "reason": f"not enough bars ({len(df) if df is not None else 0})"}

    current = (now or datetime.datetime.now(ET)).astimezone(ET)
    today = df.index[-1].date()
    if today != current.date():
        return {"label": label, "ok": False, "reason": "latest closed bar is not from the current ET session"}
    ended = df.index[-1].to_pydatetime() + datetime.timedelta(minutes=INTERVAL_MINUTES[label])
    age_minutes = (current - ended).total_seconds() / 60.0
    max_age = INTERVAL_MINUTES[label] * 2 + 5
    if age_minutes < -2 or age_minutes > max_age:
        return {"label": label, "ok": False, "reason": f"latest closed bar stale/future ({age_minutes:.1f}m)"}
    today_df = df[df.index.date == today]
    if len(today_df) < 3:
        return {"label": label, "ok": False, "reason": "not enough today bars"}

    closes = today_df["Close"]
    highs = today_df["High"]
    lows = today_df["Low"]
    last = closes.iloc[-1]
    day_high = highs.max()
    day_low = lows.min()
    day_open = today_df["Open"].iloc[0]

    # VWAP today
    vw = vwap(df)
    today_vw = vw[vw.index.date == today]
    vwap_now = today_vw.iloc[-1] if len(today_vw) else None
    above_vwap = bool(vwap_now is not None and last > vwap_now)
    dist_vwap_pct = ((last - vwap_now) / vwap_now * 100) if vwap_now else None

    # Recent N bars (the most recent "leg")
    n = min(8, len(today_df))
    recent = today_df.tail(n)
    recent_low = recent["Low"].min()
    recent_high = recent["High"].max()
    bars_since_day_low = (today_df["Low"] == day_low).iloc[::-1].argmax() if day_low in today_df["Low"].values else None

    # is day_low getting older? (positive = floor holding)
    day_low_idx = today_df["Low"].idxmin()
    bars_since_low = len(today_df) - today_df.index.get_loc(day_low_idx) - 1

    # higher-lows / lower-highs over last n bars
    lows_seq = recent["Low"].values
    highs_seq = recent["High"].values
    hl_count = sum(1 for i in range(1, len(lows_seq)) if lows_seq[i] > lows_seq[i - 1])
    lh_count = sum(1 for i in range(1, len(highs_seq)) if highs_seq[i] < highs_seq[i - 1])
    ll_count = sum(1 for i in range(1, len(lows_seq)) if lows_seq[i] < lows_seq[i - 1])

    # range contraction: avg true range last n/2 vs prior n/2
    tr = (today_df["High"] - today_df["Low"])
    half = max(2, len(tr) // 4)
    range_now = tr.tail(half).mean()
    range_prev = tr.iloc[-(2 * half):-half].mean() if len(tr) >= 2 * half else tr.head(half).mean()
    contracting = bool(range_now < range_prev * 0.85)
    expanding = bool(range_now > range_prev * 1.15)

    # EMA drift
    ema8 = ema(closes, 8).iloc[-1]
    ema21 = ema(closes, 21).iloc[-1] if len(closes) >= 21 else None
    drift_up = bool(ema21 is not None and ema8 > ema21)
    ema_dist_pct = ((ema8 - ema21) / ema21 * 100) if ema21 else None

    # Verdict logic
    stabilizing_signals = 0
    falling_signals = 0
    if bars_since_low >= 3: stabilizing_signals += 1   # floor is N bars old
    if hl_count >= lh_count and hl_count > ll_count: stabilizing_signals += 1  # higher-lows winning
    if contracting: stabilizing_signals += 1
    if drift_up: stabilizing_signals += 1
    if above_vwap: stabilizing_signals += 1

    if bars_since_low == 0: falling_signals += 2   # fresh new low
    if ll_count > hl_count: falling_signals += 1
    if expanding: falling_signals += 1
    if not above_vwap and ema21 and ema8 < ema21: falling_signals += 1

    if stabilizing_signals >= 4 and falling_signals <= 1:
        verdict = "STABILIZING ✓"
    elif falling_signals >= 3:
        verdict = "STILL FALLING ✗"
    else:
        verdict = "UNCLEAR"

    return {
        "label": label, "ok": True,
        "last": round(float(last), 2),
        "day_open": round(float(day_open), 2),
        "day_high": round(float(day_high), 2),
        "day_low": round(float(day_low), 2),
        "day_range_pct": round(float((day_high - day_low) / day_open * 100), 2),
        "vwap": round(float(vwap_now), 2) if vwap_now is not None else None,
        "above_vwap": above_vwap,
        "dist_vwap_pct": round(float(dist_vwap_pct), 2) if dist_vwap_pct is not None else None,
        "bars_since_day_low": int(bars_since_low),
        "higher_lows": hl_count, "lower_highs": lh_count, "lower_lows": ll_count, "of_bars": n - 1,
        "range_contracting": contracting, "range_expanding": expanding,
        "range_now": round(float(range_now), 3), "range_prev": round(float(range_prev), 3),
        "drift_up": drift_up,
        "ema_dist_pct": round(float(ema_dist_pct), 2) if ema_dist_pct is not None else None,
        "verdict": verdict,
        "stabilizing_signals": stabilizing_signals,
        "falling_signals": falling_signals,
    }


def fmt_section(a):
    if not a.get("ok"):
        return f"  {a['label']:>4}  · {a['reason']}"
    arrow_vw = "↑" if a["above_vwap"] else "↓"
    arrow_dr = "↑" if a["drift_up"] else "↓"
    rng_flag = " ⊖收缩" if a["range_contracting"] else (" ⊕扩张" if a["range_expanding"] else "")
    vwap_str = f"${a['vwap']:>7.2f} {arrow_vw}{a['dist_vwap_pct']:+.2f}%" if a["vwap"] is not None else "—"
    ema_str = f"{arrow_dr} EMA21 ({a['ema_dist_pct']:+.2f}%)" if a["ema_dist_pct"] is not None else "样本不足"
    return (
        f"  {a['label']:>4}  {a['verdict']:<16}  "
        f"价 ${a['last']:>7.2f}  "
        f"VWAP {vwap_str}  "
        f"日内 ${a['day_low']:.2f}–${a['day_high']:.2f} ({a['day_range_pct']:.2f}%)  "
        f"距日内低点 {a['bars_since_day_low']} 根  "
        f"近 {a['of_bars']} 根 HL/LH/LL {a['higher_lows']}/{a['lower_highs']}/{a['lower_lows']}{rng_flag}  "
        f"EMA8 {ema_str}"
    )


def _esc(s):
    """HTML-escape for Telegram parse_mode=HTML — needed because tickers / prices are safe but defensive."""
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def fmt_telegram(now_et, is_open, mins_to_close, results, overall, note):
    """
    Build a Telegram-friendly HTML message:
      - emoji + verdict at the top (scannable on phone lock screen)
      - prices in <code> for monospace alignment
      - 1 line per timeframe, comparable to terminal output but tighter

    The "重心" interpretation we report (and what each metric means):
      - VWAP = volume-weighted average price reset each session day. The
        literal "center of gravity" — where most of today's volume traded.
      - "重心稳住" = VWAP stops drifting down AND price reclaims/holds it
        AND 15m/30m bars stop making fresh lows AND range contracts.
      - "STILL FALLING" = price below VWAP, fresh new lows recent, range
        expanding, EMA8 below EMA21.
    """
    emoji = {"STABILIZING ✓": "🟢", "STILL FALLING ✗": "🔴", "UNCLEAR": "🟡"}.get(overall, "⚪")
    lines = []
    lines.append(f"{emoji} <b>QQQ 重心 · {overall}</b>")
    lines.append(f"<i>{_esc(now_et.strftime('%H:%M ET'))}{' · 距收盘 '+str(mins_to_close)+' 分钟' if mins_to_close is not None else ''}{'' if is_open else ' · 休市'}</i>")
    lines.append("")

    # current snapshot (from first available timeframe)
    primary = next((r for r in results if r.get("ok")), None)
    if primary:
        vwap_arrow = "↑" if primary["above_vwap"] else "↓"
        lines.append(
            f"<b>QQQ</b> <code>${primary['last']:.2f}</code>  "
            f"vs VWAP <code>${primary['vwap']:.2f}</code> {vwap_arrow}<code>{primary['dist_vwap_pct']:+.2f}%</code>"
        )
        lines.append(
            f"日内: <code>${primary['day_low']:.2f}</code>–<code>${primary['day_high']:.2f}</code> ({primary['day_range_pct']:.2f}%)"
        )
        lines.append("")

    # per-timeframe one-liners
    for r in results:
        if not r.get("ok"):
            lines.append(f"<code>{r['label']:>4}</code> · {_esc(r['reason'])}")
            continue
        rng = "⊖收缩" if r["range_contracting"] else ("⊕扩张" if r["range_expanding"] else "—")
        floor_age = f"距低点 {r['bars_since_day_low']}根"
        lines.append(
            f"<code>{r['label']:>4}</code> "
            f"{r['verdict'].replace(' ✓','').replace(' ✗','')}  "
            f"{floor_age} · HL/LH/LL <code>{r['higher_lows']}/{r['lower_highs']}/{r['lower_lows']}</code> · {rng}"
        )

    lines.append("")
    lines.append(f"<i>{_esc(note)}</i>")
    lines.append("")
    lines.append("⚠ 技术观察 · 非投资建议")
    return "\n".join(lines)


def overall_verdict(results):
    """Aggregate across timeframes."""
    tally = {"STABILIZING ✓": 0, "STILL FALLING ✗": 0, "UNCLEAR": 0}
    for r in results:
        if r.get("ok"):
            v = r["verdict"]
            # weight: short timeframes count less than longer ones for "real" stabilization
            weight = {"1m": 1, "5m": 2, "15m": 3, "30m": 4}.get(r["label"], 1)
            tally[v] += weight
    if tally["STABILIZING ✓"] >= tally["STILL FALLING ✗"] + 3:
        return ("STABILIZING ✓", "多时段重心已显现稳住迹象 — 仍是技术观察，非进场信号")
    if tally["STILL FALLING ✗"] > tally["STABILIZING ✓"]:
        return ("STILL FALLING ✗", "重心尚未止跌 — 继续等待")
    return ("UNCLEAR", "信号混杂 — 短中期未对齐，继续观察")


def main():
    now_et = datetime.datetime.now(ET)
    session = _market_session(now_et.date()) if _market_session else {
        "open": now_et.weekday() < 5,
        "openAt": now_et.replace(hour=9, minute=30, second=0, microsecond=0).isoformat(),
        "closeAt": now_et.replace(hour=16, minute=0, second=0, microsecond=0).isoformat(),
    }
    market_open = datetime.datetime.fromisoformat(session["openAt"]) if session.get("openAt") else None
    market_close = datetime.datetime.fromisoformat(session["closeAt"]) if session.get("closeAt") else None
    is_open = bool(session.get("open") and market_open <= now_et <= market_close)
    mins_to_close = max(0, int((market_close - now_et).total_seconds() / 60)) if is_open else None

    print(f"\n— QQQ 重心监控 · {now_et.strftime('%Y-%m-%d %H:%M ET')} · 市场 {'开盘' if is_open else '休市'}", end="")
    if mins_to_close is not None:
        print(f" · 距收盘 {mins_to_close} 分钟", end="")
    print(" —")

    t = yf.Ticker(SYMBOL)
    results = []
    for interval, period in INTERVALS:
        try:
            df = t.history(period=period, interval=interval, prepost=False)
            df = closed_dataframe(df, interval, now=now_et)
            r = analyze_timeframe(df, interval, now=now_et)
        except Exception as e:
            r = {"label": interval, "ok": False, "reason": f"fetch error: {type(e).__name__}: {e}"}
        results.append(r)
        print(fmt_section(r))

    overall, note = overall_verdict(results)
    by_label = {row.get("label"): row for row in results}
    evidence_fresh = all((by_label.get(label) or {}).get("ok") for label in ("5m", "15m"))
    if not evidence_fresh:
        overall, note = "UNCLEAR", "5m/15m current-session closed-bar evidence missing or stale"
    print(f"\n  综合：{overall}  ·  {note}")

    LOG_FILE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    LOG_FILE.parent.chmod(0o700)
    log_row = {
        "ts": now_et.isoformat(timespec="seconds"),
        "market_open": is_open,
        "mins_to_close": mins_to_close,
        "overall": overall,
        "closed_bar_evidence_fresh": evidence_fresh,
        "by_tf": [{k: r.get(k) for k in ("label", "verdict", "last", "vwap", "above_vwap", "bars_since_day_low", "higher_lows", "lower_highs", "lower_lows", "range_contracting", "drift_up")} for r in results],
    }
    fd = os.open(LOG_FILE, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as f:
        f.write(json.dumps(log_row, ensure_ascii=False, allow_nan=False) + "\n")
    LOG_FILE.chmod(0o600)

    print(f"\n  · 完整快照写入 {LOG_FILE.name}\n")
    print("  ⚠ 技术观察 · 非投资建议 · 重心稳住 ≠ 必须进场，仍需结合你自己的计划与仓位规则。")

    # Telegram push:
    #   - market open      → push every tick (intraday monitoring)
    #   - market just closed (within 20 min of 16:00 ET) → push closing tick + daily summary
    #   - market closed for hours/weekend → silent (launchd will still fire 24/7; we just don't spam)
    just_closed = bool(
        session.get("open") and market_close and not is_open
        and 0 <= (now_et - market_close).total_seconds() < 20 * 60
    )
    push_telegram = (is_open or just_closed) and evidence_fresh and _tg_send is not None
    if push_telegram:
        try:
            tg_msg = fmt_telegram(now_et, is_open, mins_to_close, results, overall, note)
            if just_closed:
                tg_msg += "\n\n<b>📊 收盘</b> · 当日监控结束 · 周一开盘再见"
            ok = _tg_send(tg_msg, parse_mode="HTML")
            print(f"\n  · Telegram: {'已发送 ✓' if ok else '发送失败（见 stderr，调度继续）'}")
        except Exception as e:
            print(f"\n  · Telegram 推送异常: {type(e).__name__}: {e}", file=sys.stderr)
    elif _tg_send is not None:
        print(f"\n  · Telegram: 市场休市，不推送（节约你的通知）")
    else:
        print(f"\n  · Telegram: 通知器未配置（仅写日志）")


if __name__ == "__main__":
    main()

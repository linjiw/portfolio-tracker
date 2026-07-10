#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Daily portfolio brief → Telegram.

Sends the dashboard's 今日要点 verdict ("The Morning Front Page") as a Telegram
message: attention items per holding, top movers, KPIs, QQQ weather + next
triggers. The verdict logic MIRRORS insightBanner() in generate.py — if you
change one, change the other.

Data source: the freshly generated output/portfolio_dashboard.html payload
(`const DATA = {...};`), so this never re-implements the P&L engine. With
--sync it asks the staged latest-price refresher to build and independently
verify one Yahoo mark-to-market candidate before atomically publishing it.

Usage:
    python3 scripts/daily_portfolio_brief.py                 # print to stdout
    python3 scripts/daily_portfolio_brief.py --sync          # refresh first
    python3 scripts/daily_portfolio_brief.py --sync --telegram   # the launchd job

Telegram credentials: ~/.config/ptrak/telegram.json via scripts/telegram_notifier.py
(same channel as the market sentinel). Failure to send never breaks the sync.
"""
import argparse
import datetime as dt
import html
import json
import math
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DASH = ROOT / "output" / "portfolio_dashboard.html"
sys.path.insert(0, str(ROOT / "scripts"))
from dashboard_payload import read_dashboard_payload as _read_dashboard_payload  # noqa: E402


def refresh_dashboard_for_brief(input_dir, portfolio=None, history=None,
                                no_fetch=False, timeout_seconds=1800):
    """Build one verified MTM candidate without publishing an intermediate mode."""
    common = ["--input-dir", str(input_dir)]
    if portfolio:
        common += ["--portfolio", str(portfolio)]
    if history:
        common += ["--history", str(history)]

    price_cmd = [
        sys.executable,
        str(ROOT / "scripts" / "refresh_latest_prices.py"),
        *common,
        "--momentum-strategy", "off",
        "--trigger", "daily-brief",
    ]
    if no_fetch:
        price_cmd.append("--no-fetch")
    try:
        price_run = subprocess.run(price_cmd, cwd=ROOT, timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        return False, "mark-to-market refresh timed out"
    if price_run.returncode != 0:
        return False, "mark-to-market refresh failed"
    return True, "ok"


def load_payload():
    try:
        return _read_dashboard_payload(DASH)
    except (OSError, ValueError) as exc:
        raise RuntimeError(
            f"payload not found in {DASH} — run python3 sync.py first") from exc


def fmt_money(v, sign=False):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "—"
    if not math.isfinite(v):
        return "—"
    s = "+" if (sign and v >= 0) else ("-" if v < 0 else "")
    return f"{s}${abs(v):,.0f}"


def fmt_pct(v, dec=2):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "—"
    return "—" if not math.isfinite(v) else f"{v:+.{dec}f}%"


def _weekday_lag(value, today):
    try:
        start = dt.date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None
    if start > today:
        return -1
    lag = 0
    cursor = start + dt.timedelta(days=1)
    while cursor <= today:
        lag += cursor.weekday() < 5
        cursor += dt.timedelta(days=1)
    return lag


def build_brief(data, *, refresh_warning=None, now=None):
    S = data["summary"]
    stocks = data.get("stocks", [])
    held = [s for s in stocks if s.get("held")]
    q = data.get("qqqTqqq") or {}

    # ---- attention buckets: EXACT mirror of insightBanner() in generate.py ----
    big_down = [s for s in held if s.get("dayChangePct") is not None and s["dayChangePct"] < -3]
    deep_loss = [s for s in held if s.get("unrealPct") is not None and s["unrealPct"] < -15]
    tech_bear = [s for s in held if (s.get("fib") or {}).get("now", {}).get("state") == "down"]
    attn = len(big_down) + len(deep_loss) + len(tech_bear)
    contrib = ((data.get("risk") or {}).get("contrib") or [])
    top_risk = contrib[0]["sym"] if contrib else None

    pr, sp = S.get("curReturn"), S.get("spReturn")
    alpha = (pr - sp) if (pr is not None and sp is not None) else None

    if attn == 0:
        ico, verdict = "✅", f"今日无紧急关注 · 区间收益 {fmt_pct(pr)}"
    else:
        ico = "⚠️" if attn <= 2 else "🔴"
        parts = []
        if big_down:
            parts.append(f"{len(big_down)} 只今日大跌")
        if deep_loss:
            parts.append(f"{len(deep_loss)} 只深度亏损")
        if tech_bear:
            parts.append(f"{len(tech_bear)} 只技术转空")
        verdict = f"{attn} 件需要看" + (f" · 优先 {top_risk}" if top_risk else "") + " · " + " · ".join(parts)

    price_as_of = S.get("priceAsOf") or ((S.get("dateRange") or [None, None])[-1]) or "unknown"
    lines = [
        f"{ico} <b>组合今日要点 · {html.escape(str(price_as_of))}</b>",
        html.escape(verdict),
        "",
        f"净值 <code>{fmt_money(S.get('marketValue'))}</code> · 未实现 <code>{fmt_money(S.get('unrealized'), sign=True)}</code>"
        f" · 区间 <code>{fmt_pct(pr)}</code>"
        + (f" · {'跑赢' if alpha >= 0 else '跑输'}标普 <code>{alpha:+.2f}pp</code>" if alpha is not None else ""),
    ]

    freshness_issues = []
    if S.get("priceMode") != "mark-to-market":
        freshness_issues.append("dashboard is not mark-to-market")
    if S.get("fetchOK") is False or S.get("fetchStale"):
        freshness_issues.append(str(S.get("fetchReason") or "price fetch is stale"))
    if S.get("stalePriceSymbols"):
        freshness_issues.append(f"{len(S['stalePriceSymbols'])} held symbol(s) stale")
    if S.get("missingPriceSymbols"):
        freshness_issues.append(f"{len(S['missingPriceSymbols'])} held symbol(s) missing")
    today = (now or dt.datetime.now().astimezone()).date()
    lag = _weekday_lag(price_as_of, today)
    if lag is None or lag < 0 or lag > 2:
        freshness_issues.append(f"dashboard price date is not current ({price_as_of})")
    if refresh_warning:
        freshness_issues.insert(0, str(refresh_warning))

    movers = sorted((s for s in held if s.get("dayChangePct") is not None),
                    key=lambda s: -abs(s["dayChangePct"]))[:3]
    if movers:
        lines.append("今日先看: " + " · ".join(
            f"<b>{html.escape(s['sym'])}</b> {fmt_pct(s['dayChangePct'])}" for s in movers))

    def bucket(label, items, key, dec=1):
        if not items:
            return
        tops = sorted(items, key=lambda s: s.get(key) or 0)[:5]
        lines.append(label + ": " + " · ".join(
            f"{html.escape(s['sym'])} {fmt_pct(s.get(key), dec)}" for s in tops))

    if attn:
        lines.append("")
        bucket("📉 今日大跌", big_down, "dayChangePct")
        bucket("🕳 深度亏损", deep_loss, "unrealPct", 0)
        if tech_bear:
            lines.append("🐻 技术转空: " + " · ".join(html.escape(s["sym"]) for s in tech_bear[:6]))

    if q.get("available"):
        st, dp_, tr = q.get("state") or {}, q.get("decisionPanel") or {}, q.get("trailing") or {}
        nt = {t.get("name"): t for t in (q.get("nextTriggers") or [])}
        e8, e21 = nt.get("EMA8 拿回"), nt.get("EMA21 期权")
        lines += ["", f"🌤 QQQ <b>{html.escape(st.get('label', '—'))}</b> · {html.escape(dp_.get('headline') or st.get('action') or '')}"]
        trig = []
        if e8:
            trig.append(f"EMA8 <code>{e8['level']:.0f}</code> ({e8['distancePct']:+.1f}%)")
        if e21:
            trig.append(f"EMA21 <code>{e21['level']:.0f}</code> ({e21['distancePct']:+.1f}%)")
        if tr.get("qqqTrail3Atr"):
            trig.append(f"3ATR 防守 <code>{tr['qqqTrail3Atr']:.0f}</code>")
        if trig:
            lines.append("触发: " + " / ".join(trig))

    if freshness_issues:
        lines += ["", "⛔ <b>DATA BLOCK</b>: " + html.escape("; ".join(dict.fromkeys(freshness_issues)))]

    lines += ["", "<i>技术参考，非投资建议 · 详情见 dashboard</i>"]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="Send the dashboard's 今日要点 verdict to Telegram.")
    ap.add_argument("--sync", action="store_true",
                    help="stage, verify, and atomically publish a Yahoo mark-to-market dashboard")
    ap.add_argument("--input-dir", default=str(Path.home() / "Downloads"))
    ap.add_argument("--portfolio", help="explicit Portfolio Positions CSV")
    ap.add_argument("--history", help="explicit Account History CSV (added to cumulative local history)")
    ap.add_argument("--no-fetch", action="store_true", help="reuse cached prices for both refresh steps")
    ap.add_argument("--telegram", action="store_true", help="send via telegram_notifier (default: print only)")
    args = ap.parse_args()

    refresh_warning = None
    if args.sync:
        ok, reason = refresh_dashboard_for_brief(
            args.input_dir, portfolio=args.portfolio, history=args.history, no_fetch=args.no_fetch)
        if not ok:
            # stale dashboard is better than silence — brief from the last good build, flagged inside
            print(f"{reason} — briefing from the last generated dashboard", file=sys.stderr)
            refresh_warning = reason + "; using last generated dashboard"

    msg = build_brief(load_payload(), refresh_warning=refresh_warning)
    print(msg)
    if args.telegram:
        try:
            from telegram_notifier import send_message
            ok = send_message(msg)   # defaults to parse_mode=HTML; never raises
        except Exception as e:
            print(f"telegram error: {e}", file=sys.stderr)
            ok = False
        print(f"telegram={'sent' if ok else 'failed'}")


if __name__ == "__main__":
    main()

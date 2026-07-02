#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Daily portfolio brief → Telegram.

Sends the dashboard's 今日要点 verdict ("The Morning Front Page") as a Telegram
message: attention items per holding, top movers, KPIs, QQQ weather + next
triggers. The verdict logic MIRRORS insightBanner() in generate.py — if you
change one, change the other.

Data source: the freshly generated output/portfolio_dashboard.html payload
(`const DATA = {...};`), so this never re-implements the P&L engine. With
--sync it runs `python3 sync.py` first (fetch + verify gate) so the brief
reflects today's close.

Usage:
    python3 scripts/daily_portfolio_brief.py                 # print to stdout
    python3 scripts/daily_portfolio_brief.py --sync          # refresh first
    python3 scripts/daily_portfolio_brief.py --sync --telegram   # the launchd job

Telegram credentials: ~/.config/ptrak/telegram.json via scripts/telegram_notifier.py
(same channel as the market sentinel). Failure to send never breaks the sync.
"""
import argparse
import html
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DASH = ROOT / "output" / "portfolio_dashboard.html"
sys.path.insert(0, str(ROOT / "scripts"))


def load_payload():
    text = DASH.read_text(encoding="utf-8")
    m = re.search(r"const DATA = (\{.*?\});\n", text, re.S)
    if not m:
        raise RuntimeError(f"payload not found in {DASH} — run python3 sync.py first")
    return json.loads(m.group(1))


def fmt_money(v, sign=False):
    if v is None:
        return "—"
    s = "+" if (sign and v >= 0) else ("-" if v < 0 else "")
    return f"{s}${abs(v):,.0f}"


def fmt_pct(v, dec=2):
    return "—" if v is None else f"{v:+.{dec}f}%"


def build_brief(data):
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

    lines = [
        f"{ico} <b>组合今日要点 · {S.get('priceAsOf', S['dateRange'][1])}</b>",
        html.escape(verdict),
        "",
        f"净值 <code>{fmt_money(S.get('marketValue'))}</code> · 未实现 <code>{fmt_money(S.get('unrealized'), sign=True)}</code>"
        f" · 区间 <code>{fmt_pct(pr)}</code>"
        + (f" · {'跑赢' if alpha >= 0 else '跑输'}标普 <code>{alpha:+.2f}pp</code>" if alpha is not None else ""),
    ]

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

    if S.get("fetchOK") is False or S.get("fetchStale"):
        lines += ["", f"⏱ 注意：价格为缓存（{html.escape(str(S.get('fetchReason') or '非实时'))}），数字可能滞后"]

    lines += ["", "<i>技术参考，非投资建议 · 详情见 dashboard</i>"]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="Send the dashboard's 今日要点 verdict to Telegram.")
    ap.add_argument("--sync", action="store_true", help="run python3 sync.py first (fetch + verify gate)")
    ap.add_argument("--telegram", action="store_true", help="send via telegram_notifier (default: print only)")
    args = ap.parse_args()

    if args.sync:
        r = subprocess.run([sys.executable, str(ROOT / "sync.py")], cwd=ROOT)
        if r.returncode != 0:
            # stale dashboard is better than silence — brief from the last good build, flagged inside
            print("sync failed — briefing from the last generated dashboard", file=sys.stderr)

    msg = build_brief(load_payload())
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

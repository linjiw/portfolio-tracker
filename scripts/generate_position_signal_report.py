#!/usr/bin/env python3
"""Generate a compact signal report for every currently held position."""
import argparse
import csv
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DASHBOARD = ROOT / "output" / "portfolio_dashboard.html"
DEFAULT_MD = ROOT / "output" / "position_signal_report.md"
DEFAULT_CSV = ROOT / "output" / "position_signal_report.csv"
DEFAULT_SENTINEL = ROOT / "output" / "market_sentinel" / "latest_snapshot.json"


def read_dashboard_payload(path=DEFAULT_DASHBOARD):
    text = Path(path).read_text(encoding="utf-8")
    match = re.search(r"const DATA = (\{.*?\});", text, re.S)
    if not match:
        raise RuntimeError(f"DATA payload not found in {path}")
    return json.loads(match.group(1))


def read_json(path):
    path = Path(path)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def money(value):
    try:
        return f"${float(value):,.2f}"
    except Exception:
        return "—"


def pct(value):
    try:
        return f"{float(value):+.2f}%"
    except Exception:
        return "—"


def plain_pct(value):
    try:
        return f"{float(value):.1f}%"
    except Exception:
        return "—"


def state_label(stock):
    return ((stock.get("fib") or {}).get("now") or {}).get("label") or "—"


def state_code(stock):
    return ((stock.get("fib") or {}).get("now") or {}).get("state") or "unknown"


def rsi(stock):
    return ((stock.get("fib") or {}).get("now") or {}).get("rsi")


def risk_map(payload):
    return {row.get("sym"): row for row in ((payload.get("risk") or {}).get("contrib") or [])}


def spmo_signal_text(spmo):
    if not spmo.get("available"):
        return None
    if spmo.get("label") == "BLOCK":
        return "BLOCK add: QQQ gate closed; hold existing only with moving stop"
    if spmo.get("label") == "WATCH":
        return "WATCH: wait SPMO reclaim/relative-strength repair"
    if spmo.get("label") == "ALLOW":
        return "ALLOW tranche only after buy-stop/reclaim"
    return None


def classify_position(stock, payload, sentinel):
    sym = stock.get("sym")
    code = state_code(stock)
    day = stock.get("dayChangePct")
    theme = stock.get("theme") or "—"
    qqq = (payload.get("qqqTqqq") or {}).get("latest") or {}
    qqq_close = qqq.get("qqq")
    qqq_ema21 = qqq.get("ema21")
    decision = ((sentinel.get("agents") or {}).get("decision") or {})
    spmo = ((sentinel.get("agents") or {}).get("spmoMomentum") or {})

    notes = []
    if "半导体" in theme and day is not None and day <= -5:
        notes.append("sector stress")

    if sym == "SPMO":
        text = spmo_signal_text(spmo)
        if text:
            return text, notes

    if sym == "QQQ" and qqq_close is not None and qqq_ema21 is not None and qqq_close < qqq_ema21:
        return "BLOCK new offense: below EMA21; only reclaim-watch", notes

    if sym == "TQQQ":
        if decision.get("label") == "BLOCK" or (qqq_close is not None and qqq_ema21 is not None and qqq_close < qqq_ema21):
            return "BLOCK add: tactical only after QQQ reclaim", notes
        return "WATCH: tactical only, use tight invalidation", notes

    if code == "down":
        return "BLOCK add: downtrend", notes
    if day is not None and day <= -8:
        return "WATCH: uptrend hit hard; wait repair", notes
    if code == "up":
        return "HOLD: trend intact, use moving stop", notes
    if code in ("mixed", "range"):
        return "WATCH: transition/range, no chase", notes
    return "WATCH: insufficient clean trend", notes


def build_rows(payload, sentinel):
    summary = payload.get("summary") or {}
    total = summary.get("marketValue") or 0
    risks = risk_map(payload)
    rows = []
    for stock in payload.get("stocks") or []:
        if not stock.get("held"):
            continue
        value = stock.get("value") or 0
        weight = value / total * 100 if total else 0
        risk = risks.get(stock.get("sym"), {})
        signal, notes = classify_position(stock, payload, sentinel)
        rows.append({
            "Symbol": stock.get("sym"),
            "Theme": stock.get("theme") or "—",
            "Price": stock.get("curPrice"),
            "Day": stock.get("dayChangePct"),
            "Weight": round(weight, 2),
            "Unreal": stock.get("unrealPct"),
            "Risk": risk.get("riskPct", 0.0),
            "Fib": state_label(stock),
            "RSI": rsi(stock),
            "Signal": signal,
            "Notes": "; ".join(notes),
        })
    rows.sort(key=lambda row: row["Weight"], reverse=True)
    return rows


def write_markdown(rows, payload, sentinel, path=DEFAULT_MD):
    summary = payload.get("summary") or {}
    decision = ((sentinel.get("agents") or {}).get("decision") or {})
    lines = [
        "# Position Signal Report",
        "",
        f"Data: dashboard {summary.get('priceMode')} as of {summary.get('priceAsOf')} · generated {summary.get('generatedAt')} · market value ${summary.get('marketValue', 0):,.2f}",
        f"Market gate: **{decision.get('label', '—')}** · {decision.get('primaryAction', '—')}",
        "",
        "| Symbol | Theme | Price | Day | Weight | Unreal | Risk | Fib | RSI | Signal |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in rows:
        note = f" | {row['Notes']}" if row["Notes"] else ""
        lines.append(
            f"| {row['Symbol']} | {row['Theme']} | {money(row['Price'])} | {pct(row['Day'])} | "
            f"{plain_pct(row['Weight'])} | {pct(row['Unreal'])} | {plain_pct(row['Risk'])} | "
            f"{row['Fib']} | {plain_pct(row['RSI'])} | {row['Signal']}{note} |"
        )
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_csv(rows, path=DEFAULT_CSV):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with Path(path).open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else [])
        if rows:
            writer.writeheader()
            writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(description="Generate current held-position signal report.")
    parser.add_argument("--dashboard", default=str(DEFAULT_DASHBOARD))
    parser.add_argument("--sentinel", default=str(DEFAULT_SENTINEL))
    parser.add_argument("--md", default=str(DEFAULT_MD))
    parser.add_argument("--csv", default=str(DEFAULT_CSV))
    args = parser.parse_args()

    payload = read_dashboard_payload(args.dashboard)
    sentinel = read_json(args.sentinel)
    rows = build_rows(payload, sentinel)
    write_markdown(rows, payload, sentinel, args.md)
    write_csv(rows, args.csv)
    print(f"wrote {len(rows)} held-position signals")
    print(Path(args.md).resolve())
    print(Path(args.csv).resolve())


if __name__ == "__main__":
    main()

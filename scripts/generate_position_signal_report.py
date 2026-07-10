#!/usr/bin/env python3
"""Generate a compact signal report for every currently held position."""
import argparse
import json
import math
from pathlib import Path

try:
    from scripts.artifact_io import atomic_write_csv, atomic_write_text
    from scripts.dashboard_payload import read_dashboard_payload as _read_dashboard_payload
except ImportError:
    from artifact_io import atomic_write_csv, atomic_write_text
    from dashboard_payload import read_dashboard_payload as _read_dashboard_payload


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DASHBOARD = ROOT / "output" / "portfolio_dashboard.html"
DEFAULT_MD = ROOT / "output" / "position_signal_report.md"
DEFAULT_CSV = ROOT / "output" / "position_signal_report.csv"
DEFAULT_SENTINEL = ROOT / "output" / "market_sentinel" / "latest_snapshot.json"


def read_dashboard_payload(path=DEFAULT_DASHBOARD):
    return _read_dashboard_payload(path)


def read_json(path):
    path = Path(path)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def money(value):
    try:
        value = float(value)
        return f"${value:,.2f}" if math.isfinite(value) else "—"
    except (TypeError, ValueError):
        return "—"


def pct(value):
    try:
        value = float(value)
        return f"{value:+.2f}%" if math.isfinite(value) else "—"
    except (TypeError, ValueError):
        return "—"


def plain_pct(value):
    try:
        value = float(value)
        return f"{value:.1f}%" if math.isfinite(value) else "—"
    except (TypeError, ValueError):
        return "—"


def md_cell(value):
    return str(value if value is not None else "—").replace("|", "/").replace("\n", " ").replace("\r", " ")


def dashboard_data_issues(payload):
    summary = payload.get("summary") or {}
    issues = []
    if summary.get("priceMode") != "mark-to-market":
        issues.append("dashboard_not_mark_to_market")
    if not summary.get("priceAsOf"):
        issues.append("missing_dashboard_price_date")
    if summary.get("fetchStale") or summary.get("stalePriceSymbols"):
        issues.append("stale_held_prices")
    if summary.get("missingPriceSymbols"):
        issues.append("missing_held_prices")
    try:
        market_value = float(summary.get("marketValue"))
        if not math.isfinite(market_value) or market_value <= 0:
            issues.append("invalid_equity_market_value")
    except (TypeError, ValueError):
        issues.append("invalid_equity_market_value")
    return issues


def sentinel_is_aligned(payload, sentinel):
    dashboard_as_of = (payload.get("summary") or {}).get("priceAsOf")
    sentinel_as_of = (sentinel.get("dataFreshness") or {}).get("dashboardPriceAsOf")
    return bool(dashboard_as_of and dashboard_as_of == sentinel_as_of)


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
    data_issues = dashboard_data_issues(payload)
    if data_issues:
        return "BLOCK_DATA: refresh and verify dashboard before using signals", data_issues
    if "半导体" in theme and day is not None and day <= -5:
        notes.append("sector stress")

    if sym == "SPMO":
        text = spmo_signal_text(spmo) if sentinel_is_aligned(payload, sentinel) else None
        if text:
            return text, notes
        if not sentinel_is_aligned(payload, sentinel):
            return "WATCH_DATA: SPMO sentinel is missing or not aligned", ["sentinel_not_aligned"]

    if sym == "QQQ" and qqq_close is not None and qqq_ema21 is not None and qqq_close < qqq_ema21:
        return "BLOCK new offense: below EMA21; only reclaim-watch", notes

    if sym == "TQQQ":
        if ((sentinel_is_aligned(payload, sentinel) and decision.get("label") == "BLOCK")
                or (qqq_close is not None and qqq_ema21 is not None and qqq_close < qqq_ema21)):
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
    try:
        total = float(summary.get("marketValue") or 0)
        total = total if math.isfinite(total) and total > 0 else 0.0
    except (TypeError, ValueError):
        total = 0.0
    risks = risk_map(payload)
    data_issues = dashboard_data_issues(payload)
    rows = []
    for stock in payload.get("stocks") or []:
        if not stock.get("held"):
            continue
        try:
            value = float(stock.get("value") or 0)
        except (TypeError, ValueError):
            value = 0.0
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
            "DataStatus": "PASS" if not data_issues else "BLOCK",
        })
    rows.sort(key=lambda row: row["Weight"], reverse=True)
    return rows


def write_markdown(rows, payload, sentinel, path=DEFAULT_MD):
    summary = payload.get("summary") or {}
    decision = ((sentinel.get("agents") or {}).get("decision") or {}) if sentinel_is_aligned(payload, sentinel) else {}
    issues = dashboard_data_issues(payload)
    lines = [
        "# Position Signal Report",
        "",
        f"Data: dashboard {summary.get('priceMode')} as of {summary.get('priceAsOf')} · generated {summary.get('generatedAt')} · market value {money(summary.get('marketValue'))}",
        f"Market gate: **{decision.get('label', '—')}** · {decision.get('primaryAction', '—')}",
        f"Data gate: **{'BLOCK' if issues else 'PASS'}**" + (f" · {', '.join(issues)}" if issues else ""),
        "",
        "| Symbol | Theme | Price | Day | Weight | Unreal | Risk | Fib | RSI | Signal |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in rows:
        note = f" / {md_cell(row['Notes'])}" if row["Notes"] else ""
        lines.append(
            f"| {md_cell(row['Symbol'])} | {md_cell(row['Theme'])} | {money(row['Price'])} | {pct(row['Day'])} | "
            f"{plain_pct(row['Weight'])} | {pct(row['Unreal'])} | {plain_pct(row['Risk'])} | "
            f"{md_cell(row['Fib'])} | {plain_pct(row['RSI'])} | {md_cell(row['Signal'])}{note} |"
        )
    atomic_write_text(path, "\n".join(lines) + "\n")


def write_csv(rows, path=DEFAULT_CSV):
    if rows:
        atomic_write_csv(path, rows, list(rows[0].keys()))
    else:
        atomic_write_text(path, "")


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

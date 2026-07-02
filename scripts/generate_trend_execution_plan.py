#!/usr/bin/env python3
"""Generate a portfolio-wide trend execution plan.

The report is intentionally rule-based: it turns the current dashboard payload
into reclaim buy-stop-limit levels, pullback zones, invalidation closes, moving
take-profit guides, and a recent-sell review.
"""
import argparse
import csv
import datetime as dt
import json
import math
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DASHBOARD = ROOT / "output" / "portfolio_dashboard.html"
DEFAULT_SENTINEL = ROOT / "output" / "market_sentinel" / "latest_snapshot.json"
DEFAULT_SPMO = ROOT / "output" / "spmo_momentum" / "latest_spmo_momentum.json"
DEFAULT_OUT = ROOT / "output" / "trend_execution"
CORE_ETFS = {"VOO", "SPY"}
BROAD_TACTICAL = {"QQQ", "SPMO"}
LEVERAGED = {"TQQQ"}
HIGH_BETA_THEMES = {"半导体", "杠杆", "互联网/通信"}


def _to_float(value):
    try:
        if value is None:
            return None
        if isinstance(value, float) and math.isnan(value):
            return None
        return float(value)
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


def ema(values, n):
    alpha = 2.0 / (n + 1.0)
    cur = None
    out = []
    for raw in values:
        value = _to_float(raw)
        if value is None:
            out.append(cur)
            continue
        cur = value if cur is None else alpha * value + (1 - alpha) * cur
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
    text = Path(path).read_text(encoding="utf-8")
    marker = "const DATA = "
    start = text.find(marker)
    if start < 0:
        raise RuntimeError(f"DATA payload not found in {path}")
    start += len(marker)
    depth = 0
    in_str = False
    esc = False
    for i, ch in enumerate(text[start:], start):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])
    raise RuntimeError(f"DATA payload is not complete in {path}")


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
            result[symbol] = _df_to_rows(yf.Ticker(symbol).history(period=period, interval="1d", auto_adjust=False))
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
    rows = list(rows or [])
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


def market_context(payload, sentinel):
    decision = ((sentinel.get("agents") or {}).get("decision") or {})
    q = ((payload.get("qqqTqqq") or {}).get("latest") or {})
    q_close = _to_float(q.get("qqq"))
    q_ema21 = _to_float(q.get("ema21"))
    label = decision.get("label")
    if not label:
        label = "BLOCK" if q_close is not None and q_ema21 is not None and q_close < q_ema21 else "WATCH"
    return {
        "label": label,
        "primaryAction": decision.get("primaryAction") or ((payload.get("qqqTqqq") or {}).get("decisionPanel") or {}).get("doNow"),
        "qqqClose": q_close,
        "qqqEma21": q_ema21,
        "qqqAtr14": _to_float(q.get("atr14")),
        "qqqBelowEma21": q_close is not None and q_ema21 is not None and q_close < q_ema21,
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
            reclaim_anchor = max(x for x in [ema21, _to_float(prev.get("high")), close] if x is not None)
        buy_stop = reclaim_anchor + (0.10 * atr14 if atr14 else 0)
        buy_limit = buy_stop + (0.25 * atr14 if atr14 else 0)
        invalidation = (ema50 - 0.25 * atr14) if ema50 and atr14 else (close - 2.0 * atr14 if close and atr14 else None)
        high20 = max((_to_float(r.get("high")) for r in features[-20:]), default=None)
        trail_by_high = high20 - 3.0 * atr14 if high20 and atr14 else None
        trail_by_ema = ema21 - 1.0 * atr14 if ema21 and atr14 else None
        moving_stop = max(x for x in [trail_by_high, trail_by_ema, invalidation] if x is not None) if close else None

    pullback_zone = None
    if close and ema21 and ema50 and atr14 and close > ema21 > ema50:
        pullback_zone = [_round(ema21 - 0.30 * atr14), _round(ema21 + 0.20 * atr14)]

    rs_spy = {
        "rs21": _round((return_n(features, 21) or 0) - (return_n(spy_features, 21) or 0), 2) if spy_features else None,
        "rs63": _round((return_n(features, 63) or 0) - (return_n(spy_features, 63) or 0), 2) if spy_features else None,
    }
    stock = dict(stock)
    summary_mv = _to_float(stock.get("_marketValue")) or 0.0
    stock["weightPct"] = (stock.get("value") or 0.0) / summary_mv * 100 if summary_mv else 0.0
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
    elif entry_label.startswith("WATCH") and market.get("label") != "BLOCK":
        proposed = max(0.0, min(0.5, max_weight - current_weight))

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
        "DistEMA21ATR": _round((close - ema21) / atr14, 2) if close and ema21 and atr14 else None,
        "RS21vsSPY": rs_spy.get("rs21"),
        "RS63vsSPY": rs_spy.get("rs63"),
        "EntryDecision": entry_label,
        "ExistingAction": existing_action,
        "BuyStop": _round(buy_stop),
        "BuyLimit": _round(buy_limit),
        "PullbackZone": pullback_zone,
        "InvalidationClose": _round(invalidation),
        "MovingStop": _round(moving_stop),
        "StopStatus": stop_status,
        "MaxWeightPct": _round(max_weight, 1),
        "ProposedTranchePct": _round(proposed, 2),
        "BlockedBy": ";".join(blocked_by),
    }


def manage_existing_action(entry_label, stop_status, blocked_by):
    blocked = set(blocked_by or [])
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


def build_plan(payload, sentinel, spmo, ohlc_map=None, no_fetch=False):
    summary = payload.get("summary") or {}
    held = [dict(s) for s in payload.get("stocks") or [] if s.get("held")]
    market_value = _to_float(summary.get("marketValue")) or 0.0
    for stock in held:
        stock["_marketValue"] = market_value

    symbols = [s.get("sym") for s in held if s.get("sym")]
    if ohlc_map is None:
        ohlc_map = {} if no_fetch else fetch_ohlc(symbols + ["SPY"])
    stock_by_sym = {s.get("sym"): s for s in payload.get("stocks") or []}
    features = {}
    for symbol in set(symbols + ["SPY"]):
        rows = ohlc_map.get(symbol) or close_only_rows(stock_by_sym.get(symbol, {}))
        features[symbol] = feature_rows(rows)

    market = market_context(payload, sentinel)
    concentration = concentration_context(payload)
    spy_features = features.get("SPY") or []
    rows = []
    for stock in held:
        rows.append(level_plan(stock, features.get(stock["sym"]) or [], spy_features, market, concentration, spmo))
    rows.sort(key=lambda r: r["WeightPct"] or 0, reverse=True)
    since = (dt.date.fromisoformat(summary.get("priceAsOf") or summary.get("dateRange", ["2026-01-01"])[-1]) - dt.timedelta(days=7)).isoformat()
    return {
        "generatedAt": dt.datetime.now().isoformat(timespec="seconds"),
        "dataFreshness": {
            "dashboardPriceAsOf": summary.get("priceAsOf"),
            "dashboardGeneratedAt": summary.get("generatedAt"),
            "priceSource": "Yahoo Finance OHLC via yfinance" if not no_fetch else "dashboard close cache",
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
        f"Data: dashboard prices as of {plan['dataFreshness'].get('dashboardPriceAsOf')} · source {plan['dataFreshness'].get('priceSource')}",
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
        "| Symbol | Decision | Existing | Price | EMA21 | EMA50 | ATR | RS21/SPY | Buy-stop / limit | Pullback zone | Invalidation | Moving stop | Stop | Blocked by |",
        "|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in plan["levels"]:
        zone = row.get("PullbackZone")
        zone_text = "—" if not zone else f"{fmt_money(zone[0])}-{fmt_money(zone[1])}"
        lines.append(
            f"| {row['Symbol']} | {row['EntryDecision']} | {row['ExistingAction']} | {fmt_money(row['Price'])} | {fmt_money(row['EMA21'])} | "
            f"{fmt_money(row['EMA50'])} | {fmt_money(row['ATR14'])} | {fmt_pct(row['RS21vsSPY'])} | "
            f"{fmt_money(row['BuyStop'])} / {fmt_money(row['BuyLimit'])} | {zone_text} | "
            f"{fmt_money(row['InvalidationClose'])} | {fmt_money(row['MovingStop'])} | {row['StopStatus']} | {row['BlockedBy'] or '—'} |"
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


def write_outputs(plan, out_dir=DEFAULT_OUT):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "latest_trend_execution_plan.json").write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")
    (out_dir / "latest_trend_execution_plan.md").write_text(format_report(plan), encoding="utf-8")

    with (out_dir / "latest_trend_levels.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(plan["levels"][0].keys()) if plan["levels"] else [])
        if plan["levels"]:
            writer.writeheader()
            writer.writerows(plan["levels"])

    with (out_dir / "latest_recent_sells.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(plan["recentSells"][0].keys()) if plan["recentSells"] else [])
        if plan["recentSells"]:
            writer.writeheader()
            writer.writerows(plan["recentSells"])


def main():
    parser = argparse.ArgumentParser(description="Generate trend-following execution plan for current holdings.")
    parser.add_argument("--dashboard", default=str(DEFAULT_DASHBOARD))
    parser.add_argument("--sentinel", default=str(DEFAULT_SENTINEL))
    parser.add_argument("--spmo", default=str(DEFAULT_SPMO))
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT))
    parser.add_argument("--no-fetch", action="store_true", help="Use dashboard close cache instead of fetching OHLC.")
    args = parser.parse_args()

    payload = read_dashboard_payload(args.dashboard)
    plan = build_plan(payload, read_json(args.sentinel), read_json(args.spmo), no_fetch=args.no_fetch)
    write_outputs(plan, args.out_dir)
    print(f"{plan['dataFreshness'].get('dashboardPriceAsOf')} trend execution {plan['market'].get('label')} -> {len(plan['levels'])} held levels")
    print(Path(args.out_dir).resolve() / "latest_trend_execution_plan.md")


if __name__ == "__main__":
    main()

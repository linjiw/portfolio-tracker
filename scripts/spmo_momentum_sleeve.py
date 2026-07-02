#!/usr/bin/env python3
"""SPMO momentum sleeve analysis.

The sleeve follows SPMO's idea: buy after momentum is visible, not before it.
It uses a market gate (QQQ weather), an asset trend gate (SPMO trend), relative
strength, and moving take-profit. It is decision support, not trading advice.
"""
import argparse
import csv
import datetime as dt
import json
import math
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DASHBOARD = ROOT / "output" / "portfolio_dashboard.html"
DEFAULT_OUT = ROOT / "output" / "spmo_momentum"
DEFAULT_SENTINEL = ROOT / "output" / "market_sentinel" / "latest_snapshot.json"
BENCHMARKS = ("SPY", "QQQ", "VOO")
BASE_SLEEVE_CAP_PCT = 8.0
MAX_SLEEVE_CAP_PCT = 12.0


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
    prev = None
    for row in rows:
        high, low, close = row["high"], row["low"], row["close"]
        tr = high - low if prev is None else max(high - low, abs(high - prev), abs(low - prev))
        trs.append(tr)
        prev = close
    out, cur = [], None
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


def sentinel_context_for_payload(payload, sentinel):
    """Use the market gate only when it was built from the same dashboard."""
    if not sentinel:
        return None, None
    freshness = sentinel.get("dataFreshness") or {}
    summary = (payload or {}).get("summary") or {}
    if (
        freshness.get("dashboardGeneratedAt") != summary.get("generatedAt")
        or freshness.get("dashboardPriceAsOf") != summary.get("priceAsOf")
    ):
        return None, None
    agents = sentinel.get("agents") or {}
    return agents.get("technical"), agents.get("decision")


def _df_to_rows(df):
    if df is None or df.empty:
        return []
    rows = []
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
            "volume": _to_float(row.get("Volume")) or 0.0,
        })
    return rows


def fetch_daily_rows(symbol, period="18mo"):
    import warnings
    warnings.filterwarnings("ignore")
    import yfinance as yf

    df = yf.Ticker(symbol).history(period=period, interval="1d", auto_adjust=False)
    return _df_to_rows(df)


def add_features(rows):
    closes = [r["close"] for r in rows]
    e21 = ema(closes, 21)
    e50 = ema(closes, 50)
    e200 = ema(closes, 200)
    a14 = atr(rows, 14)
    for i, row in enumerate(rows):
        row["ema21"] = e21[i]
        row["ema50"] = e50[i]
        row["ema200"] = e200[i]
        row["atr14"] = a14[i]
    return rows


def return_n(rows, bars):
    if len(rows) <= bars:
        return None
    return _pct(rows[-1]["close"], rows[-1 - bars]["close"])


def momentum_12_1(rows):
    """Approximate S&P momentum: 12-month price change excluding latest month."""
    if len(rows) >= 253:
        return _pct(rows[-22]["close"], rows[-253]["close"])
    if len(rows) >= 190:
        return _pct(rows[-22]["close"], rows[-190]["close"])
    return None


def relative_strength(spmo_rows, bench_rows):
    result = {}
    for bars, label in ((21, "rel21"), (63, "rel63"), (126, "rel126")):
        s_ret = return_n(spmo_rows, bars)
        b_ret = return_n(bench_rows, bars)
        result[label] = _round(s_ret - b_ret, 2) if s_ret is not None and b_ret is not None else None
    return result


def score_spmo(spmo_rows, benchmark_map, payload, technical=None, decision=None):
    if len(spmo_rows) < 60:
        return {"available": False, "reason": "not enough SPMO rows"}

    add_features(spmo_rows)
    for rows in benchmark_map.values():
        add_features(rows)
    last = spmo_rows[-1]
    prev = spmo_rows[-2] if len(spmo_rows) >= 2 else None
    close = last["close"]
    atr14 = last.get("atr14") or 0
    ema21 = last.get("ema21")
    ema50 = last.get("ema50")
    ema200 = last.get("ema200")

    spy_rs = relative_strength(spmo_rows, benchmark_map.get("SPY", [])) if benchmark_map.get("SPY") else {}
    qqq_rs = relative_strength(spmo_rows, benchmark_map.get("QQQ", [])) if benchmark_map.get("QQQ") else {}
    voo_rs = relative_strength(spmo_rows, benchmark_map.get("VOO", [])) if benchmark_map.get("VOO") else {}
    rs_votes = []
    for rs in (spy_rs, qqq_rs, voo_rs):
        for key in ("rel21", "rel63"):
            val = rs.get(key)
            if val is not None:
                rs_votes.append(val > 0)
    rel_score = sum(1 for x in rs_votes if x)
    spy_hard_rs = (spy_rs.get("rel21") is not None and spy_rs.get("rel21") > 0
                   and spy_rs.get("rel63") is not None and spy_rs.get("rel63") > 0)

    above21 = ema21 is not None and close > ema21
    above50 = ema50 is not None and close > ema50
    above200 = ema200 is not None and close > ema200
    slope21 = None
    slope50 = None
    if len(spmo_rows) >= 6 and spmo_rows[-6].get("ema21"):
        slope21 = close_or_delta(last.get("ema21"), spmo_rows[-6].get("ema21"))
    if len(spmo_rows) >= 11 and spmo_rows[-11].get("ema50"):
        slope50 = close_or_delta(last.get("ema50"), spmo_rows[-11].get("ema50"))
    two_below_ema21 = False
    if len(spmo_rows) >= 2 and ema21 is not None and spmo_rows[-2].get("ema21") is not None:
        two_below_ema21 = spmo_rows[-1]["close"] < spmo_rows[-1]["ema21"] and spmo_rows[-2]["close"] < spmo_rows[-2]["ema21"]

    abs_score = 0
    abs_score += 2 if above21 else -2
    abs_score += 2 if above50 else -2
    abs_score += 1 if above200 else 0
    abs_score += 1 if (slope21 is not None and slope21 > 0) else -1
    abs_score += 1 if (slope50 is not None and slope50 > 0) else 0

    market_block = False
    market_watch = False
    reasons = []
    q_latest = ((payload or {}).get("qqqTqqq") or {}).get("latest") or {}
    q_close = _to_float(q_latest.get("qqq"))
    q_ema21 = _to_float(q_latest.get("ema21"))
    if q_close is not None and q_ema21 is not None and q_close < q_ema21:
        market_block = True
        reasons.append("QQQ below EMA21")
    if technical and ((technical.get("flags") or {}).get("belowEma21") or technical.get("intradayLabel") == "BLOCK"):
        market_block = True
        reasons.append("market sentinel/重心 blocked")
    if decision and decision.get("label") == "BLOCK":
        market_block = True
        reasons.append("market decision BLOCK")
    if decision and decision.get("label") == "WATCH":
        market_watch = True
    spy_latest = (benchmark_map.get("SPY") or [{}])[-1] if benchmark_map.get("SPY") else {}
    spy_market_broken = bool(spy_latest.get("close") and spy_latest.get("ema50") and spy_latest["close"] < spy_latest["ema50"])
    if spy_market_broken:
        market_watch = True
        reasons.append("SPY below EMA50")

    one_day = _pct(close, prev["close"] if prev else None)
    hard_hit = one_day is not None and one_day <= -4.0
    if hard_hit:
        market_watch = True
        reasons.append("SPMO had a large one-day drawdown")
    stretched = bool(ema21 and atr14 and (close - ema21) / atr14 > 1.75)
    if stretched:
        market_watch = True
        reasons.append("SPMO stretched above EMA21")

    sleeve_state = "trend"
    if two_below_ema21 or not above50:
        sleeve_state = "broken_or_chop"
    elif abs_score < 1:
        sleeve_state = "broken_or_chop"
    elif not spy_hard_rs or rel_score < max(2, len(rs_votes) // 2):
        sleeve_state = "absolute_only"
    elif hard_hit:
        sleeve_state = "trend_under_stress"
    elif stretched:
        sleeve_state = "overextended"

    if market_block:
        label = "BLOCK"
        action = "No new SPMO add. Keep existing sleeve only if it respects moving-stop; wait for QQQ reclaim."
    elif sleeve_state == "trend" and abs_score >= 4 and rel_score >= max(3, len(rs_votes) // 2):
        label = "ALLOW"
        action = "Momentum confirmed. Add only by tranches after buy-stop/reclaim, not at market after a crash bar."
    elif sleeve_state in ("absolute_only", "trend_under_stress", "overextended") or market_watch:
        label = "WATCH"
        action = "Trend exists but confirmation is incomplete. Wait for SPMO reclaim/relative strength repair."
    else:
        label = "BLOCK"
        action = "SPMO trend is not clean enough for momentum adds."

    buy_stop = None
    buy_limit = None
    if ema21 and atr14:
        buy_stop = max(close + 0.10 * atr14, ema21 + 0.20 * atr14)
        buy_limit = buy_stop + 0.10 * atr14
    invalidation = (ema50 - 0.20 * atr14) if ema50 and atr14 else None
    moving_stop = (close - 3.0 * atr14) if atr14 else None

    holding = None
    for stock in (payload or {}).get("stocks", []):
        if stock.get("sym") == "SPMO" and stock.get("held"):
            holding = stock
            break
    summary = (payload or {}).get("summary") or {}
    weight = None
    if holding and summary.get("marketValue"):
        weight = holding.get("value", 0) / summary["marketValue"] * 100

    levels = {
        "buyStop": _round(buy_stop),
        "buyLimit": _round(buy_limit),
        "invalidationClose": _round(invalidation),
        "movingStop3Atr": _round(moving_stop),
        "ema21Zone": [_round(ema21 - 0.5 * atr14), _round(ema21 + 0.5 * atr14)] if ema21 and atr14 else None,
    }
    actions = action_plan(
        label=label,
        sleeve_state=sleeve_state,
        above50=above50,
        two_below_ema21=two_below_ema21,
        hard_hit=hard_hit,
        stretched=stretched,
        weight=weight,
        levels=levels,
    )

    return {
        "available": True,
        "asOf": last["date"],
        "label": label,
        "action": action,
        "sleeveState": sleeve_state,
        "reasons": sorted(set(reasons)),
        "price": _round(close),
        "oneDayPct": _round(one_day, 2),
        "ema21": _round(ema21),
        "ema50": _round(ema50),
        "ema200": _round(ema200),
        "atr14": _round(atr14),
        "distEma21Atr": _round((close - ema21) / atr14, 2) if ema21 and atr14 else None,
        "absTrendScore": abs_score,
        "relativeVotes": {"positive": rel_score, "total": len(rs_votes)},
        "gates": {
            "aboveEma21": above21,
            "aboveEma50": above50,
            "aboveEma200": above200,
            "ema21Slope5": _round(slope21, 4),
            "ema50Slope10": _round(slope50, 4),
            "twoBelowEma21": two_below_ema21,
            "spmoVsSpy21And63Positive": spy_hard_rs,
            "stretchedAboveEma21": stretched,
            "hardHitDay": hard_hit,
            "marketBlock": market_block,
            "marketWatch": market_watch,
            "spyBelowEma50": spy_market_broken,
        },
        "spmoMomentum12Minus1": _round(momentum_12_1(spmo_rows), 2),
        "returns": {
            "spmo21": _round(return_n(spmo_rows, 21), 2),
            "spmo63": _round(return_n(spmo_rows, 63), 2),
            "spmo126": _round(return_n(spmo_rows, 126), 2),
        },
        "relativeStrength": {
            "vsSPY": spy_rs,
            "vsQQQ": qqq_rs,
            "vsVOO": voo_rs,
        },
        "levels": levels,
        "position": {
            "shares": holding.get("shares") if holding else None,
            "value": holding.get("value") if holding else None,
            "weightPct": _round(weight, 1),
            "avg": holding.get("avg") if holding else None,
            "unrealPct": holding.get("unrealPct") if holding else None,
        },
        "actions": actions,
        "tranchePlan": tranche_plan(levels),
        "journalChecklist": journal_checklist(),
        "reviewers": reviewer_notes(label, sleeve_state, weight, market_block, rel_score, len(rs_votes), hard_hit, stretched),
    }


def close_or_delta(new, old):
    new = _to_float(new)
    old = _to_float(old)
    if new is None or old is None:
        return None
    return new - old


def action_plan(label, sleeve_state, above50, two_below_ema21, hard_hit, stretched, weight, levels):
    cap = BASE_SLEEVE_CAP_PCT
    if label == "ALLOW" and sleeve_state == "trend":
        cap = MAX_SLEEVE_CAP_PCT
    if hard_hit or stretched or label == "WATCH":
        cap = min(cap, BASE_SLEEVE_CAP_PCT)
    current = weight or 0.0
    max_add = max(0.0, cap - current)
    if label == "BLOCK":
        max_add = 0.0
    elif label == "WATCH":
        max_add = min(max_add, 1.0)

    if label == "ALLOW":
        new_add = "ALLOW tranche; never market-buy a vertical spike"
    elif label == "WATCH":
        new_add = "WATCH only; wait for reclaim/retest confirmation"
    else:
        new_add = "BLOCK new add"

    if not above50 or two_below_ema21:
        existing = "TRIM/DEFEND: SPMO sleeve trend has lost its intermediate support"
    elif hard_hit:
        existing = "HOLD_WITH_STOP: existing sleeve survived EMA50 but crash bar requires no add"
    else:
        existing = "HOLD_WITH_MOVING_STOP: let winner run while stop keeps moving up"

    return {
        "newAdd": new_add,
        "existingSleeve": existing,
        "maxSleevePct": _round(cap, 1),
        "maxNewAddPct": _round(max_add, 1),
        "stop": f"Close below {levels.get('invalidationClose')} shifts to defend/trim.",
        "movingTakeProfit": f"Use {levels.get('movingStop3Atr')} as 3xATR reference; never move it down.",
    }


def tranche_plan(levels):
    return [
        {
            "tranche": "1/3",
            "trigger": f"SPMO buy-stop/reclaim above {levels.get('buyStop')} with QQQ/sentinel not BLOCK.",
            "risk": f"Initial invalidation close {levels.get('invalidationClose')}.",
        },
        {
            "tranche": "1/3",
            "trigger": "Retest holds above EMA21/EMA50 after reclaim; no lower low on 15m/30m structure.",
            "risk": "If reclaim fails the same day, do not add second tranche.",
        },
        {
            "tranche": "1/3",
            "trigger": "Follow-through close with SPMO/SPY relative strength still positive.",
            "risk": "If position is stretched > 1.75 ATR above EMA21, skip add and move stop instead.",
        },
    ]


def journal_checklist():
    return [
        "Regime label and data date are fresh.",
        "QQQ/sentinel market gate is not BLOCK for a new add.",
        "SPMO is above EMA50 and not two closes below EMA21.",
        "SPMO/SPY 21d and 63d relative strength are positive.",
        "Entry uses buy-stop/reclaim or retest, not FOMO at market.",
        "Position size stays under sleeve cap and portfolio concentration cap.",
        "Invalidation, moving take-profit, and review date are written before entry.",
    ]


def reviewer_notes(label, sleeve_state, weight, market_block, rel_score, total_votes, hard_hit=False, stretched=False):
    dalio = []
    quant = []
    execution = []
    if market_block:
        dalio.append("Separate desire from evidence: the system is not allowed to add while the market weather gate is closed.")
    if weight is not None and weight >= 8:
        dalio.append("Risk first: SPMO is already meaningful; next add must improve balance, not just express conviction.")
    else:
        dalio.append("Use a small sleeve: momentum works by repeated rule-following, not one heroic entry.")
    if label == "ALLOW":
        dalio.append("Let profits run, but pre-commit the moving stop before entry.")
    if label == "BLOCK":
        dalio.append("A blocked add is not a bearish prediction; it is a rule saying evidence has not repaired yet.")
    quant.append("Avoid single-signal entries; require absolute trend plus relative strength plus market gate.")
    if total_votes and rel_score < max(3, total_votes // 2):
        quant.append("Relative strength is not broad enough; this is an absolute-trend hold, not a fresh add.")
    if sleeve_state == "trend_under_stress":
        quant.append("Crash-bar adds create bad slippage; use buy-stop/reclaim to avoid catching a falling factor.")
    if stretched:
        quant.append("Overextended momentum is a no-chase zone; future return/risk improves after retest or consolidation.")
    execution.append("New entries should be stop-triggered or retest-triggered; existing winners are managed by moving stops.")
    execution.append("If an add requires an immediate rebound to feel safe, the size or trigger is wrong.")
    return {"principlesInvestor": dalio, "quantReviewer": quant, "executionDesk": execution}


def run_once(payload=None, technical=None, decision=None):
    payload = payload or read_dashboard_payload(DEFAULT_DASHBOARD)
    spmo_rows = fetch_daily_rows("SPMO")
    bench = {symbol: fetch_daily_rows(symbol) for symbol in BENCHMARKS}
    return score_spmo(spmo_rows, bench, payload, technical=technical, decision=decision)


def format_report(result):
    lines = [
        "# SPMO Momentum Sleeve",
        "",
        f"As of: {result.get('asOf')}  ",
        f"Decision: **{result.get('label')}** - {result.get('action')}",
        "",
        "## Evidence",
        f"- Price: {result.get('price')} | EMA21 {result.get('ema21')} | EMA50 {result.get('ema50')} | ATR14 {result.get('atr14')}",
        f"- Abs trend score: {result.get('absTrendScore')} | Relative votes: {result.get('relativeVotes')}",
        f"- 12-1 momentum proxy: {result.get('spmoMomentum12Minus1')}% | returns {result.get('returns')}",
        f"- Relative strength: {result.get('relativeStrength')}",
        f"- Gates: {result.get('gates')}",
        "",
        "## Levels",
        f"- Buy-stop/reclaim: {result.get('levels', {}).get('buyStop')} / limit {result.get('levels', {}).get('buyLimit')}",
        f"- Invalidation close: {result.get('levels', {}).get('invalidationClose')}",
        f"- Moving stop guide: {result.get('levels', {}).get('movingStop3Atr')}",
        "",
        "## Actions",
        f"- New add: {result.get('actions', {}).get('newAdd')}",
        f"- Existing sleeve: {result.get('actions', {}).get('existingSleeve')}",
        f"- Max sleeve/add: {result.get('actions', {}).get('maxSleevePct')}% / {result.get('actions', {}).get('maxNewAddPct')}%",
        "",
        "## Tranche Plan",
    ]
    for item in result.get("tranchePlan") or []:
        lines.append(f"- {item['tranche']}: {item['trigger']} Risk: {item['risk']}")
    lines += [
        "",
        "## Journal Checklist",
    ]
    for item in result.get("journalChecklist") or []:
        lines.append(f"- {item}")
    lines += [
        "",
        "## Reviewers",
    ]
    reviewers = result.get("reviewers") or {}
    for name, notes in reviewers.items():
        lines.append(f"### {name}")
        for note in notes:
            lines.append(f"- {note}")
    return "\n".join(lines) + "\n"


def write_outputs(result, out_dir=DEFAULT_OUT):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "latest_spmo_momentum.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    (out_dir / "latest_spmo_momentum.md").write_text(format_report(result), encoding="utf-8")
    log = out_dir / "spmo_momentum_log.csv"
    first = not log.exists()
    row = {
        "ranAt": dt.datetime.now().isoformat(timespec="seconds"),
        "asOf": result.get("asOf"),
        "label": result.get("label"),
        "price": result.get("price"),
        "absTrendScore": result.get("absTrendScore"),
        "relativePositive": (result.get("relativeVotes") or {}).get("positive"),
        "relativeTotal": (result.get("relativeVotes") or {}).get("total"),
        "buyStop": (result.get("levels") or {}).get("buyStop"),
        "invalidationClose": (result.get("levels") or {}).get("invalidationClose"),
    }
    with log.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(row.keys()))
        if first:
            writer.writeheader()
        writer.writerow(row)


def main():
    parser = argparse.ArgumentParser(description="Analyze SPMO momentum sleeve.")
    parser.add_argument("--dashboard", default=str(DEFAULT_DASHBOARD))
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT))
    parser.add_argument("--sentinel", default=str(DEFAULT_SENTINEL),
                        help="latest market sentinel snapshot used for the QQQ/market gate")
    parser.add_argument("--ignore-sentinel", action="store_true",
                        help="ignore the saved market sentinel gate and score SPMO in isolation")
    args = parser.parse_args()
    payload = read_dashboard_payload(args.dashboard)
    technical = decision = None
    if not args.ignore_sentinel:
        technical, decision = sentinel_context_for_payload(payload, read_json(args.sentinel))
    result = run_once(payload=payload, technical=technical, decision=decision)
    write_outputs(result, args.out_dir)
    print(f"{result.get('asOf')} SPMO {result.get('label')} {result.get('action')}")
    print(Path(args.out_dir).resolve() / "latest_spmo_momentum.md")


if __name__ == "__main__":
    main()

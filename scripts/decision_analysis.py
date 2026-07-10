#!/usr/bin/env python3
"""Decision-analysis producer for the dashboard's 决策分析 (Decision Analysis) tab.

This is the reusable, deterministic QUANT layer behind that tab. Give it a list of
candidate tickers; it pulls 3y of daily history, computes momentum / risk / timing
backtest / correlation metrics, and emits a renderable ``output/decision_analysis.json``
skeleton with transparent heuristic sub-scores (momentum / risk / diversify), a
composite, a neutral review gate, and portfolio-aware cash/concentration context.
Curated thesis/recommendation text is preserved when present; placeholder fields
are replaced and the active artifact is updated atomically.

Schema consumed by generate.py (load_decision_analysis → DATA.decision):
    asOf, title, question, thesis, method, recommendation, disclaimer  (strings)
    cash: {operable, withPending, pctSettled, pctWithPending, netLiq, top4Pct, note}
    noList: [{ticker, why}]
    candidates: [{ticker, price, flag(ok|warn|spike|bad), status, momentum, risk,
                  diversify, composite, verdict, tone(good|hold|caution|avoid),
                  corr, note}]
    rules: [string]

Usage:
    python3 scripts/decision_analysis.py --tickers AMD,QQQ,TQQQ,SPMO,MU,NBIS,TSM,ASML
    python3 scripts/decision_analysis.py --tickers MU,TSM --holdings NVDA,QQQ,GOOGL,VOO

Only dependency beyond the stdlib is yfinance/pandas/numpy (already used by generate.py).
"""
import argparse
import datetime as dt
import json
import math
import os
import re
import sys

import numpy as np
import pandas as pd
import yfinance as yf

try:
    from scripts.artifact_io import atomic_write_json
    from scripts.dashboard_payload import read_dashboard_payload as _read_dashboard_payload
except ModuleNotFoundError:  # direct ``python scripts/decision_analysis.py``
    from artifact_io import atomic_write_json
    from dashboard_payload import read_dashboard_payload as _read_dashboard_payload

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DEFAULT = os.path.join(HERE, "output", "decision_analysis.json")
RF = 0.045  # risk-free annual, for Sharpe
MIN_HISTORY_BARS = 252
MIN_CORRELATION_BARS = 60
MAX_PRICE_LAG_WEEKDAYS = 2
_SYMBOL_RE = re.compile(r"^[A-Z0-9^][A-Z0-9.^=/\-]{0,19}$")


def read_dashboard(path):
    if not path or not os.path.exists(path):
        return {}
    return _read_dashboard_payload(path)


def dashboard_context(payload):
    held = [row for row in (payload.get("stocks") or []) if row.get("held")]
    held_symbols = [row.get("sym") for row in held if row.get("sym")]
    summary = payload.get("summary") or {}
    account = payload.get("account") or {}
    market_value = float(summary.get("marketValue") or 0)
    top4 = sum(sorted((float(row.get("value") or 0) for row in held), reverse=True)[:4])
    cash = float(account.get("cashTotal") or summary.get("cashTotal") or 0)
    pending = float(account.get("pending") or summary.get("pendingTotal") or 0)
    net_liq = float(account.get("netWorthWhole") or summary.get("accountNetWorth") or market_value)
    denominator = net_liq if net_liq > 0 else market_value
    qqq_state = ((payload.get("qqqTqqq") or {}).get("state") or {})
    return {
        "heldSymbols": held_symbols,
        "marketGate": qqq_state.get("code"),
        "marketLabel": qqq_state.get("label"),
        "cash": {
            "operable": round(max(cash, 0), 2),
            "withPending": round(cash + pending, 2),
            "pctSettled": round(cash / denominator * 100, 2) if denominator else None,
            "pctWithPending": round((cash + pending) / denominator * 100, 2) if denominator else None,
            "netLiq": round(net_liq, 2),
            "top4Pct": round(top4 / market_value * 100, 2) if market_value else None,
            "note": ("Known positive money-market cash only; pending activity is shown separately and "
                     "the broker export does not expose margin debit or buying power."),
        },
    }


def placeholder(value):
    if value is None:
        return True
    if isinstance(value, list):
        return not value or any("待补" in str(item) for item in value)
    return not str(value).strip() or "待补" in str(value)


def carry_forward_curated_text(doc, previous, *, enabled=False):
    """Copy manual narrative only after an explicit, provenance-bearing opt-in."""
    provenance = {
        "carriedForward": False, "sourceGeneratedAt": None, "sourceAsOf": None,
        "keys": [], "requiresReview": False,
    }
    if enabled and isinstance(previous, dict):
        for key in ("thesis", "rules", "recommendation"):
            if not placeholder(previous.get(key)):
                doc[key] = previous[key]
                provenance["keys"].append(key)
        if provenance["keys"]:
            provenance.update({
                "carriedForward": True,
                "sourceGeneratedAt": previous.get("generatedAt"),
                "sourceAsOf": previous.get("asOf"),
                "requiresReview": previous.get("asOf") != doc.get("asOf"),
            })
    doc["curatedTextProvenance"] = provenance
    return doc


def write_json_atomic(path, doc):
    atomic_write_json(path, doc)


def canonical_symbol(value):
    symbol = str(value or "").strip().upper()
    if not _SYMBOL_RE.fullmatch(symbol) or ".." in symbol or "//" in symbol:
        raise ValueError(f"invalid ticker symbol: {value!r}")
    return symbol


def weekday_lag(latest, target):
    """Count weekdays after ``latest`` through ``target`` (holidays are conservative)."""
    latest = latest if isinstance(latest, dt.date) else dt.date.fromisoformat(str(latest)[:10])
    target = target if isinstance(target, dt.date) else dt.date.fromisoformat(str(target)[:10])
    if latest > target:
        return -weekday_lag(target, latest)
    lag = 0
    cursor = latest + dt.timedelta(days=1)
    while cursor <= target:
        lag += cursor.weekday() < 5
        cursor += dt.timedelta(days=1)
    return lag


def rsi(s, n=14):
    """Wilder RSI with an explicit warm-up and neutral flat-window handling."""
    values = pd.Series(s, dtype=float)
    out = pd.Series(np.nan, index=values.index, dtype=float)
    if len(values) <= n:
        return out
    delta = values.diff()
    gains = delta.clip(lower=0.0)
    losses = -delta.clip(upper=0.0)
    avg_gain = float(gains.iloc[1:n + 1].mean())
    avg_loss = float(losses.iloc[1:n + 1].mean())

    def score(gain, loss):
        if gain == 0 and loss == 0:
            return 50.0
        if loss == 0:
            return 100.0
        if gain == 0:
            return 0.0
        return 100.0 - 100.0 / (1.0 + gain / loss)

    out.iloc[n] = score(avg_gain, avg_loss)
    for i in range(n + 1, len(values)):
        avg_gain = (avg_gain * (n - 1) + float(gains.iloc[i])) / n
        avg_loss = (avg_loss * (n - 1) + float(losses.iloc[i])) / n
        out.iloc[i] = score(avg_gain, avg_loss)
    return out


def maxdd(s):
    cum = s / s.iloc[0]
    return float((cum / cum.cummax() - 1).min())


def sharpe(s, risk_free_rate=RF):
    r = s.pct_change().dropna()
    sd = r.std()
    daily_rf = (1.0 + risk_free_rate) ** (1.0 / 252.0) - 1.0
    return float((r - daily_rf).mean() / sd * math.sqrt(252)) if sd > 0 else float("nan")


def ret_window(s, days):
    return float(s.iloc[-1] / s.iloc[-1 - days] - 1) if len(s) > days else float("nan")


def clamp(v, lo=0, hi=100):
    if v is None or not math.isfinite(v):
        v = 50
    return int(max(lo, min(hi, round(v))))


def finite(v):
    return v is not None and math.isfinite(v)


def safe(v, fallback=0.0):
    return v if finite(v) else fallback


def fmt(v, digits=0, suffix=""):
    return f"{v:.{digits}f}{suffix}" if finite(v) else "n/a"


def _close_frame(raw, symbols):
    """Normalize yfinance's single- and multi-symbol Close layouts."""
    if not isinstance(raw, pd.DataFrame) or raw.empty:
        return pd.DataFrame()
    if isinstance(raw.columns, pd.MultiIndex):
        level0 = set(map(str, raw.columns.get_level_values(0)))
        level1 = set(map(str, raw.columns.get_level_values(1)))
        if "Close" in level0:
            close = raw["Close"]
        elif "Close" in level1:
            close = raw.xs("Close", axis=1, level=1)
        else:
            return pd.DataFrame()
    elif "Close" in raw.columns and len(symbols) == 1:
        close = raw[["Close"]].rename(columns={"Close": symbols[0]})
    else:
        close = raw
    if isinstance(close, pd.Series):
        close = close.to_frame(name=symbols[0])
    close = close.copy()
    close.columns = [str(column).upper() for column in close.columns]
    close.index = pd.to_datetime(close.index, errors="coerce")
    close = close.loc[~close.index.isna()]
    if getattr(close.index, "tz", None) is not None:
        close.index = close.index.tz_convert(None)
    close = close.loc[~close.index.duplicated(keep="last")].sort_index()
    close = close.apply(pd.to_numeric, errors="coerce").replace([np.inf, -np.inf], np.nan)
    return close.dropna(how="all")


def compute(tickers, holdings, *, today=None, downloader=None, risk_free_rate=RF):
    allt = sorted(set(tickers) | set(holdings))
    print(f"· fetching 3y daily for {len(allt)} symbols ...", file=sys.stderr)
    cache_dir = os.path.join(HERE, "output", "yfinance_cache")
    os.makedirs(cache_dir, exist_ok=True)
    if hasattr(yf, "set_tz_cache_location"):
        yf.set_tz_cache_location(cache_dir)
    download = downloader or yf.download
    raw = download(allt, period="3y", interval="1d", auto_adjust=True, progress=False)
    close = _close_frame(raw, allt)
    if close.empty:
        raise RuntimeError("price provider returned no usable adjusted closes")
    today = today or dt.date.today()
    rets1y = close.pct_change(fill_method=None).iloc[-252:]

    out = []
    for t in tickers:
        if t not in close.columns or close[t].dropna().empty:
            print(f"  !! no data for {t}, skipping", file=sys.stderr)
            continue
        s = close[t].dropna()
        latest_date = s.index[-1].date()
        price_lag = weekday_lag(latest_date, today)
        sample_count = len(s)
        decision_grade = sample_count >= MIN_HISTORY_BARS and 0 <= price_lag <= MAX_PRICE_LAG_WEEKDAYS
        px = float(s.iloc[-1])
        sma50 = float(s.rolling(50).mean().iloc[-1])
        sma200 = float(s.rolling(200).mean().iloc[-1])
        s1 = s.iloc[-252:]
        vol1y = float(s1.pct_change().std() * math.sqrt(252)) * 100
        dd3y = maxdd(s) * 100
        sh3y = sharpe(s, risk_free_rate=risk_free_rate)
        rsi14 = float(rsi(s).iloc[-1])
        day_move = ret_window(s, 1) * 100
        vs50 = (px / sma50 - 1) * 100
        vs200 = (px / sma200 - 1) * 100
        hi52 = float(s1.max())
        below_hi = (px / hi52 - 1) * 100
        golden = sma50 > sma200

        # 200DMA trend-filter backtest vs buy&hold (does a trend filter tame the name?)
        dr = s.pct_change().fillna(0)
        sig = (s > s.rolling(200).mean()).shift(1).fillna(False)
        strat = (1 + dr * sig).cumprod()
        strat_dd = float((strat / strat.cummax() - 1).min()) * 100
        filter_helps = (strat_dd > dd3y)  # shallower (less negative) drawdown under filter

        # correlation to existing book (diversify): lower mean corr → higher score
        hcols = [h for h in holdings if h in rets1y.columns and h != t]
        pair_correlations = []
        pair_observations = []
        for holding in hcols:
            pair = rets1y[[t, holding]].dropna()
            if len(pair) < MIN_CORRELATION_BARS or pair[t].std() <= 0 or pair[holding].std() <= 0:
                continue
            value = float(pair[t].corr(pair[holding]))
            if math.isfinite(value):
                pair_correlations.append(max(-1.0, min(1.0, value)))
                pair_observations.append(len(pair))
        corr = float(np.mean(pair_correlations)) if pair_correlations else float("nan")

        # ---- transparent heuristic sub-scores (0-100) ----
        # momentum: trend up + filter helps, penalize extension/spike
        mom = 50
        mom += 18 if golden else -25
        mom += 8 if filter_helps else -4
        mom -= min(max(safe(vs50) - 12, 0), 30) * 0.8   # extended above 50dma
        mom -= min(max(safe(day_move) - 4, 0), 10) * 1.5  # just spiked
        momentum = clamp(mom)
        # risk: high = safe entry. penalize vol, drawdown, extension, spike
        rk = 95
        rk -= min(safe(vol1y, 60), 110) * 0.45
        rk -= min(abs(safe(dd3y, -35)), 65) * 0.35
        rk -= min(max(safe(vs200) - 20, 0), 140) * 0.30
        rk -= min(max(safe(day_move) - 4, 0), 10) * 2.0
        risk_s = clamp(rk)
        # diversify: 100*(1-corr) to the book
        diversify = clamp((1 - corr) * 100) if not math.isnan(corr) else 50
        composite = clamp((momentum + risk_s + diversify) / 3)

        # auto flag + status
        if not decision_grade:
            flag = "bad"
            if sample_count < MIN_HISTORY_BARS:
                status = f"DATA BLOCK · 仅 {sample_count} 根日线（需 {MIN_HISTORY_BARS}）"
            elif price_lag < 0:
                status = f"DATA BLOCK · 价格日期 {latest_date} 晚于运行日"
            else:
                status = f"DATA BLOCK · 价格滞后 {price_lag} 个工作日"
        elif finite(day_move) and day_move >= 4:
            flag, status = "spike", f"跳涨 +{day_move:.0f}% · {'新高' if finite(below_hi) and below_hi > -2 else '反弹'}"
        elif (finite(rsi14) and rsi14 >= 70) or (finite(vs50) and vs50 > 25):
            flag, status = "warn", (f"RSI {rsi14:.0f} · 距高 {fmt(below_hi)}%" if finite(rsi14) and rsi14 >= 70 else f"超50DMA {vs50:.0f}%")
        elif finite(below_hi) and below_hi > -2:
            flag, status = "warn", f"距高 {below_hi:.1f}%"
        else:
            flag, status = "ok", f"无跳空 · 距高 {fmt(below_hi)}%"

        if not decision_grade:
            tone, verdict = "avoid", "BLOCK_DATA"
        elif flag in ("spike", "warn"):
            tone, verdict = "caution", "WATCH_RESET"
        elif composite >= 60:
            tone, verdict = "good", "ALLOW_REVIEW"
        elif composite >= 40:
            tone, verdict = "hold", "WATCH_SETUP"
        else:
            tone, verdict = "avoid", "BLOCK"

        out.append({
            "ticker": t, "price": round(px, 2), "flag": flag, "status": status,
            "momentum": momentum, "risk": risk_s, "diversify": diversify,
            "composite": composite, "verdict": verdict, "tone": tone,
            "corr": round(corr, 2) if not math.isnan(corr) else None,
            "correlationPairs": len(pair_correlations),
            "minCorrelationObservations": min(pair_observations) if pair_observations else None,
            "priceAsOf": latest_date.isoformat(),
            "priceLagWeekdays": price_lag,
            "historyBars": sample_count,
            "dataDecisionGrade": decision_grade,
            "note": f"夏普(3y) {fmt(sh3y, 2)} · 年化波动 {fmt(vol1y)}% · 最大回撤 {fmt(dd3y)}% · "
                    f"超50DMA {fmt(vs50)}% · 距52周高 {fmt(below_hi)}%",
        })
    out.sort(key=lambda c: -c["composite"])
    return out, str(close.index[0].date()), str(close.index[-1].date())


def main():
    ap = argparse.ArgumentParser(description="Produce decision_analysis.json (quant layer).")
    ap.add_argument("--tickers", help="comma-separated candidates; defaults to current held symbols")
    ap.add_argument("--holdings", help="comma-separated holdings; defaults to dashboard holdings")
    ap.add_argument("--dashboard", default=os.path.join(HERE, "output", "portfolio_dashboard.html"))
    ap.add_argument("--out", default=OUT_DEFAULT)
    ap.add_argument("--title", default="现金部署决策：理性该买什么")
    ap.add_argument("--carry-forward-curated", action="store_true",
                    help="explicitly carry prior manual narrative with source-date provenance")
    ap.add_argument("--risk-free-rate", type=float, default=RF,
                    help="annual decimal risk-free rate used by the descriptive Sharpe (default 0.045)")
    args = ap.parse_args()

    payload = read_dashboard(args.dashboard)
    context = dashboard_context(payload)
    tickers_arg = args.tickers or ",".join(context["heldSymbols"])
    holdings_arg = args.holdings or ",".join(context["heldSymbols"])
    try:
        tickers = [canonical_symbol(t) for t in tickers_arg.split(",") if t.strip()]
        holdings = [canonical_symbol(t) for t in holdings_arg.split(",") if t.strip()]
    except ValueError as exc:
        raise SystemExit(f"!! {exc}") from exc
    if not tickers:
        sys.exit("!! no candidate tickers; pass --tickers or generate the dashboard first")
    if not -0.99 < args.risk_free_rate < 1.0:
        raise SystemExit("!! --risk-free-rate must be between -0.99 and 1.0")
    cands, d0, d1 = compute(tickers, holdings, risk_free_rate=args.risk_free_rate)
    if not cands:
        sys.exit("!! no candidates produced (no price data).")

    market_gate = context.get("marketGate")
    market_allows_review = market_gate in ("bull", "ema8")
    if not market_allows_review:
        for candidate in cands:
            if candidate["verdict"] == "ALLOW_REVIEW":
                candidate["verdict"] = "WATCH_MARKET_GATE" if market_gate else "WATCH_MARKET_DATA"
                candidate["tone"] = "hold"
    counts = {}
    for candidate in cands:
        counts[candidate["verdict"]] = counts.get(candidate["verdict"], 0) + 1
    blocked_count = sum(value for key, value in counts.items() if key.startswith("BLOCK"))
    auto_thesis = (f"Quant review queue: {counts.get('ALLOW_REVIEW', 0)} ALLOW_REVIEW, "
                   f"{sum(v for k, v in counts.items() if k.startswith('WATCH'))} WATCH, "
                   f"{blocked_count} BLOCK. Market gate: "
                   f"{context.get('marketLabel') or 'not available'}; gates are research workflow labels, not trade instructions.")
    auto_rules = [
        "ALLOW_REVIEW means eligible for thesis/sizing review; it is not an order or recommendation.",
        "WATCH_RESET blocks chasing spikes/overextension until a pullback or repair trigger is observed.",
        "Any new risk requires a written invalidation level, position cap, and portfolio-overlap check.",
    ]
    auto_recommendation = ("Review candidates only after the market gate, concentration, liquidity, and invalidation checks pass; "
                           "keep WATCH/BLOCK names out of the execution queue until their stated condition changes.")
    doc = {
        "schemaVersion": 1,
        "generatedAt": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "asOf": d1,
        "title": args.title,
        "analysisStatus": "complete_quant_no_discretionary_judgment",
        "researchOnly": True,
        "decisionGrade": False,
        "decisionGradeReason": "Heuristic ranking is a research queue; execution still requires fresh market, thesis, sizing, and invalidation review.",
        "marketGate": {"code": context.get("marketGate"), "label": context.get("marketLabel")},
        "thesis": auto_thesis,
        "cash": context["cash"],
        "noList": [{"ticker": c["ticker"], "why": c["verdict"]}
                   for c in cands if c["verdict"].startswith("BLOCK")],
        "candidates": cands,
        "rules": auto_rules,
        "recommendation": auto_recommendation,
        "method": f"3 年数据（{d0}→{d1}）+ 启发式动量/风险/分散评分（scripts/decision_analysis.py）",
        "methodology": {
            "adjustedPrices": True,
            "riskFreeRateAnnual": args.risk_free_rate,
            "correlation": "equal-weight mean of holding pair correlations with at least 60 common daily returns",
            "trendFilterSignal": "prior close above 200-day moving average; applied to next close-to-close return",
        },
        "disclaimer": "量化分析与决策框架，非投资建议，亦非收益保证；最终决定权在你。",
    }

    target = args.out
    previous = {}
    if os.path.exists(target):
        try:
            with open(target, encoding="utf-8") as handle:
                previous = json.load(handle)
        except Exception:
            previous = {}
    carry_forward_curated_text(doc, previous, enabled=args.carry_forward_curated)
    write_json_atomic(target, doc)
    print(f"\n✓ wrote {target}  ({len(cands)} candidates, data {d0}→{d1})")
    print("  Gates are transparent quant review labels; prior manual narrative is carried only with explicit opt-in.")


if __name__ == "__main__":
    main()

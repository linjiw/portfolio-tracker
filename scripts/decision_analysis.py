#!/usr/bin/env python3
"""Decision-analysis producer for the dashboard's 决策分析 (Decision Analysis) tab.

This is the reusable, deterministic QUANT layer behind that tab. Give it a list of
candidate tickers; it pulls 3y of daily history, computes momentum / risk / timing
backtest / correlation metrics, and emits a renderable ``output/decision_analysis.json``
skeleton with transparent heuristic sub-scores (momentum / risk / diversify), a
composite, an auto status/flag, and a placeholder verdict.

The JUDGMENT layer — verdict wording, tone, thesis, rules, recommendation — is added
on top by a human (or an LLM analysis pass). To avoid clobbering curated content, if
the target file already exists this writes ``output/decision_analysis.NEW.json`` and
asks you to diff/merge instead.

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
import json
import math
import os
import sys

import numpy as np
import pandas as pd
import yfinance as yf

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DEFAULT = os.path.join(HERE, "output", "decision_analysis.json")
RF = 0.045  # risk-free annual, for Sharpe


def rsi(s, n=14):
    d = s.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn)


def maxdd(s):
    cum = s / s.iloc[0]
    return float((cum / cum.cummax() - 1).min())


def sharpe(s):
    r = s.pct_change().dropna()
    sd = r.std()
    return float((r.mean() * 252 - RF) / (sd * math.sqrt(252))) if sd > 0 else float("nan")


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


def compute(tickers, holdings):
    allt = sorted(set(tickers) | set(holdings))
    print(f"· fetching 3y daily for {len(allt)} symbols ...", file=sys.stderr)
    raw = yf.download(allt, period="3y", interval="1d", auto_adjust=True, progress=False)
    close = raw["Close"] if isinstance(raw.columns, pd.MultiIndex) else raw
    close = close.dropna(how="all")
    rets1y = close.pct_change(fill_method=None).iloc[-252:]

    out = []
    for t in tickers:
        if t not in close.columns or close[t].dropna().empty:
            print(f"  !! no data for {t}, skipping", file=sys.stderr)
            continue
        s = close[t].dropna()
        px = float(s.iloc[-1])
        sma50 = float(s.rolling(50).mean().iloc[-1])
        sma200 = float(s.rolling(200).mean().iloc[-1])
        s1 = s.iloc[-252:]
        vol1y = float(s1.pct_change().std() * math.sqrt(252)) * 100
        dd3y = maxdd(s) * 100
        sh3y = sharpe(s)
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
        if hcols and t in rets1y.columns:
            corr = float(rets1y[[t] + hcols].corr().loc[t, hcols].mean())
        else:
            corr = float("nan")

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
        if finite(day_move) and day_move >= 4:
            flag, status = "spike", f"跳涨 +{day_move:.0f}% · {'新高' if finite(below_hi) and below_hi > -2 else '反弹'}"
        elif (finite(rsi14) and rsi14 >= 70) or (finite(vs50) and vs50 > 25):
            flag, status = "warn", (f"RSI {rsi14:.0f} · 距高 {fmt(below_hi)}%" if finite(rsi14) and rsi14 >= 70 else f"超50DMA {vs50:.0f}%")
        elif finite(below_hi) and below_hi > -2:
            flag, status = "warn", f"距高 {below_hi:.1f}%"
        else:
            flag, status = "ok", f"无跳空 · 距高 {fmt(below_hi)}%"

        tone = "good" if composite >= 60 else ("caution" if composite >= 40 else "avoid")
        verdict = {"good": "可起手·分批", "caution": "回调再买", "avoid": "暂避"}[tone]

        out.append({
            "ticker": t, "price": round(px, 2), "flag": flag, "status": status,
            "momentum": momentum, "risk": risk_s, "diversify": diversify,
            "composite": composite, "verdict": verdict, "tone": tone,
            "corr": round(corr, 2) if not math.isnan(corr) else None,
            "note": f"夏普(3y) {fmt(sh3y, 2)} · 年化波动 {fmt(vol1y)}% · 最大回撤 {fmt(dd3y)}% · "
                    f"超50DMA {fmt(vs50)}% · 距52周高 {fmt(below_hi)}%",
        })
    out.sort(key=lambda c: -c["composite"])
    return out, str(close.index[0].date()), str(close.index[-1].date())


def main():
    ap = argparse.ArgumentParser(description="Produce decision_analysis.json (quant layer).")
    ap.add_argument("--tickers", required=True, help="comma-separated candidate tickers")
    ap.add_argument("--holdings", default="NVDA,QQQ,GOOGL,VOO",
                    help="comma-separated existing holdings, for the diversify (correlation) score")
    ap.add_argument("--out", default=OUT_DEFAULT)
    ap.add_argument("--title", default="现金部署决策：理性该买什么")
    args = ap.parse_args()

    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    holdings = [t.strip().upper() for t in args.holdings.split(",") if t.strip()]
    cands, d0, d1 = compute(tickers, holdings)
    if not cands:
        sys.exit("!! no candidates produced (no price data).")

    doc = {
        "asOf": d1,
        "title": args.title,
        "thesis": "（待补：核心结论——这周该不该追、最强趋势是否在最差买点等）",
        "cash": {"operable": None, "withPending": None, "pctSettled": None,
                 "pctWithPending": None, "netLiq": None, "top4Pct": None,
                 "note": "（待补：现金状况与是否动用 margin 的说明）"},
        "noList": [],
        "candidates": cands,
        "rules": ["（待补：纪律规则，例如『当天涨幅 >5% 或超 50DMA >25% 则当天不买』）"],
        "recommendation": "（待补：一句话建议）",
        "method": f"3 年数据（{d0}→{d1}）+ 启发式动量/风险/分散评分（scripts/decision_analysis.py）",
        "disclaimer": "量化分析与决策框架，非投资建议，亦非收益保证；最终决定权在你。",
    }

    target = args.out
    if os.path.exists(target):
        target = target.replace(".json", ".NEW.json")
        print(f"\n! {args.out} already exists — writing {target} instead so curated "
              f"verdict/thesis/cash are not overwritten.\n  Diff and merge the candidates[] "
              f"quant fields you want, then delete the .NEW file.", file=sys.stderr)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    print(f"\n✓ wrote {target}  ({len(cands)} candidates, data {d0}→{d1})")
    print("  Sub-scores are TRANSPARENT HEURISTICS — review/override verdict & tone, "
          "then fill the （待补） fields. Re-run `python3 sync.py` to render the 决策分析 tab.")


if __name__ == "__main__":
    main()

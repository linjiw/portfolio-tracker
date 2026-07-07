#!/usr/bin/env python3
"""Momentum Top-3 sleeve: signals + 8y backtest + dashboard artifact.

Implements the three winning strategies from the July-2026 40-variant sweep
(see ~/.hermes skill quant-momentum-research / references/findings-2026-07.md):

  1. RET_11M_top3  — 11-month total return, top-3 equal weight (flagship)
  2. RET_9M_top3   —  9-month total return, top-3 equal weight (balanced)
  3. RET_5M_top5   —  5-month total return, top-5 equal weight (best Sharpe)

Design rules (no lookahead): signals are computed at month-end close and the
portfolio is held from the next trading day. Per-side transaction costs are
charged on turnover. Benchmarks SPY/QQQ ride along.

HONESTY NOTE (survivorship bias): the universe is *today's* index membership,
so absolute backtest numbers are upper bounds. Cross-strategy ranking is the
valid signal. This is decision support, not trading advice.

Outputs (under --out-dir, default output/momentum_top3/):
  momentum_top3.json  — metrics, weekly equity curves, current signals
  momentum_top3.html  — standalone dark-theme dashboard (no external deps)

Usage:
  python3 scripts/momentum_top3.py              # fetch prices (cached)
  python3 scripts/momentum_top3.py --no-fetch   # offline, reuse cache
"""
from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import math
import sys
from html.parser import HTMLParser
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "data" / "momentum_config.json"
DEFAULT_OUT = ROOT / "output" / "momentum_top3"
PRICE_CACHE = ROOT / "output" / "momentum_prices.csv.gz"
try:
    pd.tseries.frequencies.to_offset("ME")
    MONTH_END_FREQ = "ME"
except ValueError:
    MONTH_END_FREQ = "M"

WIKI = {
    "SPX": ("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies", "Symbol"),
    "NDX": ("https://en.wikipedia.org/wiki/Nasdaq-100", "Ticker"),
    "DJIA": ("https://en.wikipedia.org/wiki/Dow_Jones_Industrial_Average", "Symbol"),
}
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) portfolio-tracker/1.0"}


# ---------------------------------------------------------------- data layer
class _WikiTableParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tables = []
        self._table = None
        self._row = None
        self._cell = None

    def handle_starttag(self, tag, attrs):
        if tag == "table" and self._table is None:
            self._table = []
        elif tag == "tr" and self._table is not None:
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell = []

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag):
        if tag in {"td", "th"} and self._cell is not None and self._row is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None and self._table is not None:
            if any(self._row):
                self._table.append(self._row)
            self._row = None
        elif tag == "table" and self._table is not None:
            if self._table:
                self.tables.append(self._table)
            self._table = None


def _tickers_from_html_table(html, column):
    """Small stdlib fallback for Wikipedia constituents when pandas lacks lxml."""
    parser = _WikiTableParser()
    parser.feed(html)
    tickers = set()
    for table in parser.tables:
        if len(table) <= 20:
            continue
        header_idx = None
        col_idx = None
        for i, row in enumerate(table[:5]):
            normalized = [c.strip() for c in row]
            if column in normalized:
                header_idx = i
                col_idx = normalized.index(column)
                break
        if header_idx is None or col_idx is None:
            continue
        for row in table[header_idx + 1:]:
            if col_idx < len(row):
                ticker = row[col_idx].strip().replace(".", "-")
                if ticker and ticker.lower() != "nan":
                    tickers.add(ticker)
        if tickers:
            break
    return tickers


def fetch_universe(indices):
    import requests

    tickers = set()
    for idx in indices:
        url, col = WIKI[idx]
        r = requests.get(url, headers=UA, timeout=30)
        r.raise_for_status()
        found = set()
        try:
            for tbl in pd.read_html(io.StringIO(r.text)):
                cols = [str(c) for c in tbl.columns]
                if col in cols and len(tbl) > 20:
                    found = {str(t).strip().replace(".", "-") for t in tbl[col]}
                    break
        except ImportError:
            found = _tickers_from_html_table(r.text, col)
        if not found:
            found = _tickers_from_html_table(r.text, col)
        if not found:
            raise RuntimeError(f"Could not parse {idx} constituents from {url}")
        tickers |= found
    return sorted(t for t in tickers if t and t != "nan")


def load_prices(cfg, no_fetch=False):
    if no_fetch:
        if not PRICE_CACHE.exists():
            sys.exit(f"--no-fetch but no cache at {PRICE_CACHE}")
        return pd.read_csv(PRICE_CACHE, index_col=0, parse_dates=True)
    import yfinance as yf

    tickers = fetch_universe(cfg["universe"]["indices"])
    tickers = sorted(set(tickers) | set(cfg["backtest"]["benchmarks"]))
    end = dt.date.today() + dt.timedelta(days=1)
    start = end - dt.timedelta(days=int(365.25 * (cfg["backtest"]["years"] + 3.2)))
    px = yf.download(tickers, start=str(start), end=str(end), auto_adjust=True,
                     threads=True, progress=False)["Close"]
    if isinstance(px, pd.Series):
        px = px.to_frame()
    px = px.dropna(axis=1, how="all").sort_index()
    PRICE_CACHE.parent.mkdir(parents=True, exist_ok=True)
    px.to_csv(PRICE_CACHE, compression="gzip")
    return px


# ------------------------------------------------------------- signal layer
def asof_price(hist, months):
    ts = hist.index[-1] - pd.DateOffset(months=months)
    idx = hist.index[hist.index >= ts]
    if not len(idx):
        return hist.iloc[0] * np.nan
    return hist.loc[idx[0]]


def valid_columns(hist, months=13, coverage=0.95):
    w = hist.loc[hist.index >= hist.index[-1] - pd.DateOffset(months=months)]
    return w.columns[w.notna().sum() >= int(len(w) * coverage)]


def momentum_scores(hist, lookback_months, coverage=0.95, exclude=()):
    cols = [c for c in valid_columns(hist, coverage=coverage) if c not in exclude]
    h = hist[cols]
    return (h.iloc[-1] / asof_price(h, lookback_months) - 1).dropna()


# ----------------------------------------------------------- backtest layer
def month_end_index(px):
    return px.groupby(px.index.to_period("M")).tail(1).index


def run_backtest(px, lookback_months, top_n, start, end, cost_per_side,
                 coverage=0.95, exclude=()):
    """Monthly rebalance, equal weight top_n. Returns (equity, avg_turnover)."""
    me = month_end_index(px)
    me = me[(me >= start - pd.DateOffset(days=40)) & (me <= end)]
    daily_ret = px.pct_change(fill_method=None)
    equity, dates = [1.0], []
    prev_w = pd.Series(dtype=float)
    turnover_sum, months = 0.0, 0
    for i in range(len(me) - 1):
        t0, t1 = me[i], me[i + 1]
        if t0 < start - pd.DateOffset(days=35):
            continue
        scores = momentum_scores(px.loc[:t0], lookback_months,
                                 coverage=coverage, exclude=exclude)
        top = scores.sort_values(ascending=False).head(top_n)
        w = (pd.Series(1 / len(top), index=top.index)
             if len(top) else pd.Series(dtype=float))
        both = w.index.union(prev_w.index)
        turn = (w.reindex(both, fill_value=0)
                - prev_w.reindex(both, fill_value=0)).abs().sum() / 2
        turnover_sum += turn
        months += 1
        seg = daily_ret.loc[(daily_ret.index > t0) & (daily_ret.index <= t1),
                            w.index] if len(w) else pd.DataFrame()
        eq = equity[-1] * (1 - turn * 2 * cost_per_side)
        if len(w):
            port = seg.fillna(0) @ w.values
            for d, r in port.items():
                eq *= (1 + r)
                equity.append(eq)
                dates.append(d)
            growth = (1 + seg.fillna(0)).prod()
            drifted = w * growth
            prev_w = drifted / max(drifted.sum(), 1e-12)
        else:
            prev_w = pd.Series(dtype=float)
    ser = pd.Series(equity[1:], index=pd.DatetimeIndex(dates))
    return ser[~ser.index.duplicated(keep="last")], turnover_sum / max(months, 1)


def perf_metrics(eq):
    if len(eq) < 2:
        return {"total_x": None, "cagr_pct": None, "max_dd_pct": None, "sharpe": None}
    r = eq.pct_change().dropna()
    yrs = max((eq.index[-1] - eq.index[0]).days / 365.25, 1e-9)
    cagr = float(eq.iloc[-1]) ** (1 / yrs) - 1
    dd = float((eq / eq.cummax() - 1).min())
    sharpe = float(r.mean() / r.std() * math.sqrt(252)) if r.std() > 0 else None
    return {"total_x": round(float(eq.iloc[-1]), 2),
            "cagr_pct": round(cagr * 100, 1),
            "max_dd_pct": round(dd * 100, 1),
            "sharpe": round(sharpe, 2) if sharpe is not None else None}


def cycle_stats(eq, months_back=13):
    """周期位置: 基于策略净值的 MTD/1m/3m、距高点回撤、逐月收益、阶段判定."""
    if len(eq) < 2:
        return {"mtd_pct": None, "r1m_pct": None, "r3m_pct": None,
                "dd_from_peak_pct": None, "peak_date": None, "phase": "早期",
                "monthly_returns": []}
    end = eq.index[-1]

    def ret_since(t0, include_t0=False):
        # MTD 口径: 基准=t0(月初)前最后一个收盘, 即上月末; 滚动 1m/3m 口径: 基准=t0 当日或之前最后收盘
        cutoff = eq.index <= t0 if include_t0 else eq.index < t0
        prior, sub = eq.loc[cutoff], eq.loc[~cutoff]
        if len(prior):
            base = float(prior.iloc[-1])
        elif len(sub) > 1:
            base, sub = float(sub.iloc[0]), sub.iloc[1:]
        else:
            return None
        if not len(sub) or base <= 0:
            return None
        return round(float(sub.iloc[-1]) / base * 100 - 100, 1)

    mtd = ret_since(end.replace(day=1))
    r1m = ret_since(end - pd.DateOffset(months=1), include_t0=True)
    r3m = ret_since(end - pd.DateOffset(months=3), include_t0=True)
    peak = eq.cummax()
    dd = round(float(eq.iloc[-1] / peak.iloc[-1] - 1) * 100, 1)
    peak_date = str(eq.idxmax().date())
    if dd <= -20:   phase = "深回撤"
    elif dd <= -8:  phase = "回吐中"
    elif (r3m or 0) >= 30: phase = "成熟"
    else:           phase = "早期"
    mr = eq.resample(MONTH_END_FREQ).last().pct_change().dropna().tail(months_back)
    monthly = [[p.strftime("%Y-%m"), round(float(v)*100, 1)] for p, v in mr.items()]
    if monthly and end < mr.index[-1]:      # 数据未到月末 → 末根为进行中的部分月
        monthly[-1][0] += "*"
    return {"mtd_pct": mtd, "r1m_pct": r1m, "r3m_pct": r3m,
            "dd_from_peak_pct": dd, "peak_date": peak_date, "phase": phase,
            "monthly_returns": monthly}


def live_plan_status(plan, strategy_eq):
    """分批建仓进度: 已部署比例、下一批、入场以来策略表现(镜像参考)."""
    done = [t for t in plan["tranches"] if t.get("executed")]
    pending = [t for t in plan["tranches"] if not t.get("executed")]
    deployed = round(sum(t["fraction"] for t in done) * 100, 1)
    since = None
    t0 = pd.Timestamp(plan["started"], tz=strategy_eq.index.tz)  # 兼容 tz-aware 价格索引
    sub = strategy_eq.loc[strategy_eq.index >= t0]
    if len(sub) >= 1:
        since = round(float(sub.iloc[-1] / sub.iloc[0] - 1) * 100, 1)
    return {"account_label": plan.get("account_label", ""),
            "strategy_id": plan["strategy_id"], "started": plan["started"],
            "deployed_pct": deployed,
            "tranches": plan["tranches"],
            "next_tranche": min(pending, key=lambda t: t["target_month_end"]) if pending else None,
            "since_entry_pct": since,
            "note": plan.get("note", "")}


def weekly_points(eq):
    w = eq.groupby(eq.index.to_period("W")).last()
    return [[str(p.end_time.date()), round(float(v), 4)] for p, v in w.items()]


# ------------------------------------------------------------ artifact layer
def build_payload(cfg, px):
    bench = cfg["backtest"]["benchmarks"]
    end = px.index[-1]
    start = end - pd.DateOffset(years=cfg["backtest"]["years"])
    cost = cfg["backtest"]["cost_per_side"]
    coverage = cfg["universe"].get("min_history_coverage", 0.95)

    payload = {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "window": {"start": str(start.date()), "end": str(end.date()),
                   "years": cfg["backtest"]["years"]},
        "universe_size": int(px.shape[1] - len(bench)),
        "cost_per_side": cost,
        "disclaimer": ("Survivorship bias: universe is today's index membership; "
                       "absolute returns are upper bounds. Decision support only."),
        "strategies": [], "benchmarks": {},
    }
    for b in bench:
        eq = px[b].loc[px.index >= start].dropna()
        eq = eq / eq.iloc[0]
        payload["benchmarks"][b] = {"metrics": perf_metrics(eq),
                                    "equity_weekly": weekly_points(eq),
                                    "cycle": cycle_stats(eq)}
    eq_by_id = {}
    for s in cfg["strategies"]:
        eq, turn = run_backtest(px, s["lookback_months"], s["top_n"],
                                start, end, cost, coverage, exclude=bench)
        scores = momentum_scores(px, s["lookback_months"],
                                 coverage=coverage, exclude=bench)
        top = scores.sort_values(ascending=False).head(s["top_n"])
        eq_by_id[s["id"]] = eq
        payload["strategies"].append({
            **{k: s[k] for k in ("id", "label", "lookback_months", "top_n",
                                 "weighting", "role", "thesis")},
            "metrics": {**perf_metrics(eq),
                        "monthly_turnover": round(float(turn), 2)},
            "equity_weekly": weekly_points(eq),
            "cycle": cycle_stats(eq),
            "current_signal": {
                "as_of": str(end.date()),
                "holdings": [{"ticker": t, "lookback_return_pct": round(v * 100, 1),
                              "weight_pct": round(100 / s["top_n"], 1)}
                             for t, v in top.items()],
            },
        })
    lp = cfg.get("live_plan")
    if lp and lp.get("strategy_id") in eq_by_id:
        payload["live_plan"] = live_plan_status(lp, eq_by_id[lp["strategy_id"]])
    return payload


def render_html(payload):
    # "</" → "<\/": 防止 config 字符串中的 </script> 提前终结内联脚本
    data = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    return """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>动量 Top-3 策略舱 · Momentum Sleeve</title><style>
:root{--bg:#0b0f14;--card:#121821;--ink:#e6edf3;--mut:#8b98a5;--line:#1f2937;
--g:#3fb950;--r:#f85149;--a1:#58a6ff;--a2:#d29922;--a3:#bc8cff;--b1:#484f58;--b2:#6e7681}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:14px/1.5 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif;padding:18px}
h1{font-size:20px;margin:0 0 2px}.sub{color:var(--mut);font-size:12px;margin-bottom:14px}
.warn{background:#2d1a06;border:1px solid #6a4a12;color:#e3b341;border-radius:8px;
padding:8px 12px;font-size:12px;margin-bottom:16px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:12px;margin-bottom:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px}
.card h2{font-size:15px;margin:0 0 2px}.role{font-size:11px;color:var(--mut);margin-bottom:8px}
.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:6px;margin:10px 0}
.kpi{background:#0d1420;border-radius:6px;padding:6px 8px;text-align:center}
.kpi b{display:block;font-size:16px}.kpi span{font-size:10px;color:var(--mut)}
.pos b{color:var(--g)}.neg b{color:var(--r)}
table{width:100%;border-collapse:collapse;font-size:12px}
th,td{padding:4px 6px;text-align:right;border-bottom:1px solid var(--line)}
th:first-child,td:first-child{text-align:left}th{color:var(--mut);font-weight:500}
.thesis{font-size:11px;color:var(--mut);margin-top:8px;border-top:1px dashed var(--line);padding-top:8px}
#chartcard{margin-bottom:16px}#legend{display:flex;flex-wrap:wrap;gap:12px;font-size:12px;margin-bottom:6px}
#legend span{display:inline-flex;align-items:center;gap:5px}
#legend i{width:14px;height:3px;border-radius:2px;display:inline-block}
svg text{fill:var(--mut);font-size:10px}.hold{font-weight:600}
.cmp th,.cmp td{font-variant-numeric:tabular-nums}
.cycle{font-size:12px;margin:8px 0;color:var(--ink)}
.phase{border:1px solid;border-radius:10px;padding:1px 8px;font-size:11px;margin-right:6px}
.bars{display:flex;gap:3px;align-items:flex-start;height:72px;margin:6px 0}
.bar{flex:1;text-align:center}.bar i{display:block;width:100%;border-radius:2px}
.bar s{text-decoration:none;font-size:9px;color:var(--mut)}
#plancard{margin-bottom:16px}
.progress{height:10px;background:#0d1420;border-radius:5px;overflow:hidden;margin:8px 0}
.progress i{display:block;height:100%;background:var(--a1);border-radius:5px}
.next td{background:#0d1f33}
.mirror{font-size:11px;color:var(--a2);margin-top:6px}
</style></head><body>
<h1>动量 Top-3 策略舱</h1>
<div class="sub" id="sub"></div>
<div class="warn">⚠️ 幸存者偏差:宇宙为<b>今日</b>指数成分,绝对收益是上界;策略间相对排名有效。研究用途,非投资建议。</div>
<div class="card" id="chartcard"><h2>8年净值曲线(对数轴)</h2><div id="legend"></div><div id="chart"></div></div>
<div id="planmount"></div>
<div class="grid" id="cards"></div>
<div class="card"><h2>策略对比总表</h2><table class="cmp" id="cmp"></table></div>
<script>
const D=__DATA__;
const C={RET_11M_top3:'var(--a1)',RET_9M_top3:'var(--a2)',RET_5M_top5:'var(--a3)',SPY:'var(--b1)',QQQ:'var(--b2)'};
document.getElementById('sub').textContent=`窗口 ${D.window.start} → ${D.window.end} · 宇宙 ${D.universe_size} 只 (SPX∪NDX∪DJIA) · 月末信号/次日持有 · 单边成本 ${(D.cost_per_side*1e4).toFixed(0)}bp · 生成于 ${D.generated_at}`;
function fmt(x){return x==null?'—':x}
function series(){const out=[];for(const s of D.strategies)out.push({id:s.id,pts:s.equity_weekly});
for(const[b,v]of Object.entries(D.benchmarks))out.push({id:b,pts:v.equity_weekly});return out}
(function chart(){const S=series(),W=Math.min(document.body.clientWidth-60,1100),H=340,P={l:46,r:8,t:8,b:22};
let ymin=1e9,ymax=-1e9,xmin=null,xmax=null;
for(const s of S)for(const[d,v]of s.pts){ymin=Math.min(ymin,v);ymax=Math.max(ymax,v);
if(!xmin||d<xmin)xmin=d;if(!xmax||d>xmax)xmax=d}
const x0=new Date(xmin).getTime(),x1=new Date(xmax).getTime();
const ly0=Math.log10(ymin),ly1=Math.log10(ymax);
const X=d=>P.l+(new Date(d).getTime()-x0)/(x1-x0)*(W-P.l-P.r);
const Y=v=>P.t+(1-(Math.log10(v)-ly0)/(ly1-ly0))*(H-P.t-P.b);
let g='';const ticks=[0.5,1,2,5,10,20,50,100,200,400];
for(const t of ticks){if(t<ymin||t>ymax)continue;
g+=`<line x1="${P.l}" x2="${W-P.r}" y1="${Y(t)}" y2="${Y(t)}" stroke="#1f2937" stroke-width="1"/><text x="4" y="${Y(t)+3}">${t}x</text>`}
for(let yr=new Date(xmin).getFullYear()+1;yr<=new Date(xmax).getFullYear();yr++){
const xx=X(yr+'-01-01');g+=`<line x1="${xx}" x2="${xx}" y1="${P.t}" y2="${H-P.b}" stroke="#141c26"/><text x="${xx-12}" y="${H-6}">${yr}</text>`}
let paths='';for(const s of S){const p=s.pts.map((q,i)=>(i?'L':'M')+X(q[0]).toFixed(1)+','+Y(q[1]).toFixed(1)).join('');
paths+=`<path d="${p}" fill="none" stroke="${C[s.id]||'#999'}" stroke-width="${s.id in D.benchmarks?1.2:2}" opacity="${s.id in D.benchmarks?0.75:1}"/>`}
document.getElementById('chart').innerHTML=`<svg width="${W}" height="${H}">${g}${paths}</svg>`;
document.getElementById('legend').innerHTML=S.map(s=>{const lab=(D.strategies.find(t=>t.id===s.id)||{}).label||s.id+' (基准)';
return `<span><i style="background:${C[s.id]}"></i>${lab} · ${s.pts[s.pts.length-1][1].toFixed(1)}x</span>`}).join('')})();
document.getElementById('cards').innerHTML=D.strategies.map(s=>{const m=s.metrics;
const cy=s.cycle;let cyc='';
if(cy){const phC={'早期':'var(--g)','成熟':'var(--a2)','回吐中':'var(--r)','深回撤':'var(--r)'}[cy.phase]||'var(--mut)';
cyc=`<div class="cycle"><span class="phase" style="border-color:${phC};color:${phC}">${cy.phase}</span>
 MTD ${fmt(cy.mtd_pct)}% · 1m ${fmt(cy.r1m_pct)}% · 3m ${fmt(cy.r3m_pct)}% · 距高点 <b style="color:var(--r)">${cy.dd_from_peak_pct}%</b> (峰值 ${cy.peak_date})</div>
<div class="bars">${cy.monthly_returns.map(([mo,v])=>{const h=Math.min(Math.abs(v),60)*0.5;
return `<div class="bar" title="${mo}: ${v}%"><i style="height:${h}px;background:${v>=0?'var(--g)':'var(--r)'};margin-top:${v>=0?30-h:30}px"></i><s>${mo.slice(5)}</s></div>`}).join('')}</div>`}
return `<div class="card"><h2 style="color:${C[s.id]}">${s.label}</h2>
<div class="role">${s.id} · 回看 ${s.lookback_months} 月 · Top${s.top_n} 等权 · 月调仓 · ${s.role}</div>
<div class="kpis">
<div class="kpi pos"><b>${fmt(m.total_x)}x</b><span>8年总倍数</span></div>
<div class="kpi pos"><b>${fmt(m.cagr_pct)}%</b><span>CAGR</span></div>
<div class="kpi neg"><b>${fmt(m.max_dd_pct)}%</b><span>最大回撤</span></div>
<div class="kpi"><b>${fmt(m.sharpe)}</b><span>Sharpe</span></div></div>
${cyc}
<table><tr><th>当前信号 (${s.current_signal.as_of})</th><th>回看收益</th><th>权重</th></tr>
${s.current_signal.holdings.map(h=>`<tr><td class="hold">${h.ticker}</td><td>+${h.lookback_return_pct}%</td><td>${h.weight_pct}%</td></tr>`).join('')}</table>
<div class="thesis">${s.thesis}</div></div>`}).join('');
(function plan(){const p=D.live_plan;if(!p)return;
const nx=p.next_tranche;
document.getElementById('planmount').innerHTML=`<div class="card" id="plancard">
<h2>分批建仓进度 · ${p.account_label} (${p.strategy_id})</h2>
<div class="role">开始 ${p.started} · ${p.note}</div>
<div class="progress"><i style="width:${p.deployed_pct}%"></i></div>
<div style="font-size:12px;margin-bottom:8px">已部署 <b>${p.deployed_pct}%</b>${nx?` · 下一批: 第${nx.seq}批 目标 <b style="color:var(--a1)">${nx.target_month_end}</b> (${(nx.fraction*100).toFixed(1)}%)`:' · 全部完成'}</div>
<table><tr><th>批次</th><th>目标月末</th><th>份额</th><th>状态</th><th>实际日期</th></tr>
${p.tranches.map(t=>`<tr class="${nx&&t.seq===nx.seq?'next':''}"><td>第${t.seq}批</td><td>${t.target_month_end}</td><td>${(t.fraction*100).toFixed(1)}%</td><td>${t.executed?'✅ 已执行':'⏳ 待执行'}</td><td>${t.date||'—'}</td></tr>`).join('')}</table>
<div class="mirror">入场以来策略表现: <b>${fmt(p.since_entry_pct)}%</b>(策略净值镜像,非账户实际;含滑点/时点差)</div></div>`})();
(function cmp(){const rows=[...D.strategies.map(s=>({n:s.label,m:{...s.metrics}})),
...Object.entries(D.benchmarks).map(([b,v])=>({n:b+'(基准)',m:{...v.metrics,monthly_turnover:0}}))];
document.getElementById('cmp').innerHTML='<tr><th>策略</th><th>总倍数</th><th>CAGR</th><th>最大回撤</th><th>Sharpe</th><th>月换手</th></tr>'+
rows.map(r=>`<tr><td>${r.n}</td><td>${fmt(r.m.total_x)}x</td><td>${fmt(r.m.cagr_pct)}%</td><td>${fmt(r.m.max_dd_pct)}%</td><td>${fmt(r.m.sharpe)}</td><td>${(r.m.monthly_turnover*100).toFixed(0)}%</td></tr>`).join('')})();
</script></body></html>""".replace("__DATA__", data)


def main():
    ap = argparse.ArgumentParser(description="Momentum Top-3 sleeve artifact")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT))
    ap.add_argument("--no-fetch", action="store_true",
                    help="reuse cached prices (offline)")
    args = ap.parse_args()

    cfg = json.loads(Path(args.config).read_text())
    px = load_prices(cfg, no_fetch=args.no_fetch)
    payload = build_payload(cfg, px)

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "momentum_top3.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1))
    (out / "momentum_top3.html").write_text(render_html(payload))
    for s in payload["strategies"]:
        h = ", ".join(x["ticker"] for x in s["current_signal"]["holdings"])
        m = s["metrics"]
        print(f"{s['id']:14s} {m['total_x']:>7}x  CAGR {m['cagr_pct']:>5}%  "
              f"DD {m['max_dd_pct']:>6}%  Sharpe {m['sharpe']}  -> {h}")
    print(f"wrote {out/'momentum_top3.json'} and .html")


if __name__ == "__main__":
    main()

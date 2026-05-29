#!/usr/bin/env python3
"""
Portfolio Timeline Dashboard generator
======================================
Reads a Fidelity *Portfolio Positions* CSV (current holdings) and a Fidelity
*Account History* CSV (transactions), fetches real historical prices from
Yahoo Finance, and produces a single self-contained interactive HTML dashboard.

Designed to be re-run whenever you export fresh CSVs:

    python3 generate.py                      # auto-detect newest CSVs in ~/Downloads
    python3 generate.py --portfolio P.csv --history H.csv
    python3 generate.py --no-fetch           # reuse cached prices (offline)
    python3 generate.py --out my.html

Nothing is hard-coded: ticker list, date range, holdings and costs are all
derived from the two CSV files at run time.  See SKILLS.md for methodology.
"""
import csv, re, json, glob, os, sys, argparse, datetime

HOME = os.path.expanduser("~")
HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "output", "prices_cache.json")

# ---------------------------------------------------------------- helpers
def fnum(s):
    s = str(s).strip().replace(",", "").replace("$", "").replace("+", "")
    if s in ("", "--"):
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0

def iso(d):
    m, dd, y = d.split("/")
    return f"{y}-{int(m):02d}-{int(dd):02d}"

def newest(pattern, where):
    hits = sorted(glob.glob(os.path.join(where, pattern)), key=os.path.getmtime, reverse=True)
    return hits[0] if hits else None

# ---------------------------------------------------------------- parsing
def parse_history(path):
    """Return (txns, opt_txns, names, deposits, totals, date_min, date_max)."""
    from collections import defaultdict
    txns, opt_txns, names = defaultdict(list), defaultdict(list), {}
    deposits = tot_buy = tot_sell = 0.0
    dmin, dmax = None, None
    with open(path) as f:
        for r in csv.reader(f):
            if len(r) < 13 or r[0] == "Run Date" or not re.match(r"\d{2}/\d{2}/\d{4}", r[0]):
                continue
            date, action, sym, desc, price, qty, amt = r[0], r[1], r[2].strip(), r[3], r[5], r[6], r[10]
            d = iso(date)
            dmin = d if dmin is None or d < dmin else dmin
            dmax = d if dmax is None or d > dmax else dmax
            if "Electronic Funds" in action or "TRANSFERRED" in action:
                deposits += fnum(amt)
                continue
            if sym == "":
                continue
            is_opt = sym.startswith("-") or "CALL" in action or "PUT" in action
            side = "BUY" if "BOUGHT" in action else ("SELL" if "SOLD" in action else "?")
            rec = {"date": d, "side": side, "qty": abs(fnum(qty)),
                   "price": fnum(price), "amount": fnum(amt)}
            if is_opt:
                opt_txns[sym].append(rec)
            else:
                txns[sym].append(rec)
                names.setdefault(sym, desc.strip())
                if rec["amount"] < 0:
                    tot_buy += -rec["amount"]
                else:
                    tot_sell += rec["amount"]
    return txns, opt_txns, names, deposits, (tot_buy, tot_sell), dmin, dmax

def parse_portfolio(path):
    """Return dict: sym -> {shares, price, value, gain, cost, avg, gainpct}."""
    cur = {}
    with open(path) as f:
        for r in csv.reader(f):
            if len(r) < 14 or r[0] == "Account Number" or not r[0].startswith("Z"):
                continue
            sym = r[2].strip()
            if sym in ("SPAXX**", "Pending activity", "") or sym.startswith("-"):
                continue
            shares, price, val = fnum(r[4]), fnum(r[5]), fnum(r[7])
            gain, cost = fnum(r[10]), fnum(r[13])
            d = cur.setdefault(sym, {"shares": 0.0, "price": price, "value": 0.0, "gain": 0.0, "cost": 0.0})
            d["shares"] += shares; d["value"] += val; d["gain"] += gain; d["cost"] += cost; d["price"] = price
    for d in cur.values():
        d["avg"] = d["cost"] / d["shares"] if d["shares"] else 0
        d["gainpct"] = d["gain"] / d["cost"] * 100 if d["cost"] else 0
    return cur

# ---------------------------------------------------------------- prices
def fetch_prices(tickers, start, end, no_fetch=False):
    if no_fetch and os.path.exists(CACHE):
        print("· using cached prices (--no-fetch)")
        return json.load(open(CACHE))
    try:
        import warnings; warnings.filterwarnings("ignore")
        import yfinance as yf
    except ImportError:
        print("!! yfinance not installed (pip install yfinance). Falling back to cache.")
        return json.load(open(CACHE)) if os.path.exists(CACHE) else {}
    print(f"· fetching {len(tickers)} tickers from Yahoo Finance {start}→{end} ...")
    out = {}
    try:
        data = yf.download(list(tickers), start=start, end=end, progress=False,
                           auto_adjust=True, group_by="ticker")
        for t in tickers:
            try:
                sub = data[t]["Close"].dropna() if len(tickers) > 1 else data["Close"].dropna()
                series = {d.strftime("%Y-%m-%d"): round(float(v), 2) for d, v in sub.items()}
                if series:
                    out[t] = series
            except Exception:
                pass
    except Exception as e:
        print(f"!! fetch failed ({e}); using cache if available")
        return json.load(open(CACHE)) if os.path.exists(CACHE) else {}
    missing = [t for t in tickers if t not in out]
    print(f"· got {len(out)}/{len(tickers)} tickers" + (f"  missing: {missing}" if missing else ""))
    json.dump(out, open(CACHE, "w"))
    return out

def price_on(prices, sym, isodate):
    ps = prices.get(sym, {})
    if not ps:
        return None
    if isodate in ps:
        return ps[isodate]
    prev = None
    for k in sorted(ps):
        if k <= isodate:
            prev = ps[k]
        else:
            break
    return prev if prev is not None else ps[min(ps)]

# ---------------------------------------------------------------- engine
def build_payload(txns, opt_txns, names, cur, prices, deposits, totals, dmin, dmax):
    tot_buy, tot_sell = totals
    allsyms = sorted(set(list(txns) + list(cur)))
    stocks, total_realized = [], 0.0
    for sym in allsyms:
        tl = sorted(txns.get(sym, []), key=lambda x: x["date"])
        net = sum((t["qty"] if t["side"] == "BUY" else -t["qty"]) for t in tl)
        final = cur.get(sym, {}).get("shares", 0.0)
        open_qty = round(final - net, 4)
        held = final > 0.0001
        first_date = tl[0]["date"] if tl else dmin
        est_open = price_on(prices, sym, first_date) or (tl[0]["price"] if tl else 0)
        has_legacy = open_qty > 0.001
        qty = open_qty if open_qty > 0 else 0.0
        cost = qty * est_open
        realized = 0.0
        rows = []
        if has_legacy:
            rows.append({"date": "≤" + dmin, "side": "OPEN", "qty": round(open_qty, 3),
                         "price": round(est_open, 2), "amount": round(-open_qty * est_open, 2),
                         "pos": round(qty, 3), "avg": round(est_open, 2), "realized": None})
        for t in tl:
            if t["side"] == "BUY":
                qty += t["qty"]; cost += abs(t["amount"]); r = None
            else:
                avg = cost / qty if qty > 1e-9 else t["price"]
                r = (t["price"] - avg) * t["qty"]; realized += r
                cost = max(0.0, cost - avg * t["qty"]); qty = max(0.0, qty - t["qty"])
            avgnow = cost / qty if qty > 1e-9 else 0
            rows.append({"date": t["date"], "side": t["side"], "qty": round(t["qty"], 3),
                         "price": round(t["price"], 2), "amount": round(t["amount"], 2),
                         "pos": round(qty, 3), "avg": round(avgnow, 2),
                         "realized": (round(r, 2) if r is not None else None)})
        total_realized += realized
        c = cur.get(sym, {})
        stocks.append({
            "sym": sym, "name": names.get(sym, sym), "held": held,
            "shares": round(final, 3), "avg": round(c.get("avg", 0), 2),
            "curPrice": round(c.get("price", 0) or (price_on(prices, sym, dmax) or 0), 2),
            "value": round(c.get("value", 0), 2), "unreal": round(c.get("gain", 0), 2),
            "unrealPct": round(c.get("gainpct", 0), 2), "cost": round(c.get("cost", 0), 2),
            "realized": round(realized, 2), "hasLegacy": has_legacy,
            "numTrades": len(tl), "prices": sorted(prices.get(sym, {}).items()), "txns": rows})

    opts, opt_net = [], 0.0
    for sym in sorted(opt_txns):
        tl = sorted(opt_txns[sym], key=lambda x: x["date"])
        net = sum(t["amount"] for t in tl); opt_net += net
        opts.append({"sym": sym, "net": round(net, 2),
                     "txns": [{"date": t["date"], "side": t["side"], "qty": t["qty"],
                               "price": t["price"], "amount": round(t["amount"], 2)} for t in tl]})

    # ---- daily portfolio net-worth + time-weighted return vs benchmarks ----
    stock_syms = sorted(set(txns) | set(cur))

    def shares_after(sym, d):
        """Shares held at END of day d (anchored to current holdings)."""
        f = cur.get(sym, {}).get("shares", 0.0)
        chg = sum((t["qty"] if t["side"] == "BUY" else -t["qty"])
                  for t in txns.get(sym, []) if t["date"] > d)
        return f - chg

    # market-day axis = S&P 500 trading days (fallback NASDAQ, then any stock)
    axis_src = prices.get("^GSPC") or prices.get("^IXIC") or {}
    axis = [d for d in sorted(axis_src) if dmin <= d <= dmax]
    if not axis:
        alldays = sorted({d for s, ps in prices.items() if not s.startswith("^") for d in ps})
        axis = [d for d in alldays if dmin <= d <= dmax]

    def pval(d, shares_date=None):
        """Portfolio equity value on day d, optionally using holdings as of shares_date."""
        sd = shares_date or d
        tot = 0.0
        for sym in stock_syms:
            p = price_on(prices, sym, d)
            if p:
                tot += shares_after(sym, sd) * p
        return tot

    sp_base = price_on(prices, "^GSPC", axis[0]) if axis else None
    nq_base = price_on(prices, "^IXIC", axis[0]) if axis else None
    series = []
    cum = 1.0
    prevd = None
    vprev = 0.0
    for i, d in enumerate(axis):
        v = pval(d)
        if i == 0 or vprev <= 0:
            cumret = 0.0
        else:
            today_on_prev = pval(d, shares_date=prevd)   # yesterday's holdings at today's prices
            r = today_on_prev / vprev - 1
            cum *= (1 + r)
            cumret = cum - 1
        sp = price_on(prices, "^GSPC", d)
        nq = price_on(prices, "^IXIC", d)
        series.append({"date": d, "value": round(v, 2), "ret": round(cumret * 100, 3),
                       "sp500": (round((sp / sp_base - 1) * 100, 3) if sp and sp_base else None),
                       "nasdaq": (round((nq / nq_base - 1) * 100, 3) if nq and nq_base else None)})
        prevd, vprev = d, v

    held_val = sum(s["value"] for s in stocks if s["held"])
    held_unreal = sum(s["unreal"] for s in stocks if s["held"])
    last = series[-1] if series else {}
    summary = {"marketValue": round(held_val, 2), "unrealized": round(held_unreal, 2),
               "realized": round(total_realized, 2), "netInvested": round(tot_buy - tot_sell, 2),
               "totalBuy": round(tot_buy, 2), "totalSell": round(tot_sell, 2),
               "deposits": round(deposits, 2), "optNet": round(opt_net, 2),
               "dateRange": [dmin, dmax], "numStocks": len(stocks),
               "numHeld": sum(1 for s in stocks if s["held"]),
               "curReturn": last.get("ret", 0), "spReturn": last.get("sp500"),
               "nasdaqReturn": last.get("nasdaq"), "netWorthNow": last.get("value", 0),
               "netWorthStart": series[0]["value"] if series else 0}
    return {"summary": summary, "stocks": stocks, "options": opts, "series": series}

# ---------------------------------------------------------------- HTML
def render_html(payload):
    DATA = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    tpl = HTML_TEMPLATE.replace("__DATA__", DATA)
    return tpl

HTML_TEMPLATE = r'''<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>投资组合时间线</title>
<style>
:root{--bg:#0b0f1a;--panel:#141b2d;--panel2:#1b2438;--line:#26304a;--txt:#e6ecf5;--mut:#8a97b0;
--green:#34d399;--red:#f87171;--blue:#60a5fa;--orange:#fbbf24;--purple:#a78bfa;}
*{box-sizing:border-box}
body{margin:0;font-family:-apple-system,"PingFang SC","Segoe UI",Roboto,sans-serif;background:var(--bg);color:var(--txt);}
header{padding:18px 24px;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:16px;flex-wrap:wrap;background:linear-gradient(180deg,#10172a,#0b0f1a);}
header h1{font-size:18px;margin:0;font-weight:650}
header .sub{color:var(--mut);font-size:13px}
.kpis{display:flex;gap:12px;flex-wrap:wrap;padding:16px 24px}
.kpi{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:12px 16px;min-width:150px;flex:1}
.kpi .l{color:var(--mut);font-size:12px;margin-bottom:4px}
.kpi .v{font-size:20px;font-weight:700}
.wrap{display:flex;gap:16px;padding:0 24px 24px;align-items:flex-start}
.left{width:300px;flex:none;background:var(--panel);border:1px solid var(--line);border-radius:12px;overflow:hidden;position:sticky;top:12px}
.controls{padding:10px;border-bottom:1px solid var(--line);display:flex;flex-direction:column;gap:8px}
.controls input,.controls select{background:var(--panel2);border:1px solid var(--line);color:var(--txt);border-radius:8px;padding:7px 9px;font-size:13px;width:100%}
.tabs{display:flex;gap:6px}
.tabs button{flex:1;background:var(--panel2);border:1px solid var(--line);color:var(--mut);border-radius:8px;padding:6px;font-size:12px;cursor:pointer}
.tabs button.on{background:var(--blue);color:#06122b;border-color:var(--blue);font-weight:600}
.list{max-height:72vh;overflow:auto}
.row{display:flex;justify-content:space-between;align-items:center;padding:9px 12px;border-bottom:1px solid #1c2336;cursor:pointer;gap:8px}
.row:hover{background:var(--panel2)}
.row.sel{background:#1f2a44;border-left:3px solid var(--blue)}
.row .sym{font-weight:650;font-size:14px}
.row .meta{font-size:11px;color:var(--mut)}
.row .pnl{font-size:13px;font-weight:600;text-align:right;white-space:nowrap}
.right{flex:1;min-width:0}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:18px;margin-bottom:16px}
.dh{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;margin-bottom:6px}
.dh .t{font-size:22px;font-weight:700}
.dh .nm{color:var(--mut);font-size:13px}
.badges{display:flex;gap:18px;flex-wrap:wrap;margin:10px 0 4px}
.badge .l{color:var(--mut);font-size:11px}
.badge .v{font-size:16px;font-weight:650}
.pos{color:var(--green)}.neg{color:var(--red)}
.legend{display:flex;gap:16px;flex-wrap:wrap;font-size:12px;color:var(--mut);margin:6px 0 2px}
.legend i{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:5px;vertical-align:middle}
.chartbox{position:relative;width:100%;overflow:hidden}
svg{width:100%;height:auto;display:block}
.tt{position:fixed;pointer-events:none;background:#0a1326ee;border:1px solid var(--line);border-radius:8px;padding:8px 10px;font-size:12px;z-index:9;display:none;min-width:150px}
table{width:100%;border-collapse:collapse;font-size:12.5px;margin-top:8px}
th,td{padding:7px 8px;text-align:right;border-bottom:1px solid #1c2336;white-space:nowrap}
th{color:var(--mut);font-weight:600;position:sticky;top:0;background:var(--panel)}
td.l,th.l{text-align:left}
.tag{font-size:10px;padding:2px 6px;border-radius:6px;font-weight:600}
.tag.b{background:#0d3b2e;color:var(--green)}
.tag.s{background:#3b1414;color:var(--red)}
.tag.o{background:#2a2f45;color:var(--mut)}
.note{color:var(--mut);font-size:12px;line-height:1.6}
.scroll{max-height:420px;overflow:auto}
details{margin-top:4px}
summary{cursor:pointer;color:var(--blue);font-size:13px}
.legacychip{font-size:10px;background:#3a2d10;color:var(--orange);padding:2px 7px;border-radius:6px;margin-left:6px}
</style>
</head>
<body>
<header>
 <h1>📈 投资组合时间线</h1>
 <span class="sub" id="rangelbl"></span>
</header>
<div class="kpis" id="kpis"></div>
<div class="wrap">
 <div class="left">
   <div class="controls">
     <input id="search" placeholder="🔍 搜索代码…"/>
     <select id="sort">
       <option value="value">按市值排序</option>
       <option value="unreal">按未实现盈亏</option>
       <option value="realized">按已实现盈亏</option>
       <option value="numTrades">按交易次数</option>
       <option value="sym">按代码字母</option>
     </select>
     <div class="tabs">
       <button data-f="held" class="on">持有中</button>
       <button data-f="exited">已清仓</button>
       <button data-f="all">全部</button>
     </div>
   </div>
   <div class="list" id="list"></div>
 </div>
 <div class="right" id="right"></div>
</div>
<div class="tt" id="tt"></div>
<script>
const DATA = __DATA__;
const fmt=(n,d=2)=>n==null?'—':(n<0?'-$':'$')+Math.abs(n).toLocaleString('en-US',{minimumFractionDigits:d,maximumFractionDigits:d});
const fmtN=(n,d=0)=>n==null?'—':n.toLocaleString('en-US',{minimumFractionDigits:d,maximumFractionDigits:Math.max(d,2)});
const pct=n=>(n>=0?'+':'')+n.toFixed(2)+'%';
const cls=n=>n>0?'pos':(n<0?'neg':'');
const S=DATA.summary, D0=S.dateRange[0], D1=S.dateRange[1];
document.getElementById('rangelbl').textContent=`数据窗口 ${D0} → ${D1} · 价格来自 Yahoo Finance · 共 ${S.numStocks} 只标的（持有 ${S.numHeld}）`;

const kpis=[
 ['当前持仓市值',fmt(S.marketValue),''],
 ['未实现盈亏 (券商实际)',fmt(S.unrealized),cls(S.unrealized)],
 ['已实现盈亏 (窗口内·含估算)',fmt(S.realized),cls(S.realized)],
 ['期权净现金流',fmt(S.optNet),cls(S.optNet)],
 ['窗口内净买入',fmt(S.netInvested),''],
 ['现金转入',fmt(S.deposits),''],
];
document.getElementById('kpis').innerHTML=kpis.map(k=>`<div class="kpi"><div class="l">${k[0]}</div><div class="v ${k[2]}">${k[1]}</div></div>`).join('');

let filter='held', sortKey='value', q='', sel='__OV__';
const stocks=DATA.stocks;
function filtered(){
 let a=stocks.filter(s=>filter==='all'||(filter==='held'?s.held:!s.held));
 if(q)a=a.filter(s=>s.sym.toLowerCase().includes(q)||(s.name||'').toLowerCase().includes(q));
 const k=sortKey;
 a.sort((x,y)=> k==='sym'? x.sym.localeCompare(y.sym) : (y[k]||0)-(x[k]||0));
 return a;
}
function renderList(){
 const a=filtered();
 const ov=`<div class="row ovrow ${sel==='__OV__'?'sel':''}" data-s="__OV__">
   <div><div class="sym">📊 组合总览</div><div class="meta">净值 & 收益率 vs 指数</div></div>
   <div class="pnl ${cls(S.curReturn)}">${pct(S.curReturn)}<div class="meta">区间收益</div></div></div>`;
 document.getElementById('list').innerHTML=ov+(a.map(s=>{
  const main=s.held?s.unreal:s.realized, lbl=s.held?'未实现':'已实现';
  return `<div class="row ${sel===s.sym?'sel':''}" data-s="${s.sym}">
    <div><div class="sym">${s.sym}${s.hasLegacy?'<span class="legacychip">含旧仓</span>':''}</div>
    <div class="meta">${s.held?fmtN(s.shares)+' 股 @ '+fmt(s.avg):'已清仓 · '+s.numTrades+' 笔'}</div></div>
    <div class="pnl ${cls(main)}">${fmt(main)}<div class="meta">${lbl}</div></div></div>`;}).join('')||'<div style="padding:16px;color:var(--mut)">无匹配</div>');
 document.querySelectorAll('.row').forEach(r=>r.onclick=()=>{sel=r.dataset.s;renderList();renderDetail();});
 renderDetail();
}
function chart(s){
 const W=900,H=420,mL=58,mR=120,mT=18,mB=42;
 const prices=s.prices,txns=s.txns;
 const dates=prices.map(p=>+new Date(p[0]));
 let xmin=Math.min(...dates,+new Date(D0)), xmax=+new Date(D1);
 let ys=[...prices.map(p=>p[1])];
 txns.forEach(t=>{if(t.price)ys.push(t.price);if(t.avg)ys.push(t.avg);});
 if(s.curPrice)ys.push(s.curPrice);
 ys=ys.filter(v=>v>0);
 let ymin=Math.min(...ys),ymax=Math.max(...ys);const pad=(ymax-ymin)*0.08||1;ymin-=pad;ymax+=pad;
 const xs=d=>mL+((+new Date(d)-xmin)/(xmax-xmin))*(W-mL-mR);
 const yc=v=>mT+(1-(v-ymin)/(ymax-ymin))*(H-mT-mB);
 let el='';
 for(let i=0;i<=4;i++){const v=ymin+(ymax-ymin)*i/4,y=yc(v);
   el+=`<line x1="${mL}" y1="${y}" x2="${W-mR}" y2="${y}" stroke="#1c2336"/>`;
   el+=`<text x="${mL-8}" y="${y+4}" fill="#8a97b0" font-size="11" text-anchor="end">$${v.toFixed(0)}</text>`;}
 for(let i=0;i<=5;i++){const t=xmin+(xmax-xmin)*i/5,x=xs(new Date(t)),dt=new Date(t);
   el+=`<line x1="${x}" y1="${mT}" x2="${x}" y2="${H-mB}" stroke="#161d2e"/>`;
   el+=`<text x="${x}" y="${H-mB+18}" fill="#8a97b0" font-size="11" text-anchor="middle">${(dt.getMonth()+1)}/${dt.getDate()}</text>`;}
 if(prices.length){const pts=prices.map(p=>`${xs(p[0]).toFixed(1)},${yc(p[1]).toFixed(1)}`).join(' ');
   el+=`<polyline points="${pts}" fill="none" stroke="#5b6b8c" stroke-width="1.6" stroke-opacity="0.9"/>`;}
 const av=txns.filter(t=>t.avg>0&&t.pos>0.0001);
 if(av.length){let d2='',px=null;
   av.forEach((t,i)=>{const x=xs(t.date[0]==='≤'?D0:t.date),y=yc(t.avg);
     if(i===0)d2=`M ${x} ${y}`;else d2+=` L ${px} ${y} L ${x} ${y}`;px=x;});
   d2+=` L ${xs(D1)} ${yc(av[av.length-1].avg)}`;
   el+=`<path d="${d2}" fill="none" stroke="#fbbf24" stroke-width="1.8" stroke-dasharray="5 3" stroke-opacity="0.85"/>`;}
 if(s.curPrice&&s.held){const y=yc(s.curPrice);
   el+=`<line x1="${mL}" y1="${y}" x2="${W-mR}" y2="${y}" stroke="#60a5fa" stroke-width="1.3" stroke-dasharray="2 3"/>`;
   el+=`<text x="${W-mR+6}" y="${y+4}" fill="#60a5fa" font-size="11">现价 $${s.curPrice}</text>`;}
 const amts=txns.filter(t=>t.side!=='OPEN').map(t=>Math.abs(t.amount)),amax=Math.max(...amts,1);
 txns.forEach((t,idx)=>{const dd=t.date[0]==='≤'?D0:t.date,x=xs(dd),y=yc(t.price);
   const r=t.side==='OPEN'?5:Math.max(4,Math.min(15,4+11*Math.sqrt(Math.abs(t.amount)/amax)));
   const col=t.side==='BUY'?'#34d399':(t.side==='SELL'?'#f87171':'#8a97b0');
   el+=`<circle cx="${x}" cy="${y}" r="${r}" fill="${col}" fill-opacity="0.55" stroke="${col}" stroke-width="1.4" data-i="${idx}" data-sym="${s.sym}" class="mk" style="cursor:pointer"/>`;});
 return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet">${el}</svg>`;
}
function svgLines(ser,defs,opts){
 const W=900,H=opts.h||340,mL=66,mR=70,mT=16,mB=40;
 const xmin=+new Date(ser[0].date),xmax=+new Date(ser[ser.length-1].date);
 let vals=[];defs.forEach(d=>ser.forEach(p=>{if(p[d.key]!=null)vals.push(p[d.key]);}));
 if(opts.zero)vals.push(0);
 let ymin=Math.min(...vals),ymax=Math.max(...vals);const pad=(ymax-ymin)*0.08||1;ymin-=pad;ymax+=pad;
 const xs=d=>mL+((+new Date(d)-xmin)/((xmax-xmin)||1))*(W-mL-mR);
 const yc=v=>mT+(1-(v-ymin)/((ymax-ymin)||1))*(H-mT-mB);
 let el='';
 for(let i=0;i<=4;i++){const v=ymin+(ymax-ymin)*i/4,y=yc(v);
   el+=`<line x1="${mL}" y1="${y}" x2="${W-mR}" y2="${y}" stroke="#1c2336"/>`;
   el+=`<text x="${mL-8}" y="${y+4}" fill="#8a97b0" font-size="11" text-anchor="end">${opts.fmt(v)}</text>`;}
 const n=ser.length;
 for(let i=0;i<=5;i++){const idx=Math.round((n-1)*i/5),p=ser[idx],x=xs(p.date),dt=new Date(p.date);
   el+=`<text x="${x}" y="${H-mB+18}" fill="#8a97b0" font-size="11" text-anchor="middle">${dt.getMonth()+1}/${dt.getDate()}</text>`;}
 if(opts.zero){const y=yc(0);el+=`<line x1="${mL}" y1="${y}" x2="${W-mR}" y2="${y}" stroke="#46506e" stroke-dasharray="3 3"/>`;}
 defs.forEach(d=>{const f=ser.filter(p=>p[d.key]!=null);
   const pts=f.map(p=>`${xs(p.date).toFixed(1)},${yc(p[d.key]).toFixed(1)}`).join(' ');
   if(opts.area&&f.length){const base=yc(Math.max(ymin,0));
     el+=`<polygon points="${xs(f[0].date).toFixed(1)},${base} ${pts} ${xs(f[f.length-1].date).toFixed(1)},${base}" fill="${d.color}" fill-opacity="0.10"/>`;}
   el+=`<polyline points="${pts}" fill="none" stroke="${d.color}" stroke-width="2.1" ${d.dash?'stroke-dasharray="5 3"':''}/>`;
   const last=f[f.length-1];
   if(last)el+=`<text x="${xs(last.date)+5}" y="${yc(last[d.key])+4}" fill="${d.color}" font-size="11" font-weight="700">${opts.fmt(last[d.key])}</text>`;});
 return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet">${el}</svg>`;
}
function renderOverview(){
 const ser=DATA.series||[],right=document.getElementById('right');
 if(ser.length<2){right.innerHTML='<div class="card">数据不足，无法生成组合曲线。</div>';return;}
 const pr=S.curReturn,sp=S.spReturn,nq=S.nasdaqReturn;
 const cards=[
  ['当前持仓市值',fmt(S.netWorthNow)],
  ['期初持仓市值',fmt(S.netWorthStart)],
  ['区间收益率 (时间加权)',`<span class="${cls(pr)}">${pct(pr)}</span>`],
  ['同期 S&P 500',sp==null?'—':`<span class="${cls(sp)}">${pct(sp)}</span>`],
  ['同期 纳斯达克',nq==null?'—':`<span class="${cls(nq)}">${pct(nq)}</span>`],
  ['超额收益 vs S&P500',sp==null?'—':`<span class="${cls(pr-sp)}">${pct(pr-sp)}</span>`],
 ];
 right.innerHTML=`
 <div class="card">
   <div class="dh"><span class="t">📊 组合总览</span><span class="nm">持仓市值与收益率（股票部分，不含现金/期权）</span></div>
   <div class="badges">${cards.map(c=>`<div class="badge"><div class="l">${c[0]}</div><div class="v">${c[1]}</div></div>`).join('')}</div>
 </div>
 <div class="card"><div style="font-weight:650;margin-bottom:8px">持仓总市值（$）</div>
   ${svgLines(ser,[{key:'value',color:'#34d399'}],{area:true,fmt:v=>'$'+(v/1000).toFixed(0)+'k'})}</div>
 <div class="card"><div style="font-weight:650;margin-bottom:4px">累计收益率对比（%，时间加权）</div>
   <div class="legend"><span><i style="background:#a78bfa"></i>我的组合</span><span><i style="background:#60a5fa"></i>S&P 500</span><span><i style="background:#fbbf24"></i>纳斯达克综合</span></div>
   ${svgLines(ser,[{key:'ret',color:'#a78bfa'},{key:'sp500',color:'#60a5fa',dash:1},{key:'nasdaq',color:'#fbbf24',dash:1}],{zero:true,fmt:v=>v.toFixed(0)+'%'})}</div>`;
}
function renderDetail(){
 if(sel==='__OV__'){renderOverview();return;}
 const s=stocks.find(x=>x.sym===sel);if(!s){document.getElementById('right').innerHTML='';return;}
 const badges=[['当前持股',s.held?fmtN(s.shares)+' 股':'已清仓'],['平均成本',s.held?fmt(s.avg):'—'],
  ['现价',fmt(s.curPrice)],['市值',s.held?fmt(s.value):'—'],
  ['未实现盈亏',s.held?`<span class="${cls(s.unreal)}">${fmt(s.unreal)} (${pct(s.unrealPct)})</span>`:'—'],
  ['已实现盈亏(窗口)',`<span class="${cls(s.realized)}">${fmt(s.realized)}</span>`]];
 const rows=s.txns.map(t=>{const tag=t.side==='BUY'?'<span class="tag b">买入</span>':(t.side==='SELL'?'<span class="tag s">卖出</span>':'<span class="tag o">期初底仓</span>');
   return `<tr><td class="l">${t.date}</td><td class="l">${tag}</td><td>${fmtN(t.qty,0)}</td><td>${fmt(t.price)}</td>
    <td class="${t.amount<0?'':'pos'}">${fmt(t.amount)}</td><td>${fmtN(t.pos,0)}</td><td>${t.avg?fmt(t.avg):'—'}</td>
    <td class="${cls(t.realized)}">${t.realized==null?'—':fmt(t.realized)}</td></tr>`;}).join('');
 document.getElementById('right').innerHTML=`
 <div class="card">
   <div class="dh"><span class="t">${s.sym}</span><span class="nm">${s.name}</span>
     ${s.hasLegacy?'<span class="legacychip">含 '+D0+' 前旧底仓 · 成本按当日市价估算</span>':''}</div>
   <div class="badges">${badges.map(b=>`<div class="badge"><div class="l">${b[0]}</div><div class="v">${b[1]}</div></div>`).join('')}</div>
   <div class="legend"><span><i style="background:#5b6b8c"></i>市场价格(Yahoo)</span><span><i style="background:#fbbf24"></i>持仓平均成本</span>
     <span><i style="background:#60a5fa"></i>当前价</span><span><i style="background:#34d399"></i>买入(大小=金额)</span><span><i style="background:#f87171"></i>卖出</span></div>
   <div class="chartbox">${chart(s)}</div></div>
 <div class="card"><div style="font-weight:650;margin-bottom:6px">交易明细（${s.numTrades} 笔）</div>
   <div class="scroll"><table><thead><tr><th class="l">日期</th><th class="l">动作</th><th>数量</th><th>成交价</th><th>金额</th><th>持仓后</th><th>均价后</th><th>已实现</th></tr></thead>
   <tbody>${rows}</tbody></table></div></div>`;
 bindMarkers();
}
function bindMarkers(){
 const tt=document.getElementById('tt');
 document.querySelectorAll('.mk').forEach(m=>{
   m.onmousemove=e=>{const s=stocks.find(x=>x.sym===m.dataset.sym),t=s.txns[+m.dataset.i];
     tt.style.display='block';tt.style.left=(e.clientX+14)+'px';tt.style.top=(e.clientY+14)+'px';
     const side=t.side==='BUY'?'买入':(t.side==='SELL'?'卖出':'期初底仓');
     tt.innerHTML=`<b>${m.dataset.sym} · ${side}</b><br>${t.date}<br>数量 ${fmtN(t.qty,0)} @ ${fmt(t.price)}<br>金额 ${fmt(t.amount)}${t.realized!=null?'<br>已实现 <span class="'+cls(t.realized)+'">'+fmt(t.realized)+'</span>':''}`;};
   m.onmouseleave=()=>tt.style.display='none';});
}
function renderOptions(){
 if(!DATA.options.length)return'';
 const rows=DATA.options.map(o=>{const tr=o.txns.map(t=>`<tr><td class="l">${t.date}</td><td class="l">${t.side==='BUY'?'<span class="tag b">买入</span>':'<span class="tag s">卖出</span>'}</td><td>${t.qty}</td><td>${fmt(t.price)}</td><td class="${t.amount<0?'':'pos'}">${fmt(t.amount)}</td></tr>`).join('');
   return `<details><summary>${o.sym} · 净现金流 <span class="${cls(o.net)}">${fmt(o.net)}</span></summary>
     <table><thead><tr><th class="l">日期</th><th class="l">动作</th><th>合约</th><th>权利金</th><th>金额</th></tr></thead><tbody>${tr}</tbody></table></details>`;}).join('');
 return `<div class="card"><div style="font-weight:650;margin-bottom:6px">期权交易（净现金流合计 <span class="${cls(S.optNet)}">${fmt(S.optNet)}</span>）</div>${rows}</div>`;
}
document.getElementById('search').oninput=e=>{q=e.target.value.toLowerCase().trim();renderList();};
document.getElementById('sort').onchange=e=>{sortKey=e.target.value;renderList();};
document.querySelectorAll('.tabs button').forEach(b=>b.onclick=()=>{
 document.querySelectorAll('.tabs button').forEach(x=>x.classList.remove('on'));
 b.classList.add('on');filter=b.dataset.f;renderList();});
renderList();
const obs=()=>{const r=document.getElementById('right');
 if(r&&!document.getElementById('optsec')){const d=document.createElement('div');d.id='optsec';
   d.innerHTML=renderOptions()+`<div class="card note"><b>计算口径说明</b><br>
   • 价格曲线为 Yahoo Finance 实际日收盘价；买卖点的 Y 轴为你的真实成交价。<br>
   • <b>未实现盈亏</b>采用券商最新 Portfolio CSV 实际数据（精确）。<br>
   • <b>已实现盈亏</b>用平均成本法在数据窗口内计算；标「含旧仓」的股票在数据起点前已有底仓，其成本按起点当日市场价估算，故为近似值。<br>
   • 数据窗口 ${D0} → ${D1}，可能并非完整开户记录。</div>`;
   r.appendChild(d);} };
new MutationObserver(obs).observe(document.getElementById('right'),{childList:true});
setTimeout(obs,300);
</script>
</body></html>'''

# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Generate portfolio timeline dashboard from Fidelity CSVs.")
    ap.add_argument("--portfolio", help="Portfolio Positions CSV (current holdings)")
    ap.add_argument("--history", help="Account History CSV (transactions)")
    ap.add_argument("--input-dir", default=os.path.join(HOME, "Downloads"),
                    help="where to auto-detect CSVs (default: ~/Downloads)")
    ap.add_argument("--out", default=os.path.join(HERE, "output", "portfolio_dashboard.html"))
    ap.add_argument("--no-fetch", action="store_true", help="reuse cached prices (offline)")
    args = ap.parse_args()

    portfolio = args.portfolio or newest("Portfolio_Positions*.csv", args.input_dir)
    history = args.history or newest("History_for_Account*.csv", args.input_dir)
    if not portfolio or not history:
        sys.exit("!! Could not find CSVs. Pass --portfolio and --history explicitly.")
    print(f"· portfolio: {portfolio}")
    print(f"· history:   {history}")

    txns, opt_txns, names, deposits, totals, dmin, dmax = parse_history(history)
    cur = parse_portfolio(portfolio)
    tickers = sorted(set(list(txns) + list(cur)))

    # fetch window: a few days before first trade through day after last
    start = (datetime.date.fromisoformat(dmin) - datetime.timedelta(days=4)).isoformat()
    end = (datetime.date.fromisoformat(dmax) + datetime.timedelta(days=1)).isoformat()
    BENCH = ["^GSPC", "^IXIC"]   # S&P 500, NASDAQ Composite
    prices = fetch_prices(sorted(set(tickers) | set(BENCH)), start, end, no_fetch=args.no_fetch)

    payload = build_payload(txns, opt_txns, names, cur, prices, deposits, totals, dmin, dmax)
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    open(args.out, "w").write(render_html(payload))
    s = payload["summary"]
    print(f"\n✓ dashboard written: {args.out}")
    print(f"  window {s['dateRange'][0]}→{s['dateRange'][1]} · {s['numStocks']} stocks ({s['numHeld']} held) · {len(payload['options'])} options")
    print(f"  market value {s['marketValue']:,.0f} · unrealized {s['unrealized']:+,.0f} · realized(window) {s['realized']:+,.0f}")

if __name__ == "__main__":
    main()

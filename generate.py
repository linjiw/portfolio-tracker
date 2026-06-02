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
import csv, re, json, glob, os, sys, argparse, datetime, math

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
def _colmap(header):
    """Map a Fidelity history header row to column indices (handles both export
    layouts: with or without the extra Account/Account Number columns)."""
    idx = {}
    for i, h in enumerate(header):
        h = h.strip().lower()
        if h == "run date": idx["date"] = i
        elif h == "action": idx["action"] = i
        elif h == "symbol": idx["symbol"] = i
        elif h == "description": idx["desc"] = i
        elif h.startswith("price"): idx["price"] = i
        elif h == "quantity": idx["qty"] = i
        elif h.startswith("amount"): idx["amount"] = i
    return idx

def parse_history(path):
    """Header-aware parser. Returns a dict with txns/opt_txns/names, cash flows
    (deposits, dividends), trade totals, date range, and counts used to pick the
    best source file when several exports are present."""
    from collections import defaultdict
    txns, opt_txns, names = defaultdict(list), defaultdict(list), {}
    deposits = dividends = tot_buy = tot_sell = 0.0
    n_buys = n_eft = n_div = 0
    dmin = dmax = None
    cmap = None
    cash_rows = []   # (date, kind, amount) for cross-file union
    with open(path, encoding="utf-8-sig") as f:
        for r in csv.reader(f):
            if cmap is None:
                if r and r[0].strip() == "Run Date":
                    cmap = _colmap(r)
                continue
            if len(r) <= cmap.get("amount", 99) or not re.match(r"\d{2}/\d{2}/\d{4}", r[0]):
                continue
            action = r[cmap["action"]]
            amt = fnum(r[cmap["amount"]])
            sym = r[cmap["symbol"]].strip()
            d = iso(r[cmap["date"]])
            dmin = d if dmin is None or d < dmin else dmin
            dmax = d if dmax is None or d > dmax else dmax
            if "Electronic Funds Transfer Received" in action or "TRANSFERRED" in action:
                deposits += amt
                if "Electronic" in action:
                    n_eft += 1; cash_rows.append((d, "EFT", round(amt, 2)))
                else:
                    cash_rows.append((d, "TRANSFER", round(amt, 2)))
                continue
            if "DIVIDEND RECEIVED" in action or "INTEREST" in action:
                dividends += amt; n_div += 1; cash_rows.append((d, "DIV", round(amt, 2)))
                continue
            if "JOURNALED" in action or sym == "":
                continue
            is_opt = sym.startswith("-") or "CALL" in action or "PUT" in action
            side = "BUY" if "BOUGHT" in action else ("SELL" if "SOLD" in action else "?")
            rec = {"date": d, "side": side, "qty": abs(fnum(r[cmap["qty"]])),
                   "price": fnum(r[cmap["price"]]), "amount": amt}
            if is_opt:
                opt_txns[sym].append(rec)
            else:
                txns[sym].append(rec)
                names.setdefault(sym, r[cmap["desc"]].strip())
                if side == "BUY":
                    tot_buy += -rec["amount"]; n_buys += 1
                elif side == "SELL":
                    tot_sell += rec["amount"]
    return {"txns": txns, "opt_txns": opt_txns, "names": names,
            "deposits": round(deposits, 2), "dividends": round(dividends, 2),
            "totals": (tot_buy, tot_sell), "dmin": dmin, "dmax": dmax,
            "n_buys": n_buys, "n_eft": n_eft, "n_div": n_div,
            "cash_rows": cash_rows, "path": path}

def parse_portfolio(path):
    """Return dict: sym -> {shares, price, value, gain, cost, avg, gainpct}.

    Sums every lot of a symbol across ALL accounts in the export — brokerage
    (Z...) *and* retirement (numeric, e.g. a Rollover IRA "257937289"). Match
    account rows by an alphanumeric account id in col 0 (the header "Account
    Number" and the trailing disclaimer paragraphs both contain spaces, so they
    fail the regex). Skip money-market core positions (Fidelity flags these with
    a "**" suffix: SPAXX**, FDRXX** — they're cash, counted separately), the
    "Pending activity" line, blank symbols, and option rows (symbol starts "-").
    """
    cur = {}
    with open(path) as f:
        for r in csv.reader(f):
            if len(r) < 14 or not re.match(r"^[A-Z0-9]{5,}$", r[0].strip()):
                continue
            sym = r[2].strip()
            if sym.endswith("**") or sym in ("Pending activity", "") or sym.startswith("-"):
                continue
            shares, price, val = fnum(r[4]), fnum(r[5]), fnum(r[7])
            gain, cost = fnum(r[10]), fnum(r[13])
            d = cur.setdefault(sym, {"shares": 0.0, "price": price, "value": 0.0, "gain": 0.0, "cost": 0.0})
            d["shares"] += shares; d["value"] += val; d["gain"] += gain; d["cost"] += cost; d["price"] = price
    for d in cur.values():
        d["avg"] = d["cost"] / d["shares"] if d["shares"] else 0
        d["gainpct"] = d["gain"] / d["cost"] * 100 if d["cost"] else 0
    return cur

# ---------------------------------------------------------------- merge
def merge_histories(parsed):
    """Merge trades across overlapping exports. Each real transaction appears
    once per export that covers its date; overlapping exports repeat it. So we
    take, per identical-transaction key, the MAX count seen in any single file
    (not the sum) — robust dedup that preserves genuine same-day duplicates."""
    from collections import Counter, defaultdict
    sc, oc, names = Counter(), Counter(), {}
    for h in parsed:
        fsc, foc = Counter(), Counter()
        for sym, L in h["txns"].items():
            names.setdefault(sym, h["names"].get(sym, sym))
            for t in L:
                fsc[(t["date"], t["side"], sym, round(t["qty"], 3),
                     round(t["price"], 2), round(t["amount"], 2))] += 1
        for sym, L in h["opt_txns"].items():
            for t in L:
                foc[(t["date"], t["side"], sym, round(t["qty"], 3),
                     round(t["price"], 2), round(t["amount"], 2))] += 1
        for k, n in fsc.items():
            sc[k] = max(sc[k], n)
        for k, n in foc.items():
            oc[k] = max(oc[k], n)
    txns, opt = defaultdict(list), defaultdict(list)
    for store, src in ((txns, sc), (opt, oc)):
        for (d, side, sym, qty, price, amt), n in src.items():
            for _ in range(n):
                store[sym].append({"date": d, "side": side, "qty": qty, "price": price, "amount": amt})
    for L in txns.values():
        L.sort(key=lambda t: t["date"])
    return txns, opt, names

def continuous_start(txns, gap_days=20):
    """Largest gap-free span ending at the latest trade: returns the first date
    after the last coverage gap >= gap_days (missing-data hole, not a quiet week)."""
    dates = sorted({t["date"] for L in txns.values() for t in L})
    if not dates:
        return None, []
    start = dates[0]
    gaps = []
    for a, b in zip(dates, dates[1:]):
        if (datetime.date.fromisoformat(b) - datetime.date.fromisoformat(a)).days >= gap_days:
            gaps.append((a, b)); start = b
    return start, gaps

def union_cash(parsed, lo, hi):
    """Deduped union of cash rows across all exports, within [lo, hi]."""
    seen = set()
    dep = div = 0.0
    for h in parsed:
        for d, kind, amt in h["cash_rows"]:
            if not (lo <= d <= hi):
                continue
            key = (d, kind, amt)
            if key in seen:
                continue
            seen.add(key)
            if kind in ("EFT", "TRANSFER"):
                dep += amt
            elif kind == "DIV":
                div += amt
    return round(dep, 2), round(div, 2)

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

# ---------------------------------------------------------------- Fibonacci indicators
def _ema(vals, n):
    a = 2.0 / (n + 1)
    out, e = [], None
    for v in vals:
        e = v if e is None else a * v + (1 - a) * e
        out.append(e)
    return out

def _rsi(vals, n=14):
    out = [50.0] * len(vals)
    if len(vals) <= n:
        return out
    deltas = [vals[i] - vals[i - 1] for i in range(1, len(vals))]
    ag = sum(d for d in deltas[:n] if d > 0) / n
    al = sum(-d for d in deltas[:n] if d < 0) / n
    rs = ag / al if al > 0 else 999
    out[n] = 100 - 100 / (1 + rs)
    for i in range(n + 1, len(vals)):
        d = deltas[i - 1]
        ag = (ag * (n - 1) + max(d, 0)) / n
        al = (al * (n - 1) + max(-d, 0)) / n
        rs = ag / al if al > 0 else 999
        out[i] = 100 - 100 / (1 + rs)
    for i in range(n):
        out[i] = out[n]
    return out

def compute_fib(items):
    """Fibonacci EMA ribbon (5/8/13/21) + momentum + RSI + crossover signals.
    Momentum = 100*tanh((EMA5-EMA21)/EMA21 / 0.04): signed by fast-vs-slow
    separation, smoothly bounded to ±100. State borrows the Alligator idea:
    tight ribbon = range; cleanly stacked = trend; else transition."""
    dates = [d for d, _ in items]
    px = [p for _, p in items]
    if len(px) < 21:
        return None
    e5, e8, e13, e21 = _ema(px, 5), _ema(px, 8), _ema(px, 13), _ema(px, 21)
    rsi = _rsi(px)
    mom, state = [], []
    for i in range(len(px)):
        sp = (e5[i] - e21[i]) / e21[i] if e21[i] else 0
        mom.append(round(100 * math.tanh(sp / 0.06), 1))
        band = max(e5[i], e8[i], e13[i], e21[i]) - min(e5[i], e8[i], e13[i], e21[i])
        w = band / px[i] * 100 if px[i] else 0
        if w < 0.8:
            state.append("range")
        elif e5[i] > e8[i] > e13[i] > e21[i]:
            state.append("up")
        elif e5[i] < e8[i] < e13[i] < e21[i]:
            state.append("down")
        else:
            state.append("mixed")
    sig = []
    for i in range(1, len(px)):
        if e5[i - 1] <= e13[i - 1] and e5[i] > e13[i]:
            sig.append({"date": dates[i], "type": "golden", "price": round(px[i], 2)})
        elif e5[i - 1] >= e13[i - 1] and e5[i] < e13[i]:
            sig.append({"date": dates[i], "type": "death", "price": round(px[i], 2)})
    # multi-indicator RESONANCE: trend alignment + recent EMA5x13 cross + RSI not extreme
    res = []
    lg = ld = -999
    active = None
    for i in range(len(px)):
        if i >= 1 and e5[i - 1] <= e13[i - 1] and e5[i] > e13[i]:
            lg = i
        if i >= 1 and e5[i - 1] >= e13[i - 1] and e5[i] < e13[i]:
            ld = i
        bull = state[i] == "up" and (i - lg) <= 3 and rsi[i] < 70
        bear = state[i] == "down" and (i - ld) <= 3 and rsi[i] > 30
        cur = "bull" if bull else ("bear" if bear else None)
        if cur and cur != active:
            res.append({"date": dates[i], "price": round(px[i], 2), "rsi": round(rsi[i], 1), "type": cur})
        active = cur
    rnd = lambda L: [round(x, 2) for x in L]
    label = {"up": "多头趋势", "down": "空头趋势", "range": "盘整纠缠", "mixed": "转换中"}
    return {"e5": rnd(e5), "e8": rnd(e8), "e13": rnd(e13), "e21": rnd(e21),
            "mom": mom, "rsi": [round(x, 1) for x in rsi], "state": state,
            "signals": sig, "resonance": res,
            "now": {"state": state[-1], "label": label[state[-1]],
                    "mom": mom[-1], "rsi": round(rsi[-1], 1), "res": active}}

# ---------------------------------------------------------------- engine
def build_payload(txns, opt_txns, names, cur, prices, deposits, totals, dmin, dmax, dividends=0.0, life_deposits=0.0):
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
            "numTrades": len(tl), "prices": sorted(prices.get(sym, {}).items()),
            "txns": rows, "fib": compute_fib(sorted(prices.get(sym, {}).items()))})

    # per-stock momentum keyed by date (for the portfolio-level overlay)
    mom_map = {}
    for s in stocks:
        if s["fib"]:
            mom_map[s["sym"]] = {d: m for (d, _), m in zip(s["prices"], s["fib"]["mom"])}

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
        # value-weighted portfolio Fibonacci momentum on day d
        num = den = 0.0
        for sym in stock_syms:
            sh = shares_after(sym, d)
            if sh <= 0:
                continue
            p = price_on(prices, sym, d)
            m = mom_map.get(sym, {}).get(d)
            if p and m is not None:
                num += sh * p * m; den += sh * p
        pmom = round(num / den, 1) if den else 0
        series.append({"date": d, "value": round(v, 2), "ret": round(cumret * 100, 3),
                       "sp500": (round((sp / sp_base - 1) * 100, 3) if sp and sp_base else None),
                       "nasdaq": (round((nq / nq_base - 1) * 100, 3) if nq and nq_base else None),
                       "pmom": pmom})
        prevd, vprev = d, v

    held_val = sum(s["value"] for s in stocks if s["held"])
    held_unreal = sum(s["unreal"] for s in stocks if s["held"])
    last = series[-1] if series else {}
    summary = {"marketValue": round(held_val, 2), "unrealized": round(held_unreal, 2),
               "realized": round(total_realized, 2), "netInvested": round(tot_buy - tot_sell, 2),
               "totalBuy": round(tot_buy, 2), "totalSell": round(tot_sell, 2),
               "deposits": round(deposits, 2), "lifeDeposits": round(life_deposits, 2),
               "dividends": round(dividends, 2), "optNet": round(opt_net, 2),
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
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin/>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;600;700&family=Archivo:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500;600&family=Noto+Sans+SC:wght@400;500&display=swap" rel="stylesheet"/>
<style>
/* faint engraved-paper grain + one off-axis graphite bloom; both fixed, non-animated */
body::before{content:"";position:fixed;inset:0;z-index:-1;pointer-events:none;
 background:radial-gradient(1100px 560px at 82% -12%,#16181d 0%,rgba(11,12,14,0) 58%);}
body::after{content:"";position:fixed;inset:0;z-index:-1;pointer-events:none;opacity:.035;
 background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='160' height='160'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='2' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E");}
</style>
<style>
/* ============================================================
   GRAPHITE ATELIER — Swiss-Minimal Portfolio Terminal
   Display: Space Grotesk · UI/Latin: Archivo · Numbers: IBM Plex Mono
   CJK falls naturally to Noto Sans SC / PingFang SC
   One rationed amber accent · semantic green/red on numbers only
   ============================================================ */
:root{
  /* ---- palette ---- */
  --bg:#0B0C0E; --bg2:#0E1013; --panel:#121316; --panel2:#16181C;
  --line:#23262C; --hair:#1A1C21;
  --txt:#ECEDEF; --mut:#888D96; --faint:#5C616B;
  --accent:#E8B339;            /* the ONE chromatic accent: live line / active / "you are here" */
  --accent-ref:#B89030;        /* muted amber = reference (cost basis) — distinct from live */
  --accent-soft:rgba(232,179,57,0.12);
  --accent-line:rgba(232,179,57,0.32);
  --green:#4FB286; --red:#E5707A; --orange:#E8B339;
  --grid:#1A1C21; --axis:#6B7079;
  /* legacy aliases kept so any stray var() refs still resolve */
  --panel-2:var(--panel2); --blue:var(--accent); --purple:var(--accent);
  /* ---- type ---- */
  --f-disp:"Space Grotesk","Noto Sans SC","PingFang SC",ui-sans-serif,system-ui,sans-serif;
  --f-ui:"Archivo","Noto Sans SC","PingFang SC",-apple-system,"Segoe UI",Roboto,sans-serif;
  --f-mono:"IBM Plex Mono","SF Mono",ui-monospace,"Roboto Mono",monospace;
  /* ---- radii ---- */
  --r-card:6px; --r-ctl:6px; --r-chip:3px; --r-pill:999px;
  /* ---- shadow (used with extreme restraint) ---- */
  --sh-lift:0 14px 44px -28px rgba(0,0,0,.8);
  --sh-tt:0 4px 14px rgba(0,0,0,.45);
  /* ---- motion ---- */
  --ease:cubic-bezier(.22,.61,.36,1);
}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{
  margin:0;
  font-family:var(--f-ui);
  background:var(--bg);
  color:var(--txt);
  font-size:13px; line-height:1.5;
  font-feature-settings:"tnum" 1,"zero" 1;
  -webkit-font-smoothing:antialiased; text-rendering:optimizeLegibility;
}
/* every numeral is tabular & slashed-zero */
.kpi .v,.row .pnl,.row .meta,.badge .v,td,th,.fval,.tt,.legacychip,.chip,.sub{
  font-variant-numeric:tabular-nums slashed-zero;
}
::-webkit-scrollbar{width:8px;height:8px}
::-webkit-scrollbar-thumb{background:#2A2E36;border-radius:8px}
::-webkit-scrollbar-thumb:hover{background:#363B45}
::-webkit-scrollbar-track{background:transparent}

/* page width cap so editorial margins don't sprawl on ultrawide */
header,.kpis,.wrap{max-width:1480px;margin-left:auto;margin-right:auto}

/* ============================== MASTHEAD ============================== */
header{
  padding:14px 28px;
  display:flex; align-items:center; gap:16px; flex-wrap:wrap;
  position:sticky; top:0; z-index:20;
  background:color-mix(in srgb,var(--bg) 80%,transparent);
  -webkit-backdrop-filter:blur(14px) saturate(120%);
  backdrop-filter:blur(14px) saturate(120%);
  border-bottom:1px solid var(--line);
  box-shadow:inset 0 1px 0 rgba(255,255,255,.025);
}
header h1{
  font-family:var(--f-disp); font-size:17px; font-weight:600;
  letter-spacing:.01em; margin:0; line-height:1.1;
  display:flex; align-items:center; gap:11px;
}
/* amber index-tick masthead glyph, replaces 📈 (see structural_changes #1) */
header h1::before{
  content:""; width:3px; height:17px; border-radius:1px;
  background:var(--accent); box-shadow:0 0 10px var(--accent-line); flex:none;
}
header .sub{
  color:var(--mut); font-size:11px; font-family:var(--f-ui);
  text-transform:uppercase; letter-spacing:.11em; line-height:1.5;
  margin-left:auto; text-align:right;
}
/* render the date range inside .sub in mono via an injected span (structural_changes #1) */
header .sub b,header .sub .mono{
  font-family:var(--f-mono); font-weight:500; letter-spacing:0;
  text-transform:none; color:var(--txt);
}

/* ============================== KPI LEDGER STRIP ============================== */
/* collapse 8 floating cards into ONE ruled strip with hairline dividers */
.kpis{
  display:grid;
  grid-template-columns:repeat(4,1fr);
  gap:0;
  padding:0;
  margin:20px 28px 4px;
  background:var(--panel);
  border:1px solid var(--line);
  border-radius:var(--r-card);
  overflow:hidden;
}
.kpi{
  position:relative;
  background:none; border:0; border-radius:0; box-shadow:none;
  padding:15px 20px;
  border-left:1px solid var(--line);
  border-top:1px solid var(--line);
  transition:background-color .18s var(--ease);
}
/* first column + first row shed their inner rules so the strip reads as one panel */
.kpi:nth-child(4n+1){border-left:0}
.kpi:nth-child(-n+4){border-top:0}
.kpi:hover{background:var(--panel2)}
.kpi .l{
  font-family:var(--f-ui); font-size:10.5px; font-weight:600;
  text-transform:uppercase; letter-spacing:.085em;
  color:var(--mut); margin-bottom:8px;
  transition:color .18s var(--ease);
  white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
}
.kpi:hover .l{color:var(--txt)}
.kpi .v{
  font-family:var(--f-mono); font-size:22px; font-weight:500;
  line-height:1.0; letter-spacing:-.01em; color:var(--txt);
}
/* HERO: Market Value is the first cell — larger value + amber underline-tick under the label */
.kpi:first-child .v{font-size:26px}
.kpi:first-child .l{position:relative; padding-bottom:7px; display:inline-block}
.kpi:first-child .l::after{
  content:""; position:absolute; left:0; bottom:0;
  width:26px; height:2px; background:var(--accent); border-radius:1px;
  transform:scaleX(0); transform-origin:left;
}
body.ready .kpi:first-child .l::after{
  animation:tickIn .55s var(--ease) .35s forwards;
}
/* optional inline net-worth sparkline lives to the right of the hero value (structural_changes #5) */
.kpi .spark{display:block;margin-top:9px;width:100%;height:24px;opacity:.95}
.kpi .spark polyline{fill:none;stroke:var(--accent);stroke-width:1.4;vector-effect:non-scaling-stroke}

/* ============================== TWO-COLUMN SHELL ============================== */
.wrap{
  display:flex; gap:20px;
  padding:16px 28px 44px;
  align-items:flex-start;
}
.left{
  width:300px; flex:none;
  background:var(--panel);
  border:1px solid var(--line);
  border-radius:var(--r-card);
  overflow:hidden;
  position:sticky; top:76px;
  box-shadow:var(--sh-lift);
}
.right{flex:1; min-width:0}

/* ---- controls ---- */
.controls{
  padding:16px;
  border-bottom:1px solid var(--line);
  display:flex; flex-direction:column; gap:10px;
}
.controls input,.controls select{
  background:var(--bg2);
  border:1px solid var(--line);
  color:var(--txt);
  border-radius:var(--r-ctl);
  padding:9px 11px;
  font-size:12.5px; font-family:var(--f-ui);
  width:100%; outline:none;
  transition:border-color .16s var(--ease),box-shadow .16s var(--ease);
}
.controls input::placeholder{color:var(--faint)}
.controls input:focus,.controls select:focus{
  border-color:var(--accent);
  box-shadow:0 0 0 2px var(--accent-soft);
}
.controls select{appearance:none;cursor:pointer;
  background-image:linear-gradient(45deg,transparent 50%,var(--mut) 50%),linear-gradient(135deg,var(--mut) 50%,transparent 50%);
  background-position:calc(100% - 16px) 17px,calc(100% - 11px) 17px;
  background-size:5px 5px,5px 5px;background-repeat:no-repeat;}

/* ---- filter tabs: ghost by default, active = amber text + sliding underline ---- */
.tabs{display:flex; gap:0; position:relative; border-bottom:1px solid var(--line)}
.tabs button{
  flex:1; background:none; border:0;
  color:var(--mut);
  padding:9px 4px 10px;
  font-size:12px; font-family:var(--f-ui); font-weight:600;
  letter-spacing:.02em; cursor:pointer;
  position:relative;
  transition:color .16s var(--ease);
}
.tabs button:hover{color:var(--txt)}
.tabs button.on{color:var(--accent)}
/* the 2px amber rule that sits under the active segment (instrument-like, not a pill) */
.tabs button.on::after{
  content:""; position:absolute; left:14%; right:14%; bottom:-1px; height:2px;
  background:var(--accent); border-radius:1px;
  box-shadow:0 0 8px var(--accent-line);
}

/* ============================== TICKER LIST ============================== */
.list{max-height:74vh; overflow:auto}
.row{
  display:flex; justify-content:space-between; align-items:center;
  padding:12px 16px; gap:10px;
  border-bottom:1px solid var(--hair);
  border-left:2px solid transparent;
  cursor:pointer;
  transition:background-color .16s var(--ease),border-color .16s var(--ease);
}
.row:hover{background:var(--panel2)}
.row.sel{background:var(--accent-soft); border-left-color:var(--accent)}
.row.sel .sym{color:var(--txt)}
.row .sym{
  font-family:var(--f-disp); font-weight:600; font-size:13px;
  letter-spacing:-.005em; color:var(--txt);
  display:flex; align-items:center; gap:6px; flex-wrap:wrap;
}
.row .sym>span:first-child{font-size:10px} /* the state ● dot, shrunk */
.row .meta{
  font-family:var(--f-mono); font-size:10.5px; color:var(--mut);
  margin-top:3px; line-height:1.45;
}
.row .pnl{
  font-family:var(--f-mono); font-size:13px; font-weight:500;
  text-align:right; white-space:nowrap; color:var(--txt);
}
.row .pnl .meta{text-align:right}
/* pinned overview row = "home": amber tick + faint amber wash */
.ovrow{background:var(--accent-soft)}
.ovrow .sym{color:var(--accent)}
.ovrow.sel{border-left-color:var(--accent)}
/* one-shot "data loaded" flicker on the freshly selected row's values (graft 方向 1) */
body.ready .row.sel .pnl,body.ready .row.sel .meta{animation:dataFlick .26s ease-out}

/* ============================== CARDS ============================== */
.card{
  background:var(--panel);
  border:1px solid var(--line);
  border-radius:var(--r-card);
  padding:20px 22px;
  margin-bottom:16px;
  box-shadow:none;
}
.dh{display:flex; align-items:baseline; gap:12px; flex-wrap:wrap; margin-bottom:4px}
.dh .t{
  font-family:var(--f-disp); font-size:20px; font-weight:600;
  letter-spacing:-.01em; color:var(--txt);
  display:flex; align-items:center; gap:9px;
}
/* leading amber tick replaces the emoji prefix in card titles (structural_changes #2) */
.dh .t::before{
  content:""; width:3px; height:15px; border-radius:1px;
  background:var(--accent); flex:none;
}
.dh .nm{font-family:var(--f-ui); color:var(--mut); font-size:11.5px; font-weight:400}

/* a section caption helper to replace inline font-weight:650 divs (structural_changes #3) */
.card>div[style*="font-weight:650"]{
  font-family:var(--f-ui)!important; font-size:11px!important;
  font-weight:600!important; text-transform:uppercase; letter-spacing:.07em;
  color:var(--mut)!important; padding-bottom:6px; border-bottom:1px solid var(--hair);
}

/* ============================== BADGES ============================== */
.badges{display:flex; gap:28px; row-gap:14px; flex-wrap:wrap; margin:18px 0 8px}
.badge .l{
  font-family:var(--f-ui); color:var(--mut); font-size:10px; font-weight:600;
  text-transform:uppercase; letter-spacing:.07em; margin-bottom:4px;
}
.badge .v{font-family:var(--f-mono); font-size:16px; font-weight:500; color:var(--txt)}
.badge .v .note{font-family:var(--f-ui); font-size:11px}
.pos{color:var(--green)} .neg{color:var(--red)}

/* ============================== LEGEND ============================== */
.legend{
  display:flex; gap:14px; row-gap:6px; flex-wrap:wrap;
  font-family:var(--f-ui); font-size:10.5px; color:var(--mut);
  margin:10px 0 6px; align-items:center;
}
.legend i{
  display:inline-block; width:9px; height:9px; border-radius:2px;
  margin-right:5px; vertical-align:middle;
}

/* ============================== CHART CANVAS ============================== */
.chartbox{position:relative; width:100%; overflow:hidden; margin-top:10px}
svg{width:100%; height:auto; display:block}
svg text{font-family:var(--f-mono); font-variant-numeric:tabular-nums slashed-zero}

/* ============================== TOOLTIP ============================== */
.tt{
  position:fixed; pointer-events:none;
  background:color-mix(in srgb,var(--panel2) 97%,transparent);
  border:1px solid var(--line);
  border-radius:var(--r-card);
  padding:9px 11px; font-size:12px; font-family:var(--f-mono);
  z-index:40; display:none; min-width:150px;
  box-shadow:var(--sh-tt);
}
.tt b{font-family:var(--f-disp); font-weight:600; letter-spacing:-.005em}

/* ============================== TABLES (print-ledger) ============================== */
table{width:100%; border-collapse:collapse; font-size:12px; margin-top:8px}
th,td{padding:8px 10px; text-align:right; white-space:nowrap}
th{
  position:sticky; top:0; z-index:1;
  background:var(--panel); color:var(--mut);
  font-family:var(--f-ui); font-size:10px; font-weight:600;
  text-transform:uppercase; letter-spacing:.06em;
  border-bottom:1px solid var(--line);
}
td{font-family:var(--f-mono); font-weight:400; color:var(--txt); border-bottom:1px solid var(--grid)}
td.l,th.l{text-align:left; font-family:var(--f-ui)}
td.l{color:var(--txt)}
tbody tr{transition:background-color .14s var(--ease)}
tbody tr:hover{background:var(--panel2)}

/* ============================== TAGS / CHIPS ============================== */
.tag{
  font-family:var(--f-ui); font-size:10px; font-weight:600;
  text-transform:uppercase; letter-spacing:.04em;
  padding:2px 7px; border-radius:var(--r-chip);
}
.tag.b{background:rgba(79,178,134,.12); color:var(--green)}
.tag.s{background:rgba(229,112,122,.12); color:var(--red)}
.tag.o{background:var(--panel2); color:var(--mut)}
.note{font-family:var(--f-ui); color:var(--mut); font-size:11.5px; line-height:1.7}
.note b{color:var(--txt); font-weight:600}
.scroll{max-height:420px; overflow:auto; border-radius:var(--r-card); border:1px solid var(--hair)}
details{margin-top:6px}
summary{cursor:pointer; color:var(--accent); font-size:12.5px; font-family:var(--f-ui); font-weight:500; padding:4px 0; list-style:none}
summary::-webkit-details-marker{display:none}
summary::before{content:"▸ "; color:var(--accent); font-size:10px}
details[open] summary::before{content:"▾ "}
.legacychip{
  font-family:var(--f-mono); font-size:9.5px;
  background:var(--accent-soft); color:var(--accent);
  padding:2px 7px; border-radius:var(--r-chip); margin-left:6px;
}

/* ============================== FIB RANKING ROWS ============================== */
.frow{
  display:flex; align-items:center; gap:11px;
  padding:6px 8px; font-size:12.5px; border-radius:var(--r-chip);
  transition:background-color .14s var(--ease);
}
.frow:hover{background:var(--panel2)}
.frow .fsym{width:72px; font-family:var(--f-disp); font-weight:600; font-size:13px; display:flex; align-items:center; gap:6px}
.frow .fsym>span:first-child{font-size:10px}
.frow .fval{width:50px; text-align:right; font-family:var(--f-mono); font-weight:500}
.frow .fst{width:62px; font-family:var(--f-ui); font-size:10.5px; color:var(--mut)}
.fbar{position:relative; height:14px; background:var(--grid); border-radius:var(--r-chip); flex:1; overflow:hidden}
.fbar .z{position:absolute; left:50%; top:0; bottom:0; width:1px; background:var(--line)}
.fbar .p{position:absolute; top:2.5px; height:9px; border-radius:2px; transition:width .3s var(--ease)}

/* ============================== STATUS CHIP ============================== */
.chip{
  display:inline-flex; align-items:center; gap:5px;
  padding:3px 9px; border-radius:var(--r-pill);
  font-family:var(--f-ui); font-size:11px; font-weight:600;
  border:1px solid var(--line);
}
.chip span{font-family:var(--f-mono); font-weight:500}

/* ============================== IN-PANEL SEGMENTED TABS (added wrapper) ============================== */
/* structural_changes #4 — reuses .on for active state; underline slides like .tabs */
.seg-rail{
  display:flex; gap:0; position:sticky; top:76px; z-index:15;
  background:var(--panel); border:1px solid var(--line);
  border-radius:var(--r-card); padding:0 4px; margin-bottom:16px;
  box-shadow:var(--sh-lift);
}
.seg-rail button{
  background:none; border:0; color:var(--mut);
  font-family:var(--f-ui); font-size:12.5px; font-weight:600; letter-spacing:.02em;
  padding:11px 16px 12px; cursor:pointer; position:relative;
  transition:color .16s var(--ease);
}
.seg-rail button:hover{color:var(--txt)}
.seg-rail button.on{color:var(--accent)}
.seg-rail button.on::after{
  content:""; position:absolute; left:16px; right:16px; bottom:-1px; height:2px;
  background:var(--accent); border-radius:1px; box-shadow:0 0 8px var(--accent-line);
}
.seg[hidden]{display:none}

/* ============================== MOTION ============================== */
@keyframes riseIn{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
@keyframes tickIn{from{transform:scaleX(0)}to{transform:scaleX(1)}}
@keyframes dataFlick{0%{opacity:.35}100%{opacity:1}}
@keyframes drawIn{from{stroke-dashoffset:var(--len,1200)}to{stroke-dashoffset:0}}

@media (prefers-reduced-motion: no-preference){
  body.ready .kpi{opacity:0; animation:riseIn .46s var(--ease) forwards}
  body.ready .kpi:nth-child(1){animation-delay:.04s}
  body.ready .kpi:nth-child(2){animation-delay:.08s}
  body.ready .kpi:nth-child(3){animation-delay:.12s}
  body.ready .kpi:nth-child(4){animation-delay:.16s}
  body.ready .kpi:nth-child(5){animation-delay:.20s}
  body.ready .kpi:nth-child(6){animation-delay:.24s}
  body.ready .kpi:nth-child(7){animation-delay:.28s}
  body.ready .kpi:nth-child(8){animation-delay:.32s}
  body.ready .left{opacity:0; animation:riseIn .46s var(--ease) .14s forwards}
  /* cards are JS-regenerated each render; nth-of-type keeps the stagger without re-arming via class */
  body.ready:not(.done) .right .card{opacity:0; animation:riseIn .46s var(--ease) forwards}
  body.ready:not(.done) .right .card:nth-of-type(1){animation-delay:.06s}
  body.ready:not(.done) .right .card:nth-of-type(2){animation-delay:.13s}
  body.ready:not(.done) .right .card:nth-of-type(3){animation-delay:.20s}
  body.ready:not(.done) .right .card:nth-of-type(4){animation-delay:.27s}
  /* hero-line draw-in: opt-in via .draw on net-worth & cumulative-return polylines */
  body.ready svg polyline.draw{
    stroke-dasharray:var(--len,1400); stroke-dashoffset:var(--len,1400);
    animation:drawIn 1.15s var(--ease) .15s forwards;
  }
}
@media (prefers-reduced-motion: reduce){
  body.ready .kpi,body.ready .left,body.ready:not(.done) .right .card{opacity:1;animation:none}
  .kpi:first-child .l::after{transform:scaleX(1);animation:none}
  *{scroll-behavior:auto}
}

/* ============================== RESPONSIVE ============================== */
@media (max-width:980px){
  .kpis{grid-template-columns:repeat(2,1fr)}
  .kpi:nth-child(4n+1){border-left:1px solid var(--line)}
  .kpi:nth-child(2n+1){border-left:0}
  .kpi:nth-child(-n+4){border-top:1px solid var(--line)}
  .kpi:nth-child(-n+2){border-top:0}
  .wrap{flex-direction:column}
  .left{width:100%; position:static}
  .list{max-height:46vh}
  .seg-rail{top:0; position:static}
  header .sub{margin-left:0; text-align:left; width:100%}
}
</style>
</head>
<body>
<header>
 <h1>投资组合时间线</h1>
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
       <option value="fibmom">按斐波那契动能</option>
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
document.getElementById('rangelbl').innerHTML=`数据窗口 <b>${D0} → ${D1}</b> · 价格来自 Yahoo Finance · 共 ${S.numStocks} 只标的（持有 ${S.numHeld}）`;

const kpis=[
 ['当前持仓市值',fmt(S.marketValue),''],
 ['未实现盈亏 (券商实际)',fmt(S.unrealized),cls(S.unrealized)],
 ['已实现盈亏 (窗口内·含估算)',fmt(S.realized),cls(S.realized)],
 ['期权净现金流',fmt(S.optNet),cls(S.optNet)],
 ['窗口内净买入',fmt(S.netInvested),''],
 ['现金转入 (窗口内)',fmt(S.deposits),''],
 ['累计入金 (开户至今)',fmt(S.lifeDeposits||S.deposits),''],
 ['股息收入',fmt(S.dividends||0),(S.dividends>0?'pos':'')],
];
document.getElementById('kpis').innerHTML=kpis.map(k=>`<div class="kpi"><div class="l">${k[0]}</div><div class="v ${k[2]}">${k[1]}</div></div>`).join('');

let filter='held', sortKey='value', q='', sel='__OV__';
const stocks=DATA.stocks;
function filtered(){
 let a=stocks.filter(s=>filter==='all'||(filter==='held'?s.held:!s.held));
 if(q)a=a.filter(s=>s.sym.toLowerCase().includes(q)||(s.name||'').toLowerCase().includes(q));
 const k=sortKey, fm=s=>(s.fib&&s.fib.now)?s.fib.now.mom:-999;
 a.sort((x,y)=> k==='sym'? x.sym.localeCompare(y.sym) : (k==='fibmom'? fm(y)-fm(x) : (y[k]||0)-(x[k]||0)));
 return a;
}
function renderList(){
 const a=filtered();
 const ov=`<div class="row ovrow ${sel==='__OV__'?'sel':''}" data-s="__OV__">
   <div><div class="sym">📊 组合总览</div><div class="meta">净值 & 收益率 vs 指数</div></div>
   <div class="pnl ${cls(S.curReturn)}">${pct(S.curReturn)}<div class="meta">区间收益</div></div></div>`;
 document.getElementById('list').innerHTML=ov+(a.map(s=>{
  const main=s.held?s.unreal:s.realized, lbl=s.held?'未实现':'已实现';
  const fn=s.fib&&s.fib.now, dot=fn?`<span style="color:${FIBCOL[fn.state]}">●</span> `:'';
  const zap=fn&&fn.res?`<span title="${fn.res==='bull'?'多头共振':'空头共振'}" style="color:${fn.res==='bull'?'#4FB286':'#E5707A'}">⚡</span>`:'';
  const momtxt=fn?` · 动能<span style="color:${momColor(fn.mom)}">${fn.mom>0?'+':''}${fn.mom}</span>`:'';
  return `<div class="row ${sel===s.sym?'sel':''}" data-s="${s.sym}">
    <div><div class="sym">${dot}${s.sym} ${zap}${s.hasLegacy?'<span class="legacychip">含旧仓</span>':''}</div>
    <div class="meta">${s.held?fmtN(s.shares)+' 股 @ '+fmt(s.avg):'已清仓 · '+s.numTrades+' 笔'}${momtxt}</div></div>
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
   el+=`<line x1="${mL}" y1="${y}" x2="${W-mR}" y2="${y}" stroke="#1A1C21"/>`;
   el+=`<text x="${mL-8}" y="${y+4}" fill="#6B7079" font-size="11" text-anchor="end">$${v.toFixed(0)}</text>`;}
 for(let i=0;i<=5;i++){const t=xmin+(xmax-xmin)*i/5,x=xs(new Date(t)),dt=new Date(t);
   el+=`<line x1="${x}" y1="${mT}" x2="${x}" y2="${H-mB}" stroke="#15171B"/>`;
   el+=`<text x="${x}" y="${H-mB+18}" fill="#6B7079" font-size="11" text-anchor="middle">${(dt.getMonth()+1)}/${dt.getDate()}</text>`;}
 if(prices.length){const pts=prices.map(p=>`${xs(p[0]).toFixed(1)},${yc(p[1]).toFixed(1)}`).join(' ');
   el+=`<polyline points="${pts}" fill="none" stroke="#6B7079" stroke-width="1.6" stroke-opacity="0.9"/>`;}
 const av=txns.filter(t=>t.avg>0&&t.pos>0.0001);
 if(av.length){let d2='',px=null;
   av.forEach((t,i)=>{const x=xs(t.date[0]==='≤'?D0:t.date),y=yc(t.avg);
     if(i===0)d2=`M ${x} ${y}`;else d2+=` L ${px} ${y} L ${x} ${y}`;px=x;});
   d2+=` L ${xs(D1)} ${yc(av[av.length-1].avg)}`;
   el+=`<path d="${d2}" fill="none" stroke="#B89030" stroke-width="1.8" stroke-dasharray="5 3" stroke-opacity="0.85"/>`;}
 if(s.curPrice&&s.held){const y=yc(s.curPrice);
   el+=`<line x1="${mL}" y1="${y}" x2="${W-mR}" y2="${y}" stroke="#E8B339" stroke-width="1.3" stroke-dasharray="2 3"/>`;
   el+=`<text x="${W-mR+6}" y="${y+4}" fill="#E8B339" font-size="11">现价 $${s.curPrice}</text>`;}
 const amts=txns.filter(t=>t.side!=='OPEN').map(t=>Math.abs(t.amount)),amax=Math.max(...amts,1);
 txns.forEach((t,idx)=>{const dd=t.date[0]==='≤'?D0:t.date,x=xs(dd),y=yc(t.price);
   const r=t.side==='OPEN'?5:Math.max(4,Math.min(15,4+11*Math.sqrt(Math.abs(t.amount)/amax)));
   const col=t.side==='BUY'?'#4FB286':(t.side==='SELL'?'#E5707A':'#6B7079');
   el+=`<circle cx="${x}" cy="${y}" r="${r}" fill="${col}" fill-opacity="0.55" stroke="${col}" stroke-width="1.4" data-i="${idx}" data-sym="${s.sym}" class="mk" style="cursor:pointer"/>`;});
 return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet">${el}</svg>`;
}
function svgLines(ser,defs,opts){
 const W=900,H=opts.h||340,mL=66,mR=70,mT=16,mB=40;
 const xmin=+new Date(ser[0].date),xmax=+new Date(ser[ser.length-1].date);
 let vals=[];defs.forEach(d=>ser.forEach(p=>{if(p[d.key]!=null)vals.push(p[d.key]);}));
 if(opts.zero)vals.push(0);
 let ymin=Math.min(...vals),ymax=Math.max(...vals);const pad=(ymax-ymin)*0.08||1;ymin-=pad;ymax+=pad;
 if(opts.fixed){ymin=opts.fixed[0];ymax=opts.fixed[1];}
 const xs=d=>mL+((+new Date(d)-xmin)/((xmax-xmin)||1))*(W-mL-mR);
 const yc=v=>mT+(1-(v-ymin)/((ymax-ymin)||1))*(H-mT-mB);
 let el='';
 (opts.guides||[]).forEach(g=>{const y=yc(g.v);el+=`<line x1="${mL}" y1="${y}" x2="${W-mR}" y2="${y}" stroke="${g.color||'#2A2E36'}" stroke-dasharray="4 3"/><text x="${W-mR+4}" y="${y+4}" fill="${g.color||'#6B7079'}" font-size="10">${g.label!=null?g.label:g.v}</text>`;});
 for(let i=0;i<=4;i++){const v=ymin+(ymax-ymin)*i/4,y=yc(v);
   el+=`<line x1="${mL}" y1="${y}" x2="${W-mR}" y2="${y}" stroke="#1A1C21"/>`;
   el+=`<text x="${mL-8}" y="${y+4}" fill="#6B7079" font-size="11" text-anchor="end">${opts.fmt(v)}</text>`;}
 const n=ser.length;
 for(let i=0;i<=5;i++){const idx=Math.round((n-1)*i/5),p=ser[idx],x=xs(p.date),dt=new Date(p.date);
   el+=`<text x="${x}" y="${H-mB+18}" fill="#6B7079" font-size="11" text-anchor="middle">${dt.getMonth()+1}/${dt.getDate()}</text>`;}
 if(opts.zero){const y=yc(0);el+=`<line x1="${mL}" y1="${y}" x2="${W-mR}" y2="${y}" stroke="#2A2E36" stroke-dasharray="3 3"/>`;}
 defs.forEach(d=>{const f=ser.filter(p=>p[d.key]!=null);
   const pts=f.map(p=>`${xs(p.date).toFixed(1)},${yc(p[d.key]).toFixed(1)}`).join(' ');
   if(opts.area&&f.length){const base=yc(Math.max(ymin,0));
     el+=`<polygon points="${xs(f[0].date).toFixed(1)},${base} ${pts} ${xs(f[f.length-1].date).toFixed(1)},${base}" fill="${d.color}" fill-opacity="0.10"/>`;}
   el+=`<polyline points="${pts}" fill="none" stroke="${d.color}" stroke-width="2.1" ${d.dash?'stroke-dasharray="5 3"':''}/>`;
   const last=f[f.length-1];
   if(last)el+=`<text x="${xs(last.date)+5}" y="${yc(last[d.key])+4}" fill="${d.color}" font-size="11" font-weight="700">${opts.fmt(last[d.key])}</text>`;});
 return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet">${el}</svg>`;
}
function nwChart(ser){
 const W=900,H=330,mL=64,mR=22,mT=16,mB=40,stripH=11,stripY=H-mB+22;
 const xmin=+new Date(ser[0].date),xmax=+new Date(ser[ser.length-1].date);
 const vals=ser.map(p=>p.value);let ymin=Math.min(...vals),ymax=Math.max(...vals);
 const pad=(ymax-ymin)*0.08||1;ymin-=pad;ymax+=pad;
 const xs=d=>mL+((+new Date(d)-xmin)/((xmax-xmin)||1))*(W-mL-mR);
 const yc=v=>mT+(1-(v-ymin)/((ymax-ymin)||1))*(H-mT-mB-stripH);
 const base=yc(ymin),col=m=>m>15?'#4FB286':(m<-15?'#E5707A':'#E8B339');
 let el='';
 for(let i=0;i<=4;i++){const v=ymin+(ymax-ymin)*i/4,y=yc(v);
   el+=`<line x1="${mL}" y1="${y}" x2="${W-mR}" y2="${y}" stroke="#1A1C21"/><text x="${mL-8}" y="${y+4}" fill="#6B7079" font-size="11" text-anchor="end">$${(v/1000).toFixed(0)}k</text>`;}
 for(let i=0;i<=5;i++){const t=xmin+(xmax-xmin)*i/5,x=xs(new Date(t)),dt=new Date(t);
   el+=`<text x="${x}" y="${stripY+stripH+12}" fill="#6B7079" font-size="11" text-anchor="middle">${dt.getMonth()+1}/${dt.getDate()}</text>`;}
 for(let i=1;i<ser.length;i++){const x0=xs(ser[i-1].date),x1=xs(ser[i].date);
   el+=`<polygon points="${x0},${base} ${x0},${yc(ser[i-1].value)} ${x1},${yc(ser[i].value)} ${x1},${base}" fill="${col(ser[i].pmom)}" fill-opacity="0.15"/>`;}
 const pts=ser.map(p=>`${xs(p.date).toFixed(1)},${yc(p.value).toFixed(1)}`).join(' ');
 el+=`<polyline points="${pts}" fill="none" stroke="#E8B339" stroke-width="1.9"/>`;
 const last=ser[ser.length-1];
 el+=`<text x="${xs(last.date)-2}" y="${yc(last.value)-9}" fill="#E8B339" font-size="12" font-weight="700" text-anchor="end">$${(last.value/1000).toFixed(1)}k</text>`;
 for(let i=1;i<ser.length;i++){const x0=xs(ser[i-1].date),x1=xs(ser[i].date);
   el+=`<rect x="${x0}" y="${stripY}" width="${Math.max(1,x1-x0+0.6)}" height="${stripH}" fill="${col(ser[i].pmom)}" fill-opacity="0.9"/>`;}
 el+=`<text x="${mL-8}" y="${stripY+stripH-1}" fill="#6B7079" font-size="10" text-anchor="end">组合动能</text>`;
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
 <div class="seg-rail"><button class="on" data-seg="nw">净值</button><button data-seg="cmp">指数对比</button><button data-seg="res">共振 · 排行</button></div>
 <div class="seg" data-seg="nw">
 <div class="card">
   <div class="dh"><span class="t">组合总览</span><span class="nm">持仓市值与收益率（股票部分，不含现金/期权）</span></div>
   <div class="badges">${cards.map(c=>`<div class="badge"><div class="l">${c[0]}</div><div class="v">${c[1]}</div></div>`).join('')}</div>
 </div>
 <div class="card"><div style="font-weight:650;margin-bottom:4px">持仓总市值（$） · 叠加组合斐波那契动能</div>
   <div class="legend"><span>底部色带 = 组合加权动能：<span style="color:#4FB286">绿=强(>15)</span> / <span style="color:#E8B339">黄=中性</span> / <span style="color:#E5707A">红=弱(<-15)</span>，用来对照净值看择时节奏</span></div>
   ${nwChart(ser)}</div>
 </div>
 <div class="seg" data-seg="cmp" hidden>
 <div class="card"><div style="font-weight:650;margin-bottom:4px">累计收益率对比（%，时间加权）</div>
   <div class="legend"><span><i style="background:#E8B339"></i>我的组合</span><span><i style="background:#888D96"></i>S&P 500</span><span><i style="background:#E8B339"></i>纳斯达克综合</span></div>
   ${svgLines(ser,[{key:'ret',color:'#E8B339'},{key:'sp500',color:'#888D96',dash:1},{key:'nasdaq',color:'#E8B339',dash:1}],{zero:true,fmt:v=>v.toFixed(0)+'%'})}</div>
 </div>
 <div class="seg" data-seg="res" hidden>`+resonanceCard()+fibRanking()+`</div>`;
 segWire();
}
function resonanceCard(){
 const bull=stocks.filter(x=>x.held&&x.fib&&x.fib.now.res==='bull').sort((a,b)=>b.fib.now.mom-a.fib.now.mom);
 const bear=stocks.filter(x=>x.held&&x.fib&&x.fib.now.res==='bear');
 const chip=(x,c)=>`<span class="chip" style="cursor:pointer;color:${c};border-color:${c}55" onclick="sel='${x.sym}';renderList();window.scrollTo({top:0,behavior:'smooth'})">${x.sym} <span style="color:${c};opacity:.8">${x.fib.now.mom>0?'+':''}${x.fib.now.mom}</span></span>`;
 return `<div class="card"><div class="dh"><span class="t">今日多指标共振</span><span class="nm">趋势排列 + 3日内金/死叉 + RSI 未极端 → 高确信度</span></div>
   <div style="margin-top:10px"><div class="l" style="color:var(--mut);font-size:11px;text-transform:uppercase;letter-spacing:.6px;margin-bottom:7px;font-weight:600">多头共振 · ${bull.length}</div>
   <div style="display:flex;gap:8px;flex-wrap:wrap">${bull.map(x=>chip(x,'#4FB286')).join('')||'<span class="note">无</span>'}</div></div>
   <div style="margin-top:14px"><div class="l" style="color:var(--mut);font-size:11px;text-transform:uppercase;letter-spacing:.6px;margin-bottom:7px;font-weight:600">空头共振 · ${bear.length}</div>
   <div style="display:flex;gap:8px;flex-wrap:wrap">${bear.map(x=>chip(x,'#E5707A')).join('')||'<span class="note">无</span>'}</div></div>
   <div class="note" style="margin-top:12px">共振 = 三个指标同向确认，比单一金叉更能过滤震荡假信号；仍为技术参考，非投资建议。</div></div>`;
}
function fibRanking(){
 const held=stocks.filter(x=>x.held&&x.fib&&x.fib.now).sort((a,b)=>b.fib.now.mom-a.fib.now.mom);
 if(!held.length)return'';
 const rows=held.map(x=>{const m=x.fib.now.mom,c=momColor(m),w=Math.abs(m)/100*50,left=m>=0?50:50-w;
   return `<div class="frow" style="cursor:pointer" onclick="sel='${x.sym}';renderList();window.scrollTo({top:0,behavior:'smooth'})">
     <span class="fsym"><span style="color:${FIBCOL[x.fib.now.state]}">●</span> ${x.sym}</span>
     <div class="fbar"><div class="z"></div><div class="p" style="left:${left}%;width:${w}%;background:${c}"></div></div>
     <span class="fval" style="color:${c}">${m>0?'+':''}${m}</span>
     <span class="fst">${x.fib.now.label}</span></div>`;}).join('');
 return `<div class="card"><div class="dh"><span class="t">斐波那契动能排行</span><span class="nm">持仓按 EMA5/8/13/21 动能强弱排序（点击查看个股）</span></div>
   <div style="margin-top:6px">${rows}</div>
   <div class="note" style="margin-top:10px">动能 = EMA5 相对 EMA21 偏离度（±100 封顶），正=多头、负=空头；颜色点为斐波那契状态。技术参考，非投资建议。</div></div>`;
}
const FIBCOL={up:'#4FB286',down:'#E5707A',range:'#888D96',mixed:'#E8B339'};
const FIBLBL={up:'多头趋势',down:'空头趋势',range:'盘整纠缠',mixed:'转换中'};
function momColor(m){return m>15?'#4FB286':(m<-15?'#E5707A':'#E8B339');}
function fibChart(s){
 const f=s.fib,prices=s.prices;if(!f)return'<div class="note">价格数据不足，无法计算斐波那契指标。</div>';
 const W=900,H=400,mL=58,mR=80,mT=16,mB=46,stripH=12,stripY=H-mB+24;
 const xmin=+new Date(prices[0][0]),xmax=+new Date(D1);
 let ys=[];prices.forEach((p,i)=>{ys.push(p[1],f.e5[i],f.e21[i]);});
 let ymin=Math.min(...ys),ymax=Math.max(...ys);const pad=(ymax-ymin)*0.08||1;ymin-=pad;ymax+=pad;
 const xs=d=>mL+((+new Date(d)-xmin)/((xmax-xmin)||1))*(W-mL-mR);
 const yc=v=>mT+(1-(v-ymin)/((ymax-ymin)||1))*(H-mT-mB-stripH);
 let el='';
 for(let i=0;i<=4;i++){const v=ymin+(ymax-ymin)*i/4,y=yc(v);
   el+=`<line x1="${mL}" y1="${y}" x2="${W-mR}" y2="${y}" stroke="#1A1C21"/><text x="${mL-8}" y="${y+4}" fill="#6B7079" font-size="11" text-anchor="end">$${v.toFixed(0)}</text>`;}
 for(let i=0;i<=5;i++){const t=xmin+(xmax-xmin)*i/5,x=xs(new Date(t)),dt=new Date(t);
   el+=`<text x="${x}" y="${stripY+stripH+12}" fill="#6B7079" font-size="11" text-anchor="middle">${dt.getMonth()+1}/${dt.getDate()}</text>`;}
 // ribbon band fill (e5..e21) colored by per-day state
 for(let i=1;i<prices.length;i++){const x0=xs(prices[i-1][0]),x1=xs(prices[i][0]);
   const c=FIBCOL[f.state[i]]||'#888D96';
   el+=`<polygon points="${x0},${yc(f.e5[i-1])} ${x1},${yc(f.e5[i])} ${x1},${yc(f.e21[i])} ${x0},${yc(f.e21[i-1])}" fill="${c}" fill-opacity="0.13" stroke="none"/>`;}
 // price (faint) + 4 EMAs (fast green -> slow orange)
 const line=(arr,col,w,key)=>{const pts=arr.map((v,i)=>`${xs(prices[i][0]).toFixed(1)},${yc(key?v[key]:v).toFixed(1)}`).join(' ');return `<polyline points="${pts}" fill="none" stroke="${col}" stroke-width="${w}"/>`;};
 el+=line(prices,'#6B7079',1,1);
 el+=line(f.e21,'#5F6168',1.4)+line(f.e13,'#8C8A6E',1.4)+line(f.e8,'#C99A3A',1.5)+line(f.e5,'#E8B339',1.8);
 // golden/death cross markers
 (f.signals||[]).forEach(g=>{const x=xs(g.date),y=yc(g.price);
   if(g.type==='golden')el+=`<path d="M ${x} ${y-13} l 5 9 l -10 0 z" fill="#4FB286" stroke="#0B0C0E" stroke-width="0.5"/>`;
   else el+=`<path d="M ${x} ${y+13} l 5 -9 l -10 0 z" fill="#E5707A" stroke="#0B0C0E" stroke-width="0.5"/>`;});
 // resonance: trend + recent cross + RSI-ok  → highlighted ring
 (f.resonance||[]).forEach(g=>{const x=xs(g.date),y=yc(g.price),c=g.type==='bull'?'#4FB286':'#E5707A';
   el+=`<circle cx="${x}" cy="${y}" r="10" fill="none" stroke="${c}" stroke-width="2.2" stroke-opacity="0.9"/><circle cx="${x}" cy="${y}" r="2.6" fill="${c}"/>`;});
 // current price label
 const cp=prices[prices.length-1][1];el+=`<text x="${W-mR+4}" y="${yc(cp)+4}" fill="#6B7079" font-size="11">现价</text>`;
 // state strip
 for(let i=1;i<prices.length;i++){const x0=xs(prices[i-1][0]),x1=xs(prices[i][0]);
   el+=`<rect x="${x0}" y="${stripY}" width="${Math.max(1,x1-x0+0.6)}" height="${stripH}" fill="${FIBCOL[f.state[i]]}" fill-opacity="0.85"/>`;}
 el+=`<text x="${mL-8}" y="${stripY+stripH-1}" fill="#6B7079" font-size="10" text-anchor="end">状态</text>`;
 return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet">${el}</svg>`;
}
function renderFib(s){
 const f=s.fib;if(!f)return'';
 const n=f.now,sc=momColor(n.mom);
 const rsiCol=n.rsi>70?'#E5707A':(n.rsi<30?'#4FB286':'#e6ecf5');
 const lastSig=(f.signals||[]).slice(-1)[0];
 const ser=s.prices.map((p,i)=>({date:p[0],mom:f.mom[i],rsi:f.rsi[i]}));
 const resChip=n.res==='bull'?'<span class="chip" style="color:#4FB286;border-color:#1f5a40;background:rgba(61,220,151,.1)">⚡ 多头共振</span>'
   :(n.res==='bear'?'<span class="chip" style="color:#E5707A;border-color:#5a1f1f;background:rgba(255,107,107,.1)">⚡ 空头共振</span>':'');
 const badges=[
  ['斐波那契状态',`<span style="color:${FIBCOL[n.state]}">●</span> ${n.label}`],
  ['动能强弱',`<span style="color:${sc}">${n.mom>0?'+':''}${n.mom}</span> <span class="note">/100</span>`],
  ['RSI(14)',`<span style="color:${rsiCol}">${n.rsi}</span>`],
  ['最近信号',lastSig?(lastSig.type==='golden'?`<span class="pos">金叉 ${lastSig.date}</span>`:`<span class="neg">死叉 ${lastSig.date}</span>`):'—'],
  ['多指标共振',n.res==='bull'?'<span class="pos">多头共振中</span>':(n.res==='bear'?'<span class="neg">空头共振中</span>':'无')],
 ];
 return `<div class="card">
   <div class="dh"><span class="t">斐波那契动能分析</span>${resChip}<span class="nm">EMA 5 / 8 / 13 / 21 缎带 · 动能 · RSI</span></div>
   <div class="badges">${badges.map(b=>`<div class="badge"><div class="l">${b[0]}</div><div class="v">${b[1]}</div></div>`).join('')}</div>
   <div class="legend">
     <span><i style="background:#E8B339"></i>EMA5</span><span><i style="background:#C99A3A"></i>EMA8</span>
     <span><i style="background:#8C8A6E"></i>EMA13</span><span><i style="background:#5F6168"></i>EMA21</span>
     <span><i style="background:#4FB286"></i>金叉</span><span><i style="background:#E5707A"></i>死叉</span>
     <span>◎ 共振信号(多空趋势+金/死叉+RSI未极端)</span>
     <span>底部状态带：<span style="color:#4FB286">绿=多头</span> / <span style="color:#E5707A">红=空头</span> / <span style="color:#E8B339">黄=转换</span> / <span style="color:#6B7079">灰=盘整</span></span>
   </div>
   ${fibChart(s)}
   <div style="font-weight:650;margin:12px 0 2px">动能振荡器（−100 ~ +100）</div>
   ${svgLines(ser,[{key:'mom',color:'#E8B339'}],{zero:true,h:200,fixed:[-105,105],fmt:v=>v.toFixed(0),
     guides:[{v:15,color:'#2f6b4f',label:'强多'},{v:-15,color:'#6b2f2f',label:'强空'}]})}
   <div style="font-weight:650;margin:12px 0 2px">RSI(14)</div>
   ${svgLines(ser,[{key:'rsi',color:'#6E9CA6'}],{h:180,fixed:[0,100],fmt:v=>v.toFixed(0),
     guides:[{v:70,color:'#6b2f2f',label:'超买70'},{v:30,color:'#2f6b4f',label:'超卖30'}]})}
   <div class="note" style="margin-top:10px"><b>怎么读：</b>四条 EMA 像缎带——向上发散（绿）= 快线在上、多头排列、动能强；向下发散（红）= 空头；缠绕（灰）= 盘整观望，信号不可靠。动能值是 EMA5 相对 EMA21 的偏离度（±100 封顶），RSI>70 超买、<30 超卖。<br>
   <b>多指标共振(◎ 圆环)：</b>同时满足「均线多头/空头排列 + 3 日内出现金叉/死叉 + RSI 未到超买/超卖」三个条件才标记——比单一信号更高确信度，能过滤掉震荡市里的假交叉。<br>
   <b>诚实说明：</b>“斐波那契周期更神奇”在学术上并无强证据——5/8/13/21 相比其它周期没有统计显著的超额收益。它真正有用的地方是<b>周期按几何级数(≈1.6 倍)递增</b>，天然形成快/中/慢分层，便于判断趋势结构；这来自间距而非数字的“神秘性”。本面板为技术分析参考，<b>非投资建议</b>。</div>
 </div>`;
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
 <div class="seg-rail"><button class="on" data-seg="price">价格 · 操作</button><button data-seg="tx">交易明细</button><button data-seg="fib">斐波那契</button></div>
 <div class="seg" data-seg="price">
 <div class="card">
   <div class="dh"><span class="t">${s.sym}</span><span class="nm">${s.name}</span>
     ${s.hasLegacy?'<span class="legacychip">含 '+D0+' 前旧底仓 · 成本按当日市价估算</span>':''}</div>
   <div class="badges">${badges.map(b=>`<div class="badge"><div class="l">${b[0]}</div><div class="v">${b[1]}</div></div>`).join('')}</div>
   <div class="legend"><span><i style="background:#6B7079"></i>市场价格(Yahoo)</span><span><i style="background:#B89030"></i>持仓平均成本</span>
     <span><i style="background:#E8B339"></i>当前价</span><span><i style="background:#4FB286"></i>买入(大小=金额)</span><span><i style="background:#E5707A"></i>卖出</span></div>
   <div class="chartbox">${chart(s)}</div></div>
 </div>
 <div class="seg" data-seg="tx" hidden>
 <div class="card"><div style="font-weight:650;margin-bottom:6px">交易明细（${s.numTrades} 笔）</div>
   <div class="scroll"><table><thead><tr><th class="l">日期</th><th class="l">动作</th><th>数量</th><th>成交价</th><th>金额</th><th>持仓后</th><th>均价后</th><th>已实现</th></tr></thead>
   <tbody>${rows}</tbody></table></div></div>
 </div>
 <div class="seg" data-seg="fib" hidden>`+renderFib(s)+`</div>`;
 bindMarkers();segWire();
}
function segWire(){const r=document.getElementById('right');
 r.querySelectorAll('.seg-rail button').forEach(b=>b.onclick=()=>{
  r.querySelectorAll('.seg-rail button').forEach(x=>x.classList.toggle('on',x===b));
  r.querySelectorAll('.seg').forEach(p=>{p.hidden=p.dataset.seg!==b.dataset.seg;});});}
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
requestAnimationFrame(()=>document.body.classList.add('ready'));
setTimeout(()=>document.body.classList.add('done'),1500);
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
    if not portfolio:
        sys.exit("!! Could not find a Portfolio_Positions*.csv. Pass --portfolio explicitly.")

    # Gather every history-style export and parse them all. Different Fidelity
    # exports are complete in different ways: "History_for_Account" has the full
    # trade log; "Accounts_History" often has the complete cash/dividend record
    # (and may include deposits the other file misses). So pick the best source
    # for each purpose instead of trusting a single file.
    if args.history:
        hist_files = [args.history]
    else:
        hist_files = []
        for pat in ("History_for_Account*.csv", "Accounts_History*.csv"):
            hist_files += glob.glob(os.path.join(args.input_dir, pat))
    if not hist_files:
        sys.exit("!! Could not find any history CSV. Pass --history explicitly.")
    parsed = [parse_history(p) for p in hist_files]

    # Merge ALL exports (each contributes the dates it covers; dedup by max-count
    # per identical transaction). Then restrict the timeline to the largest
    # gap-free span ending at the latest trade — a >=20-day hole means an export
    # is simply missing those weeks, and reconstructing across it would be wrong.
    txns_all, opt_txns, names = merge_histories(parsed)
    dmin, gaps = continuous_start(txns_all)
    dmax = max((t["date"] for L in txns_all.values() for t in L), default=None)

    txns = {s: [t for t in L if t["date"] >= dmin] for s, L in txns_all.items()}
    txns = {s: L for s, L in txns.items() if L}
    opt_txns = {s: [t for t in L if t["date"] >= dmin] for s, L in opt_txns.items()}
    opt_txns = {s: L for s, L in opt_txns.items() if L}
    tot_buy = sum(-t["amount"] for L in txns.values() for t in L if t["side"] == "BUY")
    tot_sell = sum(t["amount"] for L in txns.values() for t in L if t["side"] == "SELL")
    totals = (tot_buy, tot_sell)
    deposits, dividends = union_cash(parsed, dmin, dmax)
    life_dep, _ = union_cash(parsed, "0000-00", "9999-99")

    n_trades = sum(len(L) for L in txns.values())
    print(f"· portfolio:  {portfolio}")
    print(f"· merged {len(parsed)} exports → {n_trades} trades, window {dmin}→{dmax}")
    if gaps:
        print(f"  (excluded earlier data before {dmin}: coverage gap "
              + ", ".join(f"{a}→{b}" for a, b in gaps) + ")")
    print(f"· deposits {dmin[:7]}→{dmax[:7]}: ${deposits:,.0f}  ·  lifetime deposits ${life_dep:,.0f}  ·  dividends ${dividends:,.2f}")

    cur = parse_portfolio(portfolio)
    tickers = sorted(set(list(txns) + list(cur)))

    # fetch window: a few days before first trade through day after last
    start = (datetime.date.fromisoformat(dmin) - datetime.timedelta(days=4)).isoformat()
    end = (datetime.date.fromisoformat(dmax) + datetime.timedelta(days=1)).isoformat()
    BENCH = ["^GSPC", "^IXIC"]   # S&P 500, NASDAQ Composite
    prices = fetch_prices(sorted(set(tickers) | set(BENCH)), start, end, no_fetch=args.no_fetch)

    payload = build_payload(txns, opt_txns, names, cur, prices, deposits, totals, dmin, dmax, dividends, life_dep)
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    open(args.out, "w").write(render_html(payload))
    s = payload["summary"]
    print(f"\n✓ dashboard written: {args.out}")
    print(f"  window {s['dateRange'][0]}→{s['dateRange'][1]} · {s['numStocks']} stocks ({s['numHeld']} held) · {len(payload['options'])} options")
    print(f"  market value {s['marketValue']:,.0f} · unrealized {s['unrealized']:+,.0f} · realized(window) {s['realized']:+,.0f}")

if __name__ == "__main__":
    main()

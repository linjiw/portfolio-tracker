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

# Momentum oscillator scale: the divisor on the EMA5-vs-EMA21 separation inside
# tanh. Smaller = more reactive (saturates sooner). 0.06 was tuned so a field of
# strong names doesn't all pin at ±100 (0.04 saturated). Named so the docstring
# and the code can't silently drift apart (they had: doc said 0.04, code 0.06).
MOM_SCALE = 0.06

def compute_fib(items):
    """Fibonacci EMA ribbon (5/8/13/21) + momentum + RSI + crossover signals.
    Works on ANY (date, value) series — a single stock's closes OR the whole
    portfolio's daily net-worth curve.

    Momentum = 100*tanh((EMA5-EMA21)/EMA21 / MOM_SCALE): signed by fast-vs-slow
    separation, smoothly bounded to ±100 (MOM_SCALE = 0.06).

    State borrows the Alligator idea but checks STACK ORDER FIRST, so a genuine
    low-volatility trend isn't mislabeled as chop: cleanly stacked EMAs = up/down
    trend; otherwise a tight ribbon (<0.8% of price) = range, else transition.

    NOTE: "golden"/"death" here are the FAST ribbon cross (EMA5 x EMA13) — much
    faster/noisier than the classic 50/200-day cross; resonance gating (trend +
    recent cross + RSI-not-extreme) filters the false crosses out."""
    dates = [d for d, _ in items]
    px = [p for _, p in items]
    if len(px) < 21:
        return None
    e5, e8, e13, e21 = _ema(px, 5), _ema(px, 8), _ema(px, 13), _ema(px, 21)
    rsi = _rsi(px)
    mom, state = [], []
    for i in range(len(px)):
        sp = (e5[i] - e21[i]) / e21[i] if e21[i] else 0
        mom.append(round(100 * math.tanh(sp / MOM_SCALE), 1))
        band = max(e5[i], e8[i], e13[i], e21[i]) - min(e5[i], e8[i], e13[i], e21[i])
        w = band / px[i] * 100 if px[i] else 0
        if e5[i] > e8[i] > e13[i] > e21[i]:
            state.append("up")
        elif e5[i] < e8[i] < e13[i] < e21[i]:
            state.append("down")
        elif w < 0.8:
            state.append("range")
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

# ---------------------------------------------------------------- behavioral
def analyze_behavior(stocks, summary, prices, dmin, dmax):
    """Behavioral-economics decision support, grounded in Thaler, "Behavioral
    Economics: Past, Present, and Future" (AER 2016). Detects common investing
    biases from the user's OWN trades & positions and returns observation+nudge
    flags. These are reflective prompts, NOT trade advice — every nudge reframes
    the decision toward the user's own thesis, never "buy/sell X".

    Detectors (each with a concrete, data-driven rule):
      disposition  — Odean PGR/PLR: realize winners, ride losers (p.1582)
      overtrading  — trade count / turnover vs the index benchmark (p.1587,1593)
      concentration— single-name weight, top-5, HHI (self-control, p.1595)
      sunkcost     — buying below avg into already-losing names (p.1585,1592)
      anchoring    — selling clustered at break-even / cost (p.1594)
      recency      — buying after a sharp run-up (extrapolation, p.1588-89)
    """
    held = [s for s in stocks if s["held"]]
    mv = summary.get("marketValue") or 1.0
    eps = 0.01
    flags = []

    def add(fid, level, title, headline, detail, nudge, ref, examples=None):
        flags.append({"id": fid, "level": level, "title": title, "headline": headline,
                      "detail": detail, "nudge": nudge, "ref": ref, "examples": examples or []})

    # collect sells / buys with running context from each stock's walked rows
    sells, buys, avgdown = [], [], []
    for s in stocks:
        rows = s["txns"]
        for j, t in enumerate(rows):
            if t["side"] == "SELL" and t["realized"] is not None:
                sells.append({"sym": s["sym"], "date": t["date"], "price": t["price"],
                              "proceeds": abs(t["amount"]), "realized": t["realized"]})
            elif t["side"] == "BUY":
                prev_avg = rows[j - 1]["avg"] if j > 0 else 0
                buys.append({"sym": s["sym"], "date": t["date"], "price": t["price"],
                             "amount": abs(t["amount"]), "prev_avg": prev_avg})
                if prev_avg and t["price"] < prev_avg * 0.995:
                    avgdown.append({"sym": s["sym"], "date": t["date"], "price": t["price"]})

    # 1) Disposition effect — Odean's PGR vs PLR
    win_sells = [x for x in sells if x["realized"] > eps]
    los_sells = [x for x in sells if x["realized"] < -eps]
    held_win = [s for s in held if s["unreal"] > 0]
    held_los = [s for s in held if s["unreal"] < 0]
    pgr = len(win_sells) / (len(win_sells) + len(held_win)) if (len(win_sells) + len(held_win)) else 0
    plr = len(los_sells) / (len(los_sells) + len(held_los)) if (len(los_sells) + len(held_los)) else 0
    ratio = (pgr / plr) if plr > 0 else (9.99 if pgr > 0 else 0)
    lvl = "alert" if ratio >= 1.5 else ("watch" if ratio >= 1.1 else "good")
    top_losers = sorted(held_los, key=lambda s: s["unrealPct"])[:5]
    add("disposition", lvl, "处置效应 · 卖盈持亏",
        f"PGR/PLR ≈ {ratio:.2f}（盈利卖出 {len(win_sells)} 笔、亏损卖出 {len(los_sells)} 笔；当前持仓盈 {len(held_win)} / 亏 {len(held_los)}）",
        f"PGR(已兑现盈利占比)={pgr:.0%}，PLR(已兑现亏损占比)={plr:.0%}。比值 >1.5 说明你更倾向于过早兑现盈利、却死扛亏损——经典的处置效应。",
        "在买入当下就写下卖出条件（目标价 / 止损 / “逻辑破坏即走”），让卖出由逻辑是否成立驱动，而不是由此刻是红是绿驱动。",
        "Thaler 2016 · Problem 1 (p.1582)",
        [f"{s['sym']} {s['unrealPct']:+.0f}%（仍持有）" for s in top_losers])

    # 2) Overconfidence & overtrading — turnover vs benchmark
    nbuys, nsells = len(buys), len(sells)
    ntrades = nbuys + nsells
    span = max(1, (datetime.date.fromisoformat(dmax) - datetime.date.fromisoformat(dmin)).days)
    avg_val = (summary.get("netWorthStart", 0) + summary.get("netWorthNow", 0)) / 2 or mv
    gross = summary.get("totalBuy", 0) + summary.get("totalSell", 0)
    turn_ann = (gross / avg_val * (365.0 / span)) if avg_val else 0
    sp = summary.get("spReturn")
    alpha = (summary["curReturn"] - sp) if sp is not None else None
    lvl = "alert" if turn_ann > 6 else ("watch" if turn_ann > 3 else "good")
    det = (f"窗口 {span} 天内 {ntrades} 笔交易（买 {nbuys} / 卖 {nsells}），成交额 ${gross:,.0f} ≈ 平均持仓市值的 {gross/avg_val:.1f} 倍，年化换手 ≈ {turn_ann*100:,.0f}%。")
    if alpha is not None:
        det += f" 同期组合时间加权 {summary['curReturn']:+.1f}% vs S&P {sp:+.1f}%（超额 {alpha:+.1f}%）。"
    det += " Thaler 指出主动交易平均跑不赢指数，高换手通常被成本与税侵蚀。"
    add("overtrading", lvl, "过度自信 · 过度交易",
        f"年化换手 ≈ {turn_ann*100:,.0f}% · 共 {ntrades} 笔" + (f" · 超额 {alpha:+.1f}%" if alpha is not None else ""),
        det,
        "给冲动加一道闸：用“观察清单”代替即时下单，设自我约束的最短持有期，并定期对照指数检查折腾是否真换来了超额收益。",
        "Thaler 2016 · 过度自信 (p.1578,1593-94)；主动管理 (p.1587)")

    # 3) Concentration — self-control / overconfidence
    weights = sorted([(s["sym"], s["value"] / mv * 100) for s in held], key=lambda x: -x[1])
    top = weights[0] if weights else ("—", 0)
    top5 = sum(w for _, w in weights[:5])
    hhi = sum((w / 100) ** 2 for _, w in weights)
    lvl = "alert" if (top[1] > 25 or top5 > 70) else ("watch" if (top[1] > 18 or top5 > 55) else "good")
    add("concentration", lvl, "集中度 · 自控与默认机制",
        f"最大持仓 {top[0]} 占 {top[1]:.0f}% · 前五合计 {top5:.0f}% · HHI {hhi:.2f}",
        f"单一标的 {top[0]} 占净值 {top[1]:.0f}%，前 5 大合计 {top5:.0f}%。集中会同时放大正确判断与单一标的的回撤。",
        "为每个仓位设一个目标权重区间，越界即提醒再平衡——把“先定规则、到点执行”做成默认动作（Save More Tomorrow 思路），而非临场凭情绪。",
        "Thaler 2016 · 自控/默认与承诺机制 (p.1595-96)",
        [f"{sym} {w:.0f}%" for sym, w in weights[:5]])

    # 4) Sunk-cost / loss aversion — averaging down into current losers
    loser_map = {s["sym"]: s for s in held if s["unrealPct"] < -2}
    ad_los = [b for b in avgdown if b["sym"] in loser_map]
    by_sym = {}
    for b in ad_los:
        by_sym[b["sym"]] = by_sym.get(b["sym"], 0) + 1
    lvl = "watch" if ad_los else "good"
    ex = sorted(by_sym.items(), key=lambda kv: -kv[1])[:5]
    add("sunkcost", lvl, "沉没成本 · 越跌越买",
        (f"向浮亏仓位低于均价加仓 {len(ad_los)} 次，涉及 {len(by_sym)} 只" if ad_los else "未见明显“为摊薄成本而买入浮亏仓”的模式"),
        ("在已经浮亏、且买价低于持仓均价时继续加仓，会把“已经投入”变成继续持有的理由。但投入的钱无论如何收不回，不应左右下一步决定。"
         if ad_los else "当前没有明显的沉没成本加仓信号。"),
        "做一个只看未来的检验：“若我现在空仓，会按这个价格、这个仓位重新买入它吗？”是→留；否→与已投入无关地考虑减仓。",
        "Thaler 2016 · 沉没成本/餐厅例 (p.1585)；损失厌恶 (p.1592)",
        [f"{sym} 加仓{n}次（现 {loser_map[sym]['unrealPct']:+.0f}%）" for sym, n in ex])

    # 5) Anchoring — selling clustered at break-even
    be_sells = [x for x in sells if x["proceeds"] > 0 and abs(x["realized"]) / x["proceeds"] < 0.02]
    be_frac = len(be_sells) / len(sells) if sells else 0
    lvl = "watch" if (be_frac > 0.30 and len(sells) >= 5) else "good"
    add("anchoring", lvl, "锚定 · 回本就卖",
        f"约 {be_frac:.0%} 的卖出在成本价附近（盈亏 < 成交额 2%），{len(be_sells)}/{len(sells)} 笔",
        "卖出大量集中在“刚好回本”附近，常意味着把买入成本当成了决策锚——而买入价对未来涨跌其实无关。",
        "评估默认用“当前价 / 趋势信号”这套前瞻视角，把成本只当作记账数字，不当作目标价。",
        "Thaler 2016 · 锚定 (Kahneman & Tversky 三启发式, p.1594)")

    # 6) Recency / extrapolation — buying after a sharp run-up
    buy_high = []
    for b in buys:
        d = datetime.date.fromisoformat(b["date"])
        pp = price_on(prices, b["sym"], (d - datetime.timedelta(days=20)).isoformat())
        if pp and b["price"] > pp * 1.12:
            buy_high.append({"sym": b["sym"], "date": b["date"], "runup": (b["price"] / pp - 1) * 100})
    bh_frac = len(buy_high) / len(buys) if buys else 0
    avg_runup = (sum(x["runup"] for x in buy_high) / len(buy_high)) if buy_high else 0
    lvl = "watch" if (bh_frac > 0.25 and len(buys) >= 10) else "good"
    bh_top = sorted(buy_high, key=lambda x: -x["runup"])[:5]
    add("recency", lvl, "近因 · 追涨",
        f"约 {bh_frac:.0%} 的买入发生在 20 日内涨超 12% 之后（{len(buy_high)}/{len(buys)} 笔，平均追涨 +{avg_runup:.0f}%）",
        "在近期大涨后买入，隐含“涨的会继续涨”的外推预期——这正是 Thaler 讲的近因/外推偏差，也是泡沫的微观机制。",
        "追高的当下提醒自己“近期表现不预测未来收益”；用分批建仓 / 定投来削弱追涨冲动。",
        "Thaler 2016 · 外推/近因与泡沫 (p.1588-89,1593)",
        [f"{x['sym']} {x['date']} +{x['runup']:.0f}%" for x in bh_top])

    order = {"alert": 0, "watch": 1, "good": 2, "info": 3}
    flags.sort(key=lambda f: order.get(f["level"], 9))
    stats = {"sells": nsells, "buys": nbuys, "trades": ntrades,
             "winSells": len(win_sells), "losSells": len(los_sells),
             "heldWin": len(held_win), "heldLos": len(held_los),
             "pgr": round(pgr, 3), "plr": round(plr, 3), "dispositionRatio": round(ratio, 2),
             "turnoverAnnPct": round(turn_ann * 100, 0), "topWeight": round(top[1], 1),
             "top5Weight": round(top5, 1), "hhi": round(hhi, 3)}
    return {"flags": flags, "stats": stats}

# ---------------------------------------------------------------- risk
def compute_risk(series, stocks):
    """Realized in-window RISK to sit beside the return gauge. Computed on the
    time-weighted-return basis (the daily `ret` series), NOT series[].value —
    value mixes in deposits/trades and would overstate drawdown wrongly. Returns:
      annVol/spAnnVol  annualized stdev of daily TWR (×√252), portfolio vs S&P
      beta             cov(port, sp)/var(sp) over aligned daily returns
      maxDrawdown      deepest peak-to-trough on the cumulative TWR curve (+dates)
      currentUnderwater  how far below the high-water mark right now
      uwSeries         daily underwater % (portfolio + S&P) for the red chart
      volSeries        21-day rolling annualized vol (portfolio + S&P)
      contrib          per-holding risk-contribution: dollar weight vs share of
                       portfolio volatility (marginal contribution, sums to 100%)
    Equity-only & DESCRIPTIVE (small-sample covariance — not a forecast). None if
    <25 days of curve or no held names. Pure stdlib (no numpy) — zero-dep ethos."""
    import statistics
    if len(series) < 25:
        return None
    cum = [1 + (p.get("ret") or 0) / 100 for p in series]
    spcum = [1 + (p.get("sp500") or 0) / 100 for p in series]
    r = [cum[i] / cum[i - 1] - 1 for i in range(1, len(cum))]
    spr = [spcum[i] / spcum[i - 1] - 1 for i in range(1, len(spcum))]
    rt = 252 ** 0.5

    def underwater(c):
        peak, out = c[0], []
        for v in c:
            peak = max(peak, v)
            out.append((v / peak - 1) * 100 if peak else 0.0)
        return out
    uw, spuw = underwater(cum), underwater(spcum)
    uwSeries = [{"date": series[i]["date"], "uw": round(uw[i], 2), "spuw": round(spuw[i], 2)}
                for i in range(len(series))]
    maxdd = min(uw); mddi = uw.index(maxdd)
    peaki = cum.index(max(cum[:mddi + 1]))
    cur_uw = uw[-1]
    annVol = statistics.pstdev(r) * rt * 100 if len(r) >= 2 else 0.0
    spAnnVol = statistics.pstdev(spr) * rt * 100 if len(spr) >= 2 else 0.0
    beta = None
    nb = min(len(r), len(spr))
    if nb >= 3:
        ra, sa = r[-nb:], spr[-nb:]
        mr, ms = sum(ra) / nb, sum(sa) / nb
        cov = sum((ra[i] - mr) * (sa[i] - ms) for i in range(nb)) / nb
        var = sum((sa[i] - ms) ** 2 for i in range(nb)) / nb
        beta = cov / var if var else None
    cumret = series[-1].get("ret") or 0
    retVolRatio = cumret / annVol if annVol else 0.0

    volSeries = []
    for t in range(len(series)):
        if t >= 21:
            w, sw = r[t - 21:t], spr[t - 21:t]
            volSeries.append({"date": series[t]["date"],
                              "vol": round(statistics.pstdev(w) * rt * 100, 2) if len(w) >= 2 else None,
                              "spvol": round(statistics.pstdev(sw) * rt * 100, 2) if len(sw) >= 2 else None})
        else:
            volSeries.append({"date": series[t]["date"], "vol": None, "spvol": None})

    # per-holding risk contribution (marginal contribution to portfolio vol)
    axis = [p["date"] for p in series]
    held = [s for s in stocks if s["held"]]
    tot_val = sum(s["value"] for s in held) or 1.0
    names, excluded = [], []
    for s in held:
        pm = dict(s.get("prices", []))
        overlap = sum(1 for d in axis if d in pm)
        first = next((pm[d] for d in axis if d in pm), None)
        if overlap < 30 or first is None:
            excluded.append(s["sym"]); continue
        closes, last = [], first
        for d in axis:
            if d in pm:
                last = pm[d]
            closes.append(last)
        rr = [closes[i] / closes[i - 1] - 1 if closes[i - 1] else 0.0 for i in range(1, len(closes))]
        names.append({"sym": s["sym"], "w": s["value"] / tot_val, "r": rr})

    contrib = []
    if names:
        m, T = len(names), len(names[0]["r"])
        means = [sum(nm["r"]) / T for nm in names]
        Sigma = [[0.0] * m for _ in range(m)]
        for a in range(m):
            for b in range(a, m):
                cov = sum((names[a]["r"][k] - means[a]) * (names[b]["r"][k] - means[b]) for k in range(T)) / T
                Sigma[a][b] = Sigma[b][a] = cov
        w = [nm["w"] for nm in names]
        Sw = [sum(Sigma[a][b] * w[b] for b in range(m)) for a in range(m)]
        pvar = sum(w[a] * Sw[a] for a in range(m))
        pvol = pvar ** 0.5
        if pvol > 0:
            mcr = [w[a] * Sw[a] / pvol for a in range(m)]
            tot = sum(mcr) or 1.0
            for a in range(m):
                rc = mcr[a] / tot * 100
                avol = (Sigma[a][a] ** 0.5) * rt * 100 if Sigma[a][a] > 0 else 0.0
                contrib.append({"sym": names[a]["sym"], "weightPct": round(w[a] * 100, 1),
                                "riskPct": round(rc, 1), "gap": round(rc - w[a] * 100, 1),
                                "annVol": round(avol, 1)})
            contrib.sort(key=lambda c: -c["riskPct"])

    return {"annVol": round(annVol, 1), "spAnnVol": round(spAnnVol, 1),
            "beta": (round(beta, 2) if beta is not None else None),
            "maxDrawdown": round(maxdd, 1), "maxDDpeak": series[peaki]["date"],
            "maxDDtrough": series[mddi]["date"], "currentUnderwater": round(cur_uw, 2),
            "retVolRatio": round(retVolRatio, 2), "uwSeries": uwSeries, "volSeries": volSeries,
            "contrib": contrib, "excluded": excluded,
            "basisNote": "equity-only TWR basis; excludes cash/margin/options"}

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

    # portfolio-level Fibonacci: the SAME EMA ribbon / RSI / golden-death engine,
    # run on the daily net-worth curve so the whole portfolio gets its own ribbon
    # and crossover signals (None if <21 trading days of curve).
    portfolio_fib = compute_fib([(p["date"], p["value"]) for p in series])

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
    behavior = analyze_behavior(stocks, summary, prices, dmin, dmax)
    risk = compute_risk(series, stocks)
    return {"summary": summary, "stocks": stocks, "options": opts, "series": series,
            "portfolioFib": portfolio_fib, "behavior": behavior, "risk": risk}

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
 (opts.marks||[]).forEach(g=>{const x=xs(g.date),c=g.type==='golden'?'#4FB286':'#E5707A';el+=`<line x1="${x}" y1="${mT}" x2="${x}" y2="${H-mB}" stroke="${c}" stroke-opacity="0.4" stroke-dasharray="2 3"/>`;});
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
 <div class="seg-rail"><button class="on" data-seg="nw">净值</button><button data-seg="pfib">组合斐波那契</button><button data-seg="risk">风险</button><button data-seg="cmp">指数对比</button><button data-seg="sig">持仓信号</button><button data-seg="beh">行为决策</button><button data-seg="rebal">再平衡计划</button></div>
 <div class="seg" data-seg="nw">
 <div class="card">
   <div class="dh"><span class="t">组合总览</span><span class="nm">持仓市值与收益率（股票部分，不含现金/期权）</span></div>
   <div class="badges">${cards.map(c=>`<div class="badge"><div class="l">${c[0]}</div><div class="v">${c[1]}</div></div>`).join('')}</div>
 </div>
 <div class="card"><div style="font-weight:650;margin-bottom:4px">持仓总市值（$） · 叠加组合斐波那契动能</div>
   <div class="legend"><span>底部色带 = 组合加权动能：<span style="color:#4FB286">绿=强(>15)</span> / <span style="color:#E8B339">黄=中性</span> / <span style="color:#E5707A">红=弱(<-15)</span>，用来对照净值看择时节奏</span></div>
   ${nwChart(ser)}</div>
 </div>
 <div class="seg" data-seg="pfib" hidden>`+portfolioFibCard()+`</div>
 <div class="seg" data-seg="cmp" hidden>
 <div class="card"><div style="font-weight:650;margin-bottom:4px">累计收益率对比（%，时间加权）</div>
   <div class="legend"><span><i style="background:#E8B339"></i>我的组合</span><span><i style="background:#888D96"></i>S&P 500</span><span><i style="background:#E8B339"></i>纳斯达克综合</span></div>
   ${svgLines(ser,[{key:'ret',color:'#E8B339'},{key:'sp500',color:'#888D96',dash:1},{key:'nasdaq',color:'#E8B339',dash:1}],{zero:true,fmt:v=>v.toFixed(0)+'%'})}</div>
 </div>
 <div class="seg" data-seg="sig" hidden>`+positionSignalsCard()+resonanceCard()+fibRanking()+`</div>
 <div class="seg" data-seg="beh" hidden>`+behaviorCard()+`</div>
 <div class="seg" data-seg="risk" hidden>`+riskCard()+`</div>
 <div class="seg" data-seg="rebal" hidden>`+rebalancePlanner()+`</div>`;
 segWire();wireRebal();
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
function fibBadges(n,signals,curLabel){
 const sc=momColor(n.mom),rsiCol=n.rsi>70?'#E5707A':(n.rsi<30?'#4FB286':'#e6ecf5');
 const lastSig=(signals||[]).slice(-1)[0];
 return [
  [curLabel||'斐波那契状态',`<span style="color:${FIBCOL[n.state]}">●</span> ${n.label}`],
  ['动能强弱',`<span style="color:${sc}">${n.mom>0?'+':''}${n.mom}</span> <span class="note">/100</span>`],
  ['RSI(14)',`<span style="color:${rsiCol}">${n.rsi}</span>`],
  ['最近信号',lastSig?(lastSig.type==='golden'?`<span class="pos">金叉 ${lastSig.date}</span>`:`<span class="neg">死叉 ${lastSig.date}</span>`):'—'],
  ['多指标共振',n.res==='bull'?'<span class="pos">多头共振中</span>':(n.res==='bear'?'<span class="neg">空头共振中</span>':'无')],
 ];
}
function portfolioFibCard(){
 const pf=DATA.portfolioFib,ser=DATA.series||[];
 if(!pf||ser.length<2)return `<div class="card"><div class="dh"><span class="t">组合斐波那契</span></div><div class="note">组合净值样本不足（需 ≥21 个交易日）。</div></div>`;
 const pseudo={sym:'组合净值',prices:ser.map(p=>[p.date,p.value]),fib:pf,curPrice:ser[ser.length-1].value,held:true};
 const n=pf.now,fmtK=v=>'$'+(v/1000).toFixed(0)+'k';
 const sser=ser.map((p,i)=>({date:p.date,mom:pf.mom[i],rsi:pf.rsi[i]}));
 const badges=fibBadges(n,pf.signals,'组合趋势状态');
 return `<div class="card">
   <div class="dh"><span class="t">组合斐波那契</span><span class="nm">EMA 5/8/13/21 缎带 · 金叉/死叉 · RSI —— 直接计算在组合净值曲线上</span></div>
   <div class="badges">${badges.map(b=>`<div class="badge"><div class="l">${b[0]}</div><div class="v">${b[1]}</div></div>`).join('')}</div>
   <div class="legend">
     <span><i style="background:#E8B339"></i>EMA5</span><span><i style="background:#C99A3A"></i>EMA8</span>
     <span><i style="background:#8C8A6E"></i>EMA13</span><span><i style="background:#5F6168"></i>EMA21</span>
     <span><i style="background:#4FB286"></i>金叉(5×13)</span><span><i style="background:#E5707A"></i>死叉</span>
     <span>◎ 共振</span>
     <span>底部带：<span style="color:#4FB286">绿=多头</span>/<span style="color:#E5707A">红=空头</span>/<span style="color:#E8B339">黄=转换</span>/<span style="color:#6B7079">灰=盘整</span></span></div>
   ${fibChart(pseudo,fmtK)}
   <div style="font-weight:650;margin:12px 0 2px">组合动能振荡器（−100 ~ +100）</div>
   ${svgLines(sser,[{key:'mom',color:'#E8B339'}],{zero:true,h:200,fixed:[-105,105],fmt:v=>v.toFixed(0),guides:[{v:15,color:'#2f6b4f',label:'强多'},{v:-15,color:'#6b2f2f',label:'强空'}],marks:pf.signals})}
   <div style="font-weight:650;margin:12px 0 2px">组合 RSI(14)</div>
   ${svgLines(sser,[{key:'rsi',color:'#6E9CA6'}],{h:180,fixed:[0,100],fmt:v=>v.toFixed(0),guides:[{v:70,color:'#6b2f2f',label:'超买70'},{v:30,color:'#2f6b4f',label:'超卖30'}],marks:pf.signals})}
   <div class="note" style="margin-top:10px"><b>怎么读：</b>把整支组合当成一只“基金”，在净值曲线上算 EMA 缎带与金叉/死叉，用来看组合整体的趋势结构与择时节奏。<b>金叉/死叉为快线 EMA5×EMA13 交叉</b>（非传统 50/200 日均线，更快也更灵敏）。竖虚线标出交叉日期。<b>技术参考，非投资建议。</b></div>
 </div>`;
}
function postureOf(n){
 const t=n.state==='up'?'多头':(n.state==='down'?'空头':(n.state==='range'?'盘整':'转换'));
 const m=Math.abs(n.mom)>40?'动能强':(Math.abs(n.mom)>15?'动能中':'动能弱');
 const r=n.rsi>70?' · RSI超买':(n.rsi<30?' · RSI超卖':'');
 return `${t} · ${m}${r}`;
}
function positionSignalsCard(){
 const mv=S.marketValue||1;
 const held=stocks.filter(x=>x.held&&x.fib&&x.fib.now).map(x=>({x,w:x.value/mv*100})).sort((a,b)=>b.w-a.w);
 if(!held.length)return'';
 const rows=held.map(({x,w})=>{const n=x.fib.now,sig=(x.fib.signals||[]).slice(-1)[0];
   const sigTxt=sig?(sig.type==='golden'?`<span class="pos">金叉 ${sig.date.slice(5)}</span>`:`<span class="neg">死叉 ${sig.date.slice(5)}</span>`):'—';
   const res=n.res==='bull'?'<span class="pos">多</span>':(n.res==='bear'?'<span class="neg">空</span>':'—');
   const rc=n.rsi>70?'neg':(n.rsi<30?'pos':'');
   return `<tr style="cursor:pointer" onclick="sel='${x.sym}';renderList();window.scrollTo({top:0,behavior:'smooth'})">
     <td class="l"><span style="color:${FIBCOL[n.state]}">●</span> ${x.sym}</td>
     <td>${w.toFixed(1)}%</td><td style="color:${FIBCOL[n.state]}">${n.label}</td>
     <td style="color:${momColor(n.mom)}">${n.mom>0?'+':''}${n.mom}</td>
     <td class="${rc}">${n.rsi}</td><td>${sigTxt}</td><td>${res}</td>
     <td class="l">${postureOf(n)}</td></tr>`;}).join('');
 return `<div class="card"><div class="dh"><span class="t">持仓信号一览</span><span class="nm">按权重排序 · 趋势 / 动能 / RSI / 最近金死叉 / 共振（点击看个股）</span></div>
   <div class="scroll"><table><thead><tr><th class="l">代码</th><th>权重</th><th>状态</th><th>动能</th><th>RSI</th><th>最近信号</th><th>共振</th><th class="l">技术姿态</th></tr></thead>
   <tbody>${rows}</tbody></table></div>
   <div class="note" style="margin-top:8px">“技术姿态”只是均线/动能/RSI 的客观描述，<b>非买卖建议</b>；金叉/死叉为 EMA5×13 快线交叉。</div></div>`;
}
function behaviorCard(){
 const b=DATA.behavior;if(!b||!b.flags||!b.flags.length)return'<div class="card"><div class="note">行为分析数据不足。</div></div>';
 const LV={alert:['#E5707A','⚠ 需注意'],watch:['#E8B339','留意'],good:['#4FB286','✓ 良好'],info:['#888D96','提示']};
 const cards=b.flags.map(f=>{const c=LV[f.level]||LV.info;
   const ex=(f.examples&&f.examples.length)?`<div style="margin-top:7px;display:flex;gap:6px;flex-wrap:wrap">${f.examples.map(e=>`<span class="chip" style="border-color:${c[0]}55">${e}</span>`).join('')}</div>`:'';
   return `<div class="card" style="border-left:3px solid ${c[0]}">
     <div class="dh"><span class="t">${f.title}</span><span class="chip" style="color:${c[0]};border-color:${c[0]}66">${c[1]}</span></div>
     <div style="font-weight:650;color:${c[0]};margin:3px 0 6px">${f.headline}</div>
     <div class="note" style="color:var(--txt);line-height:1.55">${f.detail}</div>${ex}
     <div style="margin-top:9px;padding:9px 11px;background:rgba(232,179,57,.07);border-radius:8px;line-height:1.55"><b>💡 Nudge：</b>${f.nudge}</div>
     <div class="note" style="margin-top:6px;opacity:.65">${f.ref}</div></div>`;}).join('');
 return `<div class="card"><div class="dh"><span class="t">行为决策辅助</span><span class="nm">基于 Thaler《行为经济学：过去、现在与未来》(2016)</span></div>
   <div class="note" style="margin-top:6px;line-height:1.6">以下信号由<b>你自己的真实买卖与持仓</b>计算而来，用于发现常见的行为偏差。它们是<b>“提醒”而非投资建议</b>——目的是帮你按既定逻辑决策、少受“红/绿盘”情绪左右。</div></div>${cards}`;
}
function riskCard(){
 const R=DATA.risk;
 if(!R)return'<div class="card"><div class="dh"><span class="t">风险</span></div><div class="note">风险数据不足（需 ≥25 个交易日）。</div></div>';
 const volCls=R.annVol>R.spAnnVol?'neg':'pos';
 const badges=[
  ['年化波动率',`<span class="${volCls}">${R.annVol.toFixed(1)}%</span> <span class="note">S&P ${R.spAnnVol.toFixed(1)}%</span>`],
  ['Beta β (vs S&P)',R.beta==null?'—':`<span class="${R.beta>1?'neg':'pos'}">${R.beta.toFixed(2)}</span>`],
  ['最大回撤',`<span class="neg">${R.maxDrawdown.toFixed(1)}%</span>`],
  ['当前回撤',`<span class="${cls(R.currentUnderwater)}">${R.currentUnderwater.toFixed(1)}%</span>`],
  ['收益/波动比',`${R.retVolRatio.toFixed(2)} <span class="note">(rf=0)</span>`],
 ];
 const rows=(R.contrib||[]).map(c=>{const g=c.gap,gc=g>0?'#E5707A':'#4FB286',w=Math.min(Math.abs(g),20)/20*50,left=g>=0?50:50-w;
   return `<tr style="cursor:pointer" onclick="sel='${c.sym}';renderList();window.scrollTo({top:0,behavior:'smooth'})">
     <td class="l">${c.sym}</td><td>${c.weightPct.toFixed(1)}%</td><td>${c.riskPct.toFixed(1)}%</td>
     <td><div class="fbar"><div class="z"></div><div class="p" style="left:${left}%;width:${w}%;background:${gc}"></div></div></td>
     <td style="color:${gc}">${g>0?'+':''}${g.toFixed(1)}</td></tr>`;}).join('');
 const exNote=(R.excluded&&R.excluded.length)?`<div class="note" style="margin-top:8px">以下标的价格重叠不足 30 日，未计入风险分解：${R.excluded.join('、')}</div>`:'';
 return `<div class="card">
   <div class="dh"><span class="t">风险</span><span class="nm">回撤 · 波动率 · Beta · 风险贡献（股票部分，不含现金/保证金/期权）</span></div>
   <div class="badges">${badges.map(b=>`<div class="badge"><div class="l">${b[0]}</div><div class="v">${b[1]}</div></div>`).join('')}</div></div>
 <div class="card"><div style="font-weight:650;margin-bottom:4px">水下回撤曲线（相对历史高点回撤 %）</div>
   <div class="legend"><span><i style="background:#E5707A"></i>我的组合</span><span><i style="background:#888D96"></i>S&P 500</span><span>0 = 创新高；越深 = 离高点越远（你真正“感受到”的亏损维度）</span></div>
   ${svgLines(R.uwSeries,[{key:'uw',color:'#E5707A'},{key:'spuw',color:'#888D96',dash:1}],{zero:true,area:true,fmt:v=>v.toFixed(0)+'%'})}
   <div class="note" style="margin-top:6px">最大回撤 <span class="neg">${R.maxDrawdown.toFixed(1)}%</span>（${R.maxDDpeak} 高点 → ${R.maxDDtrough} 低点）。</div></div>
 <div class="card"><div style="font-weight:650;margin-bottom:4px">21 日滚动年化波动率</div>
   <div class="legend"><span><i style="background:#E8B339"></i>我的组合</span><span><i style="background:#888D96"></i>S&P 500</span></div>
   ${svgLines(R.volSeries,[{key:'vol',color:'#E8B339'},{key:'spvol',color:'#888D96',dash:1}],{fmt:v=>v.toFixed(0)+'%'})}</div>
 <div class="card"><div class="dh"><span class="t">风险贡献分解</span><span class="nm">谁在制造组合波动 · 权重 ≠ 风险（点击看个股）</span></div>
   <div class="scroll"><table><thead><tr><th class="l">代码</th><th>资金权重</th><th>风险贡献</th><th>差额</th><th>风险−资金</th></tr></thead>
   <tbody>${rows}</tbody></table></div>${exNote}
   <div class="note" style="margin-top:8px"><b>怎么读：</b>“风险贡献”把组合总波动按各持仓的边际贡献拆开（合计 100%）。<b>差额为正(红)= 该标的对波动的贡献高于其资金占比</b>（隐藏的风险放大器）；<b>为负(绿)= 分散器</b>。高 Beta 单票常常风险占比远超资金占比。<br>这是对<b>窗口内已实现风险</b>的描述性分解（样本有限、非预测），且仅含股票（不含现金/保证金/期权，会<b>低估</b>你的真实杠杆风险）。<b>非投资建议。</b></div></div>`;
}
/* ===== Rebalancing Planner (Thaler commitment device) ===== */
let rebalDraft=null;
function rebalDefault(){return{policy:'cap',cap:20,band:5,glide:'edge',setOn:null};}
function rebalLoad(){try{const s=JSON.parse(localStorage.getItem('ptrak.rebal.v1'));if(s&&s.policy)return Object.assign(rebalDefault(),s);}catch(e){}return rebalDefault();}
function rebalSave(){rebalDraft.setOn=new Date().toISOString().slice(0,10);try{localStorage.setItem('ptrak.rebal.v1',JSON.stringify(rebalDraft));}catch(e){}}
function rebalClear(){try{localStorage.removeItem('ptrak.rebal.v1');}catch(e){}rebalDraft=rebalDefault();}
function rebalUniverse(){const MV=S.marketValue||stocks.filter(s=>s.held).reduce((a,s)=>a+s.value,0)||1;
 return{MV,u:stocks.filter(s=>s.held&&s.value>0).map(s=>({sym:s.sym,value:s.value,price:s.curPrice,w:s.value/MV*100})).sort((a,b)=>b.w-a.w)};}
function rebalVolMap(){const m={};((DATA.risk&&DATA.risk.contrib)||[]).forEach(c=>{if(c.annVol!=null&&c.annVol>0)m[c.sym]=c.annVol;});return m;}
function capTargets(ws,C){const N=ws.length;if(!N)return{t:[],fallback:false};if(C*N<100-1e-9)return{t:ws.map(()=>100/N),fallback:true};
 let t=ws.slice();
 for(let it=0;it<60;it++){let exc=0;t=t.map(v=>{if(v>C+1e-9){exc+=v-C;return C;}return v;});if(exc<1e-9)break;
  const base=t.reduce((a,v)=>a+(v<C-1e-9?v:0),0);
  if(base<=1e-9){const k=t.filter(v=>v<C-1e-9).length||N;t=t.map(v=>v<C-1e-9?v+exc/k:v);break;}
  t=t.map(v=>v<C-1e-9?v+exc*(v/base):v);}
 const s=t.reduce((a,b)=>a+b,0)||1;return{t:t.map(v=>v/s*100),fallback:false};}
function rebalTargets(rule,uni,volMap){const ws=uni.u.map(x=>x.w),N=ws.length;
 if(rule.policy==='equal')return{t:ws.map(()=>N?100/N:0),note:null,disabled:false};
 if(rule.policy==='invvol'){const ok=DATA.risk&&uni.u.every(x=>volMap[x.sym]>0);
  if(!ok)return{t:null,note:'部分持仓缺少波动率数据，反波动目标不可用',disabled:true};
  const raw=uni.u.map(x=>1/volMap[x.sym]),sm=raw.reduce((a,b)=>a+b,0)||1;
  return{t:raw.map(r=>r/sm*100),note:'目标 ∝ 1/波动率：高波动标的权重更低，使各仓位风险贡献趋于均衡',disabled:false};}
 const c=capTargets(ws,rule.cap);return{t:c.t,note:c.fallback?('上限 '+rule.cap+'% 对 '+N+' 只标的不可行，已退回等权'):null,disabled:false};}
function rebalHonesty(){return `<div class="card"><details><summary style="cursor:pointer;color:var(--mut)">诚实说明与边界（点击展开）</summary>
 <div class="note" style="margin-top:8px;line-height:1.65">本计划只是把权重拉回<b>你自己设定的区间</b>的描述性算术，仅含股票（不含现金/保证金/期权，会<b>低估</b>你的真实杠杆）。它<b>忽略</b>税费、洗售(wash-sale)、成本批次(lot)选择与交易/佣金/点差成本——交易成本若不实测，就是“万能借口”(Thaler)。反波动率目标基于“风险”页同一份<b>小样本、描述性（非预测）</b>协方差。股数四舍五入到整股。规则只存在<b>本浏览器本设备</b>，不跨设备。本计划<b>不声称提高收益</b>，只把权重拉回你选的区间。<b>非投资建议。</b></div></details></div>`;}
function rebalancePlanner(){
 if(rebalDraft===null)rebalDraft=rebalLoad();
 const rule=rebalDraft,uni=rebalUniverse(),volMap=rebalVolMap();
 if(!uni.u.length)return '<div class="card"><div class="dh"><span class="t">再平衡计划</span></div><div class="note">暂无持仓可用于再平衡。</div></div>';
 const invvolOk=DATA.risk&&uni.u.every(x=>volMap[x.sym]>0);
 const bt=(active,attrs,lbl,dis)=>`<button ${attrs} ${dis?'disabled':''} style="padding:5px 11px;border-radius:7px;border:1px solid ${active?'#E8B339':'#2A2E36'};background:${active?'rgba(232,179,57,.12)':'transparent'};color:${active?'#E8B339':(dis?'#555':'#cfd3da')};cursor:${dis?'not-allowed':'pointer'};font-size:12px;margin-right:6px">${lbl}</button>`;
 const pol=(k,lbl,dis)=>bt(rule.policy===k,`data-reb="pol" data-v="${k}"`,lbl,dis);
 const gl=(k,lbl)=>bt(rule.glide===k,`data-reb="glide" data-v="${k}"`,lbl,false);
 const stamp=rule.setOn?`<span class="chip" style="color:#4FB286;border-color:#1f5a40">规则设定于 ${rule.setOn}</span>`:`<span class="chip" style="color:#E8B339;border-color:#6b5a2f">建议默认 · 未保存</span>`;
 const row=(lbl,body)=>`<div style="display:flex;align-items:center;gap:10px;margin:9px 0;flex-wrap:wrap"><span style="color:var(--mut);min-width:78px;font-size:12px">${lbl}</span>${body}</div>`;
 const controls=`<div class="card">
   <div class="dh"><span class="t">再平衡计划</span><span class="nm">现在冷静时定下规则，之后由面板替你执行（Thaler 的 planner / doer 两个自我）</span></div>
   <div class="note" style="margin:2px 0 8px;line-height:1.6">规则存在本浏览器、跨刷新有效，替你扛住情绪上头的那一刻。你是<b>接受/微调默认</b>，而非从零搭建。 ${stamp}</div>
   ${row('策略',pol('cap','限制集中度')+pol('equal','等权')+pol('invvol','反波动·风险平价',!invvolOk))}
   ${rule.policy==='cap'?row('单一上限',`<input id="rebCap" type="number" min="5" max="50" step="1" value="${rule.cap}" style="width:60px;background:#15171B;color:#e6ecf5;border:1px solid #2A2E36;border-radius:6px;padding:3px 6px"> %`):''}
   ${row('触发区间 ±',`<input id="rebBand" type="number" min="1" max="20" step="0.5" value="${rule.band}" style="width:60px;background:#15171B;color:#e6ecf5;border:1px solid #2A2E36;border-radius:6px;padding:3px 6px"> pp <span class="note">（落在带内就什么都不做）</span>`)}
   ${row('回归方式',gl('edge','回到区间边缘')+gl('center','回到目标中心'))}
   <div style="margin-top:6px;display:flex;gap:8px">${bt(true,'id="rebSave"','确认并保存规则',false)}${bt(false,'id="rebClear"','清除规则',false)}</div></div>`;
 return controls+`<div id="rebalOut">`+rebalOutput(rule,uni,volMap)+`</div>`+rebalHonesty();
}
function rebalOutput(rule,uni,volMap){
 const tt=rebalTargets(rule,uni,volMap);
 if(tt.disabled||!tt.t)return `<div class="card"><div class="note">${tt.note||'目标不可用'}。请改选其它策略。</div></div>`;
 const MV=uni.MV,band=rule.band;
 const rows=uni.u.map((x,i)=>{const t=tt.t[i],lo=Math.max(0,t-band),hi=t+band,inB=x.w>=lo-1e-9&&x.w<=hi+1e-9,drift=x.w-t;
   let tw=t;if(!inB)tw=rule.glide==='center'?t:(x.w>hi?hi:lo);
   const d$=(tw-x.w)/100*MV,sh=x.price>0?Math.round(d$/x.price):null;
   return Object.assign({},x,{t,lo,hi,inB,drift,d$,sh});});
 const out=rows.filter(r=>!r.inB);
 const sells=out.filter(r=>r.d$<0).reduce((a,r)=>a-r.d$,0),buys=out.filter(r=>r.d$>0).reduce((a,r)=>a+r.d$,0),net=buys-sells;
 const hhi=a=>a.reduce((s,v)=>s+(v/100)**2,0);
 const wn=rows.map(r=>r.w),tn=rows.map(r=>r.t);
 const top=rows[0],topT=Math.max.apply(null,tn);
 const top5n=wn.slice(0,5).reduce((a,b)=>a+b,0),top5t=tn.slice().sort((a,b)=>b-a).slice(0,5).reduce((a,b)=>a+b,0);
 let banner;
 if(!out.length)banner=`<div class="card" style="border-left:3px solid #4FB286"><div style="font-weight:650;color:#4FB286">✓ 全部在区间内 · 无需操作</div><div class="note" style="margin-top:4px">在带内什么都不做，才是纪律所在——也直接对冲你的高换手。这是纪律的胜利。</div></div>`;
 else banner=`<div class="card" style="border-left:3px solid #E5707A"><div style="font-weight:650;color:#E5707A">你预先设定的规则触发了：${out.length} 只标的越界</div><div class="note" style="margin-top:4px">这不是“市场动了快行动”，而是你冷静时定下的 ±${band}pp 带宽到点了。${tt.note?'<br>'+tt.note:''}</div></div>`;
 const badges=[['最大持仓',`${top.sym} ${top.w.toFixed(0)}% → ${topT.toFixed(0)}%`],['前五合计',`${top5n.toFixed(0)}% → ${top5t.toFixed(0)}%`],
   ['HHI 集中度',`${hhi(wn).toFixed(2)} → ${hhi(tn).toFixed(2)}`],['越界标的',`${out.length} / ${rows.length}`],['双向成交额',`≈ ${fmt(sells+buys)}`]];
 const bars=rows.map(r=>{const c=r.inB?'#888D96':(r.drift>0?'#E5707A':'#4FB286'),wd=Math.min(Math.abs(r.drift),20)/20*50,left=r.drift>=0?50:50-wd;
   return `<div class="frow" style="cursor:pointer" onclick="sel='${r.sym}';renderList();window.scrollTo({top:0,behavior:'smooth'})">
     <span class="fsym">${r.sym}</span><div class="fbar"><div class="z"></div><div class="p" style="left:${left}%;width:${wd}%;background:${c}"></div></div>
     <span class="fval" style="color:${c}">${r.drift>0?'+':''}${r.drift.toFixed(1)}</span><span class="fst">${r.inB?'区间内':(r.drift>0?'超配':'低配')}</span></div>`;}).join('');
 const arows=out.map(r=>{const sh=r.sh==null?'<span class="note">缺价</span>':((r.d$<0?'−':'+')+Math.abs(r.sh)+'股'),edge=rule.glide==='center'?'目标':(r.drift>0?'上沿':'下沿');
   return `<tr style="cursor:pointer" onclick="sel='${r.sym}';renderList();window.scrollTo({top:0,behavior:'smooth'})">
     <td class="l">${r.sym}</td><td>${r.w.toFixed(1)}%</td><td>${r.lo.toFixed(0)}–${(r.t+band).toFixed(0)}%</td>
     <td style="color:${r.drift>0?'#E5707A':'#4FB286'}">${r.drift>0?'+':''}${r.drift.toFixed(1)}</td>
     <td class="l">${sh} <span class="note">(${fmt(Math.abs(r.d$))})</span> 拉回${edge}</td></tr>`;}).join('');
 const actionTable=out.length?`<div class="card"><div class="dh"><span class="t">动作清单</span><span class="nm">仅越界标的 · 你的规则触发</span></div>
   <div class="scroll"><table><thead><tr><th class="l">代码</th><th>当前</th><th>目标区间</th><th>偏离pp</th><th class="l">动作</th></tr></thead><tbody>${arows}</tbody></table></div>
   <div class="note" style="margin-top:8px">卖出释放 ${fmt(sells)} · 买入部署 ${fmt(buys)} · 净${net>=0?'需追加':'释放'} ${fmt(Math.abs(net))}（股数已取整，带内标的不动）。</div></div>`:'';
 return banner+`<div class="card"><div class="badges">${badges.map(b=>`<div class="badge"><div class="l">${b[0]}</div><div class="v">${b[1]}</div></div>`).join('')}</div></div>`
   +`<div class="card"><div style="font-weight:650;margin-bottom:4px">权重偏离目标（pp）· 颜色 = 方向</div>
      <div class="legend"><span><i style="background:#E5707A"></i>超配</span><span><i style="background:#4FB286"></i>低配</span><span><i style="background:#888D96"></i>区间内·无需操作</span></div>${bars}</div>`+actionTable;
}
function wireRebal(){const seg=document.querySelector('.seg[data-seg="rebal"]');if(!seg)return;
 seg.querySelectorAll('[data-reb]').forEach(b=>b.onclick=()=>{if(b.disabled)return;const k=b.dataset.reb,v=b.dataset.v;if(k==='pol')rebalDraft.policy=v;if(k==='glide')rebalDraft.glide=v;rerenderRebal();});
 const cap=seg.querySelector('#rebCap');if(cap)cap.onchange=()=>{rebalDraft.cap=Math.max(5,Math.min(50,+cap.value||20));rerenderRebal();};
 const band=seg.querySelector('#rebBand');if(band)band.onchange=()=>{rebalDraft.band=Math.max(1,Math.min(20,+band.value||5));rerenderRebal();};
 const sv=seg.querySelector('#rebSave');if(sv)sv.onclick=()=>{rebalSave();rerenderRebal();};
 const cl=seg.querySelector('#rebClear');if(cl)cl.onclick=()=>{if(rebalDraft.setOn&&!confirm('清除已保存的规则、退回建议默认？'))return;rebalClear();rerenderRebal();};}
function rerenderRebal(){const seg=document.querySelector('.seg[data-seg="rebal"]');if(seg){seg.innerHTML=rebalancePlanner();wireRebal();}}
function fibChart(s,fmtY){
 const f=s.fib,prices=s.prices;if(!f)return'<div class="note">价格数据不足，无法计算斐波那契指标。</div>';
 const yl=fmtY||(v=>'$'+v.toFixed(0));
 const W=900,H=400,mL=58,mR=80,mT=16,mB=46,stripH=12,stripY=H-mB+24;
 const xmin=+new Date(prices[0][0]),xmax=+new Date(D1);
 let ys=[];prices.forEach((p,i)=>{ys.push(p[1],f.e5[i],f.e21[i]);});
 let ymin=Math.min(...ys),ymax=Math.max(...ys);const pad=(ymax-ymin)*0.08||1;ymin-=pad;ymax+=pad;
 const xs=d=>mL+((+new Date(d)-xmin)/((xmax-xmin)||1))*(W-mL-mR);
 const yc=v=>mT+(1-(v-ymin)/((ymax-ymin)||1))*(H-mT-mB-stripH);
 let el='';
 for(let i=0;i<=4;i++){const v=ymin+(ymax-ymin)*i/4,y=yc(v);
   el+=`<line x1="${mL}" y1="${y}" x2="${W-mR}" y2="${y}" stroke="#1A1C21"/><text x="${mL-8}" y="${y+4}" fill="#6B7079" font-size="11" text-anchor="end">${yl(v)}</text>`;}
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
     guides:[{v:15,color:'#2f6b4f',label:'强多'},{v:-15,color:'#6b2f2f',label:'强空'}],marks:f.signals})}
   <div style="font-weight:650;margin:12px 0 2px">RSI(14)</div>
   ${svgLines(ser,[{key:'rsi',color:'#6E9CA6'}],{h:180,fixed:[0,100],fmt:v=>v.toFixed(0),
     guides:[{v:70,color:'#6b2f2f',label:'超买70'},{v:30,color:'#2f6b4f',label:'超卖30'}],marks:f.signals})}
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

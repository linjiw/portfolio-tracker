#!/usr/bin/env python3
# -*- coding: utf-8 -*-
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
derived from the two CSV files at run time. See docs/METHODOLOGY.md for methodology.
"""
import csv, re, json, glob, os, sys, argparse, datetime, math

HOME = os.path.expanduser("~")
HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "output", "prices_cache.json")
SECTORS_CACHE = os.path.join(HERE, "output", "sectors_cache.json")
OHLC_CACHE = os.path.join(HERE, "output", "ohlc_cache.json")

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

def collect_history_files(input_dir, explicit=None):
    """Return every known history export, plus an optional newly supplied file.

    Sync semantics are cumulative: a fresh Fidelity export is an increment or
    fuller snapshot of activity, not a replacement for older local history.
    """
    seen, out = set(), []
    for pat in ("History_for_Account*.csv", "Accounts_History*.csv"):
        for p in sorted(glob.glob(os.path.join(input_dir, pat)), key=os.path.getmtime):
            ap = os.path.abspath(p)
            if ap not in seen:
                seen.add(ap); out.append(ap)
    if explicit:
        ap = os.path.abspath(explicit)
        if ap not in seen:
            out.append(ap)
    return out

# ---------------------------------------------------------------- parsing
def _colmap(header):
    """Map a Fidelity history header row to column indices (handles both export
    layouts: with or without the extra Account/Account Number columns)."""
    idx = {}
    for i, h in enumerate(header):
        h = h.strip().lower()
        if h == "run date": idx["date"] = i
        elif h == "account number": idx["account"] = i
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
    m_acct = re.search(r"History_for_Account_([A-Z0-9]+)", os.path.basename(path))
    default_account = m_acct.group(1) if m_acct else ""
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
                   "price": fnum(r[cmap["price"]]), "amount": amt,
                   "account": r[cmap["account"]].strip() if "account" in cmap and len(r) > cmap["account"] else default_account}
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

    Sums every lot of a symbol across ALL accounts in the export — brokerage,
    retirement, and other account ids. Match
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
            cost = fnum(r[13])
            # Total Gain/Loss ($) is col 10, but Fidelity sometimes prints "--"
            # (e.g. after a same-day fill). For a long equity gain == value − cost
            # exactly (verified against reported rows), so fall back to that rather
            # than letting fnum("--")→0 zero out a real gain on a big holding.
            g_raw = r[10].strip()
            gain = fnum(g_raw) if g_raw not in ("", "--") else round(val - cost, 2)
            d = cur.setdefault(sym, {"shares": 0.0, "price": price, "value": 0.0, "gain": 0.0, "cost": 0.0})
            d["shares"] += shares; d["value"] += val; d["gain"] += gain; d["cost"] += cost; d["price"] = price
    for d in cur.values():
        d["avg"] = d["cost"] / d["shares"] if d["shares"] else 0
        d["gainpct"] = d["gain"] / d["cost"] * 100 if d["cost"] else 0
    return cur

def parse_account_extras(path):
    """Additive companion to parse_portfolio: returns exactly the rows the equity
    parser SKIPS — cash core ('**'), option legs ('-'), and 'Pending activity' —
    so whole-account net worth can be summed WITHOUT touching the broker-verified
    equity path that sync.py asserts to the cent. Every value is broker Current
    Value (col 7): a MARK, not delta/notional/Greeks. The margin DEBIT balance is
    NOT in this export (only positive money-market cash appears)."""
    cash, opt_legs = [], []
    cash_total = pending = opt_net = opt_gross = 0.0
    as_of = ""
    with open(path) as f:
        for r in csv.reader(f):
            if r and r[0].strip().startswith("Date downloaded"):
                as_of = r[0].strip()[len("Date downloaded"):].strip()
            # len<8 (NOT <14): the 'Pending activity' row is 14 cols; data rows 17.
            # Current Value is col 7, so 8 cols is the true minimum. Header,
            # disclaimer paragraphs and blank lines contain spaces -> fail regex.
            if len(r) < 8 or not re.match(r"^[A-Z0-9]{5,}$", r[0].strip()):
                continue
            acct, sym, val = r[0].strip(), r[2].strip(), fnum(r[7])
            if sym.endswith("**"):
                cash_total += val
                cash.append({"acct": acct, "sym": sym, "value": round(val, 2)})
            elif sym.startswith("-"):
                # side = sign of the MARK (col 7), NOT the '-' symbol prefix (which
                # is on ALL option rows). A long call also starts with '-'.
                opt_net += val; opt_gross += abs(val)
                opt_legs.append({"acct": acct, "sym": sym, "name": r[3].strip(),
                                 "qty": fnum(r[4]), "mark": round(val, 2),
                                 "side": "short" if val < 0 else "long",
                                 "type": (r[15].strip() if len(r) > 15 else "")})
            elif sym == "Pending activity":
                pending += val
    return {"cash": cash, "cashTotal": round(cash_total, 2),
            "pending": round(pending, 2), "optLegs": opt_legs,
            "optMarkNet": round(opt_net, 2), "optMarkGross": round(opt_gross, 2),
            "asOf": as_of}

MONTHS = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
          "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}

def parse_option_contract(leg):
    """Parse Fidelity option leg display fields into contract metadata.

    Returns None on uncertainty instead of guessing. Fidelity names currently
    look like "QQQ JUN 18 2026 $711 CALL".
    """
    name = (leg.get("name") or "").upper()
    m = re.search(r"\b([A-Z][A-Z0-9.]*)\s+([A-Z]{3})\s+(\d{1,2})\s+(\d{4})\s+\$([0-9.]+)\s+(CALL|PUT)\b", name)
    if not m:
        return None
    mon = MONTHS.get(m.group(2))
    if not mon:
        return None
    expiry = datetime.date(int(m.group(4)), mon, int(m.group(3))).isoformat()
    return {"underlying": m.group(1), "expiry": expiry,
            "right": "C" if m.group(6) == "CALL" else "P",
            "strike": float(m.group(5))}

def build_option_spreads(opt_legs, asof):
    enriched = []
    for idx, leg in enumerate(opt_legs or []):
        meta = parse_option_contract(leg)
        if not meta:
            continue
        qty = fnum(leg.get("qty", 0))
        mark = fnum(leg.get("mark", 0))
        contracts = abs(qty)
        mark_per_share = abs(mark) / (contracts * 100) if contracts else None
        enriched.append({**leg, **meta, "idx": idx, "qty": qty, "contracts": contracts,
                         "mark": mark, "markPerShare": _rn(mark_per_share, 2)})

    groups = {}
    for leg in enriched:
        key = (leg.get("acct"), leg["underlying"], leg["expiry"], leg["right"])
        groups.setdefault(key, []).append(leg)

    out = []
    for (acct, und, expiry, right), legs in groups.items():
        longs = sorted([x for x in legs if x["qty"] > 0], key=lambda x: x["strike"])
        shorts = sorted([x for x in legs if x["qty"] < 0], key=lambda x: x["strike"])
        used_l = set(); used_s = set()
        for li, lg in enumerate(longs):
            for si, sh in enumerate(shorts):
                if li in used_l or si in used_s:
                    continue
                contracts = min(lg["contracts"], sh["contracts"])
                if contracts <= 0:
                    continue
                width = abs(sh["strike"] - lg["strike"])
                width_value = width * contracts * 100
                net_mark = lg["mark"] + sh["mark"]
                if right == "C" and lg["strike"] < sh["strike"]:
                    kind = "bull call debit spread"; expected = [0, width_value]
                elif right == "C":
                    kind = "bear call credit spread"; expected = [-width_value, 0]
                elif right == "P" and lg["strike"] > sh["strike"]:
                    kind = "bear put debit spread"; expected = [0, width_value]
                else:
                    kind = "bull put credit spread"; expected = [-width_value, 0]
                dte = None
                try:
                    dte = (datetime.date.fromisoformat(expiry) - datetime.date.fromisoformat(asof)).days
                except Exception:
                    pass
                warn = []
                if net_mark < expected[0] - 1 or net_mark > expected[1] + 1:
                    warn.append("当前净 mark 超出垂直价差理论范围，请核对券商报价/腿配对")
                out.append({
                    "underlying": und, "acct": acct, "expiry": expiry, "right": right,
                    "longStrike": _rn(lg["strike"]), "shortStrike": _rn(sh["strike"]),
                    "contracts": _rn(contracts, 3), "kind": kind, "width": _rn(width),
                    "widthValue": _rn(width_value), "netMark": _rn(net_mark),
                    "expectedRange": [_rn(expected[0]), _rn(expected[1])], "dte": dte,
                    "longMark": _rn(lg["mark"]), "shortMark": _rn(sh["mark"]),
                    "warnings": warn,
                })
                used_l.add(li); used_s.add(si)
                break
    return out

# ---------------------------------------------------------------- classification
# fact-checked vs LIVE yfinance .info on 2026-06-02; this map's ONLY job is the
# documented GROUPING that collapses the correlated semis cluster (chips +
# 'Semiconductor Equipment & Materials' + the DRAM memory-ETF) into one 半导体
# super-theme. Every entry is a real Technology/Semiconductor* label, none
# invented. It never overrides a successful live fetch except to perform this
# collapse; unknown names flow to live .info or stay 未分类.
CURATED_THEME = {
    "NVDA": "半导体", "MU": "半导体", "TSM": "半导体", "MRVL": "半导体", "AMD": "半导体",
    "ARM": "半导体", "ASX": "半导体", "INTC": "半导体", "AVGO": "半导体", "KLAC": "半导体",
    "ASML": "半导体", "LRCX": "半导体", "TER": "半导体", "AMAT": "半导体", "ONTO": "半导体",
    "ENTG": "半导体", "COHR": "半导体", "CAMT": "半导体", "FORM": "半导体",
    "DRAM": "半导体",  # Roundhill Memory ETF — assetClass=主题ETF, theme via documented name aggregation
}

def asset_class(sym, name):
    """Asset-class bucket derived purely from sym + name (zero new data, never wrong)."""
    u = (name or "").upper()
    if sym.endswith("**"):
        return "现金"
    if sym in ("TQQQ", "SQQQ", "SPXL", "UPRO") or "ULTRAPRO" in u or "3X" in u:
        return "杠杆"
    if sym in ("VOO", "SPY", "SPMO", "QQQ", "IVV", "VTI") or "S&P 500" in u or "QQQ TR" in u:
        return "宽基指数ETF"
    if sym == "GLD" or "GOLD" in u:
        return "商品"
    if sym == "DRAM" or " ETF" in (" " + u) or sym.endswith("ETF"):
        return "主题ETF"
    return "个股"

def sector_to_theme(rec):
    """Map a REAL yfinance .info record to a Chinese theme — reads fetched fields
    verbatim, never guesses. Returns '未分类' when the record carries no usable
    sector (e.g. a 404/bad-ticker dict)."""
    ind = (rec.get("industry") or ""); sec = (rec.get("sector") or "")
    if "Semiconductor" in ind:
        return "半导体"
    if sec == "Communication Services":
        return "互联网/通信"
    if sec == "Financial Services":
        return "金融"
    if sec == "Consumer Cyclical":
        return "消费"
    if sec == "Utilities":
        return "公用事业"
    if sec == "Industrials":
        return "工业"
    if sec == "Technology":
        return "科技(非半导体)"
    if sec == "Healthcare":
        return "医疗"
    if sec == "Energy":
        return "能源"
    if sec == "Consumer Defensive":
        return "必需消费"
    if sec == "Real Estate":
        return "房地产"
    if sec == "Basic Materials":
        return "材料"
    if sec:
        return sec   # any other REAL sector kept verbatim — honest, never invented
    return "未分类"

# ---------------------------------------------------------------- merge
def merge_histories(parsed):
    """Merge trades across overlapping exports. Each real transaction appears
    once per export that covers its date; overlapping exports repeat it. So we
    take, per identical-transaction key, the MAX count seen in any single file
    (not the sum) — robust dedup that preserves genuine same-day duplicates.

    Amount is intentionally not part of the identity key: Fidelity sometimes
    changes commission/fee rounding by a cent between overlapping exports. In
    that case use the amount from the newest parsed export rather than double
    counting the fill.
    """
    from collections import Counter, defaultdict
    sc, oc, names = Counter(), Counter(), {}
    sa, oa = {}, {}
    for h in parsed:
        fsc, foc = Counter(), Counter()
        fsa, foa = defaultdict(list), defaultdict(list)
        for sym, L in h["txns"].items():
            names.setdefault(sym, h["names"].get(sym, sym))
            for t in L:
                k = (t.get("account", ""), t["date"], t["side"], sym,
                     round(t["qty"], 3), round(t["price"], 2))
                fsc[k] += 1
                fsa[k].append(round(t["amount"], 2))
        for sym, L in h["opt_txns"].items():
            for t in L:
                k = (t.get("account", ""), t["date"], t["side"], sym,
                     round(t["qty"], 3), round(t["price"], 2))
                foc[k] += 1
                foa[k].append(round(t["amount"], 2))
        for k, n in fsc.items():
            if n >= sc[k]:
                sc[k] = n
                sa[k] = fsa[k]
        for k, n in foc.items():
            if n >= oc[k]:
                oc[k] = n
                oa[k] = foa[k]
    txns, opt = defaultdict(list), defaultdict(list)
    for store, src, amts in ((txns, sc, sa), (opt, oc, oa)):
        for (acct, d, side, sym, qty, price), n in src.items():
            vals = amts.get((acct, d, side, sym, qty, price), [0.0])
            for i in range(n):
                amt = vals[i] if i < len(vals) else vals[-1]
                store[sym].append({"date": d, "side": side, "qty": qty,
                                   "price": price, "amount": amt, "account": acct})
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
# Module-level fetch status so the dashboard can warn the trader when prices are stale (correctness > silence)
FETCH_STATUS = {"ok": True, "stale": False, "reason": None, "cacheAgeDays": 0}


def _cache_age_days():
    if not os.path.exists(CACHE):
        return 0
    return max(0, int((datetime.datetime.now().timestamp() - os.path.getmtime(CACHE)) / 86400))

def _ensure_parent(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)


def fetch_prices(tickers, start, end, no_fetch=False):
    global FETCH_STATUS
    if no_fetch and os.path.exists(CACHE):
        print("· using cached prices (--no-fetch)")
        FETCH_STATUS = {"ok": True, "stale": True, "reason": "--no-fetch", "cacheAgeDays": _cache_age_days()}
        return json.load(open(CACHE))
    try:
        import warnings; warnings.filterwarnings("ignore")
        import yfinance as yf
    except ImportError:
        print("!! yfinance not installed (pip install yfinance). Falling back to cache.")
        FETCH_STATUS = {"ok": False, "stale": True, "reason": "yfinance 未安装", "cacheAgeDays": _cache_age_days()}
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
        FETCH_STATUS = {"ok": False, "stale": True, "reason": f"Yahoo 拉取失败：{type(e).__name__}", "cacheAgeDays": _cache_age_days()}
        return json.load(open(CACHE)) if os.path.exists(CACHE) else {}
    FETCH_STATUS = {"ok": True, "stale": False, "reason": None, "cacheAgeDays": 0}
    missing = [t for t in tickers if t not in out]
    print(f"· got {len(out)}/{len(tickers)} tickers" + (f"  missing: {missing}" if missing else ""))
    if os.path.exists(CACHE):
        try:
            old = json.load(open(CACHE))
            for sym, series in out.items():
                merged = old.get(sym, {})
                merged.update(series)
                old[sym] = merged
            out = old
        except Exception:
            pass
    _ensure_parent(CACHE)
    json.dump(out, open(CACHE, "w"))
    return out

def fetch_ohlc(tickers, start, end, no_fetch=False):
    """Fetch daily OHLC for strategy indicators that need High/Low (ATR).
    This is intentionally narrow and additive: the core portfolio valuation still
    uses fetch_prices' close series and remains covered by sync.py's broker gate.
    """
    if no_fetch and os.path.exists(OHLC_CACHE):
        print("· using cached OHLC (--no-fetch)")
        return json.load(open(OHLC_CACHE))
    if no_fetch:
        return json.load(open(OHLC_CACHE)) if os.path.exists(OHLC_CACHE) else {}
    try:
        import warnings; warnings.filterwarnings("ignore")
        import yfinance as yf
    except ImportError:
        print("!! yfinance not installed. OHLC strategy data from cache only.")
        return json.load(open(OHLC_CACHE)) if os.path.exists(OHLC_CACHE) else {}
    print(f"· fetching OHLC for {len(tickers)} strategy tickers {start}→{end} ...")
    out = {}
    try:
        data = yf.download(list(tickers), start=start, end=end, progress=False,
                           auto_adjust=True, group_by="ticker")
        multi = len(tickers) > 1
        for t in tickers:
            try:
                sub = data[t] if multi else data
                rows = {}
                for idx, r in sub.dropna(subset=["Open", "High", "Low", "Close"]).iterrows():
                    rows[idx.strftime("%Y-%m-%d")] = {
                        "open": round(float(r["Open"]), 4),
                        "high": round(float(r["High"]), 4),
                        "low": round(float(r["Low"]), 4),
                        "close": round(float(r["Close"]), 4),
                    }
                if rows:
                    out[t] = rows
            except Exception:
                pass
    except Exception as e:
        print(f"!! OHLC fetch failed ({e}); using cache if available")
        return json.load(open(OHLC_CACHE)) if os.path.exists(OHLC_CACHE) else {}
    if os.path.exists(OHLC_CACHE):
        try:
            old = json.load(open(OHLC_CACHE))
            for sym, rows in out.items():
                merged = old.get(sym, {})
                merged.update(rows)
                old[sym] = merged
            out = old
        except Exception:
            pass
    try:
        _ensure_parent(OHLC_CACHE)
        json.dump(out, open(OHLC_CACHE, "w"))
    except Exception:
        pass
    return out

def fetch_sectors(tickers, no_fetch=False, per_ticker_timeout=4.0, total_budget=25.0, stale_days=30):
    """Fetch sector/industry per ticker from yfinance, cached to SECTORS_CACHE.
    Mirrors fetch_prices' --no-fetch/cache/try-except contract AND is wall-clock
    bounded (get_info() has no timeout arg, so use ThreadPoolExecutor + result
    timeout + shutdown(wait=False)) so a flaky/rate-limited/hung .info can NEVER
    hang the run or break the sync gate. Purely additive metadata — touches no
    dollar figure. Only fetches names MISSING from cache and NOT in CURATED_THEME,
    so steady-state syncs make ~0 calls. Merge-on-write: a partial fetch never
    erases known-good labels. Always returns a dict (missing names just absent →
    callers fall back to '未分类')."""
    cache = json.load(open(SECTORS_CACHE)) if os.path.exists(SECTORS_CACHE) else {}
    if no_fetch:
        if cache:
            print("· using cached sectors (--no-fetch)")
        return cache
    try:
        import warnings; warnings.filterwarnings("ignore"); import yfinance as yf
    except ImportError:
        print("· yfinance missing; sectors from cache only")
        return cache
    import concurrent.futures as cf, time
    def fresh(e):
        try:
            return e and (datetime.date.today() - datetime.date.fromisoformat(e["fetchedAt"][:10])).days < stale_days
        except Exception:
            return False
    todo = [t for t in tickers if not t.startswith("^") and t not in CURATED_THEME and not fresh(cache.get(t))]
    if not todo:
        return cache
    print(f"· fetching sectors for {len(todo)} names ...")
    now = datetime.datetime.now().isoformat(timespec="seconds")
    deadline = time.time() + total_budget
    def one(t):
        i = yf.Ticker(t).get_info() or {}
        return {"sector": i.get("sector"), "industry": i.get("industry"),
                "quoteType": i.get("quoteType"), "fetchedAt": now}
    ex = cf.ThreadPoolExecutor(max_workers=4)
    try:
        for t in todo:
            if time.time() > deadline:
                print("· sector budget hit; rest stay cached/未分类"); break
            try:
                v = ex.submit(one, t).result(timeout=per_ticker_timeout)
                if v.get("sector") or v.get("quoteType"):   # skip 404/bad-ticker (both None)
                    cache[t] = v
            except cf.TimeoutError:
                pass
            except Exception:
                pass
    except Exception as e:
        print(f"· sector fetch error ({e}); using cache")
    finally:
        ex.shutdown(wait=False)
    got = sum(1 for t in tickers if t in cache)
    print(f"· sectors: {got}/{len(tickers)} cached")
    try:
        _ensure_parent(SECTORS_CACHE)
        json.dump(cache, open(SECTORS_CACHE, "w"))
    except Exception:
        pass
    return cache

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

def market_axis(prices, dmin, dmax):
    """Market-day axis shared by the real and counterfactual portfolio paths."""
    axis_src = prices.get("^GSPC") or prices.get("^IXIC") or {}
    axis = [d for d in sorted(axis_src) if dmin <= d <= dmax]
    if not axis:
        alldays = sorted({d for s, ps in prices.items() if not s.startswith("^") for d in ps})
        axis = [d for d in alldays if dmin <= d <= dmax]
    return axis

def latest_price_date(prices, tickers, through):
    """Latest market date available in the downloaded cache.

    Prefer benchmark trading days so the portfolio axis lands on a real market
    session; fall back to any held ticker if the benchmark download is missing.
    """
    for sym in ("^GSPC", "^IXIC", "QQQ"):
        ds = sorted(d for d in (prices.get(sym) or {}) if d <= through)
        if ds:
            return ds[-1]
    ds = sorted({d for sym in tickers for d in (prices.get(sym) or {}) if d <= through})
    return ds[-1] if ds else None

def mark_to_market(cur, prices, asof):
    """Revalue held equities from latest downloaded prices.

    Fidelity's Portfolio CSV remains the source of truth for shares and cost
    basis. This only updates stock price/value/unrealized fields, leaving cash
    and option marks as the broker snapshot because Yahoo close data is not a
    reliable option-chain valuation source.
    """
    refreshed = {}
    for sym, d in cur.items():
        px = price_on(prices, sym, asof)
        if not px or not d.get("shares"):
            continue
        shares = d["shares"]
        cost = d.get("cost", 0.0)
        value = shares * px
        gain = value - cost
        d["price"] = px
        d["value"] = value
        d["gain"] = gain
        d["gainpct"] = gain / cost * 100 if cost else 0.0
        d["avg"] = cost / shares if shares else 0.0
        refreshed[sym] = round(px, 4)
    return refreshed

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
    # Weighting: the three conditions are EQUALLY weighted as a hard AND gate — each
    # is a binary veto, none dominates. Rationale: this is a confirmation filter, not
    # a score. Trend supplies direction, the recent cross (≤3 bars) supplies timing,
    # RSI-not-extreme (<70 bull / >30 bear) vetoes chasing exhaustion. Any weaker
    # (weighted/soft) blend would re-admit exactly the false crosses this exists to
    # filter. No empirical per-signal weights have been fitted — by design.
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

# ---------------------------------------------------------------- QQQ/TQQQ decision map
def _ohlc_rows(prices, ohlc, sym, dmax):
    src = ohlc.get(sym) or {}
    rows = []
    if src:
        for d, r in sorted(src.items()):
            if d > dmax:
                continue
            try:
                rows.append({"date": d, "open": float(r["open"]), "high": float(r["high"]),
                             "low": float(r["low"]), "close": float(r["close"])})
            except Exception:
                pass
        if len(rows) >= 20:
            return rows, "OHLC"
    for d, c in sorted((prices.get(sym) or {}).items()):
        if d > dmax:
            continue
        try:
            c = float(c)
            rows.append({"date": d, "open": c, "high": c, "low": c, "close": c})
        except Exception:
            pass
    return rows, "close-proxy"

def _atr14(rows):
    out, trs, prev_close, prev_atr = [], [], None, None
    for r in rows:
        h, l, c = r["high"], r["low"], r["close"]
        tr = (h - l) if prev_close is None else max(h - l, abs(h - prev_close), abs(l - prev_close))
        trs.append(tr)
        if len(trs) < 14:
            atr = sum(trs) / len(trs)
        elif len(trs) == 14:
            atr = sum(trs[-14:]) / 14.0
        else:
            atr = (prev_atr * 13 + tr) / 14.0
        prev_atr, prev_close = atr, c
        out.append(atr)
    return out

def _pct_chg(vals, n):
    if len(vals) <= n or not vals[-n - 1]:
        return None
    return (vals[-1] / vals[-n - 1] - 1) * 100

def _slope_pct(vals, n=5):
    if len(vals) <= n or not vals[-n - 1]:
        return None
    return (vals[-1] / vals[-n - 1] - 1) * 100

def _rn(v, d=2):
    return None if v is None else round(v, d)

def _holding_snapshot(cur, sym):
    d = cur.get(sym, {})
    shares = d.get("shares", 0.0) or 0.0
    return {"sym": sym, "held": shares > 0.0001, "shares": round(shares, 3),
            "avg": _rn(d.get("avg", 0.0)), "value": _rn(d.get("value", 0.0)),
            "gain": _rn(d.get("gain", 0.0)), "gainPct": _rn(d.get("gainpct", 0.0))}

def _option_underlying(sym="", name=""):
    """Best-effort underlying extraction for Fidelity option symbols/names."""
    usym = (sym or "").strip().upper()
    m = re.match(r"^-?([A-Z][A-Z0-9.]*)\d{6}[CP][0-9.]+$", usym)
    if m:
        return m.group(1)
    uname = (name or "").upper()
    m = re.search(r"\b(?:CALL|PUT)\s+\(([A-Z][A-Z0-9.]*)\)", uname)
    if m:
        return m.group(1)
    meta = parse_option_contract({"name": name or ""})
    return meta.get("underlying") if meta else None

def _option_trade_summary(opt_txns, underlyings):
    out = []
    wanted = set(underlyings)
    for sym, L in sorted((opt_txns or {}).items()):
        und = _option_underlying(sym=sym)
        if und not in wanted:
            continue
        tl = sorted(L, key=lambda x: x["date"])
        net_cash = sum(t["amount"] for t in tl)
        net_qty = sum((t["qty"] if t["side"] == "BUY" else -t["qty"]) for t in tl)
        out.append({"underlying": und, "sym": sym, "lastDate": tl[-1]["date"] if tl else None,
                    "netCash": _rn(net_cash), "netQty": _rn(net_qty, 3),
                    "trades": len(tl),
                    "txns": [{"date": t["date"], "side": t["side"], "qty": t["qty"],
                              "price": t["price"], "amount": _rn(t["amount"])} for t in tl]})
    return out

def build_qqq_tqqq_strategy(prices, ohlc, cur, account, dmax, opt_txns=None):
    """QQQ/TQQQ decision map from rule-based state heuristics.

    SIGNAL WEIGHTING: all conditions below (EMA stack order, ATR-distance,
    5-day momentum, EMA proximity/slope) are IMPLICITLY EQUALLY WEIGHTED —
    they combine via plain boolean and/or, with no confidence scores or
    fitted per-signal weights. Rationale: transparent, auditable rules for a
    discretionary daily-review workflow beat an opaque empirically-tuned
    blend; each heuristic is independently explainable to the trader, and
    equal weighting means no signal can silently dominate after a data-regime
    change. If this were ever driven toward automated execution, explicit
    reliability tiers / confidence scores should be added first."""
    qrows, qsrc = _ohlc_rows(prices, ohlc, "QQQ", dmax)
    trows, tsrc = _ohlc_rows(prices, ohlc, "TQQQ", dmax)
    if len(qrows) < 55:
        return {"available": False, "reason": "QQQ price history is shorter than 55 trading days."}

    dates = [r["date"] for r in qrows]
    closes = [r["close"] for r in qrows]
    ema = {n: _ema(closes, n) for n in (8, 13, 21, 34, 55, 89)}
    atr = _atr14(qrows)
    rsi = _rsi(closes)
    i = len(closes) - 1
    c, a = closes[i], atr[i] or 0.0
    e8, e13, e21, e34, e55, e89 = (ema[n][i] for n in (8, 13, 21, 34, 55, 89))
    q5 = _pct_chg(closes, 5)
    tcloses = [r["close"] for r in trows]
    t_close = tcloses[-1] if tcloses else None
    t5 = _pct_chg(tcloses, 5) if tcloses else None
    e21s, e34s = _slope_pct(ema[21], 5), _slope_pct(ema[34], 5)
    dist8, dist21 = c - e8, c - e21
    dist8_atr = dist8 / a if a else None
    dist21_atr = dist21 / a if a else None
    band = (max(e8, e13, e21, e34) - min(e8, e13, e21, e34)) / c * 100 if c else 0.0

    stacked = c > e21 and e8 > e13 > e21 and (e21s or 0) > 0
    overheat = stacked and ((dist8_atr is not None and dist8_atr > 1.5) or
                            (q5 is not None and q5 > 3.0) or (t5 is not None and t5 > 9.0))
    near8 = bool(stacked and a and abs(c - e8) <= 0.5 * a)
    near21 = bool(a and abs(c - e21) <= 0.5 * a and (e21s or 0) > 0)
    two_below_21 = len(closes) >= 2 and closes[-1] < ema[21][-1] and closes[-2] < ema[21][-2]
    broken = two_below_21 or e8 < e21 or (c < e21 and (e21s or 0) <= 0)
    chop = (not stacked and not broken and band < 1.2)

    if broken:
        state = {"code": "break", "label": "趋势破坏/防守", "tone": "neg",
                 "action": "停止加 TQQQ / Call；等待重新站回 EMA21 或重新评估 regime。"}
    elif near21:
        state = {"code": "ema21", "label": "EMA21 期权观察区", "tone": "watch",
                 "action": "趋势未坏时，才评估 14-30DTE call debit spread 或小仓 TQQQ 弹性仓。"}
    elif near8:
        state = {"code": "ema8", "label": "EMA8 浅回踩", "tone": "pos",
                 "action": "可按计划拿回一部分 QQQ/TQQQ；期权不必急。"}
    elif overheat:
        state = {"code": "overheat", "label": "短线过热", "tone": "watch",
                 "action": "高位不追；上移移动止盈，可用小仓 TQQQ 上方 call credit spread 做刹车。"}
    elif stacked:
        state = {"code": "bull", "label": "多头趋势", "tone": "pos",
                 "action": "核心多头可以继续由趋势管理；等待 EMA8/EMA21 回踩再做新动作。"}
    elif chop:
        state = {"code": "chop", "label": "均线纠缠", "tone": "watch",
                 "action": "降低交易频率；EMA8/21 信号容易来回打脸。"}
    else:
        state = {"code": "mixed", "label": "转换中", "tone": "watch",
                 "action": "仓位先按风险管理，等 EMA8/13/21 重新排队或破位确认。"}

    def zone(mid, half):
        return [_rn(mid - half), _rn(mid + half)]

    tqqq_ccs = None
    if t_close:
        p3, p6 = t_close * 1.03, t_close * 1.06
        width = 1.0
        credit_scenarios = []
        for cr in (0.25, 0.35):
            credit_scenarios.append({
                "credit": cr, "maxProfit": cr * 100,
                "maxLoss": (width - cr) * 100,
                "breakeven": _rn(91 + cr),
            })
        tqqq_ccs = {
            "spot": _rn(t_close), "shortRange": [_rn(p3), _rn(p6)],
            "longRange": [_rn(p3 + 1), _rn(p6 + 3)],
            "qqqApproxPct": [1.0, 2.0], "dte": "7-21 DTE", "width": "$1-$3",
            "example9122": {"short": 91, "long": 92,
                            "shortPct": _rn((91 / t_close - 1) * 100, 1),
                            "longPct": _rn((92 / t_close - 1) * 100, 1),
                            "qqqApproxPct": _rn(((91 / t_close - 1) * 100) / 3, 1),
                            "width": width, "creditScenarios": credit_scenarios}
        }

    opt_focus = []
    tqqq_option_legs = []
    qqq_option_legs = []
    for leg in (account or {}).get("optLegs", []):
        txt = ((leg.get("sym") or "") + " " + (leg.get("name") or "")).upper()
        if "QQQ" in txt or "TQQQ" in txt:
            rec = {k: leg.get(k) for k in ("acct", "sym", "name", "qty", "mark", "side", "type")}
            rec["underlying"] = _option_underlying(leg.get("sym"), leg.get("name"))
            opt_focus.append(rec)
            if rec["underlying"] == "TQQQ":
                tqqq_option_legs.append(rec)
            elif rec["underlying"] == "QQQ":
                qqq_option_legs.append(rec)
    spread_focus = [s for s in (account or {}).get("optionSpreads", [])
                    if s.get("underlying") in ("QQQ", "TQQQ")]

    def day_state(j):
        cj, aj = closes[j], atr[j] or 0.0
        if ema[8][j] < ema[21][j] or cj < ema[21][j]:
            return "below21"
        if aj and cj - ema[8][j] > 1.5 * aj:
            return "overheat"
        if aj and abs(cj - ema[21][j]) <= 0.5 * aj:
            return "ema21"
        if aj and abs(cj - ema[8][j]) <= 0.5 * aj and ema[8][j] > ema[21][j]:
            return "ema8"
        if ema[8][j] > ema[13][j] > ema[21][j]:
            return "trend"
        return "mixed"

    series = []
    for j, d in enumerate(dates):
        series.append({"date": d, "close": _rn(closes[j]), "ema8": _rn(ema[8][j]),
                       "ema13": _rn(ema[13][j]), "ema21": _rn(ema[21][j]),
                       "ema34": _rn(ema[34][j]), "ema55": _rn(ema[55][j]),
                       "atr": _rn(atr[j]), "rsi": _rn(rsi[j], 1), "state": day_state(j)})

    rules = [
        {"key": "bull", "title": "多头趋势但未到买点", "active": state["code"] == "bull",
         "trigger": "Close > EMA21，EMA8 > EMA13 > EMA21，EMA21 仍向上",
         "decision": "保留核心多头；不追 TQQQ；等待 EMA8/EMA21 或过热信号"},
        {"key": "overheat", "title": "QQQ 远离 EMA8", "active": overheat,
         "trigger": "Close - EMA8 > 1.5 x ATR14，或 QQQ 5日 >3%，或 TQQQ 5日 >9%",
         "decision": "移动止盈；不追高；可小仓卖 TQQQ 上方 CCS"},
        {"key": "ema8", "title": "QQQ 回踩 EMA8", "active": near8,
         "trigger": "Close 在 EMA8 上下 0.5 x ATR14 内，且 EMA8 仍在 EMA21 上方",
         "decision": "拿回一部分 QQQ/TQQQ，仓位轻于核心仓"},
        {"key": "ema21", "title": "QQQ 回踩 EMA21", "active": near21,
         "trigger": "Close 在 EMA21 上下 0.5 x ATR14 内，EMA21 仍向上，未连续两日收破",
         "decision": "才评估期权多头，优先 call debit spread 而非高位裸追"},
        {"key": "break", "title": "跌破 EMA21 并反抽失败", "active": broken,
         "trigger": "连续两日收在 EMA21 下方，或 EMA8 下穿 EMA21",
         "decision": "不加 TQQQ，不买 call；先保护本金"},
        {"key": "chop", "title": "EMA8/13/21/34 缠绕", "active": chop,
         "trigger": "均线带宽 < 1.2%，且没有清晰多头/空头排列",
         "decision": "降低交易频率，避免被来回洗"},
    ]

    hqqq, htqqq = _holding_snapshot(cur, "QQQ"), _holding_snapshot(cur, "TQQQ")
    long_value = (hqqq.get("value") or 0.0) + (htqqq.get("value") or 0.0)
    has_long = bool(hqqq.get("held") or htqqq.get("held"))
    known_cash = (account or {}).get("cashTotal", 0.0) or 0.0
    tqqq_cash_contracts = math.floor(known_cash / (t_close * 100)) if t_close else 0
    tqqq_covered_contracts = math.floor((htqqq.get("shares") or 0.0) / 100)
    opt_hist_focus = _option_trade_summary(opt_txns or {}, ("QQQ", "TQQQ"))
    tqqq_hist_focus = [x for x in opt_hist_focus if x["underlying"] == "TQQQ"]
    qqq_hist_focus = [x for x in opt_hist_focus if x["underlying"] == "QQQ"]
    q_high20 = max(closes[-20:]) if len(closes) >= 20 else max(closes)
    q_trail_3atr = q_high20 - 3 * a if a else None
    t_atr = _atr14(trows) if len(trows) >= 2 else []
    t_atr_last = t_atr[-1] if t_atr else None
    t_high20 = max(tcloses[-20:]) if tcloses else None
    trailing = {
        "qqqHigh20": _rn(q_high20), "qqqTrail3Atr": _rn(q_trail_3atr),
        "qqqTrailGapPct": _rn((q_trail_3atr / c - 1) * 100, 2) if q_trail_3atr and c else None,
        "tqqqHigh20": _rn(t_high20), "tqqqTrail8Pct": _rn(t_high20 * 0.92) if t_high20 else None,
        "tqqqTrail12Pct": _rn(t_high20 * 0.88) if t_high20 else None,
        "tqqqAtr14": _rn(t_atr_last),
    }
    next_triggers = [
        {"name": "EMA8 拿回", "level": _rn(e8), "zone": zone(e8, 0.5 * a),
         "distancePct": _rn((e8 / c - 1) * 100, 2),
         "action": "浅回踩才考虑拿回小仓 QQQ/TQQQ"},
        {"name": "EMA21 期权", "level": _rn(e21), "zone": zone(e21, 0.5 * a),
         "distancePct": _rn((e21 / c - 1) * 100, 2),
         "action": "趋势未坏时才评估 14-30DTE call debit spread"},
        {"name": "过热阈值", "level": _rn(e8 + 1.5 * a), "zone": None,
         "distancePct": _rn(((e8 + 1.5 * a) / c - 1) * 100, 2),
         "action": "触发后上移移动止盈；只用小仓 CCS 刹车"},
        {"name": "趋势破坏", "level": _rn(e21), "zone": None,
         "distancePct": _rn((e21 / c - 1) * 100, 2),
         "action": "连续收破 EMA21 或 EMA8<EMA21 时停止进攻"},
    ]
    panel_map = {
        "bull": ("等待，不追", "核心多头继续趋势管理；新 TQQQ/期权等 EMA8/EMA21。",
                 "不要因为多头趋势就高位加 TQQQ；不要常态化卖 CCS 拖累白天鹅。"),
        "overheat": ("先防守浮盈", "上移移动止盈；战术仓可分批止盈；已有多头时才考虑小仓上方 CCS。",
                     "不要追高；不要让 CCS 从 hedge 变成账户主风险。"),
        "ema8": ("小仓拿回", "可小仓拿回 QQQ/TQQQ；仍不必急着重仓期权。",
                 "不要把浅回踩当 EMA21 深回踩；仓位轻于核心。"),
        "ema21": ("期权观察", "若 EMA21 守住且主线未破，可评估 call debit spread 或小仓 TQQQ。",
                  "不要用 0DTE/远 OTM 裸 call 代替有风险定义的结构。"),
        "break": ("停止进攻", "退出/降低战术仓，保护本金，等重新站回 EMA21。",
                  "不要无脑抄底 TQQQ；不要把趋势破坏当普通回调。"),
        "chop": ("降频小仓", "均线纠缠时减少交易频率，等方向重新排队。",
                 "不要用 EMA8/21 反复加减仓被 whipsaw。"),
        "mixed": ("等确认", "仓位按风险管理，等趋势重排或破位确认。",
                  "不要在转换区把任何单一信号看成确定性。"),
    }
    p = panel_map.get(state["code"], panel_map["mixed"])
    decision_panel = {
        "headline": p[0], "doNow": p[1], "avoid": p[2],
        "ccsRole": ("已有 QQQ/TQQQ 多头，CCS 只能小仓做 hedge；当前 QQQ/TQQQ 多头约 "
                    + f"${long_value:,.0f}") if has_long else "没有 QQQ/TQQQ 多头；上方 CCS 是偏空交易，不是 hedge。",
        "movingStop": "QQQ 用 3xATR 保护天气图；TQQQ 战术仓用 8%-12% trailing stop。",
    }
    teacher_checks = [
        {"label": "QQQ 天气", "value": state["label"], "ok": state["code"] not in ("break", "chop")},
        {"label": "多头可对冲", "value": ("有 QQQ/TQQQ 多头" if has_long else "没有 QQQ/TQQQ 多头"), "ok": has_long},
        {"label": "CCS 风险", "value": "最大亏损 = 宽度 - credit；需实盘确认 delta/credit", "ok": True},
        {"label": "移动止盈", "value": "先写 QQQ 3ATR / TQQQ 8%-12% 线", "ok": True},
        {"label": "白天鹅", "value": "核心多头不为小 credit 封顶", "ok": True},
    ]
    def option_status(kind):
        code = state["code"]
        if kind == "debit":
            if code == "ema21":
                return ("WATCH", "EMA21 守住且未连续收破时，才评估 14-30DTE call debit spread。")
            if code in ("break", "chop"):
                return ("BLOCK", "趋势破坏或均线纠缠时，不加 TQQQ/call 方向性风险。")
            return ("WATCH", "等 QQQ 回到 EMA21 期权区；高位不急着买期权。")
        if kind == "ccs":
            if not has_long:
                return ("BLOCK", "没有 QQQ/TQQQ 多头，上方 CCS 不是 hedge。")
            if code == "overheat":
                return ("WATCH", "仅小仓、定义风险，用作多头短线刹车；先确认实盘 delta/credit。")
            return ("WATCH", "等 QQQ 过热或冲高停顿；不要常态化卖 CCS 拖累白天鹅。")
        if kind == "covered":
            if tqqq_covered_contracts > 0:
                return ("WATCH", f"最多约 {tqqq_covered_contracts} 组 covered call；只有愿意交割 TQQQ 才做。")
            return ("BLOCK", f"TQQQ 仅 {htqqq.get('shares', 0)} 股，不足 100 股，不能叫 covered call。")
        if kind == "csp":
            if tqqq_cash_contracts > 0:
                return ("WATCH", f"已知现金粗略可覆盖 {tqqq_cash_contracts} 组，但必须愿意接 TQQQ。")
            need = (t_close or 0) * 100
            return ("BLOCK", f"已知现金 ${known_cash:,.0f}，不足覆盖 1 张 TQQQ put 约 ${need:,.0f}。")
        return ("WATCH", "先确认结构、流动性、最大亏损和退出线。")

    tqqq_option_plan = {
        "status": ("当前导出没有 TQQQ 期权合约；最近 TQQQ 记录是股票/ETF买入。"
                   if not tqqq_option_legs and not tqqq_hist_focus
                   else "发现 TQQQ 期权记录；先按当前腿和成交净额核对。"),
        "currentTqqqLegs": tqqq_option_legs,
        "currentQqqLegs": qqq_option_legs,
        "recentTqqqOrders": tqqq_hist_focus,
        "recentQqqOrders": qqq_hist_focus,
        "cashKnown": _rn(known_cash),
        "coveredContracts": tqqq_covered_contracts,
        "cashSecuredPutContracts": tqqq_cash_contracts,
        "structures": [
            {"name": "TQQQ call debit spread", "role": "方向性战术",
             "status": option_status("debit")[0], "rule": option_status("debit")[1],
             "risk": "付出的 debit 为最大亏损；优先 14-30 DTE、近 ATM long call + 更高 strike short call。"},
            {"name": "TQQQ 上方 CCS", "role": "多头刹车/hedge",
             "status": option_status("ccs")[0], "rule": option_status("ccs")[1],
             "risk": "最大亏损 = 宽度 - credit；小仓，50%-70% credit captured 可收。"},
            {"name": "TQQQ covered call", "role": "愿意卖出持仓才用",
             "status": option_status("covered")[0], "rule": option_status("covered")[1],
             "risk": "会封顶 TQQQ 上行；不是愿意交割就不要用。"},
            {"name": "TQQQ cash-secured put", "role": "愿意接货才用",
             "status": option_status("csp")[0], "rule": option_status("csp")[1],
             "risk": "必须有真实现金接 assignment；不要用 margin 假装 CSP。"},
        ],
    }

    return {"available": True, "asOf": dates[-1], "source": {"qqq": qsrc, "tqqq": tsrc},
            "state": state, "decisionPanel": decision_panel,
            "teacherChecks": teacher_checks, "nextTriggers": next_triggers,
            "trailing": trailing,
            "latest": {"date": dates[-1], "qqq": _rn(c), "tqqq": _rn(t_close),
                       "ema8": _rn(e8), "ema13": _rn(e13), "ema21": _rn(e21),
                       "ema34": _rn(e34), "ema55": _rn(e55), "ema89": _rn(e89),
                       "atr14": _rn(a), "rsi14": _rn(rsi[-1], 1),
                       "distEma8": {"dollar": _rn(dist8), "pct": _rn(dist8 / e8 * 100, 2), "atr": _rn(dist8_atr, 2)},
                       "distEma21": {"dollar": _rn(dist21), "pct": _rn(dist21 / e21 * 100, 2), "atr": _rn(dist21_atr, 2)},
                       "qqqRet5": _rn(q5, 2), "tqqqRet5": _rn(t5, 2),
                       "ema21Slope5": _rn(e21s, 2), "ema34Slope5": _rn(e34s, 2),
                       "bandWidthPct": _rn(band, 2), "twoBelowEma21": two_below_21},
            "zones": {"ema8Buyback": zone(e8, 0.5 * a), "ema21Options": zone(e21, 0.5 * a),
                      "ema34Defense": zone(e34, 0.25 * a), "ema55Regime": zone(e55, 0.25 * a)},
            "rules": rules, "series": series[-130:],
            "holdings": {"QQQ": hqqq, "TQQQ": htqqq},
            "tqqqCcs": tqqq_ccs, "optionLegs": opt_focus,
            "optionSpreads": spread_focus, "optionHistory": opt_hist_focus,
            "tqqqOptions": tqqq_option_plan}

# ---------------------------------------------------------------- behavioral
# Concentration warning thresholds (weights in % of equity market value), used by
# the behavioral "concentration" detector below. Named so the review cadence they
# assume (weekly/monthly portfolio review — see CODE_REVIEW_2026-07-03) is explicit
# and tunable in ONE place instead of buried in a comparison chain. Same pattern as
# MOM_SCALE: constant + rationale so docs and code can't silently drift apart.
CONC_TOP1_ALERT = 25   # single-name weight > 25% → alert (one thesis dominates drawdown risk)
CONC_TOP1_WATCH = 18   # single-name weight > 18% → watch
CONC_TOP5_ALERT = 70   # top-5 combined weight > 70% → alert (little diversification left)
CONC_TOP5_WATCH = 55   # top-5 combined weight > 55% → watch

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
    lvl = ("alert" if (top[1] > CONC_TOP1_ALERT or top5 > CONC_TOP5_ALERT)
           else ("watch" if (top[1] > CONC_TOP1_WATCH or top5 > CONC_TOP5_WATCH) else "good"))
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
    # Per-symbol bias map for the 决策一览 scorecard, so the JS join doesn't have to
    # re-parse Chinese example strings. Only GENUINELY per-name biases (their
    # examples are symbol-prefixed) and only at level alert/watch — account-level
    # flags (overtrading) have no per-name examples and are excluded. When the data
    # is clean a name simply gets no chip (honest; never manufactured).
    PER_NAME_IDS = {"disposition", "sunkcost", "recency", "anchoring"}   # concentration is account-level — don't paint broad-index ETFs (VOO/QQQ) with a per-name 集中 chip
    biasBySym = {}
    for f in flags:
        if f["id"] not in PER_NAME_IDS or f["level"] not in ("alert", "watch"):
            continue
        seen = set()
        for ex in f.get("examples", []):
            tok = (ex or "").strip().split(" ")[0].strip()
            if not tok or tok in seen:
                continue
            seen.add(tok)
            biasBySym.setdefault(tok, []).append({"id": f["id"], "level": f["level"]})
    stats = {"sells": nsells, "buys": nbuys, "trades": ntrades,
             "winSells": len(win_sells), "losSells": len(los_sells),
             "heldWin": len(held_win), "heldLos": len(held_los),
             "pgr": round(pgr, 3), "plr": round(plr, 3), "dispositionRatio": round(ratio, 2),
             "turnoverAnnPct": round(turn_ann * 100, 0), "topWeight": round(top[1], 1),
             "top5Weight": round(top5, 1), "hhi": round(hhi, 3)}
    return {"flags": flags, "stats": stats, "biasBySym": biasBySym}

# ---------------------------------------------------------------- money-weighted return
def _xnpv(rate, flows):
    d0 = flows[0][0]
    return sum(a / (1 + rate) ** ((d - d0).days / 365.0) for d, a in flows)

def _xnpv_deriv(rate, flows):
    d0 = flows[0][0]
    s = 0.0
    for d, a in flows:
        yf = (d - d0).days / 365.0
        s += -yf * a / (1 + rate) ** (yf + 1)
    return s

def xirr(flows):
    """Money-weighted (annualized) IRR for dated cash flows [(date, amount)],
    sign convention: outflow negative, inflow positive. Newton with a bisection
    fallback. Returns None when the IRR is undefined — <2 flows, span <14d, no
    sign change, or no convergence — so the caller renders an em dash rather than
    a fabricated number."""
    flows = sorted(flows, key=lambda x: x[0])
    if len(flows) < 2:
        return None
    if (flows[-1][0] - flows[0][0]).days < 14:
        return None
    amts = [a for _, a in flows if abs(a) > 1e-9]
    if not amts or min(amts) >= 0 or max(amts) <= 0:
        return None
    r = 0.10
    for _ in range(100):
        f, d = _xnpv(r, flows), _xnpv_deriv(r, flows)
        if not (math.isfinite(f) and math.isfinite(d)) or abs(d) < 1e-10:
            break
        r2 = r - f / d
        if r2 <= -0.9999:
            r2 = (r - 0.9999) / 2
        if abs(r2 - r) < 1e-7:
            return r2 if (-0.9999 < r2 < 10 and abs(_xnpv(r2, flows)) < 1e-4) else None
        r = r2
    lo, hi = -0.9999, 10.0
    flo, fhi = _xnpv(lo, flows), _xnpv(hi, flows)
    if not (math.isfinite(flo) and math.isfinite(fhi)) or flo * fhi > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        fm = _xnpv(mid, flows)
        if abs(fm) < 1e-6 or (hi - lo) < 1e-8:
            return mid
        if flo * fm < 0:
            hi = mid
        else:
            lo, flo = mid, fm
    return (lo + hi) / 2

# ---------------------------------------------------------------- counterfactual rebalancing
def flatten_stock_trades(txns):
    """Exact stock-trade ledger for replay features."""
    out = []
    for sym in sorted(txns):
        rows = sorted(txns.get(sym, []), key=lambda t: (
            t.get("date", ""), t.get("account", ""), t.get("side", ""),
            round(t.get("qty", 0.0), 8), round(t.get("amount", 0.0), 2)
        ))
        for i, t in enumerate(rows):
            if t.get("side") not in ("BUY", "SELL") or (t.get("qty") or 0) <= 0:
                continue
            qty = float(t.get("qty") or 0.0)
            amount = float(t.get("amount") or 0.0)
            price = float(t.get("price") or 0.0)
            out.append({
                "id": f"{t['date']}|{sym}|{i}|{t['side']}|{round(qty, 6)}|{round(amount, 2)}",
                "date": t["date"], "sym": sym, "side": t["side"],
                "qty": qty, "price": price, "amount": amount,
                "account": t.get("account", ""),
            })
    return sorted(out, key=lambda t: (t["date"], t["sym"], t["side"], t["id"]))

def _opening_shares(txns, cur):
    syms = sorted(set(txns) | set(cur))
    out = {}
    for sym in syms:
        net = sum((t["qty"] if t["side"] == "BUY" else -t["qty"])
                  for t in txns.get(sym, []) if t.get("side") in ("BUY", "SELL"))
        out[sym] = float(cur.get(sym, {}).get("shares", 0.0)) - net
    return out

def _shares_at_end(txns, cur, d):
    syms = sorted(set(txns) | set(cur))
    out = {}
    for sym in syms:
        final = float(cur.get(sym, {}).get("shares", 0.0))
        chg_after = sum((t["qty"] if t["side"] == "BUY" else -t["qty"])
                        for t in txns.get(sym, [])
                        if t.get("side") in ("BUY", "SELL") and t["date"] > d)
        out[sym] = final - chg_after
    return out

def detect_rebalance_events(trades, base_value=0.0, max_events=None):
    """Group single-day trade clusters that look like rebalancing stages."""
    if not trades:
        return []
    by_date = {}
    for tr in sorted(trades, key=lambda t: (t["date"], t["sym"], t["side"], t["id"])):
        by_date.setdefault(tr["date"], []).append(tr)

    min_gross = max(500.0, min((base_value or 0.0) * 0.003, 2500.0))
    events = []
    for n, (day, g) in enumerate(sorted(by_date.items())):
        syms = sorted({t["sym"] for t in g})
        sides = {t["side"] for t in g}
        gross = sum(abs(t["amount"]) for t in g)
        if gross < min_gross or not ({"BUY", "SELL"} <= sides or len(syms) >= 3):
            continue
        by_sym = {}
        for t in g:
            by_sym[t["sym"]] = by_sym.get(t["sym"], 0.0) + abs(t["amount"])
        top = [s for s, _ in sorted(by_sym.items(), key=lambda kv: -kv[1])[:4]]
        side_label = "换仓" if {"BUY", "SELL"} <= sides else ("加仓" if "BUY" in sides else "减仓")
        events.append({
            "id": f"cf{n + 1}", "start": day, "end": day,
            "label": f"{day} · {side_label} · " + "/".join(top),
            "gross": round(gross, 2), "netCash": round(sum(t["amount"] for t in g), 2),
            "symbols": top, "legIds": [t["id"] for t in g],
            "legs": [{
                "id": t["id"], "date": t["date"], "sym": t["sym"], "side": t["side"],
                "qty": round(t["qty"], 4), "price": round(t["price"], 4),
                "amount": round(t["amount"], 2)
            } for t in g],
        })
    events = sorted(events, key=lambda e: (e["start"], e["gross"]), reverse=True)
    return events[:max_events] if max_events else events

def _cf_score_from_impact(impact_pct):
    if impact_pct is None or not math.isfinite(impact_pct):
        return None
    return round(max(0.0, min(100.0, 50.0 + 50.0 * math.tanh(impact_pct / 6.0))), 1)

def _cf_grade(score):
    if score is None:
        return "—"
    if score >= 85:
        return "A"
    if score >= 70:
        return "B"
    if score >= 55:
        return "C"
    if score >= 40:
        return "D"
    return "F"

def _cf_outcome(score):
    if score is None:
        return "不可评分"
    if score >= 70:
        return "明显加分"
    if score >= 55:
        return "小幅加分"
    if score > 45:
        return "接近中性"
    if score > 30:
        return "小幅扣分"
    return "明显扣分"

def add_counterfactual_scores(replays):
    """Attach per-event scores and an aggregate scorecard.

    Score is based on today's comparable real-vs-skip delta divided by that
    stage's gross turnover. 50 is neutral; positive real-minus-skip deltas push
    the score up, negative deltas push it down.
    """
    valid = []
    for ev in replays:
        s = ev.get("summary") or {}
        gross = float(ev.get("gross") or 0.0)
        delta = s.get("currentDelta")
        impact = (float(delta) / gross * 100.0) if gross > 0 and delta is not None else None
        score = _cf_score_from_impact(impact)
        ev["score"] = {
            "score": score,
            "grade": _cf_grade(score),
            "outcome": _cf_outcome(score),
            "impactOnTurnoverPct": round(impact, 3) if impact is not None else None,
            "asOf": s.get("lastValidDate"),
            "confidence": "truncated" if s.get("isTruncated") else "latest",
        }
        if score is not None:
            valid.append(ev)

    method = "50 + 50*tanh(((real - skipped) / turnover * 100) / 6)"

    def empty(scope="latest"):
        return {
            "count": len(replays), "scoredCount": 0, "allScoredCount": 0,
            "completeCount": 0, "truncatedCount": 0, "scope": scope,
            "winCount": 0, "lossCount": 0, "winRate": None, "score": None, "grade": "—",
            "totalGross": 0.0, "totalDelta": 0.0, "impactOnTurnoverPct": None,
            "avgActualPct": None, "avgAltPct": None, "avgSpreadPct": None,
            "scoreMethod": method,
        }

    def wavg(items, key):
        vals = []
        for e in items:
            v = (e.get("summary") or {}).get(key)
            g = float(e.get("gross") or 0.0)
            if v is not None and g > 0:
                vals.append((float(v), g))
        den = sum(g for _, g in vals)
        return round(sum(v * g for v, g in vals) / den, 3) if den else None

    def ref(e):
        s = e.get("summary") or {}
        sc = e.get("score") or {}
        return {
            "id": e.get("id"), "label": e.get("label"), "date": e.get("start"),
            "score": sc.get("score"), "grade": sc.get("grade"),
            "delta": s.get("currentDelta"), "deltaPct": s.get("currentDeltaPct"),
        }

    def summarize(items, scope):
        if not items:
            return empty(scope)
        total_gross = sum(float(e.get("gross") or 0.0) for e in items)
        total_delta = sum(float((e.get("summary") or {}).get("currentDelta") or 0.0) for e in items)
        impact = (total_delta / total_gross * 100.0) if total_gross else None
        score = _cf_score_from_impact(impact)
        win_count = sum(1 for e in items if ((e.get("summary") or {}).get("currentDelta") or 0.0) > 0)
        loss_count = sum(1 for e in items if ((e.get("summary") or {}).get("currentDelta") or 0.0) < 0)
        avg_actual = wavg(items, "actualPct")
        avg_alt = wavg(items, "altPct")
        best = max(items, key=lambda e: (e.get("summary") or {}).get("currentDelta") or 0.0)
        worst = min(items, key=lambda e: (e.get("summary") or {}).get("currentDelta") or 0.0)
        return {
            "count": len(replays), "scoredCount": len(items), "scope": scope,
            "winCount": win_count, "lossCount": loss_count,
            "winRate": round(win_count / len(items) * 100.0, 1),
            "score": score, "grade": _cf_grade(score), "outcome": _cf_outcome(score),
            "totalGross": round(total_gross, 2), "totalDelta": round(total_delta, 2),
            "impactOnTurnoverPct": round(impact, 3) if impact is not None else None,
            "avgActualPct": avg_actual, "avgAltPct": avg_alt,
            "avgSpreadPct": round(avg_actual - avg_alt, 3) if avg_actual is not None and avg_alt is not None else None,
            "best": ref(best), "worst": ref(worst),
            "scoreMethod": method,
        }

    if not valid:
        return empty("latest")

    complete = [e for e in valid if not (e.get("summary") or {}).get("isTruncated")]
    all_summary = summarize(valid, "comparable")
    primary = summarize(complete if complete else valid, "latest" if complete else "comparable")
    primary.update({
        "allScoredCount": len(valid),
        "completeCount": len(complete),
        "truncatedCount": len(valid) - len(complete),
        "allComparable": all_summary,
    })
    return primary

def simulate_counterfactual_event(txns, cur, prices, axis, event):
    """Replay actual vs. 'skip this rebalancing stage' from a common fork date."""
    if not axis or not event:
        return None
    trades = flatten_stock_trades(txns)
    if not trades:
        return None
    first_idx = next((i for i, d in enumerate(axis) if d >= event["start"]), None)
    if first_idx is None:
        return None
    if first_idx == 0:
        return None
    start_idx = first_idx - 1
    branch_date = axis[start_idx]
    base_shares = _shares_at_end(txns, cur, branch_date)
    include_trade = lambda t: t["date"] > branch_date

    todo = [t for t in trades if include_trade(t)]
    skip_ids = set(event.get("legIds") or [])
    actual, alt = dict(base_shares), dict(base_shares)
    actual_cash = alt_cash = 0.0
    i = 0
    rows, warnings = [], []
    alt_valid = True

    def apply_trade(state, cash, t, strict=False):
        signed = t["qty"] if t["side"] == "BUY" else -t["qty"]
        before = state.get(t["sym"], 0.0)
        after = before + signed
        if strict and after < -1e-6:
            return state, cash, {
                "date": t["date"], "sym": t["sym"], "side": t["side"],
                "qty": round(t["qty"], 4), "reason": "后续真实卖出依赖被跳过的买入",
            }
        state[t["sym"]] = 0.0 if abs(after) < 1e-9 else after
        return state, cash + t["amount"], None

    def book_value(state, cash, d):
        total = cash
        missing = []
        for sym, qty in state.items():
            if abs(qty) <= 1e-9:
                continue
            p = price_on(prices, sym, d)
            if p is None:
                missing.append(sym); continue
            total += qty * p
        return total, missing

    base = None
    start_written = False
    for d in axis[start_idx:]:
        if not start_written:
            av, amiss = book_value(actual, actual_cash, d)
            cv, cmiss = book_value(alt, alt_cash, d)
            if av <= 0 and cv <= 0:
                return None
            base = av if av > 0 else cv
            if amiss or cmiss:
                warnings.append({"date": d, "reason": "部分标的缺少价格，使用可得价格重估", "symbols": sorted(set(amiss + cmiss))[:5]})
            rows.append({
                "date": d, "actual": round(av, 2), "actualPct": round((av / base - 1) * 100, 3),
                "alt": round(cv, 2), "altPct": round((cv / base - 1) * 100, 3),
                "delta": round(av - cv, 2), "deltaPct": round((av - cv) / base * 100, 3),
            })
            start_written = True
            if d == branch_date:
                continue
        while i < len(todo) and todo[i]["date"] <= d:
            t = todo[i]
            actual, actual_cash, _ = apply_trade(actual, actual_cash, t)
            if alt_valid and t["id"] not in skip_ids:
                alt, alt_cash, warn = apply_trade(alt, alt_cash, t, strict=True)
                if warn:
                    warnings.append(warn); alt_valid = False
            i += 1
        if not alt_valid:
            break
        av, amiss = book_value(actual, actual_cash, d)
        cv, cmiss = book_value(alt, alt_cash, d)
        if amiss or cmiss:
            warnings.append({"date": d, "reason": "部分标的缺少价格，使用可得价格重估", "symbols": sorted(set(amiss + cmiss))[:5]})
        row = {"date": d, "actual": round(av, 2), "actualPct": round((av / base - 1) * 100, 3)}
        row.update({
            "alt": round(cv, 2), "altPct": round((cv / base - 1) * 100, 3),
            "delta": round(av - cv, 2), "deltaPct": round((av - cv) / base * 100, 3),
        })
        rows.append(row)

    valid = [r for r in rows if r.get("alt") is not None]
    if len(valid) < 2:
        return None
    last = valid[-1]
    best = max(valid, key=lambda r: r["delta"])
    worst = min(valid, key=lambda r: r["delta"])
    out = dict(event)
    out.update({
        "branchDate": branch_date, "series": rows,
        "summary": {
            "currentDelta": last["delta"], "currentDeltaPct": last["deltaPct"],
            "actualPct": last["actualPct"], "altPct": last["altPct"],
            "bestDelta": best["delta"], "bestDate": best["date"],
            "worstDelta": worst["delta"], "worstDate": worst["date"],
            "lastValidDate": last["date"], "isTruncated": len(rows) < len(axis[start_idx:]),
            "days": len(valid),
        },
        "warnings": warnings[:6],
    })
    return out

def build_counterfactual_replays(txns, cur, prices, dmin, dmax, base_value=0.0, max_events=None):
    axis = market_axis(prices, dmin, dmax)
    trades = flatten_stock_trades(txns)
    events = detect_rebalance_events(trades, base_value=base_value, max_events=max_events)
    replays = []
    for ev in events:
        sim = simulate_counterfactual_event(txns, cur, prices, axis, ev)
        if sim:
            replays.append(sim)
    aggregate = add_counterfactual_scores(replays)
    return {"events": replays, "aggregate": aggregate, "mode": "skip-stage-then-real-trades"}

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
def classify(stocks, sectors):
    """Attach assetClass / theme / themeSrc to each stock. Priority: curated semis
    grouping → asset-class for ETFs/cash/etc → REAL fetched .info sector → 未分类.
    No label is ever invented."""
    for s in stocks:
        sym = s["sym"]
        s["assetClass"] = asset_class(sym, s.get("name"))
        if sym in CURATED_THEME:
            s["theme"], s["themeSrc"] = CURATED_THEME[sym], "人工标注"
        elif s["assetClass"] in ("宽基指数ETF", "杠杆", "商品", "主题ETF"):
            s["theme"], s["themeSrc"] = s["assetClass"], "资产类别"
        elif sym in sectors and sector_to_theme(sectors[sym]) != "未分类":
            s["theme"], s["themeSrc"] = sector_to_theme(sectors[sym]), "Yahoo行业"
        else:
            s["theme"], s["themeSrc"] = "未分类", "未分类"

def build_alloc(stocks, summary, risk, account, sectors):
    """Concentration X-ray: weight & risk-contribution aggregated BY asset-class and
    BY theme, plus theme-level HHI / effective-N (the diversification-illusion number).
    Additive only — adds no dollar figure to summary, so the sync gate is unaffected.
    Theme weights use the marketValue basis to reconcile with risk.contrib's 100%."""
    try:
        held = [s for s in stocks if s.get("held")]
        mv = summary.get("marketValue") or sum(s["value"] for s in held) or 1.0
        rc = {c["sym"]: c["riskPct"] for c in ((risk or {}).get("contrib") or [])}
        # by asset class (equity buckets + additive cash row)
        ac = {}
        for s in held:
            ac.setdefault(s["assetClass"], 0.0)
            ac[s["assetClass"]] += s["value"]
        cash = (account or {}).get("cashTotal") or 0.0
        acBase = mv + cash   # asset-class weights are % of (equity + cash) so the card sums to 100%; theme weights stay on the mv basis to reconcile with risk.contrib
        byAC = [{"bucket": k, "value": round(v, 2), "weightPct": round(v / acBase * 100, 1)} for k, v in ac.items()]
        if cash:
            byAC.append({"bucket": "现金", "value": round(cash, 2), "weightPct": round(cash / acBase * 100, 1), "isCash": True})
        byAC.sort(key=lambda x: -x["value"])
        # by theme (equity only)
        th = {}
        for s in held:
            t = th.setdefault(s["theme"], {"value": 0.0, "risk": 0.0, "hasRisk": False, "members": [], "src": s["themeSrc"]})
            t["value"] += s["value"]; t["members"].append(s["sym"])
            if s["sym"] in rc:
                t["risk"] += rc[s["sym"]]; t["hasRisk"] = True
        byTheme = []
        for k, v in th.items():
            w = round(v["value"] / mv * 100, 1)
            rp = round(v["risk"], 1) if v["hasRisk"] else None
            byTheme.append({"theme": k, "weightPct": w, "value": round(v["value"], 2), "n": len(v["members"]),
                            "members": sorted(v["members"]), "src": v["src"], "riskPct": rp,
                            "gap": (round(rp - w, 1) if rp is not None else None)})
        byTheme.sort(key=lambda x: -x["weightPct"])
        # theme-level concentration: HHI + effective-N (dollar and, if complete, risk)
        def hhi(ws):
            return sum((w / 100.0) ** 2 for w in ws) if ws else 0.0
        hW = hhi([t["weightPct"] for t in byTheme])
        all_risk = bool(byTheme) and all(t["riskPct"] is not None for t in byTheme)
        hR = hhi([t["riskPct"] for t in byTheme]) if all_risk else None
        hN = hhi([s["value"] / mv * 100 for s in held])
        conc = {"nominalN": len(held),
                "hhiWeight": round(hW, 3), "effNWeight": round(1 / hW, 1) if hW else None,
                "hhiRisk": (round(hR, 3) if hR else None), "effNRisk": (round(1 / hR, 1) if hR else None),
                "nameHhi": round(hN, 3), "nameEffN": round(1 / hN, 1) if hN else None}
        srcs = {}
        for s in held:
            srcs[s["themeSrc"]] = srcs.get(s["themeSrc"], 0) + 1
        prov = {"asOf": (max((sectors.get(s["sym"], {}).get("fetchedAt", "") for s in held), default="") or "—"),
                "unclassified": srcs.get("未分类", 0), "sources": srcs}
        return {"byAssetClass": byAC, "byTheme": byTheme, "largestTheme": (byTheme[0] if byTheme else None),
                "conc": conc, "provenance": prov}
    except Exception:
        return {"byAssetClass": [], "byTheme": [], "largestTheme": None, "conc": None,
                "provenance": {"asOf": "—", "unclassified": 0, "sources": {}}}

def build_payload(txns, opt_txns, names, cur, prices, deposits, totals, dmin, dmax,
                  dividends=0.0, life_deposits=0.0, account=None, sectors=None,
                  ohlc=None, price_mode="broker", price_as_of=None,
                  refreshed_prices=None):
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
        sym_window = sorted((d, p) for (d, p) in prices.get(sym, {}).items() if dmin <= d <= dmax)
        # 'today's mover': use the last two trading-day closes in the window so the sort matches the trader's morning question
        day_pct = None
        day_dollar = None
        if len(sym_window) >= 2 and sym_window[-2][1]:
            lp, pp = sym_window[-1][1], sym_window[-2][1]
            day_pct = round((lp - pp) / pp * 100.0, 2)
            if held:
                day_dollar = round((lp - pp) * final, 2)
        stocks.append({
            "sym": sym, "name": names.get(sym, sym), "held": held,
            "shares": round(final, 3), "avg": round(c.get("avg", 0), 2),
            "curPrice": round(c.get("price", 0) or (price_on(prices, sym, dmax) or 0), 2),
            "value": round(c.get("value", 0), 2), "unreal": round(c.get("gain", 0), 2),
            "unrealPct": round(c.get("gainpct", 0), 2), "cost": round(c.get("cost", 0), 2),
            "realized": round(realized, 2), "hasLegacy": has_legacy,
            "numTrades": len(tl), "prices": sorted(prices.get(sym, {}).items()),
            "txns": rows, "fib": compute_fib(sorted(prices.get(sym, {}).items())),
            "dayChangePct": day_pct, "dayChangeDollar": day_dollar})

    # per-stock momentum keyed by date (for the portfolio-level overlay)
    mom_map = {}
    for s in stocks:
        if s["fib"]:
            mom_map[s["sym"]] = {d: m for (d, _), m in zip(s["prices"], s["fib"]["mom"])}

    # PAYLOAD DIET: the per-stock EMA/momentum/RSI/state ribbons are pure functions of
    # s["prices"] and are recomputed in the browser (fibArrays() in the JS template
    # mirrors compute_fib's math exactly). Stripping them cuts ~40% off the embedded
    # JSON across 64 stocks. Event lists (signals/resonance) and the "now" snapshot
    # stay — they're small and remain the single source for headline numbers.
    # NOTE: mom_map above must be built BEFORE this strip; portfolio_fib (one series)
    # is intentionally NOT stripped.
    for s in stocks:
        if s["fib"]:
            s["fib"] = {k: s["fib"][k] for k in ("signals", "resonance", "now")}

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
    axis = market_axis(prices, dmin, dmax)

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
               "netWorthStart": series[0]["value"] if series else 0,
               "priceMode": price_mode, "priceAsOf": price_as_of or dmax,
               "refreshedPriceCount": len(refreshed_prices or {}),
               "fetchOK": FETCH_STATUS.get("ok", True),
               "fetchStale": FETCH_STATUS.get("stale", False),
               "fetchReason": FETCH_STATUS.get("reason"),
               "cacheAgeDays": FETCH_STATUS.get("cacheAgeDays", 0),
               "generatedAt": datetime.datetime.now().isoformat(timespec="seconds")}

    # ---- whole-account: fold cash + option mark-to-market into net worth ----
    # Strictly additive: NEW summary keys only. The 股票 bucket = held_val (the
    # same value feeding marketValue), so it stays broker-verified to the cent.
    acct_block = None
    if account:
        eq = round(held_val, 2)
        whole = round(eq + account["cashTotal"] + account["optMarkNet"] + account["pending"], 2)
        acct_block = {**account, "equity": eq, "netWorthWhole": whole,
                      "optPctEquity": (round(account["optMarkGross"] / held_val * 100, 2) if held_val else None),
                      "asOf": account.get("asOf") or "券商最新快照"}
        acct_block["optionSpreads"] = build_option_spreads(acct_block.get("optLegs", []), dmax)
        summary["accountNetWorth"] = whole
        summary["cashTotal"] = account["cashTotal"]
        summary["pendingTotal"] = account["pending"]
        summary["optMarkNet"] = account["optMarkNet"]      # MTM net — distinct from summary.optNet (history cash-flow)
        summary["optMarkGross"] = account["optMarkGross"]
        summary["optPctEquity"] = acct_block["optPctEquity"]
        summary["marginDebitKnown"] = False
        assert abs(whole - (eq + summary["cashTotal"] + summary["optMarkNet"] + summary["pendingTotal"])) < 0.005

    # ---- money-weighted return (equity-book XIRR) + plain-dollar bridge ----
    # Honest metric: equity-book IRR (cash & margin balances over time aren't
    # tracked, so an account-level IRR would be a fudge factor). Flows = −V0 at
    # dmin + each in-window trade's signed amount + terminal equity value.
    V0 = series[0]["value"] if series else 0.0       # equity market value at window start
    terminal = held_val
    bridge = None
    summary["mwrAnnual"] = summary["mwrPeriod"] = summary["behaviorGap"] = None
    if series:
        # XIRR flows (equity book): −V0 at dmin + each in-window trade's signed
        # amount (dmin trades already inside V0) + dividends + terminal value.
        term_date = datetime.date.fromisoformat(dmax) + datetime.timedelta(days=2)
        flows = [(datetime.date.fromisoformat(dmin), -V0)]
        by_date = {}
        for sym, tl in txns.items():
            for t in tl:
                if t["date"] > dmin:
                    by_date[t["date"]] = by_date.get(t["date"], 0.0) + t["amount"]
        for dd in sorted(by_date):
            flows.append((datetime.date.fromisoformat(dd), by_date[dd]))
        if dividends:
            flows.append((term_date, dividends))
        term_val = terminal if terminal > 0 else series[-1]["value"]
        if term_val > 0:
            flows.append((term_date, term_val))
        rate = xirr(flows)
        span_days = (term_date - datetime.date.fromisoformat(dmin)).days
        if rate is not None:
            summary["mwrPeriod"] = round(((1 + rate) ** (span_days / 365.0) - 1) * 100, 2)
            if span_days >= 60:                            # don't annualize a sub-2-month IRR
                summary["mwrAnnual"] = round(rate * 100, 2)
            summary["behaviorGap"] = round(summary["curReturn"] - summary["mwrPeriod"], 2)
        # Plain-dollar bridge. EXACT, no fudge: 持仓成本 + 未实现 = 当前市值 (broker
        # identity). The four P&L buckets are the user's mental accounts; realized/
        # dividends/options are cash already taken, so they're shown beside (not
        # summed into) the holdings value. Anchors on COST, never market-value V0,
        # to avoid double-counting pre-window embedded gains.
        held_cost = sum(s["cost"] for s in stocks if s["held"])
        bridge = {"heldCost": round(held_cost, 2), "terminal": round(terminal, 2),
                  "totalPL": round(held_unreal + total_realized + dividends + opt_net, 2),
                  "lifeDeposits": round(life_deposits, 2), "ok": rate is not None,
                  "legs": [
                      {"key": "unreal", "label": "未实现盈亏（持仓 vs 成本）", "amount": round(held_unreal, 2), "type": "pnl", "acc": "券商精确"},
                      {"key": "real", "label": "已实现盈亏（窗口）", "amount": round(total_realized, 2), "type": "pnl", "acc": "含估算均价"},
                      {"key": "div", "label": "股息", "amount": round(dividends, 2), "type": "pnl"},
                      {"key": "opt", "label": "期权净现金流", "amount": round(opt_net, 2), "type": "pnl"},
                  ]}

    behavior = analyze_behavior(stocks, summary, prices, dmin, dmax)
    risk = compute_risk(series, stocks)
    classify(stocks, sectors or {})
    alloc = build_alloc(stocks, summary, risk, acct_block, sectors or {})
    qqq_tqqq = build_qqq_tqqq_strategy(prices, ohlc or {}, cur, acct_block, dmax, opt_txns)
    counterfactual = build_counterfactual_replays(txns, cur, prices, dmin, dmax, held_val)
    return {"summary": summary, "stocks": stocks, "options": opts, "series": series,
            "portfolioFib": portfolio_fib, "behavior": behavior, "risk": risk,
            "bridge": bridge, "account": acct_block, "alloc": alloc, "qqqTqqq": qqq_tqqq,
            "counterfactual": counterfactual}

# ---------------------------------------------------------------- HTML
def load_decision_analysis(out_dir):
    """Optional, data-driven 'decision analysis' layer for the dashboard.

    Reads ``output/decision_analysis.json`` (next to the dashboard) if present and
    returns the parsed dict, else None. This is the reusable framework hook: drop a
    JSON file matching the schema (asOf / title / thesis / cash / candidates[] /
    noList[] / rules[] / recommendation / method / disclaimer) and the 决策分析 tab
    renders it; absent or malformed → the tab shows a how-to placeholder instead of
    breaking. See ``scripts/decision_analysis.py`` for the producer + schema docs.
    """
    path = os.path.join(out_dir, "decision_analysis.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (ValueError, OSError) as e:
        print(f"  (skipped decision_analysis.json: {e})")
        return None


def load_ai_semi_quant(out_dir):
    """Optional AI semiconductor framework layer.

    Reads ``output/ai_semi_quant.json`` if present. The producer is
    ``scripts/ai_semi_quant.py``; this loader is deliberately tolerant so a
    malformed research artifact never breaks the core brokerage dashboard.
    """
    path = os.path.join(out_dir, "ai_semi_quant.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (ValueError, OSError) as e:
        print(f"  (skipped ai_semi_quant.json: {e})")
        return None


def load_ai_watchlist(out_dir):
    """Optional broader AI old-capability watchlist layer."""
    path = os.path.join(out_dir, "ai_watchlist.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (ValueError, OSError) as e:
        print(f"  (skipped ai_watchlist.json: {e})")
        return None


def load_aics_payload(out_dir):
    """Optional AICS layer for the dedicated AI semiconductor tool tab.

    Reads ``output/aics.json`` if present. The producer is
    ``scripts/aics_tool.py``. Like the other research-artifact loaders, this is
    tolerant: a malformed AICS file should never break the brokerage dashboard.
    """
    path = os.path.join(out_dir, "aics.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (ValueError, OSError) as e:
        print(f"  (skipped aics.json: {e})")
        return None


def load_market_mass_dashboard(out_dir):
    """Optional market-mass boundary visualization layer.

    Reads ``output/market_mass_dashboard.json`` if present. The producer is
    ``scripts/market_mass_dashboard.py``. This loader is intentionally tolerant
    so malformed research output never breaks the core brokerage dashboard.
    """
    path = os.path.join(out_dir, "market_mass_dashboard.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (ValueError, OSError) as e:
        print(f"  (skipped market_mass_dashboard.json: {e})")
        return None


def load_financial_status(out_dir):
    """Optional FMP-backed company financial-status lens.

    Reads ``output/financial_status.json`` if present. The producer is
    ``scripts/financial_status_score.py``. This is deliberately optional and
    tolerant because API-plan limits should not break the brokerage dashboard.
    """
    path = os.path.join(out_dir, "financial_status.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (ValueError, OSError) as e:
        print(f"  (skipped financial_status.json: {e})")
        return None


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
<meta name="color-scheme" content="dark"/>
<!-- self-contained on purpose: no external fonts/requests at view time.
     The font stacks below fall straight to system faces (SF/Segoe + PingFang SC). -->
<style>
/* faint engraved-paper grain + one off-axis graphite bloom; both fixed, non-animated.
   grain rides ABOVE the opaque panels (z:30 — below the z:40 tooltip, above the z:20 header)
   so the fractalNoise actually textures cards/charts; the bloom stays behind everything.
   bloom anchor: min() keeps today's placement on ≤~1400px viewports and pins it to the
   1480px sheet's right shoulder on ultrawide (plain calc() would regress mid-width). */
body::before{content:"";position:fixed;inset:0;z-index:-1;pointer-events:none;
 background:radial-gradient(1400px 720px at min(85%, calc(50% + 450px)) -60px,#1C2026 0%,rgba(6,6,8,0) 60%),radial-gradient(1200px 600px at 10% 110%,#101216 0%,rgba(6,6,8,0) 60%);}
body::after{content:"";position:fixed;inset:0;z-index:30;pointer-events:none;opacity:.15;
 background-image:var(--grain);}
</style>
<style>
/* embedded faces — Latin/digits/symbols subsets only (pyftsubset, zero+tnum kept, SIL OFL);
   unicode-range gates them so CJK keeps falling through to Noto Sans SC / PingFang SC.
   Plex Mono ships 400/500/600 (+ ± ≈ ≤ ≥) so designed weights and math glyphs never fall back. */
@font-face{font-family:"Space Grotesk";font-style:normal;font-weight:600;font-display:swap;src:url(data:font/woff2;base64,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) format("woff2");unicode-range:U+0020-007E,U+00B7,U+00D7,U+2013-2014,U+2026,U+2190-2193,U+2212,U+25B2,U+25BC;}
@font-face{font-family:"IBM Plex Mono";font-style:normal;font-weight:400;font-display:swap;src:url(data:font/woff2;base64,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) format("woff2");unicode-range:U+0020-007E,U+00B1,U+00B7,U+00D7,U+2013-2014,U+2026,U+2190-2193,U+2212,U+2248,U+2264-2265,U+25B2,U+25BC;}
@font-face{font-family:"IBM Plex Mono";font-style:normal;font-weight:500;font-display:swap;src:url(data:font/woff2;base64,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) format("woff2");unicode-range:U+0020-007E,U+00B1,U+00B7,U+00D7,U+2013-2014,U+2026,U+2190-2193,U+2212,U+2248,U+2264-2265,U+25B2,U+25BC;}
@font-face{font-family:"IBM Plex Mono";font-style:normal;font-weight:600;font-display:swap;src:url(data:font/woff2;base64,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) format("woff2");unicode-range:U+0020-007E,U+00B1,U+00B7,U+00D7,U+2013-2014,U+2026,U+2190-2193,U+2212,U+2248,U+2264-2265,U+25B2,U+25BC;}
</style>
<style>
/* ============================================================
   GRAPHITE ATELIER — Swiss-Minimal Portfolio Terminal
   Display: Space Grotesk · UI/Latin: Archivo · Numbers: IBM Plex Mono
   CJK falls naturally to Noto Sans SC / PingFang SC
   One rationed amber accent · semantic green/red on numbers only
   ============================================================ */
:root{
  color-scheme:dark;   /* native controls (selects, scrollbars) render dark; no white flash */
  /* ============================== DESIGN TOKENS ============================== */
  /* ---- spacing scale (4 / 8 / 12 / 16 / 20 / 24 / 32 / 44 — every multiple of 4) ---- */
  --s1:4px; --s2:8px; --s3:12px; --s4:16px; --s5:20px; --s6:24px; --s7:32px;
  --tap-min:44px;                   /* iOS minimum tap target — used by mobile @media */
  /* ---- type scale (kept tight: 5 sizes cover the whole UI) ---- */
  --t-xs:11px; --t-sm:12.5px; --t-base:13px; --t-md:15px; --t-lg:17px; --t-xl:20px;
  --t-num:22px; --t-num-hero:26px;   /* numeral display sizes (KPI tier) */
  --t-2xs:10px;                       /* sub-label tier: state dots, fold markers, axis captions */
  --ls-label:.07em; --ls-tight:-.01em; --ls-wide:.14em;   /* one spacing for uppercase micro-labels; one for display/numeral titles (masthead .11em is the lone exception) */
  /* ---- masthead height (single source of truth for sticky offsets) ---- */
  --header-h:84px;
  /* ---- ticks (rhythm markers) ---- */
  --tick-s:14px; --tick-l:17px;     /* small = card title; large = masthead */
  /* ---- palette ---- */
  --bg:#060608; --bg2:#0A0C10; --panel:#121316; --panel2:#1B1E24;   /* ladder widened one perceptible notch: bg < bg2 < panel < panel2 */
  --line:#23262C; --hair:#1A1C21;
  --txt:#ECEDEF; --mut:#888D96; --faint:#7E848E;   /* faint bumped #5C616B→#7E848E for WCAG-AA on group labels / gl underline */
  --accent:#E8B339;            /* the ONE chromatic accent: live line / active / "you are here" */
  --accent-ref:#B89030;        /* muted amber = reference (cost basis) — distinct from live */
  --accent-soft:rgba(232,179,57,0.12);
  --accent-line:rgba(232,179,57,0.32);
  --green:#4FB286; --red:#E5707A;       /* semantic P&L sign / risk direction ONLY */
  --amber-line:var(--accent);           /* solid amber for the warn/attention verdict tier (hero banner, journal chip, sync dot) */
  --mut2:#B6BAC1;                        /* secondary grey: benchmark (Nasdaq) line + legend — not P&L, not the amber accent */
  --chip-bd-green:#4FB28666; --chip-bd-red:#E5707A66; --chip-bd-amber:#E8B33966;  /* one tinted-chip border convention */
  --grid:#1A1C21; --axis:#888D96;   /* axis raised from #6B7079 for WCAG-AA at 11px on panel */
  /* legacy aliases kept so any stray var() refs still resolve */
  --panel-2:var(--panel2); --blue:var(--accent); --purple:var(--accent); --orange:var(--accent);
  /* ---- type ---- */
  /* Latin-first stacks: CJK still resolves per-character through the tail; putting the
     Latin faces ahead of Noto/PingFang stops Latin glyphs rendering in PingFang's generic
     Latin — a visible display/body pairing exists even before any embedded face loads. */
  --f-disp:"Space Grotesk","Avenir Next","Noto Sans SC","PingFang SC",ui-sans-serif,system-ui,sans-serif;
  --f-ui:"Archivo",-apple-system,"Segoe UI",Roboto,"Noto Sans SC","PingFang SC",sans-serif;
  --f-mono:"IBM Plex Mono","SF Mono",ui-monospace,"Roboto Mono",monospace;
  /* ---- atmosphere (consumed by body::after in the first style block) ---- */
  --grain:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='160' height='160'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='2' stitchTiles='stitch'/%3E%3CfeComponentTransfer%3E%3CfeFuncA type='gamma' exponent='2.2'/%3E%3C/feComponentTransfer%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E");   /* gamma shapes alpha contrast: paper tooth, not a brightness lift */
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
  font-size:var(--t-base); line-height:1.5;
  font-feature-settings:"tnum" 1,"zero" 1;
  -webkit-font-smoothing:antialiased; text-rendering:optimizeLegibility;
}
/* every numeral is tabular & slashed-zero */
.kpi .v,.row .pnl,.row .meta,.badge .v,td,th,.fval,.tt,.legacychip,.chip,.sub{
  font-variant-numeric:tabular-nums slashed-zero;
}
::-webkit-scrollbar{width:8px;height:8px}
::-webkit-scrollbar-thumb{background:var(--line);border-radius:8px}
::-webkit-scrollbar-thumb:hover{background:color-mix(in srgb,var(--mut) 35%,var(--panel))}
::-webkit-scrollbar-track{background:transparent}
/* Firefox only — unscoped scrollbar-color would make Chromium 121+ drop the custom webkit bars above */
@supports (-moz-appearance:none){html{scrollbar-width:thin;scrollbar-color:var(--line) transparent}}
::selection{background:rgba(232,179,57,.25);color:var(--txt)}

/* page width cap so editorial margins don't sprawl on ultrawide */
header,.kpis,.wrap,#insight,#fetchwarn{max-width:1480px;margin-left:auto;margin-right:auto}

/* ============================== MASTHEAD ============================== */
header{
  padding:var(--s4) var(--s7) calc(var(--s4) + 6px);   /* clearance for the docked 8px barometer */
  display:flex; align-items:center; gap:var(--s4); flex-wrap:wrap;
  position:sticky; top:0; z-index:20;
  background:color-mix(in srgb,var(--bg) 80%,transparent);
  -webkit-backdrop-filter:blur(14px) saturate(120%);
  backdrop-filter:blur(14px) saturate(120%);
  border-bottom:1px solid var(--line);
  box-shadow:inset 0 1px 0 rgba(255,255,255,.045);
}
header h1{
  font-family:var(--f-disp); font-size:var(--t-lg); font-weight:600;
  letter-spacing:.01em; margin:0; line-height:1.1;
  display:flex; align-items:baseline; gap:var(--s3);
}
/* the display face's permanent stage: uppercase Latin shoulder line (embedded subset covers it) */
header h1 .h1-en{font-family:var(--f-disp); font-size:var(--t-2xs); font-weight:600; letter-spacing:var(--ls-wide,.14em); text-transform:uppercase; color:var(--faint); white-space:nowrap}
header h1 #qbar-name{font-family:var(--f-mono); font-size:var(--t-2xs); font-weight:600; letter-spacing:.04em; white-space:nowrap}
/* THE SIGNATURE — the Regime Barometer: full-bleed QQQ 60-day weather band on the masthead's
   bottom edge, present on every tab at every scroll position; 2px amber tick = today.
   Absolutely positioned: adds no height, so the measured --header-h stays untouched. */
#qbar{position:absolute; left:0; right:0; bottom:0; height:8px; display:flex; overflow:hidden; cursor:pointer}
#qbar:empty{display:none}
#qbar:focus-visible{outline:2px solid var(--accent); outline-offset:-2px}
#qbar:hover .qbar-now{box-shadow:0 0 10px var(--accent)}
#qbar svg{flex:1; height:100%}
#qbar .qbar-now{flex:none; width:2px; background:var(--accent); box-shadow:0 0 6px var(--accent-line)}
/* amber index-tick masthead glyph: largest of the tick family (--tick-l) — the masthead is the one always-live anchor */
header h1::before{
  content:""; width:3px; height:var(--tick-l); border-radius:1px;
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
  margin:var(--s5) var(--s7) var(--s1);
  background:var(--panel);
  border:1px solid var(--line);
  border-radius:var(--r-card);
  overflow:hidden;
}
.kpi{
  position:relative;
  background:none; border:0; border-radius:0; box-shadow:none;
  padding:var(--s4) var(--s5);
  border-left:1px solid var(--line);
  border-top:1px solid var(--line);
  transition:background-color .18s var(--ease);
}
/* first column + first row shed their inner rules so the strip reads as one panel */
.kpi:nth-child(4n+1){border-left:0}
.kpi:nth-child(-n+4){border-top:0}
.kpi:hover{background:var(--panel2)}
.kpi .l{
  font-family:var(--f-ui); font-size:var(--t-xs); font-weight:600;
  text-transform:uppercase; letter-spacing:.085em;
  color:var(--mut); margin-bottom:8px;
  transition:color .18s var(--ease);
  white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
}
.kpi:hover .l{color:var(--txt)}
.kpi .v{
  font-family:var(--f-mono); font-size:var(--t-num); font-weight:500;
  line-height:1.0; letter-spacing:var(--ls-tight); color:var(--txt);
}
/* the flagship semantic moment: signed hero numbers fire green/red (markup already emits .v.pos/.v.neg;
   without these the 0-2-0 rule above silently beat the global .pos/.neg) */
.kpi .v.pos{color:var(--green)} .kpi .v.neg{color:var(--red)}
/* tier-2 ledger: full-width compact strip under the four decision KPIs — visible (audit truth)
   but visually demoted so it stops competing with "did I make money" */
.kpi-ledger{
  grid-column:1/-1; display:flex; flex-wrap:wrap; gap:4px 24px;
  padding:9px var(--s5) 10px; border-top:1px solid var(--line); background:var(--bg2);
}
.kl{display:inline-flex; align-items:baseline; gap:7px; white-space:nowrap}
.kl-l{font-family:var(--f-ui); font-size:var(--t-xs); font-weight:600; text-transform:uppercase; letter-spacing:var(--ls-label); color:var(--mut)}
.kl-v{font-family:var(--f-mono); font-size:var(--t-base); color:var(--txt); font-variant-numeric:tabular-nums slashed-zero}
.kl-v.pos{color:var(--green)} .kl-v.neg{color:var(--red)}
/* HERO: Market Value is the first cell — larger value + amber underline-tick under the label */
.kpi:first-child .v{font-size:var(--t-num-hero)}
.kpi:first-child .l{position:relative; padding-bottom:7px; display:inline-block}
.kpi:first-child .l::after{
  content:""; position:absolute; left:0; bottom:0;
  width:26px; height:2px; background:var(--accent); border-radius:1px;
  transform:scaleX(0); transform-origin:left; transition:transform .2s var(--ease);
}
body.ready .kpi:first-child .l::after{
  transform:scaleX(1);   /* resting state lives here; backwards-fill covers the delay — hover can now transform */
  animation:tickIn .55s var(--ease) .35s backwards;
}
body.ready .kpi:first-child:hover .l::after{transform:scaleX(1.6)}

/* ============================== TWO-COLUMN SHELL ============================== */
.wrap{
  display:flex; gap:20px;
  padding:16px var(--s7) 44px;   /* one ruled left edge at x=32 down the whole band stack (header/.kpis/#insight share it) */
  align-items:flex-start;
}
.left{
  width:300px; flex:none;
  background:var(--panel);
  border:1px solid var(--line);
  border-radius:var(--r-card);
  overflow:hidden;
  position:sticky; top:calc(var(--header-h) - 8px);   /* tucks 8px under the z:20 header so the stack reads fused, not slit */
  box-shadow:var(--sh-lift);
}
.right{flex:1; min-width:0}
.viewbar{
  display:flex; align-items:center; justify-content:space-between; gap:12px;
  min-height:42px; margin-bottom:12px; padding:8px 2px;
}
.trail{display:flex; align-items:center; gap:8px; min-width:0; color:var(--mut)}
.trail .here{font-family:var(--f-disp); font-weight:600; color:var(--txt); white-space:nowrap}
.trail .here-sym{font-family:var(--f-mono); letter-spacing:.01em}   /* ticker = data voice */
.trail .nm-co{font-size:var(--t-2xs); letter-spacing:var(--ls-label); text-transform:uppercase; color:var(--faint)}   /* broker string becomes the designed caps line */
.trail .note{overflow:hidden; text-overflow:ellipsis; white-space:nowrap}
.view-actions{display:flex; align-items:center; justify-content:flex-end; gap:7px; flex-wrap:wrap}
.microbtn{
  display:inline-flex; align-items:center; justify-content:center; gap:5px;
  min-height:30px; padding:5px 10px;
  border:1px solid var(--line); border-radius:var(--r-ctl);
  background:var(--panel); color:var(--txt);
  font-family:var(--f-ui); font-size:var(--t-sm); font-weight:600;
  cursor:pointer; white-space:nowrap;
  transition:border-color .16s var(--ease),color .16s var(--ease),background-color .16s var(--ease);
}
.microbtn:hover{border-color:var(--accent-line); color:var(--accent); background:var(--panel2)}
.microbtn:focus-visible{outline:2px solid var(--accent); outline-offset:2px}
.microbtn.primary{border-color:var(--accent-line); color:var(--accent); background:var(--accent-soft)}
.recent-chip{font-family:var(--f-mono); font-size:11px}

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
  font-size:var(--t-sm); font-family:var(--f-ui);
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
  font-size:var(--t-sm); font-family:var(--f-ui); font-weight:600;
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
.list{max-height:74vh; overflow:auto; mask-image:linear-gradient(to bottom,#000 calc(100% - 28px),transparent); -webkit-mask-image:linear-gradient(to bottom,#000 calc(100% - 28px),transparent)}
.row{
  display:flex; justify-content:space-between; align-items:center;
  padding:12px 16px; gap:10px;
  border-bottom:1px solid var(--hair);
  border-left:2px solid transparent;
  cursor:pointer;
  transition:background-color .16s var(--ease),border-color .16s var(--ease);
}
.row:hover{border-left-color:var(--line)}   /* quiet structural hover — grey, never amber */
.row:hover{background:var(--panel2)}
.row.sel{background:var(--accent-soft); border-left-color:var(--accent)}
.row.sel .sym{color:var(--txt)}
.row .sym{
  font-family:var(--f-mono); font-weight:600; font-size:var(--t-base);   /* tickers are data keys → numeral voice (Space Grotesk reserved for what the page SAYS) */
  letter-spacing:-.005em; color:var(--txt);
  display:flex; align-items:center; gap:6px; flex-wrap:wrap;
}
.row .sym>span:first-child{font-size:10px} /* the state ● dot, shrunk */
.row .meta{
  font-family:var(--f-mono); font-size:var(--t-xs); color:var(--mut);
  margin-top:3px; line-height:1.45;
}
.row .pnl{
  font-family:var(--f-mono); font-size:var(--t-base); font-weight:500;
  text-align:right; white-space:nowrap; color:var(--txt);
  border-left:1px solid var(--hair); padding-left:10px;   /* ledger column rule */
}
/* left-rail ledger caption */
.list-cap{font-family:var(--f-ui); font-size:10px; font-weight:600; letter-spacing:var(--ls-label); text-transform:uppercase; color:var(--mut); padding:10px 16px 8px; border-bottom:1px solid var(--hair)}
.row .pnl .meta{text-align:right}
/* colorblind-safe sign glyph: ▲ for gain, ▼ for loss — color is no longer the only cue */
.row .pnl.pos::before{content:"\25B2\00a0"; opacity:0.55; font-size:0.78em; vertical-align:1px}
.row .pnl.neg::before{content:"\25BC\00a0"; opacity:0.55; font-size:0.78em; vertical-align:1px}
/* pinned overview row = "home": amber tick + faint amber wash */
.ovrow{background:var(--accent-soft)}
.ovrow .sym{color:var(--txt)}   /* was --accent; amber-on-amber-soft gave 1.7:1 contrast. White text + amber-soft bg + amber left-border still signals "this is the overview/home row" */
.ovrow.sel{border-left-color:var(--accent)}
/* dataFlick lives inside the prefers-reduced-motion:no-preference block (MOTION section) */

/* ============================== CARDS ============================== */
.card{
  background:var(--panel);
  border:1px solid var(--line);
  border-radius:var(--r-card);
  padding:var(--s5);
  margin-bottom:var(--s4);
  box-shadow:inset 0 1px 0 rgba(255,255,255,.045);   /* top-light bevel — echoes the masthead; still no drop shadows on printed cards */
}
.dh{display:flex; align-items:baseline; gap:var(--s3); flex-wrap:wrap; margin-bottom:var(--s1)}
.dh .t{
  font-family:var(--f-disp); font-size:var(--t-xl); font-weight:600;
  letter-spacing:var(--ls-tight); color:var(--txt);
  display:flex; align-items:center; gap:var(--s2);
  margin:0; padding:0;   /* h2 default margins reset — visual unchanged from when .t was a div/span */
}
/* card-title tick: decorative rhythm marker only — neutral grey, not amber (amber is reserved for live/active state cues) */
.dh .t::before{
  content:""; width:3px; height:var(--tick-s); border-radius:1px;
  background:#3A3F47; flex:none;   /* one step above --line so the rhythm marker actually reads */
  transition:height .18s var(--ease),background-color .18s var(--ease);
}
.card:hover>.dh .t::before,.foldcard:hover>summary .t::before{height:calc(var(--tick-s)*1.4); background:var(--mut)}   /* the tick answers the cursor — grey, never amber */
.dh .nm{font-family:var(--f-ui); color:var(--mut); font-size:var(--t-xs); font-weight:400}
/* lead-card bilingual shoulder — ONE per tab, echoing the masthead lockup */
.dh .t .t-en{font-family:var(--f-disp); font-size:var(--t-2xs); font-weight:600; letter-spacing:var(--ls-wide); text-transform:uppercase; color:var(--faint); margin-left:2px}

/* section captions all use the real .cap class (the legacy [style*=650] normalizer is gone) */
.hero-fig{font-family:var(--f-mono); font-size:var(--t-num); font-weight:600; color:var(--txt); letter-spacing:var(--ls-tight)}
.flag-head{font-weight:600}
/* the documented caption component (no !important — a real class need not fight inline styles) */
.cap{font-family:var(--f-ui); font-size:11px; font-weight:600; text-transform:uppercase; letter-spacing:var(--ls-label); color:var(--mut); padding-bottom:6px; border-bottom:1px solid var(--hair)}
/* amber focus ring on journal/rebal form controls (matches :focus-visible elsewhere) */
.seg input:focus,.seg textarea:focus{outline:none; border-color:var(--accent); box-shadow:0 0 0 2px var(--accent-soft)}

/* ============================== BADGES ============================== */
.badges{display:flex; gap:var(--s6); row-gap:var(--s3); flex-wrap:wrap; margin:var(--s4) 0 var(--s2)}
.badge .l{
  font-family:var(--f-ui); color:var(--mut); font-size:var(--t-xs); font-weight:600;
  text-transform:uppercase; letter-spacing:var(--ls-label); margin-bottom:var(--s1);
}
.badge .v{font-family:var(--f-mono); font-size:var(--t-md); font-weight:500; color:var(--txt)}
.badge .v .note{font-family:var(--f-ui); font-size:var(--t-xs)}
.pos{color:var(--green)} .neg{color:var(--red)}
.mm-grid{display:grid;grid-template-columns:minmax(0,1.35fr) minmax(280px,.65fr);gap:14px;align-items:start}
.mm-grid>div{min-width:0}
.mm-select{background:var(--bg2);color:var(--txt);border:1px solid var(--line);border-radius:var(--r-ctl);padding:7px 9px;font:600 var(--t-sm)/1.2 var(--f-ui)}
.mm-lanes{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px;margin-top:10px}
.mm-lane{border:1px solid var(--line);border-radius:var(--r-card);padding:9px 10px;background:var(--bg2);min-width:0}
.mm-lane .k{font-family:var(--f-mono);font-weight:600;color:var(--txt)}
.mm-lane .m{font-family:var(--f-ui);font-size:var(--t-xs);color:var(--mut);line-height:1.55;margin-top:4px}
.mm-mini{display:inline-flex;gap:7px;align-items:center;flex-wrap:wrap;margin-left:4px}
@media (max-width:820px){.mm-grid{grid-template-columns:1fr}.mm-lanes{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media (max-width:560px){.mm-lanes{grid-template-columns:1fr}.seg[data-seg="mass"]{max-width:100%;overflow:hidden}.seg[data-seg="mass"] .chartbox{max-width:100%;overflow:hidden}.seg[data-seg="mass"] svg{max-width:100%}}

/* ============================== LEGEND ============================== */
.legend{
  display:flex; gap:var(--s3); row-gap:var(--s1); flex-wrap:wrap;
  font-family:var(--f-ui); font-size:var(--t-xs); color:var(--mut);
  margin:var(--s2) 0 var(--s1); align-items:center;
}
.legend i{
  display:inline-block; width:9px; height:9px; border-radius:2px;
  margin-right:5px; vertical-align:middle;
}
/* truthful legend swatches: lines show as lines (solid/dashed/dotted), not squares — set color: on the <i> */
.legend i.ln{width:16px; height:0; border-top:2px solid currentColor; border-radius:0; background:none!important}
.legend i.lnd{width:16px; height:0; border-top:2px dashed currentColor; border-radius:0; background:none!important}
.legend i.lnt{width:16px; height:0; border-top:2px dotted currentColor; border-radius:0; background:none!important}

/* ============================== CHART CANVAS ============================== */
.chartbox{position:relative; width:100%; overflow:hidden; margin-top:10px}
svg{width:100%; height:auto; display:block}
svg text{font-family:var(--f-mono); font-variant-numeric:tabular-nums slashed-zero}

/* ============================== TOOLTIP ============================== */
.tt{
  position:fixed; pointer-events:none;
  background:color-mix(in srgb,var(--panel2) 88%,transparent);
  -webkit-backdrop-filter:blur(10px); backdrop-filter:blur(10px);
  border:1px solid var(--line);
  border-radius:var(--r-card);
  padding:9px 11px; font-size:var(--t-sm); font-family:var(--f-mono);
  z-index:40; display:none; min-width:150px;
  box-shadow:var(--sh-tt);
}
.tt b{font-family:var(--f-disp); font-weight:600; letter-spacing:-.005em}
.gl{border-bottom:1px dotted var(--faint); cursor:help}
.gl:hover{border-bottom-color:var(--accent); color:var(--txt)}
.gl:focus-visible{outline:2px solid var(--accent); outline-offset:2px; border-radius:2px}
.tt.gl-tt{pointer-events:auto; max-width:min(280px,80vw); white-space:normal; line-height:1.55; font-family:var(--f-ui)}
.tt.gl-tt .gk{font-family:var(--f-disp); font-weight:600; color:var(--accent); display:block; margin-bottom:3px}
.row:focus-visible,.seg-rail button:focus-visible,.tabs button:focus-visible{outline:2px solid var(--accent); outline-offset:2px; border-radius:var(--r-chip)}
/* interactive charts: crosshair + readout rows */
svg.xh{cursor:crosshair}
svg .cx{stroke:var(--accent); stroke-width:1; stroke-opacity:.55; pointer-events:none}
svg .cxd{fill:var(--accent); stroke:var(--bg); stroke-width:1; pointer-events:none}
.tt .xr{display:block; white-space:nowrap; line-height:1.5}
.tt .xr i{display:inline-block; width:8px; height:8px; border-radius:2px; margin-right:5px; vertical-align:middle}
.tt .xr.xd{margin-top:3px; padding-top:3px; border-top:1px solid var(--hair); font-weight:600}
@media (max-width:560px){
  /* onboarding strip: one scrollable line on phones — meta-instruction must not push the verdict down */
  #onboard{white-space:nowrap;overflow-x:auto;-webkit-overflow-scrolling:touch;scrollbar-width:none}
  #onboard::-webkit-scrollbar{display:none} svg text{font-size:var(--t-base)} }
/* sticky context pill + back-to-top */
.ctx{position:fixed; top:calc(var(--header-h) + 12px); left:50%; transform:translateX(-50%); z-index:18; display:flex; align-items:center; gap:8px; padding:5px 12px; background:color-mix(in srgb,var(--panel2) 95%,transparent); border:1px solid var(--line); border-radius:var(--r-pill); font:600 11px/1 var(--f-ui); color:var(--mut); box-shadow:var(--sh-tt); -webkit-backdrop-filter:blur(8px); backdrop-filter:blur(8px)}
.ctx[hidden]{display:none}
/* floating chrome eases in/out where the engine supports discrete-display transitions; hard toggle elsewhere */
.ctx,.totop{transition:opacity .18s var(--ease),translate .18s var(--ease),display .18s allow-discrete}
.ctx[hidden],.totop[hidden]{opacity:0}
.ctx[hidden]{translate:0 -4px}
.totop[hidden]{translate:0 6px}
@starting-style{.ctx:not([hidden]){opacity:0;translate:0 -4px}.totop:not([hidden]){opacity:0;translate:0 6px}}
.ctx-tick{width:3px; height:11px; border-radius:1px; background:var(--accent); box-shadow:0 0 8px var(--accent-line)}
.totop{position:fixed; right:22px; bottom:22px; z-index:18; width:42px; height:42px; border-radius:var(--r-pill); border:1px solid var(--line); background:var(--panel2); color:var(--accent); font-size:18px; cursor:pointer; box-shadow:var(--sh-lift)}
.totop[hidden]{display:none}
@media (pointer:coarse){ .totop{width:48px; height:48px} svg .cx{stroke-width:1.6} }
/* first-glance insight banner */
#insight{margin:18px auto 0; padding:0 var(--s7)}   /* auto-centers under the 1480px cap; padding keeps the shared 32px gutter */
#onboard-slot{max-width:1480px; margin:0 auto; padding:0 var(--s7)}
#onboard-slot:empty{display:none}
#insight:empty{display:none}
.ib{border-left:3px solid var(--hero-tone,var(--accent))}
.ib-row{line-height:1.7; margin:5px 0; font-size:var(--t-base); color:var(--txt)}
.ib-lk{cursor:pointer; color:var(--accent); border-bottom:1px dotted var(--accent-line); margin-left:6px; font-size:var(--t-sm); white-space:nowrap}
.ib-lk:hover{border-bottom-style:solid}
.ib-lk:focus-visible{outline:2px solid var(--accent); outline-offset:2px; border-radius:2px}
/* onboarding links go grey (dotted, amber only on hover) — the meta-instruction strip must not outshout the verdict */
#onboard .ib-lk{color:var(--mut); border-bottom-color:var(--mut)}
#onboard .ib-lk:hover{color:var(--accent); border-bottom-color:var(--accent)}
/* THE signature tier — the page's only typographic apex, used exactly twice:
   今日要点 hero verdict + QQQ 决策台「现在」headline. Real Space Grotesk via the embedded face. */
.verdict{font-family:var(--f-disp); font-size:clamp(17px,1.5vw,21px); line-height:1.4; font-weight:600; color:var(--txt); letter-spacing:var(--ls-tight)}
.verdict .ib-lk{font-size:.8em}
/* cap-demoted card title (今日要点): the verdict sentence is the hero, not the card label */
.dh .t-cap{font-family:var(--f-ui); font-size:var(--t-xs); font-weight:600; text-transform:uppercase; letter-spacing:var(--ls-label); color:var(--mut); margin:0; display:flex; align-items:center; gap:var(--s2)}

/* ============================== TABLES (print-ledger) ============================== */
table{width:100%; border-collapse:collapse; font-size:var(--t-sm); margin-top:8px}
th,td{padding:8px 10px; text-align:right; white-space:nowrap}
th{
  position:sticky; top:0; z-index:1;
  background:var(--panel); color:var(--mut);
  font-family:var(--f-ui); font-size:var(--t-xs); font-weight:600;
  text-transform:uppercase; letter-spacing:var(--ls-label);
  border-bottom:1px solid var(--line);
}
td{font-family:var(--f-mono); font-weight:400; color:var(--txt); border-bottom:1px solid var(--grid)}
td.l,th.l{text-align:left; font-family:var(--f-ui)}
td.l{color:var(--txt)}
tbody tr{transition:background-color .14s var(--ease)}
tbody tr:hover{background:var(--panel2)}

/* ============================== TAGS / CHIPS ============================== */
.tag{
  font-family:var(--f-ui); font-size:var(--t-xs); font-weight:600;
  text-transform:uppercase; letter-spacing:.04em;   /* tight chip spacing — deliberate exception to --ls-label */
  padding:2px 7px; border-radius:var(--r-chip);
}
.tag.b{background:rgba(79,178,134,.12); color:var(--green)}
.tag.s{background:rgba(229,112,122,.12); color:var(--red)}
.tag.o{background:var(--panel2); color:var(--mut)}
.note{font-family:var(--f-ui); color:var(--mut); font-size:var(--t-sm); line-height:1.7}
/* 决策一览 empty cells: demote the '—' placeholder below --mut so missing data reads as quiet, not as content */
.cell-empty{color:var(--mut); opacity:.5}
.note b{color:var(--txt); font-weight:600}
.scroll{max-height:420px; overflow:auto; border-radius:var(--r-card); border:1px solid var(--hair)}
/* prose measure: explainers cap at a readable column; ragged-right whitespace is composition, not waste */
.card .note,.foldbody .note{max-width:640px}
/* reference tier: structurally demoted surfaces (folded reference, methodology notes) sit on --bg2 with hairline borders and a smaller title */
.card.ref{background:var(--bg2); border-color:var(--hair)}
.card.ref .dh .t,.foldcard.ref>summary .t{font-size:var(--t-md)}
/* tier-1 decision cards carry a 2px top rule — the eye gets a ranking (brighter neutral, never amber) */
.card.t1{border-top:2px solid color-mix(in srgb,var(--mut) 35%,var(--line))}
/* reference zones: collapsed folds two-column at >=1200px; open folds + cards span full width */
@media (min-width:1200px){
  .seg[data-seg="qt"]:not([hidden]),.seg[data-seg="struct"]:not([hidden]),.seg[data-seg="nw"]:not([hidden]){display:grid; grid-template-columns:1fr 1fr; gap:0 var(--s4); align-items:start}
  .seg[data-seg="qt"]:not([hidden])>*,.seg[data-seg="struct"]:not([hidden])>*,.seg[data-seg="nw"]:not([hidden])>*{grid-column:1/-1; min-width:0}
  .seg[data-seg="qt"]:not([hidden])>details.foldcard:not([open]),.seg[data-seg="struct"]:not([hidden])>details.foldcard:not([open]),.seg[data-seg="nw"]:not([hidden])>details.foldcard:not([open]){grid-column:auto}
}
/* desktop: let the decision roster own the column instead of a fixed 420px slit */
@media (min-width:981px){.seg[data-seg="score"] .score-tablewrap{max-height:calc(100vh - 300px)}}
/* 决策一览 phone cards: rendered alongside the table, CSS decides which one shows.
   Desktop = table (12 cols), phone = stacked cards (nothing scrolls off-screen). */
.score-cards{display:none}
.smc{padding:12px 14px; border-bottom:1px solid var(--hair); cursor:pointer; border-radius:var(--r-card)}
.smc:last-child{border-bottom:0}
.smc:active{background:var(--panel2)}
.smc:focus-visible{outline:2px solid var(--accent-line); outline-offset:-2px}
.smc-top{display:flex; align-items:center; gap:7px; font-size:14px}
.smc-sym{font-family:var(--f-mono); letter-spacing:.02em}
.smc-state{font-size:var(--t-sm)}
.smc-pnl{margin-left:auto; font-family:var(--f-mono); font-size:14px}
.smc-mid{margin-top:4px; font-size:var(--t-sm); color:var(--mut); font-family:var(--f-mono); font-variant-numeric:tabular-nums slashed-zero}   /* CJK 仓位/动能 falls through per-character (precedent: .badge .v) */
.smc-bias{margin-top:6px; display:flex; flex-wrap:wrap; gap:4px}
details{margin-top:6px}
/* foldcard: a .card whose body only renders on demand — used to demote reference
   material below the decision cards (QQQ/TQQQ tab was a 8,400px wall of 11 open cards) */
details.foldcard{margin:0 0 var(--s4); padding:0}
.foldcard>summary{display:flex; align-items:baseline; gap:var(--s3); flex-wrap:wrap; padding:var(--s4) var(--s5); color:inherit; font-size:inherit}
.foldcard>summary .nm{font-family:var(--f-ui); color:var(--mut); font-size:var(--t-xs); font-weight:400}
.foldcard>summary .fold-chip{margin-left:auto; font-family:var(--f-mono); font-size:var(--t-xs); color:var(--mut); font-variant-numeric:tabular-nums slashed-zero}   /* neutral grey index value — never pass/fail colored */
.foldcard>summary:hover{background:var(--panel2); border-radius:var(--r-card)}
.foldcard>summary:focus-visible{outline:2px solid var(--accent-line); outline-offset:-2px}
.foldcard[open]>summary{padding-bottom:var(--s2)}
.foldcard>.foldbody{padding:0 var(--s5) var(--s5)}
/* boundary line between "decision" cards (open) and "reference" folds (closed) */
.ref-divider{
  margin:var(--s5) 0 var(--s3); padding-top:var(--s3);
  border-top:1px dashed var(--line);
  font-family:var(--f-ui); font-size:var(--t-xs); font-weight:600;
  text-transform:none; letter-spacing:.05em; color:var(--faint);
}
summary{cursor:pointer; color:var(--mut); font-size:var(--t-sm); font-family:var(--f-ui); font-weight:500; padding:4px 0; list-style:none}   /* grey text, amber ▸ marker — folded reference must not claim the accent */
summary::-webkit-details-marker{display:none}
summary::before{content:"▸ "; color:var(--accent); font-size:10px}
details[open] summary::before{content:"▾ "}
.legacychip{
  font-family:var(--f-mono); font-size:var(--t-2xs);
  background:none; border:1px solid var(--chip-bd-amber); color:var(--accent-ref);   /* data-quality caveat — reference tier, not the live accent */
  padding:2px 7px; border-radius:var(--r-chip); margin-left:6px;
}

/* ============================== FIB RANKING ROWS ============================== */
.frow{
  display:flex; align-items:center; gap:11px;
  padding:6px 8px; font-size:var(--t-sm); border-radius:var(--r-chip);
  transition:background-color .14s var(--ease);
}
.frow:hover{background:var(--panel2)}
.frow .fsym{width:72px; font-family:var(--f-mono); font-weight:600; font-size:var(--t-base); display:flex; align-items:center; gap:6px}
/* asset-class rows: fluid widths so bars never collapse / overflow on phones */
.acrow .fsym{width:auto; min-width:0; flex:0 1 auto; white-space:nowrap; overflow:hidden; text-overflow:ellipsis}
.acrow .fbar{background-image:repeating-linear-gradient(90deg,transparent 0 calc(10% - 1px),var(--hair) calc(10% - 1px) 10%)}   /* 10% graduations: instrument ruling, not color */
.acrow .fbar{flex:1 1 0; min-width:34px}
.acrow .acpct{flex:0 0 auto; width:54px; text-align:right; font-family:var(--f-mono)}
.acrow .acval{flex:0 0 auto; width:auto; min-width:78px; text-align:right; white-space:nowrap}
.frow .fsym>span:first-child{font-size:10px}
.frow .fval{width:auto; min-width:84px; flex:0 0 auto; white-space:nowrap; text-align:right; font-family:var(--f-mono); font-weight:500}   /* was width:50px — dollar figures overprinted the next cell */
.frow .fst{width:62px; flex:0 0 auto; font-family:var(--f-ui); font-size:var(--t-xs); color:var(--mut)}
.fbar{position:relative; height:14px; background:var(--grid); border-radius:var(--r-chip); flex:1; overflow:hidden}
.fbar .z{position:absolute; left:50%; top:0; bottom:0; width:1px; background:var(--line)}
.fbar .p{position:absolute; top:2.5px; height:9px; border-radius:2px; transition:width .3s var(--ease),filter .15s var(--ease)}
.frow:hover .fbar .p{filter:brightness(1.18)}
.frow:hover .fsym{color:var(--txt)}

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
  display:flex; gap:0; position:sticky; top:var(--header-h); z-index:15;
  background:var(--panel); border:1px solid var(--line);
  border-radius:var(--r-card); padding:0 4px; margin-bottom:16px;
  box-shadow:var(--sh-lift);
  overflow-x:auto; overflow-y:hidden; scrollbar-width:none;
}
.seg-rail::-webkit-scrollbar{display:none}
.seg-rail button{
  background:none; border:0; color:var(--mut);
  font-family:var(--f-ui); font-size:var(--t-sm); font-weight:600; letter-spacing:.02em;
  padding:11px 12px 12px; cursor:pointer; position:relative;
  transition:color .16s var(--ease);
  flex:none; white-space:nowrap;
}
.seg-rail button:hover{color:var(--txt)}
.seg-rail button[hidden]{display:none}   /* segs outside the active workspace are hidden by syncWs() */
.seg-rail button.on{color:var(--accent)}
.seg-rail button.on::after{
  content:""; position:absolute; left:12px; right:12px; bottom:-1px; height:2px;
  background:var(--accent); border-radius:1px; box-shadow:0 0 8px var(--accent-line);
}
/* primary workspace bar (level 1 of the two-level nav) — heavier identity than the sub-rail below it */
.ws-rail{
  display:flex; gap:2px; align-items:center;
  background:var(--panel); border:1px solid var(--line);
  border-radius:var(--r-card); padding:4px 6px; margin-bottom:8px;
  overflow-x:auto; overflow-y:hidden; scrollbar-width:none;
}
.ws-rail::-webkit-scrollbar{display:none}
.ws-rail button{
  background:none; border:0; color:var(--mut);
  font-family:var(--f-disp); font-size:var(--t-base,13px); font-weight:700; letter-spacing:.02em;
  padding:9px 14px; cursor:pointer; border-radius:var(--r-chip,6px);
  flex:none; white-space:nowrap; transition:color .16s var(--ease),background .16s var(--ease);
}
.ws-rail button:hover{color:var(--txt)}
.ws-rail button.on{color:var(--accent); background:var(--bg); box-shadow:inset 0 0 0 1px var(--accent-line,var(--line))}
/* breadcrumb lives IN the rail on overview (one 44px band instead of two) */
.rail-here{flex:none; align-self:center; font-family:var(--f-disp); font-size:var(--t-sm); font-weight:600; color:var(--txt); padding:0 10px 0 12px; white-space:nowrap}
.seg[hidden]{display:none}

/* ============================== MOTION ============================== */
@keyframes riseIn{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
@keyframes tickIn{from{transform:scaleX(0)}to{transform:scaleX(1)}}
@keyframes dataFlick{0%{opacity:.35}100%{opacity:1}}
@keyframes drawIn{from{stroke-dashoffset:var(--len,1200)}to{stroke-dashoffset:0}}
@keyframes segIn{from{opacity:0;transform:translateY(5px)}to{opacity:1;transform:none}}
@keyframes barIn{from{width:0}}
@keyframes heroEdge{to{border-left-color:var(--hero-tone,var(--accent))}}   /* from-only: each gauge sweeps to its inline width; replays on panel reveal */

@media (prefers-reduced-motion: no-preference){
  /* "The Morning Open" — load order follows information priority:
     verdict (.02s) → four decision KPIs (.04–.16s) → ledger settles (.30s) → rail + cards */
  body.ready #insight .card{opacity:0; animation:riseIn .46s var(--ease) .02s forwards}
  body.ready .kpi{opacity:0; animation:riseIn .46s var(--ease) forwards}
  body.ready .kpi:nth-child(1){animation-delay:.04s}
  body.ready .kpi:nth-child(2){animation-delay:.08s}
  body.ready .kpi:nth-child(3){animation-delay:.12s}
  body.ready .kpi:nth-child(4){animation-delay:.16s}
  body.ready .kpi-ledger{opacity:0; animation:riseIn .4s var(--ease) .30s forwards}
  body.ready .left{opacity:0; animation:riseIn .46s var(--ease) .14s forwards}
  /* cards are JS-regenerated each render; nth-of-type keeps the stagger without re-arming via class */
  body.ready:not(.done) .right .card{opacity:0; animation:riseIn .46s var(--ease) .32s forwards}
  body.ready:not(.done) .right .card:nth-of-type(1){animation-delay:.06s}
  body.ready:not(.done) .right .card:nth-of-type(2){animation-delay:.13s}
  body.ready:not(.done) .right .card:nth-of-type(3){animation-delay:.20s}
  body.ready:not(.done) .right .card:nth-of-type(4){animation-delay:.27s}
  /* panel-entry settle: .segin is added by activateSeg on tab switches AND emitted directly on
     stock-detail entry / post-load overview renders; the hidden→shown display flip restarts it */
  .seg.segin:not([hidden]){animation:segIn .20s var(--ease)}
  /* one-shot "data loaded" flicker on the freshly selected row's values */
  body.ready .row.sel .pnl,body.ready .row.sel .meta{animation:dataFlick .26s ease-out}
  /* gauges sweep to position on reveal (diverging drift bars grow from their outer edge — acceptable) */
  body.ready .fbar .p{animation:barIn .5s var(--ease) backwards}
  /* tab indicator blooms on activation (pseudo-element is recreated on .on toggle, so it replays) */
  .seg-rail button.on::after,.tabs button.on::after{animation:tickIn .22s var(--ease);transform-origin:center}
  /* trade markers acknowledge the cursor */
  .mk{transform-box:fill-box;transform-origin:center;transition:transform .15s var(--ease),fill-opacity .15s var(--ease)}
  .mk:hover{transform:scale(1.3);fill-opacity:1}
  /* the verdict gets STAMPED: severity rail snaps on after the sentence lands (.46s riseIn + delay) */
  body.ready .card.ib{border-left-color:transparent;animation:heroEdge .01s linear .55s forwards}
  /* pen-plotter draw-in: opt-in via .draw on lead series (any svg geometry); .draw2 = the second pen,
     delayed — market draws first, then your position follows. Dashed references stay static. */
  body.ready svg .draw:not(.drawn),body.ready svg .draw2:not(.drawn){
    stroke-dasharray:var(--len,1400); stroke-dashoffset:var(--len,1400);
    animation:drawIn 1.15s var(--ease) .15s forwards;
  }
  body.ready svg .draw2:not(.drawn){animation-delay:.5s}
  /* the Barometer sweeps in once with the masthead (svg is flex:1 — scaleX leaves the now-tick put) */
  body.ready #qbar svg{transform-origin:left;animation:tickIn .6s var(--ease) .5s backwards}
  /* KPI numerals settle after their cells rise */
  body.ready .kpi .v{animation:dataFlick .32s ease-out backwards}
  body.ready .kpi:nth-child(1) .v{animation-delay:.24s}
  body.ready .kpi:nth-child(2) .v{animation-delay:.28s}
  body.ready .kpi:nth-child(3) .v{animation-delay:.32s}
  body.ready .kpi:nth-child(4) .v{animation-delay:.36s}
}
@media (prefers-reduced-motion: reduce){
  body.ready .kpi,body.ready .left,body.ready:not(.done) .right .card,
  body.ready #insight .card,body.ready .kpi-ledger{opacity:1;animation:none}
  .kpi:first-child .l::after{transform:scaleX(1);animation:none}
  .seg{animation:none!important}
  .fbar .p{animation:none}
  .seg-rail button.on::after,.tabs button.on::after{animation:none}
  .mk{transition:none}
  body.ready .card.ib{animation:none;border-left-color:var(--hero-tone,var(--accent))}
  #qbar svg{animation:none}
  .kpi .v{animation:none}
  *{scroll-behavior:auto}
}

/* ============================== RESPONSIVE ============================== */
/* skip-to-main link: visible only when keyboard-focused — lets screen-reader users bypass header/KPIs and jump into content */
.skip{position:absolute; left:-9999px; top:auto; width:1px; height:1px; overflow:hidden}
.skip:focus{position:fixed; left:8px; top:8px; width:auto; height:auto; padding:10px 14px; background:var(--panel2); color:var(--accent); border:2px solid var(--accent); border-radius:8px; z-index:99; font:700 13px/1 var(--f-ui)}
/* in-content cross-tab links: dotted underline in --mut so they don't burn amber attention; amber returns only on :hover/:focus */
.note-lk{cursor:pointer; color:var(--mut); border-bottom:1px dotted var(--mut); padding-bottom:1px; transition:color .15s,border-color .15s}
.note-lk:hover,.note-lk:focus-visible{color:var(--accent); border-bottom-color:var(--accent); outline:none}
/* dynamic viewbar height for the mobile sticky-stack — see ResizeObserver wire at end of script */
:root{--viewbar-h:50px}
/* Desktop: hide the inline attn chip (the dedicated 关注度 column carries the signal) */
.attn-inline{display:none}
/* Tablet / landscape phone breakpoint: extend to landscape iPhones (max-height:500px) so they don't drop into desktop layout */
@media (max-width:980px), (orientation:landscape) and (max-height:500px){
  .kpis{grid-template-columns:repeat(2,1fr)}
  .kpi:nth-child(4n+1){border-left:1px solid var(--line)}
  .kpi:nth-child(2n+1){border-left:0}
  .kpi:nth-child(-n+4){border-top:1px solid var(--line)}
  .kpi:nth-child(-n+2){border-top:0}
  .wrap{flex-direction:column}
  .right{order:1;width:100%}            /* show 今日要点 / overview BEFORE the holdings rail on small screens */
  .left{order:2; width:100%; position:static}
  .list{max-height:46vh}
  .seg-rail{top:0; position:static}
  .seg-rail button{padding:9px 11px 10px; font-size:var(--t-sm)}
  .seg-rail button.on::after{left:11px; right:11px}
  header .sub{margin-left:0; text-align:left; width:100%; overflow-wrap:anywhere; text-transform:none; letter-spacing:.04em}
}
/* Phone breakpoint: also fires on landscape iPhones so the mobile chrome diet applies there too */
@media (max-width:560px), (orientation:landscape) and (max-height:500px){
  /* phone: single-column KPIs with clamped values, no clipping */
  .kpis{margin:12px 12px 4px}   /* 2-col grid from the ≤980 block applies — first decision card returns to the fold */
  /* on a stock detail view, the portfolio-wide KPI strip is irrelevant context — hide it so stock data is the first thing seen.
     Push #right down by the header height so the sticky viewbar (top:84) doesn't render behind the sticky header at scrollY=0 */
  body[data-view="stock"] .kpis{display:none}
  body[data-view="stock"] .right{padding-top:8px}   /* small breathing room above viewbar at top of page */
  body[data-view="stock"] .wrap{padding-top:calc(var(--header-h) + var(--s3))}   /* push past the sticky header so the viewbar's natural position is below it */
  .kpi{padding:12px 14px}
  .kpi .v{font-size:18px; white-space:normal; overflow-wrap:anywhere}
  .kpi:first-child .v{font-size:20px}
  .kpi-ledger{padding:9px 14px; gap:3px 18px}
  .wrap{padding:var(--s3) var(--s3) var(--s7); gap:var(--s3)}
  /* MOBILE CHROME DIET: ~80px saved before content shows.
     header: padding tightened, .sub hidden, h1 ticked down — claws back ~36px
     viewbar: recent-chips strip hidden (still on the chip strip in overview, but not eating sticky real estate)
     --header-h overridden so the sticky-stack (viewbar at top:--header-h, seg-rail below it) auto-recomputes. */
  :root{--header-h:48px}
  header{padding:var(--s2) var(--s3); gap:var(--s2)}
  header h1{font-size:var(--t-md)}
  header h1::before{height:14px}
  header .sub{display:none}
  /* on stock view, hide the secondary "最近" chips row in the viewbar (still reachable via overview banner) */
  body[data-view="stock"] .viewbar .view-actions .recent-chip,
  body[data-view="stock"] .viewbar .view-actions:nth-of-type(2){display:none}
  /* keep the back-button + breadcrumb visible after scrolling so the trader is never trapped below the fold.
     top:var(--header-h) sits BELOW the sticky page header (z:20); viewbar z:16 keeps it under header and above content */
  .viewbar{position:sticky; top:var(--header-h); z-index:16; background:var(--bg); align-items:center; flex-direction:row; flex-wrap:wrap; gap:var(--s2); padding:var(--s2) 0; margin:0 0 var(--s2); border-bottom:1px solid var(--hair)}
  .viewbar .trail{flex:1 1 auto; min-width:0}
  .view-actions{justify-content:flex-end}
  .microbtn{min-height:44px; padding:11px 14px; font-size:var(--t-base)}   /* 44pt iOS-recommended tap target */
  .viewbar .microbtn.primary{flex:0 0 auto}
  /* tab rail: scroll horizontally; chevron edge instead of fading-mask so users know more tabs exist.
     Sticky below the header (84) + sticky viewbar (--viewbar-h), so tab switching is reachable from any scroll depth. */
  .seg-rail{
    flex-wrap:nowrap; overflow-x:auto; overflow-y:hidden;
    scroll-snap-type:x proximity; -webkit-overflow-scrolling:touch;
    position:sticky; top:calc(var(--header-h) + var(--viewbar-h, 50px)); z-index:14; background:var(--bg);
  }
  .seg-rail::after{content:"›"; position:sticky; right:0; padding:6px 8px; color:var(--mut); background:linear-gradient(90deg,transparent,var(--bg) 40%); pointer-events:none; align-self:center; font-size:18px}
  .seg-rail button{flex:0 0 auto; min-height:44px; padding:13px 14px; font-size:var(--t-base); scroll-snap-align:start}   /* 44pt min-height for one-handed thumb hits */
  /* workspace bar: same horizontally-scrollable pattern + 44pt targets as the sub-rail */
  .ws-rail{flex-wrap:nowrap; overflow-x:auto; overflow-y:hidden; -webkit-overflow-scrolling:touch}
  .ws-rail button{flex:0 0 auto; min-height:44px; padding:12px 14px; font-size:var(--t-base)}
  /* bigger touch targets */
  .tabs button{padding:14px 6px; min-height:44px}
  .row{padding:14px 14px; min-height:44px}
  .controls input,.controls select{min-height:44px; padding:12px 12px; font-size:14px}   /* search & sort dropdown also 44pt */
  /* dense tables: keep the ticker column visible while the rest scrolls */
  .scroll{overflow-x:auto}
  .scroll table th:first-child,.scroll table td:first-child{position:sticky; left:0; background:var(--panel); z-index:2}
  .scroll table thead th:first-child{z-index:3}
  .scroll table th,.scroll table td{padding:7px 8px; font-size:var(--t-sm)}   /* readability floor: never below 12px in data cells */
  th,.legacychip,.tag{font-size:11px}                                  /* micro-labels floor at 11px on glass */
  /* QQQ/TQQQ tab tables carry prose columns (老师动作 / 纪律 / 含义) — let them wrap
     on phones instead of forcing a 170px+ side-scroll on the decision table */
  .seg[data-seg="qt"] table td.l{white-space:normal; min-width:72px}
  /* 决策一览: phone swaps the 12-col table for stacked cards — every signal stays on-screen,
     nothing side-scrolls (the old column-drop still left a ~156px overflow from the bias chips) */
  .score-tablewrap{display:none}
  .score-cards{display:block; margin-top:4px}
  /* show inline attention chip only when the dedicated 关注度 column is hidden (mobile) */
  .attn-inline{display:inline-block!important; font-size:11px; padding:1px 5px; margin-left:4px; vertical-align:1px}
  .ib-row{font-size:var(--t-base)}
  /* keep the floating context pill visible on mobile — it doubles as a back-button when in a stock view */
  .ctx{top:8px; right:8px; left:auto; transform:none; max-width:70vw; font-size:var(--t-sm); padding:9px 14px; cursor:pointer; min-height:38px}
  #insight{margin:12px 12px 0}
  /* phone KPI strip: single-column is vertically cheap → keep the FULL ledger
     (realized P&L / dividends / deposits are the truth the strip exists to show) */
  /* dense secondary tables: priority-column drops so decision columns survive 390px */
  .seg[data-seg="sig"] table th:nth-child(8),.seg[data-seg="sig"] table td:nth-child(8){display:none}
  .seg[data-seg="tx"] table th:nth-child(3),.seg[data-seg="tx"] table td:nth-child(3),
  .seg[data-seg="tx"] table th:nth-child(4),.seg[data-seg="tx"] table td:nth-child(4),
  .seg[data-seg="tx"] table th:nth-child(5),.seg[data-seg="tx"] table td:nth-child(5){display:none}
  .badges{gap:14px; row-gap:12px}
  .badge .v{overflow-wrap:anywhere}
}
</style>
</head>
<body>
<a class="skip" href="#main" onclick="event.preventDefault();const m=document.getElementById('main');if(m){m.setAttribute('tabindex','-1');m.focus({preventScroll:false});m.scrollIntoView();}">跳到主要内容</a>
<header>
 <h1>投资组合时间线<span class="h1-en">Portfolio Timeline</span><span id="qbar-name" aria-hidden="true"></span></h1>
 <span class="sub" id="rangelbl"></span>
 <div id="qbar" title="QQQ 近60日趋势状态带（绿=多头 · 暖琥珀=过热 · 红=破21 · 灰=转换）— 琥珀竖线=今天 · 点击打开 QQQ 决策台" role="button" tabindex="0" aria-label="QQQ 近60日趋势状态带 · 点击打开决策台" onclick="ovGo('qt')" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();ovGo('qt');}"></div>
</header>
<div id="fetchwarn"></div>
<div id="insight"></div>
<div class="kpis" id="kpis"></div>
<div id="onboard-slot"></div>
<main id="main" class="wrap">
 <nav class="left" aria-label="持仓列表">
   <div class="controls">
     <input id="search" placeholder="搜索代码或公司名…" aria-label="搜索持仓（代码或公司名）"/>
     <select id="sort" aria-label="排序方式">
       <option value="value">按市值排序</option>
       <option value="day">按今日涨跌幅</option>
       <option value="unreal">按未实现盈亏</option>
       <option value="realized">按已实现盈亏</option>
       <option value="numTrades">按交易次数</option>
       <option value="fibmom">按斐波那契动能</option>
       <option value="sym">按代码字母</option>
     </select>
     <div class="tabs" role="tablist" aria-label="持仓筛选">
       <button data-f="held" class="on" role="tab" aria-selected="true">持有中</button>
       <button data-f="exited" role="tab" aria-selected="false">已清仓</button>
       <button data-f="all" role="tab" aria-selected="false">全部</button>
     </div>
   </div>
   <div class="list" id="list" role="listbox" aria-label="持仓"></div>
 </nav>
 <section class="right" id="right" aria-label="详情面板"></section>
</main>
<div class="tt" id="tt"></div>
<div id="ctx" class="ctx" hidden role="button" tabindex="0" aria-label="跳转 / 返回组合总览" onclick="if(sel!=='__OV__')goBack();else window.scrollTo({top:0,behavior:PRM?'auto':'smooth'});" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();this.click();}"><span class="ctx-tick"></span><span id="ctxt"></span></div>
<div id="srlive" aria-live="polite" aria-atomic="true" style="position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0,0,0,0);clip-path:inset(50%)"></div>
<button id="totop" class="totop" aria-label="回到顶部" hidden>↑</button>
<script>
const DATA = __DATA__;
const fmt=(n,d=2)=>n==null?'—':(n<0?'−$':'$')+Math.abs(n).toLocaleString('en-US',{minimumFractionDigits:d,maximumFractionDigits:d});   // U+2212 true minus — in the embedded subset
const fmtN=(n,d=0)=>n==null?'—':(n<0?'−':'')+Math.abs(n).toLocaleString('en-US',{minimumFractionDigits:d,maximumFractionDigits:Math.max(d,2)});
const pct=n=>n==null?'—':(n>=0?'+':'−')+Math.abs(n).toFixed(2)+'%';
const ppf=n=>n==null?'—':(n>=0?'+':'−')+Math.abs(n).toFixed(2)+'pp';   // percentage-point difference (excess vs benchmark) — one canonical unit everywhere
const cls=n=>n>0?'pos':(n<0?'neg':'');
const chipBd=c=>({'#4FB286':'var(--chip-bd-green)','#E5707A':'var(--chip-bd-red)','#E8B339':'var(--chip-bd-amber)'}[c]||(c+'66'));   // one tinted-chip border convention
const S=DATA.summary, D0=S.dateRange[0], D1=S.dateRange[1];
const PRM=!!(window.matchMedia&&matchMedia('(prefers-reduced-motion: reduce)').matches);   // JS-driven smooth scrolls must honor it too (CSS *{scroll-behavior:auto} can't reach scrollTo options)
const priceSrc=S.priceMode==='mark-to-market'
 ? `股票市值 Yahoo refreshed <b>${S.priceAsOf}</b> · 期权/现金 Fidelity 快照`
 : `股票市值 Fidelity 快照 · 图表价格 Yahoo Finance`;
document.getElementById('rangelbl').innerHTML=`数据窗口 <b>${D0} → ${D1}</b> · ${priceSrc} · 共 ${S.numStocks} 只标的（持有 ${S.numHeld}）`;
// loud banner when Yahoo prices are stale — trader must NOT mistake yesterday's numbers for live
if(S.fetchOK===false||S.fetchStale){
 const reason=S.fetchReason||'缓存价格';
 const age=S.cacheAgeDays||0;
 const ageTxt=age>0?` · 缓存约 ${age} 天前`:'';
 if(S.fetchOK===false){   // fetch FAILURE keeps the loud full-width band — data-integrity safeguard
  document.getElementById('fetchwarn').innerHTML=`<div role="alert" style="margin:10px var(--s7) 0;padding:9px 14px;border:1px solid var(--red);background:rgba(216,77,87,.10);border-radius:var(--r-card);color:var(--txt);font:600 12.5px/1.5 var(--f-ui)">⚠ ${reason}${ageTxt} · 价格数字可能与实时不一致 — 重跑 <code style="background:var(--bg2);padding:1px 5px;border-radius:4px">python3 sync.py</code> 刷新。</div>`;
 } else {   // merely stale: compact masthead chip — full sentence in the title attr, role=alert kept
  const h1=document.querySelector('header h1');
  if(h1)h1.insertAdjacentHTML('afterend',`<span id="stalechip" class="chip" role="alert" title="${reason}${ageTxt} · 价格数字可能与实时不一致 — 重跑 python3 sync.py 刷新" style="color:var(--accent);border-color:var(--chip-bd-amber);cursor:help;flex:none;font-family:var(--f-mono)">⏱ 数据${age>0?` −${age}d`:'缓存'} · sync.py</span>`);
 }
}

// Two-tier KPI strip. Tier 1 answers "did I make money / am I winning" at full size;
// tier 2 is the accounting ledger — kept visible (it's the audit truth) but demoted so it
// no longer competes with the four decision numbers.
const kpis=[
 ['股票市值 (不含现金/期权)',fmt(S.marketValue),''],
 [S.priceMode==='mark-to-market'?'未实现盈亏 (Yahoo重估)':'未实现盈亏 (券商实际)',fmt(S.unrealized),cls(S.unrealized)],
 ['区间收益 (时间加权)',pct(S.curReturn),cls(S.curReturn)],
 ['超额 vs 标普',(S.spReturn==null?'—':ppf(S.curReturn-S.spReturn)),(S.spReturn==null?'':cls(S.curReturn-S.spReturn))],
];
const kpiLedger=[
 ['期权毛敞口',(S.optMarkGross?fmt(S.optMarkGross)+' <span class="note">≈权益 '+S.optPctEquity+'%</span>':'—'),''],
 ['已实现盈亏 (窗口·含估算)',fmt(S.realized),cls(S.realized)],
 ['期权净现金流',fmt(S.optNet),cls(S.optNet)],
 ['窗口内净买入',fmt(S.netInvested),''],
 ['现金转入 (窗口)',fmt(S.deposits),''],
 ['累计入金',fmt(S.lifeDeposits||S.deposits),''],
 ['股息',fmt(S.dividends||0),''],
];
document.getElementById('kpis').innerHTML=
 kpis.map(k=>`<div class="kpi"><div class="l">${k[0]}</div><div class="v ${k[2]}">${k[1]}</div></div>`).join('')+
 `<div class="kpi-ledger" aria-label="账目明细">${kpiLedger.map(k=>`<span class="kl"><span class="kl-l">${k[0]}</span><span class="kl-v ${k[2]}">${k[1]}</span></span>`).join('')}</div>`;

let filter='held', sortKey='value', q='', sel='__OV__';
const stocks=DATA.stocks;
const DEFAULT_SEG={ov:'score',stk:'price'};
const VALID_SEG={ov:['score','decide','fin','aisemi','aics','aiwatch','qt','mass','nw','risk','struct','cmp','pfib','sig','beh','journal','rebal'], stk:['price','fin','tx','fib','journal']};   // allowlist: keeps invalid #/stock/NVDA/banana from silently overwriting localStorage and lets us reject typo-URLs
const SEG_LABEL={score:'决策一览', decide:'决策分析', fin:'财务状态', aisemi:'AI半导体', aics:'AICS产业链', aiwatch:'AI观察池', qt:'QQQ/TQQQ', mass:'重心边界', nw:'净值·账户', risk:'波动贡献', struct:'结构', cmp:'指数对比', pfib:'技术·节奏', sig:'持仓信号', beh:'行为决策', rebal:'再平衡计划', price:'价格 · 操作', tx:'交易明细', fib:'斐波那契'};
function segLabel(seg,ctx){if(seg==='journal')return ctx==='stk'?'日志':'交易日志'; return SEG_LABEL[seg]||seg||'';}
// Two-level portfolio nav: 5 themed workspaces (level 1) over the seg sub-rail (level 2).
// Routing is untouched — hash stays #/portfolio/<seg>; the workspace is derived FROM the seg.
const WS_MAP=[
 {id:'today', label:'今日决策', segs:['score','decide','qt','mass']},
 {id:'ai',    label:'研究·AI',  segs:['fin','aisemi','aics','aiwatch']},
 {id:'perf',  label:'净值·表现', segs:['nw','cmp','pfib','sig']},
 {id:'risk',  label:'风险·结构', segs:['risk','struct']},
 {id:'plan',  label:'行为·计划', segs:['beh','journal','rebal']},
];
function wsFor(seg){for(const w of WS_MAP)if(w.segs.indexOf(seg)>=0)return w; return WS_MAP[0];}
function rememberWsSeg(seg){try{localStorage.setItem('ptrak.wsseg.'+wsFor(seg).id,seg);}catch(e){}}
function wsLastSeg(w){try{const s=localStorage.getItem('ptrak.wsseg.'+w.id);if(s&&w.segs.indexOf(s)>=0)return s;}catch(e){} return w.segs[0];}
function wsGo(id){const w=WS_MAP.filter(x=>x.id===id)[0]||WS_MAP[0]; if(sel==='__OV__'&&wsFor(activeSeg()).id===w.id)return; ovGo(wsLastSeg(w),{replace:true});}   // replace: workspace switches are view-state like tab clicks (back walks stock-to-stock, not workspace-to-workspace); same-workspace click short-circuits the full re-render
// visual sync of the two-level nav: highlight the workspace owning `seg`, show only its sub-rail segs
function syncWs(seg){
 const r=document.getElementById('right');if(!r)return;
 const bar=r.querySelector('.ws-rail');if(!bar)return;
 const w=wsFor(seg);
 bar.querySelectorAll('button[data-ws]').forEach(b=>{const on=b.dataset.ws===w.id;b.classList.toggle('on',on);b.setAttribute('aria-selected',on?'true':'false');});
 r.querySelectorAll('.seg-rail button[data-seg]').forEach(b=>{b.hidden=w.segs.indexOf(b.dataset.seg)<0;});
}
let routeApplying=false;
function findStock(sym){sym=(sym||'').toUpperCase();return stocks.find(x=>x.sym===sym)||null;}
function normSym(sym){return (sym||'__OV__').toUpperCase();}
function ctxFor(sym){return sym==='__OV__'?'ov':'stk';}
function defaultSeg(sym){return sym==='__OV__'?DEFAULT_SEG.ov:DEFAULT_SEG.stk;}
function cleanRoute(r){
 const sym=normSym(r&&r.sym);
 if(sym==='__OV__'){const seg=(r&&r.seg)||DEFAULT_SEG.ov; return {sym:'__OV__', seg:VALID_SEG.ov.indexOf(seg)>=0?seg:DEFAULT_SEG.ov};}
 if(!findStock(sym))return{sym:'__OV__',seg:DEFAULT_SEG.ov};
 const seg=(r&&r.seg)||DEFAULT_SEG.stk;
 return {sym, seg:VALID_SEG.stk.indexOf(seg)>=0?seg:DEFAULT_SEG.stk};
}
const VALID_F=['held','exited','all'], VALID_S=['value','day','fibmom','sym','unreal','realized','numTrades'];
function encodeRoute(sym,seg){
 sym=normSym(sym);seg=seg||defaultSeg(sym);
 const base=sym==='__OV__'?'#/portfolio/'+encodeURIComponent(seg):'#/stock/'+encodeURIComponent(sym)+'/'+encodeURIComponent(seg);
 // append filter/search/sort suffix when non-default so the URL is shareable AND back/forward restores them
 const qs=[
   q?'q='+encodeURIComponent(q):'',
   (filter&&filter!=='held')?'f='+encodeURIComponent(filter):'',
   (sortKey&&sortKey!=='value')?'s='+encodeURIComponent(sortKey):'',
 ].filter(Boolean).join('&');
 return qs?base+'?'+qs:base;
}
function parseRoute(){
 const raw=(window.location.hash||'').replace(/^#\/?/,'');
 if(!raw)return null;
 const qi=raw.indexOf('?');
 const path=qi>=0?raw.slice(0,qi):raw;
 const qs=qi>=0?raw.slice(qi+1):'';
 // Only override filter state when URL has an explicit ?-suffix; otherwise leave the localStorage rehydration intact
 if(qs){
   let next={q:'',filter:'held',sortKey:'value'};
   qs.split('&').forEach(kv=>{const eq=kv.indexOf('=');const k=eq>=0?kv.slice(0,eq):kv;
     let v=eq>=0?kv.slice(eq+1):'';try{v=decodeURIComponent(v);}catch(e){}
     if(k==='q')next.q=(v||'').toLowerCase().trim();
     else if(k==='f'&&VALID_F.indexOf(v)>=0)next.filter=v;
     else if(k==='s'&&VALID_S.indexOf(v)>=0)next.sortKey=v;
   });
   q=next.q; filter=next.filter; sortKey=next.sortKey;
   syncListUI();
 }
 const parts=path.split('/').filter(Boolean).map(p=>{try{return decodeURIComponent(p)}catch(e){return p}});
 const head=(parts[0]||'').toLowerCase();
 if(head==='portfolio'||head==='overview'||head==='ov')return cleanRoute({sym:'__OV__',seg:parts[1]});
 if(head==='stock'&&parts[1])return cleanRoute({sym:parts[1],seg:parts[2]});
 if(parts[0]&&findStock(parts[0]))return cleanRoute({sym:parts[0],seg:parts[1]}); // legacy short hash, e.g. #NVDA/fib
 return null;
}
function activeSeg(){
 const b=document.querySelector('#right .seg-rail button.on');
 return b&&b.dataset?b.dataset.seg:defaultSeg(sel);
}
function rememberSeg(ctx,seg){try{localStorage.setItem('ptrak.seg.'+ctx,seg);}catch(e){}}
function lastSeg(ctx){try{return localStorage.getItem('ptrak.seg.'+ctx)||DEFAULT_SEG[ctx];}catch(e){return DEFAULT_SEG[ctx];}}
function writeRoute(sym,seg,replace){
 const r=cleanRoute({sym,seg}),url=encodeRoute(r.sym,r.seg);
 if(window.location.hash===url)return;
 const liveY=window.scrollY||window.pageYOffset||0;
 const _l=document.getElementById('list');
 const liveListY=_l?_l.scrollTop:0;
 if(!routeApplying){   // skip during back/forward replay — the live scroll is mid-restore and would clobber the saved offset
   // snapshot OUTGOING view's scroll into the current entry so a future back/forward can restore it
   try{const cur=history.state||{};
     history.replaceState(Object.assign({},cur,{scrollY:liveY,listScrollY:liveListY}),'',window.location.hash);
   }catch(e){}
 }
 // for replaceState (tab/filter): preserve live scroll (in-place); for pushState (new entry): start at 0
 try{history[replace?'replaceState':'pushState']({ptrak:r,scrollY:replace?liveY:0,listScrollY:replace?liveListY:0},'',url);}
 catch(e){window.location.hash=url;}
}
function recentStocks(){
 try{const a=JSON.parse(localStorage.getItem('ptrak.recent.v1'));if(Array.isArray(a))return a.filter(x=>findStock(x));}
 catch(e){}
 return [];
}
function rememberStock(sym){
 if(sym==='__OV__'||!findStock(sym))return;
 const a=[sym].concat(recentStocks().filter(x=>x!==sym)).slice(0,8);
 try{localStorage.setItem('ptrak.recent.v1',JSON.stringify(a));}catch(e){}
}
function _updateDocTitleAndSR(sym,seg){
 const _stk=sym!=='__OV__'?findStock(sym):null;
 const _name=sym==='__OV__'?'组合总览':sym+(_stk&&_stk.name?(' · '+_stk.name):'');
 const _label=segLabel(seg,ctxFor(sym));
 try{document.title=_name+' · '+_label+' · ptrak';}catch(e){}
 try{const _sr=document.getElementById('srlive');if(_sr)_sr.textContent='已切换到 '+_name+' · '+_label;}catch(e){}
}
function activateSeg(seg,opts){
 const r=document.getElementById('right');if(!r)return null;
 let b=r.querySelector('.seg-rail [data-seg="'+seg+'"]')||r.querySelector('.seg-rail button');
 if(!b)return null;
 const chosen=b.dataset.seg;
 // overview panels are lazy-rendered — if the user clicks before idle-prefetch fills this panel, render it now
 if(sel==='__OV__'&&typeof ensureOvPanel==='function')ensureOvPanel(chosen);
 r.querySelectorAll('.seg-rail button').forEach(x=>{const on=x===b;x.classList.toggle('on',on);x.setAttribute('aria-selected',on?'true':'false');});
 if(sel==='__OV__'){try{syncWs(chosen);}catch(e){}}   // level-1 workspace bar follows the active seg (deep links auto-activate the owning workspace)
 r.querySelectorAll('.seg').forEach(p=>{const was=p.hidden;p.hidden=p.dataset.seg!==chosen;if(was&&!p.hidden)p.classList.add('segin');});   // .segin arms the panel-entry settle (CSS, motion block); display none→block restarts it per entry
 if(!routeApplying){rememberSeg(sel==='__OV__'?'ov':'stk',chosen);if(sel==='__OV__')rememberWsSeg(chosen);}   // don't overwrite user's last-chosen tab during back/forward replay
 updateCtx();
 if(!routeApplying)_updateDocTitleAndSR(sel,chosen);   // tab click should refresh window/tab title + SR-announce — applyRoute already covers route-driven changes
 if(!routeApplying){try{_tmLog({type:'tab',sym:sel,seg:chosen});}catch(e){}}
 if(!(opts&&opts.push===false)&&!routeApplying)writeRoute(sel,chosen,true);   // tab clicks are view-state, not pages: replaceState so back walks stock-to-stock
 return chosen;
}
function applyRoute(route,opts){
 const r=cleanRoute(route||{sym:'__OV__'});
 routeApplying=true;
 sel=r.sym;
 renderList();
 activateSeg(r.seg,{push:false});
 routeApplying=false;
 rememberStock(sel);
 if(opts&&opts.replace)writeRoute(sel,activeSeg(),true);
 _updateDocTitleAndSR(r.sym,r.seg);   // route change: update browser-history title + SR announcement (shared helper, also used by activateSeg)
 try{document.body.dataset.view=r.sym==='__OV__'?'ov':'stock';}catch(e){}   // exposes view-type to CSS so mobile can hide portfolio KPIs on stock pages
 // local telemetry: nav event (opt-in via window.ptrak_telemetry_on())
 try{_tmLog({type:'nav', sym:r.sym, seg:r.seg, src: opts&&opts.fromHistory?'back':((opts&&opts.src)||'click')});}catch(e){}
 // focus the active row when applyRoute came from a keyboard nav / back-forward — so Tab order doesn't reset to body
 try{if(opts&&opts.fromHistory){const _t=document.querySelector('.row[data-s="'+(r.sym==='__OV__'?'__OV__':r.sym.replace(/"/g,'\\"'))+'"]');if(_t&&document.activeElement!==_t&&document.body.contains(document.activeElement)&&!_t.contains(document.activeElement))_t.focus({preventScroll:true});}}catch(e){}
 if(opts&&opts.scroll)window.scrollTo({top:0,behavior:(PRM||opts.smooth===false)?'auto':'smooth'});
}
function navTo(sym,seg,opts){
 const r=cleanRoute({sym,seg:seg||defaultSeg(normSym(sym))});
 writeRoute(r.sym,r.seg,opts&&opts.replace);
 applyRoute(r,{scroll:!(opts&&opts.scroll===false),smooth:!(opts&&opts.smooth===false),src:opts&&opts.src});
}
function stockGo(sym,seg,opts){navTo(sym,seg||DEFAULT_SEG.stk,Object.assign({scroll:true},opts||{}));}
function ovGo(seg,opts){navTo('__OV__',seg||lastSeg('ov')||DEFAULT_SEG.ov,Object.assign({scroll:true},opts||{}));}
function goBack(){   // browser-back if we have a real history entry; fall back to overview on cold deep-link
 if(history.state&&history.state.ptrak&&history.state.ptrak.sym!=='__OV__')history.back();
 else if(sel!=='__OV__')ovGo();
}
function adjacentStock(dir){
 const a=filtered().filter(x=>x.sym!=='__OV__');
 if(!a.length)return;
 let i=a.findIndex(x=>x.sym===sel);
 if(i<0)i=dir>0?-1:0;
 const n=a[(i+dir+a.length)%a.length];
 if(n)stockGo(n.sym,sel==='__OV__'?DEFAULT_SEG.stk:activeSeg(),{replace:true,scroll:false,src:'sib'});   // sibling-paging: don't pollute history or scroll-snap
}
function viewBarOverview(){
 const rec=recentStocks().slice(0,5);
 // chips from overview = jumping IN to a stock — pushState (new history entry) is correct
 const chips=rec.map(sym=>`<button class="microbtn recent-chip" onclick="stockGo('${sym}')" aria-label="查看 ${sym}">${sym}</button>`).join('');
 return `<div class="viewbar"><div class="trail"><span class="here">组合总览</span></div>${chips?`<div class="view-actions"><span class="note">最近查看</span>${chips}</div>`:''}</div>`;
}
function viewBarStock(s){
 const rec=recentStocks().filter(x=>x!==s.sym).slice(0,4);
 // chips from a stock view = sibling jumps; use {replace:true} so back still goes to overview, not through chip history
 const chips=rec.map(sym=>`<button class="microbtn recent-chip" onclick="stockGo('${sym}',null,{replace:true,scroll:false})" aria-label="跳转到 ${sym}">${sym}</button>`).join('');
 return `<div class="viewbar"><div class="trail"><button class="microbtn primary" onclick="goBack()" aria-label="返回组合总览">← 组合总览</button><span aria-hidden="true">/</span><span class="here here-sym">${s.sym}</span><span class="note nm-co">${s.name||''}</span></div><div class="view-actions"><button class="microbtn" onclick="adjacentStock(-1)" aria-label="上一只持仓">上一只</button><button class="microbtn" onclick="adjacentStock(1)" aria-label="下一只持仓">下一只</button></div>${chips?`<div class="view-actions" style="flex-basis:100%"><span class="note">最近</span>${chips}</div>`:''}</div>`;
}
function filtered(){
 let a=stocks.filter(s=>filter==='all'||(filter==='held'?s.held:!s.held));
 if(q)a=a.filter(s=>s.sym.toLowerCase().includes(q)||(s.name||'').toLowerCase().includes(q));
 const k=sortKey, fm=s=>(s.fib&&s.fib.now)?s.fib.now.mom:-999;
 const dy=s=>(s.dayChangePct==null?-999:s.dayChangePct);   // unranked stocks fall to bottom for "today's mover" view
 a.sort((x,y)=> k==='sym'? x.sym.localeCompare(y.sym) : (k==='fibmom'? fm(y)-fm(x) : (k==='day'? dy(y)-dy(x) : (y[k]||0)-(x[k]||0))));
 return a;
}
function renderHoldingsList(){   // left-list-only rebuild — used by search/sort/filter so the right panel keeps its SVG/tooltip state
 const a=filtered();
 // announce result count to screen readers when a search query is active (visual users see the list shrink)
 try{if(q){const _sr=document.getElementById('srlive');if(_sr)_sr.textContent=a.length?`${a.length} 个匹配持仓`:'没有匹配的持仓';}}catch(e){}
 const listEl=document.getElementById('list');
 const _ls=listEl?listEl.scrollTop:0;
 const cap=`<div class="list-cap" role="presentation">持仓 · HOLDINGS · ${a.length}</div>`;
 const ov=`<div class="row ovrow ${sel==='__OV__'?'sel':''}" data-s="__OV__" tabindex="0" role="button">
   <div><div class="sym">组合总览</div><div class="meta">净值 & 收益率 vs 指数</div></div>
   <div class="pnl ${cls(S.curReturn)}">${pct(S.curReturn)}<div class="meta">区间收益</div></div></div>`;
 listEl.innerHTML=cap+ov+(a.map(s=>{
  const main=s.held?s.unreal:s.realized, lbl=s.held?'未实现':'已实现';
  const fn=s.fib&&s.fib.now, dot=fn?`<span role="img" aria-label="${FIBLBL[fn.state]||'趋势状态'}" title="${FIBLBL[fn.state]||'趋势状态'}" style="color:var(--mut)">●</span> `:'';
  const zap=fn&&fn.res?`<span role="img" aria-label="${fn.res==='bull'?'多头共振':'空头共振'}" title="${fn.res==='bull'?'多头共振':'空头共振'}" style="color:var(--mut)">⚡</span>`:'';
  const momtxt=fn?` · 动能<span style="color:var(--txt)">${fn.mom>0?'+':''}${fn.mom}</span>`:'';
  // surface today's % move on held names so the trader's morning question ('who moved overnight?') is answerable without sorting
  const showDay=(sortKey==='day'||s.held)&&s.dayChangePct!=null;
  const daytxt=showDay?` · 今日<span class="${cls(s.dayChangePct)}" style="font-weight:600">${s.dayChangePct>0?'+':''}${s.dayChangePct.toFixed(2)}%</span>`:'';
  const fr=finFind(s.sym),fo=finOmitFind(s.sym);
  const fintxt=fr?` · 财务<span style="color:${finScoreColor(fr.finalScore)};font-weight:600">${fr.finalScore??'—'}</span>`:(fo?` · 财务<span style="color:var(--faint)">N/A</span>`:'');
  return `<div class="row ${sel===s.sym?'sel':''}" data-s="${s.sym}" tabindex="0" role="button" aria-current="${sel===s.sym?'page':'false'}">
    <div><div class="sym">${dot}${s.sym} ${zap}${s.hasLegacy?'<span class="legacychip">含旧仓</span>':''}</div>
    <div class="meta">${s.held?fmtN(s.shares)+' 股 @ '+fmt(s.avg):'已清仓 · '+s.numTrades+' 笔'}${momtxt}${daytxt}${fintxt}</div></div>
    <div class="pnl ${cls(main)}">${fmt(main)}<div class="meta">${lbl}</div></div></div>`;}).join('')||`<div class="note" style="padding:16px">没有匹配「${q}」的标的 · 试试清空搜索，或切到上方 持有 / 已清仓 / 全部</div>`);
 document.querySelectorAll('.row').forEach(r=>{r.onclick=()=>{const sym=r.dataset.s;if(sym===sel)return;if(sym==='__OV__')ovGo();else stockGo(sym);};r.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();r.click();}};});
 listEl.scrollTop=_ls;
 if(sel!=='__OV__'&&!window.__ptrakRestoringScroll){const sr=listEl.querySelector('.row[data-s="'+sel+'"]');if(sr&&sr.scrollIntoView)sr.scrollIntoView({block:'nearest'});}
}
function renderList(){   // full re-render (left list + right detail) — used only by applyRoute / sel changes
 renderHoldingsList();
 renderDetail();
}
/* ===== interactive charts: registry + one delegated crosshair controller ===== */
var CHARTREG={},CHARTID=0;
// arm the hero draw-in: set --len so the CSS dasharray trick (MOTION block) has the real path length.
// getTotalLength reads the points attribute, so it works on hidden panels too.
function armDraw(root){(root||document).querySelectorAll('.draw,.draw2').forEach(p=>{try{p.style.setProperty('--len',Math.ceil(p.getTotalLength()));p.addEventListener('animationend',()=>p.classList.add('drawn'),{once:true});}catch(e){}});}   // .drawn gates replay: theater on first reveal, instant on every re-glance
/* ===== shared palette + axis kit (all five SVG builders read these) ===== */
// chart-code palette — token-backed entries DERIVE from :root at boot (one edit moves every surface);
// the chart-only grey ramp lives here and is linted by scripts/_audit_shots.py against drift
const C=(()=>{const cs=getComputedStyle(document.documentElement),v=(t,fb)=>{const x=cs.getPropertyValue(t).trim();return x||fb;};
 return {accent:v('--accent','#E8B339'),ref:v('--accent-ref','#B89030'),green:v('--green','#4FB286'),red:v('--red','#E5707A'),
 mut:v('--mut','#888D96'),mut2:v('--mut2','#B6BAC1'),hair:v('--hair','#1A1C21'),line:v('--line','#23262C'),bg:v('--bg','#060608'),bg2:v('--bg2','#0A0C10'),panel:v('--panel','#121316'),
 subj:'#D9DCE1',price:'#8A8F98',grey:'#6B7079',mid:'#AEB4BE',dim:'#5F6168',slow:'#4B4F58',
 e13:'#8C8A6E',e8w:'#C99A3A',neut:'#9A8A4A'};})();
const CHART_MOB=!!(window.matchMedia&&matchMedia('(max-width:560px)').matches);   // charts rebuild per render; no resize listener needed
const AXM={mL:CHART_MOB?50:66,mR:CHART_MOB?60:82};   // ONE shared time rail for stacked technical charts (chart()'s mR:120 stays — needs 现价 room, never stacked)
// 1/2/5-stepped y ticks: snap the domain outward to clean values (replaces blind ±8% padding)
function niceTicks(min,max,n){
 if(!isFinite(min)||!isFinite(max)){min=0;max=1;}
 if(min===max){min-=1;max+=1;}
 const step0=(max-min)/(n||4),mag=Math.pow(10,Math.floor(Math.log(step0)/Math.LN10)),norm=step0/mag;
 const step=(norm<=1?1:norm<=2?2:norm<=5?5:10)*mag;
 const lo=Math.floor(min/step)*step,hi=Math.ceil(max/step)*step,ticks=[];
 for(let v=lo;v<=hi+step/2;v+=step)ticks.push(Math.abs(v)<step*1e-9?0:v);
 return {min:lo,max:hi,ticks,step};
}
// registration corners: four 6px L-marks in C.line — the engraver's stamp on every plate
function regMarks(x,y,w,h){const L=6,s=C.line;return `<path d="M ${x} ${y+L} V ${y} H ${x+L} M ${x+w-L} ${y} H ${x+w} V ${y+L} M ${x+w} ${y+h-L} V ${y+h} H ${x+w-L} M ${x+L} ${y+h} H ${x} V ${y+h-L}" fill="none" stroke="${s}" stroke-width="1.2"/>`;}
function fd(dt,my){return (my?String(dt.getFullYear()).slice(2)+'/':'')+(dt.getMonth()+1)+'/'+dt.getDate();}   // tooltip date: year prefix only on multi-year windows
function fmtTick(v,step){return v>=1000?v.toLocaleString('en-US',{maximumFractionDigits:0}):v.toFixed(step>=1?0:(step>=0.1?1:2));}
// month-boundary x ticks, year-marked at January (2026/1 — unambiguous, fits the month gap at 11px mono);
// callers fall back to equal-fifths when <2 boundaries fit
function monthTicks(xmin,xmax){
 const out=[],d=new Date(xmin);d.setHours(0,0,0,0);d.setDate(1);
 if(+d<xmin)d.setMonth(d.getMonth()+1);
 while(+d<=xmax){out.push({t:+d,label:d.getMonth()===0?d.getFullYear()+'/1':(d.getMonth()+1)+'/1'});d.setMonth(d.getMonth()+1);}
 return out;
}
function chart(s){
 // empty-data guard: a brand-new ticker, recent delist, or symbol-mapping miss can leave prices=[] and no curPrice → ymin=Infinity → NaN axis labels. Bail with an explainer instead of rendering broken SVG.
 if((!s.prices||!s.prices.length)&&!s.curPrice){
   return `<div class="note" style="padding:16px;line-height:1.7">该标的暂无 Yahoo 价格数据（可能是新上市、退市或代码映射不到）。<br>交易明细仍可在「交易明细」标签查看。</div>`;
 }
 const MOB=CHART_MOB,W=MOB?520:900,H=420,mL=MOB?44:58,mR=MOB?90:120,mT=MOB?14:18,mB=MOB?32:42;
 const prices=s.prices,txns=s.txns;
 const dates=prices.map(p=>+new Date(p[0]));
 let xmin=Math.min(...dates,+new Date(D0)), xmax=+new Date(D1);
 let ys=[...prices.map(p=>p[1])];
 txns.forEach(t=>{if(t.price)ys.push(t.price);if(t.avg)ys.push(t.avg);});
 if(s.curPrice)ys.push(s.curPrice);
 ys=ys.filter(v=>v>0);
 const yt=niceTicks(Math.min(...ys),Math.max(...ys),4);
 let ymin=yt.min,ymax=yt.max;
 const xs=d=>mL+((+new Date(d)-xmin)/(xmax-xmin))*(W-mL-mR);
 const yc=v=>mT+(1-(v-ymin)/(ymax-ymin))*(H-mT-mB);
 let el=`<rect x="${mL}" y="${mT}" width="${W-mL-mR}" height="${H-mT-mB}" fill="${C.bg2}"/>`;   // recessed glass: data sits a step below the papered card
 yt.ticks.forEach(v=>{const y=yc(v);
   el+=`<line x1="${mL}" y1="${y}" x2="${W-mR}" y2="${y}" stroke="${C.hair}"/>`;
   el+=`<text x="${mL-8}" y="${y+4}" fill="${C.mut}" font-size="11" text-anchor="end">$${fmtTick(v,yt.step)}</text>`;});
 let mtx=monthTicks(xmin,xmax);
 if(mtx.length<2)mtx=[0,1,2,3,4,5].map(i=>{const t=xmin+(xmax-xmin)*i/5,dt=new Date(t);return{t,label:(dt.getMonth()+1)+'/'+dt.getDate()};});
 mtx.forEach(g=>{const x=xs(new Date(g.t));
   el+=`<line x1="${x}" y1="${mT}" x2="${x}" y2="${H-mB}" stroke="${C.hair}"/>`;
   el+=`<text x="${x}" y="${H-mB+18}" fill="${C.mut}" font-size="11" text-anchor="middle">${g.label}</text>`;});
 el+=`<rect x="${mL}" y="${mT}" width="${W-mL-mR}" height="${H-mT-mB}" fill="none" stroke="${C.line}"/>`+regMarks(mL,mT,W-mL-mR,(typeof stripH!=='undefined')?(H-mT-mB-stripH):(H-mT-mB));
 if(prices.length){const pts=prices.map(p=>`${xs(p[0]).toFixed(1)},${yc(p[1]).toFixed(1)}`).join(' ');
   el+=`<linearGradient id="pg${CHARTID+1}" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="${C.grey}" stop-opacity="0.08"/><stop offset="1" stop-color="${C.grey}" stop-opacity="0"/></linearGradient>`;
   el+=`<polygon points="${xs(prices[0][0]).toFixed(1)},${H-mB} ${pts} ${xs(prices[prices.length-1][0]).toFixed(1)},${H-mB}" fill="url(#pg${CHARTID+1})"/>`;
   el+=`<polyline class="draw" points="${pts}" fill="none" stroke="${C.price}" stroke-width="2" stroke-opacity="0.9"/>`;}
 const av=txns.filter(t=>t.avg>0&&t.pos>0.0001);
 if(av.length){let d2='',px=null;
   av.forEach((t,i)=>{const x=xs(t.date[0]==='≤'?D0:t.date),y=yc(t.avg);
     if(i===0)d2=`M ${x} ${y}`;else d2+=` L ${px} ${y} L ${x} ${y}`;px=x;});
   d2+=` L ${xs(D1)} ${yc(av[av.length-1].avg)}`;
   el+=`<path class="draw2" d="${d2}" fill="none" stroke="${C.ref}" stroke-width="1.8" stroke-dasharray="5 3" stroke-opacity="0.85"/>`;}
 if(s.curPrice&&s.held){const y=yc(s.curPrice);
   el+=`<line x1="${mL}" y1="${y}" x2="${W-mR}" y2="${y}" stroke="${C.accent}" stroke-width="1"/>`;   // live = SOLID amber rule; reference (cost basis) stays dashed muted amber
   el+=`<text x="${W-mR+6}" y="${y+4}" fill="${C.accent}" font-size="11">现价 $${s.curPrice}</text>`;}
 const cid='c'+(++CHARTID);
 const MY=new Date(xmin).getFullYear()!==new Date(xmax).getFullYear();CHARTREG[cid]={dates:prices.map(p=>+new Date(p[0])),ys:prices.map(p=>yc(p[1])),rows:prices.map(p=>{const dt=new Date(p[0]);const dd=(s.held&&s.curPrice&&p[1])?(s.curPrice/p[1]-1)*100:null;return '<b>'+s.sym+' '+fd(dt,MY)+'</b><br><span class="xr"><i style="background:'+C.price+'"></i>价格 '+fmt(p[1])+'</span>'+(dd!=null?'<span class="xr '+(dd>=0?'pos':'neg')+'">现价较该日 '+(dd>=0?'+':'')+dd.toFixed(1)+'%</span>':'');})};
 el+=`<g class="xg" style="display:none"><line class="cx" x1="0" y1="${mT}" x2="0" y2="${H-mB}"/><circle class="cxd" r="3.4"/></g><rect class="xhit" x="${mL}" y="${mT}" width="${W-mL-mR}" height="${H-mT-mB}" fill="transparent"/>`;
 const amts=txns.filter(t=>t.side!=='OPEN').map(t=>Math.abs(t.amount)),amax=Math.max(...amts,1);
 const RMIN=(window.matchMedia&&window.matchMedia('(pointer:coarse)').matches)?7:4;
 // cluster same-side trades within ~2 trading sessions: a buy CAMPAIGN reads as one sized marker (tooltip lists the legs)
 const _dayMs=86400000,_byside={};
 txns.forEach((t,idx)=>{(_byside[t.side]=_byside[t.side]||[]).push(idx);});
 const mgrp=[];
 Object.values(_byside).forEach(list=>{
   list.sort((a,b)=>+new Date(txns[a].date[0]==='≤'?D0:txns[a].date)-+new Date(txns[b].date[0]==='≤'?D0:txns[b].date));
   let cur=[],last=0;
   list.forEach(idx=>{const d=+new Date(txns[idx].date[0]==='≤'?D0:txns[idx].date);
     if(cur.length&&d-last>2.5*_dayMs){mgrp.push(cur);cur=[];}
     cur.push(idx);last=d;});
   if(cur.length)mgrp.push(cur);});
 const crowded=mgrp.length>12;   // dense campaign charts: smaller caps, quieter fills — the price line wins
 mgrp.forEach(g=>{const t0=txns[g[0]],dd=t0.date[0]==='≤'?D0:t0.date,x=xs(dd);
   const amt=g.reduce((a,i)=>a+Math.abs(txns[i].amount||0),0);
   const qty=g.reduce((a,i)=>a+(txns[i].qty||0),0);
   const wp=qty?g.reduce((a,i)=>a+(txns[i].price||0)*(txns[i].qty||0),0)/qty:t0.price;
   const y=yc(wp||t0.price);
   const r=t0.side==='OPEN'?5:Math.max(RMIN,Math.min(crowded?7:10,4+6.5*Math.sqrt(amt/amax)));
   const col=t0.side==='BUY'?C.green:(t0.side==='SELL'?C.red:C.grey);
   const fo=t0.side==='SELL'?0:(crowded?0.18:0.3),sw=t0.side==='SELL'?2.4:1.2;   // SELL = hollow ring so buy/sell differ by shape, not hue alone (colorblind-safe); BUY translucent so the price line wins
   el+=`<circle cx="${x}" cy="${y}" r="${r}" fill="${col}" fill-opacity="${fo}" stroke="${col}" stroke-width="${sw}" data-g="${g.join(',')}" data-i="${g[0]}" data-sym="${s.sym}" class="mk" style="cursor:pointer"/>`;});
 return `<svg id="${cid}" class="xh" role="img" aria-label="数据图表 · 关键数值见下方表格与徽标" data-x0="${xmin}" data-x1="${xmax}" data-ml="${mL}" data-pw="${W-mL-mR}" viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet">${el}</svg>`;
}
function svgLines(ser,defs,opts){
 const MOB=CHART_MOB,W=MOB?520:900,H=opts.h||340,mL=AXM.mL,mR=AXM.mR,mT=16,mB=MOB?32:40;
 const xmin=+new Date(ser[0].date),xmax=+new Date(ser[ser.length-1].date);
 let vals=[];defs.forEach(d=>ser.forEach(p=>{if(p[d.key]!=null)vals.push(p[d.key]);}));
 if(opts.zero)vals.push(0);
 let ymin=Math.min(...vals),ymax=Math.max(...vals);
 if(opts.fixed){ymin=opts.fixed[0];ymax=opts.fixed[1];}
 const yt=niceTicks(ymin,ymax,4);ymin=yt.min;ymax=yt.max;
 const xs=d=>mL+((+new Date(d)-xmin)/((xmax-xmin)||1))*(W-mL-mR);
 const yc=v=>mT+(1-(v-ymin)/((ymax-ymin)||1))*(H-mT-mB);
 let el=`<rect x="${mL}" y="${mT}" width="${W-mL-mR}" height="${H-mT-mB}" fill="${C.bg2}"/>`;
 (opts.guides||[]).forEach(g=>{const y=yc(g.v);el+=`<line x1="${mL}" y1="${y}" x2="${W-mR}" y2="${y}" stroke="${g.color||'var(--line)'}" stroke-dasharray="4 3"/><text x="${W-mR+4}" y="${y+4}" fill="${g.labelColor||g.color||C.mut}"${g.labelColor?' fill-opacity=".75"':''} font-size="10">${g.label!=null?g.label:g.v}</text>`;});
 (opts.marks||[]).forEach(g=>{const x=xs(g.date),c=g.type==='golden'?C.green:C.red,o=(0.15+0.35*Math.max(0,Math.min(1,(+new Date(g.date)-xmin)/((xmax-xmin)||1)))).toFixed(2);el+=`<line x1="${x}" y1="${mT}" x2="${x}" y2="${H-mB}" stroke="${c}" stroke-opacity="${o}" stroke-dasharray="2 3"/>`;});
 yt.ticks.forEach(v=>{const y=yc(v);
   el+=`<line x1="${mL}" y1="${y}" x2="${W-mR}" y2="${y}" stroke="${C.hair}"/>`;
   el+=`<text x="${mL-8}" y="${y+4}" fill="${C.mut}" font-size="11" text-anchor="end">${opts.fmt(v)}</text>`;});
 el+=`<rect x="${mL}" y="${mT}" width="${W-mL-mR}" height="${H-mT-mB}" fill="none" stroke="${C.line}"/>`+regMarks(mL,mT,W-mL-mR,(typeof stripH!=='undefined')?(H-mT-mB-stripH):(H-mT-mB));
 const n=ser.length;
 let mtx=monthTicks(xmin,xmax);
 if(mtx.length>=2){mtx.forEach(g=>{const x=xs(g.t);el+=`<line x1="${x}" y1="${mT}" x2="${x}" y2="${H-mB}" stroke="${C.hair}"/><text x="${x}" y="${H-mB+18}" fill="${C.mut}" font-size="11" text-anchor="middle">${g.label}</text>`;});}
 else{for(let i=0;i<=5;i++){const idx=Math.round((n-1)*i/5),p=ser[idx],x=xs(p.date),dt=new Date(p.date);
   el+=`<text x="${x}" y="${H-mB+18}" fill="${C.mut}" font-size="11" text-anchor="middle">${dt.getMonth()+1}/${dt.getDate()}</text>`;}}
 if(opts.zero){const y=yc(0);el+=`<line x1="${mL}" y1="${y}" x2="${W-mR}" y2="${y}" stroke="var(--line)" stroke-dasharray="3 3"/>`;}
 const lbls=[];
 defs.forEach(d=>{const f=ser.filter(p=>p[d.key]!=null);
   const pts=f.map(p=>`${xs(p.date).toFixed(1)},${yc(p[d.key]).toFixed(1)}`).join(' ');
   if(opts.area&&f.length){const base=yc(Math.max(ymin,0));
     el+=`<polygon points="${xs(f[0].date).toFixed(1)},${base} ${pts} ${xs(f[f.length-1].date).toFixed(1)},${base}" fill="${d.color}" fill-opacity="0.10"/>`;}
   el+=`<polyline ${d.draw?'class="draw" ':''}points="${pts}" fill="none" stroke="${d.color}" stroke-width="${d.width||2.1}" ${d.dash?`stroke-dasharray="${typeof d.dash==='string'?d.dash:'5 3'}"`:''}/>`;
   const last=f[f.length-1];
   if(last)lbls.push({x:xs(last.date)+5,y:yc(last[d.key])+4,color:d.color,txt:opts.fmt(last[d.key])});});
 // end-label collision relaxation: push successive labels ≥12 units apart, then halo them
 lbls.sort((a,b)=>a.y-b.y);
 for(let i=1;i<lbls.length;i++)if(lbls[i].y-lbls[i-1].y<12)lbls[i].y=lbls[i-1].y+12;
 lbls.forEach(L=>{el+=`<text x="${L.x}" y="${L.y}" fill="${L.color}" font-size="11" font-weight="600" stroke="${C.panel}" stroke-width="3" paint-order="stroke">${L.txt}</text>`;});
 const cid='c'+(++CHARTID),fv=opts.fmt;
 const MY=new Date(xmin).getFullYear()!==new Date(xmax).getFullYear();CHARTREG[cid]={dates:ser.map(p=>+new Date(p.date)),ys:ser.map(p=>p[defs[0].key]!=null?yc(p[defs[0].key]):null),rows:ser.map(p=>{const dt=new Date(p.date);const parts=defs.map(d=>{const v=p[d.key];return '<span class="xr"><i style="background:'+d.color+'"></i>'+(d.label||d.key)+' '+(v==null?'—':fv(v))+'</span>';}).join('');let extra='';if(opts.delta&&p[opts.delta.a]!=null&&p[opts.delta.b]!=null){const dv=p[opts.delta.a]-p[opts.delta.b];extra='<span class="xr xd '+(dv>=0?'pos':'neg')+'">'+opts.delta.label+' '+(dv>=0?'+':'')+dv.toFixed(1)+'pp</span>';}return '<b>'+fd(dt,MY)+'</b><br>'+parts+extra;})};
 el+=`<g class="xg" style="display:none"><line class="cx" x1="0" y1="${mT}" x2="0" y2="${H-mB}"/><circle class="cxd" r="3.4"/></g><rect class="xhit" x="${mL}" y="${mT}" width="${W-mL-mR}" height="${H-mT-mB}" fill="transparent"/>`;
 return `<svg id="${cid}" class="xh" role="img" aria-label="数据图表 · 关键数值见下方表格与徽标" data-x0="${xmin}" data-x1="${xmax}" data-ml="${mL}" data-pw="${W-mL-mR}" viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet">${el}</svg>`;
}
// State ramp: NO pure amber — overheat wears the chart-ramp warm amber (#C99A3A) so the Barometer's
// pure-amber now-tick can never dissolve into an overheat run. ema21-state shares the EMA21 line's
// grey-blue. Product-wide, pure amber now means exactly: masthead tick · barometer now-tick · your live line.
function qStateColor(s){return {trend:'#4FB286',overheat:'#C99A3A',ema8:'#4FB286',ema21:'#AEB4BE',below21:'#E5707A',mixed:'#888D96'}[s]||'#888D96';}
// one source of truth for the QQQ weather chart palette — strokes, end-labels, legend and tooltip all read this
const QTC={close:'#D9DCE1',ema8:'#B89030',ema21:'#AEB4BE',ema34:'#888D96',ema55:'#4B4F58'};   // ema8 = reference amber: indicators never claim the live accent
function qStateLabel(s){return {trend:'多头',overheat:'过热',ema8:'EMA8',ema21:'EMA21',below21:'破21',mixed:'转换'}[s]||s;}
function qqqStrategyChart(q){
 const ser=(q&&q.series)||[];if(!ser.length)return'<div class="note">QQQ 数据不足，无法绘图。</div>';
 const MOB=CHART_MOB,W=MOB?520:900,H=404,mL=AXM.mL,mR=AXM.mR,mT=16,mB=60,stripH=12,stripY=H-mB+24;
 const xmin=+new Date(ser[0].date),xmax=+new Date(ser[ser.length-1].date);
 let vals=[];ser.forEach(p=>['close','ema8','ema21','ema34','ema55'].forEach(k=>{if(p[k]!=null)vals.push(p[k]);}));
 Object.values(q.zones||{}).forEach(z=>(z||[]).forEach(v=>{if(v!=null)vals.push(v);}));
 const yt=niceTicks(Math.min(...vals),Math.max(...vals),4);
 const ymin=yt.min,ymax=yt.max;
 const xs=d=>mL+((+new Date(d)-xmin)/((xmax-xmin)||1))*(W-mL-mR);
 const yc=v=>mT+(1-(v-ymin)/((ymax-ymin)||1))*(H-mT-mB-stripH);
 let el=`<rect x="${mL}" y="${mT}" width="${W-mL-mR}" height="${H-mT-mB-stripH}" fill="${C.bg2}"/>`;
 const zone=(z,c,l)=>{if(!z||z.length<2)return;const y1=yc(Math.max(z[0],z[1])),y2=yc(Math.min(z[0],z[1]));el+=`<rect x="${mL}" y="${y1}" width="${W-mL-mR}" height="${Math.max(1,y2-y1)}" fill="${c}" fill-opacity="0.08"/><text x="${W-mR+4}" y="${(y1+y2)/2+4}" fill="${c}" font-size="10">${l}</text>`;};
 zone((q.zones||{}).ema8Buyback,C.green,'拿回区');zone((q.zones||{}).ema21Options,C.ref,'期权区');zone((q.zones||{}).ema34Defense,C.red,'防线');
 yt.ticks.forEach(v=>{const y=yc(v);el+=`<line x1="${mL}" y1="${y}" x2="${W-mR}" y2="${y}" stroke="${C.hair}"/><text x="${mL-8}" y="${y+4}" fill="${C.mut}" font-size="11" text-anchor="end">$${fmtTick(v,yt.step)}</text>`;});
 let mtx=monthTicks(xmin,xmax);
 if(mtx.length<2)mtx=[0,1,2,3,4,5].map(i=>{const idx=Math.round((ser.length-1)*i/5),p=ser[idx],dt=new Date(p.date);return{t:+new Date(p.date),label:(dt.getMonth()+1)+'/'+dt.getDate()};});
 mtx.forEach(g=>{const x=xs(g.t);el+=`<line x1="${x}" y1="${mT}" x2="${x}" y2="${H-mB-stripH}" stroke="${C.hair}"/><text x="${x}" y="${stripY+stripH+12}" fill="${C.mut}" font-size="11" text-anchor="middle">${g.label}</text>`;});
 el+=`<rect x="${mL}" y="${mT}" width="${W-mL-mR}" height="${H-mT-mB-stripH}" fill="none" stroke="${C.line}"/>`+regMarks(mL,mT,W-mL-mR,(typeof stripH!=='undefined')?(H-mT-mB-stripH):(H-mT-mB));
 const line=(key,col,w,dash,op=1,cls)=>{const pts=ser.filter(p=>p[key]!=null).map(p=>`${xs(p.date).toFixed(1)},${yc(p[key]).toFixed(1)}`).join(' ');el+=`<polyline ${cls?`class="${cls}" `:''}points="${pts}" fill="none" stroke="${col}" stroke-width="${w}" stroke-opacity="${op}" ${dash?'stroke-dasharray="5 3"':''}/>`;};
 line('close',QTC.close,2.35,0,1,'draw');line('ema8',QTC.ema8,2.05);line('ema21',QTC.ema21,1.95);line('ema34',QTC.ema34,1.15,1,.52);line('ema55',QTC.ema55,1.05,1,.44);
 // state strip: consecutive same-state days merge into ONE rect (no corduroy seams, far fewer nodes)
 {let r0=0;for(let i=2;i<=ser.length;i++){if(i===ser.length||ser[i].state!==ser[r0+1].state){const x0=xs(ser[r0].date),x1=xs(ser[i-1].date);el+=`<rect x="${x0}" y="${stripY}" width="${Math.max(1,x1-x0+0.6)}" height="${stripH}" fill="${qStateColor(ser[r0+1].state)}" fill-opacity="0.86"/>`;r0=i-1;}}}
 const last=ser[ser.length-1],lx=xs(last.date);
 el+=`<circle cx="${lx}" cy="${yc(last.close)}" r="4.2" fill="${QTC.close}" stroke="${C.bg}" stroke-width="1"/>`;
 [['QQQ',last.close,QTC.close,-12],['EMA8',last.ema8,QTC.ema8,4],['EMA21',last.ema21,QTC.ema21,16]].forEach(a=>{if(a[1]!=null)el+=`<text x="${Math.min(W-mR+4,lx+8)}" y="${yc(a[1])+a[3]}" fill="${a[2]}" font-size="11" font-weight="600" stroke="${C.panel}" stroke-width="3" paint-order="stroke">${a[0]}</text>`;});
 el+=`<text x="${mL-8}" y="${stripY+stripH-1}" fill="${C.mut}" font-size="10" text-anchor="end">状态</text>`;
 const cid='c'+(++CHARTID);
 const MY=new Date(xmin).getFullYear()!==new Date(xmax).getFullYear();CHARTREG[cid]={dates:ser.map(p=>+new Date(p.date)),ys:ser.map(p=>p.close!=null?yc(p.close):null),rows:ser.map(p=>{const dt=new Date(p.date);return '<b>'+fd(dt,MY)+'</b><br><span class="xr"><i style="background:'+QTC.close+'"></i>QQQ '+fmt(p.close)+'</span><span class="xr"><i style="background:'+QTC.ema8+'"></i>EMA8 '+fmt(p.ema8)+'</span><span class="xr"><i style="background:'+QTC.ema21+'"></i>EMA21 '+fmt(p.ema21)+'</span><span class="xr"><i style="background:'+qStateColor(p.state)+'"></i>'+qStateLabel(p.state)+'</span>';})};
 el+=`<g class="xg" style="display:none"><line class="cx" x1="0" y1="${mT}" x2="0" y2="${H-mB-stripH}"/><circle class="cxd" r="3.4"/></g><rect class="xhit" x="${mL}" y="${mT}" width="${W-mL-mR}" height="${H-mT-mB-stripH}" fill="transparent"/>`;
 return `<svg id="${cid}" class="xh" role="img" aria-label="QQQ EMA strategy chart" data-x0="${xmin}" data-x1="${xmax}" data-ml="${mL}" data-pw="${W-mL-mR}" viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet">${el}</svg>`;
}
function nwChart(ser){
 const MOB=CHART_MOB,W=MOB?520:900,H=346,mL=MOB?50:64,mR=MOB?52:64,mT=16,mB=56,stripH=11,stripY=H-mB+22;
 const cid='c'+(++CHARTID);
 const xmin=+new Date(ser[0].date),xmax=+new Date(ser[ser.length-1].date);
 const vals=ser.map(p=>p.value);
 const yt=niceTicks(Math.min(...vals),Math.max(...vals),4);
 const ymin=yt.min,ymax=yt.max;
 const xs=d=>mL+((+new Date(d)-xmin)/((xmax-xmin)||1))*(W-mL-mR);
 const yc=v=>mT+(1-(v-ymin)/((ymax-ymin)||1))*(H-mT-mB-stripH);
 const base=yc(ymin),col=m=>m>15?C.accent:(m<-15?C.grey:C.neut);   // amber-intensity ramp on the $ surface — green/red reserved for P&L sign (FIBCOL in technical panels keeps its own convention)
 let el=`<rect x="${mL}" y="${mT}" width="${W-mL-mR}" height="${H-mT-mB-stripH}" fill="${C.bg2}"/>`;
 yt.ticks.forEach(v=>{const y=yc(v);
   el+=`<line x1="${mL}" y1="${y}" x2="${W-mR}" y2="${y}" stroke="${C.hair}"/><text x="${mL-8}" y="${y+4}" fill="${C.mut}" font-size="11" text-anchor="end">$${(v/1000).toFixed(yt.step>=1000?0:1)}k</text>`;});
 let mtx=monthTicks(xmin,xmax);
 if(mtx.length<2)mtx=[0,1,2,3,4,5].map(i=>{const t=xmin+(xmax-xmin)*i/5,dt=new Date(t);return{t,label:(dt.getMonth()+1)+'/'+dt.getDate()};});
 mtx.forEach(g=>{const x=xs(g.t);el+=`<line x1="${x}" y1="${mT}" x2="${x}" y2="${H-mB-stripH}" stroke="${C.hair}"/><text x="${x}" y="${stripY+stripH+12}" fill="${C.mut}" font-size="11" text-anchor="middle">${g.label}</text>`;});
 el+=`<rect x="${mL}" y="${mT}" width="${W-mL-mR}" height="${H-mT-mB-stripH}" fill="none" stroke="${C.line}"/>`+regMarks(mL,mT,W-mL-mR,(typeof stripH!=='undefined')?(H-mT-mB-stripH):(H-mT-mB));
 // ONE quiet amber-fade wash under the whole curve — the bottom strip is the sole momentum encoder
 const pts=ser.map(p=>`${xs(p.date).toFixed(1)},${yc(p.value).toFixed(1)}`).join(' ');
 el+=`<linearGradient id="g${cid}" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="${C.accent}" stop-opacity="0.07"/><stop offset="1" stop-color="${C.accent}" stop-opacity="0"/></linearGradient>`;
 el+=`<polygon points="${xs(ser[0].date).toFixed(1)},${base} ${pts} ${xs(ser[ser.length-1].date).toFixed(1)},${base}" fill="url(#g${cid})"/>`;
 el+=`<polyline class="draw" points="${pts}" fill="none" stroke="${C.accent}" stroke-width="1.9"/>`;
 const last=ser[ser.length-1];
 el+=`<text x="${xs(last.date)+6}" y="${yc(last.value)+4}" fill="${C.accent}" font-size="12" font-weight="600" stroke="${C.panel}" stroke-width="3" paint-order="stroke">$${(last.value/1000).toFixed(1)}k</text>`;
 // momentum strip, run-merged: consecutive same-bucket days collapse into one rect
 {const bk=m=>m>15?2:(m<-15?0:1);let r0=0;
  for(let i=2;i<=ser.length;i++){if(i===ser.length||bk(ser[i].pmom)!==bk(ser[r0+1].pmom)){const x0=xs(ser[r0].date),x1=xs(ser[i-1].date);
    el+=`<rect x="${x0}" y="${stripY}" width="${Math.max(1,x1-x0+0.6)}" height="${stripH}" fill="${col(ser[r0+1].pmom)}" fill-opacity="0.9"/>`;r0=i-1;}}}
 el+=`<text x="${mL-8}" y="${stripY+stripH-1}" fill="${C.mut}" font-size="10" text-anchor="end">${MOB?'动能':'组合动能'}</text>`;
 const v0=ser[0].value,mw=m=>m>15?'强':(m<-15?'弱':'中性');
 const MY=new Date(xmin).getFullYear()!==new Date(xmax).getFullYear();CHARTREG[cid]={dates:ser.map(p=>+new Date(p.date)),ys:ser.map(p=>yc(p.value)),rows:ser.map(p=>{const dt=new Date(p.date),d=p.value-v0;return '<b>'+fd(dt,MY)+'</b><br><span class="xr"><i style="background:#E8B339"></i>净值 $'+(p.value/1000).toFixed(1)+'k</span><span class="xr '+(d>=0?'pos':'neg')+'">较起点 '+(d>=0?'+':'')+fmt(d)+' ('+(v0?(d/v0*100).toFixed(1):'0')+'%)</span><span class="xr" style="color:'+col(p.pmom)+'">组合动能 '+mw(p.pmom)+'</span>';})};
 el+=`<g class="xg" style="display:none"><line class="cx" x1="0" y1="${mT}" x2="0" y2="${H-mB-stripH}"/><circle class="cxd" r="3.4"/></g><rect class="xhit" x="${mL}" y="${mT}" width="${W-mL-mR}" height="${H-mT-mB-stripH}" fill="transparent"/>`;
 return `<svg id="${cid}" class="xh" role="img" aria-label="组合净值曲线 · 关键数值见上方与表格" data-x0="${xmin}" data-x1="${xmax}" data-ml="${mL}" data-pw="${W-mL-mR}" viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet">${el}</svg>`;
}
let cfSel=null;
function cfEvents(){return ((DATA.counterfactual||{}).events)||[];}
function cfAgg(){return ((DATA.counterfactual||{}).aggregate)||{};}
function cfActive(){const evs=cfEvents();if(!evs.length)return null;if(!cfSel||!evs.some(e=>e.id===cfSel))cfSel=evs[0].id;return evs.find(e=>e.id===cfSel)||evs[0];}
function cfScoreColor(score){return score==null?C.mut:(score>=70?C.green:(score>=55?C.accent:(score>45?C.mut:(score>30?C.ref:C.red))));}
function cfSignedPct(v,d=1){return v==null?'—':(v>=0?'+':'−')+Math.abs(v).toFixed(d)+'%';}
function cfScoreMeter(sc){
 const score=sc&&sc.score, val=score==null?50:Math.max(0,Math.min(100,score)), col=cfScoreColor(score);
 return `<div class="fbar" title="50 为中性；越高代表真实调仓越优于跳过调仓" style="height:12px;min-width:90px;background:linear-gradient(90deg,rgba(229,112,122,.22),rgba(136,141,150,.16),rgba(79,178,134,.22))"><div class="p" style="left:0;width:${val}%;background:${col};top:2px;height:8px"></div></div>`;
}
function cfUniverseChart(ev){
 const ser=(ev&&ev.series||[]).filter(p=>p.actualPct!=null);
 if(ser.length<2)return '<div class="note">平行宇宙样本不足，无法绘图。</div>';
 return svgLines(ser,[{key:'actualPct',color:C.accent,width:2.35,draw:1,label:'真实宇宙'},{key:'altPct',color:C.mut2,width:1.85,dash:'5 3',label:'未调仓宇宙'}],
   {zero:true,fmt:v=>v.toFixed(1)+'%',delta:{a:'actualPct',b:'altPct',label:'真实 - 平行'}});
}
function cfComparisonTable(evs,activeId){
 const rows=evs.map(e=>{const s=e.summary||{},sc=e.score||{},on=e.id===activeId,score=sc.score,scoreTxt=score==null?'—':Number(score).toFixed(1),scoreCol=cfScoreColor(score);
  return `<tr data-cfrow="${e.id}" tabindex="0" role="button" aria-pressed="${on?'true':'false'}" style="cursor:pointer;${on?'background:rgba(232,179,57,.075)':''}">
    <td class="l"><b>${e.start}</b><br><span class="note">${(e.symbols||[]).join('/')}</span></td>
    <td><span style="color:${scoreCol};font-family:var(--f-mono);font-weight:600">${scoreTxt}</span><br><span class="note">${sc.grade||'—'} · ${sc.outcome||''}</span></td>
    <td class="${cls(s.currentDelta||0)}">${fmt(s.currentDelta||0)}</td>
    <td class="${cls(s.currentDeltaPct||0)}">${ppf(s.currentDeltaPct||0)}</td>
    <td>${cfSignedPct(s.actualPct,1)}</td>
    <td>${cfSignedPct(s.altPct,1)}</td>
    <td class="${cls(sc.impactOnTurnoverPct||0)}">${cfSignedPct(sc.impactOnTurnoverPct,1)}</td>
    <td><span class="note">${s.isTruncated?'截至 '+(s.lastValidDate||'可比日'):'最新'}</span></td>
   </tr>`;}).join('');
 return `<div style="margin-top:12px"><div class="cap" style="margin-bottom:6px">所有平行宇宙收益比较</div>
   <div class="scroll"><table><thead><tr><th class="l">调仓阶段</th><th>评分</th><th>真实−未调仓</th><th>组合差</th><th>真实回报</th><th>未调仓回报</th><th>成交额效率</th><th>口径</th></tr></thead><tbody>${rows}</tbody></table></div></div>`;
}
function counterfactualCard(){
 const evs=cfEvents();
 if(!evs.length)return `<div class="card"><div class="dh"><h2 class="t">平行宇宙回放<span class="t-en">Counterfactual</span></h2><span class="nm">跳过某次调仓，和真实走势同屏比较</span></div><div class="note" style="line-height:1.7">当前交易窗口里没有识别到足够大的调仓阶段。规则：同一阶段内跨标的或买卖并存，且成交额达到组合规模阈值。后续同步有新的交易后会自动出现。<b>非投资建议。</b></div></div>`;
 const ev=cfActive(),s=ev.summary||{},warn=(ev.warnings||[])[0],agg=cfAgg(),esc=ev.score||{};
 const btn=(e,on)=>`<button data-cf="${e.id}" aria-pressed="${on?'true':'false'}" style="padding:5px 10px;border-radius:var(--r-ctl);border:1px solid ${on?'#E8B339':'var(--line)'};background:${on?'rgba(232,179,57,.12)':'transparent'};color:${on?'#E8B339':'var(--txt)'};cursor:pointer;font-size:var(--t-xs);margin:0 6px 6px 0;text-align:left">${e.label}<br><span class="note">${fmt(e.gross)} 成交</span></button>`;
 const deltaCls=cls(s.currentDelta||0),better=(s.currentDelta||0)>=0?'真实宇宙领先':'未调仓宇宙领先';
 const asof=s.isTruncated?`截至可比日 ${s.lastValidDate||''}`:'截至最新';
 const allAgg=agg.allComparable||{},scoreCol=cfScoreColor(esc.score),aggCol=cfScoreColor(agg.score),allCol=cfScoreColor(allAgg.score);
 const aggScope=agg.scope==='latest'?'最新':'可比';
 const aggBadges=[
  [`总调仓评分（${aggScope}）`,agg.score==null?'—':`<span style="color:${aggCol}">${Number(agg.score).toFixed(1)}</span> <span class="note">${agg.grade||''} · ${agg.outcome||''}</span>`],
  [`${aggScope}差额合计`,`<span class="${cls(agg.totalDelta||0)}">${fmt(agg.totalDelta||0)}</span>`],
  ['成交额效率',`<span class="${cls(agg.impactOnTurnoverPct||0)}">${cfSignedPct(agg.impactOnTurnoverPct,1)}</span>`],
  ['胜率',agg.winRate==null?'—':`${agg.winRate.toFixed(1)}% <span class="note">${agg.winCount||0}/${agg.scoredCount||0}</span>`],
  ['覆盖调仓',`${agg.allScoredCount||agg.scoredCount||0} 次 <span class="note">${agg.completeCount||0} 最新 / ${agg.truncatedCount||0} 截至可比日</span>`],
  ['加权收益差',agg.avgSpreadPct==null?'—':`<span class="${cls(agg.avgSpreadPct||0)}">${ppf(agg.avgSpreadPct)}</span>`],
 ];
 if((agg.truncatedCount||0)>0&&allAgg.score!=null)aggBadges.push(['所有可比评分',`<span style="color:${allCol}">${Number(allAgg.score).toFixed(1)}</span> <span class="note">${allAgg.grade||''} · ${fmt(allAgg.totalDelta||0)}</span>`]);
 const badges=[
  ['本次调仓评分',esc.score==null?'—':`<span style="color:${scoreCol}">${Number(esc.score).toFixed(1)}</span> <span class="note">${esc.grade||''} · ${esc.outcome||''}</span>`],
  [asof,`<span class="${deltaCls}">${fmt(s.currentDelta||0)}</span> <span class="note">${better}</span>`],
  ['成交额效率',`<span class="${cls(esc.impactOnTurnoverPct||0)}">${cfSignedPct(esc.impactOnTurnoverPct,1)}</span>`],
  ['真实宇宙',`${(s.actualPct==null?0:s.actualPct).toFixed(1)}%`],
  ['未调仓宇宙',`${(s.altPct==null?0:s.altPct).toFixed(1)}%`],
  ['最大领先/落后',`${fmt(s.bestDelta||0)} · ${fmt(s.worstDelta||0)}`],
  ['分叉日',ev.branchDate||ev.start],
 ];
 const legs=(ev.legs||[]).slice(0,10).map(t=>`<tr><td class="l">${t.date}</td><td class="l">${t.sym}</td><td>${t.side==='BUY'?'<span class="tag b">买入</span>':'<span class="tag s">卖出</span>'}</td><td>${t.qty}</td><td>${fmt(t.amount)}</td></tr>`).join('');
 const wnote=warn?`<div class="note" style="margin-top:8px;color:#C99A3A"><b>边界：</b>${warn.reason||'路径存在不可比边界'}${warn.sym?` · ${warn.sym}`:''}。图中平行线在不可比处停止。</div>`:'';
 return `<div class="card" id="cfcard">
   <div class="dh"><h2 class="t">平行宇宙回放<span class="t-en">Counterfactual</span></h2><span class="nm">如果某个阶段的调仓没有发生，后续走势会怎样</span></div>
   <div class="note" style="margin-bottom:8px;line-height:1.6">选择一个历史调仓阶段：实线=真实宇宙；虚线=跳过该阶段、后续真实交易照常重放的平行宇宙。两条线从分叉日归一到 0%。主总分只统计能一路回放到最新价格的事件；截断事件在表内按最后可比日标注。</div>
   <div class="badges">${aggBadges.map(b=>`<div class="badge"><div class="l">${b[0]}</div><div class="v">${b[1]}</div></div>`).join('')}</div>
   <div style="display:flex;align-items:center;gap:10px;margin:2px 0 12px"><span class="note">总评分刻度</span>${cfScoreMeter(agg)}<span class="note">50 = 中性</span></div>
   <div style="display:flex;flex-wrap:wrap;max-height:128px;overflow:auto;margin:4px 0 8px;padding-top:2px;border-bottom:1px solid var(--hair)">${evs.map(e=>btn(e,e.id===ev.id)).join('')}</div>
   <div class="badges">${badges.map(b=>`<div class="badge"><div class="l">${b[0]}</div><div class="v">${b[1]}</div></div>`).join('')}</div>
   <div style="display:flex;align-items:center;gap:10px;margin:2px 0 10px"><span class="note">本次评分刻度</span>${cfScoreMeter(esc)}<span class="note">成交额效率 = 差额 / 本次成交额</span></div>
   <div class="legend"><span><i class="ln" style="color:#E8B339"></i>真实宇宙</span><span><i class="lnd" style="color:#B6BAC1"></i>未调仓宇宙</span><span class="note">悬停看每日差额</span></div>
   ${cfUniverseChart(ev)}
   ${wnote}
   ${cfComparisonTable(evs,ev.id)}
   <details class="note" style="margin-top:8px"><summary>被跳过的阶段明细</summary><div class="scroll"><table><thead><tr><th class="l">日期</th><th class="l">代码</th><th>方向</th><th>股数</th><th>现金</th></tr></thead><tbody>${legs}</tbody></table></div></details>
   <div class="note" style="margin-top:8px;line-height:1.6"><b>怎么读：</b>评分 50 为中性；高于 50 代表该次真实调仓跑赢“未调仓宇宙”，低于 50 代表拖累。总分和差额是<b>每次单独跳过一个阶段</b>后的独立宇宙汇总，用来评价调仓质量，不是可同时实现的一条组合路径。现金差额随买卖一并重放；不含税费、wash-sale、lot 选择、滑点、期权希腊值与保证金。<b>非投资建议。</b></div>
 </div>`;
}
function wireCounterfactual(){const host=document.getElementById('cfcard');if(!host)return;try{fixThScope(host);}catch(e){}
 const pick=id=>{cfSel=id;host.outerHTML=counterfactualCard();wireCounterfactual();armDraw(document.getElementById('cfcard'));};
 host.querySelectorAll('[data-cf],[data-cfrow]').forEach(el=>{el.onclick=()=>pick(el.dataset.cf||el.dataset.cfrow);el.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();el.click();}};});
}
function rebalDriftMap(){
 const uni=rebalUniverse(),volMap=rebalVolMap(),rule=(typeof rebalDraft!=='undefined'&&rebalDraft)?rebalDraft:rebalLoad();
 const tt=rebalTargets(rule,uni,volMap),m={};
 if(tt&&tt.t&&!tt.disabled){uni.u.forEach((x,i)=>{m[x.sym]={cur:x.w,tgt:tt.t[i],drift:x.w-tt.t[i]};});}
 return {map:m,policy:rule.policy,band:rule.band||5};
}
function structureCard(){
 const X=DATA.alloc;
 if(!X||(!X.byAssetClass.length&&!X.byTheme.length))return '<div class="card"><div class="dh"><h2 class="t">结构</h2></div><div class="note">结构数据不足。</div></div>';
 // Card 1: asset-class structure (always-on floor)
 const acRows=X.byAssetClass.map(b=>{const w=Math.min(b.weightPct,100);
   return `<div class="frow acrow"><span class="fsym">${b.bucket}</span>
     <div class="fbar"><div class="p" style="left:0;width:${w}%;background:${b.isCash?'var(--faint)':'var(--mut)'}"></div></div>
     <span class="acpct">${b.weightPct.toFixed(1)}%</span>
     <span class="note acval">${fmt(b.value)}</span></div>`;}).join('');
 const card1=`<div class="card"><div class="dh"><h2 class="t">资产类别结构</h2><span class="nm">个股 vs 宽基ETF vs 主题ETF vs 杠杆 vs 商品 vs 现金 · 零额外数据，总是可得</span></div>${acRows}</div>`;
 // Card 2: theme/sector structure (weight vs risk)
 const lt=X.largestTheme;
 const callout=lt?`<div class="note" style="margin:4px 0 10px;padding:9px 11px;background:rgba(232,179,57,.07);border-radius:var(--r-card);line-height:1.6"><b>最大主题集中：${lt.theme}</b> · ${lt.weightPct.toFixed(0)}% 资金${lt.riskPct!=null?` / ${lt.riskPct.toFixed(0)}% 风险`:''} · ${lt.n} 只标的。${lt.n>1?`这 ${lt.n} 只高度相关，名义上分散、实为一笔约 ${lt.weightPct.toFixed(0)}% 的单一主题押注。`:''}</div>`:'';
 const tRows=X.byTheme.map(t=>{const grey=t.theme==='未分类'?'color:var(--mut)':'';
   let riskCell='<td>—</td>',gapCell='<td></td><td>—</td>';
   if(t.riskPct!=null){const g=t.gap,gc=g>0?'#E5707A':(g<0?'#4FB286':'var(--mut)'),w=Math.min(Math.abs(g),20)/20*50,left=g>=0?50:50-w;
     riskCell=`<td>${t.riskPct.toFixed(1)}%</td>`;
     gapCell=`<td><div class="fbar"><div class="z"></div><div class="p" style="left:${left}%;width:${w}%;background:${gc}"></div></div></td><td style="color:${gc}">${g>0?'+':''}${g.toFixed(1)}pp</td>`;}
   return `<tr style="${grey}"><td class="l">${t.theme}</td><td><span class="chip">${t.src}</span></td>
     <td>${t.weightPct.toFixed(1)}%</td>${riskCell}${gapCell}<td class="note">${t.n}</td></tr>`;}).join('');
 const ex=(DATA.risk&&DATA.risk.excluded)||[];
 const exNote=ex.length?`<div class="note" style="margin-top:8px">以下标的价格重叠不足 30 日，未计入风险贡献（故各主题风险占比之和可能 <100%）：${ex.join('、')}</div>`:'';
 const card2=`<div class="card"><div class="dh"><h2 class="t">主题 / 行业结构</h2><span class="nm">把相关标的合成一个敞口 · 资金占比 vs 风险占比（复用风险贡献，零重算）</span></div>${callout}
   <div class="scroll"><table><thead><tr><th class="l">主题</th><th>来源</th><th>资金权重</th><th>${gl('rc','风险贡献')}</th><th></th><th title="该主题风险占比减资金占比；正(红)=隐藏的波动放大器、负(绿)=分散器">风险−资金</th><th>只数</th></tr></thead><tbody>${tRows}</tbody></table></div>${exNote}</div>`;
 // Card 3: concentration / effective-N
 const c=X.conc;let badges=[];
 if(c){badges=[['名义持仓数',c.nominalN+' 只'],['集中度指数 '+gl('HHI','HHI')+'（资金）',c.hhiWeight!=null?c.hhiWeight.toFixed(2):'—'],['约等于几个独立押注 ('+gl('effN','有效持仓数')+'·资金)',c.effNWeight!=null?('≈ '+c.effNWeight.toFixed(1)):'—']];
   if(c.effNRisk!=null)badges.push(['主题 HHI（风险）',c.hhiRisk.toFixed(2)],['主题有效持仓数（风险）','≈ '+c.effNRisk.toFixed(1)]);
   badges.push(['名义 → 主题有效',`${c.nominalN} 只 → ≈ ${c.effNWeight!=null?c.effNWeight.toFixed(0):'—'}`]);}
 const p=X.provenance||{};
 const howto=`<div class="note" style="margin-top:10px;line-height:1.6"><b>怎么读：</b>逐只看权重，会把同一驱动的多只标的读成多笔独立小仓；本页做的是<b>你自己不会做的汇总</b>——把同一因子的标的合成<b>一个敞口</b>，同屏看它占多少<b>资金</b>、又占多少<b>风险</b>（Thaler p.1582：逐项割裂 vs 联合判断）。机制：<b>窄框架</b> · <b>相关性忽视</b>（N 只不同代码 ≠ N 笔独立押注）· <b>朴素 1/N 分散</b>与<b>分散幻觉</b>（有效持仓数 ≪ 名义只数）。<br>风险贡献为<b>窗口内已实现风险</b>的描述性分解（小样本、非预测），仅含股票，<b>低估</b>杠杆(TQQQ)与期权真实敞口${DATA.account?` <span class="note-lk" role="button" tabindex="0" onkeydown="if(event.key==='Enter'||event.key==='\ '){event.preventDefault();this.click();}" onclick="ovGo('nw')">→ 去“净值 · 全账户”看期权敞口</span>`:''}。主题标签来源：Yahoo 行业 / 人工标注 / 资产类别${p.asOf&&p.asOf!=='—'?` · 缓存于 ${p.asOf}`:''} · ${p.unclassified||0} 项未分类。结构是“你的钱与波动落在哪里”的描述，<b>不评判任何标的高估/低估</b>（Thaler p.1588）。<b>非投资建议。</b></div>`;
 const card3=`<div class="card"><div class="dh"><h2 class="t">集中度 · 有效持仓数</h2><span class="nm">名义持仓 ≠ 独立押注：主题层 HHI 与有效 N（1/HHI）</span></div>
   <div class="badges">${badges.map(b=>`<div class="badge"><div class="l">${b[0]}</div><div class="v">${b[1]}</div></div>`).join('')}</div>${howto}</div>`;
 return card1+card2+card3;
}
function contributionCard(){
 // pure presentation over the existing payload: per-name unrealized $ = value − cost (broker-exact). NOT a TWR attribution.
 const held=stocks.filter(x=>x.held&&x.unreal!=null);
 if(!held.length)return '<div class="card"><div class="dh"><h2 class="t">盈亏贡献</h2></div><div class="note">暂无持仓未实现盈亏数据。非投资建议。</div></div>';
 const ranked=held.slice().sort((a,b)=>Math.abs(b.unreal)-Math.abs(a.unreal));
 const totUn=held.reduce((a,x)=>a+x.unreal,0);
 const maxAbs=Math.max.apply(null,held.map(x=>Math.abs(x.unreal)))||1;
 const showShare=Math.abs(totUn)>maxAbs;   // only show % when the net dominates the largest single name (else share is sign-mixed nonsense)
 const rows=ranked.slice(0,10).map(x=>{const hw=Math.min(Math.abs(x.unreal)/maxAbs,1)*50,left=x.unreal>=0?50:50-hw,c=x.unreal>0?'#4FB286':(x.unreal<0?'#E5707A':'var(--mut)');
   const share=showShare?`<span class="fst" style="color:${c}">${(x.unreal/totUn*100).toFixed(0)}%</span>`:'<span class="fst"></span>';
   return `<div class="frow" style="cursor:pointer" tabindex="0" role="button" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();this.click();}" onclick="stockGo('${x.sym}')"><span class="fsym">${x.sym}</span><div class="fbar"><div class="z"></div><div class="p" style="left:${left}%;width:${hw}%;background:${c}"></div></div><span class="fval" style="color:${c}">${fmt(x.unreal)}</span>${share}</div>`;}).join('');
 const shareNote=showShare?'右列=占组合未实现盈亏净额的比例。':'各仓盈亏方向不一、净额较小，占比无意义，故只看绝对金额（条形）。';
 return `<div class="card"><div class="dh"><h2 class="t">盈亏贡献 · 谁在拉高 / 拉低</h2><span class="nm">按未实现盈亏绝对额排序（点击看个股）</span></div>
   <div class="legend"><span><i style="background:#4FB286"></i>拉高</span><span><i style="background:#E5707A"></i>拉低</span><span class="note">条形=相对最大贡献的金额</span></div>
   ${rows}
   <div class="note" style="margin-top:8px;line-height:1.6"><b>怎么读：</b>这是<b>未实现盈亏的“金额”贡献</b>（每只 = 当前市值 − 成本），看谁在把组合拉高、谁在拖累。${shareNote}<b>这不是时间加权(TWR)归因</b>——真正的 TWR 拆解需要每日持仓权重，本导出无法重建，故此处只用金额。<b>非投资建议。</b></div></div>`;
}
function scorecardCard(){
 const mv=S.marketValue||1;
 const bias=(DATA.behavior&&DATA.behavior.biasBySym)||{};
 const rc={};((DATA.risk&&DATA.risk.contrib)||[]).forEach(c=>rc[c.sym]=c);
 const dm=rebalDriftMap();
 const BL={concentration:'集中',disposition:'浮亏持有',sunkcost:'越跌越买',recency:'追涨',anchoring:'回本卖'};
 const held=stocks.filter(x=>x.held&&x.fib&&x.fib.now);
 if(!held.length)return'<div class="card"><div class="dh"><h2 class="t">决策一览</h2></div><div class="note">暂无可汇总的持仓信号：每只持仓需 ≥21 个交易日的价格样本才能计算技术姿态。新加入的持仓会在样本积累后出现于此。本表为客观汇总，非投资建议。</div></div>';
 const data=held.map(x=>{
   const w=x.value/mv*100,n=x.fib.now,sig=(x.fib.signals||[]).slice(-1)[0],c=rc[x.sym],bl=bias[x.sym]||[],d=dm.map[x.sym];
   let attn=0;
   if(n.rsi>70)attn++;
   if(c&&c.gap>10)attn++;
   attn+=bl.filter(b=>b.level==='alert').length;
   if(d&&Math.abs(d.drift)>dm.band)attn++;
   const sigTxt=sig?(sig.type==='golden'?`<span class="pos">金叉 ${sig.date.slice(5)}</span>`:`<span class="neg">死叉 ${sig.date.slice(5)}</span>`):'—';
   const rsiCls=n.rsi>70?'neg':(n.rsi<30?'pos':'');
   let riskCell='<td class="cell-empty">—</td><td class="cell-empty">—</td>';
   if(c){const g=c.gap,gc=g>0?'#E5707A':(g<0?'#4FB286':'var(--mut)'),bw=Math.min(Math.abs(g),20)/20*50,left=g>=0?50:50-bw;
     // zero-width bar carries no signal: show a muted dash instead of an empty track
     const bar=bw>0?`<div class="fbar"><div class="z"></div><div class="p" style="left:${left}%;width:${bw}%;background:${gc}"></div></div>`:'<span class="cell-empty">—</span>';
     riskCell=`<td>${c.riskPct.toFixed(1)}%</td><td>${bar}</td>`;}
   const biasHtml=bl.length?bl.map(b=>{const col=b.level==='alert'?'#E5707A':'#B89030';return `<span class="chip" style="color:${col};border-color:${chipBd(col)}">${BL[b.id]||b.id}</span>`;}).join(' '):'<span class="note cell-empty">—</span>';
   let driftCell='<td class="cell-empty">—</td>';
   if(d){const g=d.drift,gc=g>0?'#E5707A':(g<0?'#4FB286':'var(--mut)'),bw=Math.min(Math.abs(g),15)/15*50,left=g>=0?50:50-bw;
     driftCell=bw>0?`<td><div class="fbar"><div class="z"></div><div class="p" style="left:${left}%;width:${bw}%;background:${gc}"></div></div></td>`:'<td class="cell-empty">—</td>';}
   const attnBadge=attn>0?`<span class="chip" style="color:#E5707A;border-color:var(--chip-bd-red)">${attn}</span>`:'<span class="note cell-empty">—</span>';
   // Mobile drops 7 columns, leaving 关注度 as the rightmost (often offscreen). Inline a small attention chip next to the ticker so the priority signal travels with the symbol.
   const attnInline=attn>0?` <span class="chip attn-inline" style="color:#E5707A;border-color:var(--chip-bd-red)" aria-label="关注度 ${attn}">${attn}🚩</span>`:'';
   const html=`<tr style="cursor:pointer" tabindex="0" role="button" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();this.click();}" onclick="stockGo('${x.sym}')">
     <td class="l"><span style="color:${FIBCOL[n.state]}">●</span> ${x.sym}${attnInline}</td>
     <td>${fmt(x.value)} <span class="note">${w.toFixed(1)}%</span></td>
     <td class="${cls(x.unrealPct)}">${x.unrealPct>=0?'+':''}${x.unrealPct.toFixed(1)}%</td>
     <td style="color:${FIBCOL[n.state]}">${n.label}</td>
     <td style="color:${momColor(n.mom)}">${n.mom>0?'+':''}${n.mom}</td>
     <td class="${rsiCls}">${n.rsi}</td><td>${sigTxt}</td>${riskCell}
     <td class="l">${biasHtml}</td>${driftCell}<td>${attnBadge}</td></tr>`;
   // phone: same data as one stacked card — nothing scrolls off-screen, signals travel with the symbol
   const card=`<div class="smc" role="button" tabindex="0" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();this.click();}" onclick="stockGo('${x.sym}')">
     <div class="smc-top"><span style="color:${FIBCOL[n.state]}">●</span> <b class="smc-sym">${x.sym}</b>${attn>0?`<span class="chip attn-inline" style="display:inline-block;color:#E5707A;border-color:var(--chip-bd-red)" aria-label="关注度 ${attn}">${attn}🚩</span>`:''}<span class="smc-state" style="color:${FIBCOL[n.state]}">${n.label}</span><span class="smc-pnl ${cls(x.unrealPct)}">${x.unrealPct>=0?'+':''}${x.unrealPct.toFixed(1)}%</span></div>
     <div class="smc-mid"><span style="white-space:nowrap">${fmt(x.value)}</span> · <span style="white-space:nowrap">${w.toFixed(1)}% 仓位</span> · <span style="white-space:nowrap">动能 <span style="color:${momColor(n.mom)}">${n.mom>0?'+':''}${n.mom}</span></span> · <span style="white-space:nowrap">RSI <span class="${rsiCls}">${n.rsi}</span></span>${sig?` · <span style="white-space:nowrap">${sigTxt}</span>`:''}</div>
     ${bl.length?`<div class="smc-bias">${biasHtml}</div>`:''}</div>`;
   return {attn,w,html,card};
 });
 data.sort((a,b)=>b.attn-a.attn||b.w-a.w);
 return `<div class="card t1"><div class="dh"><h2 class="t">决策一览<span class="t-en">Decision Board</span></h2><span class="nm">每只持仓的 技术 · 风险 · 行为 · 再平衡偏离 同屏，联合评估而非逐项割裂（点击看个股）</span></div>
   <div class="scroll score-tablewrap"><table><thead><tr><th class="l">代码</th><th>市值/权重</th><th>未实现%</th><th>状态</th><th>${gl('mom','动能')}</th><th>${gl('rsi','RSI')}</th><th>${gl('cross','最近信号')}</th><th>${gl('rc','风险贡献')}</th><th title="该标的风险占比减资金占比，正(红)=隐藏的波动放大器、负(绿)=分散器">风险−资金</th><th class="l">行为标记</th><th title="按你在再平衡计划里设定的区间，当前权重偏离目标多少">偏离目标</th><th>${gl('attn','关注度')}</th></tr></thead>
   <tbody>${data.map(r=>r.html).join('')}</tbody></table></div>
   <div class="score-cards" aria-label="决策一览（手机版卡片）">${data.map(r=>r.card).join('')}</div>
   <details class="note" style="margin-top:8px;line-height:1.6"><summary style="cursor:pointer">怎么读 · 口径说明</summary><b>怎么读：</b>本表把已有的四类信号<b>汇总同屏</b>，让你把每只仓位放回整个组合里联合判断（Thaler：避免逐项割裂导致的被支配选择，p.1582）。“关注度”是<b>四类红色信号的计数</b>（默认排序键），<b>不是评分、更不是买卖建议</b>——技术姿态为客观描述，行为标记来自你自己的交易，偏离目标按“再平衡计划”里你自己设定的规则计算。缺数据的单元显示“—”。<b>非投资建议。</b></details></div>`;
}
function wholeAccountCard(){
 const A=DATA.account;if(!A)return'';
 const tiles=[['全账户净值',fmt(A.netWorthWhole)],['股票',fmt(A.equity)],['现金',fmt(A.cashTotal)],
   ['期权净·市价',`<span class="${cls(A.optMarkNet)}">${fmt(A.optMarkNet)}</span>`],['待结算',fmt(A.pending)]];
 const W=A.netWorthWhole||1,seg=(amt,c)=>amt>0?`<div style="width:${amt/W*100}%;background:${c}"></div>`:'';
 const bar=`<div style="display:flex;height:14px;border-radius:6px;overflow:hidden;margin:10px 0 6px">${seg(A.equity,'#E8B339')}${seg(A.cashTotal,'#888D96')}${seg(Math.max(0,A.optMarkNet),'#7F8794')}${seg(A.pending,'#555A63')}</div>`;
 const cashRows=A.cash.map(c=>`<tr><td class="l">${c.acct}</td><td class="l">${c.sym}</td><td style="text-align:right">${fmt(c.value)}</td></tr>`).join('');
 return `<div class="card t1">
   <div class="dh"><h2 class="t">全账户净值<span class="t-en">Net Worth</span></h2><span class="nm">截至 ${A.asOf} · 股票＋现金＋期权按市价＋待结算（券商精确美元口径，非敞口/杠杆口径）</span></div>
   <div class="badges">${tiles.map(t=>`<div class="badge"><div class="l">${t[0]}</div><div class="v">${t[1]}</div></div>`).join('')}</div>
   ${bar}
   <div class="legend"><span><i style="background:#E8B339"></i>股票</span><span><i style="background:#888D96"></i>现金</span><span><i style="background:#7F8794"></i>期权净</span><span><i style="background:#555A63"></i>待结算</span></div>
   <div class="note" style="line-height:1.6;margin-top:6px">恒等式：股票 ${fmt(A.equity)} ＋ 现金 ${fmt(A.cashTotal)} ＋ 期权净市价 ${fmt(A.optMarkNet)} ＋ 待结算 ${fmt(A.pending)} ＝ 全账户净值 ${fmt(A.netWorthWhole)}（四项皆券商当前价值，逐项相加，精确）。这是“净值”口径，不是“敞口/杠杆”口径——后者本导出无法计算（见下方期权敞口卡片）。</div>
   <div class="scroll" style="margin-top:8px"><table><thead><tr><th class="l">账户</th><th class="l">货币基金</th><th style="text-align:right">当前价值</th></tr></thead><tbody>${cashRows}<tr style="border-top:1px solid #1A1C21"><td class="l"><b>合计</b></td><td></td><td style="text-align:right"><b>${fmt(A.cashTotal)}</b></td></tr></tbody></table></div>
   <div class="note" style="margin-top:6px">现金仅含 Fidelity 货币基金核心(正)余额；<b>保证金借记(margin debit)余额不在本导出中</b>——若存在借记，本净值可能高估你的可动用现金。<b>非投资建议。</b></div>
 </div>`;}
function optionsExposureCard(){
 const A=DATA.account;if(!A||!A.optLegs.length)return'';
 const legs=A.optLegs.slice().sort((a,b)=>Math.abs(b.mark)-Math.abs(a.mark));
 const maxAbs=Math.max(1,...legs.map(l=>Math.abs(l.mark)));
 const dol=(v,c)=>`<span style="display:inline-block;width:92px;text-align:right;color:${c};font-variant-numeric:tabular-nums">${fmt(v)}</span>`;
 const leg=l=>{const c=l.mark<0?'#E5707A':'#4FB286',wd=Math.abs(l.mark)/maxAbs*50,left=l.mark>=0?50:50-wd;
   return `<div class="frow"><span class="fsym" style="width:auto;min-width:150px"><span class="chip" style="color:${c};border-color:${c}55">${l.side==='short'?'空头':'多头'}</span> ${l.sym.trim()}</span>
     <div class="fbar"><div class="z"></div><div class="p" style="left:${left}%;width:${wd}%;background:${c}"></div></div>${dol(l.mark,c)}</div>`;};
 const legsHtml=legs.map(leg).join('')+`<div class="frow" style="border-top:1px solid #1A1C21;margin-top:4px;padding-top:6px"><span class="fsym" style="width:auto;min-width:150px"><b>净市价小计</b></span><div class="fbar"></div>${dol(A.optMarkNet,A.optMarkNet<0?'#E5707A':'#4FB286')}</div>`;
 const tbl=`<div class="scroll" style="margin-top:10px"><table><thead><tr><th class="l">方向</th><th class="l">合约</th><th class="l">数量</th><th style="text-align:right">当前市价</th><th class="l">账户</th></tr></thead><tbody>${legs.map(l=>`<tr><td class="l">${l.side==='short'?'空头':'多头'}</td><td class="l">${l.name}</td><td class="l">${fmtN(l.qty,0)}</td><td style="text-align:right" class="${cls(l.mark)}">${fmt(l.mark)}</td><td class="l">${l.type||'—'}</td></tr>`).join('')}</tbody></table></div>`;
 return foldCard('期权敞口（隐藏杠杆）',`毛额 ${fmt(A.optMarkGross)} ≈ 权益 ${A.optPctEquity}% · 净市价 ${fmt(A.optMarkNet)} · ${A.optLegs.length} 条腿`,`
   <div class="hero-fig" style="margin:6px 0 2px">期权总市值（毛额/GROSS） ${fmt(A.optMarkGross)} <span style="font-size:var(--t-base);color:var(--mut)">≈ 权益的 ${A.optPctEquity}%</span></div>
   <div class="note" style="margin-bottom:8px">净市价仅 <span class="${cls(A.optMarkNet)}">${fmt(A.optMarkNet)}</span> —— 这是<b>盈亏口径</b>，不是你的敞口（小净值会掩盖大总额，Thaler p.1582）。</div>
   ${(A.optionSpreads&&A.optionSpreads.length)?`<div class="note" style="margin-bottom:8px">其中可配对的垂直价差，理论盈亏被<b>价差宽度</b>封顶（合计约 ${fmt(A.optionSpreads.reduce((s,x)=>s+(x.widthValue||0),0))}，远小于上面的毛额）——这部分是<b>定义化风险</b>，详见下方 <b>Spread Risk Ledger</b>；未配对的单腿真实杠杆才是毛额低估的来源。</div>`:''}
   ${legsHtml}${tbl}
   <div class="note" style="margin-top:10px;padding:9px 11px;background:rgba(232,179,57,.07);border-radius:var(--r-card);line-height:1.65"><b>重要：</b>以上为<b>市价(mark-to-market)</b>，不是 delta、不是名义本金(notional)、不是希腊字母(Greeks)——本 CSV 只给“当前市值”。<b>真实杠杆比这更大，且在此无法计算。</b>保证金借记(margin debit)余额不在本导出中（只显示正的货币基金现金），故“全账户净值”若存在借记可能高估可动用现金。${(()=>{const a=[...new Set(A.optLegs.map(l=>l.type||'—'))];return A.optLegs.length+' 条期权腿，'+(a.length===1?'均在 <b>'+a[0]+'</b> 账户':'分布于 <b>'+a.join(' / ')+'</b> 账户');})()}。<b>非投资建议。</b></div>
   <div class="note" style="margin-top:6px">Thaler 2016 · p.1582 / p.1589 / p.1594</div>`);}
function optionsSpreadLedgerCard(){
 const A=DATA.account;if(!A||!A.optLegs||!A.optLegs.length)return'';
 const spreads=A.optionSpreads||[];
 const warnN=spreads.reduce((n,s)=>n+((s.warnings&&s.warnings.length)?1:0),0);
 if(!spreads.length)return `<div class="card"><div class="dh"><h2 class="t">全账户 Spread Risk Ledger</h2><span class="nm">自动配对垂直价差失败时，先按单腿管理风险</span></div>
   <div class="note">当前有 ${A.optLegs.length} 条期权腿，但没有自动识别出同标的 / 同到期 / 同 CALL-PUT 类型的一多一空垂直价差。请按单腿查看 liquidity、assignment、margin 与最大风险；不要把未配对 short option 当作 defined-risk spread。<b>非投资建议。</b></div></div>`;
 const rows=spreads.map(s=>{const er=s.expectedRange||[null,null],bad=s.warnings&&s.warnings.length;
   const warn=bad?`<span class="chip" style="color:#E5707A;border-color:var(--chip-bd-red)">核对</span> ${s.warnings.join('；')}`:`<span class="chip" style="color:#4FB286;border-color:var(--chip-bd-green)">范围内</span> 仍需实盘 bid/ask/Greeks`;
   return `<tr><td class="l">${s.underlying}<br><span class="note">${s.kind}</span></td><td>${s.expiry}<br><span class="note">${s.dte==null?'—':s.dte+'d'}</span></td><td>${s.longStrike}/${s.shortStrike} ${s.right}</td><td>${fmt(s.widthValue)}</td><td class="${cls(s.netMark)}">${fmt(s.netMark)}</td><td>${fmt(er[0])} - ${fmt(er[1])}</td><td class="l">${warn}</td></tr>`;}).join('');
 return foldCard('全账户 Spread Risk Ledger',`${spreads.length} 组价差${warnN?` · ${warnN} 个需核对`:' · 全部范围内'}`,`
   <div class="scroll"><table><thead><tr><th class="l">结构</th><th>到期/DTE</th><th>腿</th><th>宽度</th><th>当前净Mark</th><th>理论范围</th><th class="l">风险提示</th></tr></thead><tbody>${rows}</tbody></table></div>
   <div class="note" style="margin-top:10px;line-height:1.6">这是从券商持仓名、数量、方向保守识别出的垂直价差。它只能检查<b>当前市价是否落在理论区间</b>，不能替代 entry debit/credit、Delta/Gamma、实时盘口和流动性。老师视角：先确定结构是不是 defined risk，再决定能不能移动止盈、平仓、滚仓或拆腿。<b>非投资建议。</b></div>`);}
function diagnosticsCard(){
 const mode=S.priceMode==='mark-to-market'?'Yahoo mark-to-market':'Fidelity snapshot';
 const refreshed=S.refreshedPriceCount!=null?`${S.refreshedPriceCount}/${S.numHeld} held prices refreshed`:'—';
 return `<div class="card"><details><summary>数据新鲜度 / 口径诊断</summary>
   <div class="badges" style="margin-top:10px">
    <div class="badge"><div class="l">生成时间</div><div class="v" style="font-size:var(--t-base)">${S.generatedAt||'—'}</div></div>
    <div class="badge"><div class="l">价格口径</div><div class="v" style="font-size:var(--t-base)">${mode}</div></div>
    <div class="badge"><div class="l">价格 as of</div><div class="v">${S.priceAsOf||S.dateRange[1]}</div></div>
    <div class="badge"><div class="l">刷新覆盖</div><div class="v" style="font-size:var(--t-base)">${refreshed}</div></div>
    <div class="badge"><div class="l">数据窗口</div><div class="v" style="font-size:var(--t-base)">${D0} → ${D1}</div></div>
   </div>
   <div class="note" style="margin-top:8px;line-height:1.6">普通 <b>sync</b> 会要求市值/未实现与 Fidelity CSV 完全一致；每日 <b>mark-to-market</b> 则用最新 Yahoo 股票价重估持仓，现金和期权仍保留 Fidelity 快照。若自动刷新失败，先看 <code>output/price_refresh.log</code> 与 <code>output/price_refresh_log.json</code>。<b>非投资建议。</b></div>
  </details></div>`;
}
function bridgeCard(){
 const B=DATA.bridge;if(!B||B.terminal==null)return'';
 const g=S.behaviorGap;
 const maxAbs=Math.max(1,...B.legs.map(l=>Math.abs(l.amount)));
 const leg=l=>{const c=l.amount>0?'#4FB286':(l.amount<0?'#E5707A':'#888D96');
   const wd=Math.min(Math.abs(l.amount),maxAbs)/maxAbs*50,left=l.amount>=0?50:50-wd;
   const chip=l.acc?` <span class="chip" style="opacity:.85">${l.acc}</span>`:'';
   return `<div class="frow"><span class="fsym">${l.label}${chip}</span>
     <div class="fbar"><div class="z"></div><div class="p" style="left:${left}%;width:${wd}%;background:${c}"></div></div>
     <span class="fval" style="color:${c}">${fmt(l.amount)}</span></div>`;};
 const legsHtml=B.legs.map(leg).join('')+`<div class="frow" style="border-top:1px solid #1A1C21;margin-top:4px;padding-top:6px"><span class="fsym"><b>合计盈亏</b></span><div class="fbar"></div><span class="fval ${cls(B.totalPL)}"><b>${fmt(B.totalPL)}</b></span></div>`;
 const badges=[['当前市值',fmt(B.terminal)],['持仓成本',fmt(B.heldCost)],
   ['合计盈亏（美元）',`<span class="${cls(B.totalPL)}">${fmt(B.totalPL)}</span>`],
   ['你的钱实际回报 '+gl('MWR','MWR'),S.mwrPeriod==null?'—':`<span class="${cls(S.mwrPeriod)}">${pct(S.mwrPeriod)}</span>`]];
 let lv,head,detail;
 if(g==null){lv=['#888D96','提示'];head='资金加权收益暂不可用';detail='IRR 在该现金流形态下无定义（现金流未变号或未收敛），故显示破折号。';}
 else if(g>0){lv=['#E8B339','留意'];head='你的钱表现弱于策略本身 · 典型行为缺口';detail=`你的钱（资金加权 ${pct(S.mwrPeriod)}）落后于策略本身（时间加权 ${pct(S.curReturn)}）。机制上倾向在走平或下跌前加仓——这正是经典的“行为缺口”：追近期强势（过度外推）或下跌后犹豫（近视损失厌恶）。`;}
 else if(g<0){lv=['#4FB286','良好'];head='你的钱表现优于策略本身';detail=`你的钱（资金加权 ${pct(S.mwrPeriod)}）跑赢了策略本身（时间加权 ${pct(S.curReturn)}）。机制上你净在上涨前加仓，本窗口择时较好——但单一窗口是小样本，可能含运气成分，别据此过度自信。`;}
 else {lv=['#888D96','提示'];head='加仓节奏基本中性';detail='本窗口入金时点对结果影响不大。';}
 const tip='💡 TWR 评判你的选股 / 策略本身，MWR + 本金桥评判你的钱实际经历，指数对比看相对大盘——三者回答的是不同问题。';
 const link=`<span class="note-lk" role="button" tabindex="0" onkeydown="if(event.key==='Enter'||event.key==='\ '){event.preventDefault();this.click();}" onclick="ovGo('rebal')">看到不喜欢的缺口？趁冷静先去“再平衡计划”给自己预设一条规则。</span>`;
 const gapCard=`<div class="card" style="border-left:3px solid ${lv[0]}">
   <div class="dh"><h2 class="t">${head}</h2><span class="chip" style="color:${lv[0]};border-color:${lv[0]}66">${lv[1]}</span></div>
   <div class="note" style="color:var(--txt);line-height:1.6;margin-top:4px">${detail}</div>
   <div style="margin-top:9px;padding:9px 11px;background:rgba(232,179,57,.07);border-radius:var(--r-card);line-height:1.6">${tip}<br>${link}</div>
   <div class="note" style="margin-top:6px">Thaler 2016 · p.1582 / p.1592</div></div>`;
 return `<div class="card">
   <div class="dh"><h2 class="t">真金白银桥</h2><span class="nm">本金 → 盈亏 → 当前市值（美元口径，回避百分比错觉）</span></div>
   <div class="badges">${badges.map(b=>`<div class="badge"><div class="l">${b[0]}</div><div class="v">${b[1]}</div></div>`).join('')}</div>
   <div class="note" style="margin:8px 0 6px;line-height:1.6">恒等式：持仓成本 ${fmt(B.heldCost)} ＋ 未实现 ＝ 当前市值 ${fmt(B.terminal)}（券商口径，精确）。下面按<b>美元</b>拆出你的盈亏分桶（同一零线、同一刻度，等额盈亏长度相同、不放大亏损）；未实现已在当前市值里，已实现/股息/期权为已落袋现金，故并列展示、不叠加进市值。</div>
   ${legsHtml}
   <div class="note" style="margin-top:10px;line-height:1.6">仅股票口径（不含现金/保证金，为资金加权<b>权益账面</b> XIRR，非账户级）。未实现为券商精确，已实现为窗口内均价口径（含估算）；两者时间口径不同（未实现含建仓前浮盈，已实现仅本窗口）。累计入金约 ${fmt(B.lifeDeposits)}（净值还含“开始追踪入金前”已持有的仓位，故高于入金额）。单一窗口为小样本、描述性非预测。<b>非投资建议。</b></div>
 </div>${gapCard}`;
}
function insightBanner(){
 let dismissed=false;try{dismissed=localStorage.getItem('ptrak.onboard.v1')==='done';}catch(e){}
 const guide=dismissed?`<span class="ib-lk" role="button" tabindex="0" onclick="try{localStorage.removeItem('ptrak.onboard.v1')}catch(e){};renderOverview()">显示新手指南</span>`:'';
 const A=DATA.account,X=DATA.alloc,Q=DATA.qqqTqqq,pr=S.curReturn,sp=S.spReturn,nq=S.nasdaqReturn,gap=S.behaviorGap;
 const al=(sp!=null)?pr-sp:null,lt=X&&X.largestTheme,lk=(seg,t)=>`<span class="ib-lk" role="button" tabindex="0" onclick="ovGo('${seg}')">${t} →</span>`;
 const stockChip=sym=>`<span class="ib-lk" role="button" tabindex="0" onclick="stockGo('${sym}')">${sym}</span>`;   // no trailing arrow; the surrounding sentence supplies the action verb
 // HERO VERDICT: one sentence, one primary action. Severity comes from count of attention items.
 const tr=(DATA.risk&&DATA.risk.contrib&&DATA.risk.contrib[0])?DATA.risk.contrib[0].sym:null;
 const heldStocks=stocks.filter(s=>s.held);
 const bigDownToday=heldStocks.filter(s=>s.dayChangePct!=null&&s.dayChangePct<-3).length;
 const deepLoss=heldStocks.filter(s=>s.unrealPct!=null&&s.unrealPct<-15).length;
 const techBear=heldStocks.filter(s=>s.fib&&s.fib.now&&s.fib.now.state==='down').length;   // Python emits up/down/range/mixed — 'bear-trend' never matched, so 技术转空 silently counted 0
 const attnTotal=bigDownToday+deepLoss+techBear;
 let heroTone, heroTxt;
 if(attnTotal===0){heroTone='var(--green)'; heroTxt=`今日无紧急关注 · 区间收益 <b class="${cls(pr)}">${pct(pr)}</b>${al!=null?` · ${al>=0?'跑赢':'跑输'}标普 ${ppf(al)}`:''}`;}
 else if(attnTotal<=2){heroTone='var(--amber-line)'; heroTxt=`${attnTotal} 件需要看${tr?` · 优先 ${stockChip(tr)}`:''} · ${bigDownToday?bigDownToday+' 只今日大跌·':''}${deepLoss?deepLoss+' 只深度亏损·':''}${techBear?techBear+' 只技术转空':''}`.replace(/·$/,'').replace(/·\s*$/,'');}
 else {heroTone='var(--red)'; heroTxt=`${attnTotal} 件需要看${tr?` · 优先 ${stockChip(tr)}`:''} → 建议先 ${lk('score','按关注度排序')}`;}
 // one Barometer, one home (the masthead band) — the hero row carries only its text echo
 const qSer=(Q&&Q.available&&Q.series)?Q.series.slice(-60):[];
 let ribbon='';
 if(qSer.length>5){
  const _st=qSer[qSer.length-1].state;
  ribbon=`<span style="margin-left:auto;flex:none;font:600 10px/1 var(--f-mono);letter-spacing:.04em;color:var(--faint)" title="详见页顶气压带 · 点击带子打开决策台">QQQ 60D · <span style="color:${qStateColor(_st)}">${qStateLabel(_st)}</span></span>`;
 }
 // 今日先看: top movers by |dayChange| ride the hero row (tap a chip to drill in)
 const movers=heldStocks.filter(s=>s.dayChangePct!=null).sort((a,b)=>Math.abs(b.dayChangePct)-Math.abs(a.dayChangePct)).slice(0,3);
 const moversChips=movers.map(s=>`<span class="chip" style="cursor:pointer" role="button" tabindex="0" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();this.click();}" onclick="stockGo('${s.sym}')">${s.sym} <span class="${cls(s.dayChangePct)}">${s.dayChangePct>0?'+':''}${s.dayChangePct.toFixed(2)}%</span></span>`).join(' ');
 const hero=`<div class="ib-hero" style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;padding:12px 14px;margin:0 -6px 4px;background:var(--panel2);border-radius:4px"><span style="width:3px;height:17px;border-radius:1px;background:${heroTone};flex:none" aria-hidden="true"></span><span class="verdict" style="flex:1 1 auto;min-width:200px">${heroTxt}</span>${moversChips?`<span style="display:inline-flex;align-items:center;gap:6px;flex-wrap:wrap;flex:none">${moversChips}</span>`:''}${ribbon}</div>`;
 // Detailed rows preserved behind <details> — power users still get them but won't see them first
 const l1=`股票现值 <b>${fmt(S.marketValue)}</b>，未实现 <b class="${cls(S.unrealized)}">${fmt(S.unrealized)}</b>（区间 <b class="${cls(pr)}">${pct(pr)}</b>${al!=null?`，${al>=0?'跑赢':'跑输'}标普 ${ppf(al)}`:''}${(nq!=null&&pr<nq)?`，但略输纳指 ${ppf(pr-nq)}`:''}）`;
 const l2=lt?`最大风险 → <b>${lt.theme}</b> 占 ${lt.weightPct.toFixed(0)}% 资金${lt.riskPct!=null?`、${lt.riskPct.toFixed(0)}% 风险`:''}（${lt.n} 只，${(X.conc&&X.conc.effNRisk!=null)?`实际≈ ${X.conc.effNRisk.toFixed(0)} 笔独立押注`:'高度相关'}）`:'';
 const l3=(A&&A.optMarkGross)?`隐藏杠杆 → 期权毛敞口 ≈ <b>${fmt(A.optMarkGross)}</b>（约权益 ${A.optPctEquity}%，毛额·市价口径，净≈${fmt(A.optMarkNet)}，非 Delta/名义），未计入上方市值`:'';
 const l4=(gap!=null)?`择时 → 本期${gap<0?'<b class="pos">略帮了忙 ✓</b>':(gap>0?'<b class="neg">略拖了后腿</b>':'基本中性')}（你的钱 ${pct(S.mwrPeriod)} ${gap<0?'≥':(gap>0?'<':'≈')} 策略 ${pct(pr)}）`:'';
 const nq8=Q&&Q.available&&(Q.nextTriggers||[]).find(x=>x.name==='EMA8 拿回');
 const nq21=Q&&Q.available&&(Q.nextTriggers||[]).find(x=>x.name==='EMA21 期权');
 const qTrail=Q&&Q.available&&Q.trailing;
 const qBrief=Q&&Q.available?`QQQ 天气 → <b>${Q.state.label}</b>，老师动作 <b style="color:${Q.state.tone==='neg'?'var(--red)':(Q.state.tone==='pos'?'var(--green)':'var(--mut)')}">${(Q.decisionPanel&&Q.decisionPanel.headline)||Q.state.action}</b>；下个触发：EMA8 ${nq8?fmt(nq8.level)+' ('+(nq8.distancePct>=0?'+':'')+nq8.distancePct+'%)':'—'} / EMA21 ${nq21?fmt(nq21.level)+' ('+(nq21.distancePct>=0?'+':'')+nq21.distancePct+'%)':'—'}；QQQ 3ATR 防守 ${qTrail?fmt(qTrail.qqqTrail3Atr):'—'}`:'';
 return `<div class="card ib" style="--hero-tone:${heroTone}"><div class="dh"><h2 class="t-cap">今日要点 · 一句话先看</h2>${guide}</div>
   ${hero}
   <details style="margin-top:10px"><summary style="cursor:pointer;color:var(--mut);font-size:var(--t-sm);padding:6px 0">看其他信号 ▾</summary>
     <div class="ib-row">${l1} ${lk('nw','净值')}${lk('cmp','指数对比')}</div>
     ${qBrief?`<div class="ib-row">${qBrief} ${lk('qt','老师策略台')}</div>`:''}
     ${l2?`<div class="ib-row">${l2} ${lk('struct','结构')}</div>`:''}
     ${l3?`<div class="ib-row">${l3} ${lk('nw','期权敞口')}</div>`:''}
     ${l4?`<div class="ib-row">${l4} ${lk('beh','行为决策')}</div>`:''}
     <div class="ib-row" style="margin-top:7px;border-top:1px solid var(--hair);padding-top:7px"><b>下一步</b> → ${tr?`最该看一眼 <b>${tr}</b>（最大风险贡献）　`:''}${lk('rebal','看再平衡动作清单')}</div>
     <div class="note" style="margin-top:4px">所有指标为技术 / 描述性参考，<b>非投资建议</b>。</div>
   </details></div>`;
}
// collapsible reference card: identical .card chrome, body renders on demand.
// Decision cards stay open; reference/structure cards start closed so the answer isn't buried.
const foldCard=(title,sub,body,open,chip)=>`<details class="card foldcard ref"${open?' open':''}><summary><h2 class="t">${title}</h2>${sub?`<span class="nm">${sub}</span>`:''}${chip?`<span class="fold-chip">${chip}</span>`:''}</summary><div class="foldbody">${body}</div></details>`;
function qqqTqqqTab(){
 const q=DATA.qqqTqqq;
 if(!q||!q.available)return `<div class="card"><div class="dh"><h2 class="t">QQQ/TQQQ 决策台</h2></div><div class="note">${q&&q.reason?q.reason:'QQQ/TQQQ 数据不足。'} 技术参考，非投资建议。</div></div>`;
 const L=q.latest,st=q.state,tc=st.tone==='pos'?'#4FB286':(st.tone==='neg'?'#E5707A':'#E8B339');
 const dist=(d)=>d&&d.atr!=null?`${d.dollar>=0?'+':''}${d.dollar.toFixed(2)} (${d.pct>=0?'+':''}${d.pct.toFixed(2)}%, ${d.atr>=0?'+':''}${d.atr.toFixed(2)} ATR)`:'—';
 const sourceNote=(q.source&&q.source.qqq==='OHLC')?'ATR14 使用 Yahoo OHLC 高低点。':'ATR14 当前用收盘价近似，适合看节奏，不适合精确风控。';
 const badges=[
  ['状态',`<span class="chip" style="color:${tc};border-color:${chipBd(tc)}">${st.label}</span>`],
  ['QQQ 收盘',fmt(L.qqq)],
  ['TQQQ 收盘',fmt(L.tqqq)],
  ['EMA8',fmt(L.ema8)],
  ['EMA21',fmt(L.ema21)],
  ['距 EMA8',dist(L.distEma8)],
  ['距 EMA21',dist(L.distEma21)],
  [gl('atr','ATR14'),fmt(L.atr14)],
  ['QQQ 5日',`<span class="${cls(L.qqqRet5)}">${pct(L.qqqRet5)}</span>`],
  ['TQQQ 5日',`<span class="${cls(L.tqqqRet5)}">${pct(L.tqqqRet5)}</span>`],
  ['EMA21 5日斜率',`<span class="${cls(L.ema21Slope5)}">${pct(L.ema21Slope5)}</span>`],
 [gl('rsi','RSI14'),`${L.rsi14}`],
 ];
 const dp=q.decisionPanel||{},trail=q.trailing||{};
 const nextRows=(q.nextTriggers||[]).map(n=>`<tr><td class="l">${n.name}</td><td>${fmt(n.level)}</td><td>${n.distancePct>=0?'+':''}${n.distancePct}%</td><td class="l">${n.action}</td></tr>`).join('');
 const checks=(q.teacherChecks||[]).map(x=>`<div class="badge"><div class="l">${x.label}</div><div class="v" style="font-size:var(--t-base);line-height:1.45;color:${x.ok?'#4FB286':'#E8B339'}">${x.value}</div></div>`).join('');
 const decisionBrief=`<div class="card t1" style="border-left:3px solid ${tc}">
   <div class="dh"><h2 class="t">老师 Decision Brief</h2><span class="nm">天气 → 仓位 → 工具 → 风控，一屏给结论</span></div>
   <div class="badges">
    <div class="badge"><div class="l">现在</div><div class="v verdict" style="border-left:3px solid ${tc};padding-left:10px">${dp.headline||st.label}</div></div>
    <div class="badge"><div class="l">允许动作</div><div class="v" style="font-size:var(--t-base);line-height:1.45">${dp.doNow||st.action}</div></div>
    <div class="badge"><div class="l">不要做</div><div class="v" style="font-size:var(--t-base);line-height:1.45;color:#E8B339">${dp.avoid||'先等更清晰触发，不把单一信号当确定性。'}</div></div>
    <div class="badge"><div class="l">CCS 身份</div><div class="v" style="font-size:var(--t-base);line-height:1.45">${dp.ccsRole||'必须先判断是 hedge 还是方向性做空。'}</div></div>
   </div>
   <div class="scroll" style="margin-top:10px"><table><thead><tr><th class="l">下一触发</th><th>关键价</th><th>距现价</th><th class="l">老师动作</th></tr></thead><tbody>${nextRows}</tbody></table></div>
   <div class="note" style="margin-top:10px"><b>移动止盈：</b>${dp.movingStop||'先定义防守线。'} QQQ 20日高点 ${fmt(trail.qqqHigh20)}，3xATR 参考 ${fmt(trail.qqqTrail3Atr)}（距现价 ${trail.qqqTrailGapPct==null?'—':trail.qqqTrailGapPct+'%'}）；TQQQ 20日高点 ${fmt(trail.tqqqHigh20)}，8%/12% trailing 参考 ${fmt(trail.tqqqTrail8Pct)} / ${fmt(trail.tqqqTrail12Pct)}。<b>技术参考，非投资建议。</b></div>
   <div class="badges" style="margin-top:12px">${checks}</div>
  </div>`;
 const z=q.zones||{},zr=[
  ['EMA8 拿回区',z.ema8Buyback,'浅回踩：偏 QQQ/TQQQ 小仓拿回'],
  ['EMA21 期权区',z.ema21Options,'深回踩：趋势未坏才评估 call debit spread'],
  ['EMA34 防线',z.ema34Defense,'跌到这里不再当普通浅回调'],
  ['EMA55 regime',z.ema55Regime,'重新判断市场状态'],
 ].map(r=>`<tr><td class="l">${r[0]}</td><td>${r[1]?fmt(r[1][0])+' - '+fmt(r[1][1]):'—'}</td><td class="l">${r[2]}</td></tr>`).join('');
 const rr=(q.rules||[]).map(r=>`<tr style="${r.active?'background:rgba(232,179,57,.08)':''}"><td class="l">${r.active?'<span class="chip" style="color:#E8B339;border-color:var(--chip-bd-amber)">现在</span> ':''}${r.title}</td><td class="l">${r.trigger}</td><td class="l">${r.decision}</td></tr>`).join('');
 const h=Object.values(q.holdings||{}).map(x=>`<tr><td class="l">${x.sym}</td><td>${x.held?fmtN(x.shares,0):'—'}</td><td>${x.held?fmt(x.avg):'—'}</td><td>${x.held?fmt(x.value):'—'}</td><td class="${cls(x.gain)}">${x.held?fmt(x.gain)+' / '+pct(x.gainPct):'—'}</td></tr>`).join('');
 const c=q.tqqqCcs;
 const A=DATA.account;
 const optExposure=A?foldCard('期权 / 杠杆风险抬头看','QQQ 决策前先看账户刹车距离',`
   <div class="badges">
    <div class="badge"><div class="l">期权毛市值</div><div class="v">${fmt(A.optMarkGross)}</div></div>
    <div class="badge"><div class="l">约占股票权益</div><div class="v">${A.optPctEquity==null?'—':A.optPctEquity+'%'}</div></div>
    <div class="badge"><div class="l">期权净市值</div><div class="v ${cls(A.optMarkNet)}">${fmt(A.optMarkNet)}</div></div>
    <div class="badge"><div class="l">Margin debit</div><div class="v" style="font-size:var(--t-base);color:#E8B339">CSV 不提供</div></div>
   </div>
   <div class="note">老师视角：看 QQQ/TQQQ 前先确认账户不会被 short option、保证金或流动性拖住。这里是 mark-to-market，不是 Delta/Gamma/名义本金；真实杠杆可能更大。<b>非投资建议。</b></div>`):'';
 const qh=(q.holdings&&q.holdings.QQQ)||{},th=(q.holdings&&q.holdings.TQQQ)||{};
 const knownCash=A?(A.cashTotal||0):0, pending=A?(A.pending||0):0;
 const constraintCard=foldCard('Ownership / Cash Constraint Check','老师两句话：call 卖方死于没正股，put 卖方死于没现金',`
   <div class="badges">
    <div class="badge"><div class="l">Covered call</div><div class="v" style="font-size:var(--t-base);line-height:1.45;color:${(qh.shares||0)>=100?'#4FB286':'#E8B339'}">${(qh.shares||0)>=100?'QQQ 满 100 股可覆盖':'QQQ 仅 '+fmtN(qh.shares||0,3)+' 股，不是 covered call'}</div></div>
    <div class="badge"><div class="l">CSP cash</div><div class="v" style="font-size:var(--t-base);line-height:1.45;color:#E8B339">已知现金 ${fmt(knownCash)}；pending ${fmt(pending)} 先不当可用现金</div></div>
    <div class="badge"><div class="l">Defined-risk spread</div><div class="v" style="font-size:var(--t-base);line-height:1.45">优先窄宽度；每组最大亏损必须先写入计划</div></div>
    <div class="badge"><div class="l">Long hedge base</div><div class="v" style="font-size:var(--t-base);line-height:1.45">QQQ ${fmt(qh.value||0)} / TQQQ ${fmt(th.value||0)}</div></div>
   </div>
   <div class="note">这张卡不判断买卖，只防止结构误用：没有 100 股就不是 covered call；现金不足就不是 CSP；${gl('ccs','CCS')} 即使 ${gl('defrisk','defined risk')}，也不能大到压过核心多头和移动止盈能力。<b>非投资建议。</b></div>`);
 const tp=q.tqqqOptions||{};
 const statusChip=s=>{const c=s==='BLOCK'?'#E5707A':(s==='ALLOW'?'#4FB286':'#E8B339');return `<span class="chip" style="color:${c};border-color:${chipBd(c)}">${s||'WATCH'}</span>`;};
 const optLegRows=[...(tp.currentTqqqLegs||[]),...(tp.currentQqqLegs||[])].map(l=>`<tr><td class="l">${l.underlying||'—'}</td><td class="l">${l.side==='short'?'空头':'多头'}</td><td class="l">${l.name||l.sym}</td><td>${fmtN(l.qty,0)}</td><td class="${cls(l.mark)}">${fmt(l.mark)}</td></tr>`).join('');
 const optHistRows=(tp.recentTqqqOrders||[]).concat(tp.recentQqqOrders||[]).map(o=>`<tr><td class="l">${o.underlying}</td><td class="l">${o.sym}</td><td>${o.lastDate||'—'}</td><td>${o.trades}</td><td>${fmtN(o.netQty,3)}</td><td class="${cls(o.netCash)}">${fmt(o.netCash)}</td></tr>`).join('');
 const structRows=(tp.structures||[]).map(x=>`<tr><td class="l">${x.name}<br><span class="note">${x.role}</span></td><td>${statusChip(x.status)}</td><td class="l">${x.rule}</td><td class="l">${x.risk}</td></tr>`).join('');
 const tqqqOptionControl=foldCard('TQQQ 期权控制台','把 TQQQ 股票、QQQ 期权、TQQQ 期权分开，避免误判',`
   <div class="note" style="margin-bottom:9px"><b>当前核对：</b>${tp.status||'—'} 已知现金 ${fmt(tp.cashKnown||0)}；TQQQ covered-call 可覆盖组数 ${tp.coveredContracts||0}；TQQQ CSP 可覆盖组数 ${tp.cashSecuredPutContracts||0}。<b>非投资建议。</b></div>
   <div class="scroll"><table><thead><tr><th class="l">TQQQ 结构</th><th>状态</th><th class="l">触发 / 纪律</th><th class="l">风险口径</th></tr></thead><tbody>${structRows}</tbody></table></div>
   <div class="scroll" style="margin-top:10px"><table><thead><tr><th class="l">标的</th><th class="l">方向</th><th class="l">当前 QQQ/TQQQ 期权腿</th><th>数量</th><th>Mark</th></tr></thead><tbody>${optLegRows||'<tr><td class="l" colspan="5">当前持仓快照没有 QQQ/TQQQ 期权腿。</td></tr>'}</tbody></table></div>
   <div class="scroll" style="margin-top:10px"><table><thead><tr><th class="l">标的</th><th class="l">合约</th><th>最近日期</th><th>成交数</th><th>净张数</th><th>净现金流</th></tr></thead><tbody>${optHistRows||'<tr><td class="l" colspan="6">本窗口没有 QQQ/TQQQ 期权成交。</td></tr>'}</tbody></table></div>
   <div class="note" style="margin-top:10px">解读：TQQQ 股票买入不会自动变成 TQQQ 期权风险；当前 open 的 QQQ 708/711 是 QQQ call debit spread，和未来可能设计的 TQQQ CCS / debit spread 分开管理。任何 TQQQ 期权都先写 max loss、DTE、delta/credit、止盈和失效线。</div>`);
 const ccs=c?foldCard('TQQQ 上方 CCS 对冲区','定义风险 · 小仓 · 给多头仓位加短线刹车',`
   <div class="note" style="margin-bottom:8px"><b>身份检查：</b>${dp.ccsRole||'先判断是否有多头仓可 hedge。'} 没有多头仓时，它不是 hedge，是偏空 short-premium。</div>
   <div class="scroll"><table><thead><tr><th class="l">项目</th><th>当前量化</th><th class="l">执行含义</th></tr></thead><tbody>
   <tr><td class="l">TQQQ spot</td><td>${fmt(c.spot)}</td><td class="l">执行标的，不用它判断趋势</td></tr>
   <tr><td class="l">Short call 参考</td><td>${fmt(c.shortRange[0])} - ${fmt(c.shortRange[1])}</td><td class="l">当前价上方 3%-6%，或实盘用 0.15-0.25 delta</td></tr>
   <tr><td class="l">Long call 风险封顶</td><td>${fmt(c.longRange[0])} - ${fmt(c.longRange[1])}</td><td class="l">宽度 ${c.width}，DTE ${c.dte}</td></tr>
   <tr><td class="l">QQQ 等效涨幅</td><td>约 +${c.qqqApproxPct[0].toFixed(1)}% - +${c.qqqApproxPct[1].toFixed(1)}%</td><td class="l">TQQQ 3x 的粗略换算，路径会影响结果</td></tr>
   <tr><td class="l">91/92 参照</td><td>91C ${c.example9122.shortPct>=0?'+':''}${c.example9122.shortPct}% / 92C ${c.example9122.longPct>=0?'+':''}${c.example9122.longPct}%</td><td class="l">对应 QQQ 约 ${c.example9122.qqqApproxPct>=0?'+':''}${c.example9122.qqqApproxPct}% 的短线空间</td></tr>
   </tbody></table></div>
   <div class="scroll" style="margin-top:10px"><table><thead><tr><th class="l">1美元宽示例</th><th>Max profit/组</th><th>Max loss/组</th><th>Breakeven</th></tr></thead><tbody>${(c.example9122.creditScenarios||[]).map(s=>`<tr><td class="l">credit ${fmt(s.credit)}</td><td class="pos">${fmt(s.maxProfit)}</td><td class="neg">${fmt(s.maxLoss)}</td><td>${fmt(s.breakeven)}</td></tr>`).join('')}</tbody></table></div>
   <div class="note" style="margin-top:10px">退出纪律：赚到 credit 的 50%-70% 可收；亏损到 credit 的 1.5-2 倍或 QQQ 强势突破前高时不要硬扛。这里不读期权链，delta 需实盘确认。<b>非投资建议。</b></div>`):'';
 const spreadCard=(q.optionSpreads||[]).length?foldCard('Spread Risk Ledger','先看组合结构，再看单腿',`
   <div class="scroll"><table><thead><tr><th class="l">结构</th><th>到期/DTE</th><th>腿</th><th>宽度</th><th>当前净Mark</th><th>理论范围</th><th class="l">风险提示</th></tr></thead><tbody>${q.optionSpreads.map(s=>`<tr><td class="l">${s.underlying} ${s.kind}</td><td>${s.expiry}<br><span class="note">${s.dte==null?'—':s.dte+'d'}</span></td><td>${s.longStrike}/${s.shortStrike} ${s.right}</td><td>${fmt(s.widthValue)}</td><td>${fmt(s.netMark)}</td><td>${fmt(s.expectedRange[0])} - ${fmt(s.expectedRange[1])}</td><td class="l">${(s.warnings&&s.warnings.length)?'<span class="chip" style="color:#E5707A;border-color:var(--chip-bd-red)">核对</span> '+s.warnings.join('；'):'<span class="chip" style="color:#4FB286;border-color:var(--chip-bd-green)">范围内</span> 仍需实盘 bid/ask/Greeks'}</td></tr>`).join('')}</tbody></table></div>
   <div class="note" style="margin-top:10px">这里根据券商持仓名和数量保守识别垂直价差；entry debit/credit 不在持仓 CSV 中，真实最大盈亏仍需结合成交记录和实时盘口。若 short leg 已接近 0.01/0.02 且 long leg 很便宜，才考虑老师的“买回 short、留 long 彩票”拆腿。<b>非投资建议。</b></div>`):'';
 const legs=(q.optionLegs||[]).length?foldCard('当前 QQQ/TQQQ 期权腿','券商快照市价，不是 Delta/名义敞口',`
   <div class="scroll"><table><thead><tr><th class="l">方向</th><th class="l">合约</th><th>数量</th><th>Mark</th><th class="l">账户</th></tr></thead><tbody>${q.optionLegs.map(l=>`<tr><td class="l">${l.side==='short'?'空头':'多头'}</td><td class="l">${l.name||l.sym}</td><td>${fmtN(l.qty,0)}</td><td>${fmt(l.mark)}</td><td class="l">${l.type||l.acct||'—'}</td></tr>`).join('')}</tbody></table></div>`):'';
 const playbook=foldCard('老师策略蒸馏','天气 · 执行 · 刹车 · 生存',`
   <div class="badges">
    <div class="badge"><div class="l">QQQ = 天气图</div><div class="v" style="font-size:var(--t-base);line-height:1.5">只用 QQQ 日线 EMA/ATR 判断 regime，不用 TQQQ 噪音决定世界观。</div></div>
    <div class="badge"><div class="l">TQQQ = 战术仓</div><div class="v" style="font-size:var(--t-base);line-height:1.5">只在 EMA8/EMA21 健康回踩时放大，强趋势高位不追。</div></div>
    <div class="badge"><div class="l">CCS = 小刹车</div><div class="v" style="font-size:var(--t-base);line-height:1.5">过热时小仓定义风险，机械常态化卖会拖累白天鹅。</div></div>
    <div class="badge"><div class="l">移动止盈 = 生存</div><div class="v" style="font-size:var(--t-base);line-height:1.5">过热分批止盈，runner 用 trailing stop，不把利润全还回去。</div></div>
   </div>
   <div class="note" style="margin-top:10px">回测发现：强趋势里核心 QQQ 暴露最关键；更接近老师的版本是 90% QQQ core + TQQQ 回踩战术 + 过热卖一半留 runner；CCS 只在明确过热/停顿时小仓做。<b>非投资建议。</b></div>`);
 return `<div class="card" style="border-left:3px solid ${tc}">
   <div class="dh"><h2 class="t">QQQ/TQQQ 决策台<span class="t-en">Decision Desk</span></h2><span class="nm">QQQ 判天气 · TQQQ/期权做执行 · as of ${q.asOf}</span></div>
   <div class="hero-fig" style="margin:8px 0 3px;border-left:3px solid ${tc};padding-left:10px">${st.action}</div>
   <div class="badges">${badges.map(b=>`<div class="badge"><div class="l">${b[0]}</div><div class="v">${b[1]}</div></div>`).join('')}</div>
   <div class="note">${sourceNote} 数据窗口跟随本次 CSV / Yahoo cache。<b>技术参考，非投资建议。</b></div>
 </div>
 ${decisionBrief}
 <div class="card"><div class="dh"><h2 class="t">QQQ 日线天气图</h2><span class="nm">EMA8/21/34/55 + ATR 作战区</span></div>
   <div class="legend"><span><i class="ln" style="color:${QTC.close}"></i>QQQ</span><span><i class="ln" style="color:${QTC.ema8}"></i>EMA8</span><span><i class="ln" style="color:${QTC.ema21}"></i>EMA21</span><span><i class="lnd" style="color:${QTC.ema34}"></i>EMA34</span><span><i class="lnd" style="color:${QTC.ema55}"></i>EMA55</span></div>
   ${qqqStrategyChart(q)}
 </div>
 <div class="ref-divider" role="presentation">以下为参考资料 · 点开查看 — 决策结论都在上面三张卡</div>
 ${foldCard('执行地图','高位不追 · 回踩分批 · 趋势坏先防守',`
   <div class="scroll"><table><thead><tr><th class="l">触发状态</th><th class="l">量化条件</th><th class="l">动作</th></tr></thead><tbody>${rr}</tbody></table></div><div class="note" style="margin-top:8px">这是你预先写好的机械执行清单（${gl('ema','EMA')} / ${gl('atr','ATR')} 触发），不是预测——按既定规则执行、减少临场情绪。<b>非投资建议。</b></div>`,0,`${(q.rules||[]).length} 条`)}
 ${foldCard('QQQ 作战区间','用 ATR 把 EMA8 / EMA21 变成价格带',`
   <div class="scroll"><table><thead><tr><th class="l">区间</th><th>价格带</th><th class="l">含义</th></tr></thead><tbody>${zr}</tbody></table></div><div class="note" style="margin-top:8px">用 ${gl('atr','ATR')} 把均线变成价格带，仅供对照节奏的技术参考。<b>非投资建议。</b></div>`)}
 ${optExposure}
 ${constraintCard}
 ${tqqqOptionControl}
 ${playbook}
 ${ccs}
 ${foldCard('当前 QQQ/TQQQ 仓位','券商持仓快照',`
   <div class="scroll"><table><thead><tr><th class="l">代码</th><th>股数</th><th>均价</th><th>市值</th><th>未实现</th></tr></thead><tbody>${h}</tbody></table></div>`)}
 ${spreadCard}
 ${legs}`;
}
// Panel renderers for the overview: each returns HTML for its seg panel. Called on demand by ensureOvPanel().
// Lazy-rendering cuts initial paint from ~267KB innerHTML to ~30KB; deferred panels populate after first paint via requestIdleCallback.
function _ovPanelEnv(){
 const pr=S.curReturn,sp=S.spReturn,nq=S.nasdaqReturn,ser=DATA.series||[];
 const cards=[
  ['当前持仓市值',fmt(S.netWorthNow)],
  ['期初持仓市值',fmt(S.netWorthStart)],
  ['选股策略本身的收益 ('+gl('TWR','时间加权 TWR')+')',`<span class="${cls(pr)}">${pct(pr)}</span>`],
  ['你的钱实际经历的收益 ('+gl('MWR','资金加权 MWR')+')',S.mwrPeriod==null?'—':`<span class="${cls(S.mwrPeriod)}">${pct(S.mwrPeriod)}</span>`],
  ['你的钱年化收益 ('+gl('XIRR','XIRR')+')',S.mwrAnnual==null?'—':`<span class="${cls(S.mwrAnnual)}">${pct(S.mwrAnnual)}</span> <span class="note">数月年化·非预测</span>`],
  ['择时帮了还是拖了 ('+gl('gap','行为缺口')+')',S.behaviorGap==null?'—':`<span class="${cls(-S.behaviorGap)}">${S.behaviorGap>0?'+':''}${S.behaviorGap.toFixed(2)}pp</span> <span class="note">${S.behaviorGap>0?'你的钱落后策略':(S.behaviorGap<0?'你的钱跑赢策略':'基本中性')}</span>`],
  ['同期 S&P 500',sp==null?'—':`<span class="${cls(sp)}">${pct(sp)}</span>`],
  ['同期 纳斯达克',nq==null?'—':`<span class="${cls(nq)}">${pct(nq)}</span>`],
  ['比大盘多赚 / 少赚 ('+gl('alpha','超额 vs S&P500')+')',sp==null?'—':`<span class="${cls(pr-sp)}">${ppf(pr-sp)}</span>`],
 ];
 return {pr,sp,nq,ser,cards};
}
// ---- 决策分析 (Decision Analysis): data-driven from DATA.decision (output/decision_analysis.json).
// Renders cash snapshot + a ranked candidate scorecard (trend/risk/diversify mini-bars + verdict)
// mirroring 决策一览's responsive table↔card switch. Reusable: any future decision JSON renders here.
function decisionTone(t){return {good:'#4FB286',hold:'#888D96',caution:'#C99A3A',avoid:'#E5707A'}[t]||'#888D96';}
function decBar(label,val,col){const v=Math.max(0,Math.min(100,Math.round(val||0)));
 return `<div style="display:flex;align-items:center;gap:5px"><span style="font-size:10px;color:var(--faint);width:11px">${label}</span><div class="fbar" style="height:7px;flex:1"><div class="p" style="left:0;width:${v}%;background:${col};top:1px;height:5px;border-radius:2px"></div></div><span style="font-size:10px;color:var(--mut);width:15px;text-align:right;font-family:var(--f-mono)">${v}</span></div>`;}
function decisionLabCard(){
 const D=DATA.decision;
 if(!D||!Array.isArray(D.candidates)||!D.candidates.length){
  return `<div class="card t1"><div class="dh"><h2 class="t">决策分析<span class="t-en">Decision Analysis</span></h2><span class="nm">现金部署 · 择时 · 多视角量化裁决</span></div><div class="note" style="line-height:1.8">暂无决策分析数据。<br>生成方式：运行 <b>python3 scripts/decision_analysis.py --tickers AMD,QQQ,TQQQ,SPMO,MU,NBIS,TSM,ASML</b> 计算量化指标，补上综合评分与裁决后写入 <b>output/decision_analysis.json</b>，再跑 <b>python3 sync.py</b>，本页即会渲染同样的现金 + 记分卡视图。<b>非投资建议。</b></div></div>`;
 }
 const cash=D.cash||{};
 const money=v=>'$'+Number(v).toLocaleString('en-US');
 const cashCards=[
  ['可操作现金', cash.operable!=null?money(cash.operable):'—', cash.withPending!=null?('含在途 → '+money(cash.withPending)):''],
  ['现金占比', cash.pctSettled!=null?(cash.pctSettled+'%'):'—', cash.pctWithPending!=null?('含在途 '+cash.pctWithPending+'%'):''],
  ['净清算值', cash.netLiq!=null?('$'+Math.round(cash.netLiq/1000)+'k'):'—', cash.top4Pct!=null?('前4名占 '+cash.top4Pct+'%'):'']
 ];
 const rows=D.candidates.map(c=>{const tc=decisionTone(c.tone);
  return `<tr style="cursor:pointer" tabindex="0" role="button" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();this.click();}" onclick="stockGo('${c.ticker}')">
    <td class="l"><b>${c.ticker}</b>${c.price!=null?` <span class="note">$${c.price}</span>`:''}</td>
    <td><span style="font-family:var(--f-mono);font-size:15px;color:${tc}">${c.composite!=null?c.composite:'—'}</span></td>
    <td style="color:${tc};font-size:var(--t-sm)">${c.status||''}</td>
    <td style="min-width:128px">${decBar('趋',c.momentum,'#9A8CF0')}${decBar('险',c.risk,'#4FB286')}${decBar('散',c.diversify,'#6FA8DC')}</td>
    <td><span class="chip" style="color:${tc};border-color:${chipBd(tc)}">${c.verdict||''}</span></td></tr>`;}).join('');
 const cards=D.candidates.map(c=>{const tc=decisionTone(c.tone);
  return `<div class="smc" role="button" tabindex="0" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();this.click();}" onclick="stockGo('${c.ticker}')">
    <div class="smc-top"><b class="smc-sym">${c.ticker}</b><span class="smc-state" style="color:${tc}">综合 ${c.composite!=null?c.composite:'—'}</span><span class="smc-pnl" style="color:${tc}">${c.status||''}</span></div>
    <div class="smc-mid">趋势 ${Math.round(c.momentum||0)} · 风险 ${Math.round(c.risk||0)} · 分散 ${Math.round(c.diversify||0)}${c.price!=null?' · $'+c.price:''}</div>
    <div class="smc-bias"><span class="chip" style="color:${tc};border-color:${chipBd(tc)}">${c.verdict||''}</span>${c.note?` <span class="note">${c.note}</span>`:''}</div></div>`;}).join('');
 const noList=(D.noList||[]).map(n=>`<span class="chip" style="color:#E5707A;border-color:var(--chip-bd-red)">✕ ${n.ticker}${n.why?`<span class="note" style="margin-left:4px;font-weight:400">${n.why}</span>`:''}</span>`).join(' ');
 const rules=(D.rules||[]).map(r=>`<li style="margin:4px 0">${r}</li>`).join('');
 return `<div class="card t1">
   <div class="dh"><h2 class="t">决策分析<span class="t-en">Decision Analysis</span></h2><span class="nm">${(D.title||'现金部署 · 择时 · 多视角量化裁决')}${D.asOf?(' · '+D.asOf):''}</span></div>
   ${D.thesis?`<div class="note" style="line-height:1.75;margin-bottom:10px"><b>核心结论：</b>${D.thesis}</div>`:''}
   <div class="badges">${cashCards.map(m=>`<div class="badge"><div class="l">${m[0]}</div><div class="v">${m[1]}</div>${m[2]?`<div class="note" style="font-size:var(--t-xs);margin-top:2px">${m[2]}</div>`:''}</div>`).join('')}</div>
   ${noList?`<div style="margin:14px 0 2px"><span class="cap">明确不要买</span><div style="margin-top:6px;display:flex;flex-wrap:wrap;gap:6px">${noList}</div></div>`:''}
   <div class="scroll score-tablewrap" style="margin-top:12px"><table><thead><tr><th class="l">代码</th><th title="动量·风险·组合三视角均分">综合分</th><th>今日状态</th><th title="趋势=动量质量 · 险=回撤/估值安全 · 散=与现有持仓的分散度">趋势 / 险 / 散</th><th>裁决</th></tr></thead><tbody>${rows}</tbody></table></div>
   <div class="score-cards" aria-label="决策分析（手机版卡片）">${cards}</div>
   ${rules?`<div style="margin-top:14px"><span class="cap">纪律规则</span><ul style="margin:6px 0 0;padding-left:18px;color:var(--mut);font-size:var(--t-sm);line-height:1.6">${rules}</ul></div>`:''}
   ${D.recommendation?`<div class="note" style="margin-top:12px;line-height:1.75"><b>建议：</b>${D.recommendation}</div>`:''}
   <div class="note" style="margin-top:8px;color:var(--faint);line-height:1.6">${D.method?('方法：'+D.method+'。'):''}${D.disclaimer||'量化分析与决策框架，非投资建议，亦非收益保证；最终决定权在你。'}</div>
 </div>`;
}
// ---- 财务状态 (Financial Status): FMP-backed held-company lens from DATA.financialStatus.
function finEsc(s){return String(s==null?'':s).replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));}
function finTone(g){return {STRONG_FINANCIALS:'#4FB286',HEALTHY_WATCH:'#72C49B',EARNINGS_WATCH:'#E8B339',REVIEW_QUEUE:'#C99A3A',FINANCIAL_REVIEW:'#E5707A',DATA_REVIEW:'#6FA8DC'}[g]||'#888D96';}
function finScoreColor(v){v=Number(v||0);return v>=80?'#4FB286':(v>=65?'#E8B339':(v>=50?'#C99A3A':'#E5707A'));}
function finRiskColor(v){v=Number(v||0);return v>=72?'#E5707A':(v>=58?'#E8B339':'#4FB286');}
function finPct(v,d=1){v=Number(v);return isFinite(v)?v.toFixed(d)+'%':'—';}
function finRatioPct(v,d=1){v=Number(v);return isFinite(v)?(v*100).toFixed(d)+'%':'—';}
function finNum(v,d=1){v=Number(v);return isFinite(v)?v.toFixed(d):'—';}
function finData(){return DATA.financialStatus||null;}
function finRows(){const D=finData();return D&&Array.isArray(D.scores)?D.scores:[];}
function finOmittedRows(){const D=finData();return D&&Array.isArray(D.omitted)?D.omitted:[];}
function finFind(sym){sym=(sym||'').toUpperCase();return finRows().find(r=>(r.ticker||'').toUpperCase()===sym)||null;}
function finOmitFind(sym){sym=(sym||'').toUpperCase();return finOmittedRows().find(r=>(r.ticker||'').toUpperCase()===sym)||null;}
function finReasonText(r){const x=((r&&r.gateReasons||[])[0]||{}).detail||'';return x.length>110?finEsc(x.slice(0,107))+'...':finEsc(x);}
function financialMiniBadge(sym){
 const r=finFind(sym),o=finOmitFind(sym);
 if(r){const c=finTone(r.gate);return `<span class="chip" style="color:${c};border-color:${chipBd(c)};cursor:pointer" role="button" tabindex="0" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();this.click();}" onclick="stockGo('${finEsc(sym)}','fin')" title="FMP 财务状态：${finEsc(r.gate)}">${finEsc(r.gate)} · ${r.finalScore??'—'}</span>`;}
 if(o)return `<span class="chip" style="color:var(--mut);border-color:var(--line);cursor:pointer" role="button" tabindex="0" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();this.click();}" onclick="stockGo('${finEsc(sym)}','fin')" title="${finEsc(o.reason)}">财务 N/A</span>`;
 return '';
}
function financialStatusCard(){
 const D=DATA.financialStatus;
 if(!D||!Array.isArray(D.scores)){
  return `<div class="card t1"><div class="dh"><h2 class="t">财务状态<span class="t-en">FMP Lens</span></h2><span class="nm">财务质量 · 财报记录 · 下一财报风险</span></div><div class="note" style="line-height:1.8">暂无 FMP 财务状态数据。<br>生成方式：把 FMP key 放在 <b>~/.config/ptrak/fmp.json</b> 或 <b>FMP_API_KEY</b>，运行 <b>python3 scripts/financial_status_score.py</b> 写入 <b>output/financial_status.json</b>，再跑 <b>python3 sync.py</b> 或 <b>python3 generate.py --no-fetch</b>。<b>研究框架，非投资建议。</b></div></div>`;
 }
 const sum=D.summary||{},counts=D.counts||{},rows=D.scores||[],om=D.omitted||[],gates=sum.gateCounts||{};
 const gateChip=g=>{const c=finTone(g);return `<span class="chip" style="color:${c};border-color:${chipBd(c)}">${finEsc(g)}</span>`;};
 const reason=r=>{const x=((r.gateReasons||[])[0]||{}).detail||'';return x.length>96?finEsc(x.slice(0,93))+'...':finEsc(x);};
 const leaders=(sum.leaders||[]).map(x=>`<span class="chip" style="color:${finScoreColor(x.score)};border-color:${chipBd(finScoreColor(x.score))}">${finEsc(x.ticker)} ${x.score} · ${finEsc(x.gate)}</span>`).join(' ');
 const upcoming=(sum.upcomingEarnings||[]).slice(0,8).map(x=>`<span class="chip" style="color:${finRiskColor(x.riskIndex)};border-color:${chipBd(finRiskColor(x.riskIndex))}">${finEsc(x.ticker)} ${finEsc(x.nextDate||'—')}${x.daysToNext==null?'':' · '+x.daysToNext+'d'} · risk ${x.riskIndex??'—'}</span>`).join(' ');
 const apiWarn=(sum.apiErrors||[]).slice(0,6).map(e=>`<div class="note" style="line-height:1.55">${finEsc(e)}</div>`).join('');
 const cards=[
  ['Scored / Held',`${counts.scored??0} / ${counts.held??0}`,`${counts.omitted??0} omitted as ETF/private/fund`],
  ['Average Lens',sum.avgFinalScore??'—',`financial ${sum.avgFinancialStatusScore??'—'} · earnings ${sum.avgEarningsReportScore??'—'}`],
  ['Event Risk Avg',sum.avgNextEarningsRiskIndex??'—','higher = closer/larger earnings-event risk'],
  ['Gates',`Strong ${gates.STRONG_FINANCIALS||0} · Healthy ${gates.HEALTHY_WATCH||0}`,`Review ${gates.FINANCIAL_REVIEW||0} · Data ${gates.DATA_REVIEW||0}`],
 ['FMP Usage',`${sum.apiCalls??0} fresh · ${sum.cacheHits??0} cache`,(sum.apiErrors||[]).length?`${(sum.apiErrors||[]).length} endpoint restrictions/errors logged`:'no endpoint errors logged'],
  ['Source Coverage',Object.entries(sum.sourceFamilyCounts||{}).map(([k,v])=>`${k} ${v}`).join(' · ')||'—',`enabled ${(D.sourcesEnabled||[]).join(', ')||'—'}${D.secUserAgentConfigured?' · SEC on':' · SEC UA missing'}`],
 ];
 const table=rows.map(r=>{const m=r.metrics||{},e=r.earnings||{},c=finScoreColor(r.finalScore),rc=finRiskColor(r.nextEarningsRiskIndex);
  return `<tr style="cursor:pointer" tabindex="0" role="button" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();this.click();}" onclick="stockGo('${finEsc(r.ticker)}','fin')">
   <td class="l"><b>${finEsc(r.ticker)}</b><br><span class="note">${finEsc(r.name)}</span><br><span class="note">src ${(r.sourceFamilies||[]).join('+')||'—'}</span></td>
   <td>${gateChip(r.gate)}</td>
   <td><span style="font-family:var(--f-mono);font-size:15px;color:${c}">${r.finalScore??'—'}</span></td>
   <td>${r.financialStatusScore??'—'}<br><span class="note">利 ${r.components?.profitability??'—'} · 资 ${r.components?.balanceSheet??'—'} · 现 ${r.components?.cashFlow??'—'}</span></td>
   <td>${r.earningsReportScore??'—'}<br><span class="note">EPS beat ${e.epsBeatRate==null?'—':finPct(e.epsBeatRate)}</span></td>
   <td><span style="color:${rc}">${r.nextEarningsRiskIndex??'—'}</span><br><span class="note">${finEsc(e.nextDate||'—')}${e.daysToNext==null?'':' · '+e.daysToNext+'d'}${e.nextTime?' · '+finEsc(e.nextTime):''}</span></td>
   <td>${finPct(r.portfolioWeightPct,2)}<br><span class="note">${fmt(r.value)}</span></td>
   <td>${m.piotroskiScore??'—'} / ${m.altmanZScore??'—'}<br><span class="note">ROE ${finRatioPct(m.returnOnEquityTTM)} · FCFy ${finRatioPct(m.freeCashFlowYieldTTM)}</span></td>
   <td>${finRatioPct(m.revenueGrowthAvg)}<br><span class="note">net margin ${finRatioPct(m.netMarginTTM)} · P/E ${finNum(m.priceToEarningsTTM)}</span></td>
   <td class="l"><span class="note">${reason(r)||'—'}</span></td>
  </tr>`;}).join('');
 const mobile=rows.slice(0,16).map(r=>{const e=r.earnings||{},m=r.metrics||{},c=finScoreColor(r.finalScore);
  return `<div class="smc" role="button" tabindex="0" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();this.click();}" onclick="stockGo('${finEsc(r.ticker)}','fin')">
   <div class="smc-top"><b class="smc-sym">${finEsc(r.ticker)}</b><span class="smc-state" style="color:${c}">Lens ${r.finalScore??'—'} · Fin ${r.financialStatusScore??'—'}</span><span class="smc-pnl" style="color:${finTone(r.gate)}">${finEsc(r.gate)}</span></div>
   <div class="smc-mid">财报 ${r.earningsReportScore??'—'} · event ${r.nextEarningsRiskIndex??'—'} · src ${(r.sourceFamilies||[]).join('+')||'—'} · ${finEsc(e.nextDate||'earnings n/a')}${e.daysToNext==null?'':' · '+e.daysToNext+'d'} · 仓位 ${finPct(r.portfolioWeightPct,2)}</div>
   <div class="smc-bias">${reason(r)||''} <span class="note">Piotroski ${m.piotroskiScore??'—'} · Altman ${m.altmanZScore??'—'}</span></div>
  </div>`;}).join('');
 const omitted=om.map(o=>`<tr><td class="l"><b>${finEsc(o.ticker)}</b><br><span class="note">${finEsc(o.name)}</span></td><td>${finPct(o.portfolioWeightPct,2)}</td><td>${fmt(o.value)}</td><td class="l"><span class="note">${finEsc(o.reason)}</span></td></tr>`).join('');
 return `<div class="card t1">
   <div class="dh"><h2 class="t">财务状态<span class="t-en">FMP Lens</span></h2><span class="nm">财务质量 · 财报记录 · 下一财报风险 · ${(D.asOfDate||D.generatedAt||'').slice(0,10)||'—'}</span></div>
   <div class="note" style="line-height:1.7">This lens scores operating-company holdings using FMP financial scores, TTM ratios, key metrics, financial growth, earnings history, and the earnings calendar. DATA_REVIEW means FMP did not provide enough usable fundamentals under the current API plan; the row remains visible rather than filled with invented data.</div>
   <div class="badges">${cards.map(x=>`<div class="badge"><div class="l">${x[0]}</div><div class="v">${x[1]}</div><div class="note" style="font-size:var(--t-xs);margin-top:2px">${x[2]}</div></div>`).join('')}</div>
   ${leaders?`<div style="margin-top:12px"><span class="cap">Leaders</span><div style="margin-top:6px;display:flex;gap:6px;flex-wrap:wrap">${leaders}</div></div>`:''}
   ${upcoming?`<div style="margin-top:12px"><span class="cap">Upcoming Earnings</span><div style="margin-top:6px;display:flex;gap:6px;flex-wrap:wrap">${upcoming}</div></div>`:''}
  </div>
  <div class="card">
   <div class="dh"><h2 class="t">Scoreboard<span class="t-en">FIN</span></h2><span class="nm">Lens = 45% 财务状态 + 25% 财报记录 + 20% 下一财报安全 + 10% 数据可信度</span></div>
   <div class="scroll score-tablewrap"><table><thead><tr><th class="l">Ticker</th><th>Gate</th><th>Lens</th><th>Financial</th><th>Earnings</th><th>Event Risk</th><th>PF</th><th>Piotroski / Altman</th><th>Growth / Value</th><th class="l">Reason</th></tr></thead><tbody>${table}</tbody></table></div>
   <div class="score-cards" aria-label="财务状态手机版卡片">${mobile}</div>
  </div>
  <div class="card">
   <div class="dh"><h2 class="t">Coverage<span class="t-en">DATA</span></h2><span class="nm">FMP plan limits and non-company exposures</span></div>
   ${apiWarn?`<div style="margin-bottom:12px">${apiWarn}</div>`:'<div class="note" style="line-height:1.6;margin-bottom:12px">No FMP endpoint errors were logged on this run.</div>'}
   <div class="scroll"><table><thead><tr><th class="l">Omitted</th><th>PF</th><th>Value</th><th class="l">Reason</th></tr></thead><tbody>${omitted||'<tr><td class="l">None</td><td>—</td><td>—</td><td class="l"><span class="note">All held lines were company-like securities.</span></td></tr>'}</tbody></table></div>
   <div class="note" style="margin-top:10px;line-height:1.6">${finEsc(D.disclaimer||'Research framework only, not investment advice.')}</div>
  </div>`;
}
function financialStockDetailCard(s){
 const D=finData(),r=finFind(s.sym),o=finOmitFind(s.sym);
 const f=s.fib&&s.fib.now,latestSig=(s.fib&&s.fib.signals||[]).slice(-1)[0];
 const journal=journalEntry(s.sym);
 const weight=(r&&r.portfolioWeightPct)||(o&&o.portfolioWeightPct)||(S.marketValue?((s.value||0)/S.marketValue*100):null);
 const behavior=[
  ['市场行为',f?`<span style="color:${FIBCOL[f.state]||'var(--mut)'}">●</span> ${finEsc(f.label)}`:'—',f?`动能 ${f.mom>0?'+':''}${f.mom} · RSI ${f.rsi}`:'价格数据不足'],
  ['今日波动',s.dayChangePct==null?'—':`<span class="${cls(s.dayChangePct)}">${s.dayChangePct>0?'+':''}${s.dayChangePct.toFixed(2)}%</span>`,latestSig?`${latestSig.type==='golden'?'金叉':'死叉'} ${latestSig.date}`:'无近期交叉'],
  ['仓位行为',s.held?`${fmtN(s.shares)} 股 · ${finPct(weight,2)}`:'已清仓',`${s.numTrades} 笔交易 · ${journal?'日志已记录':'日志未记录'}`],
  ['盈亏行为',s.held?`<span class="${cls(s.unreal)}">${fmt(s.unreal)}</span>`:`<span class="${cls(s.realized)}">${fmt(s.realized)}</span>`,s.held?`未实现 ${pct(s.unrealPct)}`:'窗口已实现'],
 ];
 const behaviorCard=`<div class="card">
   <div class="dh"><h2 class="t">市场行为<span class="t-en">BEHAVIOR</span></h2><span class="nm">价格趋势 · 仓位 · 交易记录</span></div>
   <div class="badges">${behavior.map(x=>`<div class="badge"><div class="l">${x[0]}</div><div class="v">${x[1]}</div><div class="note" style="font-size:var(--t-xs);margin-top:2px">${x[2]}</div></div>`).join('')}</div>
   <div class="note" style="line-height:1.65;margin-top:8px">This section separates what the stock is doing in the market from what the company fundamentals say. The financial lens below is a research filter, not a buy/sell command.</div>
  </div>`;
 if(!D||!Array.isArray(D.scores)){
  return behaviorCard+`<div class="card"><div class="dh"><h2 class="t">财务状态<span class="t-en">FMP</span></h2><span class="nm">暂无 FMP artifact</span></div><div class="note" style="line-height:1.8">还没有 <b>output/financial_status.json</b>。运行 <b>python3 scripts/financial_status_score.py</b> 后再跑 <b>python3 sync.py</b>，这里会显示该公司的财务质量、财报记录与下一财报风险。</div></div>`;
 }
 if(o&&!r){
  return behaviorCard+`<div class="card">
   <div class="dh"><h2 class="t">财务状态<span class="t-en">N/A</span></h2><span class="nm">${finEsc(o.assetClass||'non-company exposure')}</span></div>
   <div class="badges"><div class="badge"><div class="l">Omitted</div><div class="v">Not Scored</div><div class="note" style="font-size:var(--t-xs);margin-top:2px">${finEsc(o.reason)}</div></div><div class="badge"><div class="l">Portfolio Weight</div><div class="v">${finPct(o.portfolioWeightPct,2)}</div><div class="note" style="font-size:var(--t-xs);margin-top:2px">${fmt(o.value)}</div></div></div>
   <div class="note" style="line-height:1.65;margin-top:8px">Funds, ETFs, and private/non-listed exposures do not have operating-company financial statements in this FMP company lens. Use the portfolio-level risk/structure pages for these lines.</div>
  </div>`;
 }
 if(!r){
  return behaviorCard+`<div class="card"><div class="dh"><h2 class="t">财务状态<span class="t-en">DATA</span></h2><span class="nm">No company row</span></div><div class="note" style="line-height:1.8">${finEsc(s.sym)} is not present in <b>output/financial_status.json</b>. Re-run <b>python3 scripts/financial_status_score.py</b> and then <b>python3 sync.py</b>.</div></div>`;
 }
 const m=r.metrics||{},e=r.earnings||{},c=r.components||{},tone=finTone(r.gate),scoreColor=finScoreColor(r.finalScore),riskColor=finRiskColor(r.nextEarningsRiskIndex);
 const top=[
  ['Lens Score',`<span style="color:${scoreColor}">${r.finalScore??'—'}</span>`,finEsc(r.gate)],
  ['Financial Status',r.financialStatusScore??'—',`profit ${c.profitability??'—'} · balance ${c.balanceSheet??'—'} · cash ${c.cashFlow??'—'}`],
  ['Earnings Record',r.earningsReportScore??'—',`EPS beat ${e.epsBeatRate==null?'—':finPct(e.epsBeatRate)} · rows ${e.rowsUsed??0}`],
  ['Next Earnings Risk',`<span style="color:${riskColor}">${r.nextEarningsRiskIndex??'—'}</span>`,`${finEsc(e.nextDate||'unknown')}${e.daysToNext==null?'':' · '+e.daysToNext+'d'}${e.nextTime?' · '+finEsc(e.nextTime):''}`],
  ['Data Confidence',r.dataConfidenceScore??'—',`sources ${(r.sourceFamilies||[]).join('+')||'—'} · sector ${finEsc(m.sector||'—')}`],
 ];
 const metricRows=[
  ['Profitability',`gross ${finRatioPct(m.grossMarginTTM)} · op ${finRatioPct(m.operatingMarginTTM)} · net ${finRatioPct(m.netMarginTTM)}`,`ROE ${finRatioPct(m.returnOnEquityTTM)} · ROA ${finRatioPct(m.returnOnAssetsTTM)} · ROIC ${finRatioPct(m.returnOnInvestedCapitalTTM)}`],
  ['Balance Sheet',`current ${finNum(m.currentRatioTTM,2)} · quick ${finNum(m.quickRatioTTM,2)} · debt/equity ${finNum(m.debtToEquityTTM,2)}`,`Altman ${finNum(m.altmanZScore,2)} · Piotroski ${finNum(m.piotroskiScore,1)} · interest ${finNum(m.interestCoverageRatioTTM,1)}`],
  ['Cash Flow',`OCF margin ${finRatioPct(m.operatingCashFlowSalesRatioTTM)} · FCF/OCF ${finRatioPct(m.freeCashFlowOperatingCashFlowRatioTTM)}`,`FCF yield ${finRatioPct(m.freeCashFlowYieldTTM)} · net debt/EBITDA ${finNum(m.netDebtToEBITDATTM,2)}`],
  ['Growth',`revenue ${finRatioPct(m.revenueGrowthAvg)} · net income ${finRatioPct(m.netIncomeGrowthAvg)}`,`OCF ${finRatioPct(m.operatingCashFlowGrowthAvg)} · FCF ${finRatioPct(m.freeCashFlowGrowthAvg)}`],
  ['Valuation Sanity',`P/E ${finNum(m.priceToEarningsTTM,1)} · EV/EBITDA ${finNum(m.evToEBITDATTM,1)} · P/S ${finNum(m.priceToSalesTTM,1)}`,`P/FCF ${finNum(m.priceToFreeCashFlowTTM,1)} · PEG ${finNum(m.pegRatioTTM,1)}`],
 ].map(x=>`<tr><td class="l"><b>${x[0]}</b></td><td class="l">${x[1]}</td><td class="l"><span class="note">${x[2]}</span></td></tr>`).join('');
 const earningsRows=[
  ['Latest Report',finEsc(e.latestDate||'—'),`last surprise ${e.lastSurprisePct==null?'—':finPct(e.lastSurprisePct)} · avg surprise ${e.avgSurprisePct==null?'—':finPct(e.avgSurprisePct)}`],
  ['Next Report',finEsc(e.nextDate||'—'),`${e.daysToNext==null?'unknown days':e.daysToNext+' days'}${e.nextTime?' · '+finEsc(e.nextTime):''}${e.nextConfirmed==null?'':' · '+(e.nextConfirmed?'confirmed':'estimated')}`],
  ['Next Estimates',e.nextEpsEstimate==null?'EPS —':'EPS '+e.nextEpsEstimate,`revenue ${e.nextRevenueEstimate==null?'—':'$'+Number(e.nextRevenueEstimate).toLocaleString('en-US',{maximumFractionDigits:0})}`],
 ].map(x=>`<tr><td class="l"><b>${x[0]}</b></td><td>${x[1]}</td><td class="l"><span class="note">${x[2]}</span></td></tr>`).join('');
 const reasons=(r.gateReasons||[]).map(g=>`<span class="chip" style="color:${tone};border-color:${chipBd(tone)}">${finEsc(g.rule||'rule')}: <span class="note" style="margin-left:4px">${finEsc(g.detail||'')}</span></span>`).join(' ');
 return behaviorCard+`<div class="card">
   <div class="dh"><h2 class="t">财务状态<span class="t-en">FMP</span></h2><span class="chip" style="color:${tone};border-color:${chipBd(tone)}">${finEsc(r.gate)}</span><span class="nm">${finEsc(r.name||s.name)} · ${(D.asOfDate||D.generatedAt||'').slice(0,10)||'—'}</span></div>
   <div class="badges">${top.map(x=>`<div class="badge"><div class="l">${x[0]}</div><div class="v">${x[1]}</div><div class="note" style="font-size:var(--t-xs);margin-top:2px">${x[2]}</div></div>`).join('')}</div>
   ${reasons?`<div style="margin-top:12px;display:flex;gap:6px;flex-wrap:wrap">${reasons}</div>`:''}
  </div>
  <div class="card">
   <div class="dh"><h2 class="t">财务拆解<span class="t-en">QUALITY</span></h2><span class="nm">盈利 · 资产负债 · 现金流 · 增长 · 估值合理性</span></div>
   <div class="scroll"><table><thead><tr><th class="l">Block</th><th class="l">Primary</th><th class="l">Secondary</th></tr></thead><tbody>${metricRows}</tbody></table></div>
  </div>
  <div class="card">
   <div class="dh"><h2 class="t">财报日历<span class="t-en">EARNINGS</span></h2><span class="nm">历史超预期记录与下一财报窗口</span></div>
   <div class="scroll"><table><thead><tr><th class="l">Item</th><th>Value</th><th class="l">Context</th></tr></thead><tbody>${earningsRows}</tbody></table></div>
   <div class="note" style="margin-top:10px;line-height:1.6">Event-risk index is higher when earnings are close, valuation/financial quality is weak, or the name has higher beta. It flags review timing; it is not an earnings prediction.</div>
  </div>`;
}
function aiqTone(g){return {ALLOW_PLAN:'#4FB286',ALLOW_DD:'#72C49B',ALLOW:'#4FB286',WATCH:'#E8B339',WATCH_RESET:'#D59C3D',PORTFOLIO_BLOCK:'#C985E8',BLOCK:'#E5707A',DATA_REVIEW:'#6FA8DC'}[g]||'#888D96';}
function aiqScoreColor(v){return v>=86?'#4FB286':(v>=76?'#E8B339':'#E5707A');}
function aiSemiQuantCard(){
 const D=DATA.aiSemiQuant;
 if(!D||!Array.isArray(D.scores)||!D.scores.length){
  return `<div class="card t1"><div class="dh"><h2 class="t">AI半导体<span class="t-en">AI-SemiQuant</span></h2><span class="nm">产业链量化评分 · 资金瀑布 · 组合敞口</span></div><div class="note" style="line-height:1.8">暂无 AI-SemiQuant 数据。<br>生成方式：运行 <b>python3 scripts/ai_semi_quant.py</b> 写入 <b>output/ai_semi_quant.json</b> 和 <b>output/ai_semi_quant_report.md</b>，再跑 <b>python3 sync.py</b> 或 <b>python3 generate.py --no-fetch</b>。<b>研究框架，非投资建议。</b></div></div>`;
 }
 const sum=D.summary||{},leaders=sum.leaders||[],weights=D.factorWeights||{},labels=D.factorLabels||{},model=D.modelCard||{},gateCounts=sum.gateCounts||{},familyCounts=sum.gateFamilyCounts||{},torque=D.torqueOverlay||model.torqueOverlay||{};
 const gateChip=g=>{const c=aiqTone(g);return `<span class="chip" style="color:${c};border-color:${chipBd(c)}">${g}</span>`;};
 const capFmt=v=>{if(v==null)return'—';v=Number(v);if(!isFinite(v))return'—';if(v>=1e12)return'$'+(v/1e12).toFixed(2)+'T';if(v>=1e9)return'$'+(v/1e9).toFixed(1)+'B';if(v>=1e6)return'$'+(v/1e6).toFixed(0)+'M';return'$'+Math.round(v).toLocaleString('en-US');};
 const reasonTxt=r=>{const x=((r.gateReasons||[])[0]||{}).detail||r.gateNote||'';return x.length>92?x.slice(0,89)+'...':x;};
 const peerRankTxt=r=>r.peerPercentileDisplay||((r.peerGroupSize!=null&&r.peerGroupSize<3)?`N/A · n=${r.peerGroupSize}`:`P${r.peerPercentile??'—'}${r.peerGroupSize!=null?' · n='+r.peerGroupSize:''}`);
 const top=leaders.map(x=>`<span class="chip" style="color:${aiqScoreColor(x.score)};border-color:${chipBd(aiqScoreColor(x.score))}">${x.ticker} ${x.score}${x.percentile?' · P'+x.percentile:''} · ${x.gate}</span>`).join(' ');
 const rankChips=(arr,key)=>((arr||[]).map(x=>`<span class="chip" style="color:${key==='tactical'?'#6FA8DC':aiqScoreColor(x.score)};border-color:${chipBd(key==='tactical'?'#6FA8DC':aiqScoreColor(x.score))}">${x.ticker} ${x.score}${x.percentile?' · P'+x.percentile:''} · ${x.gate}</span>`).join(' '));
 const cards=[
  ['数据日期',D.marketDataAsOf||'—','Yahoo adjusted close when available'],
  ['Gate Families',`${familyCounts.ALLOW||0} / ${familyCounts.WATCH||0} / ${familyCounts.BLOCK||0} / ${familyCounts.DATA_REVIEW||0}`,'ALLOW / WATCH / BLOCK / REVIEW 聚合口径'],
  ['Detailed Gates',`PLAN ${gateCounts.ALLOW_PLAN||0} · DD ${gateCounts.ALLOW_DD||0} · RESET ${gateCounts.WATCH_RESET||0} · PF ${gateCounts.PORTFOLIO_BLOCK||0}`,'v0.3 把“可研究”和“可加仓”拆开'],
  ['Structural 权重','30 / 24 / 24 / 22','市值成长从结构分拆成 torque overlay'],
  ['Model Audit',`v${model.modelVersion||'—'} · 趋势满分数量：${model.momentumScoreAt100Count??'—'}`,'趋势分不应大量打满；异常进入 DATA_REVIEW'],
 ];
 const rows=D.scores.slice(0,24).map((r,i)=>{const m=r.market||{},p=r.portfolio||{},c=aiqScoreColor(r.finalScore);
  return `<tr>
   <td>${i+1}</td>
   <td class="l"><b>${r.ticker}</b><br><span class="note">${r.name}</span></td>
   <td class="l">${r.node}<br><span class="note">${r.role}</span></td>
   <td>${gateChip(r.gate)}</td>
   <td><span style="font-family:var(--f-mono);font-size:15px;color:${c}">${r.finalScore}</span><br><span class="note">P${r.universePercentile??'—'} / peer ${peerRankTxt(r)}</span></td>
   <td>${r.structuralScore??'—'} → ${r.torqueAdjustedScore??'—'}<br><span class="note">torque +${r.torqueBonus||0} / fragile -${r.fragilityPenalty||0}</span></td>
   <td>${(r.factors||{}).sizeGrowthTorque??'—'}<br><span class="note">${capFmt(m.marketCapUsd)} · ${m.marketCapBucket||'—'}</span></td>
   <td>${r.tacticalScore}<br><span class="note">趋 ${r.trendScore} / 险 ${r.riskScore}</span></td>
   <td>${m.displayPrice||'—'}${m.priceUsd&&m.currency!=='USD'?`<br><span class="note">≈ ${fmt(m.priceUsd)}</span>`:''}</td>
   <td class="${cls(m.ret3m)}">${pct(m.ret3m)}</td>
   <td class="${cls(m.vs50)}">${pct(m.vs50)}</td>
   <td>${p.weightPct?`${p.weightPct.toFixed(2)}%`:'—'}${p.held?'<br><span class="note">持有中 · penalty '+(r.portfolioPenalty||0)+'</span>':''}</td>
   <td class="l"><span class="note">${reasonTxt(r)||'—'}</span></td>
  </tr>`;}).join('');
 const mobile=D.scores.slice(0,12).map(r=>{const m=r.market||{},p=r.portfolio||{},c=aiqScoreColor(r.finalScore);
  return `<div class="smc">
   <div class="smc-top"><b class="smc-sym">${r.ticker}</b><span class="smc-state" style="color:${c}">总分 ${r.finalScore} · P${r.universePercentile??'—'}</span><span class="smc-pnl" style="color:${aiqTone(r.gate)}">${r.gate}</span></div>
   <div class="smc-mid">${r.node} · ${m.displayPrice||'—'} · 结构 ${r.structuralScore??'—'}→${r.torqueAdjustedScore??'—'} · torque ${(r.factors||{}).sizeGrowthTorque??'—'} · ${capFmt(m.marketCapUsd)}${m.ret3m==null?'':' · 3M '+pct(m.ret3m)}</div>
   <div class="smc-bias">${reasonTxt(r)||r.gateNote||''}${p.held?` <span class="chip" style="color:var(--mut);border-color:var(--line)">仓位 ${p.weightPct}%</span>`:''}</div></div>`;}).join('');
 const fRows=Object.keys(weights).map(k=>`<tr><td class="l">${labels[k]||k}</td><td>${Math.round(weights[k]*100)}%</td><td class="l">${({pricingPower:'市场份额、稀缺性、毛利率和合约涨价能力',profitElasticity:'跨过固定成本后新增收入转化为净利润的速度',capexConversion:'客户/晶圆厂资本开支转成订单和收入的确定性',valuationGrowth:'增长耐久度与估值之间的性价比'}[k]||'')}</td></tr>`).join('')+
  `<tr><td class="l">${labels.sizeGrowthTorque||'Size/Growth Torque'}</td><td>overlay</td><td class="l">最高 +${torque.maxBonus??6}，并按客户集中、波动、过热和脆弱性扣分；不计入结构护城河。</td></tr>`;
 const wRows=(D.capitalWaterfall||[]).map(s=>`<tr><td class="l">${s.stage}</td><td class="l">${(s.beneficiaries||[]).join(', ')}</td><td class="l">${(s.leadingKpis||[]).join(' · ')}</td></tr>`).join('');
 const eRows=(D.capitalFlowEdges||[]).map(e=>`<tr><td class="l">${e.source}</td><td class="l">${e.target}</td><td class="l">${e.edgeType}</td><td>${e.weight}</td><td>${Math.round((e.confidence||0)*100)}%</td></tr>`).join('');
 const nRows=(sum.byNode||[]).map(n=>`<tr><td class="l">${n.node}</td><td>${n.count}</td><td>${n.avgScore}</td><td class="l">${(n.leaders||[]).join(', ')}</td></tr>`).join('');
 const auditRows=[
  ['Model version',model.modelVersion||'—'],
  ['Universe',model.universeSize??'—'],
  ['Missing price',model.missingPriceCount??'—'],
  ['Missing market cap',model.missingMarketCapCount??'—'],
  ['DATA_REVIEW',model.dataReviewCount??'—'],
  ['Soft data flags',model.softDataReviewCount??'—'],
  ['Momentum = 100',model.momentumScoreAt100Count??'—'],
  ['Risk floor',(model.thresholds||{}).riskFloor??'—'],
  ['Single-name cap',((model.thresholds||{}).maxSinglePositionWeight??'—')+'%'],
 ].map(r=>`<tr><td class="l">${r[0]}</td><td>${r[1]}</td></tr>`).join('');
 const hardFlags=(model.hardDataFlags||[]).slice(0,8),softFlags=(model.softDataFlags||[]).slice(0,8);
 const flagRows=hardFlags.concat(softFlags).map(f=>`<tr><td class="l"><b>${f.ticker||'—'}</b><br><span class="note">${f.name||''}</span></td><td>${f.severity||'—'}</td><td class="l">${f.rule||'—'}</td><td class="l">${f.detail||'—'}</td></tr>`).join('')||'<tr><td class="l" colspan="4">No named data-quality flags.</td></tr>';
 const tacticalRaw=sum.tacticalLeadersRaw||[],tacticalInv=sum.tacticalLeadersInvestable||[];
 const tacticalRows=Array.from({length:Math.max(tacticalRaw.length,tacticalInv.length)}).map((_,i)=>{const a=tacticalRaw[i]||{},b=tacticalInv[i]||{};return `<tr><td>${i+1}</td><td class="l">${a.ticker||'—'}</td><td>${a.score??'—'}</td><td>${a.gate?gateChip(a.gate):'—'}</td><td class="l">${b.ticker||'—'}</td><td>${b.score??'—'}</td><td>${b.gate?gateChip(b.gate):'—'}</td></tr>`;}).join('');
 const riskRows=D.scores.slice(0,12).map(r=>{const b=r.riskBreakdown||{};return `<tr><td class="l">${r.ticker}</td><td>${r.riskScore}</td><td>${b.technicalOverextension||'—'}</td><td>${b.cycle||'—'}</td><td>${b.valuation||'—'}</td><td>${b.geopolitical||'—'}</td><td>${b.portfolioConcentration||'—'}</td></tr>`;}).join('');
 const src=(D.sources||[]).map(s=>`<li><a href="${s.url}" target="_blank" rel="noreferrer">${s.name}</a> <span class="note">${s.use||''}</span></li>`).join('');
 return `<div class="card t1">
  <div class="dh"><h2 class="t">AI半导体<span class="t-en">AI-SemiQuant</span></h2><span class="nm">${D.subtitle||'AI 半导体全产业链量化评分'} · ${D.marketDataAsOf||'结构数据'}</span></div>
  <div class="note" style="line-height:1.7;margin-bottom:10px"><b>核心用法：</b>先看产业链瓶颈和结构分，再用趋势/风险闸门决定是否进入尽调队列。${D.disclaimer||'研究框架，非投资建议。'}</div>
 <div class="badges">${cards.map(x=>`<div class="badge"><div class="l">${x[0]}</div><div class="v">${x[1]}</div><div class="note" style="font-size:var(--t-xs);margin-top:2px">${x[2]}</div></div>`).join('')}</div>
  <div style="margin-top:12px"><span class="cap">当前最高分</span><div style="display:flex;gap:7px;flex-wrap:wrap;margin-top:7px">${top}</div></div>
  <div style="margin-top:12px"><span class="cap">Strategic Rank</span><div style="display:flex;gap:7px;flex-wrap:wrap;margin-top:7px">${rankChips(sum.strategicLeaders,'strategic')}</div></div>
  <div style="margin-top:12px"><span class="cap">Raw Tactical Rank</span><div style="display:flex;gap:7px;flex-wrap:wrap;margin-top:7px">${rankChips(sum.tacticalLeadersRaw,'tactical')}</div></div>
  <div style="margin-top:12px"><span class="cap">Investable Tactical Rank</span><div style="display:flex;gap:7px;flex-wrap:wrap;margin-top:7px">${rankChips(sum.tacticalLeadersInvestable,'tactical')}</div></div>
  <div style="margin-top:12px"><span class="cap">Wait-for-Reset / Portfolio Block</span><div style="display:flex;gap:7px;flex-wrap:wrap;margin-top:7px">${rankChips(sum.resetWatchlist,'strategic')||'<span class="note">—</span>'}</div></div>
  <div class="scroll score-tablewrap" style="margin-top:12px"><table><thead><tr><th>Rank</th><th class="l">代码</th><th class="l">产业链节点</th><th>闸门</th><th>总分</th><th>结构→弹性</th><th>市值成长</th><th>战术</th><th>价格</th><th>3M</th><th>vs50</th><th>组合权重</th><th class="l">主要原因</th></tr></thead><tbody>${rows}</tbody></table></div>
  <div class="score-cards" aria-label="AI-SemiQuant 手机卡片">${mobile}</div>
  <div class="note" style="margin-top:10px;line-height:1.65">ALLOW_PLAN = 可进入分批计划评估；ALLOW_DD = 进入深度尽调；WATCH_RESET = 结构强但过热/风险/趋势需重置；PORTFOLIO_BLOCK = 公司质量不一定差，但组合集中度禁止继续加。任何实际下单仍要叠加 QQQ regime、组合集中度、止损和仓位上限。</div>
 </div>
 ${foldCard('资金瀑布 Capital Waterfall','钱从云厂 capex 流到瓶颈节点',`<div class="scroll"><table><thead><tr><th class="l">阶段</th><th class="l">受益标的</th><th class="l">领先指标</th></tr></thead><tbody>${wRows}</tbody></table></div>`,0,(D.capitalWaterfall||[]).length+' 段')}
 ${foldCard('资本流边权 Capital Flow Edges','静态研究图开始量化：边权 × 置信度',`<div class="scroll"><table><thead><tr><th class="l">Source</th><th class="l">Target</th><th class="l">Edge</th><th>权重</th><th>置信度</th></tr></thead><tbody>${eRows}</tbody></table></div><div class="note" style="margin-top:8px">这是 v0.3.1 的第一版产业资金边权；后续可接入真实订单、capex、13F/ETF/外资流来替代人工先验。</div>`,0,(D.capitalFlowEdges||[]).length+' edges')}
 ${foldCard('因子权重与节点均分','结构评分口径',`<div class="scroll"><table><thead><tr><th class="l">因子</th><th>权重</th><th class="l">含义</th></tr></thead><tbody>${fRows}</tbody></table></div><div class="scroll" style="margin-top:10px"><table><thead><tr><th class="l">节点</th><th>公司数</th><th>均分</th><th class="l">代表</th></tr></thead><tbody>${nRows}</tbody></table></div>`,0,'5 因子')}
 ${foldCard('Raw vs Investable Tactical','战术强弱不等于可加仓',`<div class="scroll"><table><thead><tr><th>Rank</th><th class="l">Raw Tactical</th><th>分</th><th>Gate</th><th class="l">Investable Tactical</th><th>分</th><th>Gate</th></tr></thead><tbody>${tacticalRows}</tbody></table></div><div class="note" style="margin-top:8px">Raw tactical 可以包含 BLOCK / DATA_REVIEW；Investable tactical 排除 hard BLOCK、PORTFOLIO_BLOCK 和 DATA_REVIEW，避免把“信号强”误读成“可以加”。</div>`,0,'v0.3.1')}
 ${foldCard('风险拆分 Risk Breakdown','不是“公司好坏”，而是风险类型叠加',`<div class="scroll"><table><thead><tr><th class="l">代码</th><th>风险分</th><th>技术拥挤</th><th>周期</th><th>估值/体量</th><th>地缘</th><th>组合集中</th></tr></thead><tbody>${riskRows}</tbody></table></div>`,0,'top 12')}
 ${foldCard('Model Card / Score Audit','每次复跑都要可审计',`<div class="scroll"><table><thead><tr><th class="l">项目</th><th>值</th></tr></thead><tbody>${auditRows}</tbody></table></div><div class="scroll" style="margin-top:10px"><table><thead><tr><th class="l">代码</th><th>级别</th><th class="l">规则</th><th class="l">原因</th></tr></thead><tbody>${flagRows}</tbody></table></div>`,0,`v${model.modelVersion||'—'}`)}
 ${foldCard('来源与边界','当前事实需要随财报 / 产业数据更新',`<ul style="margin:0;padding-left:18px;line-height:1.8">${src}</ul><div class="note" style="margin-top:10px">结构因子来自本次目标文件和参考研究的人工先验；价格和趋势来自 Yahoo Finance（如果可用）。请在财报、法说会、TrendForce/SEMI/Counterpoint 数据更新后重跑并复核。<b>非投资建议。</b></div>`,0,(D.sources||[]).length+' sources')}`;
}
function aicsTone(g){return aiqTone(g);}
function aicsScoreColor(v){return v>=86?'#4FB286':(v>=76?'#E8B339':(v>=66?'#B89030':'#E5707A'));}
function aicsGateChip(g){const c=aicsTone(g);return `<span class="chip" style="color:${c};border-color:${chipBd(c)}">${g||'—'}</span>`;}
function aicsCap(v){if(v==null)return'—';v=Number(v);if(!isFinite(v))return'—';if(v>=1e12)return'$'+(v/1e12).toFixed(2)+'T';if(v>=1e9)return'$'+(v/1e9).toFixed(1)+'B';if(v>=1e6)return'$'+(v/1e6).toFixed(0)+'M';return'$'+Math.round(v).toLocaleString('en-US');}
function aicsReason(r){const x=((r.gateReasons||[])[0]||{}).detail||r.gateNote||'';return x.length>110?x.slice(0,107)+'...':x;}
function aicsMapSvg(D){
 const rows=(D.scores||[]).slice(0,26),edges=(D.relationshipEdges||[]).slice(0,34),W=940,H=430;
 if(!rows.length)return'<div class="note">暂无 AICS 图谱数据。</div>';
 const laneName={demand:'Demand',chip:'AI Chip / ASIC',foundry:'Foundry',memory:'HBM / Memory',pack:'Packaging / Equipment',server:'Server ODM'};
 const laneX={demand:70,chip:220,foundry:370,memory:520,pack:690,server:850};
 const laneOf=r=>{const p=(r.peerGroup||'').toLowerCase(),role=(r.valueChainRole||'').toLowerCase();
   if(p.includes('accelerator')||p.includes('asic')||role.includes('networking'))return'chip';
   if(p.includes('foundry'))return'foundry';
   if(p.includes('memory'))return'memory';
   if(p.includes('packaging')||p.includes('equipment'))return'pack';
   if(p.includes('server'))return'server';
   return'pack';};
 const buckets={chip:[],foundry:[],memory:[],pack:[],server:[]};
 rows.forEach(r=>(buckets[laneOf(r)]||buckets.pack).push(r));
 const pos={
  hyperscaler_capex:{x:laneX.demand,y:92,label:'Cloud Capex',score:84,external:true},
  hbm_makers:{x:laneX.memory,y:330,label:'HBM Makers',score:80,external:true},
  advanced_packaging_shortage:{x:laneX.pack,y:330,label:'Pkg Shortage',score:78,external:true},
  ai_rack_demand:{x:laneX.server,y:330,label:'AI Racks',score:72,external:true}
 };
 Object.keys(buckets).forEach(k=>{const a=buckets[k],gap=Math.min(54,Math.max(30,300/Math.max(a.length,1)));const top=78-(Math.min(a.length,5)-1)*4;
   a.forEach((r,i)=>{pos[r.companyId]={x:laneX[k],y:top+i*gap,label:r.ticker,score:r.finalInvestmentScore,r:r};});});
 let el=`<rect x="16" y="18" width="${W-32}" height="${H-36}" rx="8" fill="${C.bg2}" stroke="${C.line}"/>`;
 Object.keys(laneX).forEach(k=>{const x=laneX[k];el+=`<line x1="${x}" y1="52" x2="${x}" y2="${H-48}" stroke="${C.hair}"/><text x="${x}" y="38" fill="${C.mut}" font-size="10" text-anchor="middle">${laneName[k]}</text>`;});
 edges.forEach(e=>{const a=pos[e.sourceCompanyId],b=pos[e.targetCompanyId];if(!a||!b)return;const w=1+Math.max(0,Math.min(100,e.edgeWeight||0))/28,op=.18+Math.max(0,Math.min(100,e.sourceConfidence||50))/170;
   el+=`<path d="M ${a.x+16} ${a.y} C ${(a.x+b.x)/2} ${a.y}, ${(a.x+b.x)/2} ${b.y}, ${b.x-16} ${b.y}" fill="none" stroke="${C.accent}" stroke-width="${w.toFixed(1)}" stroke-opacity="${op.toFixed(2)}"><title>${e.sourceName} → ${e.targetName} · ${e.relationshipType} · 权重 ${e.edgeWeight}</title></path>`;});
 Object.keys(pos).forEach(id=>{const n=pos[id],r=n.r,score=n.score||50,rad=n.external?14:Math.max(8,Math.min(18,8+(score-55)/5)),fill=n.external?C.panel:aicsScoreColor(score),stroke=r?(r.riskScore<45?C.red:(r.riskScore>65?C.green:C.line)):C.line;
   const op=n.external ? .18 : .88;
   const click=r?` onclick="aicsSelect('${r.companyId}')" style="cursor:pointer"`:'';el+=`<g${click}><circle cx="${n.x}" cy="${n.y}" r="${rad}" fill="${fill}" fill-opacity="${op}" stroke="${stroke}" stroke-width="${r?2:1}"><title>${n.label}${r?' · score '+r.finalInvestmentScore+' · '+r.gate:''}</title></circle><text x="${n.x}" y="${n.y+rad+13}" fill="${r?C.mut2:C.mut}" font-size="10" text-anchor="middle">${n.label}</text></g>`;});
 return `<div class="chartbox"><svg id="aics-map" role="img" aria-label="AICS industry map with weighted supply-chain edges" viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet">${el}</svg></div>`;
}
function aicsCompanyHtml(r){
 if(!r)return'<div class="note">暂无公司数据。</div>';
 const attr=((DATA.aics||{}).returnAttribution||[]).find(x=>x.ticker===r.ticker)||{};
 const edgeIn=((DATA.aics||{}).relationshipEdges||[]).filter(e=>e.targetCompanyId===r.companyId).slice(0,5);
 const edgeOut=((DATA.aics||{}).relationshipEdges||[]).filter(e=>e.sourceCompanyId===r.companyId).slice(0,5);
 const risks=r.riskBreakdown||{},m=r.market||{},p=r.portfolio||{};
 const subs=[
  ['Bottleneck',r.bottleneckPowerScore],['Growth',r.growthRealizationScore],['Profit Quality',r.profitQualityScore],
  ['Industrial Flow',r.industrialCapitalFlowScore],['Financial Flow',r.financialCapitalFlowScore],['Valuation',r.valuationScore],['Momentum',r.momentumScore],['Risk',r.riskScore]
 ].map(x=>`<tr><td class="l">${x[0]}</td><td style="color:${aicsScoreColor(x[1])}">${x[1]??'—'}</td></tr>`).join('');
 const edges=[...edgeIn.map(e=>`<tr><td class="l">${e.sourceName} → ${e.targetName}</td><td>${e.edgeWeight}</td><td>${e.sourceConfidence}%</td></tr>`),...edgeOut.map(e=>`<tr><td class="l">${e.sourceName} → ${e.targetName}</td><td>${e.edgeWeight}</td><td>${e.sourceConfidence}%</td></tr>`)].join('');
 const reasons=(r.gateReasons||[]).map(x=>`<li><b>${x.rule||'reason'}:</b> ${x.detail||''}</li>`).join('');
 return `<div class="dh"><h2 class="t">${r.ticker}<span class="t-en">${r.name||''}</span></h2><span class="nm">${r.valueChainRole||''} · ${r.peerGroup||''}</span></div>
  <div class="badges">
   <div class="badge"><div class="l">Final</div><div class="v" style="color:${aicsScoreColor(r.finalInvestmentScore)}">${r.finalInvestmentScore}</div></div>
   <div class="badge"><div class="l">Gate</div><div class="v">${aicsGateChip(r.gate)}</div></div>
   <div class="badge"><div class="l">Quality Rank</div><div class="v">#${r.companyQualityRank}</div></div>
   <div class="badge"><div class="l">Stock Rank</div><div class="v">#${r.stockAttractivenessRank}</div></div>
   <div class="badge"><div class="l">Price</div><div class="v">${m.displayPrice||'—'}</div></div>
   <div class="badge"><div class="l">Portfolio</div><div class="v">${p.weightPct?p.weightPct.toFixed(2)+'%':'—'}</div></div>
  </div>
  <div class="note" style="line-height:1.7"><b>Thesis:</b> ${r.thesis||'—'}</div>
  <div class="scroll" style="margin-top:10px"><table><thead><tr><th class="l">Score</th><th>Value</th></tr></thead><tbody>${subs}</tbody></table></div>
  <div class="note" style="margin-top:10px"><b>Gate reasons</b></div><ul style="margin:6px 0 0;padding-left:18px;color:var(--mut);font-size:var(--t-sm);line-height:1.7">${reasons||'<li>No gate reason recorded.</li>'}</ul>
  <div class="scroll" style="margin-top:10px"><table><thead><tr><th class="l">Risk</th><th>Level</th></tr></thead><tbody>${Object.keys(risks).map(k=>`<tr><td class="l">${k}</td><td>${risks[k]}</td></tr>`).join('')}</tbody></table></div>
  <div class="scroll" style="margin-top:10px"><table><thead><tr><th class="l">Relationship edge</th><th>Weight</th><th>Conf.</th></tr></thead><tbody>${edges||'<tr><td class="l">No direct edge in MVP graph.</td><td>—</td><td>—</td></tr>'}</tbody></table></div>
  <div class="note" style="margin-top:10px">3M attribution proxy: total ${attr.totalReturn==null?'—':pct(attr.totalReturn)} · earnings ${attr.earningsRevisionContribution==null?'—':pct(attr.earningsRevisionContribution)} · multiple ${attr.valuationMultipleContribution==null?'—':pct(attr.valuationMultipleContribution)} · flow ${attr.capitalFlowMomentumContribution==null?'—':pct(attr.capitalFlowMomentumContribution)}. ${attr.method||''}</div>`;
}
function aicsSelect(id){
	 const D=DATA.aics||{},r=(D.scores||[]).find(x=>x.companyId===id||x.ticker===id);
	 const p=document.getElementById('aics-company-panel');
	 if(p&&r){p.innerHTML=aicsCompanyHtml(r);p.scrollIntoView({block:'nearest',behavior:PRM?'auto':'smooth'});}
	}
function aicsScoreboardSortRows(rows,key){
 const n=(r,k)=>Number(r&&r[k]!=null?r[k]:-999),flow=r=>n(r,'industrialCapitalFlowScore')+n(r,'financialCapitalFlowScore'),delta=r=>r.scoreChange1W!=null?Number(r.scoreChange1W):(r.scoreChange1M!=null?Number(r.scoreChange1M):Number(r.scoreChange1Run||-999));
 const pfAdj=r=>Number(r.finalInvestmentScore||0)-Number(r.portfolioPenalty||0),sym=r=>(r.ticker||'');
 const ranked=rows.slice();
 ranked.sort((a,b)=>{
  if(key==='delta')return delta(b)-delta(a)||n(b,'finalInvestmentScore')-n(a,'finalInvestmentScore')||sym(a).localeCompare(sym(b));
  if(key==='flow')return flow(b)-flow(a)||n(b,'finalInvestmentScore')-n(a,'finalInvestmentScore')||sym(a).localeCompare(sym(b));
  if(key==='bottleneck')return n(b,'bottleneckPowerScore')-n(a,'bottleneckPowerScore')||n(b,'finalInvestmentScore')-n(a,'finalInvestmentScore')||sym(a).localeCompare(sym(b));
  if(key==='risk')return n(b,'riskScore')-n(a,'riskScore')||n(b,'finalInvestmentScore')-n(a,'finalInvestmentScore')||sym(a).localeCompare(sym(b));
  if(key==='portfolio')return pfAdj(b)-pfAdj(a)||n(b,'finalInvestmentScore')-n(a,'finalInvestmentScore')||sym(a).localeCompare(sym(b));
  if(key==='peer')return (a.peerGroup||'').localeCompare(b.peerGroup||'')||n(b,'finalInvestmentScore')-n(a,'finalInvestmentScore')||sym(a).localeCompare(sym(b));
  return n(b,'finalInvestmentScore')-n(a,'finalInvestmentScore')||sym(a).localeCompare(sym(b));
 });
 return ranked;
}
function aicsScoreboardRowsHtml(rows){
 return rows.map((r,i)=>`<tr style="cursor:pointer" tabindex="0" role="button" onclick="aicsSelect('${r.companyId}')" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();aicsSelect('${r.companyId}');}">
  <td>${i+1}</td><td class="l"><b>${r.ticker}</b><br><span class="note">${r.name}</span></td><td class="l">${r.valueChainRole}<br><span class="note">${r.peerGroup}</span></td><td>${aicsGateChip(r.gate)}</td>
  <td style="color:${aicsScoreColor(r.finalInvestmentScore)}">${r.finalInvestmentScore}</td><td>${r.bottleneckPowerScore}</td><td>${r.growthRealizationScore}</td><td>${r.profitQualityScore}</td><td>${r.industrialCapitalFlowScore}</td><td>${r.financialCapitalFlowScore}</td><td>${r.valuationScore}</td><td>${r.momentumScore}</td><td>${r.riskScore}</td><td>${r.scoreChange1W==null?'—':(r.scoreChange1W>=0?'+':'')+r.scoreChange1W}</td><td>${r.scoreChange1M==null?'—':(r.scoreChange1M>=0?'+':'')+r.scoreChange1M}</td><td>${(r.portfolio||{}).weightPct?((r.portfolio||{}).weightPct.toFixed(2)+'%'):'—'}</td><td class="l"><span class="note">${aicsReason(r)||'—'}</span></td>
 </tr>`).join('');
}
function aicsScoreboardCardsHtml(rows){
 return rows.slice(0,14).map(r=>`<div class="smc" onclick="aicsSelect('${r.companyId}')"><div class="smc-top"><b class="smc-sym">${r.ticker}</b><span class="smc-state" style="color:${aicsScoreColor(r.finalInvestmentScore)}">AICS ${r.finalInvestmentScore}</span><span class="smc-pnl" style="color:${aicsTone(r.gate)}">${r.gate}</span></div><div class="smc-mid">${r.valueChainRole} · bottleneck ${r.bottleneckPowerScore} · flow ${r.industrialCapitalFlowScore}/${r.financialCapitalFlowScore} · risk ${r.riskScore}</div><div class="smc-bias">${aicsReason(r)}</div></div>`).join('');
}
function aicsScoreboardRender(){
 const D=DATA.aics||{},sel=document.getElementById('aics-score-sort'),key=sel?sel.value:'final',rows=aicsScoreboardSortRows(D.scores||[],key);
 const body=document.getElementById('aics-scoreboard-body'),cards=document.getElementById('aics-scoreboard-cards');
 if(body)body.innerHTML=aicsScoreboardRowsHtml(rows);
 if(cards)cards.innerHTML=aicsScoreboardCardsHtml(rows);
}
function aicsSigned(n,d=1){if(n==null||!isFinite(Number(n)))return'—';const v=Number(n);return(v>=0?'+':'−')+Math.abs(v).toFixed(d).replace(/\.0$/,'');}
function aicsScenarioModel(){return((DATA.aics||{}).scenarioModel||{});}
function aicsScenarioControls(){return aicsScenarioModel().controls||[];}
function aicsScenarioDefaults(){return Object.assign({},aicsScenarioModel().defaultAssumptions||{});}
function aicsScenarioOption(key,id){
 const c=aicsScenarioControls().find(x=>x.key===key)||{},opts=c.options||[];
 return opts.find(x=>x.id===id)||opts.find(x=>x.id===c.default)||{};
}
function aicsScenarioGate(base,score){
 const g=(base||{}).gate||'WATCH',risk=(base||{}).riskScore||0;
 if(g==='DATA_REVIEW'||g==='PORTFOLIO_BLOCK')return g;
 if(risk<35)return'BLOCK';
 if(score>=82&&risk>=45)return'ALLOW_DD';
 if(score>=70)return'WATCH';
 if(score>=62)return'WATCH_RESET';
 return'BLOCK';
}
function aicsScenarioSelected(){
 const a=aicsScenarioDefaults();
 document.querySelectorAll('[data-aics-scen]').forEach(el=>{a[el.dataset.aicsScen]=el.value;});
 return a;
}
function aicsScenarioCompute(assumptions){
 const D=DATA.aics||{},M=aicsScenarioModel(),selected=Object.assign(aicsScenarioDefaults(),assumptions||{});
 const baseBy={};(D.scores||[]).forEach(r=>{baseBy[r.companyId]=r;baseBy[r.ticker]=r;});
 const rows=(M.companySensitivities||[]).map(sr=>{
  const base=baseBy[sr.companyId]||baseBy[sr.ticker]||{},sens=sr.sensitivities||{};
  let sd=0,rev=0,mar=0,eps=0,val=0;
  Object.keys(sens).forEach(k=>{const s=Number(sens[k]||0),o=aicsScenarioOption(k,selected[k]);sd+=s*Number(o.scoreImpact||0);rev+=s*Number(o.revenueImpact||0);mar+=s*Number(o.marginImpact||0);eps+=s*Number(o.epsImpact||0);val+=s*Number(o.valuationImpact||0);});
  const baseScore=Number(sr.baseScore??base.finalInvestmentScore??0),scenarioScore=Math.round(Math.max(0,Math.min(100,baseScore+sd)));
  return {companyId:sr.companyId,ticker:sr.ticker,name:base.name||'',baseScore,scoreDelta:+sd.toFixed(1),scenarioScore,revenueImpactPct:+rev.toFixed(1),marginImpactPct:+mar.toFixed(1),epsImpactPct:+eps.toFixed(1),valuationImpactPct:+val.toFixed(1),gate:base.gate,newGate:aicsScenarioGate(base,scenarioScore),portfolioWeightPct:Number(sr.portfolioWeightPct||0)};
 });
 const winners=rows.slice().sort((a,b)=>(b.scoreDelta-a.scoreDelta)||(b.scenarioScore-a.scenarioScore)||a.ticker.localeCompare(b.ticker)).slice(0,6);
 const losers=rows.slice().sort((a,b)=>(a.scoreDelta-b.scoreDelta)||(a.scenarioScore-b.scenarioScore)||a.ticker.localeCompare(b.ticker)).slice(0,6);
 let tw=0,ps=0,pr=0,pe=0,pv=0,held=[];
 rows.forEach(r=>{const w=Number(r.portfolioWeightPct||0);if(w>0){tw+=w;ps+=r.scoreDelta*w;pr+=r.revenueImpactPct*w;pe+=r.epsImpactPct*w;pv+=r.valuationImpactPct*w;held.push({ticker:r.ticker,weightPct:w,scoreDelta:r.scoreDelta,newGate:r.newGate});}});
 const den=tw||1;
 return {assumptions:selected,rows,winners,losers,portfolioImpact:{heldWeightPct:+tw.toFixed(2),scoreDelta:tw?+(ps/den).toFixed(2):null,revenueImpactPct:tw?+(pr/den).toFixed(2):null,epsImpactPct:tw?+(pe/den).toFixed(2):null,valuationImpactPct:tw?+(pv/den).toFixed(2):null,heldRows:held.sort((a,b)=>b.weightPct-a.weightPct).slice(0,8)}};
}
function aicsScenarioRunHtml(run){
 const p=(run||{}).portfolioImpact||{},rows=(run||{}).rows||[],w=(run||{}).winners||[],l=(run||{}).losers||[];
 const badges=[['PF score',aicsSigned(p.scoreDelta,2),'current-weighted delta'],['PF revenue',pct(p.revenueImpactPct),'directional impact'],['PF EPS',pct(p.epsImpactPct),'directional impact'],['PF valuation',pct(p.valuationImpactPct),'multiple sensitivity']].map(x=>`<div class="badge"><div class="l">${x[0]}</div><div class="v">${x[1]}</div><div class="note" style="font-size:var(--t-xs);margin-top:2px">${x[2]}</div></div>`).join('');
 const mini=(arr)=>arr.slice(0,5).map(x=>`<span class="chip" style="color:${x.scoreDelta>=0?C.green:C.red};border-color:${x.scoreDelta>=0?'var(--chip-bd-green)':'var(--chip-bd-red)'}">${x.ticker} ${aicsSigned(x.scoreDelta)} → ${x.scenarioScore}</span>`).join(' ');
 const detail=rows.slice().sort((a,b)=>Math.abs(b.scoreDelta)-Math.abs(a.scoreDelta)||a.ticker.localeCompare(b.ticker)).slice(0,12).map(r=>`<tr><td class="l"><b>${r.ticker}</b><br><span class="note">${r.name||''}</span></td><td>${r.baseScore}</td><td style="color:${aicsScoreColor(r.scenarioScore)}">${r.scenarioScore}</td><td>${aicsSigned(r.scoreDelta)}</td><td>${pct(r.revenueImpactPct)}</td><td>${ppf(r.marginImpactPct)}</td><td>${pct(r.epsImpactPct)}</td><td>${pct(r.valuationImpactPct)}</td><td>${aicsGateChip(r.newGate)}</td></tr>`).join('');
 const held=(p.heldRows||[]).map(r=>`<tr><td class="l">${r.ticker}</td><td>${r.weightPct.toFixed(2)}%</td><td>${aicsSigned(r.scoreDelta)}</td><td>${aicsGateChip(r.newGate)}</td></tr>`).join('')||'<tr><td class="l">No matched AICS holdings</td><td>—</td><td>—</td><td>—</td></tr>';
 return `<div class="badges">${badges}</div><div style="margin-top:10px"><span class="cap">Winners</span><div style="display:flex;gap:7px;flex-wrap:wrap;margin-top:7px">${mini(w)||'<span class="note">—</span>'}</div></div><div style="margin-top:10px"><span class="cap">Losers</span><div style="display:flex;gap:7px;flex-wrap:wrap;margin-top:7px">${mini(l)||'<span class="note">—</span>'}</div></div><div class="scroll" style="margin-top:10px"><table><thead><tr><th class="l">Company</th><th>Base</th><th>Scenario</th><th>Score Δ</th><th>Revenue</th><th>Margin</th><th>EPS</th><th>Valuation</th><th>New gate</th></tr></thead><tbody>${detail}</tbody></table></div><div class="scroll" style="margin-top:10px"><table><thead><tr><th class="l">Held ticker</th><th>Weight</th><th>Score Δ</th><th>New gate</th></tr></thead><tbody>${held}</tbody></table></div>`;
}
function aicsScenarioApplyPreset(id){
 const M=aicsScenarioModel(),preset=((DATA.aics||{}).scenarioResults||[]).find(x=>x.id===id),vals=Object.assign({},M.defaultAssumptions||{},(preset||{}).controlValues||{});
 document.querySelectorAll('[data-aics-scen]').forEach(el=>{if(vals[el.dataset.aicsScen])el.value=vals[el.dataset.aicsScen];});
 aicsScenarioRender();
}
function aicsScenarioReset(){
 const vals=aicsScenarioDefaults();
 document.querySelectorAll('[data-aics-scen]').forEach(el=>{if(vals[el.dataset.aicsScen])el.value=vals[el.dataset.aicsScen];});
 aicsScenarioRender();
}
function aicsScenarioRender(){
 const target=document.getElementById('aics-scenario-live');if(!target)return;
 target.innerHTML=aicsScenarioRunHtml(aicsScenarioCompute(aicsScenarioSelected()));
}
function aicsScenarioControlHtml(){
 const M=aicsScenarioModel(),controls=M.controls||[];
 if(!controls.length)return'<div id="aics-scenario" class="note">Scenario model unavailable. Regenerate AICS data to enable controls.</div>';
 const controlsHtml=controls.map(c=>{const opts=(c.options||[]).map(o=>`<option value="${o.id}" ${(M.defaultAssumptions||{})[c.key]===o.id?'selected':''}>${o.label}</option>`).join('');return `<label class="badge" style="min-width:180px;display:block"><span class="l">${c.label}</span><select data-aics-scen="${c.key}" onchange="aicsScenarioRender()" style="width:100%;margin-top:6px;background:var(--bg2);color:var(--txt);border:1px solid var(--line);border-radius:6px;padding:6px 8px;font:600 12px/1.2 var(--f-ui)">${opts}</select></label>`;}).join('');
 const presets=((DATA.aics||{}).scenarioResults||[]).map(s=>`<button class="chip" type="button" onclick="aicsScenarioApplyPreset('${s.id}')" style="background:transparent;cursor:pointer;color:var(--mut);border-color:var(--line)">${s.name}</button>`).join('');
 return `<div id="aics-scenario"><div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px">${presets}<button class="chip" type="button" onclick="aicsScenarioReset()" style="background:transparent;cursor:pointer;color:var(--mut);border-color:var(--line)">Reset</button></div><div class="badges">${controlsHtml}</div><div id="aics-scenario-live" style="margin-top:10px">${aicsScenarioRunHtml(M.defaultRun||aicsScenarioCompute(M.defaultAssumptions||{}))}</div><div class="note" style="margin-top:8px">情景是敏感度模型：假设冲击 × 公司系数，输出为方向性 revenue / margin / EPS / score / valuation proxy，不是盈利预测或下单信号。</div></div>`;
}
function aicsToolTab(){
 const D=DATA.aics;
 if(!D||!Array.isArray(D.scores)||!D.scores.length){
  return `<div class="card t1"><div class="dh"><h2 class="t">AICS产业链<span class="t-en">AICS</span></h2><span class="nm">AI Semiconductor Capital Flow & Scoring System</span></div><div class="note" style="line-height:1.8">暂无 AICS 数据。<br>生成方式：运行 <b>python3 scripts/aics_tool.py</b> 写入 <b>output/aics.json</b> 和 <b>output/aics_report.md</b>，再跑 <b>python3 generate.py --no-fetch</b>。<b>研究框架，非投资建议。</b></div></div>`;
 }
 const rows=D.scores||[],overlay=D.portfolioOverlay||{},model=D.modelCard||{},alerts=D.alerts||[],top=rows.slice(0,5);
 const families=rows.reduce((a,r)=>{a[r.gateFamily||'OTHER']=(a[r.gateFamily||'OTHER']||0)+1;return a;},{});
 const chip=x=>`<span class="chip" style="color:${aicsScoreColor(x.finalInvestmentScore)};border-color:${chipBd(aicsScoreColor(x.finalInvestmentScore))}">${x.ticker} ${x.finalInvestmentScore} · ${x.gate}</span>`;
 const dVal=x=>x.scoreChange1W!=null?x.scoreChange1W:(x.scoreChange1M!=null?x.scoreChange1M:x.scoreChange1Run);
 const dLbl=x=>x.scoreChange1W!=null?'1W':(x.scoreChange1M!=null?'1M':'run');
 const deltaTop=(D.scoreDeltas||[]).filter(x=>dVal(x)!=null).sort((a,b)=>(dVal(b)||0)-(dVal(a)||0)).slice(0,4).map(x=>{const v=dVal(x),c=v>=0?C.green:C.red;return `<span class="chip" style="color:${c};border-color:${v>=0?'var(--chip-bd-green)':'var(--chip-bd-red)'}">${x.ticker} ${dLbl(x)} ${v>=0?'+':''}${v}</span>`;}).join('')||'<span class="note">no history window</span>';
 const cockpit=[
  ['Model Date',D.marketDataAsOf||'structure-only','market data as of'],
  ['Gate Families',`${families.ALLOW||0} / ${families.WATCH||0} / ${families.BLOCK||0} / ${families.DATA_REVIEW||0}`,'ALLOW / WATCH / BLOCK / REVIEW'],
  ['Portfolio AICS',`${overlay.totalAicsWeightPct??0}%`,overlay.concentrationWarning?'concentration warning':'current overlap'],
  ['Graph Edges',(D.relationshipEdges||[]).length,'weighted supply-chain links'],
  ['Alerts',alerts.length,'score, risk, data-quality flags'],
 ].map(x=>`<div class="badge"><div class="l">${x[0]}</div><div class="v">${x[1]}</div><div class="note" style="font-size:var(--t-xs);margin-top:2px">${x[2]}</div></div>`).join('');
	 const sortedRows=aicsScoreboardSortRows(rows,'final');
	 const tableRows=aicsScoreboardRowsHtml(sortedRows);
	 const mobile=aicsScoreboardCardsHtml(sortedRows);
 const flowRows=(D.capitalFlow&&D.capitalFlow.industrial||[]).map(x=>`<tr><td class="l">${x.role}</td><td>${x.count}</td><td>${x.avgIndustrialFlow}</td><td>${x.avgFinancialFlow}</td><td class="l">${(x.leaders||[]).join(', ')}</td></tr>`).join('');
 const edgeRows=(D.relationshipEdges||[]).slice(0,18).map(e=>`<tr><td class="l">${e.sourceName} → ${e.targetName}</td><td class="l">${e.relationshipType}</td><td>${e.edgeWeight}</td><td>${e.sourceConfidence}%</td><td>${e.technicalDependency}</td><td>${e.capacityTightness}</td></tr>`).join('');
 const attrSum=D.returnAttributionSummary||{},attrAvg=attrSum.averageContribution||{};
 const attrBadge=[['Earnings',attrAvg.earningsRevisionContribution],['Multiple',attrAvg.valuationMultipleContribution],['Flow/Mom',attrAvg.capitalFlowMomentumContribution],['FX/Residual',attrAvg.fxDividendResidualContribution]].map(x=>`<div class="badge"><div class="l">${x[0]}</div><div class="v">${pct(x[1])}</div><div class="note" style="font-size:var(--t-xs);margin-top:2px">avg ${attrSum.window||'3M'} contribution</div></div>`).join('');
 const attrRow=x=>`<tr><td class="l"><b>${x.ticker}</b><br><span class="note">${x.qualityFlag||'normal'}</span></td><td>${pct(x.totalReturn)}</td><td>${pct(x.earningsRevisionContribution)}</td><td>${pct(x.valuationMultipleContribution)}</td><td>${pct(x.capitalFlowMomentumContribution)}</td><td>${pct(x.fxDividendResidualContribution)}</td></tr>`;
 const attrTop=(attrSum.topTotalReturn||[]).slice(0,8).map(attrRow).join('');
 const attrWorst=(attrSum.worstTotalReturn||[]).slice(0,8).map(attrRow).join('');
 const attrResidual=(attrSum.largestResiduals||[]).slice(0,8).map(attrRow).join('');
 const attrLow=(attrSum.lowQualityRallies||[]).slice(0,8).map(x=>`<tr><td class="l">${x.ticker}</td><td>${pct(x.totalReturn)}</td><td>${pct(x.earningsRevisionContribution)}</td><td>${pct(x.valuationMultipleContribution)}</td><td>${pct(x.capitalFlowMomentumContribution)}</td></tr>`).join('')||'<tr><td class="l">None flagged</td><td>—</td><td>—</td><td>—</td><td>—</td></tr>';
 const alertRows=alerts.map(a=>`<tr><td class="l">${a.type||'alert'}${a.subtype?'<br><span class="note">'+a.subtype+'</span>':''}</td><td class="l">${a.severity}</td><td class="l">${a.ticker||'portfolio'}</td><td class="l">${a.title}</td><td class="l"><span class="note">${a.detail}</span>${a.metric?`<br><span class="note">${a.metric}: ${a.value??'—'} / ${a.threshold??'—'}</span>`:''}</td></tr>`).join('')||'<tr><td class="l">clean</td><td class="l">—</td><td class="l">—</td><td class="l">No AICS alerts</td><td class="l"><span class="note">No score, risk, valuation, flow, edge, threshold, concentration, or data-quality alert on this run.</span></td></tr>';
 const back=D.backtest||{},req=model.requirements||{};
 const bw=(b,k)=>{const x=((b.windows||{})[k]||{}).returnPct;return x==null?'—':pct(x);};
 const be=(b,k)=>{const x=((b.windows||{})[k]||{}).excessVsSOXX;return x==null?'—':ppf(x);};
	 const backRows=(back.baskets||[]).map(b=>`<tr><td class="l"><b>${b.name}</b><br><span class="note">${(b.members||[]).join(', ')}</span></td><td>${bw(b,'1M')}<br><span class="note">vs SOXX ${be(b,'1M')}</span></td><td>${bw(b,'3M')}<br><span class="note">vs SOXX ${be(b,'3M')}</span></td><td>${bw(b,'6M')}<br><span class="note">vs SOXX ${be(b,'6M')}</span></td><td>${bw(b,'1Y')}<br><span class="note">vs SOXX ${be(b,'1Y')}</span></td></tr>`).join('');
	 const hist=back.historyValidation||{};
	 const histRows=(hist.baskets||[]).map(b=>`<tr><td class="l"><b>${b.name}</b><br><span class="note">${(b.latestMembers||[]).join(', ')||'no sample'}</span></td><td>${b.rebalanceCount??0}<br><span class="note">${b.observationCount??0} obs</span></td><td>${pct(b.avgNetReturnPct??b.avgReturnPct)}<br><span class="note">gross ${pct(b.avgGrossReturnPct)} · cost ${ppf(b.avgTransactionCostPct)}</span></td><td>${ppf(b.avgExcessVsUniversePct)}<br><span class="note">SOXX ${ppf(b.avgExcessVsSOXXPct)} · SMH ${ppf(b.avgExcessVsSMHPct)}</span></td><td>${pct(b.cagrPct)}<br><span class="note">DD ${pct(b.maxDrawdownPct)}</span></td><td>${b.sharpe??'—'} / ${b.sortino??'—'}<br><span class="note">hit ${b.hitRatePct==null?'—':b.hitRatePct.toFixed(1)+'%'} · turn ${b.turnoverPct==null?'—':b.turnoverPct.toFixed(1)+'%'}</span></td></tr>`).join('');
	 const cal=hist.calendarValidation||{};
	 const calRows=['monthly','quarterly'].map(k=>{const x=cal[k]||{},baskets=x.baskets||[],b=baskets.find(row=>(row.rebalanceCount||0)>0)||baskets[0]||{},periods=x.selectedPeriods||[];return `<tr><td class="l"><b>${k[0].toUpperCase()+k.slice(1)}</b><br><span class="note">${periods.slice(-4).join(', ')||'no periods'}</span></td><td>${x.status||'—'}<br><span class="note">${x.snapshotCount??0} snapshots · ${x.evaluatedPairs??0} pairs</span></td><td>${pct(b.avgNetReturnPct??b.avgReturnPct)}<br><span class="note">cost ${ppf(b.avgTransactionCostPct)}</span></td><td>${ppf(b.avgExcessVsUniversePct)}<br><span class="note">SOXX ${ppf(b.avgExcessVsSOXXPct)} · SMH ${ppf(b.avgExcessVsSMHPct)}</span></td><td>${pct(b.cagrPct)}<br><span class="note">DD ${pct(b.maxDrawdownPct)}</span></td></tr>`;}).join('');
	 const modelRows=[['AICS version',model.aicsModelVersion],['Base version',model.baseAiSemiQuantVersion],['Universe',model.universeSize],['Missing price',model.missingPriceCount],['DATA_REVIEW',model.dataReviewCount],['Edges',model.edgeCount],['All rows have gate reasons',req.allRowsHaveGateReasons],['Edges weighted',req.allEdgesWeighted]].map(r=>`<tr><td class="l">${r[0]}</td><td>${r[1]??'—'}</td></tr>`).join('');
 return `<div class="card t1">
  <div class="dh"><h2 class="t">AICS产业链<span class="t-en">AICS</span></h2><span class="nm">${D.subtitle||''} · ${D.marketDataAsOf||'structure data'}</span></div>
  <div class="note" style="line-height:1.7"><b>用途：</b>把 AI 半导体从故事投资拆成产业链权力、财务兑现、工业资金流、金融资金流、估值、动量和风险。研究框架，非投资建议。</div>
  <div class="badges">${cockpit}</div>
  <div style="margin-top:12px"><span class="cap">Top AICS</span><div style="display:flex;gap:7px;flex-wrap:wrap;margin-top:7px">${top.map(chip).join(' ')}</div></div>
  <div style="margin-top:12px"><span class="cap">Score Deltas</span><div style="display:flex;gap:7px;flex-wrap:wrap;margin-top:7px">${deltaTop}</div></div>
  <div style="margin-top:12px;display:flex;gap:7px;flex-wrap:wrap"><a class="chip" href="#aics-map" style="color:var(--mut);border-color:var(--line)">Map</a><a class="chip" href="#aics-scoreboard" style="color:var(--mut);border-color:var(--line)">Scoreboard</a><a class="chip" href="#aics-flows" style="color:var(--mut);border-color:var(--line)">Flows</a><a class="chip" href="#aics-company-panel" style="color:var(--mut);border-color:var(--line)">Company</a><a class="chip" href="#aics-attribution" style="color:var(--mut);border-color:var(--line)">Attribution</a><a class="chip" href="#aics-scenario" style="color:var(--mut);border-color:var(--line)">Scenario</a><a class="chip" href="#aics-alerts" style="color:var(--mut);border-color:var(--line)">Alerts</a><a class="chip" href="#aics-backtest" style="color:var(--mut);border-color:var(--line)">Backtest</a></div>
 </div>
 <div class="card"><div class="dh"><h2 class="t">Industry Map<span class="t-en">MAP</span></h2><span class="nm">node score · edge weight · source confidence</span></div>${aicsMapSvg(D)}<div class="note" style="margin-top:8px">节点大小约等于分数强弱，边线粗细为产业链依赖权重。点击公司节点可更新下方 tearsheet。</div></div>
	 <div class="card" id="aics-scoreboard"><div class="dh"><h2 class="t">Scoreboard<span class="t-en">RANK</span></h2><span class="nm">公司质量和股票吸引力分开看</span></div><div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:10px"><label class="note" for="aics-score-sort">Sort</label><select id="aics-score-sort" aria-label="AICS scoreboard sort" onchange="aicsScoreboardRender()" style="background:var(--bg2);color:var(--txt);border:1px solid var(--line);border-radius:6px;padding:6px 8px;font:600 12px/1.2 var(--f-ui)"><option value="final">Final score</option><option value="delta">1W / latest delta</option><option value="flow">Capital-flow score</option><option value="bottleneck">Bottleneck score</option><option value="risk">Risk score</option><option value="portfolio">Portfolio-adjusted score</option><option value="peer">Peer group</option></select></div><div class="scroll score-tablewrap"><table><thead><tr><th>Rank</th><th class="l">Ticker</th><th class="l">Role</th><th>Gate</th><th>Final</th><th>Bottle</th><th>Growth</th><th>Profit</th><th>Ind Flow</th><th>Mkt Flow</th><th>Value</th><th>Mom</th><th>Risk</th><th>Δ1W</th><th>Δ1M</th><th>PF</th><th class="l">Reason</th></tr></thead><tbody id="aics-scoreboard-body">${tableRows}</tbody></table></div><div id="aics-scoreboard-cards" class="score-cards" aria-label="AICS mobile cards">${mobile}</div></div>
 <div class="card" id="aics-flows"><div class="dh"><h2 class="t">Capital Flow Cockpit<span class="t-en">FLOWS</span></h2><span class="nm">industrial flow vs financial flow</span></div><div class="scroll"><table><thead><tr><th class="l">Role</th><th>Names</th><th>Industrial</th><th>Financial</th><th class="l">Leaders</th></tr></thead><tbody>${flowRows}</tbody></table></div><div class="scroll" style="margin-top:10px"><table><thead><tr><th class="l">Edge</th><th class="l">Type</th><th>Weight</th><th>Conf.</th><th>Tech</th><th>Capacity</th></tr></thead><tbody>${edgeRows}</tbody></table></div></div>
 <div class="card" id="aics-company-panel">${aicsCompanyHtml(rows[0])}</div>
 ${foldCard('Return Attribution','3M proxy decomposition and rally quality',`<div id="aics-attribution" class="badges">${attrBadge}</div><div class="scroll" style="margin-top:10px"><table><thead><tr><th class="l">Top return</th><th>Total</th><th>Earnings</th><th>Multiple</th><th>Flow/Mom</th><th>FX/Residual</th></tr></thead><tbody>${attrTop||'<tr><td class="l">No attribution rows</td><td>—</td><td>—</td><td>—</td><td>—</td><td>—</td></tr>'}</tbody></table></div><div class="scroll" style="margin-top:10px"><table><thead><tr><th class="l">Worst return</th><th>Total</th><th>Earnings</th><th>Multiple</th><th>Flow/Mom</th><th>FX/Residual</th></tr></thead><tbody>${attrWorst||'<tr><td class="l">No attribution rows</td><td>—</td><td>—</td><td>—</td><td>—</td><td>—</td></tr>'}</tbody></table></div><div class="scroll" style="margin-top:10px"><table><thead><tr><th class="l">Largest residual</th><th>Total</th><th>Earnings</th><th>Multiple</th><th>Flow/Mom</th><th>FX/Residual</th></tr></thead><tbody>${attrResidual||'<tr><td class="l">No attribution rows</td><td>—</td><td>—</td><td>—</td><td>—</td><td>—</td></tr>'}</tbody></table></div><div class="scroll" style="margin-top:10px"><table><thead><tr><th class="l">Low-quality rally</th><th>Total</th><th>Earnings</th><th>Multiple</th><th>Flow/Mom</th></tr></thead><tbody>${attrLow}</tbody></table></div><div class="note" style="margin-top:8px">${attrSum.method||''}</div>`,0,(attrSum.availableCount??0)+' names')}
 ${foldCard('Scenario Lab','custom assumptions + preset cases',aicsScenarioControlHtml(),0,(D.scenarioModel&&D.scenarioModel.controls||[]).length+' controls')}
 ${foldCard('Alert Center','score / risk / valuation / flow / edge / data',`<div class="scroll" id="aics-alerts"><table><thead><tr><th class="l">Type</th><th class="l">Severity</th><th class="l">Ticker</th><th class="l">Alert</th><th class="l">Detail</th></tr></thead><tbody>${alertRows}</tbody></table></div>`,0,alerts.length+' alerts')}
	 ${foldCard('Backtest / Model Audit','static baskets + history-backed validation state',`<div id="aics-backtest" class="note" style="line-height:1.7"><b>Static status:</b> ${back.status||'—'} · ${(back.method||back.note||'')}</div><div class="scroll" style="margin-top:10px"><table><thead><tr><th class="l">Static basket</th><th>1M</th><th>3M</th><th>6M</th><th>1Y</th></tr></thead><tbody>${backRows||'<tr><td class="l">No backtest rows</td><td>—</td><td>—</td><td>—</td><td>—</td></tr>'}</tbody></table></div><div class="note" style="line-height:1.7;margin-top:10px"><b>History validation:</b> ${hist.status||'—'} · ${hist.snapshotCount??0} snapshots · ${hist.evaluatedPairs??0} evaluated pairs · cost ${(hist.rules||{}).transactionCostBps??0} bps. ${hist.method||''}</div><div class="scroll" style="margin-top:10px"><table><thead><tr><th class="l">History basket</th><th>Rebalances</th><th>Net return</th><th>Excess</th><th>CAGR / drawdown</th><th>Sharpe / Sortino</th></tr></thead><tbody>${histRows||'<tr><td class="l">Need at least two price-bearing AICS history snapshots</td><td>0</td><td>—</td><td>—</td><td>—</td><td>—</td></tr>'}</tbody></table></div><div class="scroll" style="margin-top:10px"><table><thead><tr><th class="l">Calendar mode</th><th>Status</th><th>Net return</th><th>Excess</th><th>CAGR / drawdown</th></tr></thead><tbody>${calRows||'<tr><td class="l">Monthly / quarterly</td><td>not enough snapshots</td><td>—</td><td>—</td><td>—</td></tr>'}</tbody></table></div><div class="scroll" style="margin-top:10px"><table><thead><tr><th class="l">Model item</th><th>Value</th></tr></thead><tbody>${modelRows}</tbody></table></div><div class="note" style="margin-top:8px">${back.note||''}</div>`,0,model.aicsModelVersion||'v—')}`;
}
function aiwTone(g){return {BUILD_MODEL:'#4FB286',DEEP_DIVE:'#72C49B',WATCH_EVIDENCE:'#E8B339',TRACK_CATALYST:'#D6A64A',WATCH_RESET:'#D59C3D',SPECULATIVE:'#B98CE0',ACTION_READY:'#45B47E',ALLOW_REVIEW:'#4FB286',VERIFY_DATA:'#D6A64A',WAIT_RESET:'#D59C3D',WAIT_SETUP:'#E8B339',PORTFOLIO_BLOCK:'#C985E8',BLOCK_DATA:'#E5707A',BLOCK:'#E5707A',DATA_REVIEW:'#6FA8DC'}[g]||'#888D96';}
function aiwScoreColor(v){return v>=82?'#4FB286':(v>=70?'#E8B339':'#E5707A');}
function aiWatchlistCard(){
 const D=DATA.aiWatchlist;
 if(!D||!Array.isArray(D.scores)||!D.scores.length){
  return `<div class="card t1"><div class="dh"><h2 class="t">AI观察池<span class="t-en">WATCH</span></h2><span class="nm">旧能力重估 · 价格刷新 · 评分闸门</span></div><div class="note" style="line-height:1.8">暂无 AI watchlist 数据。<br>生成方式：运行 <b>python3 scripts/ai_watchlist_score.py</b> 写入 <b>output/ai_watchlist.json</b>、<b>output/ai_watchlist_report.md</b> 和 <b>output/ai_watchlist.csv</b>，再跑 <b>python3 generate.py --no-fetch</b>。<b>研究框架，非投资建议。</b></div></div>`;
 }
 const sum=D.summary||{},model=D.modelCard||{},families=sum.gateFamilyCounts||{},gates=sum.gateCounts||{},entry=sum.entryGateCounts||{},queues=D.queues||{},allRows=D.scores||[];
 const researchRows=(queues.researchQueue&&queues.researchQueue.length?queues.researchQueue:allRows.filter(r=>!r.dataQuarantined));
 const entryRows=(queues.entryQueue&&queues.entryQueue.length?queues.entryQueue:researchRows.filter(r=>['ACTION_READY','ALLOW_REVIEW'].includes(r.entryGate)));
 const dataRows=(queues.dataReviewQueue&&queues.dataReviewQueue.length?queues.dataReviewQueue:allRows.filter(r=>r.dataQuarantined||r.dataQualitySeverity==='hard_review'));
 const gateChip=g=>`<span class="chip" style="color:${aiwTone(g)};border-color:${chipBd(aiwTone(g))}">${g||'—'}</span>`;
 const capFmt=v=>{if(v==null)return'—';v=Number(v);if(!isFinite(v))return'—';if(v>=1e12)return'$'+(v/1e12).toFixed(2)+'T';if(v>=1e9)return'$'+(v/1e9).toFixed(1)+'B';if(v>=1e6)return'$'+(v/1e6).toFixed(0)+'M';return'$'+Math.round(v).toLocaleString('en-US');};
 const reasonTxt=r=>{const x=((r.gateReasons||[])[0]||{}).detail||r.gateNote||'';return x.length>96?x.slice(0,93)+'...':x;};
 const scoreVal=v=>v==null?'—':v;
 const dirScore=r=>r.directionRelativeScore==null?'N/A':'D'+r.directionRelativeScore;
 const roleTxt=r=>`${r.clusterRole||'—'} · ${r.directionRankLabel||((r.directionRank||'—')+'/'+(r.directionSize||'—'))}`;
 const top=(sum.researchQueue||sum.leaders||researchRows.slice(0,8)).slice(0,10).map(x=>`<span class="chip" style="color:${aiwScoreColor(x.finalScore)};border-color:${chipBd(aiwScoreColor(x.finalScore))}">${x.ticker} ${x.finalScore} · ${dirScore(x)} · ${x.gate}</span>`).join(' ');
 const workChips=[...(sum.onePageModels||[]).slice(0,5),...(sum.supplyChainVerify||[]).slice(0,5),...(sum.evidenceUpgrade||[]).slice(0,5)].slice(0,12).map(x=>`<span class="chip" style="color:${aiwTone(x.gate)};border-color:${chipBd(aiwTone(x.gate))}">${x.ticker} · ${x.modelWorkstream||x.actionTier||'workstream'}</span>`).join(' ');
 const cards=[
  ['数据日期',D.marketDataAsOf||'—','Yahoo adjusted close when available'],
  ['Research Queue',model.researchQueueCount??researchRows.length,'clean / soft-review names only'],
  ['Entry Queue',`${entry.ACTION_READY||0} ready · ${entry.ALLOW_REVIEW||0} review`,'manual valuation and sizing still required'],
  ['Verify Before Entry',entry.VERIFY_DATA||0,'research allowed, entry blocked until data check'],
  ['Data Quarantine',model.dataQuarantineCount??dataRows.length,'hard review removed from main rank'],
  ['Universe',model.universeSize??allRows.length,'editable JSON watchlist'],
  ['Evidence Audit',`${model.ledgerSchemaCoverage??'—'}% slots · ${model.sourceUrlCoverage??'—'}% URLs`,'ledger coverage is not citation coverage'],
 ];
 const tableRows=researchRows.slice(0,32).map((r,i)=>{const m=r.market||{},p=r.portfolio||{},c=aiwScoreColor(r.finalScore),cr=r.crowdingRisk||{};
  return `<tr>
   <td>${r.researchQueueRank||i+1}</td>
   <td class="l"><b>${r.ticker}</b><br><span class="note">${r.name}</span></td>
   <td class="l">${r.actionTier||r.priorityTier||'—'}<br><span class="note">${r.modelWorkstream||'—'}</span></td>
   <td class="l">${r.direction||r.bucket}<br><span class="note">${roleTxt(r)} · ${r.oldLabel||''}</span></td>
   <td>${gateChip(r.gate)}</td>
   <td>${gateChip(r.entryGate)}</td>
   <td><span style="font-family:var(--f-mono);font-size:15px;color:${c}">${r.finalScore}</span><br><span class="note">P${r.universePercentile??'—'} · ${r.scoreBand||''}</span></td>
   <td>${dirScore(r)}<br><span class="note">${r.directionRankLabel||'—'}</span></td>
   <td>${scoreVal(r.entryScore)}<br><span class="note">setup ${scoreVal(r.setupScore)} / risk ${scoreVal(r.riskScore)}</span></td>
   <td>${r.structuralScore}<br><span class="note">证 ${r.evidenceScore??'—'} · ${r.evidenceLevel||'—'}</span></td>
   <td>${m.displayPrice||'—'}${m.priceUsd&&m.currency!=='USD'?`<br><span class="note">≈ ${fmt(m.priceUsd)}</span>`:''}</td>
   <td class="${cls(m.ret3m)}">${pct(m.ret3m)}</td>
   <td class="${cls(m.vs50)}">${pct(m.vs50)}</td>
   <td>${cr.priceExtension||'—'}<br><span class="note">${cr.narrativeHeat||'—'} heat</span></td>
   <td>${capFmt(m.marketCapUsd)}<br><span class="note">${m.marketCapBucket||'—'}</span></td>
   <td>${p.weightPct?`${p.weightPct.toFixed(2)}%`:'—'}${p.held?'<br><span class="note">持有中</span>':''}</td>
   <td class="l"><span class="note">${r.researchAction||reasonTxt(r)||'—'}</span></td>
  </tr>`;}).join('');
 const mobile=researchRows.slice(0,14).map(r=>{const m=r.market||{},c=aiwScoreColor(r.finalScore);return `<div class="smc"><div class="smc-top"><b class="smc-sym">${r.ticker}</b><span class="smc-state" style="color:${c}">Score ${r.finalScore} · ${dirScore(r)}</span><span class="smc-pnl" style="color:${aiwTone(r.gate)}">${r.gate}</span></div><div class="smc-mid">${r.modelWorkstream||r.bucket} · ${m.displayPrice||'—'} · 3M ${pct(m.ret3m)} · vs50 ${pct(m.vs50)}</div><div class="smc-bias">${reasonTxt(r)||r.thesis||''}</div></div>`;}).join('');
 const entryRowsHtml=entryRows.slice(0,18).map((r,i)=>{const d=r.entryDiagnostics||{};return `<tr><td>${r.entryQueueRank||i+1}</td><td class="l"><b>${r.ticker}</b><br><span class="note">${r.name}</span></td><td>${gateChip(r.entryGate)}</td><td>${r.entryScore??'—'}</td><td class="${cls(d.distanceTo50DMA)}">${pct(d.distanceTo50DMA)}</td><td class="${cls(d.distanceTo200DMA)}">${pct(d.distanceTo200DMA)}</td><td class="${cls(d.threeMonthReturn)}">${pct(d.threeMonthReturn)}</td><td class="${cls(d.relativeStrengthVsBenchmark3M)}">${pct(d.relativeStrengthVsBenchmark3M)}</td><td>${d.valuationBand||'—'}</td><td>${d.postEarningsGapRisk||'—'}</td></tr>`;}).join('')||'<tr><td class="l" colspan="10">No entry-review names after data and setup filters.</td></tr>';
 const quarantineRows=dataRows.slice(0,24).map((r,i)=>{const m=r.market||{},why=((r.dataQualityReasons||[])[0]||{});return `<tr><td>${r.dataReviewQueueRank||i+1}</td><td class="l"><b>${r.ticker}</b><br><span class="note">${r.name}</span></td><td>${gateChip(r.gate)}<br>${gateChip(r.entryGate)}</td><td>${r.finalScore}<br><span class="note">raw setup ${r.rawSetupScore??'—'}</span></td><td>${m.displayPrice||'—'}<br><span class="note">${m.currency||'—'}</span></td><td class="${cls(m.ret3m)}">${pct(m.ret3m)}</td><td class="l">${why.rule||'—'}<br><span class="note">${why.detail||r.entryGateNote||'—'}</span></td></tr>`;}).join('')||'<tr><td class="l" colspan="7">No hard data-review names.</td></tr>';
 const tierRows=(sum.byPriorityTier||[]).map(b=>`<tr><td class="l">${b.priorityTier}</td><td>${b.count}</td><td>${b.avgFinal}</td><td>${b.avgStructural}</td><td class="l">${(b.leaders||[]).join(', ')}</td></tr>`).join('');
 const stageRows=(sum.byStage||[]).map(b=>`<tr><td class="l">${b.stage}</td><td>${b.count}</td><td>${b.avgFinal}</td><td>${b.avgStructural}</td><td class="l">${(b.leaders||[]).join(', ')}</td></tr>`).join('');
 const directionRows=(sum.byDirection||[]).map(b=>`<tr><td class="l">${b.direction}</td><td>${b.count}</td><td>${b.avgFinal}</td><td>${b.avgStructural}</td><td class="l">${(b.leaders||[]).join(', ')}</td></tr>`).join('');
 const weights=D.factorWeights||{},labels=D.factorLabels||{};
 const factorRows=Object.keys(weights).map(k=>`<tr><td class="l">${labels[k]||k}</td><td>${Math.round(weights[k]*100)}%</td></tr>`).join('');
 const kpiRows=researchRows.slice(0,18).map(r=>`<tr><td class="l"><b>${r.ticker}</b><br><span class="note">${r.name}</span></td><td class="l">${(r.catalysts||r.watchKpis||[]).join(' · ')||'review earnings and revenue guide'}</td><td class="l">${(r.rebuttalChecks||[]).slice(0,2).join(' · ')||'—'}</td></tr>`).join('');
 const ledgerRows=researchRows.slice(0,18).map(r=>{const e=(r.evidenceLedger||[])[0]||{},a=r.evidenceAudit||{};return `<tr><td class="l"><b>${r.ticker}</b><br><span class="note">${r.name}</span></td><td class="l">${e.sourceType||'—'}</td><td>${e.sourceDate||'—'}</td><td>${e.rawConfidence||e.confidence||'—'} → ${e.confidence||'—'}${e.confidenceCapped?'<br><span class="note">URL cap</span>':''}</td><td>${e.sourceUrl?'attached':'missing'}</td><td>${a.sourceUrlCoverage??0}%</td><td class="l">${(e.claim||'').slice(0,120)}</td></tr>`;}).join('');
 const thesis=model.portfolioThesisExposure||{},thesisRows=(thesis.byBucket||[]).map(x=>`<tr><td class="l">${x.bucket}</td><td>${x.weightPct??0}%</td></tr>`).join('')||'<tr><td class="l">No mapped watchlist holdings</td><td>0%</td></tr>';
 const thesisHeld=(thesis.heldTickers||[]).map(x=>`<span class="chip" style="color:var(--mut);border-color:var(--line)">${x.ticker} · ${x.bucket} · ${x.weightPct}%</span>`).join(' ');
 const flags=(model.dataQualityFlags||[]).map(f=>`<tr><td class="l">${f.ticker}</td><td>${f.severity||'—'}</td><td class="l">${f.rule||'—'}</td><td class="l">${f.detail||'—'}</td></tr>`).join('')||'<tr><td class="l" colspan="4">No data-quality flags.</td></tr>';
 return `<div class="card t1">
  <div class="dh"><h2 class="t">AI观察池<span class="t-en">WATCH</span></h2><span class="nm">${D.subtitle||'旧能力重估观察池'} · ${D.marketDataAsOf||'结构数据'}</span></div>
  <div class="note" style="line-height:1.7;margin-bottom:10px"><b>核心用法：</b>Research Queue 按 tier first, score second 排旧能力错配；Entry Queue 只做人工估值/仓位讨论；Data Quarantine 先清洗再排名。${D.disclaimer||'研究框架，非投资建议。'}</div>
  <div class="badges">${cards.map(x=>`<div class="badge"><div class="l">${x[0]}</div><div class="v">${x[1]}</div><div class="note" style="font-size:var(--t-xs);margin-top:2px">${x[2]}</div></div>`).join('')}</div>
  <div style="margin-top:12px"><span class="cap">Tier-Prioritized Research Queue</span><div class="note" style="margin-top:3px">Sorted by action tier first, then research priority.</div><div style="display:flex;gap:7px;flex-wrap:wrap;margin-top:7px">${top}</div></div>
  <div style="margin-top:12px"><span class="cap">Model Workstreams</span><div style="display:flex;gap:7px;flex-wrap:wrap;margin-top:7px">${workChips||'<span class="note">No active workstreams.</span>'}</div></div>
  <div class="scroll score-tablewrap" style="margin-top:12px"><table><thead><tr><th>Rank</th><th class="l">代码</th><th class="l">动作层 / 工作流</th><th class="l">方向 / 角色</th><th>研究闸门</th><th>入场闸门</th><th>研究分</th><th>方向分</th><th>Entry</th><th>结构/证据</th><th>价格</th><th>3M</th><th>vs50</th><th>Crowding</th><th>市值</th><th>组合</th><th class="l">下一步</th></tr></thead><tbody>${tableRows}</tbody></table></div>
  <div class="score-cards" aria-label="AI Watchlist 手机卡片">${mobile}</div>
 </div>
 ${foldCard('Entry Queue','只表示可以进入人工估值、仓位和失效条件讨论',`<div class="scroll"><table><thead><tr><th>Rank</th><th class="l">代码</th><th>Gate</th><th>Entry</th><th>50DMA</th><th>200DMA</th><th>3M</th><th>vs SPY</th><th>Valuation</th><th>Gap risk</th></tr></thead><tbody>${entryRowsHtml}</tbody></table></div>`,0,entryRows.length+' names')}
 ${foldCard('Data Quarantine','hard review 熔断：价格 / FX / split / 市值未核验前不进主榜',`<div class="scroll"><table><thead><tr><th>Rank</th><th class="l">代码</th><th>Gate</th><th>Research</th><th>价格</th><th>3M</th><th class="l">需要核验</th></tr></thead><tbody>${quarantineRows}</tbody></table></div>`,0,dataRows.length+' names')}
 ${foldCard('优先级 Tiers','T1 先做财报模型；T2 盯估值和持续性；T3 看证据升级；T4 季度跟踪',`<div class="scroll"><table><thead><tr><th class="l">Tier</th><th>公司数</th><th>研究均分</th><th>结构均分</th><th class="l">代表</th></tr></thead><tbody>${tierRows}</tbody></table></div>`,0,(sum.byPriorityTier||[]).length+' tiers')}
 ${foldCard('阶段地图 Stages','第二阶段看 AI 工厂瓶颈；第三阶段看端侧 / physical AI / 应用变现',`<div class="scroll"><table><thead><tr><th class="l">Stage</th><th>公司数</th><th>研究均分</th><th>结构均分</th><th class="l">代表</th></tr></thead><tbody>${stageRows}</tbody></table></div><div class="scroll" style="margin-top:10px"><table><thead><tr><th class="l">Direction</th><th>公司数</th><th>研究均分</th><th>结构均分</th><th class="l">代表</th></tr></thead><tbody>${directionRows}</tbody></table></div>`,0,(sum.byDirection||[]).length+' directions')}
 ${foldCard('评分权重与验证清单','每个标的必须同时有催化剂和反证条件',`<div class="scroll"><table><thead><tr><th class="l">因子</th><th>权重</th></tr></thead><tbody>${factorRows}</tbody></table></div><div class="scroll" style="margin-top:10px"><table><thead><tr><th class="l">代码</th><th class="l">催化剂</th><th class="l">反证检查</th></tr></thead><tbody>${kpiRows}</tbody></table></div>`,0,model.modelVersion||'v—')}
 ${foldCard('Evidence Ledger','schema coverage vs citation coverage',`<div class="note" style="line-height:1.7;margin-bottom:8px">${model.ledgerSchemaCoverage??'—'}% evidence slots populated; ${model.sourceUrlCoverage??'—'}% source URLs attached (${model.sourceUrlAttachedCount??0}/${model.sourceUrlRequiredCount??0}). Missing URLs cap confidence at medium.</div><div class="scroll"><table><thead><tr><th class="l">代码</th><th class="l">Source</th><th>Date</th><th>Conf.</th><th>URL</th><th>Coverage</th><th class="l">Claim</th></tr></thead><tbody>${ledgerRows}</tbody></table></div>`,0,(model.sourceUrlCoverage??'—')+'% cited')}
 ${foldCard('Thesis Correlation','portfolio exposure by AI thesis bucket',`<div class="scroll"><table><thead><tr><th class="l">Bucket</th><th>Portfolio weight</th></tr></thead><tbody>${thesisRows}</tbody></table></div><div style="display:flex;gap:7px;flex-wrap:wrap;margin-top:10px">${thesisHeld||'<span class="note">No matching held watchlist names.</span>'}</div>`,0,(thesis.totalMappedWeightPct??0)+'% mapped')}
 ${foldCard('Data Quality','价格 / FX / 异常波动审计',`<div class="scroll"><table><thead><tr><th class="l">代码</th><th>级别</th><th class="l">规则</th><th class="l">原因</th></tr></thead><tbody>${flags}</tbody></table></div>`,0,(model.dataQualityFlags||[]).length+' flags')}`;
}
var MMSEL=null;
function mmEsc(s){return String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function mmData(){return DATA.marketMass||null;}
function mmSymbols(){
 const M=mmData(),S=M&&M.symbols?Object.keys(M.symbols):[];
 const pri={'QQQ':0,'^NDX':1,'VOO':2,'^GSPC':3};
 return S.sort((a,b)=>(pri[a]??10)-(pri[b]??10)||a.localeCompare(b));
}
function mmItem(sym){const M=mmData();return M&&M.symbols?M.symbols[sym]:null;}
function mmCurrent(item){return item&&item.current?item.current:null;}
function mmRegimeColor(r){return {active_center:'#4FB286',centered_mean_reverting:'#4FB286',detached_from_mass:'#E5707A',no_reliable_center:'#E5707A',transition_weak_center:'#C99A3A',mixed_gravity:'#C99A3A'}[r]||'#888D96';}
function mmRegimeLabel(r){return {active_center:'active center',detached_from_mass:'detached',no_reliable_center:'no center',transition_weak_center:'transition',centered_mean_reverting:'mean-reverting'}[r]||r||'—';}
function mmPx(sym,v){return v==null?'—':(String(sym||'').startsWith('^')?fmtN(v,2):fmt(v));}
function mmSigned(v,d=2){return v==null?'—':(v>=0?'+':'−')+Math.abs(v).toFixed(d);}
function mmBoundary(item){
 const c=mmCurrent(item),b=c&&c.selected_boundary;
 if(b&&b.lower_boundary!=null)return b;
 const M=mmData(),p=(M&&M.profile)||{},h=p.defaultHorizonDays||5,conf=p.defaultConfidence||0.80;
 return ((item&&item.boundaries)||[]).find(x=>Number(x.horizon_days)===Number(h)&&Math.abs(Number(x.confidence)-Number(conf))<0.001)||{};
}
function mmWarns(item){return ((item&&item.warnings)||[]).filter(Boolean);}
function mmConfidence(item){return (item&&item.dashboardConfidence)||{};}
function mmConfidenceColor(label){return {high:'#4FB286',medium:'#C99A3A',low:'#E5707A'}[label]||'#888D96';}
function mmPyramid(item){return (item&&item.pyramid)||{};}
function mmMassHealth(item){return (mmPyramid(item).massHealth)||{};}
function mmAgreement(item){return (mmPyramid(item).agreement)||{};}
function mmHealthColor(label){return {coherent_mass:'#4FB286',working_mass:'#6DAF7B',fragile_or_transition:'#C99A3A',low_friction_or_no_mass:'#E5707A'}[label]||'#888D96';}
function mmFrictionColor(label){return {strong_friction:'#4FB286',friction_present:'#6DAF7B',weak_friction:'#C99A3A',low_friction:'#E5707A',low_friction_escape_risk:'#E5707A'}[label]||'#888D96';}
function mmHealthLabel(label){return {coherent_mass:'coherent mass',working_mass:'working mass',fragile_or_transition:'fragile/transition',low_friction_or_no_mass:'low-friction/no mass'}[label]||label||'—';}
function mmFrictionLabel(label){return {strong_friction:'strong friction',friction_present:'friction present',weak_friction:'weak friction',low_friction:'low friction',low_friction_escape_risk:'escape-risk friction'}[label]||label||'—';}
function mmPyramidProfiles(item){const p=(mmPyramid(item).profiles)||{};const order=['tactical','swing','structural'];return order.filter(k=>p[k]).concat(Object.keys(p).filter(k=>!order.includes(k))).map(k=>p[k]);}
function mmFreshnessText(item){
 if(!item)return'—';
 if(item.stale)return item.staleReason?`stale · ${item.staleReason}`:'stale';
 return `fresh · price ${item.priceAsOf||'—'} · generated ${((item.freshness||{}).generatedAt||'').slice(0,10)||'—'}`;
}
function mmReliabilityNotes(item){
 const conf=mmConfidence(item),reasons=(conf.reasons||[]).map(r=>`<li>${mmEsc(r)}</li>`).join('');
 const warns=mmWarns(item).map(w=>`<li>${mmEsc(w)}</li>`).join('');
 const h=mmMassHealth(item),hreasons=(h.reasons||[]).map(r=>`<li>${mmEsc(r)}</li>`).join('');
 return `<details class="note" style="margin-top:10px;line-height:1.65"><summary style="cursor:pointer">Boundary reliability notes</summary><ul style="margin:8px 0 0 18px;padding:0">${reasons||'<li>No confidence reasons recorded.</li>'}${hreasons}${warns}</ul></details>`;
}
function marketMassStockBadge(sym){
 const item=mmItem(sym),c=mmCurrent(item);if(!c)return'';
 const color=mmRegimeColor(c.regime),b=mmBoundary(item);
 const conf=mmConfidence(item),confLabel=conf.label||'—',confColor=mmConfidenceColor(confLabel);
 const health=mmMassHealth(item),hLabel=health.label||'—',hColor=mmHealthColor(hLabel);
 const dist=c.distance_z==null?'—':mmSigned(c.distance_z,2)+'σ';
 const q=c.quality_score==null?'—':c.quality_score.toFixed(0);
 const build=c.volatility_build_up_score==null?'—':c.volatility_build_up_score.toFixed(0);
 const proxy=(item.fallbackVolTicker&&!item.volTicker)?' · proxy vol':'';
 const stale=item.stale?' · stale':'';
 const note=b&&b.lower_boundary!=null?`${mmPx(sym,b.lower_boundary)}–${mmPx(sym,b.upper_boundary)}`:`center ${mmPx(sym,c.center_price)}`;
 return `<div class="badge"><div class="l">重心边界</div><div class="v" style="font-size:var(--t-base);line-height:1.45"><span style="color:${hColor}">${mmHealthLabel(hLabel)}</span> · Q${q} · B${build} · ${dist}</div><div class="note" style="font-size:var(--t-xs);margin-top:2px"><span style="color:${color}">${mmRegimeLabel(c.regime)}</span> · ${note} · <span style="color:${confColor}">${confLabel}</span>${proxy}${stale}</div></div>`;
}
function marketMassLane(sym){
 const item=mmItem(sym),c=mmCurrent(item);
 if(!item||!c)return `<div class="mm-lane"><div class="k">${mmEsc(sym)}</div><div class="m">No market-mass snapshot.</div></div>`;
 const color=mmRegimeColor(c.regime),b=mmBoundary(item),conf=mmConfidence(item),confLabel=conf.label||'—';
 const h=mmMassHealth(item),hLabel=h.label||'—',fr=h.frictionLabel||'—';
 return `<div class="mm-lane"><div class="k">${mmEsc(sym)} <span style="color:${color}">●</span> <span style="color:${mmHealthColor(hLabel)};font-size:var(--t-xs)">${mmHealthLabel(hLabel)}</span></div><div class="m">price ${mmPx(sym,c.current_price)} · center ${mmPx(sym,c.center_price)}<br>Q${c.quality_score==null?'—':c.quality_score.toFixed(0)} / build ${c.volatility_build_up_score==null?'—':c.volatility_build_up_score.toFixed(0)} / ${mmSigned(c.distance_z,2)}σ<br><span style="color:${mmFrictionColor(fr)}">${mmFrictionLabel(fr)}</span> · ${confLabel} confidence<br>${b.lower_boundary!=null?`5d ${mmPx(sym,b.lower_boundary)}–${mmPx(sym,b.upper_boundary)}`:'boundary —'}<br>${mmEsc(item.stale?'stale':('as of '+(item.priceAsOf||'—')))}</div></div>`;
}
function marketMassChart(sym,item){
 const rows=((item&&item.history)||[]).filter(r=>r&&r.date&&r.close!=null);
 if(rows.length<2)return `<div class="note" style="padding:16px;line-height:1.7">No historical market-mass replay for ${mmEsc(sym)}. Run <b>python3 scripts/market_mass_dashboard.py</b> to rebuild the artifact.</div>`;
 const MOB=CHART_MOB,W=MOB?520:900,H=360,mL=AXM.mL,mR=AXM.mR,mT=16,mB=MOB?34:42;
 const xmin=+new Date(rows[0].date),xmax=+new Date(rows[rows.length-1].date);
 let vals=[];rows.forEach(r=>['close','center','lower_zone_low','lower_zone_high','upper_zone_low','upper_zone_high','lower_boundary','upper_boundary'].forEach(k=>{if(r[k]!=null&&isFinite(r[k]))vals.push(Number(r[k]));}));
 const yt=niceTicks(Math.min(...vals),Math.max(...vals),4),ymin=yt.min,ymax=yt.max;
 const xs=d=>mL+((+new Date(d)-xmin)/((xmax-xmin)||1))*(W-mL-mR);
 const yc=v=>mT+(1-(v-ymin)/((ymax-ymin)||1))*(H-mT-mB);
 let el=`<rect x="${mL}" y="${mT}" width="${W-mL-mR}" height="${H-mT-mB}" fill="${C.bg2}"/>`;
 yt.ticks.forEach(v=>{const y=yc(v);el+=`<line x1="${mL}" y1="${y}" x2="${W-mR}" y2="${y}" stroke="${C.hair}"/><text x="${mL-8}" y="${y+4}" fill="${C.mut}" font-size="11" text-anchor="end">${String(sym).startsWith('^')?fmtN(v,0):'$'+fmtN(v,0)}</text>`;});
 let mtx=monthTicks(xmin,xmax);if(mtx.length<2)mtx=[0,1,2,3,4,5].map(i=>{const t=xmin+(xmax-xmin)*i/5,dt=new Date(t);return{t,label:(dt.getMonth()+1)+'/'+dt.getDate()};});
 mtx.forEach(g=>{const x=xs(g.t);el+=`<line x1="${x}" y1="${mT}" x2="${x}" y2="${H-mB}" stroke="${C.hair}"/><text x="${x}" y="${H-mB+18}" fill="${C.mut}" font-size="11" text-anchor="middle">${g.label}</text>`;});
 function band(lo,hi,color,op){const f=rows.filter(r=>r[lo]!=null&&r[hi]!=null);if(f.length<2)return;const a=f.map(r=>`${xs(r.date).toFixed(1)},${yc(r[lo]).toFixed(1)}`).join(' ');const b=f.slice().reverse().map(r=>`${xs(r.date).toFixed(1)},${yc(r[hi]).toFixed(1)}`).join(' ');el+=`<polygon points="${a} ${b}" fill="${color}" fill-opacity="${op}"/>`;}
 function line(k,color,w,dash,klass){const f=rows.filter(r=>r[k]!=null);if(f.length<2)return;const pts=f.map(r=>`${xs(r.date).toFixed(1)},${yc(r[k]).toFixed(1)}`).join(' ');el+=`<polyline ${klass?`class="${klass}" `:''}points="${pts}" fill="none" stroke="${color}" stroke-width="${w||1.6}" ${dash?`stroke-dasharray="${dash}"`:''}/>`;}
 band('lower_zone_low','lower_zone_high',C.green,.12);band('upper_zone_low','upper_zone_high',C.red,.12);
 line('lower_boundary',C.green,1.2,'5 3');line('upper_boundary',C.red,1.2,'5 3');line('center',C.ref,1.7,'4 3');line('close',C.price,2.25,null,'draw');
 const bw=Math.max(2,(W-mL-mR)/rows.length);
 rows.forEach(r=>{const x=xs(r.date)-bw/2;el+=`<rect x="${x.toFixed(1)}" y="${H-mB+3}" width="${bw.toFixed(1)}" height="5" fill="${mmRegimeColor(r.regime)}" fill-opacity=".75"/>`;});
 el+=`<rect x="${mL}" y="${mT}" width="${W-mL-mR}" height="${H-mT-mB}" fill="none" stroke="${C.line}"/>`+regMarks(mL,mT,W-mL-mR,H-mT-mB);
 const cid='c'+(++CHARTID),MY=new Date(xmin).getFullYear()!==new Date(xmax).getFullYear();
 CHARTREG[cid]={dates:rows.map(r=>+new Date(r.date)),ys:rows.map(r=>yc(r.close)),rows:rows.map(r=>{const dt=new Date(r.date);return `<b>${mmEsc(sym)} ${fd(dt,MY)}</b><br><span class="xr"><i style="background:${C.price}"></i>price ${mmPx(sym,r.close)}</span><span class="xr"><i style="background:${C.ref}"></i>center ${mmPx(sym,r.center)}</span><span class="xr"><i style="background:${C.green}"></i>lower ${mmPx(sym,r.lower_boundary)}</span><span class="xr"><i style="background:${C.red}"></i>upper ${mmPx(sym,r.upper_boundary)}</span><span class="xr" style="color:${mmRegimeColor(r.regime)}">${mmRegimeLabel(r.regime)} · Q${r.quality==null?'—':r.quality.toFixed(0)} · build ${r.buildUp==null?'—':r.buildUp.toFixed(0)} · ${mmSigned(r.distanceZ,2)}σ</span>`;})};
 el+=`<g class="xg" style="display:none"><line class="cx" x1="0" y1="${mT}" x2="0" y2="${H-mB}"/><circle class="cxd" r="3.4"/></g><rect class="xhit" x="${mL}" y="${mT}" width="${W-mL-mR}" height="${H-mT-mB}" fill="transparent"/>`;
 return `<svg id="${cid}" class="xh" role="img" aria-label="${mmEsc(sym)} market-mass boundary chart" data-x0="${xmin}" data-x1="${xmax}" data-ml="${mL}" data-pw="${W-mL-mR}" viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet">${el}</svg>`;
}
function marketMassPyramidTable(sym,item){
 const profiles=mmPyramidProfiles(item),agreement=mmAgreement(item);
 if(!profiles.length)return '';
 const rows=profiles.map(p=>{
  const reg=p.regime||'—',rel=p.reliability==null?'—':Math.round(p.reliability*100);
  return `<tr><td class="l"><b>${mmEsc(p.profile||'—')}</b><br><span class="note">${p.lookback||'—'} bars · half-life ${p.halfLife||'—'}</span></td><td>${mmPx(sym,p.center_price)}</td><td>${p.quality_score==null?'—':p.quality_score.toFixed(0)}</td><td>${p.stability_score==null?'—':p.stability_score.toFixed(0)}</td><td>${p.volatility_build_up_score==null?'—':p.volatility_build_up_score.toFixed(0)}</td><td>${mmSigned(p.distance_z,2)}σ</td><td>${p.gravity_score==null?'—':p.gravity_score.toFixed(0)}</td><td>${p.levitation_score==null?'—':p.levitation_score.toFixed(0)}</td><td>${rel}</td><td style="color:${mmRegimeColor(reg)}">${mmRegimeLabel(reg)}</td></tr>`;
 }).join('');
 const spread=agreement.centerSpreadPct==null?'—':agreement.centerSpreadPct.toFixed(2)+'%';
 const dz=agreement.centerDisagreementZ==null?'—':agreement.centerDisagreementZ.toFixed(2)+'σ';
 const agree=agreement.agreementScore==null?'—':agreement.agreementScore.toFixed(0);
 return `<details open style="margin-top:12px;border:1px solid var(--line);border-radius:8px;padding:10px;background:var(--bg2)"><summary class="cap" style="cursor:pointer;border-bottom:0">Pyramid mass check · agreement ${agree}/100 · center spread ${spread} · disagreement ${dz}</summary><div class="scroll" style="margin-top:8px"><table><thead><tr><th class="l">Profile</th><th>Center</th><th>Q</th><th>Stable</th><th>Build</th><th>Dist</th><th>Gravity</th><th>Levitation</th><th>Rel.</th><th>Regime</th></tr></thead><tbody>${rows}</tbody></table></div><div class="note" style="margin-top:7px;line-height:1.6">Tactical reacts fastest; structural decays slowest. If their centers separate by more than about 1.25 mass-sigma, the single center is less trustworthy.</div></details>`;
}
function marketMassDetail(sym){
 const item=mmItem(sym),c=mmCurrent(item);
 if(!item||!c)return `<div class="card"><div class="dh"><h2 class="t">${mmEsc(sym||'Market Mass')}</h2><span class="nm">no usable artifact</span></div><div class="note">No market-mass data for this symbol.</div></div>`;
 const b=mmBoundary(item),color=mmRegimeColor(c.regime),vol=c.volatility||{},roles=(item.role||[]).join(' · '),conf=mmConfidence(item),confLabel=conf.label||'—',h=mmMassHealth(item),hLabel=h.label||'—',fr=h.frictionLabel||'—',agree=mmAgreement(item);
 const cards=[['Price',mmPx(sym,c.current_price),'latest close used by the model'],['Center',mmPx(sym,c.center_price),'weighted price-volume gravity point'],['Mass health',`<span style="color:${mmHealthColor(hLabel)}">${mmHealthLabel(hLabel)}</span> ${h.score==null?'':h.score.toFixed(0)+'/100'}`,'multi-timescale center agreement'],['Friction',`<span style="color:${mmFrictionColor(fr)}">${mmFrictionLabel(fr)}</span>`,'mean-reversion resistance state'],['Distance',mmSigned(c.distance_z,2)+'σ','log-price distance from center'],['Quality',c.quality_score==null?'—':c.quality_score.toFixed(1),'single swing-profile mass quality'],['Build-up',c.volatility_build_up_score==null?'—':c.volatility_build_up_score.toFixed(1),'stored volatility pressure'],['Regime',`<span style="color:${color}">${mmRegimeLabel(c.regime)}</span>`,roles||'universe'],['Pyramid agreement',agree.agreementScore==null?'—':agree.agreementScore.toFixed(0)+'/100',`spread ${agree.centerSpreadPct==null?'—':agree.centerSpreadPct.toFixed(2)+'%'}`],['Dashboard confidence',`<span style="color:${mmConfidenceColor(confLabel)}">${confLabel}</span> ${conf.score==null?'':Math.round(conf.score*100)+'/100'}`,'practical trust score'],['Freshness',item.stale?'<span class="neg">stale</span>':'<span class="pos">fresh</span>',`price ${item.priceAsOf||'—'} · generated ${((item.freshness||{}).generatedAt||'').slice(0,10)||'—'}`],['5d lower',b.lower_boundary==null?'—':`${mmPx(sym,b.lower_zone_low)}–${mmPx(sym,b.lower_zone_high)}`,'probabilistic lower zone'],['5d upper',b.upper_boundary==null?'—':`${mmPx(sym,b.upper_zone_low)}–${mmPx(sym,b.upper_zone_high)}`,'probabilistic upper zone']];
 const warns=mmWarns(item).map(w=>`<span class="chip" style="color:var(--amber-line);border-color:var(--chip-bd-amber)">${mmEsc(w)}</span>`).join(' ');
 return `<div class="card t1"><div class="dh"><h2 class="t">${mmEsc(sym)}<span class="t-en">MASS</span></h2><span class="nm">mass ${mmEsc(item.massAsOf||c.as_of||'—')} · price ${mmEsc(item.priceAsOf||'—')} · ${mmEsc(item.priceTicker||sym)} ${item.volumeTicker?`· volume ${mmEsc(item.volumeTicker)}`:''}</span></div><div class="badges">${cards.map(x=>`<div class="badge"><div class="l">${x[0]}</div><div class="v">${x[1]}</div><div class="note" style="font-size:var(--t-xs);margin-top:2px">${x[2]}</div></div>`).join('')}</div><div class="legend"><span><i class="ln" style="color:#8A8F98"></i>close</span><span><i class="lnd" style="color:#B89030"></i>center of mass</span><span><i style="background:#4FB286"></i>lower boundary zone</span><span><i style="background:#E5707A"></i>upper boundary zone</span><span>bottom ticks = regime</span></div><div class="chartbox">${marketMassChart(sym,item)}</div>${marketMassPyramidTable(sym,item)}<div class="note" style="margin-top:8px;line-height:1.65">Vol proxy: ${mmEsc(item.volTicker||item.fallbackVolTicker||'realized only')} · annual vol used ${vol.annual_vol_used==null?'—':(vol.annual_vol_used*100).toFixed(1)+'%'} · implied source ${mmEsc(vol.implied_vol_source||'—')}. Boundary zones are probabilistic ranges, not guaranteed support/resistance.</div>${mmReliabilityNotes(item)}${warns?`<div style="display:flex;gap:7px;flex-wrap:wrap;margin-top:10px">${warns}</div>`:''}</div>`;
}
function marketMassComparisonTable(){
 const syms=mmSymbols();
 const rows=syms.map(sym=>{const item=mmItem(sym),c=mmCurrent(item),b=mmBoundary(item),warn=mmWarns(item);
   if(!c)return `<tr><td class="l">${mmEsc(sym)}</td><td colspan="14" class="l">${warn.map(mmEsc).join(' · ')||'No data'}</td></tr>`;
   const conf=mmConfidence(item),cl=conf.label||'—';
   const h=mmMassHealth(item),hl=h.label||'—',fr=h.frictionLabel||'—',agree=mmAgreement(item);
   return `<tr style="cursor:pointer" tabindex="0" role="button" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();this.click();}" onclick="MMSEL='${mmEsc(sym)}';var s=document.getElementById('mm-symbol');if(s)s.value='${mmEsc(sym)}';marketMassRenderSelected()"><td class="l"><b>${mmEsc(sym)}</b><br><span class="note">${(item.role||[]).join(' · ')}</span></td><td>${mmPx(sym,c.current_price)}</td><td>${mmPx(sym,c.center_price)}</td><td style="color:${mmHealthColor(hl)}">${mmHealthLabel(hl)}${h.score==null?'':' '+h.score.toFixed(0)}</td><td style="color:${mmFrictionColor(fr)}">${mmFrictionLabel(fr)}</td><td>${agree.agreementScore==null?'—':agree.agreementScore.toFixed(0)}</td><td>${c.quality_score==null?'—':c.quality_score.toFixed(0)}</td><td>${c.volatility_build_up_score==null?'—':c.volatility_build_up_score.toFixed(0)}</td><td>${mmSigned(c.distance_z,2)}σ</td><td style="color:${mmRegimeColor(c.regime)}">${mmRegimeLabel(c.regime)}</td><td style="color:${mmConfidenceColor(cl)}">${cl}</td><td>${item.stale?'<span class="neg">stale</span>':'fresh'}</td><td>${b.lower_boundary==null?'—':mmPx(sym,b.lower_boundary)}</td><td>${b.upper_boundary==null?'—':mmPx(sym,b.upper_boundary)}</td><td class="l"><span class="note">${warn.length?mmEsc(warn[0]):'—'}</span></td></tr>`;
 }).join('');
 return `<div class="card"><div class="dh"><h2 class="t">Boundary Table</h2><span class="nm">5d / 80% default lens</span></div><div class="scroll"><table><thead><tr><th class="l">Ticker</th><th>Price</th><th>Center</th><th>Mass health</th><th>Friction</th><th>Agree</th><th>Quality</th><th>Build</th><th>Dist</th><th>Regime</th><th>Conf.</th><th>Fresh</th><th>Lower</th><th>Upper</th><th class="l">Flags</th></tr></thead><tbody>${rows}</tbody></table></div></div>`;
}
function marketMassRenderSelected(){const selEl=document.getElementById('mm-symbol'),host=document.getElementById('mm-detail');if(!host)return;MMSEL=(selEl&&selEl.value)||MMSEL||mmSymbols()[0];host.innerHTML=marketMassDetail(MMSEL);fixThScope(host);armDraw(host);}
function marketMassTab(){
 const M=mmData(),syms=mmSymbols();
 if(!M||!M.symbols||!syms.length)return `<div class="card t1"><div class="dh"><h2 class="t">重心边界<span class="t-en">MASS</span></h2><span class="nm">market-mass artifact missing</span></div><div class="note" style="line-height:1.8">暂无 market-mass 可视化数据。生成方式：运行 <b>python3 scripts/market_mass_dashboard.py --anchor-only</b> 写入 <b>output/market_mass_dashboard.json</b>，再运行 <b>python3 generate.py --no-fetch</b>。边界是概率区间，不是保证支撑/阻力。</div></div>`;
 const p=M.profile||{};MMSEL=(MMSEL&&syms.includes(MMSEL))?MMSEL:(syms.includes('QQQ')?'QQQ':syms[0]);
 const opts=syms.map(s=>`<option value="${mmEsc(s)}" ${s===MMSEL?'selected':''}>${mmEsc(s)}</option>`).join('');
 const anchor=['QQQ','^NDX','VOO','^GSPC'].filter(s=>M.symbols[s]).map(marketMassLane).join('');
 const warn=(M.warnings||[]).map(w=>`<span class="chip" style="color:var(--amber-line);border-color:var(--chip-bd-amber)">${mmEsc(w)}</span>`).join(' ');
 return `<div class="card t1"><div class="dh"><h2 class="t">重心边界<span class="t-en">MASS</span></h2><span class="nm">${mmEsc(M.universeMode||'universe')} · ${mmEsc(p.gravityProfile||'swing')} · lookback ${p.lookback||84} · half-life ${p.halfLife||21} · pyramid ${(p.pyramidProfiles||[]).join('/')||'tactical/swing/structural'} · ${p.defaultHorizonDays||5}d ${((p.defaultConfidence||.8)*100).toFixed(0)}% · generated ${(M.generatedAt||'').slice(0,10)||'—'}</span></div><div class="note" style="line-height:1.7">Use this panel to check whether price is orbiting an accepted participation center or losing its anchor. Mass health now compares tactical, swing, and structural centers; weak agreement or high levitation means price may be moving with less mean-reversion friction. Boundary zones are probabilistic ranges, not guaranteed support/resistance.</div>${anchor?`<div class="mm-lanes">${anchor}</div>`:''}${warn?`<div style="display:flex;gap:7px;flex-wrap:wrap;margin-top:10px">${warn}</div>`:''}</div><div class="mm-grid"><div><div style="display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;margin-bottom:10px"><span class="cap" style="border-bottom:0;padding-bottom:0">Selected Symbol</span><select id="mm-symbol" class="mm-select" onchange="marketMassRenderSelected()">${opts}</select></div><div id="mm-detail">${marketMassDetail(MMSEL)}</div></div><div>${marketMassComparisonTable()}</div></div>`;
}
const PANEL_RENDERERS_OV={
 score: ()=>scorecardCard(),
 decide:()=>decisionLabCard(),
 fin:   ()=>financialStatusCard(),
 aisemi:()=>aiSemiQuantCard(),
 aics:()=>aicsToolTab(),
 aiwatch:()=>aiWatchlistCard(),
 qt:    ()=>qqqTqqqTab(),
 mass:  ()=>marketMassTab(),
 nw:    ()=>{const e=_ovPanelEnv();
   return wholeAccountCard()+
     `<div class="card"><div class="dh"><h2 class="t">组合总览</h2><span class="nm">股票口径收益率（现金 / 期权按市价见本页顶部"全账户"卡片）</span></div>`+
     `<div class="badges">${e.cards.map(c=>`<div class="badge"><div class="l">${c[0]}</div><div class="v">${c[1]}</div></div>`).join('')}</div></div>`+
     `<div class="card"><div class="cap" style="margin-bottom:4px">持仓总市值（$） · 叠加组合斐波那契动能</div>`+
     `<div class="legend"><span>底部色带 = 组合加权动能：<span style="color:#E8B339">琥珀=强(&gt;15)</span> / <span style="color:#9A8A4A">浅琥珀=中性</span> / <span style="color:#6B7079">灰=弱(&lt;-15)</span>，用来对照净值看择时节奏</span></div>`+
     nwChart(e.ser)+
     `<div class="note" style="margin-top:10px;line-height:1.6"><b>怎么读：</b>这条是<b>持仓总市值（$，含入金）</b>——净值上升可能来自<b>入金而非盈利</b>。要看真金白银的回报，去"指数对比"的 ${gl('TWR','TWR')} 与本卡下方"真金白银桥"的 ${gl('MWR','MWR')}。底部色带 = 组合加权动能，仅供对照择时节奏。<b>非投资建议。</b></div></div>`+
     counterfactualCard()+
     bridgeCard()+
     `<div class="ref-divider" role="presentation">以下为参考资料 · 点开查看</div>`+
     optionsExposureCard()+optionsSpreadLedgerCard()+renderOptions()+diagnosticsCard();
 },
 pfib:  ()=>portfolioFibCard(),
 cmp:   ()=>{const e=_ovPanelEnv();
   return `<div class="card"><div class="cap" style="margin-bottom:4px">累计收益率对比（%，时间加权）</div>`+
     `<div class="legend"><span><i class="ln" style="color:#E8B339"></i>我的组合</span><span><i class="lnd" style="color:#888D96"></i>S&amp;P 500</span><span><i class="lnt" style="color:#B6BAC1"></i>纳斯达克综合</span></div>`+
     svgLines(e.ser,[{key:'ret',color:'#E8B339',width:2.3,draw:1,label:'我的组合'},{key:'sp500',color:'#888D96',width:1.5,dash:'5 3',label:'S&P'},{key:'nasdaq',color:'#B6BAC1',width:1.5,dash:'1.5 3',label:'纳斯达克'}],{zero:true,fmt:v=>v.toFixed(0)+'%',delta:{a:'ret',b:'sp500',label:'超额 vs S&P'}})+
     `<div class="note" style="margin-top:10px;line-height:1.6"><b>怎么读：</b>这三条都是<b>时间加权(TWR)</b>——只评判选股 / 策略本身，<b>剔除了你出入金的时点</b>，所以 ≠ 你的钱实际赚到的回报（你的钱真实经历看"净值"里的 <b>MWR</b>）。虚线为基准，<b>悬停看超额(pp)</b>，即我的组合(琥珀)与 S&amp;P(灰虚线)之间的差距。<b>非投资建议。</b></div></div>`+
     contributionCard();
 },
 sig:   ()=>positionSignalsCard()+`<div class="ref-divider" role="presentation">以下为参考分析</div>`+resonanceCard()+fibRanking(),
 beh:   ()=>behaviorCard(),
 risk:  ()=>riskCard(),
 struct:()=>structureCard(),
 journal:()=>journalCard(),
 rebal: ()=>rebalancePlanner(),
};
const PANEL_AFTER_RENDER_OV={
 nw:()=>{try{wireCounterfactual();}catch(e){}},
 rebal: ()=>{try{wireRebal();}catch(e){}},
 journal:()=>{try{wireJournalTab();}catch(e){}},
 aics:()=>{try{aicsScenarioRender();fixThScope(document.querySelector('#right .seg[data-seg="aics"]'));}catch(e){}},
};
// Ensure a single OV panel is rendered; called on first activation. No-op if already populated.
function ensureOvPanel(seg){
 if(sel!=='__OV__')return;
 const panel=document.querySelector('#right .seg[data-seg="'+seg+'"]');
 if(!panel||panel.innerHTML.trim()!=='')return;
 const rdr=PANEL_RENDERERS_OV[seg];if(!rdr)return;
 try{panel.innerHTML=rdr();}catch(e){console.warn('panel render failed',seg,e);panel.innerHTML='<div class="note">面板渲染失败</div>';return;}
 fixThScope(panel);armDraw(panel);
 const after=PANEL_AFTER_RENDER_OV[seg];if(after)after();
}
// screen-reader table semantics: every header cell announces as a column header.
// One pass per render — cheaper than authoring scope into 180+ template-string <th>s.
function fixThScope(root){(root||document).querySelectorAll('thead th:not([scope])').forEach(th=>th.setAttribute('scope','col'));}
function renderOverview(){
 CHARTREG={};
 const ser=DATA.series||[],right=document.getElementById('right');
 if(ser.length<2){const _ins0=document.getElementById('insight');if(_ins0)_ins0.innerHTML='';const _ob0=document.getElementById('onboard-slot');if(_ob0)_ob0.innerHTML=onboardStrip();right.innerHTML='<div class="card">数据不足，无法生成组合曲线。</div>';return;}
 const _ins=document.getElementById('insight');if(_ins)_ins.innerHTML=insightBanner();   // verdict + KPI ledger sit adjacent
 const _ob=document.getElementById('onboard-slot');if(_ob)_ob.innerHTML=onboardStrip();   // onboarding lives below the ledger, out of the hero zone
 // Render skeleton: viewbar + seg-rail + empty seg panels. ensureOvPanel() populates each on first activate.
 // activeSeg from URL is rendered eagerly so the initial paint has actual content.
 const segs=['score','decide','fin','aisemi','aics','aiwatch','qt','mass','nw','risk','struct','cmp','pfib','sig','beh','journal','rebal'];
 const _ls=lastSeg('ov');const initialSeg=segs.indexOf(_ls)>=0?_ls:DEFAULT_SEG.ov;   // validate stale/invalid localStorage seg so .on/syncWs/panelMarkup all agree
 const _segin=document.body.classList.contains('done')?' segin':'';   // post-load renders settle; initial load keeps the stagger
 const panelMarkup=segs.map(s=>`<div class="seg${s===initialSeg?_segin:''}" data-seg="${s}"${s===initialSeg?'':' hidden'}></div>`).join('');
 right.innerHTML=`
 <nav aria-label="工作区" class="ws-rail-wrap"><div class="ws-rail"><span class="rail-here">组合总览</span>${WS_MAP.map(w=>`<button data-ws="${w.id}" title="${w.segs.map(s=>segLabel(s,'ov')).join(' · ')}" onclick="wsGo('${w.id}')">${w.label}</button>`).join('')}</div></nav>
 <nav aria-label="组合分页" class="seg-rail-wrap"><div class="seg-rail"><button data-seg="score" title="每只持仓今天值不值得你看一眼">决策一览</button><button data-seg="decide" title="现金部署 · 择时 · 多视角量化裁决：理性该买什么">决策分析</button><button data-seg="fin" title="FMP 财务质量、财报记录与下一财报风险评分">财务状态</button><button data-seg="aisemi" title="AI 半导体产业链量化评分与资金瀑布">AI半导体</button><button data-seg="aics" title="AI 半导体资金流图谱、评分、情景和预警">AICS产业链</button><button data-seg="aiwatch" title="AI 旧能力重估观察池：价格刷新、评分、闸门">AI观察池</button><button data-seg="qt" title="QQQ 判断趋势，TQQQ/期权做执行">QQQ/TQQQ</button><button data-seg="mass" title="价格是否仍围绕成交重心运行，以及概率边界在哪里">重心边界</button><button data-seg="nw" title="我现在到底有多少钱（含现金 / 期权）">净值·账户</button><button data-seg="risk" title="哪只仓位贡献了最多波动">波动贡献</button><button data-seg="struct" title="钱和风险其实集中在哪几个主题">结构</button><button data-seg="cmp" title="我跑赢大盘了吗">指数对比</button><button data-seg="pfib" title="整个组合的动能强弱与节奏（技术参考，非投资建议）">技术·节奏</button><button data-seg="sig" title="各持仓最近的技术信号">持仓信号</button><button data-seg="beh" title="我的择时帮了还是拖了后腿">行为决策</button><button data-seg="journal" title="把你自己的交易当成诚实反馈：决策质量 vs 结果 + 成熟度评分 + 每周复盘">交易日志</button><button data-seg="rebal" title="该不该调仓、怎么调回我设的区间">再平衡计划</button></div></nav>
 ${panelMarkup}`;
 // Eagerly render the initial-seg panel so the first paint shows content (not an empty placeholder)
 ensureOvPanel(initialSeg);
 {const _b=right.querySelector('.seg-rail [data-seg="'+initialSeg+'"]');if(_b)_b.classList.add('on');}   // .on follows initialSeg (was hardcoded on score — mismatched when lastSeg('ov') differs and its workspace hides score)
 segWire();syncWs(initialSeg);bindCharts();updateCtx();renderOptSec();
 // Idle-prefetch other panels so subsequent tab clicks are instant. Skipped on browsers without rIC,
 // and rationed on low-end devices (≤4 cores or <4GB) — panels still render on demand at tab click.
 const _lowEnd=(navigator.deviceMemory&&navigator.deviceMemory<4)||(navigator.hardwareConcurrency&&navigator.hardwareConcurrency<=4);
 if(!_lowEnd){
  const _ric=window.requestIdleCallback||function(cb){return setTimeout(()=>cb({didTimeout:false,timeRemaining:()=>1}),200);};
  segs.forEach((s,i)=>{if(s===initialSeg)return;_ric(()=>ensureOvPanel(s),{timeout:1500+i*250});});
 }
}
function resonanceCard(){
 const bull=stocks.filter(x=>x.held&&x.fib&&x.fib.now&&x.fib.now.res==='bull').sort((a,b)=>b.fib.now.mom-a.fib.now.mom);
 const bear=stocks.filter(x=>x.held&&x.fib&&x.fib.now&&x.fib.now.res==='bear');
 const chip=(x,c)=>`<span class="chip" style="cursor:pointer;color:${c};border-color:${c}55" tabindex="0" role="button" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();this.click();}" onclick="stockGo('${x.sym}')">${x.sym} <span style="color:${c};opacity:.8">${x.fib.now.mom>0?'+':''}${x.fib.now.mom}</span></span>`;
 return `<div class="card"><div class="dh"><h2 class="t">今日多指标共振</h2><span class="nm">趋势排列 + 3日内金/死叉 + RSI 未极端 → 高确信度</span></div>
   <div style="margin-top:10px"><div class="l" style="color:var(--mut);font-size:11px;text-transform:uppercase;letter-spacing:var(--ls-label);margin-bottom:7px;font-weight:600">多头共振 · ${bull.length}</div>
   <div style="display:flex;gap:8px;flex-wrap:wrap">${bull.map(x=>chip(x,'#4FB286')).join('')||'<span class="note">无</span>'}</div></div>
   <div style="margin-top:14px"><div class="l" style="color:var(--mut);font-size:11px;text-transform:uppercase;letter-spacing:var(--ls-label);margin-bottom:7px;font-weight:600">空头共振 · ${bear.length}</div>
   <div style="display:flex;gap:8px;flex-wrap:wrap">${bear.map(x=>chip(x,'#E5707A')).join('')||'<span class="note">无</span>'}</div></div>
   <div class="note" style="margin-top:12px">共振 = 三个指标同向确认，比单一金叉更能过滤震荡假信号；仍为技术参考，非投资建议。</div></div>`;
}
function fibRanking(){
 const held=stocks.filter(x=>x.held&&x.fib&&x.fib.now).sort((a,b)=>b.fib.now.mom-a.fib.now.mom);
 if(!held.length)return'';
 const rows=held.map(x=>{const m=x.fib.now.mom,c=momColor(m),w=Math.abs(m)/100*50,left=m>=0?50:50-w;
   return `<div class="frow" style="cursor:pointer" tabindex="0" role="button" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();this.click();}" onclick="stockGo('${x.sym}')">
     <span class="fsym"><span style="color:${FIBCOL[x.fib.now.state]}">●</span> ${x.sym}</span>
     <div class="fbar"><div class="z"></div><div class="p" style="left:${left}%;width:${w}%;background:${c}"></div></div>
     <span class="fval" style="color:${c}">${m>0?'+':''}${m}</span>
     <span class="fst">${x.fib.now.label}</span></div>`;}).join('');
 return `<div class="card"><div class="dh"><h2 class="t">斐波那契动能排行</h2><span class="nm">持仓按 EMA5/8/13/21 动能强弱排序（点击查看个股）</span></div>
   <div style="margin-top:6px">${rows}</div>
   <div class="note" style="margin-top:10px">动能 = EMA5 相对 EMA21 偏离度（±100 封顶），正=多头、负=空头；颜色点为斐波那契状态。技术参考，非投资建议。</div></div>`;
}
const FIBCOL={up:'#4FB286',down:'#E5707A',range:'#888D96',mixed:'#B89030'};   // mixed = muted reference amber — pure amber stays reserved for live/active cues
const FIBLBL={up:'多头趋势',down:'空头趋势',range:'盘整纠缠',mixed:'转换中'};
function momColor(m){return m>15?'#4FB286':(m<-15?'#E5707A':'#B89030');}   // neutral band = muted reference amber, not the live accent
function fibBadges(n,signals,curLabel){
 const sc=momColor(n.mom),rsiCol=n.rsi>70?'#E5707A':(n.rsi<30?'#4FB286':'var(--txt)');
 const lastSig=(signals||[]).slice(-1)[0];
 return [
  [curLabel||'斐波那契状态',`<span style="color:${FIBCOL[n.state]}">●</span> ${n.label}`],
  [gl('mom','动能强弱'),`<span style="color:${sc}">${n.mom>0?'+':''}${n.mom}</span> <span class="note">/100</span>`],
  [gl('rsi','RSI(14)'),`<span style="color:${rsiCol}">${n.rsi}</span>`],
  ['最近信号',lastSig?(lastSig.type==='golden'?`<span class="pos">金叉 ${lastSig.date}</span>`:`<span class="neg">死叉 ${lastSig.date}</span>`):'—'],
  [gl('res','多指标共振'),n.res==='bull'?'<span class="pos">多头共振中</span>':(n.res==='bear'?'<span class="neg">空头共振中</span>':'无')],
 ];
}
function portfolioFibCard(){
 const pf=DATA.portfolioFib,ser=DATA.series||[];
 if(!pf||ser.length<2)return `<div class="card"><div class="dh"><h2 class="t">组合斐波那契</h2></div><div class="note">组合净值样本不足（需 ≥21 个交易日）。</div></div>`;
 const pseudo={sym:'组合净值',prices:ser.map(p=>[p.date,p.value]),fib:pf,curPrice:ser[ser.length-1].value,held:true};
 const n=pf.now,fmtK=v=>'$'+(v/1000).toFixed(0)+'k';
 const sser=ser.map((p,i)=>({date:p.date,mom:pf.mom[i],rsi:pf.rsi[i]}));
 const badges=fibBadges(n,pf.signals,'组合趋势状态');
 return `<div class="card">
   <div class="dh"><h2 class="t">组合斐波那契 · 技术参考<span class="t-en">Momentum</span></h2><span class="nm">EMA 5/8/13/21 缎带 · 金叉/死叉 · RSI —— 直接计算在组合净值曲线上（非投资建议）</span></div>
   <div class="badges">${badges.map(b=>`<div class="badge"><div class="l">${b[0]}</div><div class="v">${b[1]}</div></div>`).join('')}</div>
   <div class="legend">
     <span><i class="ln" style="color:#C99A3A"></i>EMA5</span><span><i class="ln" style="color:#B89030"></i>EMA8</span>
     <span><i class="ln" style="color:#8C8A6E"></i>EMA13</span><span><i class="ln" style="color:#5F6168"></i>EMA21</span>
     <span><span style="color:#4FB286">▲</span> 金叉(5×13)</span><span><span style="color:#E5707A">▼</span> 死叉</span>
     <span>◎ 共振</span>
     <span>底部带：<span style="color:#4FB286">绿=多头</span>/<span style="color:#E5707A">红=空头</span>/<span style="color:#B89030">黄=转换</span>/<span style="color:#888D96">灰=盘整</span></span></div>
   ${fibChart(pseudo,fmtK)}
   <div class="cap" style="margin:12px 0 2px">组合动能振荡器（−100 ~ +100）</div>
   ${svgLines(sser,[{key:'mom',color:'#C99A3A',draw:1,label:'动能'}],{zero:true,h:200,fixed:[-100,100],fmt:v=>v.toFixed(0),guides:[{v:15,color:'#2f6b4f',labelColor:'#4FB286',label:'强多'},{v:-15,color:'#6b2f2f',labelColor:'#E5707A',label:'强空'}],marks:pf.signals})}
   <div class="cap" style="margin:12px 0 2px">组合 RSI(14)</div>
   ${svgLines(sser,[{key:'rsi',color:'#888D96',label:'RSI'}],{h:180,fixed:[0,100],fmt:v=>v.toFixed(0),guides:[{v:70,color:'#6b2f2f',labelColor:'#E5707A',label:'超买70'},{v:30,color:'#2f6b4f',labelColor:'#4FB286',label:'超卖30'}],marks:pf.signals})}
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
   return `<tr style="cursor:pointer" tabindex="0" role="button" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();this.click();}" onclick="stockGo('${x.sym}')">
     <td class="l"><span style="color:${FIBCOL[n.state]}">●</span> ${x.sym}</td>
     <td>${w.toFixed(1)}%</td><td style="color:${FIBCOL[n.state]}">${n.label}</td>
     <td style="color:${momColor(n.mom)}">${n.mom>0?'+':''}${n.mom}</td>
     <td class="${rc}">${n.rsi}</td><td>${sigTxt}</td><td>${res}</td>
     <td class="l">${postureOf(n)}</td></tr>`;}).join('');
 return `<div class="card t1"><div class="dh"><h2 class="t">持仓信号一览<span class="t-en">Position Signals</span></h2><span class="nm">按权重排序 · 趋势 / 动能 / RSI / 最近金死叉 / 共振（点击看个股）</span></div>
   <div class="scroll"><table><thead><tr><th class="l">代码</th><th>权重</th><th>状态</th><th>${gl('mom','动能')}</th><th>${gl('rsi','RSI')}</th><th>${gl('cross','最近信号')}</th><th>${gl('res','共振')}</th><th class="l">技术姿态</th></tr></thead>
   <tbody>${rows}</tbody></table></div>
   <div class="note" style="margin-top:8px">“技术姿态”只是均线/动能/RSI 的客观描述，<b>非投资建议</b>；金叉/死叉为 EMA5×13 快线交叉。</div></div>`;
}
function behaviorCard(){
 const b=DATA.behavior;if(!b||!b.flags||!b.flags.length)return'<div class="card"><div class="note">行为分析数据不足。</div></div>';
 const LV={alert:['#E5707A','⚠ 需注意'],watch:['#E8B339','留意'],good:['#4FB286','✓ 良好'],info:['#888D96','提示']};
 const cards=b.flags.map(f=>{const c=LV[f.level]||LV.info;
   const ex=(f.examples&&f.examples.length)?`<div style="margin-top:7px;display:flex;gap:6px;flex-wrap:wrap">${f.examples.map(e=>`<span class="chip">${e}</span>`).join('')}</div>`:'';
   return `<div class="card" style="border-left:3px solid ${c[0]}">
     <div class="dh"><h2 class="t">${f.title}</h2><span class="chip" style="color:${c[0]};border-color:${c[0]}66">${c[1]}</span></div>
     <div class="flag-head" style="margin:3px 0 6px">${f.headline}</div>
     <div class="note" style="color:var(--txt);line-height:1.55">${f.detail}</div>${ex}
     <div style="margin-top:9px;padding:9px 11px;background:rgba(232,179,57,.07);border-radius:var(--r-card);line-height:1.55"><b>💡 Nudge：</b>${f.nudge}</div>
     <div class="note" style="margin-top:6px">${f.ref}</div></div>`;}).join('');
 return `<div class="card"><div class="dh"><h2 class="t">行为决策辅助</h2><span class="nm">基于 Thaler《行为经济学：过去、现在与未来》(2016)</span></div>
   <div class="note" style="margin-top:6px;line-height:1.6">以下信号由<b>你自己的真实买卖与持仓</b>计算而来，用于发现常见的行为偏差。它们是<b>“提醒”而非投资建议</b>——目的是帮你按既定逻辑决策、少受“红/绿盘”情绪左右。</div></div>${cards}`;
}
function riskCard(){
 const R=DATA.risk;
 if(!R)return'<div class="card"><div class="dh"><h2 class="t">风险</h2></div><div class="note">风险数据不足（需 ≥25 个交易日）。</div></div>';
 const volCls=R.annVol>R.spAnnVol?'neg':'pos';
 const badges=[
  [gl('vol','年化波动率'),`<span class="${volCls}">${R.annVol.toFixed(1)}%</span> <span class="note">S&P ${R.spAnnVol.toFixed(1)}%</span>`],
  [gl('beta','Beta β (vs S&P)'),R.beta==null?'—':`<span class="${R.beta>1?'neg':'pos'}">${R.beta.toFixed(2)}</span>`],
  [gl('dd','最大回撤'),`<span class="${cls(R.maxDrawdown)}">${R.maxDrawdown.toFixed(1)}%</span>`],
  ['当前回撤',`<span class="${cls(R.currentUnderwater)}">${R.currentUnderwater.toFixed(1)}%</span>`],
  ['收益/波动比',`${R.retVolRatio.toFixed(2)} <span class="note">(rf=0)</span>`],
 ];
 const rows=(R.contrib||[]).map(c=>{const g=c.gap,gc=g>0?'#E5707A':(g<0?'#4FB286':'var(--mut)'),w=Math.min(Math.abs(g),20)/20*50,left=g>=0?50:50-w;
   return `<tr style="cursor:pointer" tabindex="0" role="button" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();this.click();}" onclick="stockGo('${c.sym}')">
     <td class="l">${c.sym}</td><td>${c.weightPct.toFixed(1)}%</td><td>${c.riskPct.toFixed(1)}%</td>
     <td><div class="fbar"><div class="z"></div><div class="p" style="left:${left}%;width:${w}%;background:${gc}"></div></div></td>
     <td style="color:${gc}">${g>0?'+':''}${g.toFixed(1)}pp</td></tr>`;}).join('');
 const exNote=(R.excluded&&R.excluded.length)?`<div class="note" style="margin-top:8px">以下标的价格重叠不足 30 日，未计入风险分解：${R.excluded.join('、')}</div>`:'';
 return `<div class="card">
   <div class="dh"><h2 class="t">风险<span class="t-en">Risk Ledger</span></h2><span class="nm">回撤 · 波动率 · Beta · 风险贡献（股票部分，不含现金/保证金/期权）</span></div>
   <div class="badges">${badges.map(b=>`<div class="badge"><div class="l">${b[0]}</div><div class="v">${b[1]}</div></div>`).join('')}</div></div>
 <div class="card"><div class="cap" style="margin-bottom:4px">水下回撤曲线（相对历史高点回撤 %）</div>
   <div class="legend"><span><i class="ln" style="color:#E5707A"></i>我的组合</span><span><i class="lnd" style="color:#888D96"></i>S&P 500</span><span>0 = 创新高；越深 = 离高点越远（你真正“感受到”的亏损维度）</span></div>
   ${svgLines(R.uwSeries,[{key:'uw',color:'#E5707A',draw:1,label:'我的回撤'},{key:'spuw',color:'#888D96',dash:1,label:'S&P回撤'}],{zero:true,area:true,fmt:v=>v.toFixed(0)+'%',delta:{a:'uw',b:'spuw',label:'相对 S&P'}})}
   <div class="note" style="margin-top:6px">最大回撤 <span class="${cls(R.maxDrawdown)}">${R.maxDrawdown.toFixed(1)}%</span>（${R.maxDDpeak} 高点 → ${R.maxDDtrough} 低点）。</div></div>
 <div class="card"><div class="cap" style="margin-bottom:4px">21 日滚动年化波动率</div>
   <div class="legend"><span><i class="ln" style="color:#E8B339"></i>我的组合</span><span><i class="lnd" style="color:#888D96"></i>S&P 500</span></div>
   ${svgLines(R.volSeries,[{key:'vol',color:'#E8B339',draw:1,label:'我的波动'},{key:'spvol',color:'#888D96',dash:1,label:'S&P波动'}],{fmt:v=>v.toFixed(0)+'%'})}
   <div class="note" style="margin-top:6px"><b>怎么读：</b>波动率衡量“颠簸幅度”，<b>不是盈亏</b>——线在 S&P 之上 = 近期比大盘更颠簸，未必是亏损。21 日滚动、窗口内小样本，<b>描述性、非预测</b>。</div></div>
 <div class="card"><div class="dh"><h2 class="t">风险贡献分解</h2><span class="nm">谁在制造组合波动 · 权重 ≠ 风险（点击看个股）</span></div>
   <div class="scroll"><table><thead><tr><th class="l">代码</th><th>资金权重</th><th>${gl('rc','风险贡献')}</th><th></th><th title="该标的风险占比减资金占比；正(红)=波动放大器、负(绿)=分散器">风险−资金</th></tr></thead>
   <tbody>${rows}</tbody></table></div>${exNote}
   <div class="note" style="margin-top:8px"><b>怎么读：</b>“风险贡献”把组合总波动按各持仓的边际贡献拆开（合计 100%）。<b>差额为正(红)= 该标的对波动的贡献高于其资金占比</b>（隐藏的风险放大器）；<b>为负(绿)= 分散器</b>。高 Beta 单票常常风险占比远超资金占比。<br>这是对<b>窗口内已实现风险</b>的描述性分解（样本有限、非预测），且仅含股票（不含现金/保证金/期权，会<b>低估</b>你的真实杠杆风险）。${DATA.account?` <span class="note-lk" role="button" tabindex="0" onkeydown="if(event.key==='Enter'||event.key==='\ '){event.preventDefault();this.click();}" onclick="ovGo('nw')">→ 去“净值 · 全账户”看期权敞口</span>`:''}<b>非投资建议。</b></div></div>`;
}
/* ===== Rebalancing Planner (Thaler commitment device) ===== */
let rebalDraft=null;
function rebalDefault(){return{policy:'cap',cap:20,band:5,glide:'edge',setOn:null};}
function rebalLoad(){try{const s=JSON.parse(localStorage.getItem('ptrak.rebal.v1'));if(s&&s.policy)return Object.assign(rebalDefault(),s);}catch(e){}return rebalDefault();}
// User-initiated localStorage saves should NEVER silently fail — private browsing and quota-exceeded leave the trader thinking their journal/rebal rule saved when it didn't.
function _ptrakSafeSet(k,v){try{localStorage.setItem(k,v);return true;}catch(e){return false;}}
function _ptrakNotifySaveFailed(what){const reason=(navigator&&navigator.cookieEnabled===false)?'浏览器禁用 Cookie / 存储':(window.matchMedia&&window.matchMedia('(prefers-color-scheme: dark)').matches?'存储空间已满或浏览器在隐私模式':'存储空间已满');alert(what+' 未能保存 — '+reason+'。\n\n尝试：关闭隐私模式或清理浏览器存储后重试。');}
function rebalSave(){rebalDraft.setOn=new Date().toISOString().slice(0,10);if(!_ptrakSafeSet('ptrak.rebal.v1',JSON.stringify(rebalDraft)))_ptrakNotifySaveFailed('再平衡规则');}
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
 <div class="note" style="margin-top:8px;line-height:1.65">本计划只是把权重拉回<b>你自己设定的区间</b>的描述性算术，仅含股票（不含现金/保证金/期权，会<b>低估</b>你的真实杠杆${DATA.account?`，<span class="note-lk" role="button" tabindex="0" onkeydown="if(event.key==='Enter'||event.key==='\ '){event.preventDefault();this.click();}" onclick="ovGo('nw')">→ 去“净值 · 全账户”看期权敞口</span>`:''}）。它<b>忽略</b>税费、洗售(wash-sale)、成本批次(lot)选择与交易/佣金/点差成本——交易成本若不实测，就是“万能借口”(Thaler)。反波动率目标基于“风险”页同一份<b>小样本、描述性（非预测）</b>协方差。股数四舍五入到整股。规则只存在<b>本浏览器本设备</b>，不跨设备、不上传。本计划<b>不声称提高收益</b>，只把权重拉回你选的区间。<b>非投资建议。</b></div></details></div>`;}
function rebalancePlanner(){
 if(rebalDraft===null)rebalDraft=rebalLoad();
 const rule=rebalDraft,uni=rebalUniverse(),volMap=rebalVolMap();
 if(!uni.u.length)return '<div class="card"><div class="dh"><h2 class="t">再平衡计划</h2></div><div class="note">暂无持仓可用于再平衡。</div></div>';
 const invvolOk=DATA.risk&&uni.u.every(x=>volMap[x.sym]>0);
 const bt=(active,attrs,lbl,dis)=>`<button ${attrs} ${dis?'disabled':''} style="padding:5px 11px;border-radius:var(--r-ctl);border:1px solid ${dis?'var(--line)':(active?'#E8B339':'var(--line)')};background:${dis?'transparent':(active?'rgba(232,179,57,.12)':'transparent')};color:${dis?'#555':(active?'#E8B339':'var(--txt)')};cursor:${dis?'not-allowed':'pointer'};font-size:var(--t-sm);margin-right:6px">${lbl}</button>`;
 const pol=(k,lbl,dis)=>bt(rule.policy===k,`data-reb="pol" data-v="${k}"`,lbl,dis);
 const gl=(k,lbl)=>bt(rule.glide===k,`data-reb="glide" data-v="${k}"`,lbl,false);
 const stamp=rule.setOn?`<span class="chip" style="color:#4FB286;border-color:var(--chip-bd-green)">规则设定于 ${rule.setOn}</span>`:`<span class="chip" style="color:#E8B339;border-color:var(--chip-bd-amber)">建议默认 · 未保存</span>`;
 const row=(lbl,body)=>`<div style="display:flex;align-items:center;gap:10px;margin:9px 0;flex-wrap:wrap"><span style="color:var(--mut);min-width:78px;font-size:var(--t-sm)">${lbl}</span>${body}</div>`;
 const controls=`<div class="card">
   <div class="dh"><h2 class="t">再平衡计划</h2><span class="nm">现在冷静时定下规则，之后由面板替你执行（Thaler 的 planner / doer 两个自我）</span></div>
   <div class="note" style="margin:2px 0 8px;line-height:1.6">规则存在本浏览器、跨刷新有效，替你扛住情绪上头的那一刻。你是<b>接受/微调默认</b>，而非从零搭建。 ${stamp}</div>
   ${row('策略',pol('cap','限制集中度')+pol('equal','等权')+pol('invvol','反波动·风险平价',!invvolOk))}
   ${rule.policy==='cap'?row('单一上限',`<input id="rebCap" type="number" min="5" max="50" step="1" value="${rule.cap}" aria-label="单一上限（百分比 %）" style="width:60px;background:var(--bg2);color:var(--txt);border:1px solid var(--line);border-radius:6px;padding:3px 6px"> %`):''}
   ${row('触发区间 ±',`<input id="rebBand" type="number" min="1" max="20" step="0.5" value="${rule.band}" aria-label="触发区间 ±（百分点 pp）" style="width:60px;background:var(--bg2);color:var(--txt);border:1px solid var(--line);border-radius:6px;padding:3px 6px"> pp <span class="note">（落在带内就什么都不做）</span>`)}
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
 if(!out.length)banner=`<div class="card" style="border-left:3px solid #4FB286"><div class="flag-head" style="color:#4FB286;margin:0 0 2px">✓ 全部在区间内 · 无需操作</div><div class="note" style="margin-top:4px">在带内什么都不做，才是纪律所在——也直接对冲你的高换手。这是纪律的胜利。</div></div>`;
 else banner=`<div class="card" style="border-left:3px solid #E5707A"><div class="flag-head" style="color:#E5707A;margin:0 0 2px">你预先设定的规则触发了：${out.length} 只标的越界</div><div class="note" style="margin-top:4px">这不是“市场动了快行动”，而是你冷静时定下的 ±${band}pp 带宽到点了。${tt.note?'<br>'+tt.note:''}</div></div>`;
 const badges=[['最大持仓',`${top.sym} ${top.w.toFixed(0)}% → ${topT.toFixed(0)}%`],['前五合计',`${top5n.toFixed(0)}% → ${top5t.toFixed(0)}%`],
   ['HHI 集中度',`${hhi(wn).toFixed(2)} → ${hhi(tn).toFixed(2)}`],['越界标的',`${out.length} / ${rows.length}`],['双向成交额',`≈ ${fmt(sells+buys)}`]];
 const barRow=r=>{const c=r.inB?'#888D96':'#E8B339',wd=Math.min(Math.abs(r.drift),20)/20*50,left=r.drift>=0?50:50-wd;
   return `<div class="frow" style="cursor:pointer" tabindex="0" role="button" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();this.click();}" onclick="stockGo('${r.sym}')">
     <span class="fsym">${r.sym}</span><div class="fbar"><div class="z"></div><div class="p" style="left:${left}%;width:${wd}%;background:${c}"></div></div>
     <span class="fval" style="color:${c}">${r.drift>0?'+':''}${r.drift.toFixed(1)}</span><span class="fst">${r.inB?'区间内':(r.drift>0?'超配':'低配')}</span></div>`;};
 const inRows=rows.filter(r=>r.inB);
 const bars=rows.filter(r=>!r.inB).map(barRow).join('')
   +(inRows.length?`<details style="margin-top:6px"><summary>其余 ${inRows.length} 只在容差内 — 什么都不做才是纪律</summary><div style="margin-top:6px">${inRows.map(barRow).join('')}</div></details>`:'');
 const arows=out.map(r=>{const sh=r.sh==null?'<span class="note">缺价</span>':((r.d$<0?'−':'+')+Math.abs(r.sh)+'股'),edge=rule.glide==='center'?'目标':(r.drift>0?'上沿':'下沿');
   return `<tr style="cursor:pointer" tabindex="0" role="button" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();this.click();}" onclick="stockGo('${r.sym}')">
     <td class="l">${r.sym}</td><td>${r.w.toFixed(1)}%</td><td>${r.lo.toFixed(0)}–${(r.t+band).toFixed(0)}%</td>
     <td style="color:${r.inB?'#888D96':'#E8B339'}">${r.drift>0?'+':''}${r.drift.toFixed(1)}</td>
     <td class="l">${sh} <span class="note">(${fmt(Math.abs(r.d$))})</span> 拉回${edge}</td></tr>`;}).join('');
 const actionTable=out.length?`<div class="card"><div class="dh"><h2 class="t">动作清单</h2><span class="nm">仅越界标的 · 你的规则触发</span></div>
   <div class="scroll"><table><thead><tr><th class="l">代码</th><th>当前</th><th>目标区间</th><th>偏离pp</th><th class="l">动作</th></tr></thead><tbody>${arows}</tbody></table></div>
   <div class="note" style="margin-top:8px">卖出释放 ${fmt(sells)} · 买入部署 ${fmt(buys)} · 净${net>=0?'需追加':'释放'} ${fmt(Math.abs(net))}（股数已取整，带内标的不动）。</div></div>`:'';
 return banner+`<div class="card"><div class="badges">${badges.map(b=>`<div class="badge"><div class="l">${b[0]}</div><div class="v">${b[1]}</div></div>`).join('')}</div></div>`
   +`<div class="card"><div class="cap" style="margin-bottom:4px">权重偏离目标（pp）· 颜色 = 方向</div>
      <div class="legend"><span><i style="background:#E8B339"></i>越界·需操作</span><span><i style="background:#888D96"></i>区间内·无需操作</span><span class="note">方向（超配/低配）见每行标签</span></div>${bars}
      <div class="note" style="margin-top:8px"><b>怎么读：</b><b>pp</b>=百分点（当前权重 − 你设定的目标）；<b>区间内(灰)</b>=在你冷静时定的带宽内，什么都不做才是纪律；<b>越界(琥珀)</b>=你预设的规则触发了；动作列“拉回上沿 / 下沿 / 目标”对应你选的回归方式。仅描述把权重拉回你所选区间的算术，<b>非投资建议。</b></div></div>`+actionTable;
}
function wireRebal(){const seg=document.querySelector('.seg[data-seg="rebal"]');if(!seg)return;
 seg.querySelectorAll('[data-reb]').forEach(b=>b.onclick=()=>{if(b.disabled)return;const k=b.dataset.reb,v=b.dataset.v;if(k==='pol')rebalDraft.policy=v;if(k==='glide')rebalDraft.glide=v;rerenderRebal();});
 const cap=seg.querySelector('#rebCap');if(cap)cap.onchange=()=>{rebalDraft.cap=Math.max(5,Math.min(50,+cap.value||20));rerenderRebal();};
 const band=seg.querySelector('#rebBand');if(band)band.onchange=()=>{rebalDraft.band=Math.max(1,Math.min(20,+band.value||5));rerenderRebal();};
 const sv=seg.querySelector('#rebSave');if(sv)sv.onclick=()=>{rebalSave();rerenderRebal();};
 const cl=seg.querySelector('#rebClear');if(cl)cl.onclick=()=>{if(rebalDraft.setOn&&!confirm('清除已保存的规则、退回建议默认？'))return;rebalClear();rerenderRebal();};}
function rerenderRebal(){const seg=document.querySelector('.seg[data-seg="rebal"]');if(seg){seg.innerHTML=rebalancePlanner();wireRebal();}}
/* ===== Trade Journal + Investor-Maturity layer (per-position, localStorage) ===== */
let journalState=null, journalDraft=null;
const journalDrafts={};   // per-symbol draft cache: navigating between stocks preserves in-progress edits
const J_DESTRUCT=['FOMO追高','报复','无聊'], J_CONSTRUCT=['冷静','坚定','纪律'];
const jToday=()=>new Date().toISOString().slice(0,10);
const jesc=s=>(s||'').replace(/</g,'&lt;');
function journalLoad(){if(journalState)return journalState;try{const s=JSON.parse(localStorage.getItem('ptrak.journal.v1'));if(s&&s.entries&&typeof s.entries==='object'){journalState={v:1,entries:s.entries};return journalState;}}catch(e){}journalState={v:1,entries:{}};return journalState;}
function journalEntry(sym){return journalLoad().entries[sym]||null;}
function journalSaveEntry(sym,e){const st=journalLoad();e.updatedAt=jToday();st.entries[sym]=Object.assign({},st.entries[sym],e);journalState=st;if(!_ptrakSafeSet('ptrak.journal.v1',JSON.stringify(st)))_ptrakNotifySaveFailed('交易日志「'+sym+'」');}
function journalClearEntry(sym){const st=journalLoad();delete st.entries[sym];journalState=st;_ptrakSafeSet('ptrak.journal.v1',JSON.stringify(st));}
function isoWeek(d){d=new Date(Date.UTC(d.getFullYear(),d.getMonth(),d.getDate()));const day=d.getUTCDay()||7;d.setUTCDate(d.getUTCDate()+4-day);const y0=new Date(Date.UTC(d.getUTCFullYear(),0,1));const wk=Math.ceil(((d-y0)/86400000+1)/7);return d.getUTCFullYear()+'-W'+String(wk).padStart(2,'0');}
function reviewLoad(){try{const s=JSON.parse(localStorage.getItem('ptrak.review.v1'));if(s&&s.weeks)return s;}catch(e){}return{v:1,weeks:{}};}
function reviewGet(wk){return reviewLoad().weeks[wk]||null;}
function reviewSave(wk,r){const st=reviewLoad();r.savedOn=jToday();st.weeks[wk]=Object.assign({},st.weeks[wk],r);if(!_ptrakSafeSet('ptrak.review.v1',JSON.stringify(st)))_ptrakNotifySaveFailed('每周复盘');}
function reviewClear(wk){const st=reviewLoad();delete st.weeks[wk];try{localStorage.setItem('ptrak.review.v1',JSON.stringify(st));}catch(e){}}
function journalComponents(){
 const J=journalLoad().entries, held=stocks.filter(x=>x.held), ent=sym=>J[sym], vals=Object.keys(J).map(k=>J[k]);
 const cnt=(arr,f)=>arr.filter(f).length;
 // COVERAGE rates use the HELD denominator (a gap SHOULD drag the score down — anti-vanity).
 const thesisCov={val:held.length?cnt(held,x=>ent(x.sym)&&ent(x.sym).thesis&&ent(x.sym).thesis.trim())/held.length:null,n:held.length,key:'thesisCov',label:'论点覆盖率'};
 const jrnCov={val:held.length?cnt(held,x=>!!ent(x.sym))/held.length:null,n:held.length,key:'jrnCov',label:'日志覆盖率'};
 // BEHAVIOR rates use the JOURNALED denominator (PENDING until the owner has tagged that field — don't punish unreached fields). Maintainer: do NOT "fix" this asymmetry.
 const adhDen=cnt(vals,e=>e.adherence!=null), planAdh={val:adhDen?cnt(vals,e=>e.adherence==='在计划内')/adhDen:null,n:adhDen,key:'padh',label:'计划遵守率'};
 const ckDen=cnt(vals,e=>e.checklist!=null), checklist={val:ckDen?cnt(vals,e=>e.checklist==='是')/ckDen:null,n:ckDen,key:'ckl',label:'清单完成率'};
 const emoDen=cnt(vals,e=>e.emotion!=null), emoDisc={val:emoDen?1-cnt(vals,e=>J_DESTRUCT.indexOf(e.emotion)>=0)/emoDen:null,n:emoDen,key:'emo',label:'情绪纪律'};
 return [thesisCov,planAdh,checklist,jrnCov,emoDisc];
}
function maturityScore(){const all=journalComponents(),c=all.filter(x=>x.val!=null);const anyEntry=Object.keys(journalLoad().entries).length>0;if(!anyEntry||!c.length)return{score:null,comps:all,weak:null};const score=Math.round(c.reduce((a,x)=>a+x.val,0)/c.length*100);const weak=c.slice().sort((a,b)=>a.val-b.val)[0];return{score,comps:all,weak};}
function maturityBand(s){return s==null?'起步':(s>=90?'纪律化':(s>=75?'稳健':(s>=60?'成型中':(s>=40?'易受情绪左右':'主要靠运气'))));}
function maturityCard(){
 const m=maturityScore();
 const bars=m.comps.map(c=>{const w=c.val==null?0:c.val*100;
   return `<div class="frow"><span class="fsym" style="width:auto;min-width:120px">${gl(c.key,c.label)}</span>
     <div class="fbar"><div class="p" style="left:0;width:${w}%;background:#E8B339"></div></div>
     <span class="fval" style="color:var(--txt)">${c.val==null?'待补':(c.val*100).toFixed(0)+'%'}</span>
     <span class="fst">n=${c.n}</span></div>`;}).join('');
 const weakLine=m.score==null?'记录第一条论点即可点亮评分。':(m.weak&&m.weak.val<0.9?`你的最大短板：${m.weak.label} 仅 ${(m.weak.val*100).toFixed(0)}%（n=${m.weak.n}）`:'流程很扎实，保持记录的连贯性即可。');
 return `<div class="card" style="border-left:3px solid #E8B339">
   <div class="dh"><h2 class="t">投资成熟度评分</h2><span class="nm">只衡量你能控制的「流程」，与盈亏无关</span></div>
   <div class="hero-fig" style="color:var(--txt);margin:4px 0 2px">${m.score==null?'起步':m.score}<span style="font-size:14px;color:var(--mut)">${m.score==null?' · 尚无足够日志':' / 100'}</span> <span class="chip" style="color:var(--mut);border-color:var(--line)">${maturityBand(m.score)}</span></div>
   <div class="note" style="margin:2px 0 8px">高分=流程纪律好，与赚不赚钱无关；一笔靠运气赚到的钱不会提高这个分数。<b>${weakLine}</b></div>
   ${bars}</div>`;
}
function meanBy(arr,f){const xs=arr.map(f).filter(v=>v!=null&&isFinite(v));return xs.length?xs.reduce((a,b)=>a+b,0)/xs.length:null;}
function killerStatCard(){
 const J=journalLoad().entries, held=stocks.filter(x=>x.held);
 const grp=tag=>held.filter(x=>J[x.sym]&&J[x.sym].adherence===tag);
 const inn=grp('在计划内'), out=grp('计划外'), base=meanBy(held,x=>x.unrealPct);
 if(!inn.length&&!out.length)return `<div class="card"><div class="dh"><h2 class="t">计划遵守 → 结果</h2><span class="nm">在计划内 vs 计划外的平均结果</span></div><div class="note">给持仓标上「在计划内 / 计划外」后，这里会对比两组的平均结果。</div></div>`;
 const bar=(label,g)=>{const m=meanBy(g,x=>x.unrealPct),small=g.length<3;const c=small?'#888D96':(m>=0?'#4FB286':'#E5707A');   // bar color = P&L sign (green=+/red=−); distance-to-baseline carried by bar offset/width, not fill
   const wd=m==null?0:Math.min(Math.abs(m-base),30)/30*50,left=(m!=null&&m>=base)?50:50-wd;
   return `<div class="frow"><span class="fsym" style="width:auto;min-width:96px">${label}</span>
     <div class="fbar"><div class="z"></div><div class="p" style="left:${left}%;width:${wd}%;background:${c}"></div></div>
     <span class="fval" style="color:var(--txt)">${m==null?'—':(m>=0?'+':'')+m.toFixed(1)+'%'}</span>
     <span class="fst">n=${g.length}${small?' 样本不足':''}</span></div>`;};
 return foldCard('计划遵守 → 结果',`${gl('killer','在计划内 vs 计划外')}的平均未实现%（基线 ${base==null?'—':(base>=0?'+':'')+base.toFixed(1)+'%'}）`,`
   ${bar('在计划内',inn)}${bar('计划外',out)}
   <div class="note" style="margin-top:8px">颜色仅表示盈亏方向（绿＝正、红＝负），长度表示与基线的差距。这是你自己过去交易的记录，不是买卖建议；任一组 n&lt;3 仅描述、不下结论。</div>`,0,`n=${inn.length+out.length}`);
}
function emotionOutcomeCard(){
 const J=journalLoad().entries, held=stocks.filter(x=>x.held);
 const tags=J_DESTRUCT.concat(J_CONSTRUCT).filter(t=>held.some(x=>J[x.sym]&&J[x.sym].emotion===t));
 if(!tags.length)return '';
 const rows=tags.map(t=>{const g=held.filter(x=>J[x.sym]&&J[x.sym].emotion===t),m=meanBy(g,x=>x.unrealPct),small=g.length<3,dc='var(--mut)';   // dot is neutral — green/red stays for P&L sign only; the emotion label itself names destructive vs constructive
   return `<tr style="${small?'color:var(--mut)':''}"><td class="l"><span style="color:${dc}">●</span> ${t}</td><td class="${small?'':cls(m)}">${m==null?'—':(m>=0?'+':'')+m.toFixed(1)+'%'}</td><td class="note">n=${g.length}${small?' 样本不足':''}</td></tr>`;}).join('');
 return foldCard(`${gl('emoOut','情绪 → 结果')}`,'各情绪标签下持仓的平均未实现%',`
   <div class="scroll"><table><thead><tr><th class="l">情绪</th><th>平均未实现%</th><th>样本</th></tr></thead><tbody>${rows}</tbody></table></div>
   <div class="note" style="margin-top:8px">看到报复/追涨平均更差，不代表下次必然——小样本，仅描述你已有的交易。<b>非投资建议。</b></div>`,0,`${tags.length} 标签`);
}
function unjournaledWorklist(){
 const J=journalLoad().entries, mv=S.marketValue||1;
 const held=stocks.filter(x=>x.held).sort((a,b)=>b.value-a.value);
 const todo=held.filter(x=>!J[x.sym]);
 if(!todo.length)return `<div class="card"><div class="dh"><h2 class="t">待写日志</h2></div><div class="note">✓ 所有持仓都已写下论点。保持下去。</div></div>`;
 const rows=todo.map(x=>`<div class="frow jwork" data-jsym="${x.sym}" style="cursor:pointer"><span class="fsym">${x.sym}</span><div class="fbar"><div class="p" style="left:0;width:${Math.min(x.value/mv*100,100)}%;background:#E8B339"></div></div><span class="fval">${(x.value/mv*100).toFixed(1)}%</span><span class="fst" style="width:auto;min-width:80px;text-align:right">${fmt(x.value)}</span></div>`).join('');
 return `<div class="card"><div class="dh"><h2 class="t">待写日志 · ${todo.length} 只</h2><span class="nm">按权重排序（点开写论点）</span></div>${rows}
   <div class="note" style="margin-top:8px">先给这些持仓写下论点，最能提高你的日志覆盖率。</div></div>`;
}
function weeklyReview(){
 const wk=isoWeek(new Date()), r=reviewGet(wk)||{}, mv=S.marketValue, topH=stocks.filter(x=>x.held).sort((a,b)=>b.value-a.value)[0];
 const heldN=stocks.filter(x=>x.held).length, cov=stocks.filter(x=>x.held&&journalEntry(x.sym)).length;
 const facts=[['本周市值',fmt(mv)],['区间收益',pct(S.curReturn)],['超额 vs 标普',S.spReturn==null?'—':ppf(S.curReturn-S.spReturn)],
   ['最大回撤',DATA.risk?(DATA.risk.maxDrawdown.toFixed(1)+'%'):'—'],['行为缺口',S.behaviorGap==null?'—':((S.behaviorGap>0?'+':'')+S.behaviorGap.toFixed(2)+'pp '+(S.behaviorGap>0?'(择时拖累)':(S.behaviorGap<0?'(择时帮忙)':'(中性)')))],
   ['最大持仓',topH?topH.sym+' '+(topH.value/mv*100).toFixed(0)+'%':'—'],['本周日志覆盖',cov+'/'+heldN]];
 const ta=(id,lbl,v)=>`<div style="margin:8px 0 2px;font-size:var(--t-sm);color:var(--mut)">${lbl}</div><textarea id="${id}" rows="2" aria-label="${lbl}" style="width:100%;background:var(--bg2);color:var(--txt);border:1px solid var(--line);border-radius:6px;padding:6px 8px;font-family:var(--f-ui);font-size:var(--t-sm);resize:vertical">${jesc(v)}</textarea>`;
 const stamp=r.savedOn?`<span class="chip" style="color:#4FB286;border-color:var(--chip-bd-green)">本周复盘 · 保存于 ${r.savedOn}</span>`:`<span class="chip" style="color:#E8B339;border-color:var(--chip-bd-amber)">本周尚未复盘</span>`;
 return `<div class="card t1"><div class="dh"><h2 class="t">每周复盘<span class="t-en">Trade Journal</span> · ${wk}</h2><span class="nm">把复盘做成每周默认动作</span> ${stamp}</div>
   <div class="badges">${facts.map(f=>`<div class="badge"><div class="l">${f[0]}</div><div class="v">${f[1]}</div></div>`).join('')}</div>
   <div class="note" style="margin:6px 0">事实由面板自动带入——你无法事后改写，只能据此反思。</div>
   ${ta('rvBest','本周最好的决策',r.best)}${ta('rvWorst','本周最差的决策',r.worst)}${ta('rvLesson','学到了什么',r.lesson)}${ta('rvDo','下周要做',r.doNext)}${ta('rvAvoid','下周要避免',r.avoidNext)}
   <div style="margin-top:8px;display:flex;gap:8px"><button id="rvSave" style="padding:6px 14px;border-radius:var(--r-ctl);border:1px solid #E8B339;background:rgba(232,179,57,.12);color:#E8B339;cursor:pointer">保存本周复盘</button><button id="rvClear" style="padding:6px 14px;border-radius:var(--r-ctl);border:1px solid var(--line);background:transparent;color:var(--txt);cursor:pointer">清除本周</button></div></div>`;
}
function thesisCheckpointCard(){
 const J=journalLoad().entries, held=stocks.filter(x=>x.held&&J[x.sym]&&(J[x.sym].planStop!=null||J[x.sym].planTarget!=null));
 if(!held.length)return '';
 const rows=held.map(x=>{const e=J[x.sym],p=x.curPrice;
   let rail='var(--line)',tag='<span class="chip">区间内</span>',msg='现价仍在你写下的失效价与目标价之间。';
   if(e.planStop!=null&&p<=e.planStop){rail='var(--red)';tag='<span class="chip" style="color:#E5707A;border-color:var(--chip-bd-red)">失效触及</span>';msg=`现价 ${fmt(p)} ≤ 你写下的失效价 ${fmt(e.planStop)} —— 论点已被你自己的规则证伪，去复盘是否离场。`;}
   else if(e.planTarget!=null&&p>=e.planTarget){rail='var(--accent)';tag='<span class="chip" style="color:#E8B339;border-color:var(--chip-bd-amber)">目标触及</span>';msg=`现价 ${fmt(p)} ≥ 你写下的目标价 ${fmt(e.planTarget)} —— 按计划兑现，还是上移止盈？`;}
   return `<div class="frow" style="cursor:pointer;border-left:3px solid ${rail};padding-left:9px" tabindex="0" role="button" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();this.click();}" onclick="stockGo('${x.sym}','journal')"><span class="fsym" style="width:auto;min-width:120px">${x.sym} ${tag}</span><span class="note" style="flex:1">${msg}</span></div>`;}).join('');
 return `<div class="card"><div class="dh"><h2 class="t">论点检查点</h2><span class="nm">现价 vs 你写下的失效 / 目标价 —— 让离场由你冷静时的规则触发，而非价格噪音</span></div>${rows}
   <div class="note" style="margin-top:8px">仅对照你在“日志”里写下的计划价，纯描述、不预测；触及不等于必须操作。<b>非投资建议。</b></div></div>`;
}
function journalHonesty(){return foldCard('怎么读 · 边界说明','流程分 ≠ 盈亏 · 小样本仅描述',`
 <div class="note" style="margin-top:8px;line-height:1.65">成熟度评分<b>只看流程、不看盈亏</b>——靠运气赚的钱不会加分。小样本仅描述、非预测（任一分组 n&lt;3 不显示颜色结论）。<b>结果 ≠ 决策质量</b>：别用结果回头改写当时的论点（updatedAt 已标注更新日期；复盘与论点分两个字段正是为此）。不做多维交叉切片（避免在 情绪×周期×信念 里凑出偶然结论）。别过度记录：日志是为了执行计划，不是替代交易。日志与复盘<b>只存在本浏览器本设备</b>，不跨设备、不上传。<b>非投资建议。</b></div>`);}
function journalCard(){return weeklyReview()+maturityCard()+thesisCheckpointCard()+unjournaledWorklist()+`<div class="ref-divider" role="presentation">以下为参考分析 · 点开查看</div>`+killerStatCard()+emotionOutcomeCard()+journalHonesty();}
function wireJournalTab(){const seg=document.querySelector('.seg[data-seg="journal"]');if(!seg)return;
 seg.querySelectorAll('.jwork').forEach(r=>r.onclick=()=>{stockGo(r.dataset.jsym,'journal');});
 const wk=isoWeek(new Date());
 const sv=seg.querySelector('#rvSave');if(sv)sv.onclick=()=>{const g=id=>{const el=seg.querySelector('#'+id);return el?el.value:'';};
   reviewSave(wk,{best:g('rvBest'),worst:g('rvWorst'),lesson:g('rvLesson'),doNext:g('rvDo'),avoidNext:g('rvAvoid'),
     facts:{marketValue:S.marketValue,curReturn:S.curReturn,spReturn:S.spReturn,behaviorGap:S.behaviorGap,maxDrawdown:DATA.risk?DATA.risk.maxDrawdown:null}});
   rerenderJournalTab();};
 const cl=seg.querySelector('#rvClear');if(cl)cl.onclick=()=>{if(reviewGet(wk)&&!confirm('清除本周复盘？'))return;reviewClear(wk);rerenderJournalTab();};}
function rerenderJournalTab(){const seg=document.querySelector('.seg[data-seg="journal"]');if(seg){seg.innerHTML=journalCard();wireJournalTab();}}
function positionJournalEditor(s){
 if(journalDraft===null)journalDraft=journalDrafts[s.sym]=(journalDrafts[s.sym]||Object.assign({},journalEntry(s.sym)||{}));   // restore in-progress draft if user previously navigated away mid-edit
 const d=journalDraft;
 const chip=(field,val,active)=>`<button data-jf="${field}" data-jv="${val}" aria-pressed="${active?'true':'false'}" style="padding:5px 11px;border-radius:var(--r-ctl);border:1px solid ${active?'#E8B339':'var(--line)'};background:${active?'rgba(232,179,57,.12)':'transparent'};color:${active?'#E8B339':'var(--txt)'};cursor:pointer;font-size:var(--t-sm);margin:0 6px 6px 0">${val}</button>`;
 const row=(lbl,body)=>`<div style="margin:9px 0"><div style="font-size:var(--t-sm);color:var(--mut);margin-bottom:5px">${lbl}</div>${body}</div>`;
 const num=(id,v,ph)=>`<input id="${id}" type="number" step="any" value="${v==null?'':v}" placeholder="${ph}" aria-label="${ph}" style="width:90px;background:var(--bg2);color:var(--txt);border:1px solid var(--line);border-radius:6px;padding:4px 7px;margin-right:8px">`;
 const ck=(d.adherence==='计划外'&&((DATA.behavior&&DATA.behavior.biasBySym&&DATA.behavior.biasBySym[s.sym])||[]).some(b=>b.id==='disposition'))?`<div class="note" style="margin:6px 0;color:#E5707A">⚠ 处置效应 + 计划外卖出 = 信号叠加，值得复盘。</div>`:'';
 const ck2=(d.conviction>=4&&d.planStop==null)?`<div class="note" style="margin:6px 0;color:#E8B339">⚠ 高信念却未设失效条件（“看到再说”反模式）。</div>`:'';
 const stamp=d.updatedAt?`<span class="chip" style="color:#4FB286;border-color:var(--chip-bd-green)">上次更新 ${d.updatedAt}</span>`:`<span class="chip" style="color:#E8B339;border-color:var(--chip-bd-amber)">尚未记录 · 未保存</span>`;
 return `<div class="card">
   <div class="dh"><h2 class="t">交易日志 · ${s.sym}</h2><span class="nm">大多是点选，目标 &lt; 1 分钟</span> ${stamp}</div>
   ${row('论点（为什么买/持有）',`<textarea id="jThesis" rows="2" aria-label="论点（为什么买/持有）" style="width:100%;background:var(--bg2);color:var(--txt);border:1px solid var(--line);border-radius:6px;padding:6px 8px;font-family:var(--f-ui);font-size:var(--t-sm);resize:vertical" placeholder="买入时就写下卖出条件，让卖出由逻辑驱动、而非红绿驱动">${jesc(d.thesis)}</textarea>`)}
   ${row('计划（入场 · 目标 · 止损/失效）',num('jEntry',d.planEntry,'入场')+num('jTarget',d.planTarget,'目标')+num('jStop',d.planStop,'止损/失效'))}
   ${row('计划仓位（占组合）',['≤2%','2–5%','5–10%','＞10%'].map(v=>chip('size',v,d.size===v)).join(''))}
   ${row('信念',[1,2,3,4,5].map(v=>chip('conviction',v,d.conviction===v)).join(''))}
   ${row('时间维度',['日内','波段','核心多年'].map(v=>chip('horizon',v,d.horizon===v)).join(''))}
   ${row('情绪',J_CONSTRUCT.concat(J_DESTRUCT).map(v=>chip('emotion',v,d.emotion===v)).join(''))}
   ${row('是否按计划',['在计划内','计划外'].map(v=>chip('adherence',v,d.adherence===v)).join(''))}
   ${row('清单：若我现在空仓，会按这个价格、这个仓位重新买入它吗？',['是','否'].map(v=>chip('checklist',v,d.checklist===v)).join(''))}
   ${ck}${ck2}
   ${row('复盘（事后反思，独立于上面的论点）',`<textarea id="jLesson" rows="2" aria-label="复盘（事后反思，独立于上面的论点）" style="width:100%;background:var(--bg2);color:var(--txt);border:1px solid var(--line);border-radius:6px;padding:6px 8px;font-family:var(--f-ui);font-size:var(--t-sm);resize:vertical" placeholder="别因为亏了就把当时合理的逻辑判成错的；判断决策看当时信息，不看结果。">${jesc(d.lesson)}</textarea>`)}
   <div style="margin-top:8px;display:flex;gap:8px"><button id="jSave" style="padding:6px 14px;border-radius:var(--r-ctl);border:1px solid #E8B339;background:rgba(232,179,57,.12);color:#E8B339;cursor:pointer">保存日志</button><button id="jClear" style="padding:6px 14px;border-radius:var(--r-ctl);border:1px solid var(--line);background:transparent;color:var(--txt);cursor:pointer">清除</button></div>
   <div class="note" style="margin-top:8px">日志只存在本浏览器本设备，不跨设备同步。判断决策看当时信息、不看结果。<b>非投资建议。</b></div></div>`;
}
function wireJournalEditor(s){const seg=document.querySelector('.seg[data-seg="journal"]');if(!seg||!s)return;
 seg.querySelectorAll('[data-jf]').forEach(b=>b.onclick=()=>{const f=b.dataset.jf;let v=b.dataset.jv;if(f==='conviction')v=+v;journalDraft[f]=(journalDraft[f]===v)?null:v;rerenderJournalEditor(s);});
 [['jEntry','planEntry'],['jTarget','planTarget'],['jStop','planStop']].forEach(([id,f])=>{const el=seg.querySelector('#'+id);if(el)el.oninput=()=>{journalDraft[f]=el.value===''?null:+el.value;};});
 [['jThesis','thesis'],['jLesson','lesson']].forEach(([id,f])=>{const el=seg.querySelector('#'+id);if(el)el.oninput=()=>{journalDraft[f]=el.value;};});   // sync textareas live so a chip click (which rebuilds the editor) can't wipe typed text
 const sv=seg.querySelector('#jSave');if(sv)sv.onclick=()=>{const t=seg.querySelector('#jThesis'),l=seg.querySelector('#jLesson');if(t)journalDraft.thesis=t.value;if(l)journalDraft.lesson=l.value;[['jEntry','planEntry'],['jTarget','planTarget'],['jStop','planStop']].forEach(([id,f])=>{const el=seg.querySelector('#'+id);if(el)journalDraft[f]=el.value===''?null:+el.value;});journalSaveEntry(s.sym,journalDraft);journalDraft=journalDrafts[s.sym]=Object.assign({},journalEntry(s.sym));rerenderJournalEditor(s);};
 const cl=seg.querySelector('#jClear');if(cl)cl.onclick=()=>{if(journalEntry(s.sym)&&!confirm('清除这只持仓的日志？'))return;journalClearEntry(s.sym);journalDraft=journalDrafts[s.sym]={};rerenderJournalEditor(s);};}
function rerenderJournalEditor(s){const seg=document.querySelector('.seg[data-seg="journal"]');if(seg){seg.innerHTML=positionJournalEditor(s);wireJournalEditor(s);}}
// Recompute the EMA/momentum/RSI/state ribbons from s.prices on first use (memoized).
// MIRRORS Python compute_fib exactly: same EMA seeding (first value), Wilder RSI with
// first-14 backfill, mom=100*tanh(sep/0.06) rounded to 0.1, state from UNROUNDED EMAs.
// The payload no longer ships these arrays (~40% of the old JSON); signals/resonance/now
// still come from Python and remain the source of truth for headline numbers.
const _fibMemo={};
function fibArrays(s){
 if(_fibMemo[s.sym])return _fibMemo[s.sym];
 const px=s.prices.map(p=>p[1]),L=px.length;
 const ema=n=>{const a=2/(n+1);let e=null;return px.map(v=>e=(e===null?v:a*v+(1-a)*e));};
 const E5=ema(5),E8=ema(8),E13=ema(13),E21=ema(21);
 const N=14,rsi=new Array(L).fill(50);
 if(L>N){
  const d=[];for(let i=1;i<L;i++)d.push(px[i]-px[i-1]);
  let ag=0,al=0;for(let i=0;i<N;i++){if(d[i]>0)ag+=d[i];if(d[i]<0)al-=d[i];}
  ag/=N;al/=N;
  let rs=al>0?ag/al:999;rsi[N]=100-100/(1+rs);
  for(let i=N+1;i<L;i++){const x=d[i-1];ag=(ag*(N-1)+Math.max(x,0))/N;al=(al*(N-1)+Math.max(-x,0))/N;rs=al>0?ag/al:999;rsi[i]=100-100/(1+rs);}
  for(let i=0;i<N;i++)rsi[i]=rsi[N];
 }
 const mom=[],state=[];
 for(let i=0;i<L;i++){
  const sp=E21[i]?(E5[i]-E21[i])/E21[i]:0;
  mom.push(Math.round(100*Math.tanh(sp/0.06)*10)/10);
  const mx=Math.max(E5[i],E8[i],E13[i],E21[i]),mn=Math.min(E5[i],E8[i],E13[i],E21[i]);
  const w=px[i]?(mx-mn)/px[i]*100:0;
  state.push((E5[i]>E8[i]&&E8[i]>E13[i]&&E13[i]>E21[i])?'up':((E5[i]<E8[i]&&E8[i]<E13[i]&&E13[i]<E21[i])?'down':(w<0.8?'range':'mixed')));
 }
 const r2=a=>a.map(v=>Math.round(v*100)/100),r1=a=>a.map(v=>Math.round(v*10)/10);
 return _fibMemo[s.sym]={e5:r2(E5),e8:r2(E8),e13:r2(E13),e21:r2(E21),mom,rsi:r1(rsi),state};
}
function fibChart(s,fmtY){
 const f=s.fib&&Object.assign({},s.fib,fibArrays(s)),prices=s.prices;if(!f)return'<div class="note">价格数据不足，无法计算斐波那契指标。</div>';
 const yl=fmtY||(v=>'$'+fmtTick(v,1));
 const MOB=CHART_MOB,W=MOB?520:900,H=414,mL=AXM.mL,mR=AXM.mR,mT=16,mB=60,stripH=12,stripY=H-mB+24;
 const xmin=+new Date(prices[0][0]),xmax=+new Date(D1);
 let ys=[];prices.forEach((p,i)=>{ys.push(p[1],f.e5[i],f.e21[i]);});
 const yt=niceTicks(Math.min(...ys),Math.max(...ys),4);
 const ymin=yt.min,ymax=yt.max;
 const xs=d=>mL+((+new Date(d)-xmin)/((xmax-xmin)||1))*(W-mL-mR);
 const yc=v=>mT+(1-(v-ymin)/((ymax-ymin)||1))*(H-mT-mB-stripH);
 let el=`<rect x="${mL}" y="${mT}" width="${W-mL-mR}" height="${H-mT-mB-stripH}" fill="${C.bg2}"/>`;
 yt.ticks.forEach(v=>{const y=yc(v);
   el+=`<line x1="${mL}" y1="${y}" x2="${W-mR}" y2="${y}" stroke="${C.hair}"/><text x="${mL-8}" y="${y+4}" fill="${C.mut}" font-size="11" text-anchor="end">${yl(v)}</text>`;});
 let mtx=monthTicks(xmin,xmax);
 if(mtx.length<2)mtx=[0,1,2,3,4,5].map(i=>{const t=xmin+(xmax-xmin)*i/5,dt=new Date(t);return{t,label:(dt.getMonth()+1)+'/'+dt.getDate()};});
 mtx.forEach(g=>{const x=xs(g.t);el+=`<line x1="${x}" y1="${mT}" x2="${x}" y2="${H-mB-stripH}" stroke="${C.hair}"/><text x="${x}" y="${stripY+stripH+12}" fill="${C.mut}" font-size="11" text-anchor="middle">${g.label}</text>`;});
 el+=`<rect x="${mL}" y="${mT}" width="${W-mL-mR}" height="${H-mT-mB-stripH}" fill="none" stroke="${C.line}"/>`+regMarks(mL,mT,W-mL-mR,(typeof stripH!=='undefined')?(H-mT-mB-stripH):(H-mT-mB));
 // ribbon band fill (e5..e21): consecutive same-state days merge into ONE polygon (no corduroy seams)
 {let r0=0;for(let i=2;i<=prices.length;i++){if(i===prices.length||f.state[i]!==f.state[r0+1]){
   const c=FIBCOL[f.state[r0+1]]||C.mut;
   let top='',bot='';
   for(let j=r0;j<=i-1;j++){top+=`${xs(prices[j][0]).toFixed(1)},${yc(f.e5[j]).toFixed(1)} `;}
   for(let j=i-1;j>=r0;j--){bot+=`${xs(prices[j][0]).toFixed(1)},${yc(f.e21[j]).toFixed(1)} `;}
   el+=`<polygon points="${top}${bot}" fill="${c}" fill-opacity="0.13" stroke="none"/>`;r0=i-1;}}}
 // price (faint) + 4 EMAs (fast amber -> slow grey)
 const line=(arr,col,w,key)=>{const pts=arr.map((v,i)=>`${xs(prices[i][0]).toFixed(1)},${yc(key?v[key]:v).toFixed(1)}`).join(' ');return `<polyline points="${pts}" fill="none" stroke="${col}" stroke-width="${w}"/>`;};
 el+=line(prices,C.subj,2,1);   // the SUBJECT is the price, not the indicator
 el+=line(f.e21,C.dim,1.4)+line(f.e13,C.e13,1.4)+line(f.e8,C.ref,1.5)+line(f.e5,C.e8w,1.8);   // indicators never claim the live accent
 // golden/death cross markers + resonance rings: fade by age so the recent signals lead
 const age=d=>{const t=+new Date(d);return 0.25+0.65*Math.max(0,Math.min(1,(t-xmin)/((xmax-xmin)||1)));};
 (f.signals||[]).forEach(g=>{const x=xs(g.date),y=yc(g.price),o=age(g.date).toFixed(2);
   if(g.type==='golden')el+=`<path d="M ${x} ${y-13} l 5 9 l -10 0 z" fill="${C.green}" fill-opacity="${o}" stroke="${C.bg}" stroke-width="0.5"/>`;
   else el+=`<path d="M ${x} ${y+13} l 5 -9 l -10 0 z" fill="${C.red}" fill-opacity="${o}" stroke="${C.bg}" stroke-width="0.5"/>`;});
 (f.resonance||[]).forEach(g=>{const x=xs(g.date),y=yc(g.price),c=g.type==='bull'?C.green:C.red,o=age(g.date).toFixed(2);
   el+=`<circle cx="${x}" cy="${y}" r="8" fill="none" stroke="${c}" stroke-width="1.6" stroke-opacity="${o}"/><circle cx="${x}" cy="${y}" r="2.6" fill="${c}" fill-opacity="${o}"/>`;});
 // current price label
 const cp=prices[prices.length-1][1];el+=`<text x="${W-mR+4}" y="${yc(cp)+4}" fill="${C.subj}" font-size="11" stroke="${C.panel}" stroke-width="3" paint-order="stroke">现价 ${yl(cp)}</text>`;
 // state strip, run-merged like the ribbon
 {let r0=0;for(let i=2;i<=prices.length;i++){if(i===prices.length||f.state[i]!==f.state[r0+1]){const x0=xs(prices[r0][0]),x1=xs(prices[i-1][0]);
   el+=`<rect x="${x0}" y="${stripY}" width="${Math.max(1,x1-x0+0.6)}" height="${stripH}" fill="${FIBCOL[f.state[r0+1]]}" fill-opacity="0.85"/>`;r0=i-1;}}}
 el+=`<text x="${mL-8}" y="${stripY+stripH-1}" fill="${C.mut}" font-size="10" text-anchor="end">状态</text>`;
 const cid='c'+(++CHARTID),SW={up:'多头',down:'空头',range:'盘整',mixed:'转换'};
 const MY=new Date(xmin).getFullYear()!==new Date(xmax).getFullYear();CHARTREG[cid]={dates:prices.map(p=>+new Date(p[0])),ys:prices.map(p=>yc(p[1])),rows:prices.map((p,i)=>{const dt=new Date(p[0]);return '<b>'+fd(dt,MY)+'</b><br><span class="xr"><i style="background:'+C.subj+'"></i>价格 '+yl(p[1])+'</span><span class="xr"><i style="background:'+C.e8w+'"></i>EMA5 '+yl(f.e5[i])+'</span><span class="xr"><i style="background:'+C.dim+'"></i>EMA21 '+yl(f.e21[i])+'</span><span class="xr" style="color:'+(FIBCOL[f.state[i]]||C.mut)+'">状态 '+(SW[f.state[i]]||f.state[i])+'</span>';})};
 el+=`<g class="xg" style="display:none"><line class="cx" x1="0" y1="${mT}" x2="0" y2="${H-mB-stripH}"/><circle class="cxd" r="3.4"/></g><rect class="xhit" x="${mL}" y="${mT}" width="${W-mL-mR}" height="${H-mT-mB-stripH}" fill="transparent"/>`;
 return `<svg id="${cid}" class="xh" role="img" aria-label="数据图表 · 关键数值见下方表格与徽标" data-x0="${xmin}" data-x1="${xmax}" data-ml="${mL}" data-pw="${W-mL-mR}" viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet">${el}</svg>`;
}
function renderFib(s){
 const f=s.fib&&Object.assign({},s.fib,fibArrays(s));if(!f)return`<div class="card"><div class="dh"><h2 class="t">斐波那契动能分析</h2><span class="nm">EMA 5 / 8 / 13 / 21 缎带 · 动能 · RSI</span></div><div class="note">价格数据不足（需 ≥21 个交易日），无法计算该股的斐波那契指标。新建仓或上市不久的标的会出现这种情况。</div></div>`;
 const n=f.now,sc=momColor(n.mom);
 const rsiCol=n.rsi>70?'#E5707A':(n.rsi<30?'#4FB286':'var(--txt)');
 const lastSig=(f.signals||[]).slice(-1)[0];
 const ser=s.prices.map((p,i)=>({date:p[0],mom:f.mom[i],rsi:f.rsi[i]}));
 const resChip=n.res==='bull'?'<span class="chip" style="color:#4FB286;border-color:var(--chip-bd-green);background:rgba(79,178,134,.1)">⚡ 多头共振</span>'
   :(n.res==='bear'?'<span class="chip" style="color:#E5707A;border-color:var(--chip-bd-red);background:rgba(229,112,122,.1)">⚡ 空头共振</span>':'');
 const badges=[
  ['斐波那契状态',`<span style="color:${FIBCOL[n.state]}">●</span> ${n.label}`],
  [gl('mom','动能强弱'),`<span style="color:${sc}">${n.mom>0?'+':''}${n.mom}</span> <span class="note">/100</span>`],
  [gl('rsi','RSI(14)'),`<span style="color:${rsiCol}">${n.rsi}</span>`],
  ['最近信号',lastSig?(lastSig.type==='golden'?`<span class="pos">金叉 ${lastSig.date}</span>`:`<span class="neg">死叉 ${lastSig.date}</span>`):'—'],
  [gl('res','多指标共振'),n.res==='bull'?'<span class="pos">多头共振中</span>':(n.res==='bear'?'<span class="neg">空头共振中</span>':'无')],
 ];
 return `<div class="card">
   <div class="dh"><h2 class="t">斐波那契动能分析</h2>${resChip}<span class="nm">EMA 5 / 8 / 13 / 21 缎带 · 动能 · RSI</span></div>
   <div class="badges">${badges.map(b=>`<div class="badge"><div class="l">${b[0]}</div><div class="v">${b[1]}</div></div>`).join('')}</div>
   <div class="legend">
     <span><i class="ln" style="color:#C99A3A"></i>EMA5</span><span><i class="ln" style="color:#B89030"></i>EMA8</span>
     <span><i class="ln" style="color:#8C8A6E"></i>EMA13</span><span><i class="ln" style="color:#5F6168"></i>EMA21</span>
     <span><span style="color:#4FB286">▲</span> 金叉</span><span><span style="color:#E5707A">▼</span> 死叉</span>
     <span>◎ 共振信号(多空趋势+金/死叉+RSI未极端)</span>
     <span>底部状态带：<span style="color:#4FB286">绿=多头</span> / <span style="color:#E5707A">红=空头</span> / <span style="color:#B89030">黄=转换</span> / <span style="color:#888D96">灰=盘整</span></span>
   </div>
   ${fibChart(s)}
   <div class="cap" style="margin:12px 0 2px">动能振荡器（−100 ~ +100）</div>
   ${svgLines(ser,[{key:'mom',color:'#C99A3A',draw:1,label:'动能'}],{zero:true,h:200,fixed:[-100,100],fmt:v=>v.toFixed(0),
     guides:[{v:15,color:'#2f6b4f',labelColor:'#4FB286',label:'强多'},{v:-15,color:'#6b2f2f',labelColor:'#E5707A',label:'强空'}],marks:f.signals})}
   <div class="cap" style="margin:12px 0 2px">RSI(14)</div>
   ${svgLines(ser,[{key:'rsi',color:'#888D96',label:'RSI'}],{h:180,fixed:[0,100],fmt:v=>v.toFixed(0),
     guides:[{v:70,color:'#6b2f2f',labelColor:'#E5707A',label:'超买70'},{v:30,color:'#2f6b4f',labelColor:'#4FB286',label:'超卖30'}],marks:f.signals})}
   <details class="note" style="margin-top:10px"><summary style="cursor:pointer">怎么读 · 诚实说明</summary><b>怎么读：</b>四条 EMA 像缎带——向上发散（绿）= 快线在上、多头排列、动能强；向下发散（红）= 空头；缠绕（灰）= 盘整观望，信号不可靠。动能值是 EMA5 相对 EMA21 的偏离度（±100 封顶），RSI>70 超买、<30 超卖。<br>
   <b>多指标共振(◎ 圆环)：</b>同时满足「均线多头/空头排列 + 3 日内出现金叉/死叉 + RSI 未到超买/超卖」三个条件才标记——比单一信号更高确信度，能过滤掉震荡市里的假交叉。<br>
   <b>诚实说明：</b>“斐波那契周期更神奇”在学术上并无强证据——5/8/13/21 相比其它周期没有统计显著的超额收益。它真正有用的地方是<b>周期按几何级数(≈1.6 倍)递增</b>，天然形成快/中/慢分层，便于判断趋势结构；这来自间距而非数字的“神秘性”。本面板为技术分析参考，<b>非投资建议</b>。</details>
 </div>`;
}
function renderDetail(){
 if(sel==='__OV__'){renderOverview();return;}
 CHARTREG={};journalDraft=(sel!=='__OV__'?(journalDrafts[sel]||null):null);   // restore previously-typed draft for this symbol (positionJournalEditor lazy-initializes if absent)
 const _ins=document.getElementById('insight');if(_ins)_ins.innerHTML='';   // banner is overview-only
 const s=stocks.find(x=>x.sym===sel);if(!s){document.getElementById('right').innerHTML='';return;}
 const badges=[['当前持股',s.held?fmtN(s.shares)+' 股':'已清仓'],['平均成本',s.held?fmt(s.avg):'—'],
  ['现价',fmt(s.curPrice)],['市值',s.held?fmt(s.value):'—'],
  ['未实现盈亏',s.held?`<span class="${cls(s.unreal)}">${fmt(s.unreal)} (${pct(s.unrealPct)})</span>`:'—'],
  ['已实现盈亏(窗口)',`<span class="${cls(s.realized)}">${fmt(s.realized)}</span>`]];
 const rows=s.txns.map(t=>{const tag=t.side==='BUY'?'<span class="tag b">买入</span>':(t.side==='SELL'?'<span class="tag s">卖出</span>':'<span class="tag o">期初底仓</span>');
   return `<tr><td class="l">${t.date}</td><td class="l">${tag}</td><td>${fmtN(t.qty,0)}</td><td>${fmt(t.price)}</td>
    <td>${fmt(t.amount)}</td><td>${fmtN(t.pos,0)}</td><td>${t.avg?fmt(t.avg):'—'}</td>
    <td class="${cls(t.realized)}">${t.realized==null?'—':fmt(t.realized)}</td></tr>`;}).join('');
 document.getElementById('right').innerHTML=`
 ${viewBarStock(s)}
 <nav aria-label="个股分页" class="seg-rail-wrap"><div class="seg-rail"><button class="on" data-seg="price" title="价格曲线与你的买卖点">价格 · 操作</button><button data-seg="fin" title="FMP 财务质量、财报记录、下一财报风险与市场行为">财务状态</button><button data-seg="tx" title="逐笔交易明细">交易明细</button><button data-seg="fib" title="这只股票的斐波那契动能">斐波那契</button><button data-seg="journal" title="给这只持仓写下论点、计划与情绪（目标 <1 分钟）">日志</button></div></nav>
 <div class="seg segin" data-seg="price">
 <div class="card">
   <div class="dh"><h2 class="t">${s.sym}</h2><span class="nm">${s.name}</span>
     ${s.hasLegacy?'<span class="legacychip">含 '+D0+' 前旧底仓 · 成本按当日市价估算</span>':''}
     ${(()=>{const j=journalEntry(s.sym);if(!s.held)return '';
       return j?`<span class="chip" style="cursor:pointer;color:var(--mut);border-color:var(--line)" role="button" tabindex="0" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();this.click();}" onclick="stockGo('${s.sym}','journal')" title="上次更新：${j.updatedAt||'—'}">📝 已记录</span>`:`<span class="chip" style="cursor:pointer;color:var(--amber-line);border-color:var(--chip-bd-amber)" role="button" tabindex="0" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();this.click();}" onclick="stockGo('${s.sym}','journal')" title="给这只持仓写下论点与计划 · 目标 <1 分钟">📝 未记录论点 · 写入</span>`;
     })()}</div>
   <div class="badges">${badges.map(b=>`<div class="badge"><div class="l">${b[0]}</div><div class="v">${b[1]}</div></div>`).join('')}${financialMiniBadge(s.sym)}${marketMassStockBadge(s.sym)}</div>
   <div class="legend"><span><i class="ln" style="color:#8A8F98"></i>市场价格(Yahoo)</span><span><i class="lnd" style="color:#B89030"></i>持仓平均成本</span>
     <span><i class="ln" style="color:#E8B339"></i>当前价</span><span><i style="background:#4FB286;border-radius:50%"></i>买入(大小=金额)</span><span><i style="background:transparent;border:2px solid #E5707A;border-radius:50%;box-sizing:border-box"></i>卖出(空心环)</span></div>
   <div class="chartbox">${chart(s)}</div>
   <div class="note" style="margin-top:8px"><b>怎么读：</b>灰线 = 市场收盘价(Yahoo)，琥珀阶梯 = 你的持仓平均成本，绿实心 = 买入、红空心环 = 卖出（圆点大小 ≈ 成交金额）。价格与成交点都是历史事实，不预测未来。<b>非投资建议。</b></div></div>
 </div>
 <div class="seg" data-seg="fin" hidden>`+financialStockDetailCard(s)+`</div>
 <div class="seg" data-seg="tx" hidden>
 <div class="card"><div class="dh"><h2 class="t">交易明细</h2><span class="nm">${s.numTrades} 笔交易${s.hasLegacy?' · 另含期初底仓 1 行':''}</span></div>
   <div class="scroll"><table><thead><tr><th class="l">日期</th><th class="l">动作</th><th>数量</th><th>成交价</th><th>金额</th><th>持仓后</th><th>均价后</th><th>已实现</th></tr></thead>
   <tbody>${rows}</tbody></table></div></div>
 </div>
 <div class="seg" data-seg="fib" hidden>`+renderFib(s)+`</div>
 <div class="seg" data-seg="journal" hidden>`+positionJournalEditor(s)+`</div>`;
 bindMarkers();segWire();bindCharts();updateCtx();wireJournalEditor(s);renderOptSec();fixThScope(document.getElementById('right'));armDraw(document.getElementById('right'));   // restoreSeg removed — URL is source of truth; renderOptSec replaces the MutationObserver
}
function segWire(){const r=document.getElementById('right');
 r.querySelectorAll('.seg-rail,.ws-rail').forEach(rail=>rail.setAttribute('role','tablist'));
 r.querySelectorAll('.ws-rail button[data-ws]').forEach(b=>{b.setAttribute('role','tab');b.setAttribute('aria-selected',b.classList.contains('on')?'true':'false');});
 r.querySelectorAll('.seg-rail button').forEach(b=>{
  b.setAttribute('role','tab');b.setAttribute('aria-selected',b.classList.contains('on')?'true':'false');
  {const sg=b.dataset.seg,panel=r.querySelector('.seg[data-seg="'+sg+'"]');if(panel){const tid='segtab-'+sg,pid='segpanel-'+sg;b.id=tid;b.setAttribute('aria-controls',pid);panel.id=pid;panel.setAttribute('role','tabpanel');panel.setAttribute('aria-labelledby',tid);if(!panel.hasAttribute('tabindex'))panel.setAttribute('tabindex','0');}}
  b.onclick=()=>activateSeg(b.dataset.seg,{push:true});});}
function restoreSeg(ctx){if(routeApplying)return;try{var want=localStorage.getItem('ptrak.seg.'+ctx);if(want)activateSeg(want,{push:false});}catch(e){}}
function onboardStrip(){let done=false;try{done=localStorage.getItem('ptrak.onboard.v1')==='done';}catch(e){}if(done)return'';
 const j=(seg,t)=>`<span class="ib-lk" role="button" tabindex="0" style="margin-left:0" onclick="ovGo('${seg}')">${t}</span>`;
 return `<div id="onboard" class="note" style="margin-bottom:14px;padding:10px 13px;background:var(--panel);border:1px solid var(--line);border-radius:var(--r-card);line-height:1.7">
   <b style="color:var(--accent)">从这里开始</b> · 想知道我赚了多少 → ${j('score','决策一览')} / ${j('nw','净值')}　|　财务/财报 → ${j('fin','财务状态')}　|　AI/半导体框架 → ${j('aisemi','AI半导体')}　|　QQQ/TQQQ 节奏 → ${j('qt','策略台')}　|　钱和风险集中在哪 → ${j('struct','结构')} / ${j('risk','风险')}　|　该不该调仓 → ${j('beh','行为决策')} / ${j('rebal','再平衡计划')}
   <span role="button" tabindex="0" style="float:right;cursor:pointer;color:var(--mut)" onclick="try{localStorage.setItem('ptrak.onboard.v1','done')}catch(e){};var _o=document.getElementById('onboard-slot');if(_o)_o.innerHTML=onboardStrip()">不再显示 ✕</span></div>`;}
/* ===== plain-language glossary: hover/tap any .gl term -> reuse the #tt tooltip ===== */
const GLOSS={
 thesisCov:'论点覆盖率：写了买入逻辑的持仓 ÷ 全部持仓。没论点的仓位无法事后复盘，会拉低成熟度评分。',
 padh:'计划遵守率：标记“在计划内”的笔数 ÷ 有标记的笔数。衡量冷静时的计划有没有被上头时推翻。',
 ckl:'清单完成率：下单前过了“若空仓今天还会买吗”这条检查的比例。',
 jrnCov:'日志覆盖率：写过任何日志的持仓 ÷ 全部持仓。赚钱但没记录的一周会降低覆盖率、从而降低评分（反虚荣）。',
 emo:'情绪纪律：1 − 破坏性情绪（FOMO追高/报复/无聊）占比。下单时少被情绪驱动的程度。',
 killer:'计划遵守 → 结果：按计划 vs 拍脑袋两组的平均结果差。这是你自己的记录，不是买卖信号；小样本仅描述。',
 emoOut:'情绪 → 结果：各情绪标签下持仓的平均未实现%。看见报复/追涨平均更差，但小样本不预测未来。',
 TWR:'时间加权收益率：剔除你出入金时点的影响，衡量选股 / 策略本身的好坏。',
 MWR:'资金加权收益率：把你真实的出入金时点算进去，是你的钱实际经历的回报。',
 XIRR:'按真实出入金现金流求出的年化资金加权收益率（仅股票账面口径）。',
 gap:'行为缺口 = 时间加权 − 资金加权；正(红)=你的择时拖了后腿，负(绿)=择时帮了忙。',
 alpha:'超额收益：你的组合相对 S&P500 多赚或少赚的百分点。',
 HHI:'集中度指数：各持仓权重平方和，越大越集中、越靠近押注单一标的。',
 effN:'有效持仓数 ≈ 1 ÷ HHI：把同因子标的合并后，约等于几个独立押注（常远小于名义只数）。',
 beta:'Beta：相对大盘(S&P)的波动倍数，>1 比大盘更猛、<1 更稳。窗口内日收益小样本估计，区间窄时不稳定、非预测。',
 dd:'回撤：相对历史最高点跌了多少（描述风险，不预测未来）。',
 vol:'年化波动率：日收益标准差×√252（颠簸幅度，非盈亏）。这里红=高于S&P（更颠簸），绿=低于S&P（更稳）；红≠亏损。',
 rc:'风险贡献：组合总波动按各持仓边际贡献拆开（合计100%），不等于资金权重。',
 mom:'动能：EMA5 相对 EMA21 的偏离度，±100 封顶，正=多头、负=空头。',
 rsi:'RSI(14)：0–100 的强弱摆动，>70 偏超买、<30 偏超卖（技术参考）。',
 cross:'金叉 / 死叉：快线 EMA5 上穿 / 下穿 EMA13，比传统 50 / 200 日更灵敏。',
 attn:'关注度：四类红色信号的计数，是默认排序键，不是评分也不是买卖建议。',
 res:'共振：趋势排列 + 近3日金 / 死叉 + RSI 未极端 三者同向，确信度更高的技术信号。',
 atr:'ATR(14)：平均真实波幅，衡量近 14 日每天大约波动多少美元，用来把 EMA 变成价格作战带（技术参考）。',
 ema:'EMA：指数移动平均，越近的价格权重越大；EMA8 / 21 / 55 的排列用来判断趋势方向（技术参考）。',
 ccs:'CCS（Call Credit Spread，看涨信用价差）：卖近一档 call、买远一档 call 的定义化风险组合，过热时小仓给多头加一道短线刹车。',
 dte:'DTE（Days To Expiry）：期权距到期还有几天；越临近到期，时间价值与指派风险变化越快。',
 breakeven:'盈亏平衡价：到期时不赚不亏的标的价位（行权价 ± 净权利金）。',
 mark:'Mark（市价）：券商对持仓的当前估值，不是你的成本，也不是 delta / 名义敞口。',
 notional:'名义本金（Notional）：期权对应的标的总市值（行权价×100×张数），通常远大于期权市价——真实杠杆藏在这里。',
 marginDebit:'保证金借记（Margin Debit）：向券商借的钱；不在本导出中，若存在会让“可动用现金 / 净值”被高估。',
 sharpe:'夏普比率（rf=0）：年化收益 ÷ 年化波动，衡量每单位颠簸换来多少回报。小样本、描述性、非预测。',
 sortino:'索提诺比率（rf=0）：年化收益 ÷ 仅下行波动，只罚“向下”的颠簸。小样本、描述性、非预测。',
 assign:'指派风险（Assignment）：卖出的期权被对手行权、你被迫买 / 卖正股的风险；越价内、越临近到期越高。',
 rvr:'收益 / 波动比：年化收益 ÷ 年化波动（rf=0 的近似夏普）。小样本、描述性、非预测。',
 size:'计划仓位：你在冷静时打算让这笔占组合多大比重，用来事后检查有没有超配。',
 defrisk:'定义化风险（Defined Risk）：垂直价差等组合最大盈亏被价差宽度封顶，不像裸卖期权那样敞口无限。',
 PGR:'盈利兑现率（PGR）：把赚钱仓位卖掉的倾向；与 PLR 一起看处置效应（赢家拿不住、输家舍不得割）。',
 PLR:'亏损兑现率（PLR）：把亏钱仓位卖掉的倾向；PGR≫PLR 是典型处置效应。',
 turnover:'换手率：一段时间买卖金额相对组合规模的比例；越高越接近过度交易，摩擦成本越大。'
};
function gl(k,l){return GLOSS[k]?'<span class="gl" tabindex="0" data-g="'+k+'">'+l+'</span>':l;}
(function(){
 const tt=document.getElementById('tt');if(!tt)return;let pinned=false;
 function setPinned(v){pinned=v;window.__ttPinned=v;}   // expose for the Esc-to-back handler so it only defers to PINNED glossary tooltips, not transient hovers
 function show(el,x,y){const d=GLOSS[el.dataset.g];if(!d)return;
   tt.className='tt gl-tt';tt.innerHTML='<span class="gk">'+(el.textContent||'')+'</span>'+d;tt.style.display='block';
   const w=tt.offsetWidth||260;let L=x+14;if(L+w>window.innerWidth-8)L=window.innerWidth-w-8;if(L<8)L=8;
   tt.style.left=L+'px';tt.style.top=(y+14)+'px';}
 function hide(){if(pinned)return;tt.style.display='none';tt.classList.remove('gl-tt');}
 function unpin(){setPinned(false);tt.style.display='none';tt.classList.remove('gl-tt');}
 document.addEventListener('mouseover',e=>{const g=e.target.closest&&e.target.closest('.gl');if(g&&!pinned)show(g,e.clientX,e.clientY);});
 document.addEventListener('mousemove',e=>{const g=e.target.closest&&e.target.closest('.gl');if(g&&!pinned)show(g,e.clientX,e.clientY);});
 document.addEventListener('mouseout',e=>{const g=e.target.closest&&e.target.closest('.gl');if(g)hide();});
 document.addEventListener('click',e=>{const g=e.target.closest&&e.target.closest('.gl');
   if(g){unpin();setPinned(true);const r=g.getBoundingClientRect();show(g,r.left-14,r.bottom-14);e.stopPropagation();}
   else if(pinned)unpin();});
 document.addEventListener('keydown',e=>{const a=document.activeElement;
   if((e.key==='Enter'||e.key===' ')&&a&&a.classList&&a.classList.contains('gl')){e.preventDefault();a.click();}
   else if(e.key==='Escape'&&pinned)unpin();});
})();
function bindMarkers(){
 const tt=document.getElementById('tt');
 document.querySelectorAll('.mk').forEach(m=>{
   m.onmousemove=e=>{const s=stocks.find(x=>x.sym===m.dataset.sym);
     const idxs=(m.dataset.g||m.dataset.i||'0').split(',').map(Number),ts=idxs.map(i=>s.txns[i]).filter(Boolean),t=ts[0];
     tt.classList.remove('gl-tt');
     tt.style.display='block';tt.style.left=(e.clientX+14)+'px';tt.style.top=(e.clientY+14)+'px';
     const side=t.side==='BUY'?'买入':(t.side==='SELL'?'卖出':'期初底仓');
     if(ts.length===1){
       tt.innerHTML=`<b>${m.dataset.sym} · ${side}</b><br>${t.date}<br>数量 ${fmtN(t.qty,0)} @ ${fmt(t.price)}<br>金额 ${fmt(t.amount)}${t.realized!=null?'<br>已实现 <span class="'+cls(t.realized)+'">'+fmt(t.realized)+'</span>':''}`;
     }else{   // merged same-day same-side marker: summary line + the individual legs
       const qty=ts.reduce((a,x)=>a+(x.qty||0),0),amt=ts.reduce((a,x)=>a+(x.amount||0),0);
       const wp=qty?ts.reduce((a,x)=>a+(x.price||0)*(x.qty||0),0)/qty:t.price;
       const legs=ts.slice(0,4).map(x=>`${x.date.slice(5)} · ${fmtN(x.qty,0)} @ ${fmt(x.price)}`).join('<br>')+(ts.length>4?`<br>… 共 ${ts.length} 笔`:'');
       const dr=ts[0].date===ts[ts.length-1].date?t.date:`${ts[0].date} → ${ts[ts.length-1].date.slice(5)}`;
       tt.innerHTML=`<b>${m.dataset.sym} · ${side} ×${ts.length}</b><br>${dr}<br>合计 ${fmtN(qty,0)} @ 均价 ${fmt(wp)}<br>金额 ${fmt(amt)}<br><span class="note">${legs}</span>`;
     }};
   m.onmouseleave=()=>tt.style.display='none';
   m.ontouchstart=e=>{if(e.touches&&e.touches.length>1)return;const p=e.touches&&e.touches[0];if(!p)return;e.stopPropagation();e.preventDefault();m.onmousemove({clientX:p.clientX,clientY:p.clientY});};});   // multi-touch → defer to native pinch-zoom; single-touch shows the trade-marker tooltip
 if(!window.__mkTouchWired){window.__mkTouchWired=1;document.addEventListener('touchstart',ev=>{if(!(ev.target.classList&&ev.target.classList.contains('mk'))&&!(ev.target.closest&&ev.target.closest('svg.xh')))tt.style.display='none';},{passive:true});}
}
function bindCharts(){
 if(window.__xhWired)return;window.__xhWired=1;
 const tt=document.getElementById('tt');if(!tt)return;
 function place(svg,e){
   if(e.target.closest&&e.target.closest('.mk'))return;          // defer to trade-dot tooltip
   const reg=CHARTREG[svg.id];if(!reg||!reg.dates.length)return;
   const x0=+svg.dataset.x0,x1=+svg.dataset.x1,ml=+svg.dataset.ml,pw=+svg.dataset.pw;
   const box=svg.getBoundingClientRect();if(!box.width)return;
   const vbW=(svg.viewBox&&svg.viewBox.baseVal&&svg.viewBox.baseVal.width)||900;
   const ux=(e.clientX-box.left)/box.width*vbW;
   if(ux<ml-4||ux>ml+pw+4){clear(svg);return;}
   const epoch=x0+((ux-ml)/(pw||1))*((x1-x0)||1);
   let lo=0,hi=reg.dates.length-1;while(lo<hi){const mid=(lo+hi)>>1;if(reg.dates[mid]<epoch)lo=mid+1;else hi=mid;}
   if(lo>0&&Math.abs(reg.dates[lo-1]-epoch)<Math.abs(reg.dates[lo]-epoch))lo--;
   const cx=ml+((reg.dates[lo]-x0)/((x1-x0)||1))*pw;
   const g=svg.querySelector('.xg');
   if(g){const ln=g.querySelector('.cx'),dot=g.querySelector('.cxd');ln.setAttribute('x1',cx);ln.setAttribute('x2',cx);dot.setAttribute('cx',cx);const dy=(reg.ys&&reg.ys[lo]!=null)?reg.ys[lo]:ln.getAttribute('y1');dot.setAttribute('cy',dy);g.style.display='';}   // dot rides the primary series, not the frame
   // linked panes: same-window charts in the same card echo the hairline (one tooltip, synced time)
   const host=svg.closest('.card,.foldbody');
   if(host)host.querySelectorAll('svg.xh').forEach(o=>{
     if(o===svg||o.dataset.x0!==svg.dataset.x0||o.dataset.x1!==svg.dataset.x1)return;
     const oreg=CHARTREG[o.id];if(!oreg||!oreg.dates.length)return;
     let l2=0,h2=oreg.dates.length-1;while(l2<h2){const m2=(l2+h2)>>1;if(oreg.dates[m2]<epoch)l2=m2+1;else h2=m2;}
     if(l2>0&&Math.abs(oreg.dates[l2-1]-epoch)<Math.abs(oreg.dates[l2]-epoch))l2--;
     const ocx=(+o.dataset.ml)+((oreg.dates[l2]-(+o.dataset.x0))/(((+o.dataset.x1)-(+o.dataset.x0))||1))*(+o.dataset.pw);
     const og=o.querySelector('.xg');
     if(og){const oln=og.querySelector('.cx'),od=og.querySelector('.cxd');oln.setAttribute('x1',ocx);oln.setAttribute('x2',ocx);od.setAttribute('cx',ocx);od.setAttribute('cy',(oreg.ys&&oreg.ys[l2]!=null)?oreg.ys[l2]:oln.getAttribute('y1'));og.style.display='';}});
   tt.classList.remove('gl-tt');tt.innerHTML=reg.rows[lo];tt.style.display='block';
   const w=tt.offsetWidth||180;let L=e.clientX+14;if(L+w>window.innerWidth-8)L=window.innerWidth-w-8;if(L<8)L=8;
   tt.style.left=L+'px';tt.style.top=(e.clientY+14)+'px';
 }
 function clear(svg){const g=svg&&svg.querySelector('.xg');if(g)g.style.display='none';const host=svg&&svg.closest&&svg.closest('.card,.foldbody');if(host)host.querySelectorAll('svg.xh .xg').forEach(x=>x.style.display='none');if(!tt.classList.contains('gl-tt'))tt.style.display='none';}
 document.addEventListener('mousemove',e=>{const svg=e.target.closest&&e.target.closest('svg.xh');if(svg)place(svg,e);});
 // touch: single tap pins the crosshair + readout (no touchmove — scroll/pinch untouched); tap outside clears via the .mk hider's svg guard
 document.addEventListener('touchstart',e=>{if(e.touches&&e.touches.length>1)return;const t=e.touches&&e.touches[0];if(!t)return;const svg=e.target.closest&&e.target.closest('svg.xh');if(svg&&!(e.target.closest&&e.target.closest('.mk')))place(svg,{clientX:t.clientX,clientY:t.clientY,target:e.target});},{passive:true});
 document.addEventListener('mouseout',e=>{const svg=e.target.closest&&e.target.closest('svg.xh');if(svg&&!svg.contains(e.relatedTarget))clear(svg);});
 // Crosshair tracking on touchmove is desktop-only (hover-driven). On (pointer:coarse) devices the user can't see a line under their finger anyway, and a non-passive touchmove blocks page scroll + pinch-zoom.
 // Coarse-pointer users get tap-to-pin via the trade-marker handlers above; the chart's data still readable via the markers and the table view.
 const _coarsePtr=window.matchMedia&&window.matchMedia('(pointer:coarse)').matches;
 if(!_coarsePtr){
   document.addEventListener('touchmove',e=>{
     if(e.touches&&e.touches.length>1)return;   // multi-touch → defer to native pinch-zoom (was missing — pinch was being blocked over charts)
     const t=e.touches[0];if(!t)return;
     if(t.clientX<24||t.clientX>window.innerWidth-24)return;   // skip iOS edge-swipe-back/forward zones
     const el=document.elementFromPoint(t.clientX,t.clientY),s=el&&el.closest&&el.closest('svg.xh');
     if(s){place(s,t);e.preventDefault();}
   },{passive:false});
 }
}
function renderOptions(){   // reference foldCard (default-closed), rendered ONLY inside the 净值 seg — was a ~900px coda appended to all 11 tabs
 if(!DATA.options.length)return'';
 const rows=DATA.options.map(o=>{const tr=o.txns.map(t=>`<tr><td class="l">${t.date}</td><td class="l">${t.side==='BUY'?'<span class="tag b">买入</span>':(t.side==='SELL'?'<span class="tag s">卖出</span>':'<span class="tag o">到期/行权</span>')}</td><td>${t.qty}</td><td>${fmt(t.price)}</td><td>${fmt(t.amount)}</td></tr>`).join('');
   return `<details><summary>${o.sym} · 净现金流 <span class="${cls(o.net)}">${fmt(o.net)}</span></summary>
     <table><thead><tr><th class="l">日期</th><th class="l">动作</th><th>合约</th><th>权利金</th><th>金额</th></tr></thead><tbody>${tr}</tbody></table></details>`;}).join('');
 return foldCard('期权交易',`净现金流合计 <span class="${cls(S.optNet)}">${fmt(S.optNet)}</span> · ${DATA.options.length} 个合约逐笔`,rows);
}
/* ===== ptrak telemetry: local-only ring buffer for the trader's back-and-forth flow =====
   Opt-in. Run window.ptrak_telemetry_on() in DevTools to start, ptrak_stats() to read,
   ptrak_telemetry_off() to stop, ptrak_telemetry_clear() to wipe. Buffer caps at 500 events
   in localStorage 'ptrak.nav.v1'. Nothing leaves the browser. */
const PTRAK_TM={KEY:'ptrak.nav.v1', MAX:500, ON_KEY:'ptrak.telemetry.v1'};
function _tmOn(){try{return localStorage.getItem(PTRAK_TM.ON_KEY)==='on';}catch(e){return false;}}
function _tmLog(ev){if(!_tmOn())return;
 try{let buf=[];try{buf=JSON.parse(localStorage.getItem(PTRAK_TM.KEY)||'[]');if(!Array.isArray(buf))buf=[];}catch(e){buf=[];}
   buf.push(Object.assign({t:Date.now()},ev));
   if(buf.length>PTRAK_TM.MAX)buf=buf.slice(buf.length-PTRAK_TM.MAX);
   localStorage.setItem(PTRAK_TM.KEY,JSON.stringify(buf));
 }catch(e){}
}
window.ptrak_telemetry_on=function(){try{localStorage.setItem(PTRAK_TM.ON_KEY,'on');console.log('%cptrak telemetry: ON — events log locally to localStorage.'+PTRAK_TM.KEY,'color:#4FB286');}catch(e){}};
window.ptrak_telemetry_off=function(){try{localStorage.setItem(PTRAK_TM.ON_KEY,'off');console.log('%cptrak telemetry: OFF','color:#E5707A');}catch(e){}};
window.ptrak_telemetry_clear=function(){try{localStorage.removeItem(PTRAK_TM.KEY);console.log('ptrak telemetry buffer cleared');}catch(e){}};
window.ptrak_stats=function(days){
 days=days||7;
 const since=Date.now()-days*86400000;
 let buf=[];try{buf=JSON.parse(localStorage.getItem(PTRAK_TM.KEY)||'[]');if(!Array.isArray(buf))buf=[];}catch(e){buf=[];}
 const recent=buf.filter(e=>e&&e.t>=since);
 const navs=recent.filter(e=>e.type==='nav');
 const dwell={}, visits={};
 for(let i=0;i<navs.length;i++){
   const key=navs[i].sym+'·'+(navs[i].seg||'');
   visits[key]=(visits[key]||0)+1;
   if(i<navs.length-1){const ms=navs[i+1].t-navs[i].t;if(ms>0&&ms<3600000)dwell[key]=(dwell[key]||0)+ms;}
 }
 const rows=Object.keys(visits).map(k=>({view:k, visits:visits[k], dwell_sec:Math.round((dwell[k]||0)/1000)})).sort((a,b)=>b.visits-a.visits).slice(0,25);
 const srcCount={};navs.forEach(n=>{srcCount[n.src||'?']=(srcCount[n.src||'?']||0)+1;});
 const backRate=navs.length?(srcCount.back||0)/navs.length:0;
 console.log('%c— ptrak '+days+'d telemetry —','font-weight:bold;color:#E8B339');
 console.log('events:',recent.length,'· navs:',navs.length,'· back-button rate:',(backRate*100).toFixed(1)+'%');
 console.table(rows);
 console.log('nav sources:',srcCount,'· filter/sort/search events:',recent.filter(e=>e.type==='filter'||e.type==='sort'||e.type==='search').length);
 return {events:recent.length, navs:navs.length, backRate, srcCount, topViews:rows};
};
['pagehide','visibilitychange','beforeunload'].forEach(ev=>window.addEventListener(ev,()=>{
 if(ev==='visibilitychange'&&!document.hidden)return;
 _tmLog({type:'leave',sym:sel,seg:activeSeg()});
}));
try{if(!_tmOn())console.log('%cptrak: nav telemetry is OFF (privacy default). Run window.ptrak_telemetry_on() to enable a local-only ring buffer; window.ptrak_stats() to see your own usage.','color:#6B7079;font-style:italic');}catch(e){}
let __searchT=0;
function rememberList(){try{localStorage.setItem('ptrak.list.v1',JSON.stringify({q,f:filter,s:sortKey}));}catch(e){}}
function syncListUI(){   // mirror restored state into the visible controls when the URL/storage rehydrates filter/q/sortKey
 try{const si=document.getElementById('search');if(si&&si.value!==q)si.value=q;
   const so=document.getElementById('sort');if(so&&so.value!==sortKey)so.value=sortKey;
   document.querySelectorAll('.tabs button').forEach(b=>{const on=b.dataset.f===filter; b.classList.toggle('on',on); b.setAttribute('aria-selected',on?'true':'false');});
 }catch(e){}
}
document.getElementById('search').oninput=e=>{q=e.target.value.toLowerCase().trim();
 clearTimeout(__searchT);
 __searchT=setTimeout(()=>{renderHoldingsList();rememberList();writeRoute(sel,activeSeg(),true);_tmLog({type:'search',q});},150);   // debounce + replaceState (search is view-state, not a page)
};
document.getElementById('search').addEventListener('keydown',e=>{if(e.key==='Enter'){clearTimeout(__searchT);renderHoldingsList();rememberList();writeRoute(sel,activeSeg(),true);_tmLog({type:'search',q});}});
document.getElementById('sort').onchange=e=>{sortKey=e.target.value;renderHoldingsList();rememberList();writeRoute(sel,activeSeg(),true);_tmLog({type:'sort',s:sortKey});};
document.querySelectorAll('.tabs button').forEach(b=>b.onclick=()=>{
 document.querySelectorAll('.tabs button').forEach(x=>{const on=x===b;x.classList.toggle('on',on);x.setAttribute('aria-selected',on?'true':'false');});
 filter=b.dataset.f;renderHoldingsList();rememberList();writeRoute(sel,activeSeg(),true);_tmLog({type:'filter',f:filter});});
function _inTypingField(el){if(!el)return false;const tag=(el.tagName||'').toLowerCase();return tag==='input'||tag==='textarea'||el.isContentEditable;}
document.addEventListener('keydown',e=>{
 const a=document.activeElement;
 if(a&&a.id==='search'){if(e.key==='Escape'){a.value='';q='';renderHoldingsList();rememberList();writeRoute(sel,activeSeg(),true);}return;}
 if(a&&a.classList&&a.classList.contains('ib-lk')&&(e.key==='Enter'||e.key===' ')){e.preventDefault();a.click();return;}
 // Esc-to-back — but: (1) defer to PINNED tooltips (glossary), not hover tooltips. (2) When focused on a textarea/input, Esc just blurs (don't yank trader out of a half-written journal thesis).
 if(e.key==='Escape'){
   if(window.__ttPinned)return;
   if(_inTypingField(a)){a.blur();return;}
   if(sel!=='__OV__')goBack();
   return;
 }
 if((e.key==='ArrowLeft'||e.key==='ArrowRight')&&a&&a.parentElement&&a.parentElement.classList&&(a.parentElement.classList.contains('seg-rail')||a.parentElement.classList.contains('ws-rail')||a.parentElement.classList.contains('tabs'))){
   let sib=e.key==='ArrowRight'?a.nextElementSibling:a.previousElementSibling;
   while(sib&&(sib.tagName!=='BUTTON'||sib.hidden))sib=e.key==='ArrowRight'?sib.nextElementSibling:sib.previousElementSibling;   // skip .rail-here span + segs hidden by the inactive workspace
   if(sib){const _ws=sib.dataset&&sib.dataset.ws;sib.click();if(_ws){const _nb=document.querySelector('.ws-rail button[data-ws="'+_ws+'"]');if(_nb)_nb.focus();else sib.focus();}else sib.focus();e.preventDefault();}return;}
 // keyboard listbox on the holdings list: ArrowUp/Down/Home/End cycle row focus; Enter/Space already wired per-row
 if(a&&a.classList&&a.classList.contains('row')){
   if(e.key==='ArrowDown'||e.key==='ArrowUp'){const sib=e.key==='ArrowDown'?a.nextElementSibling:a.previousElementSibling;if(sib&&sib.classList&&sib.classList.contains('row')){sib.focus();e.preventDefault();}return;}
   if(e.key==='Home'){const f=document.querySelector('#list .row');if(f){f.focus();e.preventDefault();}return;}
   if(e.key==='End'){const all=document.querySelectorAll('#list .row');const l=all[all.length-1];if(l){l.focus();e.preventDefault();}return;}
 }
 // global power-user shortcuts when not typing: j/k = adjacent stock, / = focus search, ? = nothing (reserved)
 if(!_inTypingField(a)){
   if(e.key==='/'){const si=document.getElementById('search');if(si){si.focus();si.select&&si.select();e.preventDefault();}return;}
   if(e.key==='j'){adjacentStock(1);e.preventDefault();return;}
   if(e.key==='k'){adjacentStock(-1);e.preventDefault();return;}
   if(e.key==='g'&&sel!=='__OV__'){goBack();e.preventDefault();return;}
 }
});
function updateCtx(){var c=document.getElementById('ctx'),t=document.getElementById('ctxt');if(!c||!t)return;var on=document.querySelector('#right .seg-rail button.on');var instk=(typeof sel!=='undefined'&&sel!=='__OV__');var name=instk?sel:'组合总览';t.textContent=(instk?'← ':'↑ ')+name+(on?' · '+on.textContent.trim():'');c.setAttribute('aria-label',instk?'返回组合总览':'回到顶部');}
window.addEventListener('scroll',function(){var y=window.scrollY||0,c=document.getElementById('ctx'),t=document.getElementById('totop');if(c)c.hidden=y<300;if(t)t.hidden=y<600;},{passive:true});
(function(){var b=document.getElementById('totop');if(b)b.onclick=function(){window.scrollTo({top:0,behavior:PRM?'auto':'smooth'});};})();
function routeFromBrowser(){
 if(routeApplying)return;
 const st=history.state;
 window.__ptrakRestoringScroll=!!(st&&(st.scrollY||st.listScrollY));   // suppress renderList's scrollIntoView while we restore the saved offset
 applyRoute(parseRoute()||{sym:'__OV__',seg:lastSeg('ov')||DEFAULT_SEG.ov},{scroll:false,fromHistory:true});
 if(st){
   requestAnimationFrame(()=>requestAnimationFrame(()=>{   // two RAFs let the layout/optsec append settle before we restore the saved offset
     if(st.scrollY!=null)window.scrollTo(0,st.scrollY);
     if(st.listScrollY!=null){const l=document.getElementById('list');if(l)l.scrollTop=st.listScrollY;}
     window.__ptrakRestoringScroll=false;
   }));
 } else {window.__ptrakRestoringScroll=false;}
}
try{history.scrollRestoration='manual';}catch(e){}   // we manage scroll restoration ourselves; browser default would race our RAF restore
window.addEventListener('popstate',routeFromBrowser);   // pushState/replaceState already update the hash, so hashchange would fire routeFromBrowser a second time — single listener is enough
(function(){   // initial load: if user landed on a deep-linked stock URL, insert a synthetic Overview entry first so the FIRST browser-back goes home rather than out of the dashboard
 // cold-load restore: when URL has no ?-suffix, rehydrate filter/q/sortKey from the last session so reload doesn't reset filters
 if(!(window.location.hash||'').includes('?')){
   try{const st=JSON.parse(localStorage.getItem('ptrak.list.v1')||'{}');
     if(typeof st.q==='string')q=st.q;
     if(VALID_F.indexOf(st.f)>=0)filter=st.f;
     if(VALID_S.indexOf(st.s)>=0)sortKey=st.s;
     syncListUI();
   }catch(e){}
 }
 const _r=parseRoute();
 // synthetic-OV trick: only fire when (a) the URL is a stock route AND (b) browser has no prior state.ptrak (i.e. genuine first-visit, not a reload).
 // Without (b), every reload of a bookmarked stock URL grows browser history by +1 — iOS swipe-back becomes useless and the back-history menu fills with duplicates.
 const _hasState=!!(history.state&&history.state.ptrak);
 if(_r&&_r.sym!=='__OV__'&&!_hasState){
   writeRoute('__OV__',lastSeg('ov')||DEFAULT_SEG.ov,true);   // replace the entry the browser created on load with our canonical OV entry
   writeRoute(_r.sym,_r.seg,false);                            // then push the actual deep-linked stock entry on top
   applyRoute(_r,{scroll:false});
 } else {
   applyRoute(_r||{sym:'__OV__',seg:lastSeg('ov')||DEFAULT_SEG.ov},{scroll:false,replace:true});
 }
})();
function renderQbar(){
 try{
  const el=document.getElementById('qbar'),q=DATA.qqqTqqq;
  if(!el)return;
  const ser=(q&&q.available&&q.series)?q.series.slice(-60):[];
  if(ser.length<=5){el.innerHTML='';return;}   // no data, no band
  const n=ser.length;let rects='',r0=0;
  for(let i=1;i<=n;i++){if(i===n||ser[i].state!==ser[r0].state){rects+=`<rect x="${r0}" y="0" width="${i-r0}" height="6" fill="${qStateColor(ser[r0].state)}" fill-opacity="0.8"/>`;r0=i;}}
  el.innerHTML=`<svg viewBox="0 0 ${n} 6" preserveAspectRatio="none" aria-hidden="true">${rects}</svg><span class="qbar-now" aria-hidden="true"></span>`;
  // the instrument's permanent name: regime word + days-in-state, in the masthead lockup
  const nameEl=document.getElementById('qbar-name');
  if(nameEl){let run=1;for(let i=ser.length-1;i>0&&ser[i-1].state===ser[ser.length-1].state;i--)run++;
   nameEl.innerHTML=`<span style="color:${qStateColor(ser[ser.length-1].state)}">QQQ ${qStateLabel(ser[ser.length-1].state)}</span> <span style="color:var(--faint)">第${run}天</span>`;}
 }catch(e){}
}
renderQbar();
requestAnimationFrame(()=>document.body.classList.add('ready'));
setTimeout(()=>document.body.classList.add('done'),1500);
// Measure the REAL masthead height into --header-h (the CSS 84px/48px values are fallback guesses).
// Every sticky offset (.left / .seg-rail / .viewbar / .ctx) derives from it, so the sticky stack
// fuses to the actual header edge instead of leaving a content-leaking slit.
(function(){
 const h=document.querySelector('header');if(!h)return;
 const set=()=>document.documentElement.style.setProperty('--header-h',Math.round(h.getBoundingClientRect().height)+'px');
 if('ResizeObserver' in window)new ResizeObserver(set).observe(h);
 window.addEventListener('resize',set,{passive:true});
 set();
})();
// Track .viewbar height so the mobile sticky-stack (header → viewbar → seg-rail) doesn't overlap when the viewbar wraps to 2-3 rows.
(function(){
 if(!('ResizeObserver' in window))return;
 const setH=()=>{const vb=document.querySelector('.viewbar');if(vb)document.documentElement.style.setProperty('--viewbar-h',Math.round(vb.getBoundingClientRect().height)+'px');};
 const ob=new ResizeObserver(setH);
 const right=document.getElementById('right');if(!right)return;
 const rewire=()=>{const vb=document.querySelector('.viewbar');if(vb){ob.disconnect();ob.observe(vb);setH();}else{document.documentElement.style.setProperty('--viewbar-h','0px');}};
 new MutationObserver(rewire).observe(right,{childList:true});
 rewire();
})();
function renderOptSec(){   // append options-trading card (overview only) + universal data-window note. Direct call from renderOverview/renderDetail replaces the previous MutationObserver which fired on every seg toggle
 const r=document.getElementById('right');
 if(!r||document.getElementById('optsec'))return;
 const d=document.createElement('div');d.id='optsec';
 d.innerHTML=`<div class="card note ref"><b>计算口径说明</b><br>
 • 价格曲线为 Yahoo Finance 实际日收盘价；买卖点的 Y 轴为你的真实成交价。<br>
 • <b>未实现盈亏</b>采用券商最新 Portfolio CSV 实际数据（精确）。<br>
 • <b>已实现盈亏</b>用平均成本法在数据窗口内计算；标「含旧仓」的股票在数据起点前已有底仓，其成本按起点当日市场价估算，故为近似值。<br>
 • 数据窗口 ${D0} → ${D1}，可能并非完整开户记录。</div>`;
 r.appendChild(d);
 fixThScope(d);
}
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
    ap.add_argument("--mark-to-market", action="store_true",
                    help="revalue held stocks from the latest Yahoo price while keeping shares/cost from the Portfolio CSV")
    ap.add_argument("--as-of", help="target price date for --mark-to-market (YYYY-MM-DD; default: today)")
    args = ap.parse_args()

    portfolio = args.portfolio or newest("Portfolio_Positions*.csv", args.input_dir)
    if not portfolio:
        sys.exit("!! Could not find a Portfolio_Positions*.csv. Pass --portfolio explicitly.")

    # Gather every history-style export and parse them all. Different Fidelity
    # exports are complete in different ways: "History_for_Account" has the full
    # trade log; "Accounts_History" often has the complete cash/dividend record
    # (and may include deposits the other file misses). So pick the best source
    # for each purpose instead of trusting a single file. An explicit --history
    # is ADDED to the accumulated local set, not used as a replacement.
    hist_files = collect_history_files(args.input_dir, args.history)
    if not hist_files:
        sys.exit("!! Could not find any history CSV. Pass --history explicitly.")
    parsed = [parse_history(p) for p in hist_files]

    # Merge ALL exports (each contributes the dates it covers; dedup by max-count
    # per identical transaction). Then restrict the timeline to the largest
    # gap-free span ending at the latest trade — a >=20-day hole means an export
    # is simply missing those weeks, and reconstructing across it would be wrong.
    txns_all, opt_txns, names = merge_histories(parsed)
    dmin, gaps = continuous_start(txns_all)
    txn_dmax = max((t["date"] for L in txns_all.values() for t in L), default=None)
    if not dmin or not txn_dmax:
        sys.exit("!! Could not find any dated stock transactions in the history exports.")

    txns = {s: [t for t in L if t["date"] >= dmin] for s, L in txns_all.items()}
    txns = {s: L for s, L in txns.items() if L}
    opt_txns = {s: [t for t in L if t["date"] >= dmin] for s, L in opt_txns.items()}
    opt_txns = {s: L for s, L in opt_txns.items() if L}
    tot_buy = sum(-t["amount"] for L in txns.values() for t in L if t["side"] == "BUY")
    tot_sell = sum(t["amount"] for L in txns.values() for t in L if t["side"] == "SELL")
    totals = (tot_buy, tot_sell)

    n_trades = sum(len(L) for L in txns.values())
    cur = parse_portfolio(portfolio)
    acct_extras = parse_account_extras(portfolio)
    strategy_tickers = {"QQQ", "TQQQ"}
    tickers = sorted(set(list(txns) + list(cur)))

    target_asof = args.as_of or datetime.date.today().isoformat()
    try:
        datetime.date.fromisoformat(target_asof)
    except ValueError:
        sys.exit("!! --as-of must be YYYY-MM-DD")
    fetch_dmax = target_asof if args.mark_to_market else txn_dmax

    # fetch window: a few days before first trade through day after last needed
    price_start = datetime.date.fromisoformat(dmin) - datetime.timedelta(days=4)
    # QQQ/TQQQ strategy needs enough daily bars for EMA55/89 + ATR context even
    # when the latest activity export covers only a short window.
    strategy_start = datetime.date.fromisoformat(fetch_dmax) - datetime.timedelta(days=260)
    start = min(price_start, strategy_start).isoformat()
    end = (datetime.date.fromisoformat(fetch_dmax) + datetime.timedelta(days=1)).isoformat()
    BENCH = ["^GSPC", "^IXIC"]   # S&P 500, NASDAQ Composite
    prices = fetch_prices(sorted(set(tickers) | set(BENCH) | strategy_tickers), start, end, no_fetch=args.no_fetch)
    ohlc = fetch_ohlc(sorted(strategy_tickers), start, end, no_fetch=args.no_fetch)

    dmax = txn_dmax
    refreshed = {}
    price_mode = "broker"
    price_as_of = txn_dmax
    if args.mark_to_market:
        price_dmax = latest_price_date(prices, sorted(set(tickers) | set(BENCH) | strategy_tickers), target_asof)
        if not price_dmax:
            sys.exit("!! --mark-to-market could not find any downloaded Yahoo prices.")
        dmax = max(txn_dmax, price_dmax)
        price_as_of = price_dmax
        price_mode = "mark-to-market"
        refreshed = mark_to_market(cur, prices, price_dmax)

    deposits, dividends = union_cash(parsed, dmin, dmax)
    life_dep, _ = union_cash(parsed, "0000-00", "9999-99")

    print(f"· portfolio:  {portfolio}")
    print(f"· merged {len(parsed)} exports → {n_trades} trades, window {dmin}→{dmax}")
    if args.mark_to_market:
        print(f"· mark-to-market: refreshed {len(refreshed)}/{len(cur)} held/equity symbols with Yahoo prices as of {price_as_of}")
    if gaps:
        print(f"  (excluded earlier data before {dmin}: coverage gap "
              + ", ".join(f"{a}→{b}" for a, b in gaps) + ")")
    print(f"· deposits {dmin[:7]}→{dmax[:7]}: ${deposits:,.0f}  ·  lifetime deposits ${life_dep:,.0f}  ·  dividends ${dividends:,.2f}")

    sectors = fetch_sectors(tickers, no_fetch=args.no_fetch)

    payload = build_payload(txns, opt_txns, names, cur, prices, deposits, totals,
                            dmin, dmax, dividends, life_dep, acct_extras, sectors,
                            ohlc, price_mode=price_mode, price_as_of=price_as_of,
                            refreshed_prices=refreshed)
    payload["decision"] = load_decision_analysis(os.path.dirname(args.out))
    payload["aiSemiQuant"] = load_ai_semi_quant(os.path.dirname(args.out))
    payload["aiWatchlist"] = load_ai_watchlist(os.path.dirname(args.out))
    payload["aics"] = load_aics_payload(os.path.dirname(args.out))
    payload["marketMass"] = load_market_mass_dashboard(os.path.dirname(args.out))
    payload["financialStatus"] = load_financial_status(os.path.dirname(args.out))
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    open(args.out, "w").write(render_html(payload))
    s = payload["summary"]
    print(f"\n✓ dashboard written: {args.out}")
    print(f"  window {s['dateRange'][0]}→{s['dateRange'][1]} · {s['numStocks']} stocks ({s['numHeld']} held) · {len(payload['options'])} options")
    print(f"  market value {s['marketValue']:,.0f} · unrealized {s['unrealized']:+,.0f} · realized(window) {s['realized']:+,.0f}")

if __name__ == "__main__":
    main()

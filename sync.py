#!/usr/bin/env python3
"""
sync.py — one-command portfolio sync, with a built-in correctness gate.
=======================================================================
Run this whenever you (or Claude) drop fresh Fidelity exports in ~/Downloads:
a new ``Portfolio_Positions_*.csv`` snapshot and the latest
``Accounts_History*.csv`` / ``History_for_Account*.csv``.

It does three things:

  1. **Regenerate** the dashboard by delegating to ``generate.py`` — the single
     source of truth (it auto-detects the newest portfolio, merges EVERY
     overlapping history export, fetches prices, writes the HTML).

  2. **Verify** — independently re-derive held-equity *market value*,
     *unrealized P&L* and *held count* straight from the newest Portfolio CSV
     and assert the freshly written dashboard agrees to the cent. This is the
     safety net: it catches a whole account or holding being silently dropped
     (exactly the Rollover-IRA bug — a numeric account number slipping past a
     ``Z``-only row filter) or a money-market position being miscounted as
     equity. If the numbers disagree the sync FAILS loudly.

  3. **Log** — append a one-line snapshot (value, P&L, return, deposits) to
     ``output/sync_log.json`` and print the delta versus the previous sync, so
     there is a longitudinal record across syncs.

Usage::

    python3 sync.py                       # full sync (fetch fresh prices)
    python3 sync.py --no-fetch            # reuse cached prices (offline)
    python3 sync.py --open                # open the dashboard when done
    python3 sync.py --portfolio P.csv --history H.csv   # explicit files

Any unrecognised flags are passed straight through to ``generate.py``.
See SKILLS.md → "Syncing new exports" for the full methodology and gotchas.
"""
import argparse, csv, datetime, glob, json, os, re, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.expanduser("~")
OUT = os.path.join(HERE, "output", "portfolio_dashboard.html")
LOG = os.path.join(HERE, "output", "sync_log.json")

# Dollar tolerance for the verification gate. Both sides read the identical
# numbers out of the same CSV, so the real difference is ~0; $1 absorbs any
# rounding without ever masking a dropped position (the smallest real lot here
# is worth ~$10, a dropped account is worth hundreds+).
TOL = 1.0


def fnum(s):
    s = str(s).strip().replace(",", "").replace("$", "").replace("+", "")
    if s in ("", "--"):
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def newest(pattern, where):
    hits = sorted(glob.glob(os.path.join(where, pattern)), key=os.path.getmtime, reverse=True)
    return hits[0] if hits else None


def independent_totals(path):
    """Re-derive held-equity totals from the Portfolio CSV, INDEPENDENT of
    generate.parse_portfolio, so a regression in that parser can't hide here.

    Equity row = col 0 is an account id (alphanumeric: brokerage ``Z...`` *and*
    numeric retirement accounts), and the symbol is a real equity — not a
    money-market core position (Fidelity suffixes those with ``**``: SPAXX**,
    FDRXX**), not an option (symbol starts ``-``), not ``Pending activity`` or
    blank.
    """
    val = gain = 0.0
    syms, accounts = set(), set()
    with open(path) as f:
        for r in csv.reader(f):
            if len(r) < 14 or not re.match(r"^[A-Z0-9]{5,}$", r[0].strip()):
                continue
            accounts.add(r[0].strip())
            sym = r[2].strip()
            if sym.endswith("**") or sym.startswith("-") or sym in ("Pending activity", ""):
                continue
            val += fnum(r[7])
            gain += fnum(r[10])
            syms.add(sym)
    return {"marketValue": round(val, 2), "unrealized": round(gain, 2),
            "numHeld": len(syms), "accounts": sorted(accounts)}


def dashboard_summary(html_path):
    """Pull the injected payload's ``summary`` block out of the generated HTML."""
    html = open(html_path).read()
    m = re.search(r"const DATA = (\{.*?\});", html, re.S)
    if not m:
        sys.exit(f"!! could not find the data payload in {html_path}")
    return json.loads(m.group(1))["summary"]


def pct(x):
    return f"{x:+.2f}%" if isinstance(x, (int, float)) else "n/a"


def main():
    ap = argparse.ArgumentParser(
        description="Sync the portfolio dashboard from the newest Fidelity exports, verify it against the broker CSV, and log the snapshot.")
    ap.add_argument("--input-dir", default=os.path.join(HOME, "Downloads"),
                    help="where to auto-detect the CSVs (default: ~/Downloads)")
    ap.add_argument("--portfolio", help="explicit Portfolio Positions CSV")
    ap.add_argument("--no-fetch", action="store_true", help="reuse cached prices (offline)")
    ap.add_argument("--open", action="store_true", help="open the dashboard in a browser when done")
    args, passthrough = ap.parse_known_args()

    portfolio = args.portfolio or newest("Portfolio_Positions*.csv", args.input_dir)
    if not portfolio:
        sys.exit(f"!! No Portfolio_Positions*.csv found in {args.input_dir}. Pass --portfolio.")

    # 1) regenerate via the single source of truth -----------------------------
    cmd = [sys.executable, os.path.join(HERE, "generate.py"),
           "--input-dir", args.input_dir, "--portfolio", portfolio]
    if args.no_fetch:
        cmd.append("--no-fetch")
    cmd += passthrough
    print("▶ regenerating:", " ".join(os.path.basename(c) if c.endswith(".py") else c for c in cmd), flush=True)
    if subprocess.run(cmd).returncode != 0:
        sys.exit("!! generate.py failed — dashboard NOT updated.")

    # 2) independent verification gate -----------------------------------------
    exp = independent_totals(portfolio)
    got = dashboard_summary(OUT)
    print("\n— verification: independent CSV sum vs. written dashboard —")
    print(f"  accounts in snapshot: {', '.join(exp['accounts'])}")
    hard_ok = True
    for k in ("marketValue", "unrealized"):
        a, b = exp[k], got[k]
        ok = abs(a - b) <= TOL
        hard_ok = hard_ok and ok
        print(f"  {k:12} csv=${a:>13,.2f}   dashboard=${b:>13,.2f}   {'OK' if ok else '!! MISMATCH'}")
    held_ok = exp["numHeld"] == got["numHeld"]
    flag = "OK" if held_ok else "!! differs (check for dust lots / dropped names)"
    print(f"  {'numHeld':12} csv={exp['numHeld']:>14}   dashboard={got['numHeld']:>14}   {flag}")
    if not hard_ok:
        sys.exit("\n!! VERIFICATION FAILED: the dashboard does not match the Portfolio CSV.\n"
                 "   A holding or an entire account may have been dropped. Do NOT trust this\n"
                 "   dashboard until generate.parse_portfolio is fixed. See SKILLS.md.")
    print("  ✓ dashboard market value & unrealized P&L match the broker CSV exactly")

    # 3) append to the sync log + show the delta -------------------------------
    log = []
    if os.path.exists(LOG):
        try:
            log = json.load(open(LOG))
        except Exception:
            log = []
    prev = log[-1] if log else None
    snap = {
        "syncedAt": datetime.datetime.now().isoformat(timespec="seconds"),
        "portfolioFile": os.path.basename(portfolio),
        "dateRange": got["dateRange"],
        "numHeld": got["numHeld"],
        "marketValue": got["marketValue"],
        "unrealized": got["unrealized"],
        "realizedWindow": got["realized"],
        "curReturn": got["curReturn"],
        "spReturn": got["spReturn"],
        "nasdaqReturn": got["nasdaqReturn"],
        "depositsWindow": got["deposits"],
        "lifeDeposits": got["lifeDeposits"],
        "dividends": got["dividends"],
        "netWorthNow": got["netWorthNow"],
    }
    log.append(snap)
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    json.dump(log, open(LOG, "w"), indent=2)

    alpha = (got["curReturn"] - got["spReturn"]) if isinstance(got["spReturn"], (int, float)) else None
    print("\n— snapshot logged → output/sync_log.json —")
    print(f"  window {snap['dateRange'][0]} → {snap['dateRange'][1]}  ·  {snap['numHeld']} held")
    print(f"  market value ${snap['marketValue']:,.0f}  ·  unrealized ${snap['unrealized']:+,.0f}  ·  realized(window) ${snap['realizedWindow']:+,.0f}")
    print(f"  TWR {pct(snap['curReturn'])}  vs S&P {pct(snap['spReturn'])}"
          + (f"  (alpha {pct(alpha)})" if alpha is not None else "")
          + f"  vs NASDAQ {pct(snap['nasdaqReturn'])}")
    print(f"  deposits(window) ${snap['depositsWindow']:,.0f}  ·  lifetime ${snap['lifeDeposits']:,.0f}  ·  dividends ${snap['dividends']:,.2f}")
    if prev:
        d_mv = snap["marketValue"] - prev["marketValue"]
        d_held = snap["numHeld"] - prev["numHeld"]
        print(f"\n— change since last sync ({prev['syncedAt'][:10]}, {prev['portfolioFile']}) —")
        print(f"  market value {d_mv:+,.0f}  ·  held {d_held:+d}  ·  net-worth curve {snap['netWorthNow'] - prev['netWorthNow']:+,.0f}")

    if args.open:
        subprocess.run(["open", OUT])
    print("\n✓ sync complete → output/portfolio_dashboard.html")


if __name__ == "__main__":
    main()

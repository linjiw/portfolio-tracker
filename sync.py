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
     *unrealized P&L*, cash, pending activity, option net/gross marks, option
     leg count, and whole-account net worth straight from the newest Portfolio
     CSV and assert the freshly written dashboard agrees. This is the
     safety net: it catches a whole account or holding being silently dropped
     (for example, a numeric account id slipping past an overly narrow row
     filter) or a money-market position being miscounted as equity. If the
     numbers disagree the sync FAILS loudly.

  3. **Refresh Momentum Top-3** — update the strategy artifact from latest
     Yahoo index-constituent prices before the dashboard embeds it.

  4. **Refresh financial lens** — update the company financial-status artifact
     using the configured source cascade (default: FMP + Yahoo/yfinance + SEC
     when SEC_USER_AGENT is configured) and re-embed it in the HTML. This step
     is skipped in ``--no-fetch`` mode unless explicitly requested.

  5. **Log** — append a one-line snapshot (value, P&L, return, deposits) to
     ``output/sync_log.json`` and print the delta versus the previous sync, so
     there is a longitudinal record across syncs.

Usage::

    python3 sync.py                       # full sync (fetch fresh prices)
    python3 sync.py --no-fetch            # reuse cached prices (offline)
    python3 sync.py --financial-status on # force financial lens refresh / cache-only in --no-fetch
    python3 sync.py --open                # open the dashboard when done
    python3 sync.py --portfolio P.csv --history H.csv   # explicit files

Any unrecognised flags are passed straight through to ``generate.py``.
See docs/METHODOLOGY.md for the full methodology and data contract.
"""
import argparse, atexit, csv, datetime, glob, hashlib, json, math, os, re, shutil, subprocess, sys
from zoneinfo import ZoneInfo

from scripts.artifact_io import atomic_write_json
from scripts.dashboard_payload import read_dashboard_payload as _read_dashboard_payload

HERE = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.expanduser("~")
OUT = os.path.join(HERE, "output", "portfolio_dashboard.html")
LOG = os.path.join(HERE, "output", "sync_log.json")
FMP_CONFIG = os.path.join(HOME, ".config", "ptrak", "fmp.json")
IMPORT_ROOT = os.path.join(HERE, "output", "imports", "raw")
IMPORT_MANIFEST = os.path.join(HERE, "output", "imports", "manifest.json")

# Dollar tolerance for the verification gate. Both sides read the identical
# broker snapshot, so only cent rounding is expected; two cents allows sums of
# individually rounded lots without masking a real account discrepancy.
TOL = 0.02
BROKER_TZ = ZoneInfo("America/New_York")
CASH_CORE_SYMBOLS = {"SPAXX", "FDRXX", "FCASH", "SPRXX", "FZFXX"}


def fnum(s):
    s = str(s).strip().replace(",", "").replace("$", "").replace("+", "")
    if s in ("", "--"):
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def _strict_number(value, *, field, row_number, allow_missing=False):
    """Parse a broker numeric field without turning corruption into a zero.

    The dashboard parser and this verification parser intentionally do not
    share code.  This side is strict: blank/``--`` is accepted only where the
    broker schema documents it as optional, while malformed or non-finite
    values stop publication of the new dashboard.
    """
    text = str(value or "").strip().replace(",", "").replace("$", "").replace("+", "")
    if text.startswith("(") and text.endswith(")"):
        text = "-" + text[1:-1]
    if text in ("", "--"):
        if allow_missing:
            return 0.0
        raise ValueError(f"row {row_number}: required numeric field {field!r} is blank")
    try:
        number = float(text)
    except ValueError as exc:
        raise ValueError(f"row {row_number}: invalid numeric field {field!r}: {value!r}") from exc
    if not math.isfinite(number):
        raise ValueError(f"row {row_number}: non-finite numeric field {field!r}: {value!r}")
    return number


def newest(pattern, where):
    hits = sorted(glob.glob(os.path.join(where, pattern)), key=os.path.getmtime, reverse=True)
    return hits[0] if hits else None


def broker_download_timestamp(path):
    """Read Fidelity's in-file ``Date downloaded`` timestamp when available."""
    try:
        with open(path, encoding="utf-8-sig", newline="") as handle:
            for row in csv.reader(handle):
                text = row[0].strip() if row else ""
                match = re.search(
                    r"Date downloaded\s+([A-Z][a-z]{2}-\d{2}-\d{4})(?:\s+(\d{1,2}:\d{2})\s*([ap])\.?m\.?)?",
                    text, re.I,
                )
                if not match:
                    continue
                day = datetime.datetime.strptime(match.group(1).title(), "%b-%d-%Y").date()
                if match.group(2):
                    hour_minute = datetime.datetime.strptime(
                        f"{match.group(2)} {match.group(3).upper()}M", "%I:%M %p").time()
                else:
                    # Date-only exports are treated as end-of-day to avoid false
                    # expiry while still detecting a copied weeks-old snapshot.
                    hour_minute = datetime.time(23, 59, 59)
                return datetime.datetime.combine(day, hour_minute, tzinfo=BROKER_TZ)
    except (OSError, UnicodeError, ValueError):
        return None
    return None


def validate_export_freshness(path, *, max_age_hours=120.0, allow_old=False, now=None):
    """Reject a future-dated or unexpectedly old position snapshot.

    Fidelity's embedded ``Date downloaded`` is authoritative when present;
    filesystem mtime is an explicitly labeled fallback. Five days covers normal
    weekends/market holidays while preventing an unattended job from silently
    treating a copied weeks-old broker file as current.
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=datetime.timezone.utc)
    embedded = broker_download_timestamp(path)
    observed = embedded or datetime.datetime.fromtimestamp(os.path.getmtime(path), datetime.timezone.utc)
    age_hours = (now.astimezone(datetime.timezone.utc)
                 - observed.astimezone(datetime.timezone.utc)).total_seconds() / 3600
    if age_hours < -5 / 60:
        raise ValueError("portfolio export has a future filesystem timestamp")
    age_hours = max(0.0, age_hours)
    if age_hours > max_age_hours and not allow_old:
        raise ValueError(
            f"portfolio export is {age_hours:.1f} hours old (limit {max_age_hours:.1f}); "
            "export a current snapshot or pass --allow-old-export explicitly"
        )
    return {
        "snapshotAt": observed.isoformat(timespec="seconds"),
        "timestampSource": "broker-export" if embedded else "filesystem-mtime-fallback",
        "ageHours": round(age_hours, 2),
        "maxAgeHours": max_age_hours,
        "oldOverride": bool(allow_old and age_hours > max_age_hours),
    }


def _atomic_json(path, payload):
    atomic_write_json(path, payload)


def archive_sources(paths):
    """Durably archive private broker exports by content hash (output is gitignored)."""
    try:
        manifest = json.load(open(IMPORT_MANIFEST)) if os.path.exists(IMPORT_MANIFEST) else {"imports": []}
    except (OSError, ValueError):
        raise RuntimeError(f"invalid import manifest: {IMPORT_MANIFEST}")
    records = {row.get("sha256"): row for row in manifest.get("imports", []) if row.get("sha256")}
    imported_at = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    for source in paths:
        if not source or not os.path.isfile(source):
            continue
        with open(source, "rb") as handle:
            digest = hashlib.sha256(handle.read()).hexdigest()
        dest_dir = os.path.join(IMPORT_ROOT, digest[:16])
        dest = os.path.join(dest_dir, os.path.basename(source))
        os.makedirs(dest_dir, mode=0o700, exist_ok=True)
        os.chmod(dest_dir, 0o700)
        if not os.path.exists(dest):
            shutil.copy2(source, dest)
            os.chmod(dest, 0o600)
        record = records.get(digest, {})
        record.update({
            "sha256": digest,
            "originalBasename": os.path.basename(source),
            "archivePath": os.path.relpath(dest, HERE),
            "bytes": os.path.getsize(source),
            "kind": "positions" if os.path.basename(source).startswith("Portfolio_Positions") else "history",
        })
        record.setdefault("firstImportedAt", imported_at)
        record["lastSeenAt"] = imported_at
        records[digest] = record
    doc = {"updatedAt": imported_at, "imports": sorted(records.values(), key=lambda row: row["firstImportedAt"])}
    _atomic_json(IMPORT_MANIFEST, doc)
    return doc


def independent_totals(path):
    """Re-derive held-equity totals from the Portfolio CSV, INDEPENDENT of
    generate.parse_portfolio, so a regression in that parser can't hide here.

    This implementation is deliberately header-driven and strict, unlike the
    generator's parser.  A broker column reorder, renamed/missing required
    field, invalid number, or empty data section fails the gate instead of
    allowing both sides to agree on silently dropped zeroes.
    """
    val = gain = cost_total = cash = pending = opt_net = opt_gross = opt_pnl = opt_entry_cash = 0.0
    opt_leg_count = 0
    syms, accounts, shares_by_symbol = set(), set(), {}
    recognized_rows = 0
    required = {
        "account number", "symbol", "quantity", "current value",
        "total gain/loss dollar", "cost basis total",
    }
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = csv.reader(f)
        header = None
        for row_number, raw in enumerate(rows, 1):
            normalized = [str(cell).strip().lower() for cell in raw]
            if "account number" in normalized and "symbol" in normalized and "current value" in normalized:
                header = raw
                break
        if header is None:
            raise ValueError("portfolio CSV header not found")
        normalized_header = [str(cell).strip().lower() for cell in header]
        missing = sorted(required - set(normalized_header))
        if missing:
            raise ValueError("portfolio CSV missing required columns: " + ", ".join(missing))
        if len(set(normalized_header)) != len(normalized_header):
            raise ValueError("portfolio CSV contains duplicate column names")
        index = {name: i for i, name in enumerate(normalized_header)}

        def cell(row, name):
            i = index[name]
            return row[i] if i < len(row) else ""

        for row_number, row in enumerate(rows, row_number + 1):
            account = cell(row, "account number").strip()
            sym = cell(row, "symbol").strip()
            if not account or not sym:
                continue
            accounts.add(account)
            recognized_rows += 1
            pending_row = sym.lower() == "pending activity"
            v = _strict_number(cell(row, "current value"), field="Current Value",
                               row_number=row_number, allow_missing=pending_row)
            if sym.endswith("**") or sym.upper().rstrip("*") in CASH_CORE_SYMBOLS:
                cash += v
                continue
            option_core = sym.replace(" ", "").lstrip("-")
            is_option = bool(re.fullmatch(
                r"[A-Z0-9.]{1,10}\d{6}[CP]\d+(?:\.\d+)?", option_core, re.I))
            if is_option:
                opt_net += v
                opt_gross += abs(v)
                qty = _strict_number(cell(row, "quantity"), field="Quantity",
                                     row_number=row_number)
                cost_basis = _strict_number(cell(row, "cost basis total"),
                                            field="Cost Basis Total", row_number=row_number,
                                            allow_missing=True)
                opt_pnl += _strict_number(cell(row, "total gain/loss dollar"),
                                          field="Total Gain/Loss Dollar", row_number=row_number,
                                          allow_missing=True)
                opt_entry_cash += cost_basis if qty < 0 else -cost_basis
                opt_leg_count += 1
                continue
            if pending_row:
                pending += v
                continue
            g_raw = cell(row, "total gain/loss dollar").strip()
            cost = _strict_number(cell(row, "cost basis total"), field="Cost Basis Total",
                                  row_number=row_number)
            shares = _strict_number(cell(row, "quantity"), field="Quantity",
                                    row_number=row_number)
            val += v
            cost_total += cost
            gain += (_strict_number(g_raw, field="Total Gain/Loss Dollar", row_number=row_number)
                     if g_raw not in ("", "--") else round(v - cost, 2))
            syms.add(sym)
            shares_by_symbol[sym] = shares_by_symbol.get(sym, 0.0) + shares
    if recognized_rows == 0:
        raise ValueError("portfolio CSV contains no recognized account rows")
    whole = val + cash + opt_net + pending
    return {"marketValue": round(val, 2), "unrealized": round(gain, 2),
            "numHeld": len(syms), "accounts": sorted(accounts),
            "cashTotal": round(cash, 2), "pendingTotal": round(pending, 2),
            "optMarkNet": round(opt_net, 2), "optMarkGross": round(opt_gross, 2),
            "optBrokerPnl": round(opt_pnl, 2), "optEntryCashNet": round(opt_entry_cash, 2),
            "optLegCount": opt_leg_count, "accountNetWorth": round(whole, 2),
            "equityCostBasis": round(cost_total, 2),
            "sharesBySymbol": {sym: round(qty, 8) for sym, qty in sorted(shares_by_symbol.items())},
            "recognizedRowCount": recognized_rows}


def _staging_path(target):
    target = os.path.abspath(target)
    return f"{target}.staging.{os.getpid()}"


def _remove_if_present(path):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def publish_verified_dashboard(staged, target):
    """Atomically promote a fully generated and reconciled dashboard."""
    parent = os.path.dirname(os.path.abspath(target))
    existed = os.path.isdir(parent)
    os.makedirs(parent, mode=0o700, exist_ok=True)
    runtime_root = os.path.abspath(os.path.join(HERE, "output"))
    try:
        in_runtime_root = os.path.commonpath((parent, runtime_root)) == runtime_root
    except ValueError:
        in_runtime_root = False
    # Runtime output is always private. Preserve permissions on an arbitrary
    # caller-owned existing directory such as /tmp or an external mount.
    if not existed or in_runtime_root:
        os.chmod(parent, 0o700)
    os.chmod(staged, 0o600)
    os.replace(staged, target)
    os.chmod(target, 0o600)


def dashboard_payload(html_path):
    """Pull the injected payload out of the generated HTML."""
    try:
        return _read_dashboard_payload(html_path)
    except (OSError, ValueError) as exc:
        sys.exit(f"!! could not find the data payload in {html_path}: {exc}")


def dashboard_summary(html_path):
    return dashboard_payload(html_path)["summary"]


def fmp_key_available():
    return bool(os.environ.get("FMP_API_KEY") or os.path.exists(FMP_CONFIG))


def financial_sources_available(sources):
    source_set = {s.strip().lower() for s in sources.split(",") if s.strip()}
    return bool(source_set & {"yahoo", "sec"} or ("fmp" in source_set and fmp_key_available()))


def financial_summary():
    path = os.path.join(HERE, "output", "financial_status.json")
    if not os.path.exists(path):
        return None
    try:
        doc = json.load(open(path))
        counts = doc.get("counts", {})
        summary = doc.get("summary", {})
        return {
            "scored": counts.get("scored"),
            "omitted": counts.get("omitted"),
            "dataReview": counts.get("dataReview"),
            "leaders": [x.get("ticker") for x in (summary.get("leaders") or [])[:4]],
            "avg": summary.get("avgFinalScore"),
        }
    except Exception:
        return None


def momentum_summary():
    path = os.path.join(HERE, "output", "momentum_top3", "momentum_top3.json")
    if not os.path.exists(path):
        return None
    try:
        doc = json.load(open(path))
        return {
            "generatedAt": doc.get("generated_at"),
            "end": (doc.get("window") or {}).get("end"),
            "signals": [
                (s.get("id"), [h.get("ticker") for h in ((s.get("current_signal") or {}).get("holdings") or [])])
                for s in (doc.get("strategies") or [])[:3]
            ],
        }
    except Exception:
        return None


def refresh_momentum_strategy(mode, no_fetch):
    if mode == "off":
        return False
    cmd = [sys.executable, os.path.join(HERE, "scripts", "momentum_top3.py")]
    if no_fetch:
        cmd.append("--no-fetch")
    print("\n▶ refreshing Momentum Top-3 strategy:", " ".join(os.path.basename(c) if c.endswith(".py") else c for c in cmd), flush=True)
    rc = subprocess.run(cmd).returncode
    if rc != 0:
        if mode == "on":
            sys.exit("!! momentum_top3.py failed — dashboard NOT updated.")
        print("  (momentum strategy refresh failed; keeping previous artifact)")
        return False
    ms = momentum_summary()
    if ms:
        flagship = ms["signals"][0][1] if ms["signals"] else []
        print(f"  momentum strategy: data through {ms['end']} · flagship {', '.join(flagship)}")
    return True


def pct(x):
    return f"{x:+.2f}%" if isinstance(x, (int, float)) else "n/a"


def main():
    os.umask(0o077)
    ap = argparse.ArgumentParser(
        description="Sync the portfolio dashboard from the newest Fidelity exports, verify it against the broker CSV, and log the snapshot.")
    ap.add_argument("--input-dir", default=os.path.join(HOME, "Downloads"),
                    help="where to auto-detect the CSVs (default: ~/Downloads)")
    ap.add_argument("--portfolio", help="explicit Portfolio Positions CSV")
    ap.add_argument("--history", help="explicit Account History CSV (added to cumulative history by default)")
    ap.add_argument("--history-mode", choices=("cumulative", "exact"), default="cumulative",
                    help="merge overlapping local histories (default) or use only --history")
    ap.add_argument("--out", default=OUT,
                    help="dashboard output path (a caller may use a staging path and publish it after verification)")
    ap.add_argument("--no-fetch", action="store_true", help="reuse cached prices (offline)")
    ap.add_argument("--max-export-age-hours", type=float, default=120.0,
                    help="maximum age of auto/explicit position snapshot (default: 120 hours)")
    ap.add_argument("--allow-old-export", action="store_true",
                    help="explicitly allow a position snapshot older than --max-export-age-hours")
    ap.add_argument("--financial-status", choices=("auto", "on", "off"), default="auto",
                    help="refresh/embed the multi-source financial-status lens (default: auto when a financial source is configured and --no-fetch is not set)")
    ap.add_argument("--financial-status-refresh", action="store_true",
                    help="force-refresh cached financial-status source responses")
    ap.add_argument("--financial-sources", default="fmp,yahoo,sec",
                    help="comma-separated financial lens sources passed to scripts/financial_status_score.py")
    ap.add_argument("--sec-user-agent", default=None,
                    help="SEC EDGAR User-Agent string passed to the financial lens")
    ap.add_argument("--momentum-strategy", choices=("auto", "on", "off"), default="auto",
                    help="refresh/embed the Momentum Top-3 strategy artifact before rendering (default: auto)")
    ap.add_argument("--open", action="store_true", help="open the dashboard in a browser when done")
    args, passthrough = ap.parse_known_args()
    target_out = os.path.abspath(args.out)
    staged_out = _staging_path(target_out)
    _remove_if_present(staged_out)
    atexit.register(_remove_if_present, staged_out)

    portfolio = args.portfolio or newest("Portfolio_Positions*.csv", args.input_dir)
    if not portfolio:
        sys.exit(f"!! No Portfolio_Positions*.csv found in {args.input_dir}. Pass --portfolio.")
    if args.max_export_age_hours <= 0:
        sys.exit("!! --max-export-age-hours must be positive")
    try:
        portfolio_freshness = validate_export_freshness(
            portfolio, max_age_hours=args.max_export_age_hours, allow_old=args.allow_old_export)
    except ValueError as exc:
        sys.exit(f"!! {exc}")

    if args.history_mode == "exact":
        history_sources = [args.history] if args.history else []
    else:
        history_sources = sorted(set(
            glob.glob(os.path.join(args.input_dir, "Accounts_History*.csv"))
            + glob.glob(os.path.join(args.input_dir, "History_for_Account*.csv"))
            + ([args.history] if args.history else [])))
    if args.history_mode == "exact" and not history_sources:
        sys.exit("!! --history-mode exact requires --history")
    import_manifest = archive_sources([portfolio] + history_sources)
    current_hashes = {row["originalBasename"]: row["sha256"] for row in import_manifest["imports"]
                      if row.get("originalBasename") in {os.path.basename(p) for p in [portfolio] + history_sources if p}}

    refresh_momentum_strategy(args.momentum_strategy, args.no_fetch)

    # 1) regenerate via the single source of truth -----------------------------
    cmd = [sys.executable, os.path.join(HERE, "generate.py"),
           "--input-dir", args.input_dir, "--portfolio", portfolio,
           "--history-mode", args.history_mode, "--out", staged_out]
    if args.history:
        cmd += ["--history", args.history]
    if args.no_fetch:
        cmd.append("--no-fetch")
    cmd += passthrough
    print("▶ regenerating:", " ".join(os.path.basename(c) if c.endswith(".py") else c for c in cmd), flush=True)
    if subprocess.run(cmd).returncode != 0:
        sys.exit("!! generate.py failed — dashboard NOT updated.")

    # 1b) optional FMP company financial-status lens ---------------------------
    fin_requested = args.financial_status == "on"
    fin_auto = args.financial_status == "auto" and (not args.no_fetch) and financial_sources_available(args.financial_sources)
    fin_ran = False
    if fin_requested or fin_auto:
        fcmd = [sys.executable, os.path.join(HERE, "scripts", "financial_status_score.py"),
                "--dashboard", staged_out, "--sources", args.financial_sources]
        if args.sec_user_agent:
            fcmd += ["--sec-user-agent", args.sec_user_agent]
        if args.no_fetch:
            fcmd.append("--no-fetch")
        if args.financial_status_refresh:
            fcmd.append("--refresh-cache")
        print("\n▶ refreshing FMP financial-status lens:", " ".join(os.path.basename(c) if c.endswith(".py") else c for c in fcmd), flush=True)
        rc = subprocess.run(fcmd).returncode
        if rc != 0:
            if fin_requested:
                sys.exit("!! financial_status_score.py failed — dashboard NOT updated.")
            print("  (financial-status refresh failed; keeping previous artifact)")
        else:
            fin_ran = True
            # Re-embed output/financial_status.json without moving market prices.
            recmd = [sys.executable, os.path.join(HERE, "generate.py"),
                     "--input-dir", args.input_dir, "--portfolio", portfolio,
                     "--history-mode", args.history_mode, "--out", staged_out, "--no-fetch"]
            if args.history:
                recmd += ["--history", args.history]
            recmd += [x for x in passthrough if x != "--no-fetch"]
            print("▶ embedding financial-status lens:", " ".join(os.path.basename(c) if c.endswith(".py") else c for c in recmd), flush=True)
            if subprocess.run(recmd).returncode != 0:
                sys.exit("!! generate.py failed while embedding financial status — dashboard NOT updated.")
            fs = financial_summary()
            if fs:
                print(f"  financial lens: {fs['scored']} scored · {fs['omitted']} omitted · "
                      f"{fs['dataReview']} data-review · avg {fs['avg']} · leaders {', '.join(fs['leaders'])}")
    elif args.financial_status == "auto":
        why = "--no-fetch is set" if args.no_fetch else "no financial source configured"
        print(f"\n▶ financial-status lens: skipped ({why}); existing artifact remains embedded if present")

    # 2) independent verification gate -----------------------------------------
    exp = independent_totals(portfolio)
    written = dashboard_payload(staged_out)
    got = written["summary"]
    print("\n— verification: independent CSV sum vs. written dashboard —")
    print(f"  accounts in snapshot: {len(exp['accounts'])} (identifiers redacted)")
    print(f"  recognized broker rows: {exp['recognizedRowCount']}")
    hard_ok = True
    for k in ("marketValue", "unrealized", "cashTotal", "pendingTotal",
              "optMarkNet", "optMarkGross", "optBrokerPnl", "optEntryCashNet",
              "accountNetWorth"):
        a, b = exp[k], got[k]
        ok = abs(a - b) <= TOL
        hard_ok = hard_ok and ok
        print(f"  {k:12} csv=${a:>13,.2f}   dashboard=${b:>13,.2f}   {'OK' if ok else '!! MISMATCH'}")
    held_ok = exp["numHeld"] == got["numHeld"]
    flag = "OK" if held_ok else "!! differs (check for dust lots / dropped names)"
    print(f"  {'numHeld':12} csv={exp['numHeld']:>14}   dashboard={got['numHeld']:>14}   {flag}")
    legs_ok = exp["optLegCount"] == got.get("optLegCount")
    print(f"  {'optLegCount':12} csv={exp['optLegCount']:>14}   dashboard={got.get('optLegCount'):>14}   {'OK' if legs_ok else '!! MISMATCH'}")
    dashboard_shares = {row["sym"]: round(float(row.get("shares") or 0), 8)
                        for row in written.get("stocks", []) if row.get("held")}
    share_mismatches = sorted(sym for sym in set(exp["sharesBySymbol"]) | set(dashboard_shares)
                              if abs(exp["sharesBySymbol"].get(sym, 0) - dashboard_shares.get(sym, 0)) > 1e-7)
    shares_ok = not share_mismatches
    dashboard_cost = round(sum(float(row.get("cost") or 0) for row in written.get("stocks", []) if row.get("held")), 2)
    cost_ok = abs(exp["equityCostBasis"] - dashboard_cost) <= TOL
    print(f"  {'equityCost':12} csv=${exp['equityCostBasis']:>13,.2f}   dashboard=${dashboard_cost:>13,.2f}   {'OK' if cost_ok else '!! MISMATCH'}")
    print(f"  {'shareFingerprint':12} {'all symbols match' if shares_ok else '!! ' + ', '.join(share_mismatches)}")
    hard_ok = hard_ok and held_ok and legs_ok and shares_ok and cost_ok
    if not hard_ok:
        sys.exit("\n!! VERIFICATION FAILED: the dashboard does not match the Portfolio CSV.\n"
                 "   A holding or an entire account may have been dropped. Do NOT trust this\n"
                 "   dashboard until generate.parse_portfolio is fixed. See docs/METHODOLOGY.md.")
    print("  ✓ shares, cost basis, equities, cash, pending, options, and whole-account net worth match the broker CSV")
    publish_verified_dashboard(staged_out, target_out)
    print(f"  ✓ atomically published verified dashboard → {os.path.relpath(target_out, HERE)}")

    # 3) append to the sync log + show the delta -------------------------------
    log = []
    if os.path.exists(LOG):
        try:
            log = json.load(open(LOG))
        except Exception:
            log = []
    prev = log[-1] if log else None
    snap = {
        "syncedAt": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "portfolioFile": os.path.basename(portfolio),
        "historyMode": args.history_mode,
        "historyFiles": [os.path.basename(p) for p in history_sources],
        "sourceHashes": current_hashes,
        "portfolioSnapshot": portfolio_freshness,
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
        "accountNetWorth": got.get("accountNetWorth"),
        "cashTotal": got.get("cashTotal"),
        "pendingTotal": got.get("pendingTotal"),
        "optMarkNet": got.get("optMarkNet"),
        "optMarkGross": got.get("optMarkGross"),
        "optBrokerPnl": got.get("optBrokerPnl"),
        "optEntryCashNet": got.get("optEntryCashNet"),
        "optLegCount": got.get("optLegCount"),
    }
    log.append(snap)
    _atomic_json(LOG, log)

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
        subprocess.run(["open", target_out])
    print(f"\n✓ sync complete → {os.path.relpath(target_out, HERE)}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Refresh the dashboard with latest Yahoo equity prices.

This is the automation entrypoint. It uses the newest Fidelity CSVs for shares,
cost basis, cash and option marks, then asks generate.py to revalue held stocks
with the latest available Yahoo prices. It intentionally does not call sync.py:
sync.py verifies equality with the broker Portfolio CSV, while this job is a
daily mark-to-market refresh that may differ from the stale broker snapshot.
"""
import argparse
import atexit
import datetime as dt
import fcntl
import glob
import json
import math
import os
import shlex
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output" / "portfolio_dashboard.html"
RUN_LOG = ROOT / "output" / "price_refresh_log.json"
TEXT_LOG = ROOT / "output" / "price_refresh.log"
LOCK_PATH = ROOT / "output" / ".price_refresh.lock"
PRIVATE_FILE_MODE = 0o600
PRIVATE_DIR_MODE = 0o700

sys.path.insert(0, str(ROOT))
import sync as sync_gate  # noqa: E402  (ROOT must be known before import)
from scripts.artifact_io import atomic_write_json  # noqa: E402
from scripts.dashboard_payload import read_dashboard_payload as _read_dashboard_payload  # noqa: E402


def _private_runtime_dir(path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    if path == ROOT / "output" or ROOT / "output" in path.parents:
        path.chmod(PRIVATE_DIR_MODE)


def _scrub(text, sensitive=()):
    replacements = [(str(Path(value).expanduser().resolve()), "<private-path>")
                    for value in sensitive if value]
    replacements += [(str(ROOT), "<repo>"), (str(Path.home()), "<home>")]
    result = str(text)
    for value, label in sorted(set(replacements), key=lambda item: len(item[0]), reverse=True):
        result = result.replace(value, label)
    return result


def atomic_json(path, payload):
    atomic_write_json(path, payload)


def read_payload(path):
    return _read_dashboard_payload(path)


def run_momentum_strategy(mode, no_fetch, started):
    if mode == "off":
        return
    cmd = [sys.executable, str(ROOT / "scripts" / "momentum_top3.py")]
    if no_fetch:
        cmd.append("--no-fetch")
    proc = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True)
    _private_runtime_dir(TEXT_LOG.parent)
    with TEXT_LOG.open("a") as f:
        f.write(f"\n=== {started} momentum-strategy returncode={proc.returncode} ===\n")
        f.write("$ " + shlex.join(_scrub(part) for part in cmd) + "\n")
        f.write(_scrub(proc.stdout))
        if proc.stderr:
            f.write("\n[stderr]\n" + _scrub(proc.stderr))
    TEXT_LOG.chmod(PRIVATE_FILE_MODE)
    sys.stdout.write(proc.stdout)
    if proc.stderr:
        sys.stderr.write(proc.stderr)
    if proc.returncode and mode == "on":
        raise SystemExit(proc.returncode)
    if proc.returncode:
        print("(momentum strategy refresh failed; keeping previous artifact)")


def newest_portfolio(input_dir):
    matches = glob.glob(str(Path(input_dir).expanduser() / "Portfolio_Positions*.csv"))
    return max(matches, key=os.path.getmtime) if matches else None


def _assert_close(label, actual, expected, tolerance=sync_gate.TOL):
    try:
        a, b = float(actual), float(expected)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"mark-to-market verification missing numeric {label}") from exc
    if not (math.isfinite(a) and math.isfinite(b)) or abs(a - b) > tolerance:
        raise RuntimeError(f"mark-to-market verification failed for {label}: dashboard={a}, broker={b}")


def validate_mark_to_market(payload, portfolio, *, allow_stale=False):
    """Validate freshness plus every non-price broker/account invariant."""
    summary = payload.get("summary") or {}
    if summary.get("priceMode") != "mark-to-market":
        raise RuntimeError("price refresh did not produce a mark-to-market dashboard")
    stale = summary.get("stalePriceSymbols") or {}
    missing = summary.get("missingPriceSymbols") or []
    if (stale or missing) and not allow_stale:
        raise RuntimeError(
            f"held-price freshness gate failed: {len(stale)} stale, {len(missing)} missing; "
            "last-known-good dashboard retained"
        )

    expected = sync_gate.independent_totals(portfolio)
    stocks = payload.get("stocks") or []
    dashboard_shares = {
        row.get("sym"): round(float(row.get("shares") or 0), 8)
        for row in stocks if row.get("held") and row.get("sym")
    }
    expected_shares = expected["sharesBySymbol"]
    mismatches = sorted(
        sym for sym in set(expected_shares) | set(dashboard_shares)
        if abs(expected_shares.get(sym, 0) - dashboard_shares.get(sym, 0)) > 1e-7
    )
    if mismatches:
        raise RuntimeError("mark-to-market share fingerprint mismatch: " + ", ".join(mismatches))
    if summary.get("numHeld") != expected["numHeld"]:
        raise RuntimeError("mark-to-market held-position count does not match broker snapshot")

    dashboard_cost = sum(float(row.get("cost") or 0) for row in stocks if row.get("held"))
    _assert_close("equity cost basis", dashboard_cost, expected["equityCostBasis"])
    for key in ("cashTotal", "pendingTotal", "optMarkNet", "optMarkGross",
                "optBrokerPnl", "optEntryCashNet"):
        _assert_close(key, summary.get(key), expected[key])
    if summary.get("optLegCount") != expected["optLegCount"]:
        raise RuntimeError("mark-to-market option-leg count does not match broker snapshot")

    market_value = float(summary.get("marketValue") or 0)
    unrealized = float(summary.get("unrealized") or 0)
    _assert_close("mark-to-market unrealized P&L", unrealized,
                  market_value - expected["equityCostBasis"])
    expected_whole = market_value + expected["cashTotal"] + expected["pendingTotal"] + expected["optMarkNet"]
    _assert_close("whole-account arithmetic", summary.get("accountNetWorth"), expected_whole)
    return expected


def publish_private(staged, target):
    staged, target = Path(staged), Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    staged.chmod(PRIVATE_FILE_MODE)
    os.replace(staged, target)
    target.chmod(PRIVATE_FILE_MODE)


def main():
    os.umask(0o077)
    ap = argparse.ArgumentParser(description="Daily portfolio dashboard price refresh.")
    ap.add_argument("--input-dir", default=str(Path.home() / "Downloads"))
    ap.add_argument("--portfolio")
    ap.add_argument("--history")
    ap.add_argument("--history-mode", choices=("cumulative", "exact"), default="cumulative")
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--as-of", help="target price date, YYYY-MM-DD; default: today")
    ap.add_argument("--no-fetch", action="store_true", help="reuse cached prices")
    ap.add_argument("--allow-stale-prices", action="store_true",
                    help="publish even when a held symbol lacks an exact --as-of close")
    ap.add_argument("--max-export-age-hours", type=float, default=120.0,
                    help="maximum position-snapshot age (default: 120 hours)")
    ap.add_argument("--allow-old-export", action="store_true",
                    help="explicit override for a historical/old position snapshot")
    ap.add_argument("--momentum-strategy", choices=("auto", "on", "off"), default="auto",
                    help="refresh/embed the Momentum Top-3 strategy artifact before rendering")
    ap.add_argument("--open", action="store_true", help="open dashboard after refresh")
    ap.add_argument("--trigger", default=os.environ.get("PORTFOLIO_REFRESH_TRIGGER", "manual"))
    args = ap.parse_args()

    started = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    safe_trigger = "".join(ch for ch in str(args.trigger) if ch.isprintable() and ch not in "\r\n")[:80] or "unknown"
    if args.as_of:
        try:
            as_of_date = dt.date.fromisoformat(args.as_of)
        except ValueError as exc:
            raise SystemExit("--as-of must be YYYY-MM-DD") from exc
        if as_of_date > dt.date.today():
            raise SystemExit(f"--as-of {as_of_date} is in the future")

    portfolio = args.portfolio or newest_portfolio(args.input_dir)
    if not portfolio:
        raise SystemExit(f"no Portfolio_Positions*.csv found in {args.input_dir}")
    if args.max_export_age_hours <= 0:
        raise SystemExit("--max-export-age-hours must be positive")
    try:
        sync_gate.validate_export_freshness(
            portfolio, max_age_hours=args.max_export_age_hours, allow_old=args.allow_old_export)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    _private_runtime_dir(LOCK_PATH.parent)
    lock_handle = LOCK_PATH.open("a+")
    LOCK_PATH.chmod(PRIVATE_FILE_MODE)
    try:
        fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        raise SystemExit("another price refresh is already running") from exc
    atexit.register(lock_handle.close)
    run_momentum_strategy(args.momentum_strategy, args.no_fetch, started)
    target = Path(args.out).expanduser().resolve()
    staged = target.with_name(f".{target.name}.refresh.{os.getpid()}.tmp")
    try:
        staged.unlink()
    except FileNotFoundError:
        pass
    atexit.register(lambda: staged.unlink(missing_ok=True))

    cmd = [
        sys.executable, str(ROOT / "generate.py"),
        "--input-dir", args.input_dir,
        "--portfolio", portfolio,
        "--history-mode", args.history_mode,
        "--out", str(staged),
        "--mark-to-market",
    ]
    if args.history:
        cmd += ["--history", args.history]
    if args.as_of:
        cmd += ["--as-of", args.as_of]
    if args.no_fetch:
        cmd.append("--no-fetch")

    proc = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True)
    sensitive = (args.input_dir, portfolio, args.history, args.out, staged)
    _private_runtime_dir(TEXT_LOG.parent)
    with TEXT_LOG.open("a") as f:
        f.write(f"\n=== {started} trigger={safe_trigger} returncode={proc.returncode} ===\n")
        f.write("$ " + shlex.join(_scrub(part, sensitive) for part in cmd) + "\n")
        f.write(_scrub(proc.stdout, sensitive))
        if proc.stderr:
            f.write("\n[stderr]\n" + _scrub(proc.stderr, sensitive))
    TEXT_LOG.chmod(PRIVATE_FILE_MODE)

    sys.stdout.write(proc.stdout)
    if proc.stderr:
        sys.stderr.write(proc.stderr)
    if proc.returncode:
        raise SystemExit(proc.returncode)

    payload = read_payload(staged)
    validate_mark_to_market(payload, portfolio, allow_stale=args.allow_stale_prices)
    publish_private(staged, target)
    s = payload["summary"]
    entry = {
        "ranAt": started,
        "trigger": safe_trigger,
        "dashboard": (str(target.relative_to(ROOT)) if target.is_relative_to(ROOT) else target.name),
        "dateRange": s.get("dateRange"),
        "priceMode": s.get("priceMode"),
        "priceAsOf": s.get("priceAsOf"),
        "refreshedPriceCount": s.get("refreshedPriceCount"),
        "freshPriceSymbols": s.get("freshPriceSymbols") or [],
        "stalePriceSymbols": s.get("stalePriceSymbols") or {},
        "missingPriceSymbols": s.get("missingPriceSymbols") or [],
        "oldestHeldPriceAsOf": s.get("oldestHeldPriceAsOf"),
        "fetchPartial": s.get("fetchPartial", False),
        "marketValue": s.get("marketValue"),
        "unrealized": s.get("unrealized"),
        "curReturn": s.get("curReturn"),
        "spReturn": s.get("spReturn"),
        "nasdaqReturn": s.get("nasdaqReturn"),
        "accountNetWorth": s.get("accountNetWorth"),
    }
    if RUN_LOG.exists():
        try:
            log = json.loads(RUN_LOG.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"invalid price refresh log {RUN_LOG}: {exc}") from exc
        if not isinstance(log, list):
            raise RuntimeError(f"invalid price refresh log {RUN_LOG}: expected JSON list")
    else:
        log = []
    log.append(entry)
    atomic_json(RUN_LOG, log)

    if args.open:
        subprocess.run(["open", str(target)], check=False)

    print(f"\n✓ daily price refresh complete: {entry['priceAsOf']} · market value ${entry['marketValue']:,.2f}")


if __name__ == "__main__":
    main()

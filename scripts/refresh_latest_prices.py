#!/usr/bin/env python3
"""Refresh the dashboard with latest Yahoo equity prices.

This is the automation entrypoint. It uses the newest Fidelity CSVs for shares,
cost basis, cash and option marks, then asks generate.py to revalue held stocks
with the latest available Yahoo prices. It intentionally does not call sync.py:
sync.py verifies equality with the broker Portfolio CSV, while this job is a
daily mark-to-market refresh that may differ from the stale broker snapshot.
"""
import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output" / "portfolio_dashboard.html"
RUN_LOG = ROOT / "output" / "price_refresh_log.json"
TEXT_LOG = ROOT / "output" / "price_refresh.log"


def read_payload(path):
    html = path.read_text()
    m = re.search(r"const DATA = (\{.*?\});", html, re.S)
    if not m:
        raise RuntimeError(f"DATA payload not found in {path}")
    return json.loads(m.group(1))


def run_momentum_strategy(mode, no_fetch, started):
    if mode == "off":
        return
    cmd = [sys.executable, str(ROOT / "scripts" / "momentum_top3.py")]
    if no_fetch:
        cmd.append("--no-fetch")
    proc = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True)
    TEXT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with TEXT_LOG.open("a") as f:
        f.write(f"\n=== {started} momentum-strategy returncode={proc.returncode} ===\n")
        f.write("$ " + " ".join(cmd) + "\n")
        f.write(proc.stdout)
        if proc.stderr:
            f.write("\n[stderr]\n" + proc.stderr)
    sys.stdout.write(proc.stdout)
    if proc.stderr:
        sys.stderr.write(proc.stderr)
    if proc.returncode and mode == "on":
        raise SystemExit(proc.returncode)
    if proc.returncode:
        print("(momentum strategy refresh failed; keeping previous artifact)")


def main():
    ap = argparse.ArgumentParser(description="Daily portfolio dashboard price refresh.")
    ap.add_argument("--input-dir", default=str(Path.home() / "Downloads"))
    ap.add_argument("--portfolio")
    ap.add_argument("--history")
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--as-of", help="target price date, YYYY-MM-DD; default: today")
    ap.add_argument("--no-fetch", action="store_true", help="reuse cached prices")
    ap.add_argument("--momentum-strategy", choices=("auto", "on", "off"), default="auto",
                    help="refresh/embed the Momentum Top-3 strategy artifact before rendering")
    ap.add_argument("--open", action="store_true", help="open dashboard after refresh")
    ap.add_argument("--trigger", default=os.environ.get("PORTFOLIO_REFRESH_TRIGGER", "manual"))
    args = ap.parse_args()

    started = dt.datetime.now().isoformat(timespec="seconds")
    run_momentum_strategy(args.momentum_strategy, args.no_fetch, started)

    cmd = [
        sys.executable, str(ROOT / "generate.py"),
        "--input-dir", args.input_dir,
        "--out", args.out,
        "--mark-to-market",
    ]
    if args.portfolio:
        cmd += ["--portfolio", args.portfolio]
    if args.history:
        cmd += ["--history", args.history]
    if args.as_of:
        cmd += ["--as-of", args.as_of]
    if args.no_fetch:
        cmd.append("--no-fetch")

    proc = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True)
    TEXT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with TEXT_LOG.open("a") as f:
        f.write(f"\n=== {started} trigger={args.trigger} returncode={proc.returncode} ===\n")
        f.write("$ " + " ".join(cmd) + "\n")
        f.write(proc.stdout)
        if proc.stderr:
            f.write("\n[stderr]\n" + proc.stderr)

    sys.stdout.write(proc.stdout)
    if proc.stderr:
        sys.stderr.write(proc.stderr)
    if proc.returncode:
        raise SystemExit(proc.returncode)

    payload = read_payload(Path(args.out))
    s = payload["summary"]
    entry = {
        "ranAt": started,
        "trigger": args.trigger,
        "dashboard": str(Path(args.out).resolve()),
        "dateRange": s.get("dateRange"),
        "priceMode": s.get("priceMode"),
        "priceAsOf": s.get("priceAsOf"),
        "refreshedPriceCount": s.get("refreshedPriceCount"),
        "marketValue": s.get("marketValue"),
        "unrealized": s.get("unrealized"),
        "curReturn": s.get("curReturn"),
        "spReturn": s.get("spReturn"),
        "nasdaqReturn": s.get("nasdaqReturn"),
        "accountNetWorth": s.get("accountNetWorth"),
    }
    try:
        log = json.loads(RUN_LOG.read_text()) if RUN_LOG.exists() else []
    except Exception:
        log = []
    log.append(entry)
    RUN_LOG.write_text(json.dumps(log, indent=2))

    if args.open:
        subprocess.run(["open", str(Path(args.out).resolve())], check=False)

    print(f"\n✓ daily price refresh complete: {entry['priceAsOf']} · market value ${entry['marketValue']:,.2f}")


if __name__ == "__main__":
    main()

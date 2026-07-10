#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Install a macOS LaunchAgent that runs the QQQ intraday monitor every 15 min.

Fires at :07/:22/:37/:52 of every hour (off the round marks to avoid Yahoo
rate-limit clustering). The monitor itself decides whether to push Telegram
(only during market hours + a "just closed" tick at 16:00 ET); outside that
window it just writes to the local log and exits silently.

Run:
    python3 scripts/install_qqq_monitor_launchd.py            # install + load
    python3 scripts/install_qqq_monitor_launchd.py --uninstall

Logs:
    output/qqq_monitor.out.log    (stdout — last few runs)
    output/qqq_monitor.err.log    (stderr — Telegram errors etc)
"""
import argparse
import os
import plistlib
import subprocess
import sys
from pathlib import Path

try:
    from scripts.artifact_io import atomic_write_bytes
except ModuleNotFoundError:
    from artifact_io import atomic_write_bytes


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "qqq_intraday_monitor.py"
LAUNCH_DIR = Path.home() / "Library" / "LaunchAgents"
OUT = ROOT / "output"
LABEL = "org.portfolio.tracker.qqq-monitor"


def plist_path():
    return LAUNCH_DIR / f"{LABEL}.plist"


def build_plist(python_exe):
    # Fire at :07/:22/:37/:52 of every hour. launchd evaluates these as
    # "any minute matching one of these values" — same calendar Match logic as
    # cron with comma-separated minutes. The script self-gates on market hours.
    minutes = [7, 22, 37, 52]
    calendar_intervals = [{"Minute": m} for m in minutes]
    payload = {
        "Label": LABEL,
        "ProgramArguments": [python_exe, "-W", "ignore", str(RUNNER)],
        "StartCalendarInterval": calendar_intervals,
        "StandardOutPath": str(OUT / "qqq_monitor.out.log"),
        "StandardErrorPath": str(OUT / "qqq_monitor.err.log"),
        "RunAtLoad": False,           # don't fire immediately on load — wait for the next :07/:22/:37/:52
        "ProcessType": "Background",
        # PATH so Python can find yfinance + system libs reliably
        "EnvironmentVariables": {
            "PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": str(Path.home()),
            "LANG": "en_US.UTF-8",
        },
    }
    return payload


def install(python_exe):
    LAUNCH_DIR.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True, mode=0o700)
    OUT.chmod(0o700)
    path = plist_path()
    payload = build_plist(python_exe)
    # Unload if already loaded so we can overwrite cleanly
    if path.exists():
        subprocess.run(["launchctl", "unload", str(path)], stderr=subprocess.DEVNULL)
    atomic_write_bytes(path, plistlib.dumps(payload))
    print(f"· wrote {path}")
    # Load (launchctl bootstrap is the modern API; fall back to load on older macOS)
    uid = os.getuid()
    cmd = ["launchctl", "bootstrap", f"gui/{uid}", str(path)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        # bootstrap fails if already bootstrapped — fall back to old load
        r2 = subprocess.run(["launchctl", "load", str(path)], capture_output=True, text=True)
        if r2.returncode != 0:
            print(f"!! launchctl load failed: {r2.stderr.strip() or r.stderr.strip()}", file=sys.stderr)
            return 1
    # Confirm
    list_r = subprocess.run(["launchctl", "list", LABEL], capture_output=True, text=True)
    if list_r.returncode == 0:
        print(f"✓ {LABEL} loaded — fires at :07 / :22 / :37 / :52 of every hour")
        print(f"  · stdout log: {OUT / 'qqq_monitor.out.log'}")
        print(f"  · stderr log: {OUT / 'qqq_monitor.err.log'}")
        print(f"  · script gates Telegram on US market hours (open + closing tick only)")
    else:
        print(f"!! agent installed but not visible in launchctl list", file=sys.stderr)
        return 1
    return 0


def uninstall():
    path = plist_path()
    if not path.exists():
        print(f"· nothing to remove ({path} not found)")
        return 0
    subprocess.run(["launchctl", "unload", str(path)], stderr=subprocess.DEVNULL)
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{LABEL}"], stderr=subprocess.DEVNULL)
    path.unlink()
    print(f"✓ {LABEL} removed")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--python", default="/Applications/Xcode.app/Contents/Developer/usr/bin/python3",
                    help="Python interpreter (must have yfinance + urllib)")
    ap.add_argument("--uninstall", action="store_true", help="Remove the LaunchAgent")
    args = ap.parse_args()
    if args.uninstall:
        return uninstall()
    return install(args.python)


if __name__ == "__main__":
    sys.exit(main())

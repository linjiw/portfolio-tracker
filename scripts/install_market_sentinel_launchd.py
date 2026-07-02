#!/usr/bin/env python3
"""Install a weekday LaunchAgent for the market sentinel Telegram report.

Default times are Pacific:
  - 06:45: early cash-session read
  - 10:30: midday regime check
  - 12:45: pre-close decision check
  - 13:15: close/post-close summary

Telegram credentials are read by scripts/telegram_notifier.py from
~/.config/ptrak/telegram.json, so the bot token is not written into this plist.
"""
import argparse
import os
import plistlib
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "market_sentinel.py"
LAUNCH_DIR = Path.home() / "Library" / "LaunchAgents"
OUT = ROOT / "output" / "market_sentinel"
LABEL = "org.portfolio.tracker.market-sentinel"
DEFAULT_TIMES = [(6, 45), (10, 30), (12, 45), (13, 15)]


def plist_path():
    return LAUNCH_DIR / f"{LABEL}.plist"


def launchctl(*parts):
    return subprocess.run(["launchctl", *parts], text=True, capture_output=True)


def parse_times(raw):
    if not raw:
        return DEFAULT_TIMES
    out = []
    for part in raw.split(","):
        hh, mm = part.strip().split(":", 1)
        out.append((int(hh), int(mm)))
    return out


def build_plist(args, python_exe):
    program = [
        python_exe,
        str(RUNNER),
        "--refresh-dashboard",
        "--refresh-intraday",
        "--telegram",
        "--input-dir",
        args.input_dir,
    ]
    if args.no_news:
        program.append("--no-news")
    intervals = []
    for hour, minute in parse_times(args.times):
        for weekday in range(1, 6):
            intervals.append({"Weekday": weekday, "Hour": hour, "Minute": minute})
    return {
        "Label": LABEL,
        "ProgramArguments": program,
        "WorkingDirectory": str(ROOT),
        "EnvironmentVariables": {
            "PATH": f"{Path(python_exe).parent}:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            "PYTHONUNBUFFERED": "1",
        },
        "StartCalendarInterval": intervals,
        "StandardOutPath": str(OUT / "launchd.out.log"),
        "StandardErrorPath": str(OUT / "launchd.err.log"),
        "RunAtLoad": args.run_at_load,
    }


def install(args):
    LAUNCH_DIR.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)
    path = plist_path()
    uid = os.getuid()
    with path.open("wb") as f:
        plistlib.dump(build_plist(args, str(Path(sys.executable).resolve())), f)
    path.chmod(0o600)
    launchctl("bootout", f"gui/{uid}", str(path))
    boot = launchctl("bootstrap", f"gui/{uid}", str(path))
    if boot.returncode != 0:
        raise SystemExit(f"launchctl bootstrap failed: {boot.stderr.strip()}")
    en = launchctl("enable", f"gui/{uid}/{LABEL}")
    if en.returncode != 0:
        raise SystemExit(f"launchctl enable failed: {en.stderr.strip()}")
    print(path)
    print("✓ installed market sentinel LaunchAgent")


def uninstall():
    uid = os.getuid()
    path = plist_path()
    launchctl("bootout", f"gui/{uid}", str(path))
    if path.exists():
        path.unlink()
        print(f"removed {path}")
    print("✓ uninstalled market sentinel LaunchAgent")


def status():
    uid = os.getuid()
    proc = launchctl("print", f"gui/{uid}/{LABEL}")
    print(f"{LABEL}: {'loaded' if proc.returncode == 0 else 'not loaded'}")
    path = plist_path()
    if path.exists():
        print(path)


def main():
    ap = argparse.ArgumentParser(description="Install/uninstall the market sentinel LaunchAgent.")
    ap.add_argument("--input-dir", default=str(Path.home() / "Downloads"))
    ap.add_argument("--times", help="comma-separated PT times such as 06:45,10:30,12:45,13:15")
    ap.add_argument("--run-at-load", action="store_true")
    ap.add_argument("--no-news", action="store_true")
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()
    if args.uninstall:
        uninstall()
    elif args.status:
        status()
    else:
        install(args)


if __name__ == "__main__":
    main()

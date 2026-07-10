#!/usr/bin/env python3
"""Install macOS LaunchAgents for portfolio price refresh.

Schedules two weekday jobs in the local timezone:
  - market-open refresh: 06:35 Pacific, shortly after the US cash open
  - market-close refresh: 13:10 Pacific, shortly after the US cash close

launchd cannot natively skip NYSE holidays. On holidays this job is harmless:
generate.py uses the latest available Yahoo market date.
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
RUNNER = ROOT / "scripts" / "refresh_latest_prices.py"
LAUNCH_DIR = Path.home() / "Library" / "LaunchAgents"
OUT = ROOT / "output"

JOBS = {
    "market-open": {"hour": 6, "minute": 35},
    "market-close": {"hour": 13, "minute": 10},
}


def plist_label(trigger):
    return f"org.portfolio.tracker.price-refresh.{trigger}"


def plist_path(trigger):
    return LAUNCH_DIR / f"{plist_label(trigger)}.plist"


def build_plist(trigger, hour, minute, python_exe, input_dir, no_fetch):
    args = [python_exe, str(RUNNER), "--input-dir", input_dir, "--trigger", trigger]
    if no_fetch:
        args.append("--no-fetch")
    return {
        "Label": plist_label(trigger),
        "ProgramArguments": args,
        "WorkingDirectory": str(ROOT),
        "EnvironmentVariables": {
            "PATH": f"{Path(python_exe).parent}:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            "PYTHONUNBUFFERED": "1",
        },
        "StartCalendarInterval": [
            {"Weekday": weekday, "Hour": hour, "Minute": minute}
            for weekday in range(1, 6)
        ],
        "StandardOutPath": str(OUT / f"price_refresh.{trigger}.launchd.out.log"),
        "StandardErrorPath": str(OUT / f"price_refresh.{trigger}.launchd.err.log"),
        "RunAtLoad": False,
    }


def launchctl(*parts):
    return subprocess.run(["launchctl", *parts], text=True, capture_output=True)


def install(args):
    LAUNCH_DIR.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True, mode=0o700)
    OUT.chmod(0o700)
    uid = os.getuid()
    python_exe = str(Path(sys.executable).resolve())
    written = []
    for trigger, when in JOBS.items():
        path = plist_path(trigger)
        obj = build_plist(trigger, when["hour"], when["minute"], python_exe, args.input_dir, args.no_fetch)
        atomic_write_bytes(path, plistlib.dumps(obj))
        written.append(path)
        launchctl("bootout", f"gui/{uid}", str(path))
        boot = launchctl("bootstrap", f"gui/{uid}", str(path))
        if boot.returncode != 0:
            raise SystemExit(f"launchctl bootstrap failed for {path}: {boot.stderr.strip()}")
        en = launchctl("enable", f"gui/{uid}/{plist_label(trigger)}")
        if en.returncode != 0:
            raise SystemExit(f"launchctl enable failed for {plist_label(trigger)}: {en.stderr.strip()}")
    for path in written:
        print(path)
    print("✓ installed portfolio price refresh LaunchAgents")


def uninstall():
    uid = os.getuid()
    for trigger in JOBS:
        path = plist_path(trigger)
        launchctl("bootout", f"gui/{uid}", str(path))
        if path.exists():
            path.unlink()
            print(f"removed {path}")
    print("✓ uninstalled portfolio price refresh LaunchAgents")


def print_status():
    uid = os.getuid()
    for trigger in JOBS:
        label = plist_label(trigger)
        proc = launchctl("print", f"gui/{uid}/{label}")
        state = "loaded" if proc.returncode == 0 else "not loaded"
        print(f"{label}: {state}")


def main():
    ap = argparse.ArgumentParser(description="Install/uninstall portfolio price refresh LaunchAgents.")
    ap.add_argument("--input-dir", default=str(Path.home() / "Downloads"))
    ap.add_argument("--no-fetch", action="store_true", help="install jobs in cache-only mode")
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()

    if args.uninstall:
        uninstall()
    elif args.status:
        print_status()
    else:
        install(args)


if __name__ == "__main__":
    main()

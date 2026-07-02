#!/usr/bin/env python3
"""Install a weekday LaunchAgent for the daily portfolio Telegram brief.

Runs scripts/daily_portfolio_brief.py --sync --telegram on weekdays at 13:25
Pacific (after market close and after the sentinel's 13:15 close summary), so
the 今日要点 verdict lands with today's actual close baked in.

Telegram credentials come from ~/.config/ptrak/telegram.json via
scripts/telegram_notifier.py — nothing secret is written into the plist.

Usage:
    python3 scripts/install_daily_brief_launchd.py            # install / reload
    python3 scripts/install_daily_brief_launchd.py --times "7:00,13:25"
    python3 scripts/install_daily_brief_launchd.py --uninstall
"""
import argparse
import plistlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "daily_portfolio_brief.py"
LAUNCH_DIR = Path.home() / "Library" / "LaunchAgents"
LABEL = "org.portfolio.tracker.daily-brief"
DEFAULT_TIMES = [(13, 25)]


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


def build_plist(times, python_exe):
    intervals = [{"Weekday": wd, "Hour": h, "Minute": m}
                 for (h, m) in times for wd in range(1, 6)]
    return {
        "Label": LABEL,
        "ProgramArguments": [python_exe, str(RUNNER), "--sync", "--telegram"],
        "WorkingDirectory": str(ROOT),
        "EnvironmentVariables": {
            "PATH": f"{Path(python_exe).parent}:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            "PYTHONUNBUFFERED": "1",
        },
        "StartCalendarInterval": intervals,
        "RunAtLoad": False,
        "StandardOutPath": str(ROOT / "output" / "daily_brief.launchd.out.log"),
        "StandardErrorPath": str(ROOT / "output" / "daily_brief.launchd.err.log"),
    }


def main():
    ap = argparse.ArgumentParser(description="Install the daily portfolio brief LaunchAgent.")
    ap.add_argument("--times", help='comma-separated local times, e.g. "7:00,13:25" (default 13:25)')
    ap.add_argument("--uninstall", action="store_true")
    args = ap.parse_args()

    pp = plist_path()
    if args.uninstall:
        launchctl("bootout", f"gui/{__import__('os').getuid()}", str(pp))
        if pp.exists():
            pp.unlink()
        print(f"✓ removed {LABEL}")
        return

    times = parse_times(args.times)
    LAUNCH_DIR.mkdir(parents=True, exist_ok=True)
    with open(pp, "wb") as f:
        plistlib.dump(build_plist(times, sys.executable), f)
    uid = __import__("os").getuid()
    launchctl("bootout", f"gui/{uid}", str(pp))          # reload if already present
    r = launchctl("bootstrap", f"gui/{uid}", str(pp))
    if r.returncode != 0:
        print(f"launchctl bootstrap failed: {r.stderr.strip()}", file=sys.stderr)
        sys.exit(1)
    tt = ", ".join(f"{h:02d}:{m:02d}" for h, m in times)
    print(f"✓ installed {LABEL}")
    print(f"  · weekdays at {tt} (local) → sync + verify gate + Telegram 今日要点")
    print(f"  · logs: output/daily_brief.launchd.out.log / .err.log")
    print(f"  · remove with: python3 scripts/install_daily_brief_launchd.py --uninstall")


if __name__ == "__main__":
    main()

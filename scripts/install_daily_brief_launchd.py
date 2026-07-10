#!/usr/bin/env python3
"""Install the locked weekday close refresh plus Telegram brief.

Runs scripts/refresh_portfolio_intelligence.py on weekdays at 13:25 Pacific.
That one process owns broker reconciliation, fresh prices, all dashboard
producers, the final cached render, downstream risk tools, and the Telegram
brief.  This prevents the older chain of independent close jobs from
overwriting one another with different price modes.

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

try:
    from scripts.artifact_io import atomic_write_bytes
except ModuleNotFoundError:
    from artifact_io import atomic_write_bytes

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "refresh_portfolio_intelligence.py"
LAUNCH_DIR = Path.home() / "Library" / "LaunchAgents"
LABEL = "org.portfolio.tracker.daily-brief"
OUT = ROOT / "output"
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
        hour, minute = int(hh), int(mm)
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ValueError(f"invalid launch time: {part!r}")
        out.append((hour, minute))
    return out


def build_plist(times, python_exe, input_dir, include_news):
    intervals = [{"Weekday": wd, "Hour": h, "Minute": m}
                 for (h, m) in times for wd in range(1, 6)]
    return {
        "Label": LABEL,
        "ProgramArguments": [python_exe, str(RUNNER), "--input-dir", input_dir, "--telegram"]
                            + (["--news"] if include_news else []),
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
    ap.add_argument("--input-dir", default=str(Path.home() / "Downloads"))
    ap.add_argument("--news", action="store_true", help="include the sentinel news scan in the close refresh")
    ap.add_argument("--uninstall", action="store_true")
    args = ap.parse_args()

    pp = plist_path()
    if args.uninstall:
        launchctl("bootout", f"gui/{__import__('os').getuid()}", str(pp))
        if pp.exists():
            pp.unlink()
        print(f"✓ removed {LABEL}")
        return

    try:
        times = parse_times(args.times)
    except (TypeError, ValueError) as exc:
        ap.error(str(exc))
    LAUNCH_DIR.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True, mode=0o700)
    OUT.chmod(0o700)
    atomic_write_bytes(
        pp, plistlib.dumps(build_plist(
            times, str(Path(sys.executable).resolve()), args.input_dir, args.news)))
    uid = __import__("os").getuid()
    launchctl("bootout", f"gui/{uid}", str(pp))          # reload if already present
    r = launchctl("bootstrap", f"gui/{uid}", str(pp))
    if r.returncode != 0:
        print(f"launchctl bootstrap failed: {r.stderr.strip()}", file=sys.stderr)
        sys.exit(1)
    tt = ", ".join(f"{h:02d}:{m:02d}" for h, m in times)
    print(f"✓ installed {LABEL}")
    print(f"  · weekdays at {tt} (local) → locked full refresh + verify gate + Telegram 今日要点")
    print(f"  · logs: output/daily_brief.launchd.out.log / .err.log")
    print(f"  · remove with: python3 scripts/install_daily_brief_launchd.py --uninstall")


if __name__ == "__main__":
    main()

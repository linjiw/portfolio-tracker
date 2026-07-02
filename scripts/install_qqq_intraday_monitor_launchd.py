#!/usr/bin/env python3
"""Install a LaunchAgent for QQQ intraday 15-minute monitoring.

The LaunchAgent triggers every 15 minutes, and the monitor exits cleanly outside
the weekday US cash-session window. This is more robust than a long-running loop:
if one pull fails, the next interval still runs. Telegram credentials are read
from ~/.config/ptrak/telegram.json, so the bot token is not written into the
plist.
"""
import argparse
import os
import plistlib
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "monitor_qqq_intraday.py"
LAUNCH_DIR = Path.home() / "Library" / "LaunchAgents"
OUT = ROOT / "output" / "intraday_qqq"
LABEL = "org.portfolio.tracker.qqq-intraday-monitor"


def plist_path():
    return LAUNCH_DIR / f"{LABEL}.plist"


def launchctl(*parts):
    return subprocess.run(["launchctl", *parts], text=True, capture_output=True)


def build_plist(args, python_exe):
    prog = [
        python_exe, str(RUNNER),
        "--symbol", args.symbol,
        "--out-dir", str(OUT),
        "--market-hours-only",
    ]
    env = {
        "PATH": f"{Path(python_exe).parent}:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "PYTHONUNBUFFERED": "1",
    }
    if args.telegram:
        prog.append("--telegram")
        prog.append("--telegram-auto-chat-id")
    return {
        "Label": LABEL,
        "ProgramArguments": prog,
        "WorkingDirectory": str(ROOT),
        "EnvironmentVariables": env,
        "StartInterval": args.poll_seconds,
        "StandardOutPath": "/tmp/qqq-intraday-launchd.out.log",
        "StandardErrorPath": "/tmp/qqq-intraday-launchd.err.log",
        "RunAtLoad": getattr(args, "run_at_load", False),
    }


def install(args):
    LAUNCH_DIR.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)
    path = plist_path()
    uid = os.getuid()
    obj = build_plist(args, str(Path(sys.executable).resolve()))
    with path.open("wb") as f:
        plistlib.dump(obj, f)
    path.chmod(0o600)
    launchctl("bootout", f"gui/{uid}", str(path))
    boot = launchctl("bootstrap", f"gui/{uid}", str(path))
    if boot.returncode != 0:
        raise SystemExit(f"launchctl bootstrap failed: {boot.stderr.strip()}")
    en = launchctl("enable", f"gui/{uid}/{LABEL}")
    if en.returncode != 0:
        raise SystemExit(f"launchctl enable failed: {en.stderr.strip()}")
    print(path)
    print("✓ installed QQQ intraday LaunchAgent")
    if args.telegram:
        print("✓ Telegram enabled via ~/.config/ptrak/telegram.json; token was not stored in the plist.")


def uninstall():
    uid = os.getuid()
    path = plist_path()
    launchctl("bootout", f"gui/{uid}", str(path))
    if path.exists():
        path.unlink()
        print(f"removed {path}")
    print("✓ uninstalled QQQ intraday LaunchAgent")


def status():
    uid = os.getuid()
    proc = launchctl("print", f"gui/{uid}/{LABEL}")
    print(f"{LABEL}: {'loaded' if proc.returncode == 0 else 'not loaded'}")
    path = plist_path()
    if path.exists():
        print(path)


def main():
    ap = argparse.ArgumentParser(description="Install/uninstall QQQ intraday monitor LaunchAgent.")
    ap.add_argument("--symbol", default="QQQ")
    ap.add_argument("--poll-seconds", type=int, default=900)
    ap.add_argument("--run-at-load", action="store_true", default=True)
    ap.add_argument("--telegram", action="store_true")
    ap.add_argument("--telegram-token-env", default="TELEGRAM_BOT_TOKEN")
    ap.add_argument("--telegram-chat-id-env", default="TELEGRAM_CHAT_ID")
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

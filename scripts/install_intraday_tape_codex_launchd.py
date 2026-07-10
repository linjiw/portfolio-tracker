#!/usr/bin/env python3
"""Install launchd wrapper that invokes Codex CLI as the intraday Judge."""

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
RUNNER = ROOT / "scripts" / "run_intraday_tape_codex.py"
OUT = ROOT / "output" / "intraday_tape"
LAUNCH_DIR = Path.home() / "Library" / "LaunchAgents"
LABEL = "org.portfolio.tracker.intraday-tape-codex"
SAFE_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"


def plist_path():
    return LAUNCH_DIR / f"{LABEL}.plist"


def launchctl(*parts):
    return subprocess.run(["launchctl", *parts], text=True, capture_output=True)


def build_plist():
    return {
        "Label": LABEL,
        "ProgramArguments": [sys.executable, str(RUNNER)],
        "WorkingDirectory": str(ROOT),
        "EnvironmentVariables": {
            "PATH": SAFE_PATH,
            "PYTHONUNBUFFERED": "1",
            "HOME": str(Path.home()),
        },
        "StartInterval": 900,
        "RunAtLoad": True,
        "StandardOutPath": str(OUT / "launchd.out.log"),
        "StandardErrorPath": str(OUT / "launchd.err.log"),
    }


def install():
    LAUNCH_DIR.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True, mode=0o700)
    OUT.chmod(0o700)
    RUNNER.chmod(0o755)
    path = plist_path()
    uid = os.getuid()
    atomic_write_bytes(path, plistlib.dumps(build_plist()))
    launchctl("bootout", f"gui/{uid}", str(path))
    boot = launchctl("bootstrap", f"gui/{uid}", str(path))
    if boot.returncode != 0:
        raise SystemExit(f"launchctl bootstrap failed: {boot.stderr.strip()}")
    en = launchctl("enable", f"gui/{uid}/{LABEL}")
    if en.returncode != 0:
        raise SystemExit(f"launchctl enable failed: {en.stderr.strip()}")
    print(path)
    print("installed intraday tape Codex Judge LaunchAgent")


def uninstall():
    uid = os.getuid()
    path = plist_path()
    launchctl("bootout", f"gui/{uid}", str(path))
    if path.exists():
        path.unlink()
        print(f"removed {path}")


def status():
    uid = os.getuid()
    proc = launchctl("print", f"gui/{uid}/{LABEL}")
    print(f"{LABEL}: {'loaded' if proc.returncode == 0 else 'not loaded'}")
    if proc.stdout:
        print(proc.stdout[:2000])
    if proc.stderr:
        print(proc.stderr)


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Install/uninstall Codex intraday tape LaunchAgent.")
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()
    if args.uninstall:
        uninstall()
    elif args.status:
        status()
    else:
        install()


if __name__ == "__main__":
    main()

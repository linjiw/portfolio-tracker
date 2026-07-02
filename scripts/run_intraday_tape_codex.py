#!/usr/bin/env python3
"""Launch Codex CLI as the intraday tape Judge with lock/window guards."""

import datetime as dt
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output" / "intraday_tape"
PROMPT = ROOT / "prompts" / "intraday_tape_judge.md"
LOCK = Path("/tmp/qqq-intraday-codex.lock")
PT = dt.timezone(dt.timedelta(hours=-7), name="America/Los_Angeles")
CODEX_TIMEOUT_SECONDS = 600


def log(path, text):
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / path).open("a", encoding="utf-8") as f:
        f.write(f"{dt.datetime.now(tz=PT).isoformat(timespec='seconds')} {text}\n")


def in_window():
    now = dt.datetime.now(tz=PT)
    minute = now.hour * 60 + now.minute
    return now.weekday() < 5 and (6 * 60 + 30) <= minute <= (13 * 60 + 5)


def main():
    if not in_window():
        log("codex_runner.log", "skip outside monitor window")
        return 0
    try:
        LOCK.mkdir()
    except FileExistsError:
        log("codex_runner.log", "skip previous run still active")
        return 0
    try:
        log("codex_runner.log", "codex judge start")
        prompt = PROMPT.read_text(encoding="utf-8")
        cmd = [
            "/opt/homebrew/bin/codex",
            "--dangerously-bypass-approvals-and-sandbox",
            "-m", "gpt-5.5",
            "exec",
            "--cd", str(ROOT),
            "--output-last-message", str(OUT / "codex_last_message.txt"),
            "-",
        ]
        env = os.environ.copy()
        env["PATH"] = os.environ.get("PATH", "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin")
        with (OUT / "codex_runner.log").open("a", encoding="utf-8") as stdout, (
            OUT / "codex_runner.err.log"
        ).open("a", encoding="utf-8") as stderr:
            try:
                proc = subprocess.run(
                    cmd,
                    input=prompt,
                    text=True,
                    cwd=ROOT,
                    env=env,
                    stdout=stdout,
                    stderr=stderr,
                    timeout=CODEX_TIMEOUT_SECONDS,
                )
            except subprocess.TimeoutExpired:
                log("codex_runner.log", f"codex judge timeout after {CODEX_TIMEOUT_SECONDS}s")
                return 124
        log("codex_runner.log", f"codex judge done rc={proc.returncode}")
        return proc.returncode
    finally:
        try:
            LOCK.rmdir()
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())

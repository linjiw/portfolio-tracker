#!/usr/bin/env python3
"""Run the intraday Codex Judge with bounded privileges and stage guards."""

from __future__ import annotations

import datetime as dt
import fcntl
import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output" / "intraday_tape"
PROMPT = ROOT / "prompts" / "intraday_tape_judge.md"
LOCK = OUT / ".codex_judge.lock"
JUDGE_WORK = OUT / ".judge_workspace"
PT = ZoneInfo("America/Los_Angeles")
CODEX_TIMEOUT_SECONDS = 600
STAGE_TIMEOUT_SECONDS = 180
CODEX_MODEL = "gpt-5.5"
SAFE_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
MAX_LOG_BYTES = 2 * 1024 * 1024
REQUIRED_DIFF_KEYS = {
    "run_id",
    "verdict_tuple",
    "scenarios",
    "verdict_history",
    "burden_of_proof",
    "trigger_status",
    "position_instruction",
}
VERDICTS = {"ALLOW", "WATCH", "BLOCK", "BLOCK_DATA"}


def _private_directory(path):
    Path(path).mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        Path(path).chmod(0o700)
    except OSError:
        pass


def _rotate_log(path, max_bytes=MAX_LOG_BYTES):
    path = Path(path)
    try:
        if path.stat().st_size <= max_bytes:
            return
    except FileNotFoundError:
        return
    archived = path.with_suffix(path.suffix + ".1")
    os.replace(path, archived)
    try:
        archived.chmod(0o600)
    except OSError:
        pass


def log(path, message):
    _private_directory(OUT)
    target = OUT / path
    _rotate_log(target)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(f"{dt.datetime.now(tz=PT).isoformat(timespec='seconds')} {message}\n")
    try:
        target.chmod(0o600)
    except OSError:
        pass


def in_window(now=None):
    current = (now or dt.datetime.now(tz=PT)).astimezone(PT)
    current_et = current.astimezone(ZoneInfo("America/New_York"))
    try:
        from scripts.intraday_tape_sensor import market_session
    except ModuleNotFoundError:
        from intraday_tape_sensor import market_session
    session = market_session(current_et.date())
    if not session.get("open"):
        return False
    opened = dt.datetime.fromisoformat(session["openAt"])
    close_grace = dt.datetime.fromisoformat(session["closeAt"]) + dt.timedelta(minutes=5)
    return opened <= current_et <= close_grace


def acquire_lock(path=LOCK):
    """Acquire a crash-safe process lock; a stale file never blocks a future run."""
    path = Path(path)
    _private_directory(path.parent)
    handle = path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    handle.seek(0)
    handle.truncate()
    handle.write(f"pid={os.getpid()} started={dt.datetime.now(tz=PT).isoformat()}\n")
    handle.flush()
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return handle


def release_lock(handle):
    if handle is None:
        return
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def minimal_environment(source=None):
    """Return only runtime plumbing, never inherited trading/API credentials."""
    source = os.environ if source is None else source
    allowed = (
        "HOME",
        "USER",
        "LOGNAME",
        "TMPDIR",
        "CODEX_HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TERM",
        "TZ",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "__CF_USER_TEXT_ENCODING",
    )
    env = {name: str(source[name]) for name in allowed if source.get(name)}
    env.setdefault("HOME", str(Path.home()))
    env["PATH"] = SAFE_PATH
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def find_codex_binary(source=None):
    source = os.environ if source is None else source
    configured = source.get("CODEX_BIN")
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate.resolve())
        raise RuntimeError(f"CODEX_BIN is not executable: {candidate}")
    found = shutil.which("codex", path=SAFE_PATH)
    if not found:
        raise RuntimeError("codex executable not found on the restricted PATH")
    return found


def build_codex_command(binary, work_dir=JUDGE_WORK):
    """Build a noninteractive, ephemeral, no-network, workspace-bounded command."""
    work_dir = Path(work_dir)
    return [
        str(binary),
        "--sandbox", "workspace-write",
        "--ask-for-approval", "never",
        "--model", CODEX_MODEL,
        "--config", "sandbox_workspace_write.network_access=false",
        "exec",
        "--ephemeral",
        "--ignore-user-config",
        "--skip-git-repo-check",
        "--cd", str(work_dir),
        "--output-last-message", str(work_dir / "codex_last_message.txt"),
        "-",
    ]


def render_prompt():
    template = PROMPT.read_text(encoding="utf-8")
    replacements = {
        "{REPOSITORY_ROOT}": str(ROOT),
        "{JUDGE_WORKDIR}": str(JUDGE_WORK),
        "{CODEX_HOME}": str(Path.home() / ".codex"),
    }
    for marker, value in replacements.items():
        template = template.replace(marker, value)
    return template


def _run_stage(name, command, env, timeout):
    log("codex_runner.log", f"{name} start")
    stdout_path = OUT / "codex_runner.log"
    stderr_path = OUT / "codex_runner.err.log"
    _rotate_log(stdout_path)
    _rotate_log(stderr_path)
    with stdout_path.open("a", encoding="utf-8") as stdout, stderr_path.open("a", encoding="utf-8") as stderr:
        try:
            proc = subprocess.run(
                command,
                text=True,
                cwd=ROOT,
                env=env,
                stdout=stdout,
                stderr=stderr,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            log("codex_runner.log", f"{name} timeout after {timeout}s")
            return 124
    log("codex_runner.log", f"{name} done rc={proc.returncode}")
    return proc.returncode


def _strict_json(path):
    def reject_constant(value):
        raise ValueError(f"non-finite JSON constant: {value}")

    return json.loads(Path(path).read_text(encoding="utf-8"), parse_constant=reject_constant)


def validate_judge_outputs(work_dir=JUDGE_WORK, gates_path=None):
    """Reject absent, linked, oversized, or schema-incomplete model outputs."""
    work_dir = Path(work_dir)
    telegram_path = work_dir / "telegram_message.html"
    diff_path = work_dir / "state_diff.json"
    for path in (telegram_path, diff_path):
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ValueError(f"judge output is not a regular file: {path.name}")
        if info.st_size <= 0 or info.st_size > 256 * 1024:
            raise ValueError(f"judge output has invalid size: {path.name}")

    telegram = telegram_path.read_text(encoding="utf-8").strip()
    if not telegram or len(telegram) > 4096 or len(telegram.splitlines()) > 12:
        raise ValueError("telegram_message.html is empty or exceeds Telegram limits")
    if "not financial advice" not in telegram.lower():
        raise ValueError("telegram_message.html must retain the risk disclaimer")

    diff = _strict_json(diff_path)
    if not isinstance(diff, dict):
        raise ValueError("state_diff.json must be an object")
    missing = sorted(REQUIRED_DIFF_KEYS - set(diff))
    if missing:
        raise ValueError(f"state_diff.json missing required keys: {', '.join(missing)}")
    if not isinstance(diff.get("verdict_tuple"), list) or len(diff["verdict_tuple"]) != 4:
        raise ValueError("state_diff.json verdict_tuple must contain four items")
    run_id = diff.get("run_id")
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("state_diff.json run_id must identify the sensor bundle")
    verdict = diff["verdict_tuple"][0]
    if verdict not in VERDICTS:
        raise ValueError(f"unsupported Judge verdict: {verdict!r}")
    if not isinstance(diff["verdict_tuple"][1], str) or not diff["verdict_tuple"][1].strip():
        raise ValueError("verdict_tuple surviving branch must be non-empty")
    if diff["verdict_tuple"][2] != diff.get("trigger_status"):
        raise ValueError("verdict_tuple trigger status does not match trigger_status")
    if diff["verdict_tuple"][3] != diff.get("position_instruction"):
        raise ValueError("verdict_tuple position instruction does not match position_instruction")
    history = diff.get("verdict_history")
    if not isinstance(history, list) or not history or not isinstance(history[-1], dict):
        raise ValueError("state_diff.json verdict_history must append a verdict item")
    if any(history[-1].get(key) in (None, "") for key in ("t", "verdict", "conviction")):
        raise ValueError("latest verdict_history item requires t, verdict, and conviction")
    if history[-1].get("verdict") != verdict:
        raise ValueError("latest verdict_history verdict does not match verdict_tuple")
    try:
        history_time = dt.datetime.fromisoformat(str(history[-1]["t"]).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("latest verdict_history timestamp is invalid") from exc
    if history_time.tzinfo is None:
        raise ValueError("latest verdict_history timestamp requires a timezone")
    if not isinstance(history[-1].get("conviction"), str):
        raise ValueError("latest verdict_history conviction must be text")
    if not isinstance(diff.get("scenarios"), list):
        raise ValueError("state_diff.json scenarios must be a list")
    if any(not isinstance(item, dict) for item in diff["scenarios"]):
        raise ValueError("state_diff.json scenarios must contain objects")
    if not isinstance(diff.get("burden_of_proof"), str) or not diff["burden_of_proof"].strip():
        raise ValueError("state_diff.json burden_of_proof must be non-empty")
    if not isinstance(diff.get("trigger_status"), str) or not diff["trigger_status"].strip():
        raise ValueError("state_diff.json trigger_status must be non-empty")
    if not isinstance(diff.get("position_instruction"), str) or not diff["position_instruction"].strip():
        raise ValueError("state_diff.json position_instruction must be non-empty")
    if "positions" in diff:
        if not isinstance(diff["positions"], list) or any(not isinstance(item, dict) for item in diff["positions"]):
            raise ValueError("state_diff.json positions must be a list of objects")
    if "levels" in diff:
        if not isinstance(diff["levels"], list) or any(not isinstance(item, dict) for item in diff["levels"]):
            raise ValueError("state_diff.json levels must be a list of objects")
        for level in diff["levels"]:
            try:
                px = float(level["px"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("each state_diff level requires numeric px") from exc
            if not math.isfinite(px) or px <= 0:
                raise ValueError("state_diff level px must be finite and positive")

    first_line = telegram.splitlines()[0]
    message_verdicts = re.findall(r"(?<![A-Z_])(BLOCK_DATA|ALLOW|WATCH|BLOCK)(?![A-Z_])", first_line.upper())
    if not message_verdicts or message_verdicts[0] != verdict:
        raise ValueError("Telegram verdict line must lead with the state_diff verdict")

    if gates_path is not None:
        gates = _strict_json(gates_path)
        if not isinstance(gates, dict) or gates.get("schemaVersion") != 1:
            raise ValueError("binding gates.json is missing schemaVersion 1")
        if gates.get("run_id") != run_id:
            raise ValueError("Judge run_id does not match the binding sensor gates")
        if gates.get("score_cap") == "BLOCK_DATA" and verdict != "BLOCK_DATA":
            raise ValueError("BLOCK_DATA hard gate requires a BLOCK_DATA Judge verdict")
        if (gates.get("prohibit_allow") or gates.get("action_lock")) and verdict == "ALLOW":
            raise ValueError("binding hard gates prohibit an ALLOW Judge verdict")
    return telegram_path, diff_path


def publish_judge_outputs(work_dir=JUDGE_WORK):
    telegram_path, diff_path = validate_judge_outputs(work_dir, gates_path=OUT / "gates.json")
    for source, destination in (
        (telegram_path, OUT / "telegram_message.html"),
        (diff_path, OUT / "state_diff.json"),
    ):
        os.replace(source, destination)
        destination.chmod(0o600)
    last_message = Path(work_dir) / "codex_last_message.txt"
    if last_message.is_file() and not last_message.is_symlink():
        destination = OUT / "codex_last_message.txt"
        os.replace(last_message, destination)
        destination.chmod(0o600)


def _prepare_judge_workspace():
    _private_directory(JUDGE_WORK)
    for name in ("telegram_message.html", "state_diff.json", "codex_last_message.txt"):
        try:
            (JUDGE_WORK / name).unlink()
        except FileNotFoundError:
            pass


def main():
    if not in_window():
        log("codex_runner.log", "skip outside monitor window")
        return 0

    lock = acquire_lock()
    if lock is None:
        log("codex_runner.log", "skip previous run still active")
        return 0
    try:
        env = minimal_environment()
        sensor = [sys.executable, str(ROOT / "scripts" / "intraday_tape_sensor.py"), "--symbol", "QQQ"]
        rc = _run_stage("sensor", sensor, env, STAGE_TIMEOUT_SECONDS)
        if rc:
            return rc

        try:
            binary = find_codex_binary()
        except RuntimeError as exc:
            log("codex_runner.log", f"codex judge unavailable: {exc}")
            return 127

        _prepare_judge_workspace()
        log("codex_runner.log", "codex judge start sandbox=workspace-write network=off env=minimal")
        stdout_path = OUT / "codex_runner.log"
        stderr_path = OUT / "codex_runner.err.log"
        _rotate_log(stdout_path)
        _rotate_log(stderr_path)
        with stdout_path.open("a", encoding="utf-8") as stdout, stderr_path.open("a", encoding="utf-8") as stderr:
            try:
                proc = subprocess.run(
                    build_codex_command(binary),
                    input=render_prompt(),
                    text=True,
                    cwd=JUDGE_WORK,
                    env=env,
                    stdout=stdout,
                    stderr=stderr,
                    timeout=CODEX_TIMEOUT_SECONDS,
                )
            except subprocess.TimeoutExpired:
                log("codex_runner.log", f"codex judge timeout after {CODEX_TIMEOUT_SECONDS}s")
                return 124
        log("codex_runner.log", f"codex judge done rc={proc.returncode}")
        if proc.returncode:
            return proc.returncode
        try:
            publish_judge_outputs()
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            log("codex_runner.log", f"codex judge output rejected: {exc}")
            return 65

        finalizer = [sys.executable, str(ROOT / "scripts" / "intraday_tape_finalize.py")]
        return _run_stage("finalizer", finalizer, env, STAGE_TIMEOUT_SECONDS)
    finally:
        release_lock(lock)


if __name__ == "__main__":
    raise SystemExit(main())

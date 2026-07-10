#!/usr/bin/env python3
"""Apply Codex Judge state_diff, enforce dispatcher dedupe, and send Telegram."""

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import re
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    from scripts.artifact_io import append_csv_row_private, atomic_write_json, ensure_private_directory
except ModuleNotFoundError:  # direct ``python scripts/intraday_tape_finalize.py``
    from artifact_io import append_csv_row_private, atomic_write_json, ensure_private_directory


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output" / "intraday_tape"
PT = ZoneInfo("America/Los_Angeles")
REQUIRED_DIFF_KEYS = {
    "run_id", "verdict_tuple", "scenarios", "verdict_history", "burden_of_proof",
    "trigger_status", "position_instruction",
}
VERDICTS = {"ALLOW", "WATCH", "BLOCK", "BLOCK_DATA"}


def load_json(path, default):
    path = Path(path)
    if not path.exists():
        return default
    def reject_constant(value):
        raise ValueError(f"non-finite JSON constant {value}")
    return json.loads(path.read_text(encoding="utf-8"), parse_constant=reject_constant)


def write_json(path, data):
    atomic_write_json(path, data)


def acquire_lock(path):
    path = Path(path)
    ensure_private_directory(path.parent)
    handle = path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    path.chmod(0o600)
    return handle


def release_lock(handle):
    if handle is None:
        return
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def validate_diff(diff, state, gates):
    if not isinstance(diff, dict):
        raise ValueError("state_diff.json must be a JSON object")
    if not isinstance(gates, dict) or gates.get("schemaVersion") != 1:
        raise ValueError("gates.json must be a schemaVersion 1 object")
    missing = sorted(REQUIRED_DIFF_KEYS - set(diff))
    if missing:
        raise ValueError(f"state_diff.json missing required keys: {', '.join(missing)}")
    verdict_tuple = diff.get("verdict_tuple")
    if not isinstance(verdict_tuple, list) or len(verdict_tuple) != 4 or verdict_tuple[0] not in VERDICTS:
        raise ValueError("state_diff.json has an invalid verdict_tuple")
    if verdict_tuple[2] != diff.get("trigger_status") or verdict_tuple[3] != diff.get("position_instruction"):
        raise ValueError("state_diff.json verdict_tuple is internally inconsistent")
    history = diff.get("verdict_history")
    if not isinstance(history, list) or not history or not isinstance(history[-1], dict):
        raise ValueError("state_diff.json must append verdict_history")
    if any(history[-1].get(key) in (None, "") for key in ("t", "verdict", "conviction")):
        raise ValueError("latest verdict_history item requires t, verdict, and conviction")
    if history[-1].get("verdict") != verdict_tuple[0]:
        raise ValueError("latest verdict_history verdict does not match verdict_tuple")
    if not isinstance(diff.get("scenarios"), list):
        raise ValueError("state_diff scenarios must be a list")
    if not isinstance(diff.get("burden_of_proof"), str) or not diff["burden_of_proof"].strip():
        raise ValueError("state_diff burden_of_proof must be non-empty")
    if state.get("sensor_run_id") != diff.get("run_id") or gates.get("run_id") != diff.get("run_id"):
        raise ValueError("state_diff.json does not match the active sensor bundle")
    if gates.get("score_cap") == "BLOCK_DATA" and verdict_tuple[0] != "BLOCK_DATA":
        raise ValueError("BLOCK_DATA gate cannot be overridden")
    if (gates.get("prohibit_allow") or gates.get("action_lock")) and verdict_tuple[0] == "ALLOW":
        raise ValueError("hard gates prohibit ALLOW")
    return diff


def validate_message(message, verdict):
    if not message or len(message) > 4096 or len(message.splitlines()) > 12:
        raise ValueError("Telegram message is empty or exceeds limits")
    if "not financial advice" not in message.lower():
        raise ValueError("Telegram message is missing the risk disclaimer")
    labels = re.findall(
        r"(?<![A-Z_])(BLOCK_DATA|ALLOW|WATCH|BLOCK)(?![A-Z_])",
        message.splitlines()[0].upper(),
    )
    if not labels or labels[0] != verdict:
        raise ValueError("Telegram verdict line does not match state_diff")
    return message


def merge_levels(state, diff):
    incoming = diff.get("levels") or []
    if not incoming:
        return
    by_px = {}
    for level in state.get("levels", []):
        try:
            by_px[f"{float(level['px']):.2f}"] = level
        except Exception:
            continue
    for level in incoming:
        try:
            key = f"{float(level['px']):.2f}"
        except Exception:
            continue
        old = by_px.get(key, {})
        old.update(level)
        by_px[key] = old
    state["levels"] = sorted(by_px.values(), key=lambda x: float(x.get("px") or 0))


def verdict_hash(diff, telegram):
    vt = diff.get("verdict_tuple")
    if vt is None:
        hist = diff.get("verdict_history") or []
        last = hist[-1] if hist else {}
        vt = [
            last.get("verdict"),
            diff.get("surviving_branch"),
            diff.get("trigger_status"),
            diff.get("position_instruction"),
        ]
    raw = json.dumps(vt, sort_keys=True, ensure_ascii=False)
    if raw in ("[null, null, null, null]", "null"):
        raw = telegram.splitlines()[0] if telegram else ""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def compact_heartbeat(telegram):
    first = telegram.splitlines()[0].strip() if telegram.strip() else "QQQ tape"
    if "无新信息" in first:
        return first + "\nNot financial advice"
    return first + " | 无新信息\nNot financial advice"


def apply_diff(state, diff):
    merge_levels(state, diff)
    if "scenarios" in diff:
        state["scenarios"] = diff.get("scenarios") or []
    if "burden_of_proof" in diff:
        state["burden_of_proof"] = diff.get("burden_of_proof")
    if "swings" in diff:
        state.setdefault("swings", {}).update(diff.get("swings") or {})
    if "positions" in diff:
        positions = diff.get("positions")
        if not isinstance(positions, list):
            raise ValueError("state_diff positions must be a list")
        state["positions"] = positions
    for item in diff.get("verdict_history") or []:
        state.setdefault("verdict_history", []).append(item)
    return state


def main():
    ap = argparse.ArgumentParser(description="Finalize intraday tape Judge output.")
    ap.add_argument("--out-dir", default=str(OUT))
    ap.add_argument("--telegram-file", default=str(OUT / "telegram_message.html"))
    ap.add_argument("--state-diff-file", default=str(OUT / "state_diff.json"))
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--compact-unchanged", action="store_true",
                    help="compress unchanged verdicts to a one-line heartbeat")
    args = ap.parse_args()

    out = Path(args.out_dir)
    lock = acquire_lock(out / ".dispatch.lock")
    if lock is None:
        raise SystemExit("another intraday dispatch is active")
    try:
        return finalize(args, out)
    finally:
        release_lock(lock)


def finalize(args, out):
    state_path = out / "state.json"
    state = load_json(state_path, {})
    diff = load_json(args.state_diff_file, {})
    gates = load_json(out / "gates.json", {})
    validate_diff(diff, state, gates)
    telegram_path = Path(args.telegram_file)
    if not telegram_path.is_file():
        raise ValueError(f"missing Telegram message: {telegram_path}")
    telegram = telegram_path.read_text(encoding="utf-8").strip()
    validate_message(telegram, diff["verdict_tuple"][0])
    digest = verdict_hash(diff, telegram)
    changed = args.force or digest != state.get("last_verdict_hash")
    message = compact_heartbeat(telegram) if (not changed and args.compact_unchanged) else telegram

    sys.path.insert(0, str(ROOT / "scripts"))
    from telegram_notifier import send_message

    ok = send_message(message)
    if state.get("last_applied_judge_run_id") != diff.get("run_id"):
        state = apply_diff(state, diff)
        state["last_applied_judge_run_id"] = diff.get("run_id")
    attempted_at = dt.datetime.now(tz=PT).isoformat(timespec="seconds")
    state["last_dispatch_attempt_at"] = attempted_at
    state["last_dispatch_ok"] = bool(ok)
    if ok:
        state["last_verdict_hash"] = digest
        state["last_dispatch_at"] = attempted_at
    state["last_dispatch_changed"] = changed
    write_json(state_path, state)

    log_path = out / "dispatch_log.csv"
    fields = ["sentAt", "ok", "changed", "hash", "chars"]
    append_csv_row_private(log_path, {
        "sentAt": attempted_at,
        "ok": ok,
        "changed": changed,
        "hash": digest[:12],
        "chars": len(message),
    }, fields)
    if not ok:
        raise SystemExit("telegram send failed")
    print(json.dumps({"sent": ok, "changed": changed, "hash": digest[:12]}, ensure_ascii=False))


if __name__ == "__main__":
    main()

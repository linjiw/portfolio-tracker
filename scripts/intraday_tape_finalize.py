#!/usr/bin/env python3
"""Apply Codex Judge state_diff, enforce dispatcher dedupe, and send Telegram."""

import argparse
import csv
import datetime as dt
import hashlib
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output" / "intraday_tape"
PT = dt.timezone(dt.timedelta(hours=-7), name="America/Los_Angeles")


def load_json(path, default):
    path = Path(path)
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path, data):
    Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


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
        state["positions"] = diff.get("positions") or state.get("positions", [])
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
    state_path = out / "state.json"
    state = load_json(state_path, {})
    diff = load_json(args.state_diff_file, {})
    telegram_path = Path(args.telegram_file)
    telegram = telegram_path.read_text(encoding="utf-8").strip()
    digest = verdict_hash(diff, telegram)
    changed = args.force or digest != state.get("last_verdict_hash")
    message = compact_heartbeat(telegram) if (not changed and args.compact_unchanged) else telegram

    sys.path.insert(0, str(ROOT / "scripts"))
    from telegram_notifier import send_message

    ok = send_message(message)
    state = apply_diff(state, diff)
    state["last_verdict_hash"] = digest
    state["last_dispatch_at"] = dt.datetime.now(tz=PT).isoformat(timespec="seconds")
    state["last_dispatch_changed"] = changed
    write_json(state_path, state)

    log_path = out / "dispatch_log.csv"
    first = not log_path.exists()
    with log_path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["sentAt", "ok", "changed", "hash", "chars"])
        if first:
            writer.writeheader()
        writer.writerow({
            "sentAt": state["last_dispatch_at"],
            "ok": ok,
            "changed": changed,
            "hash": digest[:12],
            "chars": len(message),
        })
    if not ok:
        raise SystemExit("telegram send failed")
    print(json.dumps({"sent": ok, "changed": changed, "hash": digest[:12]}, ensure_ascii=False))


if __name__ == "__main__":
    main()

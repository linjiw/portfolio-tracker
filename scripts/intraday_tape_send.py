#!/usr/bin/env python3
"""Send a prepared intraday tape Judge message to Telegram."""

import argparse
import datetime as dt
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    from scripts.artifact_io import append_csv_row_private
    from scripts.intraday_tape_finalize import acquire_lock, release_lock
except ModuleNotFoundError:  # direct ``python scripts/intraday_tape_send.py``
    from artifact_io import append_csv_row_private
    from intraday_tape_finalize import acquire_lock, release_lock


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output" / "intraday_tape"
PT = ZoneInfo("America/Los_Angeles")


def main():
    ap = argparse.ArgumentParser(description="Send a prepared Telegram message.")
    ap.add_argument("--message-file", default=str(OUT / "telegram_message.html"))
    ap.add_argument("--log-file", default=str(OUT / "manual_dispatch_log.csv"),
                    help="manual-send audit log; separate from the Judge finalizer schema")
    args = ap.parse_args()

    lock = acquire_lock(Path(args.log_file).parent / ".dispatch.lock")
    if lock is None:
        raise SystemExit("another intraday dispatch is active")
    try:
        return dispatch(args)
    finally:
        release_lock(lock)


def dispatch(args):

    path = Path(args.message_file)
    if not path.exists():
        raise SystemExit(f"missing message file: {path}")
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise SystemExit(f"empty message file: {path}")

    sys.path.insert(0, str(ROOT / "scripts"))
    from telegram_notifier import send_message

    ok = send_message(text)
    log_path = Path(args.log_file)
    fields = ["sentAt", "ok", "messageFile", "chars"]
    append_csv_row_private(log_path, {
        "sentAt": dt.datetime.now(tz=PT).isoformat(timespec="seconds"),
        "ok": ok,
        "messageFile": path.name,
        "chars": len(text),
    }, fields)
    if not ok:
        raise SystemExit("telegram send failed")
    print(f"sent {len(text)} chars to Telegram")


if __name__ == "__main__":
    main()

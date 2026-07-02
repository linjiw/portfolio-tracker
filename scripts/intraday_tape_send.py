#!/usr/bin/env python3
"""Send a prepared intraday tape Judge message to Telegram."""

import argparse
import csv
import datetime as dt
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output" / "intraday_tape"
PT = dt.timezone(dt.timedelta(hours=-7), name="America/Los_Angeles")


def main():
    ap = argparse.ArgumentParser(description="Send a prepared Telegram message.")
    ap.add_argument("--message-file", default=str(OUT / "telegram_message.html"))
    ap.add_argument("--log-file", default=str(OUT / "dispatch_log.csv"))
    args = ap.parse_args()

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
    log_path.parent.mkdir(parents=True, exist_ok=True)
    first = not log_path.exists()
    with log_path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["sentAt", "ok", "messageFile", "chars"])
        if first:
            writer.writeheader()
        writer.writerow({
            "sentAt": dt.datetime.now(tz=PT).isoformat(timespec="seconds"),
            "ok": ok,
            "messageFile": str(path),
            "chars": len(text),
        })
    if not ok:
        raise SystemExit("telegram send failed")
    print(f"sent {len(text)} chars to Telegram")


if __name__ == "__main__":
    main()

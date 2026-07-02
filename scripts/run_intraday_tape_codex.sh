#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCK_DIR="/tmp/qqq-intraday-codex.lock"
LOG_DIR="$ROOT/output/intraday_tape"
PROMPT="$ROOT/prompts/intraday_tape_judge.md"

mkdir -p "$LOG_DIR"

if ! python3 - <<'PY'
import datetime as dt
import sys
PT = dt.timezone(dt.timedelta(hours=-7))
now = dt.datetime.now(tz=PT)
minute = now.hour * 60 + now.minute
sys.exit(0 if now.weekday() < 5 and (6 * 60 + 30) <= minute <= (13 * 60 + 5) else 1)
PY
then
  echo "$(date -Iseconds) skip outside monitor window" >> "$LOG_DIR/codex_runner.log"
  exit 0
fi

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  echo "$(date -Iseconds) skip previous run still active" >> "$LOG_DIR/codex_runner.log"
  exit 0
fi
trap 'rmdir "$LOCK_DIR"' EXIT

cd "$ROOT"
echo "$(date -Iseconds) codex judge start" >> "$LOG_DIR/codex_runner.log"

/opt/homebrew/bin/codex \
  --dangerously-bypass-approvals-and-sandbox \
  -m gpt-5.5 \
  exec \
  --cd "$ROOT" \
  --output-last-message "$LOG_DIR/codex_last_message.txt" \
  - < "$PROMPT" >> "$LOG_DIR/codex_runner.log" 2>> "$LOG_DIR/codex_runner.err.log"

echo "$(date -Iseconds) codex judge done" >> "$LOG_DIR/codex_runner.log"

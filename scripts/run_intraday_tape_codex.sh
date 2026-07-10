#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$(command -v python3)}"

# Keep a single implementation of the time window, crash-safe lock, restricted
# environment, Codex sandbox, artifact validation, and finalization sequence.
exec "$PYTHON_BIN" "$ROOT/scripts/run_intraday_tape_codex.py" "$@"

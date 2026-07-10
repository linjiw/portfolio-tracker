#!/usr/bin/env python3
"""Run one locked, dependency-ordered portfolio close refresh.

The portfolio project has several producers that read the generated dashboard
and several renderers that embed those producer artifacts.  Running the jobs
independently can therefore leave a fresh price payload containing stale
analysis (or, worse, let a later broker-mode render overwrite fresh marks).
This entrypoint makes the dependency order explicit and records a machine-
readable run manifest.
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import glob
import hashlib
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterable

try:
    from scripts.artifact_io import atomic_write_json
    from scripts.dashboard_payload import read_dashboard_payload as _read_dashboard_payload
except ModuleNotFoundError:
    from artifact_io import atomic_write_json
    from dashboard_payload import read_dashboard_payload as _read_dashboard_payload


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "output"
DASHBOARD = OUTPUT / "portfolio_dashboard.html"
RUN_ROOT = OUTPUT / "refresh_runs"
LATEST_MANIFEST = OUTPUT / "latest_refresh_manifest.json"
LOCK_PATH = OUTPUT / ".portfolio_refresh.lock"
PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600


def private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(PRIVATE_DIR_MODE)


def atomic_json(path: Path, payload: object) -> None:
    atomic_write_json(path, payload)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_payload(path: Path) -> dict:
    return _read_dashboard_payload(path)


def tail(text: str, limit: int = 5000) -> str:
    return text[-limit:] if len(text) > limit else text


def decoded_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


class RefreshRun:
    def __init__(self, args: argparse.Namespace) -> None:
        now = dt.datetime.now().astimezone()
        self.run_id = now.strftime("%Y%m%dT%H%M%S.%f%z")
        self.run_dir = RUN_ROOT / self.run_id
        private_dir(RUN_ROOT)
        self.run_dir.mkdir(parents=True, exist_ok=False, mode=PRIVATE_DIR_MODE)
        self.run_dir.chmod(PRIVATE_DIR_MODE)
        self.log_path = self.run_dir / "refresh.log"
        self.manifest_path = self.run_dir / "manifest.json"
        self.step_timeout_seconds = args.step_timeout_seconds
        redactions = [
            (str(Path(args.portfolio).resolve()), "<portfolio-export>"),
            (str(Path(args.history).resolve()), "<history-export>"),
            (str(Path(args.input_dir).expanduser().resolve()), "<input-dir>"),
            (str(ROOT), "<repo>"),
            (str(Path.home()), "<home>"),
        ]
        seen: set[str] = set()
        self.redactions = []
        for value, label in sorted(redactions, key=lambda item: len(item[0]), reverse=True):
            if value not in seen:
                self.redactions.append((value, label))
                seen.add(value)
        self.manifest = {
            "schemaVersion": 1,
            "runId": self.run_id,
            "status": "running",
            "startedAt": now.isoformat(timespec="seconds"),
            "python": Path(sys.executable).name,
            "asOf": args.as_of,
            "noFetch": args.no_fetch,
            "maxExportAgeHours": args.max_export_age_hours,
            "allowOldExport": args.allow_old_export,
            "inputs": {
                "portfolioSha256": file_sha256(Path(args.portfolio)),
                "portfolioBytes": Path(args.portfolio).stat().st_size,
                "historySha256": file_sha256(Path(args.history)),
                "historyBytes": Path(args.history).stat().st_size,
                "historyMode": args.history_mode,
            },
            "steps": [],
        }
        self.write_manifest()

    def write_manifest(self) -> None:
        atomic_json(self.manifest_path, self.manifest)
        atomic_json(LATEST_MANIFEST, self.manifest)

    def log(self, text: str) -> None:
        text = self.scrub(text)
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(text)
            if not text.endswith("\n"):
                handle.write("\n")
        self.log_path.chmod(PRIVATE_FILE_MODE)

    def scrub(self, text: str) -> str:
        """Remove local input/repository paths before text enters logs or manifests."""
        result = str(text)
        for value, label in self.redactions:
            result = result.replace(value, label)
        return result

    def skip(self, name: str, reason: str) -> None:
        stamp = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        self.manifest["steps"].append({
            "name": name, "status": "skipped", "reason": reason,
            "startedAt": stamp, "finishedAt": stamp, "durationSeconds": 0,
        })
        self.write_manifest()
        print(f"↷ {name}: skipped ({reason})", flush=True)

    def command(self, name: str, command: Iterable[str], *, required: bool = True) -> bool:
        cmd = [str(part) for part in command]
        safe_cmd = [self.scrub(part) for part in cmd]
        started = dt.datetime.now().astimezone()
        start_clock = time.monotonic()
        print(f"▶ {name}", flush=True)
        self.log(f"\n=== {name} · {started.isoformat(timespec='seconds')} ===\n$ {shlex.join(safe_cmd)}\n")
        timed_out = False
        try:
            proc = subprocess.run(
                cmd, cwd=ROOT, text=True, capture_output=True,
                timeout=self.step_timeout_seconds,
            )
            stdout, stderr, return_code = proc.stdout, proc.stderr, proc.returncode
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            stdout = decoded_text(exc.stdout)
            stderr = decoded_text(exc.stderr) + (
                f"\nstep exceeded timeout of {self.step_timeout_seconds:g} seconds"
            )
            return_code = 124
        elapsed = round(time.monotonic() - start_clock, 3)
        self.log(stdout)
        if stderr:
            self.log("\n[stderr]\n" + stderr)
        step = {
            "name": name,
            "status": "ok" if return_code == 0 else "failed",
            "required": required,
            "returnCode": return_code,
            "timedOut": timed_out,
            "startedAt": started.isoformat(timespec="seconds"),
            "finishedAt": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "durationSeconds": elapsed,
            "command": safe_cmd,
            "stdoutTail": tail(self.scrub(stdout)),
            "stderrTail": tail(self.scrub(stderr)),
        }
        self.manifest["steps"].append(step)
        self.write_manifest()
        if return_code:
            print(f"✗ {name}: {'timeout' if timed_out else 'exit ' + str(return_code)}", flush=True)
            if required:
                why = "timed out" if timed_out else "failed"
                raise RuntimeError(f"required step {why}: {name}; see {self.log_path}")
            return False
        print(f"✓ {name} ({elapsed:.1f}s)", flush=True)
        return True

    def inspect_dashboard(self, staged: Path, label: str, *, require_fresh: bool) -> dict:
        payload = read_payload(staged)
        summary = payload.get("summary") or {}
        if require_fresh:
            stale = summary.get("stalePriceSymbols") or {}
            missing = summary.get("missingPriceSymbols") or []
            if summary.get("priceMode") != "mark-to-market":
                raise RuntimeError(f"{label}: expected mark-to-market dashboard")
            if (stale or missing) and not self.manifest.get("allowStalePrices"):
                raise RuntimeError(f"{label}: stale={sorted(stale)} missing={missing}")
        self.manifest.setdefault("validatedDashboards", []).append({
            "label": label,
            "validatedAt": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "priceMode": summary.get("priceMode"),
            "priceAsOf": summary.get("priceAsOf"),
            "marketValue": summary.get("marketValue"),
            "numHeld": summary.get("numHeld"),
            "freshPriceCount": len(summary.get("freshPriceSymbols") or []),
            "stalePriceSymbols": summary.get("stalePriceSymbols") or {},
            "missingPriceSymbols": summary.get("missingPriceSymbols") or [],
            "sha256": file_sha256(staged),
        })
        self.write_manifest()
        return payload

    def publish_dashboard(self, staged: Path, label: str, *, require_fresh: bool) -> dict:
        payload = self.inspect_dashboard(staged, label, require_fresh=require_fresh)
        os.replace(staged, DASHBOARD)
        DASHBOARD.chmod(PRIVATE_FILE_MODE)
        self.manifest.setdefault("publishedDashboards", []).append({
            "label": label,
            "publishedAt": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "sha256": file_sha256(DASHBOARD),
        })
        self.write_manifest()
        return payload


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Run a locked full portfolio intelligence refresh.")
    ap.add_argument("--input-dir", default=str(Path.home() / "Downloads"))
    ap.add_argument("--portfolio", help="exact broker Portfolio Positions CSV; defaults to newest in --input-dir")
    ap.add_argument("--history", help="newest Account History CSV; defaults to newest in --input-dir")
    ap.add_argument("--history-mode", choices=("cumulative", "exact"), default="cumulative")
    ap.add_argument("--as-of", default=dt.date.today().isoformat())
    ap.add_argument("--no-fetch", action="store_true", help="use existing caches and skip live-only producers")
    ap.add_argument("--allow-stale-prices", action="store_true",
                    help="publish even when a held symbol lacks an exact --as-of close")
    ap.add_argument("--max-export-age-hours", type=float, default=120.0,
                    help="maximum broker position-snapshot age (default: 120 hours)")
    ap.add_argument("--allow-old-export", action="store_true",
                    help="explicit override for a historical/old position snapshot")
    ap.add_argument("--news", action="store_true",
                    help="include the sentinel's network news scan (off by default for a reproducible close run)")
    ap.add_argument("--telegram", action="store_true", help="send the final daily brief through configured Telegram")
    ap.add_argument("--step-timeout-seconds", type=float, default=1800.0,
                    help="terminate a wedged producer after this many seconds (default: 1800)")
    return ap


def newest(input_dir: str, patterns: Iterable[str]) -> str | None:
    matches: list[str] = []
    for pattern in patterns:
        matches.extend(glob.glob(str(Path(input_dir).expanduser() / pattern)))
    return max(matches, key=os.path.getmtime) if matches else None


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    args = parser().parse_args(argv)
    if args.step_timeout_seconds <= 0:
        raise SystemExit("!! --step-timeout-seconds must be positive")
    if args.max_export_age_hours <= 0:
        raise SystemExit("!! --max-export-age-hours must be positive")
    try:
        as_of_date = dt.date.fromisoformat(args.as_of)
    except ValueError as exc:
        raise SystemExit("!! --as-of must be YYYY-MM-DD") from exc
    if as_of_date > dt.date.today():
        raise SystemExit(f"!! --as-of {as_of_date} is in the future")
    args.portfolio = args.portfolio or newest(args.input_dir, ["Portfolio_Positions*.csv"])
    args.history = args.history or newest(args.input_dir, ["Accounts_History*.csv", "History_for_Account*.csv"])
    for label in ("portfolio", "history"):
        if not getattr(args, label):
            raise SystemExit(f"!! no {label} export found in {args.input_dir}; pass --{label}")
        path = Path(getattr(args, label)).expanduser()
        if not path.is_file():
            raise SystemExit(f"!! {label} file not found: {path}")
        setattr(args, label, str(path.resolve()))

    private_dir(OUTPUT)
    lock_handle = LOCK_PATH.open("a+")
    LOCK_PATH.chmod(PRIVATE_FILE_MODE)
    try:
        fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit(f"!! another portfolio refresh holds {LOCK_PATH}")

    run = RefreshRun(args)
    run.manifest["allowStalePrices"] = args.allow_stale_prices
    run.write_manifest()
    python = sys.executable
    common = ["--input-dir", args.input_dir, "--portfolio", args.portfolio, "--history", args.history]
    if args.no_fetch:
        fetch_flag = ["--no-fetch"]
    else:
        fetch_flag = []

    broker_stage = OUTPUT / f".portfolio_dashboard.broker.{os.getpid()}.html"
    mtm_stage = OUTPUT / f".portfolio_dashboard.mtm.{os.getpid()}.html"
    final_stage = OUTPUT / f".portfolio_dashboard.final.{os.getpid()}.html"
    try:
        run.command("broker_sync_and_gate", [
            python, ROOT / "sync.py", *common,
            "--history-mode", args.history_mode,
            "--max-export-age-hours", str(args.max_export_age_hours),
            *(["--allow-old-export"] if args.allow_old_export else []),
            "--out", broker_stage,
            "--momentum-strategy", "off", "--financial-status", "off",
            *fetch_flag,
        ])
        # Keep broker marks private to the run.  The canonical dashboard remains
        # last-known-good until a complete mark-to-market render passes every
        # required freshness and broker-fingerprint gate.
        run.inspect_dashboard(broker_stage, "broker_verified", require_fresh=False)

        run.command("initial_mark_to_market", [
            python, ROOT / "scripts" / "refresh_latest_prices.py", *common,
            "--out", mtm_stage, "--as-of", args.as_of,
            "--history-mode", args.history_mode,
            "--max-export-age-hours", str(args.max_export_age_hours),
            *(["--allow-old-export"] if args.allow_old_export else []),
            "--momentum-strategy", "off", "--trigger", "orchestrator-initial",
            *(["--allow-stale-prices"] if args.allow_stale_prices else []),
            *fetch_flag,
        ])
        run.inspect_dashboard(mtm_stage, "initial_mark_to_market", require_fresh=True)
        analysis_dashboard = mtm_stage

        run.command("financial_status", [
            python, ROOT / "scripts" / "financial_status_score.py",
            "--dashboard", analysis_dashboard, "--as-of", args.as_of,
            *(["--no-fetch"] if args.no_fetch else ["--refresh-cache"]),
        ], required=False)
        for name, script in (("ai_semi_quant", "ai_semi_quant.py"),
                             ("ai_watchlist", "ai_watchlist_score.py"),
                             ("aics", "aics_tool.py")):
            if args.no_fetch:
                run.skip(name, "structural-only mode would overwrite the last price-bearing artifact")
            else:
                run.command(name, [python, ROOT / "scripts" / script, "--dashboard", analysis_dashboard], required=False)
        run.command("momentum_top3", [python, ROOT / "scripts" / "momentum_top3.py", *fetch_flag], required=False)
        run.command("memory_flow", [
            python, ROOT / "scripts" / "memory_flow.py", "--as-of", args.as_of, *fetch_flag,
        ], required=False)

        live_only = [
            ("market_mass", [python, ROOT / "scripts" / "market_mass_dashboard.py",
                             "--input-dir", args.input_dir, "--portfolio", args.portfolio]),
            ("usd_liquidity", [python, ROOT / "scripts" / "usd_liquidity_score.py", "--as-of", args.as_of]),
            ("decision_analysis", [python, ROOT / "scripts" / "decision_analysis.py", "--dashboard", analysis_dashboard]),
            ("close_vs_intraday", [python, ROOT / "scripts" / "close_vs_intraday.py",
                                    "--dashboard", analysis_dashboard, "--as-of", args.as_of]),
        ]
        for name, cmd in live_only:
            if args.no_fetch:
                run.skip(name, "live-only producer in --no-fetch mode")
            else:
                run.command(name, cmd, required=False)

        run.command("final_cached_render", [
            python, ROOT / "scripts" / "refresh_latest_prices.py", *common,
            "--out", final_stage, "--as-of", args.as_of, "--no-fetch",
            "--history-mode", args.history_mode,
            "--max-export-age-hours", str(args.max_export_age_hours),
            *(["--allow-old-export"] if args.allow_old_export else []),
            "--momentum-strategy", "off", "--trigger", "orchestrator-final",
            *(["--allow-stale-prices"] if args.allow_stale_prices else []),
        ])
        final_payload = run.publish_dashboard(final_stage, "final_cached_mark_to_market", require_fresh=True)

        downstream = [
            ("market_sentinel", [python, ROOT / "scripts" / "market_sentinel.py",
                                 "--dashboard", DASHBOARD, *([] if args.news else ["--no-news"])]),
            ("spmo_momentum", [python, ROOT / "scripts" / "spmo_momentum_sleeve.py", "--dashboard", DASHBOARD]),
            ("position_signals", [python, ROOT / "scripts" / "generate_position_signal_report.py", "--dashboard", DASHBOARD]),
            ("trend_execution", [python, ROOT / "scripts" / "generate_trend_execution_plan.py", "--dashboard", DASHBOARD,
                                 *(["--no-fetch"] if args.no_fetch else [])]),
            ("daily_brief", [python, ROOT / "scripts" / "daily_portfolio_brief.py",
                             *(["--telegram"] if args.telegram else [])]),
        ]
        for name, cmd in downstream:
            if args.no_fetch and name in {"market_sentinel", "spmo_momentum"}:
                run.skip(name, "producer has no complete offline mode")
            else:
                run.command(name, cmd, required=False)

        failures = [step["name"] for step in run.manifest["steps"] if step["status"] == "failed"]
        summary = final_payload.get("summary") or {}
        run.manifest["status"] = "complete_with_warnings" if failures else "complete"
        run.manifest["warnings"] = failures
        run.manifest["finishedAt"] = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        run.manifest["final"] = {
            "dashboard": "output/portfolio_dashboard.html",
            "priceMode": summary.get("priceMode"),
            "priceAsOf": summary.get("priceAsOf"),
            "marketValue": summary.get("marketValue"),
            "unrealized": summary.get("unrealized"),
            "accountNetWorth": summary.get("accountNetWorth"),
            "numHeld": summary.get("numHeld"),
        }
        run.write_manifest()
        print(f"\n✓ refresh {run.manifest['status']}: {DASHBOARD}")
        print(run.manifest_path)
        return 0 if not failures else 2
    except Exception as exc:
        run.manifest["status"] = "failed"
        run.manifest["finishedAt"] = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        run.manifest["error"] = run.scrub(str(exc))
        run.write_manifest()
        print(f"\n✗ refresh failed: {exc}", file=sys.stderr)
        print(run.manifest_path, file=sys.stderr)
        return 1
    finally:
        for staged in (broker_stage, mtm_stage, final_stage):
            try:
                staged.unlink()
            except FileNotFoundError:
                pass
        fcntl.flock(lock_handle, fcntl.LOCK_UN)
        lock_handle.close()


if __name__ == "__main__":
    raise SystemExit(main())

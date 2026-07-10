"""Security and artifact-contract tests for the unattended Codex Judge."""

import datetime as dt
import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "run_intraday_tape_codex.py"
SPEC = importlib.util.spec_from_file_location("run_intraday_tape_codex", SCRIPT)
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


def test_codex_command_is_bounded_ephemeral_and_offline(tmp_path):
    command = runner.build_codex_command("/usr/local/bin/codex", tmp_path)
    joined = " ".join(command)
    assert "dangerously-bypass" not in joined
    assert command[command.index("--sandbox") + 1] == "workspace-write"
    assert command[command.index("--ask-for-approval") + 1] == "never"
    assert "sandbox_workspace_write.network_access=false" in command
    assert "--ephemeral" in command
    assert "--ignore-user-config" in command
    assert command[command.index("--cd") + 1] == str(tmp_path)


def test_minimal_environment_does_not_inherit_credentials():
    source = {
        "HOME": "/Users/example",
        "LANG": "en_US.UTF-8",
        "FMP_API_KEY": "portfolio-secret",
        "TELEGRAM_BOT_TOKEN": "telegram-secret",
        "AWS_SECRET_ACCESS_KEY": "cloud-secret",
    }
    env = runner.minimal_environment(source)
    assert env["HOME"] == "/Users/example"
    assert env["PATH"] == runner.SAFE_PATH
    assert "FMP_API_KEY" not in env
    assert "TELEGRAM_BOT_TOKEN" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env


def test_monitor_window_uses_dst_aware_pacific_time():
    winter = dt.datetime(2026, 1, 9, 7, 0, tzinfo=runner.PT)
    summer = dt.datetime(2026, 7, 9, 7, 0, tzinfo=runner.PT)
    assert winter.utcoffset() == dt.timedelta(hours=-8)
    assert summer.utcoffset() == dt.timedelta(hours=-7)
    assert runner.in_window(winter)
    assert runner.in_window(summer)


def test_monitor_window_respects_us_holiday_and_early_close():
    holiday = dt.datetime(2026, 11, 26, 7, 0, tzinfo=runner.PT)
    early_close_grace_end = dt.datetime(2026, 11, 27, 10, 5, tzinfo=runner.PT)
    after_early_close = dt.datetime(2026, 11, 27, 10, 6, tzinfo=runner.PT)
    assert not runner.in_window(holiday)
    assert runner.in_window(early_close_grace_end)
    assert not runner.in_window(after_early_close)


def test_judge_outputs_are_schema_validated(tmp_path):
    (tmp_path / "telegram_message.html").write_text("QQQ WATCH\nNot financial advice", encoding="utf-8")
    diff = {
        "run_id": "run-1",
        "verdict_tuple": ["WATCH", "base", "waiting", "hold"],
        "scenarios": [],
        "verdict_history": [{"t": "2026-07-09T12:00:00-04:00", "verdict": "WATCH", "conviction": "medium"}],
        "burden_of_proof": "bulls",
        "trigger_status": "waiting",
        "position_instruction": "hold",
    }
    (tmp_path / "state_diff.json").write_text(json.dumps(diff), encoding="utf-8")
    telegram, state_diff = runner.validate_judge_outputs(tmp_path)
    assert telegram.name == "telegram_message.html"
    assert state_diff.name == "state_diff.json"


def test_judge_outputs_reject_incomplete_diff(tmp_path):
    (tmp_path / "telegram_message.html").write_text("QQQ WATCH\nNot financial advice", encoding="utf-8")
    (tmp_path / "state_diff.json").write_text('{"verdict_tuple": []}', encoding="utf-8")
    with pytest.raises(ValueError, match="missing required keys"):
        runner.validate_judge_outputs(tmp_path)


def test_binding_hard_gate_mechanically_rejects_allow(tmp_path):
    (tmp_path / "telegram_message.html").write_text("QQQ ALLOW\nNot financial advice", encoding="utf-8")
    diff = {
        "run_id": "run-1",
        "verdict_tuple": ["ALLOW", "base", "triggered", "small add"],
        "scenarios": [],
        "verdict_history": [{
            "t": "2026-07-09T12:00:00-04:00", "verdict": "ALLOW", "conviction": "medium",
        }],
        "burden_of_proof": "bulls",
        "trigger_status": "triggered",
        "position_instruction": "small add",
    }
    (tmp_path / "state_diff.json").write_text(json.dumps(diff), encoding="utf-8")
    gates = tmp_path / "gates.json"
    gates.write_text(json.dumps({
        "schemaVersion": 1,
        "run_id": "run-1",
        "prohibit_allow": True,
        "action_lock": [],
        "score_cap": "观察",
    }), encoding="utf-8")

    with pytest.raises(ValueError, match="prohibit an ALLOW"):
        runner.validate_judge_outputs(tmp_path, gates_path=gates)


def test_block_data_gate_requires_matching_verdict_and_run_id(tmp_path):
    (tmp_path / "telegram_message.html").write_text("QQQ WATCH\nNot financial advice", encoding="utf-8")
    diff = {
        "run_id": "old-run",
        "verdict_tuple": ["WATCH", "none", "blocked", "manage only"],
        "scenarios": [],
        "verdict_history": [{
            "t": "2026-07-09T12:00:00-04:00", "verdict": "WATCH", "conviction": "high",
        }],
        "burden_of_proof": "data",
        "trigger_status": "blocked",
        "position_instruction": "manage only",
    }
    (tmp_path / "state_diff.json").write_text(json.dumps(diff), encoding="utf-8")
    gates = tmp_path / "gates.json"
    gates.write_text(json.dumps({
        "schemaVersion": 1,
        "run_id": "current-run",
        "prohibit_allow": True,
        "action_lock": ["no new entries"],
        "score_cap": "BLOCK_DATA",
    }), encoding="utf-8")

    with pytest.raises(ValueError, match="run_id"):
        runner.validate_judge_outputs(tmp_path, gates_path=gates)


def test_shell_runner_cannot_bypass_sandbox():
    shell = (ROOT / "scripts" / "run_intraday_tape_codex.sh").read_text(encoding="utf-8")
    assert "dangerously-bypass" not in shell
    assert "run_intraday_tape_codex.py" in shell

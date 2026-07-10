import datetime as dt
import json
import tempfile
from pathlib import Path

import pytest
import pandas as pd

from scripts import intraday_tape_finalize as finalize
from scripts import intraday_tape_send as send
from scripts import qqq_intraday_monitor as legacy_monitor


def test_intraday_dispatch_timezones_follow_dst():
    winter = dt.datetime(2026, 1, 9, 12, tzinfo=dt.timezone.utc)
    summer = dt.datetime(2026, 7, 9, 12, tzinfo=dt.timezone.utc)
    assert winter.astimezone(finalize.PT).utcoffset() == dt.timedelta(hours=-8)
    assert summer.astimezone(finalize.PT).utcoffset() == dt.timedelta(hours=-7)
    assert winter.astimezone(send.PT).utcoffset() == dt.timedelta(hours=-8)
    assert winter.astimezone(legacy_monitor.ET).utcoffset() == dt.timedelta(hours=-5)
    assert summer.astimezone(legacy_monitor.ET).utcoffset() == dt.timedelta(hours=-4)


def test_invalid_existing_state_fails_closed_and_atomic_write_is_private():
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "state.json"
        path.write_text("not-json", encoding="utf-8")
        with pytest.raises(json.JSONDecodeError):
            finalize.load_json(path, {})

        finalize.write_json(path, {"ok": True})
        assert finalize.load_json(path, {}) == {"ok": True}
        assert path.stat().st_mode & 0o777 == 0o600


def test_finalizer_rechecks_sensor_bundle_and_hard_gate():
    state = {"sensor_run_id": "run-1"}
    gates = {
        "schemaVersion": 1,
        "run_id": "run-1", "prohibit_allow": True,
        "action_lock": [], "score_cap": "观察",
    }
    diff = {
        "run_id": "run-1",
        "verdict_tuple": ["ALLOW", "base", "triggered", "add"],
        "scenarios": [],
        "verdict_history": [{"t": "now", "verdict": "ALLOW", "conviction": "medium"}],
        "burden_of_proof": "bulls",
        "trigger_status": "triggered",
        "position_instruction": "add",
    }
    with pytest.raises(ValueError, match="prohibit ALLOW"):
        finalize.validate_diff(diff, state, gates)

    diff["run_id"] = "old-run"
    diff["verdict_tuple"][0] = "WATCH"
    diff["verdict_history"][-1]["verdict"] = "WATCH"
    with pytest.raises(ValueError, match="active sensor bundle"):
        finalize.validate_diff(diff, state, gates)


def test_legacy_monitor_excludes_forming_provider_candle():
    index = pd.DatetimeIndex([
        "2026-07-09T11:55:00-04:00",
        "2026-07-09T12:00:00-04:00",
    ])
    frame = pd.DataFrame({
        "Open": [100, 101], "High": [101, 102], "Low": [99, 100],
        "Close": [100.5, 101.5], "Volume": [1000, 500],
    }, index=index)
    now = dt.datetime(2026, 7, 9, 12, 3, tzinfo=legacy_monitor.ET)

    closed = legacy_monitor.closed_dataframe(frame, "5m", now=now)

    assert len(closed) == 1
    assert closed.index[-1].isoformat().startswith("2026-07-09T11:55:00")


def test_state_diff_can_explicitly_clear_closed_positions():
    state = {"positions": [{"symbol": "QQQ"}]}
    updated = finalize.apply_diff(state, {"positions": []})
    assert updated["positions"] == []


def test_finalizer_rejects_message_and_diff_verdict_mismatch():
    with pytest.raises(ValueError, match="does not match"):
        finalize.validate_message("QQQ ALLOW\nNot financial advice", "WATCH")
    with pytest.raises(ValueError, match="disclaimer"):
        finalize.validate_message("QQQ WATCH", "WATCH")

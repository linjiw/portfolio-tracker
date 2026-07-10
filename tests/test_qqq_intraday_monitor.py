import importlib.util
import datetime as dt
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock


SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "monitor_qqq_intraday.py"
SPEC = importlib.util.spec_from_file_location("qqq_intraday_monitor", SCRIPT)
monitor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(monitor)

INSTALLER_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "install_qqq_intraday_monitor_launchd.py"
INSTALLER_SPEC = importlib.util.spec_from_file_location("install_qqq_intraday_monitor_launchd", INSTALLER_SCRIPT)
installer = importlib.util.module_from_spec(INSTALLER_SPEC)
INSTALLER_SPEC.loader.exec_module(installer)


def rows_from_closes(closes):
    rows = []
    base = dt.datetime(2026, 6, 5, 10, 0, tzinfo=monitor.ET)
    for i, close in enumerate(closes):
        rows.append({
            "time": (base + dt.timedelta(minutes=i)).isoformat(timespec="minutes"),
            "open": close - 0.2,
            "high": close + 0.3,
            "low": close - 0.4,
            "close": close,
            "volume": 1000 + i,
        })
    return monitor.add_intraday_features(rows)


def with_prior_volume_sessions(rows, dates=("2026-06-02", "2026-06-03", "2026-06-04")):
    history = []
    for date in dates:
        history.extend([{**row, "time": row["time"].replace("2026-06-05", date)} for row in rows])
    return history + rows


class QqqIntradayMonitorTests(unittest.TestCase):
    def test_symbol_component_rejects_output_path_traversal(self):
        self.assertEqual(monitor.safe_symbol_component("qqq"), "QQQ")
        for symbol in ("../QQQ", "QQQ/../../private", "", "."):
            with self.subTest(symbol=symbol), self.assertRaises(ValueError):
                monitor.safe_symbol_component(symbol)

    def test_ema_values_are_standard_exponential_average(self):
        vals = monitor.ema_values([100, 102, 104], 3)
        self.assertEqual(vals[0], 100)
        self.assertAlmostEqual(vals[1], 101)
        self.assertAlmostEqual(vals[2], 102.5)

    def test_flat_series_rsi_is_neutral_not_overbought(self):
        values = monitor.rsi_values([100.0] * 20, 14)
        self.assertEqual(values[14], 50.0)
        self.assertEqual(values[-1], 50.0)
        self.assertIsNone(monitor._to_float(float("inf")))

    def test_45m_ema_slope_uses_three_full_15m_intervals(self):
        rows = rows_from_closes([100, 101, 102, 103, 104])
        summary = monitor.summarize_interval(rows, "15m")

        self.assertAlmostEqual(
            summary["ema8Slope45m"],
            round(rows[-1]["ema8"] - rows[-4]["ema8"], 2),
        )
        short = monitor.summarize_interval(rows[:3], "15m")
        self.assertIsNone(short["ema8Slope45m"])

    def test_volume_ratio_uses_same_time_of_day_not_arbitrary_recent_bars(self):
        rows = []
        for date, slot_volume in (("2026-06-02", 100), ("2026-06-03", 200), ("2026-06-04", 300)):
            rows += [
                {"time": f"{date}T10:00:00-04:00", "volume": 50},
                {"time": f"{date}T10:15:00-04:00", "volume": slot_volume},
            ]
        session = [
            {"time": "2026-06-05T10:00:00-04:00", "volume": 50},
            {"time": "2026-06-05T10:15:00-04:00", "volume": 400},
        ]
        volume = monitor.volume_context(rows + session, session, "15m")

        self.assertTrue(volume["volumeBaselineSufficient"])
        self.assertEqual(volume["vma20"], 200)
        self.assertEqual(volume["volumeRatio"], 2.0)
        self.assertEqual(volume["cumulativeVolumeRatio"], 1.8)

    def test_stability_signal_allows_when_multitimeframe_reclaims(self):
        rising = [100 + i * 0.08 for i in range(120)]
        intraday = {
            "1m": with_prior_volume_sessions(rows_from_closes(rising)),
            "5m": with_prior_volume_sessions(rows_from_closes(rising[::5])),
            "15m": with_prior_volume_sessions(rows_from_closes(rising[::15] + [rising[-1]])),
            "30m": with_prior_volume_sessions(rows_from_closes([rising[0], rising[30], rising[-1]])),
        }
        daily = {"available": True, "ema21": 102.0}

        signal = monitor.evaluate_signal(intraday, daily)

        self.assertEqual(signal["label"], "ALLOW")
        self.assertGreaterEqual(signal["score"], 6)
        self.assertFalse(signal["tradeActionAuthorized"])
        self.assertEqual(signal["decisionScope"], "closed_bar_tape_only")
        self.assertNotIn("ALLOW", {plan["status"] for plan in signal["spreadPlan"]})
        self.assertIn("chain not loaded", signal["spreadPlan"][0]["structure"])

    def test_daily_ema21_break_blocks_even_if_short_term_bounces(self):
        rising = [100 + i * 0.1 for i in range(60)]
        intraday = {
            "1m": rows_from_closes(rising),
            "5m": rows_from_closes(rising[::5]),
            "15m": rows_from_closes(rising[::15] + [rising[-1]]),
            "30m": rows_from_closes([rising[0], rising[30], rising[-1]]),
        }
        daily = {"available": True, "ema21": 120.0}

        signal = monitor.evaluate_signal(intraday, daily)

        self.assertEqual(signal["label"], "BLOCK")

    def test_missing_volume_and_structure_inputs_never_earn_positive_checks(self):
        rising = [100 + i * 0.1 for i in range(60)]
        intraday = {
            "1m": rows_from_closes(rising),
            "5m": rows_from_closes(rising[::5]),
            "15m": rows_from_closes(rising[::30]),
            "30m": [],
        }

        signal = monitor.evaluate_signal(intraday, {"available": True, "ema21": 99.0})
        checks = {item["name"]: item["ok"] for item in signal["checks"]}

        self.assertNotEqual(signal["label"], "ALLOW")
        self.assertIsNone(checks["15m EMA8 斜率转正"])
        self.assertIsNone(checks["30m 收盘不在低位"])
        self.assertIsNone(checks["5m 量价不再下移"])

    def test_missing_completed_daily_weather_is_block_data(self):
        rising = [100 + i * 0.1 for i in range(60)]
        intraday = {
            "1m": rows_from_closes(rising),
            "5m": rows_from_closes(rising[::5]),
            "15m": rows_from_closes(rising[::15]),
            "30m": rows_from_closes([100, 102, 104]),
        }
        signal = monitor.evaluate_signal(intraday, {"available": False})
        self.assertEqual(signal["label"], "BLOCK_DATA")

    def test_forming_daily_candle_is_not_used_for_weather(self):
        now = dt.datetime(2026, 7, 9, 15, 0, tzinfo=monitor.ET)
        rows = [
            {"time": "2026-07-08T00:00:00-04:00"},
            {"time": "2026-07-09T00:00:00-04:00"},
        ]
        self.assertEqual(
            [row["time"] for row in monitor.completed_daily_rows(rows, now=now)],
            ["2026-07-08T00:00:00-04:00"],
        )

    def test_telegram_message_explains_center_of_gravity_inputs(self):
        rising = [100 + i * 0.15 for i in range(60)]
        intraday = {
            "1m": rows_from_closes(rising),
            "5m": rows_from_closes(rising[::5]),
            "15m": rows_from_closes(rising[::15] + [rising[-1]]),
            "30m": rows_from_closes([rising[0], rising[30], rising[-1]]),
        }
        daily = {"available": True, "ema21": 102.0, "ema8": 103.0, "atr14": 2.5, "rsi14": 55.0}
        snapshot = {
            "pulledAt": "2026-06-05T10:45:00-07:00",
            "daily": daily,
            "signal": monitor.evaluate_signal(intraday, daily),
        }

        msg = monitor.format_telegram_message(snapshot)

        self.assertIn("重心定义", msg)
        self.assertIn("5m", msg)
        self.assertIn("15m", msg)
        self.assertIn("Teacher action", msg)

    def test_intraday_launchd_plist_does_not_store_telegram_token(self):
        class Args:
            symbol = "QQQ"
            poll_seconds = 900
            hour = 6
            minute = 31
            telegram = True
            telegram_token_env = "TELEGRAM_BOT_TOKEN"
            telegram_chat_id_env = "TELEGRAM_CHAT_ID"

        old_token = os.environ.get("TELEGRAM_BOT_TOKEN")
        old_chat_id = os.environ.get("TELEGRAM_CHAT_ID")
        os.environ["TELEGRAM_BOT_TOKEN"] = "secret-token-must-not-enter-plist"
        os.environ["TELEGRAM_CHAT_ID"] = "123456"
        try:
            plist = installer.build_plist(Args(), "/usr/bin/python3")
        finally:
            if old_token is None:
                os.environ.pop("TELEGRAM_BOT_TOKEN", None)
            else:
                os.environ["TELEGRAM_BOT_TOKEN"] = old_token
            if old_chat_id is None:
                os.environ.pop("TELEGRAM_CHAT_ID", None)
            else:
                os.environ["TELEGRAM_CHAT_ID"] = old_chat_id

        dumped = json.dumps(plist, ensure_ascii=False)
        self.assertIn("--telegram", plist["ProgramArguments"])
        self.assertNotIn("TELEGRAM_BOT_TOKEN", dumped)
        self.assertNotIn("TELEGRAM_CHAT_ID", dumped)
        self.assertNotIn("secret-token-must-not-enter-plist", dumped)

    def test_market_timezones_follow_dst(self):
        winter = dt.datetime(2026, 1, 9, 12, tzinfo=monitor.ET)
        summer = dt.datetime(2026, 7, 9, 12, tzinfo=monitor.ET)
        self.assertEqual(winter.utcoffset(), dt.timedelta(hours=-5))
        self.assertEqual(summer.utcoffset(), dt.timedelta(hours=-4))
        self.assertEqual(winter.astimezone(monitor.PT).utcoffset(), dt.timedelta(hours=-8))
        self.assertEqual(summer.astimezone(monitor.PT).utcoffset(), dt.timedelta(hours=-7))

    def test_monitor_window_respects_holiday_and_early_close(self):
        holiday = dt.datetime(2026, 11, 26, 10, 0, tzinfo=monitor.ET)
        before_early_close = dt.datetime(2026, 11, 27, 12, 59, tzinfo=monitor.ET)
        after_early_grace = dt.datetime(2026, 11, 27, 13, 6, tzinfo=monitor.ET)
        self.assertFalse(monitor.is_market_monitor_window(holiday))
        self.assertTrue(monitor.is_market_monitor_window(before_early_close))
        self.assertFalse(monitor.is_market_monitor_window(after_early_grace))

    def test_forming_provider_bar_is_excluded(self):
        now = dt.datetime(2026, 7, 9, 12, 3, tzinfo=monitor.ET)
        rows = [
            {"time": "2026-07-09T11:55:00-04:00"},
            {"time": "2026-07-09T12:00:00-04:00"},
        ]
        closed = monitor.closed_interval_rows(rows, "5m", now=now)
        self.assertEqual([r["time"] for r in closed], ["2026-07-09T11:55:00-04:00"])

    def test_freshness_requires_current_et_session(self):
        now = dt.datetime(2026, 7, 9, 12, 0, tzinfo=monitor.ET)
        fresh = {
            "5m": [{"time": "2026-07-09T11:50:00-04:00"}],
            "15m": [{"time": "2026-07-09T11:30:00-04:00"}],
        }
        stale = {
            "5m": [{"time": "2026-07-08T15:50:00-04:00"}],
            "15m": [{"time": "2026-07-08T15:30:00-04:00"}],
        }
        self.assertTrue(monitor.market_data_freshness(fresh, now=now)["fresh"])
        result = monitor.market_data_freshness(stale, now=now)
        self.assertFalse(result["fresh"])
        self.assertEqual(result["intervals"]["5m"]["reason"], "different_et_session_date")

    def test_freshness_rejects_one_missing_full_15m_close(self):
        now = dt.datetime(2026, 7, 9, 12, 15, tzinfo=monitor.ET)
        intraday = {
            "5m": [{"time": "2026-07-09T12:00:00-04:00"}],
            "15m": [{"time": "2026-07-09T11:30:00-04:00"}],
        }
        result = monitor.market_data_freshness(intraday, now=now)
        self.assertFalse(result["fresh"])
        self.assertEqual(result["intervals"]["15m"]["reason"], "bar_stale")

    def test_single_run_failure_returns_nonzero(self):
        with (mock.patch.object(sys, "argv", [str(SCRIPT)]),
              mock.patch.object(monitor, "run_once", side_effect=RuntimeError("feed down")),
              mock.patch.object(monitor, "append_telegram_log")):
            self.assertEqual(monitor.main(), 1)

    def test_empty_feed_replaces_stale_csv_with_header_only(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "QQQ_5m.csv"
            path.write_text("stale-data\n", encoding="utf-8")
            monitor.write_csv(path, [])
            lines = path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("time,open,high,low,close"))

    def test_level_test_counts_reset_for_new_session(self):
        with tempfile.TemporaryDirectory() as td:
            out = pathlib.Path(td)
            (out / monitor.STATE_FILE_NAME).write_text(json.dumps({
                "schemaVersion": 1,
                "sessionDate": "2026-07-08",
                "levels": {"700.0": {"tests": 9}},
                "lastLabel": "ALLOW",
            }), encoding="utf-8")
            state = monitor.load_state(out, session_date="2026-07-09")
        self.assertEqual(state["levels"], {})
        self.assertIsNone(state["lastLabel"])
        self.assertEqual(state["sessionDate"], "2026-07-09")

    def test_missing_event_calendar_forces_standalone_monitor_block_data(self):
        now = dt.datetime(2026, 7, 9, 12, 0, tzinfo=monitor.ET)
        signal = {
            "label": "ALLOW", "action": "add", "teacherRead": "allow",
            "dataQuality": {"decisionGrade": True, "confirmationGaps": []},
            "summaries": {
                "5m": {"available": True, "lastTime": "2026-07-09T11:50:00-04:00",
                       "volumeRatio": 1.2, "cumulativeVolumeRatio": 1.1,
                       "volumeBaselineSufficient": True, "volumeBaselineSampleDays": 3,
                       "extensionAtr20": 0.5, "moveDirection": "up"},
                "15m": {"available": True, "lastTime": "2026-07-09T11:30:00-04:00",
                        "volumeRatio": 1.2, "cumulativeVolumeRatio": 1.1,
                        "volumeBaselineSufficient": True, "volumeBaselineSampleDays": 3},
            },
            "spreadPlan": [{"status": "ALLOW", "rule": "candidate"}],
            "scenarios": [{"tactic": "enter"}],
        }
        freshness = {"fresh": True, "reason": None, "intervals": {}}
        with tempfile.TemporaryDirectory() as td:
            missing = pathlib.Path(td) / "missing-events.json"
            gates = monitor.build_hard_gates(signal, freshness, now, events_file=missing)
        result = monitor.enforce_hard_gates(signal, gates)

        self.assertEqual(result["label"], "BLOCK_DATA")
        self.assertIn("G2_DATA", [item["gate"] for item in gates["triggered"]])
        self.assertNotEqual(result["spreadPlan"][0]["status"], "ALLOW")

    def test_time_and_chase_gates_cap_every_allow_candidate(self):
        now = dt.datetime(2026, 7, 9, 15, 0, tzinfo=monitor.ET)
        signal = {
            "label": "ALLOW", "action": "add", "teacherRead": "allow",
            "dataQuality": {"decisionGrade": True, "confirmationGaps": []},
            "summaries": {
                "5m": {"available": True, "lastTime": "2026-07-09T14:50:00-04:00",
                       "volumeRatio": 1.2, "cumulativeVolumeRatio": 1.1,
                       "volumeBaselineSufficient": True, "volumeBaselineSampleDays": 3,
                       "extensionAtr20": 2.0, "moveDirection": "up"},
                "15m": {"available": True, "lastTime": "2026-07-09T14:30:00-04:00",
                        "volumeRatio": 1.2, "cumulativeVolumeRatio": 1.1,
                        "volumeBaselineSufficient": True, "volumeBaselineSampleDays": 3},
            },
            "spreadPlan": [{"status": "ALLOW", "rule": "candidate"}],
            "scenarios": [{"tactic": "enter"}],
        }
        freshness = {"fresh": True, "reason": None, "intervals": {}}
        with tempfile.TemporaryDirectory() as td:
            events = pathlib.Path(td) / "events.json"
            events.write_text(json.dumps({
                "schemaVersion": 1,
                "generatedAt": "2026-07-09T08:00:00-04:00",
                "validThrough": "2026-07-10",
                "events": [],
            }), encoding="utf-8")
            gates = monitor.build_hard_gates(signal, freshness, now, events_file=events)
        result = monitor.enforce_hard_gates(signal, gates)

        self.assertEqual(result["label"], "WATCH")
        self.assertTrue({"G1", "G5"}.issubset({item["gate"] for item in gates["triggered"]}))
        self.assertEqual(result["spreadPlan"][0]["status"], "WATCH")
        self.assertIn("manage/wait", result["spreadPlan"][0]["rule"])


if __name__ == "__main__":
    unittest.main()

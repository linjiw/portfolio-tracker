import datetime as dt
import importlib.util
import json
import pathlib
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "intraday_tape_sensor.py"
SPEC = importlib.util.spec_from_file_location("intraday_tape_sensor", SCRIPT)
sensor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sensor)


def observation(five="2026-07-09T11:50:00-04:00", fifteen="2026-07-09T11:30:00-04:00"):
    return {
        "run_id": "test",
        "intervals": {
            "5m": {
                "available": True,
                "last_closed": {"t": five},
                "volume": {"rvol": 1.2, "cum_rvol": 1.1},
                "extension_atr20": 0.5,
            },
            "15m": {
                "available": True,
                "last_closed": {"t": fifteen},
                "volume": {"rvol": 1.2, "cum_rvol": 1.1},
            },
        },
    }


class IntradayTapeSensorTests(unittest.TestCase):
    def test_zoneinfo_tracks_dst_instead_of_fixed_offsets(self):
        winter = dt.datetime(2026, 1, 15, 12, tzinfo=sensor.ET)
        summer = dt.datetime(2026, 7, 15, 12, tzinfo=sensor.ET)

        self.assertEqual(winter.utcoffset(), dt.timedelta(hours=-5))
        self.assertEqual(summer.utcoffset(), dt.timedelta(hours=-4))

    def test_market_calendar_handles_holidays_dst_and_early_closes(self):
        thanksgiving = sensor.market_session(dt.date(2026, 11, 26))
        early = sensor.market_session(dt.date(2026, 11, 27))
        observed_independence = sensor.market_session(dt.date(2026, 7, 3))
        winter = sensor.market_session(dt.date(2026, 1, 9))
        summer = sensor.market_session(dt.date(2026, 7, 9))

        self.assertFalse(thanksgiving["open"])
        self.assertFalse(observed_independence["open"])
        self.assertTrue(early["earlyClose"])
        self.assertIn("13:00", early["closeAt"])
        self.assertIn("11:30", early["newEntryEnd"])
        self.assertTrue(winter["openAt"].endswith("-05:00"))
        self.assertTrue(summer["openAt"].endswith("-04:00"))

    def test_early_close_uses_close_minus_ninety_minute_entry_cutoff(self):
        now = dt.datetime(2026, 11, 27, 12, 0, tzinfo=sensor.ET)
        obs = observation(
            five="2026-11-27T11:50:00-05:00",
            fifteen="2026-11-27T11:30:00-05:00",
        )
        obs["data_freshness"] = {"fresh": True, "reason": None, "intervals": {}}
        calendar = {
            "available": True, "fresh": True, "events": [],
            "validThrough": "2026-11-27", "eventCount": 0,
        }

        gates = sensor.build_gates(obs, {}, event_calendar=calendar, now=now)

        self.assertIn("G1", [item["gate"] for item in gates["triggered"]])
        self.assertTrue(gates["market_session"]["earlyClose"])
        self.assertIn("不开新仓", gates["action_lock"])

    def test_current_session_closed_bars_are_fresh(self):
        now = dt.datetime(2026, 7, 9, 12, 0, tzinfo=sensor.ET)

        status = sensor.observation_freshness(observation(), now=now)

        self.assertTrue(status["fresh"])

    def test_prior_session_bars_fail_closed(self):
        now = dt.datetime(2026, 7, 9, 12, 0, tzinfo=sensor.ET)

        status = sensor.observation_freshness(
            observation("2026-07-08T15:50:00-04:00", "2026-07-08T15:30:00-04:00"),
            now=now,
        )

        self.assertFalse(status["fresh"])
        self.assertEqual(status["intervals"]["5m"]["reason"], "different_et_session_date")

    def test_missing_a_full_closed_15m_bar_is_stale(self):
        now = dt.datetime(2026, 7, 9, 12, 15, tzinfo=sensor.ET)
        status = sensor.observation_freshness(
            observation("2026-07-09T12:00:00-04:00", "2026-07-09T11:30:00-04:00"),
            now=now,
        )
        self.assertFalse(status["fresh"])
        self.assertEqual(status["intervals"]["15m"]["reason"], "bar_stale")
        self.assertEqual(status["intervals"]["15m"]["maxAgeMinutes"], 20)

    def test_missing_event_calendar_blocks_new_entries(self):
        now = dt.datetime(2026, 7, 9, 12, 0, tzinfo=sensor.ET)
        obs = observation()
        obs["data_freshness"] = sensor.observation_freshness(obs, now=now)

        gates = sensor.build_gates(
            obs,
            {},
            event_calendar={"available": False, "fresh": False,
                            "reason": "event_calendar_missing", "events": []},
            now=now,
        )

        self.assertIn("G2_DATA", [item["gate"] for item in gates["triggered"]])
        self.assertTrue(gates["prohibit_allow"])
        self.assertEqual(gates["score_cap"], "BLOCK_DATA")

    def test_stale_market_data_does_not_emit_tape_dependent_gates(self):
        now = dt.datetime(2026, 7, 9, 12, 0, tzinfo=sensor.ET)
        obs = observation("2026-07-08T15:50:00-04:00", "2026-07-08T15:30:00-04:00")
        obs["intervals"]["5m"]["extension_atr20"] = 3.0
        obs["intervals"]["15m"]["volume"] = {
            "rvol": 0.5, "cum_rvol": 0.5, "sufficient": True, "sample_days": 10,
        }
        obs["data_freshness"] = sensor.observation_freshness(obs, now=now)
        calendar = {
            "available": True, "fresh": True, "events": [],
            "validThrough": "2026-07-10", "eventCount": 0,
        }

        gates = sensor.build_gates(obs, {}, event_calendar=calendar, now=now)
        names = {item["gate"] for item in gates["triggered"]}

        self.assertIn("G0", names)
        self.assertTrue(names.isdisjoint({"G3", "G3_DATA", "G5"}))

    def test_valid_empty_calendar_is_explicitly_safe(self):
        now = dt.datetime(2026, 7, 9, 12, 0, tzinfo=sensor.ET)
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "events.json"
            path.write_text(json.dumps({
                "schemaVersion": 1,
                "generatedAt": "2026-07-09T08:00:00-04:00",
                "validThrough": "2026-07-10",
                "events": [],
            }), encoding="utf-8")
            calendar = sensor.load_event_calendar(path, now=now)

        self.assertTrue(calendar["available"])
        self.assertTrue(calendar["fresh"])
        self.assertEqual(calendar["events"], [])

    def test_timezone_aware_event_activates_g2_window(self):
        now = dt.datetime(2026, 7, 9, 12, 0, tzinfo=sensor.ET)
        obs = observation()
        obs["data_freshness"] = sensor.observation_freshness(obs, now=now)
        calendar = {
            "available": True, "fresh": True, "reason": None,
            "validThrough": "2026-07-10", "eventCount": 1,
            "events": [{"name": "Test event", "t": "2026-07-09T12:30:00-04:00"}],
        }

        gates = sensor.build_gates(obs, {}, event_calendar=calendar, now=now)

        self.assertIn("G2", [item["gate"] for item in gates["triggered"]])
        self.assertTrue(gates["prohibit_allow"])

    def test_off_cycle_event_waits_for_a_fully_post_event_15m_bar(self):
        now = dt.datetime(2026, 7, 9, 10, 26, tzinfo=sensor.ET)
        obs = observation(
            five="2026-07-09T10:20:00-04:00",
            fifteen="2026-07-09T10:00:00-04:00",
        )
        obs["data_freshness"] = {"fresh": True, "reason": None, "intervals": {}}
        calendar = {
            "available": True, "fresh": True, "reason": None,
            "validThrough": "2026-07-10", "eventCount": 1,
            "events": [{"name": "Off-cycle event", "t": "2026-07-09T10:10:00-04:00"}],
        }

        waiting = sensor.build_gates(obs, {}, event_calendar=calendar, now=now)
        g2 = next(item for item in waiting["triggered"] if item["gate"] == "G2")
        self.assertEqual(g2["events"][0]["status"], "awaiting_post_event_closed_15m")

        obs["intervals"]["15m"]["last_closed"]["t"] = "2026-07-09T10:15:00-04:00"
        released = sensor.build_gates(
            obs, {}, event_calendar=calendar,
            now=dt.datetime(2026, 7, 9, 10, 31, tzinfo=sensor.ET),
        )
        self.assertNotIn("G2", [item["gate"] for item in released["triggered"]])

    def test_wilder_seed_and_warmup_are_explicit(self):
        values = list(range(1, 16))
        result = sensor.wilder_values(values, 14)

        self.assertEqual(result[:13], [None] * 13)
        self.assertEqual(result[13], sum(range(1, 15)) / 14)
        self.assertAlmostEqual(result[14], (result[13] * 13 + 15) / 14)

    def test_non_finite_market_values_are_missing_not_numeric(self):
        self.assertIsNone(sensor.to_float(float("inf")))
        self.assertIsNone(sensor.to_float(float("nan")))

    def test_chase_extension_requires_full_twenty_bar_session_mean(self):
        rows = []
        for index in range(10):
            close = 100.0 + index
            rows.append({
                "t": f"2026-07-09T10:{index:02d}:00-04:00",
                "open": close, "high": close + 1, "low": close - 1,
                "close": close, "volume": 100,
                "ema5": close, "ema8": close, "ema13": close,
                "ema21": close, "ema34": close, "vwap": close,
            })

        summary = sensor.interval_summary(rows, "5m")

        self.assertIsNone(summary["mean20"])
        self.assertIsNone(summary["extension_atr20"])
        self.assertIsNone(summary["fresh_low45"])

    def test_volume_baseline_uses_same_time_slot_and_minimum_sessions(self):
        rows = []
        for day, last_volume in (("2026-07-06", 100), ("2026-07-07", 200), ("2026-07-08", 300)):
            rows += [
                {"t": f"{day}T09:30:00-04:00", "volume": 50},
                {"t": f"{day}T09:45:00-04:00", "volume": last_volume},
            ]
        current = {"t": "2026-07-09T09:45:00-04:00", "volume": 400}
        rows += [{"t": "2026-07-09T09:30:00-04:00", "volume": 50}, current]

        volume = sensor.same_tod_baseline(rows, current)

        self.assertTrue(volume["sufficient"])
        self.assertEqual(volume["baseline"], 200)
        self.assertEqual(volume["rvol"], 2.0)
        self.assertEqual(volume["cum_baseline"], 250)
        self.assertEqual(volume["cum_rvol"], 1.8)

    def test_missing_same_time_volume_baseline_caps_allow(self):
        now = dt.datetime(2026, 7, 9, 12, 0, tzinfo=sensor.ET)
        obs = observation()
        obs["data_freshness"] = sensor.observation_freshness(obs, now=now)
        calendar = {
            "available": True, "fresh": True, "events": [],
            "validThrough": "2026-07-10", "eventCount": 0,
        }

        gates = sensor.build_gates(obs, {}, event_calendar=calendar, now=now)

        self.assertIn("G3_DATA", [item["gate"] for item in gates["triggered"]])
        self.assertEqual(gates["score_cap"], "观察")
        self.assertTrue(gates["prohibit_allow"])

    def test_forming_daily_bar_is_excluded_until_settlement(self):
        now = dt.datetime(2026, 7, 9, 15, 30, tzinfo=sensor.ET)
        rows = [
            {"t": "2026-07-08T00:00:00-04:00"},
            {"t": "2026-07-09T00:00:00-04:00"},
        ]
        self.assertEqual(
            [row["t"] for row in sensor.completed_daily_rows(rows, now=now)],
            ["2026-07-08T00:00:00-04:00"],
        )

    def test_corrupt_state_fails_closed_instead_of_resetting(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "state.json"
            path.write_text("not-json", encoding="utf-8")
            with self.assertRaises(json.JSONDecodeError):
                sensor.load_state(path, "QQQ")
            path.write_text('{"schemaVersion": 2}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unsupported"):
                sensor.load_state(path, "QQQ")


if __name__ == "__main__":
    unittest.main()

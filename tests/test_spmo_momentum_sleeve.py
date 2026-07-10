import importlib.util
import datetime as dt
import json
import pathlib
import sys
import tempfile
import types
import unittest
from unittest import mock

import pytest


SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "spmo_momentum_sleeve.py"
SPEC = importlib.util.spec_from_file_location("spmo_momentum_sleeve", SCRIPT)
spmo = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(spmo)
TEST_AS_OF = "2025-09-17"
TEST_NOW = dt.datetime(2025, 9, 17, 17, 0, tzinfo=dt.timezone(dt.timedelta(hours=-4)))


def rows_from_closes(closes, start=1):
    rows = []
    first_day = dt.date(2025, 1, 1) + dt.timedelta(days=start - 1)
    for i, close in enumerate(closes, start):
        rows.append({
            "date": (first_day + dt.timedelta(days=i - start)).isoformat(),
            "open": close,
            "high": close + 1,
            "low": close - 1,
            "close": close,
            "volume": 1000000,
        })
    return rows


def payload(qqq=720, ema21=700, weight=6.0, as_of=TEST_AS_OF):
    return {
        "summary": {
            "marketValue": 100000, "priceAsOf": as_of,
            "generatedAt": f"{as_of}T16:30:00-04:00",
        },
        "qqqTqqq": {"latest": {"date": as_of, "qqq": qqq, "ema21": ema21}},
        "stocks": [
            {"sym": "SPMO", "held": True, "shares": 40, "value": weight * 1000,
             "avg": 140, "unrealPct": 2.0},
        ],
    }


class SpmoMomentumSleeveTests(unittest.TestCase):
    def test_blocks_new_add_when_market_gate_is_closed_but_separates_existing_hold(self):
        closes = [100 + i * 0.25 for i in range(260)]
        result = spmo.score_spmo(
            rows_from_closes(closes),
            {
                "SPY": rows_from_closes([95 + i * 0.15 for i in range(260)]),
                "QQQ": rows_from_closes([90 + i * 0.10 for i in range(260)]),
                "VOO": rows_from_closes([95 + i * 0.14 for i in range(260)]),
            },
            payload(qqq=690, ema21=700),
            technical={"flags": {"belowEma21": True}, "intradayLabel": "BLOCK"},
            decision={"label": "BLOCK"},
            now=TEST_NOW,
        )

        self.assertEqual(result["label"], "BLOCK")
        self.assertTrue(result["gates"]["marketBlock"])
        self.assertEqual(result["actions"]["maxNewAddPct"], 0.0)
        self.assertIn("HOLD_WITH_MOVING_STOP", result["actions"]["existingSleeve"])

    def test_allows_tranche_after_market_and_relative_strength_repair(self):
        closes = [100 + i * 0.35 for i in range(260)]
        result = spmo.score_spmo(
            rows_from_closes(closes),
            {
                "SPY": rows_from_closes([100 + i * 0.18 for i in range(260)]),
                "QQQ": rows_from_closes([100 + i * 0.20 for i in range(260)]),
                "VOO": rows_from_closes([100 + i * 0.18 for i in range(260)]),
            },
            payload(qqq=725, ema21=700, weight=6.0),
            technical={"flags": {"belowEma21": False}, "intradayLabel": "ALLOW"},
            decision={"label": "ALLOW"},
            now=TEST_NOW,
        )

        self.assertEqual(result["label"], "ALLOW")
        self.assertEqual(result["sleeveState"], "trend")
        self.assertGreater(result["actions"]["maxNewAddPct"], 0)
        self.assertIn("ALLOW tranche", result["actions"]["newAdd"])
        self.assertEqual(len(result["tranchePlan"]), 3)

    def test_overextended_momentum_is_watch_not_chase(self):
        closes = [100 + i * 0.18 for i in range(255)] + [170, 175, 180, 185, 190]
        result = spmo.score_spmo(
            rows_from_closes(closes),
            {
                "SPY": rows_from_closes([100 + i * 0.08 for i in range(260)]),
                "QQQ": rows_from_closes([100 + i * 0.09 for i in range(260)]),
                "VOO": rows_from_closes([100 + i * 0.08 for i in range(260)]),
            },
            payload(qqq=725, ema21=700, weight=3.0),
            technical={"flags": {"belowEma21": False}, "intradayLabel": "ALLOW"},
            decision={"label": "ALLOW"},
            now=TEST_NOW,
        )

        self.assertEqual(result["label"], "WATCH")
        self.assertTrue(result["gates"]["stretchedAboveEma21"])
        self.assertIn("WAIT", result["actions"]["newAdd"].upper())
        self.assertTrue(any("Overextended" in note for note in result["reviewers"]["quantReviewer"]))

    def test_market_watch_is_evaluated_before_asset_allow_and_prohibits_new_add(self):
        closes = [100 + i * 0.35 for i in range(260)]
        result = spmo.score_spmo(
            rows_from_closes(closes),
            {
                "SPY": rows_from_closes([100 + i * 0.18 for i in range(260)]),
                "QQQ": rows_from_closes([100 + i * 0.20 for i in range(260)]),
                "VOO": rows_from_closes([100 + i * 0.18 for i in range(260)]),
            },
            payload(qqq=725, ema21=700),
            technical={"flags": {"belowEma21": False}, "intradayLabel": "WATCH"},
            decision={"label": "ALLOW"},
            now=TEST_NOW,
        )

        self.assertEqual(result["label"], "WATCH")
        self.assertTrue(result["gates"]["marketWatch"])
        self.assertEqual(result["actions"]["maxNewAddPct"], 0.0)

    def test_missing_market_context_fails_closed_even_when_asset_trend_is_strong(self):
        closes = [100 + i * 0.35 for i in range(260)]
        technical, decision = spmo.sentinel_context_for_payload(payload(), {})
        result = spmo.score_spmo(
            rows_from_closes(closes),
            {
                "SPY": rows_from_closes([100 + i * 0.18 for i in range(260)]),
                "QQQ": rows_from_closes([100 + i * 0.20 for i in range(260)]),
                "VOO": rows_from_closes([100 + i * 0.18 for i in range(260)]),
            },
            payload(qqq=725, ema21=700),
            technical=technical,
            decision=decision,
            now=TEST_NOW,
        )

        self.assertEqual(result["label"], "BLOCK")
        self.assertFalse(result["gates"]["marketDataValid"])
        self.assertEqual(result["gates"]["marketGateStatus"], "MISSING_SENTINEL")
        self.assertEqual(result["actions"]["maxNewAddPct"], 0.0)

    def test_sentinel_context_requires_matching_dashboard_freshness(self):
        now = dt.datetime(2026, 6, 17, 17, 0, tzinfo=spmo.ET)
        dash = {"summary": {"generatedAt": "2026-06-17T16:30:00-04:00", "priceAsOf": "2026-06-17"}}
        sentinel = {
            "ranAt": "2026-06-17T16:40:00-04:00",
            "dataFreshness": {
                "dashboardGeneratedAt": "2026-06-17T16:30:00-04:00",
                "dashboardPriceAsOf": "2026-06-17",
            },
            "agents": {
                "technical": {"intradayLabel": "BLOCK"},
                "decision": {"label": "BLOCK"},
            },
        }

        technical, decision = spmo.sentinel_context_for_payload(dash, sentinel, now=now)
        self.assertEqual(technical["intradayLabel"], "BLOCK")
        self.assertEqual(decision["label"], "BLOCK")

        sentinel["dataFreshness"]["dashboardGeneratedAt"] = "2026-06-16T10:00:00"
        technical, decision = spmo.sentinel_context_for_payload(dash, sentinel, now=now)
        self.assertEqual(technical["intradayLabel"], "BLOCK_DATA")
        self.assertEqual(decision["label"], "BLOCK_DATA")
        self.assertEqual(technical["_marketGateContext"]["status"], "MISALIGNED_SENTINEL")

    def test_old_but_perfectly_aligned_2019_inputs_cannot_allow(self):
        closes = [100 + i * 0.35 for i in range(260)]
        old_rows = rows_from_closes(closes)
        for row in old_rows:
            row["date"] = "2019" + row["date"][4:]
        benchmarks = {}
        for symbol, slope in (("SPY", 0.18), ("QQQ", 0.20), ("VOO", 0.18)):
            rows = rows_from_closes([100 + i * slope for i in range(260)])
            for row in rows:
                row["date"] = "2019" + row["date"][4:]
            benchmarks[symbol] = rows
        old_as_of = old_rows[-1]["date"]
        old_payload = payload(qqq=725, ema21=700, as_of=old_as_of)

        result = spmo.score_spmo(
            old_rows, benchmarks, old_payload,
            technical={"flags": {"belowEma21": False}, "intradayLabel": "ALLOW"},
            decision={"label": "ALLOW"},
            now=dt.datetime(2026, 7, 9, 17, 0, tzinfo=spmo.ET),
        )

        self.assertEqual(result["label"], "BLOCK")
        self.assertEqual(result["dataFreshness"]["status"], "BLOCK")
        self.assertEqual(result["gates"]["marketGateStatus"], "STALE_OR_FUTURE_PRICE_DATA")
        self.assertEqual(result["actions"]["maxNewAddPct"], 0.0)

    def test_future_daily_rows_cannot_allow_even_when_all_dates_align(self):
        rows = rows_from_closes([100 + i * 0.35 for i in range(260)])
        future_as_of = rows[-1]["date"]
        benchmarks = {
            symbol: rows_from_closes([100 + i * 0.18 for i in range(260)])
            for symbol in spmo.BENCHMARKS
        }
        result = spmo.score_spmo(
            rows, benchmarks, payload(qqq=725, ema21=700, as_of=future_as_of),
            technical={"flags": {"belowEma21": False}, "intradayLabel": "ALLOW"},
            decision={"label": "ALLOW"},
            now=dt.datetime(2025, 9, 16, 17, 0, tzinfo=spmo.ET),
        )

        self.assertEqual(result["label"], "BLOCK")
        self.assertEqual(result["dataFreshness"]["absoluteSession"]["reason"],
                         "date_after_last_closed_session")

    def test_fetch_uses_adjusted_ohlc_for_dividend_and_split_consistent_signals(self):
        pd = pytest.importorskip("pandas")
        calls = {}

        class FakeTicker:
            def history(self, **kwargs):
                calls.update(kwargs)
                return pd.DataFrame(
                    {"Open": [100], "High": [101], "Low": [99], "Close": [100], "Volume": [10]},
                    index=pd.to_datetime(["2026-07-09"]),
                )

        fake = types.SimpleNamespace(Ticker=lambda symbol: FakeTicker())
        with mock.patch.dict(sys.modules, {"yfinance": fake}):
            rows = spmo.fetch_daily_rows("SPMO")

        self.assertTrue(calls["auto_adjust"])
        self.assertEqual(rows[0]["close"], 100.0)

    def test_saved_moving_stop_ratchets_up_and_marks_a_breach(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp)
            first = {
                "asOf": "2026-07-08",
                "price": 160.0,
                "position": {"shares": 40, "avg": 140},
                "levels": {"movingStop3Atr": 150.0},
                "gates": {},
                "actions": {"existingSleeve": "HOLD"},
            }
            spmo.write_outputs(first, out)

            second = {
                "asOf": "2026-07-09",
                "price": 149.0,
                "position": {"shares": 40, "avg": 140},
                "levels": {"movingStop3Atr": 145.0},
                "gates": {},
                "actions": {"existingSleeve": "HOLD"},
            }
            spmo.write_outputs(second, out)
            saved = json.loads((out / "latest_spmo_momentum.json").read_text(encoding="utf-8"))

        self.assertEqual(saved["levels"]["movingStop3AtrRaw"], 145.0)
        self.assertEqual(saved["levels"]["priorMovingStop3Atr"], 150.0)
        self.assertEqual(saved["levels"]["movingStop3Atr"], 150.0)
        self.assertEqual(saved["levels"]["movingStopSource"], "prior_saved_floor")
        self.assertTrue(saved["levels"]["movingStopBreached"])
        self.assertTrue(saved["gates"]["movingStopBreached"])
        self.assertIn("STOP_BREACHED", saved["actions"]["existingSleeve"])
        self.assertEqual(saved["positionLifecycle"]["status"], "CONTINUING")
        self.assertIsNotNone(saved["positionLifecycle"]["id"])

    def test_saved_moving_stop_can_rise_but_never_fall(self):
        previous = {
            "asOf": "2026-07-08",
            "position": {"shares": 40, "avg": 140},
            "positionLifecycle": {"id": "spmo-existing"},
            "levels": {"movingStop3Atr": 150.0},
        }
        rising = {
            "asOf": "2026-07-09",
            "price": 170.0,
            "position": {"shares": 45, "avg": 142},
            "levels": {"movingStop3Atr": 155.0},
            "actions": {},
        }
        spmo.apply_persisted_moving_stop(rising, previous)
        self.assertEqual(rising["levels"]["movingStop3Atr"], 155.0)
        self.assertFalse(rising["levels"]["movingStopBreached"])
        self.assertEqual(rising["positionLifecycle"]["id"], "spmo-existing")

    def test_flat_snapshot_clears_old_stop_and_reopen_starts_new_lifecycle(self):
        previous = {
            "asOf": "2026-07-07",
            "price": 160.0,
            "position": {"shares": 40, "avg": 140},
            "positionLifecycle": {"id": "spmo-old"},
            "levels": {"movingStop3Atr": 150.0},
            "actions": {},
        }
        flat = {
            "asOf": "2026-07-08",
            "price": 155.0,
            "position": {"shares": None, "avg": None},
            "levels": {"movingStop3Atr": 145.0},
            "actions": {},
        }
        spmo.apply_persisted_moving_stop(flat, previous)
        self.assertIsNone(flat["levels"]["movingStop3Atr"])
        self.assertEqual(flat["positionLifecycle"]["status"], "FLAT")

        reopened = {
            "asOf": "2026-07-09",
            "price": 150.0,
            "position": {"shares": 20, "avg": 148},
            "levels": {"movingStop3Atr": 140.0},
            "actions": {},
        }
        spmo.apply_persisted_moving_stop(reopened, flat)
        self.assertEqual(reopened["levels"]["movingStop3Atr"], 140.0)
        self.assertEqual(reopened["levels"]["movingStopSource"], "current_3atr_lifecycle_start")
        self.assertEqual(reopened["positionLifecycle"]["status"], "OPENED_OR_RESTARTED")
        self.assertNotEqual(reopened["positionLifecycle"]["id"], "spmo-old")

    def test_manual_reset_requires_reason_and_does_not_inherit_prior_stop(self):
        previous = {
            "asOf": "2026-07-08",
            "position": {"shares": 40, "avg": 140},
            "positionLifecycle": {"id": "spmo-old"},
            "levels": {"movingStop3Atr": 150.0},
        }
        current = {
            "asOf": "2026-07-09",
            "price": 151.0,
            "position": {"shares": 40, "avg": 149},
            "levels": {"movingStop3Atr": 140.0},
            "actions": {},
        }
        with self.assertRaises(ValueError):
            spmo.apply_persisted_moving_stop(dict(current), previous, reset_stop=True)

        spmo.apply_persisted_moving_stop(
            current,
            previous,
            reset_stop=True,
            reset_reason="position was closed and re-entered between daily snapshots",
        )
        self.assertEqual(current["levels"]["movingStop3Atr"], 140.0)
        self.assertEqual(current["positionLifecycle"]["status"], "MANUAL_RESET")
        self.assertTrue(current["positionLifecycle"]["resetApplied"])
        self.assertNotEqual(current["positionLifecycle"]["id"], "spmo-old")


if __name__ == "__main__":
    unittest.main()

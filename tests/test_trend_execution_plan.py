import importlib.util
import datetime as dt
import json
import pathlib
import tempfile
import unittest


SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "generate_trend_execution_plan.py"
SPEC = importlib.util.spec_from_file_location("generate_trend_execution_plan", SCRIPT)
plan = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(plan)


def rows(closes):
    out = []
    start = dt.date(2025, 1, 1)
    for i, close in enumerate(closes, 1):
        out.append({
            "date": (start + dt.timedelta(days=i)).isoformat(),
            "open": close,
            "high": close + 1,
            "low": close - 1,
            "close": close,
        })
    return out


def rows_ending(end_date, count=230, slope=0.25):
    start = end_date - dt.timedelta(days=count - 1)
    return [
        {
            "date": (start + dt.timedelta(days=i)).isoformat(),
            "open": 100 + i * slope,
            "high": 101 + i * slope,
            "low": 99 + i * slope,
            "close": 100 + i * slope,
        }
        for i in range(count)
    ]


def output_document(candidate, *, price=170.0, generated_at="2026-07-09T17:00:00-04:00"):
    return {
        "generatedAt": generated_at,
        "schemaVersion": 3,
        "dataFreshness": {
            "status": "PASS", "dashboardPriceAsOf": "2026-07-09", "priceSource": "test",
        },
        "market": {"label": "ALLOW"},
        "concentration": {},
        "rules": [],
        "levels": [{
            "Symbol": "AAA", "Theme": "other", "Price": price, "Shares": 10,
            "WeightPct": 1, "UnrealPct": 1, "DayPct": 1, "Fib": "up",
            "EMA21": 160, "EMA50": 150, "EMA200": 120, "ATR14": 2,
            "HistoryBars": 230, "PriceAsOf": "2026-07-09", "DataQuality": "PASS",
            "DistEMA21ATR": 5, "RS21vsSPY": 1, "RS63vsSPY": 1,
            "EntryDecision": "ALLOW_TRANCHE", "ExistingAction": "ALLOW_SMALL_TRANCHE",
            "BuyStop": 171, "BuyLimit": 172, "PullbackZone": [159, 161],
            "InvalidationClose": 149, "MovingStopCandidate": candidate,
            "PriorMovingStop": None, "MovingStop": candidate,
            "MovingStopSource": "current_candidate_unpersisted",
            "MovingStopBreached": False, "StopLifecycle": None, "StopStatus": "OK",
            "MaxWeightPct": 3, "ProposedTranchePct": 1, "BlockedBy": "",
        }],
        "recentSellsSince": "2026-07-02",
        "recentSells": [],
    }


class TrendExecutionPlanTests(unittest.TestCase):
    def test_ema_values_are_unavailable_until_full_seed_window(self):
        values = list(range(1, 205))
        result = plan.ema(values, 200)

        self.assertTrue(all(value is None for value in result[:199]))
        self.assertEqual(result[199], sum(values[:200]) / 200)
        self.assertIsNotNone(result[-1])

    def test_reclaim_buy_stop_uses_ema21_not_prior_high_after_break(self):
        closes = [100 + i for i in range(60)] + [150, 148, 146, 144, 142]
        features = plan.feature_rows(rows(closes))
        stock = {
            "sym": "QQQ",
            "theme": "宽基指数ETF",
            "held": True,
            "shares": 1,
            "value": 142,
            "curPrice": 142,
            "dayChangePct": -4,
            "unrealPct": 0,
            "_marketValue": 1000,
            "fib": {"now": {"label": "转换中"}},
        }

        row = plan.level_plan(
            stock,
            features,
            features,
            {"label": "BLOCK", "qqqBelowEma21": True},
            {"semisRiskPct": 0},
            {},
        )

        self.assertLess(row["BuyStop"], 150)
        self.assertAlmostEqual(row["BuyStop"], row["EMA21"] + 0.1 * row["ATR14"], places=2)
        self.assertIn("market_gate_BLOCK", row["BlockedBy"])

    def test_existing_action_marks_breached_trailing_stop(self):
        closes = [100 + i for i in range(80)] + [175, 150]
        features = plan.feature_rows(rows(closes))
        stock = {
            "sym": "NVDA",
            "theme": "半导体",
            "held": True,
            "shares": 1,
            "value": 150,
            "curPrice": 150,
            "dayChangePct": -10,
            "unrealPct": 0,
            "_marketValue": 1000,
            "fib": {"now": {"label": "转换中"}},
        }

        row = plan.level_plan(
            stock,
            features,
            features,
            {"label": "BLOCK", "qqqBelowEma21": True},
            {"semisRiskPct": 55},
            {},
        )

        self.assertIn(row["StopStatus"], {"TRAIL_BREACHED", "INVALIDATION_BREACHED"})
        self.assertIn("TRIM", row["ExistingAction"])

    def test_watch_rows_never_propose_a_tranche(self):
        closes = [100 + i * 0.1 for i in range(80)]
        features = plan.feature_rows(rows(closes))
        stock = {
            "sym": "AAA", "theme": "other", "held": True, "shares": 1,
            "value": closes[-1], "_marketValue": 1000,
            "fib": {"now": {"state": "up", "label": "up"}},
        }

        row = plan.level_plan(
            stock, features, features,
            {"label": "WATCH", "qqqBelowEma21": False},
            {"semisRiskPct": 0}, {},
        )

        self.assertTrue(row["EntryDecision"].startswith("WATCH"))
        self.assertEqual(row["ProposedTranchePct"], 0.0)

    def test_missing_spy_history_cannot_be_treated_as_zero_relative_return(self):
        closes = [100 + i for i in range(80)]
        features = plan.feature_rows(rows(closes))
        stock = {
            "sym": "AAA", "theme": "other", "held": True, "shares": 1,
            "value": closes[-1], "_marketValue": 1000,
            "fib": {"now": {"state": "up", "label": "up"}},
        }

        row = plan.level_plan(
            stock, features, [],
            {"label": "ALLOW", "qqqBelowEma21": False},
            {"semisRiskPct": 0}, {},
        )

        self.assertIsNone(row["RS21vsSPY"])
        self.assertNotEqual(row["EntryDecision"], "ALLOW_TRANCHE")

    def test_build_plan_filters_price_rows_after_dashboard_as_of(self):
        pd_rows = []
        start = dt.date(2025, 12, 1)
        for i in range(230):
            day = start + dt.timedelta(days=i)
            pd_rows.append({
                "date": day.isoformat(), "open": 100 + i, "high": 101 + i,
                "low": 99 + i, "close": 100 + i,
            })
        payload = {
            "summary": {"marketValue": 1000, "priceAsOf": "2026-06-30"},
            "stocks": [{
                "sym": "AAA", "held": True, "value": 1000, "shares": 1,
                "theme": "other", "prices": [], "fib": {"now": {"state": "up"}},
            }],
        }

        document = plan.build_plan(
            payload, {}, {}, ohlc_map={"AAA": pd_rows, "SPY": pd_rows})

        self.assertLessEqual(document["levels"][0]["PriceAsOf"], "2026-06-30")
        self.assertNotEqual(document["levels"][0]["PriceAsOf"], pd_rows[-1]["date"])

    def test_stale_sentinel_allow_is_blocked_as_data(self):
        payload = {
            "summary": {
                "priceAsOf": "2026-07-09",
                "generatedAt": "2026-07-09T17:00:00-07:00",
            },
            "qqqTqqq": {"latest": {"qqq": 720, "ema21": 710}},
        }
        sentinel = {
            "ranAt": "2026-07-08T17:01:00-07:00",
            "dataFreshness": {"dashboardPriceAsOf": "2026-07-08"},
            "agents": {"decision": {"label": "ALLOW"}},
        }

        context = plan.market_context(payload, sentinel)

        self.assertEqual(context["label"], "BLOCK_DATA")
        self.assertFalse(context["sentinelCurrent"])

    def test_old_but_perfectly_aligned_2019_inputs_cannot_allow_a_tranche(self):
        as_of = dt.date(2019, 9, 17)
        generated_at = "2019-09-17T16:30:00-04:00"
        payload = {
            "summary": {
                "marketValue": 10000,
                "priceAsOf": as_of.isoformat(),
                "generatedAt": generated_at,
            },
            "qqqTqqq": {
                "latest": {"date": as_of.isoformat(), "qqq": 300, "ema21": 290},
            },
            "stocks": [{
                "sym": "AAA", "held": True, "shares": 10, "value": 1000,
                "theme": "other", "prices": [], "fib": {"now": {"state": "up"}},
            }],
        }
        sentinel = {
            "ranAt": "2019-09-17T16:40:00-04:00",
            "dataFreshness": {
                "dashboardPriceAsOf": as_of.isoformat(),
                "dashboardGeneratedAt": generated_at,
            },
            "agents": {"decision": {"label": "ALLOW", "primaryAction": "add"}},
        }
        document = plan.build_plan(
            payload,
            sentinel,
            {},
            ohlc_map={
                "AAA": rows_ending(as_of, slope=0.4),
                "SPY": rows_ending(as_of, slope=0.2),
            },
            now=dt.datetime(2026, 7, 9, 17, 0, tzinfo=plan.ET),
        )

        self.assertEqual(document["dataFreshness"]["status"], "BLOCK")
        self.assertEqual(document["market"]["label"], "BLOCK_DATA")
        self.assertEqual(document["levels"][0]["EntryDecision"], "BLOCK_DATA")
        self.assertEqual(document["levels"][0]["ProposedTranchePct"], 0.0)
        self.assertEqual(
            document["market"]["dashboardSession"]["reason"],
            "last_closed_session_stale",
        )

    def test_future_aligned_dashboard_and_sentinel_cannot_allow_a_tranche(self):
        as_of = dt.date(2026, 7, 13)
        generated_at = "2026-07-13T16:30:00-04:00"
        payload = {
            "summary": {"marketValue": 10000, "priceAsOf": as_of.isoformat(), "generatedAt": generated_at},
            "qqqTqqq": {"latest": {"date": as_of.isoformat(), "qqq": 300, "ema21": 290}},
            "stocks": [{
                "sym": "AAA", "held": True, "shares": 10, "value": 1000,
                "theme": "other", "prices": [], "fib": {"now": {"state": "up"}},
            }],
        }
        sentinel = {
            "ranAt": "2026-07-13T16:40:00-04:00",
            "dataFreshness": {
                "dashboardPriceAsOf": as_of.isoformat(), "dashboardGeneratedAt": generated_at,
            },
            "agents": {"decision": {"label": "ALLOW"}},
        }
        document = plan.build_plan(
            payload, sentinel, {},
            ohlc_map={"AAA": rows_ending(as_of, slope=0.4), "SPY": rows_ending(as_of, slope=0.2)},
            now=dt.datetime(2026, 7, 10, 17, 0, tzinfo=plan.ET),
        )

        self.assertEqual(document["levels"][0]["EntryDecision"], "BLOCK_DATA")
        self.assertEqual(
            document["dataFreshness"]["absoluteSession"]["reason"],
            "date_after_last_closed_session",
        )

    def test_saved_stop_ratchets_and_reasoned_reset_starts_new_lifecycle(self):
        with tempfile.TemporaryDirectory() as td:
            out = pathlib.Path(td) / "trend"
            first = output_document(150)
            plan.write_outputs(first, out)
            first_saved = json.loads(
                (out / "latest_trend_execution_plan.json").read_text(encoding="utf-8")
            )
            first_id = first_saved["levels"][0]["StopLifecycle"]["id"]

            plan.write_outputs(output_document(145), out)
            lower = json.loads(
                (out / "latest_trend_execution_plan.json").read_text(encoding="utf-8")
            )
            self.assertEqual(lower["levels"][0]["MovingStopCandidate"], 145.0)
            self.assertEqual(lower["levels"][0]["MovingStop"], 150.0)
            self.assertEqual(lower["levels"][0]["MovingStopSource"], "prior_saved_floor")
            self.assertEqual(lower["levels"][0]["StopLifecycle"]["status"], "CONTINUING")
            self.assertEqual(lower["levels"][0]["StopLifecycle"]["id"], first_id)

            plan.write_outputs(output_document(155), out)
            higher = json.loads(
                (out / "latest_trend_execution_plan.json").read_text(encoding="utf-8")
            )
            self.assertEqual(higher["levels"][0]["MovingStop"], 155.0)
            self.assertEqual(higher["levels"][0]["MovingStopSource"], "current_candidate")

            reason = "position closed and re-entered between saved snapshots"
            plan.write_outputs(output_document(140), out, resets={"AAA": reason})
            reset = json.loads(
                (out / "latest_trend_execution_plan.json").read_text(encoding="utf-8")
            )

        level = reset["levels"][0]
        self.assertEqual(level["MovingStop"], 140.0)
        self.assertEqual(level["StopLifecycle"]["status"], "MANUAL_RESET")
        self.assertEqual(level["StopLifecycle"]["resetReason"], reason)
        self.assertNotEqual(level["StopLifecycle"]["id"], first_id)
        self.assertEqual(reset["schemaVersion"], 3)
        self.assertTrue(reset["stopLifecyclePolicy"]["loweringRequiresExplicitReset"])
        self.assertEqual(reset["stopLifecyclePolicy"]["resetsApplied"][0]["reason"], reason)

    def test_stop_reset_validation_is_fail_closed(self):
        with self.assertRaises(ValueError):
            plan.parse_stop_resets(["AAA"])
        with self.assertRaises(ValueError):
            plan.parse_stop_resets(["AAA="])
        with self.assertRaises(ValueError):
            plan.parse_stop_resets(["AAA=one", "aaa=two"])
        with self.assertRaises(ValueError):
            plan.apply_persisted_moving_stops(
                output_document(140), {}, resets={"MISSING": "manual lifecycle reset"}
            )

    def test_output_bundle_is_strict_atomic_and_private(self):
        document = {
            "generatedAt": "2026-07-09T12:00:00",
            "dataFreshness": {"dashboardPriceAsOf": "2026-07-09", "priceSource": "test"},
            "market": {"label": "WATCH"},
            "concentration": {},
            "rules": [],
            "levels": [],
            "recentSellsSince": "2026-07-02",
            "recentSells": [],
        }
        with tempfile.TemporaryDirectory() as td:
            out = pathlib.Path(td) / "trend"
            plan.write_outputs(document, out)
            json_path = out / "latest_trend_execution_plan.json"
            old_json = json_path.read_text(encoding="utf-8")

            self.assertEqual(json.loads(old_json)["market"]["label"], "WATCH")
            for path in out.iterdir():
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(out.stat().st_mode & 0o777, 0o700)
            self.assertEqual((out / "latest_trend_levels.csv").read_text(encoding="utf-8"), "")

            invalid = dict(document, invalid=float("nan"))
            with self.assertRaises(ValueError):
                plan.write_outputs(invalid, out)
            self.assertEqual(json_path.read_text(encoding="utf-8"), old_json)


if __name__ == "__main__":
    unittest.main()

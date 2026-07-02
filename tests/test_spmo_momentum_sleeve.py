import importlib.util
import pathlib
import unittest


SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "spmo_momentum_sleeve.py"
SPEC = importlib.util.spec_from_file_location("spmo_momentum_sleeve", SCRIPT)
spmo = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(spmo)


def rows_from_closes(closes, start=1):
    rows = []
    for i, close in enumerate(closes, start):
        rows.append({
            "date": f"2026-01-{(i % 28) + 1:02d}",
            "open": close,
            "high": close + 1,
            "low": close - 1,
            "close": close,
            "volume": 1000000,
        })
    return rows


def payload(qqq=720, ema21=700, weight=6.0):
    return {
        "summary": {"marketValue": 100000},
        "qqqTqqq": {"latest": {"qqq": qqq, "ema21": ema21}},
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
        )

        self.assertEqual(result["label"], "WATCH")
        self.assertTrue(result["gates"]["stretchedAboveEma21"])
        self.assertIn("WAIT", result["actions"]["newAdd"].upper())
        self.assertTrue(any("Overextended" in note for note in result["reviewers"]["quantReviewer"]))

    def test_sentinel_context_requires_matching_dashboard_freshness(self):
        dash = {"summary": {"generatedAt": "2026-06-17T22:40:45", "priceAsOf": "2026-06-17"}}
        sentinel = {
            "dataFreshness": {
                "dashboardGeneratedAt": "2026-06-17T22:40:45",
                "dashboardPriceAsOf": "2026-06-17",
            },
            "agents": {
                "technical": {"intradayLabel": "BLOCK"},
                "decision": {"label": "BLOCK"},
            },
        }

        technical, decision = spmo.sentinel_context_for_payload(dash, sentinel)
        self.assertEqual(technical["intradayLabel"], "BLOCK")
        self.assertEqual(decision["label"], "BLOCK")

        sentinel["dataFreshness"]["dashboardGeneratedAt"] = "2026-06-16T10:00:00"
        self.assertEqual(spmo.sentinel_context_for_payload(dash, sentinel), (None, None))


if __name__ == "__main__":
    unittest.main()

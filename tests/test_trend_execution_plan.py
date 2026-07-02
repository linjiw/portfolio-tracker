import importlib.util
import pathlib
import unittest


SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "generate_trend_execution_plan.py"
SPEC = importlib.util.spec_from_file_location("generate_trend_execution_plan", SCRIPT)
plan = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(plan)


def rows(closes):
    out = []
    for i, close in enumerate(closes, 1):
        out.append({
            "date": f"2026-01-{(i % 28) + 1:02d}",
            "open": close,
            "high": close + 1,
            "low": close - 1,
            "close": close,
        })
    return out


class TrendExecutionPlanTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()

import importlib.util
import pathlib
import unittest


SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "generate_position_signal_report.py"
SPEC = importlib.util.spec_from_file_location("generate_position_signal_report", SCRIPT)
report = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(report)


class PositionSignalReportTests(unittest.TestCase):
    def test_classifies_qqq_tqqq_and_spmo_with_market_gate(self):
        payload = {
            "summary": {
                "marketValue": 100000,
                "priceMode": "mark-to-market",
                "priceAsOf": "2026-07-09",
                "fetchStale": False,
                "stalePriceSymbols": {},
                "missingPriceSymbols": [],
            },
            "qqqTqqq": {"latest": {"qqq": 705, "ema21": 716}},
            "stocks": [
                {"sym": "QQQ", "held": True, "value": 10000, "curPrice": 705, "dayChangePct": -4.8, "fib": {"now": {"state": "mixed", "label": "转换中", "rsi": 48}}},
                {"sym": "TQQQ", "held": True, "value": 500, "curPrice": 73, "dayChangePct": -14, "fib": {"now": {"state": "mixed", "label": "转换中", "rsi": 46}}},
                {"sym": "SPMO", "held": True, "value": 6500, "curPrice": 144, "dayChangePct": -5.6, "fib": {"now": {"state": "up", "label": "多头趋势", "rsi": 51}}},
            ],
            "risk": {"contrib": []},
        }
        sentinel = {
            "dataFreshness": {"dashboardPriceAsOf": "2026-07-09"},
            "agents": {
                "decision": {"label": "BLOCK", "primaryAction": "wait"},
                "spmoMomentum": {"available": True, "label": "BLOCK"},
            }
        }

        rows = {row["Symbol"]: row for row in report.build_rows(payload, sentinel)}

        self.assertIn("below EMA21", rows["QQQ"]["Signal"])
        self.assertIn("tactical only", rows["TQQQ"]["Signal"])
        self.assertIn("QQQ gate closed", rows["SPMO"]["Signal"])

    def test_stale_dashboard_blocks_all_position_signals(self):
        payload = {
            "summary": {
                "marketValue": 1000, "priceMode": "mark-to-market",
                "priceAsOf": "2026-07-09", "fetchStale": True,
            },
            "stocks": [{"sym": "AAA", "held": True, "value": 1000}],
        }

        row = report.build_rows(payload, {})[0]

        self.assertEqual(row["DataStatus"], "BLOCK")
        self.assertTrue(row["Signal"].startswith("BLOCK_DATA"))

    def test_unaligned_sentinel_cannot_drive_spmo_signal(self):
        payload = {
            "summary": {
                "marketValue": 1000, "priceMode": "mark-to-market",
                "priceAsOf": "2026-07-09", "fetchStale": False,
            },
            "stocks": [{"sym": "SPMO", "held": True, "value": 1000}],
        }
        sentinel = {
            "dataFreshness": {"dashboardPriceAsOf": "2026-07-08"},
            "agents": {"spmoMomentum": {"available": True, "label": "ALLOW"}},
        }

        row = report.build_rows(payload, sentinel)[0]

        self.assertTrue(row["Signal"].startswith("WATCH_DATA"))


if __name__ == "__main__":
    unittest.main()

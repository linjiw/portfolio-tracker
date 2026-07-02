import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import generate


class PriceRefreshTests(unittest.TestCase):
    def test_mark_to_market_revalues_only_symbols_with_prices(self):
        cur = {
            "QQQ": {"shares": 2.0, "price": 100.0, "value": 200.0, "gain": 20.0, "cost": 180.0},
            "MISSING": {"shares": 3.0, "price": 10.0, "value": 30.0, "gain": 0.0, "cost": 30.0},
        }
        prices = {"QQQ": {"2026-06-01": 110.0, "2026-06-03": 125.0}}

        refreshed = generate.mark_to_market(cur, prices, "2026-06-04")

        self.assertEqual(refreshed, {"QQQ": 125.0})
        self.assertEqual(cur["QQQ"]["shares"], 2.0)
        self.assertEqual(cur["QQQ"]["cost"], 180.0)
        self.assertEqual(cur["QQQ"]["price"], 125.0)
        self.assertEqual(cur["QQQ"]["value"], 250.0)
        self.assertEqual(cur["QQQ"]["gain"], 70.0)
        self.assertAlmostEqual(cur["QQQ"]["gainpct"], 70.0 / 180.0 * 100)
        self.assertEqual(cur["MISSING"]["value"], 30.0)

    def test_latest_price_date_prefers_market_benchmark(self):
        prices = {
            "ABC": {"2026-06-04": 10.0},
            "^GSPC": {"2026-06-02": 100.0, "2026-06-03": 101.0},
        }

        self.assertEqual(generate.latest_price_date(prices, ["ABC"], "2026-06-04"), "2026-06-03")

    def test_explicit_history_is_added_to_accumulated_exports(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            old_a = root / "Accounts_History (1).csv"
            old_b = root / "History_for_Account_DEMO01.csv"
            explicit = root / "fresh.csv"
            for p in (old_a, old_b, explicit):
                p.write_text("", encoding="utf-8")

            got = [Path(p).name for p in generate.collect_history_files(td, str(explicit))]

        self.assertEqual(set(got), {"Accounts_History (1).csv", "History_for_Account_DEMO01.csv", "fresh.csv"})
        self.assertEqual(len(got), 3)

    def test_merge_histories_dedupes_fee_rounding_drift(self):
        older = {
            "txns": {},
            "opt_txns": {"-TQQQ260612C92": [
                {"account": "DEMO01", "date": "2026-06-04", "side": "SELL",
                 "qty": 10.0, "price": 0.48, "amount": 473.28},
            ]},
            "names": {},
        }
        newer = {
            "txns": {},
            "opt_txns": {"-TQQQ260612C92": [
                {"account": "DEMO01", "date": "2026-06-04", "side": "SELL",
                 "qty": 10.0, "price": 0.48, "amount": 473.27},
            ]},
            "names": {},
        }

        _, opt, _ = generate.merge_histories([older, newer])

        self.assertEqual(len(opt["-TQQQ260612C92"]), 1)
        self.assertEqual(opt["-TQQQ260612C92"][0]["amount"], 473.27)


if __name__ == "__main__":
    unittest.main()

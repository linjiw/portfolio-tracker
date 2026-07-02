import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import generate


def tx(date, sym, side, qty, price, amount):
    return {"date": date, "side": side, "qty": qty, "price": price, "amount": amount, "account": "Z"}


class CounterfactualReplayTests(unittest.TestCase):
    def test_skip_stage_keeps_old_holding_and_cash_balanced(self):
        txns = {
            "AAA": [tx("2026-01-02", "AAA", "SELL", 10, 100, 1000)],
            "BBB": [tx("2026-01-02", "BBB", "BUY", 10, 100, -1000)],
        }
        cur = {"AAA": {"shares": 0}, "BBB": {"shares": 10}}
        prices = {
            "^GSPC": {"2026-01-01": 100, "2026-01-02": 100, "2026-01-03": 100, "2026-01-04": 100},
            "AAA": {"2026-01-01": 100, "2026-01-02": 110, "2026-01-03": 120, "2026-01-04": 130},
            "BBB": {"2026-01-01": 100, "2026-01-02": 90, "2026-01-03": 80, "2026-01-04": 70},
        }

        cf = generate.build_counterfactual_replays(txns, cur, prices, "2026-01-01", "2026-01-04", 1000)

        self.assertEqual(len(cf["events"]), 1)
        event = cf["events"][0]
        self.assertEqual(event["branchDate"], "2026-01-01")
        self.assertEqual(event["series"][0]["actualPct"], 0)
        self.assertEqual(event["series"][0]["altPct"], 0)
        self.assertEqual(event["series"][-1]["actual"], 700)
        self.assertEqual(event["series"][-1]["alt"], 1300)
        self.assertEqual(event["summary"]["currentDelta"], -600)

    def test_later_sell_dependent_on_skipped_buy_is_flagged(self):
        txns = {
            "AAA": [
                tx("2026-01-02", "AAA", "BUY", 10, 100, -1000),
                tx("2026-01-06", "AAA", "SELL", 10, 100, 1000),
            ],
            "BBB": [tx("2026-01-02", "BBB", "BUY", 10, 100, -1000)],
            "CCC": [tx("2026-01-02", "CCC", "BUY", 10, 100, -1000)],
        }
        cur = {"AAA": {"shares": 0}, "BBB": {"shares": 10}, "CCC": {"shares": 10}, "DDD": {"shares": 10}}
        prices = {
            "^GSPC": {"2026-01-01": 100, "2026-01-02": 100, "2026-01-03": 100, "2026-01-06": 100},
            "AAA": {"2026-01-01": 100, "2026-01-02": 100, "2026-01-03": 100, "2026-01-06": 100},
            "BBB": {"2026-01-01": 100, "2026-01-02": 100, "2026-01-03": 100, "2026-01-06": 100},
            "CCC": {"2026-01-01": 100, "2026-01-02": 100, "2026-01-03": 100, "2026-01-06": 100},
            "DDD": {"2026-01-01": 100, "2026-01-02": 100, "2026-01-03": 100, "2026-01-06": 100},
        }

        cf = generate.build_counterfactual_replays(txns, cur, prices, "2026-01-01", "2026-01-06", 1000)

        self.assertEqual(len(cf["events"]), 1)
        self.assertTrue(cf["events"][0]["warnings"])
        self.assertTrue(cf["events"][0]["summary"]["isTruncated"])
        self.assertEqual(cf["events"][0]["summary"]["lastValidDate"], "2026-01-03")
        self.assertTrue(all(r["alt"] is not None for r in cf["events"][0]["series"]))

    def test_detection_does_not_merge_weeks_of_trades(self):
        trades = generate.flatten_stock_trades({
            "AAA": [tx("2026-01-02", "AAA", "SELL", 5, 100, 500)],
            "BBB": [tx("2026-01-02", "BBB", "BUY", 5, 100, -500)],
            "CCC": [tx("2026-01-06", "CCC", "SELL", 5, 100, 500)],
            "DDD": [tx("2026-01-06", "DDD", "BUY", 5, 100, -500)],
        })

        events = generate.detect_rebalance_events(trades, base_value=1000, max_events=10)

        self.assertEqual(len(events), 2)
        self.assertEqual({e["start"] for e in events}, {"2026-01-02", "2026-01-06"})
        self.assertTrue(all(e["start"] == e["end"] for e in events))

    def test_first_axis_day_event_is_skipped_without_prefork_close(self):
        txns = {
            "AAA": [tx("2026-01-01", "AAA", "SELL", 1, 100, 100)],
            "BBB": [tx("2026-01-01", "BBB", "BUY", 1, 100, -100)],
        }
        cur = {"AAA": {"shares": 0}, "BBB": {"shares": 1}, "DDD": {"shares": 10}}
        prices = {
            "^GSPC": {"2026-01-01": 100, "2026-01-02": 100},
            "AAA": {"2026-01-01": 100, "2026-01-02": 100},
            "BBB": {"2026-01-01": 100, "2026-01-02": 100},
            "DDD": {"2026-01-01": 100, "2026-01-02": 100},
        }

        cf = generate.build_counterfactual_replays(txns, cur, prices, "2026-01-01", "2026-01-02", 1000)

        self.assertEqual(cf["events"], [])
        self.assertEqual(cf["aggregate"]["scoredCount"], 0)

    def test_scoring_uses_latest_delta_per_turnover(self):
        txns = {
            "AAA": [tx("2026-01-02", "AAA", "SELL", 10, 100, 1000)],
            "BBB": [tx("2026-01-02", "BBB", "BUY", 10, 100, -1000)],
        }
        cur = {"AAA": {"shares": 0}, "BBB": {"shares": 10}}
        prices = {
            "^GSPC": {"2026-01-01": 100, "2026-01-02": 100, "2026-01-03": 100},
            "AAA": {"2026-01-01": 100, "2026-01-02": 100, "2026-01-03": 100},
            "BBB": {"2026-01-01": 100, "2026-01-02": 100, "2026-01-03": 110},
        }

        cf = generate.build_counterfactual_replays(txns, cur, prices, "2026-01-01", "2026-01-03", 1000)

        event = cf["events"][0]
        self.assertEqual(event["summary"]["currentDelta"], 100)
        self.assertAlmostEqual(event["score"]["impactOnTurnoverPct"], 5.0)
        self.assertGreater(event["score"]["score"], 80)
        self.assertLess(event["score"]["score"], 90)
        self.assertEqual(cf["aggregate"]["totalDelta"], 100)
        self.assertEqual(cf["aggregate"]["totalGross"], 2000)
        self.assertEqual(cf["aggregate"]["winRate"], 100.0)
        self.assertEqual(cf["aggregate"]["scoredCount"], 1)


if __name__ == "__main__":
    unittest.main()

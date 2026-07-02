import datetime
import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import generate


def dates(n):
    start = datetime.date(2026, 1, 2)
    return [(start + datetime.timedelta(days=i)).isoformat() for i in range(n)]


def ohlc_from_closes(closes, spread=0.5):
    return {
        d: {
            "open": c,
            "high": c + spread,
            "low": c - spread,
            "close": c,
        }
        for d, c in zip(dates(len(closes)), closes)
    }


def close_prices(closes):
    return {d: c for d, c in zip(dates(len(closes)), closes)}


def ema(vals, n):
    a = 2.0 / (n + 1)
    out, e = [], None
    for v in vals:
        e = v if e is None else a * v + (1 - a) * e
        out.append(e)
    return out


class QqqTqqqStrategyTests(unittest.TestCase):
    def test_atr14_uses_wilder_smoothing(self):
        trs = list(range(1, 17))
        rows = [
            {"date": dates(len(trs))[i], "open": 100, "high": 100 + tr, "low": 100, "close": 100}
            for i, tr in enumerate(trs)
        ]

        atr = generate._atr14(rows)

        first_14 = sum(trs[:14]) / 14.0
        self.assertAlmostEqual(atr[13], first_14)
        self.assertAlmostEqual(atr[14], (first_14 * 13 + trs[14]) / 14.0)
        self.assertAlmostEqual(atr[15], (atr[14] * 13 + trs[15]) / 14.0)

    def test_overheat_state_zones_ccs_and_account_extracts(self):
        q_closes = [100 + i for i in range(70)]
        t_closes = [30 + i * 0.7 for i in range(70)]
        prices = {"QQQ": close_prices(q_closes), "TQQQ": close_prices(t_closes)}
        ohlc = {"QQQ": ohlc_from_closes(q_closes), "TQQQ": ohlc_from_closes(t_closes, 0.25)}
        cur = {
            "QQQ": {"shares": 10, "avg": 120, "value": 1690, "gain": 490, "gainpct": 40.83},
            "TQQQ": {"shares": 3, "avg": 60, "value": 234.9, "gain": 54.9, "gainpct": 30.5},
        }
        account = {"optLegs": [
            {"sym": "-QQQ260618C170", "name": "QQQ JUN 18 2026 $170 CALL", "qty": 1, "mark": 120, "side": "long", "type": "Margin"},
            {"sym": "-SPY260618C600", "name": "SPY JUN 18 2026 $600 CALL", "qty": 1, "mark": 90, "side": "long", "type": "Margin"},
        ]}

        out = generate.build_qqq_tqqq_strategy(prices, ohlc, cur, account, dates(70)[-1])
        latest = out["latest"]
        expected_e8 = ema(q_closes, 8)[-1]

        self.assertTrue(out["available"])
        self.assertEqual(out["source"], {"qqq": "OHLC", "tqqq": "OHLC"})
        self.assertEqual(out["state"]["code"], "overheat")
        self.assertEqual([r["key"] for r in out["rules"] if r["active"]], ["overheat"])
        self.assertAlmostEqual(latest["ema8"], round(expected_e8, 2))
        self.assertAlmostEqual(out["zones"]["ema8Buyback"][0], round(expected_e8 - 0.5 * latest["atr14"], 2))
        self.assertAlmostEqual(out["zones"]["ema8Buyback"][1], round(expected_e8 + 0.5 * latest["atr14"], 2))
        self.assertEqual(out["holdings"]["QQQ"]["shares"], 10)
        self.assertEqual(len(out["optionLegs"]), 1)
        self.assertIn("QQQ", out["optionLegs"][0]["name"])

        spot = t_closes[-1]
        self.assertEqual(out["tqqqCcs"]["spot"], round(spot, 2))
        self.assertEqual(out["tqqqCcs"]["shortRange"], [round(spot * 1.03, 2), round(spot * 1.06, 2)])
        self.assertEqual(out["tqqqCcs"]["example9122"]["shortPct"], round((91 / spot - 1) * 100, 1))

    def test_tqqq_option_plan_separates_stock_buys_from_option_contracts(self):
        q_closes = [100 + i for i in range(70)]
        t_closes = [30 + i * 0.7 for i in range(70)]
        prices = {"QQQ": close_prices(q_closes), "TQQQ": close_prices(t_closes)}
        ohlc = {"QQQ": ohlc_from_closes(q_closes), "TQQQ": ohlc_from_closes(t_closes, 0.25)}
        cur = {
            "QQQ": {"shares": 5, "avg": 120, "value": 845, "gain": 245, "gainpct": 40.83},
            "TQQQ": {"shares": 4.593, "avg": 80, "value": 362.0, "gain": 10.0, "gainpct": 2.84},
        }
        account = {
            "cashTotal": 751.0,
            "optLegs": [
                {"sym": "-QQQ260618C708", "name": "QQQ JUN 18 2026 $708 CALL",
                 "qty": 1, "mark": 3860, "side": "long", "type": "Margin"},
                {"sym": "-QQQ260618C711", "name": "QQQ JUN 18 2026 $711 CALL",
                 "qty": -1, "mark": -3995, "side": "short", "type": "Margin"},
            ],
            "optionSpreads": [],
        }
        opt_txns = {
            "-QQQ260618C708": [{"date": dates(70)[-2], "side": "BUY", "qty": 1, "price": 20.84, "amount": -2084.67}],
            "-QQQ260618C711": [{"date": dates(70)[-2], "side": "SELL", "qty": 1, "price": 19.17, "amount": 1916.29}],
        }

        out = generate.build_qqq_tqqq_strategy(prices, ohlc, cur, account, dates(70)[-1], opt_txns)
        plan = out["tqqqOptions"]

        self.assertIn("没有 TQQQ 期权合约", plan["status"])
        self.assertEqual(plan["currentTqqqLegs"], [])
        self.assertEqual(len(plan["currentQqqLegs"]), 2)
        self.assertEqual(plan["recentTqqqOrders"], [])
        self.assertEqual(len(plan["recentQqqOrders"]), 2)
        by_name = {x["name"]: x["status"] for x in plan["structures"]}
        self.assertEqual(by_name["TQQQ covered call"], "BLOCK")
        self.assertEqual(by_name["TQQQ cash-secured put"], "BLOCK")

    def test_break_state_overrides_pullback_setups(self):
        q_closes = [100 + i for i in range(60)] + [121, 118]
        t_closes = [30 + i * 0.5 for i in range(len(q_closes))]
        prices = {"QQQ": close_prices(q_closes), "TQQQ": close_prices(t_closes)}
        ohlc = {"QQQ": ohlc_from_closes(q_closes), "TQQQ": ohlc_from_closes(t_closes, 0.25)}

        out = generate.build_qqq_tqqq_strategy(prices, ohlc, {}, {"optLegs": []}, dates(len(q_closes))[-1])

        self.assertEqual(out["state"]["code"], "break")
        self.assertTrue(out["latest"]["twoBelowEma21"])
        self.assertEqual([r["key"] for r in out["rules"] if r["active"]], ["break"])

    def test_option_spread_ledger_pairs_verticals_and_flags_bad_marks(self):
        legs = [
            {"acct": "A", "sym": "-QQQ260618C708", "name": "QQQ JUN 18 2026 $708 CALL",
             "qty": 1, "mark": 3860, "side": "long", "type": "Margin"},
            {"acct": "A", "sym": "-QQQ260618C711", "name": "QQQ JUN 18 2026 $711 CALL",
             "qty": -1, "mark": -3995, "side": "short", "type": "Margin"},
            {"acct": "A", "sym": "-TER260618P185", "name": "TER JUN 18 2026 $185 PUT",
             "qty": 1, "mark": 700, "side": "long", "type": "Margin"},
            {"acct": "A", "sym": "-TER260618P175", "name": "TER JUN 18 2026 $175 PUT",
             "qty": -1, "mark": -200, "side": "short", "type": "Margin"},
        ]

        spreads = generate.build_option_spreads(legs, "2026-06-03")
        by_underlying = {s["underlying"]: s for s in spreads}

        self.assertEqual(set(by_underlying), {"QQQ", "TER"})
        self.assertEqual(by_underlying["QQQ"]["kind"], "bull call debit spread")
        self.assertEqual(by_underlying["QQQ"]["expectedRange"], [0, 300])
        self.assertEqual(by_underlying["QQQ"]["netMark"], -135)
        self.assertTrue(by_underlying["QQQ"]["warnings"])
        self.assertEqual(by_underlying["TER"]["kind"], "bear put debit spread")
        self.assertEqual(by_underlying["TER"]["expectedRange"], [0, 1000])
        self.assertEqual(by_underlying["TER"]["netMark"], 500)
        self.assertEqual(by_underlying["TER"]["warnings"], [])


if __name__ == "__main__":
    unittest.main()

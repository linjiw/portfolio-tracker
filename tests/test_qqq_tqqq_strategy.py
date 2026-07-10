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
        self.assertEqual(atr[:13], [None] * 13)
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
        self.assertTrue(out["tqqqAligned"])
        self.assertEqual(out["sourceAsOf"], {"qqq": dates(70)[-1], "tqqq": dates(70)[-1]})
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

    def test_tqqq_dependent_outputs_are_withheld_when_dates_do_not_align(self):
        q_closes = [100 + i for i in range(70)]
        t_closes = [30 + i * 0.7 for i in range(69)]
        prices = {"QQQ": close_prices(q_closes), "TQQQ": close_prices(t_closes)}
        ohlc = {"QQQ": ohlc_from_closes(q_closes),
                "TQQQ": ohlc_from_closes(t_closes, 0.25)}

        out = generate.build_qqq_tqqq_strategy(
            prices, ohlc, {}, {"cashTotal": 100_000, "optLegs": []}, dates(70)[-1])

        self.assertFalse(out["tqqqAligned"])
        self.assertEqual(dates(69)[-1], out["sourceAsOf"]["tqqq"])
        self.assertIsNone(out["latest"]["tqqq"])
        self.assertIsNone(out["latest"]["tqqqRet5"])
        self.assertIsNone(out["tqqqCcs"])
        self.assertIsNone(out["trailing"]["tqqqAtr14"])
        self.assertTrue(all(
            row["status"] == "BLOCK" for row in out["tqqqOptions"]["structures"]))

    def test_mutually_aligned_but_stale_qqq_tqqq_feeds_block_the_strategy(self):
        q_closes = [100 + i for i in range(70)]
        t_closes = [30 + i * 0.7 for i in range(70)]
        prices = {"QQQ": close_prices(q_closes), "TQQQ": close_prices(t_closes)}
        ohlc = {"QQQ": ohlc_from_closes(q_closes),
                "TQQQ": ohlc_from_closes(t_closes, 0.25)}
        requested = (datetime.date.fromisoformat(dates(70)[-1])
                     + datetime.timedelta(days=1)).isoformat()

        out = generate.build_qqq_tqqq_strategy(
            prices, ohlc, {}, {"cashTotal": 100_000, "optLegs": []}, requested)

        self.assertFalse(out["available"])
        self.assertEqual("BLOCK_DATA", out["dataStatus"])
        self.assertEqual(dates(70)[-1], out["sourceAsOf"]["qqq"])
        self.assertIn("does not match", out["reason"])

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

    def test_price_reclaim_with_broken_ribbon_has_noncontradictory_repair_brief(self):
        q_closes = [100 + i * 0.5 for i in range(60)] + [123] * 8 + [124]
        t_closes = [30 + i * 0.2 for i in range(len(q_closes))]
        prices = {"QQQ": close_prices(q_closes), "TQQQ": close_prices(t_closes)}
        ohlc = {"QQQ": ohlc_from_closes(q_closes), "TQQQ": ohlc_from_closes(t_closes, 0.25)}

        out = generate.build_qqq_tqqq_strategy(
            prices, ohlc, {"QQQ": {"shares": 1, "value": 124}}, {"optLegs": []}, dates(len(q_closes))[-1],
        )

        self.assertEqual("repair", out["state"]["code"])
        self.assertTrue(out["latest"]["priceAboveEma21"])
        self.assertTrue(out["latest"]["ema8UnderEma21"])
        self.assertIn("价格已收复 EMA21", out["decisionPanel"]["doNow"])
        self.assertNotIn("等重新站回 EMA21", out["decisionPanel"]["doNow"])
        self.assertEqual("重新失守 EMA21", out["nextTriggers"][-1]["name"])
        debit = next(row for row in out["tqqqOptions"]["structures"] if row["name"] == "TQQQ call debit spread")
        self.assertEqual("BLOCK", debit["status"])

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
        self.assertEqual(by_underlying["QQQ"]["longSym"], "-QQQ260618C708")
        self.assertEqual(by_underlying["QQQ"]["shortSym"], "-QQQ260618C711")
        self.assertEqual(by_underlying["QQQ"]["expectedRange"], [0, 300])
        self.assertEqual(by_underlying["QQQ"]["netMark"], -135)
        self.assertTrue(by_underlying["QQQ"]["warnings"])
        self.assertEqual(by_underlying["TER"]["kind"], "bear put debit spread")
        self.assertEqual(by_underlying["TER"]["expectedRange"], [0, 1000])
        self.assertEqual(by_underlying["TER"]["netMark"], 500)
        self.assertEqual(by_underlying["TER"]["warnings"], [])

        # Fully paired vertical legs must not reappear as residual naked risk.
        risk = generate.build_unpaired_option_risk(
            {"optLegs": legs, "cashByAccount": {}, "equitySharesByAccount": {}}, spreads)
        self.assertEqual(risk["rows"], [])
        self.assertEqual(risk["summary"]["unpairedLegs"], 0)

    def test_option_spread_parser_accepts_comma_formatted_strikes(self):
        legs = [
            {"acct": "A", "sym": "-NDXP260710P27800",
             "name": "NDXP JUL 10 2026 $27,800 PUT", "qty": 1, "mark": 75},
            {"acct": "A", "sym": "-NDXP260710P27900",
             "name": "NDXP JUL 10 2026 $27,900 PUT", "qty": -1, "mark": -300},
        ]

        spreads = generate.build_option_spreads(legs, "2026-07-09")

        self.assertEqual(len(spreads), 1)
        self.assertEqual(spreads[0]["underlying"], "NDXP")
        self.assertEqual(spreads[0]["longStrike"], 27800)
        self.assertEqual(spreads[0]["shortStrike"], 27900)
        self.assertEqual(spreads[0]["dte"], 1)

    def test_vertical_pairing_allocates_contract_quantities_across_multiple_legs(self):
        legs = [
            {"acct": "A", "sym": "-QQQ260710C700", "name": "QQQ JUL 10 2026 $700 CALL",
             "qty": 3, "mark": 900, "costBasis": 750, "brokerPnl": 150},
            {"acct": "A", "sym": "-QQQ260710C705", "name": "QQQ JUL 10 2026 $705 CALL",
             "qty": -1, "mark": -200, "costBasis": 250, "brokerPnl": 50},
            {"acct": "A", "sym": "-QQQ260710C710", "name": "QQQ JUL 10 2026 $710 CALL",
             "qty": -2, "mark": -300, "costBasis": 400, "brokerPnl": 100},
        ]

        spreads = generate.build_option_spreads(legs, "2026-07-09")
        risk = generate.build_unpaired_option_risk(
            {"optLegs": legs, "cashByAccount": {}, "equitySharesByAccount": {}}, spreads)

        self.assertEqual(len(spreads), 2)
        self.assertEqual(sum(row["contracts"] for row in spreads), 3)
        self.assertEqual({row["shortStrike"] for row in spreads}, {705, 710})
        self.assertTrue(all(row["warnings"] for row in spreads))
        self.assertEqual(risk["rows"], [])

    def test_spread_ledger_uses_broker_basis_for_exact_risk(self):
        legs = [
            {"acct": "A", "sym": "-NDXP260710P27800", "name": "NDXP JUL 10 2026 $27,800 PUT",
             "qty": 1, "mark": 75, "costBasis": 7603.11, "brokerPnl": -7528.11},
            {"acct": "A", "sym": "-NDXP260710P27900", "name": "NDXP JUL 10 2026 $27,900 PUT",
             "qty": -1, "mark": -300, "costBasis": 8385.89, "brokerPnl": 8085.89},
        ]

        spread = generate.build_option_spreads(legs, "2026-07-09")[0]

        self.assertEqual(spread["entryType"], "credit")
        self.assertEqual(spread["entryCredit"], 782.78)
        self.assertEqual(spread["maxProfit"], 782.78)
        self.assertEqual(spread["maxLoss"], 9217.22)
        self.assertEqual(spread["currentPnl"], 557.78)

    def test_unpaired_option_risk_distinguishes_margin_put_and_covered_call(self):
        account = {
            "cashByAccount": {"A": 65},
            "equitySharesByAccount": {"A": {"NVDA": 126.9}},
            "optLegs": [
                {"acct": "A", "sym": "-HBMX261218P40", "name": "HBMX DEC 18 2026 $40 PUT",
                 "qty": -1, "mark": -1780, "costBasis": 1417.31, "brokerPnl": -362.69},
                {"acct": "A", "sym": "-NVDA260710C235", "name": "NVDA JUL 10 2026 $235 CALL",
                 "qty": -1, "mark": -1, "costBasis": 91.33, "brokerPnl": 90.33},
            ],
        }

        risk = generate.build_unpaired_option_risk(account, [])
        by_sym = {row["sym"]: row for row in risk["rows"]}

        self.assertEqual(by_sym["-HBMX261218P40"]["riskClass"], "MARGIN_SHORT_PUT")
        self.assertEqual(by_sym["-HBMX261218P40"]["assignmentValue"], 4000)
        self.assertEqual(by_sym["-NVDA260710C235"]["riskClass"], "COVERED_CALL")
        self.assertEqual(risk["summary"]["coveredCallContracts"], 1)
        self.assertEqual(risk["summary"]["marginShortPutContracts"], 1)

    def test_terminal_option_action_closes_lifecycle_net_quantity(self):
        sym = "-QQQ260710C750"
        rows = {
            sym: [
                {"date": "2026-07-01", "side": "BUY", "qty": 1.0,
                 "price": 2.0, "amount": -200.0, "actionType": "TRADE"},
                {"date": "2026-07-10", "side": "OTHER", "qty": 1.0,
                 "price": 0.0, "amount": 0.0, "actionType": "EXPIRED"},
            ]
        }

        summary = generate._option_trade_summary(rows, ("QQQ",))[0]

        self.assertEqual(summary["netQty"], 0.0)
        self.assertEqual(summary["txns"][-1]["actionType"], "EXPIRED")


if __name__ == "__main__":
    unittest.main()

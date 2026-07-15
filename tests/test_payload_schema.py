"""Schema validation for the JSON payload embedded in the dashboard HTML.

The dashboard's JS template consumes ``DATA.<key>`` for a fixed set of
top-level keys. ``build_payload`` produces the core keys; the optional
research layers (decision / aiSemiQuant / semiLeverage / aiWatchlist / aics / marketMass /
momentumTop3 / financialStatus) are attached in ``main`` via ``load_*`` helpers. These tests
build a payload from a small synthetic portfolio and assert:

* every UI-expected top-level key is present (core + optional loaders),
* value types match what the template expects,
* P&L invariants hold (value = shares*price, total = sum of positions,
  gain = value - cost),
* date strings are ISO YYYY-MM-DD,
* the payload is strict-JSON serializable (no NaN/Infinity anywhere).
"""
import datetime
import json
import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import generate

ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Keys the HTML/JS template reads as DATA.<key> (grep 'DATA\.' in generate.py).
UI_CORE_KEYS = {
    "summary", "stocks", "options", "series", "portfolioFib", "behavior",
    "risk", "bridge", "account", "alloc", "qqqTqqq", "counterfactual",
    "optionSettlements",
}
UI_OPTIONAL_KEYS = {
    "decision", "aiSemiQuant", "semiLeverage", "aiWatchlist", "aics", "marketMass",
    "momentumTop3", "financialStatus", "closeVsIntraday", "artifactHealth",
}


def trading_days(start, n):
    """n weekday (Mon-Fri) ISO dates starting at start."""
    d = datetime.date.fromisoformat(start)
    out = []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += datetime.timedelta(days=1)
    return out


# 30 sessions: compute_risk needs >=25 days of curve to return a dict.
DATES = trading_days("2026-05-01", 30)
DMIN, DMAX = DATES[0], DATES[-1]


def make_fixture():
    """Small synthetic two-stock portfolio with benchmarks over 30 sessions."""
    txns = {
        "AAA": [
            {"date": DATES[0], "side": "BUY", "qty": 10.0, "price": 100.0, "amount": -1000.0},
            {"date": DATES[10], "side": "SELL", "qty": 4.0, "price": 120.0, "amount": 480.0},
        ],
        "BBB": [
            {"date": DATES[2], "side": "BUY", "qty": 5.0, "price": 50.0, "amount": -250.0},
        ],
    }
    opt_txns = {
        "-AAA260620C110": [
            {"date": DATES[5], "side": "SELL", "qty": 1.0, "price": 2.0, "amount": 200.0},
        ],
    }
    names = {"AAA": "Alpha Corp", "BBB": "Beta Inc"}
    # Gently wobbly uptrends (non-constant daily returns keep vol/beta finite).
    prices = {
        "AAA": {d: round(100.0 + i * 2 + (3 if i % 4 == 0 else 0), 2)
                for i, d in enumerate(DATES)},
        "BBB": {d: round(50.0 + i + (1.5 if i % 3 == 0 else 0), 2)
                for i, d in enumerate(DATES)},
        "^GSPC": {d: round(5000.0 + i * 10 + (12 if i % 5 == 0 else 0), 2)
                  for i, d in enumerate(DATES)},
        "^IXIC": {d: round(16000.0 + i * 20 + (25 if i % 5 == 0 else 0), 2)
                  for i, d in enumerate(DATES)},
    }
    # Broker snapshot at last close — gain = value - cost by construction.
    last_a = prices["AAA"][DMAX]
    last_b = prices["BBB"][DMAX]
    cur = {
        "AAA": {"shares": 6.0, "price": last_a, "value": round(6.0 * last_a, 2),
                "gain": round(6.0 * last_a - 600.0, 2), "cost": 600.0,
                "avg": 100.0, "gainpct": round((6.0 * last_a - 600.0) / 600.0 * 100, 4)},
        "BBB": {"shares": 5.0, "price": last_b, "value": round(5.0 * last_b, 2),
                "gain": round(5.0 * last_b - 250.0, 2), "cost": 250.0,
                "avg": 50.0, "gainpct": round((5.0 * last_b - 250.0) / 250.0 * 100, 4)},
    }
    return txns, opt_txns, names, cur, prices


def build():
    txns, opt_txns, names, cur, prices = make_fixture()
    return generate.build_payload(
        txns, opt_txns, names, cur, prices,
        deposits=1250.0, totals=(1250.0, 480.0),
        dmin=DMIN, dmax=DMAX,
        dividends=5.0, life_deposits=1250.0)


class PayloadSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = build()

    # ---------------- top-level shape ----------------
    def test_core_ui_keys_present(self):
        self.assertEqual(set(self.payload.keys()), UI_CORE_KEYS)

    def test_optional_loader_keys_cover_remaining_ui_keys(self):
        # main() attaches each optional layer via its load_* helper; with no
        # artifact files present every loader must return None (never raise),
        # so the template can rely on the key existing.
        with tempfile.TemporaryDirectory() as td:
            health = {}
            optional = {
                "decision": generate.load_decision_analysis(td, None, health),
                "aiSemiQuant": generate.load_ai_semi_quant(td, None, health),
                "semiLeverage": generate.load_semi_leverage(td, None, health),
                "aiWatchlist": generate.load_ai_watchlist(td, None, health),
                "aics": generate.load_aics_payload(td, None, health),
                "marketMass": generate.load_market_mass_dashboard(td, None, health),
                "momentumTop3": generate.load_momentum_top3(td, None, health),
                "financialStatus": generate.load_financial_status(td, None, health),
                "closeVsIntraday": generate.load_close_vs_intraday(td, None, health),
                "artifactHealth": health,
            }
        self.assertEqual(set(optional.keys()), UI_OPTIONAL_KEYS)
        for k, v in optional.items():
            if k == "artifactHealth":
                continue
            self.assertIsNone(v, f"loader for {k} should return None when file absent")
        self.assertEqual(set(health), UI_OPTIONAL_KEYS - {"artifactHealth"})
        full = dict(self.payload, **optional)
        self.assertEqual(set(full.keys()), UI_CORE_KEYS | UI_OPTIONAL_KEYS)

    def test_close_vs_intraday_loader_is_tolerant_and_reads_valid_artifact(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "close_vs_intraday.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("not json")
            self.assertIsNone(generate.load_close_vs_intraday(td))
            expected = {
                "schemaVersion": 1, "generatedAt": "2026-07-09T20:00:00Z",
                "asOf": "2026-07-09", "windows": {},
                "researchOnly": True, "decisionGrade": False,
            }
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(expected, handle)
            self.assertEqual(generate.load_close_vs_intraday(td), expected)

    def test_top_level_types(self):
        p = self.payload
        self.assertIsInstance(p["summary"], dict)
        self.assertIsInstance(p["stocks"], list)
        self.assertIsInstance(p["options"], list)
        self.assertIsInstance(p["series"], list)
        self.assertIsInstance(p["behavior"], dict)
        self.assertIsInstance(p["risk"], dict)
        self.assertIsInstance(p["bridge"], dict)
        self.assertIsInstance(p["alloc"], dict)
        self.assertIsNone(p["account"])          # no account snapshot supplied
        self.assertIsInstance(p["counterfactual"], (dict, list, type(None)))

    def test_future_cache_rows_are_not_embedded_or_used_by_indicators(self):
        txns, opt_txns, names, cur, prices = make_fixture()
        future = (datetime.date.fromisoformat(DMAX) + datetime.timedelta(days=1)).isoformat()
        prices["AAA"][future] = 9999.0
        prices["BBB"][future] = 9999.0
        prices["^GSPC"][future] = 99999.0

        payload = generate.build_payload(
            txns, opt_txns, names, cur, prices,
            deposits=1250.0, totals=(1250.0, 480.0),
            dmin=DMIN, dmax=DMAX,
            dividends=5.0, life_deposits=1250.0)

        self.assertTrue(all(day <= DMAX for stock in payload["stocks"]
                            for day, _price in stock["prices"]))
        self.assertTrue(all(row["date"] <= DMAX for row in payload["series"]))
        aaa = next(stock for stock in payload["stocks"] if stock["sym"] == "AAA")
        self.assertNotEqual(aaa["fib"]["now"]["mom"], 100.0)

    # ---------------- summary ----------------
    def test_summary_types_and_counts(self):
        s = self.payload["summary"]
        for key in ("marketValue", "unrealized", "realized", "netInvested",
                    "totalBuy", "totalSell", "deposits", "lifeDeposits",
                    "dividends", "optNet", "netWorthNow", "netWorthStart",
                    "curReturn"):
            self.assertIsInstance(s[key], (int, float), key)
            self.assertNotIsInstance(s[key], bool, key)
        self.assertIsInstance(s["dateRange"], list)
        self.assertEqual(len(s["dateRange"]), 2)
        self.assertEqual(s["numStocks"], 2)
        self.assertEqual(s["numHeld"], 2)
        self.assertIsInstance(s["priceMode"], str)
        self.assertIsInstance(s["fetchOK"], bool)
        self.assertIsInstance(s["fetchStale"], bool)

    def test_summary_pnl_invariants(self):
        s = self.payload["summary"]
        stocks = self.payload["stocks"]
        held = [st for st in stocks if st["held"]]
        # total market value = sum of held positions
        self.assertAlmostEqual(s["marketValue"], sum(st["value"] for st in held), places=2)
        # total unrealized = sum of held positions' unrealized
        self.assertAlmostEqual(s["unrealized"], sum(st["unreal"] for st in held), places=2)
        # total realized = sum over all positions
        self.assertAlmostEqual(s["realized"], sum(st["realized"] for st in stocks), places=2)
        # net invested = buys - sells
        self.assertAlmostEqual(s["netInvested"], s["totalBuy"] - s["totalSell"], places=2)
        # fixture ground truth: realized = (120-100)*4; net invested 1250-480
        self.assertAlmostEqual(s["realized"], 80.0, places=2)
        self.assertAlmostEqual(s["netInvested"], 770.0, places=2)

    def test_summary_exposes_return_and_realized_quality(self):
        s = self.payload["summary"]

        self.assertEqual(s["twrQuality"]["status"], "complete")
        self.assertEqual(s["twrQuality"]["usableSessions"], len(DATES))
        self.assertEqual(s["realizedPnlQuality"]["confidence"], "medium")
        self.assertIn("average-cost", s["realizedPnlQuality"]["method"])

    def test_realized_pnl_uses_net_sale_cash_and_flags_cross_account_pooling(self):
        txns, opt_txns, names, cur, prices = make_fixture()
        txns["AAA"][0]["account"] = "A"
        txns["AAA"][1]["account"] = "B"
        txns["AAA"][1]["amount"] = 479.0  # $1 fee vs price × quantity

        payload = generate.build_payload(
            txns, opt_txns, names, cur, prices, deposits=1250.0,
            totals=(1250.0, 479.0), dmin=DMIN, dmax=DMAX)

        aaa = next(row for row in payload["stocks"] if row["sym"] == "AAA")
        self.assertEqual(aaa["realized"], 79.0)
        self.assertEqual(aaa["realizedBasisScope"], "cross-account-average-cost")
        self.assertTrue(any("pooled across accounts" in warning
                            for warning in aaa["realizedWarnings"]))
        self.assertEqual(aaa["realizedConfidence"], "medium")

    def test_twr_starts_only_when_every_held_symbol_has_a_past_price(self):
        txns, opt_txns, names, cur, prices = make_fixture()
        for d in DATES[:5]:
            prices["BBB"].pop(d)

        payload = generate.build_payload(
            txns, opt_txns, names, cur, prices, deposits=1250.0,
            totals=(1250.0, 480.0), dmin=DMIN, dmax=DMAX)

        self.assertEqual(payload["series"][0]["date"], DATES[5])
        quality = payload["summary"]["twrQuality"]
        self.assertEqual(quality["status"], "partial")
        self.assertEqual(quality["seriesStart"], DATES[5])
        self.assertEqual(quality["missingSymbols"], ["BBB"])
        self.assertTrue(any(x["date"] == DATES[2] for x in quality["failures"]))

    def test_mwr_uses_dated_cash_events_and_real_terminal_date(self):
        txns, opt_txns, names, cur, prices = make_fixture()
        payload = generate.build_payload(
            txns, opt_txns, names, cur, prices, deposits=1250.0,
            totals=(1250.0, 480.0), dmin=DMIN, dmax=DMAX,
            dividends=5.0, life_deposits=1250.0, price_as_of=DMAX,
            cash_events=[{"date": DATES[15], "kind": "DIV", "amount": 5.0,
                          "account": "A"}])

        quality = payload["summary"]["mwrQuality"]
        self.assertEqual(quality["terminalDate"], DMAX)
        self.assertEqual(quality["datedCashEventCount"], 1)
        self.assertEqual(quality["fallbackAggregateCash"], 0.0)
        self.assertTrue(quality["alignedWithTWR"])
        self.assertEqual(quality["status"], "complete")
        self.assertEqual(quality["exactTransactionFlowCount"], 3)
        self.assertEqual(quality["estimatedOpeningValue"], 0.0)

        expected_flows = [
            (datetime.date.fromisoformat(DATES[0]), -1000.0),
            (datetime.date.fromisoformat(DATES[2]), -250.0),
            (datetime.date.fromisoformat(DATES[10]), 480.0),
            (datetime.date.fromisoformat(DATES[15]), 5.0),
            (datetime.date.fromisoformat(DMAX), payload["summary"]["marketValue"]),
        ]
        expected_rate = generate.xirr(expected_flows)
        expected_period = ((1 + expected_rate) ** (
            (datetime.date.fromisoformat(DMAX) -
             datetime.date.fromisoformat(DMIN)).days / 365.0) - 1) * 100
        self.assertAlmostEqual(payload["summary"]["mwrPeriod"],
                               round(expected_period, 2))

    def test_mwr_downgrades_pre_window_inventory_estimate(self):
        txns, opt_txns, names, cur, prices = make_fixture()
        # One AAA share existed before the first usable session.
        cur["AAA"]["shares"] += 1.0
        cur["AAA"]["value"] += prices["AAA"][DMAX]
        cur["AAA"]["cost"] += prices["AAA"][DMIN]
        cur["AAA"]["gain"] = cur["AAA"]["value"] - cur["AAA"]["cost"]

        payload = generate.build_payload(
            txns, opt_txns, names, cur, prices, deposits=1250.0,
            totals=(1250.0, 480.0), dmin=DMIN, dmax=DMAX,
            cash_events=[])

        quality = payload["summary"]["mwrQuality"]
        self.assertEqual(quality["status"], "estimated-opening-market-value")
        self.assertEqual(quality["estimatedOpeningSymbols"], ["AAA"])
        self.assertAlmostEqual(quality["estimatedOpeningValue"],
                               prices["AAA"][DMIN])

    def test_nonconventional_cash_flows_withhold_xirr_headline(self):
        txns, opt_txns, names, cur, prices = make_fixture()
        txns["AAA"].extend([
            {"date": DATES[5], "side": "SELL", "qty": 2.0,
             "price": 110.0, "amount": 220.0},
            {"date": DATES[8], "side": "BUY", "qty": 2.0,
             "price": 115.0, "amount": -230.0},
        ])

        payload = generate.build_payload(
            txns, opt_txns, names, cur, prices, deposits=1250.0,
            totals=(1480.0, 700.0), dmin=DMIN, dmax=DMAX,
            price_as_of=DMAX, cash_events=[])

        summary = payload["summary"]
        quality = summary["mwrQuality"]
        self.assertEqual(quality["status"], "review-nonconventional-cashflows")
        self.assertGreater(quality["cashFlowSignChanges"], 1)
        self.assertIsNotNone(quality["candidatePeriodPct"])
        self.assertFalse(quality["published"])
        self.assertIsNone(summary["mwrPeriod"])
        self.assertIsNone(summary["mwrAnnual"])
        self.assertIsNone(summary["behaviorGap"])

    def test_settlement_accounting_and_behavior_provenance(self):
        txns, opt_txns, names, cur, prices = make_fixture()
        # Same-day, same-account exercise/assignment delivery is net-zero stock
        # exposure and belongs to the option lifecycle, not equity realized P&L.
        txns["AAA"].extend([
            {"date": DATES[15], "side": "BUY", "qty": 100.0, "price": 130.0,
             "amount": -13000.0, "account": "A", "actionType": "OPTION_SETTLEMENT",
             "settlementType": "EXERCISED"},
            {"date": DATES[15], "side": "SELL", "qty": 100.0, "price": 120.0,
             "amount": 12000.0, "account": "A", "actionType": "OPTION_SETTLEMENT",
             "settlementType": "ASSIGNED"},
            # An unpaired delivery must remain in share/cost accounting.
            {"date": DATES[16], "side": "BUY", "qty": 2.0, "price": 80.0,
             "amount": -160.0, "account": "A", "actionType": "OPTION_SETTLEMENT",
             "settlementType": "ASSIGNED"},
        ])
        txns["BBB"].append(
            {"date": DATES[20], "side": "BUY", "qty": 1.0, "price": 60.0,
             "amount": -60.0, "account": "A", "actionType": "REINVESTMENT"})
        cur["AAA"].update({
            "shares": 8.0, "value": round(8 * cur["AAA"]["price"], 2),
            "cost": 760.0, "gain": round(8 * cur["AAA"]["price"] - 760, 2),
            "avg": 95.0,
        })
        cur["AAA"]["gainpct"] = cur["AAA"]["gain"] / cur["AAA"]["cost"] * 100
        cur["BBB"].update({
            "shares": 6.0, "value": round(6 * cur["BBB"]["price"], 2),
            "cost": 310.0, "gain": round(6 * cur["BBB"]["price"] - 310, 2),
            "avg": 310 / 6,
        })
        cur["BBB"]["gainpct"] = cur["BBB"]["gain"] / cur["BBB"]["cost"] * 100

        payload = generate.build_payload(
            txns, opt_txns, names, cur, prices, deposits=1250.0,
            totals=(999999.0, 999999.0), dmin=DMIN, dmax=DMAX,
            dividends=5.0, life_deposits=1250.0, corporate_action_cash=0.88)

        s = payload["summary"]
        self.assertEqual(s["realized"], 80.0)
        self.assertEqual(s["totalBuy"], 1470.0)
        self.assertEqual(s["totalSell"], 480.0)
        self.assertEqual(s["optionSettlementCash"], -1160.0)
        self.assertEqual(s["pairedOptionSettlementCash"], -1000.0)
        self.assertEqual(s["corporateActionCash"], 0.88)
        self.assertEqual(s["optionSettlementGroups"], 2)
        self.assertEqual(payload["behavior"]["stats"]["trades"], 3)

        aaa = next(x for x in payload["stocks"] if x["sym"] == "AAA")
        self.assertEqual([t["actionType"] for t in aaa["txns"]],
                         ["TRADE", "TRADE", "OPTION_SETTLEMENT"])
        self.assertEqual(aaa["shares"], 8.0)
        paired = next(x for x in payload["optionSettlements"] if x["pairedNetZero"])
        self.assertEqual(paired["accountingTreatment"], "excluded_from_equity_realized")
        unpaired = next(x for x in payload["optionSettlements"] if not x["pairedNetZero"])
        self.assertEqual(unpaired["accountingTreatment"], "included_in_share_accounting")

    # ---------------- per-stock ----------------
    def test_stock_row_schema(self):
        for st in self.payload["stocks"]:
            for key in ("sym", "name"):
                self.assertIsInstance(st[key], str)
            for key in ("held", "hasLegacy"):
                self.assertIsInstance(st[key], bool)
            for key in ("shares", "avg", "curPrice", "value", "unreal",
                        "unrealPct", "cost", "realized"):
                self.assertIsInstance(st[key], (int, float), f"{st['sym']}.{key}")
            self.assertIsInstance(st["numTrades"], int)
            self.assertIsInstance(st["prices"], list)
            self.assertIsInstance(st["txns"], list)

    def test_stock_pnl_invariants(self):
        for st in self.payload["stocks"]:
            if not st["held"]:
                continue
            # value = shares * price (within rounding: both rounded, so 2¢ slack)
            self.assertAlmostEqual(st["value"], st["shares"] * st["curPrice"], delta=0.02,
                                   msg=st["sym"])
            # unrealized gain = value - cost
            self.assertAlmostEqual(st["unreal"], st["value"] - st["cost"], delta=0.02,
                                   msg=st["sym"])

    def test_stock_fib_stripped_to_ui_keys(self):
        # PAYLOAD DIET: per-stock fib must ship only signals/resonance/now
        # (ribbons are recomputed browser-side).
        for st in self.payload["stocks"]:
            if st["fib"] is not None:
                self.assertEqual(set(st["fib"].keys()), {"signals", "resonance", "now"},
                                 st["sym"])

    # ---------------- options ----------------
    def test_options_schema_and_net(self):
        opts = self.payload["options"]
        self.assertEqual(len(opts), 1)
        o = opts[0]
        self.assertIsInstance(o["sym"], str)
        self.assertIsInstance(o["net"], (int, float))
        self.assertAlmostEqual(o["net"], 200.0, places=2)
        self.assertAlmostEqual(self.payload["summary"]["optNet"],
                               sum(x["net"] for x in opts), places=2)
        for t in o["txns"]:
            self.assertTrue(ISO_DATE.match(t["date"]), t["date"])

    # ---------------- series ----------------
    def test_series_schema(self):
        series = self.payload["series"]
        self.assertEqual(len(series), len(DATES))  # one point per benchmark session
        for pt in series:
            self.assertTrue(ISO_DATE.match(pt["date"]), pt["date"])
            self.assertIsInstance(pt["value"], (int, float))
            self.assertIsInstance(pt["ret"], (int, float))
            self.assertIn("sp500", pt)
            self.assertIn("nasdaq", pt)
        # dates strictly ascending, first cumulative return is 0
        dates = [pt["date"] for pt in series]
        self.assertEqual(dates, sorted(dates))
        self.assertEqual(len(set(dates)), len(dates))
        self.assertEqual(series[0]["ret"], 0.0)
        self.assertAlmostEqual(self.payload["summary"]["netWorthNow"],
                               series[-1]["value"], places=2)

    # ---------------- dates ----------------
    def test_all_dates_are_iso(self):
        s = self.payload["summary"]
        for d in s["dateRange"] + [s["priceAsOf"]]:
            self.assertTrue(ISO_DATE.match(d), d)
            datetime.date.fromisoformat(d)  # must be a real calendar date
        for st in self.payload["stocks"]:
            for d, _ in st["prices"]:
                self.assertTrue(ISO_DATE.match(d), d)
            for row in st["txns"]:
                # legacy pre-window OPEN rows are prefixed '≤'
                d = row["date"].lstrip("≤")
                self.assertTrue(ISO_DATE.match(d), row["date"])

    # ---------------- bridge ----------------
    def test_bridge_invariants(self):
        b = self.payload["bridge"]
        s = self.payload["summary"]
        legs = {leg["key"]: leg["amount"] for leg in b["legs"]}
        self.assertEqual(set(legs), {"unreal", "real", "div", "opt", "optSettlement"})
        self.assertAlmostEqual(b["totalPL"], sum(legs.values()), places=2)
        self.assertAlmostEqual(legs["unreal"], s["unrealized"], places=2)
        self.assertAlmostEqual(legs["real"], s["realized"], places=2)
        self.assertAlmostEqual(legs["div"], s["dividends"], places=2)
        self.assertAlmostEqual(legs["opt"], s["optNet"], places=2)
        self.assertAlmostEqual(legs["optSettlement"], s["pairedOptionSettlementCash"], places=2)
        # broker identity: held cost + unrealized = terminal market value
        self.assertAlmostEqual(b["heldCost"] + legs["unreal"], b["terminal"], delta=0.02)

    # ---------------- strict JSON ----------------
    def test_payload_is_strict_json_no_nan_or_infinity(self):
        # Exactly what render_html embeds — must survive allow_nan=False.
        text = json.dumps(self.payload, ensure_ascii=False,
                          separators=(",", ":"), allow_nan=False)
        self.assertNotIn("NaN", text)
        self.assertNotIn("Infinity", text)
        # round-trips
        self.assertEqual(json.loads(text)["summary"]["numStocks"], 2)

    def test_render_html_embeds_payload(self):
        html = generate.render_html(self.payload)
        self.assertNotIn("__DATA__", html)
        mv = self.payload["summary"]["marketValue"]
        self.assertIn(f'"marketValue":{mv}', html)


if __name__ == "__main__":
    unittest.main()

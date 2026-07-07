"""Schema validation for the JSON payload embedded in the dashboard HTML.

The dashboard's JS template consumes ``DATA.<key>`` for a fixed set of
top-level keys. ``build_payload`` produces the core keys; the optional
research layers (decision / aiSemiQuant / aiWatchlist / aics / marketMass /
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
}
UI_OPTIONAL_KEYS = {
    "decision", "aiSemiQuant", "aiWatchlist", "aics", "marketMass",
    "momentumTop3", "financialStatus",
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
            optional = {
                "decision": generate.load_decision_analysis(td),
                "aiSemiQuant": generate.load_ai_semi_quant(td),
                "aiWatchlist": generate.load_ai_watchlist(td),
                "aics": generate.load_aics_payload(td),
                "marketMass": generate.load_market_mass_dashboard(td),
                "momentumTop3": generate.load_momentum_top3(td),
                "financialStatus": generate.load_financial_status(td),
            }
        self.assertEqual(set(optional.keys()), UI_OPTIONAL_KEYS)
        for k, v in optional.items():
            self.assertIsNone(v, f"loader for {k} should return None when file absent")
        full = dict(self.payload, **optional)
        self.assertEqual(set(full.keys()), UI_CORE_KEYS | UI_OPTIONAL_KEYS)

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
        self.assertEqual(set(legs), {"unreal", "real", "div", "opt"})
        self.assertAlmostEqual(b["totalPL"], sum(legs.values()), places=2)
        self.assertAlmostEqual(legs["unreal"], s["unrealized"], places=2)
        self.assertAlmostEqual(legs["real"], s["realized"], places=2)
        self.assertAlmostEqual(legs["div"], s["dividends"], places=2)
        self.assertAlmostEqual(legs["opt"], s["optNet"], places=2)
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

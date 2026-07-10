import csv
import os
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import generate
from scripts import refresh_latest_prices as refresh


def write_positions(path):
    header = ["Account Number", "Account Name", "Symbol", "Description", "Quantity",
              "Last Price", "Last Price Change", "Current Value", "Today's Gain/Loss Dollar",
              "Today's Gain/Loss Percent", "Total Gain/Loss Dollar", "Total Gain/Loss Percent",
              "Percent Of Account", "Cost Basis Total", "Average Cost Basis", "Type"]
    rows = [
        ["A", "Demo", "QQQ", "QQQ", "2", "$100", "", "$200", "", "", "$20", "", "", "$180", "", "Cash"],
        ["A", "Demo", "SPAXX**", "Cash", "", "", "", "$50", "", "", "", "", "", "", "", "Cash"],
        ["A", "Demo", "-QQQ260710C110", "Call", "-1", "$1", "", "-$100", "", "", "$20", "", "", "$120", "", "Margin"],
        ["A", "Demo", "Pending activity", "Pending", "", "", "", "-$5", "", "", "", "", "", "", "", "Cash"],
    ]
    with Path(path).open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)


def mtm_payload(**summary_overrides):
    summary = {
        "priceMode": "mark-to-market", "priceAsOf": "2026-07-09",
        "freshPriceSymbols": ["QQQ"], "stalePriceSymbols": {}, "missingPriceSymbols": [],
        "numHeld": 1, "marketValue": 250.0, "unrealized": 70.0,
        "cashTotal": 50.0, "pendingTotal": -5.0, "optMarkNet": -100.0,
        "optMarkGross": 100.0, "optBrokerPnl": 20.0, "optEntryCashNet": 120.0,
        "optLegCount": 1, "accountNetWorth": 195.0,
    }
    summary.update(summary_overrides)
    return {"summary": summary, "stocks": [
        {"sym": "QQQ", "held": True, "shares": 2.0, "cost": 180.0, "value": 250.0},
    ]}


class PriceRefreshTests(unittest.TestCase):
    def test_mtm_gate_verifies_freshness_and_broker_fingerprint(self):
        with tempfile.TemporaryDirectory() as td:
            portfolio = Path(td) / "positions.csv"
            write_positions(portfolio)
            expected = refresh.validate_mark_to_market(mtm_payload(), portfolio)

        self.assertEqual(expected["sharesBySymbol"], {"QQQ": 2.0})

    def test_mtm_gate_rejects_stale_prices_unless_explicitly_allowed(self):
        with tempfile.TemporaryDirectory() as td:
            portfolio = Path(td) / "positions.csv"
            write_positions(portfolio)
            payload = mtm_payload(freshPriceSymbols=[], stalePriceSymbols={"QQQ": "2026-07-08"})
            with self.assertRaisesRegex(RuntimeError, "freshness gate failed"):
                refresh.validate_mark_to_market(payload, portfolio)
            refresh.validate_mark_to_market(payload, portfolio, allow_stale=True)

    def test_mtm_gate_rejects_changed_share_fingerprint(self):
        with tempfile.TemporaryDirectory() as td:
            portfolio = Path(td) / "positions.csv"
            write_positions(portfolio)
            payload = mtm_payload()
            payload["stocks"][0]["shares"] = 1.0
            with self.assertRaisesRegex(RuntimeError, "share fingerprint mismatch"):
                refresh.validate_mark_to_market(payload, portfolio)

    def test_publish_private_is_atomic_and_private(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "dashboard.html"
            staged = Path(td) / ".dashboard.tmp"
            target.write_text("old", encoding="utf-8")
            staged.write_text("verified", encoding="utf-8")
            refresh.publish_private(staged, target)
            self.assertEqual(target.read_text(encoding="utf-8"), "verified")
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_direct_generator_private_write_is_atomic_and_private(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "dashboard.html"
            target.write_text("last-known-good", encoding="utf-8")

            generate._write_text_atomic_private(target, "new-dashboard")

            self.assertEqual(target.read_text(encoding="utf-8"), "new-dashboard")
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            self.assertEqual(list(Path(td).glob("dashboard.html.tmp.*")), [])

    def test_direct_generator_creates_private_output_directory(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "new-output" / "dashboard.html"

            generate._write_text_atomic_private(target, "dashboard")

            self.assertEqual(target.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_no_fetch_without_cache_never_calls_network(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(generate, "CACHE", str(Path(td) / "missing.json")), \
                mock.patch.dict(sys.modules, {"yfinance": mock.Mock()}):
            yf = sys.modules["yfinance"]

            out = generate.fetch_prices(["QQQ"], "2026-01-01", "2026-01-02", no_fetch=True)

        self.assertEqual(out, {})
        yf.download.assert_not_called()

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
        self.assertEqual(generate.MARK_STATUS["freshSymbols"], [])
        self.assertEqual(generate.MARK_STATUS["staleSymbols"], {"QQQ": "2026-06-03"})
        self.assertEqual(generate.MARK_STATUS["missingSymbols"], ["MISSING"])

    def test_exact_date_cache_is_not_labeled_stale(self):
        with mock.patch.object(generate, "MARK_STATUS", {
                "freshSymbols": ["QQQ"], "staleSymbols": {},
                "missingSymbols": [], "oldestAsOf": "2026-07-09"}), \
                mock.patch.object(generate, "FETCH_STATUS", {
                    "ok": True, "stale": True, "reason": "--no-fetch", "cacheAgeDays": 0}):
            generate.reconcile_fetch_freshness("2026-07-09", "2026-07-09")
            self.assertFalse(generate.FETCH_STATUS["stale"])
            self.assertEqual(generate.FETCH_STATUS["reason"], "exact-date cache verified")

    def test_older_or_missing_held_price_remains_stale(self):
        with mock.patch.object(generate, "MARK_STATUS", {
                "freshSymbols": [], "staleSymbols": {"QQQ": "2026-07-08"},
                "missingSymbols": ["ABC"], "oldestAsOf": "2026-07-08"}), \
                mock.patch.object(generate, "FETCH_STATUS", {
                    "ok": True, "stale": False, "reason": None, "cacheAgeDays": 0}):
            generate.reconcile_fetch_freshness("2026-07-09", "2026-07-08")
            self.assertTrue(generate.FETCH_STATUS["stale"])
            self.assertIn("1 held symbols stale", generate.FETCH_STATUS["reason"])
            self.assertIn("1 held symbols missing", generate.FETCH_STATUS["reason"])

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
                p.write_text(p.name, encoding="utf-8")

            got = [Path(p).name for p in generate.collect_history_files(
                td, str(explicit), include_archive=False)]

        self.assertEqual(set(got), {"Accounts_History (1).csv", "History_for_Account_DEMO01.csv", "fresh.csv"})
        self.assertEqual(len(got), 3)

    def test_exact_history_mode_uses_only_explicit_file(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "Accounts_History (1).csv").write_text("", encoding="utf-8")
            explicit = root / "fresh.csv"
            explicit.write_text("", encoding="utf-8")

            got = generate.collect_history_files(td, str(explicit), exact=True)

        self.assertEqual([Path(p).resolve() for p in got], [explicit.resolve()])

    def test_option_symbol_canonicalization_does_not_misclassify_stock_settlement(self):
        self.assertEqual(generate.canonical_option_symbol("QQQ260611C705"), "-QQQ260611C705")
        self.assertEqual(generate.canonical_option_symbol(" -QQQ260611C705"), "-QQQ260611C705")
        self.assertIsNone(generate.canonical_option_symbol("QQQ"))

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

    def test_merge_histories_uses_newest_present_multiplicity(self):
        fill = {"account": "DEMO01", "date": "2026-07-08", "side": "BUY",
                "qty": 1.0, "price": 100.0, "amount": -100.0}
        older = {"txns": {"QQQ": [dict(fill), dict(fill)]}, "opt_txns": {},
                 "names": {"QQQ": "Invesco QQQ"}}
        newer = {"txns": {"QQQ": [dict(fill)]}, "opt_txns": {},
                 "names": {"QQQ": "Invesco QQQ"}}

        txns, _, _ = generate.merge_histories([older, newer])

        self.assertEqual(len(txns["QQQ"]), 1)

    def test_union_cash_preserves_true_same_day_duplicates(self):
        older = {"cash_rows": [("2026-06-23", "EFT", 1000.0, "A")]}
        newer = {"cash_rows": [
            ("2026-06-23", "EFT", 1000.0, "A"),
            ("2026-06-23", "EFT", 1000.0, "A"),
            ("2026-06-23", "DIV", 0.36, "A"),
            ("2026-06-23", "DIV", 0.36, "B"),
        ]}

        deposits, dividends = generate.union_cash(
            [older, newer], "2026-01-01", "2026-12-31")

        self.assertEqual(deposits, 2000.0)
        self.assertEqual(dividends, 0.72)

    def test_union_cash_events_preserves_deduplicated_dates_for_mwr(self):
        older = {"cash_rows": [("2026-07-08", "EFT", 216.0, "A"),
                                ("2026-07-08", "DIV", 1.25, "A")]}
        newer = {"cash_rows": [("2026-07-09", "EFT", 216.0, "A"),
                                ("2026-07-08", "DIV", 1.25, "A")]}

        events = generate.union_cash_events(
            [older, newer], "2026-01-01", "2026-12-31")

        self.assertEqual(events, [
            {"date": "2026-07-08", "kind": "DIV", "amount": 1.25, "account": "A"},
            {"date": "2026-07-09", "kind": "EFT", "amount": 216.0, "account": "A"},
        ])

    def test_parse_history_accepts_hyphen_dates(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "Accounts_History.csv"
            path.write_text(
                "Run Date,Account,Account Number,Action,Symbol,Description,Type,Price ($),Quantity,Commission ($),Fees ($),Accrued Interest ($),Amount ($),Settlement Date\n"
                "07-08-2026,Individual,DEMO01,YOU BOUGHT INVESCO QQQ TR (QQQ) (Cash),QQQ,Invesco QQQ,Cash,100,1,,,, -100,07-09-2026\n",
                encoding="utf-8",
            )

            parsed = generate.parse_history(path)

        self.assertEqual(parsed["dmin"], "2026-07-08")
        self.assertEqual(len(parsed["txns"]["QQQ"]), 1)

    def test_parse_history_rejects_malformed_required_trade_numbers(self):
        header = ("Run Date,Account,Account Number,Action,Symbol,Description,Type,Price ($),Quantity,"
                  "Commission ($),Fees ($),Accrued Interest ($),Amount ($),Settlement Date\n")
        bad_rows = {
            "amount": "07/09/2026,Individual,A,YOU BOUGHT QQQ (Cash),QQQ,QQQ,Cash,100,1,,,,oops,\n",
            "quantity": "07/09/2026,Individual,A,YOU SOLD QQQ (Cash),QQQ,QQQ,Cash,100,,,,,100,\n",
            "price": "07/09/2026,Individual,A,YOU BOUGHT QQQ (Cash),QQQ,QQQ,Cash,bad,1,,,,-100,\n",
        }
        for field, row in bad_rows.items():
            with self.subTest(field=field), tempfile.TemporaryDirectory() as td:
                path = Path(td) / "Accounts_History.csv"
                path.write_text(header + row, encoding="utf-8")

                with self.assertRaisesRegex(ValueError, rf"row 2: .*trade {field}"):
                    generate.parse_history(path)

    def test_parse_history_rejects_malformed_trade_symbol(self):
        header = ("Run Date,Account,Account Number,Action,Symbol,Description,Type,Price ($),Quantity,"
                  "Commission ($),Fees ($),Accrued Interest ($),Amount ($),Settlement Date\n")
        row = ("07/09/2026,Individual,A,YOU BOUGHT HOSTILE (Cash),"
               "BAD<script>,Hostile,Cash,100,1,,,,-100,\n")
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "Accounts_History.csv"
            path.write_text(header + row, encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "row 2: invalid equity symbol"):
                generate.parse_history(path)

    def test_parse_history_rejects_malformed_recognized_cash_amount(self):
        header = ("Run Date,Account,Account Number,Action,Symbol,Description,Type,Price ($),Quantity,"
                  "Commission ($),Fees ($),Accrued Interest ($),Amount ($),Settlement Date\n")
        row = ("07/09/2026,Individual,A,DIVIDEND RECEIVED QQQ (Cash),QQQ,QQQ,Cash,"
               ",0,,,,not-a-number,\n")
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "Accounts_History.csv"
            path.write_text(header + row, encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "row 2: invalid dividend/interest amount"):
                generate.parse_history(path)

    def test_parse_history_handles_reinvestment_cash_core_and_tax_rows(self):
        header = ("Run Date,Account,Account Number,Action,Symbol,Description,Type,Price ($),Quantity,"
                  "Commission ($),Fees ($),Accrued Interest ($),Amount ($),Settlement Date\n")
        rows = [
            "07/09/2026,Individual,A,REINVESTMENT VANGUARD INDEX FUNDS (VOO) (Cash),VOO,Vanguard,Cash,683.85,0.091,,,,-62.23,07/10/2026\n",
            "07/09/2026,Individual,A,REINVESTMENT FIDELITY GOVERNMENT MONEY MARKET (SPAXX) (Cash),SPAXX,Cash,Cash,1,8.9,,,,-8.9,07/10/2026\n",
            "07/09/2026,Individual,A,FOREIGN TAX PAID TAIWAN SEMICONDUCTOR (TSM) (Cash),TSM,TSM,Cash,,0,,,,-1.63,\n",
            "07/09/2026,Individual,A,ADJUSTMENT (CREDIT ADJUSTMENT) QUAL DIV 10/31/25 (QQQ) (Cash),QQQ,QQQ,Cash,,0,,,,0.18,\n",
        ]
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "Accounts_History.csv"
            path.write_text(header + "".join(rows), encoding="utf-8")

            parsed = generate.parse_history(path)

        self.assertEqual(parsed["txns"]["VOO"][0]["side"], "BUY")
        self.assertEqual(parsed["txns"]["VOO"][0]["actionType"], "REINVESTMENT")
        self.assertNotIn("SPAXX", parsed["txns"])
        self.assertNotIn("FDRXX", parsed["txns"])
        self.assertEqual(parsed["dividends"], -1.45)

    def test_plain_ticker_call_settlement_is_equity_but_occ_symbol_is_option(self):
        header = ("Run Date,Account,Account Number,Action,Symbol,Description,Type,Price ($),Quantity,"
                  "Commission ($),Fees ($),Accrued Interest ($),Amount ($),Settlement Date\n")
        rows = [
            "07/09/2026,Individual,A,YOU BOUGHT EXERCISED CALLS (QQQ) (Cash),QQQ,QQQ,Cash,700,100,,,,-70000,07/10/2026\n",
            "07/09/2026,Individual,A,EXPIRED CALL (QQQ),QQQ260710C750,QQQ CALL,Margin,,1,,,,0,\n",
        ]
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "Accounts_History.csv"
            path.write_text(header + "".join(rows), encoding="utf-8")

            parsed = generate.parse_history(path)

        self.assertEqual(parsed["txns"]["QQQ"][0]["side"], "BUY")
        self.assertEqual(parsed["txns"]["QQQ"][0]["actionType"], "OPTION_SETTLEMENT")
        self.assertEqual(parsed["txns"]["QQQ"][0]["settlementType"], "EXERCISED")
        self.assertIn("-QQQ260710C750", parsed["opt_txns"])
        self.assertEqual(parsed["opt_txns"]["-QQQ260710C750"][0]["side"], "OTHER")
        self.assertEqual(parsed["opt_txns"]["-QQQ260710C750"][0]["actionType"], "EXPIRED")

    def test_merge_histories_preserves_action_provenance(self):
        row = {"account": "A", "date": "2026-07-09", "side": "BUY",
               "qty": 100.0, "price": 700.0, "amount": -70000.0,
               "actionType": "OPTION_SETTLEMENT", "settlementType": "EXERCISED"}
        parsed = {"txns": {"QQQ": [row]}, "opt_txns": {}, "names": {"QQQ": "QQQ"}}

        txns, _, _ = generate.merge_histories([parsed])

        self.assertEqual(txns["QQQ"][0]["actionType"], "OPTION_SETTLEMENT")
        self.assertEqual(txns["QQQ"][0]["settlementType"], "EXERCISED")

    def test_cash_in_lieu_is_deduped_as_corporate_action_cash(self):
        header = ("Run Date,Account,Account Number,Action,Symbol,Description,Type,Price ($),Quantity,"
                  "Commission ($),Fees ($),Accrued Interest ($),Amount ($),Settlement Date\n")
        row = ("07/06/2026,Individual,A,IN LIEU OF FRX SHARE SPINOFF FROM:(SPGI ) "
               "MOBILITY GLOBAL INC COM SHS (MBGL) (Cash),MBGL,Mobility,Cash,,0,,,,0.88,\n")
        with tempfile.TemporaryDirectory() as td:
            p1 = Path(td) / "Accounts_History (1).csv"
            p2 = Path(td) / "Accounts_History (2).csv"
            p1.write_text(header + row, encoding="utf-8")
            p2.write_text(header + row, encoding="utf-8")
            parsed = [generate.parse_history(p1), generate.parse_history(p2)]

        deposits, dividends, corporate = generate.union_cash(
            parsed, "2026-01-01", "2026-12-31", include_corporate_actions=True)

        self.assertEqual(parsed[-1]["corporateActions"], 0.88)
        self.assertEqual((deposits, dividends, corporate), (0.0, 0.0, 0.88))
        self.assertNotIn("MBGL", parsed[-1]["txns"])

    def test_union_cash_tracks_run_date_drift_without_double_counting(self):
        older = {"cash_rows": [("2026-07-08", "EFT", 216.0, "A")]}
        newer = {"cash_rows": [("2026-07-09", "EFT", 216.0, "A")]}

        deposits, _ = generate.union_cash([older, newer], "2026-01-01", "2026-12-31")

        self.assertEqual(deposits, 216.0)

    def test_union_cash_preserves_identical_adjacent_transfers_in_same_export(self):
        one_file = {"cash_rows": [
            ("2026-07-08", "EFT", 216.0, "A"),
            ("2026-07-09", "EFT", 216.0, "A"),
        ]}

        deposits, _ = generate.union_cash([one_file], "2026-01-01", "2026-12-31")

        self.assertEqual(deposits, 432.0)

    def test_union_cash_newer_snapshot_can_correct_transient_duplicate(self):
        older = {"cash_rows": [
            ("2026-07-08", "EFT", 22.0, "A"),
            ("2026-07-08", "EFT", 22.0, "A"),
        ]}
        newer = {"cash_rows": [("2026-07-09", "EFT", 22.0, "A")]}

        deposits, _ = generate.union_cash([older, newer], "2026-01-01", "2026-12-31")

        self.assertEqual(deposits, 22.0)


if __name__ == "__main__":
    unittest.main()

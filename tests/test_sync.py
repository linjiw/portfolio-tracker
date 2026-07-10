import csv
import datetime as dt
import os
import tempfile
import unittest
from pathlib import Path

import sync


class SyncVerificationTests(unittest.TestCase):
    def test_position_export_freshness_gate(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "positions.csv"
            path.write_text("snapshot", encoding="utf-8")
            now = dt.datetime(2026, 7, 9, 20, tzinfo=dt.timezone.utc)
            modified = now - dt.timedelta(hours=121)
            os.utime(path, (modified.timestamp(), modified.timestamp()))

            with self.assertRaisesRegex(ValueError, "121.0 hours old"):
                sync.validate_export_freshness(path, max_age_hours=120, now=now)
            info = sync.validate_export_freshness(
                path, max_age_hours=120, allow_old=True, now=now)
            self.assertTrue(info["oldOverride"])

    def test_future_position_export_timestamp_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "positions.csv"
            path.write_text("snapshot", encoding="utf-8")
            now = dt.datetime(2026, 7, 9, 20, tzinfo=dt.timezone.utc)
            future = now + dt.timedelta(hours=1)
            os.utime(path, (future.timestamp(), future.timestamp()))
            with self.assertRaisesRegex(ValueError, "future filesystem timestamp"):
                sync.validate_export_freshness(path, now=now)

    def test_embedded_broker_timestamp_prevents_mtime_freshening(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "positions.csv"
            path.write_text(
                "Account Number,Symbol,Current Value\n"
                "Date downloaded Jun-01-2026 4:40 p.m ET\n",
                encoding="utf-8",
            )
            now = dt.datetime(2026, 7, 9, 20, tzinfo=dt.timezone.utc)
            os.utime(path, (now.timestamp(), now.timestamp()))

            stamp = sync.broker_download_timestamp(path)
            self.assertEqual(stamp.isoformat(), "2026-06-01T16:40:00-04:00")
            with self.assertRaisesRegex(ValueError, "portfolio export is"):
                sync.validate_export_freshness(path, max_age_hours=120, now=now)
            info = sync.validate_export_freshness(
                path, max_age_hours=120, allow_old=True, now=now)
            self.assertEqual(info["timestampSource"], "broker-export")

    def test_independent_totals_cover_whole_account(self):
        header = ["Account Number", "Account Name", "Symbol", "Description", "Quantity",
                  "Last Price", "Last Price Change", "Current Value", "Today's Gain/Loss Dollar",
                  "Today's Gain/Loss Percent", "Total Gain/Loss Dollar", "Total Gain/Loss Percent",
                  "Percent Of Account", "Cost Basis Total", "Average Cost Basis", "Type"]
        rows = [
            ["Z12345", "Individual", "QQQ", "QQQ", "2", "$100", "", "$200", "", "", "$20", "", "", "$180", "", "Cash"],
            ["Z12345", "Individual", "SPAXX**", "Cash", "", "", "", "$50", "", "", "", "", "", "", "", "Cash"],
            ["Z12345", "Individual", "-QQQ260710C110", "Call", "-1", "$1", "", "-$100", "", "", "$20", "", "", "$120", "", "Margin"],
            ["Z12345", "Individual", "Pending activity", "Pending", "", "", "", "-$5", "", "", "", "", "", "", "", "Cash"],
        ]
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "positions.csv"
            with path.open("w", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(header)
                writer.writerows(rows)
            got = sync.independent_totals(path)

        self.assertEqual(got["marketValue"], 200)
        self.assertEqual(got["equityCostBasis"], 180)
        self.assertEqual(got["sharesBySymbol"], {"QQQ": 2.0})
        self.assertEqual(got["cashTotal"], 50)
        self.assertEqual(got["optMarkNet"], -100)
        self.assertEqual(got["optMarkGross"], 100)
        self.assertEqual(got["optBrokerPnl"], 20)
        self.assertEqual(got["optEntryCashNet"], 120)
        self.assertEqual(got["pendingTotal"], -5)
        self.assertEqual(got["accountNetWorth"], 145)
        self.assertEqual(got["optLegCount"], 1)
        self.assertEqual(got["recognizedRowCount"], 4)

    def test_independent_totals_is_header_driven_and_accepts_nonstandard_account_ids(self):
        header = ["Symbol", "Current Value", "Account Number", "Cost Basis Total",
                  "Quantity", "Total Gain/Loss Dollar"]
        rows = [
            ["QQQ", "$250", "acct-01 / IRA", "$200", "2", "$50"],
            ["SPAXX", "$25", "acct-01 / IRA", "$25", "25", "--"],
        ]
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "positions.csv"
            with path.open("w", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(header)
                writer.writerows(rows)
            got = sync.independent_totals(path)

        self.assertEqual(got["marketValue"], 250)
        self.assertEqual(got["cashTotal"], 25)
        self.assertEqual(got["sharesBySymbol"], {"QQQ": 2.0})
        self.assertEqual(got["accounts"], ["acct-01 / IRA"])

    def test_independent_totals_rejects_malformed_required_number(self):
        header = ["Account Number", "Symbol", "Quantity", "Current Value",
                  "Total Gain/Loss Dollar", "Cost Basis Total"]
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "positions.csv"
            with path.open("w", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(header)
                writer.writerow(["A-1", "QQQ", "2", "not-a-number", "$50", "$200"])
            with self.assertRaisesRegex(ValueError, "invalid numeric field"):
                sync.independent_totals(path)

    def test_publish_verified_dashboard_is_atomic_and_private(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "dashboard.html"
            staged = root / "dashboard.html.staging"
            target.write_text("last-known-good", encoding="utf-8")
            staged.write_text("verified-new", encoding="utf-8")

            # The old target remains untouched until the explicit promotion.
            self.assertEqual(target.read_text(encoding="utf-8"), "last-known-good")
            sync.publish_verified_dashboard(str(staged), str(target))

            self.assertEqual(target.read_text(encoding="utf-8"), "verified-new")
            self.assertFalse(staged.exists())
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_publish_preserves_an_existing_external_parent_mode(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            root.chmod(0o755)
            target = root / "dashboard.html"
            staged = root / "dashboard.html.staging"
            staged.write_text("verified-new", encoding="utf-8")

            sync.publish_verified_dashboard(str(staged), str(target))

            self.assertEqual(root.stat().st_mode & 0o777, 0o755)
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_atomic_json_is_private(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "private" / "sync.json"
            sync._atomic_json(str(target), {"ok": True})
            self.assertEqual(target.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()

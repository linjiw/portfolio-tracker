import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import refresh_portfolio_intelligence as refresh


def dashboard(path, summary):
    path.write_text(
        "<script>const DATA = " + json.dumps({"summary": summary}) + ";</script>",
        encoding="utf-8",
    )


class RefreshPortfolioIntelligenceTests(unittest.TestCase):
    def test_newest_uses_mtime_across_supported_patterns(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            old = root / "Accounts_History.csv"
            new = root / "History_for_Account_X.csv"
            old.write_text("old", encoding="utf-8")
            new.write_text("new", encoding="utf-8")
            old.touch()
            import os
            os.utime(old, (1, 1))
            os.utime(new, (2, 2))
            got = refresh.newest(td, ["Accounts_History*.csv", "History_for_Account*.csv"])
        self.assertEqual(got, str(new))

    def test_publish_rejects_stale_held_prices(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            staged = root / "staged.html"
            target = root / "portfolio_dashboard.html"
            dashboard(staged, {
                "priceMode": "mark-to-market",
                "stalePriceSymbols": {"QQQ": "2026-07-08"},
                "missingPriceSymbols": [],
            })
            run = refresh.RefreshRun.__new__(refresh.RefreshRun)
            run.manifest = {"allowStalePrices": False}
            run.write_manifest = lambda: None
            with mock.patch.object(refresh, "DASHBOARD", target):
                with self.assertRaisesRegex(RuntimeError, "stale"):
                    run.publish_dashboard(staged, "test", require_fresh=True)
            self.assertTrue(staged.exists())

    def test_publish_atomically_promotes_fresh_dashboard(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            staged = root / "staged.html"
            target = root / "portfolio_dashboard.html"
            dashboard(staged, {
                "priceMode": "mark-to-market", "priceAsOf": "2026-07-09",
                "freshPriceSymbols": ["QQQ"], "stalePriceSymbols": {},
                "missingPriceSymbols": [], "marketValue": 100, "numHeld": 1,
            })
            run = refresh.RefreshRun.__new__(refresh.RefreshRun)
            run.manifest = {"allowStalePrices": False}
            run.write_manifest = lambda: None
            with mock.patch.object(refresh, "DASHBOARD", target):
                payload = run.publish_dashboard(staged, "test", require_fresh=True)
            self.assertFalse(staged.exists())
            self.assertTrue(target.exists())
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            self.assertEqual(payload["summary"]["priceAsOf"], "2026-07-09")

    def test_inspect_keeps_canonical_dashboard_untouched(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            staged = root / "broker-stage.html"
            target = root / "portfolio_dashboard.html"
            dashboard(staged, {"priceMode": "broker-exact", "numHeld": 1})
            target.write_text("last-known-good-current-prices", encoding="utf-8")
            run = refresh.RefreshRun.__new__(refresh.RefreshRun)
            run.manifest = {"allowStalePrices": False}
            run.write_manifest = lambda: None
            with mock.patch.object(refresh, "DASHBOARD", target):
                run.inspect_dashboard(staged, "broker_verified", require_fresh=False)
            self.assertTrue(staged.exists())
            self.assertEqual(target.read_text(encoding="utf-8"), "last-known-good-current-prices")

    def test_command_timeout_is_recorded_and_nonrequired_step_returns_false(self):
        with tempfile.TemporaryDirectory() as td:
            run = refresh.RefreshRun.__new__(refresh.RefreshRun)
            run.step_timeout_seconds = 1.0
            run.log_path = Path(td) / "run.log"
            run.manifest = {"steps": []}
            run.redactions = []
            run.write_manifest = lambda: None
            with mock.patch.object(
                    refresh.subprocess, "run",
                    side_effect=refresh.subprocess.TimeoutExpired(
                        ["job"], 1, output=b"partial\xff", stderr=b"provider stalled")):
                ok = run.command("slow", ["job"], required=False)
            self.assertFalse(ok)
            self.assertTrue(run.manifest["steps"][0]["timedOut"])
            self.assertEqual(run.manifest["steps"][0]["returnCode"], 124)
            self.assertIn("partial", run.manifest["steps"][0]["stdoutTail"])

    def test_atomic_json_uses_private_permissions(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "private" / "manifest.json"
            refresh.atomic_json(target, {"ok": True})
            self.assertEqual(target.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_scrub_redacts_sensitive_paths(self):
        run = refresh.RefreshRun.__new__(refresh.RefreshRun)
        run.redactions = [
            ("/Users/example/Downloads/Portfolio.csv", "<portfolio-export>"),
            ("/Users/example/repo", "<repo>"),
        ]
        text = run.scrub("read /Users/example/Downloads/Portfolio.csv from /Users/example/repo")
        self.assertEqual(text, "read <portfolio-export> from <repo>")


if __name__ == "__main__":
    unittest.main()

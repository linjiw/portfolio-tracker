import importlib.util
import datetime as dt
import os
import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SPEC = importlib.util.spec_from_file_location(
    "daily_portfolio_brief", ROOT / "scripts" / "daily_portfolio_brief.py")
brief = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = brief
SPEC.loader.exec_module(brief)


class DailyPortfolioBriefRefreshTests(unittest.TestCase):
    @mock.patch.object(brief.subprocess, "run")
    def test_sync_finishes_with_mark_to_market_render(self, run):
        run.return_value = mock.Mock(returncode=0)

        ok, reason = brief.refresh_dashboard_for_brief(
            "/tmp/downloads", portfolio="positions.csv", history="history.csv")

        self.assertTrue(ok)
        self.assertEqual(reason, "ok")
        self.assertEqual(run.call_count, 1)
        price_cmd = run.call_args.args[0]
        self.assertIn("refresh_latest_prices.py", price_cmd[1])
        self.assertIn("--portfolio", price_cmd)
        self.assertIn("--history", price_cmd)
        self.assertEqual(price_cmd[-4:], ["--momentum-strategy", "off", "--trigger", "daily-brief"])

    @mock.patch.object(brief.subprocess, "run")
    def test_failed_refresh_does_not_start_a_second_publication_step(self, run):
        run.return_value = mock.Mock(returncode=1)

        ok, reason = brief.refresh_dashboard_for_brief("/tmp/downloads")

        self.assertFalse(ok)
        self.assertEqual(reason, "mark-to-market refresh failed")
        self.assertEqual(run.call_count, 1)

    @mock.patch.object(brief.subprocess, "run",
                       side_effect=brief.subprocess.TimeoutExpired("refresh", 10))
    def test_refresh_timeout_is_reported(self, run):
        ok, reason = brief.refresh_dashboard_for_brief("/tmp/downloads", timeout_seconds=10)

        self.assertFalse(ok)
        self.assertEqual(reason, "mark-to-market refresh timed out")
        self.assertEqual(run.call_count, 1)

    def test_failed_refresh_warning_is_visible_even_when_last_dashboard_claimed_fresh(self):
        payload = {
            "summary": {
                "priceMode": "mark-to-market", "priceAsOf": "2026-07-09",
                "dateRange": ["2026-01-01", "2026-07-09"],
                "marketValue": 1000, "unrealized": 10,
                "curReturn": 1.0, "spReturn": 0.5,
                "fetchOK": True, "fetchStale": False,
            },
            "stocks": [],
        }

        message = brief.build_brief(
            payload,
            refresh_warning="mark-to-market refresh failed; using last generated dashboard",
            now=dt.datetime(2026, 7, 10, 8, 0),
        )

        self.assertIn("DATA BLOCK", message)
        self.assertIn("refresh failed", message)

    def test_old_dashboard_date_is_data_blocked(self):
        payload = {
            "summary": {
                "priceMode": "mark-to-market", "priceAsOf": "2026-06-30",
                "dateRange": ["2026-01-01", "2026-06-30"],
                "marketValue": 1000, "unrealized": 10,
                "curReturn": 1.0, "spReturn": 0.5,
            },
            "stocks": [],
        }

        message = brief.build_brief(
            payload, now=dt.datetime(2026, 7, 10, 8, 0))

        self.assertIn("dashboard price date is not current", message)


if __name__ == "__main__":
    unittest.main()

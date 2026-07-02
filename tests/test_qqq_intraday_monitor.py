import importlib.util
import json
import os
import pathlib
import unittest


SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "monitor_qqq_intraday.py"
SPEC = importlib.util.spec_from_file_location("qqq_intraday_monitor", SCRIPT)
monitor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(monitor)

INSTALLER_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "install_qqq_intraday_monitor_launchd.py"
INSTALLER_SPEC = importlib.util.spec_from_file_location("install_qqq_intraday_monitor_launchd", INSTALLER_SCRIPT)
installer = importlib.util.module_from_spec(INSTALLER_SPEC)
INSTALLER_SPEC.loader.exec_module(installer)


def rows_from_closes(closes):
    rows = []
    for i, close in enumerate(closes):
        rows.append({
            "time": f"2026-06-05T10:{i:02d}-04:00",
            "open": close - 0.2,
            "high": close + 0.3,
            "low": close - 0.4,
            "close": close,
            "volume": 1000 + i,
        })
    return monitor.add_intraday_features(rows)


class QqqIntradayMonitorTests(unittest.TestCase):
    def test_ema_values_are_standard_exponential_average(self):
        vals = monitor.ema_values([100, 102, 104], 3)
        self.assertEqual(vals[0], 100)
        self.assertAlmostEqual(vals[1], 101)
        self.assertAlmostEqual(vals[2], 102.5)

    def test_stability_signal_allows_when_multitimeframe_reclaims(self):
        rising = [100 + i * 0.15 for i in range(60)]
        intraday = {
            "1m": rows_from_closes(rising),
            "5m": rows_from_closes(rising[::5]),
            "15m": rows_from_closes(rising[::15] + [rising[-1]]),
            "30m": rows_from_closes([rising[0], rising[30], rising[-1]]),
        }
        daily = {"available": True, "ema21": 102.0}

        signal = monitor.evaluate_signal(intraday, daily)

        self.assertEqual(signal["label"], "ALLOW")
        self.assertGreaterEqual(signal["score"], 6)

    def test_daily_ema21_break_blocks_even_if_short_term_bounces(self):
        rising = [100 + i * 0.1 for i in range(60)]
        intraday = {
            "1m": rows_from_closes(rising),
            "5m": rows_from_closes(rising[::5]),
            "15m": rows_from_closes(rising[::15] + [rising[-1]]),
            "30m": rows_from_closes([rising[0], rising[30], rising[-1]]),
        }
        daily = {"available": True, "ema21": 120.0}

        signal = monitor.evaluate_signal(intraday, daily)

        self.assertEqual(signal["label"], "BLOCK")

    def test_telegram_message_explains_center_of_gravity_inputs(self):
        rising = [100 + i * 0.15 for i in range(60)]
        intraday = {
            "1m": rows_from_closes(rising),
            "5m": rows_from_closes(rising[::5]),
            "15m": rows_from_closes(rising[::15] + [rising[-1]]),
            "30m": rows_from_closes([rising[0], rising[30], rising[-1]]),
        }
        daily = {"available": True, "ema21": 102.0, "ema8": 103.0, "atr14": 2.5, "rsi14": 55.0}
        snapshot = {
            "pulledAt": "2026-06-05T10:45:00-07:00",
            "daily": daily,
            "signal": monitor.evaluate_signal(intraday, daily),
        }

        msg = monitor.format_telegram_message(snapshot)

        self.assertIn("重心定义", msg)
        self.assertIn("5m", msg)
        self.assertIn("15m", msg)
        self.assertIn("Teacher action", msg)

    def test_intraday_launchd_plist_does_not_store_telegram_token(self):
        class Args:
            symbol = "QQQ"
            poll_seconds = 900
            hour = 6
            minute = 31
            telegram = True
            telegram_token_env = "TELEGRAM_BOT_TOKEN"
            telegram_chat_id_env = "TELEGRAM_CHAT_ID"

        old_token = os.environ.get("TELEGRAM_BOT_TOKEN")
        old_chat_id = os.environ.get("TELEGRAM_CHAT_ID")
        os.environ["TELEGRAM_BOT_TOKEN"] = "secret-token-must-not-enter-plist"
        os.environ["TELEGRAM_CHAT_ID"] = "123456"
        try:
            plist = installer.build_plist(Args(), "/usr/bin/python3")
        finally:
            if old_token is None:
                os.environ.pop("TELEGRAM_BOT_TOKEN", None)
            else:
                os.environ["TELEGRAM_BOT_TOKEN"] = old_token
            if old_chat_id is None:
                os.environ.pop("TELEGRAM_CHAT_ID", None)
            else:
                os.environ["TELEGRAM_CHAT_ID"] = old_chat_id

        dumped = json.dumps(plist, ensure_ascii=False)
        self.assertIn("--telegram", plist["ProgramArguments"])
        self.assertNotIn("TELEGRAM_BOT_TOKEN", dumped)
        self.assertNotIn("TELEGRAM_CHAT_ID", dumped)
        self.assertNotIn("secret-token-must-not-enter-plist", dumped)


if __name__ == "__main__":
    unittest.main()

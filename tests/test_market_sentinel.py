import importlib.util
import pathlib
import unittest


SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "market_sentinel.py"
SPEC = importlib.util.spec_from_file_location("market_sentinel", SCRIPT)
sentinel = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sentinel)


def fixture_payload(close=705.0, ema21=716.0, ema34=699.0):
    return {
        "summary": {
            "priceAsOf": "2026-06-05",
            "generatedAt": "2026-06-05T16:36:18",
            "accountNetWorth": 110000.0,
            "marketValue": 107000.0,
            "cashTotal": 4500.0,
            "optPctEquity": 56.0,
        },
        "alloc": {
            "largestTheme": {
                "theme": "半导体",
                "weightPct": 42.0,
                "riskPct": 62.0,
                "members": ["NVDA", "MU"],
            }
        },
        "risk": {"contrib": [{"sym": "NVDA", "riskPct": 29.0}]},
        "qqqTqqq": {
            "state": {"label": "转换中"},
            "latest": {
                "date": "2026-06-05",
                "qqq": close,
                "tqqq": 73.0,
                "ema8": 729.0,
                "ema13": 726.0,
                "ema21": ema21,
                "ema34": ema34,
                "atr14": 12.0,
                "twoBelowEma21": False,
            },
            "holdings": {"QQQ": {"value": 14000}, "TQQQ": {"value": 550}},
            "optionSpreads": [],
        },
    }


def fixture_quotes(qqq_ret=-4.8, vix_ret=40.0, tnx_ret=1.3):
    quotes = {
        "QQQ": {"symbol": "QQQ", "available": True, "last": 705.0, "ret1": qqq_ret, "ret5": -4.5},
        "TQQQ": {"symbol": "TQQQ", "available": True, "last": 73.0, "ret1": -14.0, "ret5": -13.0},
        "^VIX": {"symbol": "^VIX", "available": True, "last": 21.5, "ret1": vix_ret},
        "^TNX": {"symbol": "^TNX", "available": True, "last": 4.54, "ret1": tnx_ret},
        "USO": {"symbol": "USO", "available": True, "last": 133.0, "ret1": -2.0},
    }
    for sym, ret in [("SMH", -9.0), ("NVDA", -6.0), ("AVGO", -8.0), ("MU", -13.0), ("AMD", -10.0), ("MRVL", -16.0)]:
        quotes[sym] = {"symbol": sym, "available": True, "last": 100.0, "ret1": ret, "ret5": -5.0}
    return quotes


class MarketSentinelTests(unittest.TestCase):
    def test_technical_agent_blocks_below_ema21(self):
        technical = sentinel.technical_agent(fixture_payload(), {"signal": {"label": "BLOCK", "last": 705.0}})

        self.assertEqual(technical["regime"], "below_ema21_watch")
        self.assertTrue(technical["flags"]["belowEma21"])

    def test_decision_agent_blocks_when_below_ema21_and_leaders_break(self):
        payload = fixture_payload()
        quotes = fixture_quotes()
        technical = sentinel.technical_agent(payload, {"signal": {"label": "BLOCK", "last": 705.0}})
        leaders = sentinel.leaders_agent(quotes)
        mood = sentinel.mood_agent(quotes, {"keywordCounts": {"hawkish": 2, "ai_semis": 3}})
        portfolio = sentinel.portfolio_agent(payload)

        decision = sentinel.decision_agent(technical, leaders, mood, portfolio, quotes)

        self.assertEqual(decision["label"], "BLOCK")
        self.assertTrue(any("EMA21" in b for b in decision["blockers"]))
        self.assertIn("reclaimBuyStop", decision["levels"])

    def test_decision_agent_allows_only_after_repair(self):
        payload = fixture_payload(close=730.0, ema21=716.0, ema34=699.0)
        quotes = fixture_quotes(qqq_ret=1.2, vix_ret=-8.0, tnx_ret=-0.5)
        for sym in ["SMH", "NVDA", "AVGO", "MU", "AMD", "MRVL"]:
            quotes[sym]["ret1"] = 1.5
        technical = sentinel.technical_agent(payload, {"signal": {"label": "ALLOW", "score": 7, "maxScore": 9, "last": 730.0}})
        leaders = sentinel.leaders_agent(quotes)
        mood = sentinel.mood_agent(quotes, {"keywordCounts": {"hawkish": 0, "dovish": 1}})
        portfolio = sentinel.portfolio_agent(payload)

        decision = sentinel.decision_agent(technical, leaders, mood, portfolio, quotes)

        self.assertEqual(decision["label"], "WATCH")
        self.assertIn("组合半导体", "；".join(decision["watchItems"]))

    def test_rss_parser_extracts_titles(self):
        xml = b"""<?xml version="1.0"?><rss><channel><item><title>Fed keeps rates steady</title><source>Example</source><pubDate>Fri</pubDate><link>https://x</link></item></channel></rss>"""

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return xml

        old = sentinel.urllib.request.urlopen
        try:
            sentinel.urllib.request.urlopen = lambda *a, **k: Resp()
            items = sentinel.fetch_rss_items("https://example.test/rss", "Macro", limit=5)
        finally:
            sentinel.urllib.request.urlopen = old

        self.assertEqual(items[0]["title"], "Fed keeps rates steady")

    def test_message_contains_decision_and_levels(self):
        payload = fixture_payload()
        quotes = fixture_quotes()
        technical = sentinel.technical_agent(payload, {"signal": {"label": "BLOCK", "score": 1, "maxScore": 9, "last": 705.0}})
        leaders = sentinel.leaders_agent(quotes)
        mood = sentinel.mood_agent(quotes, {"keywordCounts": {"hawkish": 2, "ai_semis": 3}, "headlines": []})
        portfolio = sentinel.portfolio_agent(payload)
        decision = sentinel.decision_agent(technical, leaders, mood, portfolio, quotes)
        snapshot = {
            "ranAt": "2026-06-05T16:45:00-07:00",
            "agents": {
                "price": {"quotes": quotes},
                "news": {"headlines": [], "errors": {}},
                "portfolio": portfolio,
                "technical": technical,
                "leaders": leaders,
                "mood": mood,
                "decision": decision,
            },
        }

        msg = sentinel.format_telegram_message(snapshot)

        self.assertIn("Market Sentinel", msg)
        self.assertIn("Reclaim buy-stop", msg)
        self.assertIn("BLOCK", msg)

    def test_launchd_plist_does_not_store_telegram_token(self):
        installer_path = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "install_market_sentinel_launchd.py"
        spec = importlib.util.spec_from_file_location("install_market_sentinel_launchd", installer_path)
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)

        class Args:
            input_dir = "/tmp/downloads"
            times = "06:45,13:15"
            no_news = False
            run_at_load = False

        plist = installer.build_plist(Args(), "/usr/bin/python3")

        self.assertIn("--telegram", plist["ProgramArguments"])
        self.assertIn("--refresh-dashboard", plist["ProgramArguments"])
        self.assertIn("--refresh-intraday", plist["ProgramArguments"])
        self.assertNotIn("TELEGRAM_BOT_TOKEN", json_dumps(plist))
        self.assertEqual(len(plist["StartCalendarInterval"]), 10)


def json_dumps(obj):
    import json

    return json.dumps(obj, ensure_ascii=False)


if __name__ == "__main__":
    unittest.main()

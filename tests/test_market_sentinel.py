import csv
import datetime as dt
import importlib.util
import json
import math
import pathlib
import sys
import tempfile
import unittest
from unittest import mock


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


def fixture_quotes(qqq_ret=-4.8, vix_ret=40.0, tnx_ret=1.3, quote_date=None):
    quote_date = quote_date or dt.datetime.now(tz=sentinel.ET_TZ).date().isoformat()
    quotes = {
        "QQQ": {"symbol": "QQQ", "available": True, "date": quote_date, "last": 705.0, "ret1": qqq_ret, "ret5": -4.5},
        "TQQQ": {"symbol": "TQQQ", "available": True, "date": quote_date, "last": 73.0, "ret1": -14.0, "ret5": -13.0},
        "^VIX": {"symbol": "^VIX", "available": True, "date": quote_date, "last": 21.5, "ret1": vix_ret},
        "^TNX": {"symbol": "^TNX", "available": True, "date": quote_date, "last": 4.54, "ret1": tnx_ret},
        "USO": {"symbol": "USO", "available": True, "date": quote_date, "last": 133.0, "ret1": -2.0},
    }
    for sym, ret in [("SMH", -9.0), ("NVDA", -6.0), ("AVGO", -8.0), ("MU", -13.0), ("AMD", -10.0), ("MRVL", -16.0)]:
        quotes[sym] = {"symbol": sym, "available": True, "date": quote_date, "last": 100.0, "ret1": ret, "ret5": -5.0}
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

    def test_leaders_reject_old_aligned_quotes_instead_of_signaling_repair(self):
        now = dt.datetime(2026, 7, 9, 12, 0, tzinfo=sentinel.ET_TZ)
        quotes = fixture_quotes(qqq_ret=1.5, quote_date="2019-07-09")
        for symbol in sentinel.LEADER_SYMBOLS:
            quotes[symbol]["ret1"] = 2.0

        leaders = sentinel.leaders_agent(quotes, now=now)

        self.assertEqual(leaders["state"], "data_block")
        self.assertEqual(leaders["dataStatus"], "BLOCK")
        self.assertEqual(leaders["freshCount"], 0)
        self.assertTrue(all(reason == "quote_stale" for reason in leaders["rejectedQuotes"].values()))

    def test_mood_rejects_future_quotes_instead_of_emitting_bullish_regime(self):
        now = dt.datetime(2026, 7, 9, 12, 0, tzinfo=sentinel.ET_TZ)
        quotes = fixture_quotes(qqq_ret=2.0, vix_ret=-10.0, tnx_ret=-1.0,
                                quote_date="2026-07-10")

        mood = sentinel.mood_agent(quotes, {"keywordCounts": {}}, now=now)

        self.assertEqual(mood["mood"], "data_insufficient")
        self.assertEqual(mood["dataStatus"], "BLOCK")
        self.assertEqual(mood["inputs"]["qqqRet1"], None)
        self.assertEqual(mood["quoteFreshness"]["QQQ"]["reason"], "quote_date_in_future")

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

    def test_intraday_watch_can_never_be_promoted_to_allow(self):
        payload = fixture_payload(close=730.0, ema21=716.0, ema34=699.0)
        intraday = {
            "pulledAt": "2026-07-09T11:59:00-04:00",
            "signal": {
                "label": "WATCH", "score": 7, "maxScore": 9, "last": 730.0,
                "summaries": {
                    "5m": {"available": True, "lastTime": "2026-07-09T11:55:00-04:00"},
                    "15m": {"available": True, "lastTime": "2026-07-09T11:45:00-04:00"},
                },
            },
        }
        now = dt.datetime(2026, 7, 9, 12, 0, tzinfo=sentinel.ET_TZ)
        technical = sentinel.technical_agent(payload, intraday, now=now)
        decision = sentinel.decision_agent(
            technical,
            {"state": "leader_repair", "dataStatus": "PASS"},
            {"flags": {}, "dataStatus": "PASS"},
            {"largestTheme": {"riskPct": 10.0}},
            fixture_quotes(qqq_ret=1.2, vix_ret=-8.0, tnx_ret=-0.5),
        )

        self.assertEqual(decision["label"], "WATCH")
        self.assertIn("不得提升为 ALLOW", "；".join(decision["watchItems"]))

    def test_missing_largest_theme_risk_does_not_crash_decision(self):
        payload = fixture_payload(close=730.0, ema21=716.0, ema34=699.0)
        quotes = fixture_quotes(qqq_ret=1.2, vix_ret=-8.0, tnx_ret=-0.5)
        technical = sentinel.technical_agent(
            payload, {"signal": {"label": "WATCH", "last": 730.0}})

        decision = sentinel.decision_agent(
            technical, {"state": "leader_repair"}, {"flags": {}},
            {"largestTheme": {"riskPct": None}}, quotes)

        self.assertIn(decision["label"], {"WATCH", "BLOCK"})
        self.assertNotIn("组合半导体", "；".join(decision["watchItems"]))

    def test_ccs_is_not_called_a_hedge_without_verified_long_exposure(self):
        payload = fixture_payload(close=730.0, ema21=716.0, ema34=699.0)
        technical = sentinel.technical_agent(payload, None)
        decision = sentinel.decision_agent(
            technical,
            {"state": "mixed"},
            {"flags": {}},
            {"largestTheme": {"riskPct": 10.0}, "qqqHolding": None, "tqqqHolding": None},
            fixture_quotes(),
        )
        ccs = decision["levels"]["tqqqCcsGuide"]
        self.assertFalse(ccs["verifiedLongExposure"])
        self.assertEqual(ccs["role"], "directional_short_premium_not_hedge")
        self.assertEqual(ccs["strikeSelectionStatus"], "requires_live_option_chain")
        self.assertNotIn("shortCallRange", ccs)

        portfolio = sentinel.portfolio_agent(payload)
        decision_with_long = sentinel.decision_agent(
            technical, {"state": "mixed"}, {"flags": {}}, portfolio, fixture_quotes(),
        )
        self.assertEqual(decision_with_long["levels"]["tqqqCcsGuide"]["role"], "hedge")

    def test_fresh_tape_prohibit_allow_gate_caps_decision_at_watch(self):
        now = dt.datetime(2026, 7, 9, 12, 0, tzinfo=sentinel.ET_TZ)
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "gates.json"
            path.write_text(json.dumps({
                "schemaVersion": 1,
                "run_id": "run-1",
                "generated_at": "2026-07-09T11:50:00-04:00",
                "triggered": [{"gate": "G3", "name": "vacuum_gate"}],
                "action_lock": [],
                "score_cap": "观察",
                "prohibit_allow": True,
                "llm_may_not_override": True,
            }), encoding="utf-8")
            gates = sentinel.read_intraday_tape_gates(path, now=now)

        intraday = {
            "pulledAt": "2026-07-09T11:55:00-04:00",
            "signal": {
                "label": "ALLOW",
                "score": 8,
                "maxScore": 9,
                "last": 730.0,
                "summaries": {
                    "5m": {"available": True, "close": 730.0, "aboveVwap": True,
                           "lastTime": "2026-07-09T11:55:00-04:00"},
                    "15m": {"available": True, "close": 730.0,
                            "lastTime": "2026-07-09T11:45:00-04:00"},
                },
            },
        }
        payload = fixture_payload(close=730.0, ema21=716.0, ema34=699.0)
        technical = sentinel.technical_agent(payload, intraday, gates, now=now)
        quotes = fixture_quotes(qqq_ret=1.2, vix_ret=-8.0, tnx_ret=-0.5)
        for sym in ["SMH", "NVDA", "AVGO", "MU", "AMD", "MRVL"]:
            quotes[sym]["ret1"] = 1.5
        decision = sentinel.decision_agent(
            technical,
            {"state": "leader_repair", "dataStatus": "PASS"},
            {"flags": {}, "dataStatus": "PASS"},
            {"largestTheme": {"riskPct": 10.0}},
            quotes,
        )

        self.assertTrue(gates["fresh"])
        self.assertEqual(decision["label"], "WATCH")
        self.assertIn("prohibit_allow", "；".join(decision["watchItems"]))
        self.assertIn("WATCH 不是入场许可", decision["primaryAction"])

    def test_stale_tape_gate_is_not_enforced_as_current(self):
        now = dt.datetime(2026, 7, 9, 12, 0, tzinfo=sentinel.ET_TZ)
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "gates.json"
            path.write_text(json.dumps({
                "schemaVersion": 1,
                "run_id": "run-1",
                "generated_at": "2026-07-09T10:00:00-04:00",
                "prohibit_allow": True,
                "action_lock": ["不开新仓"],
                "triggered": [{"gate": "G1"}],
                "score_cap": None,
                "llm_may_not_override": True,
            }), encoding="utf-8")
            gates = sentinel.read_intraday_tape_gates(path, now=now)

        self.assertFalse(gates["fresh"])
        self.assertEqual(gates["reason"], "stale")

    def test_malformed_hard_gate_schema_fails_closed_without_crashing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "gates.json"
            path.write_text(json.dumps({
                "schemaVersion": 1,
                "run_id": "run-1",
                "generated_at": "2026-07-09T11:50:00-04:00",
                "prohibit_allow": "false",
                "action_lock": [],
                "triggered": [],
                "score_cap": None,
                "llm_may_not_override": True,
            }), encoding="utf-8")
            gates = sentinel.read_intraday_tape_gates(path)

        self.assertFalse(gates["available"])
        self.assertFalse(gates["fresh"])
        self.assertEqual(gates["reason"], "prohibit_allow_not_boolean")

    def test_missing_intraday_data_can_never_produce_allow(self):
        payload = fixture_payload(close=730.0, ema21=716.0, ema34=699.0)
        technical = sentinel.technical_agent(payload, None)
        quotes = fixture_quotes(qqq_ret=1.2, vix_ret=-8.0, tnx_ret=-0.5)
        decision = sentinel.decision_agent(
            technical,
            {"state": "leader_repair", "dataStatus": "PASS"},
            {"flags": {}, "dataStatus": "PASS"},
            {"largestTheme": {"riskPct": 10.0}},
            quotes,
        )

        self.assertNotEqual(decision["label"], "ALLOW")
        self.assertIn("日内数据缺失或过期", "；".join(decision["watchItems"]))

    def test_fresh_snapshot_requires_fresh_bar_timestamps_not_only_fresh_file(self):
        now = dt.datetime(2026, 7, 9, 12, 0, tzinfo=sentinel.ET_TZ)
        snapshot = {
            "pulledAt": "2026-07-09T11:59:00-04:00",
            "signal": {
                "label": "ALLOW", "last": 730.0,
                "summaries": {
                    "5m": {"available": True, "lastTime": "2026-07-08T15:55:00-04:00"},
                    "15m": {"available": True, "lastTime": "2026-07-08T15:45:00-04:00"},
                },
            },
        }

        status = sentinel.intraday_snapshot_status(snapshot, now=now)

        self.assertTrue(status["available"])
        self.assertFalse(status["fresh"])
        self.assertIn("stale_or_wrong_session_bars", status["reason"])

    def test_bar_freshness_is_measured_from_close_not_bar_start(self):
        now = dt.datetime(2026, 7, 9, 12, 0, tzinfo=sentinel.ET_TZ)
        snapshot = {
            "pulledAt": "2026-07-09T11:59:00-04:00",
            "signal": {
                "label": "WATCH", "last": 730.0,
                "summaries": {
                    "5m": {"available": True, "lastTime": "2026-07-09T11:50:00-04:00"},
                    # Started 30m ago but closed 15m ago; measuring the start
                    # would incorrectly reject this boundary-safe bar.
                    "15m": {"available": True, "lastTime": "2026-07-09T11:30:00-04:00"},
                },
            },
        }
        status = sentinel.intraday_snapshot_status(snapshot, now=now)
        self.assertTrue(status["fresh"])
        self.assertEqual(status["bars"]["15m"]["ageMinutes"], 15.0)

    def test_level_calculation_prefers_fresh_quote_over_stale_dashboard_mark(self):
        technical = sentinel.technical_agent(fixture_payload(close=700.0), None)
        levels = sentinel.compute_levels(
            technical,
            {"QQQ": {"last": 730.0}, "TQQQ": {"last": 80.0}},
        )
        self.assertEqual(levels["current3AtrReference"], 694.0)
        self.assertFalse(levels["current3AtrReferenceIsRatcheted"])
        self.assertEqual(levels["tqqqCcsGuide"]["spot"], 80.0)

    def test_expired_june_playbook_no_longer_caps_clean_decision(self):
        now = dt.datetime(2026, 7, 9, 12, 0, tzinfo=sentinel.ET_TZ)
        playbook = sentinel.june_playbook_agent(now=now)
        payload = fixture_payload(close=730.0, ema21=716.0, ema34=699.0)
        intraday = {
            "pulledAt": "2026-07-09T11:59:00-04:00",
            "signal": {
                "label": "ALLOW", "score": 8, "maxScore": 9, "last": 730.0,
                "summaries": {
                    "5m": {"available": True, "lastTime": "2026-07-09T11:55:00-04:00"},
                    "15m": {"available": True, "lastTime": "2026-07-09T11:45:00-04:00"},
                },
            },
        }
        gates = {
            "available": True, "fresh": True, "reason": None,
            "prohibitAllow": False, "actionLocks": [], "triggered": [],
        }
        technical = sentinel.technical_agent(payload, intraday, gates, now=now)
        quotes = fixture_quotes(qqq_ret=1.2, vix_ret=-8.0, tnx_ret=-0.5)
        decision = sentinel.decision_agent(
            technical,
            {"state": "leader_repair", "dataStatus": "PASS"},
            {"flags": {}, "dataStatus": "PASS"},
            {"largestTheme": {"riskPct": 10.0}},
            quotes,
            playbook=playbook,
        )

        self.assertFalse(playbook["active"])
        self.assertIsNone(playbook["slowState"])
        self.assertEqual(decision["label"], "ALLOW")
        self.assertNotIn("六月慢变量", "；".join(decision["watchItems"]))

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
        with self.assertRaises(ValueError):
            installer.parse_times("24:00")
        with self.assertRaises(ValueError):
            installer.parse_times("06:60")

    def test_log_writer_preserves_append_semantics_and_private_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "runtime" / "sentinel.csv"
            sentinel.append_schema_csv(path, {"ranAt": "one", "label": "WATCH"})
            inode = path.stat().st_ino
            sentinel.append_schema_csv(path, {"ranAt": "two", "label": "BLOCK"})

            with path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual([row["ranAt"] for row in rows], ["one", "two"])
            self.assertEqual(path.stat().st_ino, inode)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)

    def test_snapshot_bundle_is_strict_atomic_and_private(self):
        snapshot = {
            "ranAt": "2026-07-09T12:00:00-07:00",
            "agents": {
                "decision": {
                    "label": "WATCH", "primaryAction": "wait", "levels": {},
                    "blockers": [], "watchItems": [],
                },
                "technical": {
                    "regime": "mixed", "intradayLabel": "WATCH", "latest": {},
                    "intradayData": {}, "intradayHardGates": {},
                },
                "mood": {"mood": "mixed", "reason": "test"},
                "portfolio": {"summary": {"cashTotal": 1000}, "largestTheme": {}},
                "price": {"quotes": {}},
                "leaders": {"state": "mixed", "leaders": []},
                "news": {"headlines": [], "errors": {}},
                "junePlaybook": {"active": False, "status": "expired"},
                "spmoMomentum": {},
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp) / "sentinel"
            sentinel.write_outputs(snapshot, out)
            json_path = out / "latest_snapshot.json"
            original = json_path.read_text(encoding="utf-8")

            self.assertEqual(json.loads(original)["agents"]["decision"]["label"], "WATCH")
            for path in out.iterdir():
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(out.stat().st_mode & 0o777, 0o700)

            invalid = dict(snapshot, invalid=math.nan)
            with self.assertRaises(ValueError):
                sentinel.write_outputs(invalid, out)
            self.assertEqual(json_path.read_text(encoding="utf-8"), original)

    def test_telegram_failure_returns_nonzero(self):
        snapshot = {
            "ranAt": "2026-07-09T12:00:00-07:00",
            "agents": {"decision": {"label": "WATCH", "teacherRead": "watch"}},
        }
        with tempfile.TemporaryDirectory() as td:
            with (
                mock.patch.object(sys, "argv", [str(SCRIPT), "--telegram", "--out-dir", td]),
                mock.patch.object(sentinel, "build_snapshot", return_value=snapshot),
                mock.patch.object(sentinel, "write_outputs", return_value="message"),
                mock.patch.object(sentinel, "send_telegram", return_value=False),
            ):
                self.assertEqual(sentinel.main(), 1)


def json_dumps(obj):
    return json.dumps(obj, ensure_ascii=False)


if __name__ == "__main__":
    unittest.main()

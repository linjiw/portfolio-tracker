import datetime as dt
import importlib.util
import json
import pathlib
import tempfile
import unittest
from zoneinfo import ZoneInfo

import pandas as pd


SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "memory_flow.py"
SPEC = importlib.util.spec_from_file_location("memory_flow", SCRIPT)
memory_flow = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(memory_flow)


def price_rows(start="2026-06-01", count=30, *, shock=False):
    dates = pd.bdate_range(start=start, periods=count)
    rows = []
    close = 100.0
    for index, day in enumerate(dates):
        previous = close
        if shock and index == count - 4:
            close *= 0.82
            volume = 4_000_000
        else:
            close *= 1.005
            volume = 1_000_000
        rows.append({
            "date": day.date().isoformat(),
            "open": previous,
            "high": max(previous, close) * 1.01,
            "low": min(previous, close) * 0.99,
            "close": close,
            "volume": volume,
        })
    return rows


class MemoryFlowTests(unittest.TestCase):
    def test_naver_parser_preserves_categories_and_marks_residual(self):
        source = """
        <table><tr><th>날짜</th><th>종가</th><th>전일비</th><th>등락률</th><th>거래량</th><th>기관</th><th>외국인</th></tr>
        <tr><td>2026.07.09</td><td>2,186,000</td><td>상승 110,000</td><td>+5.30%</td><td>6,261,504</td><td>+404,365</td><td>-209,485</td><td>356,395,099</td><td>50.01%</td></tr></table>
        """
        rows = memory_flow.parse_naver_investor_html(source)
        self.assertEqual(1, len(rows))
        self.assertEqual(404365, rows[0]["institutionNetShares"])
        self.assertEqual(-209485, rows[0]["foreignNetShares"])
        self.assertEqual(-194880, rows[0]["individualOtherResidualNetShares"])
        self.assertEqual(50.01, rows[0]["foreignOwnershipPct"])

    def test_foreign_ownership_change_uses_five_complete_intervals(self):
        rows = [
            {"date": f"2026-07-{day:02d}", "foreignOwnershipPct": float(index),
             "institutionNetShares": 0, "foreignNetShares": 0,
             "individualOtherResidualNetShares": 0, "close": 100}
            for index, day in enumerate(range(1, 7))
        ]

        summary = memory_flow.flow_summary(rows)

        self.assertEqual(5.0, summary["foreignOwnershipChange5dPp"])
        self.assertEqual(6, summary["foreignOwnershipChange5dObservations"])
        self.assertTrue(summary["foreignOwnershipChange5dComplete"])
        self.assertIsNone(summary["foreignOwnershipChange20dPp"])
        self.assertFalse(summary["foreignOwnershipChange20dComplete"])

    def test_partial_korean_bar_is_not_used_as_closed_confirmation(self):
        rows = price_rows(count=10)
        rows[-1]["date"] = "2026-07-10"
        now = dt.datetime(2026, 7, 10, 10, 0, tzinfo=ZoneInfo("Asia/Seoul"))
        closed, live = memory_flow.closed_and_live_rows(rows, "000660.KS", now)
        self.assertEqual(9, len(closed))
        self.assertEqual("2026-07-10", live["date"])

    def test_market_clocks_use_official_regular_session_closes(self):
        before_krx = dt.datetime(2026, 7, 10, 15, 29, tzinfo=ZoneInfo("Asia/Seoul"))
        after_krx = dt.datetime(2026, 7, 10, 15, 30, tzinfo=ZoneInfo("Asia/Seoul"))
        before_us = dt.datetime(2026, 7, 10, 15, 59, tzinfo=ZoneInfo("America/New_York"))
        after_us = dt.datetime(2026, 7, 10, 16, 0, tzinfo=ZoneInfo("America/New_York"))
        self.assertTrue(memory_flow.market_clock("000660.KS", before_krx)[2])
        self.assertFalse(memory_flow.market_clock("000660.KS", after_krx)[2])
        self.assertTrue(memory_flow.market_clock("MU", before_us)[2])
        self.assertFalse(memory_flow.market_clock("MU", after_us)[2])

    def test_technical_metrics_identify_shock_and_close_location(self):
        rows = price_rows(count=35, shock=True)
        # Finish the last session at its low to model an upper-wick supply day.
        rows[-1]["open"] = rows[-1]["close"] * 1.04
        rows[-1]["high"] = rows[-1]["close"] * 1.08
        rows[-1]["low"] = rows[-1]["close"]
        metrics = memory_flow.technical_metrics("MU", rows)
        self.assertTrue(metrics["available"])
        self.assertLess(metrics["shock"]["returnPct"], -15)
        self.assertLess(metrics["latestCloseLocation"], -0.9)
        self.assertIn("confirmEma21", metrics["levels"])

    def test_technical_metrics_fail_closed_and_do_not_label_partial_smas(self):
        unavailable = memory_flow.technical_metrics("MU", price_rows(count=34))
        self.assertFalse(unavailable["available"])
        metrics = memory_flow.technical_metrics("MU", price_rows(count=35))
        self.assertTrue(metrics["available"])
        self.assertIsNone(metrics["sma50"])
        self.assertIsNone(metrics["sma200"])
        self.assertIsNotNone(metrics["atr14"])
        self.assertIsNotNone(metrics["rsi14"])

    def test_new_leveraged_etf_can_report_pain_without_becoming_signal_eligible(self):
        rows = price_rows(count=25)
        rows[-1]["close"] = 60
        rows[-1]["high"] = 62
        rows[-1]["low"] = 58
        rows[-1]["open"] = 61
        snapshot = memory_flow.descriptive_price_snapshot(rows)
        self.assertLess(snapshot["drawdownFrom20dHighPct"], -30)
        self.assertFalse(snapshot["signalEligible"])

    def test_wilder_rsi_is_neutral_for_flat_prices(self):
        rows = price_rows(count=35)
        for row in rows:
            row.update({"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0})
        metrics = memory_flow.technical_metrics("MU", rows)
        self.assertEqual(50.0, metrics["rsi14"])

    def test_missing_required_regime_metric_blocks_instead_of_coercing_zero(self):
        metrics = memory_flow.technical_metrics("MU", price_rows(count=35))
        metrics["ema21Slope5Pct"] = None
        self.assertEqual("BLOCK_DATA", memory_flow.classify_regime(metrics))

    def test_finra_short_volume_is_labeled_as_volume_not_interest(self):
        rows = memory_flow.parse_finra_file(
            "Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market\n"
            "20260709|MU|40|1|100|B,Q,N\n",
            {"MU"},
        )
        summary = memory_flow.finra_summary(rows)
        self.assertEqual(40.0, summary["latestPct"])
        self.assertIn("not short interest", summary["caveat"])

    def test_krx_short_parser_and_summary_separate_transactions_from_balance(self):
        rows = memory_flow.parse_krx_short_payload({"OutBlock_1": [{
            "TRD_DD": "2026/07/10", "CVSRTSELL_TRDVOL": "255,279",
            "UPTICKRULE_APPL_TRDVOL": "250,000", "UPTICKRULE_EXCPT_TRDVOL": "5,279",
            "STR_CONST_VAL1": "398,312", "CVSRTSELL_TRDVAL": "500,000,000,000",
            "STR_CONST_VAL2": "870,000,000,000",
        }]})
        prices = [{"date": "2026-07-10", "volume": 7_620_000}]
        summary = memory_flow.krx_short_summary(
            rows, prices, 711_075_500, shares_outstanding_as_of="2026-07-06",
            shares_outstanding_source="https://example.test/official-share-count",
            shares_outstanding_valid_through="2026-07-10",
        )
        self.assertEqual(255279, summary["latestShortVolumeShares"])
        self.assertAlmostEqual(3.35, summary["latestShortTransactionPct"], places=2)
        self.assertAlmostEqual(0.0560, summary["reportedNetShortBalancePctOutstanding"], places=4)
        self.assertTrue(summary["denominatorVerified"])
        self.assertIn("not a new short position", summary["caveat"])

        unverified = memory_flow.krx_short_summary(rows, prices, 711_075_500)
        self.assertIsNone(unverified["reportedNetShortBalancePctOutstanding"])
        self.assertFalse(unverified["denominatorVerified"])

        expired = memory_flow.krx_short_summary(
            rows, prices, 711_075_500, shares_outstanding_as_of="2026-07-06",
            shares_outstanding_source="https://example.test/official-share-count",
            shares_outstanding_valid_through="2026-07-09",
        )
        self.assertIsNone(expired["reportedNetShortBalancePctOutstanding"])
        self.assertFalse(expired["denominatorVerified"])

    def test_seibro_parser_keeps_stock_loan_distinct_from_directional_short(self):
        html = """
        <table><tr><th>일자</th><th>체결 주수</th><th>상환주수</th><th>잔고주수</th></tr>
        <tr><td>2026/07/09</td><td>856,467</td><td>402,577</td><td>11,396,645</td></tr>
        <tr><td>2026/07/08</td><td>378,763</td><td>152,048</td><td>10,942,755</td></tr></table>
        """
        rows = memory_flow.parse_seibro_loan_html(html)
        summary = memory_flow.securities_lending_summary(
            rows, 711_075_500, shares_outstanding_as_of="2026-07-06",
            shares_outstanding_source="https://example.test/official-share-count",
            shares_outstanding_valid_through="2026-07-09",
        )
        self.assertEqual("2026-07-09", summary["asOf"])
        self.assertEqual(11396645, summary["loanBalanceShares"])
        self.assertAlmostEqual(1.603, summary["loanBalancePctOutstanding"], places=3)
        self.assertIn("not synonymous with directional short interest", summary["caveat"])

    def test_kofia_market_leverage_detects_thin_credit_cleanup_and_cash_drain(self):
        credit = memory_flow.parse_kofia_credit_payload({"ds1": [
            {"TMPV1": "20260624", "TMPV2": "38,632,824", "TMPV3": "29,754,263", "TMPV4": "8,878,561"},
            {"TMPV1": "20260709", "TMPV2": "36,633,641", "TMPV3": "28,837,405", "TMPV4": "7,796,236"},
        ]})
        funds = memory_flow.parse_kofia_funds_payload({"ds1": [
            {"TMPV1": "20260624", "TMPV2": "136,552,702", "TMPV6": "110,793", "TMPV7": "7.5"},
            {"TMPV1": "20260709", "TMPV2": "107,127,865", "TMPV6": "142,197", "TMPV7": "10.2"},
        ]})
        summary = memory_flow.kofia_leverage_summary(credit, funds)
        self.assertAlmostEqual(-5.17, summary["totalMarginCreditFrom30dPeakPct"], places=2)
        self.assertAlmostEqual(-3.08, summary["kospiMarginCreditFrom30dPeakPct"], places=2)
        self.assertAlmostEqual(-21.55, summary["customerDepositsFrom30dPeakPct"], places=2)
        self.assertGreater(summary["marginCreditToDeposits20dChangePp"], 5)
        self.assertFalse(summary["marketWideCreditClearingConfirmed"])

    def test_missing_forced_liquidation_stays_missing_not_zero(self):
        summary = memory_flow.kofia_leverage_summary(
            [{"date": "2026-07-09", "marginCreditTotalMillionKrw": 100,
              "marginCreditKospiMillionKrw": 80}],
            [{"date": "2026-07-09", "customerDepositMillionKrw": 200}],
        )

        self.assertIsNone(summary["forcedLiquidationBillionKrw"])
        self.assertEqual("missing_latest", summary["forcedLiquidationDataStatus"])

    def test_official_kofia_balance_rejects_clean_leverage_claim(self):
        symbols = {
            "000660.KS": {
                "technical": {"shock": {"returnPct": -14}, "vsEma21Pct": -8, "levels": {}},
                "investorFlow": {"5d": {"foreignNetShares": -100, "institutionNetShares": 50}},
                "regime": "TREND_BREAK",
            },
        }
        leverage = {
            "painConfirmed": True, "averageDrawdownFrom20dHighPct": -50,
            "individualOtherResidual5dShares": 1000,
            "marketMargin": {
                "totalMarginCreditFrom30dPeakPct": -5.17,
                "kospiMarginCreditFrom30dPeakPct": -3.08,
                "customerDepositsFrom30dPeakPct": -21.55,
                "marginCreditToDeposits20dChangePp": 5.91,
                "marketWideCreditClearingConfirmed": False,
            },
        }
        washout = next(item for item in memory_flow.hypothesis_audit(symbols, leverage)
                       if item["id"] == "leverage_washed_out")
        self.assertEqual("REJECTED", washout["verdict"])
        self.assertIn("SK hynix-specific margin balance", washout["missingDecisiveData"])

    def test_nasdaq_retail_tracker_keeps_top_five_coverage_caveat(self):
        result = memory_flow.parse_nasdaq_retail_tracker({"data": {
            "date": "Data as of Jul 8, 2026",
            "table": {"rows": [{
                "ticker": ["MU", "Micron"], "activity": "-3.81%", "sentiment": "SELL -2",
            }]},
        }})
        self.assertEqual("2026-07-08", result["MU"]["asOf"])
        self.assertEqual(-2, result["MU"]["sentimentScore"])
        self.assertIn("top five", result["MU"]["coverageCaveat"])

    def test_option_oi_never_claims_known_dealer_direction(self):
        calls = pd.DataFrame([{
            "strike": 100, "openInterest": 1000, "volume": 100,
            "bid": 2.0, "ask": 2.2, "impliedVolatility": 0.5,
        }])
        puts = pd.DataFrame([{
            "strike": 100, "openInterest": 1200, "volume": 120,
            "bid": 2.1, "ask": 2.3, "impliedVolatility": 0.55,
        }])
        result = memory_flow.summarize_option_frames(
            "MU", 100, [("2026-07-17", calls, puts)], "2026-07-10T16:00:00-04:00",
        )
        self.assertFalse(result["dealerScenarios"]["directionKnown"])
        self.assertEqual("insufficient_open_interest_quality", result["dataQuality"])
        self.assertIsNone(result["putCallOpenInterestRatio"])

    def test_leverage_pain_without_balance_data_cannot_be_supported(self):
        symbols = {
            "000660.KS": {
                "technical": {"shock": {"returnPct": -14}, "vsEma21Pct": -8, "levels": {}},
                "investorFlow": {"5d": {"foreignNetShares": -100, "institutionNetShares": 50}},
                "regime": "TREND_BREAK",
            },
        }
        leverage = {
            "painConfirmed": True,
            "averageDrawdownFrom20dHighPct": -50,
            "individualOtherResidual5dShares": 1000,
        }
        audit = memory_flow.hypothesis_audit(symbols, leverage)
        washout = next(item for item in audit if item["id"] == "leverage_washed_out")
        self.assertEqual("MIXED", washout["verdict"])
        self.assertNotEqual("SUPPORTED", washout["verdict"])
        self.assertIn("KOFIA margin/credit balance", washout["missingDecisiveData"])

    def test_adr_parity_uses_ten_ads_per_local_share(self):
        config = {"skHynixAdr": {
            "localSymbol": "000660.KS", "whenIssuedSymbol": "SKHYV", "regularSymbol": "SKHY",
            "adsPerLocalShare": 10, "currencyPair": "KRW=X",
            "ratioSource": "https://www.sec.gov/example-official-ratio",
        }}
        prices = {
            "SKHYV": [{"date": "2026-07-10", "open": 150, "high": 150, "low": 150, "close": 150, "volume": 1}],
            "KRW=X": [{"date": "2026-07-10", "open": 1500, "high": 1500, "low": 1500, "close": 1500, "volume": 1}],
        }
        symbols = {"000660.KS": {"technical": {"close": 2_000_000, "priceAsOf": "2026-07-10"}}}
        now = dt.datetime(2026, 7, 10, 17, 0, tzinfo=ZoneInfo("America/New_York"))
        result = memory_flow.adr_parity(config, prices, symbols, now, "2026-07-10")
        self.assertEqual(2_250_000, result["adrImpliedLocalKrw"])
        self.assertEqual(12.5, result["premiumDiscountPct"])

    def test_adr_parity_does_not_default_an_unverified_conversion_ratio(self):
        config = {"skHynixAdr": {
            "localSymbol": "000660.KS", "whenIssuedSymbol": "SKHYV",
            "regularSymbol": "SKHY", "currencyPair": "KRW=X",
        }}
        result = memory_flow.adr_parity(
            config, {}, {"000660.KS": {}}, dt.datetime.now(dt.timezone.utc), None,
        )
        self.assertEqual("ratio_unverified", result["status"])
        self.assertFalse(result["ratioVerified"])
        self.assertIsNone(result["adsPerLocalShare"])

    def test_prelisting_adr_offer_is_an_anchor_not_a_traded_price(self):
        config = {"skHynixAdr": {
            "localSymbol": "000660.KS", "whenIssuedSymbol": "SKHYV", "regularSymbol": "SKHY",
            "adsPerLocalShare": 10, "currencyPair": "KRW=X",
            "ratioSource": "https://www.sec.gov/example-official-ratio",
            "offerPriceUsd": 149, "newCommonShares": 17_790_000,
        }}
        prices = {"KRW=X": [{"date": "2026-07-10", "open": 1500, "high": 1500,
                              "low": 1500, "close": 1500, "volume": 1}]}
        symbols = {"000660.KS": {"sharesIssued": 712_702_365,
                                  "sharesIssuedAsOf": "2026-06-24",
                                  "sharesIssuedValidThrough": "2026-07-10",
                                  "sharesIssuedSource": "https://example.test/official-share-count",
                                  "technical": {"close": 2_180_000, "priceAsOf": "2026-07-10"}}}
        now = dt.datetime(2026, 7, 10, 17, 0, tzinfo=ZoneInfo("America/New_York"))
        result = memory_flow.adr_parity(config, prices, symbols, now, "2026-07-10")
        self.assertEqual("pre_listing_offer_anchor", result["status"])
        self.assertEqual(2_235_000, result["offerImpliedLocalKrw"])
        self.assertAlmostEqual(2.496, result["newSharesPctPreOffering"], places=3)
        self.assertTrue(result["sharesIssuedDenominatorVerified"])
        self.assertNotIn("adrPriceUsd", result)

    def test_adr_dilution_denominator_expires_after_the_offering(self):
        config = {"skHynixAdr": {
            "localSymbol": "000660.KS", "whenIssuedSymbol": "SKHYV", "regularSymbol": "SKHY",
            "adsPerLocalShare": 10, "currencyPair": "KRW=X",
            "ratioSource": "https://www.sec.gov/example-official-ratio",
            "offerPriceUsd": 149, "newCommonShares": 17_790_000,
        }}
        symbols = {"000660.KS": {"sharesIssued": 712_702_365,
                                  "sharesIssuedAsOf": "2026-06-24",
                                  "sharesIssuedValidThrough": "2026-07-09",
                                  "sharesIssuedSource": "https://example.test/official-share-count",
                                  "technical": {"close": 2_180_000, "priceAsOf": "2026-07-10"}}}
        result = memory_flow.adr_parity(
            config, {}, symbols,
            dt.datetime(2026, 7, 10, 17, 0, tzinfo=ZoneInfo("America/New_York")),
            "2026-07-10",
        )
        self.assertFalse(result["sharesIssuedDenominatorVerified"])
        self.assertIsNone(result["newSharesPctPreOffering"])

    def test_no_fetch_rebuild_is_strict_json_and_private(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            universe = root / "universe.json"
            cache = root / "cache.json"
            out = root / "result.json"
            report = root / "report.md"
            universe.write_text(json.dumps({
                "schemaVersion": 1,
                "instruments": [{
                    "symbol": "MU", "companyId": "micron", "name": "Micron",
                    "subsector": "HBM", "market": "United States", "instrumentType": "equity",
                    "benchmark": "SMH", "decisionSymbol": True,
                }],
                "benchmarks": ["SMH"],
                "skHynixAdr": {},
            }), encoding="utf-8")
            rows = price_rows(count=40)
            cache.write_text(json.dumps({
                "schemaVersion": 1, "updatedAt": "2026-07-09T20:00:00-04:00",
                "priceHistory": {"MU": rows, "SMH": rows},
            }), encoding="utf-8")
            code = memory_flow.main([
                "--universe", str(universe), "--cache", str(cache),
                "--out", str(out), "--report", str(report), "--no-fetch",
            ])
            self.assertEqual(0, code)
            payload = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(1, payload["schemaVersion"])
            self.assertEqual(0o600, out.stat().st_mode & 0o777)


if __name__ == "__main__":
    unittest.main()

import importlib.util
import io
import json
import pathlib
import sys
import unittest
import zipfile


SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "semi_leverage_tracker.py"
SPEC = importlib.util.spec_from_file_location("semi_leverage_tracker", SCRIPT)
tracker = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = tracker
SPEC.loader.exec_module(tracker)


def make_prices(count=80):
    start = tracker.dt.date(2026, 1, 1)
    rows = []
    day = start
    while len(rows) < count:
        if day.weekday() < 5:
            rows.append({"date": day.isoformat(), "close": 100.0 + len(rows)})
        day += tracker.dt.timedelta(days=1)
    return rows


class SemiLeverageTrackerTests(unittest.TestCase):
    def test_kofia_ratio_and_units(self):
        credit = {
            "ds1": [
                {
                    "TMPV1": "20260102",
                    "TMPV2": 20_000_000,
                    "TMPV3": 12_000_000,
                    "TMPV4": 8_000_000,
                    "TMPV5": 20_000,
                    "TMPV9": 21_000_000,
                },
                {
                    "TMPV1": "20260105",
                    "TMPV2": 21_000_000,
                    "TMPV3": 13_000_000,
                    "TMPV4": 8_000_000,
                    "TMPV5": 22_000,
                    "TMPV9": 21_500_000,
                },
            ]
        }
        funds = {
            "ds1": [
                {"TMPV1": "20260102", "TMPV2": 50_000_000, "TMPV5": 500_000, "TMPV6": 5_000, "TMPV7": 1.0},
                {"TMPV1": "20260105", "TMPV2": 50_000_000, "TMPV5": 600_000, "TMPV6": 6_000, "TMPV7": 1.1},
            ]
        }

        rows = tracker.normalize_kofia(credit, funds)

        self.assertEqual(rows[0]["creditLoansKrwBn"], 20_000.0)
        self.assertEqual(rows[0]["investorDepositsKrwBn"], 50_000.0)
        self.assertEqual(rows[0]["leverageRatioPct"], 40.0)
        self.assertEqual(rows[1]["ratioChangePp"], 2.0)

    def test_short_volume_is_aggregated_before_ratio(self):
        text = (
            '"tradeReportDate","shortParQuantity","shortExemptParQuantity","totalParQuantity","reportingFacilityCode"\n'
            '"2026-01-02","20","1","100","A"\n'
            '"2026-01-02","80","2","100","B"\n'
            '"2026-01-05","45","0","100","A"\n'
            '"2026-01-05","55","0","100","B"\n'
        )

        rows = tracker.aggregate_short_volume(text)

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["shortSharePct"], 50.0)
        self.assertEqual(rows[0]["facilityCount"], 2)

    def test_finra_availability_uses_following_month(self):
        self.assertEqual(tracker.finra_availability_date("2026-05"), "2026-06-25")
        self.assertEqual(tracker.finra_availability_date("2025-12"), "2026-01-25")

    def test_xlsx_parser_reads_inline_strings_and_ratios(self):
        sheet = """<?xml version="1.0" encoding="UTF-8"?>
        <worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>
        <row r="1"><c r="A1" t="inlineStr"><is><t>Year-Month</t></is></c><c r="B1" t="inlineStr"><is><t>Debit</t></is></c><c r="C1" t="inlineStr"><is><t>Cash</t></is></c><c r="D1" t="inlineStr"><is><t>Margin</t></is></c></row>
        <row r="2"><c r="A2" t="inlineStr"><is><t>2026-05</t></is></c><c r="B2"><v>1400</v></c><c r="C2"><v>200</v></c><c r="D2"><v>220</v></c></row>
        </sheetData></worksheet>"""
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("xl/worksheets/sheet1.xml", sheet)

        rows = tracker.parse_finra_margin_xlsx(buffer.getvalue())

        self.assertEqual(rows[0]["date"], "2026-05-31")
        self.assertEqual(rows[0]["debitUsdBn"], 1.4)
        self.assertAlmostEqual(rows[0]["leverageRatioX"], 1400 / 420, places=5)

    def test_alignment_never_uses_price_before_availability(self):
        prices = make_prices(15)
        metrics = [
            {"date": "2026-01-02", "ratio": 10.0},
            {"date": "2026-01-05", "ratio": 11.0},
        ]

        aligned = tracker.align_observations(metrics, "ratio", prices, lag_calendar_days=1)

        self.assertEqual(aligned[0]["availableDate"], "2026-01-03")
        self.assertEqual(aligned[0]["priceDate"], "2026-01-05")
        self.assertGreaterEqual(aligned[0]["priceDate"], aligned[0]["availableDate"])
        self.assertEqual(aligned[1]["priceDate"], "2026-01-06")

    def test_forward_analysis_uses_future_sessions(self):
        prices = make_prices(80)
        metrics = [
            {"date": prices[i]["date"], "ratio": float(i)}
            for i in range(0, 50, 2)
        ]

        result = tracker.relationship_analysis(
            "TEST",
            "Test",
            "Test",
            "ratio",
            "change",
            metrics,
            "ratio",
            prices,
            [("1d", 1), ("5d", 5)],
            "daily",
            lag_calendar_days=0,
            block_length=3,
        )

        self.assertGreater(result["alignedObservations"], 20)
        self.assertEqual(result["forward"][0]["sessions"], 1)
        self.assertGreater(result["forward"][0]["n"], 20)

    def test_event_study_separates_top_and_bottom_quintiles(self):
        prices = []
        start = tracker.dt.date(2026, 1, 1)
        close = 100.0
        for i in range(70):
            date = start + tracker.dt.timedelta(days=i)
            close *= 1.01 if i % 5 == 0 else 0.999
            prices.append({"date": date.isoformat(), "close": close})
        metrics = [
            {"date": (start + tracker.dt.timedelta(days=i)).isoformat(), "ratio": float((i % 10) ** 2)}
            for i in range(45)
        ]

        result = tracker.relationship_analysis(
            "TEST",
            "Test",
            "Test",
            "ratio",
            "change",
            metrics,
            "ratio",
            prices,
            [("1d", 1), ("5d", 5)],
            "daily",
            lag_calendar_days=0,
            block_length=3,
        )

        self.assertGreater(result["eventStudy"]["topCount"], 0)
        self.assertGreater(result["eventStudy"]["bottomCount"], 0)
        self.assertIsNotNone(result["eventStudy"]["spreadPct"])

    def test_cross_market_pressure_is_standardized_separately(self):
        korea = [{"date": f"2026-01-{i:02d}", "ratio": float(i)} for i in range(1, 11)]
        us = [{"date": f"2025-{i:02d}-28", "ratio": float(i * 100)} for i in range(1, 11)]

        korea_pressure = tracker.current_pressure(korea, "ratio", 10)
        us_pressure = tracker.current_pressure(us, "ratio", 10)

        self.assertAlmostEqual(korea_pressure["zScore"], us_pressure["zScore"], places=2)
        self.assertEqual(korea_pressure["percentile"], us_pressure["percentile"])

    def test_indexed_chart_preserves_actual_adjusted_closes(self):
        metrics = [
            {"date": "2026-01-02", "ratio": 10.0},
            {"date": "2026-01-05", "ratio": 12.0},
        ]
        prices = [
            {"date": "2026-01-02", "close": 200.0},
            {"date": "2026-01-05", "close": 250.0},
        ]

        rows = tracker.indexed_chart(metrics, "ratio", {"TEST": prices}, 10)

        self.assertEqual(rows[1]["TEST"], 125.0)
        self.assertEqual(rows[1]["priceValues"]["TEST"], 250.0)

    def test_naver_quote_parser_keeps_currency_and_timestamp(self):
        content = json.dumps(
            {
                "closePrice": "279,500",
                "localTradedAt": "2026-07-15T16:10:20+09:00",
                "marketStatus": "CLOSE",
                "endUrl": "https://m.stock.naver.com/domestic/stock/005930",
            }
        ).encode()

        quote = tracker.parse_naver_quote(content, "005930.KS")

        self.assertEqual(quote["price"], 279500.0)
        self.assertEqual(quote["currency"], "KRW")
        self.assertEqual(quote["asOfLabel"], "2026-07-15 16:10 KST")

    def test_nasdaq_quote_parser_distinguishes_after_hours_from_close(self):
        content = json.dumps(
            {
                "data": {
                    "marketStatus": "After-Hours",
                    "primaryData": {
                        "lastSalePrice": "$899.1575",
                        "lastTradeTimestamp": "Jul 15, 2026 6:33 PM ET",
                    },
                    "secondaryData": {
                        "lastSalePrice": "$904.28",
                        "lastTradeTimestamp": "Closed at Jul 15, 2026 4:00 PM ET",
                    },
                }
            }
        ).encode()

        quote = tracker.parse_nasdaq_quote(content, "MU")

        self.assertEqual(quote["price"], 899.1575)
        self.assertEqual(quote["regularClose"], 904.28)
        self.assertEqual(quote["session"], "After-Hours")


if __name__ == "__main__":
    unittest.main()

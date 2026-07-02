import datetime as dt
import importlib.util
import pathlib
import sys

import pytest

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "financial_status_score.py"
SPEC = importlib.util.spec_from_file_location("financial_status_score", SCRIPT)
fin = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = fin
SPEC.loader.exec_module(fin)


def ok_status(rows=1):
    return {"status": "fresh", "rows": rows, "ok": bool(rows), "error": None}


def test_classify_omits_funds_and_private_exposure():
    assert fin.classify_omission({"sym": "QQQ", "name": "INVESCO QQQ TR", "assetClass": "宽基指数ETF"})
    assert fin.classify_omission({"sym": "SOXQ", "name": "INVESCO EXCHANGE-TRADED FD TR", "assetClass": "个股"})
    assert fin.classify_omission({"sym": "SPCX", "name": "SPACE EXPL TECHNOLOGIES CORP CL A", "assetClass": "个股"})
    assert fin.classify_omission({"sym": "NVDA", "name": "NVIDIA CORPORATION COM", "assetClass": "个股"}) is None


def test_compute_earnings_prefers_calendar_timing_for_next_date():
    today = dt.date(2026, 7, 2)
    bundle = {
        "earnings": [
            {"symbol": "NVDA", "date": "2026-08-26", "epsEstimated": 2.08, "revenueEstimated": 91_707_790_000},
            {"symbol": "NVDA", "date": "2026-05-20", "epsActual": 1.70, "epsEstimated": 1.60, "revenueActual": 44, "revenueEstimated": 40},
            {"symbol": "NVDA", "date": "2026-02-25", "epsActual": 1.52, "epsEstimated": 1.44, "revenueActual": 38, "revenueEstimated": 37},
        ]
    }
    calendar = {
        "symbol": "NVDA",
        "date": "2026-08-26",
        "time": "amc",
        "confirmed": True,
        "epsEstimated": 2.08,
        "revenueEstimated": 91_707_790_000,
    }

    out = fin.compute_earnings(bundle, calendar, today)

    assert out["nextDate"] == "2026-08-26"
    assert out["nextTime"] == "amc"
    assert out["nextConfirmed"] is True
    assert out["epsBeatRate"] == 100.0
    assert out["earningsReportScore"] > 70


def test_assign_gate_prioritizes_low_data_confidence():
    gate, reasons = fin.assign_gate(
        final_score=75,
        financial=80,
        earnings=80,
        event_risk=40,
        data_conf=20,
        metrics={},
        earnings_meta={"daysToNext": 60},
    )

    assert gate == "DATA_REVIEW"
    assert reasons[0]["rule"] == "DATA_COVERAGE"


def test_score_company_strong_financials_with_complete_bundle():
    today = dt.date(2026, 7, 2)
    bundle = {
        "profile": [{"symbol": "TEST", "companyName": "Test Co", "sector": "Technology", "industry": "Semiconductors", "marketCap": 1_000_000_000, "beta": 1.2}],
        "financialScores": [{"symbol": "TEST", "altmanZScore": 7.0, "piotroskiScore": 8}],
        "ratiosTtm": [
            {
                "symbol": "TEST",
                "grossProfitMarginTTM": 0.68,
                "operatingProfitMarginTTM": 0.36,
                "netProfitMarginTTM": 0.26,
                "currentRatioTTM": 2.1,
                "quickRatioTTM": 1.5,
                "debtToEquityRatioTTM": 0.2,
                "interestCoverageRatioTTM": 20,
                "operatingCashFlowSalesRatioTTM": 0.34,
                "freeCashFlowOperatingCashFlowRatioTTM": 0.9,
                "capitalExpenditureCoverageRatioTTM": 10,
                "priceToEarningsRatioTTM": 24,
                "priceToSalesRatioTTM": 8,
                "priceToFreeCashFlowRatioTTM": 28,
            }
        ],
        "keyMetricsTtm": [
            {
                "symbol": "TEST",
                "returnOnEquityTTM": 0.38,
                "returnOnAssetsTTM": 0.2,
                "returnOnInvestedCapitalTTM": 0.28,
                "netDebtToEBITDATTM": 0.2,
                "freeCashFlowYieldTTM": 0.05,
                "incomeQualityTTM": 1.3,
                "evToEBITDATTM": 18,
            }
        ],
        "financialGrowth": [
            {"symbol": "TEST", "date": "2026-03-31", "revenueGrowth": 0.25, "netIncomeGrowth": 0.31, "operatingIncomeGrowth": 0.29, "operatingCashFlowGrowth": 0.2, "freeCashFlowGrowth": 0.22},
            {"symbol": "TEST", "date": "2025-12-31", "revenueGrowth": 0.22, "netIncomeGrowth": 0.28, "operatingIncomeGrowth": 0.25, "operatingCashFlowGrowth": 0.19, "freeCashFlowGrowth": 0.2},
        ],
        "earnings": [
            {"symbol": "TEST", "date": "2026-05-01", "epsActual": 1.1, "epsEstimated": 1.0, "revenueActual": 110, "revenueEstimated": 100},
            {"symbol": "TEST", "date": "2026-02-01", "epsActual": 1.0, "epsEstimated": 0.92, "revenueActual": 105, "revenueEstimated": 100},
        ],
    }
    statuses = {k: ok_status(len(v)) for k, v in bundle.items()}
    stock = {"sym": "TEST", "name": "Test Co", "value": 1000, "shares": 10, "portfolioWeightPct": 1.0, "assetClass": "个股", "theme": "半导体"}
    calendar = {"symbol": "TEST", "date": "2026-09-01", "time": "bmo", "confirmed": True, "epsEstimated": 1.2, "revenueEstimated": 120}

    out = fin.score_company(stock, bundle, statuses, calendar, today)

    assert out["gate"] == "STRONG_FINANCIALS"
    assert out["financialStatusScore"] >= 85
    assert out["earnings"]["nextTime"] == "bmo"


def test_merge_bundles_preserves_fmp_priority_and_uses_yahoo_fallback():
    fmp = {
        "profile": [{"symbol": "TEST", "companyName": "FMP Name", "marketCap": 100}],
        "ratiosTtm": [],
        "keyMetricsTtm": [{"symbol": "TEST", "returnOnEquityTTM": 0.2}],
        "financialGrowth": [],
        "earnings": [{"symbol": "TEST", "date": "2026-05-01", "epsActual": 1.1, "epsEstimated": 1.0}],
        "financialScores": [{"symbol": "TEST", "altmanZScore": 4, "piotroskiScore": 7}],
    }
    yahoo = {
        "profile": [{"symbol": "TEST", "companyName": "Yahoo Name", "sector": "Technology", "marketCap": 200}],
        "ratiosTtm": [{"symbol": "TEST", "grossProfitMarginTTM": 0.5}],
        "keyMetricsTtm": [{"symbol": "TEST", "returnOnAssetsTTM": 0.1}],
        "financialGrowth": [{"symbol": "TEST", "date": "2026-03-31", "revenueGrowth": 0.2}],
        "earnings": [{"symbol": "TEST", "date": "2026-08-01", "epsEstimated": 1.2}],
    }

    merged = fin.merge_bundles(fmp, yahoo, {})

    assert merged["profile"][0]["companyName"] == "FMP Name"
    assert merged["profile"][0]["sector"] == "Technology"
    assert merged["ratiosTtm"][0]["grossProfitMarginTTM"] == 0.5
    assert merged["keyMetricsTtm"][0]["returnOnEquityTTM"] == 0.2
    assert merged["keyMetricsTtm"][0]["returnOnAssetsTTM"] == 0.1
    assert len(merged["earnings"]) == 2


def test_build_yahoo_bundle_from_statement_frames():
    pd = pytest.importorskip("pandas")
    cols = pd.to_datetime(["2026-03-31", "2025-12-31", "2025-09-30", "2025-06-30", "2025-03-31"])
    income = pd.DataFrame(
        [
            [120, 110, 105, 100, 90],
            [72, 66, 60, 55, 45],
            [36, 31, 30, 27, 20],
            [28, 24, 22, 20, 14],
            [2, 2, 2, 2, 2],
            [40, 34, 32, 30, 23],
        ],
        index=["Total Revenue", "Gross Profit", "Operating Income", "Net Income", "Interest Expense", "EBITDA"],
        columns=cols,
    )
    balance = pd.DataFrame(
        [[500], [220], [150], [90], [40], [60], [250]],
        index=["Total Assets", "Stockholders Equity", "Current Assets", "Current Liabilities", "Total Debt", "Net Debt", "Invested Capital"],
        columns=[cols[0]],
    )
    cashflow = pd.DataFrame(
        [
            [32, 30, 29, 27, 20],
            [28, 25, 24, 22, 16],
            [-4, -5, -5, -5, -4],
        ],
        index=["Operating Cash Flow", "Free Cash Flow", "Capital Expenditure"],
        columns=cols,
    )
    earnings = pd.DataFrame(
        {"EPS Estimate": [1.0], "Reported EPS": [1.1], "Surprise(%)": [10.0]},
        index=pd.to_datetime(["2026-05-01"]),
    )

    out = fin.build_yahoo_bundle(
        "TEST",
        {"longName": "Test Co", "sector": "Technology", "industry": "Software", "beta": 1.1, "trailingPE": 20, "priceToSalesTrailing12Months": 4, "enterpriseToEbitda": 15},
        {"marketCap": 1000},
        income,
        balance,
        cashflow,
        earnings,
    )

    assert out["profile"][0]["companyName"] == "Test Co"
    assert out["ratiosTtm"][0]["grossProfitMarginTTM"] > 0.55
    assert out["keyMetricsTtm"][0]["freeCashFlowYieldTTM"] > 0.08
    assert out["financialGrowth"][0]["revenueGrowth"] > 0.25
    assert out["earnings"][0]["epsActual"] == 1.1

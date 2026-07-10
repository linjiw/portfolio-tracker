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
    assert fin.classify_omission({"sym": "SPCX", "name": "SPACE EXPL TECHNOLOGIES CORP CL A", "assetClass": "个股"}) is None
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


def test_compute_earnings_keeps_unreported_today_as_event_risk():
    today = dt.date(2026, 7, 9)
    bundle = {"earnings": [{"symbol": "TEST", "date": "2026-07-09", "epsEstimated": 1.25}]}

    out = fin.compute_earnings(bundle, None, today)

    assert out["nextDate"] == "2026-07-09"
    assert out["daysToNext"] == 0
    assert out["latestDate"] is None


def test_sec_fcf_subtracts_positive_capex_outflow():
    def fact(value, tag):
        return {"units": {"USD": [{"val": value, "form": "10-K", "fp": "FY",
                                    "end": "2025-12-31", "filed": "2026-02-01", "frame": "CY2025"}]}}
    companyfacts = {"entityName": "Test Co", "facts": {"us-gaap": {
        "NetCashProvidedByUsedInOperatingActivities": fact(100, "ocf"),
        "PaymentsToAcquirePropertyPlantAndEquipment": fact(30, "capex"),
    }}}

    bundle = fin.build_sec_bundle("TEST", {"title": "Test Co"}, companyfacts, market_cap=700)
    ratio = bundle["ratiosTtm"][0]["priceToFreeCashFlowRatioTTM"]

    assert ratio == 10


def test_sec_fallback_never_mixes_different_fiscal_years_in_one_margin():
    def duration(rows):
        return {"units": {"USD": rows}}
    companyfacts = {"entityName": "Test Co", "facts": {"us-gaap": {
        "Revenues": duration([
            {"val": 200, "form": "10-K", "fp": "FY", "end": "2025-12-31",
             "filed": "2026-02-01", "frame": "CY2025"},
        ]),
        "GrossProfit": duration([
            {"val": 80, "form": "10-K", "fp": "FY", "end": "2024-12-31",
             "filed": "2025-02-01", "frame": "CY2024"},
        ]),
        "NetIncomeLoss": duration([
            {"val": 20, "form": "10-K", "fp": "FY", "end": "2025-12-31",
             "filed": "2026-02-01", "frame": "CY2025"},
        ]),
    }}}

    bundle = fin.build_sec_bundle("TEST", {"title": "Test Co"}, companyfacts)
    ratios = bundle["ratiosTtm"][0]

    assert ratios["periodEnd"] == "2025-12-31"
    assert ratios["grossProfitMarginTTM"] is None


def test_sec_debt_prefers_reported_total_over_component_sum():
    def fact(value):
        return {"units": {"USD": [{
            "val": value, "form": "10-K", "fp": "FY", "end": "2025-12-31",
            "filed": "2026-02-01", "frame": "CY2025",
        }]}}

    companyfacts = {"entityName": "Test Co", "facts": {"us-gaap": {
        "Revenues": fact(500),
        "Assets": fact(500),
        "StockholdersEquity": fact(200),
        "LongTermDebtAndFinanceLeaseObligations": fact(90),
        "DebtCurrent": fact(30),
        "LongTermDebtNoncurrent": fact(70),
    }}}

    ratios = fin.build_sec_bundle("TEST", {}, companyfacts)["ratiosTtm"][0]

    assert ratios["debtToEquityRatioTTM"] == 0.45
    assert ratios["debtToAssetsRatioTTM"] == 0.18
    assert ratios["totalDebtDefinition"] == "reported_total_debt"


def test_sec_debt_fallback_sums_current_and_noncurrent_at_same_period():
    def fact(rows):
        return {"units": {"USD": rows}}

    annual = {
        "form": "10-K", "fp": "FY", "end": "2025-12-31",
        "filed": "2026-02-01", "frame": "CY2025",
    }
    companyfacts = {"entityName": "Test Co", "facts": {"us-gaap": {
        "Revenues": fact([dict(annual, val=500)]),
        "Assets": fact([dict(annual, val=500)]),
        "StockholdersEquity": fact([dict(annual, val=200)]),
        "DebtCurrent": fact([dict(annual, val=30)]),
        "LongTermDebtNoncurrent": fact([
            dict(annual, val=70),
            {"val": 999, "form": "10-K", "fp": "FY", "end": "2024-12-31",
             "filed": "2025-02-01", "frame": "CY2024"},
        ]),
    }}}

    ratios = fin.build_sec_bundle("TEST", {}, companyfacts)["ratiosTtm"][0]

    assert ratios["debtToEquityRatioTTM"] == 0.5
    assert ratios["debtToAssetsRatioTTM"] == 0.2
    assert ratios["totalDebtDefinition"] == "same_period_current_plus_noncurrent_debt"


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


def test_merge_bundles_uses_null_fallback_preserves_zero_and_deduplicates_periods():
    fmp = {
        "ratiosTtm": [{"symbol": "TEST", "grossProfitMarginTTM": None, "debtToEquityRatioTTM": 0}],
        "financialGrowth": [{"symbol": "TEST", "date": "2026-03-31", "revenueGrowth": None, "netIncomeGrowth": 0.3}],
        "earnings": [{"symbol": "TEST", "date": "2026-05-01", "epsActual": 1.1, "epsEstimated": None}],
    }
    yahoo = {
        "ratiosTtm": [{"symbol": "TEST", "grossProfitMarginTTM": 0.5, "debtToEquityRatioTTM": 0.4}],
        "financialGrowth": [{"symbol": "TEST", "date": "2026-03-31", "revenueGrowth": 0.2, "netIncomeGrowth": 0.1}],
        "earnings": [{"symbol": "TEST", "date": "2026-05-01", "epsActual": 1.05, "epsEstimated": 1.0}],
    }

    merged = fin.merge_bundles(fmp, yahoo, {})

    assert merged["ratiosTtm"][0]["grossProfitMarginTTM"] == 0.5
    assert merged["ratiosTtm"][0]["debtToEquityRatioTTM"] == 0
    assert len(merged["financialGrowth"]) == 1
    assert merged["financialGrowth"][0]["revenueGrowth"] == 0.2
    assert merged["financialGrowth"][0]["netIncomeGrowth"] == 0.3
    assert len(merged["earnings"]) == 1
    assert merged["earnings"][0]["epsActual"] == 1.1
    assert merged["earnings"][0]["epsEstimated"] == 1.0


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
    # Operating income divided by one current invested-capital snapshot is not
    # standard ROIC and must not be published under the standard field.
    assert out["keyMetricsTtm"][0]["returnOnInvestedCapitalTTM"] is None
    assert out["keyMetricsTtm"][0]["returnOnInvestedCapitalMethod"] is None
    assert out["financialGrowth"][0]["revenueGrowth"] > 0.25
    assert out["earnings"][0]["epsActual"] == 1.1


def test_yahoo_calculates_standard_roic_and_same_period_component_debt():
    pd = pytest.importorskip("pandas")
    income_cols = pd.to_datetime(["2026-03-31", "2025-12-31", "2025-09-30", "2025-06-30"])
    balance_cols = pd.to_datetime([
        "2026-03-31", "2025-12-31", "2025-09-30", "2025-06-30", "2025-03-31",
    ])
    income = pd.DataFrame(
        [[40, 40, 40, 40], [50, 50, 50, 50], [10, 10, 10, 10]],
        index=["Operating Income", "Pretax Income", "Tax Provision"],
        columns=income_cols,
    )
    balance = pd.DataFrame(
        [
            [500, 490, 480, 470, 460],
            [200, 195, 190, 185, 180],
            [20, 20, 20, 20, 20],
            [60, 60, 60, 60, 60],
            [240, 230, 220, 210, 160],
        ],
        index=[
            "Total Assets", "Stockholders Equity", "Current Debt", "Long Term Debt",
            "Invested Capital",
        ],
        columns=balance_cols,
    )

    out = fin.build_yahoo_bundle("TEST", {}, {}, income, balance, None, None)
    ratios = out["ratiosTtm"][0]
    metrics = out["keyMetricsTtm"][0]

    assert ratios["debtToEquityRatioTTM"] == 0.4
    assert ratios["debtToAssetsRatioTTM"] == 0.16
    assert ratios["totalDebtDefinition"] == "same_period_current_plus_noncurrent_debt"
    # TTM operating income 160 * (1 - 20% tax) / average IC ((240+160)/2).
    assert metrics["returnOnInvestedCapitalTTM"] == pytest.approx(0.64)
    assert metrics["returnOnInvestedCapitalMethod"] == "nopat_ttm_over_average_invested_capital"
    assert metrics["effectiveTaxRateTTMForROIC"] == pytest.approx(0.2)


def test_yahoo_debt_does_not_add_components_to_reported_total():
    pd = pytest.importorskip("pandas")
    columns = pd.to_datetime(["2026-03-31"])
    balance = pd.DataFrame(
        [[70], [20], [60]],
        index=["Total Debt", "Current Debt", "Long Term Debt"],
        columns=columns,
    )

    debt, definition = fin.yahoo_comprehensive_debt(balance)

    assert debt == 70
    assert definition == "reported_total_debt"


def test_yahoo_ttm_sum_requires_four_complete_quarters():
    pd = pytest.importorskip("pandas")
    cols = pd.to_datetime(["2026-03-31", "2025-12-31", "2025-09-30", "2025-06-30"])
    income = pd.DataFrame(
        [[100, 100, 100, 100], [60, 60, None, 60]],
        index=["Total Revenue", "Gross Profit"], columns=cols,
    )

    out = fin.build_yahoo_bundle("TEST", {"trailingPE": 20}, {}, income, None, None, None)

    # A three-quarter numerator must not be divided by a four-quarter revenue
    # denominator and mislabeled as a TTM gross margin.
    assert out["ratiosTtm"][0]["grossProfitMarginTTM"] is None


def test_empty_yahoo_identity_row_does_not_claim_usable_financial_data():
    out = fin.build_yahoo_bundle("TEST", {}, {}, None, None, None, None)

    assert out["profile"] == []
    assert out["ratiosTtm"] == []
    assert out["keyMetricsTtm"] == []


def test_cached_yahoo_error_is_not_counted_as_valid_source_coverage(tmp_path):
    client = fin.FmpClient(None, tmp_path / "cache.json", no_fetch=True)
    key = client.cache_key("yahoo:bundle", {"symbol": "TEST"})
    client.put_cache(key, "yahoo:bundle", {"symbol": "TEST"}, "error", {"error": "rate limited"})

    payload, statuses = fin.fetch_yahoo_bundle(client, "TEST")

    assert payload["error"] == "rate limited"
    assert statuses["yahoo"]["ok"] is False
    assert statuses["yahoo"]["rows"] == 0
    assert statuses["yahoo"]["status"] == "cache_error"


def test_no_fetch_stale_cache_is_retained_for_audit_but_not_counted_current(tmp_path):
    client = fin.FmpClient(None, tmp_path / "cache.json", no_fetch=True, cache_ttl_hours=18)
    key = client.cache_key("yahoo:bundle", {"symbol": "TEST"})
    client.put_cache(
        key, "yahoo:bundle", {"symbol": "TEST"}, "fresh",
        {"ratiosTtm": [{"grossProfitMarginTTM": 0.5}]},
    )
    client.cache["entries"][key]["fetchedAt"] = "2020-01-01T00:00:00Z"

    payload, statuses = fin.fetch_yahoo_bundle(client, "TEST")

    assert payload["ratiosTtm"]
    assert statuses["yahoo"]["status"] == "stale_cache"
    assert statuses["yahoo"]["ok"] is False


def test_any_stale_priority_source_forces_data_review_gate():
    bundle = {
        "profile": [{"companyName": "Test"}],
        "ratiosTtm": [{"grossProfitMarginTTM": 0.7, "operatingProfitMarginTTM": 0.4,
                       "netProfitMarginTTM": 0.3, "currentRatioTTM": 2,
                       "debtToEquityRatioTTM": 0.1, "priceToEarningsRatioTTM": 20}],
        "keyMetricsTtm": [{"returnOnEquityTTM": 0.4, "returnOnAssetsTTM": 0.2}],
        "earnings": [{"date": "2026-05-01", "epsActual": 2, "epsEstimated": 1}],
    }
    statuses = {
        "fmp:ratios": {"status": "stale_cache", "rows": 1, "ok": False, "source": "fmp"},
    }

    out = fin.score_company(
        {"sym": "TEST", "name": "Test", "portfolioWeightPct": 1, "value": 100, "shares": 1},
        bundle, statuses, {"date": "2026-09-01"}, dt.date(2026, 7, 9),
    )

    assert out["gate"] == "DATA_REVIEW"
    assert out["gateReasons"][0]["rule"] == "STALE_SOURCE_CACHE"


def test_revenue_beat_history_is_scored_even_when_eps_history_is_missing():
    out = fin.compute_earnings(
        {
            "earnings": [
                {
                    "date": "2026-05-01",
                    "revenueActual": 110,
                    "revenueEstimated": 100,
                }
            ]
        },
        None,
        dt.date(2026, 7, 9),
    )

    assert out["rowsUsed"] == 0
    assert out["revenueRowsUsed"] == 1
    assert out["revenueBeatRate"] == 100.0


def test_unknown_next_earnings_date_cannot_receive_strong_financials_gate():
    gate, reasons = fin.assign_gate(
        final_score=95,
        financial=95,
        earnings=95,
        event_risk=55,
        data_conf=95,
        metrics={"piotroskiScore": 9, "altmanZScore": 8},
        earnings_meta={"daysToNext": None},
    )

    assert gate == "EARNINGS_WATCH"
    assert reasons[0]["rule"] == "EARNINGS_WINDOW"


def test_ratio_normalization_requires_an_explicit_unit_and_never_guesses_by_magnitude():
    assert fin.pct_ratio(3.5, unit="decimal") == 3.5
    assert fin.pct_ratio(3.5, unit="percent") == 0.035
    assert fin.pct_ratio(0, unit="decimal") == 0
    with pytest.raises(TypeError):
        fin.pct_ratio(25)
    with pytest.raises(ValueError, match="unsupported ratio unit"):
        fin.pct_ratio(25, unit="auto")


def test_zero_metrics_do_not_fall_through_to_secondary_sources():
    bundle = {
        "profile": [{"symbol": "ZERO", "marketCap": 0}],
        "financialScores": [],
        "ratiosTtm": [{
            "symbol": "ZERO",
            "currentRatioTTM": 0,
            "enterpriseValueMultipleTTM": 20,
        }],
        "keyMetricsTtm": [{
            "symbol": "ZERO",
            "currentRatioTTM": 2,
            "marketCap": 100,
            "evToEBITDATTM": 0,
        }],
        "financialGrowth": [],
    }

    component, _ = fin.compute_components(bundle, {})

    assert component["metrics"]["currentRatioTTM"] == 0
    assert component["metrics"]["marketCap"] == 0
    assert component["metrics"]["evToEBITDATTM"] == 0


def test_zero_component_scores_are_not_replaced_by_neutral_defaults(monkeypatch):
    monkeypatch.setattr(
        fin,
        "compute_components",
        lambda bundle, statuses: ({
            "financialStatusScore": 0,
            "components": {"dataConfidence": 0, "valuation": 0},
            "metrics": {"companyName": "Zero Co", "beta": None},
        }, {}),
    )
    monkeypatch.setattr(
        fin,
        "compute_earnings",
        lambda bundle, calendar, today: {
            "earningsReportScore": 0,
            "daysToNext": 60,
        },
    )

    out = fin.score_company(
        {"sym": "ZERO", "name": "Zero Co", "portfolioWeightPct": 0, "value": 0, "shares": 0},
        {},
        {},
        None,
        dt.date(2026, 7, 9),
    )

    assert out["financialStatusScore"] == 0
    assert out["earningsReportScore"] == 0
    assert out["dataConfidenceScore"] == 0
    assert out["gate"] == "DATA_REVIEW"


def test_symbol_subset_keeps_full_portfolio_exposure_denominator():
    payload = {
        "stocks": [
            {"sym": "AAA", "held": True, "value": 30, "name": "A"},
            {"sym": "BBB", "held": True, "value": 70, "name": "B"},
        ]
    }

    scored, omitted = fin.portfolio_rows(payload, ["AAA"])

    assert omitted == []
    assert len(scored) == 1
    assert scored[0]["portfolioWeightPct"] == 30.0


def test_historical_as_of_fails_closed_unless_explicitly_labeled_non_point_in_time():
    run_date = dt.date(2026, 7, 9)
    with pytest.raises(SystemExit, match="historical --as-of"):
        fin.temporal_integrity(dt.date(2026, 7, 8), run_date)

    provenance = fin.temporal_integrity(
        dt.date(2026, 7, 8),
        run_date,
        allow_non_point_in_time=True,
    )
    assert provenance["fundamentalsPointInTime"] is False
    assert provenance["backtestEligible"] is False
    assert provenance["overrideUsed"] is True
    assert "Current/as-known" in provenance["warning"]


def test_future_as_of_is_rejected():
    with pytest.raises(SystemExit, match="in the future"):
        fin.temporal_integrity(dt.date(2026, 7, 10), dt.date(2026, 7, 9))


def test_fmp_secret_config_requires_private_regular_file(tmp_path, monkeypatch):
    monkeypatch.delenv("FMP_API_KEY", raising=False)
    config = tmp_path / "fmp.json"
    config.write_text('{"apiKey":"secret","secUserAgent":"test test@example.com"}', encoding="utf-8")
    config.chmod(0o644)

    with pytest.raises(PermissionError, match="0600"):
        fin.load_api_key(config)

    config.chmod(0o600)
    assert fin.load_api_key(config) == "secret"
    assert fin.load_sec_user_agent(config) == "test test@example.com"


def test_fmp_secret_config_rejects_symlink(tmp_path, monkeypatch):
    monkeypatch.delenv("FMP_API_KEY", raising=False)
    target = tmp_path / "actual.json"
    target.write_text('{"apiKey":"secret"}', encoding="utf-8")
    target.chmod(0o600)
    link = tmp_path / "fmp.json"
    link.symlink_to(target)

    with pytest.raises(PermissionError, match="non-symlink"):
        fin.load_api_key(link)

import datetime as dt
import importlib.util
import json
import math
from pathlib import Path

import pandas as pd
import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "close_vs_intraday.py"
SPEC = importlib.util.spec_from_file_location("close_vs_intraday", SCRIPT)
close = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(close)


def test_daily_metrics_use_open_range_midpoint_and_close_location():
    rows = [
        {"date": "2026-06-10", "open": 100, "high": 110, "low": 90, "close": 95},
        {"date": "2026-07-01", "open": 100, "high": 110, "low": 90, "close": 105},
        # Outside the requested window and therefore excluded.
        {"date": "2026-05-01", "open": 100, "high": 110, "low": 90, "close": 80},
    ]

    observations = close.daily_observations(
        rows, dt.date(2026, 6, 1), dt.date(2026, 7, 9)
    )
    result = close.aggregate_weighted(
        {"AAA": observations},
        {"AAA": 1.0},
        close.DAILY_BOOLEAN_METRICS,
        close.DAILY_AVERAGE_METRICS,
    )

    assert result["sessions"] == 2
    assert result["closeBelowOpenPct"] == 50.0
    assert result["closeBelowRangeMidpointPct"] == 50.0
    assert result["avgCloseLocationPct"] == 50.0
    assert result["avgOpenToClosePct"] == 0.0
    assert result["avgHighToClosePct"] == pytest.approx(-9.091, abs=0.001)


def test_flat_daily_range_returns_null_location_not_nan():
    observations = close.daily_observations(
        [{"date": "2026-07-01", "open": 100, "high": 100, "low": 100, "close": 100}],
        dt.date(2026, 6, 1),
        dt.date(2026, 7, 9),
    )
    result = close.aggregate_weighted(
        {"AAA": observations},
        {"AAA": 1.0},
        close.DAILY_BOOLEAN_METRICS,
        close.DAILY_AVERAGE_METRICS,
    )

    assert result["avgCloseLocationPct"] is None
    json.dumps(result, allow_nan=False)


def test_intraday_metrics_use_typical_price_vwap_middle_bar_and_closing_bucket():
    closes = [100, 102, 104, 106, 108, 107, 105]
    rows = []
    for index, price in enumerate(closes):
        timestamp = pd.Timestamp("2026-07-08 09:30", tz="America/New_York") + pd.Timedelta(
            hours=index
        )
        if index == len(closes) - 1:
            rows.append(
                {
                    "timestamp": timestamp.isoformat(),
                    "open": 107,
                    "high": 107,
                    "low": 105,
                    "close": 105,
                    "volume": 1,
                }
            )
        else:
            rows.append(
                {
                    "timestamp": timestamp.isoformat(),
                    "open": price,
                    "high": price,
                    "low": price,
                    "close": price,
                    "volume": 1,
                }
            )

    observations = close.intraday_observations(
        rows, dt.date(2026, 6, 1), dt.date(2026, 7, 9)
    )
    assert len(observations) == 1
    observation = observations[0]
    assert observation["closeBelowVwap"] is False
    assert observation["closeBelowMidSession"] is True
    assert observation["closingBarRed"] is True
    assert observation["closingBarReturnPct"] == pytest.approx((105 / 107 - 1) * 100)
    assert observation["peakBefore14"] is True

    summary = close.aggregate_weighted(
        {"AAA": observations},
        {"AAA": 1.0},
        close.INTRADAY_BOOLEAN_METRICS,
        close.INTRADAY_AVERAGE_METRICS,
    )
    assert summary["closeBelowVwapPct"] == 0.0
    assert summary["compositeCloseBelowVwapPct"] == 0.0
    assert summary["closeBelowMidSessionPct"] == 100.0
    assert summary["closingBarRedPct"] == 100.0
    assert "lastHourRedPct" not in summary
    assert "avgLastHourReturnPct" not in summary


def test_weighted_stock_day_share_is_distinct_from_portfolio_composite_day():
    observations = {
        "AAA": [
            {"date": "2026-07-01", "closeBelowVwap": True, "closeVsVwapPct": -0.1},
            {"date": "2026-07-02", "closeBelowVwap": True, "closeVsVwapPct": -0.1},
        ],
        "BBB": [
            # The smaller name's strong positive return makes the day-level
            # composite positive even though 80% of current weight is below VWAP.
            {"date": "2026-07-01", "closeBelowVwap": False, "closeVsVwapPct": 5.0},
        ],
    }

    result = close.aggregate_weighted(
        observations,
        {"AAA": 0.8, "BBB": 0.2},
        {"closeBelowVwap": "closeBelowVwapPct"},
        {"closeVsVwapPct": "avgCloseVsVwapPct"},
    )

    # Day 1 weighted share=.8; day 2 (BBB missing) re-normalizes AAA to 1.
    assert result["closeBelowVwapPct"] == 90.0
    # Day 1 composite is +0.92%; day 2 is -0.1%, so one of two sessions <0.
    assert result["compositeCloseBelowVwapPct"] == 50.0
    assert result["averageCoveragePct"] == 90.0
    assert result["metricSessions"]["compositeCloseBelowVwapPct"] == 2


def _dashboard(path: Path) -> None:
    payload = {
        "summary": {
            "priceAsOf": "2026-07-09",
            "generatedAt": "2026-07-09T16:10:00-07:00",
        },
        "stocks": [
            {"sym": "AAA", "held": True, "value": 750},
            {"sym": "BBB", "held": True, "value": 250},
            {"sym": "OLD", "held": False, "value": 0},
        ],
    }
    # The thesis deliberately contains `};` to prove JSONDecoder does not stop
    # where a regex-based payload reader might.
    payload["thesis"] = "literal }; text is valid"
    path.write_text(
        "<html><script>const DATA = " + json.dumps(payload) + ";</script></html>",
        encoding="utf-8",
    )


def _frames(symbols, interval):
    if interval == "1d":
        index = pd.to_datetime(["2026-04-10", "2026-06-10", "2026-07-01", "2026-07-09"])
        output = {}
        for offset, symbol in enumerate(symbols):
            base = 100 + offset * 10
            output[symbol] = pd.DataFrame(
                {
                    "Open": [base, base, base, base],
                    "High": [base + 4, base + 4, base + 4, base + 4],
                    "Low": [base - 4, base - 4, base - 4, base - 4],
                    "Close": [base + 1, base - 1, base + 2, base - 2],
                    "Volume": [1000, 1000, 1000, 1000],
                },
                index=index,
            )
        return output
    if interval == "60m":
        index = pd.date_range(
            "2026-07-09 09:30", periods=7, freq="60min", tz="America/New_York"
        )
        output = {}
        for offset, symbol in enumerate(symbols):
            base = 100 + offset * 10
            output[symbol] = pd.DataFrame(
                {
                    "Open": [base, base + 1, base + 2, base + 3, base + 4, base + 3, base + 2],
                    "High": [base + 1, base + 2, base + 3, base + 4, base + 5, base + 4, base + 3],
                    "Low": [base - 1, base, base + 1, base + 2, base + 3, base + 2, base],
                    "Close": [base + 1, base + 2, base + 3, base + 4, base + 4, base + 3, base + 1],
                    "Volume": [100, 100, 100, 100, 100, 100, 100],
                },
                index=index,
            )
        return output
    raise AssertionError(interval)


def test_live_run_writes_raw_cache_and_report_then_no_fetch_recomputes(tmp_path):
    dashboard = tmp_path / "dashboard.html"
    output = tmp_path / "report.json"
    cache = tmp_path / "raw-cache.json"
    _dashboard(dashboard)
    calls = []

    def downloader(symbols, *, start, end, interval):
        calls.append((tuple(symbols), start, end, interval))
        return _frames(symbols, interval)

    now = dt.datetime(2026, 7, 10, 1, 0, tzinfo=dt.timezone.utc)
    report = close.run(
        dashboard,
        out=output,
        cache=cache,
        downloader=downloader,
        now=now,
    )

    assert {call[3] for call in calls} == {"1d", "60m"}
    assert output.exists() and cache.exists()
    assert report["fetchMode"] == "live"
    assert report["universe"]["heldSymbols"] == ["AAA", "BBB"]
    assert report["universe"]["currentWeightsPct"] == {"AAA": 75.0, "BBB": 25.0}
    assert report["benchmark"]["symbol"] == "QQQ"
    assert set(report["windows"]) == {"1M", "2M", "3M"}
    ui = report["windows"]["1M"]
    assert set(ui["symbols"]) == {"AAA", "BBB"}
    assert "closingBarRedPct" in ui["portfolio"]
    assert "lastHourRedPct" not in ui["portfolio"]
    assert "compositeCloseBelowVwapPct" in ui["portfolio"]
    assert ui["portfolio"]["verdict"] in {
        "SUPPORTS_LOWER_CLOSE",
        "REJECTS_LOWER_CLOSE",
        "MIXED",
        "DATA_MISSING",
        "DATA_INSUFFICIENT",
    }
    assert ui["portfolio"]["dataQualityStatus"] == "BLOCK"
    assert ui["portfolio"]["minimumRequiredSessions"] == 15
    json.dumps(report, allow_nan=False)
    json.dumps(json.loads(output.read_text(encoding="utf-8")), allow_nan=False)
    raw_cache = json.loads(cache.read_text(encoding="utf-8"))
    assert raw_cache["schemaVersion"] == 1
    assert raw_cache["daily"]["AAA"]
    assert raw_cache["intraday60m"]["QQQ"]

    cached_output = tmp_path / "cached-report.json"

    def forbidden(*args, **kwargs):
        raise AssertionError("network downloader must not run in --no-fetch mode")

    cached = close.run(
        dashboard,
        out=cached_output,
        cache=cache,
        no_fetch=True,
        downloader=forbidden,
        now=now,
    )
    assert cached["fetchMode"] == "cache-only"
    assert cached["windows"] == report["windows"]
    assert cached_output.exists()


def test_no_fetch_requires_raw_cache(tmp_path):
    dashboard = tmp_path / "dashboard.html"
    _dashboard(dashboard)
    with pytest.raises(FileNotFoundError, match="raw-bar cache"):
        close.run(
            dashboard,
            out=tmp_path / "report.json",
            cache=tmp_path / "missing-cache.json",
            no_fetch=True,
        )


def test_atomic_json_sanitizes_non_finite_numbers_and_leaves_no_tmp(tmp_path):
    target = tmp_path / "strict.json"
    close.atomic_json(target, {"nan": math.nan, "inf": math.inf, "ok": 1.25})
    text = target.read_text(encoding="utf-8")
    assert "NaN" not in text and "Infinity" not in text
    assert json.loads(text) == {"nan": None, "inf": None, "ok": 1.25}
    assert not list(tmp_path.glob(".*.tmp.*"))


def test_multiindex_yfinance_shape_is_normalized_for_each_symbol():
    index = pd.to_datetime(["2026-07-08", "2026-07-09"])
    columns = pd.MultiIndex.from_product(
        [["AAA", "QQQ"], ["Open", "High", "Low", "Close", "Volume"]]
    )
    data = []
    for row_number in range(2):
        row = []
        for symbol_offset in (0, 10):
            base = 100 + symbol_offset + row_number
            row.extend([base, base + 2, base - 2, base + 1, 1000])
        data.append(row)
    frame = pd.DataFrame(data, index=index, columns=columns)

    normalized = close._normalize_daily(frame, ["AAA", "QQQ"])

    assert set(normalized) == {"AAA", "QQQ"}
    assert normalized["AAA"][-1]["close"] == 102.0
    assert normalized["QQQ"][-1]["close"] == 112.0


def test_directional_verdict_requires_sample_coverage_and_current_data():
    observations = [
        {"date": f"2026-07-{day:02d}", "closeBelowVwap": True, "closeVsVwapPct": -0.2}
        for day in range(1, 16)
    ]
    window = {
        "daily": close.aggregate_weighted(
            {"AAA": observations}, {"AAA": 1.0}, {}, {}
        ),
        "intraday60m": close.aggregate_weighted(
            {"AAA": observations}, {"AAA": 1.0},
            {"closeBelowVwap": "closeBelowVwapPct"},
            {"closeVsVwapPct": "avgCloseVsVwapPct"},
        ),
    }

    result = close._ui_metrics(
        window, min_sessions=15, data_cutoff=dt.date(2026, 7, 16))

    assert result["dataQualityStatus"] == "PASS"
    assert result["verdict"] == "SUPPORTS_LOWER_CLOSE"


def test_build_report_excludes_a_still_forming_current_session():
    payload = {
        "summary": {"priceAsOf": "2026-07-09"},
        "stocks": [{"sym": "AAA", "held": True, "value": 100}],
    }
    bars = {
        "daily": {"AAA": [
            {"date": "2026-07-08", "open": 100, "high": 102, "low": 99, "close": 101},
            {"date": "2026-07-09", "open": 101, "high": 103, "low": 100, "close": 102},
        ]},
        "intraday60m": {"AAA": []},
    }

    report = close.build_report(
        payload, bars, dt.date(2026, 7, 9), fetch_mode="cache-only",
        generated_at=dt.datetime(2026, 7, 9, 18, 0, tzinfo=dt.timezone.utc),
    )

    assert report["formingSessionExcluded"] is True
    assert report["effectiveDataCutoff"] == "2026-07-08"
    assert report["windows"]["1M"]["endDate"] == "2026-07-08"


def test_dashboard_context_rejects_missing_or_nonpositive_held_values():
    with pytest.raises(ValueError, match="must be positive"):
        close.dashboard_context({"stocks": [{"sym": "AAA", "held": True, "value": None}]})

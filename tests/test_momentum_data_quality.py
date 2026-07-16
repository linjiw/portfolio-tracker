from __future__ import annotations

import numpy as np
import pandas as pd

from scripts import momentum_data_quality as quality


def provider_frame(rows: list[dict[str, object]]) -> pd.DataFrame:
    return quality.normalize_provider_long(pd.DataFrame(rows))


def minimal_registry(*events: dict[str, object]) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "decisionGradeGate": "BLOCK_DECISION_GRADE",
        "decisionGradeReasons": ["PIT membership incomplete"],
        "knownTickerReuse": [],
        "securities": {},
        "events": list(events),
    }


def test_official_registry_locks_required_identities_and_events() -> None:
    registry = quality.load_event_registry()

    assert registry["decisionGradeGate"] == "BLOCK_DECISION_GRADE"
    assert registry["securities"]["SNDK"]["cik"] == "2023554"
    assert registry["securities"]["SNDK"]["regularWayStart"] == "2025-02-24"
    assert registry["securities"]["WDC"]["cik"] == "106040"
    assert registry["securities"]["DHR"]["cik"] == "313616"
    assert registry["securities"]["FTV"]["cik"] == "1659166"
    assert {event["id"] for event in registry["events"]} >= {
        "WDC_SNDK_2025_SPIN",
        "DHR_FTV_2016_SPIN",
    }
    assert any(
        row["ticker"] == "SNDK"
        and row["oldCik"] == "1000180"
        and row["newCik"] == "2023554"
        for row in registry["knownTickerReuse"]
    )


def test_fractional_split_classifier_distinguishes_plain_splits() -> None:
    assert quality.non_integer_split(1.323)
    assert quality.non_integer_split(1.319)
    assert quality.non_integer_split(1.5)
    assert not quality.non_integer_split(2.0)
    assert not quality.non_integer_split(0.5)


def test_dhr_provider_action_is_flagged_but_mapped_to_official_event() -> None:
    registry = quality.load_event_registry()
    provider = provider_frame([
        {
            "date": "2016-07-01", "ticker": "DHR", "raw_close": 68.77,
            "adj_close": 42.01, "dividends": 0.0, "stock_splits": 0.0,
        },
        {
            "date": "2016-07-05", "ticker": "DHR", "raw_close": 71.28,
            "adj_close": 67.73, "dividends": 24.56, "stock_splits": 1.319,
        },
    ])

    flags, actions = quality.action_flags("DHR", provider, registry)

    assert {row["code"] for row in flags} == {
        "FRACTIONAL_SPLIT_FACTOR",
        "LARGE_DISTRIBUTION",
    }
    assert all(row["severity"] == "WATCH" for row in flags)
    assert all(row["eventId"] == "DHR_FTV_2016_SPIN" for row in flags)
    assert actions[-1]["stockSplit"] == 1.319
    assert actions[-1]["dividend"] == 24.56


def test_unmapped_fractional_action_blocks() -> None:
    provider = provider_frame([
        {
            "date": "2024-01-02", "ticker": "XYZ", "raw_close": 100.0,
            "adj_close": 100.0, "dividends": 0.0, "stock_splits": 0.0,
        },
        {
            "date": "2024-01-03", "ticker": "XYZ", "raw_close": 70.0,
            "adj_close": 70.0, "dividends": 20.0, "stock_splits": 1.271,
        },
    ])

    flags, _ = quality.action_flags("XYZ", provider, minimal_registry())

    assert {row["code"] for row in flags} == {
        "FRACTIONAL_SPLIT_FACTOR",
        "LARGE_DISTRIBUTION",
    }
    assert all(row["severity"] == "BLOCK" for row in flags)


def test_unexplained_jump_is_blocked_but_action_date_is_not() -> None:
    calendar = pd.bdate_range("2024-01-02", periods=6)
    unexplained = pd.Series([100, 101, 102, 190, 191, 192], index=calendar, dtype=float)
    empty_provider = quality.normalize_provider_long(pd.DataFrame())

    flags, _ = quality.price_flags(
        "XYZ", unexplained, calendar, empty_provider, minimal_registry()
    )
    jump = next(row for row in flags if row["code"] == "UNEXPLAINED_JUMP")
    assert jump["severity"] == "BLOCK"
    assert jump["date"] == str(calendar[3].date())

    event = {
        "id": "XYZ_ACTION",
        "type": "SPINOFF_COMPLEX",
        "parentTicker": "XYZ",
        "childTicker": "NEW",
        "distributionDate": str(calendar[3].date()),
        "regularWayDate": str(calendar[3].date()),
    }
    flags, _ = quality.price_flags(
        "XYZ", unexplained, calendar, empty_provider, minimal_registry(event)
    )
    assert not any(row["code"] == "UNEXPLAINED_JUMP" for row in flags)


def test_flat_run_and_internal_gap_are_reported() -> None:
    calendar = pd.bdate_range("2024-01-02", periods=10)
    series = pd.Series(100.0, index=calendar, dtype=float)
    series = series.drop(calendar[4])

    flags, diagnostics = quality.price_flags(
        "XYZ",
        series,
        calendar,
        quality.normalize_provider_long(pd.DataFrame()),
        minimal_registry(),
    )

    assert {row["code"] for row in flags} >= {"FLAT_RUN", "INTERNAL_GAP"}
    assert diagnostics["internalGaps"]["count"] == 1
    assert diagnostics["internalGaps"]["max_run"] == 1
    assert diagnostics["longestFlatRun"]["sessions"] == 9


def test_provider_adjusted_mismatch_is_detected() -> None:
    calendar = pd.bdate_range("2024-01-02", periods=5)
    cache = pd.Series([100, 101, 102, 103, 104], index=calendar, dtype=float)
    provider = provider_frame([
        {
            "date": day, "ticker": "XYZ", "raw_close": value,
            "adj_close": value if i < 4 else 80.0,
            "dividends": 0.0, "stock_splits": 0.0,
        }
        for i, (day, value) in enumerate(cache.items())
    ])

    flags, diagnostics = quality.price_flags(
        "XYZ", cache, calendar, provider, minimal_registry()
    )

    mismatch = next(row for row in flags if row["code"] == "PROVIDER_ADJUSTED_MISMATCH")
    assert mismatch["severity"] == "BLOCK"
    assert diagnostics["providerAlignment"]["observations"] == 5


def test_wdc_historical_watch_does_not_poison_post_event_signal() -> None:
    security = {
        "cik": "106040",
        "identityStatus": "VERIFIED",
        "currentMembershipIndex": "NDX",
        "currentMembershipStart": "2025-12-22",
    }
    flags = [{
        "code": "FRACTIONAL_SPLIT_FACTOR",
        "severity": "WATCH",
        "date": "2025-02-24",
        "detail": "mapped complex action",
    }]
    signal_date = pd.Timestamp("2026-06-30")

    post_event = quality.signal_eligibility(
        "WDC", 7.96, signal_date, pd.Timestamp("2025-07-30"),
        pd.Timestamp("2026-07-10"), security, flags, True,
    )
    crossing_event = quality.signal_eligibility(
        "WDC", 7.96, signal_date, pd.Timestamp("2025-01-30"),
        pd.Timestamp("2026-07-10"), security, flags, True,
    )

    assert post_event["gate"] == "PASS"
    assert crossing_event["gate"] == "WATCH"


def test_identity_and_regular_way_dates_are_hard_signal_gates() -> None:
    signal_date = pd.Timestamp("2026-06-30")
    unknown = quality.signal_eligibility(
        "XYZ", 1.0, signal_date, pd.Timestamp("2025-07-30"),
        pd.Timestamp("2026-07-10"), {}, [], True,
    )
    premature = quality.signal_eligibility(
        "SNDK", 1.0, pd.Timestamp("2025-02-20"), pd.Timestamp("2024-03-20"),
        pd.Timestamp("2025-02-20"),
        {
            "cik": "2023554",
            "identityStatus": "VERIFIED",
            "regularWayStart": "2025-02-24",
            "currentMembershipStart": "2026-04-20",
        },
        [],
        True,
    )

    assert unknown["gate"] == "BLOCK"
    assert premature["gate"] == "BLOCK"
    assert any("regular-way" in reason for reason in premature["reasons"])


def test_provider_download_multiindex_normalizes_to_long() -> None:
    dates = pd.bdate_range("2024-01-02", periods=2)
    columns = pd.MultiIndex.from_product([
        ["Close", "Adj Close", "Dividends", "Stock Splits"],
        ["AAA", "BBB"],
    ])
    data = pd.DataFrame(index=dates, columns=columns, dtype=float)
    data[("Close", "AAA")] = [10, 11]
    data[("Adj Close", "AAA")] = [9, 10]
    data[("Dividends", "AAA")] = [0, 1]
    data[("Stock Splits", "AAA")] = [0, 0]
    data[("Close", "BBB")] = [20, 21]
    data[("Adj Close", "BBB")] = [19, 20]
    data[("Dividends", "BBB")] = [0, 0]
    data[("Stock Splits", "BBB")] = [0, 2]

    long = quality.provider_download_to_long(data, ["AAA", "BBB"])

    assert long.index.names == ["date", "ticker"]
    assert long.at[(dates[1], "AAA"), "dividends"] == 1
    assert long.at[(dates[1], "BBB"), "stock_splits"] == 2


def test_momentum_scores_use_last_completed_signal_and_calendar_anchor() -> None:
    dates = pd.bdate_range("2023-01-02", "2024-06-14")
    prices = pd.DataFrame(index=dates)
    prices["SPY"] = np.linspace(100, 120, len(dates))
    prices["QQQ"] = np.linspace(100, 130, len(dates))
    prices["AAA"] = np.linspace(100, 200, len(dates))
    prices["BBB"] = np.linspace(100, 150, len(dates))
    signal = quality.latest_completed_month_signal_date(prices)

    scores = quality.momentum_scores(prices, signal, 5)

    assert signal.to_period("M") == pd.Period("2024-05", "M")
    assert list(scores.index) == ["AAA", "BBB"]
    assert scores["AAA"] > scores["BBB"] > 0

"""Tests for scripts/momentum_top3.py — signal math, backtest hygiene, payload schema."""
import json
import sys
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import momentum_top3 as mt


@pytest.fixture
def toy_px():
    """3 years of daily prices: A trends up hard, B flat, C trends down."""
    idx = pd.bdate_range("2022-01-03", "2025-01-03")
    n = len(idx)
    rng = np.random.default_rng(7)
    a = 100 * np.exp(np.linspace(0, 2.0, n) + rng.normal(0, 0.005, n))
    b = 100 * np.exp(rng.normal(0, 0.003, n).cumsum())
    c = 100 * np.exp(np.linspace(0, -0.8, n) + rng.normal(0, 0.005, n))
    spy = 100 * np.exp(np.linspace(0, 0.5, n))
    return pd.DataFrame({"AAA": a, "BBB": b, "CCC": c, "SPY": spy}, index=idx)


@pytest.fixture
def payload_px(toy_px):
    """Payload/cache fixture with both benchmarks and five signal candidates."""
    return toy_px.assign(
        QQQ=toy_px["BBB"],
        DDD=(toy_px["AAA"] + toy_px["BBB"]) / 2,
        EEE=(toy_px["BBB"] + toy_px["CCC"]) / 2,
        FFF=(toy_px["AAA"] + toy_px["CCC"]) / 2,
    )


def small_cache_config():
    cfg = json.loads((ROOT / "data" / "momentum_config.json").read_text())
    return {
        **cfg,
        "backtest": {**cfg["backtest"], "years": 2},
        "universe": {**cfg["universe"], "min_cache_members": 5},
    }


def test_momentum_scores_ranks_trend(toy_px):
    s = mt.momentum_scores(toy_px, 9, exclude=("SPY",))
    assert list(s.sort_values(ascending=False).index) == ["AAA", "BBB", "CCC"]
    assert s["AAA"] > 0 > s["CCC"]


def test_momentum_scores_excludes_benchmarks(toy_px):
    assert "SPY" not in mt.momentum_scores(toy_px, 6, exclude=("SPY",)).index


def test_asof_price_uses_last_close_on_or_before_calendar_anchor():
    index = pd.to_datetime(["2025-01-31", "2025-02-28", "2025-03-03", "2026-03-02"])
    prices = pd.DataFrame({"AAA": [90, 100, 110, 150]}, index=index)

    anchor = mt.asof_price(prices, 12)

    # 2026-03-02 minus 12 months is Sunday 2025-03-02, so the valid anchor is
    # Friday 2025-02-28, not Monday 2025-03-03.
    assert anchor["AAA"] == 100


def test_asof_price_is_per_security_and_rejects_stale_anchor():
    index = pd.to_datetime(["2025-01-29", "2025-01-30", "2025-01-31", "2025-03-02"])
    prices = pd.DataFrame(
        {
            "AAA": [90.0, 100.0, np.nan, 130.0],
            "BBB": [80.0, np.nan, np.nan, 120.0],
            "MKT": [100.0, 100.0, 100.0, 100.0],
        },
        index=index,
    )

    anchor = mt.asof_price(prices, 1, max_staleness_sessions=1)

    # The calendar anchor is 2025-02-02. AAA can use its own Jan-30 close,
    # one market session behind Jan-31; BBB's Jan-29 close is too stale.
    assert anchor["AAA"] == 100.0
    assert pd.isna(anchor["BBB"])


def test_valid_columns_drops_sparse(toy_px):
    px = toy_px.copy()
    px.iloc[-200:, px.columns.get_loc("BBB")] = np.nan
    assert "BBB" not in mt.valid_columns(px)


def test_valid_columns_uses_ceiling_not_floor_for_coverage():
    index = pd.bdate_range("2026-01-02", periods=3)
    prices = pd.DataFrame({"AAA": [100.0, 101.0, np.nan]}, index=index)
    assert "AAA" not in mt.valid_columns(prices, months=13, coverage=0.95)


def test_backtest_no_lookahead_and_costs(toy_px):
    start, end = toy_px.index[0] + pd.DateOffset(months=13), toy_px.index[-1]
    eq0, _ = mt.run_backtest(toy_px, 6, 1, start, end, 0.0, exclude=("SPY",))
    eq1, turn = mt.run_backtest(toy_px, 6, 1, start, end, 0.01, exclude=("SPY",))
    assert float(eq1.iloc[-1]) < float(eq0.iloc[-1])  # costs must bite
    assert 0 <= turn <= 1
    # top-1 on this toy universe should ride AAA and beat flat/down names
    assert float(eq0.iloc[-1]) > 1.5


def test_backtest_fills_at_next_close_and_carries_prior_weights_on_fill_day(monkeypatch):
    idx = pd.to_datetime([
        "2026-01-30", "2026-01-31", "2026-02-03", "2026-02-04",
        "2026-02-28", "2026-03-03", "2026-03-04",
    ])
    px = pd.DataFrame({
        "AAA": [100, 100, 200, 200, 200, 400, 400],
        "BBB": [100, 100, 100, 100, 100, 100, 100],
    }, index=idx, dtype=float)

    def scores(hist, *_args, **_kwargs):
        if hist.index[-1] == pd.Timestamp("2026-01-31"):
            return pd.Series({"AAA": 1.0, "BBB": 0.0})
        return pd.Series({"BBB": 1.0, "AAA": 0.0})

    monkeypatch.setattr(mt, "momentum_scores", scores)
    eq, turnover, audit = mt.run_backtest(
        px, 1, 1, idx[0], idx[-1], 0.0, return_diagnostics=True,
    )

    # Jan-31 signal cannot capture AAA's Jan-31 -> Feb-03 jump: the initial
    # book is cash through the Feb-03 close, then buys AAA at that close.
    assert eq.loc["2026-02-03"] == pytest.approx(1.0)
    # Feb-28 selects BBB, but old AAA remains held through the Mar-03 close and
    # captures that fill-day jump before the switch to BBB.
    assert eq.loc["2026-03-03"] == pytest.approx(2.0)
    assert [row["fillDate"] for row in audit["fills"]] == ["2026-02-03", "2026-03-03"]
    assert turnover == pytest.approx(0.75)  # 0.5 initial entry, 1.0 full rotation


def test_fill_close_turnover_uses_drifted_weights_and_cost_is_charged_after_return(monkeypatch):
    idx = pd.to_datetime([
        "2026-01-30", "2026-01-31", "2026-02-03", "2026-02-04",
        "2026-02-28", "2026-03-03", "2026-03-04",
    ])
    px = pd.DataFrame({
        "AAA": [100, 100, 100, 100, 100, 200, 200],
        "BBB": [100, 100, 100, 100, 100, 100, 100],
    }, index=idx, dtype=float)
    monkeypatch.setattr(
        mt,
        "momentum_scores",
        lambda *_args, **_kwargs: pd.Series({"AAA": 1.0, "BBB": 0.5}),
    )

    eq, turnover, audit = mt.run_backtest(
        px, 1, 2, idx[0], idx[-1], 0.01, return_diagnostics=True,
    )
    first, second = audit["fills"]

    assert eq.iloc[0] == 1.0  # explicit initial-capital anchor
    assert first["grossTraded"] == pytest.approx(1.0)
    assert eq.loc["2026-02-03"] == pytest.approx(0.99)
    # AAA doubles on the second fill day: 50/50 drifts to 2/3 vs 1/3 before
    # turnover is measured. Rebalancing to 50/50 trades 1/3 gross, then costs.
    assert second["preTradeWeights"]["AAA"] == pytest.approx(2 / 3)
    assert second["oneWayTurnover"] == pytest.approx(1 / 6)
    assert second["equityBeforeCost"] == pytest.approx(0.99 * 1.5)
    assert second["costRate"] == pytest.approx((1 / 3) * 0.01)
    assert eq.loc["2026-03-03"] == pytest.approx(0.99 * 1.5 * (1 - (1 / 3) * 0.01))
    assert turnover == pytest.approx((0.5 + 1 / 6) / 2)


def test_initial_anchor_makes_first_fill_cost_visible_in_drawdown(monkeypatch):
    idx = pd.to_datetime(["2026-01-30", "2026-01-31", "2026-02-03", "2026-02-04"])
    px = pd.DataFrame({"AAA": [100, 100, 100, 100]}, index=idx, dtype=float)
    monkeypatch.setattr(mt, "momentum_scores", lambda *_args, **_kwargs: pd.Series({"AAA": 1.0}))

    eq, _ = mt.run_backtest(px, 1, 1, idx[0], idx[-1], 0.10)
    metrics = mt.perf_metrics(eq)

    assert eq.iloc[0] == 1.0
    assert eq.loc["2026-02-03"] == pytest.approx(0.9)
    assert metrics["total_x"] == pytest.approx(0.9)
    assert metrics["max_dd_pct"] == pytest.approx(-10.0)


def test_midmonth_anchor_inherits_the_already_active_book(monkeypatch):
    idx = pd.bdate_range("2024-11-01", "2025-03-10")
    start = pd.Timestamp("2025-01-15")
    aaa = pd.Series(100.0, index=idx)
    aaa.loc[idx > start] = np.linspace(101.0, 125.0, int((idx > start).sum()))
    px = pd.DataFrame({"AAA": aaa, "BBB": 100.0}, index=idx)
    monkeypatch.setattr(
        mt, "momentum_scores", lambda *_args, **_kwargs: pd.Series({"AAA": 1.0, "BBB": 0.0})
    )

    eq, _, audit = mt.run_backtest(
        px, 1, 1, start, idx[-1], 0.0, return_diagnostics=True,
    )

    assert audit["warmStartUsed"] is True
    assert audit["warmStartMonths"] == 2
    assert audit["inheritedWeights"] == {"AAA": pytest.approx(1.0)}
    assert eq.index[0] == start
    assert eq.iloc[0] == pytest.approx(1.0)
    assert eq.iloc[1] > 1.0  # no cash wait until the next month-end


def test_held_price_gap_catches_up_from_last_observed_close(monkeypatch):
    idx = pd.to_datetime([
        "2026-01-30", "2026-01-31", "2026-02-03", "2026-02-04", "2026-02-05",
    ])
    px = pd.DataFrame({
        "AAA": [100, 100, 100, np.nan, 110],
        "BBB": [100, 100, 100, 100, 100],
    }, index=idx, dtype=float)
    monkeypatch.setattr(mt, "momentum_scores", lambda *_args, **_kwargs: pd.Series({"AAA": 1.0}))

    eq, _, audit = mt.run_backtest(
        px, 1, 1, idx[0], idx[-1], 0.0, return_diagnostics=True,
    )

    assert eq.loc["2026-02-04"] == pytest.approx(1.0)
    assert eq.loc["2026-02-05"] == pytest.approx(1.1)
    assert audit["priceGapObservations"] == [{
        "symbol": "AAA",
        "date": "2026-02-04",
        "lastObservedDate": "2026-02-03",
    }]
    assert audit["cumulativeGapReturns"][0]["cumulativeReturnPct"] == pytest.approx(10.0)
    assert audit["unresolvedPriceGaps"] == []


def test_unresolved_held_price_gap_blocks_metrics_without_truncating(monkeypatch):
    idx = pd.to_datetime([
        "2026-01-30", "2026-01-31", "2026-02-03", "2026-02-04", "2026-02-05",
    ])
    px = pd.DataFrame({
        "AAA": [100, 100, 100, np.nan, np.nan],
        "BBB": [100, 100, 100, 100, 100],
    }, index=idx, dtype=float)
    monkeypatch.setattr(mt, "momentum_scores", lambda *_args, **_kwargs: pd.Series({"AAA": 1.0}))

    eq, _, audit = mt.run_backtest(
        px, 1, 1, idx[0], idx[-1], 0.0, return_diagnostics=True,
    )

    metrics = mt.perf_metrics(eq)

    assert eq.index[-1] == pd.Timestamp("2026-02-05")
    assert "evaluationTruncatedBefore" not in audit
    assert audit["unresolvedPriceGaps"][0]["symbol"] == "AAA"
    assert audit["metricStatus"] == "BLOCK"
    assert metrics["status"] == "BLOCK"
    assert metrics["total_x"] is None


def test_missing_fill_retries_on_the_next_session(monkeypatch):
    idx = pd.to_datetime([
        "2026-01-30", "2026-01-31", "2026-02-03", "2026-02-04", "2026-02-05",
    ])
    px = pd.DataFrame({
        "AAA": [100.0, 100.0, np.nan, 110.0, 121.0],
        "BBB": [100.0, 100.0, 100.0, 100.0, 100.0],
    }, index=idx)
    monkeypatch.setattr(mt, "momentum_scores", lambda *_a, **_k: pd.Series({"AAA": 1.0}))

    eq, _, audit = mt.run_backtest(
        px, 1, 1, idx[0], idx[-1], 0.0,
        return_diagnostics=True, max_fill_retry_sessions=2,
    )

    assert audit["skippedFills"][0]["attemptDate"] == "2026-02-03"
    assert audit["skippedFills"][0]["willRetry"] is True
    assert audit["fills"][0]["fillDate"] == "2026-02-04"
    assert audit["fills"][0]["retryCount"] == 1
    assert audit["expiredPendingFills"] == []
    assert eq.loc["2026-02-05"] == pytest.approx(1.1)


def test_independent_trading_start_removes_when_issued_coverage():
    idx = pd.bdate_range("2025-01-02", "2026-02-27")
    prices = pd.DataFrame(
        {
            "SNDK": np.linspace(20.0, 400.0, len(idx)),
            "AAA": np.linspace(100.0, 130.0, len(idx)),
        },
        index=idx,
    )

    unmasked = mt.momentum_scores(prices, 11, coverage=0.95)
    regular_way = mt.momentum_scores(
        prices,
        11,
        coverage=0.95,
        independent_trading_start={"SNDK": "2025-02-24"},
    )

    assert "SNDK" in unmasked
    assert "SNDK" not in regular_way


def test_load_independent_trading_starts_reads_only_regular_way_dates(tmp_path):
    registry = tmp_path / "events.json"
    registry.write_text(
        json.dumps({
            "schemaVersion": 1,
            "securities": {
                "NEW": {"regularWayStart": "2025-02-24"},
                "OLD": {"identityStatus": "VERIFIED"},
            },
        }),
        encoding="utf-8",
    )
    assert mt.load_independent_trading_starts(registry) == {
        "NEW": "2025-02-24",
    }


def test_point_in_time_eligibility_and_action_masks_fail_closed():
    idx = pd.bdate_range("2025-01-02", "2026-02-27")
    prices = pd.DataFrame(
        {
            "AAA": np.linspace(100.0, 180.0, len(idx)),
            "BBB": np.linspace(100.0, 120.0, len(idx)),
        },
        index=idx,
    )
    eligibility = pd.DataFrame(
        {"AAA": [False], "BBB": [True]}, index=[pd.Timestamp("2025-01-01")]
    )
    actions = pd.DataFrame(
        {"AAA": [True], "BBB": [True]}, index=[pd.Timestamp("2025-01-01")]
    )

    scores = mt.momentum_scores(
        prices, 11, coverage=0.95,
        eligibility_mask=eligibility, action_mask=actions,
    )

    assert list(scores.index) == ["BBB"]


def test_held_security_action_mask_failure_blocks_metrics(monkeypatch):
    idx = pd.to_datetime([
        "2026-01-30", "2026-01-31", "2026-02-03", "2026-02-04", "2026-02-05",
    ])
    prices = pd.DataFrame({"AAA": 100.0, "BBB": 100.0}, index=idx)
    monkeypatch.setattr(mt, "momentum_scores", lambda *_a, **_k: pd.Series({"AAA": 1.0}))

    def validated(date, symbol):
        return symbol != "AAA" or date <= pd.Timestamp("2026-02-03")

    eq, _, audit = mt.run_backtest(
        prices, 1, 1, idx[0], idx[-1], 0.0,
        return_diagnostics=True, action_mask=validated,
    )

    assert audit["unresolvedCorporateActions"]
    assert audit["metricStatus"] == "BLOCK"
    assert mt.perf_metrics(eq)["status"] == "BLOCK"


def test_perf_metrics_shape(toy_px):
    eq = toy_px["AAA"] / toy_px["AAA"].iloc[0]
    m = mt.perf_metrics(eq)
    assert set(m) == {
        "total_x", "cagr_pct", "max_dd_pct", "sharpe", "status", "block_reasons"
    }
    assert m["status"] == "PASS"
    assert m["total_x"] > 1 and m["max_dd_pct"] <= 0


def test_config_and_payload_schema(toy_px, payload_px):
    cfg = json.loads((ROOT / "data" / "momentum_config.json").read_text())
    assert cfg["backtest"]["years"] == 10
    assert [s["id"] for s in cfg["strategies"]] == [
        "RET_11M_top3", "RET_9M_top3", "RET_11M_top5"]
    assert [(s["lookback_months"], s["top_n"]) for s in cfg["strategies"] if s["id"] != "RET_9M_top3"] == [
        (11, 3), (11, 5)
    ]
    cfg = {**cfg, "backtest": {**cfg["backtest"], "years": 2}}
    payload = mt.build_payload(cfg, payload_px)
    assert {"generated_at", "window", "strategies", "benchmarks",
            "disclaimer"} <= set(payload)
    assert payload["schemaVersion"] == 2
    assert payload["researchOnly"] is True
    assert payload["decisionGrade"] is False
    assert payload["methodology"]["fillTiming"] == "next trading session close"
    assert "cumulative return" in payload["methodology"]["missingHeldClosePolicy"]
    assert "truncate" in payload["methodology"]["unresolvedHeldClosePolicy"]
    assert payload["methodology"]["currentUniverseSurvivorship"] is True
    assert payload["universe_mode"] == "current_universe_survivor_biased"
    assert payload["methodology"]["inSampleStrategySelection"] is True
    assert payload["price_freshness"]["fresh_count"] == len(payload_px.columns)
    for s in payload["strategies"]:
        assert len(s["current_signal"]["holdings"]) <= s["top_n"]
        assert s["equity_weekly"], "equity curve must be non-empty"
        w = sum(h["weight_pct"] for h in s["current_signal"]["holdings"])
        assert w == pytest.approx(100.0, abs=0.2)
        assert s["execution"]["anchor_date"] == payload["window"]["start"]
        assert s["execution"]["warm_start_months"] == 2
        assert s["metrics"]["status"] == "PASS"


def test_current_signal_excludes_symbol_without_latest_close(payload_px):
    cfg = json.loads((ROOT / "data" / "momentum_config.json").read_text())
    cfg = {**cfg, "backtest": {**cfg["backtest"], "years": 2}}
    px = payload_px.copy()
    px.loc[px.index[-1], "AAA"] = np.nan
    payload = mt.build_payload(cfg, px)
    latest_ranked = {h["ticker"] for s in payload["strategies"]
                     for h in s["latest_rank_snapshot"]["holdings"]}
    assert "AAA" not in latest_ranked
    assert "AAA" in payload["price_freshness"]["stale_symbols"]
    for strategy in payload["strategies"]:
        current = strategy["current_signal"]
        if "AAA" in {row["ticker"] for row in current["holdings"]}:
            assert current["status"] == "BLOCK_PRICE_STALE"
            assert "AAA" in current["stale_target_symbols"]


def test_current_signal_uses_completed_month_end_and_latest_rank_is_non_actionable(payload_px):
    cfg = json.loads((ROOT / "data" / "momentum_config.json").read_text())
    cfg = {**cfg, "backtest": {**cfg["backtest"], "years": 2}}

    payload = mt.build_payload(cfg, payload_px)

    for strategy in payload["strategies"]:
        signal = strategy["current_signal"]
        assert signal["as_of"] < payload["window"]["end"]
        assert signal["effective_from"] > signal["as_of"]
        assert strategy["latest_rank_snapshot"]["actionable"] is False


def test_live_price_refresh_falls_back_to_cached_universe(tmp_path, payload_px):
    cache = tmp_path / "prices.csv.gz"
    payload_px.to_csv(cache, compression="gzip")
    cfg = small_cache_config()
    downloaded = payload_px.copy()
    with (mock.patch.object(mt, "PRICE_CACHE", cache),
          mock.patch.object(mt, "fetch_universe", side_effect=RuntimeError("markup changed")),
          mock.patch("yfinance.download", return_value=pd.concat({"Close": downloaded}, axis=1))):
        got = mt.load_prices(cfg, no_fetch=False)
    assert set(got.columns) == set(payload_px.columns)
    assert got.attrs["universeSource"] == "cached_constituents_live_prices"
    assert "markup changed" in got.attrs["universeWarning"]


def test_partial_download_preserves_last_known_good_cache(tmp_path, payload_px):
    cache = tmp_path / "prices.csv.gz"
    payload_px.to_csv(cache, compression="gzip")
    before = cache.read_bytes()
    cfg = small_cache_config()
    universe = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"]
    partial = payload_px[["SPY"]]
    with (mock.patch.object(mt, "PRICE_CACHE", cache),
          mock.patch.object(mt, "fetch_universe", return_value=universe),
          mock.patch("yfinance.download", return_value=pd.concat({"Close": partial}, axis=1))):
        got = mt.load_prices(cfg, no_fetch=False)
    assert got.attrs["universeSource"] == "price_cache_fallback"
    assert "rejected" in got.attrs["universeWarning"]
    assert cache.read_bytes() == before


def test_price_cache_publish_is_validated_and_atomic(tmp_path, payload_px):
    cache = tmp_path / "prices.csv.gz"
    cfg = small_cache_config()
    published = mt.publish_price_cache(
        payload_px,
        cfg,
        expected_tickers=list(payload_px.columns),
        path=cache,
    )
    assert cache.exists()
    assert set(published.columns) == set(payload_px.columns)
    assert not list(tmp_path.glob(".prices.csv.gz.tmp.*"))
    assert mt.read_price_cache(cache).index[-1] == payload_px.index[-1]


def test_payload_rejects_missing_benchmark(payload_px):
    cfg = small_cache_config()
    with pytest.raises(ValueError, match="missing benchmark QQQ"):
        mt.build_payload(cfg, payload_px.drop(columns=["QQQ"]))


def test_render_html_embeds_data(payload_px):
    cfg = json.loads((ROOT / "data" / "momentum_config.json").read_text())
    cfg = {**cfg, "backtest": {**cfg["backtest"], "years": 2}}
    payload = mt.build_payload(cfg, payload_px)
    html = mt.render_html(payload)
    assert "__DATA__" not in html and "RET_11M_top3" in html
    assert "Decision Grade: NO" in html
    assert "样本外证据" in html


def test_cycle_stats_basic():
    idx = pd.bdate_range("2025-01-02", "2026-07-06")
    eq = pd.Series(np.linspace(1, 3, len(idx)), index=idx)
    eq.iloc[-10:] = eq.iloc[-11] * 0.85          # 尾部 -15% 回撤
    c = mt.cycle_stats(eq)
    assert c["dd_from_peak_pct"] == pytest.approx(-15.0, abs=0.3)
    assert c["phase"] == "回吐中"
    assert set(c) >= {"mtd_pct", "r1m_pct", "r3m_pct", "dd_from_peak_pct",
                      "peak_date", "phase", "monthly_returns"}
    assert len(c["monthly_returns"]) <= 13


def test_live_plan_status():
    idx = pd.bdate_range("2026-06-01", "2026-07-06")
    eq = pd.Series(np.linspace(1.0, 1.1, len(idx)), index=idx)
    plan = {"strategy_id": "X", "started": "2026-07-06", "tranches": [
        {"seq": 1, "target_month_end": "2026-07-31", "fraction": 0.5, "executed": True, "date": "2026-07-06"},
        {"seq": 2, "target_month_end": "2026-08-31", "fraction": 0.5, "executed": False, "date": None}]}
    st = mt.live_plan_status(plan, eq)
    assert st["deployed_pct"] == 50.0
    assert st["next_tranche"]["seq"] == 2
    assert st["since_entry_pct"] is not None   # 净值自 started 起的收益


def test_payload_has_cycle_and_live_plan(payload_px):
    cfg = json.loads((ROOT / "data" / "momentum_config.json").read_text())
    cfg = {**cfg, "backtest": {**cfg["backtest"], "years": 2}}
    payload = mt.build_payload(cfg, payload_px)
    assert "cycle" in payload["strategies"][0]
    assert payload["strategies"][0]["cycle"]["phase"] in ("早期", "成熟", "回吐中", "深回撤")
    for v in payload["benchmarks"].values():
        assert "cycle" in v
    assert payload["live_plan"]["strategy_id"] == "RET_11M_top3"
    assert "deployed_pct" in payload["live_plan"]

def test_cycle_stats_anchor_semantics():
    """钉死口径: MTD 锚上月末收盘; 1m/3m 锚 t0 当日(或之前最后)收盘."""
    idx = pd.bdate_range("2026-01-02", "2026-07-06")
    eq = pd.Series(1.0, index=idx)
    eq.loc["2026-06-30"] = 2.0                     # 上月末收盘 = 2.0
    eq.iloc[-1] = 3.0                              # 最新 = 3.0
    c = mt.cycle_stats(eq)
    assert c["mtd_pct"] == pytest.approx(50.0)     # 3.0/2.0-1, 锚 6/30 而非 7 月首日
    # 1m: t0=2026-06-06(周六)→ 之前最后收盘 6/5=1.0 → 200%
    assert c["r1m_pct"] == pytest.approx(200.0)
    # t0 恰为交易日: 锚当日收盘 (include_t0=True), 不再多吃一天
    idx3 = pd.bdate_range("2026-01-02", "2026-08-06")
    eq3 = pd.Series(1.0, index=idx3)
    eq3.loc["2026-07-06"] = 2.0                    # t0=8/6-1m=7/6(周一,交易日)
    eq3.loc["2026-07-07":] = 4.0
    c3 = mt.cycle_stats(eq3)
    assert c3["r1m_pct"] == pytest.approx(100.0)   # 4/2-1, 锚 7/6 当日收盘 2.0


def test_cycle_stats_degrades_not_crashes():
    assert mt.cycle_stats(pd.Series(dtype=float))["mtd_pct"] is None
    one = pd.Series([1.0], index=pd.DatetimeIndex(["2026-07-01"]))
    assert mt.cycle_stats(one)["phase"] == "早期"
    idx = pd.bdate_range("2026-06-01", "2026-07-06")
    z = pd.Series(0.0, index=idx); z.iloc[-1] = 1.0
    assert mt.cycle_stats(z)["r1m_pct"] is None    # base=0 → None 而非除零


def test_cycle_stats_partial_month_marked():
    idx = pd.bdate_range("2025-06-02", "2026-07-06")
    eq = pd.Series(np.linspace(1, 2, len(idx)), index=idx)
    mr = mt.cycle_stats(eq)["monthly_returns"]
    assert mr[-1][0] == "2026-07*"                 # 进行中月份带 * 标记
    assert all(not m[0].endswith("*") for m in mr[:-1])


def test_live_plan_tz_aware_index():
    idx = pd.bdate_range("2026-06-01", "2026-07-06", tz="America/New_York")
    eq = pd.Series(np.linspace(1.0, 1.2, len(idx)), index=idx)
    plan = {"strategy_id": "X", "started": "2026-07-06", "tranches": [
        {"seq": 1, "target_month_end": "2026-07-31", "fraction": 1.0,
         "executed": True, "date": "2026-07-06"}]}
    st = mt.live_plan_status(plan, eq)             # 不应 TypeError
    assert st["deployed_pct"] == 100.0


def test_render_html_escapes_script_close():
    payload = {"generated_at": "x", "window": {"start": "a", "end": "b"},
               "universe_size": 1, "cost_per_side": 0.0005,
               "strategies": [], "benchmarks": {},
               "disclaimer": "评</script><b>注入", "note": ""}
    html = mt.render_html(payload)
    assert "评</script>" not in html
    assert "\\u003c/script\\u003e" in html
    assert "function esc(v)" in html

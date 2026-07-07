"""Tests for scripts/momentum_top3.py — signal math, backtest hygiene, payload schema."""
import json
import sys
from pathlib import Path

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


def test_momentum_scores_ranks_trend(toy_px):
    s = mt.momentum_scores(toy_px, 9, exclude=("SPY",))
    assert list(s.sort_values(ascending=False).index) == ["AAA", "BBB", "CCC"]
    assert s["AAA"] > 0 > s["CCC"]


def test_momentum_scores_excludes_benchmarks(toy_px):
    assert "SPY" not in mt.momentum_scores(toy_px, 6, exclude=("SPY",)).index


def test_valid_columns_drops_sparse(toy_px):
    px = toy_px.copy()
    px.iloc[-200:, px.columns.get_loc("BBB")] = np.nan
    assert "BBB" not in mt.valid_columns(px)


def test_backtest_no_lookahead_and_costs(toy_px):
    start, end = toy_px.index[0] + pd.DateOffset(months=13), toy_px.index[-1]
    eq0, _ = mt.run_backtest(toy_px, 6, 1, start, end, 0.0, exclude=("SPY",))
    eq1, turn = mt.run_backtest(toy_px, 6, 1, start, end, 0.01, exclude=("SPY",))
    assert float(eq1.iloc[-1]) < float(eq0.iloc[-1])  # costs must bite
    assert 0 <= turn <= 1
    # top-1 on this toy universe should ride AAA and beat flat/down names
    assert float(eq0.iloc[-1]) > 1.5


def test_perf_metrics_shape(toy_px):
    eq = toy_px["AAA"] / toy_px["AAA"].iloc[0]
    m = mt.perf_metrics(eq)
    assert set(m) == {"total_x", "cagr_pct", "max_dd_pct", "sharpe"}
    assert m["total_x"] > 1 and m["max_dd_pct"] <= 0


def test_config_and_payload_schema(toy_px):
    cfg = json.loads((ROOT / "data" / "momentum_config.json").read_text())
    assert [s["id"] for s in cfg["strategies"]] == [
        "RET_11M_top3", "RET_9M_top3", "RET_5M_top5"]
    cfg = {**cfg, "backtest": {**cfg["backtest"], "years": 2}}
    payload = mt.build_payload(cfg, toy_px.rename(columns={"BBB": "QQQ"}))
    assert {"generated_at", "window", "strategies", "benchmarks",
            "disclaimer"} <= set(payload)
    for s in payload["strategies"]:
        assert len(s["current_signal"]["holdings"]) <= s["top_n"]
        assert s["equity_weekly"], "equity curve must be non-empty"
        w = sum(h["weight_pct"] for h in s["current_signal"]["holdings"])
        assert w <= 100.1


def test_render_html_embeds_data(toy_px):
    cfg = json.loads((ROOT / "data" / "momentum_config.json").read_text())
    cfg = {**cfg, "backtest": {**cfg["backtest"], "years": 2}}
    payload = mt.build_payload(cfg, toy_px.rename(columns={"BBB": "QQQ"}))
    html = mt.render_html(payload)
    assert "__DATA__" not in html and "RET_11M_top3" in html
    assert "幸存者偏差" in html


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


def test_payload_has_cycle_and_live_plan(toy_px):
    cfg = json.loads((ROOT / "data" / "momentum_config.json").read_text())
    cfg = {**cfg, "backtest": {**cfg["backtest"], "years": 2}}
    payload = mt.build_payload(cfg, toy_px.rename(columns={"BBB": "QQQ"}))
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
    assert "评</script>" not in html and "<\\/script>" in html

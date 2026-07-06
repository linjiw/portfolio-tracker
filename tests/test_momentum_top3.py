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

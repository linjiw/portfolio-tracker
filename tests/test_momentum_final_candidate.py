from __future__ import annotations

import copy

import numpy as np
import pandas as pd

from scripts import momentum_final_candidate as final


def metric(cagr: float, drawdown: float, sharpe: float) -> dict[str, float]:
    return {
        "cagr": cagr,
        "max_drawdown": drawdown,
        "sharpe": sharpe,
    }


def test_grid_is_exact_and_focus_neighbors_are_local() -> None:
    grid = final.candidate_grid()
    assert len(grid) == 27
    assert len({row["id"] for row in grid}) == 27
    assert {row["months"] for row in grid} == {6, 10, 12}
    assert {row["risk_on"] for row in grid} == {1.0, 1.15, 1.25}
    assert {row["risk_off"] for row in grid} == {0.0, 0.25, 0.5}
    assert final.FOCUS_ID in {row["id"] for row in grid}
    assert final.focus_neighbor_ids() == [
        "m10_on125_off50",
        "m6_on125_off50",
        "m12_on125_off50",
        "m10_on115_off50",
        "m10_on125_off25",
    ]


def test_monthly_target_uses_only_completed_months() -> None:
    index = pd.bdate_range("2020-01-02", "2021-03-10")
    price = pd.Series(np.linspace(100.0, 180.0, len(index)), index=index)
    spec = {
        "id": "m6_on125_off50",
        "months": 6,
        "risk_on": 1.25,
        "risk_off": 0.5,
        "max_exposure": 1.25,
    }
    target, state, rebalance = final.monthly_asymmetric_target(price, spec)

    assert not bool(rebalance.iloc[-1])
    assert all(
        day.to_period("M") < index[-1].to_period("M")
        for day in rebalance.index[rebalance]
    )
    assert target.iloc[0] == 0.5
    assert bool(state.loc["2020-12-31"])
    assert target.loc["2020-12-31"] == 1.25


def test_close_signal_fills_next_close_then_weight_drifts() -> None:
    index = pd.bdate_range("2024-01-02", periods=4)
    price = pd.Series([100.0, 100.0, 110.0, 121.0], index=index)
    cash = pd.Series(0.0, index=index)
    target = pd.Series([0.0, 0.5, 0.5, 0.5], index=index)
    rebalance = pd.Series([False, True, False, False], index=index)
    spec = {"id": "toy", "risk_off": 0.0, "max_exposure": 0.5}

    path = final.simulate(
        price,
        cash,
        target,
        rebalance,
        spec,
        start=index[1],
        end=index[-1],
        cost_bps=0.0,
        funding_spread=0.03,
    )

    # The signal at index[1] cannot earn index[1] -> index[2] risky return;
    # it fills only at index[2]'s close.
    assert path.exposure.at[index[1]] == 0.0
    assert path.exposure.at[index[2]] == 0.5
    assert path.equity.at[index[2]] == 1.0
    assert path.equity.at[index[3]] == 1.05
    assert path.exposure.at[index[3]] > 0.5


def test_cost_and_funding_stresses_reduce_levered_path() -> None:
    index = pd.bdate_range("2020-01-02", periods=160)
    price = pd.Series(100.0 * np.power(1.001, np.arange(len(index))), index=index)
    cash = pd.Series(0.00005, index=index)
    target = pd.Series(1.25, index=index)
    rebalance = pd.Series(False, index=index)
    rebalance.iloc[::21] = True
    spec = {"id": "levered", "risk_off": 0.5, "max_exposure": 1.25}

    base = final.simulate(
        price, cash, target, rebalance, spec,
        start=index[1], end=index[-1], cost_bps=10.0, funding_spread=0.03,
    )
    cost_stress = final.simulate(
        price, cash, target, rebalance, spec,
        start=index[1], end=index[-1], cost_bps=20.0, funding_spread=0.03,
    )
    funding_stress = final.simulate(
        price, cash, target, rebalance, spec,
        start=index[1], end=index[-1], cost_bps=10.0, funding_spread=0.06,
    )

    assert cost_stress.equity.iloc[-1] < base.equity.iloc[-1]
    assert funding_stress.equity.iloc[-1] < base.equity.iloc[-1]


def test_development_ranking_ignores_replay_values() -> None:
    specs = {
        row["id"]: row for row in [final.baseline_spec(), *final.candidate_grid()]
    }
    book: dict[str, dict[str, dict[str, dict[str, float]]]] = {}
    for candidate_id in specs:
        book[candidate_id] = {}
        for asset in final.RISK_ASSETS:
            if candidate_id == final.BASELINE_ID:
                train = validation = metric(0.10, -0.20, 1.0)
            elif candidate_id == final.FOCUS_ID:
                train = validation = metric(0.12, -0.15, 1.2)
            else:
                train = validation = metric(0.08, -0.25, 0.8)
            book[candidate_id][asset] = {
                "train": dict(train),
                "validation": dict(validation),
                "replay": metric(0.0, -0.99, -9.0),
            }

    first = final.rank_development(book, specs)
    changed = copy.deepcopy(book)
    for asset in final.RISK_ASSETS:
        changed[final.FOCUS_ID][asset]["replay"] = metric(-0.99, -0.999, -99.0)
    second = final.rank_development(changed, specs)

    assert final.development_selection(first) == final.FOCUS_ID
    assert final.development_selection(second) == final.FOCUS_ID
    assert [(row["id"], row["score"]) for row in first] == [
        (row["id"], row["score"]) for row in second
    ]


def test_three_segment_diagnostics_keeps_folds_separate() -> None:
    book: dict[str, dict[str, dict[str, dict[str, float]]]] = {
        final.BASELINE_ID: {},
        final.FOCUS_ID: {},
    }
    for asset in final.RISK_ASSETS:
        book[final.BASELINE_ID][asset] = {
            "train": metric(0.10, -0.20, 1.0),
            "validation": metric(0.11, -0.21, 1.0),
            "replay": metric(0.12, -0.22, 1.0),
        }
        book[final.FOCUS_ID][asset] = {
            "train": metric(0.13, -0.17, 1.1),
            "validation": metric(0.14, -0.18, 1.1),
            "replay": metric(0.15, -0.19, 1.1),
        }

    rows = final.build_three_segment_diagnostics(book)

    assert tuple(rows) == final.RISK_ASSETS
    assert tuple(rows["SPY"]) == ("train", "validation", "replay")
    assert rows["SPY"]["train"]["candidate"]["cagr"] == 0.13
    assert rows["SPY"]["validation"]["candidate"]["cagr"] == 0.14
    assert rows["SPY"]["replay"]["candidate"]["cagr"] == 0.15


def test_annual_schedule_has_five_year_train_and_21_session_embargo() -> None:
    index = pd.bdate_range("1999-01-04", "2026-07-10")
    frame = pd.DataFrame(100.0, index=index, columns=final.REQUIRED_COLUMNS)
    starts = {asset: pd.Timestamp("1999-01-01") for asset in final.RISK_ASSETS}
    folds = final.annual_fold_schedule(frame, starts, end=pd.Timestamp("2026-07-10"))

    fold = next(row for row in folds if row["year"] == 2024)
    assert fold["train"] == ["2018-01-01", "2022-12-31"]
    assert fold["validation"][0] == "2023-01-01"
    assert fold["test"] == ["2024-01-01", "2024-12-31"]
    assert fold["embargo_sessions"] == 21
    validation_end = pd.Timestamp(fold["validation"][1])
    embargo = index[(index > validation_end) & (index < pd.Timestamp("2024-01-01"))]
    assert len(embargo) == 21


def test_top3_is_explicitly_excluded() -> None:
    assert final.RISK_ASSETS == (
        "SPY", "QQQ", "PDP", "MTUM", "MMTM", "SPMO", "QMOM"
    )
    assert not any("TOP" in ticker.upper() for ticker in final.RISK_ASSETS)

#!/usr/bin/env python3
"""Build the strictly allowlisted public momentum-research study JSON.

This is a one-way publication boundary.  Private source artifacts may contain
account state, local paths, or operational detail; this module never copies a
source object wholesale.  Every public field is constructed explicitly from a
small schema-shaped allowlist.

The historical Top-3/Top-5 paths are deliberately labelled current-universe
proxies.  They are useful for replaying the rule, but incomplete point-in-time
membership and delisting returns keep them BLOCK_DECISION_GRADE.
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import math
import re
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import pandas as pd

try:
    from scripts.artifact_io import atomic_write_json
    from scripts import momentum_final_candidate as final_candidate
    from scripts import momentum_top3
except ModuleNotFoundError:  # direct ``python scripts/...`` execution
    from artifact_io import atomic_write_json
    import momentum_final_candidate as final_candidate
    import momentum_top3


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EXTENDED_PRICES = ROOT / "output" / "momentum_policy_lab" / "extended_prices.csv.gz"
DEFAULT_TOP3_ARTIFACT = ROOT / "output" / "momentum_top3" / "momentum_top3.json"
DEFAULT_QUALITY_ARTIFACT = (
    ROOT / "output" / "momentum_data_quality" / "momentum_data_quality.json"
)
DEFAULT_SPMO_ARTIFACT = ROOT / "output" / "spmo_momentum" / "latest_spmo_momentum.json"
DEFAULT_OUTPUT = ROOT / "site" / "momentum-research-public" / "data" / "study.json"

PUBLIC_STRATEGY_IDS = (
    "spmo-production",
    "spmo-shadow-10m",
    "spy-benchmark",
    "qqq-benchmark",
    "top3-11m-proxy",
    "top5-11m-proxy",
)
EXPECTED_TOP3 = ("SNDK", "MU", "WDC")
EXPECTED_TOP5 = ("SNDK", "MU", "WDC", "LITE", "INTC")
PUBLIC_COST_BPS = 10.0
PUBLIC_FUNDING_SPREAD = 0.03
TRADING_DAYS = 252.0
EPS = 1e-12

FORBIDDEN_KEYS = {
    "account",
    "accountid",
    "accountnumber",
    "position",
    "positions",
    "holding",
    "holdings",
    "quantity",
    "shares",
    "transaction",
    "transactions",
    "costbasis",
    "marketvalue",
    "cashflow",
    "broker",
    "email",
    "filepath",
    "absolutepath",
}
PRIVATE_VALUE = re.compile(
    r"(?:/Users/|/home/|file://|[A-Za-z]:\\|[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,})",
    re.IGNORECASE,
)


def _load_json(path: Path) -> dict[str, Any]:
    with Path(path).open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {Path(path).name}")
    return value


def _number(value: Any, digits: int = 8) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("public study cannot contain a non-finite number")
    return round(number, digits)


def _date(value: Any) -> str:
    return str(pd.Timestamp(value).date())


def _completed_month_ends(series: pd.Series) -> pd.DatetimeIndex:
    series = series.dropna().sort_index()
    observed = series.groupby(series.index.to_period("M")).tail(1).index
    return observed[observed.to_period("M") < series.index[-1].to_period("M")]


def _trailing_start(index: pd.DatetimeIndex, end: pd.Timestamp, years: int = 10) -> pd.Timestamp:
    eligible = index[index >= end - pd.DateOffset(years=years)]
    if not len(eligible):
        raise ValueError("price history does not cover the requested public window")
    start = eligible[0]
    if int((index < start).sum()) < 1:
        raise ValueError("simulation needs a pre-window observation")
    return start


def _performance_metrics(
    equity: pd.Series,
    *,
    annual_turnover: float,
    status: str,
) -> dict[str, Any]:
    equity = equity.dropna().sort_index()
    if len(equity) < 2 or float(equity.iloc[0]) <= 0:
        raise ValueError("equity path is too short or invalid")
    years = max((equity.index[-1] - equity.index[0]).days / 365.25, 1.0 / TRADING_DAYS)
    total = float(equity.iloc[-1] / equity.iloc[0])
    cagr = total ** (1.0 / years) - 1.0
    drawdown = equity / equity.cummax() - 1.0
    maximum_drawdown = float(drawdown.min())
    calmar = cagr / abs(maximum_drawdown) if maximum_drawdown < -EPS else 0.0
    return {
        "cagr": _number(cagr),
        "maxDrawdown": _number(maximum_drawdown),
        "calmar": _number(calmar, 6),
        "annualTurnover": _number(max(0.0, annual_turnover), 6),
        "status": status,
    }


def _weekly_plus_decisions(
    equity: pd.Series,
    exposure: pd.Series,
    decisions: Mapping[pd.Timestamp, dict[str, Any]],
) -> list[dict[str, Any]]:
    equity = equity.dropna().sort_index()
    exposure = exposure.reindex(equity.index).ffill().fillna(0.0)
    drawdown = equity / equity.cummax() - 1.0
    weekly = equity.groupby(equity.index.to_period("W-SUN")).tail(1).index
    decision_dates = pd.DatetimeIndex(
        [pd.Timestamp(value) for value in decisions if pd.Timestamp(value) in equity.index]
    )
    selected = pd.DatetimeIndex([equity.index[0], equity.index[-1], drawdown.idxmin()])
    selected = selected.union(weekly).union(decision_dates).sort_values()

    points: list[dict[str, Any]] = []
    for day in selected:
        points.append({
            "date": str(day.date()),
            "nav": _number(equity.at[day], 8),
            "drawdown": _number(min(0.0, float(drawdown.at[day])), 8),
            "exposure": _number(float(exposure.at[day]), 6),
            "decision": copy.deepcopy(decisions.get(day)),
        })
    return points


def _quality_ledger(quality: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    current = quality.get("currentCandidates")
    if not isinstance(current, dict):
        raise ValueError("data-quality artifact lacks currentCandidates")

    quality_data = quality.get("data")
    covered = quality_data.get("providerCoveredCount") if isinstance(quality_data, dict) else None
    security_count = quality_data.get("securityCount") if isinstance(quality_data, dict) else None
    coverage_note = (
        f"提供商原始/调整/行动字段覆盖 {int(covered)}/{int(security_count)}；"
        if covered is not None and security_count is not None else ""
    )

    def convert(source_name: str, expected: tuple[str, ...]) -> list[dict[str, Any]]:
        rows = current.get(source_name)
        if not isinstance(rows, list):
            raise ValueError(f"data-quality artifact lacks {source_name}")
        if tuple(str(row.get("ticker")) for row in rows) != expected:
            raise ValueError(f"unexpected {source_name} candidate order")
        output: list[dict[str, Any]] = []
        for rank, row in enumerate(rows, start=1):
            gate = str(row.get("gate", "BLOCK")).upper()
            status = {
                "PASS": "PASS",
                "WATCH": "WATCH",
                "BLOCK": "BLOCK_DECISION_GRADE",
            }.get(gate, "BLOCK_DECISION_GRADE")
            raw_reasons = row.get("gateReasons")
            reasons = [str(value) for value in raw_reasons] if isinstance(raw_reasons, list) else []
            reason = reasons[0] if reasons else "quality evidence is incomplete"
            evidence = f"{coverage_note}当前11个月窗口数据门禁 {gate}：{reason}。"
            output.append({
                "rank": rank,
                "asset": str(row["ticker"]),
                "momentumReturn": _number(row["momentumReturn"], 8),
                "status": status,
                "evidence": evidence[:240],
            })
        return output

    top3 = convert("11mTop3", EXPECTED_TOP3)
    top5 = convert("11mTop5", EXPECTED_TOP5)
    return {"top3": top3, "top5": top5}


def _spmo_public_models(spmo: Mapping[str, Any]) -> list[dict[str, Any]]:
    as_of = _date(spmo["asOf"])
    price = _number(spmo["price"], 2)
    ema21 = _number(spmo["ema21"], 2)
    ema50 = _number(spmo["ema50"], 2)
    invalidation = _number(spmo.get("levels", {}).get("invalidationClose"), 2)
    moving_stop = _number(spmo.get("levels", {}).get("movingStop3Atr"), 2)
    buy_stop = _number(spmo.get("levels", {}).get("buyStop"), 2)
    buy_limit = _number(spmo.get("levels", {}).get("buyLimit"), 2)
    market_label = str(spmo.get("gates", {}).get("marketDecisionLabel", "BLOCK_DATA"))
    return [
        {
            "track": "existing-sleeve",
            "label": "既有生产袖套",
            "asOf": as_of,
            "riskyAsset": "SPMO",
            "targetExposure": 0.08,
            "cashExposure": 0.92,
            "action": "仅管理既有8%模型袖套；不新增",
            "gate": "WATCH",
            "note": (
                f"SPMO {price:.2f}，EMA21 {ema21:.2f}，EMA50 {ema50:.2f}；"
                f"收盘跌破 {invalidation:.2f} 防守/减仓；3ATR移动止盈 {moving_stop:.2f} 只升不降。"
            ),
        },
        {
            "track": "new-capital",
            "label": "新增资金",
            "asOf": as_of,
            "riskyAsset": "SPMO",
            "targetExposure": 0.0,
            "cashExposure": 1.0,
            "action": "BLOCK 新增；等待市场门恢复",
            "gate": "BLOCK",
            "note": (
                f"买入触发 {buy_stop:.2f}、限价 {buy_limit:.2f}，且市场门须为ALLOW；"
                f"当前 {market_label}，否则0%风险资产/100%现金。"
            ),
        },
    ]


def _shadow_public_models(as_of: str, current_internal_target: float) -> list[dict[str, Any]]:
    state = "ON" if current_internal_target > 0.5 + EPS else "OFF"
    account_shadow = 0.10 if state == "ON" else 0.04
    return [
        {
            "track": "existing-sleeve",
            "label": "影子观察轨",
            "asOf": as_of,
            "riskyAsset": "SPMO",
            "targetExposure": account_shadow,
            "cashExposure": 1.0 - account_shadow,
            "action": f"影子状态 {state}；记录而不转生产",
            "gate": "WATCH",
            "note": (
                f"内部目标 {current_internal_target:.2f}×；按8%基准映射为"
                f"{account_shadow:.0%}，生产分配仍为0%。"
            ),
        },
        {
            "track": "new-capital",
            "label": "生产新增资金",
            "asOf": as_of,
            "riskyAsset": "SPMO",
            "targetExposure": 0.0,
            "cashExposure": 1.0,
            "action": "不部署：仅做前瞻影子验证",
            "gate": "BLOCK",
            "note": "普通与强化学习严格门均未证明可替代生产基线，继续样本外观察。",
        },
    ]


def _comparison_models(asset: str, as_of: str, reference_price: float) -> list[dict[str, Any]]:
    return [
        {
            "track": "existing-sleeve",
            "label": "历史对照",
            "asOf": as_of,
            "riskyAsset": asset,
            "targetExposure": 0.0,
            "cashExposure": 1.0,
            "action": "仅保留为历史基准",
            "gate": "BLOCK",
            "note": f"同期参考收盘 {reference_price:.2f}；只用于衡量相对表现，不构成配置建议。",
        },
        {
            "track": "new-capital",
            "label": "新增资金",
            "asOf": as_of,
            "riskyAsset": asset,
            "targetExposure": 0.0,
            "cashExposure": 1.0,
            "action": "不以基准替代执行策略",
            "gate": "BLOCK",
            "note": f"同期参考收盘 {reference_price:.2f}；若单独投资该基准，应另行建立风险预算。",
        },
    ]


def _top_proxy_models(
    assets: tuple[str, ...],
    as_of: str,
) -> list[dict[str, Any]]:
    names = " / ".join(assets)
    count = len(assets)
    return [
        {
            "track": "existing-sleeve",
            "label": f"11m Top{count} 研究轨",
            "asOf": as_of,
            "riskyAsset": names,
            "targetExposure": 0.0,
            "cashExposure": 1.0,
            "action": "BLOCK：候选仅展示，不部署",
            "gate": "BLOCK",
            "note": f"当前研究候选为 {names}，若仅作纸面组合则等权；生产目标仍为0%。",
        },
        {
            "track": "new-capital",
            "label": "新增资金",
            "asOf": as_of,
            "riskyAsset": names,
            "targetExposure": 0.0,
            "cashExposure": 1.0,
            "action": "等待PIT成分与退市收益补齐",
            "gate": "BLOCK",
            "note": "身份与当前窗口核验不能修复历史幸存者偏差，因此不用于真实下单。",
        },
    ]


def _baseline_decisions(price: pd.Series, start: pd.Timestamp) -> dict[pd.Timestamp, dict[str, Any]]:
    ema21 = price.ewm(span=21, adjust=False).mean()
    ema50 = price.ewm(span=50, adjust=False).mean()
    decisions: dict[pd.Timestamp, dict[str, Any]] = {}
    for day in _completed_month_ends(price):
        if day < start:
            continue
        close = float(price.at[day])
        fast = float(ema21.at[day])
        slow = float(ema50.at[day])
        if close > fast > slow:
            regime, action = "趋势向上", "HOLD"
        elif close > slow:
            regime, action = "趋势观察", "HOLD / WATCH"
        else:
            regime, action = "趋势转弱", "DEFEND REVIEW"
        decisions[day] = {
            "action": action,
            "regime": regime,
            "reason": (
                f"月末只复盘SPMO收盘 {close:.2f}、EMA21 {fast:.2f}、EMA50 {slow:.2f}；"
                "这是可复现的趋势监测，不追溯声称历史sentinel门。"
            ),
            "targetExposure": 1.0,
            "modelBasket": [{"asset": "SPMO", "weight": 1.0}],
        }
    return decisions


def _benchmark_decisions(price: pd.Series, start: pd.Timestamp, asset: str) -> dict[pd.Timestamp, dict[str, Any]]:
    return {
        day: {
            "action": "HOLD",
            "regime": "被动基准",
            "reason": f"{asset}买入持有对照；月末仅记录，不进行择时或换仓。",
            "targetExposure": 1.0,
            "modelBasket": [{"asset": asset, "weight": 1.0}],
        }
        for day in _completed_month_ends(price)
        if day >= start
    }


def _shadow_decisions(
    price: pd.Series,
    start: pd.Timestamp,
    target: pd.Series,
    signal_mask: pd.Series,
) -> dict[pd.Timestamp, dict[str, Any]]:
    month_close = price.groupby(price.index.to_period("M")).last()
    month_average = month_close.rolling(10, min_periods=10).mean()
    decisions: dict[pd.Timestamp, dict[str, Any]] = {}
    for day in signal_mask[signal_mask].index:
        if day < start:
            continue
        period = day.to_period("M")
        close = float(price.at[day])
        average = float(month_average.at[period])
        desired = float(target.at[day])
        state = "ON" if desired > 0.5 + EPS else "OFF"
        decisions[day] = {
            "action": f"SET {desired:.2f}×",
            "regime": f"10m SMA {state}",
            "reason": (
                f"完成月收盘 {close:.2f} {'高于' if close > average else '不高于'}"
                f"10个月均线 {average:.2f}；目标在下一交易日收盘执行。"
            ),
            "targetExposure": desired,
            "modelBasket": [{"asset": "SPMO", "weight": desired}],
        }
    return decisions


def _buy_hold_strategy(
    frame: pd.DataFrame,
    asset: str,
    *,
    strategy_id: str,
    name: str,
    family: str,
    summary: str,
    status: str,
    current_models: list[dict[str, Any]],
    quality_ledger: dict[str, Any],
    charge_entry_cost: bool,
    trend_decisions: bool = False,
) -> dict[str, Any]:
    price = frame[asset].dropna().sort_index()
    end = price.index[-1]
    start = _trailing_start(price.index, end)
    if charge_entry_cost:
        target, _, signal_mask = final_candidate.monthly_asymmetric_target(
            price, final_candidate.baseline_spec()
        )
        cash = final_candidate.cash_returns(frame, price.index)
        simulation = final_candidate.simulate(
            price,
            cash,
            target,
            signal_mask,
            final_candidate.baseline_spec(),
            start=start,
            end=end,
            cost_bps=PUBLIC_COST_BPS,
            funding_spread=PUBLIC_FUNDING_SPREAD,
        )
        equity = simulation.equity
        exposure = simulation.exposure
        years = max((equity.index[-1] - equity.index[0]).days / 365.25, EPS)
        annual_turnover = float(simulation.turnover.sum()) / years
    else:
        equity = price.loc[price.index >= start]
        equity = equity / float(equity.iloc[0])
        exposure = pd.Series(1.0, index=equity.index, dtype=float)
        annual_turnover = 0.0
    decisions = (
        _baseline_decisions(price, equity.index[0])
        if trend_decisions else _benchmark_decisions(price, equity.index[0], asset)
    )
    return {
        "id": strategy_id,
        "name": name,
        "family": family,
        "summary": summary,
        "exposureUnit": "percent",
        "metrics": _performance_metrics(
            equity, annual_turnover=annual_turnover, status=status
        ),
        "currentModels": current_models,
        "series": _weekly_plus_decisions(equity, exposure, decisions),
        "qualityLedger": copy.deepcopy(quality_ledger),
    }


def _shadow_strategy(
    frame: pd.DataFrame,
    *,
    as_of: str,
    quality_ledger: dict[str, Any],
) -> dict[str, Any]:
    price = frame["SPMO"].dropna().sort_index()
    end = price.index[-1]
    start = _trailing_start(price.index, end)
    spec = {
        "id": "m10_on125_off50",
        "months": 10,
        "risk_on": 1.25,
        "risk_off": 0.5,
        "max_exposure": 1.25,
    }
    target, _, signal_mask = final_candidate.monthly_asymmetric_target(price, spec)
    cash = final_candidate.cash_returns(frame, price.index)
    simulation = final_candidate.simulate(
        price,
        cash,
        target,
        signal_mask,
        spec,
        start=start,
        end=end,
        cost_bps=PUBLIC_COST_BPS,
        funding_spread=PUBLIC_FUNDING_SPREAD,
    )
    years = max(
        (simulation.equity.index[-1] - simulation.equity.index[0]).days / 365.25,
        EPS,
    )
    annual_turnover = float(simulation.turnover.sum()) / years
    current_target = float(target.iloc[-1])
    decisions = _shadow_decisions(price, simulation.equity.index[0], target, signal_mask)
    return {
        "id": "spmo-shadow-10m",
        "name": "备选 · SPMO 10m 非对称趋势",
        "family": "SPMO影子策略",
        "summary": (
            "完成月收盘高于10个月均线时内部1.25×，否则0.50×；"
            "仅做前瞻影子验证，生产分配仍为0%。"
        ),
        "exposureUnit": "multiplier",
        "metrics": _performance_metrics(
            simulation.equity,
            annual_turnover=annual_turnover,
            status="影子",
        ),
        "currentModels": _shadow_public_models(as_of, current_target),
        "series": _weekly_plus_decisions(
            simulation.equity, simulation.exposure, decisions
        ),
        "qualityLedger": copy.deepcopy(quality_ledger),
    }


def _top_proxy_strategy(
    prices: pd.DataFrame,
    *,
    top_n: int,
    as_of: str,
    cost_per_side: float,
    quality_ledger: dict[str, Any],
    independent_starts: Mapping[str, Any],
) -> dict[str, Any]:
    end = prices.index[-1]
    start = _trailing_start(prices.index, end)
    equity, _, diagnostics = momentum_top3.run_backtest(
        prices,
        11,
        top_n,
        start,
        end,
        cost_per_side,
        coverage=0.95,
        exclude=("SPY", "QQQ"),
        return_diagnostics=True,
        anchor_staleness_sessions=3,
        signal_staleness_sessions=0,
        warm_start_months=2,
        max_fill_retry_sessions=3,
        independent_trading_start=independent_starts,
    )
    if len(equity) < 2:
        raise ValueError(f"11m Top{top_n} proxy produced no usable path")
    exposure = pd.Series(1.0, index=equity.index, dtype=float)
    decisions: dict[pd.Timestamp, dict[str, Any]] = {}
    for row in diagnostics.get("fills", []):
        day = pd.Timestamp(row["signalDate"])
        if day not in equity.index:
            continue
        weights = row.get("targetWeights", {})
        assets = [str(asset) for asset, weight in weights.items() if float(weight) > 0]
        if not assets:
            continue
        decisions[day] = {
            "action": "REBALANCE → " + " / ".join(assets),
            "regime": f"11m Top{top_n} · 当代成分代理",
            "reason": (
                "用当月末11个月总回报排序并等权，下一交易日收盘执行；"
                "历史成分与退市收益不完整，BLOCK_DECISION_GRADE。"
            ),
            "targetExposure": 1.0,
            "modelBasket": [
                {"asset": str(asset), "weight": _number(weight, 10)}
                for asset, weight in weights.items()
                if float(weight) > 0
            ],
        }
    years = max((equity.index[-1] - equity.index[0]).days / 365.25, EPS)
    annual_turnover = sum(
        float(row.get("oneWayTurnover", 0.0)) for row in diagnostics.get("fills", [])
    ) / years
    assets = EXPECTED_TOP3 if top_n == 3 else EXPECTED_TOP5
    return {
        "id": f"top{top_n}-11m-proxy",
        "name": f"研究 · 11m Top{top_n} 当代成分代理",
        "family": "个股横截面动量（非决策级）",
        "summary": (
            f"每个完成月末按11个月总回报选择Top{top_n}等权。"
            "身份与当期窗口可核验，但十年曲线仍受现时成分、退市与公司行动缺口影响。"
        ),
        "exposureUnit": "percent",
        "metrics": _performance_metrics(
            equity, annual_turnover=annual_turnover, status="非决策级代理"
        ),
        "currentModels": _top_proxy_models(assets, as_of),
        "series": _weekly_plus_decisions(equity, exposure, decisions),
        "qualityLedger": copy.deepcopy(quality_ledger),
    }


def _independent_starts(quality: Mapping[str, Any]) -> dict[str, str]:
    output: dict[str, str] = {}
    securities = quality.get("securities")
    if not isinstance(securities, dict):
        return output
    for ticker, row in securities.items():
        if not isinstance(row, dict):
            continue
        identity = row.get("identity")
        start = identity.get("regularWayStart") if isinstance(identity, dict) else None
        if start:
            output[str(ticker)] = _date(start)
    return output


def _signal_as_of(top3_source: Mapping[str, Any], quality: Mapping[str, Any]) -> str:
    for strategy in top3_source.get("strategies", []):
        if strategy.get("id") == "RET_11M_top3":
            signal = strategy.get("current_signal")
            if isinstance(signal, dict) and signal.get("as_of"):
                return _date(signal["as_of"])
    securities = quality.get("securities", {})
    sndk = securities.get("SNDK", {}) if isinstance(securities, dict) else {}
    signal = sndk.get("signalEligibility", {}).get("11m", {}) if isinstance(sndk, dict) else {}
    if signal.get("signalDate"):
        return _date(signal["signalDate"])
    raise ValueError("cannot determine current 11m signal date")


def _assert_top_proxy_matches_source(
    top3_source: Mapping[str, Any],
    public_strategies: list[dict[str, Any]],
) -> None:
    window = top3_source.get("window")
    if not isinstance(window, dict) or int(window.get("years", 0)) != 10:
        raise ValueError("corrected Top-3/Top-5 source must use a ten-year window")
    source = {
        str(row.get("id")): row
        for row in top3_source.get("strategies", [])
        if isinstance(row, dict)
    }
    pairs = (
        ("RET_11M_top3", "top3-11m-proxy"),
        ("RET_11M_top5", "top5-11m-proxy"),
    )
    public = {row["id"]: row for row in public_strategies}
    for source_id, public_id in pairs:
        if source_id not in source or public_id not in public:
            raise ValueError(f"missing corrected proxy {source_id}")
        expected = float(source[source_id].get("metrics", {}).get("total_x"))
        series = public[public_id]["series"]
        observed = float(series[-1]["nav"]) / float(series[0]["nav"])
        if not math.isclose(observed, expected, abs_tol=0.011):
            raise ValueError(
                f"{public_id} endpoint {observed:.4f} disagrees with source {expected:.2f}"
            )


def _assert_public_safety(payload: Mapping[str, Any]) -> None:
    def walk(value: Any, path: str = "study") -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
                if normalized in FORBIDDEN_KEYS:
                    raise ValueError(f"forbidden public key at {path}")
                walk(child, f"{path}.{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, f"{path}[{index}]")
        elif isinstance(value, str) and PRIVATE_VALUE.search(value):
            raise ValueError(f"private-looking value at {path}")
        elif isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"non-finite value at {path}")

    walk(payload)


def build_public_study(
    *,
    extended_prices_path: Path = DEFAULT_EXTENDED_PRICES,
    top3_artifact_path: Path = DEFAULT_TOP3_ARTIFACT,
    quality_artifact_path: Path = DEFAULT_QUALITY_ARTIFACT,
    spmo_artifact_path: Path = DEFAULT_SPMO_ARTIFACT,
) -> dict[str, Any]:
    """Build a deterministic, schema-shaped public study without private keys."""
    frame = final_candidate.read_extended_cache(Path(extended_prices_path))
    top3_source = _load_json(Path(top3_artifact_path))
    quality = _load_json(Path(quality_artifact_path))
    spmo = _load_json(Path(spmo_artifact_path))
    price_cache = momentum_top3.read_price_cache()

    if quality.get("decisionGradeGate") != "BLOCK_DECISION_GRADE":
        raise ValueError("historical data-quality gate must fail closed")
    ledger = _quality_ledger(quality)
    generated_date = max(
        _date(frame.index[-1]),
        _date(price_cache.index[-1]),
        _date(spmo["asOf"]),
    )
    signal_as_of = _signal_as_of(top3_source, quality)
    completed_signal_date = quality.get("data", {}).get("completedSignalDate")
    if completed_signal_date and signal_as_of != _date(completed_signal_date):
        raise ValueError("Top-3 signal date disagrees with the quality audit")
    if signal_as_of != "2026-06-30":
        raise ValueError("public release expects the audited 2026-06-30 signal")
    quality_data = quality.get("data", {})
    if (
        quality_data.get("providerCoveredCount") != 518
        or quality_data.get("securityCount") != 518
    ):
        raise ValueError("public release expects complete 518/518 provider coverage")
    cost_per_side = float(top3_source.get("cost_per_side", PUBLIC_COST_BPS / 10000.0))
    if not math.isclose(cost_per_side, PUBLIC_COST_BPS / 10000.0, abs_tol=1e-12):
        raise ValueError("Top-3 artifact cost must be 10 bps per side")

    strategies = [
        _buy_hold_strategy(
            frame,
            "SPMO",
            strategy_id="spmo-production",
            name="首选 · SPMO 生产基线",
            family="SPMO顺势袖套",
            summary=(
                "生产上限以小袖套执行；历史图采用SPMO十年买入持有代理。"
                "当前只管理既有8%，市场门未恢复前新增资金保持现金。"
            ),
            status="采用",
            current_models=_spmo_public_models(spmo),
            quality_ledger=ledger,
            charge_entry_cost=True,
            trend_decisions=True,
        ),
        _shadow_strategy(frame, as_of=generated_date, quality_ledger=ledger),
        _buy_hold_strategy(
            frame,
            "SPY",
            strategy_id="spy-benchmark",
            name="基准 · SPY",
            family="市场基准",
            summary="SPY十年买入持有，仅用于比较收益与回撤，不是当前建议配置。",
            status="基准",
            current_models=_comparison_models(
                "SPY", generated_date, float(frame["SPY"].dropna().iloc[-1])
            ),
            quality_ledger=ledger,
            charge_entry_cost=False,
        ),
        _buy_hold_strategy(
            frame,
            "QQQ",
            strategy_id="qqq-benchmark",
            name="基准 · QQQ",
            family="市场基准",
            summary="QQQ十年买入持有，仅用于比较纳指暴露，不是当前建议配置。",
            status="基准",
            current_models=_comparison_models(
                "QQQ", generated_date, float(frame["QQQ"].dropna().iloc[-1])
            ),
            quality_ledger=ledger,
            charge_entry_cost=False,
        ),
        _top_proxy_strategy(
            price_cache,
            top_n=3,
            as_of=signal_as_of,
            cost_per_side=cost_per_side,
            quality_ledger=ledger,
            independent_starts=_independent_starts(quality),
        ),
        _top_proxy_strategy(
            price_cache,
            top_n=5,
            as_of=signal_as_of,
            cost_per_side=cost_per_side,
            quality_ledger=ledger,
            independent_starts=_independent_starts(quality),
        ),
    ]
    if tuple(strategy["id"] for strategy in strategies) != PUBLIC_STRATEGY_IDS:
        raise AssertionError("public strategy ordering drifted")
    _assert_top_proxy_matches_source(top3_source, strategies)

    payload = {
        "schemaVersion": 1,
        "dataStatus": "public-research",
        "siteTitle": "动量策略研究与历史决策回放",
        "generatedDate": generated_date,
        "notice": (
            "研究用途，不构成投资建议。当前生产建议为既有SPMO模型袖套8%、新增资金0%；"
            "11m Top3/Top5仅展示已核验的当前候选，历史表现仍BLOCK_DECISION_GRADE。"
        ),
        "strategies": strategies,
        "methodology": [
            {
                "title": "执行口径",
                "body": (
                    "月末收盘形成信号，下一交易日收盘成交；旧组合先获得成交日收益。"
                    "换仓按单边10bp计费；影子策略杠杆部分按13周国库券收益再加3%年化融资差。"
                ),
            },
            {
                "title": "研究分段",
                "body": (
                    "参数研究训练期截至2021年，2022–2023为验证期，2024年至今为固定回放。"
                    "11m个股代理没有完整PIT成分与独立样本外证明，因此单独封锁。"
                ),
            },
            {
                "title": "生产与影子分离",
                "body": (
                    "首选策略继续采用不加杠杆的SPMO小袖套与移动风控；"
                    "10m ON1.25/OFF0.50只记录影子结果，当前ON映射10%观察目标但生产为0%。"
                ),
            },
            {
                "title": "身份与公司行动",
                "body": (
                    "518/518证券已取得提供商原始价、调整价与行动字段；当前11m候选通过永久身份、"
                    "现时指数成分、常规交易起点和回看窗口行动核验。"
                    "十年个股回放仍缺完整退市收益，故不能用极高收益作实盘依据。"
                ),
            },
            {
                "title": "强化学习结论",
                "body": (
                    "普通强化学习实验在七个严格门中通过0个；在可解释基线未被稳定超越前，"
                    "不部署RL策略，也不因训练内高分提高真实风险预算。"
                ),
            },
        ],
        "limitations": [
            "Top3/Top5十年曲线使用今天的SPX、NDX与DJIA成分回放，存在明确幸存者偏差。",
            "免费数据未覆盖所有退市收益、并购后分配与历史成分；跨复杂公司行动的区间可能失真。",
            "当前候选的数据门禁PASS只说明当前11个月窗口可用，不代表历史曲线达到决策级。",
            "曲线使用Yahoo/yfinance调整后日线价格回报代理，不是发行人官方NAV总回报；日线也不能验证盘中buy-stop或stop的真实成交。",
            "网页展示的是模型百分比，不含任何真实账户、股数、成本、交易或资金金额。",
        ],
    }
    _assert_public_safety(payload)
    return payload


def write_public_study(path: Path = DEFAULT_OUTPUT) -> tuple[Path, dict[str, Any]]:
    payload = build_public_study()
    target = atomic_write_json(Path(path), payload, indent=2)
    return target, payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="strictly sanitized public study JSON target",
    )
    args = parser.parse_args(argv)
    target, payload = write_public_study(args.output)
    print(f"public study: {target}")
    for strategy in payload["strategies"]:
        metrics = strategy["metrics"]
        print(
            f"{strategy['id']}: CAGR={metrics['cagr']:.4%} "
            f"MDD={metrics['maxDrawdown']:.4%} "
            f"Calmar={metrics['calmar']:.3f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

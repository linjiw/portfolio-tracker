#!/usr/bin/env python3
"""Close-only YTD research backtest for the QQQ weather / TQQQ tactical strategy.

The repository cache stores daily closes. That is enough to test the broad
EMA8/EMA21 timing idea, but not enough to prove intraday low-touch behavior or
historical option prices. Signals observed at a close are filled at the next
available close with adverse slippage. The option overlay is a synthetic payoff
stress test and is deliberately excluded from strategy return claims.
"""

import argparse
import datetime as dt
import html
import json
import math
from pathlib import Path

try:
    from scripts.artifact_io import (
        atomic_write_csv,
        atomic_write_json,
        atomic_write_text,
        ensure_private_directory,
    )
except ModuleNotFoundError:
    from artifact_io import atomic_write_csv, atomic_write_json, atomic_write_text, ensure_private_directory


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CACHE = ROOT / "output" / "prices_cache.json"
DEFAULT_OUT = ROOT / "output" / "qqq_tqqq_backtest"

METHODOLOGY_VERSION = "2.0"
MODEL_GRADE = "research_only_not_decision_grade"
DEFAULT_EQUITY_SLIPPAGE_BPS = 5.0
DEFAULT_COMMISSION_PER_ORDER = 0.0
DEFAULT_CCS_EXECUTION_COST_PER_CONTRACT = 4.60
MIN_INDICATOR_WARMUP_SESSIONS = 34

LIMITATIONS = [
    "Daily adjusted closes only: signals cannot observe or execute intraday touches, stops, or gaps.",
    "A signal observed at session close is filled at the next session close, not at the next open.",
    "Max drawdown is close-to-close and can understate intraday peak-to-trough loss.",
    "QQQ close-to-close absolute change is an ATR proxy because OHLC true range is unavailable.",
    "EMA values are seeded from the first cached observation and remain path-dependent during warmup.",
    "Matching gaps in both symbols cannot be distinguished from exchange holidays without a market calendar.",
    "TQQQ history reflects the vendor's adjusted series; leverage drag and corporate-action quality depend on that vendor.",
    "The default 5 bps per-side equity slippage is an explicit scenario assumption, not a quote-calibrated estimate.",
    "CCS strikes, credits, and expiration payoffs do not use a historical option chain, IV surface, bid/ask, or assignment model.",
    "The CCS diagnostic is excluded from strategy returns, drawdowns, and parameter ranking.",
    "The DCA benchmark knows the selected sample horizon and deploys all capital evenly by its final row.",
    "Parameter sweeps are in-sample and have no walk-forward or out-of-sample validation.",
    "Taxes, borrow constraints, market impact, and cash yield are not modeled.",
]


DEFAULT_STRATEGY_CONFIG = {
    "name": "baseline_70_core_full_overheat",
    "core_alloc": 0.70,
    "ema8_alloc": 0.15,
    "ema21_alloc": 0.30,
    "overheat_exit": "full",
    "overheat_sell_pct": 1.0,
    "trail_pct": 0.08,
    "ccs_mode": "none",
    "ccs_hold_days": 10,
    "ccs_multiplier": 4.0,
    "ccs_contracts": 1,
    "equity_slippage_bps": DEFAULT_EQUITY_SLIPPAGE_BPS,
    "commission_per_order": DEFAULT_COMMISSION_PER_ORDER,
    "ccs_execution_cost_per_contract": DEFAULT_CCS_EXECUTION_COST_PER_CONTRACT,
}


def ema(vals, n):
    a = 2.0 / (n + 1)
    out, e = [], None
    for v in vals:
        e = v if e is None else a * v + (1 - a) * e
        out.append(e)
    return out


def rsi(vals, n=14):
    out = [None] * len(vals)
    if len(vals) <= n:
        return out
    deltas = [vals[i] - vals[i - 1] for i in range(1, len(vals))]
    avg_gain = sum(d for d in deltas[:n] if d > 0) / n
    avg_loss = sum(-d for d in deltas[:n] if d < 0) / n

    def value(gain, loss):
        if loss == 0:
            return 100.0 if gain > 0 else 50.0
        if gain == 0:
            return 0.0
        return 100 - 100 / (1 + gain / loss)

    out[n] = value(avg_gain, avg_loss)
    for i in range(n + 1, len(vals)):
        d = deltas[i - 1]
        avg_gain = (avg_gain * (n - 1) + max(d, 0)) / n
        avg_loss = (avg_loss * (n - 1) + max(-d, 0)) / n
        out[i] = value(avg_gain, avg_loss)
    return out


def close_proxy_atr(vals, n=14):
    """Wilder-smoothed close-change proxy, unavailable until ``n`` ranges exist."""
    if not isinstance(n, int) or n <= 0:
        raise ValueError("ATR period must be a positive integer.")
    if not vals:
        return []
    out, trs, prev_atr = [None], [], None
    prev = vals[0]
    for v in vals[1:]:
        tr = abs(v - prev)
        trs.append(tr)
        if len(trs) < n:
            atr = None
        elif len(trs) == n:
            atr = sum(trs) / n
        else:
            atr = (prev_atr * (n - 1) + tr) / n
        out.append(atr)
        if atr is not None:
            prev_atr = atr
        prev = v
    return out


def pct_change(vals, i, n):
    if i < n or vals[i - n] == 0:
        return None
    return (vals[i] / vals[i - n] - 1) * 100


def slope_pct(vals, i, n=5):
    return pct_change(vals, i, n)


def max_drawdown(values):
    peak = None
    worst = 0.0
    for v in values:
        peak = v if peak is None else max(peak, v)
        if peak:
            worst = min(worst, v / peak - 1)
    return worst


def max_drawdown_from_capital(values, initial_capital):
    """Peak-to-trough drawdown including the investable capital at inception."""
    return max_drawdown([initial_capital, *values])


def annualized_return(start_value, final_value, start_date, end_date):
    days = max((parse_date(end_date) - parse_date(start_date)).days, 1)
    if start_value <= 0 or final_value <= 0:
        return None
    return (final_value / start_value) ** (365.2425 / days) - 1


def annualized_return_pct(start_value, final_value, start_date, end_date):
    value = annualized_return(start_value, final_value, start_date, end_date)
    return value * 100 if value is not None else None


def parse_date(s):
    return dt.date.fromisoformat(s)


def rn(v, digits=2):
    return None if v is None else round(v, digits)


def load_prices(path):
    with open(path, "r") as f:
        return json.load(f)


def fetch_yfinance_prices(start, end=None):
    try:
        import yfinance as yf
    except ImportError as exc:
        raise RuntimeError("yfinance is required for --fetch-yfinance") from exc
    # yfinance treats end as exclusive; this CLI documents --end as inclusive.
    fetch_end = (parse_date(end) + dt.timedelta(days=1)).isoformat() if end else None
    df = yf.download(
        ["QQQ", "TQQQ"],
        start=start,
        end=fetch_end,
        auto_adjust=True,
        progress=False,
        threads=False,
    )
    if df.empty:
        raise RuntimeError("yfinance returned no QQQ/TQQQ rows.")
    prices = {"QQQ": {}, "TQQQ": {}}
    close = df["Close"] if "Close" in df else df
    for sym in ("QQQ", "TQQQ"):
        series = close[sym].dropna()
        for d, v in series.items():
            prices[sym][d.date().isoformat()] = float(v)
    return prices


def build_rows(prices, start="2026-01-01", end=None):
    start_date = parse_date(start)
    end_date = parse_date(end) if end else None
    if end_date and end_date < start_date:
        raise ValueError("end must be on or after start.")
    qqq = prices.get("QQQ") or {}
    tqqq = prices.get("TQQQ") or {}
    if not isinstance(qqq, dict) or not isinstance(tqqq, dict):
        raise ValueError("QQQ and TQQQ price series must be date-to-close mappings.")

    def dates_in_range(series, symbol):
        selected = set()
        for date_key in series:
            try:
                value = parse_date(date_key)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Invalid {symbol} price date: {date_key!r}") from exc
            if value >= start_date and (not end_date or value <= end_date):
                selected.add(date_key)
        return selected

    qqq_dates = dates_in_range(qqq, "QQQ")
    tqqq_dates = dates_in_range(tqqq, "TQQQ")
    if qqq_dates != tqqq_dates:
        q_only = sorted(qqq_dates - tqqq_dates)[:3]
        t_only = sorted(tqqq_dates - qqq_dates)[:3]
        raise ValueError(
            "QQQ/TQQQ session coverage mismatch in requested range; "
            f"QQQ-only examples={q_only}, TQQQ-only examples={t_only}."
        )
    dates = sorted(qqq_dates)
    rows = []
    for d in dates:
        try:
            q_value = float(qqq[d])
            t_value = float(tqqq[d])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Non-numeric QQQ/TQQQ close on {d}.") from exc
        if not (math.isfinite(q_value) and math.isfinite(t_value) and q_value > 0 and t_value > 0):
            raise ValueError(f"QQQ/TQQQ closes must be positive and finite on {d}.")
        rows.append({"date": d, "QQQ": q_value, "TQQQ": t_value})
    if len(rows) < 40:
        raise ValueError("Need at least 40 overlapping QQQ/TQQQ close rows for this backtest.")
    return rows


def add_indicators(rows):
    q = [r["QQQ"] for r in rows]
    t = [r["TQQQ"] for r in rows]
    ema_map = {n: ema(q, n) for n in (8, 13, 21, 34, 55)}
    atr = close_proxy_atr(q)
    q_rsi = rsi(q)
    for i, r in enumerate(rows):
        for n, series in ema_map.items():
            r[f"ema{n}"] = series[i]
        r["atr14"] = atr[i]
        r["rsi14"] = q_rsi[i]
        r["qqq_5d_pct"] = pct_change(q, i, 5)
        r["tqqq_5d_pct"] = pct_change(t, i, 5)
        r["ema21_slope_5d"] = slope_pct(ema_map[21], i, 5)
    return rows


def classify(row, prev_row=None):
    atr = row["atr14"]
    close = row["QQQ"]
    e8, e13, e21, e34 = row["ema8"], row["ema13"], row["ema21"], row["ema34"]
    e21_slope = row["ema21_slope_5d"] or 0.0
    stacked = close > e21 and e8 > e13 > e21 and e21_slope > 0
    two_below = bool(prev_row and close < e21 and prev_row["QQQ"] < prev_row["ema21"])
    trend_break = two_below or e8 < e21 or (close < e21 and e21_slope <= 0)
    overheat = bool(stacked and atr and (
        close - e8 > 1.5 * atr
        or (row["qqq_5d_pct"] is not None and row["qqq_5d_pct"] > 3.0)
        or (row["tqqq_5d_pct"] is not None and row["tqqq_5d_pct"] > 9.0)
    ))
    near8 = bool(stacked and atr and not overheat and close >= e8 and abs(close - e8) <= 0.5 * atr)
    near21 = bool(
        atr
        and not overheat
        and close >= e21
        and abs(close - e21) <= 0.5 * atr
        and e21_slope > 0
        and e21 >= e34 * 0.995
        and not two_below
    )
    if trend_break:
        state = "break"
    elif near21:
        state = "ema21"
    elif near8:
        state = "ema8"
    elif overheat:
        state = "overheat"
    elif stacked:
        state = "bull"
    else:
        state = "mixed"
    return {
        "state": state,
        "stacked": stacked,
        "trend_break": trend_break,
        "overheat": overheat,
        "near8": near8,
        "near21": near21,
    }


def strategy_config(**overrides):
    cfg = dict(DEFAULT_STRATEGY_CONFIG)
    cfg.update(overrides)
    return cfg


def validate_strategy_inputs(rows, capital, warmup, cfg):
    if not rows:
        raise ValueError("At least one price row is required.")
    if not math.isfinite(capital) or capital <= 0:
        raise ValueError("capital must be a positive finite number.")
    if not isinstance(warmup, int) or warmup < 0:
        raise ValueError("warmup must be a non-negative integer.")
    previous_date = None
    for row in rows:
        try:
            row_date = parse_date(row["date"])
            q_close = float(row["QQQ"])
            t_close = float(row["TQQQ"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Every row must contain a valid ISO date and numeric QQQ/TQQQ closes.") from exc
        if previous_date and row_date <= previous_date:
            raise ValueError("Price rows must be strictly increasing by date.")
        if not (math.isfinite(q_close) and math.isfinite(t_close) and q_close > 0 and t_close > 0):
            raise ValueError("QQQ/TQQQ row closes must be positive and finite.")
        previous_date = row_date
    for key in ("core_alloc", "ema8_alloc", "ema21_alloc", "overheat_sell_pct"):
        value = cfg[key]
        if not math.isfinite(value) or not 0 <= value <= 1:
            raise ValueError(f"{key} must be between 0 and 1.")
    if not math.isfinite(cfg["trail_pct"]) or not 0 < cfg["trail_pct"] < 1:
        raise ValueError("trail_pct must be between 0 and 1.")
    for key in ("equity_slippage_bps", "commission_per_order", "ccs_execution_cost_per_contract"):
        value = cfg[key]
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"{key} must be a non-negative finite number.")
    if cfg["equity_slippage_bps"] >= 10_000:
        raise ValueError("equity_slippage_bps must be below 10,000.")
    if cfg["overheat_exit"] not in ("none", "full", "partial"):
        raise ValueError("overheat_exit must be none, full, or partial.")
    if cfg["ccs_mode"] not in ("none", "spot_pct", "ema21_4atr_floor", "ema21_4atr_skip"):
        raise ValueError("Unsupported ccs_mode.")
    if not isinstance(cfg["ccs_hold_days"], int) or cfg["ccs_hold_days"] <= 0:
        raise ValueError("ccs_hold_days must be a positive integer.")
    if not math.isfinite(cfg["ccs_multiplier"]) or cfg["ccs_multiplier"] <= 0:
        raise ValueError("ccs_multiplier must be positive and finite.")
    if not isinstance(cfg["ccs_contracts"], int) or cfg["ccs_contracts"] < 0:
        raise ValueError("ccs_contracts must be a non-negative integer.")


def execution_price(reference_close, side, slippage_bps):
    """Return an adverse close-proxy fill for a buy or sell."""
    if reference_close <= 0:
        raise ValueError("reference_close must be positive.")
    slip = slippage_bps / 10_000.0
    if side == "buy":
        return reference_close * (1 + slip)
    if side == "sell":
        return reference_close * (1 - slip)
    raise ValueError(f"Unknown side: {side}")


def buy_with_budget(reference_close, budget, slippage_bps, commission):
    """Spend at most budget and return shares, effective fill, and explicit cost."""
    if budget <= commission:
        return 0.0, None, 0.0
    fill = execution_price(reference_close, "buy", slippage_bps)
    shares = (budget - commission) / fill
    return shares, fill, budget


def open_tactical(row, cash, equity_before, entry_type, alloc, signal_date, cfg):
    t_close = row["TQQQ"]
    spend = min(cash, equity_before * alloc)
    shares, fill, total_cost = buy_with_budget(
        t_close,
        spend,
        cfg["equity_slippage_bps"],
        cfg["commission_per_order"],
    )
    if shares <= 0 or fill is None:
        return None, cash
    return {
        "signal_date": signal_date,
        "entry_date": row["date"],
        "type": entry_type,
        "entry_price": fill,
        "entry_reference_close": t_close,
        "shares": shares,
        "cost": total_cost,
        "entry_execution_cost_total": total_cost - shares * t_close,
        "high_close": t_close,
        "partial_taken": False,
    }, cash - total_cost


def close_tactical_piece(tactical, date, t_close, shares_to_close, status, reason, signal_date, cfg):
    shares_to_close = min(tactical["shares"], shares_to_close)
    shares_before = tactical["shares"]
    if shares_to_close <= 0 or shares_before <= 0:
        raise ValueError("shares_to_close must be positive.")
    fill = execution_price(t_close, "sell", cfg["equity_slippage_bps"])
    gross_proceeds = shares_to_close * fill
    commission = cfg["commission_per_order"]
    proceeds = gross_proceeds - commission
    allocated_cost = tactical["cost"] * (shares_to_close / shares_before)
    allocated_entry_execution_cost = tactical["entry_execution_cost_total"] * (shares_to_close / shares_before)
    pnl = proceeds - allocated_cost
    tactical["shares"] -= shares_to_close
    tactical["cost"] -= allocated_cost
    tactical["entry_execution_cost_total"] -= allocated_entry_execution_cost
    return proceeds, {
        "entry_signal_date": tactical["signal_date"],
        "entry_date": tactical["entry_date"],
        "exit_signal_date": signal_date,
        "exit_date": date,
        "type": tactical["type"],
        "status": status,
        "entry_price": tactical["entry_price"],
        "entry_reference_close": tactical["entry_reference_close"],
        "exit_price": fill,
        "exit_reference_close": t_close,
        "shares": shares_to_close,
        "return_pct": (proceeds / allocated_cost - 1) * 100 if allocated_cost else None,
        "pnl_dollars": pnl,
        "exit_reason": reason,
        "entry_execution_cost_dollars": allocated_entry_execution_cost,
        "exit_execution_cost_dollars": shares_to_close * t_close - proceeds,
        "fill_timing": "next_trading_close_after_signal",
        "equity_slippage_bps_per_side": cfg["equity_slippage_bps"],
        "commission_per_order": commission,
    }


def run_strategy_on_rows(rows, capital=100000.0, warmup=34, config=None):
    cfg = strategy_config(**(config or {}))
    validate_strategy_inputs(rows, capital, warmup, cfg)
    first = rows[0]
    core_alloc = cfg["core_alloc"]
    core_budget = capital * core_alloc
    core_shares, core_fill, core_cost = buy_with_budget(
        first["QQQ"], core_budget, cfg["equity_slippage_bps"], cfg["commission_per_order"]
    )
    cash = capital - core_cost
    transaction_costs = core_cost - core_shares * first["QQQ"]
    tactical = None
    trades = []
    curve = []
    states = []
    pending_action = None
    qqq_bh_shares, _, qqq_bh_cost = buy_with_budget(
        first["QQQ"], capital, cfg["equity_slippage_bps"], cfg["commission_per_order"]
    )
    qqq_bh_cash = capital - qqq_bh_cost
    tqqq_bh_shares, _, tqqq_bh_cost = buy_with_budget(
        first["TQQQ"], capital, cfg["equity_slippage_bps"], cfg["commission_per_order"]
    )
    tqqq_bh_cash = capital - tqqq_bh_cost
    dca_shares = 0.0
    dca_cash = capital
    dca_daily = capital / len(rows)

    for i, row in enumerate(rows):
        prev = rows[i - 1] if i else None
        sig = classify(row, prev)
        q_close, t_close = row["QQQ"], row["TQQQ"]

        # A close-based signal cannot be filled at that same close. Execute only
        # the action queued by the prior trading session.
        if pending_action:
            action = pending_action
            pending_action = None
            if action["action"] == "entry" and tactical is None:
                equity_before = cash + core_shares * q_close
                tactical, cash = open_tactical(
                    row,
                    cash,
                    equity_before,
                    action["entry_type"],
                    action["alloc"],
                    action["signal_date"],
                    cfg,
                )
                if tactical:
                    transaction_costs += tactical["entry_execution_cost_total"]
            elif action["action"] in ("exit_full", "exit_partial") and tactical:
                fraction = 1.0 if action["action"] == "exit_full" else action["sell_pct"]
                status = "closed" if fraction >= 1.0 else "partial"
                proceeds, trade = close_tactical_piece(
                    tactical,
                    row["date"],
                    t_close,
                    tactical["shares"] * fraction,
                    status,
                    action["reason"],
                    action["signal_date"],
                    cfg,
                )
                cash += proceeds
                transaction_costs += trade["exit_execution_cost_dollars"]
                trades.append(trade)
                if action["action"] == "exit_partial":
                    tactical["partial_taken"] = True
                if tactical["shares"] <= 0.000001:
                    tactical = None

        if tactical:
            tactical["high_close"] = max(tactical["high_close"], t_close)
            drawdown = t_close / tactical["high_close"] - 1 if tactical["high_close"] else 0
            exit_reason = None
            if sig["trend_break"]:
                exit_reason = "trend_break"
            elif drawdown <= -cfg["trail_pct"]:
                exit_reason = f"trailing_stop_{int(cfg['trail_pct'] * 100)}pct"
            elif sig["overheat"] and cfg["overheat_exit"] == "full":
                exit_reason = "overheat_take_profit"
            if exit_reason:
                pending_action = {
                    "action": "exit_full",
                    "signal_date": row["date"],
                    "reason": exit_reason,
                }
            elif sig["overheat"] and cfg["overheat_exit"] == "partial" and not tactical["partial_taken"]:
                sell_pct = max(0.0, min(1.0, cfg["overheat_sell_pct"]))
                if sell_pct > 0:
                    pending_action = {
                        "action": "exit_partial" if sell_pct < 1 else "exit_full",
                        "signal_date": row["date"],
                        "reason": f"overheat_partial_{int(sell_pct * 100)}pct",
                        "sell_pct": sell_pct,
                    }

        if tactical is None and pending_action is None and i >= warmup:
            entry_type, alloc = None, 0.0
            if sig["near21"]:
                entry_type, alloc = "EMA21 tactical TQQQ", cfg["ema21_alloc"]
            elif sig["near8"]:
                entry_type, alloc = "EMA8 tactical TQQQ", cfg["ema8_alloc"]
            if entry_type:
                pending_action = {
                    "action": "entry",
                    "signal_date": row["date"],
                    "entry_type": entry_type,
                    "alloc": alloc,
                }

        if i == len(rows) - 1:
            # There is no next observed close at which to fill this signal.
            pending_at_end = pending_action
            pending_action = None
        else:
            pending_at_end = None

        tactical_value = tactical["shares"] * t_close if tactical else 0.0
        strategy_value = cash + core_shares * q_close + tactical_value
        dca_purchase, _, dca_cost = buy_with_budget(
            q_close, min(dca_daily, dca_cash), cfg["equity_slippage_bps"], cfg["commission_per_order"]
        )
        dca_shares += dca_purchase
        dca_cash -= dca_cost
        states.append({
            **sig,
            "date": row["date"],
            "action_for_next_close": pending_action["action"] if pending_action else None,
        })
        curve.append({
            "date": row["date"],
            "strategy": strategy_value,
            "cash": cash,
            "core_qqq_value": core_shares * q_close,
            "tactical_tqqq_value": tactical_value,
            "buy_hold_qqq": qqq_bh_cash + qqq_bh_shares * q_close,
            "daily_dca_qqq": dca_shares * q_close + dca_cash,
            "buy_hold_tqqq": tqqq_bh_cash + tqqq_bh_shares * t_close,
            "state": sig["state"],
        })

    if tactical:
        last = rows[-1]
        shares = tactical["shares"]
        marked_value = shares * last["TQQQ"]
        open_reason = "open_at_end"
        open_exit_signal = None
        if pending_at_end and pending_at_end["action"].startswith("exit"):
            open_reason = f"{pending_at_end['reason']}_signal_unfilled_at_end"
            open_exit_signal = pending_at_end["signal_date"]
        trades.append({
            "entry_signal_date": tactical["signal_date"],
            "entry_date": tactical["entry_date"],
            "exit_signal_date": open_exit_signal,
            "exit_date": last["date"],
            "type": tactical["type"],
            "status": "open_mark_to_market",
            "entry_price": tactical["entry_price"],
            "entry_reference_close": tactical["entry_reference_close"],
            "exit_price": last["TQQQ"],
            "exit_reference_close": last["TQQQ"],
            "shares": shares,
            "return_pct": (marked_value / tactical["cost"] - 1) * 100 if tactical["cost"] else None,
            "pnl_dollars": marked_value - tactical["cost"],
            "exit_reason": open_reason,
            "entry_execution_cost_dollars": tactical["entry_execution_cost_total"],
            "exit_execution_cost_dollars": 0.0,
            "fill_timing": "open_position_marked_at_final_close",
            "equity_slippage_bps_per_side": cfg["equity_slippage_bps"],
            "commission_per_order": cfg["commission_per_order"],
        })
    ccs_pnl = 0.0
    ccs_hedges = []
    if cfg.get("ccs_mode") and cfg["ccs_mode"] != "none":
        ccs_hedges = simulate_ccs(
            rows, states, hold_days=cfg["ccs_hold_days"], strike_mode=cfg["ccs_mode"],
            multiplier=cfg["ccs_multiplier"],
            execution_cost_per_contract=cfg["ccs_execution_cost_per_contract"],
            warmup=warmup,
        )
        ccs_pnl = sum(h["stylized_net_payoff_per_contract"] for h in ccs_hedges) * cfg["ccs_contracts"]
    final_value = curve[-1]["strategy"] if curve else capital
    values = [r["strategy"] for r in curve]
    start, end = rows[0]["date"], rows[-1]["date"]
    positions = {(t["entry_date"], t["type"]) for t in trades}
    total_return_pct = (final_value / capital - 1) * 100
    return {
        "config": cfg,
        "curve": curve,
        "trades": trades,
        "states": states,
        "ccs_hedges": ccs_hedges,
        "final_value": final_value,
        "total_return_pct": total_return_pct,
        "annualized_return_pct": annualized_return_pct(capital, final_value, start, end),
        "max_drawdown_pct": max_drawdown_from_capital(values, capital) * 100,
        "num_tactical_trades": len(positions),
        "num_tactical_exit_records": len(trades),
        "tactical_pnl_dollars": sum(float(t.get("pnl_dollars") or 0) for t in trades),
        "transaction_costs_dollars": transaction_costs,
        "ccs_stylized_payoff_dollars": ccs_pnl,
        "ccs_pnl_dollars": None,
        "combined_final_value": final_value,
        "combined_return_pct": total_return_pct,
        "return_includes_ccs": False,
        "decision_grade": False,
        "model_grade": MODEL_GRADE,
        "execution_model": "signal_at_close_filled_next_trading_close_with_adverse_slippage",
        "core_entry_price": core_fill,
    }


def run_backtest(prices, start="2026-01-01", end=None, capital=100000.0, warmup=34):
    rows = add_indicators(build_rows(prices, start=start, end=end))
    baseline = run_strategy_on_rows(rows, capital=capital, warmup=warmup)
    curve, trades, states = baseline["curve"], baseline["trades"], baseline["states"]

    ccs = simulate_ccs(rows, states, hold_days=5, strike_mode="spot_pct", warmup=warmup)
    ccs_grid, ccs_detail = run_ccs_grid(rows, states, warmup=warmup)
    summary = build_summary(rows, curve, trades, ccs, capital, warmup)
    sweep = run_strategy_sweep(rows, capital=capital, warmup=warmup)
    presets = preset_comparison(rows, capital=capital, warmup=warmup)
    return {
        "rows": rows, "curve": curve, "trades": trades, "ccs": ccs,
        "ccs_grid": ccs_grid, "ccs_detail": ccs_detail,
        "summary": summary, "states": states, "sweep": sweep, "presets": presets,
        "methodology": {
            "version": METHODOLOGY_VERSION,
            "model_grade": MODEL_GRADE,
            "decision_grade": False,
            "decision_grade_reason": (
                "Close-only proxy with next-close fills and no historical option chain; "
                "requires OHLC/option-chain data plus walk-forward validation before decision use."
            ),
            "signal_timing": "indicators computed at close_t; orders filled at close_t_plus_1",
            "annualization": "geometric CAGR using ACT/365.2425 calendar-day basis",
            "equity_slippage_bps_per_side": DEFAULT_EQUITY_SLIPPAGE_BPS,
            "commission_per_order": DEFAULT_COMMISSION_PER_ORDER,
            "ccs_execution_cost_per_contract_assumed": DEFAULT_CCS_EXECUTION_COST_PER_CONTRACT,
            "ccs_min_indicator_warmup_sessions": MIN_INDICATOR_WARMUP_SESSIONS,
            "ccs_included_in_strategy_returns": False,
            "parameter_selection": "in_sample_sweep_only",
            "limitations": LIMITATIONS,
        },
    }


def ccs_payoff_per_contract(expiry_close, short_call, width=1.0, credit=0.25):
    intrinsic = min(max(expiry_close - short_call, 0.0), width)
    return (credit - intrinsic) * 100


def ccs_credit_for_hold(hold_days):
    # Synthetic scenario input only; never treat this as a historical premium.
    return round(min(0.49, 0.25 * math.sqrt(hold_days / 5.0)), 2)


def nearest_strike(x, increment=1.0):
    return round(round(x / increment) * increment, 2)


def ccs_short_call(row, strike_mode="spot_pct", multiplier=4.0, floor_pct=0.045):
    spot = row["TQQQ"]
    if strike_mode == "spot_pct":
        target = spot * (1 + floor_pct)
        source = "TQQQ spot + 4.5%"
    elif strike_mode in ("ema21_4atr_floor", "ema21_4atr_skip"):
        q_target = row["ema21"] + multiplier * row["atr14"]
        q_move = q_target / row["QQQ"] - 1 if row["QQQ"] else 0.0
        mapped_target = spot * (1 + 3 * q_move)
        floor = spot * (1 + floor_pct)
        if strike_mode == "ema21_4atr_skip" and mapped_target <= spot:
            return None
        target = max(mapped_target, floor) if strike_mode == "ema21_4atr_floor" else mapped_target
        source = f"QQQ EMA21 + {multiplier:g}*ATR mapped to TQQQ"
    else:
        raise ValueError(f"Unknown CCS strike_mode: {strike_mode}")
    short_call = nearest_strike(target, 1.0)
    if short_call <= spot:
        short_call = math.floor(spot) + 1.0
    return {
        "short_call": round(short_call, 2),
        "raw_target": round(target, 2),
        "qqq_band": round(row["ema21"] + multiplier * row["atr14"], 2),
        "source": source,
    }


def simulate_ccs(
    rows,
    states,
    hold_days=5,
    credit=None,
    strike_mode="spot_pct",
    multiplier=4.0,
    floor_pct=0.045,
    execution_cost_per_contract=DEFAULT_CCS_EXECUTION_COST_PER_CONTRACT,
    warmup=MIN_INDICATOR_WARMUP_SESSIONS,
):
    if len(states) != len(rows):
        raise ValueError("states must align one-to-one with rows.")
    if not isinstance(hold_days, int) or hold_days <= 0:
        raise ValueError("hold_days must be a positive integer.")
    if execution_cost_per_contract < 0 or not math.isfinite(execution_cost_per_contract):
        raise ValueError("execution_cost_per_contract must be non-negative and finite.")
    if not isinstance(warmup, int) or warmup < 0:
        raise ValueError("warmup must be a non-negative integer.")
    credit = ccs_credit_for_hold(hold_days) if credit is None else credit
    if not math.isfinite(credit) or not 0 <= credit < 1.0:
        raise ValueError("credit must be between zero and the $1 spread width.")
    hedges = []
    next_available = 0
    effective_warmup = max(warmup, MIN_INDICATOR_WARMUP_SESSIONS)
    for signal_i, signal_row in enumerate(rows):
        entry_i = signal_i + 1
        expiry_i = entry_i + hold_days
        if entry_i < next_available:
            continue
        if signal_i < effective_warmup:
            continue
        indicator_values = [
            signal_row.get(key)
            for key in ("ema8", "ema13", "ema21", "ema34", "ema55", "atr14")
        ]
        try:
            indicators_ready = all(
                value is not None and math.isfinite(float(value))
                for value in indicator_values
            )
        except (TypeError, ValueError):
            indicators_ready = False
        if not indicators_ready:
            continue
        if not states[signal_i]["overheat"]:
            continue
        # Do not shorten an intended holding period at the dataset boundary.
        if expiry_i >= len(rows):
            continue
        entry_row = rows[entry_i]
        expiry_row = rows[expiry_i]
        strike = ccs_short_call(
            signal_row,
            strike_mode=strike_mode,
            multiplier=multiplier,
            floor_pct=floor_pct,
        )
        if not strike:
            continue
        short_call = strike["short_call"]
        long_call = round(short_call + 1.0, 2)
        gross_payoff = ccs_payoff_per_contract(expiry_row["TQQQ"], short_call, 1.0, credit)
        net_payoff = gross_payoff - execution_cost_per_contract
        hedges.append({
            "signal_date": signal_row["date"],
            "entry_date": entry_row["date"],
            "expiry_date": expiry_row["date"],
            "tqqq_signal_close": signal_row["TQQQ"],
            "tqqq_entry_close": entry_row["TQQQ"],
            "tqqq_expiry_close": expiry_row["TQQQ"],
            "short_call": short_call,
            "long_call": long_call,
            "credit_per_share_assumed": credit,
            "premium_source": "synthetic_formula_not_historical_option_chain",
            "hold_days": hold_days,
            "strike_mode": strike_mode,
            "raw_target": strike["raw_target"],
            "qqq_ema21_plus_atr_band": strike["qqq_band"],
            "atr_multiplier": multiplier,
            "strike_source": strike["source"],
            "fill_timing": "signal_close_then_next_trading_close_scenario_entry",
            "signal_warmup_sessions": effective_warmup,
            "gross_expiration_payoff_per_contract": gross_payoff,
            "execution_cost_per_contract_assumed": execution_cost_per_contract,
            "stylized_net_payoff_per_contract": net_payoff,
            "stylized_max_gain_per_contract": credit * 100 - execution_cost_per_contract,
            "stylized_max_loss_per_contract": (1.0 - credit) * 100 + execution_cost_per_contract,
            # Backward-compatible alias. This remains explicitly stylized, not historical P&L.
            "pnl_per_contract": net_payoff,
            "decision_grade": False,
            "win": net_payoff > 0,
        })
        next_available = expiry_i + 1
    return hedges


def summarize_ccs(hedges, strike_mode, hold_days, credit, multiplier=None):
    wins = sum(1 for h in hedges if h["win"])
    payoff = sum(h["stylized_net_payoff_per_contract"] for h in hedges)
    trades = len(hedges)
    return {
        "strike_mode": strike_mode,
        "multiplier": multiplier,
        "hold_days": hold_days,
        "credit_per_share_assumed": credit,
        "num_hedges": trades,
        "wins": wins,
        "losses": trades - wins,
        "win_rate_pct": (wins / trades * 100) if trades else None,
        "total_stylized_payoff_per_contract": payoff,
        "avg_stylized_payoff_per_contract": (payoff / trades) if trades else None,
        # Backward-compatible aliases for existing CSV consumers.
        "total_pnl_per_contract": payoff,
        "avg_pnl_per_contract": (payoff / trades) if trades else None,
        "decision_grade": False,
    }


def run_ccs_grid(rows, states, warmup=MIN_INDICATOR_WARMUP_SESSIONS):
    hold_days_grid = [3, 5, 10, 15, 21]
    ema_multipliers = [3.5, 4.0, 4.5]
    summary, detail = [], {}
    for hold_days in hold_days_grid:
        credit = ccs_credit_for_hold(hold_days)
        hedges = simulate_ccs(
            rows, states, hold_days=hold_days, credit=credit,
            strike_mode="spot_pct", warmup=warmup,
        )
        summary.append(summarize_ccs(hedges, "spot_pct", hold_days, credit))
        detail[f"spot_pct_{hold_days}d"] = hedges
    for mode in ("ema21_4atr_floor", "ema21_4atr_skip"):
        for multiplier in ema_multipliers:
            for hold_days in hold_days_grid:
                credit = ccs_credit_for_hold(hold_days)
                hedges = simulate_ccs(
                    rows, states, hold_days=hold_days, credit=credit,
                    strike_mode=mode, multiplier=multiplier, warmup=warmup,
                )
                summary.append(summarize_ccs(hedges, mode, hold_days, credit, multiplier))
                key = f"{mode}_{str(multiplier).replace('.', 'p')}x_{hold_days}d"
                detail[key] = hedges
    return summary, detail


def strategy_result_row(module, variant, result):
    cfg = result["config"]
    return {
        "module": module,
        "variant": variant,
        "core_alloc": cfg["core_alloc"],
        "ema8_alloc": cfg["ema8_alloc"],
        "ema21_alloc": cfg["ema21_alloc"],
        "overheat_exit": cfg["overheat_exit"],
        "overheat_sell_pct": cfg["overheat_sell_pct"],
        "trail_pct": cfg["trail_pct"],
        "ccs_mode": cfg["ccs_mode"],
        "ccs_hold_days": cfg["ccs_hold_days"],
        "ccs_multiplier": cfg["ccs_multiplier"],
        "final_value": result["final_value"],
        "total_return_pct": result["total_return_pct"],
        "max_drawdown_pct": result["max_drawdown_pct"],
        "num_tactical_trades": result["num_tactical_trades"],
        "tactical_pnl_dollars": result["tactical_pnl_dollars"],
        "transaction_costs_dollars": result["transaction_costs_dollars"],
        "ccs_stylized_payoff_dollars": result["ccs_stylized_payoff_dollars"],
        "ccs_pnl_dollars": result["ccs_pnl_dollars"],
        "combined_final_value": result["combined_final_value"],
        "combined_return_pct": result["combined_return_pct"],
        "teacher_aligned": cfg["overheat_exit"] in ("full", "partial"),
        "return_includes_ccs": False,
        "decision_grade": False,
    }


def run_variant(rows, capital, warmup, module, variant, **overrides):
    result = run_strategy_on_rows(rows, capital=capital, warmup=warmup, config=strategy_config(**overrides))
    return strategy_result_row(module, variant, result)


def run_strategy_sweep(rows, capital=100000.0, warmup=34):
    module_rows = []
    full_rows = []
    teacher_base = strategy_config(
        name="teacher_base_80_core_partial_runner",
        core_alloc=0.80,
        ema8_alloc=0.15,
        ema21_alloc=0.30,
        overheat_exit="partial",
        overheat_sell_pct=0.50,
        trail_pct=0.08,
    )

    for core_alloc in (0.70, 0.80, 0.90):
        cfg = dict(teacher_base, core_alloc=core_alloc, ccs_mode="none")
        module_rows.append(run_variant(rows, capital, warmup, "core_exposure", f"core_{int(core_alloc * 100)}", **cfg))

    for ema8_alloc, ema21_alloc in ((0.10, 0.20), (0.15, 0.30), (0.20, 0.40)):
        cfg = dict(teacher_base, ema8_alloc=ema8_alloc, ema21_alloc=ema21_alloc, ccs_mode="none")
        module_rows.append(run_variant(
            rows, capital, warmup, "tqqq_sizing",
            f"ema8_{int(ema8_alloc * 100)}_ema21_{int(ema21_alloc * 100)}", **cfg,
        ))

    exit_variants = [
        ("full_overheat_8trail", {"overheat_exit": "full", "overheat_sell_pct": 1.0, "trail_pct": 0.08}),
        ("partial50_8trail", {"overheat_exit": "partial", "overheat_sell_pct": 0.50, "trail_pct": 0.08}),
        ("partial70_8trail", {"overheat_exit": "partial", "overheat_sell_pct": 0.70, "trail_pct": 0.08}),
        ("runner_only_8trail", {"overheat_exit": "none", "overheat_sell_pct": 0.0, "trail_pct": 0.08}),
        ("partial50_10trail", {"overheat_exit": "partial", "overheat_sell_pct": 0.50, "trail_pct": 0.10}),
        ("partial50_12trail", {"overheat_exit": "partial", "overheat_sell_pct": 0.50, "trail_pct": 0.12}),
    ]
    for name, changes in exit_variants:
        cfg = dict(teacher_base, ccs_mode="none")
        cfg.update(changes)
        module_rows.append(run_variant(rows, capital, warmup, "exit_policy", name, **cfg))

    ccs_variants = [
        ("no_ccs", {"ccs_mode": "none"}),
        ("spot_5d", {"ccs_mode": "spot_pct", "ccs_hold_days": 5}),
        ("ema21_4atr_skip_5d", {"ccs_mode": "ema21_4atr_skip", "ccs_hold_days": 5, "ccs_multiplier": 4.0}),
        ("ema21_4atr_skip_10d", {"ccs_mode": "ema21_4atr_skip", "ccs_hold_days": 10, "ccs_multiplier": 4.0}),
        ("ema21_4atr_skip_21d", {"ccs_mode": "ema21_4atr_skip", "ccs_hold_days": 21, "ccs_multiplier": 4.0}),
    ]
    for name, changes in ccs_variants:
        cfg = dict(teacher_base)
        cfg.update(changes)
        module_rows.append(run_variant(rows, capital, warmup, "ccs_overlay", name, **cfg))

    size_grid = [(0.10, 0.20), (0.15, 0.30), (0.20, 0.40)]
    exit_grid = [
        ("full8", {"overheat_exit": "full", "overheat_sell_pct": 1.0, "trail_pct": 0.08}),
        ("partial50_8", {"overheat_exit": "partial", "overheat_sell_pct": 0.50, "trail_pct": 0.08}),
        ("partial70_8", {"overheat_exit": "partial", "overheat_sell_pct": 0.70, "trail_pct": 0.08}),
        ("runner8", {"overheat_exit": "none", "overheat_sell_pct": 0.0, "trail_pct": 0.08}),
        ("partial50_10", {"overheat_exit": "partial", "overheat_sell_pct": 0.50, "trail_pct": 0.10}),
    ]
    ccs_grid = [
        ("no_ccs", {"ccs_mode": "none"}),
        ("skip10", {"ccs_mode": "ema21_4atr_skip", "ccs_hold_days": 10, "ccs_multiplier": 4.0}),
    ]
    for core_alloc in (0.70, 0.80, 0.90):
        for ema8_alloc, ema21_alloc in size_grid:
            for exit_name, exit_changes in exit_grid:
                for ccs_name, ccs_changes in ccs_grid:
                    cfg = strategy_config(
                        core_alloc=core_alloc,
                        ema8_alloc=ema8_alloc,
                        ema21_alloc=ema21_alloc,
                    )
                    cfg.update(exit_changes)
                    cfg.update(ccs_changes)
                    variant = (
                        f"core{int(core_alloc * 100)}_e8{int(ema8_alloc * 100)}_"
                        f"e21{int(ema21_alloc * 100)}_{exit_name}_{ccs_name}"
                    )
                    full_rows.append(run_variant(rows, capital, warmup, "full_grid", variant, **cfg))

    full_rows.sort(key=lambda r: r["combined_return_pct"], reverse=True)
    teacher_rows = [r for r in full_rows if r["teacher_aligned"] and r["overheat_exit"] == "partial"]
    teacher_rows.sort(key=lambda r: r["combined_return_pct"], reverse=True)
    return {
        "module_rows": module_rows,
        "full_rows": full_rows,
        "best_overall": full_rows[0] if full_rows else None,
        "best_teacher_aligned": teacher_rows[0] if teacher_rows else None,
    }


def benchmark_curve(
    rows,
    capital,
    mode,
    slippage_bps=DEFAULT_EQUITY_SLIPPAGE_BPS,
    commission_per_order=DEFAULT_COMMISSION_PER_ORDER,
):
    if mode == "buy_hold_qqq":
        shares, _, cost = buy_with_budget(rows[0]["QQQ"], capital, slippage_bps, commission_per_order)
        cash = capital - cost
        return [cash + shares * r["QQQ"] for r in rows]
    if mode == "daily_dca_qqq":
        shares = 0.0
        cash = capital
        daily = capital / len(rows)
        out = []
        for r in rows:
            purchased, _, cost = buy_with_budget(
                r["QQQ"], min(daily, cash), slippage_bps, commission_per_order
            )
            shares += purchased
            cash -= cost
            out.append(shares * r["QQQ"] + cash)
        return out
    raise ValueError(f"Unknown benchmark mode: {mode}")


def preset_comparison(rows, capital=100000.0, warmup=34):
    start, end = rows[0]["date"], rows[-1]["date"]
    presets = [
        ("High-core runner preset", strategy_config(
            name="core90_e810_e2120_runner8_skip10",
            core_alloc=0.90, ema8_alloc=0.10, ema21_alloc=0.20,
            overheat_exit="none", overheat_sell_pct=0.0, trail_pct=0.08,
            ccs_mode="ema21_4atr_skip", ccs_hold_days=10, ccs_multiplier=4.0,
        )),
        ("Teacher-aligned version", strategy_config(
            name="core90_e810_e2120_partial50_8_skip10",
            core_alloc=0.90, ema8_alloc=0.10, ema21_alloc=0.20,
            overheat_exit="partial", overheat_sell_pct=0.50, trail_pct=0.08,
            ccs_mode="ema21_4atr_skip", ccs_hold_days=10, ccs_multiplier=4.0,
        )),
    ]
    out = []
    for name, cfg in presets:
        res = run_strategy_on_rows(rows, capital=capital, warmup=warmup, config=cfg)
        out.append({
            "scenario": name,
            "start_date": start,
            "end_date": end,
            "final_value": res["combined_final_value"],
            "total_return_pct": res["combined_return_pct"],
            "annualized_return_pct": annualized_return_pct(capital, res["combined_final_value"], start, end),
            "max_drawdown_pct": res["max_drawdown_pct"],
            "num_tactical_trades": res["num_tactical_trades"],
            "tactical_pnl_dollars": res["tactical_pnl_dollars"],
            "transaction_costs_dollars": res["transaction_costs_dollars"],
            "ccs_stylized_payoff_dollars": res["ccs_stylized_payoff_dollars"],
            "ccs_pnl_dollars": res["ccs_pnl_dollars"],
            "return_includes_ccs": False,
            "decision_grade": False,
            "config": cfg["name"] if cfg.get("name") else name,
        })
    for label, mode in (
        ("Buy & hold QQQ", "buy_hold_qqq"),
        ("Fixed-horizon daily DCA QQQ", "daily_dca_qqq"),
    ):
        values = benchmark_curve(rows, capital, mode)
        final = values[-1]
        out.append({
            "scenario": label,
            "start_date": start,
            "end_date": end,
            "final_value": final,
            "total_return_pct": (final / capital - 1) * 100,
            "annualized_return_pct": annualized_return_pct(capital, final, start, end),
            "max_drawdown_pct": max_drawdown_from_capital(values, capital) * 100,
            "num_tactical_trades": 0,
            "tactical_pnl_dollars": 0.0,
            "transaction_costs_dollars": None,
            "ccs_stylized_payoff_dollars": 0.0,
            "ccs_pnl_dollars": 0.0,
            "return_includes_ccs": False,
            "decision_grade": False,
            "config": mode,
        })
    return out


def build_summary(rows, curve, trades, ccs, capital, warmup):
    start, end = rows[0]["date"], rows[-1]["date"]
    fields = [
        ("Teacher-style 70% QQQ core + TQQQ tactical", "strategy"),
        ("Buy & hold QQQ", "buy_hold_qqq"),
        ("Fixed-horizon daily DCA QQQ", "daily_dca_qqq"),
        ("Buy & hold TQQQ", "buy_hold_tqqq"),
    ]
    out = []
    for name, key in fields:
        values = [r[key] for r in curve]
        final = values[-1]
        out.append({
            "scenario": name,
            "start_date": start,
            "end_date": end,
            "final_value": final,
            "total_return_pct": (final / capital - 1) * 100,
            "annualized_return_pct": annualized_return_pct(capital, final, start, end),
            "max_drawdown_pct": max_drawdown_from_capital(values, capital) * 100,
            "num_tactical_trades": (
                len({(t["entry_date"], t["type"]) for t in trades}) if key == "strategy" else 0
            ),
            "data_note": (
                f"research-only close proxy; signals fill next close; {DEFAULT_EQUITY_SLIPPAGE_BPS:g} bps "
                f"adverse slippage per side; first {warmup} sessions block tactical entries"
            ),
            "decision_grade": False,
        })
    ccs_total = sum(h["stylized_net_payoff_per_contract"] for h in ccs)
    out.append({
        "scenario": "Stylized TQQQ 1-wide CCS payoff diagnostic",
        "start_date": start,
        "end_date": end,
        "final_value": None,
        "total_return_pct": None,
        "annualized_return_pct": None,
        "max_drawdown_pct": None,
        "num_tactical_trades": len(ccs),
        "stylized_payoff_dollars": ccs_total,
        "data_note": (
            "not historical P&L or a return; synthetic premium, next-close scenario entry, complete 5-session "
            f"holding periods only, and ${DEFAULT_CCS_EXECUTION_COST_PER_CONTRACT:.2f} assumed execution cost"
        ),
        "decision_grade": False,
    })
    return out


def write_csv(path, rows, fields=None):
    fields = fields or (list(rows[0].keys()) if rows else [])
    if not rows:
        if fields:
            atomic_write_csv(path, [], fields)
        else:
            atomic_write_text(path, "")
        return
    atomic_write_csv(path, ({k: row.get(k) for k in fields} for row in rows), fields)


def fmt_money(v):
    if v is None:
        return "-"
    return f"${v:,.0f}"


def fmt_pct(v):
    if v is None:
        return "-"
    return f"{v:+.2f}%"


def scale_points(items, x_key, y_key, x_min, x_max, y_min, y_max, w, h, left, right, top, bottom):
    pts = []
    denom_x = max(x_max - x_min, 1)
    denom_y = y_max - y_min if y_max != y_min else 1
    for r in items:
        x = left + (r[x_key] - x_min) / denom_x * (w - left - right)
        y = h - bottom - (r[y_key] - y_min) / denom_y * (h - top - bottom)
        pts.append(f"{x:.1f},{y:.1f}")
    return " ".join(pts)


def line_svg(series, title, height=360):
    width = 980
    left, right, top, bottom = 56, 34, 78, 44
    indexed = []
    vals = []
    for i, row in enumerate(series["rows"]):
        point = {"i": i}
        for key in series["keys"]:
            point[key] = row[key]
            vals.append(row[key])
        indexed.append(point)
    y_min, y_max = min(vals), max(vals)
    margin = (y_max - y_min) * 0.08 or 1
    y_min -= margin
    y_max += margin
    palette = ["#2563eb", "#f97316", "#16a34a", "#64748b", "#dc2626"]
    lines = []
    for idx, key in enumerate(series["keys"]):
        pts = scale_points(indexed, "i", key, 0, len(indexed) - 1, y_min, y_max, width, height, left, right, top, bottom)
        lines.append(f'<polyline points="{pts}" fill="none" stroke="{palette[idx % len(palette)]}" stroke-width="2.4" />')
    grid = []
    for j in range(5):
        y = top + j * (height - top - bottom) / 4
        val = y_max - j * (y_max - y_min) / 4
        grid.append(f'<line x1="{left}" x2="{width-right}" y1="{y:.1f}" y2="{y:.1f}" stroke="#e2e8f0" />')
        grid.append(f'<text x="8" y="{y+4:.1f}" font-size="12" fill="#64748b">{val:,.0f}</text>')
    legend = []
    for idx, label in enumerate(series["labels"]):
        x = left + idx * 178
        legend.append(f'<circle cx="{x}" cy="52" r="5" fill="{palette[idx % len(palette)]}" />')
        legend.append(f'<text x="{x+10}" y="56" font-size="13" fill="#334155">{html.escape(label)}</text>')
    start_label = html.escape(series["rows"][0]["date"])
    end_label = html.escape(series["rows"][-1]["date"])
    return f'''<svg viewBox="0 0 {width} {height}" role="img" aria-label="{html.escape(title)}">
<rect width="{width}" height="{height}" rx="8" fill="#fff" />
<text x="{left}" y="24" font-size="16" font-weight="700" fill="#0f172a">{html.escape(title)}</text>
{''.join(legend)}
{''.join(grid)}
{''.join(lines)}
<text x="{left}" y="{height-12}" font-size="12" fill="#64748b">{start_label}</text>
<text x="{width-right-72}" y="{height-12}" font-size="12" fill="#64748b">{end_label}</text>
</svg>'''


def qqq_signal_svg(rows, trades, height=390):
    plot_rows = [{"date": r["date"], "QQQ": r["QQQ"], "EMA8": r["ema8"], "EMA21": r["ema21"], "EMA34": r["ema34"]} for r in rows]
    base = line_svg({"rows": plot_rows, "keys": ["QQQ", "EMA8", "EMA21", "EMA34"], "labels": ["QQQ close", "EMA8", "EMA21", "EMA34"]}, "QQQ Daily Weather Map", height)
    markers = []
    width = 980
    left, right, top, bottom = 56, 34, 78, 44
    values = [v for r in plot_rows for v in (r["QQQ"], r["EMA8"], r["EMA21"], r["EMA34"])]
    y_min, y_max = min(values), max(values)
    margin = (y_max - y_min) * 0.08 or 1
    y_min -= margin
    y_max += margin
    date_to_i = {r["date"]: i for i, r in enumerate(rows)}
    q_by_date = {r["date"]: r["QQQ"] for r in rows}
    for tr in trades:
        for date_key, color, label in (("entry_date", "#16a34a", "B"), ("exit_date", "#dc2626", "S")):
            d = tr.get(date_key)
            if d not in date_to_i:
                continue
            i = date_to_i[d]
            x = left + i / max(len(rows) - 1, 1) * (width - left - right)
            y = height - bottom - (q_by_date[d] - y_min) / (y_max - y_min) * (height - top - bottom)
            markers.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="7" fill="{color}" stroke="#fff" stroke-width="2" />')
            markers.append(f'<text x="{x-4:.1f}" y="{y+4:.1f}" font-size="10" font-weight="700" fill="#fff">{label}</text>')
    return base.replace("</svg>", "".join(markers) + "</svg>")


def write_report(result, out_dir, capital):
    rows, curve, trades, ccs, summary = (result[k] for k in ("rows", "curve", "trades", "ccs", "summary"))
    equity_svg = line_svg({
        "rows": curve,
        "keys": ["strategy", "buy_hold_qqq", "daily_dca_qqq", "buy_hold_tqqq"],
        "labels": ["Teacher-style", "QQQ buy/hold", "QQQ fixed-horizon DCA", "TQQQ buy/hold"],
    }, "YTD Equity Curve")
    signal_svg = qqq_signal_svg(rows, trades)
    atomic_write_text(out_dir / "equity_curve.svg", equity_svg)
    atomic_write_text(out_dir / "qqq_signal_chart.svg", signal_svg)
    summary_rows = "".join(
        f"<tr><td>{html.escape(r['scenario'])}</td><td>{fmt_money(r['final_value'])}</td>"
        f"<td>{fmt_pct(r['total_return_pct'])}</td><td>{fmt_pct(r['annualized_return_pct'])}</td>"
        f"<td>{fmt_pct(r['max_drawdown_pct'])}</td><td>{r['num_tactical_trades']}</td>"
        f"<td>{fmt_money(r.get('stylized_payoff_dollars'))}</td></tr>"
        for r in summary
    )
    preset_rows = "".join(
        f"<tr><td>{html.escape(r['scenario'])}</td><td>{html.escape(r['config'])}</td>"
        f"<td>{fmt_money(r['final_value'])}</td><td>{fmt_pct(r['total_return_pct'])}</td>"
        f"<td>{fmt_pct(r['annualized_return_pct'])}</td><td>{fmt_pct(r['max_drawdown_pct'])}</td>"
        f"<td>{r['num_tactical_trades']}</td><td>{fmt_money(r['tactical_pnl_dollars'])}</td>"
        f"<td>{fmt_money(r['ccs_stylized_payoff_dollars'])}</td></tr>"
        for r in (result.get("presets") or [])
    )
    trade_rows = "".join(
        f"<tr><td>{t.get('entry_signal_date') or '-'}</td><td>{t['entry_date']}</td>"
        f"<td>{t.get('exit_signal_date') or '-'}</td><td>{t['exit_date']}</td><td>{html.escape(t['type'])}</td>"
        f"<td>{t['entry_price']:.2f}</td><td>{t['exit_price']:.2f}</td><td>{fmt_pct(t['return_pct'])}</td>"
        f"<td>{fmt_money(t['pnl_dollars'])}</td><td>{html.escape(t['exit_reason'])}</td></tr>"
        for t in trades
    ) or '<tr><td colspan="10">No tactical TQQQ trades fired under the close-only rules.</td></tr>'
    ccs_rows = "".join(
        f"<tr><td>{h['signal_date']}</td><td>{h['entry_date']}</td><td>{h['expiry_date']}</td><td>{h['tqqq_entry_close']:.2f}</td>"
        f"<td>{h['short_call']:.2f}/{h['long_call']:.2f}</td><td>{h['tqqq_expiry_close']:.2f}</td>"
        f"<td>{fmt_money(h['stylized_net_payoff_per_contract'])}</td>"
        f"<td>{fmt_money(h['stylized_max_gain_per_contract'])}</td>"
        f"<td>{fmt_money(-h['stylized_max_loss_per_contract'])}</td>"
        f"<td>{'Positive' if h['win'] else 'Negative'}</td></tr>"
        for h in ccs
    ) or '<tr><td colspan="10">No complete overheat CCS scenario windows fired.</td></tr>'
    grid_rows = "".join(
        f"<tr><td>{html.escape(g['strike_mode'])}</td><td>{g['multiplier'] if g['multiplier'] is not None else '-'}</td><td>{g['hold_days']}</td>"
        f"<td>${g['credit_per_share_assumed']:.2f}</td><td>{g['num_hedges']}</td>"
        f"<td>{g['wins']}/{g['losses']}</td><td>{fmt_pct(g['win_rate_pct'])}</td>"
        f"<td>{fmt_money(g['total_stylized_payoff_per_contract'])}</td>"
        f"<td>{fmt_money(g['avg_stylized_payoff_per_contract'])}</td></tr>"
        for g in result["ccs_grid"]
    )
    sweep = result.get("sweep") or {}
    best_teacher = sweep.get("best_teacher_aligned")
    best_overall = sweep.get("best_overall")
    def sweep_tr(r):
        return (
            f"<tr><td>{html.escape(r['module'])}</td><td>{html.escape(r['variant'])}</td>"
            f"<td>{int(r['core_alloc'] * 100)}%</td><td>{int(r['ema8_alloc'] * 100)}%/{int(r['ema21_alloc'] * 100)}%</td>"
            f"<td>{html.escape(r['overheat_exit'])} {int(r['overheat_sell_pct'] * 100)}%</td>"
            f"<td>{int(r['trail_pct'] * 100)}%</td><td>{html.escape(r['ccs_mode'])} {r['ccs_hold_days']}d</td>"
            f"<td>{fmt_pct(r['combined_return_pct'])}</td><td>{fmt_pct(r['max_drawdown_pct'])}</td>"
            f"<td>{fmt_money(r['ccs_stylized_payoff_dollars'])}</td></tr>"
        )
    module_rows_html = "".join(sweep_tr(r) for r in (sweep.get("module_rows") or []))
    top_rows_html = "".join(sweep_tr(r) for r in (sweep.get("full_rows") or [])[:12])
    best_teacher_html = ""
    if best_teacher:
        best_teacher_html = (
            f"<div class=\"note\"><b>Highest in-sample teacher-aligned row (selection-biased):</b> {html.escape(best_teacher['variant'])} · "
            f"return {fmt_pct(best_teacher['combined_return_pct'])}, max DD {fmt_pct(best_teacher['max_drawdown_pct'])}, "
            f"synthetic CCS diagnostic {fmt_money(best_teacher['ccs_stylized_payoff_dollars'])}. "
            f"Highest raw in-sample return: {html.escape(best_overall['variant']) if best_overall else '-'}. "
            "No row is validated out of sample.</div>"
        )
    playbook_cards = [
        ("QQQ is weather", "Only QQQ daily trend defines the regime: EMA8/13/21 stack, EMA21 slope, ATR distance, and 5-day momentum. RSI is reference-only."),
        ("TQQQ is execution", "Use TQQQ only at healthy EMA8/EMA21 pullbacks; do not let TQQQ noise define the market view."),
        ("CCS needs chain data", "A defined-risk overlay still requires live strikes, executable credit, liquidity, max loss, and long-exposure context."),
        ("Profit needs a leash", "At overheat, take partial profits and leave a runner with trailing stop instead of all-or-nothing exits."),
    ]
    playbook_html = "".join(
        f"<div class=\"play\"><div class=\"k\">{html.escape(k)}</div><div>{html.escape(v)}</div></div>"
        for k, v in playbook_cards
    )
    decision_rows = [
        ("QQQ above EMA21, EMA8>13>21", "Hold core QQQ; wait for pullbacks before new TQQQ."),
        ("QQQ pulls to EMA8 zone", "Small TQQQ tactical add; avoid chasing options."),
        ("QQQ pulls to EMA21 zone", "If EMA21 still rises, this is the higher-quality TQQQ/call-debit-spread area."),
        ("QQQ > EMA8 + 1.5 ATR or 5-day momentum extended", "Move stops up; sell part of TQQQ; evaluate CCS only with an actual liquid option chain."),
        ("Two closes below EMA21 or EMA8<EMA21", "Stop adding TQQQ/calls; treat it as regime risk, not a normal dip."),
    ]
    decision_html = "".join(
        f"<tr><td>{html.escape(a)}</td><td>{html.escape(b)}</td></tr>"
        for a, b in decision_rows
    )
    lessons = [
        "All tactical signals are observed at one close and executed at the following close with adverse slippage.",
        "Reported return and drawdown exclude every CCS diagnostic because no historical option-chain premium is available.",
        "The parameter sweep is in-sample exploration; its top row is not evidence of future superiority.",
        "Annualized values can magnify short-period results; total return and max drawdown are the primary period metrics.",
        "Decision-grade testing still requires OHLC, corporate-action-verified prices, historical option quotes, and walk-forward validation.",
    ]
    lessons_html = "".join(f"<li>{html.escape(x)}</li>" for x in lessons)
    strat = summary[0]
    qqq = summary[1]
    diff_return = (strat["total_return_pct"] or 0) - (qqq["total_return_pct"] or 0)
    diff_dd = (strat["max_drawdown_pct"] or 0) - (qqq["max_drawdown_pct"] or 0)
    limitations_html = "".join(f"<li>{html.escape(x)}</li>" for x in result["methodology"]["limitations"])
    html_doc = f'''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>QQQ/TQQQ Strategy Backtest</title>
<style>
body{{margin:0;background:#f8fafc;color:#0f172a;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif}}
main{{max-width:1160px;margin:0 auto;padding:28px 20px 44px}}
h1{{font-size:28px;margin:0 0 8px}} h2{{font-size:18px;margin:28px 0 12px}}
.sub{{color:#64748b;margin:0 0 20px;line-height:1.55}}
.cards{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin:18px 0}}
.card{{background:#fff;border:1px solid #e2e8f0;border-radius:8px;padding:14px;box-shadow:0 1px 2px rgba(15,23,42,.04)}}
.playbook{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin:14px 0}}
.play{{background:#fff;border:1px solid #dbeafe;border-radius:8px;padding:13px;line-height:1.45;color:#334155}}
.k{{font-size:12px;color:#64748b;text-transform:uppercase;letter-spacing:.04em}} .v{{font-size:22px;font-weight:800;margin-top:6px}}
.good{{color:#15803d}} .bad{{color:#b91c1c}} .warn{{color:#b45309}}
.panel{{background:#fff;border:1px solid #e2e8f0;border-radius:8px;padding:16px;margin-top:14px;overflow:auto}}
table{{width:100%;border-collapse:collapse;font-size:13px}} th,td{{padding:10px;border-bottom:1px solid #e2e8f0;text-align:left;white-space:nowrap}} th{{color:#475569;background:#f8fafc}}
.note{{font-size:13px;color:#475569;line-height:1.6;background:#fff7ed;border:1px solid #fed7aa;border-radius:8px;padding:12px 14px}}
.lessons{{margin:0;padding-left:20px;color:#334155;line-height:1.65;font-size:14px}}
svg{{width:100%;height:auto;display:block}}
@media(max-width:850px){{.cards,.playbook{{grid-template-columns:repeat(2,minmax(0,1fr))}}}}
@media(max-width:560px){{main{{padding:20px 12px}}.cards{{grid-template-columns:1fr}}th,td{{padding:8px}}}}
@media(max-width:560px){{.playbook{{grid-template-columns:1fr}}}}
</style>
</head>
<body><main>
<h1>QQQ/TQQQ Strategy Backtest</h1>
<p class="sub">Period: {rows[0]['date']} to {rows[-1]['date']} · Capital: {fmt_money(capital)} · Data: adjusted daily closes / cached closes.</p>
<div class="note"><b>RESEARCH ONLY · NOT DECISION GRADE.</b> Signals at close t fill at close t+1 with {DEFAULT_EQUITY_SLIPPAGE_BPS:g} bps adverse slippage per side. CCS figures use synthetic credits and are excluded from every strategy return, drawdown, and ranking claim.</div>
<h2>Teacher Strategy Distilled</h2>
<div class="playbook">{playbook_html}</div>
<section class="panel"><table><thead><tr><th>Market condition</th><th>Decision rule</th></tr></thead><tbody>{decision_html}</tbody></table></section>
<div class="cards">
<div class="card"><div class="k">Strategy Final</div><div class="v">{fmt_money(strat['final_value'])}</div></div>
<div class="card"><div class="k">Strategy Return</div><div class="v good">{fmt_pct(strat['total_return_pct'])}</div></div>
<div class="card"><div class="k">Vs QQQ Return</div><div class="v {'good' if diff_return >= 0 else 'bad'}">{diff_return:+.2f} pts</div></div>
<div class="card"><div class="k">Vs QQQ Max DD</div><div class="v {'good' if diff_dd >= 0 else 'warn'}">{diff_dd:+.2f} pts</div></div>
</div>
<h2>Experiment Lessons</h2>
<section class="panel"><ul class="lessons">{lessons_html}</ul></section>
<section class="panel">{equity_svg}</section>
<section class="panel">{signal_svg}</section>
<h2>Fixed Preset Comparison</h2>
<section class="panel"><table><thead><tr><th>Scenario</th><th>Config</th><th>Final</th><th>Total return</th><th>Annualized</th><th>Max drawdown</th><th>Tactical positions</th><th>TQQQ P&L</th><th>CCS synthetic diagnostic</th></tr></thead><tbody>{preset_rows}</tbody></table></section>
<h2>Summary</h2>
<section class="panel"><table><thead><tr><th>Scenario</th><th>Final</th><th>Total return</th><th>Annualized</th><th>Max drawdown</th><th>Positions/windows</th><th>Synthetic diagnostic</th></tr></thead><tbody>{summary_rows}</tbody></table></section>
<h2>Tactical TQQQ Trades</h2>
<section class="panel"><table><thead><tr><th>Entry signal</th><th>Entry fill</th><th>Exit signal</th><th>Exit fill/mark</th><th>Type</th><th>Entry px</th><th>Exit px</th><th>Return</th><th>P&L</th><th>Exit reason</th></tr></thead><tbody>{trade_rows}</tbody></table></section>
<h2>Stylized CCS Diagnostics (not historical P&amp;L)</h2>
<section class="panel"><table><thead><tr><th>Signal</th><th>Scenario entry</th><th>Expiry</th><th>TQQQ entry</th><th>Spread</th><th>TQQQ expiry</th><th>Net synthetic payoff / contract</th><th>Conditional max gain</th><th>Conditional max loss</th><th>Sign</th></tr></thead><tbody>{ccs_rows}</tbody></table></section>
<h2>CCS Strike / Time Grid</h2>
<section class="panel"><table><thead><tr><th>Strike mode</th><th>Multiplier</th><th>Hold days</th><th>Synthetic credit</th><th>Windows</th><th>Positive/negative</th><th>Positive rate</th><th>Total synthetic payoff</th><th>Avg synthetic payoff</th></tr></thead><tbody>{grid_rows}</tbody></table></section>
<h2>Teacher-Style Sweep</h2>
{best_teacher_html}
<section class="panel"><table><thead><tr><th>Module</th><th>Variant</th><th>Core</th><th>EMA8/21 TQQQ</th><th>Overheat</th><th>Trail</th><th>CCS diagnostic</th><th>Equity return (CCS excluded)</th><th>Max DD</th><th>CCS synthetic payoff</th></tr></thead><tbody>{module_rows_html}</tbody></table></section>
<h2>Top Full-Grid Versions</h2>
<section class="panel"><table><thead><tr><th>Module</th><th>Variant</th><th>Core</th><th>EMA8/21 TQQQ</th><th>Overheat</th><th>Trail</th><th>CCS diagnostic</th><th>Equity return (CCS excluded)</th><th>Max DD</th><th>CCS synthetic payoff</th></tr></thead><tbody>{top_rows_html}</tbody></table></section>
<h2>Methodology Limits</h2>
<section class="panel"><ul class="lessons">{limitations_html}</ul></section>
</main></body></html>'''
    report = out_dir / "qqq_tqqq_ytd_backtest_report.html"
    atomic_write_text(report, html_doc)
    return report


def save_outputs(result, out_dir, capital):
    ensure_private_directory(out_dir)
    write_csv(out_dir / "ytd_summary.csv", result["summary"], [
        "scenario", "start_date", "end_date", "final_value", "total_return_pct",
        "annualized_return_pct", "max_drawdown_pct", "num_tactical_trades",
        "stylized_payoff_dollars", "data_note", "decision_grade",
    ])
    write_csv(out_dir / "preset_comparison.csv", result["presets"], [
        "scenario", "config", "start_date", "end_date", "final_value", "total_return_pct",
        "annualized_return_pct", "max_drawdown_pct", "num_tactical_trades",
        "tactical_pnl_dollars", "transaction_costs_dollars", "ccs_stylized_payoff_dollars",
        "return_includes_ccs", "decision_grade",
    ])
    write_csv(out_dir / "trades.csv", result["trades"], [
        "entry_signal_date", "entry_date", "exit_signal_date", "exit_date", "type", "status",
        "entry_reference_close", "entry_price", "exit_reference_close", "exit_price",
        "shares", "return_pct", "pnl_dollars", "entry_execution_cost_dollars",
        "exit_execution_cost_dollars", "exit_reason", "fill_timing",
        "equity_slippage_bps_per_side", "commission_per_order",
    ])
    write_csv(out_dir / "ccs_hedges.csv", result["ccs"], [
        "signal_date", "entry_date", "expiry_date", "tqqq_signal_close", "tqqq_entry_close",
        "tqqq_expiry_close",
        "short_call", "long_call", "credit_per_share_assumed", "hold_days", "strike_mode",
        "raw_target", "qqq_ema21_plus_atr_band", "atr_multiplier", "strike_source",
        "premium_source", "fill_timing", "signal_warmup_sessions",
        "gross_expiration_payoff_per_contract", "execution_cost_per_contract_assumed",
        "stylized_net_payoff_per_contract", "stylized_max_gain_per_contract",
        "stylized_max_loss_per_contract", "decision_grade", "win",
    ])
    write_csv(out_dir / "ccs_grid.csv", result["ccs_grid"], [
        "strike_mode", "multiplier", "hold_days", "credit_per_share_assumed", "num_hedges",
        "wins", "losses", "win_rate_pct", "total_stylized_payoff_per_contract",
        "avg_stylized_payoff_per_contract", "decision_grade",
    ])
    sweep_fields = [
        "module", "variant", "core_alloc", "ema8_alloc", "ema21_alloc",
        "overheat_exit", "overheat_sell_pct", "trail_pct",
        "ccs_mode", "ccs_hold_days", "ccs_multiplier",
        "final_value", "total_return_pct", "max_drawdown_pct", "num_tactical_trades",
        "tactical_pnl_dollars", "transaction_costs_dollars", "ccs_stylized_payoff_dollars",
        "combined_final_value", "combined_return_pct", "teacher_aligned",
        "return_includes_ccs", "decision_grade",
    ]
    write_csv(out_dir / "strategy_module_comparison.csv", result["sweep"]["module_rows"], sweep_fields)
    write_csv(out_dir / "strategy_full_grid.csv", result["sweep"]["full_rows"], sweep_fields)
    best_rows = []
    if result["sweep"].get("best_overall"):
        best_rows.append({**result["sweep"]["best_overall"], "module": "best_overall"})
    if result["sweep"].get("best_teacher_aligned"):
        best_rows.append({**result["sweep"]["best_teacher_aligned"], "module": "best_teacher_aligned"})
    write_csv(out_dir / "strategy_best_versions.csv", best_rows, sweep_fields)
    for key, hedges in result["ccs_detail"].items():
        write_csv(out_dir / f"ccs_{key}.csv", hedges, [
            "signal_date", "entry_date", "expiry_date", "tqqq_signal_close", "tqqq_entry_close",
            "tqqq_expiry_close",
            "short_call", "long_call", "credit_per_share_assumed", "hold_days", "strike_mode",
            "raw_target", "qqq_ema21_plus_atr_band", "atr_multiplier", "strike_source",
            "premium_source", "fill_timing", "signal_warmup_sessions",
            "gross_expiration_payoff_per_contract", "execution_cost_per_contract_assumed",
            "stylized_net_payoff_per_contract", "stylized_max_gain_per_contract",
            "stylized_max_loss_per_contract", "decision_grade", "win",
        ])
    write_csv(out_dir / "equity_curve.csv", result["curve"])
    indicator_rows = []
    for r, st in zip(result["rows"], result["states"]):
        indicator_rows.append({
            "date": r["date"], "QQQ": r["QQQ"], "TQQQ": r["TQQQ"],
            "ema8": r["ema8"], "ema13": r["ema13"], "ema21": r["ema21"],
            "ema34": r["ema34"], "ema55": r["ema55"], "atr14_close_proxy": r["atr14"],
            "rsi14": r["rsi14"], "qqq_5d_pct": r["qqq_5d_pct"],
            "tqqq_5d_pct": r["tqqq_5d_pct"], "state": st["state"],
        })
    write_csv(out_dir / "indicator_series.csv", indicator_rows)
    report = write_report(result, out_dir, capital)
    atomic_write_json(out_dir / "summary.json", result["summary"])
    atomic_write_json(out_dir / "methodology.json", result["methodology"])
    return report


def main():
    ap = argparse.ArgumentParser(
        description="Research-only close proxy for QQQ EMA weather / TQQQ tactical rules."
    )
    ap.add_argument("--prices-cache", default=str(DEFAULT_CACHE))
    ap.add_argument("--output-dir", default=str(DEFAULT_OUT))
    ap.add_argument("--start", default="2026-01-01")
    ap.add_argument("--end", default=None)
    ap.add_argument("--capital", type=float, default=100000.0)
    ap.add_argument("--warmup", type=int, default=34)
    ap.add_argument("--fetch-yfinance", action="store_true",
                    help="Fetch adjusted QQQ/TQQQ closes from yfinance instead of using prices_cache.json.")
    args = ap.parse_args()

    prices = fetch_yfinance_prices(args.start, args.end) if args.fetch_yfinance else load_prices(Path(args.prices_cache))
    result = run_backtest(prices, start=args.start, end=args.end, capital=args.capital, warmup=args.warmup)
    report = save_outputs(result, Path(args.output_dir), args.capital)
    strat, qqq, dca, tqqq, ccs = result["summary"]
    print(f"Backtest period: {strat['start_date']} to {strat['end_date']}")
    print(f"Strategy final: {fmt_money(strat['final_value'])} ({fmt_pct(strat['total_return_pct'])}), max DD {fmt_pct(strat['max_drawdown_pct'])}")
    print(f"QQQ buy/hold:   {fmt_money(qqq['final_value'])} ({fmt_pct(qqq['total_return_pct'])}), max DD {fmt_pct(qqq['max_drawdown_pct'])}")
    print(
        f"QQQ fixed-horizon DCA: {fmt_money(dca['final_value'])} "
        f"({fmt_pct(dca['total_return_pct'])}), max DD {fmt_pct(dca['max_drawdown_pct'])}"
    )
    print(f"TQQQ buy/hold:  {fmt_money(tqqq['final_value'])} ({fmt_pct(tqqq['total_return_pct'])}), max DD {fmt_pct(tqqq['max_drawdown_pct'])}")
    print(
        f"Tactical execution records: {len(result['trades'])}; CCS diagnostic windows: {len(result['ccs'])}; "
        f"synthetic CCS payoff total (excluded from returns): {fmt_money(ccs.get('stylized_payoff_dollars'))}"
    )
    print("Model grade: RESEARCH ONLY / NOT DECISION GRADE")
    print(f"Report: {report}")


if __name__ == "__main__":
    main()

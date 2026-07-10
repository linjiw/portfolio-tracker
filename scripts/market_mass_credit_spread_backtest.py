#!/usr/bin/env python3
"""Weekly credit-spread backtest using market-mass boundary signals.

This is a research/model backtest. Free yfinance/FRED/Stooq data can give
historical underlying prices and volatility proxies, but not reliable
historical option fills. Premiums here are estimated with Black-Scholes from
the entry spot, selected strike, DTE, and the market-mass volatility estimate.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import importlib.util
import math
from pathlib import Path
from statistics import NormalDist
from zoneinfo import ZoneInfo

try:
    from scripts.artifact_io import atomic_write_csv, atomic_write_json, atomic_write_text
except ImportError:  # direct `python scripts/market_mass_credit_spread_backtest.py`
    from artifact_io import atomic_write_csv, atomic_write_json, atomic_write_text


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "output" / "market_mass_credit_spreads"


def _load_market_mass_module():
    path = Path(__file__).with_name("market_mass_boundaries.py")
    spec = importlib.util.spec_from_file_location("market_mass_boundaries", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mmb = _load_market_mass_module()
NORM = NormalDist()
GRAVITY_PROFILES = getattr(mmb, "GRAVITY_PROFILES", {
    "structural": (252, 63),
    "swing": (84, 21),
    "tactical": (63, 14),
})


DEFAULT_CONFIG = {
    "name": "mass_adaptive_q65_c80_w5",
    "side_policy": "mass_adaptive",
    "boundary_model": "mass_vol",
    "confidence": 0.80,
    "quality_min": 65.0,
    "max_build_up": 65.0,
    "max_abs_distance_z": 2.2,
    "min_gravity_score": 0.0,
    "max_levitation_score": 100.0,
    "min_short_hvn_distance_em": 0.0,
    "max_quality_drop_5d": 18.0,
    "max_abs_distance_z_slope_5d": 1.25,
    "min_credit": 0.05,
    "min_credit_risk": 0.16,
    "min_otm_pct": 0.008,
    "boundary_buffer_pct": 0.002,
    "max_short_delta": 0.35,
    "min_ic_body_pct": 0.0,
    "min_ic_wing_credit": 0.0,
    "min_ic_credit_balance": 0.0,
    "width": 5.0,
    "hold_bars": 4,
    "entry_weekday": 0,
    "expiry_mode": "weekly_friday",
    "profit_take_pct": 0.65,
    "stop_loss_multiple": 2.0,
    "credit_haircut_pct": 0.0,
    "exit_slippage_per_spread": 0.02,
    "dividend_yield": 0.0,
    "put_iv_multiplier": 1.0,
    "call_iv_multiplier": 1.0,
    # Signals are formed from the prior completed daily bar.  A zero-lag
    # same-close fill can see the very close it claims to trade and is retained
    # only as an explicitly requested sensitivity case.
    "signal_lag_bars": 1,
    "entry_fill_model": "synthetic",
    "snapshot_max_age_days": 0,
    "snapshot_max_age_minutes": 30.0,
    "snapshot_strike_tolerance": 0.01,
    "snapshot_fill_edge": 0.25,
    "require_snapshot_fills": False,
    "min_snapshot_fill_coverage": 0.0,
    "entry_timestamp_policy": "same_day_close",
    "breach_stop": False,
    "blocked_ic_weather": "",
    "allow_overlapping_positions": False,
    "risk_per_trade_pct": 0.01,
    "max_contracts": 10,
}


SNAPSHOT_FILL_MODELS = ("mid", "natural", "conservative_mid", "haircut_mid", "bid_ask_slippage")


def _to_float(value):
    try:
        if value is None:
            return None
        out = float(value)
        if math.isnan(out) or math.isinf(out):
            return None
        return out
    except Exception:
        return None


def rn(value, digits=2):
    value = _to_float(value)
    return None if value is None else round(value, digits)


def parse_date(value):
    return dt.date.fromisoformat(mmb.date_key(value))


def pct(new, old):
    new = _to_float(new)
    old = _to_float(old)
    if new is None or old in (None, 0):
        return None
    return (new / old - 1.0) * 100.0


def apply_credit_haircut(credit, config):
    haircut = mmb.clamp((config.get("credit_haircut_pct") or 0.0) / 100.0, 0.0, 0.95)
    return max(0.0, credit * (1.0 - haircut))


def normalize_option_type(value):
    raw = str(value or "").strip().lower()
    if raw in ("c", "call", "calls"):
        return "call"
    if raw in ("p", "put", "puts"):
        return "put"
    return raw


def option_mid(row):
    bid = _to_float(row.get("bid"))
    ask = _to_float(row.get("ask"))
    if bid is not None and ask is not None and 0 <= bid <= ask:
        return (bid + ask) / 2.0
    mid = _to_float(row.get("mid"))
    return mid if mid is not None and mid >= 0 else None


def parse_snapshot_ts(value):
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo("America/New_York"))
    return parsed


def entry_reference_ts(entry_date, policy="same_day_close"):
    policy = policy or "same_day_close"
    text = str(entry_date)
    if "T" in text or len(text) > 10:
        parsed = parse_snapshot_ts(text)
        if parsed:
            return parsed
    date = parse_date(entry_date)
    tz = ZoneInfo("America/New_York")
    if policy == "same_day_open":
        return dt.datetime.combine(date, dt.time(9, 30), tzinfo=tz)
    if policy == "next_open":
        next_day = date + dt.timedelta(days=1)
        while next_day.weekday() >= 5:
            next_day += dt.timedelta(days=1)
        return dt.datetime.combine(next_day, dt.time(9, 30), tzinfo=tz)
    return dt.datetime.combine(date, dt.time(16, 0), tzinfo=tz)


class OptionSnapshotStore:
    """Point-in-time option-chain snapshots indexed by snapshot date and expiry."""

    def __init__(self, rows, max_age_days=0, strike_tolerance=0.01, max_age_minutes=None):
        self.max_age_days = max(0, int(max_age_days or 0))
        self.strike_tolerance = max(0.0, float(strike_tolerance or 0.0))
        self.max_age_minutes = None if max_age_minutes is None else max(0.0, float(max_age_minutes))
        self.by_date_expiry = {}
        self.by_expiry = {}
        self.row_count = 0
        self.rejected_row_count = 0
        for raw in rows:
            snapshot_ts = str(raw.get("snapshot_ts") or raw.get("timestamp") or "").strip()
            expiry = mmb.date_key(raw.get("expiry") or raw.get("expiration") or "")
            option_type = normalize_option_type(raw.get("option_type") or raw.get("type"))
            strike = _to_float(raw.get("strike"))
            snapshot_dt = parse_snapshot_ts(snapshot_ts)
            try:
                expiry_date = dt.date.fromisoformat(expiry)
            except ValueError:
                expiry_date = None
            if (
                not snapshot_ts
                or snapshot_dt is None
                or expiry_date is None
                or option_type not in ("call", "put")
                or strike is None
                or strike <= 0
            ):
                self.rejected_row_count += 1
                continue
            row = dict(raw)
            row["snapshot_ts"] = snapshot_ts
            row["snapshot_date"] = snapshot_dt.astimezone(
                ZoneInfo("America/New_York")
            ).date().isoformat()
            row["expiry"] = expiry
            row["option_type"] = option_type
            row["strike"] = strike
            row["_snapshot_dt"] = snapshot_dt
            for key in (
                "bid", "ask", "mid", "last", "volume", "open_interest",
                "implied_volatility", "delta", "model_delta",
            ):
                if key in row:
                    row[key] = _to_float(row.get(key))
            self.by_date_expiry.setdefault((row["snapshot_date"], expiry), []).append(row)
            self.by_expiry.setdefault(expiry, []).append(row)
            self.row_count += 1

    @classmethod
    def from_path(cls, path, max_age_days=0, strike_tolerance=0.01, max_age_minutes=None):
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"option snapshot path does not exist: {path}")
        files = [path] if path.is_file() else sorted(path.rglob("*.csv"))
        if not files:
            raise ValueError(f"option snapshot path contains no CSV files: {path}")
        rows = []
        for file in files:
            with file.open("r", encoding="utf-8-sig", newline="") as fh:
                rows.extend(dict(row) for row in csv.DictReader(fh))
        store = cls(
            rows,
            max_age_days=max_age_days,
            strike_tolerance=strike_tolerance,
            max_age_minutes=max_age_minutes,
        )
        if store.row_count == 0:
            raise ValueError(f"option snapshot path contains no valid point-in-time rows: {path}")
        return store

    def chain_rows(self, entry_date, expiry, timestamp_policy="same_day_close", max_age_minutes=None):
        expiry = mmb.date_key(expiry)
        reference_ts = entry_reference_ts(entry_date, timestamp_policy)
        reference_date = reference_ts.date()
        try:
            if dt.date.fromisoformat(expiry) < reference_date:
                return [], "invalid_expiry"
        except ValueError:
            return [], "invalid_expiry"
        max_minutes = self.max_age_minutes if max_age_minutes is None else max_age_minutes
        candidates = []
        for age in range(self.max_age_days + 1):
            key_date = (reference_date - dt.timedelta(days=age)).isoformat()
            rows = self.by_date_expiry.get((key_date, expiry))
            if rows:
                candidates.extend(rows)
        if not candidates:
            return [], "missing_expiry"
        valid = []
        saw_future = False
        for row in candidates:
            snap_dt = row.get("_snapshot_dt")
            if snap_dt is None:
                continue
            age_minutes = (reference_ts - snap_dt).total_seconds() / 60.0
            # Future quotes are look-ahead even when the caller elects not to
            # cap quote age.  Freshness and causality are separate contracts.
            if age_minutes < 0:
                saw_future = True
                continue
            if max_minutes is not None:
                if age_minutes > float(max_minutes):
                    continue
            valid.append(row)
        if not valid:
            return [], "future_snapshot" if saw_future else "stale_snapshot"
        latest_dt = max(row["_snapshot_dt"] for row in valid)
        return [row for row in valid if row["_snapshot_dt"] == latest_dt], None

    def find_contract(self, entry_date, expiry, option_type, strike, timestamp_policy="same_day_close", max_age_minutes=None):
        option_type = normalize_option_type(option_type)
        strike = float(strike)
        chain, chain_error = self.chain_rows(entry_date, expiry, timestamp_policy=timestamp_policy, max_age_minutes=max_age_minutes)
        if not chain:
            return None, chain_error or "missing_expiry"
        candidates = [
            row for row in chain
            if row.get("option_type") == option_type
        ]
        if not candidates:
            return None, f"missing_{option_type}"
        best = min(candidates, key=lambda row: abs(float(row["strike"]) - strike))
        if abs(float(best["strike"]) - strike) > self.strike_tolerance:
            return None, f"missing_{option_type}_strike"
        return best, None


def load_option_snapshots(path, max_age_days=0, strike_tolerance=0.01, max_age_minutes=None):
    return OptionSnapshotStore.from_path(
        path,
        max_age_days=max_age_days,
        strike_tolerance=strike_tolerance,
        max_age_minutes=max_age_minutes,
    )


def snapshot_leg_prices(row, fill_edge=0.25):
    bid = _to_float(row.get("bid"))
    ask = _to_float(row.get("ask"))
    # Historical last/mid alone is not an executable quote.  Every supported
    # replay fill model needs a valid contemporaneous bid/ask market.
    if bid is None or ask is None or bid < 0 or ask < 0 or ask < bid:
        return None
    mid = (bid + ask) / 2.0
    edge = mmb.clamp(fill_edge, 0.0, 1.0)
    return {
        "bid": bid,
        "ask": ask,
        "mid": mid,
        "sell_slip": bid + edge * (mid - bid),
        "buy_slip": ask - edge * (ask - mid),
    }


def snapshot_spread_credit(
    store,
    entry_date,
    expiry_date,
    side,
    short_strike,
    long_strike,
    fill_model="conservative_mid",
    credit_haircut_pct=0.0,
    fill_edge=0.25,
    timestamp_policy="same_day_close",
    max_age_minutes=None,
    explain=False,
):
    if store is None:
        return (None, "no_snapshot_store") if explain else None
    option_type = "call" if side == "CCS" else "put"
    short, short_error = store.find_contract(
        entry_date,
        expiry_date,
        option_type,
        short_strike,
        timestamp_policy=timestamp_policy,
        max_age_minutes=max_age_minutes,
    )
    long, long_error = store.find_contract(
        entry_date,
        expiry_date,
        option_type,
        long_strike,
        timestamp_policy=timestamp_policy,
        max_age_minutes=max_age_minutes,
    )
    if not short:
        reason = f"missing_short_{option_type}" if short_error in (None, f"missing_{option_type}_strike") else short_error
        return (None, reason) if explain else None
    if not long:
        reason = f"missing_long_{option_type}" if long_error in (None, f"missing_{option_type}_strike") else long_error
        return (None, reason) if explain else None
    short_px = snapshot_leg_prices(short, fill_edge=fill_edge)
    long_px = snapshot_leg_prices(long, fill_edge=fill_edge)
    if not short_px or not long_px:
        return (None, "missing_leg_quote") if explain else None
    mid_credit = short_px["mid"] - long_px["mid"]
    natural_credit = short_px["bid"] - long_px["ask"]
    conservative_credit = 0.75 * mid_credit + 0.25 * natural_credit
    haircut = mmb.clamp((credit_haircut_pct or 0.0) / 100.0, 0.0, 0.95)
    model = fill_model or "conservative_mid"
    if model == "mid":
        selected = mid_credit
    elif model == "natural":
        selected = natural_credit
    elif model == "conservative_mid":
        selected = conservative_credit
    elif model == "haircut_mid":
        selected = mid_credit * (1.0 - haircut)
    elif model == "bid_ask_slippage":
        selected = short_px["sell_slip"] - long_px["buy_slip"]
    else:
        raise ValueError(f"unknown snapshot fill model: {model}")
    reference_ts = entry_reference_ts(entry_date, timestamp_policy)
    snapshot_dt = parse_snapshot_ts(short.get("snapshot_ts"))
    snapshot_age_minutes = (
        (reference_ts - snapshot_dt).total_seconds() / 60.0
        if snapshot_dt is not None else None
    )
    result = {
        "source": f"snapshot:{model}",
        "snapshot_ts": short.get("snapshot_ts"),
        "snapshot_age_minutes": snapshot_age_minutes,
        "snapshot_mid_credit": mid_credit,
        "snapshot_natural_credit": natural_credit,
        "snapshot_conservative_credit": conservative_credit,
        "snapshot_fill_credit": selected,
        "credit": max(0.0, selected),
        "short_bid": short_px["bid"],
        "short_ask": short_px["ask"],
        "short_mid": short_px["mid"],
        "long_bid": long_px["bid"],
        "long_ask": long_px["ask"],
        "long_mid": long_px["mid"],
        "short_observed_delta": (
            short.get("delta")
            if short.get("delta") is not None
            else short.get("model_delta")
        ),
        "short_observed_iv": short.get("implied_volatility"),
        "long_observed_delta": (
            long.get("delta")
            if long.get("delta") is not None
            else long.get("model_delta")
        ),
        "long_observed_iv": long.get("implied_volatility"),
    }
    return (result, None) if explain else result


def credit_error_pct(synthetic_credit, observed_credit):
    synthetic_credit = _to_float(synthetic_credit)
    observed_credit = _to_float(observed_credit)
    if synthetic_credit is None or observed_credit in (None, 0):
        return None
    return (synthetic_credit / observed_credit - 1.0) * 100.0


def snapshot_metrics(prefix, credit_info, synthetic_credit):
    if not credit_info:
        return {
            f"{prefix}entry_credit_source": "synthetic",
            f"{prefix}snapshot_ts": None,
            f"{prefix}snapshot_age_minutes": None,
            f"{prefix}snapshot_mid_credit": None,
            f"{prefix}snapshot_natural_credit": None,
            f"{prefix}snapshot_conservative_credit": None,
            f"{prefix}snapshot_fill_credit": None,
            f"{prefix}credit_error_pct": None,
        }
    return {
        f"{prefix}entry_credit_source": credit_info.get("source"),
        f"{prefix}snapshot_ts": credit_info.get("snapshot_ts"),
        f"{prefix}snapshot_age_minutes": rn(credit_info.get("snapshot_age_minutes"), 2),
        f"{prefix}snapshot_mid_credit": rn(credit_info.get("snapshot_mid_credit"), 4),
        f"{prefix}snapshot_natural_credit": rn(credit_info.get("snapshot_natural_credit"), 4),
        f"{prefix}snapshot_conservative_credit": rn(credit_info.get("snapshot_conservative_credit"), 4),
        f"{prefix}snapshot_fill_credit": rn(credit_info.get("snapshot_fill_credit"), 4),
        f"{prefix}credit_error_pct": rn(credit_error_pct(synthetic_credit, credit_info.get("credit")), 2),
    }


def max_not_none(*values):
    values = [v for v in values if v is not None]
    return max(values) if values else None


def mapped_hvn_nodes(state, trade_spot):
    center = state.get("center") or {}
    mass_spot = center.get("current_price")
    if not mass_spot or mass_spot <= 0 or not trade_spot:
        return []
    ratio = trade_spot / mass_spot
    profile_nodes = center.get("profile_nodes") or {}
    out = []
    for kind in ("support", "resistance"):
        for node in profile_nodes.get(kind) or []:
            price = node.get("price")
            if price is None:
                continue
            out.append({
                "kind": kind,
                "price": price * ratio,
                "mass_price": price,
                "strength": node.get("strength"),
                "mass_share": node.get("mass_share"),
            })
    return out


def strike_hvn_metrics(state, trade_spot, strike, horizon_days, annual_vol):
    nodes = mapped_hvn_nodes(state, trade_spot)
    if not nodes or not trade_spot or not annual_vol:
        return {
            "nearest_hvn_price": None,
            "nearest_hvn_kind": None,
            "nearest_hvn_distance_em": None,
            "hvn_penalty": None,
        }
    expected_move = max(trade_spot * annual_vol * math.sqrt(max(horizon_days, 1) / 252.0), trade_spot * 0.0025)
    best = None
    best_distance = None
    best_penalty = 0.0
    for node in nodes:
        distance = abs(strike - node["price"]) / expected_move
        mass = _to_float(node.get("mass_share"))
        strength = _to_float(node.get("strength"))
        node_weight = mass if mass is not None else (strength or 0.0)
        penalty = node_weight * math.exp(-distance)
        if best_distance is None or distance < best_distance:
            best = node
            best_distance = distance
        best_penalty = max(best_penalty, penalty)
    return {
        "nearest_hvn_price": best["price"] if best else None,
        "nearest_hvn_kind": best["kind"] if best else None,
        "nearest_hvn_distance_em": best_distance,
        "hvn_penalty": best_penalty,
    }


def prefixed_metrics(prefix, metrics):
    return {
        f"{prefix}_nearest_hvn_price": rn(metrics.get("nearest_hvn_price"), 4),
        f"{prefix}_nearest_hvn_kind": metrics.get("nearest_hvn_kind"),
        f"{prefix}_hvn_distance_em": rn(metrics.get("nearest_hvn_distance_em"), 4),
        f"{prefix}_hvn_penalty": rn(metrics.get("hvn_penalty"), 6),
    }


def rejection(reason, entry, expiry=None, side=None, **fields):
    row = {
        "_rejected": True,
        "date": entry.get("date") if isinstance(entry, dict) else entry,
        "entry_date": entry.get("date") if isinstance(entry, dict) else entry,
        "expiry_date": expiry.get("date") if isinstance(expiry, dict) else expiry,
        "side": side,
        "reason": reason,
    }
    row.update(fields)
    return row


def max_drawdown(values):
    peak = None
    worst = 0.0
    for value in values:
        peak = value if peak is None else max(peak, value)
        if peak:
            worst = min(worst, value / peak - 1.0)
    return worst


def annualized_return(start_value, end_value, start_date, end_date):
    if start_value <= 0 or end_value <= 0:
        return None
    days = max((parse_date(end_date) - parse_date(start_date)).days, 1)
    return (end_value / start_value) ** (365.0 / days) - 1.0


def ema(values, n):
    alpha = 2.0 / (n + 1.0)
    cur = None
    out = []
    for value in values:
        value = _to_float(value)
        cur = value if cur is None else alpha * value + (1.0 - alpha) * cur
        out.append(cur)
    return out


def atr(rows, n=14):
    trs = []
    prev = None
    for row in rows:
        high = row["high"]
        low = row["low"]
        close = row["close"]
        tr = high - low if prev is None else max(high - low, abs(high - prev), abs(low - prev))
        trs.append(tr)
        prev = close
    out = []
    cur = None
    for i, tr in enumerate(trs):
        if i < n - 1:
            out.append(None)
        elif i == n - 1:
            cur = sum(trs[:n]) / n
            out.append(cur)
        else:
            cur = (cur * (n - 1) + tr) / n
            out.append(cur)
    return out


def add_indicators(rows):
    closes = [r["close"] for r in rows]
    ema_map = {n: ema(closes, n) for n in (8, 13, 21, 34, 55)}
    atr14 = atr(rows, 14)
    for i, row in enumerate(rows):
        for n, values in ema_map.items():
            row[f"ema{n}"] = values[i]
        row["atr14"] = atr14[i]
        row["ret5_pct"] = pct(row["close"], rows[i - 5]["close"]) if i >= 5 else None
        row["ema21_slope_5d"] = pct(row["ema21"], rows[i - 5]["ema21"]) if i >= 5 else None
    return rows


def classify_weather(row, prev=None):
    close = row["close"]
    e8 = row.get("ema8")
    e13 = row.get("ema13")
    e21 = row.get("ema21")
    e34 = row.get("ema34")
    atr14 = row.get("atr14") or 0.0
    slope = row.get("ema21_slope_5d") or 0.0
    if not all(_to_float(x) is not None for x in (e8, e13, e21, e34)):
        return "unknown"
    stacked = close > e21 and e8 > e13 > e21 and slope > 0
    two_below = bool(prev and close < e21 and prev["close"] < prev.get("ema21", close))
    trend_break = two_below or e8 < e21 or (close < e21 and slope <= 0)
    overheat = bool(stacked and atr14 and (
        close - e8 > 1.5 * atr14
        or (row.get("ret5_pct") is not None and row["ret5_pct"] > 3.0)
    ))
    near8 = bool(stacked and atr14 and close >= e8 and abs(close - e8) <= 0.5 * atr14)
    near21 = bool(atr14 and close >= e21 and abs(close - e21) <= 0.5 * atr14 and slope > 0)
    if trend_break:
        return "break"
    if near21:
        return "ema21_pullback"
    if near8:
        return "ema8_pullback"
    if overheat:
        return "overheat"
    if stacked:
        return "bull"
    return "mixed"


def norm_cdf(x):
    return NORM.cdf(x)


def black_scholes_price(
    spot,
    strike,
    years,
    sigma,
    option_type,
    rate=0.04,
    dividend_yield=0.0,
):
    spot = max(float(spot), 1e-9)
    strike = max(float(strike), 1e-9)
    years = max(float(years), 1.0 / 365.0)
    sigma = max(float(sigma), 0.01)
    vol_t = sigma * math.sqrt(years)
    dividend_yield = float(dividend_yield or 0.0)
    carry = rate - dividend_yield
    d1 = (math.log(spot / strike) + (carry + 0.5 * sigma * sigma) * years) / vol_t
    d2 = d1 - vol_t
    discounted_spot = spot * math.exp(-dividend_yield * years)
    if option_type == "call":
        return discounted_spot * norm_cdf(d1) - strike * math.exp(-rate * years) * norm_cdf(d2)
    if option_type == "put":
        return strike * math.exp(-rate * years) * norm_cdf(-d2) - discounted_spot * norm_cdf(-d1)
    raise ValueError(f"unknown option_type: {option_type}")


def black_scholes_delta(
    spot,
    strike,
    years,
    sigma,
    option_type,
    rate=0.04,
    dividend_yield=0.0,
):
    spot = max(float(spot), 1e-9)
    strike = max(float(strike), 1e-9)
    years = max(float(years), 1.0 / 365.0)
    sigma = max(float(sigma), 0.01)
    vol_t = sigma * math.sqrt(years)
    dividend_yield = float(dividend_yield or 0.0)
    d1 = (
        math.log(spot / strike)
        + (rate - dividend_yield + 0.5 * sigma * sigma) * years
    ) / vol_t
    discount = math.exp(-dividend_yield * years)
    if option_type == "call":
        return discount * norm_cdf(d1)
    if option_type == "put":
        return discount * (norm_cdf(d1) - 1.0)
    raise ValueError(f"unknown option_type: {option_type}")


def spread_mark(
    spot,
    short_strike,
    long_strike,
    side,
    years,
    sigma,
    rate=0.04,
    dividend_yield=0.0,
):
    if side == "CCS":
        short_value = black_scholes_price(
            spot, short_strike, years, sigma, "call", rate, dividend_yield
        )
        long_value = black_scholes_price(
            spot, long_strike, years, sigma, "call", rate, dividend_yield
        )
    elif side == "PCS":
        short_value = black_scholes_price(
            spot, short_strike, years, sigma, "put", rate, dividend_yield
        )
        long_value = black_scholes_price(
            spot, long_strike, years, sigma, "put", rate, dividend_yield
        )
    else:
        raise ValueError(f"unknown side: {side}")
    return max(0.0, short_value - long_value)


def point_in_time_exit_vol(
    rows,
    row_index,
    vol_series=None,
    fallback_vol_series=None,
    fallback_sigma=0.20,
):
    """Return the volatility proxy observable at an exit close.

    This remains a broad-proxy model, not an option-surface reconstruction, but
    it avoids holding pre-entry IV fixed through a volatility shock when dated
    VXN/VIX observations are available.
    """
    if not vol_series and not fallback_vol_series:
        return max(float(fallback_sigma), 0.01), "fixed_entry_proxy"
    ctx = mmb.volatility_context(
        rows[: row_index + 1],
        vol_series=vol_series or {},
        fallback_vol_series=fallback_vol_series or {},
        as_of_date=rows[row_index]["date"],
    )
    sigma = _to_float(ctx.get("annual_vol_used"))
    if sigma is None or sigma <= 0:
        return max(float(fallback_sigma), 0.01), "fixed_entry_proxy"
    source = ctx.get("implied_vol_source") or "realized_only"
    return max(sigma, 0.01), f"point_in_time_{source}"


def nearest_strike(value, increment, direction):
    if direction == "up":
        return math.ceil(value / increment) * increment
    if direction == "down":
        return math.floor(value / increment) * increment
    return round(value / increment) * increment


def choose_short_strike(side, spot, boundary, width, increment, min_otm_pct, boundary_buffer_pct=0.0):
    if side == "CCS":
        min_strike = spot * (1.0 + min_otm_pct)
        buffered_boundary = boundary * (1.0 + boundary_buffer_pct)
        short_strike = nearest_strike(max(buffered_boundary, min_strike), increment, "up")
        long_strike = short_strike + width
    elif side == "PCS":
        max_strike = spot * (1.0 - min_otm_pct)
        buffered_boundary = boundary * (1.0 - boundary_buffer_pct)
        short_strike = nearest_strike(min(buffered_boundary, max_strike), increment, "down")
        long_strike = short_strike - width
    else:
        raise ValueError(f"unknown side: {side}")
    return round(short_strike, 2), round(long_strike, 2)


def expiry_details(rows, entry_idx, hold_bars, mode="weekly_friday"):
    """Resolve the modeled expiry without manufacturing a Friday from bars.

    ``weekly_friday`` targets the next Friday after entry and, when that date
    is absent, uses the last observed session before Friday in the same week.
    This matches holiday-shortened weekly expiries while flagging the calendar
    adjustment. ``session_count`` is retained only for explicit sensitivity.
    """
    if entry_idx < 0 or entry_idx >= len(rows):
        return None
    entry_date = parse_date(rows[entry_idx]["date"])
    if mode == "session_count":
        exp_idx = entry_idx + int(hold_bars)
        if exp_idx >= len(rows):
            return None
        return {
            "index": exp_idx,
            "mode": mode,
            "target_date": rows[exp_idx]["date"],
            "calendar_adjusted": False,
        }
    if mode != "weekly_friday":
        raise ValueError(f"unknown expiry_mode: {mode}")
    days_ahead = (4 - entry_date.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    target = entry_date + dt.timedelta(days=days_ahead)
    candidates = []
    for idx in range(entry_idx + 1, len(rows)):
        row_date = parse_date(rows[idx]["date"])
        if row_date > target:
            break
        candidates.append((idx, row_date))
    if not candidates:
        return None
    exp_idx, observed = candidates[-1]
    # Do not bridge a missing week and call it a holiday adjustment.
    if observed < target - dt.timedelta(days=3):
        return None
    return {
        "index": exp_idx,
        "mode": mode,
        "target_date": target.isoformat(),
        "calendar_adjusted": observed != target,
    }


def expiry_index(rows, entry_idx, hold_bars, mode="session_count"):
    details = expiry_details(rows, entry_idx, hold_bars, mode=mode)
    return details["index"] if details else None


def option_years(entry_date, expiry_date):
    days = max((parse_date(expiry_date) - parse_date(entry_date)).days, 1)
    return days / 365.0


def path_settlement(side, expiry_close, short_strike, width):
    if side == "CCS":
        return min(max(expiry_close - short_strike, 0.0), width)
    if side == "PCS":
        return min(max(short_strike - expiry_close, 0.0), width)
    raise ValueError(f"unknown side: {side}")


def side_candidates(policy, weather, center, build_up):
    regime = center.get("regime")
    quality = center.get("quality_score") or 0.0
    distance_z = center.get("distance_z") or 0.0
    build_score = build_up.get("score") or 0.0
    if policy == "pcs_only":
        return ["PCS"]
    if policy == "ccs_only":
        return ["CCS"]
    if policy == "iron_condor":
        return ["IC"] if regime == "active_center" and abs(distance_z) <= 1.0 and build_score <= 72 else []
    if policy != "mass_adaptive":
        raise ValueError(f"unknown side_policy: {policy}")

    if quality < 55 or regime in ("no_reliable_center", "detached_from_mass"):
        return []
    if weather == "break":
        return ["CCS"] if distance_z >= -0.5 else []
    if weather == "overheat" or distance_z >= 1.75:
        return ["CCS"]
    if weather in ("bull", "ema8_pullback", "ema21_pullback") and distance_z >= -1.25:
        return ["PCS"]
    if weather == "mixed" and regime == "active_center" and build_score <= 55 and abs(distance_z) <= 0.8:
        return ["IC"]
    if weather == "mixed" and regime == "active_center" and build_score <= 45 and 0.8 < distance_z < 1.75:
        return ["PCS"]
    return []


def is_weather_blocked_for_ic(weather, config):
    raw = config.get("blocked_ic_weather") or ""
    if isinstance(raw, str):
        blocked = {item.strip() for item in raw.split(",") if item.strip()}
    else:
        blocked = {str(item).strip() for item in raw if str(item).strip()}
    return weather in blocked


def map_mass_band_to_trade(band, state, trade_spot):
    mass_spot = state["center"]["current_price"]
    if not mass_spot:
        return dict(band)
    out = dict(band)
    for key in (
        "lower_boundary", "upper_boundary", "lower_zone_low", "lower_zone_high",
        "upper_zone_low", "upper_zone_high", "vol_lower", "vol_upper",
        "mass_lower", "mass_upper",
    ):
        if key in out and out[key] is not None:
            out[key] = trade_spot * (out[key] / mass_spot)
    out["mass_to_trade_spot_ratio"] = trade_spot / mass_spot
    return out


def build_entry_context(
    trade_rows,
    mass_rows,
    idx,
    vol_series,
    fallback_vol_series,
    lookback,
    half_life,
    horizon,
    confidence,
    boundary_model="mass_vol",
    map_trade_spot=None,
):
    state = mmb.analyze_state(
        mass_rows[:idx + 1],
        vol_series=vol_series,
        fallback_vol_series=fallback_vol_series,
        lookback=lookback,
        half_life=half_life,
    )
    if idx >= 5:
        try:
            prev_state = mmb.analyze_state(
                mass_rows[: idx - 4],
                vol_series=vol_series,
                fallback_vol_series=fallback_vol_series,
                lookback=lookback,
                half_life=half_life,
            )
            state["center"]["quality_slope_5d"] = (
                state["center"]["quality_score"]
                - prev_state["center"]["quality_score"]
            )
            state["center"]["distance_z_slope_5d"] = (
                state["center"]["distance_z"]
                - prev_state["center"]["distance_z"]
            )
        except (RuntimeError, ValueError):
            state["center"]["quality_slope_5d"] = None
            state["center"]["distance_z_slope_5d"] = None
    band = mmb.boundary_rows(state, [horizon], [confidence], boundary_model=boundary_model)[0]
    trade_band = map_mass_band_to_trade(band, state, map_trade_spot or trade_rows[idx]["close"])
    prev = trade_rows[idx - 1] if idx else None
    weather = classify_weather(trade_rows[idx], prev)
    state["signal_weather"] = weather
    return state, trade_band, weather


def build_context_cache(
    trade_rows,
    mass_rows,
    vol_series,
    fallback_vol_series,
    lookback,
    half_life,
    horizon,
    confidences,
    boundary_model="mass_vol",
    start_trade_date=None,
    end_trade_date=None,
):
    confidences = sorted({round(float(c), 4) for c in confidences})
    cache = {}
    start_idx = max(int(lookback) - 5, 60)
    for idx in range(start_idx, len(trade_rows) - int(horizon)):
        row_date = mmb.date_key(trade_rows[idx]["date"])
        # Retain pre-evaluation contexts so five-bar slope features at the
        # first eligible entry do not silently become unavailable.
        if end_trade_date and row_date > end_trade_date:
            continue
        state = mmb.analyze_state(
            mass_rows[:idx + 1],
            vol_series=vol_series,
            fallback_vol_series=fallback_vol_series,
            lookback=lookback,
            half_life=half_life,
        )
        bands = {}
        for confidence in confidences:
            band = mmb.boundary_rows(state, [horizon], [confidence], boundary_model=boundary_model)[0]
            bands[confidence] = map_mass_band_to_trade(band, state, trade_rows[idx]["close"])
        prev = cache.get(idx - 5)
        if prev:
            prev_center = prev["state"]["center"]
            state["center"]["quality_slope_5d"] = state["center"]["quality_score"] - prev_center["quality_score"]
            state["center"]["distance_z_slope_5d"] = state["center"]["distance_z"] - prev_center["distance_z"]
        else:
            state["center"]["quality_slope_5d"] = None
            state["center"]["distance_z_slope_5d"] = None
        for confidence, band in bands.items():
            width_pct = (band["upper_boundary"] - band["lower_boundary"]) / trade_rows[idx]["close"] * 100.0
            band["boundary_width_pct"] = width_pct
            if prev and confidence in prev["bands"]:
                band["boundary_width_slope_5d"] = width_pct - prev["bands"][confidence].get("boundary_width_pct", width_pct)
            else:
                band["boundary_width_slope_5d"] = None
        prev = trade_rows[idx - 1] if idx else None
        cache[idx] = {
            "state": state,
            "bands": bands,
            "weather": classify_weather(trade_rows[idx], prev),
            "horizon": int(horizon),
        }
        state["signal_weather"] = cache[idx]["weather"]
    return cache


def validate_backtest_rows(rows, label="trade"):
    if not rows:
        raise ValueError(f"{label} rows are empty")
    previous_date = None
    for idx, row in enumerate(rows):
        try:
            row_date = parse_date(row.get("date"))
            values = {key: float(row[key]) for key in ("open", "high", "low", "close")}
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"{label} row {idx + 1} has invalid date/OHLC") from exc
        if previous_date is not None and row_date <= previous_date:
            raise ValueError(f"{label} dates must be strictly increasing")
        previous_date = row_date
        if not all(math.isfinite(value) and value > 0 for value in values.values()):
            raise ValueError(f"{label} row {idx + 1} has non-positive/non-finite OHLC")
        if (
            values["high"] < max(values["open"], values["close"])
            or values["low"] > min(values["open"], values["close"])
            or values["high"] < values["low"]
        ):
            raise ValueError(f"{label} row {idx + 1} has inconsistent OHLC range")
        if row.get("ohlc_imputed"):
            raise ValueError(f"{label} row {idx + 1} has imputed OHLC; path exits require complete bars")


def simulate_trade(
    rows,
    entry_idx,
    side,
    band,
    state,
    config,
    capital,
    strike_increment=1.0,
    slippage_per_spread=0.02,
    commission_per_option_contract=0.65,
    risk_free_rate=0.04,
    option_snapshots=None,
    explain_rejections=False,
    vol_series=None,
    fallback_vol_series=None,
):
    entry = rows[entry_idx]
    expiry_info = expiry_details(
        rows,
        entry_idx,
        int(config["hold_bars"]),
        mode=config.get("expiry_mode", "weekly_friday"),
    )
    if expiry_info is None:
        return rejection("missing_expiry", entry, side=side) if explain_rejections else None
    exp_idx = expiry_info["index"]
    expiry = rows[exp_idx]
    spot = entry["close"]
    width = float(config["width"])
    boundary = band["upper_boundary"] if side == "CCS" else band["lower_boundary"]
    short_strike, long_strike = choose_short_strike(
        side,
        spot,
        boundary,
        width,
        strike_increment,
        float(config["min_otm_pct"]),
        float(config.get("boundary_buffer_pct", 0.0)),
    )
    years = option_years(entry["date"], expiry["date"])
    signal_sigma = max(state["volatility"].get("annual_vol_used") or 0.20, 0.01)
    base_sigma, entry_iv_path = point_in_time_exit_vol(
        rows,
        entry_idx,
        vol_series=vol_series,
        fallback_vol_series=fallback_vol_series,
        fallback_sigma=signal_sigma,
    )
    iv_multiplier = float(
        config.get("call_iv_multiplier" if side == "CCS" else "put_iv_multiplier", 1.0)
        or 1.0
    )
    sigma = max(base_sigma * iv_multiplier, 0.01)
    dividend_yield = float(config.get("dividend_yield", 0.0) or 0.0)
    hvn = strike_hvn_metrics(state, spot, short_strike, int(config["hold_bars"]), sigma)
    min_hvn_distance = config.get("min_short_hvn_distance_em") or 0.0
    if min_hvn_distance and hvn.get("nearest_hvn_distance_em") is not None:
        if hvn["nearest_hvn_distance_em"] < float(min_hvn_distance):
            return rejection(
                "short_hvn_distance_below_min",
                entry,
                expiry,
                side,
                short_strike=short_strike,
                long_strike=long_strike,
                short_hvn_distance_em=hvn.get("nearest_hvn_distance_em"),
            ) if explain_rejections else None
    synthetic_raw_credit = spread_mark(
        spot,
        short_strike,
        long_strike,
        side,
        years,
        sigma,
        risk_free_rate,
        dividend_yield,
    )
    synthetic_credit = apply_credit_haircut(max(0.0, synthetic_raw_credit - slippage_per_spread), config)
    snapshot_credit = None
    if option_snapshots is not None:
        snapshot_credit, snapshot_error = snapshot_spread_credit(
            option_snapshots,
            entry["date"],
            expiry["date"],
            side,
            short_strike,
            long_strike,
            fill_model=config.get("entry_fill_model", "conservative_mid"),
            credit_haircut_pct=config.get("credit_haircut_pct", 0.0),
            fill_edge=config.get("snapshot_fill_edge", 0.25),
            timestamp_policy=config.get("entry_timestamp_policy", "same_day_close"),
            max_age_minutes=config.get("snapshot_max_age_minutes"),
            explain=True,
        )
        if snapshot_credit is None:
            return rejection(
                snapshot_error or "missing_snapshot_fill",
                entry,
                expiry,
                side,
                short_strike=short_strike,
                long_strike=long_strike,
                synthetic_credit_per_share=rn(synthetic_credit, 4),
                snapshot_fill_available=False,
            ) if explain_rejections else None
        credit = snapshot_credit["credit"]
        raw_credit = snapshot_credit["credit"]
    else:
        raw_credit = synthetic_raw_credit
        credit = synthetic_credit
    if credit <= 0 or credit < float(config.get("min_credit", 0.0)):
        return rejection(
            "credit_below_min",
            entry,
            expiry,
            side,
            short_strike=short_strike,
            long_strike=long_strike,
            credit_per_share=rn(credit, 4),
            synthetic_credit_per_share=rn(synthetic_credit, 4),
            snapshot_fill_available=snapshot_credit is not None,
            entry_credit_source=snapshot_credit.get("source") if snapshot_credit else "synthetic",
        ) if explain_rejections else None
    option_type = "call" if side == "CCS" else "put"
    model_short_delta = black_scholes_delta(
        spot,
        short_strike,
        years,
        sigma,
        option_type,
        risk_free_rate,
        dividend_yield,
    )
    observed_short_delta = _to_float(
        snapshot_credit.get("short_observed_delta") if snapshot_credit else None
    )
    use_observed_delta = observed_short_delta is not None and abs(observed_short_delta) <= 1.0
    short_delta = observed_short_delta if use_observed_delta else model_short_delta
    short_delta_source = "snapshot" if use_observed_delta else "black_scholes"
    if abs(short_delta) > float(config.get("max_short_delta", 1.0)):
        return rejection(
            "short_delta_above_max",
            entry,
            expiry,
            side,
            short_strike=short_strike,
            long_strike=long_strike,
            short_delta=rn(short_delta, 4),
            snapshot_fill_available=snapshot_credit is not None,
        ) if explain_rejections else None
    risk_per_share = max(width - credit, 0.0)
    if risk_per_share <= 0:
        return rejection(
            "invalid_risk",
            entry,
            expiry,
            side,
            short_strike=short_strike,
            long_strike=long_strike,
            snapshot_fill_available=snapshot_credit is not None,
        ) if explain_rejections else None
    credit_risk = credit / risk_per_share
    if credit_risk < float(config["min_credit_risk"]):
        return rejection(
            "credit_to_risk_below_min",
            entry,
            expiry,
            side,
            short_strike=short_strike,
            long_strike=long_strike,
            credit_per_share=rn(credit, 4),
            credit_risk=rn(credit_risk, 4),
            synthetic_credit_per_share=rn(synthetic_credit, 4),
            snapshot_fill_available=snapshot_credit is not None,
            entry_credit_source=snapshot_credit.get("source") if snapshot_credit else "synthetic",
        ) if explain_rejections else None
    risk_per_contract = risk_per_share * 100.0
    round_trip_commission_per_spread = commission_per_option_contract * 4.0
    sized_risk_per_contract = risk_per_contract + round_trip_commission_per_spread
    contracts = int((capital * float(config["risk_per_trade_pct"])) // sized_risk_per_contract)
    contracts = max(0, min(int(config["max_contracts"]), contracts))
    if contracts <= 0:
        return rejection(
            "position_size_zero",
            entry,
            expiry,
            side,
            short_strike=short_strike,
            long_strike=long_strike,
            snapshot_fill_available=snapshot_credit is not None,
        ) if explain_rejections else None

    exit_idx = exp_idx
    exit_reason = "expiry"
    exit_mark = path_settlement(side, expiry["close"], short_strike, width)
    touched_short = False
    exit_slippage = max(float(config.get("exit_slippage_per_spread", 0.0) or 0.0), 0.0)
    exit_sigma = sigma
    exit_iv_path = "fixed_entry_proxy"
    # Expiry settlement is intrinsic.  Do not assign a fabricated extra day of
    # time value at the expiry close.
    for j in range(entry_idx + 1, exp_idx):
        row = rows[j]
        touched_now = (
            (side == "CCS" and row["high"] >= short_strike)
            or (side == "PCS" and row["low"] <= short_strike)
        )
        touched_short = touched_short or touched_now
        rem_years = max(option_years(row["date"], expiry["date"]), 1.0 / 365.0)
        exit_base_sigma, exit_iv_path = point_in_time_exit_vol(
            rows,
            j,
            vol_series=vol_series,
            fallback_vol_series=fallback_vol_series,
            fallback_sigma=base_sigma,
        )
        exit_sigma = max(exit_base_sigma * iv_multiplier, 0.01)
        close_mark = spread_mark(
            row["close"], short_strike, long_strike, side, rem_years,
            exit_sigma, risk_free_rate, dividend_yield,
        )
        executable_mark = min(close_mark + exit_slippage, width)
        if bool(config.get("breach_stop")) and touched_now:
            touch_spot = short_strike
            if side == "CCS" and row["open"] >= short_strike:
                touch_spot = row["open"]
            elif side == "PCS" and row["open"] <= short_strike:
                touch_spot = row["open"]
            touch_sigma = max(exit_sigma, sigma)
            touch_mark = spread_mark(
                touch_spot, short_strike, long_strike, side, rem_years,
                touch_sigma, risk_free_rate, dividend_yield,
            )
            exit_idx = j
            exit_reason = "short_strike_breach_stop"
            exit_mark = min(max(close_mark, touch_mark) + exit_slippage, width)
            break
        if executable_mark <= credit * (1.0 - float(config["profit_take_pct"])):
            exit_idx = j
            exit_reason = f"profit_take_{int(float(config['profit_take_pct']) * 100)}pct"
            exit_mark = executable_mark
            break
        if executable_mark >= credit * float(config["stop_loss_multiple"]):
            exit_idx = j
            exit_reason = f"stop_{config['stop_loss_multiple']}x_credit"
            exit_mark = executable_mark
            break

    if exit_idx == exp_idx:
        expiry_touched = (
            (side == "CCS" and expiry["high"] >= short_strike)
            or (side == "PCS" and expiry["low"] <= short_strike)
        )
        touched_short = touched_short or expiry_touched
        if bool(config.get("breach_stop")) and expiry_touched:
            expiry_base_sigma, exit_iv_path = point_in_time_exit_vol(
                rows,
                exp_idx,
                vol_series=vol_series,
                fallback_vol_series=fallback_vol_series,
                fallback_sigma=base_sigma,
            )
            exit_sigma = max(expiry_base_sigma * iv_multiplier, 0.01)
            touch_sigma = max(exit_sigma, sigma)
            touch_spot = short_strike
            if side == "CCS" and expiry["open"] >= short_strike:
                touch_spot = expiry["open"]
            elif side == "PCS" and expiry["open"] <= short_strike:
                touch_spot = expiry["open"]
            touch_mark = spread_mark(
                touch_spot, short_strike, long_strike, side, 1.0 / 365.0,
                touch_sigma, risk_free_rate, dividend_yield,
            )
            exit_reason = "short_strike_breach_stop_expiry_day"
            exit_mark = min(max(exit_mark, touch_mark) + exit_slippage, width)
        elif exit_reason == "expiry":
            exit_sigma = None
            exit_iv_path = "intrinsic_expiry_no_iv"

    pnl_per_share = credit - min(exit_mark, width)
    commission = commission_per_option_contract * 4.0 * contracts
    pnl_dollars = pnl_per_share * 100.0 * contracts - commission
    max_loss_dollars = risk_per_contract * contracts + commission
    max_gain_dollars = credit * 100.0 * contracts - commission
    exit_row = rows[exit_idx]
    return {
        "entry_date": entry["date"],
        "expiry_date": expiry["date"],
        "expiry_target_date": expiry_info["target_date"],
        "expiry_mode": expiry_info["mode"],
        "expiry_calendar_adjusted": expiry_info["calendar_adjusted"],
        "exit_date": exit_row["date"],
        "side": side,
        "role": "defined-risk credit spread",
        "entry_close": rn(spot, 4),
        "exit_close": rn(exit_row["close"], 4),
        "expiry_close": rn(expiry["close"], 4),
        "short_strike": rn(short_strike, 2),
        "long_strike": rn(long_strike, 2),
        "width": rn(width, 2),
        "contracts": contracts,
        "raw_credit_per_share": rn(raw_credit, 4),
        "synthetic_credit_per_share": rn(synthetic_credit, 4),
        "credit_per_share": rn(credit, 4),
        "short_delta": rn(short_delta, 4),
        "model_short_delta": rn(model_short_delta, 4),
        "short_delta_source": short_delta_source,
        "short_observed_iv": rn(snapshot_credit.get("short_observed_iv"), 6) if snapshot_credit else None,
        "exit_mark_per_share": rn(exit_mark, 4),
        "credit_risk": rn(credit_risk, 4),
        "pnl_per_share": rn(pnl_per_share, 4),
        "pnl_dollars": rn(pnl_dollars, 2),
        "max_gain_dollars": rn(max_gain_dollars, 2),
        "max_loss_dollars": rn(max_loss_dollars, 2),
        "risk_per_contract": rn(risk_per_contract, 2),
        "return_on_risk_pct": rn(pnl_dollars / max_loss_dollars * 100.0, 2) if max_loss_dollars else None,
        "win": pnl_dollars > 0,
        "exit_reason": exit_reason,
        "touched_short": touched_short,
        "touched_side": side if touched_short else "none",
        "model_iv": rn(sigma, 4),
        "base_model_iv": rn(base_sigma, 4),
        "signal_model_iv": rn(signal_sigma, 4),
        "entry_iv_path": entry_iv_path,
        "exit_model_iv": rn(exit_sigma, 4),
        "exit_iv_path": exit_iv_path,
        "iv_multiplier": rn(iv_multiplier, 4),
        "dividend_yield": rn(dividend_yield, 6),
        "exit_slippage_per_spread": rn(exit_slippage, 4),
        "regime": state["center"]["regime"],
        "weather": state.get("signal_weather") or classify_weather(entry, rows[entry_idx - 1] if entry_idx else None),
        "quality_score": rn(state["center"]["quality_score"], 2),
        "distance_z": rn(state["center"]["distance_z"], 3),
        "gravity_score": rn((state["center"].get("gravity") or {}).get("gravity_score"), 2),
        "levitation_score": rn((state["center"].get("gravity") or {}).get("levitation_score"), 2),
        "kappa_tstat": rn((state["center"].get("gravity") or {}).get("kappa_tstat"), 3),
        "quality_slope_5d": rn(state["center"].get("quality_slope_5d"), 3),
        "distance_z_slope_5d": rn(state["center"].get("distance_z_slope_5d"), 3),
        "build_up_score": rn(state["volatility_build_up"]["score"], 2),
        "center_price": rn(state["center"]["center_price"], 2),
        "mass_current_price": rn(state["center"].get("current_price", spot), 2),
        "lower_boundary": rn(band["lower_boundary"], 2),
        "upper_boundary": rn(band["upper_boundary"], 2),
        "boundary_horizon_sessions": band.get("horizon_days"),
        "boundary_width_pct": rn(band.get("boundary_width_pct"), 3),
        "boundary_width_slope_5d": rn(band.get("boundary_width_slope_5d"), 3),
        "mass_to_trade_spot_ratio": rn(band.get("mass_to_trade_spot_ratio"), 6),
        "credit_haircut_pct": rn(config.get("credit_haircut_pct"), 2),
        "signal_lag_bars": int(config.get("signal_lag_bars") or 0),
        **snapshot_metrics("", snapshot_credit, synthetic_credit),
        **prefixed_metrics("short", hvn),
    }


def simulate_iron_condor_trade(
    rows,
    entry_idx,
    band,
    state,
    config,
    capital,
    strike_increment=1.0,
    slippage_per_spread=0.02,
    commission_per_option_contract=0.65,
    risk_free_rate=0.04,
    option_snapshots=None,
    explain_rejections=False,
    vol_series=None,
    fallback_vol_series=None,
):
    entry = rows[entry_idx]
    expiry_info = expiry_details(
        rows,
        entry_idx,
        int(config["hold_bars"]),
        mode=config.get("expiry_mode", "weekly_friday"),
    )
    if expiry_info is None:
        return rejection("missing_expiry", entry, side="IC") if explain_rejections else None
    exp_idx = expiry_info["index"]
    expiry = rows[exp_idx]
    spot = entry["close"]
    width = float(config["width"])
    put_short, put_long = choose_short_strike(
        "PCS", spot, band["lower_boundary"], width, strike_increment,
        float(config["min_otm_pct"]), float(config.get("boundary_buffer_pct", 0.0)),
    )
    call_short, call_long = choose_short_strike(
        "CCS", spot, band["upper_boundary"], width, strike_increment,
        float(config["min_otm_pct"]), float(config.get("boundary_buffer_pct", 0.0)),
    )
    if put_short >= call_short:
        return rejection(
            "ic_body_invalid",
            entry,
            expiry,
            "IC",
            put_short_strike=put_short,
            call_short_strike=call_short,
        ) if explain_rejections else None
    years = option_years(entry["date"], expiry["date"])
    signal_sigma = max(state["volatility"].get("annual_vol_used") or 0.20, 0.01)
    base_sigma, entry_iv_path = point_in_time_exit_vol(
        rows,
        entry_idx,
        vol_series=vol_series,
        fallback_vol_series=fallback_vol_series,
        fallback_sigma=signal_sigma,
    )
    put_iv_multiplier = float(config.get("put_iv_multiplier", 1.0) or 1.0)
    call_iv_multiplier = float(config.get("call_iv_multiplier", 1.0) or 1.0)
    put_sigma = max(base_sigma * put_iv_multiplier, 0.01)
    call_sigma = max(base_sigma * call_iv_multiplier, 0.01)
    dividend_yield = float(config.get("dividend_yield", 0.0) or 0.0)
    put_hvn = strike_hvn_metrics(state, spot, put_short, int(config["hold_bars"]), put_sigma)
    call_hvn = strike_hvn_metrics(state, spot, call_short, int(config["hold_bars"]), call_sigma)
    min_hvn_distance = config.get("min_short_hvn_distance_em") or 0.0
    hvn_distances = [
        d for d in (put_hvn.get("nearest_hvn_distance_em"), call_hvn.get("nearest_hvn_distance_em"))
        if d is not None
    ]
    if min_hvn_distance and hvn_distances and min(hvn_distances) < float(min_hvn_distance):
        return rejection(
            "short_hvn_distance_below_min",
            entry,
            expiry,
            "IC",
            put_short_strike=put_short,
            put_long_strike=put_long,
            call_short_strike=call_short,
            call_long_strike=call_long,
            put_short_hvn_distance_em=put_hvn.get("nearest_hvn_distance_em"),
            call_short_hvn_distance_em=call_hvn.get("nearest_hvn_distance_em"),
        ) if explain_rejections else None
    put_raw_synthetic = spread_mark(
        spot, put_short, put_long, "PCS", years, put_sigma,
        risk_free_rate, dividend_yield,
    )
    call_raw_synthetic = spread_mark(
        spot, call_short, call_long, "CCS", years, call_sigma,
        risk_free_rate, dividend_yield,
    )
    put_synthetic_credit = apply_credit_haircut(max(0.0, put_raw_synthetic - slippage_per_spread), config)
    call_synthetic_credit = apply_credit_haircut(max(0.0, call_raw_synthetic - slippage_per_spread), config)
    put_snapshot_credit = None
    call_snapshot_credit = None
    if option_snapshots is not None:
        put_snapshot_credit, put_snapshot_error = snapshot_spread_credit(
            option_snapshots,
            entry["date"],
            expiry["date"],
            "PCS",
            put_short,
            put_long,
            fill_model=config.get("entry_fill_model", "conservative_mid"),
            credit_haircut_pct=config.get("credit_haircut_pct", 0.0),
            fill_edge=config.get("snapshot_fill_edge", 0.25),
            timestamp_policy=config.get("entry_timestamp_policy", "same_day_close"),
            max_age_minutes=config.get("snapshot_max_age_minutes"),
            explain=True,
        )
        call_snapshot_credit, call_snapshot_error = snapshot_spread_credit(
            option_snapshots,
            entry["date"],
            expiry["date"],
            "CCS",
            call_short,
            call_long,
            fill_model=config.get("entry_fill_model", "conservative_mid"),
            credit_haircut_pct=config.get("credit_haircut_pct", 0.0),
            fill_edge=config.get("snapshot_fill_edge", 0.25),
            timestamp_policy=config.get("entry_timestamp_policy", "same_day_close"),
            max_age_minutes=config.get("snapshot_max_age_minutes"),
            explain=True,
        )
        if put_snapshot_credit is None or call_snapshot_credit is None:
            reason = put_snapshot_error or call_snapshot_error or "missing_snapshot_fill"
            return rejection(
                reason,
                entry,
                expiry,
                "IC",
                put_short_strike=put_short,
                put_long_strike=put_long,
                call_short_strike=call_short,
                call_long_strike=call_long,
                synthetic_credit_per_share=rn(put_synthetic_credit + call_synthetic_credit, 4),
                snapshot_fill_available=False,
            ) if explain_rejections else None
        put_raw = put_snapshot_credit["credit"]
        call_raw = call_snapshot_credit["credit"]
        put_credit = put_snapshot_credit["credit"]
        call_credit = call_snapshot_credit["credit"]
    else:
        put_raw = put_raw_synthetic
        call_raw = call_raw_synthetic
        put_credit = put_synthetic_credit
        call_credit = call_synthetic_credit
    body_width = call_short - put_short
    body_pct = body_width / spot if spot else 0.0
    credit_balance = (
        min(put_credit, call_credit) / max(put_credit, call_credit)
        if max(put_credit, call_credit) > 0 else 0.0
    )
    if body_pct < float(config.get("min_ic_body_pct", 0.0)):
        return rejection(
            "ic_body_below_min",
            entry,
            expiry,
            "IC",
            ic_body_pct=rn(body_pct * 100.0, 4),
            snapshot_fill_available=put_snapshot_credit is not None and call_snapshot_credit is not None,
        ) if explain_rejections else None
    if min(put_credit, call_credit) < float(config.get("min_ic_wing_credit", 0.0)):
        return rejection(
            "ic_wing_credit_below_min",
            entry,
            expiry,
            "IC",
            put_credit_per_share=rn(put_credit, 4),
            call_credit_per_share=rn(call_credit, 4),
            snapshot_fill_available=put_snapshot_credit is not None and call_snapshot_credit is not None,
        ) if explain_rejections else None
    if credit_balance < float(config.get("min_ic_credit_balance", 0.0)):
        return rejection(
            "ic_credit_balance_below_min",
            entry,
            expiry,
            "IC",
            ic_credit_balance=rn(credit_balance, 4),
            snapshot_fill_available=put_snapshot_credit is not None and call_snapshot_credit is not None,
        ) if explain_rejections else None
    credit = put_credit + call_credit
    if credit <= 0 or credit < float(config.get("min_credit", 0.0)):
        return rejection(
            "credit_below_min",
            entry,
            expiry,
            "IC",
            credit_per_share=rn(credit, 4),
            synthetic_credit_per_share=rn(put_synthetic_credit + call_synthetic_credit, 4),
            snapshot_fill_available=put_snapshot_credit is not None and call_snapshot_credit is not None,
            entry_credit_source=put_snapshot_credit.get("source") if put_snapshot_credit else "synthetic",
        ) if explain_rejections else None
    put_model_delta = black_scholes_delta(
        spot, put_short, years, put_sigma, "put", risk_free_rate, dividend_yield
    )
    call_model_delta = black_scholes_delta(
        spot, call_short, years, call_sigma, "call", risk_free_rate, dividend_yield
    )
    put_observed_delta = _to_float(
        put_snapshot_credit.get("short_observed_delta") if put_snapshot_credit else None
    )
    call_observed_delta = _to_float(
        call_snapshot_credit.get("short_observed_delta") if call_snapshot_credit else None
    )
    use_put_observed_delta = put_observed_delta is not None and abs(put_observed_delta) <= 1.0
    use_call_observed_delta = call_observed_delta is not None and abs(call_observed_delta) <= 1.0
    put_delta = put_observed_delta if use_put_observed_delta else put_model_delta
    call_delta = call_observed_delta if use_call_observed_delta else call_model_delta
    max_abs_delta = max(abs(put_delta), abs(call_delta))
    if max_abs_delta > float(config.get("max_short_delta", 1.0)):
        return rejection(
            "short_delta_above_max",
            entry,
            expiry,
            "IC",
            short_delta=rn(max_abs_delta, 4),
            snapshot_fill_available=put_snapshot_credit is not None and call_snapshot_credit is not None,
        ) if explain_rejections else None
    risk_per_share = max(width - credit, 0.0)
    if risk_per_share <= 0:
        return rejection(
            "invalid_risk",
            entry,
            expiry,
            "IC",
            credit_per_share=rn(credit, 4),
            snapshot_fill_available=put_snapshot_credit is not None and call_snapshot_credit is not None,
        ) if explain_rejections else None
    credit_risk = credit / risk_per_share
    if credit_risk < float(config["min_credit_risk"]):
        return rejection(
            "credit_to_risk_below_min",
            entry,
            expiry,
            "IC",
            credit_per_share=rn(credit, 4),
            credit_risk=rn(credit_risk, 4),
            synthetic_credit_per_share=rn(put_synthetic_credit + call_synthetic_credit, 4),
            snapshot_fill_available=put_snapshot_credit is not None and call_snapshot_credit is not None,
            entry_credit_source=put_snapshot_credit.get("source") if put_snapshot_credit else "synthetic",
        ) if explain_rejections else None
    risk_per_contract = risk_per_share * 100.0
    round_trip_commission_per_spread = commission_per_option_contract * 8.0
    sized_risk_per_contract = risk_per_contract + round_trip_commission_per_spread
    contracts = int((capital * float(config["risk_per_trade_pct"])) // sized_risk_per_contract)
    contracts = max(0, min(int(config["max_contracts"]), contracts))
    if contracts <= 0:
        return rejection(
            "position_size_zero",
            entry,
            expiry,
            "IC",
            snapshot_fill_available=put_snapshot_credit is not None and call_snapshot_credit is not None,
        ) if explain_rejections else None

    exit_idx = exp_idx
    exit_reason = "expiry"
    put_settle = path_settlement("PCS", expiry["close"], put_short, width)
    call_settle = path_settlement("CCS", expiry["close"], call_short, width)
    exit_mark = max(put_settle, call_settle)
    touched_short = False
    touched_put = False
    touched_call = False
    exit_slippage = max(float(config.get("exit_slippage_per_spread", 0.0) or 0.0), 0.0)
    exit_put_sigma = put_sigma
    exit_call_sigma = call_sigma
    exit_iv_path = "fixed_entry_proxy"
    for j in range(entry_idx + 1, exp_idx):
        row = rows[j]
        put_touched_now = row["low"] <= put_short
        call_touched_now = row["high"] >= call_short
        touched_put = touched_put or put_touched_now
        touched_call = touched_call or call_touched_now
        touched_short = touched_put or touched_call
        rem_years = max(option_years(row["date"], expiry["date"]), 1.0 / 365.0)
        exit_base_sigma, exit_iv_path = point_in_time_exit_vol(
            rows,
            j,
            vol_series=vol_series,
            fallback_vol_series=fallback_vol_series,
            fallback_sigma=base_sigma,
        )
        exit_put_sigma = max(exit_base_sigma * put_iv_multiplier, 0.01)
        exit_call_sigma = max(exit_base_sigma * call_iv_multiplier, 0.01)
        put_mark = spread_mark(
            row["close"], put_short, put_long, "PCS", rem_years,
            exit_put_sigma, risk_free_rate, dividend_yield,
        )
        call_mark = spread_mark(
            row["close"], call_short, call_long, "CCS", rem_years,
            exit_call_sigma, risk_free_rate, dividend_yield,
        )
        close_mark = min(put_mark + call_mark, width)
        executable_mark = min(close_mark + 2.0 * exit_slippage, width)
        if bool(config.get("breach_stop")) and (put_touched_now or call_touched_now):
            touch_put_sigma = max(exit_put_sigma, put_sigma)
            touch_call_sigma = max(exit_call_sigma, call_sigma)
            touch_marks = [close_mark]
            if put_touched_now:
                put_touch_spot = row["open"] if row["open"] <= put_short else put_short
                touch_marks.append(
                    spread_mark(
                        put_touch_spot, put_short, put_long, "PCS", rem_years,
                        touch_put_sigma, risk_free_rate, dividend_yield,
                    )
                    + spread_mark(
                        put_touch_spot, call_short, call_long, "CCS", rem_years,
                        touch_call_sigma, risk_free_rate, dividend_yield,
                    )
                )
            if call_touched_now:
                call_touch_spot = row["open"] if row["open"] >= call_short else call_short
                touch_marks.append(
                    spread_mark(
                        call_touch_spot, put_short, put_long, "PCS", rem_years,
                        touch_put_sigma, risk_free_rate, dividend_yield,
                    )
                    + spread_mark(
                        call_touch_spot, call_short, call_long, "CCS", rem_years,
                        touch_call_sigma, risk_free_rate, dividend_yield,
                    )
                )
            exit_idx = j
            exit_reason = "short_strike_breach_stop"
            exit_mark = min(max(touch_marks) + 2.0 * exit_slippage, width)
            break
        if executable_mark <= credit * (1.0 - float(config["profit_take_pct"])):
            exit_idx = j
            exit_reason = f"profit_take_{int(float(config['profit_take_pct']) * 100)}pct"
            exit_mark = executable_mark
            break
        if executable_mark >= credit * float(config["stop_loss_multiple"]):
            exit_idx = j
            exit_reason = f"stop_{config['stop_loss_multiple']}x_credit"
            exit_mark = executable_mark
            break

    if exit_idx == exp_idx:
        expiry_put_touch = expiry["low"] <= put_short
        expiry_call_touch = expiry["high"] >= call_short
        touched_put = touched_put or expiry_put_touch
        touched_call = touched_call or expiry_call_touch
        touched_short = touched_put or touched_call
        if bool(config.get("breach_stop")) and (expiry_put_touch or expiry_call_touch):
            expiry_base_sigma, exit_iv_path = point_in_time_exit_vol(
                rows,
                exp_idx,
                vol_series=vol_series,
                fallback_vol_series=fallback_vol_series,
                fallback_sigma=base_sigma,
            )
            exit_put_sigma = max(expiry_base_sigma * put_iv_multiplier, 0.01)
            exit_call_sigma = max(expiry_base_sigma * call_iv_multiplier, 0.01)
            touch_put_sigma = max(exit_put_sigma, put_sigma)
            touch_call_sigma = max(exit_call_sigma, call_sigma)
            touch_marks = [exit_mark]
            if expiry_put_touch:
                put_touch_spot = expiry["open"] if expiry["open"] <= put_short else put_short
                touch_marks.append(
                    spread_mark(
                        put_touch_spot, put_short, put_long, "PCS", 1.0 / 365.0,
                        touch_put_sigma, risk_free_rate, dividend_yield,
                    )
                    + spread_mark(
                        put_touch_spot, call_short, call_long, "CCS", 1.0 / 365.0,
                        touch_call_sigma, risk_free_rate, dividend_yield,
                    )
                )
            if expiry_call_touch:
                call_touch_spot = expiry["open"] if expiry["open"] >= call_short else call_short
                touch_marks.append(
                    spread_mark(
                        call_touch_spot, put_short, put_long, "PCS", 1.0 / 365.0,
                        touch_put_sigma, risk_free_rate, dividend_yield,
                    )
                    + spread_mark(
                        call_touch_spot, call_short, call_long, "CCS", 1.0 / 365.0,
                        touch_call_sigma, risk_free_rate, dividend_yield,
                    )
                )
            exit_reason = "short_strike_breach_stop_expiry_day"
            exit_mark = min(max(touch_marks) + 2.0 * exit_slippage, width)
        elif exit_reason == "expiry":
            exit_put_sigma = None
            exit_call_sigma = None
            exit_iv_path = "intrinsic_expiry_no_iv"

    pnl_per_share = credit - min(exit_mark, width)
    commission = commission_per_option_contract * 8.0 * contracts
    pnl_dollars = pnl_per_share * 100.0 * contracts - commission
    max_loss_dollars = risk_per_contract * contracts + commission
    max_gain_dollars = credit * 100.0 * contracts - commission
    exit_row = rows[exit_idx]
    if touched_put and touched_call:
        touched_side = "both"
    elif touched_put:
        touched_side = "put"
    elif touched_call:
        touched_side = "call"
    else:
        touched_side = "none"
    return {
        "entry_date": entry["date"],
        "expiry_date": expiry["date"],
        "expiry_target_date": expiry_info["target_date"],
        "expiry_mode": expiry_info["mode"],
        "expiry_calendar_adjusted": expiry_info["calendar_adjusted"],
        "exit_date": exit_row["date"],
        "side": "IC",
        "role": "defined-risk iron condor",
        "entry_close": rn(spot, 4),
        "exit_close": rn(exit_row["close"], 4),
        "expiry_close": rn(expiry["close"], 4),
        "short_strike": rn((put_short + call_short) / 2.0, 2),
        "long_strike": rn((put_long + call_long) / 2.0, 2),
        "put_short_strike": rn(put_short, 2),
        "put_long_strike": rn(put_long, 2),
        "call_short_strike": rn(call_short, 2),
        "call_long_strike": rn(call_long, 2),
        "width": rn(width, 2),
        "contracts": contracts,
        "raw_credit_per_share": rn(put_raw + call_raw, 4),
        "synthetic_credit_per_share": rn(put_synthetic_credit + call_synthetic_credit, 4),
        "credit_per_share": rn(credit, 4),
        "put_credit_per_share": rn(put_credit, 4),
        "call_credit_per_share": rn(call_credit, 4),
        "put_synthetic_credit_per_share": rn(put_synthetic_credit, 4),
        "call_synthetic_credit_per_share": rn(call_synthetic_credit, 4),
        "ic_body_width": rn(body_width, 2),
        "ic_body_pct": rn(body_pct * 100.0, 3),
        "ic_credit_balance": rn(credit_balance, 4),
        "short_delta": rn(max_abs_delta, 4),
        "put_short_delta": rn(put_delta, 4),
        "call_short_delta": rn(call_delta, 4),
        "put_model_short_delta": rn(put_model_delta, 4),
        "call_model_short_delta": rn(call_model_delta, 4),
        "put_short_delta_source": "snapshot" if use_put_observed_delta else "black_scholes",
        "call_short_delta_source": "snapshot" if use_call_observed_delta else "black_scholes",
        "put_short_observed_iv": rn(put_snapshot_credit.get("short_observed_iv"), 6) if put_snapshot_credit else None,
        "call_short_observed_iv": rn(call_snapshot_credit.get("short_observed_iv"), 6) if call_snapshot_credit else None,
        "exit_mark_per_share": rn(exit_mark, 4),
        "credit_risk": rn(credit_risk, 4),
        "pnl_per_share": rn(pnl_per_share, 4),
        "pnl_dollars": rn(pnl_dollars, 2),
        "max_gain_dollars": rn(max_gain_dollars, 2),
        "max_loss_dollars": rn(max_loss_dollars, 2),
        "risk_per_contract": rn(risk_per_contract, 2),
        "return_on_risk_pct": rn(pnl_dollars / max_loss_dollars * 100.0, 2) if max_loss_dollars else None,
        "win": pnl_dollars > 0,
        "exit_reason": exit_reason,
        "touched_short": touched_short,
        "touched_side": touched_side,
        "model_iv": rn(max(put_sigma, call_sigma), 4),
        "base_model_iv": rn(base_sigma, 4),
        "signal_model_iv": rn(signal_sigma, 4),
        "entry_iv_path": entry_iv_path,
        "exit_put_model_iv": rn(exit_put_sigma, 4),
        "exit_call_model_iv": rn(exit_call_sigma, 4),
        "exit_iv_path": exit_iv_path,
        "put_model_iv": rn(put_sigma, 4),
        "call_model_iv": rn(call_sigma, 4),
        "put_iv_multiplier": rn(put_iv_multiplier, 4),
        "call_iv_multiplier": rn(call_iv_multiplier, 4),
        "dividend_yield": rn(dividend_yield, 6),
        "exit_slippage_per_spread": rn(exit_slippage, 4),
        "regime": state["center"]["regime"],
        "weather": state.get("signal_weather") or classify_weather(entry, rows[entry_idx - 1] if entry_idx else None),
        "quality_score": rn(state["center"]["quality_score"], 2),
        "distance_z": rn(state["center"]["distance_z"], 3),
        "gravity_score": rn((state["center"].get("gravity") or {}).get("gravity_score"), 2),
        "levitation_score": rn((state["center"].get("gravity") or {}).get("levitation_score"), 2),
        "kappa_tstat": rn((state["center"].get("gravity") or {}).get("kappa_tstat"), 3),
        "quality_slope_5d": rn(state["center"].get("quality_slope_5d"), 3),
        "distance_z_slope_5d": rn(state["center"].get("distance_z_slope_5d"), 3),
        "build_up_score": rn(state["volatility_build_up"]["score"], 2),
        "center_price": rn(state["center"]["center_price"], 2),
        "mass_current_price": rn(state["center"].get("current_price", spot), 2),
        "lower_boundary": rn(band["lower_boundary"], 2),
        "upper_boundary": rn(band["upper_boundary"], 2),
        "boundary_horizon_sessions": band.get("horizon_days"),
        "boundary_width_pct": rn(band.get("boundary_width_pct"), 3),
        "boundary_width_slope_5d": rn(band.get("boundary_width_slope_5d"), 3),
        "mass_to_trade_spot_ratio": rn(band.get("mass_to_trade_spot_ratio"), 6),
        "credit_haircut_pct": rn(config.get("credit_haircut_pct"), 2),
        "signal_lag_bars": int(config.get("signal_lag_bars") or 0),
        **snapshot_metrics("", {
            "source": put_snapshot_credit.get("source") if put_snapshot_credit else None,
            "snapshot_ts": put_snapshot_credit.get("snapshot_ts") if put_snapshot_credit else None,
            "snapshot_age_minutes": max_not_none(
                put_snapshot_credit.get("snapshot_age_minutes"),
                call_snapshot_credit.get("snapshot_age_minutes"),
            ) if put_snapshot_credit and call_snapshot_credit else None,
            "snapshot_mid_credit": (
                put_snapshot_credit["snapshot_mid_credit"] + call_snapshot_credit["snapshot_mid_credit"]
                if put_snapshot_credit and call_snapshot_credit else None
            ),
            "snapshot_natural_credit": (
                put_snapshot_credit["snapshot_natural_credit"] + call_snapshot_credit["snapshot_natural_credit"]
                if put_snapshot_credit and call_snapshot_credit else None
            ),
            "snapshot_conservative_credit": (
                put_snapshot_credit["snapshot_conservative_credit"] + call_snapshot_credit["snapshot_conservative_credit"]
                if put_snapshot_credit and call_snapshot_credit else None
            ),
            "snapshot_fill_credit": (
                put_snapshot_credit["snapshot_fill_credit"] + call_snapshot_credit["snapshot_fill_credit"]
                if put_snapshot_credit and call_snapshot_credit else None
            ),
            "credit": credit,
        } if put_snapshot_credit and call_snapshot_credit else None, put_synthetic_credit + call_synthetic_credit),
        **snapshot_metrics("put_", put_snapshot_credit, put_synthetic_credit),
        **snapshot_metrics("call_", call_snapshot_credit, call_synthetic_credit),
        **prefixed_metrics("put_short", put_hvn),
        **prefixed_metrics("call_short", call_hvn),
    }


def config_with(**overrides):
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(overrides)
    if overrides.get("name"):
        cfg["name"] = overrides["name"]
    else:
        cfg["name"] = (
            f"{cfg['side_policy']}_q{int(cfg['quality_min'])}_"
            f"c{int(float(cfg['confidence']) * 100)}_w{str(cfg['width']).replace('.', 'p')}"
        )
        if cfg["side_policy"] == "iron_condor":
            body = float(cfg.get("min_ic_body_pct", 0.0))
            wing = float(cfg.get("min_ic_wing_credit", 0.0))
            balance = float(cfg.get("min_ic_credit_balance", 0.0))
            if body:
                cfg["name"] += f"_body{str(round(body * 100.0, 2)).replace('.', 'p')}"
            if wing:
                cfg["name"] += f"_wing{str(round(wing, 2)).replace('.', 'p')}"
            if balance:
                cfg["name"] += f"_bal{str(round(balance, 2)).replace('.', 'p')}"
    return cfg


def should_enter(row, entry_weekday):
    try:
        return parse_date(row["date"]).weekday() == int(entry_weekday)
    except Exception:
        return False


def run_backtest_on_rows(
    rows,
    mass_rows=None,
    vol_series=None,
    fallback_vol_series=None,
    config=None,
    capital=100000.0,
    lookback=252,
    half_life=63,
    strike_increment=1.0,
    slippage_per_spread=0.02,
    commission_per_option_contract=0.65,
    risk_free_rate=0.04,
    start_trade_date=None,
    end_trade_date=None,
    context_cache=None,
    option_snapshots=None,
):
    validate_backtest_rows(rows, "trade")
    if mass_rows is not None:
        validate_backtest_rows(mass_rows, "mass")
        if len(mass_rows) != len(rows) or any(
            mmb.date_key(a["date"]) != mmb.date_key(b["date"])
            for a, b in zip(rows, mass_rows)
        ):
            raise ValueError("trade and mass rows must be one-to-one date aligned")
    rows = add_indicators([dict(r) for r in rows])
    mass_rows = [dict(r) for r in (mass_rows or rows)]
    cfg = config_with(**(config or {}))
    trades = []
    skipped = []
    rejections = []
    curve = [{"date": rows[0]["date"], "equity": capital}]
    equity = capital
    max_hold = int(cfg["hold_bars"])
    start_idx = max(int(lookback), 60)
    signal_lag = max(0, int(cfg.get("signal_lag_bars") or 0))
    active_until_idx = -1
    date_to_idx = {mmb.date_key(row["date"]): idx for idx, row in enumerate(rows)}

    for i in range(start_idx, len(rows) - max_hold):
        row = rows[i]
        row_date = mmb.date_key(row["date"])
        if start_trade_date and row_date < start_trade_date:
            continue
        if end_trade_date and row_date > end_trade_date:
            continue
        if not should_enter(row, cfg["entry_weekday"]):
            continue
        if not cfg.get("allow_overlapping_positions") and i <= active_until_idx:
            skipped.append({"date": row["date"], "reason": "capital_already_reserved"})
            continue
        preview_expiry = expiry_details(
            rows,
            i,
            int(cfg["hold_bars"]),
            mode=cfg.get("expiry_mode", "weekly_friday"),
        )
        if preview_expiry is None:
            skipped.append({"date": row["date"], "reason": "missing_expiry"})
            continue
        try:
            confidence_key = round(float(cfg["confidence"]), 4)
            context_idx = i - signal_lag
            if context_idx < start_idx:
                skipped.append({"date": row["date"], "reason": "signal_lag_context_unavailable"})
                continue
            signal_horizon = preview_expiry["index"] - context_idx
            cached = (
                (context_cache or {}).get(context_idx)
                if (context_cache or {}).get(context_idx, {}).get("horizon") == signal_horizon
                else None
            )
            if cached and confidence_key in cached["bands"]:
                state = cached["state"]
                band = dict(cached["bands"][confidence_key])
                if context_idx != i:
                    context_spot = float(rows[context_idx]["close"])
                    entry_spot = float(row["close"])
                    ratio = entry_spot / context_spot
                    for key in (
                        "lower_boundary", "upper_boundary", "lower_zone_low",
                        "lower_zone_high", "upper_zone_low", "upper_zone_high",
                        "vol_lower", "vol_upper", "mass_lower", "mass_upper",
                    ):
                        if band.get(key) is not None:
                            band[key] *= ratio
                    band["mass_to_trade_spot_ratio"] = (
                        entry_spot / float(state["center"]["current_price"])
                    )
                weather = cached["weather"]
            else:
                state, band, weather = build_entry_context(
                    rows,
                    mass_rows,
                    context_idx,
                    vol_series or {},
                    fallback_vol_series or {},
                    lookback=lookback,
                    half_life=half_life,
                    horizon=signal_horizon,
                    confidence=float(cfg["confidence"]),
                    boundary_model=cfg.get("boundary_model", "mass_vol"),
                    map_trade_spot=row["close"],
                )
        except Exception as exc:
            skipped.append({"date": row["date"], "reason": f"context_error:{type(exc).__name__}"})
            continue
        center = state["center"]
        build = state["volatility_build_up"]
        if center["quality_score"] < float(cfg["quality_min"]):
            skipped.append({"date": row["date"], "reason": "quality_below_min"})
            continue
        if build["score"] > float(cfg["max_build_up"]):
            skipped.append({"date": row["date"], "reason": "build_up_above_max"})
            continue
        if abs(center["distance_z"]) > float(cfg.get("max_abs_distance_z", 99.0)):
            skipped.append({"date": row["date"], "reason": "distance_z_above_max"})
            continue
        gravity = center.get("gravity") or {}
        if (gravity.get("gravity_score") or 0.0) < float(cfg.get("min_gravity_score", 0.0)):
            skipped.append({"date": row["date"], "reason": "gravity_below_min"})
            continue
        if (gravity.get("levitation_score") or 0.0) > float(cfg.get("max_levitation_score", 100.0)):
            skipped.append({"date": row["date"], "reason": "levitation_above_max"})
            continue
        quality_slope = center.get("quality_slope_5d")
        if quality_slope is not None and quality_slope < -float(cfg.get("max_quality_drop_5d", 999.0)):
            skipped.append({"date": row["date"], "reason": "quality_slope_drop"})
            continue
        distance_slope = center.get("distance_z_slope_5d")
        if distance_slope is not None and abs(distance_slope) > float(cfg.get("max_abs_distance_z_slope_5d", 999.0)):
            skipped.append({"date": row["date"], "reason": "distance_z_slope_above_max"})
            continue
        sides = side_candidates(cfg["side_policy"], weather, center, build)
        if not sides:
            skipped.append({"date": row["date"], "reason": f"no_side:{weather}:{center['regime']}"})
            continue
        if "IC" in sides:
            if is_weather_blocked_for_ic(weather, cfg):
                skipped.append({"date": row["date"], "reason": f"IC:blocked_weather:{weather}"})
                continue
            trade = simulate_iron_condor_trade(
                rows,
                i,
                band,
                state,
                cfg,
                capital=equity,
                strike_increment=strike_increment,
                slippage_per_spread=slippage_per_spread,
                commission_per_option_contract=commission_per_option_contract,
                risk_free_rate=risk_free_rate,
                option_snapshots=option_snapshots,
                explain_rejections=True,
                vol_series=vol_series,
                fallback_vol_series=fallback_vol_series,
            )
            if trade is None:
                skipped.append({"date": row["date"], "reason": "IC:premium_delta_or_size_filter"})
                continue
            if trade.get("_rejected"):
                if option_snapshots is not None:
                    rejections.append(trade)
                skipped.append({"date": row["date"], "reason": f"IC:{trade['reason']}"})
                continue
            equity += trade["pnl_dollars"]
            trade["equity_after"] = rn(equity, 2)
            trades.append(trade)
            curve.append({"date": trade["exit_date"], "equity": equity})
            active_until_idx = max(
                active_until_idx,
                date_to_idx.get(mmb.date_key(trade["exit_date"]), i),
            )
            continue
        for side in sides:
            trade = simulate_trade(
                rows,
                i,
                side,
                band,
                state,
                cfg,
                capital=equity,
                strike_increment=strike_increment,
                slippage_per_spread=slippage_per_spread,
                commission_per_option_contract=commission_per_option_contract,
                risk_free_rate=risk_free_rate,
                option_snapshots=option_snapshots,
                explain_rejections=True,
                vol_series=vol_series,
                fallback_vol_series=fallback_vol_series,
            )
            if trade is None:
                skipped.append({"date": row["date"], "reason": f"{side}:premium_or_size_filter"})
                continue
            if trade.get("_rejected"):
                if option_snapshots is not None:
                    rejections.append(trade)
                skipped.append({"date": row["date"], "reason": f"{side}:{trade['reason']}"})
                continue
            equity += trade["pnl_dollars"]
            trade["equity_after"] = rn(equity, 2)
            trades.append(trade)
            curve.append({"date": trade["exit_date"], "equity": equity})
            active_until_idx = max(
                active_until_idx,
                date_to_idx.get(mmb.date_key(trade["exit_date"]), i),
            )
    evaluation_start = start_trade_date or rows[start_idx]["date"]
    evaluation_end = end_trade_date or rows[-1]["date"]
    summary = summarize_result(
        trades,
        curve,
        cfg,
        capital,
        evaluation_start,
        evaluation_end,
        skipped,
        rejections,
    )
    return {
        "config": cfg,
        "summary": summary,
        "loss_analysis": loss_analysis(trades),
        "hvn_outcome_analysis": hvn_outcome_analysis(trades),
        "trades": trades,
        "curve": curve,
        "skipped": skipped,
        "snapshot_rejections": rejections,
    }


def summarize_result(trades, curve, cfg, capital, start_date, end_date, skipped=None, rejections=None):
    wins = sum(1 for t in trades if t["win"])
    losses = len(trades) - wins
    gross_profit = sum(t["pnl_dollars"] for t in trades if t["pnl_dollars"] > 0)
    gross_loss = -sum(t["pnl_dollars"] for t in trades if t["pnl_dollars"] < 0)
    total_pnl = sum(t["pnl_dollars"] for t in trades)
    final_equity = capital + total_pnl
    values = [r["equity"] for r in curve] or [capital]
    total_risk = sum(t["max_loss_dollars"] for t in trades)
    hvn_distances = []
    credit_errors = []
    snapshot_ages = []
    for trade in trades:
        candidates = [
            trade.get("short_hvn_distance_em"),
            trade.get("put_short_hvn_distance_em"),
            trade.get("call_short_hvn_distance_em"),
        ]
        candidates = [x for x in candidates if x is not None]
        if candidates:
            hvn_distances.append(min(candidates))
        if trade.get("credit_error_pct") is not None:
            credit_errors.append(trade["credit_error_pct"])
        if trade.get("snapshot_age_minutes") is not None:
            snapshot_ages.append(trade["snapshot_age_minutes"])
    rejections = rejections or []
    snapshot_mode = cfg.get("entry_fill_model") != "synthetic"
    attempted_structures = len(trades) + len(rejections) if snapshot_mode else 0
    trades_with_snapshot_entry = sum(
        1 for t in trades if str(t.get("entry_credit_source") or "").startswith("snapshot:")
    )
    trades_falling_back_to_synthetic = sum(
        1 for t in trades
        if cfg.get("entry_fill_model") != "synthetic"
        and not str(t.get("entry_credit_source") or "").startswith("snapshot:")
    )
    rejections_with_snapshot_fill = sum(1 for r in rejections if r.get("snapshot_fill_available"))
    snapshot_fill_coverage_count = trades_with_snapshot_entry + rejections_with_snapshot_fill
    snapshot_fill_coverage_pct = (
        snapshot_fill_coverage_count / attempted_structures * 100.0 if attempted_structures else None
    )
    by_side = {}
    for side in ("PCS", "CCS", "IC"):
        side_trades = [t for t in trades if t["side"] == side]
        side_wins = sum(1 for t in side_trades if t["win"])
        by_side[side] = {
            "trades": len(side_trades),
            "win_rate_pct": side_wins / len(side_trades) * 100.0 if side_trades else None,
            "pnl_dollars": sum(t["pnl_dollars"] for t in side_trades),
        }
    return {
        "name": cfg["name"],
        "side_policy": cfg["side_policy"],
        "boundary_model": cfg.get("boundary_model", "mass_vol"),
        "confidence": cfg["confidence"],
        "quality_min": cfg["quality_min"],
        "max_build_up": cfg["max_build_up"],
        "max_abs_distance_z": cfg.get("max_abs_distance_z"),
        "min_gravity_score": cfg.get("min_gravity_score"),
        "max_levitation_score": cfg.get("max_levitation_score"),
        "min_short_hvn_distance_em": cfg.get("min_short_hvn_distance_em"),
        "credit_haircut_pct": cfg.get("credit_haircut_pct"),
        "signal_lag_bars": cfg.get("signal_lag_bars"),
        "entry_fill_model": cfg.get("entry_fill_model"),
        "snapshot_max_age_days": cfg.get("snapshot_max_age_days"),
        "snapshot_max_age_minutes": cfg.get("snapshot_max_age_minutes"),
        "entry_timestamp_policy": cfg.get("entry_timestamp_policy"),
        "require_snapshot_fills": cfg.get("require_snapshot_fills"),
        "min_snapshot_fill_coverage": cfg.get("min_snapshot_fill_coverage"),
        "max_quality_drop_5d": cfg.get("max_quality_drop_5d"),
        "max_abs_distance_z_slope_5d": cfg.get("max_abs_distance_z_slope_5d"),
        "min_credit": cfg.get("min_credit"),
        "min_credit_risk": cfg["min_credit_risk"],
        "boundary_buffer_pct": cfg.get("boundary_buffer_pct"),
        "max_short_delta": cfg.get("max_short_delta"),
        "min_ic_body_pct": cfg.get("min_ic_body_pct"),
        "min_ic_wing_credit": cfg.get("min_ic_wing_credit"),
        "min_ic_credit_balance": cfg.get("min_ic_credit_balance"),
        "width": cfg["width"],
        "hold_bars": cfg["hold_bars"],
        "entry_weekday": cfg["entry_weekday"],
        "expiry_mode": cfg.get("expiry_mode"),
        "breach_stop": cfg.get("breach_stop"),
        "allow_overlapping_positions": bool(cfg.get("allow_overlapping_positions")),
        "dividend_yield": cfg.get("dividend_yield"),
        "put_iv_multiplier": cfg.get("put_iv_multiplier"),
        "call_iv_multiplier": cfg.get("call_iv_multiplier"),
        "exit_slippage_per_spread": cfg.get("exit_slippage_per_spread"),
        "evaluation_start_date": mmb.date_key(start_date),
        "evaluation_end_date": mmb.date_key(end_date),
        "blocked_ic_weather": cfg.get("blocked_ic_weather"),
        "trades": len(trades),
        "attempted_structures": attempted_structures,
        "wins": wins,
        "losses": losses,
        "win_rate_pct": wins / len(trades) * 100.0 if trades else None,
        "total_pnl_dollars": total_pnl,
        "final_equity": final_equity,
        "total_return_pct": (final_equity / capital - 1.0) * 100.0,
        "return_basis": "starting account capital; idle cash interest and collateral yield excluded",
        "annualized_return_pct": (
            annualized_return(capital, final_equity, start_date, end_date) * 100.0
            if final_equity > 0 else None
        ),
        "max_drawdown_pct": max_drawdown(values) * 100.0,
        "drawdown_measurement": "realized trade-exit equity only; intratrade mark-to-market drawdown unavailable",
        "avg_pnl_dollars": total_pnl / len(trades) if trades else None,
        "avg_return_on_risk_pct": (
            sum(t["return_on_risk_pct"] for t in trades if t["return_on_risk_pct"] is not None) / len(trades)
            if trades else None
        ),
        "profit_factor": gross_profit / gross_loss if gross_loss > 0 else None,
        "profit_factor_censored_no_losses": gross_loss == 0 and gross_profit > 0,
        "gross_profit_dollars": gross_profit,
        "gross_loss_dollars": gross_loss,
        "total_risk_dollars": total_risk,
        "return_on_total_risk_pct": total_pnl / total_risk * 100.0 if total_risk else None,
        "touched_short_rate_pct": (
            sum(1 for t in trades if t["touched_short"]) / len(trades) * 100.0 if trades else None
        ),
        "avg_short_delta_abs": (
            sum(abs(t["short_delta"]) for t in trades if t.get("short_delta") is not None) / len(trades)
            if trades else None
        ),
        "avg_gravity_score": (
            sum(t["gravity_score"] for t in trades if t.get("gravity_score") is not None) / len(trades)
            if trades else None
        ),
        "avg_levitation_score": (
            sum(t["levitation_score"] for t in trades if t.get("levitation_score") is not None) / len(trades)
            if trades else None
        ),
        "avg_short_hvn_distance_em": (
            sum(hvn_distances) / len(hvn_distances) if hvn_distances else None
        ),
        "avg_credit_error_pct": (
            sum(credit_errors) / len(credit_errors) if credit_errors else None
        ),
        "pricing_source": (
            "no_trades" if not trades else
            "synthetic_only" if trades_with_snapshot_entry == 0 else
            (cfg.get("entry_fill_model") if trades_with_snapshot_entry == len(trades) and trades else "mixed")
        ),
        "snapshot_fill_coverage_pct": (
            snapshot_fill_coverage_pct
        ),
        "snapshot_coverage_gate_pass": (
            snapshot_fill_coverage_pct is not None
            and snapshot_fill_coverage_pct >= float(cfg.get("min_snapshot_fill_coverage") or 0.0)
            and (not cfg.get("require_snapshot_fills") or trades_falling_back_to_synthetic == 0)
        ) if snapshot_mode else None,
        "trades_with_snapshot_entry": trades_with_snapshot_entry,
        "trades_with_snapshot_exit": 0,
        "trades_falling_back_to_synthetic": trades_falling_back_to_synthetic,
        "snapshot_rejection_count": len(rejections),
        "rejections_with_snapshot_fill": rejections_with_snapshot_fill,
        "avg_snapshot_age_minutes": (
            sum(snapshot_ages) / len(snapshot_ages) if snapshot_ages else None
        ),
        "max_snapshot_age_minutes": max(snapshot_ages) if snapshot_ages else None,
        "avg_ic_body_pct": (
            sum(t["ic_body_pct"] for t in trades if t.get("ic_body_pct") is not None) / by_side["IC"]["trades"]
            if by_side["IC"]["trades"] else None
        ),
        "avg_ic_credit_balance": (
            sum(t["ic_credit_balance"] for t in trades if t.get("ic_credit_balance") is not None) / by_side["IC"]["trades"]
            if by_side["IC"]["trades"] else None
        ),
        "skipped": len(skipped or []),
        "pcs": by_side["PCS"],
        "ccs": by_side["CCS"],
        "ic": by_side["IC"],
    }


def trade_hvn_distance(trade):
    candidates = [
        trade.get("short_hvn_distance_em"),
        trade.get("put_short_hvn_distance_em"),
        trade.get("call_short_hvn_distance_em"),
    ]
    candidates = [x for x in candidates if x is not None]
    return min(candidates) if candidates else None


def classify_loss_causes(trade):
    causes = []
    if trade.get("touched_short"):
        causes.append("short_strike_touched")
    if str(trade.get("exit_reason") or "").startswith("stop_"):
        causes.append("stop_loss")
    hvn_distance = trade_hvn_distance(trade)
    if hvn_distance is not None and hvn_distance < 0.75:
        causes.append("hvn_magnet_risk")
    gravity = trade.get("gravity_score")
    levitation = trade.get("levitation_score")
    if gravity is not None and gravity < 60:
        causes.append("weak_gravity")
    if levitation is not None and levitation > 55:
        causes.append("high_levitation")
    if trade.get("build_up_score") is not None and trade["build_up_score"] > 45:
        causes.append("stored_energy")
    if trade.get("credit_risk") is not None and trade["credit_risk"] < 0.15:
        causes.append("thin_credit")
    if trade.get("entry_close") and trade.get("expiry_close"):
        move = abs(trade["expiry_close"] / trade["entry_close"] - 1.0) * 100.0
        if move >= 3.0:
            causes.append("large_expiry_move")
    if not causes:
        causes.append("unclassified")
    return causes


def loss_analysis(trades):
    losses = [t for t in trades if not t.get("win")]
    rows = []
    buckets = {}
    for trade in losses:
        causes = classify_loss_causes(trade)
        for cause in causes:
            buckets[cause] = buckets.get(cause, 0) + 1
        rows.append({
            "entry_date": trade.get("entry_date"),
            "expiry_date": trade.get("expiry_date"),
            "side": trade.get("side"),
            "touched_side": trade.get("touched_side"),
            "pnl_dollars": trade.get("pnl_dollars"),
            "credit_per_share": trade.get("credit_per_share"),
            "credit_risk": trade.get("credit_risk"),
            "short_delta": trade.get("short_delta"),
            "gravity_score": trade.get("gravity_score"),
            "levitation_score": trade.get("levitation_score"),
            "quality_score": trade.get("quality_score"),
            "build_up_score": trade.get("build_up_score"),
            "distance_z": trade.get("distance_z"),
            "hvn_distance_em": trade_hvn_distance(trade),
            "entry_close": trade.get("entry_close"),
            "expiry_close": trade.get("expiry_close"),
            "exit_reason": trade.get("exit_reason"),
            "causes": ",".join(causes),
        })
    return {
        "losses": len(losses),
        "buckets": [{"cause": k, "count": buckets[k]} for k in sorted(buckets, key=lambda x: (-buckets[x], x))],
        "rows": rows,
    }


def _median(values):
    values = sorted(v for v in (_to_float(x) for x in values) if v is not None)
    if not values:
        return None
    mid = len(values) // 2
    if len(values) % 2:
        return values[mid]
    return (values[mid - 1] + values[mid]) / 2.0


def hvn_outcome_analysis(trades):
    groups = {
        "all": trades,
        "winners": [t for t in trades if t.get("win")],
        "losers": [t for t in trades if not t.get("win")],
        "touched_winners": [t for t in trades if t.get("win") and t.get("touched_short")],
        "touched_losers": [t for t in trades if (not t.get("win")) and t.get("touched_short")],
        "untouched_winners": [t for t in trades if t.get("win") and not t.get("touched_short")],
    }
    rows = []
    for name, items in groups.items():
        distances = [trade_hvn_distance(t) for t in items]
        distances = [d for d in distances if d is not None]
        near_count = sum(1 for d in distances if d < 0.20)
        magnet_count = sum(1 for d in distances if d < 0.75)
        rows.append({
            "group": name,
            "trades": len(items),
            "with_hvn_metric": len(distances),
            "avg_hvn_distance_em": rn(sum(distances) / len(distances), 4) if distances else None,
            "median_hvn_distance_em": rn(_median(distances), 4),
            "min_hvn_distance_em": rn(min(distances), 4) if distances else None,
            "near_hvn_lt_0p20_count": near_count,
            "near_hvn_lt_0p20_rate_pct": rn(near_count / len(distances) * 100.0, 2) if distances else None,
            "magnet_hvn_lt_0p75_count": magnet_count,
            "magnet_hvn_lt_0p75_rate_pct": rn(magnet_count / len(distances) * 100.0, 2) if distances else None,
        })
    return rows


def _grid_values(base, key, default_values):
    override = base.get(f"grid_{key}")
    if override:
        return mmb.parse_num_list(override, float)
    return default_values


def sweep_configs(base=None):
    base = dict(base or {})
    explicit_base = dict(base)
    for key in (
        "side_policy", "confidence", "quality_min", "max_build_up",
        "min_credit_risk", "min_gravity_score", "max_levitation_score",
        "width", "min_ic_body_pct", "min_ic_wing_credit", "min_ic_credit_balance",
    ):
        explicit_base.pop(key, None)
    configs = []
    policies = ["mass_adaptive", "pcs_only", "ccs_only", "iron_condor"]
    confidences = _grid_values(base, "confidences", [0.68, 0.80, 0.90])
    quality_mins = _grid_values(base, "min_qualities", [55.0, 65.0, 75.0])
    max_builds = _grid_values(base, "max_builds", [45.0, 65.0, 85.0])
    min_credit_risks = _grid_values(base, "credit_to_risks", [0.10, 0.16, 0.22])
    min_gravity_scores = _grid_values(base, "min_gravity_scores", [float(base.get("min_gravity_score", DEFAULT_CONFIG["min_gravity_score"]))])
    max_levitation_scores = _grid_values(base, "max_levitation_scores", [float(base.get("max_levitation_score", DEFAULT_CONFIG["max_levitation_score"]))])
    widths = _grid_values(base, "widths", [2.0, 3.0, 5.0, 7.0])
    ic_body_pcts = _grid_values(base, "min_ic_body_pcts", [float(base.get("min_ic_body_pct", DEFAULT_CONFIG["min_ic_body_pct"]))])
    ic_wing_credits = _grid_values(
        base, "min_ic_wing_credits", [float(base.get("min_ic_wing_credit", DEFAULT_CONFIG["min_ic_wing_credit"]))]
    )
    ic_credit_balances = _grid_values(
        base, "min_ic_credit_balances", [float(base.get("min_ic_credit_balance", DEFAULT_CONFIG["min_ic_credit_balance"]))]
    )
    for policy in policies:
        policy_body_pcts = ic_body_pcts if policy == "iron_condor" else [float(base.get("min_ic_body_pct", 0.0))]
        policy_wing_credits = ic_wing_credits if policy == "iron_condor" else [float(base.get("min_ic_wing_credit", 0.0))]
        policy_credit_balances = (
            ic_credit_balances if policy == "iron_condor" else [float(base.get("min_ic_credit_balance", 0.0))]
        )
        for confidence in confidences:
            for quality_min in quality_mins:
                for max_build_up in max_builds:
                    for min_credit_risk in min_credit_risks:
                        for min_gravity_score in min_gravity_scores:
                            for max_levitation_score in max_levitation_scores:
                                for width in widths:
                                    for min_ic_body_pct in policy_body_pcts:
                                        for min_ic_wing_credit in policy_wing_credits:
                                            for min_ic_credit_balance in policy_credit_balances:
                                                configs.append(config_with(
                                                    **explicit_base,
                                                    side_policy=policy,
                                                    confidence=confidence,
                                                    quality_min=quality_min,
                                                    max_build_up=max_build_up,
                                                    min_credit_risk=min_credit_risk,
                                                    min_gravity_score=min_gravity_score,
                                                    max_levitation_score=max_levitation_score,
                                                    width=width,
                                                    min_ic_body_pct=min_ic_body_pct,
                                                    min_ic_wing_credit=min_ic_wing_credit,
                                                    min_ic_credit_balance=min_ic_credit_balance,
                                                ))
    return configs


def score_summary(summary):
    trades = summary["trades"]
    if trades < 12:
        return -999999.0 + trades
    win = (summary["win_rate_pct"] or 0.0) / 100.0
    ret = summary["total_return_pct"] or 0.0
    dd = abs(summary["max_drawdown_pct"] or 0.0)
    pf = 5.0 if summary.get("profit_factor_censored_no_losses") else min(summary["profit_factor"] or 0.0, 5.0)
    risk_ret = summary["return_on_total_risk_pct"] or 0.0
    touch = (summary["touched_short_rate_pct"] or 0.0) / 100.0
    return ret + 6.0 * pf + 7.5 * win + 0.55 * risk_ret - 0.85 * dd - 4.0 * touch


def split_rows(rows, train_fraction=0.70):
    cut = max(80, min(len(rows) - 30, int(len(rows) * train_fraction)))
    return rows[:cut], rows[cut - 252 if cut > 252 else 0:]


def split_trade_mass_rows(rows, mass_rows, train_fraction=0.70, context_bars=252):
    cut = max(80, min(len(rows) - 30, int(len(rows) * train_fraction)))
    context_bars = max(60, int(context_bars))
    test_start = max(0, cut - context_bars)
    return rows[:cut], mass_rows[:cut], rows[test_start:], mass_rows[test_start:]


def calendar_walk_forward_windows(
    rows,
    train_years=3,
    min_train_sessions_per_year=200,
    min_test_sessions=20,
):
    by_year = {}
    for i, row in enumerate(rows):
        by_year.setdefault(parse_date(row["date"]).year, []).append(i)
    years = sorted(by_year)
    windows = []
    for test_year in years:
        train_start_year = test_year - train_years
        train_year_set = set(range(train_start_year, test_year))
        if not train_year_set.issubset(by_year):
            continue
        if any(
            len(by_year[year]) < int(min_train_sessions_per_year)
            for year in train_year_set
        ):
            continue
        train_indices = [i for y in range(train_start_year, test_year) for i in by_year[y]]
        test_indices = by_year[test_year]
        if not train_indices or len(test_indices) < int(min_test_sessions):
            continue
        windows.append({
            "train_start": rows[train_indices[0]]["date"],
            "train_end": rows[train_indices[-1]]["date"],
            "test_start": rows[test_indices[0]]["date"],
            "test_end": rows[test_indices[-1]]["date"],
            "context_start_idx": train_indices[0],
            "context_end_idx": test_indices[-1] + 1,
            "test_start_date": mmb.date_key(rows[test_indices[0]]["date"]),
            "test_end_date": mmb.date_key(rows[test_indices[-1]]["date"]),
            "test_session_count": len(test_indices),
            "test_year_complete": len(test_indices) >= int(min_train_sessions_per_year),
        })
    return windows


def summarize_aggregate(name, rows):
    if not rows:
        return {"name": name, "windows": 0, "trades": 0}
    trades = sum(r["trades"] for r in rows)
    wins = sum(r["wins"] for r in rows)
    losses = sum(r["losses"] for r in rows)
    pnl = sum(r["total_pnl_dollars"] for r in rows)
    gross_profit = sum(r["gross_profit_dollars"] for r in rows)
    gross_loss = sum(r["gross_loss_dollars"] for r in rows)
    risk = sum(r["total_risk_dollars"] for r in rows)
    worst_dd = min((r["max_drawdown_pct"] for r in rows), default=0.0)
    touched_count_proxy = sum((r["touched_short_rate_pct"] or 0.0) * r["trades"] / 100.0 for r in rows)
    return {
        "name": name,
        "windows": len(rows),
        "trades": trades,
        "wins": wins,
        "losses": losses,
        "win_rate_pct": wins / trades * 100.0 if trades else None,
        "total_pnl_dollars": pnl,
        "avg_window_pnl_dollars": pnl / len(rows),
        "profit_factor": gross_profit / gross_loss if gross_loss > 0 else None,
        "profit_factor_censored_no_losses": gross_loss == 0 and gross_profit > 0,
        "return_on_total_risk_pct": pnl / risk * 100.0 if risk else None,
        "worst_window_drawdown_pct": worst_dd,
        "touched_short_rate_pct": touched_count_proxy / trades * 100.0 if trades else None,
    }


def run_calendar_walk_forward(
    rows,
    mass_rows,
    configs,
    vol_series,
    fallback_vol_series,
    capital,
    lookback,
    half_life,
    strike_increment,
    slippage_per_spread,
    commission_per_option_contract,
    risk_free_rate,
    train_years=3,
    option_snapshots=None,
    min_train_sessions_per_year=200,
    min_test_sessions=20,
):
    windows = calendar_walk_forward_windows(
        rows,
        train_years=train_years,
        min_train_sessions_per_year=min_train_sessions_per_year,
        min_test_sessions=min_test_sessions,
    )
    if not windows:
        return {"available": False, "reason": "not enough full calendar years", "windows": []}
    if not configs:
        return {"available": False, "reason": "no candidate configurations", "windows": []}
    details = []
    walk_forward_equity = float(capital)
    confidences = sorted({cfg["confidence"] for cfg in configs})
    for window in windows:
        start = window["context_start_idx"]
        end = window["context_end_idx"]
        context_rows = rows[start:end]
        context_mass_rows = mass_rows[start:end]
        cache = build_context_cache(
            context_rows,
            context_mass_rows,
            vol_series,
            fallback_vol_series,
            lookback,
            half_life,
            (
                int(configs[0]["hold_bars"])
                + max(0, int(configs[0].get("signal_lag_bars") or 0))
                if configs else DEFAULT_CONFIG["hold_bars"] + DEFAULT_CONFIG["signal_lag_bars"]
            ),
            confidences,
            boundary_model=configs[0].get("boundary_model", "mass_vol") if configs else DEFAULT_CONFIG["boundary_model"],
            start_trade_date=mmb.date_key(window["train_start"]),
            end_trade_date=window["test_end_date"],
        )
        ranked_train = []
        for cfg in configs:
            train_result = run_backtest_on_rows(
                context_rows,
                mass_rows=context_mass_rows,
                vol_series=vol_series,
                fallback_vol_series=fallback_vol_series,
                config=cfg,
                capital=capital,
                lookback=lookback,
                half_life=half_life,
                strike_increment=strike_increment,
                slippage_per_spread=slippage_per_spread,
                commission_per_option_contract=commission_per_option_contract,
                risk_free_rate=risk_free_rate,
                start_trade_date=mmb.date_key(window["train_start"]),
                end_trade_date=mmb.date_key(window["train_end"]),
                context_cache=cache,
                option_snapshots=option_snapshots,
            )
            train_summary = dict(train_result["summary"])
            ranked_train.append((score_summary(train_summary), cfg, train_summary))
        # Deterministic tie-break prevents filesystem/order noise from changing
        # a selected model with identical training scores.
        ranked_train.sort(key=lambda item: (item[0], item[1]["name"]), reverse=True)
        train_score, selected, train_summary = ranked_train[0]
        test_result = run_backtest_on_rows(
            context_rows,
            mass_rows=context_mass_rows,
            vol_series=vol_series,
            fallback_vol_series=fallback_vol_series,
            config=selected,
            capital=walk_forward_equity,
            lookback=lookback,
            half_life=half_life,
            strike_increment=strike_increment,
            slippage_per_spread=slippage_per_spread,
            commission_per_option_contract=commission_per_option_contract,
            risk_free_rate=risk_free_rate,
            start_trade_date=window["test_start_date"],
            end_trade_date=window["test_end_date"],
            context_cache=cache,
            option_snapshots=option_snapshots,
        )
        row = dict(test_result["summary"])
        window_start_capital = walk_forward_equity
        walk_forward_equity += float(row.get("total_pnl_dollars") or 0.0)
        row.update({k: window[k] for k in ("train_start", "train_end", "test_start", "test_end")})
        row.update({
            "selected_config": selected["name"],
            "train_score": train_score,
            "train_trades": train_summary["trades"],
            "train_total_return_pct": train_summary["total_return_pct"],
            "candidate_count": len(configs),
            "window_start_capital": window_start_capital,
            "window_end_capital": walk_forward_equity,
        })
        details.append(row)
    aggregate = summarize_aggregate("nested_window_selection", details)
    aggregate.update({
        "selection_mode": "nested_train_then_next_calendar_year_test",
        "candidate_count": len(configs),
        "unique_selected_configs": len({row["selected_config"] for row in details}),
        "initial_capital": float(capital),
        "final_equity": walk_forward_equity,
        "total_return_pct": (walk_forward_equity / float(capital) - 1.0) * 100.0,
    })
    return {
        "available": True,
        "train_years": train_years,
        "nested_model_selection": True,
        "selection_mode": "each test-year configuration selected only on its preceding training window",
        "candidate_count": len(configs),
        "windows": windows,
        "summary": [aggregate],
        "details": details,
    }


def run_sweep(
    rows,
    mass_rows,
    vol_series,
    fallback_vol_series,
    base_config,
    capital,
    lookback,
    half_life,
    strike_increment,
    slippage_per_spread,
    commission_per_option_contract,
    risk_free_rate,
    limit=None,
    option_snapshots=None,
):
    train_rows, train_mass_rows, test_rows, test_mass_rows = split_trade_mass_rows(
        rows,
        mass_rows or rows,
        context_bars=max(int(lookback), 60),
    )
    holdout_start_date = mmb.date_key(rows[len(train_rows)]["date"])
    train_results = []
    configs = sweep_configs(base_config)
    if limit and len(configs) > int(limit):
        limit = int(limit)
        step = len(configs) / float(limit)
        configs = [configs[min(len(configs) - 1, int(i * step))] for i in range(limit)]
    confidences = sorted({cfg["confidence"] for cfg in configs})
    train_cache = build_context_cache(
        train_rows, train_mass_rows, vol_series, fallback_vol_series,
        lookback, half_life,
        int(base_config.get("hold_bars", DEFAULT_CONFIG["hold_bars"]))
        + max(0, int(base_config.get("signal_lag_bars", DEFAULT_CONFIG["signal_lag_bars"]) or 0)),
        confidences,
        boundary_model=base_config.get("boundary_model", DEFAULT_CONFIG["boundary_model"]),
    )
    test_cache = build_context_cache(
        test_rows, test_mass_rows, vol_series, fallback_vol_series,
        lookback, half_life,
        int(base_config.get("hold_bars", DEFAULT_CONFIG["hold_bars"]))
        + max(0, int(base_config.get("signal_lag_bars", DEFAULT_CONFIG["signal_lag_bars"]) or 0)),
        confidences,
        boundary_model=base_config.get("boundary_model", DEFAULT_CONFIG["boundary_model"]),
        start_trade_date=holdout_start_date,
    )
    for cfg in configs:
        result = run_backtest_on_rows(
            train_rows,
            mass_rows=train_mass_rows,
            vol_series=vol_series,
            fallback_vol_series=fallback_vol_series,
            config=cfg,
            capital=capital,
            lookback=lookback,
            half_life=half_life,
            strike_increment=strike_increment,
            slippage_per_spread=slippage_per_spread,
            commission_per_option_contract=commission_per_option_contract,
            risk_free_rate=risk_free_rate,
            context_cache=train_cache,
            option_snapshots=option_snapshots,
        )
        row = dict(result["summary"])
        row["score"] = score_summary(row)
        train_results.append(row)
    train_results.sort(key=lambda r: r["score"], reverse=True)
    top = train_results[:10]
    test_results = []
    explicit_base = dict(base_config)
    for key in (
        "side_policy", "confidence", "quality_min", "max_build_up",
        "min_credit_risk", "min_gravity_score", "max_levitation_score",
        "width", "min_ic_body_pct", "min_ic_wing_credit", "min_ic_credit_balance",
    ):
        explicit_base.pop(key, None)
    for train_rank, row in enumerate(top, start=1):
        cfg = config_with(
            **explicit_base,
            side_policy=row["side_policy"],
            confidence=row["confidence"],
            quality_min=row["quality_min"],
            max_build_up=row["max_build_up"],
            min_credit_risk=row["min_credit_risk"],
            min_gravity_score=row.get("min_gravity_score", base_config.get("min_gravity_score", DEFAULT_CONFIG["min_gravity_score"])),
            max_levitation_score=row.get(
                "max_levitation_score", base_config.get("max_levitation_score", DEFAULT_CONFIG["max_levitation_score"])
            ),
            min_ic_body_pct=row.get("min_ic_body_pct", base_config.get("min_ic_body_pct", DEFAULT_CONFIG["min_ic_body_pct"])),
            min_ic_wing_credit=row.get(
                "min_ic_wing_credit", base_config.get("min_ic_wing_credit", DEFAULT_CONFIG["min_ic_wing_credit"])
            ),
            min_ic_credit_balance=row.get(
                "min_ic_credit_balance", base_config.get("min_ic_credit_balance", DEFAULT_CONFIG["min_ic_credit_balance"])
            ),
            width=row["width"],
        )
        result = run_backtest_on_rows(
            test_rows,
            mass_rows=test_mass_rows,
            vol_series=vol_series,
            fallback_vol_series=fallback_vol_series,
            config=cfg,
            capital=capital,
            lookback=lookback,
            half_life=half_life,
            strike_increment=strike_increment,
            slippage_per_spread=slippage_per_spread,
            commission_per_option_contract=commission_per_option_contract,
            risk_free_rate=risk_free_rate,
            context_cache=test_cache,
            option_snapshots=option_snapshots,
            start_trade_date=holdout_start_date,
        )
        out = dict(result["summary"])
        out["train_score"] = row["score"]
        out["train_total_return_pct"] = row["total_return_pct"]
        out["train_win_rate_pct"] = row["win_rate_pct"]
        out["train_trades"] = row["trades"]
        out["train_rank"] = train_rank
        out["score"] = score_summary(out)
        test_results.append(out)
    rolling = run_calendar_walk_forward(
        rows,
        mass_rows or rows,
        configs,
        vol_series,
        fallback_vol_series,
        capital,
        lookback,
        half_life,
        strike_increment,
        slippage_per_spread,
        commission_per_option_contract,
        risk_free_rate,
        option_snapshots=option_snapshots,
    )
    return {
        "train_rows": len(train_rows),
        "test_rows": len(test_rows),
        "test_evaluation_start": holdout_start_date,
        "top_train": train_results[:25],
        "top_test_from_train": test_results,
        "holdout_ordering": "training rank; holdout results are not re-ranked for selection",
        "rolling_walk_forward": rolling,
    }


def write_csv(path, rows, fields=None):
    path = Path(path)
    rows = list(rows or [])
    if fields is None:
        fields = sorted({k for row in rows for k in row.keys()})
    atomic_write_csv(path, rows, fields, extrasaction="ignore")


def write_report(path, payload):
    path = Path(path)
    summary = payload["baseline"]["summary"]
    best = (payload.get("sweep") or {}).get("top_test_from_train") or []
    rolling = ((payload.get("sweep") or {}).get("rolling_walk_forward") or {})
    sources = payload.get("sources") or {}
    price_source = sources.get("price") or {}
    lines = [
        "# Market Mass Weekly Credit Spread Backtest",
        "",
        "Research/model backtest only. Entries may use archived quotes; historical exits remain modeled, not observed fills.",
        "Decision grade: false. A current chain and independent risk review are required for any live decision.",
        "",
        f"- Trade symbol: {payload['inputs']['price_ticker']}",
        f"- Mass symbol: {payload['inputs'].get('mass_price_ticker') or payload['inputs']['price_ticker']}",
        f"- Mass volume proxy: {payload['inputs'].get('mass_volume_ticker') or payload['inputs'].get('volume_ticker') or payload['inputs']['price_ticker']}",
        f"- Trade data: {price_source.get('trade_price') if isinstance(price_source, dict) else price_source}",
        f"- Mass data: {price_source.get('mass_price') if isinstance(price_source, dict) else price_source}",
        f"- Price adjustment: {sources.get('price_adjustment')}",
        f"- Period rows: {payload['data']['rows']} ({payload['data']['first_date']} to {payload['data']['last_date']})",
        f"- Baseline policy: {summary['side_policy']}",
        f"- Boundary model: {summary.get('boundary_model', 'mass_vol')}",
        f"- Blocked IC weather: {summary.get('blocked_ic_weather') or 'none'}",
        f"- Baseline trades: {summary['trades']}",
        f"- Baseline win rate: {summary['win_rate_pct']:.1f}%" if summary["win_rate_pct"] is not None else "- Baseline win rate: n/a",
        f"- Baseline P&L: ${summary['total_pnl_dollars']:.2f}",
        f"- Baseline return: {summary['total_return_pct']:.2f}%",
        f"- Baseline max drawdown: {summary['max_drawdown_pct']:.2f}%",
        f"- Profit factor: {summary['profit_factor']:.2f}" if summary["profit_factor"] is not None else "- Profit factor: n/a",
        f"- Touch rate: {summary['touched_short_rate_pct']:.1f}%" if summary["touched_short_rate_pct"] is not None else "- Touch rate: n/a",
        f"- Avg abs short delta: {summary['avg_short_delta_abs']:.3f}" if summary["avg_short_delta_abs"] is not None else "- Avg abs short delta: n/a",
        f"- Avg gravity score: {summary['avg_gravity_score']:.1f}/100" if summary.get("avg_gravity_score") is not None else "- Avg gravity score: n/a",
        f"- Avg levitation score: {summary['avg_levitation_score']:.1f}/100" if summary.get("avg_levitation_score") is not None else "- Avg levitation score: n/a",
        f"- Avg short-HVN distance: {summary['avg_short_hvn_distance_em']:.2f} expected moves" if summary.get("avg_short_hvn_distance_em") is not None else "- Avg short-HVN distance: n/a",
        f"- Signal lag bars: {summary.get('signal_lag_bars') or 0}",
        f"- Entry fill model: {summary.get('entry_fill_model') or 'synthetic'}",
        f"- Entry timestamp policy: {summary.get('entry_timestamp_policy') or 'same_day_close'}",
        f"- Expiry mapping: {summary.get('expiry_mode') or 'weekly_friday'}",
        f"- Credit haircut: {(summary.get('credit_haircut_pct') or 0):.1f}%",
        f"- Exit slippage per vertical: ${(summary.get('exit_slippage_per_spread') or 0):.2f}",
        f"- Dividend yield assumption: {(summary.get('dividend_yield') or 0) * 100:.2f}%",
        f"- Put/call IV multipliers: {(summary.get('put_iv_multiplier') or 1):.2f}x / {(summary.get('call_iv_multiplier') or 1):.2f}x",
        f"- Avg synthetic-vs-snapshot credit error: {summary['avg_credit_error_pct']:.1f}%" if summary.get("avg_credit_error_pct") is not None else "- Avg synthetic-vs-snapshot credit error: n/a",
        "",
        "## Baseline By Side",
        "",
        "| Side | Trades | Win rate | P&L |",
        "|---|---:|---:|---:|",
    ]
    for side in ("PCS", "CCS", "IC"):
        item = summary[side.lower()]
        win = "n/a" if item["win_rate_pct"] is None else f"{item['win_rate_pct']:.1f}%"
        lines.append(f"| {side} | {item['trades']} | {win} | ${item['pnl_dollars']:.2f} |")
    loss = payload["baseline"].get("loss_analysis") or {}
    if loss.get("losses"):
        lines += [
            "",
            "## Losing Trade Decomposition",
            "",
            "| Cause | Count |",
            "|---|---:|",
        ]
        for row in loss.get("buckets", []):
            lines.append(f"| {row['cause']} | {row['count']} |")
    if summary.get("entry_fill_model") != "synthetic":
        cov = "n/a" if summary.get("snapshot_fill_coverage_pct") is None else f"{summary['snapshot_fill_coverage_pct']:.1f}%"
        avg_age = "n/a" if summary.get("avg_snapshot_age_minutes") is None else f"{summary['avg_snapshot_age_minutes']:.1f}"
        max_age = "n/a" if summary.get("max_snapshot_age_minutes") is None else f"{summary['max_snapshot_age_minutes']:.1f}"
        gate = summary.get("snapshot_coverage_gate_pass")
        lines += [
            "",
            "## Snapshot Fill Coverage",
            "",
            f"- Pricing source: {summary.get('pricing_source')}",
            f"- Snapshot fill coverage: {cov}",
            f"- Coverage gate pass: {gate}",
            f"- Attempted structures: {summary.get('attempted_structures')}",
            f"- Trades with snapshot entry: {summary.get('trades_with_snapshot_entry')}",
            f"- Trades with snapshot exit: {summary.get('trades_with_snapshot_exit')}",
            f"- Trades falling back to synthetic: {summary.get('trades_falling_back_to_synthetic')}",
            f"- Snapshot rejections: {summary.get('snapshot_rejection_count')}",
            f"- Rejections with valid snapshot fill but failed strategy filters: {summary.get('rejections_with_snapshot_fill')}",
            f"- Avg snapshot age minutes: {avg_age}",
            f"- Max snapshot age minutes: {max_age}",
        ]
    hvn_outcome = payload["baseline"].get("hvn_outcome_analysis") or []
    if hvn_outcome:
        lines += [
            "",
            "## HVN Distance By Outcome",
            "",
            "| Group | Trades | Avg EM | Median EM | <0.20 EM | <0.75 EM |",
            "|---|---:|---:|---:|---:|---:|",
        ]
        for row in hvn_outcome:
            avg = "n/a" if row.get("avg_hvn_distance_em") is None else f"{row['avg_hvn_distance_em']:.2f}"
            med = "n/a" if row.get("median_hvn_distance_em") is None else f"{row['median_hvn_distance_em']:.2f}"
            near = "n/a" if row.get("near_hvn_lt_0p20_rate_pct") is None else f"{row['near_hvn_lt_0p20_rate_pct']:.1f}%"
            magnet = "n/a" if row.get("magnet_hvn_lt_0p75_rate_pct") is None else f"{row['magnet_hvn_lt_0p75_rate_pct']:.1f}%"
            lines.append(f"| {row['group']} | {row['trades']} | {avg} | {med} | {near} | {magnet} |")
    if best:
        lines += [
            "",
            "## Training-Ranked Candidates On Chronological Holdout",
            "",
            "| Policy | Conf | Quality | Build max | Grav min | Lev max | Width | IC body | IC wing | IC bal | Trades | Win | Return | Max DD | Train return |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
        for row in best[:10]:
            win = "n/a" if row["win_rate_pct"] is None else f"{row['win_rate_pct']:.1f}%"
            body = row.get("min_ic_body_pct") or 0.0
            wing = row.get("min_ic_wing_credit") or 0.0
            balance = row.get("min_ic_credit_balance") or 0.0
            lines.append(
                f"| {row['side_policy']} | {row['confidence']:.2f} | {row['quality_min']:.0f} | "
                f"{row['max_build_up']:.0f} | {(row.get('min_gravity_score') or 0):.0f} | "
                f"{(row.get('max_levitation_score') or 100):.0f} | {row['width']:.0f} | {body * 100.0:.1f}% | "
                f"${wing:.2f} | {balance:.2f} | {row['trades']} | {win} | "
                f"{row['total_return_pct']:.2f}% | {row['max_drawdown_pct']:.2f}% | "
                f"{row['train_total_return_pct']:.2f}% |"
            )
    if rolling.get("available") and rolling.get("summary"):
        row = rolling["summary"][0]
        win = "n/a" if row.get("win_rate_pct") is None else f"{row['win_rate_pct']:.1f}%"
        pf = "n/a" if row.get("profit_factor") is None else f"{row['profit_factor']:.2f}"
        touch = "n/a" if row.get("touched_short_rate_pct") is None else f"{row['touched_short_rate_pct']:.1f}%"
        lines += [
            "",
            "## Nested Calendar Walk-Forward",
            "",
            "Each test-year configuration is selected only from its preceding training years; later test data does not choose earlier parameters.",
            "",
            f"- Candidate configurations per window: {rolling.get('candidate_count')}",
            f"- Windows: {row['windows']}; unique selected configurations: {row.get('unique_selected_configs')}",
            f"- Test trades: {row['trades']}; win rate: {win}; P&L: ${row['total_pnl_dollars']:.2f}",
            f"- Profit factor: {pf}; touch rate: {touch}; worst window realized-exit drawdown: {row['worst_window_drawdown_pct']:.2f}%",
        ]
    lines += [
        "",
        "## Why This Is Not Live-Ready",
        "",
        "- Historical option fills are synthetic unless `--use-option-snapshots` is supplied with point-in-time chain data.",
        "- The strategy has shown material sensitivity to one-bar signal lag.",
        "- The strategy has shown material sensitivity to credit haircuts.",
        "- Trade count is still modest for a weekly short-premium system.",
        "- HVN filtering improved the current sample, but the loser sample is small.",
        "",
        "## Assumptions",
        "",
        "- Weekly entries are close-to-close using daily bars unless snapshot replay is supplied.",
        "- Premiums are Black-Scholes estimates using dated point-in-time volatility proxies plus explicit fixed put/call IV multipliers and dividend yield.",
        "- Snapshot replay, when enabled, replaces entry credit with archived bid/ask-derived credit but still uses modeled path marks for exits.",
        "- Entry/exit slippage, commissions, profit-taking, and end-of-day stop rules are included; daily bars cannot establish intraday event ordering.",
        "- Defined-risk spreads only; no naked options.",
    ]
    atomic_write_text(path, "\n".join(lines) + "\n")


def rounded(obj):
    if isinstance(obj, float):
        return round(obj, 6)
    if isinstance(obj, list):
        return [rounded(x) for x in obj]
    if isinstance(obj, dict):
        return {k: rounded(v) for k, v in obj.items()}
    return obj


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="Backtest weekly QQQ/NDX credit spreads with market-mass signals.")
    ap.add_argument("--price-ticker", default="QQQ")
    ap.add_argument("--mass-price-ticker", default=None, help="Optional separate instrument for gravity/mass, e.g. ^NDX.")
    ap.add_argument("--mass-volume-ticker", default=None, help="Optional volume proxy for the mass instrument, e.g. QQQ.")
    ap.add_argument("--volume-ticker", default=None)
    ap.add_argument("--vol-ticker", default="^VXN")
    ap.add_argument("--fallback-vol-ticker", default="^VIX")
    ap.add_argument("--period", default="5y")
    ap.add_argument("--interval", choices=["1d"], default="1d",
                    help="Daily bars only; weather, annualization, and expiry mapping assume sessions.")
    ap.add_argument("--input-csv", default=None)
    ap.add_argument("--gravity-profile", choices=sorted(GRAVITY_PROFILES), default=None,
                    help="Named lookback/half-life profile: structural=252/63, swing=84/21, tactical=63/14.")
    ap.add_argument("--lookback", type=int, default=252)
    ap.add_argument("--half-life", type=float, default=63)
    ap.add_argument("--capital", type=float, default=100000.0)
    ap.add_argument("--side-policy", "--side-mode", dest="side_policy",
                    choices=["mass_adaptive", "adaptive", "pcs_only", "ccs_only", "iron_condor"],
                    default="mass_adaptive")
    ap.add_argument("--boundary-model", choices=["mass_vol", "ou_hybrid"], default="mass_vol",
                    help="mass_vol keeps the proven mass/vol blend; ou_hybrid lets OU gravity influence boundaries.")
    ap.add_argument("--confidence", type=float, default=0.80)
    ap.add_argument("--quality-min", "--min-center-quality", dest="quality_min", type=float, default=65.0)
    ap.add_argument("--max-build-up", type=float, default=65.0)
    ap.add_argument("--max-abs-distance-z", type=float, default=2.2)
    ap.add_argument("--min-gravity-score", type=float, default=0.0,
                    help="Skip entries unless reference-style gravity score is at least this 0-100 value.")
    ap.add_argument("--max-levitation-score", type=float, default=100.0,
                    help="Skip entries when reference-style levitation/thinness score exceeds this 0-100 value.")
    ap.add_argument("--min-short-hvn-distance-em", type=float, default=0.0,
                    help="Optional strike magnet filter: reject shorts closer than this expected-move distance to an HVN.")
    ap.add_argument("--max-quality-drop-5d", type=float, default=18.0)
    ap.add_argument("--max-abs-distance-z-slope-5d", type=float, default=1.25)
    ap.add_argument("--min-credit", type=float, default=0.05)
    ap.add_argument("--min-credit-risk", "--min-credit-to-risk", dest="min_credit_risk", type=float, default=0.16)
    ap.add_argument("--min-otm-pct", type=float, default=0.008)
    ap.add_argument("--boundary-buffer-pct", type=float, default=0.002)
    ap.add_argument("--max-short-delta", type=float, default=0.35)
    ap.add_argument("--min-ic-body-pct", type=float, default=0.0,
                    help="For iron condors, minimum short-strike lane width as a fraction of spot.")
    ap.add_argument("--min-ic-wing-credit", type=float, default=0.0,
                    help="For iron condors, minimum modeled credit per wing before netting.")
    ap.add_argument("--min-ic-credit-balance", type=float, default=0.0,
                    help="For iron condors, minimum smaller/larger wing-credit ratio.")
    ap.add_argument("--width", "--spread-width", dest="width", type=float, default=5.0)
    ap.add_argument("--hold-bars", type=int, default=4)
    ap.add_argument("--entry-weekday", type=int, default=0, help="0=Monday, 4=Friday.")
    ap.add_argument(
        "--expiry-mode",
        choices=["weekly_friday", "session_count"],
        default="weekly_friday",
        help="Map to the next observed weekly Friday expiry (default); session_count is sensitivity-only.",
    )
    ap.add_argument("--risk-per-trade-pct", type=float, default=0.01)
    ap.add_argument("--max-contracts", type=int, default=10)
    ap.add_argument("--profit-take-pct", type=float, default=0.65)
    ap.add_argument("--stop-loss-multiple", type=float, default=2.0)
    ap.add_argument("--credit-haircut-pct", type=float, default=0.0,
                    help="Reduce modeled entry credit by this percent after slippage for fill-sensitivity tests.")
    ap.add_argument("--exit-slippage-per-spread", type=float, default=0.02,
                    help="Modeled buy-to-close slippage per vertical wing, in option-price dollars.")
    ap.add_argument("--dividend-yield", type=float, default=0.0,
                    help="Constant annualized dividend yield used by synthetic Black-Scholes marks.")
    ap.add_argument("--put-iv-multiplier", type=float, default=1.0,
                    help="Explicit put-wing IV/skew sensitivity multiplier.")
    ap.add_argument("--call-iv-multiplier", type=float, default=1.0,
                    help="Explicit call-wing IV/skew sensitivity multiplier.")
    ap.add_argument("--signal-lag-bars", type=int, default=1,
                    help="Use signals from this many completed bars before entry (default: 1; zero is optimistic sensitivity only).")
    ap.add_argument("--use-option-snapshots", default=None,
                    help="Directory or CSV of archived point-in-time option chains for entry-credit replay.")
    ap.add_argument("--entry-fill-model", choices=("synthetic",) + SNAPSHOT_FILL_MODELS, default="synthetic",
                    help="Entry credit source. Use synthetic for Black-Scholes or a snapshot model with --use-option-snapshots.")
    ap.add_argument("--snapshot-max-age-days", type=int, default=0,
                    help="Allow snapshots up to this many calendar days before the entry date.")
    ap.add_argument("--max-snapshot-age-minutes", "--snapshot-max-age-minutes", dest="snapshot_max_age_minutes",
                    type=float, default=30.0,
                    help="Reject snapshots older than this many minutes relative to the entry timestamp policy.")
    ap.add_argument("--snapshot-strike-tolerance", type=float, default=0.01,
                    help="Maximum strike mismatch allowed when matching archived contracts.")
    ap.add_argument("--snapshot-fill-edge", type=float, default=0.25,
                    help="For bid_ask_slippage, fraction of the distance from natural to mid captured on each leg.")
    ap.add_argument("--require-snapshot-fills", "--no-synthetic-fallback", dest="require_snapshot_fills",
                    action="store_true",
                    help="Fail closed: snapshot replay must not silently use synthetic entry credits.")
    ap.add_argument("--min-snapshot-fill-coverage", type=float, default=0.0,
                    help="Minimum percent of attempted structures with valid snapshot fills.")
    ap.add_argument("--entry-timestamp-policy", choices=["same_day_close", "same_day_open", "next_open"],
                    default="same_day_close",
                    help="Timestamp contract for matching daily signals to option-chain snapshots.")
    ap.add_argument("--allow-timestamp-mismatch-sensitivity", action="store_true",
                    help=argparse.SUPPRESS)
    ap.add_argument("--breach-stop", action="store_true", help="Exit when the short strike is touched intraperiod.")
    ap.add_argument("--blocked-ic-weather", default="",
                    help="Comma-separated QQQ weather labels that block IC entries, e.g. break,overheat.")
    ap.add_argument("--strike-increment", type=float, default=1.0)
    ap.add_argument("--slippage-per-spread", type=float, default=0.02)
    ap.add_argument("--commission-per-option-contract", type=float, default=0.65)
    ap.add_argument("--risk-free-rate", type=float, default=0.04)
    ap.add_argument("--sweep", "--grid-search", dest="sweep", action="store_true")
    ap.add_argument("--grid-confidences", default=None)
    ap.add_argument("--grid-widths", default=None)
    ap.add_argument("--grid-min-qualities", default=None)
    ap.add_argument("--grid-max-builds", default=None)
    ap.add_argument("--grid-credit-to-risks", default=None)
    ap.add_argument("--grid-min-gravity-scores", default=None)
    ap.add_argument("--grid-max-levitation-scores", default=None)
    ap.add_argument("--grid-min-ic-body-pcts", default=None)
    ap.add_argument("--grid-min-ic-wing-credits", default=None)
    ap.add_argument("--grid-min-ic-credit-balances", default=None)
    ap.add_argument("--sweep-limit", type=int, default=None)
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT))
    args = ap.parse_args(argv)
    if args.side_policy == "adaptive":
        args.side_policy = "mass_adaptive"
    if args.gravity_profile:
        args.lookback, args.half_life = GRAVITY_PROFILES[args.gravity_profile]
    numeric_checks = (
        (math.isfinite(args.lookback) and args.lookback >= 30, "--lookback must be at least 30"),
        (math.isfinite(args.half_life) and args.half_life > 0, "--half-life must be positive"),
        (math.isfinite(args.confidence) and 0 < args.confidence < 1, "--confidence must be between 0 and 1"),
        (math.isfinite(args.width) and args.width > 0, "--width must be positive"),
        (args.hold_bars > 0, "--hold-bars must be positive"),
        (0 <= args.entry_weekday <= 4, "--entry-weekday must be 0..4"),
        (math.isfinite(args.capital) and args.capital > 0, "--capital must be positive"),
        (math.isfinite(args.risk_per_trade_pct) and 0 < args.risk_per_trade_pct <= 1, "--risk-per-trade-pct must be in (0, 1]"),
        (args.max_contracts >= 1, "--max-contracts must be at least 1"),
        (math.isfinite(args.profit_take_pct) and 0 < args.profit_take_pct <= 1, "--profit-take-pct must be in (0, 1]"),
        (math.isfinite(args.stop_loss_multiple) and args.stop_loss_multiple > 0, "--stop-loss-multiple must be positive"),
        (math.isfinite(args.slippage_per_spread) and args.slippage_per_spread >= 0, "--slippage-per-spread cannot be negative"),
        (math.isfinite(args.exit_slippage_per_spread) and args.exit_slippage_per_spread >= 0, "--exit-slippage-per-spread cannot be negative"),
        (math.isfinite(args.commission_per_option_contract) and args.commission_per_option_contract >= 0, "--commission-per-option-contract cannot be negative"),
        (math.isfinite(args.risk_free_rate) and -0.25 < args.risk_free_rate < 1.0, "--risk-free-rate must be between -0.25 and 1"),
        (math.isfinite(args.dividend_yield) and -0.25 < args.dividend_yield < 1.0, "--dividend-yield must be between -0.25 and 1"),
        (math.isfinite(args.put_iv_multiplier) and args.put_iv_multiplier > 0, "--put-iv-multiplier must be positive"),
        (math.isfinite(args.call_iv_multiplier) and args.call_iv_multiplier > 0, "--call-iv-multiplier must be positive"),
        (math.isfinite(args.min_credit) and args.min_credit >= 0, "--min-credit cannot be negative"),
        (math.isfinite(args.min_credit_risk) and args.min_credit_risk >= 0, "--min-credit-risk cannot be negative"),
        (math.isfinite(args.min_otm_pct) and 0 <= args.min_otm_pct < 1, "--min-otm-pct must be in [0, 1)"),
        (math.isfinite(args.boundary_buffer_pct) and 0 <= args.boundary_buffer_pct < 1, "--boundary-buffer-pct must be in [0, 1)"),
        (math.isfinite(args.max_short_delta) and 0 < args.max_short_delta <= 1, "--max-short-delta must be in (0, 1]"),
        (math.isfinite(args.credit_haircut_pct) and 0 <= args.credit_haircut_pct < 100, "--credit-haircut-pct must be in [0, 100)"),
        (args.snapshot_max_age_days >= 0, "--snapshot-max-age-days cannot be negative"),
        (args.snapshot_max_age_minutes is None or (math.isfinite(args.snapshot_max_age_minutes) and args.snapshot_max_age_minutes >= 0), "--max-snapshot-age-minutes cannot be negative"),
        (math.isfinite(args.snapshot_strike_tolerance) and args.snapshot_strike_tolerance >= 0, "--snapshot-strike-tolerance cannot be negative"),
        (math.isfinite(args.snapshot_fill_edge) and 0 <= args.snapshot_fill_edge <= 1, "--snapshot-fill-edge must be in [0, 1]"),
        (math.isfinite(args.min_snapshot_fill_coverage) and 0 <= args.min_snapshot_fill_coverage <= 100, "--min-snapshot-fill-coverage must be in [0, 100]"),
        (math.isfinite(args.quality_min) and 0 <= args.quality_min <= 100, "--quality-min must be in [0, 100]"),
        (math.isfinite(args.max_build_up) and 0 <= args.max_build_up <= 100, "--max-build-up must be in [0, 100]"),
        (math.isfinite(args.max_abs_distance_z) and args.max_abs_distance_z >= 0, "--max-abs-distance-z cannot be negative"),
        (math.isfinite(args.min_gravity_score) and 0 <= args.min_gravity_score <= 100, "--min-gravity-score must be in [0, 100]"),
        (math.isfinite(args.max_levitation_score) and 0 <= args.max_levitation_score <= 100, "--max-levitation-score must be in [0, 100]"),
        (math.isfinite(args.min_short_hvn_distance_em) and args.min_short_hvn_distance_em >= 0, "--min-short-hvn-distance-em cannot be negative"),
        (math.isfinite(args.max_quality_drop_5d) and args.max_quality_drop_5d >= 0, "--max-quality-drop-5d cannot be negative"),
        (math.isfinite(args.max_abs_distance_z_slope_5d) and args.max_abs_distance_z_slope_5d >= 0, "--max-abs-distance-z-slope-5d cannot be negative"),
        (math.isfinite(args.min_ic_body_pct) and 0 <= args.min_ic_body_pct < 1, "--min-ic-body-pct must be in [0, 1)"),
        (math.isfinite(args.min_ic_wing_credit) and args.min_ic_wing_credit >= 0, "--min-ic-wing-credit cannot be negative"),
        (math.isfinite(args.min_ic_credit_balance) and 0 <= args.min_ic_credit_balance <= 1, "--min-ic-credit-balance must be in [0, 1]"),
    )
    for valid, message in numeric_checks:
        if not valid:
            ap.error(message)
    if (
        args.entry_timestamp_policy != "same_day_close"
        and not args.allow_timestamp_mismatch_sensitivity
    ):
        ap.error(
            "same_day_open/next_open use a close-derived spot and require "
            "--allow-timestamp-mismatch-sensitivity"
        )
    if args.entry_fill_model != "synthetic" and not args.use_option_snapshots:
        ap.error("a snapshot --entry-fill-model requires --use-option-snapshots")
    if args.use_option_snapshots and args.entry_fill_model == "synthetic":
        ap.error("--use-option-snapshots requires a non-synthetic --entry-fill-model")
    if (
        (args.require_snapshot_fills or args.min_snapshot_fill_coverage > 0)
        and not args.use_option_snapshots
    ):
        ap.error("snapshot coverage requirements require --use-option-snapshots")
    return args


def load_market_rows(args):
    if args.input_csv:
        price_rows = mmb.read_ohlcv_csv(args.input_csv)
        price_source = "local OHLCV CSV input"
        volume_rows = None
    else:
        price_rows, price_source = mmb.fetch_price_rows(args.price_ticker, args.period, args.interval)
        volume_rows = None
        if args.volume_ticker and args.volume_ticker != args.price_ticker:
            volume_rows, volume_source = mmb.fetch_price_rows(args.volume_ticker, args.period, args.interval)
            price_source = f"{price_source}; volume proxy {args.volume_ticker} from {volume_source}"
    rows = mmb.merge_price_and_volume_rows(price_rows, volume_rows)
    if len(rows) < args.lookback + args.hold_bars + 20:
        raise RuntimeError("not enough rows for requested lookback and weekly hold.")
    return rows, price_source


def align_trade_and_mass_rows(trade_rows, mass_rows):
    trade_by_date = {mmb.date_key(r["date"]): r for r in trade_rows}
    mass_by_date = {mmb.date_key(r["date"]): r for r in mass_rows}
    if len(trade_by_date) != len(trade_rows):
        raise ValueError("trade rows contain duplicate dates")
    if len(mass_by_date) != len(mass_rows):
        raise ValueError("mass rows contain duplicate dates")
    dates = sorted(set(trade_by_date) & set(mass_by_date))
    if not dates:
        return [], []
    first, last = dates[0], dates[-1]
    trade_inner = {date for date in trade_by_date if first <= date <= last}
    mass_inner = {date for date in mass_by_date if first <= date <= last}
    if trade_inner != mass_inner:
        missing_trade = sorted(mass_inner - trade_inner)[:3]
        missing_mass = sorted(trade_inner - mass_inner)[:3]
        raise ValueError(
            "trade/mass calendars differ inside their common date range "
            f"(missing trade={missing_trade}, missing mass={missing_mass})"
        )
    aligned_trade = []
    aligned_mass = []
    for date in dates:
        trade_row = dict(trade_by_date[date])
        mass_row = dict(mass_by_date[date])
        trade_row["date"] = date
        mass_row["date"] = date
        aligned_trade.append(trade_row)
        aligned_mass.append(mass_row)
    return aligned_trade, aligned_mass


def load_trade_and_mass_rows(args):
    if args.input_csv:
        trade_price_rows = mmb.read_ohlcv_csv(args.input_csv)
        trade_source = "local OHLCV CSV input"
    else:
        trade_price_rows, trade_source = mmb.fetch_price_rows(args.price_ticker, args.period, args.interval)
    trade_rows = mmb.merge_price_and_volume_rows(trade_price_rows, None)

    mass_price_ticker = args.mass_price_ticker or args.price_ticker
    mass_volume_ticker = args.mass_volume_ticker or args.volume_ticker
    if args.input_csv and mass_price_ticker == args.price_ticker:
        mass_price_rows = trade_price_rows
        mass_source = trade_source
    else:
        mass_price_rows, mass_source = mmb.fetch_price_rows(mass_price_ticker, args.period, args.interval)

    mass_volume_rows = None
    if mass_volume_ticker and mass_volume_ticker != mass_price_ticker:
        mass_volume_rows, volume_source = mmb.fetch_price_rows(mass_volume_ticker, args.period, args.interval)
        mass_source = f"{mass_source}; mass volume proxy {mass_volume_ticker} from {volume_source}"
    mass_rows = mmb.merge_price_and_volume_rows(mass_price_rows, mass_volume_rows)
    trade_rows, mass_rows = align_trade_and_mass_rows(trade_rows, mass_rows)
    if len(trade_rows) < args.lookback + args.hold_bars + 20:
        raise RuntimeError("not enough aligned trade/mass rows for requested lookback and weekly hold.")
    source = {
        "trade_price": trade_source,
        "mass_price": mass_source,
        "mass_price_ticker": mass_price_ticker,
        "mass_volume_ticker": mass_volume_ticker,
    }
    return trade_rows, mass_rows, source


def main(argv=None):
    args = parse_args(argv)
    rows, mass_rows, price_source = load_trade_and_mass_rows(args)
    vol_series, vol_source, vol_error = mmb.fetch_vol_series(args.vol_ticker, args.period, args.interval)
    fallback_series, fallback_source, fallback_error = mmb.fetch_vol_series(
        args.fallback_vol_ticker, args.period, args.interval
    )
    cfg = config_with(
        side_policy=args.side_policy,
        boundary_model=args.boundary_model,
        confidence=args.confidence,
        quality_min=args.quality_min,
        max_build_up=args.max_build_up,
        max_abs_distance_z=args.max_abs_distance_z,
        min_gravity_score=args.min_gravity_score,
        max_levitation_score=args.max_levitation_score,
        min_short_hvn_distance_em=args.min_short_hvn_distance_em,
        max_quality_drop_5d=args.max_quality_drop_5d,
        max_abs_distance_z_slope_5d=args.max_abs_distance_z_slope_5d,
        min_credit=args.min_credit,
        min_credit_risk=args.min_credit_risk,
        min_otm_pct=args.min_otm_pct,
        boundary_buffer_pct=args.boundary_buffer_pct,
        max_short_delta=args.max_short_delta,
        min_ic_body_pct=args.min_ic_body_pct,
        min_ic_wing_credit=args.min_ic_wing_credit,
        min_ic_credit_balance=args.min_ic_credit_balance,
        width=args.width,
        hold_bars=args.hold_bars,
        entry_weekday=args.entry_weekday,
        expiry_mode=args.expiry_mode,
        risk_per_trade_pct=args.risk_per_trade_pct,
        max_contracts=args.max_contracts,
        profit_take_pct=args.profit_take_pct,
        stop_loss_multiple=args.stop_loss_multiple,
        credit_haircut_pct=args.credit_haircut_pct,
        exit_slippage_per_spread=args.exit_slippage_per_spread,
        dividend_yield=args.dividend_yield,
        put_iv_multiplier=args.put_iv_multiplier,
        call_iv_multiplier=args.call_iv_multiplier,
        signal_lag_bars=args.signal_lag_bars,
        entry_fill_model=args.entry_fill_model,
        snapshot_max_age_days=args.snapshot_max_age_days,
        snapshot_max_age_minutes=args.snapshot_max_age_minutes,
        snapshot_strike_tolerance=args.snapshot_strike_tolerance,
        snapshot_fill_edge=args.snapshot_fill_edge,
        require_snapshot_fills=args.require_snapshot_fills,
        min_snapshot_fill_coverage=args.min_snapshot_fill_coverage,
        entry_timestamp_policy=args.entry_timestamp_policy,
        breach_stop=args.breach_stop,
        blocked_ic_weather=args.blocked_ic_weather,
    )
    option_snapshots = None
    if args.use_option_snapshots:
        if args.entry_fill_model == "synthetic":
            raise RuntimeError("--entry-fill-model must be a snapshot model when --use-option-snapshots is supplied")
        option_snapshots = load_option_snapshots(
            args.use_option_snapshots,
            max_age_days=args.snapshot_max_age_days,
            strike_tolerance=args.snapshot_strike_tolerance,
            max_age_minutes=args.snapshot_max_age_minutes,
        )
    baseline = run_backtest_on_rows(
        rows,
        mass_rows=mass_rows,
        vol_series=vol_series,
        fallback_vol_series=fallback_series,
        config=cfg,
        capital=args.capital,
        lookback=args.lookback,
        half_life=args.half_life,
        strike_increment=args.strike_increment,
        slippage_per_spread=args.slippage_per_spread,
        commission_per_option_contract=args.commission_per_option_contract,
        risk_free_rate=args.risk_free_rate,
        option_snapshots=option_snapshots,
    )
    sweep = None
    if args.sweep:
        sweep = run_sweep(
            rows,
            mass_rows,
            vol_series,
            fallback_series,
            base_config={
                "boundary_model": args.boundary_model,
                "hold_bars": args.hold_bars,
                "entry_weekday": args.entry_weekday,
                "expiry_mode": args.expiry_mode,
                "risk_per_trade_pct": args.risk_per_trade_pct,
                "max_contracts": args.max_contracts,
                "profit_take_pct": args.profit_take_pct,
                "stop_loss_multiple": args.stop_loss_multiple,
                "credit_haircut_pct": args.credit_haircut_pct,
                "exit_slippage_per_spread": args.exit_slippage_per_spread,
                "dividend_yield": args.dividend_yield,
                "put_iv_multiplier": args.put_iv_multiplier,
                "call_iv_multiplier": args.call_iv_multiplier,
                "signal_lag_bars": args.signal_lag_bars,
                "entry_fill_model": args.entry_fill_model,
                "snapshot_max_age_days": args.snapshot_max_age_days,
                "snapshot_max_age_minutes": args.snapshot_max_age_minutes,
                "snapshot_strike_tolerance": args.snapshot_strike_tolerance,
                "snapshot_fill_edge": args.snapshot_fill_edge,
                "require_snapshot_fills": args.require_snapshot_fills,
                "min_snapshot_fill_coverage": args.min_snapshot_fill_coverage,
                "entry_timestamp_policy": args.entry_timestamp_policy,
                "max_abs_distance_z": args.max_abs_distance_z,
                "min_gravity_score": args.min_gravity_score,
                "max_levitation_score": args.max_levitation_score,
                "min_short_hvn_distance_em": args.min_short_hvn_distance_em,
                "max_quality_drop_5d": args.max_quality_drop_5d,
                "max_abs_distance_z_slope_5d": args.max_abs_distance_z_slope_5d,
                "min_credit": args.min_credit,
                "min_otm_pct": args.min_otm_pct,
                "boundary_buffer_pct": args.boundary_buffer_pct,
                "max_short_delta": args.max_short_delta,
                "min_ic_body_pct": args.min_ic_body_pct,
                "min_ic_wing_credit": args.min_ic_wing_credit,
                "min_ic_credit_balance": args.min_ic_credit_balance,
                "breach_stop": args.breach_stop,
                "blocked_ic_weather": args.blocked_ic_weather,
                "grid_confidences": args.grid_confidences,
                "grid_widths": args.grid_widths,
                "grid_min_qualities": args.grid_min_qualities,
                "grid_max_builds": args.grid_max_builds,
                "grid_credit_to_risks": args.grid_credit_to_risks,
                "grid_min_gravity_scores": args.grid_min_gravity_scores,
                "grid_max_levitation_scores": args.grid_max_levitation_scores,
                "grid_min_ic_body_pcts": args.grid_min_ic_body_pcts,
                "grid_min_ic_wing_credits": args.grid_min_ic_wing_credits,
                "grid_min_ic_credit_balances": args.grid_min_ic_credit_balances,
            },
            capital=args.capital,
            lookback=args.lookback,
            half_life=args.half_life,
            strike_increment=args.strike_increment,
            slippage_per_spread=args.slippage_per_spread,
            commission_per_option_contract=args.commission_per_option_contract,
            risk_free_rate=args.risk_free_rate,
            limit=args.sweep_limit,
            option_snapshots=option_snapshots,
        )
    public_inputs = dict(vars(args))
    public_inputs["input_csv"] = bool(args.input_csv)
    public_inputs["use_option_snapshots"] = bool(args.use_option_snapshots)
    public_inputs.pop("out_dir", None)
    payload = {
        "schemaVersion": 1,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "inputs": public_inputs,
        "sources": {
            "price": price_source,
            "primary_volatility": vol_source,
            "fallback_volatility": fallback_source,
            "vol_errors": [x for x in (vol_error, fallback_error) if x],
            "premium_model": (
                f"Entry credit from archived option snapshots using {args.entry_fill_model}; exits still use modeled path marks."
                if args.use_option_snapshots else
                "Black-Scholes estimate from entry spot/strike/DTE/market-mass volatility."
            ),
            "option_snapshots": bool(args.use_option_snapshots),
            "entry_fill_model": args.entry_fill_model,
            "entry_timestamp_policy": args.entry_timestamp_policy,
            "data_grade": (
                "Snapshot-replay research backtest for entry credits; exit marks are still modeled."
                if args.use_option_snapshots else
                "Research-grade model backtest; not historical option-fill data."
            ),
            "price_adjustment": (
                "yfinance_auto_adjust_true"
                if "Yahoo Finance" in str(price_source)
                else "source_adjustment_not_independently_verified"
            ),
        },
        "data": {
            "rows": len(rows),
            "first_date": rows[0]["date"],
            "last_date": rows[-1]["date"],
        },
        "validation": {
            "decisionGrade": False,
            "signalLagBars": args.signal_lag_bars,
            "sameBarLookaheadProtected": args.signal_lag_bars >= 1,
            "entryFillGrade": ("archived_snapshot_entry_modeled_exit"
                               if args.use_option_snapshots else "synthetic_black_scholes"),
            "entryTimingAligned": args.entry_timestamp_policy == "same_day_close",
            "timestampMismatchSensitivityWaived": bool(args.allow_timestamp_mismatch_sensitivity),
            "expiryMapping": args.expiry_mode,
            "weeklyFridayMapped": args.expiry_mode == "weekly_friday",
            "dividendYieldAssumption": args.dividend_yield,
            "putIvMultiplier": args.put_iv_multiplier,
            "callIvMultiplier": args.call_iv_multiplier,
            "exitVolatilityPath": "dated broad volatility proxy when available; fixed entry proxy fallback",
            "deltaFilter": "archived snapshot delta when valid; Black-Scholes fallback",
            "adjustedPriceVerified": "Yahoo Finance" in str(price_source),
            "exitSlippagePerVertical": args.exit_slippage_per_spread,
            "nestedWalkForward": bool((sweep or {}).get("rolling_walk_forward", {}).get("nested_model_selection")),
            "limitations": [
                "Historical option exits are modeled rather than reconstructed from full bid/ask chains.",
                "Daily bars cannot establish intraday ordering of touches, stops, and profit targets.",
                "Synthetic marks use constant rate/dividend and wing-multiplier assumptions; dated broad volatility proxies are not a historical option surface.",
                "Taxes, assignment/margin path, early exercise, and market-impact liquidity are not simulated.",
                "Drawdown is observed only at modeled trade exits, not daily option mark-to-market.",
                "Research backtest only; ALLOW/WATCH/BLOCK requires a current chain and independent risk review.",
            ],
        },
        "baseline": baseline,
        "sweep": sweep,
        "disclaimer": "Research only. Defined-risk model backtest, not financial advice or a live trading signal.",
    }
    out_dir = Path(args.out_dir)
    atomic_write_json(out_dir / "summary.json", rounded(payload))
    trade_fields = [
        "entry_date", "exit_date", "expiry_date", "expiry_target_date", "expiry_mode",
        "expiry_calendar_adjusted", "side", "weather", "regime",
        "entry_close", "exit_close", "expiry_close", "short_strike", "long_strike",
        "put_short_strike", "put_long_strike", "call_short_strike", "call_long_strike",
        "contracts", "synthetic_credit_per_share", "credit_per_share", "raw_credit_per_share",
        "entry_credit_source", "snapshot_ts", "snapshot_age_minutes", "snapshot_mid_credit", "snapshot_natural_credit",
        "snapshot_conservative_credit", "snapshot_fill_credit", "credit_error_pct",
        "short_delta", "model_short_delta", "short_delta_source", "short_observed_iv",
        "exit_mark_per_share", "credit_risk", "signal_model_iv", "base_model_iv", "model_iv",
        "entry_iv_path",
        "exit_model_iv", "exit_put_model_iv", "exit_call_model_iv", "exit_iv_path",
        "put_model_iv", "call_model_iv", "iv_multiplier", "put_iv_multiplier", "call_iv_multiplier",
        "dividend_yield", "exit_slippage_per_spread",
        "put_credit_per_share", "call_credit_per_share", "put_short_delta", "call_short_delta",
        "put_model_short_delta", "call_model_short_delta", "put_short_delta_source", "call_short_delta_source",
        "put_short_observed_iv", "call_short_observed_iv",
        "put_synthetic_credit_per_share", "call_synthetic_credit_per_share",
        "put_entry_credit_source", "put_snapshot_ts", "put_snapshot_age_minutes", "put_snapshot_mid_credit", "put_snapshot_natural_credit",
        "put_snapshot_conservative_credit", "put_snapshot_fill_credit", "put_credit_error_pct",
        "call_entry_credit_source", "call_snapshot_ts", "call_snapshot_age_minutes", "call_snapshot_mid_credit", "call_snapshot_natural_credit",
        "call_snapshot_conservative_credit", "call_snapshot_fill_credit", "call_credit_error_pct",
        "ic_body_width", "ic_body_pct", "ic_credit_balance",
        "pnl_dollars", "max_gain_dollars", "max_loss_dollars", "return_on_risk_pct",
        "win", "exit_reason", "touched_short", "touched_side", "quality_score", "distance_z",
        "gravity_score", "levitation_score", "kappa_tstat",
        "build_up_score", "mass_current_price", "center_price", "lower_boundary", "upper_boundary",
        "boundary_horizon_sessions",
        "quality_slope_5d", "distance_z_slope_5d", "boundary_width_pct", "boundary_width_slope_5d",
        "mass_to_trade_spot_ratio",
        "short_nearest_hvn_price", "short_nearest_hvn_kind", "short_hvn_distance_em", "short_hvn_penalty",
        "put_short_nearest_hvn_price", "put_short_nearest_hvn_kind", "put_short_hvn_distance_em", "put_short_hvn_penalty",
        "call_short_nearest_hvn_price", "call_short_nearest_hvn_kind", "call_short_hvn_distance_em", "call_short_hvn_penalty",
        "credit_haircut_pct", "signal_lag_bars",
    ]
    write_csv(out_dir / "trades.csv", baseline["trades"], trade_fields)
    write_csv(out_dir / "skipped.csv", baseline["skipped"], ["date", "reason"])
    rejection_fields = [
        "entry_date", "expiry_date", "side", "reason",
        "short_strike", "long_strike",
        "put_short_strike", "put_long_strike", "call_short_strike", "call_long_strike",
        "credit_per_share", "synthetic_credit_per_share", "credit_risk",
        "entry_credit_source", "snapshot_fill_available", "short_delta",
        "put_credit_per_share", "call_credit_per_share",
        "put_short_hvn_distance_em", "call_short_hvn_distance_em", "short_hvn_distance_em",
        "ic_body_pct", "ic_credit_balance",
    ]
    write_csv(out_dir / "snapshot_replay_rejections.csv", baseline.get("snapshot_rejections") or [], rejection_fields)
    atomic_write_json(
        out_dir / "snapshot_replay_rejections.json",
        rounded(baseline.get("snapshot_rejections") or []),
    )
    write_csv(
        out_dir / "hvn_outcome_analysis.csv",
        baseline.get("hvn_outcome_analysis") or [],
        [
            "group", "trades", "with_hvn_metric", "avg_hvn_distance_em",
            "median_hvn_distance_em", "min_hvn_distance_em",
            "near_hvn_lt_0p20_count", "near_hvn_lt_0p20_rate_pct",
            "magnet_hvn_lt_0p75_count", "magnet_hvn_lt_0p75_rate_pct",
        ],
    )
    atomic_write_json(
        out_dir / "loss_analysis.json",
        rounded(baseline.get("loss_analysis") or {}),
    )
    atomic_write_json(
        out_dir / "hvn_outcome_analysis.json",
        rounded(baseline.get("hvn_outcome_analysis") or []),
    )
    write_csv(
        out_dir / "losing_trades.csv",
        (baseline.get("loss_analysis") or {}).get("rows") or [],
        [
            "entry_date", "expiry_date", "side", "touched_side", "pnl_dollars",
            "credit_per_share", "credit_risk", "short_delta", "gravity_score",
            "levitation_score", "quality_score", "build_up_score", "distance_z",
            "hvn_distance_em", "entry_close", "expiry_close", "exit_reason", "causes",
        ],
    )
    if sweep:
        write_csv(out_dir / "sweep_train.csv", sweep["top_train"])
        write_csv(out_dir / "sweep_walk_forward.csv", sweep["top_test_from_train"])
        rolling = sweep.get("rolling_walk_forward") or {}
        if rolling.get("available"):
            write_csv(out_dir / "rolling_walk_forward.csv", rolling.get("summary") or [])
            write_csv(out_dir / "rolling_walk_forward_windows.csv", rolling.get("details") or [])
    write_report(out_dir / "report.md", payload)

    s = baseline["summary"]
    win = "n/a" if s["win_rate_pct"] is None else f"{s['win_rate_pct']:.1f}%"
    print(
        f"{args.price_ticker} weekly credit spreads: trades={s['trades']} "
        f"win={win} pnl=${s['total_pnl_dollars']:.2f} "
        f"return={s['total_return_pct']:.2f}% maxDD={s['max_drawdown_pct']:.2f}%"
    )
    if sweep and sweep["top_test_from_train"]:
        best = sweep["top_test_from_train"][0]
        best_win = "n/a" if best["win_rate_pct"] is None else f"{best['win_rate_pct']:.1f}%"
        print(
            f"Top training-selected candidate on holdout: {best['side_policy']} "
            f"conf={best['confidence']} q={best['quality_min']} width={best['width']} "
            f"trades={best['trades']} win={best_win} return={best['total_return_pct']:.2f}%"
        )
    print(f"Wrote {out_dir / 'summary.json'}")
    print(f"Wrote {out_dir / 'report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

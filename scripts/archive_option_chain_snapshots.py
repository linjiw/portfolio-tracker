#!/usr/bin/env python3
"""Archive point-in-time option-chain snapshots with market-gravity context.

The backtests in this repo can estimate option credits synthetically, but that
is the largest remaining model risk. This script records real bid/ask chains
from yfinance so future backtests can replay entry credits from archived data.
"""

from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import math
import re
import subprocess
from pathlib import Path
from statistics import NormalDist
from zoneinfo import ZoneInfo

try:
    from scripts.artifact_io import atomic_write_csv, atomic_write_json
except ImportError:  # direct `python scripts/archive_option_chain_snapshots.py`
    from artifact_io import atomic_write_csv, atomic_write_json


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "data" / "option_chain_snapshots"
NORM = NormalDist()


def _load_script_module(name):
    path = Path(__file__).with_name(f"{name}.py")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mmb = _load_script_module("market_mass_boundaries")
mmcs = _load_script_module("market_mass_credit_spread_backtest")


FIELDS = [
    "snapshot_ts", "snapshot_date", "underlying", "underlying_price", "expiry", "dte",
    "option_type", "strike", "bid", "ask", "mid", "last", "volume", "open_interest",
    "implied_volatility", "delta", "gamma", "theta", "vega",
    "model_delta", "model_gamma", "model_theta", "model_vega", "model_iv_input",
    "source", "contract_symbol", "last_trade_date", "in_the_money",
    "gravity_score", "levitation_score", "center_price", "distance_z",
    "center_quality", "build_up", "regime", "mass_current_price",
    "nearest_hvn_price", "nearest_hvn_kind", "strike_hvn_distance_em", "hvn_penalty",
    "put_short_hvn_distance_em", "call_short_hvn_distance_em",
    "boundary_lower", "boundary_upper", "boundary_confidence", "boundary_model",
]


def safe_symbol_component(value):
    """Validate a Yahoo-style symbol before using it as a path component."""
    text = str(value or "").strip().upper()
    if (not re.fullmatch(r"[A-Z0-9^][A-Z0-9._^=-]{0,63}", text)
            or text in {".", ".."}):
        raise ValueError(f"unsafe ticker for snapshot path: {value!r}")
    return text


def safe_expiry(value):
    """Return an exact ISO expiry date safe for API and path use."""
    text = str(value or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        raise ValueError(f"invalid option expiry: {value!r}")
    try:
        parsed = dt.date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"invalid option expiry: {value!r}") from exc
    return parsed.isoformat()


def rn(value, digits=6):
    value = mmb._to_float(value)
    return None if value is None else round(value, digits)


def date_key(value):
    return mmb.date_key(value)


def option_type_from_frame_name(name):
    return "call" if name == "calls" else "put"


def dte(snapshot_date, expiry):
    return max((dt.date.fromisoformat(safe_expiry(expiry)) - dt.date.fromisoformat(date_key(snapshot_date))).days, 0)


def bs_greeks(spot, strike, days_to_expiry, sigma, option_type, rate=0.04):
    spot = max(float(spot or 0.0), 1e-9)
    strike = max(float(strike or 0.0), 1e-9)
    years = max(float(days_to_expiry or 0) / 365.0, 1.0 / 365.0)
    sigma = max(float(sigma or 0.0), 0.01)
    vol_t = sigma * math.sqrt(years)
    d1 = (math.log(spot / strike) + (rate + 0.5 * sigma * sigma) * years) / vol_t
    d2 = d1 - vol_t
    pdf = math.exp(-0.5 * d1 * d1) / math.sqrt(2.0 * math.pi)
    gamma = pdf / (spot * vol_t)
    vega = spot * pdf * math.sqrt(years) / 100.0
    if option_type == "call":
        delta = NORM.cdf(d1)
        theta = (
            -(spot * pdf * sigma) / (2.0 * math.sqrt(years))
            - rate * strike * math.exp(-rate * years) * NORM.cdf(d2)
        ) / 365.0
    else:
        delta = NORM.cdf(d1) - 1.0
        theta = (
            -(spot * pdf * sigma) / (2.0 * math.sqrt(years))
            + rate * strike * math.exp(-rate * years) * NORM.cdf(-d2)
        ) / 365.0
    return {
        "model_delta": delta,
        "model_gamma": gamma,
        "model_theta": theta,
        "model_vega": vega,
        "d1": d1,
        "d2": d2,
    }


def output_path(out_dir, underlying, snapshot_ts, expiry):
    snap_dt = dt.datetime.fromisoformat(snapshot_ts)
    underlying = safe_symbol_component(underlying)
    root = Path(out_dir) / underlying / f"{snap_dt.year:04d}" / f"{snap_dt.month:02d}"
    stamp = snap_dt.strftime("%Y-%m-%d_%H%M%S_ET")
    filename = f"{stamp}_{underlying.upper()}_{safe_expiry(expiry)}.csv"
    return root / filename


def raw_output_path(out_dir, underlying, snapshot_ts, expiry):
    snap_dt = dt.datetime.fromisoformat(snapshot_ts)
    underlying = safe_symbol_component(underlying)
    root = Path(out_dir) / underlying / "raw" / snap_dt.strftime("%Y-%m-%d")
    stamp = snap_dt.strftime("%Y-%m-%d_%H%M%S_ET")
    filename = f"{stamp}_{underlying.upper()}_{safe_expiry(expiry)}_raw.json"
    return root / filename


def metadata_output_path(out_dir, underlying, snapshot_ts, expiry):
    snap_dt = dt.datetime.fromisoformat(snapshot_ts)
    underlying = safe_symbol_component(underlying)
    root = Path(out_dir) / underlying / "metadata" / snap_dt.strftime("%Y-%m-%d")
    stamp = snap_dt.strftime("%Y-%m-%d_%H%M%S_ET")
    filename = f"{stamp}_{underlying.upper()}_{safe_expiry(expiry)}_metadata.json"
    return root / filename


def dataframe_rows(df):
    if df is None or getattr(df, "empty", True):
        return []
    return [dict(row) for _, row in df.iterrows()]


def get_underlying_price(ticker_obj):
    fast = getattr(ticker_obj, "fast_info", None)
    for key in ("last_price", "lastPrice", "regular_market_price"):
        try:
            value = fast.get(key) if hasattr(fast, "get") else getattr(fast, key)
        except Exception:
            value = None
        value = mmb._to_float(value)
        if value and value > 0:
            return value
    hist = ticker_obj.history(period="5d", interval="1d", auto_adjust=False)
    rows = mmb._df_to_rows(hist)
    if not rows:
        raise RuntimeError("could not resolve underlying price from yfinance")
    return rows[-1]["close"]


def load_gravity_context(args, underlying_price):
    trade_price_rows, _ = mmb.fetch_price_rows(args.ticker, args.period, args.interval)
    trade_rows = mmb.merge_price_and_volume_rows(trade_price_rows, None)
    mass_price_ticker = args.mass_price_ticker or args.ticker
    mass_volume_ticker = args.mass_volume_ticker or args.volume_ticker
    mass_price_rows, _ = mmb.fetch_price_rows(mass_price_ticker, args.period, args.interval)
    mass_volume_rows = None
    if mass_volume_ticker and mass_volume_ticker != mass_price_ticker:
        mass_volume_rows, _ = mmb.fetch_price_rows(mass_volume_ticker, args.period, args.interval)
    mass_rows = mmb.merge_price_and_volume_rows(mass_price_rows, mass_volume_rows)
    trade_rows, mass_rows = mmcs.align_trade_and_mass_rows(trade_rows, mass_rows)
    vol_series, _, _ = mmb.fetch_vol_series(args.vol_ticker, args.period, args.interval)
    fallback_series, _, _ = mmb.fetch_vol_series(args.fallback_vol_ticker, args.period, args.interval)
    state = mmb.analyze_state(
        mass_rows,
        vol_series=vol_series,
        fallback_vol_series=fallback_series,
        lookback=args.lookback,
        half_life=args.half_life,
    )
    band = mmb.boundary_rows(
        state,
        [args.horizon_days],
        [args.boundary_confidence],
        boundary_model=args.boundary_model,
    )[0]
    band = mmcs.map_mass_band_to_trade(band, state, underlying_price)
    return state, band


def gravity_fields(args, state, band, underlying_price, days_to_expiry, option_type, strike):
    if not state or not band:
        return {}
    annual_vol = state.get("volatility", {}).get("annual_vol_used") or 0.20
    days = max(int(days_to_expiry or 0), 1)
    hvn = mmcs.strike_hvn_metrics(state, underlying_price, strike, days, annual_vol)
    distance = rn(hvn.get("nearest_hvn_distance_em"), 6)
    return {
        "gravity_score": rn((state["center"].get("gravity") or {}).get("gravity_score"), 4),
        "levitation_score": rn((state["center"].get("gravity") or {}).get("levitation_score"), 4),
        "center_price": rn(state["center"].get("center_price"), 4),
        "distance_z": rn(state["center"].get("distance_z"), 6),
        "center_quality": rn(state["center"].get("quality_score"), 4),
        "build_up": rn(state.get("volatility_build_up", {}).get("score"), 4),
        "regime": state["center"].get("regime"),
        "mass_current_price": rn(state["center"].get("current_price"), 4),
        "nearest_hvn_price": rn(hvn.get("nearest_hvn_price"), 4),
        "nearest_hvn_kind": hvn.get("nearest_hvn_kind"),
        "strike_hvn_distance_em": distance,
        "hvn_penalty": rn(hvn.get("hvn_penalty"), 8),
        "put_short_hvn_distance_em": distance if option_type == "put" else None,
        "call_short_hvn_distance_em": distance if option_type == "call" else None,
        "boundary_lower": rn(band.get("lower_boundary"), 4),
        "boundary_upper": rn(band.get("upper_boundary"), 4),
        "boundary_confidence": args.boundary_confidence,
        "boundary_model": args.boundary_model,
    }


def normalize_option_row(raw, option_type, args, snapshot_ts, underlying, underlying_price, expiry, state, band):
    strike = mmb._to_float(raw.get("strike"))
    if strike is None or strike <= 0 or option_type not in {"call", "put"}:
        return None
    bid = mmb._to_float(raw.get("bid"))
    ask = mmb._to_float(raw.get("ask"))
    # The raw artifact retains vendor rows for audit. The normalized chain is
    # deliberately executable-quote-shaped: no missing, negative, or crossed
    # markets enter future replay inputs.
    if bid is None or ask is None or bid < 0 or ask < 0 or ask < bid:
        return None
    mid = (bid + ask) / 2.0
    last = mmb._to_float(raw.get("lastPrice") if "lastPrice" in raw else raw.get("last"))
    iv = mmb._to_float(raw.get("impliedVolatility") if "impliedVolatility" in raw else raw.get("implied_volatility"))
    if last is not None and last < 0:
        last = None
    if iv is not None and iv <= 0:
        iv = None
    days = dte(snapshot_ts, expiry)
    model_iv = iv or (state or {}).get("volatility", {}).get("annual_vol_used") or 0.20
    greeks = bs_greeks(underlying_price, strike, days, model_iv, option_type, rate=args.risk_free_rate)
    row = {
        "snapshot_ts": snapshot_ts,
        "snapshot_date": date_key(snapshot_ts),
        "underlying": underlying,
        "underlying_price": rn(underlying_price, 6),
        "expiry": safe_expiry(expiry),
        "dte": days,
        "option_type": option_type,
        "strike": rn(strike, 4),
        "bid": rn(bid, 6),
        "ask": rn(ask, 6),
        "mid": rn(mid, 6),
        "last": rn(last, 6),
        "volume": rn(raw.get("volume"), 0),
        "open_interest": rn(raw.get("openInterest") if "openInterest" in raw else raw.get("open_interest"), 0),
        "implied_volatility": rn(iv, 8),
        "delta": rn(raw.get("delta"), 8),
        "gamma": rn(raw.get("gamma"), 8),
        "theta": rn(raw.get("theta"), 8),
        "vega": rn(raw.get("vega"), 8),
        "model_delta": rn(greeks["model_delta"], 8),
        "model_gamma": rn(greeks["model_gamma"], 8),
        "model_theta": rn(greeks["model_theta"], 8),
        "model_vega": rn(greeks["model_vega"], 8),
        "model_iv_input": rn(model_iv, 8),
        "source": "yfinance",
        "contract_symbol": raw.get("contractSymbol") or raw.get("contract_symbol"),
        "last_trade_date": str(raw.get("lastTradeDate") or raw.get("last_trade_date") or ""),
        "in_the_money": raw.get("inTheMoney") if "inTheMoney" in raw else raw.get("in_the_money"),
    }
    row.update(gravity_fields(args, state, band, underlying_price, days, option_type, strike))
    return row


def write_csv(path, rows):
    atomic_write_csv(path, rows, FIELDS, extrasaction="ignore")


def json_safe(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            return None
        return value
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def write_json(path, payload):
    atomic_write_json(path, json_safe(payload))


def git_sha():
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=ROOT,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return None


def snapshot_metadata(args, snapshot_ts, underlying_price, expiry, rows, raw_count):
    return {
        "snapshot_ts": snapshot_ts,
        "source": "yfinance",
        "script_version": "market_mass_v0.5_snapshot_archive_guardrails",
        "git_sha": git_sha(),
        "underlying": args.ticker.upper(),
        "underlying_price": underlying_price,
        "underlying_bid": None,
        "underlying_ask": None,
        "expiry": safe_expiry(expiry),
        "normalized_rows": len(rows),
        "raw_rows": raw_count,
        "risk_free_rate_input": args.risk_free_rate,
        "dividend_yield_input": None,
        "timezone": "America/New_York",
        "created_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        "gravity_profile": args.gravity_profile,
        "lookback": args.lookback,
        "half_life": args.half_life,
        "boundary_confidence": args.boundary_confidence,
        "boundary_model": args.boundary_model,
    }


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="Archive yfinance option chains with market-gravity context.")
    ap.add_argument("--ticker", default="QQQ")
    ap.add_argument("--expiry", default=None, help="Comma-separated expiries. Defaults to first --max-expiries expiries.")
    ap.add_argument("--max-expiries", type=int, default=2)
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT))
    ap.add_argument("--mass-price-ticker", default="^NDX")
    ap.add_argument("--mass-volume-ticker", default="QQQ")
    ap.add_argument("--volume-ticker", default=None)
    ap.add_argument("--vol-ticker", default="^VXN")
    ap.add_argument("--fallback-vol-ticker", default="^VIX")
    ap.add_argument("--period", default="5y")
    ap.add_argument("--interval", choices=["1d"], default="1d")
    ap.add_argument("--gravity-profile", choices=sorted(mmcs.GRAVITY_PROFILES), default="swing")
    ap.add_argument("--lookback", type=int, default=84)
    ap.add_argument("--half-life", type=float, default=21)
    ap.add_argument("--horizon-days", type=int, default=5)
    ap.add_argument("--boundary-confidence", type=float, default=0.75)
    ap.add_argument("--boundary-model", choices=["mass_vol", "ou_hybrid"], default="mass_vol")
    ap.add_argument("--risk-free-rate", type=float, default=0.04)
    ap.add_argument("--skip-gravity-state", action="store_true")
    ap.add_argument("--write-raw", action=argparse.BooleanOptionalAction, default=True,
                    help="Also write raw yfinance chain rows and snapshot metadata JSON.")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    args.ticker = safe_symbol_component(args.ticker)
    if args.expiry:
        args.expiry = ",".join(safe_expiry(value) for value in args.expiry.split(",") if value.strip())
    if args.max_expiries < 0:
        ap.error("--max-expiries must be non-negative")
    if args.lookback <= 1 or args.half_life <= 0 or args.horizon_days <= 0:
        ap.error("lookback must exceed 1 and half-life/horizon-days must be positive")
    if not 0 < args.boundary_confidence < 1:
        ap.error("--boundary-confidence must be between 0 and 1")
    if not math.isfinite(args.risk_free_rate):
        ap.error("--risk-free-rate must be finite")
    if args.gravity_profile:
        args.lookback, args.half_life = mmcs.GRAVITY_PROFILES[args.gravity_profile]
    return args


def main(argv=None):
    args = parse_args(argv)
    try:
        import warnings
        warnings.filterwarnings("ignore")
        import yfinance as yf
    except Exception as exc:
        raise RuntimeError("yfinance is required to archive option-chain snapshots") from exc

    snapshot_ts = dt.datetime.now(ZoneInfo("America/New_York")).replace(microsecond=0).isoformat()
    ticker_obj = yf.Ticker(args.ticker)
    expiries = [x.strip() for x in args.expiry.split(",") if x.strip()] if args.expiry else list(ticker_obj.options)
    if args.max_expiries > 0:
        expiries = expiries[:args.max_expiries]
    if not expiries:
        raise RuntimeError(f"yfinance returned no option expiries for {args.ticker}")
    underlying_price = get_underlying_price(ticker_obj)
    state = band = None
    if not args.skip_gravity_state:
        state, band = load_gravity_context(args, underlying_price)

    written = []
    total_rows = 0
    for expiry in expiries:
        chain = ticker_obj.option_chain(expiry)
        rows = []
        raw_rows = []
        for frame_name, frame in (("calls", chain.calls), ("puts", chain.puts)):
            option_type = option_type_from_frame_name(frame_name)
            for raw in dataframe_rows(frame):
                raw_copy = dict(raw)
                raw_copy["option_type"] = option_type
                raw_rows.append(raw_copy)
                row = normalize_option_row(
                    raw, option_type, args, snapshot_ts, args.ticker.upper(),
                    underlying_price, expiry, state, band,
                )
                if row:
                    rows.append(row)
        total_rows += len(rows)
        path = output_path(args.out_dir, args.ticker, snapshot_ts, expiry)
        if not args.dry_run:
            write_csv(path, rows)
            if args.write_raw:
                write_json(raw_output_path(args.out_dir, args.ticker, snapshot_ts, expiry), raw_rows)
                write_json(
                    metadata_output_path(args.out_dir, args.ticker, snapshot_ts, expiry),
                    snapshot_metadata(args, snapshot_ts, underlying_price, expiry, rows, len(raw_rows)),
                )
            written.append(str(path))
        else:
            written.append(f"dry-run:{path}")
            if args.write_raw:
                written.append(f"dry-run:{raw_output_path(args.out_dir, args.ticker, snapshot_ts, expiry)}")
                written.append(f"dry-run:{metadata_output_path(args.out_dir, args.ticker, snapshot_ts, expiry)}")

    print(f"Archived {total_rows} option rows for {args.ticker} at {snapshot_ts}")
    for path in written:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

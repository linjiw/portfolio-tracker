#!/usr/bin/env python3
"""Market mass boundary engine.

This is a research tool, not investment advice. It estimates probabilistic
upper/lower boundary zones from two sources:

1. A volume/recency weighted center of mass in log-price space.
2. A realized/implied volatility cone around the current price.

When the center is strong and price is still orbiting it, the bands lean toward
the mass profile. When the center is weak or price is detached, the bands lean
toward volatility.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import math
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from statistics import NormalDist

try:
    from scripts.artifact_io import atomic_write_csv, atomic_write_json, atomic_write_text
except ImportError:  # direct `python scripts/market_mass_boundaries.py`
    from artifact_io import atomic_write_csv, atomic_write_json, atomic_write_text


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT_DIR = ROOT / "output" / "market_mass_boundaries"

DEFAULT_HORIZONS = (1, 5, 10, 21, 63)
DEFAULT_CONFIDENCES = (0.68, 0.80, 0.95)
DEFAULT_LOOKBACK = 252
DEFAULT_HALF_LIFE = 63
DEFAULT_MAX_IMPLIED_VOL_AGE_DAYS = 7
GRAVITY_PROFILES = {
    "structural": (252, 63),
    "swing": (84, 21),
    "tactical": (63, 14),
}

FRED_VOL_SERIES = {
    "^VXN": "VXNCLS",
    "VXN": "VXNCLS",
    "VXNCLS": "VXNCLS",
    "^VIX": "VIXCLS",
    "VIX": "VIXCLS",
    "VIXCLS": "VIXCLS",
}

STOOQ_SYMBOLS = {
    "QQQ": "qqq.us",
    "TQQQ": "tqqq.us",
    "VOO": "voo.us",
    "SPY": "spy.us",
    "SPMO": "spmo.us",
    "^NDX": "^ndx",
    "NDX": "^ndx",
    "^GSPC": "^spx",
    "GSPC": "^spx",
    "^IXIC": "^ixic",
    "^VIX": "^vix",
}


def _to_float(value):
    try:
        if value is None:
            return None
        if isinstance(value, str) and value.strip() in ("", ".", "nan", "NaN"):
            return None
        out = float(value)
        if math.isnan(out) or math.isinf(out):
            return None
        return out
    except Exception:
        return None


def _round(value, digits=2):
    value = _to_float(value)
    return None if value is None else round(value, digits)


def clamp(value, low=0.0, high=1.0):
    value = _to_float(value)
    if value is None:
        return low
    return max(low, min(high, value))


def norm01(value, low, high):
    value = _to_float(value)
    if value is None or high == low:
        return 0.0
    return clamp((value - low) / (high - low))


def date_key(value):
    return str(value)[:10]


def parse_num_list(text, cast=float):
    if not text:
        return []
    out = []
    for part in str(text).split(","):
        part = part.strip()
        if part:
            out.append(cast(part))
    return out


def pct(new, old):
    new = _to_float(new)
    old = _to_float(old)
    if new is None or old in (None, 0):
        return None
    return (new / old - 1.0) * 100.0


def simple_stdev(values):
    values = [_to_float(v) for v in values]
    values = [v for v in values if v is not None]
    if len(values) < 2:
        return None
    mean = sum(values) / len(values)
    return math.sqrt(sum((v - mean) ** 2 for v in values) / (len(values) - 1))


def median(values):
    values = sorted(v for v in (_to_float(x) for x in values) if v is not None)
    if not values:
        return None
    mid = len(values) // 2
    if len(values) % 2:
        return values[mid]
    return (values[mid - 1] + values[mid]) / 2.0


def weighted_mean(values, weights):
    total = sum(weights)
    if total <= 0:
        return None
    return sum(v * w for v, w in zip(values, weights)) / total


def weighted_quantile(values, weights, q):
    pairs = sorted((v, w) for v, w in zip(values, weights) if w > 0)
    if not pairs:
        return None
    total = sum(w for _, w in pairs)
    if total <= 0:
        return None
    target = total * clamp(q, 0.0, 1.0)
    running = 0.0
    for value, weight in pairs:
        running += weight
        if running >= target:
            return value
    return pairs[-1][0]


def log_returns(rows, lookback=None):
    closes = [r["close"] for r in rows if _to_float(r.get("close")) and r["close"] > 0]
    if lookback:
        closes = closes[-(lookback + 1):]
    out = []
    for prev, cur in zip(closes, closes[1:]):
        if prev > 0 and cur > 0:
            out.append(math.log(cur / prev))
    return out


def annualized_realized_vol(rows, lookback=21):
    returns = log_returns(rows, lookback=lookback)
    sd = simple_stdev(returns)
    if sd is None:
        return None
    return sd * math.sqrt(252.0)


def row_range_log(row):
    high = _to_float(row.get("high"))
    low = _to_float(row.get("low"))
    close = _to_float(row.get("close"))
    if high is None or low is None or high <= 0 or low <= 0:
        return None
    if high < low:
        high, low = low, high
    if high == low and close and close > 0:
        high = close * 1.0001
        low = close * 0.9999
    if high <= low:
        return None
    return math.log(high / low)


def z_for_confidence(confidence):
    confidence = clamp(confidence, 0.01, 0.999)
    return NormalDist().inv_cdf(0.5 + confidence / 2.0)


def _row_from_mapping(raw):
    close = _to_float(raw.get("Close") if "Close" in raw else raw.get("close"))
    open_ = _to_float(raw.get("Open") if "Open" in raw else raw.get("open"))
    high = _to_float(raw.get("High") if "High" in raw else raw.get("high"))
    low = _to_float(raw.get("Low") if "Low" in raw else raw.get("low"))
    volume = _to_float(raw.get("Volume") if "Volume" in raw else raw.get("volume")) or 0.0
    if close is None:
        return None
    ohlc_imputed = any(value is None for value in (open_, high, low))
    open_ = close if open_ is None else open_
    high = close if high is None else high
    low = close if low is None else low
    if min(open_, high, low, close) <= 0:
        return None
    # Do not silently normalize impossible bars.  Range-sensitive mass and
    # option-touch calculations need the source defect to remain observable.
    if high < max(open_, close) or low > min(open_, close) or high < low:
        return None
    return {
        "date": str(raw.get("Date") or raw.get("date")),
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
        "ohlc_imputed": ohlc_imputed,
    }


def read_ohlcv_csv(path):
    rows = []
    with Path(path).open("r", encoding="utf-8-sig", newline="") as fh:
        for row_number, raw in enumerate(csv.DictReader(fh), start=2):
            row = _row_from_mapping(raw)
            if row is None or row["close"] <= 0:
                raise ValueError(f"invalid OHLC row {row_number} in {path}")
            if row.get("ohlc_imputed"):
                raise ValueError(f"incomplete OHLC row {row_number} in {path}")
            try:
                dt.date.fromisoformat(date_key(row["date"]))
            except ValueError as exc:
                raise ValueError(f"invalid date at row {row_number} in {path}") from exc
            rows.append(row)
    rows.sort(key=lambda r: r["date"])
    dates = [date_key(row["date"]) for row in rows]
    if len(dates) != len(set(dates)):
        raise ValueError(f"duplicate dates in {path}")
    return rows


def _df_to_rows(df):
    if df is None or getattr(df, "empty", True):
        return []
    rows = []
    for idx, row in df.iterrows():
        close = _to_float(row.get("Close"))
        open_ = _to_float(row.get("Open"))
        high = _to_float(row.get("High"))
        low = _to_float(row.get("Low"))
        if close is None or close <= 0:
            continue
        ohlc_imputed = any(value is None for value in (open_, high, low))
        open_ = close if open_ is None else open_
        high = close if high is None else high
        low = close if low is None else low
        if min(open_, high, low, close) <= 0:
            continue
        if high < max(open_, close) or low > min(open_, close) or high < low:
            continue
        volume = _to_float(row.get("Volume")) or 0.0
        rows.append({
            "date": idx.isoformat() if hasattr(idx, "isoformat") else str(idx),
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "ohlc_imputed": ohlc_imputed,
        })
    return rows


def fetch_yfinance_rows(symbol, period="5y", interval="1d"):
    try:
        import warnings
        warnings.filterwarnings("ignore")
        import yfinance as yf
    except Exception as exc:
        raise RuntimeError("yfinance is required for live Yahoo Finance fetches") from exc

    # Long-horizon price geometry must be split/distribution adjusted; otherwise
    # a corporate action can manufacture a false mass wall or volatility jump.
    df = yf.Ticker(symbol).history(period=period, interval=interval, auto_adjust=True)
    rows = _df_to_rows(df)
    if not rows:
        raise RuntimeError(f"yfinance returned no rows for {symbol}")
    return rows


def fetch_stooq_daily_rows(symbol):
    stooq_symbol = STOOQ_SYMBOLS.get(symbol.upper(), symbol.lower())
    encoded = urllib.parse.quote(stooq_symbol)
    url = f"https://stooq.com/q/d/l/?s={encoded}&i=d"
    with urllib.request.urlopen(url, timeout=20) as resp:
        text = resp.read().decode("utf-8", errors="replace")
    if "No data" in text or not text.strip():
        raise RuntimeError(f"Stooq returned no rows for {symbol}")
    rows = []
    for raw in csv.DictReader(text.splitlines()):
        row = _row_from_mapping(raw)
        if row and row["close"] > 0:
            rows.append(row)
    rows.sort(key=lambda r: r["date"])
    if not rows:
        raise RuntimeError(f"Stooq CSV parsed no rows for {symbol}")
    return rows


def fetch_price_rows(symbol, period="5y", interval="1d", stooq_fallback=True):
    try:
        return fetch_yfinance_rows(symbol, period=period, interval=interval), "Yahoo Finance via yfinance"
    except Exception as exc:
        if not stooq_fallback or interval != "1d":
            raise
        try:
            return (
                fetch_stooq_daily_rows(symbol),
                f"Stooq daily CSV fallback (corporate-action adjustment not independently verified) "
                f"after yfinance error: {type(exc).__name__}",
            )
        except Exception:
            raise exc


def fetch_fred_series(series_id):
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={urllib.parse.quote(series_id)}"
    with urllib.request.urlopen(url, timeout=20) as resp:
        text = resp.read().decode("utf-8", errors="replace")
    out = {}
    for raw in csv.DictReader(text.splitlines()):
        value = _to_float(raw.get(series_id))
        if value is not None:
            out[raw["observation_date"]] = value
    return out


def fetch_vol_series(symbol, period="5y", interval="1d"):
    if not symbol:
        return {}, None, None
    fred_id = FRED_VOL_SERIES.get(symbol.upper())
    if fred_id and interval == "1d":
        try:
            return fetch_fred_series(fred_id), f"FRED {fred_id}", None
        except Exception as exc:
            fred_error = f"{type(exc).__name__}: {exc}"
    else:
        fred_error = None

    try:
        rows = fetch_yfinance_rows(symbol, period=period, interval=interval)
        return {date_key(r["date"]): r["close"] for r in rows}, f"Yahoo Finance via yfinance {symbol}", fred_error
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"
        if fred_error:
            err = f"FRED failed ({fred_error}); yfinance failed ({err})"
        return {}, None, err


def latest_on_or_before(series, key):
    if not series:
        return None, None
    key = date_key(key)
    keys = sorted(k for k in series.keys() if k <= key)
    if not keys:
        return None, None
    last = keys[-1]
    return series[last], last


def merge_price_and_volume_rows(price_rows, volume_rows=None):
    if len({date_key(r["date"]) for r in price_rows}) != len(price_rows):
        raise ValueError("price rows contain duplicate dates")
    if not volume_rows:
        out = []
        for row in price_rows:
            row = dict(row)
            row["volume_proxy_close"] = row["close"]
            row["volume_proxy_volume"] = row.get("volume") or 0.0
            row["dollar_volume"] = row["volume_proxy_close"] * row["volume_proxy_volume"]
            out.append(row)
        return out

    price_by_date = {date_key(r["date"]): r for r in price_rows}
    by_date = {date_key(r["date"]): r for r in volume_rows}
    if len(by_date) != len(volume_rows):
        raise ValueError("volume-proxy rows contain duplicate dates")
    common = sorted(set(price_by_date) & set(by_date))
    if not common:
        return []
    first, last = common[0], common[-1]
    price_inner = {key for key in price_by_date if first <= key <= last}
    volume_inner = {key for key in by_date if first <= key <= last}
    if price_inner != volume_inner:
        raise ValueError("price and volume-proxy calendars differ inside their common date range")
    merged = []
    for row in price_rows:
        key = date_key(row["date"])
        proxy = by_date.get(key)
        if not proxy:
            continue
        out = dict(row)
        out["volume_proxy_close"] = proxy["close"]
        out["volume_proxy_volume"] = proxy.get("volume") or 0.0
        out["dollar_volume"] = out["volume_proxy_close"] * out["volume_proxy_volume"]
        merged.append(out)
    return merged


def compute_masses(rows, half_life=DEFAULT_HALF_LIFE):
    dollar_volumes = [r.get("dollar_volume") for r in rows if _to_float(r.get("dollar_volume")) and r.get("dollar_volume") > 0]
    med_dv = median(dollar_volumes) or 1.0
    ranges = [row_range_log(r) for r in rows]
    med_range = median([r for r in ranges if r and r > 0]) or 0.0
    n = len(rows)
    masses = []
    recencies = []
    absorptions = []
    for i, row in enumerate(rows):
        age = n - 1 - i
        recency = 0.5 ** (age / max(float(half_life), 1.0))
        observed_dv = _to_float(row.get("dollar_volume"))
        dv = observed_dv if observed_dv is not None and observed_dv > 0 else med_dv * 0.10
        volume_term = math.sqrt(clamp(max(dv, 0.0) / med_dv if med_dv > 0 else 1.0, 0.10, 10.0))
        range_log = ranges[i]
        if med_range > 0 and range_log and range_log > 0:
            absorption = math.sqrt(clamp(med_range / range_log, 0.25, 4.0))
        else:
            absorption = 1.0
        masses.append(recency * volume_term * absorption)
        recencies.append(recency)
        absorptions.append(absorption)
    return masses, recencies, med_dv, absorptions


def concentration_score(log_prices, masses):
    if len(log_prices) < 3:
        return 0.0
    lo = min(log_prices)
    hi = max(log_prices)
    if hi <= lo:
        return 1.0
    bins = min(32, max(8, int(math.sqrt(len(log_prices))) + 4))
    bucket = [0.0 for _ in range(bins)]
    width = hi - lo
    for value, mass in zip(log_prices, masses):
        idx = int((value - lo) / width * bins)
        idx = min(bins - 1, max(0, idx))
        bucket[idx] += mass
    total = sum(bucket)
    if total <= 0:
        return 0.0
    p = [b / total for b in bucket if b > 0]
    if len(p) <= 1:
        return 1.0
    hhi = sum(x * x for x in p)
    hhi_min = 1.0 / len(p)
    hhi_norm = (hhi - hhi_min) / max(1.0 - hhi_min, 1e-9)
    q05 = weighted_quantile(log_prices, masses, 0.05)
    q25 = weighted_quantile(log_prices, masses, 0.25)
    q75 = weighted_quantile(log_prices, masses, 0.75)
    q95 = weighted_quantile(log_prices, masses, 0.95)
    profile_width = max((q95 or hi) - (q05 or lo), 1e-9)
    iqr_width = max((q75 or hi) - (q25 or lo), 0.0)
    profile_compactness = clamp(1.0 - iqr_width / profile_width)
    return clamp(0.55 * profile_compactness + 0.45 * math.sqrt(clamp(hhi_norm)))


def effective_sample_score(masses):
    if not masses:
        return 0.0, 0.0
    total = sum(masses)
    total_sq = sum(m * m for m in masses)
    if total <= 0 or total_sq <= 0:
        return 0.0, 0.0
    eff_n = total * total / total_sq
    raw_ratio = eff_n / len(masses)
    score = clamp((raw_ratio - 0.05) / 0.55)
    return score, raw_ratio


def estimate_ou_gravity(rows, half_life=DEFAULT_HALF_LIFE):
    """Estimate mean reversion of log-price residuals around rolling centers.

    Regression: dy_t = a + b * y_{t-1} + eps_t, where
    y_t = log(price_t) - rolling_center_log_{t-1}.  The lagged center prevents
    the same close from mechanically moving its own reference point. Positive
    kappa = -b implies pull back toward the prior accepted-participation center.
    """
    if len(rows) < 35:
        return {
            "kappa": None,
            "kappa_tstat": None,
            "ou_half_life_bars": None,
            "ou_residual_sigma": None,
            "ou_phi": None,
            "ou_samples": 0,
        }

    residuals = []
    min_window = min(30, max(10, len(rows) // 3))
    for end in range(len(rows)):
        if end + 1 < min_window:
            residuals.append(None)
            continue
        basic = compute_center_basic(rows[:end], half_life=half_life)
        close = _to_float(rows[end].get("close"))
        if not basic or close is None or close <= 0:
            residuals.append(None)
            continue
        residuals.append(math.log(close) - basic["center_log"])

    pairs = []
    for prev, cur in zip(residuals, residuals[1:]):
        if prev is None or cur is None:
            continue
        pairs.append((prev, cur - prev))

    if len(pairs) < 30:
        return {
            "kappa": None,
            "kappa_tstat": None,
            "ou_half_life_bars": None,
            "ou_residual_sigma": None,
            "ou_phi": None,
            "ou_samples": len(pairs),
        }

    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    xbar = sum(xs) / len(xs)
    ybar = sum(ys) / len(ys)
    sxx = sum((x - xbar) ** 2 for x in xs)
    if sxx <= 1e-18:
        return {
            "kappa": None,
            "kappa_tstat": None,
            "ou_half_life_bars": None,
            "ou_residual_sigma": None,
            "ou_phi": None,
            "ou_samples": len(pairs),
        }
    b = sum((x - xbar) * (y - ybar) for x, y in zip(xs, ys)) / sxx
    a = ybar - b * xbar
    residual = [y - (a + b * x) for x, y in zip(xs, ys)]
    dof = max(len(residual) - 2, 1)
    s2 = sum(e * e for e in residual) / dof
    se_b = math.sqrt(max(s2 / sxx, 0.0))
    kappa = -b
    kappa_tstat = kappa / se_b if se_b > 0 else None
    ou_half_life = math.log(2.0) / kappa if kappa and kappa > 0 else None
    phi = clamp(1.0 - kappa, -0.99, 0.999) if kappa is not None else None
    return {
        "kappa": kappa,
        "kappa_tstat": kappa_tstat,
        "ou_half_life_bars": ou_half_life,
        "ou_residual_sigma": math.sqrt(max(s2, 0.0)),
        "ou_phi": phi,
        "ou_samples": len(pairs),
    }


def amihud_lightness_z(rows, lookback=None):
    window = rows[-lookback:] if lookback else rows
    values = []
    for prev, cur in zip(window, window[1:]):
        prev_close = _to_float(prev.get("close"))
        close = _to_float(cur.get("close"))
        dv = _to_float(cur.get("dollar_volume"))
        if prev_close and close and prev_close > 0 and close > 0 and dv and dv > 0:
            values.append(abs(math.log(close / prev_close)) / dv * 1e9)
    if len(values) < 10:
        return 0.0
    now = median(values[-5:]) or 0.0
    med = median(values) or 0.0
    mad = median([abs(v - med) for v in values]) or 0.0
    if mad <= 0:
        return 0.0
    return (now - med) / (1.4826 * mad)


def volume_profile_nodes(rows, half_life=DEFAULT_HALF_LIFE, bins=48):
    valid = [r for r in rows if _to_float(r.get("close")) and r["close"] > 0]
    if len(valid) < 10:
        return {"support": [], "resistance": [], "profile_concentration": None}
    masses, _, _, _ = compute_masses(valid, half_life=half_life)
    logs = [math.log(r["close"]) for r in valid]
    current = valid[-1]["close"]
    lo, hi = min(logs), max(logs)
    if hi <= lo:
        return {"support": [], "resistance": [], "profile_concentration": 100.0}
    bins = min(max(int(bins), 12), 80)
    width = (hi - lo) / bins
    buckets = [0.0 for _ in range(bins)]
    for value, mass in zip(logs, masses):
        idx = min(bins - 1, max(0, int((value - lo) / max(width, 1e-12))))
        buckets[idx] += mass
    total = sum(buckets)
    if total <= 0:
        return {"support": [], "resistance": [], "profile_concentration": None}
    shares = [b / total for b in buckets]
    nonzero = [s for s in shares if s > 0]
    entropy = -sum(s * math.log(s) for s in nonzero)
    profile_concentration = clamp(1.0 - entropy / math.log(len(shares))) * 100.0 if len(shares) > 1 else 100.0
    smooth = []
    for i in range(bins):
        vals = shares[max(0, i - 1): min(bins, i + 2)]
        smooth.append(sum(vals) / len(vals))
    cutoff = max(median(smooth) or 0.0, sum(smooth) / len(smooth))
    levels = []
    for i, share in enumerate(smooth):
        if share < cutoff:
            continue
        log_mid = lo + (i + 0.5) * width
        price = math.exp(log_mid)
        distance = abs(math.log(current / price)) if price > 0 and current > 0 else 0.0
        levels.append({
            "price": price,
            "strength": share / (1.0 + 5.0 * distance),
            "mass_share": shares[i],
            "distance_pct": pct(price, current),
        })
    supports = sorted((x for x in levels if x["price"] < current), key=lambda x: x["strength"], reverse=True)[:3]
    resistances = sorted((x for x in levels if x["price"] > current), key=lambda x: x["strength"], reverse=True)[:3]
    return {
        "support": supports,
        "resistance": resistances,
        "support_1": supports[0] if supports else None,
        "resistance_1": resistances[0] if resistances else None,
        "profile_concentration": profile_concentration,
    }


def gravity_metrics(rows, center, vol_ctx, half_life=DEFAULT_HALF_LIFE):
    ou = estimate_ou_gravity(rows, half_life=half_life)
    lightness_z = amihud_lightness_z(rows, lookback=len(rows))
    concentration = (center.get("quality_components") or {}).get("concentration", 0.0) / 100.0
    kappa_score = norm01(ou.get("kappa_tstat"), 0.0, 3.0)
    proximity_score = 1.0 - norm01(abs(center.get("distance_z") or 0.0), 0.5, 3.0)
    realized = vol_ctx.get("realized_vol_21d") or 0.0
    vol_stability = 1.0 - norm01(realized, 0.12, 0.45)
    lightness = norm01(lightness_z, 0.0, 3.0)
    gravity_score = clamp(
        0.30 * concentration
        + 0.30 * kappa_score
        + 0.20 * proximity_score
        + 0.20 * vol_stability
        - 0.20 * lightness
    ) * 100.0
    levitation_score = clamp(
        1.0 - gravity_score / 100.0
        + 0.25 * lightness
        + 0.15 * norm01(abs(center.get("distance_z") or 0.0), 2.0, 5.0)
    ) * 100.0
    if gravity_score >= 65.0 and kappa_score > 0.45:
        regime = "centered_mean_reverting"
    elif levitation_score >= 65.0:
        regime = "levitating_thin_or_momentum"
    else:
        regime = "mixed_gravity"
    out = dict(ou)
    out.update({
        "gravity_score": gravity_score,
        "levitation_score": levitation_score,
        "gravity_regime": regime,
        "amihud_lightness_z": lightness_z,
        "mean_reversion_score": kappa_score * 100.0,
        "center_proximity_score": proximity_score * 100.0,
        "vol_stability_score": vol_stability * 100.0,
        "thinness_score": lightness * 100.0,
    })
    return out


def compute_center_basic(rows, half_life=DEFAULT_HALF_LIFE):
    valid = [r for r in rows if _to_float(r.get("close")) and r["close"] > 0]
    if len(valid) < 3:
        return None
    masses, recencies, median_dv, absorptions = compute_masses(valid, half_life=half_life)
    logs = [math.log(r["close"]) for r in valid]
    com_log = weighted_mean(logs, masses)
    if com_log is None:
        return None
    total_mass = sum(masses)
    volume_coverage_ratio = sum(
        1 for row in valid if (_to_float(row.get("dollar_volume")) or 0.0) > 0
    ) / len(valid)
    variance = sum(w * (x - com_log) ** 2 for x, w in zip(logs, masses)) / max(total_mass, 1e-12)
    sigma = math.sqrt(max(variance, 1e-12))
    current_log = logs[-1]
    return {
        "rows": valid,
        "log_prices": logs,
        "masses": masses,
        "recencies": recencies,
        "absorptions": absorptions,
        "median_dollar_volume": median_dv,
        "median_absorption": median(absorptions),
        "center_log": com_log,
        "center_price": math.exp(com_log),
        "mass_sigma": sigma,
        "current_log": current_log,
        "current_price": valid[-1]["close"],
        "mass_quantity": total_mass,
        "mass_quantity_ratio": total_mass / max(sum(recencies), 1e-12),
        "volume_coverage_ratio": volume_coverage_ratio,
    }


def score_center(rows, half_life=DEFAULT_HALF_LIFE):
    basic = compute_center_basic(rows, half_life=half_life)
    if not basic:
        return {
            "available": False,
            "reason": "not enough valid rows",
            "quality_score": 0.0,
            "regime": "no_reliable_center",
        }

    logs = basic["log_prices"]
    masses = basic["masses"]
    com_log = basic["center_log"]
    sigma = max(basic["mass_sigma"], 1e-6)
    total_mass = max(sum(masses), 1e-12)

    concentration = concentration_score(logs, masses)
    near_mass = sum(w for x, w in zip(logs, masses) if abs(x - com_log) <= sigma)
    local_density = clamp(near_mass / total_mass / 0.6827)
    below = sum(w for x, w in zip(logs, masses) if x < com_log)
    above = sum(w for x, w in zip(logs, masses) if x > com_log)
    balance = clamp(1.0 - abs(below - above) / total_mass)
    effective_sample, effective_sample_ratio = effective_sample_score(masses)
    volume_coverage = clamp(basic.get("volume_coverage_ratio"))

    shift = min(10, max(3, len(rows) // 12))
    if len(rows) > shift + 10:
        prev_basic = compute_center_basic(rows[:-shift], half_life=half_life)
        if prev_basic:
            center_shift_z = abs(com_log - prev_basic["center_log"]) / sigma
            stability = clamp(1.0 - center_shift_z / 1.5)
        else:
            center_shift_z = None
            stability = 0.5
    else:
        center_shift_z = None
        stability = 0.5

    quality = 100.0 * (
        0.24 * concentration
        + 0.24 * local_density
        + 0.20 * stability
        + 0.16 * balance
        + 0.16 * effective_sample
    )
    # A missing-volume center is still a useful recency/range heuristic, but it
    # is not entitled to the same confidence as observed dollar-volume mass.
    quality *= 0.65 + 0.35 * volume_coverage
    distance_z = (basic["current_log"] - com_log) / sigma

    if quality < 40:
        regime = "no_reliable_center"
    elif quality >= 55 and abs(distance_z) <= 2.5:
        regime = "active_center"
    elif quality >= 55 and abs(distance_z) > 2.5:
        regime = "detached_from_mass"
    else:
        regime = "transition_weak_center"

    out = dict(basic)
    out.update({
        "available": True,
        "quality_score": quality,
        "quality_components": {
            "concentration": concentration * 100.0,
            "local_density": local_density * 100.0,
            "stability": stability * 100.0,
            "balance": balance * 100.0,
            "effective_sample_ratio": effective_sample * 100.0,
            "volume_coverage": volume_coverage * 100.0,
        },
        "effective_sample_raw_ratio": effective_sample_ratio,
        "center_shift_z": center_shift_z,
        "distance_z": distance_z,
        "has_center": quality >= 40,
        "usable_center": quality >= 55,
        "active_center": quality >= 55 and abs(distance_z) <= 2.5,
        "regime": regime,
        "mass_sigma_pct": (math.exp(sigma) - 1.0) * 100.0,
    })
    return out


def _calendar_age_days(as_of_date, observation_date):
    try:
        as_of = dt.date.fromisoformat(date_key(as_of_date))
        observed = dt.date.fromisoformat(date_key(observation_date))
    except (TypeError, ValueError):
        return None
    return (as_of - observed).days


def volatility_context(
    rows,
    vol_series=None,
    fallback_vol_series=None,
    as_of_date=None,
    max_implied_vol_age_days=DEFAULT_MAX_IMPLIED_VOL_AGE_DAYS,
):
    as_of_date = date_key(as_of_date or rows[-1]["date"])
    realized_21 = annualized_realized_vol(rows, lookback=21)
    realized_10 = annualized_realized_vol(rows, lookback=10)
    realized_63 = annualized_realized_vol(rows, lookback=63)
    implied_raw, implied_date = latest_on_or_before(vol_series or {}, as_of_date)
    fallback_raw, fallback_date = latest_on_or_before(fallback_vol_series or {}, as_of_date)
    primary_date = implied_date
    implied_age_days = _calendar_age_days(as_of_date, implied_date) if implied_date else None
    fallback_age_days = _calendar_age_days(as_of_date, fallback_date) if fallback_date else None
    max_age = max(0, int(max_implied_vol_age_days))
    primary_invalid = implied_raw is not None and implied_raw <= 0
    fallback_invalid = fallback_raw is not None and fallback_raw <= 0
    primary_stale = implied_raw is not None and (implied_age_days is None or implied_age_days > max_age)
    fallback_stale = fallback_raw is not None and (fallback_age_days is None or fallback_age_days > max_age)
    if primary_stale or primary_invalid:
        implied_raw = None
    if fallback_stale or fallback_invalid:
        fallback_raw = None
    implied_source = None
    implied_vol = None
    if implied_raw is not None:
        implied_vol = implied_raw / 100.0
        implied_source = "primary"
    elif fallback_raw is not None:
        implied_vol = fallback_raw / 100.0
        implied_date = fallback_date
        implied_source = "fallback"

    if implied_vol is not None and realized_21 is not None and implied_source == "primary":
        annual_vol = 0.45 * realized_21 + 0.55 * implied_vol
        blend_method = "45pct_realized_55pct_primary_implied"
    elif implied_vol is not None and realized_21 is not None:
        # A broad-market fallback is not security-specific enough to pull a
        # volatile asset's cone inward. It may only act as a volatility floor.
        annual_vol = max(realized_21, implied_vol)
        blend_method = "max_realized_or_fallback_proxy"
    elif implied_vol is not None:
        annual_vol = implied_vol
        blend_method = "implied_only"
    elif realized_21 is not None:
        annual_vol = realized_21
        blend_method = "realized_only"
    else:
        annual_vol = 0.20
        blend_method = "constant_20pct_last_resort"

    ratio = None
    if implied_raw is not None and fallback_raw not in (None, 0):
        ratio = implied_raw / fallback_raw

    return {
        "realized_vol_10d": realized_10,
        "realized_vol_21d": realized_21,
        "realized_vol_63d": realized_63,
        "implied_vol": implied_vol,
        "implied_vol_raw": implied_raw if implied_raw is not None else fallback_raw,
        "implied_vol_date": implied_date if implied_raw is not None else fallback_date,
        "implied_vol_source": implied_source,
        "implied_vol_max_age_days": max_age,
        "primary_vol_observation_date": primary_date,
        "primary_vol_age_calendar_days": implied_age_days,
        "primary_vol_stale": primary_stale,
        "primary_vol_invalid": primary_invalid,
        "fallback_vol_observation_date": fallback_date,
        "fallback_vol_age_calendar_days": fallback_age_days,
        "fallback_vol_stale": fallback_stale,
        "fallback_vol_invalid": fallback_invalid,
        "fallback_vol_raw": fallback_raw,
        "fallback_vol_date": fallback_date,
        "primary_to_fallback_vol_ratio": ratio,
        "annual_vol_used": annual_vol,
        "annual_vol_blend_method": blend_method,
    }


def volatility_build_up(rows, center, vol_ctx):
    rv10 = vol_ctx.get("realized_vol_10d")
    rv21 = vol_ctx.get("realized_vol_21d")
    rv63 = vol_ctx.get("realized_vol_63d")
    implied = vol_ctx.get("implied_vol")
    ratio = vol_ctx.get("primary_to_fallback_vol_ratio")

    if rv10 is not None and rv63 and rv10 < rv63:
        realized_compression = clamp((rv63 - rv10) / rv63)
    else:
        realized_compression = 0.0

    implied_pressure_parts = []
    if implied is not None and rv21 and rv21 > 0:
        implied_pressure_parts.append(clamp((implied / rv21 - 1.0) / 1.0))
    if ratio is not None:
        implied_pressure_parts.append(clamp((ratio - 1.0) / 0.70))
    implied_pressure = sum(implied_pressure_parts) / len(implied_pressure_parts) if implied_pressure_parts else 0.0

    distance_z = abs(center.get("distance_z") or 0.0)
    distance_pressure = clamp(distance_z / 3.0)
    potential_energy = 0.5 * distance_z * distance_z

    returns_5 = log_returns(rows, lookback=5)
    five_day_return = sum(returns_5[-5:]) if returns_5 else 0.0
    daily_sd = simple_stdev(log_returns(rows, lookback=21)) or 0.0
    if daily_sd > 0:
        momentum_z = five_day_return / (daily_sd * math.sqrt(max(len(returns_5[-5:]), 1)))
    else:
        momentum_z = 0.0
    kinetic_energy = 0.5 * momentum_z * momentum_z
    kinetic_pressure = clamp(abs(momentum_z) / 2.5)

    recent_dv = _to_float(rows[-1].get("dollar_volume"))
    med_dv = median([r.get("dollar_volume") for r in rows[-63:] if _to_float(r.get("dollar_volume"))])
    if recent_dv is not None and med_dv and med_dv > 0:
        volume_pressure = clamp((math.sqrt(recent_dv / med_dv) - 1.0) / 1.5)
    else:
        volume_pressure = 0.0

    score = 100.0 * (
        0.30 * realized_compression
        + 0.25 * implied_pressure
        + 0.20 * distance_pressure
        + 0.15 * kinetic_pressure
        + 0.10 * volume_pressure
    )

    return {
        "score": score,
        "components": {
            "realized_vol_compression": realized_compression * 100.0,
            "implied_vol_pressure": implied_pressure * 100.0,
            "distance_from_center_pressure": distance_pressure * 100.0,
            "kinetic_pressure": kinetic_pressure * 100.0,
            "volume_pressure": volume_pressure * 100.0,
        },
        "potential_energy": potential_energy,
        "kinetic_energy": kinetic_energy,
        "momentum_z": momentum_z,
    }


def center_weight(center, build_up=None):
    quality = center.get("quality_score") or 0.0
    distance_z = abs(center.get("distance_z") or 0.0)
    gravity = center.get("gravity") or {}
    gravity_score = gravity.get("gravity_score")
    levitation_score = gravity.get("levitation_score")
    if quality < 40:
        return 0.0
    weight = clamp((quality - 40.0) / 35.0) * 0.85
    if quality < 55:
        weight *= 0.50
    if distance_z > 2.5:
        weight *= 0.35
    if gravity_score is not None:
        weight *= 0.75 + 0.25 * clamp(gravity_score / 100.0)
    if levitation_score is not None and levitation_score >= 70:
        weight *= 0.70
    if build_up and build_up.get("score", 0.0) >= 75:
        weight *= 0.85
    return clamp(weight, 0.0, 0.85)


def forecast_ou_logs(current_log, center_log, gravity, horizon, z, fallback_daily_sigma):
    kappa = _to_float((gravity or {}).get("kappa"))
    phi = _to_float((gravity or {}).get("ou_phi"))
    resid_sigma = _to_float((gravity or {}).get("ou_residual_sigma"))
    if (
        kappa is None
        or kappa <= 0.01
        or phi is None
        or abs(phi) >= 1.0
        or resid_sigma is None
        or resid_sigma <= 0
    ):
        sd = fallback_daily_sigma * math.sqrt(max(horizon, 1))
        return current_log - z * sd, current_log + z * sd, 0.0
    mean = center_log + (current_log - center_log) * (phi ** horizon)
    denom = max(1.0 - phi ** 2, 1e-9)
    variance = resid_sigma ** 2 * (1.0 - phi ** (2 * horizon)) / denom
    sd = math.sqrt(max(variance, 0.0))
    return mean - z * sd, mean + z * sd, 1.0


def boundary_rows(state, horizons, confidences, calibration=None, apply_calibration=False, boundary_model="mass_vol"):
    center = state["center"]
    vol_ctx = state["volatility"]
    build = state["volatility_build_up"]
    current_log = center["current_log"]
    current_price = center["current_price"]
    com_log = center["center_log"]
    sigma = max(center["mass_sigma"], 1e-6)
    annual_vol = vol_ctx["annual_vol_used"]
    weight = center_weight(center, build)
    distance_z = center.get("distance_z") or 0.0
    gravity = center.get("gravity") or {}
    gravity_score = gravity.get("gravity_score") or 0.0
    kappa_score = gravity.get("mean_reversion_score") or 0.0
    if boundary_model == "ou_hybrid":
        gravity_boundary_weight = clamp((gravity_score - 45.0) / 35.0) * clamp(kappa_score / 70.0)
    else:
        gravity_boundary_weight = 0.0
    daily_sigma = annual_vol / math.sqrt(252.0)
    out = []

    calibration = calibration or {}
    for horizon in horizons:
        horizon = int(horizon)
        for confidence in confidences:
            confidence = float(confidence)
            z = z_for_confidence(confidence)
            vol_width = z * annual_vol * math.sqrt(horizon / 252.0)
            vol_lower = current_log - vol_width
            vol_upper = current_log + vol_width

            mass_width = z * sigma
            mass_lower = com_log - mass_width
            mass_upper = com_log + mass_width
            ou_lower, ou_upper, ou_available = forecast_ou_logs(
                current_log,
                com_log,
                gravity,
                horizon,
                z,
                daily_sigma,
            )
            ou_weight = gravity_boundary_weight * ou_available
            profile_lower = ou_weight * ou_lower + (1.0 - ou_weight) * mass_lower
            profile_upper = ou_weight * ou_upper + (1.0 - ou_weight) * mass_upper

            lower_weight = weight
            upper_weight = weight
            if abs(distance_z) > 2.5:
                if distance_z > 0:
                    upper_weight = 0.0
                else:
                    lower_weight = 0.0

            lower = lower_weight * profile_lower + (1.0 - lower_weight) * vol_lower
            upper = upper_weight * profile_upper + (1.0 - upper_weight) * vol_upper
            lower = min(lower, current_log)
            upper = max(upper, current_log)

            cal_key = f"{horizon}|{confidence:.2f}"
            multiplier = 1.0
            if apply_calibration:
                calibration_row = calibration.get(cal_key) or {}
                multiplier = (
                    calibration_row.get("suggested_width_multiplier", 1.0)
                    if calibration_row.get("reliable")
                    else 1.0
                )
                multiplier = max(1.0, _to_float(multiplier) or 1.0)
                lower = current_log - (current_log - lower) * multiplier
                upper = current_log + (upper - current_log) * multiplier

            zone_half = max(0.0015, 0.10 * vol_width)
            row = {
                "horizon_days": horizon,
                "confidence": confidence,
                "z_score": z,
                "center_weight": weight,
                "lower_center_weight": lower_weight,
                "upper_center_weight": upper_weight,
                "gravity_boundary_weight": ou_weight,
                "calibration_multiplier": multiplier,
                "lower_boundary": math.exp(lower),
                "upper_boundary": math.exp(upper),
                "lower_zone_low": math.exp(lower - zone_half),
                "lower_zone_high": math.exp(lower + zone_half),
                "upper_zone_low": math.exp(upper - zone_half),
                "upper_zone_high": math.exp(upper + zone_half),
                "vol_lower": math.exp(vol_lower),
                "vol_upper": math.exp(vol_upper),
                "mass_lower": math.exp(mass_lower),
                "mass_upper": math.exp(mass_upper),
                "profile_lower": math.exp(profile_lower),
                "profile_upper": math.exp(profile_upper),
                "ou_lower": math.exp(ou_lower),
                "ou_upper": math.exp(ou_upper),
                "distance_lower_pct": pct(math.exp(lower), current_price),
                "distance_upper_pct": pct(math.exp(upper), current_price),
            }
            out.append(row)
    return out


def analyze_state(rows, vol_series=None, fallback_vol_series=None, lookback=DEFAULT_LOOKBACK, half_life=DEFAULT_HALF_LIFE):
    if len(rows) < 30:
        raise RuntimeError("At least 30 bars are required for market-mass analysis")
    window = rows[-min(lookback, len(rows)):]
    center = score_center(window, half_life=half_life)
    if not center.get("available"):
        raise RuntimeError(center.get("reason") or "center calculation unavailable")
    vol_ctx = volatility_context(rows, vol_series=vol_series, fallback_vol_series=fallback_vol_series, as_of_date=rows[-1]["date"])
    build = volatility_build_up(rows, center, vol_ctx)
    center["gravity"] = gravity_metrics(window, center, vol_ctx, half_life=half_life)
    center["profile_nodes"] = volume_profile_nodes(window, half_life=half_life)
    center["center_weight"] = center_weight(center, build)
    return {
        "as_of": rows[-1]["date"],
        "center": center,
        "volatility": vol_ctx,
        "volatility_build_up": build,
    }


def backtest_calibration(rows, vol_series, fallback_vol_series, lookback, half_life, horizons, confidences, max_evals=750):
    if len(rows) < lookback + max(horizons) + 5:
        return {
            "available": False,
            "reason": f"not enough rows for lookback={lookback} and horizons={max(horizons)}",
            "rows_tested": 0,
            "by_band": [],
            "multipliers": {},
        }

    start = lookback - 1
    end = len(rows) - max(horizons) - 1
    total_candidates = max(end - start + 1, 1)
    step = max(1, total_candidates // max(max_evals, 1))
    buckets = {}
    tested = 0

    for idx in range(start, end + 1, step):
        hist = rows[:idx + 1]
        try:
            state = analyze_state(hist, vol_series, fallback_vol_series, lookback=lookback, half_life=half_life)
            bands = boundary_rows(state, horizons, confidences)
        except Exception:
            continue
        band_map = {(b["horizon_days"], round(b["confidence"], 2)): b for b in bands}
        for horizon in horizons:
            future = rows[idx + 1: idx + horizon + 1]
            if len(future) < horizon:
                continue
            future_close = future[-1]["close"]
            future_high = max(r.get("high", r["close"]) for r in future)
            future_low = min(r.get("low", r["close"]) for r in future)
            for confidence in confidences:
                key = (int(horizon), round(float(confidence), 2))
                band = band_map[key]
                bucket = buckets.setdefault(key, {
                    "horizon_days": key[0],
                    "confidence": key[1],
                    "samples": 0,
                    "close_hits": 0,
                    "path_hits": 0,
                    "required_width_multipliers": [],
                })
                bucket["samples"] += 1
                if band["lower_boundary"] <= future_close <= band["upper_boundary"]:
                    bucket["close_hits"] += 1
                if band["lower_boundary"] <= future_low and future_high <= band["upper_boundary"]:
                    bucket["path_hits"] += 1
                current = float(state["center"]["current_price"])
                lower_width = max(math.log(current / band["lower_boundary"]), 1e-12)
                upper_width = max(math.log(band["upper_boundary"] / current), 1e-12)
                lower_excursion = max(math.log(current / future_low), 0.0)
                upper_excursion = max(math.log(future_high / current), 0.0)
                bucket["required_width_multipliers"].append(
                    max(lower_excursion / lower_width, upper_excursion / upper_width)
                )
        tested += 1

    by_band = []
    multipliers = {}
    for key in sorted(buckets):
        bucket = buckets[key]
        samples = max(bucket["samples"], 1)
        close_cov = bucket["close_hits"] / samples
        path_cov = bucket["path_hits"] / samples
        target = bucket["confidence"]
        required = sorted(bucket.pop("required_width_multipliers", []))
        if required:
            # Empirical target quantile of the path excursion relative to each
            # forecast's own asymmetric width.  Coverage ratios are not linear
            # in width, so target/path_coverage is not a valid width estimator.
            rank = max(0, min(len(required) - 1, math.ceil(target * len(required)) - 1))
            raw_suggested = required[rank]
            suggested = min(2.5, max(1.0, raw_suggested))
        else:
            raw_suggested = None
            suggested = 1.0
        row = {
            **bucket,
            "close_coverage": close_cov,
            "path_coverage": path_cov,
            "target_coverage": target,
            "suggested_width_multiplier": suggested,
            "uncapped_width_multiplier": raw_suggested,
            "calibrated_path_coverage": (
                sum(1 for value in required if value <= suggested) / len(required)
                if required else None
            ),
            "multiplier_cap_binding": bool(
                raw_suggested is not None and raw_suggested > 2.5
            ),
            "minimum_reliable_samples": 30,
            "reliable": bucket["samples"] >= 30,
        }
        by_band.append(row)
        multipliers[f"{key[0]}|{key[1]:.2f}"] = row

    return {
        "available": bool(by_band),
        "decision_grade": False,
        "point_in_time_forecasts": True,
        "evaluation": "in_sample_empirical_path_coverage",
        "multiplier_method": "empirical target quantile of required asymmetric log-width",
        "calibration_end_date": date_key(rows[end]["date"]),
        "rows_tested": tested,
        "by_band": by_band,
        "multipliers": multipliers,
    }


def compact_center(center):
    keys = [
        "current_price", "center_price", "quality_score", "distance_z",
        "mass_quantity", "mass_quantity_ratio", "mass_sigma_pct",
        "volume_coverage_ratio",
        "effective_sample_raw_ratio", "center_shift_z", "center_weight",
        "has_center", "usable_center", "active_center", "regime",
    ]
    out = {k: center.get(k) for k in keys}
    out["quality_components"] = center.get("quality_components")
    out["gravity"] = center.get("gravity")
    out["profile_nodes"] = center.get("profile_nodes")
    return out


def rounded_for_json(obj):
    if isinstance(obj, float):
        return round(obj, 6)
    if isinstance(obj, list):
        return [rounded_for_json(x) for x in obj]
    if isinstance(obj, dict):
        return {
            k: rounded_for_json(v)
            for k, v in obj.items()
            if k not in {"rows", "log_prices", "masses", "recencies", "absorptions"}
        }
    return obj


def write_boundaries_csv(path, rows):
    path = Path(path)
    fields = [
        "horizon_days", "confidence", "center_weight", "lower_center_weight", "upper_center_weight",
        "gravity_boundary_weight", "calibration_multiplier",
        "lower_zone_low", "lower_boundary", "lower_zone_high",
        "upper_zone_low", "upper_boundary", "upper_zone_high",
        "distance_lower_pct", "distance_upper_pct",
        "vol_lower", "vol_upper", "mass_lower", "mass_upper", "profile_lower", "profile_upper", "ou_lower", "ou_upper",
    ]
    serialized = ({k: _round(row.get(k), 6) for k in fields} for row in rows)
    atomic_write_csv(path, serialized, fields)


def write_markdown(path, summary, boundaries):
    path = Path(path)
    cur = summary["current"]
    center = cur["center"]
    vol = cur["volatility"]
    build = cur["volatility_build_up"]
    gravity = center.get("gravity") or {}
    nodes = center.get("profile_nodes") or {}
    support_1 = nodes.get("support_1") or {}
    resistance_1 = nodes.get("resistance_1") or {}
    lines = [
        "# Market Mass Boundary Report",
        "",
        "Research output only. Boundary zones are probabilistic, not guaranteed support/resistance.",
        "Decision grade: false. These levels do not include executable option-chain economics or portfolio sizing.",
        "",
        f"- Price ticker: {summary['inputs']['price_ticker']}",
        f"- Volume proxy: {summary['inputs'].get('volume_ticker') or summary['inputs']['price_ticker']}",
        f"- As of: {cur['as_of']}",
        f"- Regime: {center['regime']}",
        f"- Current price: {center['current_price']:.2f}",
        f"- Center of mass: {center['center_price']:.2f}",
        f"- Distance from center: {center['distance_z']:.2f} sigma",
        f"- Center quality: {center['quality_score']:.1f}/100",
        f"- Center weight in bands: {center['center_weight']:.2f}",
        f"- Mass quantity ratio: {center['mass_quantity_ratio']:.2f}",
        f"- Gravity score: {(gravity.get('gravity_score') or 0):.1f}/100",
        f"- Levitation score: {(gravity.get('levitation_score') or 0):.1f}/100",
        f"- Gravity regime: {gravity.get('gravity_regime') or 'n/a'}",
        f"- OU kappa / t-stat: {(gravity.get('kappa') or 0):.4f} / {(gravity.get('kappa_tstat') or 0):.2f}",
        f"- Amihud lightness z: {(gravity.get('amihud_lightness_z') or 0):.2f}",
        f"- Vol build-up score: {build['score']:.1f}/100",
        f"- Realized vol 21d: {((vol.get('realized_vol_21d') or 0) * 100):.1f}%",
        f"- Implied vol used: {((vol.get('implied_vol') or 0) * 100):.1f}%",
        f"- Annual vol used: {((vol.get('annual_vol_used') or 0) * 100):.1f}%",
        f"- Nearest support HVN: {_round(support_1.get('price'), 2) if support_1 else 'n/a'}",
        f"- Nearest resistance HVN: {_round(resistance_1.get('price'), 2) if resistance_1 else 'n/a'}",
        "",
        "## Boundary Zones",
        "",
        "| Horizon | Confidence | Lower zone | Upper zone | Center weight | OU weight |",
        "|---:|---:|---:|---:|---:|---:|",
    ]
    for row in boundaries:
        lines.append(
            f"| {row['horizon_days']}d | {row['confidence']:.0%} | "
            f"{row['lower_zone_low']:.2f} - {row['lower_zone_high']:.2f} | "
            f"{row['upper_zone_low']:.2f} - {row['upper_zone_high']:.2f} | "
            f"{row['center_weight']:.2f} | {row.get('gravity_boundary_weight', 0):.2f} |"
        )
    if summary.get("calibration", {}).get("available"):
        lines += [
            "",
            "## Backtest Calibration",
            "",
            "| Horizon | Confidence | Close coverage | Path coverage | Suggested width | Applied-eligible |",
            "|---:|---:|---:|---:|---:|---:|",
        ]
        for row in summary["calibration"]["by_band"]:
            lines.append(
                f"| {row['horizon_days']}d | {row['confidence']:.0%} | "
                f"{row['close_coverage']:.1%} | {row['path_coverage']:.1%} | "
                f"{row['suggested_width_multiplier']:.2f}x | {row.get('reliable', False)} |"
            )
    atomic_write_text(path, "\n".join(lines) + "\n")


def build_summary(args, market_rows, price_source, vol_series, vol_source, vol_error, fallback_series, fallback_source, fallback_error):
    state = analyze_state(
        market_rows,
        vol_series=vol_series,
        fallback_vol_series=fallback_series,
        lookback=args.lookback,
        half_life=args.half_life,
    )
    calibration = {}
    if args.calibrate or args.apply_calibration:
        calibration = backtest_calibration(
            market_rows,
            vol_series,
            fallback_series,
            lookback=args.lookback,
            half_life=args.half_life,
            horizons=args.horizons,
            confidences=args.confidences,
            max_evals=args.max_backtest_evals,
        )
    boundaries = boundary_rows(
        state,
        args.horizons,
        args.confidences,
        calibration=calibration.get("multipliers") if calibration else None,
        apply_calibration=args.apply_calibration,
        boundary_model=args.boundary_model,
    )
    warnings = []
    if args.price_ticker.upper().startswith("^") and not args.volume_ticker:
        warnings.append("Index price has no direct tradable volume; pass --volume-ticker QQQ for NDX-style analysis.")
    if vol_error:
        warnings.append(f"Primary volatility fetch issue: {vol_error}")
    if fallback_error:
        warnings.append(f"Fallback volatility fetch issue: {fallback_error}")
    if state["volatility"].get("implied_vol") is None:
        warnings.append("No implied volatility series was available; boundaries use realized volatility only.")
    if (state["center"].get("volume_coverage_ratio") or 0.0) < 0.80:
        warnings.append("Fewer than 80% of mass-window bars had positive dollar volume; center confidence is penalized.")
    if state["volatility"].get("primary_vol_stale"):
        warnings.append("Primary implied-volatility observation exceeded the freshness limit and was excluded.")
    if state["volatility"].get("fallback_vol_stale"):
        warnings.append("Fallback implied-volatility observation exceeded the freshness limit and was excluded.")
    if state["volatility"].get("primary_vol_invalid") or state["volatility"].get("fallback_vol_invalid"):
        warnings.append("A non-positive volatility observation was excluded.")
    if "Stooq daily CSV fallback" in str(price_source):
        warnings.append("Stooq fallback corporate-action adjustment was not independently verified; treat long-horizon geometry as provisional.")
    if args.apply_calibration and not calibration.get("available"):
        warnings.append("Calibration was requested but unavailable; current boundaries were not widened.")
    elif args.apply_calibration and any(not row.get("reliable") for row in calibration.get("by_band", [])):
        warnings.append("At least one calibration band had fewer than 30 samples and was not applied.")

    summary = {
        "schemaVersion": 1,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "inputs": {
            "price_ticker": args.price_ticker,
            "volume_ticker": args.volume_ticker,
            "vol_ticker": args.vol_ticker,
            "fallback_vol_ticker": args.fallback_vol_ticker,
            "period": args.period,
            "interval": args.interval,
            "lookback": args.lookback,
            "half_life": args.half_life,
            "horizons": args.horizons,
            "confidences": args.confidences,
            "boundary_model": args.boundary_model,
            "apply_calibration": args.apply_calibration,
        },
        "sources": {
            "price": price_source,
            "primary_volatility": vol_source,
            "fallback_volatility": fallback_source,
            "fred_note": "FRED VXNCLS/VIXCLS are used as no-key daily volatility backups when available.",
            "data_grade": "Research-grade; not execution-grade market data.",
        },
        "warnings": warnings,
        "data": {
            "rows": len(market_rows),
            "first_date": market_rows[0]["date"],
            "last_date": market_rows[-1]["date"],
        },
        "current": {
            "as_of": state["as_of"],
            "center": compact_center(state["center"]),
            "volatility": state["volatility"],
            "volatility_build_up": state["volatility_build_up"],
        },
        "boundaries": boundaries,
        "calibration": calibration or {"available": False, "reason": "not requested"},
        "validation": {
            "decisionGrade": False,
            "researchOnly": True,
            "calibrationEvaluation": (
                calibration.get("evaluation") if calibration else "not_requested"
            ),
            "adjustedPricePolicy": (
                "yfinance_auto_adjust_true"
                if "Yahoo Finance" in str(price_source)
                else "source_adjustment_not_independently_verified"
            ),
            "limitations": [
                "Boundaries are distributional research estimates, not executable option strikes.",
                "Calibration is in-sample empirical path coverage unless independently evaluated later.",
                "Broad volatility proxies do not reproduce a security's option skew or term structure.",
            ],
        },
        "disclaimer": "Probabilistic research model only; boundaries are not guaranteed support/resistance and are not financial advice.",
    }
    return rounded_for_json(summary), boundaries


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="Analyze market center-of-mass and probabilistic boundary zones.")
    ap.add_argument("--price-ticker", default="QQQ", help="Price ticker, e.g. QQQ or ^NDX.")
    ap.add_argument("--volume-ticker", default=None, help="Optional tradable volume proxy, e.g. QQQ for ^NDX.")
    ap.add_argument("--vol-ticker", default="^VXN", help="Primary implied volatility proxy.")
    ap.add_argument("--fallback-vol-ticker", default="^VIX", help="Fallback implied volatility proxy.")
    ap.add_argument("--period", default="5y", help="yfinance period, e.g. 2y, 5y, max.")
    ap.add_argument("--interval", choices=["1d"], default="1d",
                    help="Daily bars only; annualization and horizon units assume trading days.")
    ap.add_argument("--input-csv", default=None, help="Optional local OHLCV CSV instead of live price fetch.")
    ap.add_argument("--gravity-profile", choices=sorted(GRAVITY_PROFILES), default=None,
                    help="Named lookback/half-life profile: structural=252/63, swing=84/21, tactical=63/14.")
    ap.add_argument("--lookback", type=int, default=DEFAULT_LOOKBACK, help="Bars in the mass window.")
    ap.add_argument("--half-life", type=float, default=DEFAULT_HALF_LIFE, help="Recency half-life in bars.")
    ap.add_argument("--horizons", type=lambda s: parse_num_list(s, int), default=list(DEFAULT_HORIZONS))
    ap.add_argument("--confidences", "--confidence", dest="confidences",
                    type=lambda s: parse_num_list(s, float), default=list(DEFAULT_CONFIDENCES))
    ap.add_argument("--boundary-model", choices=["mass_vol", "ou_hybrid"], default="mass_vol",
                    help="mass_vol keeps the proven mass/vol blend; ou_hybrid lets OU gravity influence profile boundaries.")
    ap.add_argument("--calibrate", action="store_true", help="Backtest whether historical bands contained future prices.")
    ap.add_argument("--apply-calibration", action="store_true", help="Inflate current bands using backtest path coverage.")
    ap.add_argument("--max-backtest-evals", type=int, default=750)
    ap.add_argument("--no-stooq-fallback", action="store_true", help="Disable Stooq fallback for daily price fetches.")
    ap.add_argument("--out-json", default=str(DEFAULT_OUT_DIR / "latest_summary.json"))
    ap.add_argument("--out-csv", default=str(DEFAULT_OUT_DIR / "latest_boundaries.csv"))
    ap.add_argument("--out-md", default=str(DEFAULT_OUT_DIR / "latest_report.md"))
    args = ap.parse_args(argv)
    if args.gravity_profile:
        args.lookback, args.half_life = GRAVITY_PROFILES[args.gravity_profile]
    if args.lookback < 30:
        ap.error("--lookback must be at least 30 daily bars")
    if not math.isfinite(args.half_life) or args.half_life <= 0:
        ap.error("--half-life must be positive and finite")
    if any(horizon <= 0 for horizon in args.horizons):
        ap.error("all --horizons must be positive")
    if any(not 0 < confidence < 1 for confidence in args.confidences):
        ap.error("all --confidences must be between 0 and 1")
    if args.max_backtest_evals <= 0:
        ap.error("--max-backtest-evals must be positive")
    return args


def main(argv=None):
    args = parse_args(argv)
    if not args.horizons:
        raise SystemExit("--horizons cannot be empty")
    if not args.confidences:
        raise SystemExit("--confidences cannot be empty")

    if args.input_csv:
        price_rows = read_ohlcv_csv(args.input_csv)
        price_source = "local OHLCV CSV input"
        volume_rows = None
    else:
        price_rows, price_source = fetch_price_rows(
            args.price_ticker,
            period=args.period,
            interval=args.interval,
            stooq_fallback=not args.no_stooq_fallback,
        )
        volume_rows = None
        if args.volume_ticker and args.volume_ticker != args.price_ticker:
            volume_rows, volume_source = fetch_price_rows(
                args.volume_ticker,
                period=args.period,
                interval=args.interval,
                stooq_fallback=not args.no_stooq_fallback,
            )
            price_source = f"{price_source}; volume proxy {args.volume_ticker} from {volume_source}"

    market_rows = merge_price_and_volume_rows(price_rows, volume_rows)
    if len(market_rows) < 30:
        raise SystemExit(f"Only {len(market_rows)} merged rows available; need at least 30.")

    vol_series, vol_source, vol_error = fetch_vol_series(args.vol_ticker, period=args.period, interval=args.interval)
    fallback_series, fallback_source, fallback_error = fetch_vol_series(
        args.fallback_vol_ticker,
        period=args.period,
        interval=args.interval,
    )
    summary, boundaries = build_summary(
        args,
        market_rows,
        price_source,
        vol_series,
        vol_source,
        vol_error,
        fallback_series,
        fallback_source,
        fallback_error,
    )

    out_json = Path(args.out_json)
    atomic_write_json(out_json, summary)
    write_boundaries_csv(args.out_csv, boundaries)
    write_markdown(args.out_md, summary, boundaries)

    cur = summary["current"]["center"]
    build = summary["current"]["volatility_build_up"]
    print(
        f"{args.price_ticker} {summary['current']['as_of']} "
        f"price={cur['current_price']:.2f} center={cur['center_price']:.2f} "
        f"quality={cur['quality_score']:.1f} regime={cur['regime']} "
        f"build_up={build['score']:.1f}"
    )
    print(f"Wrote {args.out_json}")
    print(f"Wrote {args.out_csv}")
    print(f"Wrote {args.out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

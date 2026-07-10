#!/usr/bin/env python3
"""Build a multi-source company financial-status lens for held positions.

The producer reads the latest embedded dashboard payload, scores operating
companies currently held in the portfolio, and writes:

    output/financial_status.json
    output/financial_status.csv
    output/financial_status_report.md

FMP is the primary source when available. Yahoo Finance (via yfinance) and SEC
EDGAR companyfacts are used as fallbacks/corroborating sources so API-plan gaps
do not turn every incomplete FMP row into DATA_REVIEW.

Secrets stay out of repo files. FMP is read from FMP_API_KEY or
~/.config/ptrak/fmp.json. SEC access uses SEC_USER_AGENT, --sec-user-agent, or
secUserAgent in the same config file.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import re
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:
    from scripts.artifact_io import atomic_write_csv, atomic_write_json, atomic_write_text
    from scripts.dashboard_payload import read_dashboard_payload as _read_dashboard_payload
except ModuleNotFoundError:  # direct ``python scripts/financial_status_score.py``
    from artifact_io import atomic_write_csv, atomic_write_json, atomic_write_text
    from dashboard_payload import read_dashboard_payload as _read_dashboard_payload


ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "output" / "portfolio_dashboard.html"
OUT_JSON = ROOT / "output" / "financial_status.json"
OUT_CSV = ROOT / "output" / "financial_status.csv"
OUT_MD = ROOT / "output" / "financial_status_report.md"
CACHE_PATH = ROOT / "output" / "financial_status_fmp_cache.json"
DEFAULT_CONFIG = Path.home() / ".config" / "ptrak" / "fmp.json"
FMP_BASE = "https://financialmodelingprep.com/stable"
SEC_BASE = "https://data.sec.gov"
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
MODEL_VERSION = "0.4.0"

SOURCE_DOCS = [
    "https://site.financialmodelingprep.com/developer/docs/stable/financial-scores",
    "https://site.financialmodelingprep.com/developer/docs/stable/metrics-ratios-ttm",
    "https://site.financialmodelingprep.com/developer/docs/stable/key-metrics-ttm",
    "https://site.financialmodelingprep.com/developer/docs/stable/financial-statement-growth",
    "https://site.financialmodelingprep.com/developer/docs/stable/earnings-company",
    "https://site.financialmodelingprep.com/developer/docs/stable/earnings-calendar",
    "https://ranaroussi.github.io/yfinance/reference/api/yfinance.Ticker.quarterly_income_stmt.html",
    "https://ranaroussi.github.io/yfinance/reference/api/yfinance.Ticker.quarterly_balance_sheet.html",
    "https://ranaroussi.github.io/yfinance/reference/api/yfinance.Ticker.quarterly_cashflow.html",
    "https://ranaroussi.github.io/yfinance/reference/api/yfinance.Ticker.earnings_dates.html",
    "https://www.sec.gov/search-filings/edgar-application-programming-interfaces",
]

COMPONENT_WEIGHTS = {
    "profitability": 0.25,
    "balanceSheet": 0.20,
    "cashFlow": 0.20,
    "growth": 0.20,
    "valuation": 0.10,
    "dataConfidence": 0.05,
}
FINAL_WEIGHTS = {
    "financialStatus": 0.45,
    "earningsReport": 0.25,
    "nextEarningsSafety": 0.20,
    "dataConfidence": 0.10,
}

ETF_SYMBOLS = {"QQQ", "VOO", "SPMO", "SOXQ", "HBMX", "RAM"}
NON_COMPANY_SYMBOLS = {}
ETF_NAME_HINTS = (
    " ETF",
    "EXCHANGE-TRADED",
    " INDEX FUND",
    " INDEX FUNDS",
    "INVESCO QQQ",
    "VANGUARD INDEX",
    "ETF OPPORTUNITIES",
    "SPDR",
    "ISHARES",
    "PROSHARES",
)


def clamp(value: Optional[float], lo: float = 0.0, hi: float = 100.0, fallback: float = 50.0) -> float:
    if value is None or not math.isfinite(value):
        value = fallback
    return max(lo, min(hi, float(value)))


def rn(value: Optional[float], digits: int = 2) -> Optional[float]:
    if value is None or not math.isfinite(value):
        return None
    return round(float(value), digits)


def finite(value: Any) -> bool:
    try:
        return value is not None and math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def num(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    return value


def pct_ratio(value: Any, *, unit: str) -> Optional[float]:
    """Normalize an explicitly declared ratio unit to decimal form.

    Guessing from magnitude is unsafe: 3.5 can legitimately mean either 350%
    or 3.5%.  Every upstream adapter in this module emits decimal ratios, while
    callers handling a percent-valued field must say ``unit="percent"``.
    """
    x = num(value)
    if x is None:
        return None
    if unit == "percent":
        return x / 100.0
    if unit == "decimal":
        return x
    raise ValueError(f"unsupported ratio unit: {unit!r}")


def first_num(row: Optional[dict], *keys: str, ratio_unit: Optional[str] = None) -> Optional[float]:
    if not isinstance(row, dict):
        return None
    for key in keys:
        if key in row:
            value = pct_ratio(row.get(key), unit=ratio_unit) if ratio_unit else num(row.get(key))
            if value is not None:
                return value
    return None


def first_not_none(*values: Any) -> Any:
    """Return the first non-None value without treating zero as missing."""
    for value in values:
        if value is not None:
            return value
    return None


def first_str(row: Optional[dict], *keys: str) -> Optional[str]:
    if not isinstance(row, dict):
        return None
    for key in keys:
        value = row.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def as_list(payload: Any) -> List[dict]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict) and payload:
        if "Error Message" in payload or "error" in payload:
            return []
        return [payload]
    return []


def parse_date(value: Any) -> Optional[dt.date]:
    if not value:
        return None
    text = str(value)[:10]
    try:
        return dt.date.fromisoformat(text)
    except ValueError:
        return None


def weighted_average(items: Iterable[Tuple[Optional[float], float]], fallback: float = 50.0) -> float:
    total = 0.0
    weight = 0.0
    for value, w in items:
        if value is None or not math.isfinite(float(value)):
            continue
        total += float(value) * w
        weight += w
    if weight <= 0:
        return fallback
    return clamp(total / weight, fallback=fallback)


def high_score(value: Optional[float], bad: float, good: float, fallback: Optional[float] = None) -> Optional[float]:
    if value is None:
        return fallback
    if good == bad:
        return fallback
    return clamp((value - bad) / (good - bad) * 100.0)


def low_score(value: Optional[float], good: float, bad: float, fallback: Optional[float] = None) -> Optional[float]:
    if value is None:
        return fallback
    if bad == good:
        return fallback
    return clamp((bad - value) / (bad - good) * 100.0)


def banded_low_score(value: Optional[float], good_ceiling: float, bad_ceiling: float) -> Optional[float]:
    """Valuation sanity: high is reasonable, low is distressed or very expensive."""
    if value is None:
        return None
    if value <= 0:
        return 35.0
    if value <= good_ceiling:
        return 88.0
    return clamp(88.0 - (value - good_ceiling) / (bad_ceiling - good_ceiling) * 68.0)


def extract_dashboard_payload(path: Path) -> dict:
    return _read_dashboard_payload(path)


def validate_private_config(config: Path) -> None:
    """Require a current-user-owned regular 0600 file before reading secrets."""
    info = config.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise PermissionError("FMP config must be a regular, non-symlink file")
    if info.st_uid != os.getuid():
        raise PermissionError("FMP config must be owned by the current user")
    if info.st_mode & 0o077:
        raise PermissionError("FMP config permissions must be 0600")


def load_api_key(config: Path) -> Optional[str]:
    env_key = os.environ.get("FMP_API_KEY")
    if env_key:
        return env_key.strip()
    if not config.exists():
        return None
    validate_private_config(config)
    raw = json.loads(config.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        return None
    key = raw.get("apiKey") or raw.get("apikey") or raw.get("key")
    return str(key).strip() if key else None


def load_config(config: Path) -> dict:
    if not config.exists():
        return {}
    validate_private_config(config)
    try:
        raw = json.loads(config.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        return {}


def load_sec_user_agent(config: Path, override: Optional[str] = None) -> Optional[str]:
    if override:
        return override.strip()
    env_ua = os.environ.get("SEC_USER_AGENT")
    if env_ua:
        return env_ua.strip()
    raw = load_config(config)
    value = raw.get("secUserAgent") or raw.get("sec_user_agent")
    return str(value).strip() if value else None


def source_status(status: dict, source: str) -> dict:
    out = dict(status)
    out["source"] = source
    return out


def json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dt.datetime):
        return value.isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    if hasattr(value, "item"):
        try:
            return json_safe(value.item())
        except Exception:
            pass
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            pass
    return value


class FmpClient:
    def __init__(
        self,
        api_key: Optional[str],
        cache_path: Path,
        *,
        no_fetch: bool = False,
        refresh_cache: bool = False,
        cache_ttl_hours: float = 18.0,
        pause_seconds: float = 0.12,
    ) -> None:
        self.api_key = api_key
        self.cache_path = cache_path
        self.no_fetch = no_fetch
        self.refresh_cache = refresh_cache
        self.cache_ttl_hours = cache_ttl_hours
        self.pause_seconds = pause_seconds
        self.calls = 0
        self.cache_hits = 0
        self.source_calls: Dict[str, int] = {}
        self.source_cache_hits: Dict[str, int] = {}
        self.errors: List[str] = []
        self.fmp_global_circuit_status: Optional[int] = None
        self.fmp_blocked_endpoints = set()
        self.cache: Dict[str, Any] = {"entries": {}}
        if cache_path.exists():
            try:
                self.cache = json.loads(cache_path.read_text(encoding="utf-8"))
                if not isinstance(self.cache, dict):
                    self.cache = {"entries": {}}
                self.cache.setdefault("entries", {})
            except (OSError, ValueError):
                self.cache = {"entries": {}}

    def cache_key(self, endpoint: str, params: dict) -> str:
        clean = {k: v for k, v in params.items() if k.lower() != "apikey"}
        return json.dumps([endpoint, sorted(clean.items())], separators=(",", ":"), sort_keys=True)

    def cache_entry(self, key: str) -> Tuple[Any, Optional[str]]:
        entry = self.cache.get("entries", {}).get(key)
        if not entry or self.refresh_cache:
            return None, None
        fetched_at = entry.get("fetchedAt")
        fresh = False
        if fetched_at:
            try:
                age = dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
                fresh = 0 <= age.total_seconds() <= self.cache_ttl_hours * 3600
            except ValueError:
                fresh = False
        if fresh:
            return entry.get("payload"), str(entry.get("status") or "cache")
        if self.no_fetch:
            original = str(entry.get("status") or "cache")
            status = "stale_cache_error" if "error" in original else "stale_cache"
            return entry.get("payload"), status
        return None, None

    def put_cache(self, key: str, endpoint: str, params: dict, status: str, payload: Any) -> None:
        self.cache.setdefault("entries", {})[key] = {
            "fetchedAt": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
            "endpoint": endpoint,
            "params": params,
            "status": status,
            "payload": json_safe(payload),
        }

    def bump_call(self, source: str) -> None:
        self.calls += 1
        self.source_calls[source] = self.source_calls.get(source, 0) + 1

    def bump_cache_hit(self, source: str) -> None:
        self.cache_hits += 1
        self.source_cache_hits[source] = self.source_cache_hits.get(source, 0) + 1

    def get(self, endpoint: str, params: dict) -> Tuple[Any, str]:
        key = self.cache_key(endpoint, params)
        cached_payload, cached_status = self.cache_entry(key)
        if cached_status is not None:
            self.bump_cache_hit("fmp")
            return cached_payload, (
                cached_status if ("error" in cached_status or cached_status.startswith("stale_"))
                else "cache"
            )
        if self.no_fetch:
            return None, "missing_cache"
        if not self.api_key:
            return None, "missing_key"
        if self.fmp_global_circuit_status is not None:
            return {"error": f"FMP circuit open after HTTP {self.fmp_global_circuit_status}",
                    "httpStatus": self.fmp_global_circuit_status}, "circuit_open"
        if endpoint in self.fmp_blocked_endpoints:
            return {"error": "FMP endpoint unavailable under the current plan", "httpStatus": 402}, "circuit_open"

        query = dict(params)
        query["apikey"] = self.api_key
        url = f"{FMP_BASE}/{endpoint}?" + urllib.parse.urlencode(query)
        try:
            with urllib.request.urlopen(url, timeout=35) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            self.bump_call("fmp")
            detail = exc.read().decode("utf-8", errors="ignore")[:160]
            self.errors.append(f"{endpoint} {params.get('symbol','')}: HTTP {exc.code} {detail}")
            payload = {"error": detail, "httpStatus": exc.code}
            if exc.code == 402:
                self.fmp_blocked_endpoints.add(endpoint)
            elif exc.code in (401, 403, 429):
                self.fmp_global_circuit_status = exc.code
            self.put_cache(key, endpoint, {k: v for k, v in params.items() if k.lower() != "apikey"}, "http_error", payload)
            return payload, "http_error"
        except Exception as exc:  # network and JSON failures are soft per-symbol errors
            self.bump_call("fmp")
            self.errors.append(f"{endpoint} {params.get('symbol','')}: {type(exc).__name__} {str(exc)[:140]}")
            payload = {"error": f"{type(exc).__name__}: {str(exc)[:140]}"}
            self.put_cache(key, endpoint, {k: v for k, v in params.items() if k.lower() != "apikey"}, "error", payload)
            return payload, "error"

        self.bump_call("fmp")
        self.put_cache(key, endpoint, {k: v for k, v in params.items() if k.lower() != "apikey"}, "fresh", payload)
        time.sleep(self.pause_seconds)
        return payload, "fresh"

    def get_json_url(self, source: str, endpoint: str, url: str, headers: Optional[dict] = None) -> Tuple[Any, str]:
        key = self.cache_key(f"{source}:{endpoint}", {"url": url})
        cached_payload, cached_status = self.cache_entry(key)
        if cached_status is not None:
            self.bump_cache_hit(source)
            return cached_payload, (
                cached_status if ("error" in cached_status or cached_status.startswith("stale_"))
                else "cache"
            )
        if self.no_fetch:
            return None, "missing_cache"
        try:
            req = urllib.request.Request(url, headers=headers or {})
            with urllib.request.urlopen(req, timeout=35) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            self.bump_call(source)
            detail = exc.read().decode("utf-8", errors="ignore")[:160]
            self.errors.append(f"{source}:{endpoint}: HTTP {exc.code} {detail}")
            payload = {"error": detail, "httpStatus": exc.code}
            self.put_cache(key, f"{source}:{endpoint}", {"url": url}, "http_error", payload)
            return payload, "http_error"
        except Exception as exc:
            self.bump_call(source)
            self.errors.append(f"{source}:{endpoint}: {type(exc).__name__} {str(exc)[:140]}")
            payload = {"error": f"{type(exc).__name__}: {str(exc)[:140]}"}
            self.put_cache(key, f"{source}:{endpoint}", {"url": url}, "error", payload)
            return payload, "error"
        self.bump_call(source)
        self.put_cache(key, f"{source}:{endpoint}", {"url": url}, "fresh", payload)
        time.sleep(self.pause_seconds)
        return payload, "fresh"

    def save(self) -> None:
        self.cache["updatedAt"] = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
        atomic_write_json(self.cache_path, self.cache)


def classify_omission(stock: dict) -> Optional[str]:
    sym = str(stock.get("sym") or "").upper()
    name = str(stock.get("name") or "").upper()
    asset = str(stock.get("assetClass") or "").upper()
    if sym in NON_COMPANY_SYMBOLS:
        return NON_COMPANY_SYMBOLS[sym]
    if sym in ETF_SYMBOLS:
        return "ETF/fund exposure; company fundamentals lens is not applicable"
    if "ETF" in asset or any(hint in name for hint in ETF_NAME_HINTS):
        return "ETF/fund exposure; company fundamentals lens is not applicable"
    return None


def portfolio_rows(payload: dict, forced_symbols: Optional[List[str]] = None) -> Tuple[List[dict], List[dict]]:
    all_stocks = [s for s in payload.get("stocks", []) if isinstance(s, dict) and s.get("held")]
    # Exposure always uses the complete held-equity denominator.  Filtering a
    # report to one or two tickers must not make those names look like 100% of
    # the portfolio.
    market_value = sum(num(s.get("value")) or 0.0 for s in all_stocks)
    stocks = all_stocks
    if forced_symbols:
        wanted = {s.upper() for s in forced_symbols}
        stocks = [s for s in stocks if str(s.get("sym") or "").upper() in wanted]
    scored, omitted = [], []
    for stock in stocks:
        row = dict(stock)
        value = num(row.get("value"))
        row["portfolioWeightPct"] = (
            round((value or 0.0) / market_value * 100.0, 4)
            if market_value > 0
            else None
        )
        reason = classify_omission(row)
        if reason:
            omitted.append(
                {
                    "ticker": row.get("sym"),
                    "name": row.get("name"),
                    "assetClass": row.get("assetClass"),
                    "theme": row.get("theme"),
                    "value": rn(num(row.get("value")), 2),
                    "portfolioWeightPct": row["portfolioWeightPct"],
                    "reason": reason,
                }
            )
        else:
            scored.append(row)
    return scored, omitted


def latest_by_date(rows: List[dict], *, reverse: bool = True) -> Optional[dict]:
    dated = [(parse_date(r.get("date")), r) for r in rows if parse_date(r.get("date"))]
    if not dated:
        return rows[0] if rows else None
    dated.sort(key=lambda x: x[0], reverse=reverse)
    return dated[0][1]


def endpoint_status(payload: Any, status: str) -> dict:
    rows = as_list(payload)
    err = ""
    if isinstance(payload, dict):
        err = str(payload.get("Error Message") or payload.get("error") or "")[:160]
    unusable = any(token in str(status) for token in ("error", "missing", "circuit", "stale"))
    return {"status": status, "rows": len(rows), "ok": bool(rows) and not unusable, "error": err or None}


def fetch_calendar(client: FmpClient, today: dt.date, horizon_days: int) -> Tuple[Dict[str, dict], dict]:
    payload, status = client.get(
        "earnings-calendar",
        {
            "from": today.isoformat(),
            "to": (today + dt.timedelta(days=horizon_days)).isoformat(),
            "includeReportTimes": "true",
        },
    )
    rows = []
    for row in as_list(payload):
        day = parse_date(row.get("date"))
        if day and day >= today:
            rows.append((day, row))
    rows.sort(key=lambda x: x[0])
    by_symbol: Dict[str, dict] = {}
    for _, row in rows:
        sym = str(row.get("symbol") or "").upper()
        if sym and sym not in by_symbol:
            by_symbol[sym] = row
    return by_symbol, endpoint_status(payload, status)


def fetch_company_bundle(client: FmpClient, symbol: str) -> Tuple[dict, dict]:
    specs = {
        "profile": ("profile", {"symbol": symbol}),
        "financialScores": ("financial-scores", {"symbol": symbol}),
        "ratiosTtm": ("ratios-ttm", {"symbol": symbol}),
        "keyMetricsTtm": ("key-metrics-ttm", {"symbol": symbol}),
        "financialGrowth": ("financial-growth", {"symbol": symbol, "period": "quarter", "limit": "5"}),
        "earnings": ("earnings", {"symbol": symbol, "limit": "5"}),
    }
    bundle: Dict[str, Any] = {}
    statuses: Dict[str, dict] = {}
    for name, (endpoint, params) in specs.items():
        payload, status = client.get(endpoint, params)
        bundle[name] = payload
        statuses[name] = source_status(endpoint_status(payload, status), "fmp")
    return bundle, statuses


def df_empty(df: Any) -> bool:
    return df is None or bool(getattr(df, "empty", True))


def df_row(df: Any, *names: str) -> Optional[Any]:
    if df_empty(df):
        return None
    exact = {str(idx): idx for idx in getattr(df, "index", [])}
    lowered = {str(idx).lower().replace("_", " ").replace("-", " "): idx for idx in getattr(df, "index", [])}
    for name in names:
        if name in exact:
            return df.loc[exact[name]]
        norm = name.lower().replace("_", " ").replace("-", " ")
        if norm in lowered:
            return df.loc[lowered[norm]]
    return None


def df_columns(df: Any, limit: int = 4) -> List[Any]:
    if df_empty(df):
        return []
    cols = list(getattr(df, "columns", []))
    try:
        cols.sort(reverse=True)
    except TypeError:
        pass
    return cols[:limit]


def df_sum(
    df: Any,
    names: Iterable[str],
    columns: Optional[List[Any]] = None,
    *,
    required_periods: int = 4,
) -> Optional[float]:
    row = df_row(df, *names)
    if row is None:
        return None
    cols = columns or df_columns(df, 4)
    if len(cols) != required_periods:
        return None
    vals = []
    for col in cols:
        try:
            value = num(row[col])
        except Exception:
            value = None
        if value is not None:
            vals.append(value)
    return sum(vals) if len(vals) == required_periods else None


def df_latest(df: Any, names: Iterable[str]) -> Optional[float]:
    row = df_row(df, *names)
    if row is None:
        return None
    for col in df_columns(df, 1):
        try:
            value = num(row[col])
        except Exception:
            value = None
        if value is not None:
            return value
    return None


def df_value_at_column(df: Any, names: Iterable[str], column: Any) -> Optional[float]:
    """Return one named balance-sheet value at one exact reporting date."""
    row = df_row(df, *tuple(names))
    if row is None:
        return None
    try:
        return num(row[column])
    except Exception:
        return None


def yahoo_comprehensive_debt(balance: Any) -> Tuple[Optional[float], Optional[str]]:
    """Use reported total debt or a same-period current + noncurrent sum.

    The fallback intentionally requires both components. Treating a lone
    current or noncurrent row as total debt materially understates leverage,
    while adding components to an already comprehensive total double-counts it.
    """
    columns = df_columns(balance, 1)
    if not columns:
        return None, None
    column = columns[0]
    reported_total = df_value_at_column(balance, ("Total Debt",), column)
    if reported_total is not None:
        return reported_total, "reported_total_debt"
    current = df_value_at_column(
        balance,
        (
            "Current Debt",
            "Current Debt And Capital Lease Obligation",
            "Current Debt And Finance Lease Obligation",
        ),
        column,
    )
    noncurrent = df_value_at_column(
        balance,
        (
            "Long Term Debt",
            "Long Term Debt And Capital Lease Obligation",
            "Long Term Debt And Finance Lease Obligation",
            "Long Term Debt Noncurrent",
        ),
        column,
    )
    if current is None or noncurrent is None:
        return None, None
    return current + noncurrent, "same_period_current_plus_noncurrent_debt"


def yahoo_average_invested_capital(balance: Any) -> Optional[float]:
    """Average the latest and approximately year-earlier invested capital."""
    columns = df_columns(balance, 8)
    if len(columns) < 2:
        return None
    latest_column = columns[0]
    latest_date = parse_date(latest_column)
    if latest_date is None:
        return None
    candidates = []
    for column in columns[1:]:
        column_date = parse_date(column)
        if column_date is None:
            continue
        age_days = (latest_date - column_date).days
        if 300 <= age_days <= 430:
            candidates.append((abs(age_days - 365), column))
    if not candidates:
        return None
    _, year_ago_column = min(candidates, key=lambda item: item[0])
    latest = df_value_at_column(balance, ("Invested Capital",), latest_column)
    year_ago = df_value_at_column(balance, ("Invested Capital",), year_ago_column)
    if latest is None or year_ago is None:
        return None
    average = (latest + year_ago) / 2.0
    return average if average > 0 else None


def standard_roic(
    operating_income: Optional[float],
    tax_provision: Optional[float],
    pretax_income: Optional[float],
    average_invested_capital: Optional[float],
) -> Tuple[Optional[float], Optional[float]]:
    """Return NOPAT / average invested capital and its effective tax rate."""
    if (
        operating_income is None
        or tax_provision is None
        or pretax_income is None
        or average_invested_capital is None
        or pretax_income <= 0
        or tax_provision < 0
        or average_invested_capital <= 0
    ):
        return None, None
    effective_tax_rate = tax_provision / pretax_income
    if not 0 <= effective_tax_rate <= 1:
        return None, None
    nopat = operating_income * (1.0 - effective_tax_rate)
    return nopat / average_invested_capital, effective_tax_rate


def df_yoy_growth(df: Any, names: Iterable[str]) -> Optional[float]:
    row = df_row(df, *names)
    if row is None:
        return None
    cols = df_columns(df, 5)
    if len(cols) < 5:
        return None
    try:
        cur = num(row[cols[0]])
        prev = num(row[cols[4]])
    except Exception:
        return None
    if cur is None or prev is None or abs(prev) < 1e-9:
        return None
    return cur / prev - 1.0


def safe_div(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None or b is None or abs(b) < 1e-9:
        return None
    return a / b


def info_num(info: dict, *keys: str) -> Optional[float]:
    for key in keys:
        value = num(info.get(key))
        if value is not None:
            return value
    return None


def info_str(info: dict, *keys: str) -> Optional[str]:
    for key in keys:
        value = info.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def build_yahoo_bundle(symbol: str, info: dict, fast_info: dict, income: Any, balance: Any, cashflow: Any, earnings_dates: Any) -> dict:
    cols_i = df_columns(income, 4)
    cols_c = df_columns(cashflow, 4)
    revenue = df_sum(income, ("Total Revenue", "Operating Revenue"), cols_i)
    gross_profit = df_sum(income, ("Gross Profit",), cols_i)
    operating_income = df_sum(income, ("Operating Income", "EBIT"), cols_i)
    ebit = df_sum(income, ("EBIT", "Operating Income"), cols_i)
    ebitda = df_sum(income, ("EBITDA", "Normalized EBITDA"), cols_i)
    net_income = df_sum(
        income,
        ("Net Income", "Net Income Common Stockholders", "Net Income From Continuing Operation Net Minority Interest"),
        cols_i,
    )
    interest_expense = df_sum(income, ("Interest Expense",), cols_i)
    pretax_income = df_sum(income, ("Pretax Income", "Income Before Tax"), cols_i)
    tax_provision = df_sum(income, ("Tax Provision", "Income Tax Expense"), cols_i)

    assets = df_latest(balance, ("Total Assets",))
    current_assets = df_latest(balance, ("Current Assets",))
    liabilities = df_latest(balance, ("Total Liabilities Net Minority Interest", "Total Liabilities"))
    current_liabilities = df_latest(balance, ("Current Liabilities",))
    equity = df_latest(balance, ("Stockholders Equity", "Common Stock Equity", "Total Equity Gross Minority Interest"))
    total_debt, debt_definition = yahoo_comprehensive_debt(balance)
    net_debt = df_latest(balance, ("Net Debt",))
    average_invested_capital = yahoo_average_invested_capital(balance)
    roic, roic_tax_rate = standard_roic(
        operating_income,
        tax_provision,
        pretax_income,
        average_invested_capital,
    )

    ocf = df_sum(cashflow, ("Operating Cash Flow", "Cash Flow From Continuing Operating Activities"), cols_c)
    fcf = df_sum(cashflow, ("Free Cash Flow",), cols_c)
    capex = df_sum(cashflow, ("Capital Expenditure",), cols_c)
    market_cap = first_not_none(info_num(fast_info, "marketCap"), info_num(info, "marketCap"))

    ratios = {
        "symbol": symbol,
        "grossProfitMarginTTM": safe_div(gross_profit, revenue),
        "operatingProfitMarginTTM": safe_div(operating_income, revenue),
        "ebitMarginTTM": safe_div(ebit, revenue),
        "netProfitMarginTTM": safe_div(net_income, revenue),
        "currentRatioTTM": safe_div(current_assets, current_liabilities),
        "quickRatioTTM": safe_div(num(info.get("quickRatio")), 1.0),
        "debtToEquityRatioTTM": safe_div(total_debt, equity),
        "debtToAssetsRatioTTM": safe_div(total_debt, assets),
        "interestCoverageRatioTTM": safe_div(ebit, abs(interest_expense) if interest_expense is not None else None),
        "operatingCashFlowSalesRatioTTM": safe_div(ocf, revenue),
        "freeCashFlowOperatingCashFlowRatioTTM": safe_div(fcf, ocf),
        "capitalExpenditureCoverageRatioTTM": safe_div(ocf, abs(capex) if capex is not None else None),
        "operatingCashFlowCoverageRatioTTM": safe_div(ocf, total_debt),
        "totalDebtDefinition": debt_definition,
        "priceToEarningsRatioTTM": info_num(info, "trailingPE"),
        "priceToEarningsGrowthRatioTTM": info_num(info, "pegRatio"),
        "priceToSalesRatioTTM": info_num(info, "priceToSalesTrailing12Months"),
        "priceToFreeCashFlowRatioTTM": safe_div(market_cap, fcf),
        "enterpriseValueMultipleTTM": info_num(info, "enterpriseToEbitda"),
    }
    metrics = {
        "symbol": symbol,
        "marketCap": market_cap,
        "currentRatioTTM": ratios["currentRatioTTM"],
        "netDebtToEBITDATTM": safe_div(net_debt, ebitda),
        "returnOnAssetsTTM": safe_div(net_income, assets),
        "returnOnEquityTTM": safe_div(net_income, equity),
        "returnOnInvestedCapitalTTM": roic,
        "returnOnInvestedCapitalMethod": (
            "nopat_ttm_over_average_invested_capital" if roic is not None else None
        ),
        "effectiveTaxRateTTMForROIC": roic_tax_rate,
        "freeCashFlowYieldTTM": safe_div(fcf, market_cap),
        "incomeQualityTTM": safe_div(ocf, net_income),
        "evToEBITDATTM": info_num(info, "enterpriseToEbitda"),
    }
    growth = {
        "symbol": symbol,
        "date": str(df_columns(income, 1)[0].date()) if df_columns(income, 1) and hasattr(df_columns(income, 1)[0], "date") else None,
        "revenueGrowth": df_yoy_growth(income, ("Total Revenue", "Operating Revenue")),
        "grossProfitGrowth": df_yoy_growth(income, ("Gross Profit",)),
        "operatingIncomeGrowth": df_yoy_growth(income, ("Operating Income", "EBIT")),
        "netIncomeGrowth": df_yoy_growth(income, ("Net Income", "Net Income Common Stockholders", "Net Income From Continuing Operation Net Minority Interest")),
        "operatingCashFlowGrowth": df_yoy_growth(cashflow, ("Operating Cash Flow", "Cash Flow From Continuing Operating Activities")),
        "freeCashFlowGrowth": df_yoy_growth(cashflow, ("Free Cash Flow",)),
    }

    earnings = []
    if not df_empty(earnings_dates):
        for idx, row in earnings_dates.iterrows():
            day = parse_date(idx)
            if not day:
                continue
            eps_est = num(row.get("EPS Estimate")) if hasattr(row, "get") else None
            eps_actual = num(row.get("Reported EPS")) if hasattr(row, "get") else None
            item = {"symbol": symbol, "date": day.isoformat(), "epsEstimated": eps_est, "epsActual": eps_actual}
            if idx is not None and hasattr(idx, "hour"):
                item["time"] = "amc" if int(getattr(idx, "hour", 0)) >= 16 else ("bmo" if int(getattr(idx, "hour", 0)) and int(getattr(idx, "hour", 0)) < 12 else None)
            earnings.append(item)

    profile = {
        "symbol": symbol,
        "companyName": info_str(info, "longName", "shortName"),
        "sector": info_str(info, "sector"),
        "industry": info_str(info, "industry"),
        "marketCap": market_cap,
        "beta": info_num(info, "beta"),
    }
    has_profile = any(value is not None for key, value in profile.items() if key != "symbol")
    metadata_keys = {
        "symbol", "periodBasis", "periodEnd", "totalDebtDefinition",
        "returnOnInvestedCapitalMethod",
    }
    has_ratios = any(value is not None for key, value in ratios.items() if key not in metadata_keys)
    has_metrics = any(value is not None for key, value in metrics.items() if key not in metadata_keys)
    return {
        "profile": [json_safe(profile)] if has_profile else [],
        "ratiosTtm": [json_safe(ratios)] if has_ratios else [],
        "keyMetricsTtm": [json_safe(metrics)] if has_metrics else [],
        "financialGrowth": [json_safe(growth)] if any(v is not None for k, v in growth.items() if k not in ("symbol", "date")) else [],
        "earnings": json_safe(earnings),
    }


def fetch_yahoo_bundle(client: FmpClient, symbol: str, enabled: bool = True) -> Tuple[dict, Dict[str, dict]]:
    if not enabled:
        return {}, {"yahoo": source_status({"status": "disabled", "rows": 0, "ok": False, "error": None}, "yahoo")}
    key = client.cache_key("yahoo:bundle", {"symbol": symbol})
    cached_payload, cached_status = client.cache_entry(key)
    if cached_status is not None:
        client.bump_cache_hit("yahoo")
        payload = cached_payload or {}
        rows = sum(
            1
            for key_name in ("ratiosTtm", "keyMetricsTtm", "financialGrowth", "earnings")
            if as_list(payload.get(key_name))
        )
        errors = []
        if isinstance(payload, dict) and payload.get("error"):
            errors.append(str(payload.get("error")))
        errors.extend(str(item) for item in (payload.get("componentErrors") or []) if item)
        error = "; ".join(errors) or None
        if cached_status.startswith("stale_"):
            status = "stale_cache_error" if error else "stale_cache"
        else:
            status = "cache_error" if error and not rows else ("cache_partial" if error else "cache")
        return payload, {
            "yahoo": source_status(
                {"status": status, "rows": rows,
                 "ok": rows > 0 and not status.startswith("stale_") and "error" not in status,
                 "error": error},
                "yahoo",
            )
        }
    if client.no_fetch:
        return {}, {"yahoo": source_status({"status": "missing_cache", "rows": 0, "ok": False, "error": None}, "yahoo")}
    try:
        import yfinance as yf

        cache_dir = ROOT / "output" / "yfinance_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        if hasattr(yf, "set_tz_cache_location"):
            yf.set_tz_cache_location(str(cache_dir))

        ticker = yf.Ticker(symbol)
        component_errors = []
        def component(name, getter, default):
            try:
                return getter()
            except Exception as exc:
                detail = f"{name} {type(exc).__name__}: {str(exc)[:120]}"
                component_errors.append(detail)
                client.errors.append(f"yahoo {symbol}: {detail}")
                return default
        try:
            info = ticker.info or {}
        except Exception:
            info = {}
        try:
            fast = dict(ticker.fast_info or {})
        except Exception:
            fast = {}
        payload = build_yahoo_bundle(
            symbol,
            info,
            fast,
            component("quarterly_income_stmt", lambda: ticker.quarterly_income_stmt, None),
            component("quarterly_balance_sheet", lambda: ticker.quarterly_balance_sheet, None),
            component("quarterly_cashflow", lambda: ticker.quarterly_cashflow, None),
            component("earnings_dates", lambda: ticker.earnings_dates, None),
        )
        payload["componentErrors"] = component_errors
    except Exception as exc:
        client.bump_call("yahoo")
        detail = f"{type(exc).__name__}: {str(exc)[:140]}"
        client.errors.append(f"yahoo {symbol}: {detail}")
        payload = {"error": detail}
        client.put_cache(key, "yahoo:bundle", {"symbol": symbol}, "error", payload)
        return {}, {"yahoo": source_status({"status": "error", "rows": 0, "ok": False, "error": detail}, "yahoo")}
    client.bump_call("yahoo")
    client.put_cache(key, "yahoo:bundle", {"symbol": symbol}, "fresh", payload)
    rows = sum(1 for key_name in ("ratiosTtm", "keyMetricsTtm", "financialGrowth", "earnings") if as_list(payload.get(key_name)))
    component_error = "; ".join(payload.get("componentErrors") or []) or None
    return payload, {"yahoo": source_status({"status": "fresh_partial" if component_error else "fresh",
                                               "rows": rows, "ok": rows > 0,
                                               "error": component_error}, "yahoo")}


def first_fact_values(facts: dict, tags: Iterable[str], unit: str = "USD") -> List[dict]:
    out: List[dict] = []
    for tag in tags:
        item = facts.get(tag)
        units = ((item or {}).get("units") or {})
        rows = units.get(unit) or units.get("USD/shares") or []
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict) and num(row.get("val")) is not None:
                    out.append(dict(row, tag=tag))
    return out


def latest_fact(facts: dict, tags: Iterable[str], forms: Tuple[str, ...] = ("10-Q", "10-K", "20-F", "40-F")) -> Optional[float]:
    rows = [r for r in first_fact_values(facts, tags) if r.get("form") in forms and parse_date(r.get("end"))]
    if not rows:
        return None
    rows.sort(key=lambda r: (parse_date(r.get("end")) or dt.date.min, parse_date(r.get("filed")) or dt.date.min), reverse=True)
    return num(rows[0].get("val"))


def latest_annual_fact_row(facts: dict, tags: Iterable[str]) -> Optional[dict]:
    """Return the latest coherent fiscal-year duration fact.

    SEC companyfacts mixes quarter-only, year-to-date, and annual durations in
    the same tag.  Selecting merely by latest end date can therefore label a
    Q1 or six-month value as TTM.  The SEC fallback deliberately uses the most
    recent filed fiscal year until a filing-vintage TTM assembler exists.
    """
    rows = [
        row
        for row in first_fact_values(facts, tags)
        if row.get("form") in ("10-K", "20-F", "40-F")
        and (row.get("fp") == "FY" or str(row.get("frame", "")).startswith("CY"))
        and parse_date(row.get("end"))
    ]
    if not rows:
        return None
    rows.sort(
        key=lambda row: (
            parse_date(row.get("end")) or dt.date.min,
            parse_date(row.get("filed")) or dt.date.min,
        ),
        reverse=True,
    )
    return rows[0]


def latest_annual_fact(facts: dict, tags: Iterable[str]) -> Optional[float]:
    row = latest_annual_fact_row(facts, tags)
    return num(row.get("val")) if row else None


def annual_fact_at_end(facts: dict, tags: Iterable[str], period_end: Optional[str]) -> Optional[float]:
    if not period_end:
        return latest_annual_fact(facts, tags)
    rows = [
        row for row in first_fact_values(facts, tags)
        if row.get("form") in ("10-K", "20-F", "40-F")
        and (row.get("fp") == "FY" or str(row.get("frame", "")).startswith("CY"))
        and row.get("end") == period_end
    ]
    rows.sort(key=lambda row: parse_date(row.get("filed")) or dt.date.min, reverse=True)
    return num(rows[0].get("val")) if rows else None


def instant_fact_at_end(facts: dict, tags: Iterable[str], period_end: Optional[str]) -> Optional[float]:
    if not period_end:
        return latest_fact(facts, tags)
    rows = [
        row for row in first_fact_values(facts, tags)
        if row.get("form") in ("10-K", "20-F", "40-F") and row.get("end") == period_end
    ]
    rows.sort(key=lambda row: parse_date(row.get("filed")) or dt.date.min, reverse=True)
    return num(rows[0].get("val")) if rows else None


def preferred_instant_fact_at_end(
    facts: dict,
    tags: Iterable[str],
    period_end: Optional[str],
) -> Optional[float]:
    """Choose the first available taxonomy concept at one exact period end."""
    if not period_end:
        return None
    for tag in tags:
        value = instant_fact_at_end(facts, (tag,), period_end)
        if value is not None:
            return value
    return None


def sec_comprehensive_debt_at_end(
    facts: dict,
    period_end: Optional[str],
) -> Tuple[Optional[float], Optional[str]]:
    """Return reported total debt or same-period current + noncurrent debt."""
    reported_total = preferred_instant_fact_at_end(
        facts,
        (
            "DebtAndFinanceLeaseObligations",
            "DebtAndCapitalLeaseObligations",
            "LongTermDebtAndFinanceLeaseObligations",
            "LongTermDebtAndCapitalLeaseObligations",
        ),
        period_end,
    )
    if reported_total is not None:
        return reported_total, "reported_total_debt"

    current = preferred_instant_fact_at_end(
        facts,
        (
            "DebtCurrent",
            "LongTermDebtAndFinanceLeaseObligationsCurrent",
            "LongTermDebtAndCapitalLeaseObligationsCurrent",
            "LongTermDebtCurrent",
        ),
        period_end,
    )
    noncurrent = preferred_instant_fact_at_end(
        facts,
        (
            "LongTermDebtAndFinanceLeaseObligationsNoncurrent",
            "LongTermDebtAndCapitalLeaseObligationsNoncurrent",
            "LongTermDebtNoncurrent",
        ),
        period_end,
    )
    if current is None or noncurrent is None:
        return None, None
    return current + noncurrent, "same_period_current_plus_noncurrent_debt"


def annual_fact_pair_growth(facts: dict, tags: Iterable[str]) -> Optional[float]:
    rows = [r for r in first_fact_values(facts, tags) if r.get("form") in ("10-K", "20-F", "40-F") and (r.get("fp") == "FY" or str(r.get("frame", "")).startswith("CY")) and parse_date(r.get("end"))]
    rows.sort(key=lambda r: (parse_date(r.get("end")) or dt.date.min, parse_date(r.get("filed")) or dt.date.min), reverse=True)
    vals = []
    seen = set()
    for row in rows:
        end = row.get("end")
        if end in seen:
            continue
        seen.add(end)
        vals.append(num(row.get("val")))
        if len(vals) == 2:
            break
    if len(vals) < 2 or vals[1] is None or abs(vals[1]) < 1e-9 or vals[0] is None:
        return None
    return vals[0] / vals[1] - 1.0


def build_sec_bundle(symbol: str, ticker_meta: dict, companyfacts: dict, market_cap: Optional[float] = None) -> dict:
    us_gaap = ((companyfacts.get("facts") or {}).get("us-gaap") or {})
    revenue_tags = ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet")
    revenue_row = latest_annual_fact_row(us_gaap, revenue_tags)
    period_end = str(revenue_row.get("end")) if revenue_row else None
    revenue = num(revenue_row.get("val")) if revenue_row else None
    gross_profit = annual_fact_at_end(us_gaap, ("GrossProfit",), period_end)
    operating_income = annual_fact_at_end(us_gaap, ("OperatingIncomeLoss",), period_end)
    net_income = annual_fact_at_end(us_gaap, ("NetIncomeLoss", "ProfitLoss"), period_end)
    assets = instant_fact_at_end(us_gaap, ("Assets",), period_end)
    current_assets = instant_fact_at_end(us_gaap, ("AssetsCurrent",), period_end)
    liabilities = instant_fact_at_end(us_gaap, ("Liabilities",), period_end)
    current_liabilities = instant_fact_at_end(us_gaap, ("LiabilitiesCurrent",), period_end)
    equity = instant_fact_at_end(us_gaap, ("StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"), period_end)
    debt_total, debt_definition = sec_comprehensive_debt_at_end(us_gaap, period_end)
    ocf = annual_fact_at_end(us_gaap, ("NetCashProvidedByUsedInOperatingActivities",), period_end)
    capex = annual_fact_at_end(us_gaap, ("PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets"), period_end)
    # SEC PaymentsToAcquire* facts are normally reported as positive cash
    # outflows, so free cash flow is OCF minus the absolute capex amount.
    fcf = (ocf - abs(capex)) if ocf is not None and capex is not None else None

    ratios = {
        "symbol": symbol,
        "periodBasis": "latest_fiscal_year_fallback",
        "periodEnd": period_end,
        "grossProfitMarginTTM": safe_div(gross_profit, revenue),
        "operatingProfitMarginTTM": safe_div(operating_income, revenue),
        "netProfitMarginTTM": safe_div(net_income, revenue),
        "currentRatioTTM": safe_div(current_assets, current_liabilities),
        "debtToEquityRatioTTM": safe_div(debt_total, equity),
        "debtToAssetsRatioTTM": safe_div(debt_total, assets),
        "totalDebtDefinition": debt_definition,
        "operatingCashFlowSalesRatioTTM": safe_div(ocf, revenue),
        "capitalExpenditureCoverageRatioTTM": safe_div(ocf, abs(capex) if capex is not None else None),
        "priceToFreeCashFlowRatioTTM": safe_div(market_cap, fcf),
    }
    metrics = {
        "symbol": symbol,
        "periodBasis": "latest_fiscal_year_fallback",
        "periodEnd": period_end,
        "marketCap": market_cap,
        "currentRatioTTM": ratios["currentRatioTTM"],
        "returnOnAssetsTTM": safe_div(net_income, assets),
        "returnOnEquityTTM": safe_div(net_income, equity),
        "freeCashFlowYieldTTM": safe_div(fcf, market_cap),
        "incomeQualityTTM": safe_div(ocf, net_income),
    }
    growth = {
        "symbol": symbol,
        "date": None,
        "revenueGrowth": annual_fact_pair_growth(us_gaap, ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet")),
        "grossProfitGrowth": annual_fact_pair_growth(us_gaap, ("GrossProfit",)),
        "operatingIncomeGrowth": annual_fact_pair_growth(us_gaap, ("OperatingIncomeLoss",)),
        "netIncomeGrowth": annual_fact_pair_growth(us_gaap, ("NetIncomeLoss", "ProfitLoss")),
        "operatingCashFlowGrowth": annual_fact_pair_growth(us_gaap, ("NetCashProvidedByUsedInOperatingActivities",)),
    }
    profile = {
        "symbol": symbol,
        "companyName": companyfacts.get("entityName") or ticker_meta.get("title"),
        "cik": str(companyfacts.get("cik") or ticker_meta.get("cik_str") or "").zfill(10) if (companyfacts.get("cik") or ticker_meta.get("cik_str")) else None,
    }
    metadata_keys = {
        "symbol", "periodBasis", "periodEnd", "totalDebtDefinition",
        "returnOnInvestedCapitalMethod",
    }
    has_ratios = any(value is not None for key, value in ratios.items() if key not in metadata_keys)
    has_metrics = any(value is not None for key, value in metrics.items() if key not in metadata_keys)
    return {
        "profile": [json_safe(profile)] if profile.get("companyName") else [],
        "ratiosTtm": [json_safe(ratios)] if has_ratios else [],
        "keyMetricsTtm": [json_safe(metrics)] if has_metrics else [],
        "financialGrowth": [json_safe(growth)] if any(v is not None for k, v in growth.items() if k not in ("symbol", "date")) else [],
    }


def fetch_sec_bundle(client: FmpClient, symbol: str, sec_user_agent: Optional[str], market_cap: Optional[float], enabled: bool = True) -> Tuple[dict, Dict[str, dict]]:
    if not enabled:
        return {}, {"sec": source_status({"status": "disabled", "rows": 0, "ok": False, "error": None}, "sec")}
    if not sec_user_agent:
        return {}, {"sec": source_status({"status": "missing_user_agent", "rows": 0, "ok": False, "error": "SEC_USER_AGENT not configured"}, "sec")}
    headers = {"User-Agent": sec_user_agent, "Accept": "application/json"}
    tickers, t_status = client.get_json_url("sec", "company_tickers", SEC_TICKERS_URL, headers=headers)
    if not isinstance(tickers, dict):
        return {}, {"sec": source_status(endpoint_status(tickers, t_status), "sec")}
    meta = None
    for row in tickers.values():
        if isinstance(row, dict) and str(row.get("ticker") or "").upper() == symbol.upper():
            meta = row
            break
    if not meta:
        return {}, {"sec": source_status({"status": "not_sec_registered", "rows": 0, "ok": False, "error": None}, "sec")}
    cik = str(meta.get("cik_str") or "").zfill(10)
    facts_url = f"{SEC_BASE}/api/xbrl/companyfacts/CIK{cik}.json"
    facts, f_status = client.get_json_url("sec", f"companyfacts:{symbol}", facts_url, headers=headers)
    if not isinstance(facts, dict) or "facts" not in facts:
        return {}, {"sec": source_status(endpoint_status(facts, f_status), "sec")}
    bundle = build_sec_bundle(symbol, meta, facts, market_cap=market_cap)
    rows = sum(1 for key_name in ("profile", "ratiosTtm", "keyMetricsTtm", "financialGrowth") if as_list(bundle.get(key_name)))
    normalized_status = (
        f_status if ("error" in f_status or f_status.startswith("stale_"))
        else ("cache" if f_status == "cache" else "fresh")
    )
    return bundle, {"sec": source_status({
        "status": normalized_status,
        "rows": rows,
        "ok": rows > 0 and not normalized_status.startswith("stale_") and "error" not in normalized_status,
        "error": None,
    }, "sec")}


def merge_row_lists(*rows_by_source: List[dict]) -> List[dict]:
    """Merge one current row per source, preserving priority and null fallback.

    Sources are passed highest priority first.  A higher-priority null does not
    erase a usable lower-priority value, while numeric zero remains a real
    observation and does override the fallback.
    """
    merged: dict = {}
    found = False
    for rows in reversed(rows_by_source):
        if not rows:
            continue
        found = True
        for key, value in rows[0].items():
            if value is not None:
                merged[key] = value
    return [merged] if found else []


def merge_rows_by_date(*rows_by_source: List[dict]) -> List[dict]:
    """Null-aware source-priority merge for dated event/period rows."""
    merged: Dict[str, dict] = {}
    for rows in reversed(rows_by_source):
        for row in rows:
            if not isinstance(row, dict):
                continue
            day = parse_date(row.get("date"))
            key = day.isoformat() if day else "__undated__"
            target = merged.setdefault(key, {})
            for field, value in row.items():
                if value is not None:
                    target[field] = value
    result = list(merged.values())
    result.sort(key=lambda row: parse_date(row.get("date")) or dt.date.min, reverse=True)
    return result


def merge_bundles(fmp: dict, yahoo: dict, sec: dict) -> dict:
    out = {
        "profile": merge_row_lists(as_list(fmp.get("profile")), as_list(yahoo.get("profile")), as_list(sec.get("profile"))),
        "financialScores": as_list(fmp.get("financialScores")),
        "ratiosTtm": merge_row_lists(as_list(fmp.get("ratiosTtm")), as_list(yahoo.get("ratiosTtm")), as_list(sec.get("ratiosTtm"))),
        "keyMetricsTtm": merge_row_lists(as_list(fmp.get("keyMetricsTtm")), as_list(yahoo.get("keyMetricsTtm")), as_list(sec.get("keyMetricsTtm"))),
    }
    out["financialGrowth"] = merge_rows_by_date(
        as_list(fmp.get("financialGrowth")),
        as_list(yahoo.get("financialGrowth")),
        as_list(sec.get("financialGrowth")),
    )
    out["earnings"] = merge_rows_by_date(
        as_list(fmp.get("earnings")),
        as_list(yahoo.get("earnings")),
    )
    return out


def avg_growth(rows: List[dict], *keys: str) -> Optional[float]:
    vals: List[float] = []
    for row in rows[:4]:
        for key in keys:
            value = pct_ratio(row.get(key), unit="decimal")
            if value is not None:
                vals.append(value)
                break
    if not vals:
        return None
    return sum(vals) / len(vals)


def compute_components(bundle: dict, statuses: dict) -> Tuple[dict, dict]:
    profile = latest_by_date(as_list(bundle.get("profile"))) or {}
    score_row = latest_by_date(as_list(bundle.get("financialScores"))) or {}
    ratios = latest_by_date(as_list(bundle.get("ratiosTtm"))) or {}
    metrics = latest_by_date(as_list(bundle.get("keyMetricsTtm"))) or {}
    growth_rows = sorted(as_list(bundle.get("financialGrowth")), key=lambda r: parse_date(r.get("date")) or dt.date.min, reverse=True)

    gross_margin = first_num(ratios, "grossProfitMarginTTM", ratio_unit="decimal")
    operating_margin = first_num(ratios, "operatingProfitMarginTTM", "ebitMarginTTM", ratio_unit="decimal")
    net_margin = first_num(ratios, "netProfitMarginTTM", "bottomLineProfitMarginTTM", ratio_unit="decimal")
    roe = first_num(metrics, "returnOnEquityTTM", ratio_unit="decimal")
    roa = first_num(metrics, "returnOnAssetsTTM", ratio_unit="decimal")
    roic = first_num(metrics, "returnOnInvestedCapitalTTM", ratio_unit="decimal")

    current_ratio = first_not_none(first_num(ratios, "currentRatioTTM"), first_num(metrics, "currentRatioTTM"))
    quick_ratio = first_num(ratios, "quickRatioTTM")
    debt_equity = first_num(ratios, "debtToEquityRatioTTM")
    interest_coverage = first_num(ratios, "interestCoverageRatioTTM")
    net_debt_ebitda = first_num(metrics, "netDebtToEBITDATTM")
    altman = first_num(score_row, "altmanZScore")
    piotroski = first_num(score_row, "piotroskiScore")

    ocf_margin = first_num(ratios, "operatingCashFlowSalesRatioTTM", ratio_unit="decimal")
    fcf_conversion = first_num(ratios, "freeCashFlowOperatingCashFlowRatioTTM", ratio_unit="decimal")
    capex_coverage = first_num(ratios, "capitalExpenditureCoverageRatioTTM")
    fcf_yield = first_num(metrics, "freeCashFlowYieldTTM", ratio_unit="decimal")
    income_quality = first_num(metrics, "incomeQualityTTM")
    cash_flow_debt = first_num(ratios, "operatingCashFlowCoverageRatioTTM", "cashFlowToDebtRatioTTM")

    revenue_growth = avg_growth(growth_rows, "revenueGrowth")
    net_income_growth = avg_growth(growth_rows, "netIncomeGrowth")
    operating_income_growth = avg_growth(growth_rows, "operatingIncomeGrowth", "ebitgrowth")
    ocf_growth = avg_growth(growth_rows, "operatingCashFlowGrowth")
    fcf_growth = avg_growth(growth_rows, "freeCashFlowGrowth")

    pe = first_num(ratios, "priceToEarningsRatioTTM")
    peg = first_num(ratios, "priceToEarningsGrowthRatioTTM", "forwardPriceToEarningsGrowthRatioTTM")
    ps = first_num(ratios, "priceToSalesRatioTTM")
    pfcf = first_num(ratios, "priceToFreeCashFlowRatioTTM")
    ev_ebitda = first_not_none(first_num(metrics, "evToEBITDATTM"), first_num(ratios, "enterpriseValueMultipleTTM"))

    endpoint_ok = sum(1 for status in statuses.values() if status.get("ok"))
    source_ok = {status.get("source") for status in statuses.values() if status.get("ok") and status.get("source")}
    raw_metric_count = sum(
        1
        for value in [
            gross_margin,
            operating_margin,
            net_margin,
            roe,
            roa,
            current_ratio,
            debt_equity,
            altman,
            piotroski,
            ocf_margin,
            fcf_yield,
            revenue_growth,
            net_income_growth,
            pe,
            ev_ebitda,
        ]
        if value is not None
    )
    metric_coverage = min(raw_metric_count, 15) / 15.0
    endpoint_coverage = endpoint_ok / max(len(statuses), 1)
    source_coverage = min(len(source_ok), 3) / 3.0
    data_confidence = clamp(metric_coverage * 65.0 + endpoint_coverage * 20.0 + source_coverage * 15.0)

    profitability = weighted_average(
        [
            (high_score(gross_margin, 0.18, 0.65), 0.18),
            (high_score(operating_margin, -0.02, 0.35), 0.22),
            (high_score(net_margin, -0.05, 0.25), 0.22),
            (high_score(roe, 0.00, 0.35), 0.18),
            (high_score(roa, 0.00, 0.18), 0.12),
            (high_score(roic, 0.00, 0.25), 0.08),
        ],
        fallback=45.0,
    )
    balance = weighted_average(
        [
            (high_score(current_ratio, 0.70, 1.80), 0.16),
            (high_score(quick_ratio, 0.45, 1.30), 0.12),
            (low_score(debt_equity, 0.20, 2.50), 0.17),
            (high_score(interest_coverage, 1.0, 12.0), 0.14),
            (low_score(net_debt_ebitda, 0.0, 4.0), 0.10),
            (high_score(altman, 1.8, 5.0), 0.18),
            ((piotroski / 9.0 * 100.0) if piotroski is not None else None, 0.13),
        ],
        fallback=45.0,
    )
    cash_flow = weighted_average(
        [
            (high_score(ocf_margin, 0.00, 0.30), 0.22),
            (high_score(fcf_conversion, 0.25, 0.90), 0.22),
            (high_score(capex_coverage, 1.0, 8.0), 0.14),
            (high_score(fcf_yield, -0.02, 0.06), 0.18),
            (high_score(income_quality, 0.5, 1.6), 0.14),
            (high_score(cash_flow_debt, 0.05, 0.65), 0.10),
        ],
        fallback=45.0,
    )
    growth = weighted_average(
        [
            (high_score(revenue_growth, -0.12, 0.28), 0.25),
            (high_score(net_income_growth, -0.30, 0.35), 0.22),
            (high_score(operating_income_growth, -0.25, 0.35), 0.18),
            (high_score(ocf_growth, -0.25, 0.30), 0.18),
            (high_score(fcf_growth, -0.30, 0.30), 0.17),
        ],
        fallback=45.0,
    )
    valuation = weighted_average(
        [
            (banded_low_score(pe, 30.0, 85.0), 0.26),
            (banded_low_score(ev_ebitda, 22.0, 60.0), 0.24),
            (banded_low_score(ps, 9.0, 32.0), 0.18),
            (banded_low_score(pfcf, 35.0, 95.0), 0.17),
            (banded_low_score(peg, 2.0, 6.0), 0.15),
        ],
        fallback=50.0,
    )

    components = {
        "profitability": rn(profitability, 1),
        "balanceSheet": rn(balance, 1),
        "cashFlow": rn(cash_flow, 1),
        "growth": rn(growth, 1),
        "valuation": rn(valuation, 1),
        "dataConfidence": rn(data_confidence, 1),
    }
    financial_status = weighted_average(
        [
            (profitability, COMPONENT_WEIGHTS["profitability"]),
            (balance, COMPONENT_WEIGHTS["balanceSheet"]),
            (cash_flow, COMPONENT_WEIGHTS["cashFlow"]),
            (growth, COMPONENT_WEIGHTS["growth"]),
            (valuation, COMPONENT_WEIGHTS["valuation"]),
            (data_confidence, COMPONENT_WEIGHTS["dataConfidence"]),
        ],
        fallback=45.0,
    )
    metrics_out = {
        "companyName": first_str(profile, "companyName", "companyNameFull", "name"),
        "sector": first_str(profile, "sector"),
        "industry": first_str(profile, "industry"),
        "marketCap": rn(first_not_none(first_num(profile, "marketCap"), first_num(metrics, "marketCap")), 0),
        "beta": rn(first_num(profile, "beta"), 2),
        "grossMarginTTM": rn(gross_margin, 4),
        "operatingMarginTTM": rn(operating_margin, 4),
        "netMarginTTM": rn(net_margin, 4),
        "returnOnEquityTTM": rn(roe, 4),
        "returnOnAssetsTTM": rn(roa, 4),
        "returnOnInvestedCapitalTTM": rn(roic, 4),
        "currentRatioTTM": rn(current_ratio, 3),
        "quickRatioTTM": rn(quick_ratio, 3),
        "debtToEquityTTM": rn(debt_equity, 3),
        "interestCoverageRatioTTM": rn(interest_coverage, 3),
        "netDebtToEBITDATTM": rn(net_debt_ebitda, 3),
        "altmanZScore": rn(altman, 3),
        "piotroskiScore": rn(piotroski, 1),
        "operatingCashFlowSalesRatioTTM": rn(ocf_margin, 4),
        "freeCashFlowOperatingCashFlowRatioTTM": rn(fcf_conversion, 4),
        "freeCashFlowYieldTTM": rn(fcf_yield, 4),
        "revenueGrowthAvg": rn(revenue_growth, 4),
        "netIncomeGrowthAvg": rn(net_income_growth, 4),
        "operatingIncomeGrowthAvg": rn(operating_income_growth, 4),
        "operatingCashFlowGrowthAvg": rn(ocf_growth, 4),
        "freeCashFlowGrowthAvg": rn(fcf_growth, 4),
        "priceToEarningsTTM": rn(pe, 2),
        "evToEBITDATTM": rn(ev_ebitda, 2),
        "priceToSalesTTM": rn(ps, 2),
        "priceToFreeCashFlowTTM": rn(pfcf, 2),
        "pegRatioTTM": rn(peg, 2),
        "sourceFamilies": sorted(source_ok),
    }
    return (
        {
            "financialStatusScore": rn(financial_status, 1),
            "components": components,
            "metrics": metrics_out,
        },
        {
            "profile": profile,
            "financialScores": score_row,
            "ratiosTtm": ratios,
            "keyMetricsTtm": metrics,
            "financialGrowth": growth_rows,
        },
    )


def compute_earnings(bundle: dict, calendar_row: Optional[dict], today: dt.date) -> dict:
    rows = sorted(as_list(bundle.get("earnings")), key=lambda r: parse_date(r.get("date")) or dt.date.min, reverse=True)
    past = []
    future = []
    for row in rows:
        day = parse_date(row.get("date"))
        if not day:
            continue
        has_actual = (num(row.get("epsActual")) is not None
                      or num(row.get("revenueActual")) is not None)
        if day < today or (day == today and has_actual):
            past.append(row)
        else:
            future.append(row)

    if calendar_row:
        cal_day = parse_date(calendar_row.get("date"))
        if cal_day and cal_day >= today:
            future.insert(0, calendar_row)
            future.sort(key=lambda r: parse_date(r.get("date")) or dt.date.max)
    else:
        future.sort(key=lambda r: parse_date(r.get("date")) or dt.date.max)

    last_rows = [r for r in past if num(r.get("epsActual")) is not None and num(r.get("epsEstimated")) is not None][:8]
    revenue_rows = [
        r
        for r in past
        if num(r.get("revenueActual")) is not None and num(r.get("revenueEstimated")) is not None
    ][:8]
    beats = []
    surprises = []
    rev_beats = []
    for row in last_rows:
        actual = num(row.get("epsActual"))
        est = num(row.get("epsEstimated"))
        if actual is not None and est is not None:
            beats.append(1 if actual >= est else 0)
            denom = abs(est) if abs(est) > 1e-9 else max(abs(actual), 1.0)
            surprises.append((actual - est) / denom * 100.0)
    for row in revenue_rows:
        rev_actual = num(row.get("revenueActual"))
        rev_est = num(row.get("revenueEstimated"))
        rev_beats.append(1 if rev_actual >= rev_est else 0)

    beat_rate = sum(beats) / len(beats) if beats else None
    rev_beat_rate = sum(rev_beats) / len(rev_beats) if rev_beats else None
    latest_surprise = surprises[0] if surprises else None
    avg_surprise = sum(surprises) / len(surprises) if surprises else None

    earnings_score = weighted_average(
        [
            ((beat_rate * 100.0) if beat_rate is not None else None, 0.36),
            ((rev_beat_rate * 100.0) if rev_beat_rate is not None else None, 0.18),
            (high_score(latest_surprise, -10.0, 10.0), 0.23),
            (high_score(avg_surprise, -8.0, 8.0), 0.17),
            (min(len(last_rows), 8) / 8.0 * 100.0 if last_rows else None, 0.06),
        ],
        fallback=50.0,
    )
    next_row = future[0] if future else None
    next_day = parse_date(next_row.get("date")) if next_row else None
    days_to_next = (next_day - today).days if next_day else None

    return {
        "earningsReportScore": rn(earnings_score, 1),
        "latestDate": past[0].get("date") if past else None,
        "nextDate": next_day.isoformat() if next_day else None,
        "nextTime": first_str(next_row, "time", "estimatedTime", "reportTime") if next_row else None,
        "nextConfirmed": bool(next_row.get("confirmed")) if isinstance(next_row, dict) and "confirmed" in next_row else None,
        "daysToNext": days_to_next,
        "nextEpsEstimate": rn(first_num(next_row, "epsEstimated", "epsEstimate", "estimatedEps"), 4) if next_row else None,
        "nextRevenueEstimate": rn(first_num(next_row, "revenueEstimated", "revenueEstimate"), 0) if next_row else None,
        "lastSurprisePct": rn(latest_surprise, 2),
        "avgSurprisePct": rn(avg_surprise, 2),
        "epsBeatRate": rn(beat_rate * 100.0, 1) if beat_rate is not None else None,
        "revenueBeatRate": rn(rev_beat_rate * 100.0, 1) if rev_beat_rate is not None else None,
        "rowsUsed": len(last_rows),
        "revenueRowsUsed": len(revenue_rows),
    }


def days_risk(days: Optional[int]) -> float:
    if days is None:
        return 55.0
    if days < 0:
        return 65.0
    if days <= 3:
        return 95.0
    if days <= 7:
        return 88.0
    if days <= 14:
        return 78.0
    if days <= 30:
        return 62.0
    if days <= 60:
        return 48.0
    if days <= 120:
        return 38.0
    return 32.0


def assign_gate(final_score: float, financial: float, earnings: float, event_risk: float, data_conf: float, metrics: dict, earnings_meta: dict) -> Tuple[str, List[dict]]:
    reasons: List[dict] = []
    days = earnings_meta.get("daysToNext")
    piotroski = metrics.get("piotroskiScore")
    altman = metrics.get("altmanZScore")

    if data_conf < 35:
        reasons.append({"rule": "DATA_COVERAGE", "detail": f"Multi-source usable data confidence is {data_conf:.0f}/100."})
        return "DATA_REVIEW", reasons
    if financial < 50:
        reasons.append({"rule": "FINANCIAL_STATUS", "detail": f"Financial status score is {financial:.0f}/100."})
    if piotroski is not None and piotroski <= 3:
        reasons.append({"rule": "PIOTROSKI", "detail": f"Piotroski score is {piotroski}."})
    if altman is not None and altman < 1.8:
        reasons.append({"rule": "ALTMAN_Z", "detail": f"Altman Z-score is {altman}."})
    if reasons:
        return "FINANCIAL_REVIEW", reasons
    if days is None or event_risk >= 72 or days <= 14:
        detail = "next earnings date is unknown" if days is None else f"next earnings is in {days} day(s)"
        reasons.append({"rule": "EARNINGS_WINDOW", "detail": f"{detail}; event-risk index {event_risk:.0f}/100."})
        return "EARNINGS_WATCH", reasons
    if final_score >= 80 and financial >= 75 and event_risk < 65:
        reasons.append({"rule": "QUALITY_GATE", "detail": "Financial status is strong and near-term earnings event risk is contained."})
        return "STRONG_FINANCIALS", reasons
    if final_score >= 65:
        reasons.append({"rule": "HEALTHY_BASE", "detail": "Composite lens score is above the healthy-watch threshold."})
        return "HEALTHY_WATCH", reasons
    if earnings < 45:
        reasons.append({"rule": "EARNINGS_HISTORY", "detail": f"Earnings report score is {earnings:.0f}/100."})
    else:
        reasons.append({"rule": "REVIEW_QUEUE", "detail": "Composite score is below the healthy-watch threshold."})
    return "REVIEW_QUEUE", reasons


def score_company(stock: dict, bundle: dict, statuses: dict, calendar_row: Optional[dict], today: dt.date) -> dict:
    component_doc, raw = compute_components(bundle, statuses)
    earnings_meta = compute_earnings(bundle, calendar_row, today)
    financial = float(first_not_none(component_doc["financialStatusScore"], 45.0))
    earnings_score = float(first_not_none(earnings_meta["earningsReportScore"], 50.0))
    data_conf = float(first_not_none(component_doc["components"]["dataConfidence"], 45.0))
    event_risk = days_risk(earnings_meta.get("daysToNext"))

    valuation_score = first_not_none(component_doc["components"].get("valuation"), 50.0)
    beta = component_doc["metrics"].get("beta")
    if valuation_score < 45:
        event_risk += 8.0
    if earnings_score < 45:
        event_risk += 8.0
    if financial < 50:
        event_risk += 7.0
    if beta is not None and beta >= 1.6:
        event_risk += 6.0
    event_risk = clamp(event_risk)

    final_score = weighted_average(
        [
            (financial, FINAL_WEIGHTS["financialStatus"]),
            (earnings_score, FINAL_WEIGHTS["earningsReport"]),
            (100.0 - event_risk, FINAL_WEIGHTS["nextEarningsSafety"]),
            (data_conf, FINAL_WEIGHTS["dataConfidence"]),
        ],
        fallback=50.0,
    )
    gate, reasons = assign_gate(final_score, financial, earnings_score, event_risk, data_conf, component_doc["metrics"], earnings_meta)
    stale_sources = sorted(
        key for key, status in statuses.items()
        if str(status.get("status") or "").startswith("stale_")
    )
    if stale_sources:
        gate = "DATA_REVIEW"
        reasons = [{
            "rule": "STALE_SOURCE_CACHE",
            "detail": "Offline cache is past its configured TTL: " + ", ".join(stale_sources),
        }] + reasons
    profile_name = component_doc["metrics"].get("companyName")
    source_status = {k: statuses.get(k, {}) for k in sorted(statuses)}
    source_families = sorted({v.get("source") for v in source_status.values() if v.get("ok") and v.get("source")})

    return {
        "ticker": stock.get("sym"),
        "name": profile_name or stock.get("name"),
        "portfolioName": stock.get("name"),
        "portfolioWeightPct": rn(num(stock.get("portfolioWeightPct")), 3),
        "value": rn(num(stock.get("value")), 2),
        "shares": rn(num(stock.get("shares")), 4),
        "assetClass": stock.get("assetClass"),
        "theme": stock.get("theme"),
        "finalScore": rn(final_score, 1),
        "financialStatusScore": rn(financial, 1),
        "earningsReportScore": rn(earnings_score, 1),
        "nextEarningsRiskIndex": rn(event_risk, 1),
        "dataConfidenceScore": rn(data_conf, 1),
        "gate": gate,
        "gateReasons": reasons,
        "components": component_doc["components"],
        "metrics": component_doc["metrics"],
        "earnings": earnings_meta,
        "sourceFamilies": source_families,
        "sourceStatus": source_status,
        "staleSourceCaches": stale_sources,
    }


def summarize(scores: List[dict], omitted: List[dict], calendar_status: dict, client: FmpClient) -> dict:
    scores_sorted = sorted(scores, key=lambda r: (r.get("finalScore") is not None, r.get("finalScore") or -1), reverse=True)
    gate_counts: Dict[str, int] = {}
    for row in scores:
        gate_counts[row["gate"]] = gate_counts.get(row["gate"], 0) + 1
    upcoming = [
        {
            "ticker": r["ticker"],
            "daysToNext": r["earnings"].get("daysToNext"),
            "nextDate": r["earnings"].get("nextDate"),
            "riskIndex": r.get("nextEarningsRiskIndex"),
            "gate": r.get("gate"),
        }
        for r in scores
        if r.get("earnings", {}).get("daysToNext") is not None
    ]
    upcoming.sort(key=lambda r: r["daysToNext"])
    risk_queue = sorted(scores, key=lambda r: (r.get("nextEarningsRiskIndex") or 0, -(r.get("financialStatusScore") or 0)), reverse=True)
    avg = lambda key: rn(sum(float(r.get(key) or 0) for r in scores) / len(scores), 1) if scores else None
    return {
        "leaders": [{"ticker": r["ticker"], "score": r["finalScore"], "gate": r["gate"]} for r in scores_sorted[:6]],
        "riskQueue": [{"ticker": r["ticker"], "riskIndex": r["nextEarningsRiskIndex"], "gate": r["gate"]} for r in risk_queue[:8]],
        "upcomingEarnings": upcoming[:12],
        "gateCounts": gate_counts,
        "avgFinalScore": avg("finalScore"),
        "avgFinancialStatusScore": avg("financialStatusScore"),
        "avgEarningsReportScore": avg("earningsReportScore"),
        "avgNextEarningsRiskIndex": avg("nextEarningsRiskIndex"),
        "calendarStatus": calendar_status,
        "apiCalls": client.calls,
        "cacheHits": client.cache_hits,
        "sourceCalls": dict(sorted(client.source_calls.items())),
        "sourceCacheHits": dict(sorted(client.source_cache_hits.items())),
        "sourceFamilyCounts": {
            source: sum(1 for r in scores if source in (r.get("sourceFamilies") or []))
            for source in sorted({s for r in scores for s in (r.get("sourceFamilies") or [])})
        },
        "apiErrors": client.errors[:12],
        "omittedTickers": [x["ticker"] for x in omitted],
    }


def write_csv(path: Path, scores: List[dict], omitted: List[dict]) -> None:
    fields = [
        "ticker",
        "name",
        "gate",
        "finalScore",
        "financialStatusScore",
        "earningsReportScore",
        "nextEarningsRiskIndex",
        "dataConfidenceScore",
        "sourceFamilies",
        "portfolioWeightPct",
        "value",
        "nextEarningsDate",
        "daysToNext",
        "epsBeatRate",
        "altmanZScore",
        "piotroskiScore",
        "revenueGrowthAvg",
        "netMarginTTM",
        "freeCashFlowYieldTTM",
        "omitReason",
    ]
    export_rows = []
    for row in scores:
        export_rows.append(
            {
                    "ticker": row["ticker"],
                    "name": row["name"],
                    "gate": row["gate"],
                    "finalScore": row["finalScore"],
                    "financialStatusScore": row["financialStatusScore"],
                    "earningsReportScore": row["earningsReportScore"],
                    "nextEarningsRiskIndex": row["nextEarningsRiskIndex"],
                    "dataConfidenceScore": row["dataConfidenceScore"],
                    "sourceFamilies": ",".join(row.get("sourceFamilies") or []),
                    "portfolioWeightPct": row["portfolioWeightPct"],
                    "value": row["value"],
                    "nextEarningsDate": row["earnings"].get("nextDate"),
                    "daysToNext": row["earnings"].get("daysToNext"),
                    "epsBeatRate": row["earnings"].get("epsBeatRate"),
                    "altmanZScore": row["metrics"].get("altmanZScore"),
                    "piotroskiScore": row["metrics"].get("piotroskiScore"),
                    "revenueGrowthAvg": row["metrics"].get("revenueGrowthAvg"),
                    "netMarginTTM": row["metrics"].get("netMarginTTM"),
                    "freeCashFlowYieldTTM": row["metrics"].get("freeCashFlowYieldTTM"),
                    "omitReason": "",
            }
        )
    for row in omitted:
        export_rows.append(
            {
                    "ticker": row["ticker"],
                    "name": row["name"],
                    "portfolioWeightPct": row["portfolioWeightPct"],
                    "value": row["value"],
                    "omitReason": row["reason"],
            }
        )
    atomic_write_csv(path, export_rows, fields)


def pct_fmt(value: Optional[float], digits: int = 1) -> str:
    if value is None:
        return "-"
    return f"{value * 100:.{digits}f}%"


def write_report(path: Path, doc: dict) -> None:
    summary = doc["summary"]
    scores = doc["scores"]
    lines = [
        "# Financial Status Lens",
        "",
        f"- Generated: {doc['generatedAt']}",
        f"- Temporal integrity: {doc['temporalIntegrity']['mode']} "
        f"(point-in-time fundamentals: {doc['temporalIntegrity']['fundamentalsPointInTime']})",
        f"- Portfolio price date: {doc['portfolioAsOf'].get('priceAsOf') or '-'}",
        f"- Scored companies: {doc['counts']['scored']} / held positions: {doc['counts']['held']}",
        f"- Selection: {doc['selection']['mode']}; selected exposure "
        f"{doc['selection']['selectedPortfolioWeightPct']}% of the full held-equity denominator",
        f"- Average final / financial / earnings / event-risk: {summary.get('avgFinalScore')} / {summary.get('avgFinancialStatusScore')} / {summary.get('avgEarningsReportScore')} / {summary.get('avgNextEarningsRiskIndex')}",
        f"- API calls this run: {summary.get('apiCalls')} fresh, {summary.get('cacheHits')} cache hits",
        f"- Source coverage: {summary.get('sourceFamilyCounts') or {}}",
        "",
        "## Leaders",
        "",
    ]
    for row in summary.get("leaders", []):
        lines.append(f"- {row['ticker']}: {row['score']} ({row['gate']})")
    lines.extend(["", "## Scoreboard", ""])
    lines.append("| Ticker | Gate | Final | Financial | Earnings | Event risk | Next earnings | Reason |")
    lines.append("| --- | --- | ---: | ---: | ---: | ---: | --- | --- |")
    for row in sorted(scores, key=lambda r: r.get("finalScore") or 0, reverse=True):
        reason = ((row.get("gateReasons") or [{}])[0].get("detail") or "").replace("|", "/")
        earn = row.get("earnings") or {}
        next_text = earn.get("nextDate") or "-"
        if earn.get("daysToNext") is not None:
            next_text += f" ({earn['daysToNext']}d)"
        lines.append(
            f"| {row['ticker']} | {row['gate']} | {row['finalScore']} | {row['financialStatusScore']} | "
            f"{row['earningsReportScore']} | {row['nextEarningsRiskIndex']} | {next_text} | {reason} |"
        )
    lines.extend(["", "## Omitted", ""])
    if doc["omitted"]:
        for row in doc["omitted"]:
            lines.append(f"- {row['ticker']}: {row['reason']}")
    else:
        lines.append("- None")
    lines.extend(["", doc["disclaimer"], ""])
    atomic_write_text(path, "\n".join(lines))


def temporal_integrity(as_of: Optional[dt.date], run_date: dt.date, allow_non_point_in_time: bool = False) -> dict:
    """Describe or reject the relationship between scoring date and source data.

    FMP TTM, Yahoo summary, and this module's SEC adapter are current/as-known
    feeds.  They are not filing-vintage snapshots suitable for historical
    backtests.  Historical runs therefore fail closed unless the caller opts
    into a prominently labeled, non-point-in-time research artifact.
    """
    effective = as_of or run_date
    if effective > run_date:
        raise SystemExit(f"--as-of {effective} is in the future (run date {run_date})")
    historical = effective < run_date
    if historical and not allow_non_point_in_time:
        raise SystemExit(
            f"historical --as-of {effective} cannot use current fundamentals safely; "
            "omit --as-of, use today's date, or explicitly pass --allow-non-point-in-time"
        )
    return {
        "requestedAsOf": effective.isoformat(),
        "runDate": run_date.isoformat(),
        "mode": "CURRENT_AS_KNOWN" if not historical else "HISTORICAL_DATE_WITH_CURRENT_FUNDAMENTALS",
        "fundamentalsPointInTime": not historical,
        "backtestEligible": not historical,
        "overrideUsed": bool(historical and allow_non_point_in_time),
        "warning": (
            None
            if not historical
            else "Current/as-known fundamentals were scored against a historical date; do not use this artifact for backtesting."
        ),
    }


def build_document(args: argparse.Namespace) -> dict:
    if args.cache_ttl_hours <= 0:
        raise SystemExit("--cache-ttl-hours must be positive")
    if args.earnings_horizon_days <= 0:
        raise SystemExit("--earnings-horizon-days must be positive")
    if args.max_symbols < 0:
        raise SystemExit("--max-symbols cannot be negative")
    payload = extract_dashboard_payload(args.dashboard)
    forced = [s.strip().upper() for s in args.symbols.split(",") if s.strip()] if args.symbols else None
    companies, omitted = portfolio_rows(payload, forced)
    if args.max_symbols:
        companies = companies[: args.max_symbols]
    sources = {s.strip().lower() for s in args.sources.split(",") if s.strip()}
    unknown_sources = sources - {"fmp", "yahoo", "sec"}
    if unknown_sources:
        raise SystemExit(f"unknown sources: {', '.join(sorted(unknown_sources))}")
    api_key = load_api_key(args.config)
    if "fmp" in sources and not args.no_fetch and not api_key and sources == {"fmp"}:
        raise SystemExit(
            f"missing FMP API key. Set FMP_API_KEY or create {args.config} with {{\"apiKey\":\"...\"}}"
        )
    sec_user_agent = load_sec_user_agent(args.config, args.sec_user_agent)
    effective_sources = set(sources)
    if "fmp" in effective_sources and not api_key and not args.no_fetch:
        effective_sources.remove("fmp")
    if "sec" in effective_sources and not sec_user_agent and not args.no_fetch:
        effective_sources.remove("sec")
    client = FmpClient(
        api_key,
        args.cache,
        no_fetch=args.no_fetch,
        refresh_cache=args.refresh_cache,
        cache_ttl_hours=args.cache_ttl_hours,
    )
    run_date = dt.date.today()
    temporal = temporal_integrity(
        args.as_of,
        run_date,
        allow_non_point_in_time=getattr(args, "allow_non_point_in_time", False),
    )
    today = args.as_of or run_date
    if "fmp" in effective_sources:
        calendar, calendar_status = fetch_calendar(client, today, args.earnings_horizon_days)
    else:
        calendar, calendar_status = {}, source_status({"status": "disabled", "rows": 0, "ok": False, "error": None}, "fmp")
    scores = []
    for i, stock in enumerate(companies, 1):
        symbol = str(stock.get("sym") or "").upper()
        print(f"· financial scoring {i}/{len(companies)} {symbol} via {','.join(sorted(effective_sources)) or 'cache-only'}", file=sys.stderr)
        fmp_bundle, fmp_statuses = ({}, {})
        if "fmp" in effective_sources:
            fmp_bundle, fmp_statuses = fetch_company_bundle(client, symbol)
        yahoo_bundle, yahoo_statuses = ({}, {})
        if "yahoo" in effective_sources:
            yahoo_bundle, yahoo_statuses = fetch_yahoo_bundle(client, symbol, enabled=True)
        # Yahoo is usually the fastest way to get market cap for SEC-derived FCF yield.
        market_cap = first_num((as_list(yahoo_bundle.get("profile")) or [{}])[0], "marketCap")
        if market_cap is None:
            market_cap = first_num((as_list(fmp_bundle.get("profile")) or [{}])[0], "marketCap")
        sec_bundle, sec_statuses = ({}, {})
        if "sec" in effective_sources:
            sec_bundle, sec_statuses = fetch_sec_bundle(client, symbol, sec_user_agent, market_cap, enabled=True)
        bundle = merge_bundles(fmp_bundle, yahoo_bundle, sec_bundle)
        statuses = {}
        statuses.update({f"fmp:{k}": v for k, v in fmp_statuses.items()})
        statuses.update({f"yahoo:{k}": v for k, v in yahoo_statuses.items()})
        statuses.update({f"sec:{k}": v for k, v in sec_statuses.items()})
        scores.append(score_company(stock, bundle, statuses, calendar.get(symbol), today))
    client.save()
    scores.sort(
        key=lambda r: (
            first_not_none(r.get("finalScore"), -1),
            first_not_none(r.get("portfolioWeightPct"), 0),
        ),
        reverse=True,
    )
    counts = {
        "held": len([s for s in payload.get("stocks", []) if isinstance(s, dict) and s.get("held")]),
        "scored": len(scores),
        "omitted": len(omitted),
        "dataReview": sum(1 for s in scores if s.get("gate") == "DATA_REVIEW"),
        "earningsWatch": sum(1 for s in scores if s.get("gate") == "EARNINGS_WATCH"),
    }
    summary = summarize(scores, omitted, calendar_status, client)
    selected_weight = sum(first_not_none(num(r.get("portfolioWeightPct")), 0.0) for r in scores + omitted)
    return {
        "schemaVersion": 1,
        "researchOnly": True,
        "decisionGrade": False,
        "modelVersion": MODEL_VERSION,
        "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "asOfDate": today.isoformat(),
        "temporalIntegrity": temporal,
        "source": "FMP + Yahoo Finance/yfinance + SEC EDGAR companyfacts, merged by source priority",
        "sourcesRequested": sorted(sources),
        "sourcesEnabled": sorted(effective_sources),
        "secUserAgentConfigured": bool(sec_user_agent),
        "sourceDocs": SOURCE_DOCS,
        "portfolioAsOf": {
            "generatedAt": payload.get("summary", {}).get("generatedAt"),
            "priceAsOf": payload.get("summary", {}).get("priceAsOf"),
            "dateRange": payload.get("summary", {}).get("dateRange"),
            "marketValue": payload.get("summary", {}).get("marketValue"),
        },
        "selection": {
            "mode": "subset" if forced or args.max_symbols else "all-held",
            "requestedSymbols": forced or [],
            "maxSymbols": args.max_symbols or None,
            "selectedPortfolioWeightPct": rn(selected_weight, 3),
            "weightDenominator": "all held equity positions before --symbols/--max-symbols filtering",
        },
        "weights": {
            "financialStatusComponents": COMPONENT_WEIGHTS,
            "finalScore": FINAL_WEIGHTS,
            "nextEarningsRiskIndex": "Higher means more event risk; final score uses 100 - risk.",
        },
        "counts": counts,
        "summary": summary,
        "scores": scores,
        "omitted": omitted,
        "disclaimer": "Research and portfolio-risk lens only. Scores are deterministic heuristics from current/as-known FMP, Yahoo, and SEC data plus full-portfolio exposure; not investment advice, an earnings prediction, or a point-in-time backtest unless temporalIntegrity.backtestEligible is true.",
    }


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Create multi-source financial-status scoring artifacts for current holdings.")
    ap.add_argument("--dashboard", type=Path, default=DASHBOARD)
    ap.add_argument("--out-json", type=Path, default=OUT_JSON)
    ap.add_argument("--out-csv", type=Path, default=OUT_CSV)
    ap.add_argument("--out-md", type=Path, default=OUT_MD)
    ap.add_argument("--cache", type=Path, default=CACHE_PATH)
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--symbols", default="", help="Optional comma-separated held ticker subset.")
    ap.add_argument("--max-symbols", type=int, default=0, help="Debug limit after omission filtering.")
    ap.add_argument("--sources", default="fmp,yahoo,sec",
                    help="Comma-separated data sources to use: fmp,yahoo,sec. Default: all.")
    ap.add_argument("--sec-user-agent", default=None,
                    help="SEC EDGAR User-Agent string. Also reads SEC_USER_AGENT or secUserAgent in config.")
    ap.add_argument("--no-fetch", action="store_true", help="Use cache only; missing endpoints become DATA_REVIEW.")
    ap.add_argument("--refresh-cache", action="store_true", help="Ignore cached FMP responses and refetch.")
    ap.add_argument("--cache-ttl-hours", type=float, default=18.0)
    ap.add_argument("--earnings-horizon-days", type=int, default=210)
    ap.add_argument("--as-of", type=lambda s: dt.date.fromisoformat(s), default=None)
    ap.add_argument(
        "--allow-non-point-in-time",
        action="store_true",
        help="Allow historical --as-of with current/as-known fundamentals; artifact is labeled non-point-in-time and backtest-ineligible.",
    )
    return ap.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> None:
    args = parse_args(argv)
    doc = build_document(args)
    atomic_write_json(args.out_json, doc)
    write_csv(args.out_csv, doc["scores"], doc["omitted"])
    write_report(args.out_md, doc)
    print(f"✓ wrote {args.out_json} ({doc['counts']['scored']} companies, {doc['counts']['omitted']} omitted)")
    print(f"  csv: {args.out_csv}")
    print(f"  report: {args.out_md}")


if __name__ == "__main__":
    main()

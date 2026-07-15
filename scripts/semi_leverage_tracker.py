#!/usr/bin/env python3
"""Korea/U.S. semiconductor leverage and positioning tracker.

The two markets do not expose equivalent data:

* Korea: KOFIA publishes daily aggregate margin-credit loans and investor
  deposits. The primary ratio is credit loans / investor deposits.
* United States: FINRA publishes aggregate customer margin balances monthly.
  The primary ratio is margin debit / total free credit balances.
* Micron: FINRA Reg SHO off-exchange short volume is a daily security-level
  flow-pressure proxy. It is not leverage, short interest, or borrowed dollars.

The module keeps network adapters separate from the analytical helpers so the
alignment and event-study logic can be tested with synthetic data.
"""
from __future__ import annotations

import argparse
import calendar
import csv
import datetime as dt
import io
import json
import math
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
OUT_JSON = ROOT / "output" / "semi_leverage_tracker.json"
OUT_MD = ROOT / "output" / "semi_leverage_tracker_report.md"
CACHE_DIR = ROOT / "output" / "semi_leverage_cache"

KOFIA_URL = "https://freesis.kofia.or.kr/meta/getMetaDataList.do"
FINRA_MARGIN_URL = "https://www.finra.org/sites/default/files/2021-03/margin-statistics.xlsx"
FINRA_SHORT_URL = "https://api.finra.org/data/group/otcMarket/name/regShoDaily"
NAVER_QUOTE_URL = "https://m.stock.naver.com/api/stock/{symbol}/basic"
NASDAQ_QUOTE_URL = "https://api.nasdaq.com/api/quote/{symbol}/info?assetclass={asset_class}"

PRICE_TICKERS = {
    "005930.KS": {"name": "Samsung Electronics", "market": "Korea", "currency": "KRW"},
    "000660.KS": {"name": "SK hynix", "market": "Korea", "currency": "KRW"},
    "MU": {"name": "Micron", "market": "United States", "currency": "USD"},
    "SOXX": {"name": "iShares Semiconductor ETF", "market": "United States", "currency": "USD"},
}

SOURCE_NOTES = {
    "kofia": {
        "name": "Korea Financial Investment Association FreeSIS",
        "url": "https://freesis.kofia.or.kr/",
        "frequency": "daily",
        "scope": "Korean market aggregate",
        "metric": "Margin-credit loan balance / investor deposits",
        "limitation": "Market-wide balance, not security-level borrowing. The two balance series are joined on the same KOFIA date.",
    },
    "finraMargin": {
        "name": "FINRA Margin Statistics",
        "url": "https://www.finra.org/rules-guidance/key-topics/margin-accounts/margin-statistics",
        "frequency": "monthly",
        "scope": "U.S. customer securities accounts aggregate",
        "metric": "Margin debit / cash-account and margin-account free credit",
        "limitation": "Monthly aggregate data, generally released during the following month; it cannot identify semiconductor borrowing.",
    },
    "finraShort": {
        "name": "FINRA Reg SHO Daily Short Sale Volume",
        "url": "https://developer.finra.org/docs",
        "frequency": "daily",
        "scope": "Off-exchange trades reported to FINRA facilities",
        "metric": "Short volume / total reported volume",
        "limitation": "Flow-pressure proxy only. It is not short interest, leverage, days-to-cover, or directional net shorting.",
    },
    "prices": {
        "name": "Yahoo Finance via yfinance",
        "url": "https://finance.yahoo.com/",
        "frequency": "daily",
        "scope": "Adjusted closes",
        "metric": "Adjusted closing price",
        "limitation": "Unofficial convenience feed; corporate-action and exchange-calendar differences can affect alignment.",
    },
    "quotes": {
        "name": "Naver Finance / Nasdaq",
        "url": "https://m.stock.naver.com/",
        "frequency": "latest trade",
        "scope": "Samsung and SK hynix from Naver; MU and SOXX from Nasdaq",
        "metric": "Latest displayed trade plus regular-session close when available",
        "limitation": "Latest trades can be pre-market or after-hours and therefore differ from the daily adjusted close used in the analysis.",
    },
}


def rn(value, digits: int = 3):
    """Round finite numeric values and convert all non-finite values to None."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return round(number, digits)


def iso_date(value) -> str:
    if isinstance(value, dt.datetime):
        return value.date().isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    text = str(value).strip()
    if len(text) == 8 and text.isdigit():
        return dt.datetime.strptime(text, "%Y%m%d").date().isoformat()
    return dt.date.fromisoformat(text[:10]).isoformat()


def parse_number(value) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        number = float(
            str(value)
            .replace(",", "")
            .replace("$", "")
            .replace("₩", "")
            .strip()
        )
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def scaled(value, divisor: float, digits: int = 3):
    number = parse_number(value)
    return rn(number / divisor, digits) if number is not None else None


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def http_post_json(url: str, payload: dict, timeout: int = 40) -> bytes:
    data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "Accept": "application/json,text/csv,*/*",
            "Content-Type": "application/json",
            "User-Agent": "portfolio-tracker/1.0 leverage-research",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def http_get(url: str, timeout: int = 40, headers: Optional[dict] = None) -> bytes:
    request_headers = {"User-Agent": "portfolio-tracker/1.0 leverage-research"}
    request_headers.update(headers or {})
    request = urllib.request.Request(
        url,
        headers=request_headers,
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def kofia_request(object_name: str, start: str, end: str) -> dict:
    payload = {
        "dmSearch": {
            "tmpV40": "1000000",
            "tmpV41": "1",
            "tmpV1": "D",
            "tmpV45": start.replace("-", ""),
            "tmpV46": end.replace("-", ""),
            "OBJ_NM": object_name,
        }
    }
    return json.loads(http_post_json(KOFIA_URL, payload).decode("utf-8"))


def normalize_kofia(credit_payload: dict, funds_payload: dict) -> List[dict]:
    """Join the two official daily KOFIA series and calculate the ratio."""
    credit = {row.get("TMPV1"): row for row in credit_payload.get("ds1") or []}
    funds = {row.get("TMPV1"): row for row in funds_payload.get("ds1") or []}
    records = []
    for raw_date in sorted(set(credit) & set(funds)):
        c_row, f_row = credit[raw_date], funds[raw_date]
        credit_total = parse_number(c_row.get("TMPV2"))
        deposits = parse_number(f_row.get("TMPV2"))
        if credit_total is None or deposits in (None, 0):
            continue
        records.append(
            {
                "date": iso_date(raw_date),
                "creditLoansKrwBn": rn(credit_total / 1000.0, 3),
                "kospiCreditKrwBn": scaled(c_row.get("TMPV3"), 1000.0),
                "kosdaqCreditKrwBn": scaled(c_row.get("TMPV4"), 1000.0),
                "creditStockLendingKrwBn": scaled(c_row.get("TMPV5"), 1000.0),
                "securitiesBackedLoansKrwBn": scaled(c_row.get("TMPV9"), 1000.0),
                "investorDepositsKrwBn": rn(deposits / 1000.0, 3),
                "brokerageReceivablesKrwBn": scaled(f_row.get("TMPV5"), 1000.0),
                "forcedLiquidationKrwBn": scaled(f_row.get("TMPV6"), 1000.0),
                "forcedLiquidationRatioPct": rn(f_row.get("TMPV7"), 2),
                "leverageRatioPct": rn(credit_total / deposits * 100.0, 4),
            }
        )
    _add_changes(records, "leverageRatioPct", "ratioChangePp", 1)
    _add_changes(records, "creditLoansKrwBn", "creditChange5dPct", 5, percent=True)
    return records


def _add_changes(
    rows: List[dict],
    key: str,
    output_key: str,
    periods: int,
    percent: bool = False,
) -> None:
    for i, row in enumerate(rows):
        if i < periods:
            row[output_key] = None
            continue
        before, current = rows[i - periods].get(key), row.get(key)
        if before is None or current is None or (percent and before == 0):
            row[output_key] = None
        elif percent:
            row[output_key] = rn((current / before - 1.0) * 100.0, 4)
        else:
            row[output_key] = rn(current - before, 4)


def _cell_text(cell, namespace: str) -> str:
    cell_type = cell.attrib.get("t")
    if cell_type == "inlineStr":
        return "".join(node.text or "" for node in cell.iter(f"{{{namespace}}}t"))
    value = cell.find(f"{{{namespace}}}v")
    return "" if value is None else (value.text or "")


def parse_finra_margin_xlsx(content: bytes) -> List[dict]:
    """Parse FINRA's small XLSX with only the Python standard library."""
    namespace = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        root = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
    table = []
    for xml_row in root.findall(f".//{{{namespace}}}row"):
        values = {}
        for cell in xml_row.findall(f"{{{namespace}}}c"):
            ref = cell.attrib.get("r", "")
            column = "".join(ch for ch in ref if ch.isalpha())
            values[column] = _cell_text(cell, namespace)
        table.append(values)
    if not table:
        return []

    records = []
    for row in table[1:]:
        period = (row.get("A") or "").strip()
        if len(period) != 7 or period[4] != "-":
            continue
        debit = parse_number(row.get("B"))
        cash_free = parse_number(row.get("C"))
        margin_free = parse_number(row.get("D"))
        if debit is None or cash_free is None or margin_free is None:
            continue
        free_total = cash_free + margin_free
        if free_total <= 0:
            continue
        year, month = (int(part) for part in period.split("-"))
        month_end = dt.date(year, month, calendar.monthrange(year, month)[1])
        records.append(
            {
                "period": period,
                "date": month_end.isoformat(),
                "availableDateEstimate": finra_availability_date(period),
                "debitUsdBn": rn(debit / 1000.0, 3),
                "cashFreeCreditUsdBn": rn(cash_free / 1000.0, 3),
                "marginFreeCreditUsdBn": rn(margin_free / 1000.0, 3),
                "totalFreeCreditUsdBn": rn(free_total / 1000.0, 3),
                "leverageRatioX": rn(debit / free_total, 5),
            }
        )
    records.sort(key=lambda item: item["date"])
    _add_changes(records, "leverageRatioX", "ratioChangeX", 1)
    _add_changes(records, "debitUsdBn", "debitChangePct", 1, percent=True)
    return records


def finra_availability_date(period: str) -> str:
    """Conservative no-lookahead date when exact historical release dates are absent."""
    year, month = (int(part) for part in period.split("-"))
    if month == 12:
        year, month = year + 1, 1
    else:
        month += 1
    return dt.date(year, month, 25).isoformat()


def finra_short_request(symbol: str, start: str, end: str) -> str:
    payload = {
        "compareFilters": [
            {
                "compareType": "EQUAL",
                "fieldName": "securitiesInformationProcessorSymbolIdentifier",
                "fieldValue": symbol,
            }
        ],
        "dateRangeFilters": [
            {"startDate": start, "endDate": end, "fieldName": "tradeReportDate"}
        ],
        "limit": 5000,
    }
    return http_post_json(FINRA_SHORT_URL, payload).decode("utf-8-sig")


def aggregate_short_volume(content: str) -> List[dict]:
    """Aggregate the three FINRA reporting facilities before taking shares."""
    by_date: Dict[str, dict] = {}
    for row in csv.DictReader(io.StringIO(content)):
        date = iso_date(row.get("tradeReportDate"))
        bucket = by_date.setdefault(
            date, {"short": 0.0, "shortExempt": 0.0, "total": 0.0, "facilities": set()}
        )
        bucket["short"] += parse_number(row.get("shortParQuantity")) or 0.0
        bucket["shortExempt"] += parse_number(row.get("shortExemptParQuantity")) or 0.0
        bucket["total"] += parse_number(row.get("totalParQuantity")) or 0.0
        if row.get("reportingFacilityCode"):
            bucket["facilities"].add(row["reportingFacilityCode"])
    records = []
    for date in sorted(by_date):
        value = by_date[date]
        if value["total"] <= 0:
            continue
        records.append(
            {
                "date": date,
                "shortVolume": rn(value["short"], 3),
                "shortExemptVolume": rn(value["shortExempt"], 3),
                "totalVolume": rn(value["total"], 3),
                "shortSharePct": rn(value["short"] / value["total"] * 100.0, 4),
                "facilityCount": len(value["facilities"]),
            }
        )
    shares = pd.Series([row["shortSharePct"] for row in records], dtype=float)
    rolling = shares.rolling(5, min_periods=3).mean()
    for i, row in enumerate(records):
        row["shortShare5dPct"] = rn(rolling.iloc[i], 4)
    _add_changes(records, "shortShare5dPct", "shortShare5dChangePp", 1)
    return records


def fetch_prices(tickers: Iterable[str], start: str, end: str) -> Dict[str, List[dict]]:
    import warnings

    warnings.filterwarnings("ignore")
    import yfinance as yf

    symbols = sorted(set(tickers))
    end_exclusive = (dt.date.fromisoformat(end) + dt.timedelta(days=1)).isoformat()
    raw = yf.download(
        symbols,
        start=start,
        end=end_exclusive,
        interval="1d",
        auto_adjust=True,
        progress=False,
        threads=True,
    )
    if raw is None or raw.empty:
        raise RuntimeError("Yahoo Finance returned no prices")
    if getattr(raw.columns, "nlevels", 1) > 1:
        if "Close" in raw.columns.get_level_values(0):
            closes = raw["Close"]
        else:
            closes = raw.xs("Close", axis=1, level=1)
    else:
        closes = raw[["Close"]].rename(columns={"Close": symbols[0]})
    output = {}
    for symbol in symbols:
        if symbol not in closes:
            output[symbol] = []
            continue
        series = closes[symbol].dropna()
        output[symbol] = [
            {"date": index.date().isoformat(), "close": rn(value, 4)}
            for index, value in series.items()
            if rn(value, 4) is not None
        ]
    return output


def parse_naver_quote(content: bytes, symbol: str) -> dict:
    payload = json.loads(content.decode("utf-8"))
    price = parse_number(payload.get("closePrice"))
    if price is None:
        raise ValueError(f"Naver returned no price for {symbol}")
    traded_at = payload.get("localTradedAt")
    return {
        "symbol": symbol,
        "price": rn(price, 4),
        "regularClose": rn(price, 4),
        "currency": "KRW",
        "asOf": traded_at,
        "asOfLabel": traded_at.replace("T", " ")[:16] + " KST" if traded_at else None,
        "session": payload.get("marketStatus"),
        "source": "Naver Finance",
        "sourceUrl": payload.get("endUrl") or "https://m.stock.naver.com/",
    }


def parse_nasdaq_quote(content: bytes, symbol: str) -> dict:
    payload = json.loads(content.decode("utf-8"))
    data = payload.get("data") or {}
    primary = data.get("primaryData") or {}
    secondary = data.get("secondaryData") or {}
    price = parse_number(primary.get("lastSalePrice"))
    if price is None:
        raise ValueError(f"Nasdaq returned no price for {symbol}")
    regular_close = parse_number(secondary.get("lastSalePrice"))
    traded_at = primary.get("lastTradeTimestamp")
    return {
        "symbol": symbol,
        "price": rn(price, 4),
        "regularClose": rn(regular_close, 4),
        "currency": "USD",
        "asOf": traded_at,
        "asOfLabel": traded_at,
        "regularCloseAsOf": secondary.get("lastTradeTimestamp"),
        "session": data.get("marketStatus"),
        "source": "Nasdaq",
        "sourceUrl": f"https://www.nasdaq.com/market-activity/{'etf' if symbol == 'SOXX' else 'stocks'}/{symbol.lower()}",
    }


def fetch_latest_quotes() -> Dict[str, dict]:
    browser_headers = {
        "Accept": "application/json,text/plain,*/*",
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
    }
    output = {}
    for symbol, naver_symbol in (("005930.KS", "005930"), ("000660.KS", "000660")):
        output[symbol] = parse_naver_quote(
            http_get(NAVER_QUOTE_URL.format(symbol=naver_symbol), headers=browser_headers),
            symbol,
        )
    for symbol, asset_class in (("MU", "stocks"), ("SOXX", "etf")):
        output[symbol] = parse_nasdaq_quote(
            http_get(
                NASDAQ_QUOTE_URL.format(symbol=symbol, asset_class=asset_class),
                headers=browser_headers,
            ),
            symbol,
        )
    return output


def cached_source(
    source_id: str,
    fetcher: Callable[[], object],
    no_fetch: bool,
    warnings: List[str],
) -> Tuple[object, dict]:
    cache_path = CACHE_DIR / f"{source_id}.json"
    note = dict(SOURCE_NOTES[source_id])
    if not no_fetch:
        try:
            value = fetcher()
            write_json(cache_path, value)
            note.update({"id": source_id, "status": "live"})
            return value, note
        except Exception as exc:
            warnings.append(f"{source_id}: live fetch failed ({type(exc).__name__}: {exc}); cache used when available.")
    if cache_path.exists():
        value = read_json(cache_path)
        modified = dt.datetime.fromtimestamp(cache_path.stat().st_mtime, tz=dt.timezone.utc)
        note.update(
            {
                "id": source_id,
                "status": "cache",
                "cacheUpdatedAt": modified.isoformat(timespec="seconds"),
            }
        )
        return value, note
    note.update({"id": source_id, "status": "unavailable"})
    warnings.append(f"{source_id}: no live response or cache is available.")
    return {} if source_id in {"prices", "quotes"} else [], note


def percentile_rank(values: Sequence[float], current: float) -> Optional[float]:
    valid = np.asarray([value for value in values if rn(value) is not None], dtype=float)
    if not len(valid):
        return None
    return rn(float(np.mean(valid <= current) * 100.0), 1)


def current_pressure(rows: Sequence[dict], key: str, window: int) -> dict:
    valid = [row for row in rows if rn(row.get(key)) is not None]
    if not valid:
        return {"zScore": None, "percentile": None, "sampleSize": 0}
    sample = valid[-window:]
    values = np.asarray([float(row[key]) for row in sample], dtype=float)
    std = float(np.std(values, ddof=1)) if len(values) > 1 else 0.0
    z_score = (values[-1] - float(np.mean(values))) / std if std > 0 else None
    return {
        "zScore": rn(z_score, 2),
        "percentile": percentile_rank(values, values[-1]),
        "sampleSize": len(values),
        "windowStart": sample[0]["date"],
    }


def _price_arrays(prices: Sequence[dict]) -> Tuple[List[str], np.ndarray]:
    clean = sorted(
        (
            (iso_date(row["date"]), float(row["close"]))
            for row in prices
            if rn(row.get("close")) is not None
        ),
        key=lambda pair: pair[0],
    )
    return [item[0] for item in clean], np.asarray([item[1] for item in clean], dtype=float)


def align_observations(
    metric_rows: Sequence[dict],
    metric_key: str,
    prices: Sequence[dict],
    availability_key: Optional[str] = None,
    lag_calendar_days: int = 1,
) -> List[dict]:
    """Align each observation to the first close available after publication.

    Duplicate aligned price dates are collapsed by retaining the latest metric
    observation. This avoids zero-period returns around weekends and holidays.
    """
    price_dates, closes = _price_arrays(prices)
    if not price_dates:
        return []
    by_price_date = {}
    previous_value = None
    for row in sorted(metric_rows, key=lambda item: item["date"]):
        value = rn(row.get(metric_key), 8)
        if value is None:
            continue
        if availability_key and row.get(availability_key):
            available = iso_date(row[availability_key])
        else:
            base = dt.date.fromisoformat(iso_date(row["date"]))
            available = (base + dt.timedelta(days=lag_calendar_days)).isoformat()
        position = int(np.searchsorted(price_dates, available, side="left"))
        change = None if previous_value is None else value - previous_value
        previous_value = value
        if position >= len(price_dates):
            continue
        by_price_date[price_dates[position]] = {
            "metricDate": iso_date(row["date"]),
            "availableDate": available,
            "priceDate": price_dates[position],
            "priceIndex": position,
            "metricValue": value,
            "metricChange": change,
            "close": float(closes[position]),
        }
    aligned = [by_price_date[key] for key in sorted(by_price_date)]
    previous_close = None
    for row in aligned:
        row["concurrentReturnPct"] = (
            None if previous_close in (None, 0) else (row["close"] / previous_close - 1.0) * 100.0
        )
        previous_close = row["close"]
    return aligned


def pearson(x: Sequence[float], y: Sequence[float]) -> Optional[float]:
    if len(x) < 3:
        return None
    x_arr, y_arr = np.asarray(x, dtype=float), np.asarray(y, dtype=float)
    if np.std(x_arr) == 0 or np.std(y_arr) == 0:
        return None
    return float(np.corrcoef(x_arr, y_arr)[0, 1])


def spearman(x: Sequence[float], y: Sequence[float]) -> Optional[float]:
    if len(x) < 3:
        return None
    x_rank = pd.Series(x, dtype=float).rank(method="average").to_numpy()
    y_rank = pd.Series(y, dtype=float).rank(method="average").to_numpy()
    return pearson(x_rank, y_rank)


def block_bootstrap_corr(
    x: Sequence[float],
    y: Sequence[float],
    block_length: int = 5,
    samples: int = 400,
    seed: int = 17,
) -> Optional[List[float]]:
    """Deterministic moving-block bootstrap interval for Pearson correlation."""
    n = len(x)
    if n < max(8, block_length + 2):
        return None
    x_arr, y_arr = np.asarray(x, dtype=float), np.asarray(y, dtype=float)
    rng = np.random.default_rng(seed)
    estimates = []
    max_start = max(1, n - block_length + 1)
    for _ in range(samples):
        indices = []
        while len(indices) < n:
            start = int(rng.integers(0, max_start))
            indices.extend(range(start, min(start + block_length, n)))
        indices = np.asarray(indices[:n], dtype=int)
        estimate = pearson(x_arr[indices], y_arr[indices])
        if estimate is not None and math.isfinite(estimate):
            estimates.append(estimate)
    if len(estimates) < samples // 2:
        return None
    low, high = np.quantile(estimates, [0.025, 0.975])
    return [rn(low, 3), rn(high, 3)]


def correlation_summary(
    pairs: Sequence[Tuple[float, float]],
    block_length: int,
) -> dict:
    clean = [
        (float(x), float(y))
        for x, y in pairs
        if rn(x, 8) is not None and rn(y, 8) is not None
    ]
    x = [pair[0] for pair in clean]
    y = [pair[1] for pair in clean]
    return {
        "n": len(clean),
        "pearson": rn(pearson(x, y), 3),
        "spearman": rn(spearman(x, y), 3),
        "pearsonCi95": block_bootstrap_corr(x, y, block_length=block_length),
    }


def relationship_analysis(
    ticker: str,
    name: str,
    market: str,
    metric_name: str,
    metric_unit: str,
    metric_rows: Sequence[dict],
    metric_key: str,
    prices: Sequence[dict],
    horizons: Sequence[Tuple[str, int]],
    frequency: str,
    availability_key: Optional[str] = None,
    lag_calendar_days: int = 1,
    block_length: int = 5,
) -> dict:
    aligned = align_observations(
        metric_rows,
        metric_key,
        prices,
        availability_key=availability_key,
        lag_calendar_days=lag_calendar_days,
    )
    _, closes = _price_arrays(prices)
    concurrent_pairs = [
        (row["metricChange"], row["concurrentReturnPct"])
        for row in aligned
        if row.get("metricChange") is not None and row.get("concurrentReturnPct") is not None
    ]
    forward = []
    forward_values: Dict[int, List[Tuple[float, float]]] = {}
    for label, sessions in horizons:
        pairs = []
        for row in aligned:
            target = row["priceIndex"] + sessions
            if row.get("metricChange") is None or target >= len(closes) or row["close"] == 0:
                continue
            future_return = (float(closes[target]) / row["close"] - 1.0) * 100.0
            pairs.append((row["metricChange"], future_return))
        forward_values[sessions] = pairs
        forward.append(
            {
                "horizon": label,
                "sessions": sessions,
                **correlation_summary(pairs, block_length=block_length),
            }
        )

    reverse_pairs = []
    for current, following in zip(aligned, aligned[1:]):
        if current.get("concurrentReturnPct") is None or following.get("metricChange") is None:
            continue
        reverse_pairs.append((current["concurrentReturnPct"], following["metricChange"]))

    event_label, event_sessions = horizons[min(1, len(horizons) - 1)]
    event_pairs = forward_values.get(event_sessions) or []
    event = {
        "horizon": event_label,
        "topCount": 0,
        "bottomCount": 0,
        "topAvgReturnPct": None,
        "bottomAvgReturnPct": None,
        "spreadPct": None,
    }
    if len(event_pairs) >= 10:
        changes = np.asarray([pair[0] for pair in event_pairs], dtype=float)
        returns = np.asarray([pair[1] for pair in event_pairs], dtype=float)
        low_cut, high_cut = np.quantile(changes, [0.2, 0.8])
        bottom = returns[changes <= low_cut]
        top = returns[changes >= high_cut]
        if len(top) and len(bottom):
            event.update(
                {
                    "topCount": int(len(top)),
                    "bottomCount": int(len(bottom)),
                    "topAvgReturnPct": rn(np.mean(top), 3),
                    "bottomAvgReturnPct": rn(np.mean(bottom), 3),
                    "spreadPct": rn(np.mean(top) - np.mean(bottom), 3),
                }
            )

    return {
        "ticker": ticker,
        "name": name,
        "market": market,
        "frequency": frequency,
        "metric": metric_name,
        "metricUnit": metric_unit,
        "availabilityRule": (
            f"{availability_key} (conservative estimate)"
            if availability_key
            else f"first price session on/after metric date + {lag_calendar_days} calendar day"
        ),
        "alignedObservations": len(aligned),
        "concurrent": correlation_summary(concurrent_pairs, block_length=block_length),
        "forward": forward,
        "reverse": {
            "description": "Current observation-period stock return vs next metric change",
            **correlation_summary(reverse_pairs, block_length=block_length),
        },
        "eventStudy": event,
    }


def _price_on_or_before(prices: Sequence[dict], target: str) -> Optional[float]:
    dates, closes = _price_arrays(prices)
    if not dates:
        return None
    position = int(np.searchsorted(dates, target, side="right")) - 1
    return float(closes[position]) if position >= 0 else None


def indexed_chart(
    metric_rows: Sequence[dict],
    metric_key: str,
    price_map: Dict[str, Sequence[dict]],
    limit: int,
) -> List[dict]:
    rows = [row for row in metric_rows if rn(row.get(metric_key)) is not None][-limit:]
    raw = []
    for row in rows:
        point = {"date": row["date"], "metric": float(row[metric_key])}
        for symbol, prices in price_map.items():
            point[symbol] = _price_on_or_before(prices, row["date"])
        raw.append(point)
    bases = {}
    for key in [metric_key, *price_map.keys()]:
        source_key = "metric" if key == metric_key else key
        bases[key] = next(
            (point[source_key] for point in raw if point.get(source_key) not in (None, 0)),
            None,
        )
    output = []
    for point in raw:
        row = {
            "date": point["date"],
            "metricValue": rn(point["metric"], 4),
            "priceValues": {},
        }
        row["metricIndex"] = rn(point["metric"] / bases[metric_key] * 100.0, 3) if bases[metric_key] else None
        for symbol in price_map:
            value = point.get(symbol)
            row[symbol] = rn(value / bases[symbol] * 100.0, 3) if value and bases[symbol] else None
            row["priceValues"][symbol] = rn(value, 4)
        output.append(row)
    return output


def relationship_takeaway(item: dict) -> str:
    forward = next((row for row in item.get("forward") or [] if row.get("n", 0) >= 8), None)
    if not forward or forward.get("pearson") is None:
        return f"{item['ticker']}: insufficient aligned observations for a stable forward relationship."
    corr = forward["pearson"]
    interval = forward.get("pearsonCi95")
    if interval and interval[0] <= 0 <= interval[1]:
        strength = "not distinguishable from zero in the block-bootstrap interval"
    elif abs(corr) < 0.2:
        strength = "weak"
    elif abs(corr) < 0.4:
        strength = "moderate"
    else:
        strength = "strong in-sample"
    direction = "positive" if corr > 0 else "negative"
    statement = (
        f"{item['ticker']}: {forward['horizon']} forward Pearson r={corr:+.2f} "
        f"(n={forward['n']}), a {direction} relationship that is {strength}."
    )
    reverse = item.get("reverse") or {}
    reverse_ci = reverse.get("pearsonCi95")
    if (
        reverse.get("pearson") is not None
        and reverse_ci
        and not (reverse_ci[0] <= 0 <= reverse_ci[1])
    ):
        statement += (
            f" The reverse test is clearer (return -> next metric change "
            f"r={reverse['pearson']:+.2f}, CI [{reverse_ci[0]:+.2f}, {reverse_ci[1]:+.2f}]), "
            "which is more consistent with price chasing than a forward signal."
        )
    return statement


def _latest_date(rows: Sequence[dict]) -> Optional[str]:
    return max((row.get("date") for row in rows if row.get("date")), default=None)


def build_document(args) -> dict:
    warnings = []
    source_status = []

    def fetch_kofia():
        credit = kofia_request("STATSCU0100000070BO", args.start, args.end)
        funds = kofia_request("STATSCU0100000060BO", args.start, args.end)
        return normalize_kofia(credit, funds)

    korea, status = cached_source("kofia", fetch_kofia, args.no_fetch, warnings)
    source_status.append(status)

    def fetch_finra_margin():
        return parse_finra_margin_xlsx(http_get(FINRA_MARGIN_URL))

    us_margin, status = cached_source(
        "finraMargin", fetch_finra_margin, args.no_fetch, warnings
    )
    source_status.append(status)

    short_start = max(
        dt.date.fromisoformat(args.start),
        dt.date.fromisoformat(args.end) - dt.timedelta(days=364),
    ).isoformat()

    def fetch_short():
        return aggregate_short_volume(finra_short_request("MU", short_start, args.end))

    mu_short, status = cached_source("finraShort", fetch_short, args.no_fetch, warnings)
    source_status.append(status)

    def fetch_all_prices():
        return fetch_prices(PRICE_TICKERS, args.start, args.end)

    prices, status = cached_source("prices", fetch_all_prices, args.no_fetch, warnings)
    source_status.append(status)

    latest_quotes, status = cached_source(
        "quotes", fetch_latest_quotes, args.no_fetch, warnings
    )
    source_status.append(status)

    korea = list(korea or [])
    us_margin = [
        row for row in (us_margin or []) if args.start <= row.get("date", "") <= args.end
    ]
    mu_short = list(mu_short or [])
    prices = prices or {}
    latest_quotes = latest_quotes or {}
    for symbol in PRICE_TICKERS:
        history = prices.get(symbol) or []
        if symbol in latest_quotes or not history:
            continue
        latest = history[-1]
        latest_quotes[symbol] = {
            "symbol": symbol,
            "price": latest.get("close"),
            "regularClose": latest.get("close"),
            "currency": PRICE_TICKERS[symbol]["currency"],
            "asOf": latest.get("date"),
            "asOfLabel": latest.get("date"),
            "session": "DAILY_CLOSE",
            "source": "Yahoo Finance fallback",
            "sourceUrl": "https://finance.yahoo.com/",
        }

    analyses = []
    for symbol in ("005930.KS", "000660.KS"):
        analyses.append(
            relationship_analysis(
                symbol,
                PRICE_TICKERS[symbol]["name"],
                "Korea",
                "KOFIA credit loans / investor deposits",
                "percentage-point change",
                korea,
                "leverageRatioPct",
                prices.get(symbol) or [],
                [("1d", 1), ("5d", 5), ("20d", 20)],
                "daily",
                lag_calendar_days=1,
                block_length=10,
            )
        )
    for symbol in ("MU", "SOXX"):
        analyses.append(
            relationship_analysis(
                symbol,
                PRICE_TICKERS[symbol]["name"],
                "United States",
                "FINRA margin debit / total free credit",
                "ratio change",
                us_margin,
                "leverageRatioX",
                prices.get(symbol) or [],
                [("1m", 21), ("3m", 63), ("6m", 126)],
                "monthly",
                availability_key="availableDateEstimate",
                block_length=4,
            )
        )
    analyses.append(
        relationship_analysis(
            "MU",
            "Micron",
            "United States",
            "MU off-exchange short-volume share, 5-session average",
            "percentage-point change",
            mu_short,
            "shortShare5dPct",
            prices.get("MU") or [],
            [("1d", 1), ("5d", 5), ("20d", 20)],
            "daily proxy",
            lag_calendar_days=1,
            block_length=10,
        )
    )

    korea_pressure = current_pressure(korea, "leverageRatioPct", 756)
    us_pressure = current_pressure(us_margin, "leverageRatioX", 36)
    current_korea = dict(korea[-1]) if korea else None
    current_us = dict(us_margin[-1]) if us_margin else None
    current_short = dict(mu_short[-1]) if mu_short else None
    if current_korea:
        current_korea["pressure"] = korea_pressure
    if current_us:
        current_us["pressure"] = us_pressure
    if current_short:
        current_short["pressure"] = current_pressure(mu_short, "shortShare5dPct", 252)

    for status in source_status:
        source_id = status["id"]
        source_rows = {
            "kofia": korea,
            "finraMargin": us_margin,
            "finraShort": mu_short,
        }.get(source_id)
        if source_rows is not None:
            status["asOf"] = _latest_date(source_rows)
            status["observations"] = len(source_rows)
        elif source_id == "prices":
            dates = [
                row["date"]
                for series in prices.values()
                for row in series
                if row.get("date")
            ]
            status["asOf"] = max(dates, default=None)
            status["observations"] = sum(len(series) for series in prices.values())
        elif source_id == "quotes":
            status["asOf"] = max(
                (quote.get("asOf") or "" for quote in latest_quotes.values()),
                default=None,
            )
            status["observations"] = len(latest_quotes)

    as_of = max(
        (
            date
            for date in [
                _latest_date(korea),
                _latest_date(us_margin),
                _latest_date(mu_short),
                *[
                    _latest_date(series)
                    for series in prices.values()
                    if isinstance(series, list)
                ],
            ]
            if date
        ),
        default=None,
    )
    generated_at = dt.datetime.now().astimezone().isoformat(timespec="seconds")

    return {
        "schemaVersion": 2,
        "generatedAt": generated_at,
        "asOf": as_of,
        "researchOnly": True,
        "decisionGrade": False,
        "title": "Semiconductor Leverage Tracker",
        "subtitle": "Korea/U.S. market leverage and memory-stock relationship analysis",
        "comparisonRule": "Compare within-country z-scores and percentiles, never the raw Korea percentage with the raw U.S. multiple.",
        "marketComparison": {
            "korea": {
                "label": "Korea daily market leverage",
                "zScore": korea_pressure.get("zScore"),
                "percentile": korea_pressure.get("percentile"),
                "sampleSize": korea_pressure.get("sampleSize"),
            },
            "unitedStates": {
                "label": "U.S. monthly market leverage",
                "zScore": us_pressure.get("zScore"),
                "percentile": us_pressure.get("percentile"),
                "sampleSize": us_pressure.get("sampleSize"),
            },
        },
        "korea": {
            "definition": "KOFIA total margin-credit loan balance / investor deposits",
            "frequency": "daily",
            "current": current_korea,
            "history": korea,
            "chart": indexed_chart(
                korea,
                "leverageRatioPct",
                {
                    "005930.KS": prices.get("005930.KS") or [],
                    "000660.KS": prices.get("000660.KS") or [],
                },
                756,
            ),
        },
        "unitedStates": {
            "definition": "FINRA margin debit / cash-account plus margin-account free credit balances",
            "frequency": "monthly",
            "current": current_us,
            "history": us_margin,
            "chart": indexed_chart(
                us_margin,
                "leverageRatioX",
                {"MU": prices.get("MU") or [], "SOXX": prices.get("SOXX") or []},
                48,
            ),
            "muShortFlow": {
                "definition": "FINRA off-exchange short volume / total reported volume",
                "classification": "daily flow-pressure proxy, not leverage",
                "current": current_short,
                "history": mu_short,
                "chart": indexed_chart(
                    mu_short,
                    "shortShare5dPct",
                    {"MU": prices.get("MU") or []},
                    252,
                ),
            },
        },
        "prices": {
            symbol: {
                **PRICE_TICKERS[symbol],
                "asOf": _latest_date(prices.get(symbol) or []),
                "adjustedClose": (
                    (prices.get(symbol) or [{}])[-1].get("close")
                    if prices.get(symbol)
                    else None
                ),
                "latestQuote": latest_quotes.get(symbol),
                "history": (prices.get(symbol) or [])[-520:],
            }
            for symbol in PRICE_TICKERS
        },
        "analysis": {
            "method": [
                "Use changes in leverage/proxy ratios, not trending levels.",
                "Concurrent correlation pairs each metric change with the stock return between metric observations; it is descriptive, not tradeable.",
                "Forward returns start at the first close on or after estimated publication availability.",
                "FINRA monthly availability is conservatively estimated as the 25th of the following month because the workbook does not include release timestamps.",
                "Event studies compare top and bottom metric-change quintiles at the middle listed horizon.",
                "Pearson intervals use a deterministic moving-block bootstrap and are descriptive, not p-values.",
            ],
            "relationships": analyses,
            "takeaways": [relationship_takeaway(item) for item in analyses],
        },
        "sources": source_status,
        "warnings": warnings,
        "disclaimer": "Research analytics only. Aggregate leverage and short-volume data do not identify security-level borrowed positions and are not investment advice.",
    }


def _fmt(value, digits=2, suffix="") -> str:
    number = rn(value, digits)
    return "-" if number is None else f"{number:,.{digits}f}{suffix}"


def render_report(doc: dict) -> str:
    korea = (doc.get("korea") or {}).get("current") or {}
    united_states = (doc.get("unitedStates") or {}).get("current") or {}
    short = ((doc.get("unitedStates") or {}).get("muShortFlow") or {}).get("current") or {}
    lines = [
        "# Semiconductor Leverage Tracker",
        "",
        f"Generated: {doc.get('generatedAt', '-')}",
        f"Data as of: {doc.get('asOf') or '-'}",
        "",
        "## Current Readings",
        "",
        "| Market | Official/Proxy | Latest | Within-market pressure |",
        "| --- | --- | ---: | --- |",
        (
            f"| Korea | Credit loans / investor deposits | "
            f"{_fmt(korea.get('leverageRatioPct'), 2, '%')} | "
            f"z={_fmt((korea.get('pressure') or {}).get('zScore'), 2)}, "
            f"P{_fmt((korea.get('pressure') or {}).get('percentile'), 1)} |"
        ),
        (
            f"| United States | Margin debit / total free credit | "
            f"{_fmt(united_states.get('leverageRatioX'), 2, 'x')} | "
            f"z={_fmt((united_states.get('pressure') or {}).get('zScore'), 2)}, "
            f"P{_fmt((united_states.get('pressure') or {}).get('percentile'), 1)} |"
        ),
        (
            f"| Micron | Off-exchange short-volume share, 5D average (proxy) | "
            f"{_fmt(short.get('shortShare5dPct'), 2, '%')} | "
            f"z={_fmt((short.get('pressure') or {}).get('zScore'), 2)}, "
            f"P{_fmt((short.get('pressure') or {}).get('percentile'), 1)} |"
        ),
        "",
        "Raw Korean and U.S. ratios are not comparable. The dashboard compares only within-market z-scores and percentiles.",
        "",
        "## Latest Stock Quotes",
        "",
        "| Ticker | Latest trade | Session | As of | Source | Regular close |",
        "| --- | ---: | --- | --- | --- | ---: |",
    ]
    for symbol, price_data in (doc.get("prices") or {}).items():
        quote = price_data.get("latestQuote") or {}
        currency = quote.get("currency") or price_data.get("currency")
        prefix = "KRW " if currency == "KRW" else "USD "
        lines.append(
            f"| {symbol} | {prefix}{_fmt(quote.get('price'), 0 if currency == 'KRW' else 2)} | "
            f"{quote.get('session') or '-'} | {quote.get('asOfLabel') or quote.get('asOf') or '-'} | "
            f"{quote.get('source') or '-'} | {prefix}{_fmt(quote.get('regularClose'), 0 if currency == 'KRW' else 2)} |"
        )
    lines.extend(
        [
        "",
        "## Balance Detail",
        "",
        f"- Korea credit loans: KRW {_fmt(korea.get('creditLoansKrwBn'), 1)}bn.",
        f"- Korea investor deposits: KRW {_fmt(korea.get('investorDepositsKrwBn'), 1)}bn.",
        f"- U.S. margin debit: USD {_fmt(united_states.get('debitUsdBn'), 1)}bn.",
        f"- U.S. total free credit: USD {_fmt(united_states.get('totalFreeCreditUsdBn'), 1)}bn.",
        "",
        "## Relationship Analysis",
        "",
        "| Ticker | Metric | Frequency | Concurrent r | Forward horizon | Forward r | 95% block CI | n | Event spread |",
        "| --- | --- | --- | ---: | --- | ---: | --- | ---: | ---: |",
        ]
    )
    for item in (doc.get("analysis") or {}).get("relationships") or []:
        forward = next((row for row in item.get("forward") or [] if row.get("n", 0) >= 8), (item.get("forward") or [{}])[0])
        concurrent = item.get("concurrent") or {}
        event = item.get("eventStudy") or {}
        interval = forward.get("pearsonCi95")
        interval_text = (
            f"[{interval[0]:+.2f}, {interval[1]:+.2f}]" if interval else "-"
        )
        lines.append(
            f"| {item.get('ticker')} | {item.get('metric')} | {item.get('frequency')} | "
            f"{_fmt(concurrent.get('pearson'), 2)} | {forward.get('horizon', '-')} | "
            f"{_fmt(forward.get('pearson'), 2)} | {interval_text} | {forward.get('n', 0)} | "
            f"{_fmt(event.get('spreadPct'), 2, 'pp')} |"
        )
    lines.extend(["", "### Interpretation", ""])
    for takeaway in (doc.get("analysis") or {}).get("takeaways") or []:
        lines.append(f"- {takeaway}")

    lines.extend(
        [
            "",
            "## Source Inventory",
            "",
            "| Source | Status | Frequency | As of | Observations | Limitation |",
            "| --- | --- | --- | --- | ---: | --- |",
        ]
    )
    for source in doc.get("sources") or []:
        lines.append(
            f"| [{source.get('name')}]({source.get('url')}) | {source.get('status')} | "
            f"{source.get('frequency')} | {source.get('asOf') or '-'} | "
            f"{source.get('observations', '-')} | {source.get('limitation')} |"
        )
    lines.extend(["", "## Methodology", ""])
    for method in (doc.get("analysis") or {}).get("method") or []:
        lines.append(f"- {method}")
    if doc.get("warnings"):
        lines.extend(["", "## Data Warnings", ""])
        lines.extend(f"- {warning}" for warning in doc["warnings"])
    lines.extend(
        [
            "",
            "## Interpretation Limits",
            "",
            "- Correlation is not causation. Rising prices can attract leverage, leverage can amplify prices, and both can respond to a third variable.",
            "- KOFIA and FINRA ratios have different definitions, frequencies, currencies, account populations, and publication lags.",
            "- MU short-volume share covers off-exchange reported flow and can include market-making or hedging activity.",
            "- Samples overlap for multi-session forward returns, so ordinary independent-observation significance tests would be overstated.",
            "",
            f"**{doc.get('disclaimer')}**",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    today = dt.date.today()
    default_start = max(dt.date(2024, 1, 1), today - dt.timedelta(days=3 * 365))
    parser = argparse.ArgumentParser(
        description="Generate Korea/U.S. semiconductor leverage tracker JSON and report."
    )
    parser.add_argument("--start", default=default_start.isoformat())
    parser.add_argument("--end", default=today.isoformat())
    parser.add_argument("--out-json", default=str(OUT_JSON))
    parser.add_argument("--out-md", default=str(OUT_MD))
    parser.add_argument(
        "--no-fetch",
        action="store_true",
        help="use output/semi_leverage_cache only; never access network sources",
    )
    args = parser.parse_args()
    if dt.date.fromisoformat(args.start) > dt.date.fromisoformat(args.end):
        parser.error("--start must be on or before --end")

    document = build_document(args)
    out_json, out_md = Path(args.out_json), Path(args.out_md)
    write_json(out_json, document)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text(render_report(document), encoding="utf-8")
    print(f"wrote {out_json}")
    print(f"wrote {out_md}")
    print(
        f"as of {document.get('asOf') or 'unavailable'}; "
        + ", ".join(
            f"{source['id']}={source['status']}" for source in document["sources"]
        )
    )


if __name__ == "__main__":
    main()

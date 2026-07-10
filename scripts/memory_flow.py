#!/usr/bin/env python3
"""Memory-stock flow and market-structure monitor.

This producer deliberately separates observed flow from market-structure
proxies and from behavioral inference.  It cannot see a dealer's inventory or
intent.  Instead it asks whether public evidence is consistent with leverage
washout, institutional absorption, or distribution, and records the evidence
that would falsify each hypothesis.

Live mode uses Yahoo daily OHLCV, Naver's KRX-derived investor table, official
KRX short-sale data, KOFIA leverage balances, KSD SEIBRO securities lending,
FINRA off-exchange daily short-sale-volume files, Nasdaq retail activity, and
quality-gated best-effort Yahoo option chains. ``--no-fetch`` recomputes the
artifact from the private raw cache without touching the network.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import html
import json
import math
import re
import statistics
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

import pandas as pd
import requests

try:
    from scripts.artifact_io import atomic_write_json, atomic_write_text
except ModuleNotFoundError:  # direct ``python scripts/memory_flow.py``
    from artifact_io import atomic_write_json, atomic_write_text


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_UNIVERSE = ROOT / "data" / "memory_flow_universe.json"
DEFAULT_OUT = ROOT / "output" / "memory_flow.json"
DEFAULT_REPORT = ROOT / "output" / "memory_flow_report.md"
DEFAULT_CACHE = ROOT / "output" / "memory_flow_cache.json"

ET = ZoneInfo("America/New_York")
KST = ZoneInfo("Asia/Seoul")
UTC = dt.timezone.utc

FINRA_URL = "https://cdn.finra.org/equity/regsho/daily/CNMSshvol{date}.txt"
NAVER_URL = "https://finance.naver.com/item/frgn.naver?code={code}&page={page}"
NASDAQ_RTAT_URL = "https://api.nasdaq.com/api/quote/list-type-extended/RTAT"
KRX_SHORT_LOADER = "https://data.krx.co.kr/comm/srt/srtLoader/index.cmd?screenId=MDCSTAT300&isuCd={code}"
KRX_JSON_URL = "https://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd"
KRX_SHORT_BLD = "dbms/MDC_OUT/STAT/srt/MDCSTAT30001_OUT"
KOFIA_META_URL = "https://freesis.kofia.or.kr/meta/getMetaDataList.do"
KOFIA_CREDIT_OBJECT = "STATSCU0100000070BO"
KOFIA_FUNDS_OBJECT = "STATSCU0100000060BO"
SEIBRO_LOAN_URL = "https://m.seibro.or.kr/cnts/loan/selectStockLoanDeal.do"

US_OPTION_SYMBOLS = {"MU", "SNDK", "WDC", "STX"}
LEVERAGE_SYMBOLS = {"0193T0.KS", "0195S0.KS", "0192L0.KS"}


def finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def rn(value: Any, digits: int = 4) -> float | None:
    number = finite(value)
    return round(number, digits) if number is not None else None


def pct(new: Any, old: Any) -> float | None:
    new_number, old_number = finite(new), finite(old)
    if new_number is None or old_number in (None, 0):
        return None
    return (new_number / old_number - 1.0) * 100.0


def verified_share_denominator(
    value: Any,
    denominator_as_of: Any,
    source: Any,
    metric_as_of: Any = None,
    valid_through: Any = None,
) -> bool:
    """Require a positive, sourced share count valid on the metric date.

    ``valid_through`` is optional for historical data without a known corporate
    action.  When supplied, it prevents a pre-action denominator from leaking
    into post-action ratios.
    """
    denominator = finite(value)
    source_text = str(source or "")
    try:
        denominator_date = dt.date.fromisoformat(str(denominator_as_of))
        metric_date = dt.date.fromisoformat(str(metric_as_of)) if metric_as_of else None
        expiry_date = dt.date.fromisoformat(str(valid_through)) if valid_through else None
    except (TypeError, ValueError):
        return False
    if denominator is None or denominator <= 0 or not source_text.startswith(("https://", "http://")):
        return False
    if metric_date and denominator_date > metric_date:
        return False
    if expiry_date and (denominator_date > expiry_date or (metric_date and metric_date > expiry_date)):
        return False
    return True


def sanitize(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, (dt.date, dt.datetime, pd.Timestamp)):
        return value.isoformat()
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return sanitize(item())
        except (TypeError, ValueError):
            pass
    if isinstance(value, Mapping):
        return {str(key): sanitize(item_value) for key, item_value in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [sanitize(item_value) for item_value in value]
    return str(value)


def _number(text: str | None) -> float | None:
    if text is None:
        return None
    cleaned = re.sub(r"[^0-9+-.]", "", str(text).replace(",", ""))
    if cleaned in {"", "+", "-", "."}:
        return None
    return finite(cleaned)


def load_json(path: str | Path, default: Any = None) -> Any:
    source = Path(path)
    if not source.is_file():
        return copy.deepcopy(default)
    return json.loads(source.read_text(encoding="utf-8"))


class _TableRows(HTMLParser):
    """Minimal HTML table reader; avoids another runtime dependency."""

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self.row: list[str] | None = None
        self.cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self.row = []
        elif tag in {"td", "th"} and self.row is not None:
            self.cell = []
        elif tag == "br" and self.cell is not None:
            self.cell.append(" ")

    def handle_data(self, data: str) -> None:
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self.cell is not None and self.row is not None:
            self.row.append(" ".join("".join(self.cell).split()))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            if self.row:
                self.rows.append(self.row)
            self.row = None
            self.cell = None


def parse_naver_investor_html(text: str) -> list[dict[str, Any]]:
    parser = _TableRows()
    parser.feed(text)
    rows: list[dict[str, Any]] = []
    for cells in parser.rows:
        if len(cells) < 9 or not re.fullmatch(r"\d{4}\.\d{2}\.\d{2}", cells[0]):
            continue
        institution = _number(cells[5])
        foreign = _number(cells[6])
        close = _number(cells[1])
        volume = _number(cells[4])
        if None in (institution, foreign, close, volume):
            continue
        rows.append({
            "date": cells[0].replace(".", "-"),
            "close": rn(close, 4),
            "returnPct": rn(_number(cells[3]), 4),
            "volume": rn(volume, 0),
            "institutionNetShares": rn(institution, 0),
            "foreignNetShares": rn(foreign, 0),
            # With the two disclosed categories netted out, this is useful as a
            # retail/other residual.  It is not brokerage-tagged retail flow.
            "individualOtherResidualNetShares": rn(-(institution + foreign), 0),
            "foreignHoldingsShares": rn(_number(cells[7]), 0),
            "foreignOwnershipPct": rn(_number(cells[8]), 4),
        })
    return sorted({row["date"]: row for row in rows}.values(), key=lambda row: row["date"])


def fetch_naver_investor(code: str, pages: int = 3, session: requests.Session | None = None) -> list[dict[str, Any]]:
    client = session or requests.Session()
    collected: list[dict[str, Any]] = []
    for page in range(1, pages + 1):
        response = client.get(
            NAVER_URL.format(code=code, page=page),
            headers={"User-Agent": "Mozilla/5.0 portfolio-tracker-memory-flow/1.0"},
            timeout=15,
        )
        response.raise_for_status()
        decoded = response.content.decode("euc-kr", errors="ignore")
        collected.extend(parse_naver_investor_html(decoded))
    return sorted({row["date"]: row for row in collected}.values(), key=lambda row: row["date"])


def parse_krx_short_payload(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for raw in payload.get("OutBlock_1") or []:
        date_text = str(raw.get("TRD_DD") or "").replace("/", "-")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_text):
            continue
        rows.append({
            "date": date_text,
            "shortVolume": rn(_number(raw.get("CVSRTSELL_TRDVOL")), 0),
            "uptickRuleVolume": rn(_number(raw.get("UPTICKRULE_APPL_TRDVOL")), 0),
            "uptickExemptVolume": rn(_number(raw.get("UPTICKRULE_EXCPT_TRDVOL")), 0),
            "reportedNetShortBalanceShares": rn(_number(raw.get("STR_CONST_VAL1")), 0),
            "shortNotionalKrw": rn(_number(raw.get("CVSRTSELL_TRDVAL")), 0),
            "reportedNetShortBalanceKrw": rn(_number(raw.get("STR_CONST_VAL2")), 0),
        })
    return sorted(rows, key=lambda row: row["date"])


def fetch_krx_short(
    code: str,
    isin: str,
    start_date: dt.date,
    end_date: dt.date,
    session: requests.Session | None = None,
) -> list[dict[str, Any]]:
    client = session or requests.Session()
    loader = KRX_SHORT_LOADER.format(code=code)
    headers = {
        "User-Agent": "Mozilla/5.0 portfolio-tracker-memory-flow/1.0",
        "Referer": loader,
        "X-Requested-With": "XMLHttpRequest",
    }
    client.get(loader, headers=headers, timeout=15).raise_for_status()
    response = client.post(
        KRX_JSON_URL,
        data={
            "bld": KRX_SHORT_BLD,
            "locale": "ko_KR",
            "isuCd": isin,
            "strtDd": start_date.strftime("%Y%m%d"),
            "endDd": end_date.strftime("%Y%m%d"),
        },
        headers=headers,
        timeout=20,
    )
    response.raise_for_status()
    return parse_krx_short_payload(response.json())


def parse_seibro_loan_html(text: str) -> list[dict[str, Any]]:
    parser = _TableRows()
    parser.feed(text)
    rows = []
    target_table_seen = False
    for cells in parser.rows:
        if "일자" in cells and "잔고주수" in cells:
            target_table_seen = True
            continue
        if not target_table_seen:
            continue
        if len(cells) < 4 or not re.fullmatch(r"\d{4}/\d{2}/\d{2}", cells[0]):
            continue
        rows.append({
            "date": cells[0].replace("/", "-"),
            "newLoanShares": rn(_number(cells[1]), 0),
            "returnedLoanShares": rn(_number(cells[2]), 0),
            "loanBalanceShares": rn(_number(cells[3]), 0),
        })
    return sorted({row["date"]: row for row in rows}.values(), key=lambda row: row["date"])


def fetch_seibro_loan(
    code: str, name: str, session: requests.Session | None = None,
) -> list[dict[str, Any]]:
    client = session or requests.Session()
    response = client.get(
        SEIBRO_LOAN_URL,
        params={"searchCode": "00", "txt_sch": code, "txt_code": name},
        headers={"User-Agent": "Mozilla/5.0 portfolio-tracker-memory-flow/1.0"},
        timeout=20,
    )
    response.raise_for_status()
    return parse_seibro_loan_html(response.text)


def parse_kofia_credit_payload(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for raw in payload.get("ds1") or []:
        date_text = str(raw.get("TMPV1") or "")
        if not re.fullmatch(r"\d{8}", date_text):
            continue
        rows.append({
            "date": f"{date_text[:4]}-{date_text[4:6]}-{date_text[6:]}",
            "marginCreditTotalMillionKrw": rn(_number(raw.get("TMPV2")), 2),
            "marginCreditKospiMillionKrw": rn(_number(raw.get("TMPV3")), 2),
            "marginCreditKosdaqMillionKrw": rn(_number(raw.get("TMPV4")), 2),
            "stockBorrowCreditTotalMillionKrw": rn(_number(raw.get("TMPV5")), 2),
            "subscriptionLoanMillionKrw": rn(_number(raw.get("TMPV8")), 2),
            "securitiesCollateralLoanMillionKrw": rn(_number(raw.get("TMPV9")), 2),
        })
    return sorted(rows, key=lambda row: row["date"])


def parse_kofia_funds_payload(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for raw in payload.get("ds1") or []:
        date_text = str(raw.get("TMPV1") or "")
        if not re.fullmatch(r"\d{8}", date_text):
            continue
        rows.append({
            "date": f"{date_text[:4]}-{date_text[4:6]}-{date_text[6:]}",
            "customerDepositMillionKrw": rn(_number(raw.get("TMPV2")), 2),
            "derivativesDepositMillionKrw": rn(_number(raw.get("TMPV3")), 2),
            "rpBalanceMillionKrw": rn(_number(raw.get("TMPV4")), 2),
            "uncollectedReceivableMillionKrw": rn(_number(raw.get("TMPV5")), 2),
            "forcedLiquidationMillionKrw": rn(_number(raw.get("TMPV6")), 2),
            "forcedLiquidationRatioPct": rn(_number(raw.get("TMPV7")), 2),
        })
    return sorted(rows, key=lambda row: row["date"])


def fetch_kofia_series(
    object_name: str,
    start_date: dt.date,
    end_date: dt.date,
    session: requests.Session | None = None,
) -> Mapping[str, Any]:
    client = session or requests.Session()
    response = client.post(
        KOFIA_META_URL,
        json={"dmSearch": {
            "tmpV1": "RD",
            "tmpV45": start_date.strftime("%Y%m%d"),
            "tmpV46": end_date.strftime("%Y%m%d"),
            "tmpV40": "1000000",
            "OBJ_NM": object_name,
        }},
        headers={
            "User-Agent": "Mozilla/5.0 portfolio-tracker-memory-flow/1.0",
            "Content-Type": "application/json;charset=UTF-8",
            "Referer": "https://freesis.kofia.or.kr/",
        },
        timeout=20,
    )
    response.raise_for_status()
    return response.json()


def normalize_history(frame: pd.DataFrame) -> list[dict[str, Any]]:
    if frame is None or frame.empty:
        return []
    columns = {str(column).lower(): column for column in frame.columns}
    required = ("open", "high", "low", "close")
    if any(name not in columns for name in required):
        return []
    rows = []
    for index, raw in frame.iterrows():
        row = {name: finite(raw[columns[name]]) for name in required}
        if any(row[name] is None for name in required):
            continue
        stamp = pd.Timestamp(index)
        rows.append({
            "date": stamp.date().isoformat(),
            "open": rn(row["open"], 8),
            "high": rn(row["high"], 8),
            "low": rn(row["low"], 8),
            "close": rn(row["close"], 8),
            "volume": rn(raw[columns["volume"]], 4) if "volume" in columns else None,
        })
    return sorted({row["date"]: row for row in rows}.values(), key=lambda row: row["date"])


def market_clock(symbol: str, now: dt.datetime) -> tuple[dt.date, dt.time, bool]:
    timezone = KST if symbol.endswith(".KS") or symbol == "^KS11" else ET
    local = now.astimezone(timezone)
    # Use the official regular-session closes.  A vendor may publish a daily
    # candle later, but a bar is no longer forming once the exchange session
    # ends (KRX 15:30 KST; US cash equities 16:00 ET).
    close_time = dt.time(15, 30) if timezone == KST else dt.time(16, 0)
    return local.date(), local.time(), local.time() < close_time


def closed_and_live_rows(
    rows: Iterable[Mapping[str, Any]], symbol: str, now: dt.datetime, as_of: str | None = None
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    ordered = sorted((dict(row) for row in rows if row.get("date")), key=lambda row: row["date"])
    if as_of:
        ordered = [row for row in ordered if str(row["date"]) <= as_of]
    if not ordered:
        return [], None
    local_date, _, session_not_final = market_clock(symbol, now)
    live = None
    if ordered[-1]["date"] == local_date.isoformat() and session_not_final:
        live = ordered.pop()
    return ordered, live


def fetch_price_cache(config: Mapping[str, Any], period: str, now: dt.datetime) -> dict[str, list[dict[str, Any]]]:
    import warnings
    warnings.filterwarnings("ignore")
    import yfinance as yf

    cache_dir = ROOT / "output" / "yfinance_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    setter = getattr(yf, "set_tz_cache_location", None)
    if setter:
        setter(str(cache_dir))
    symbols = {item["symbol"] for item in config.get("instruments") or []}
    symbols.update(config.get("benchmarks") or [])
    symbols.add((config.get("skHynixAdr") or {}).get("currencyPair", "KRW=X"))
    if now.astimezone(ET).date() >= dt.date(2026, 7, 10):
        adr = config.get("skHynixAdr") or {}
        symbols.update(filter(None, (adr.get("whenIssuedSymbol"), adr.get("regularSymbol"))))

    output: dict[str, list[dict[str, Any]]] = {}
    for symbol in sorted(filter(None, symbols)):
        try:
            history = yf.Ticker(symbol).history(
                period=period, interval="1d", auto_adjust=True, actions=False,
            )
            rows = normalize_history(history)
            if rows:
                output[symbol] = rows
        except Exception:
            continue
    return output


def fetch_short_interest(symbols: Iterable[str]) -> dict[str, dict[str, Any]]:
    import warnings
    warnings.filterwarnings("ignore")
    import yfinance as yf

    output = {}
    for symbol in symbols:
        try:
            info = yf.Ticker(symbol).get_info()
            timestamp = finite(info.get("dateShortInterest"))
            date = dt.datetime.fromtimestamp(timestamp, UTC).date().isoformat() if timestamp else None
            current = finite(info.get("sharesShort"))
            prior = finite(info.get("sharesShortPriorMonth"))
            output[symbol] = {
                "asOf": date,
                "sharesShort": rn(current, 0),
                "sharesShortPriorMonth": rn(prior, 0),
                "changePct": rn(pct(current, prior), 2),
                "shortPercentOfFloat": rn((finite(info.get("shortPercentOfFloat")) or 0) * 100, 2),
                "daysToCover": rn(info.get("shortRatio"), 2),
                "source": "Yahoo profile (secondary; exchange short-interest date)",
            }
        except Exception:
            continue
    return output


def parse_finra_file(text: str, wanted: set[str]) -> list[dict[str, Any]]:
    rows = []
    for line in text.splitlines()[1:]:
        parts = line.strip().split("|")
        if len(parts) < 5 or parts[1] not in wanted:
            continue
        short, exempt, total = map(finite, (parts[2], parts[3], parts[4]))
        if short is None or total in (None, 0):
            continue
        rows.append({
            "date": f"{parts[0][0:4]}-{parts[0][4:6]}-{parts[0][6:8]}",
            "symbol": parts[1],
            "shortVolume": rn(short, 6),
            "shortExemptVolume": rn(exempt, 6),
            "reportedTotalVolume": rn(total, 6),
            "shortVolumePct": rn(short / total * 100.0, 4),
        })
    return rows


def fetch_finra_short_volume(
    symbols: Iterable[str], end_date: dt.date, target_sessions: int = 25,
    session: requests.Session | None = None,
) -> dict[str, list[dict[str, Any]]]:
    wanted = set(symbols)
    output = {symbol: [] for symbol in wanted}
    client = session or requests.Session()
    for offset in range(65):
        day = end_date - dt.timedelta(days=offset)
        if day.weekday() >= 5:
            continue
        try:
            response = client.get(
                FINRA_URL.format(date=day.strftime("%Y%m%d")),
                headers={"User-Agent": "portfolio-tracker-memory-flow/1.0"},
                timeout=12,
            )
            if response.status_code != 200:
                continue
            for row in parse_finra_file(response.text, wanted):
                output[row["symbol"]].append(row)
        except requests.RequestException:
            continue
        if all(len(rows) >= target_sessions for rows in output.values()):
            break
    return {symbol: sorted(rows, key=lambda row: row["date"])[-target_sessions:]
            for symbol, rows in output.items() if rows}


def parse_nasdaq_retail_tracker(payload: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    data = payload.get("data") or {}
    date_text = str(data.get("date") or "")
    match = re.search(r"([A-Z][a-z]{2} \d{1,2}, \d{4})", date_text)
    as_of = None
    if match:
        try:
            as_of = dt.datetime.strptime(match.group(1), "%b %d, %Y").date().isoformat()
        except ValueError:
            pass
    output = {}
    for row in ((data.get("table") or {}).get("rows") or []):
        ticker = row.get("ticker") or []
        symbol = str(ticker[0] if isinstance(ticker, list) and ticker else ticker).upper()
        sentiment_text = str(row.get("sentiment") or "")
        sentiment_match = re.search(r"(-?\d+(?:\.\d+)?)", sentiment_text)
        if not symbol:
            continue
        output[symbol] = {
            "asOf": as_of,
            "activityPct": rn(_number(row.get("activity")), 2),
            "sentiment": sentiment_text or None,
            "sentimentScore": rn(sentiment_match.group(1), 1) if sentiment_match else None,
            "source": "Nasdaq Retail Trading Activity Tracker",
            "directness": "observed_modelled_retail_flow",
            "coverageCaveat": "Only the daily top five by percent of retail equity flow are returned; absence is not neutral flow.",
        }
    return output


def fetch_nasdaq_retail_tracker(session: requests.Session | None = None) -> dict[str, dict[str, Any]]:
    client = session or requests.Session()
    response = client.get(
        NASDAQ_RTAT_URL,
        headers={
            "User-Agent": "Mozilla/5.0 portfolio-tracker-memory-flow/1.0",
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://www.nasdaq.com/",
        },
        timeout=15,
    )
    response.raise_for_status()
    return parse_nasdaq_retail_tracker(response.json())


def _option_value(raw: Mapping[str, Any], key: str) -> float | None:
    value = raw.get(key)
    return finite(value)


def summarize_option_frames(
    symbol: str,
    spot: float,
    expiries: Iterable[tuple[str, Any, Any]],
    generated_at: str,
) -> dict[str, Any]:
    call_oi = put_oi = call_volume = put_volume = 0.0
    eligible = oi_known = positive_oi = quote_known = 0
    gamma_by_strike: dict[float, float] = {}
    call_oi_by_strike: dict[float, float] = {}
    put_oi_by_strike: dict[float, float] = {}
    atm_ivs: list[float] = []
    expiry_count = 0
    row_count = 0

    for expiry, calls, puts in expiries:
        expiry_count += 1
        days = max((dt.date.fromisoformat(expiry) - dt.date.fromisoformat(generated_at[:10])).days, 1)
        years = days / 365.0
        for option_type, frame in (("call", calls), ("put", puts)):
            if frame is None or getattr(frame, "empty", True):
                continue
            for _, series in frame.iterrows():
                raw = dict(series)
                strike = _option_value(raw, "strike")
                if strike is None or strike <= 0:
                    continue
                row_count += 1
                volume = _option_value(raw, "volume")
                oi_raw = raw.get("openInterest")
                oi = finite(oi_raw)
                bid, ask = _option_value(raw, "bid"), _option_value(raw, "ask")
                iv = _option_value(raw, "impliedVolatility")
                if option_type == "call":
                    call_volume += volume or 0.0
                    call_oi += oi or 0.0
                else:
                    put_volume += volume or 0.0
                    put_oi += oi or 0.0
                if 0.6 * spot <= strike <= 1.4 * spot:
                    eligible += 1
                    if oi is not None:
                        oi_known += 1
                    if oi and oi > 0:
                        positive_oi += 1
                    if bid is not None and ask is not None and ask >= bid:
                        quote_known += 1
                    if iv and 0.85 * spot <= strike <= 1.15 * spot:
                        atm_ivs.append(iv)
                    if oi and iv:
                        vol_time = iv * math.sqrt(years)
                        if vol_time > 0:
                            d1 = (math.log(spot / strike) + (0.04 + 0.5 * iv * iv) * years) / vol_time
                            gamma = math.exp(-0.5 * d1 * d1) / math.sqrt(2 * math.pi) / (spot * vol_time)
                            gex = gamma * oi * 100.0 * spot * spot * 0.01
                            gamma_by_strike[strike] = gamma_by_strike.get(strike, 0.0) + gex
                            target = call_oi_by_strike if option_type == "call" else put_oi_by_strike
                            target[strike] = target.get(strike, 0.0) + oi

    coverage = oi_known / eligible if eligible else 0.0
    top_gamma = sorted(gamma_by_strike.items(), key=lambda pair: pair[1], reverse=True)[:5]
    total_oi = call_oi + put_oi
    total_volume = call_volume + put_volume
    median_iv_pct = statistics.median(atm_ivs) * 100 if atm_ivs else None
    plausible_oi_scale = total_oi >= max(100, total_volume * 0.01)
    plausible_iv = median_iv_pct is not None and 5 <= median_iv_pct <= 500
    quality = "usable_proxy" if (
        coverage >= 0.7 and positive_oi >= 20 and plausible_oi_scale and plausible_iv
    ) else "insufficient_open_interest_quality"
    return {
        "symbol": symbol,
        "asOf": generated_at,
        "source": "Yahoo option chain (best effort; snapshot, not signed customer flow)",
        "expiryCount": expiry_count,
        "rowCount": row_count,
        "openInterestCoveragePct": rn(coverage * 100, 1),
        "positiveOpenInterestRows": positive_oi,
        "quoteCoveragePct": rn(quote_known / eligible * 100, 1) if eligible else 0.0,
        "dataQuality": quality,
        "callOpenInterest": rn(call_oi, 0),
        "putOpenInterest": rn(put_oi, 0),
        "putCallOpenInterestRatio": rn(put_oi / call_oi, 3) if quality == "usable_proxy" and call_oi else None,
        "callVolume": rn(call_volume, 0),
        "putVolume": rn(put_volume, 0),
        "putCallVolumeRatio": rn(put_volume / call_volume, 3) if call_volume else None,
        "medianAtmIvPct": rn(median_iv_pct, 1) if plausible_iv else None,
        "unsignedGammaNotionalPer1Pct": rn(sum(gamma_by_strike.values()), 0) if quality == "usable_proxy" else None,
        "topUnsignedGammaStrikes": [
            {"strike": rn(strike, 4), "notionalPer1Pct": rn(value, 0)}
            for strike, value in top_gamma
        ] if quality == "usable_proxy" else [],
        "callOiWall": rn(max(call_oi_by_strike, key=call_oi_by_strike.get), 4)
        if quality == "usable_proxy" and call_oi_by_strike else None,
        "putOiWall": rn(max(put_oi_by_strike, key=put_oi_by_strike.get), 4)
        if quality == "usable_proxy" and put_oi_by_strike else None,
        "dealerScenarios": {
            "dealersNetLongGamma": "hedging would tend to damp moves and pin price near large gamma strikes",
            "dealersNetShortGamma": "hedging would tend to chase moves and amplify realized volatility",
            "directionKnown": False,
        },
    }


def fetch_options(symbols: Iterable[str], now: dt.datetime, max_dte: int = 45, max_expiries: int = 6) -> dict[str, dict[str, Any]]:
    import warnings
    warnings.filterwarnings("ignore")
    import yfinance as yf

    output = {}
    generated_at = now.astimezone(ET).isoformat(timespec="seconds")
    for symbol in symbols:
        try:
            ticker = yf.Ticker(symbol)
            history = ticker.history(period="5d", interval="1d", auto_adjust=True)
            if history is None or history.empty:
                continue
            spot = finite(history["Close"].dropna().iloc[-1])
            if not spot:
                continue
            selected = []
            for expiry in ticker.options:
                days = (dt.date.fromisoformat(expiry) - now.astimezone(ET).date()).days
                if 0 <= days <= max_dte:
                    selected.append(expiry)
                if len(selected) >= max_expiries:
                    break
            frames = []
            for expiry in selected:
                chain = ticker.option_chain(expiry)
                frames.append((expiry, chain.calls, chain.puts))
            output[symbol] = summarize_option_frames(symbol, spot, frames, generated_at)
        except Exception:
            continue
    return output


def validate_cached_option_summary(summary: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not summary:
        return None
    result = copy.deepcopy(dict(summary))
    total_oi = (finite(result.get("callOpenInterest")) or 0) + (finite(result.get("putOpenInterest")) or 0)
    total_volume = (finite(result.get("callVolume")) or 0) + (finite(result.get("putVolume")) or 0)
    positive_rows = int(finite(result.get("positiveOpenInterestRows")) or 0)
    iv = finite(result.get("medianAtmIvPct"))
    plausible = (
        total_oi >= max(100, total_volume * 0.01)
        and positive_rows >= 20
        and iv is not None and 5 <= iv <= 500
    )
    if not plausible:
        result["dataQuality"] = "insufficient_open_interest_quality"
        result["putCallOpenInterestRatio"] = None
        result["medianAtmIvPct"] = None
        result["unsignedGammaNotionalPer1Pct"] = None
        result["topUnsignedGammaStrikes"] = []
        result["callOiWall"] = None
        result["putOiWall"] = None
    return result


def _series(rows: list[dict[str, Any]], field: str) -> pd.Series:
    return pd.Series([finite(row.get(field)) for row in rows], dtype="float64")


def _wilder_atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """Return Wilder ATR with no value before the full seed window."""
    true_range = pd.concat(
        [(high - low), (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1,
    ).max(axis=1)
    result = pd.Series(float("nan"), index=close.index, dtype="float64")
    if len(true_range) < period:
        return result
    seed = true_range.iloc[:period].mean()
    result.iloc[period - 1] = seed
    previous = seed
    for index in range(period, len(true_range)):
        previous = ((period - 1) * previous + true_range.iloc[index]) / period
        result.iloc[index] = previous
    return result


def _wilder_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Return standard seeded Wilder RSI and treat a flat window as neutral."""
    result = pd.Series(float("nan"), index=close.index, dtype="float64")
    if len(close) <= period:
        return result
    delta = close.diff()
    gains = delta.clip(lower=0)
    losses = -delta.clip(upper=0)
    avg_gain = gains.iloc[1 : period + 1].mean()
    avg_loss = losses.iloc[1 : period + 1].mean()

    def value(gain: float, loss: float) -> float:
        if gain == 0 and loss == 0:
            return 50.0
        if loss == 0:
            return 100.0
        if gain == 0:
            return 0.0
        return 100.0 - 100.0 / (1.0 + gain / loss)

    result.iloc[period] = value(avg_gain, avg_loss)
    for index in range(period + 1, len(close)):
        avg_gain = ((period - 1) * avg_gain + gains.iloc[index]) / period
        avg_loss = ((period - 1) * avg_loss + losses.iloc[index]) / period
        result.iloc[index] = value(avg_gain, avg_loss)
    return result


def technical_metrics(
    symbol: str,
    rows: list[dict[str, Any]],
    live: dict[str, Any] | None = None,
    benchmark_return5: float | None = None,
) -> dict[str, Any]:
    # EMA34 is a named regime input, while CMF/RVOL require 20 prior sessions.
    # Refuse a signal until all of those indicators have a defensible sample.
    if len(rows) < 35:
        return {
            "symbol": symbol,
            "available": False,
            "historyBars": len(rows),
            "minimumHistoryBars": 35,
            "reason": "fewer than 35 closed daily bars",
        }
    open_price, close, high, low, volume = (
        _series(rows, field) for field in ("open", "close", "high", "low", "volume")
    )
    if any(series.isna().any() for series in (open_price, close, high, low)):
        return {"symbol": symbol, "available": False, "historyBars": len(rows), "reason": "non-finite OHLC data"}
    if bool((close <= 0).any()) or bool((high < low).any()) or bool((high < open_price).any()) \
            or bool((high < close).any()) or bool((low > open_price).any()) or bool((low > close).any()):
        return {"symbol": symbol, "available": False, "historyBars": len(rows), "reason": "invalid OHLC geometry"}
    if volume.tail(21).isna().any() or bool((volume.tail(21) < 0).any()) or volume.tail(21).sum() <= 0:
        return {"symbol": symbol, "available": False, "historyBars": len(rows), "reason": "insufficient recent volume data"}
    returns = close.pct_change()
    ema8 = close.ewm(span=8, adjust=False).mean()
    ema21 = close.ewm(span=21, adjust=False).mean()
    ema34 = close.ewm(span=34, adjust=False).mean()
    atr14 = _wilder_atr(high, low, close)
    rsi = _wilder_rsi(close)
    spread = (high - low).replace(0, float("nan"))
    clv = ((2 * close - high - low) / spread).fillna(0)
    cmf_window = 20
    cmf = (clv.tail(cmf_window) * volume.tail(cmf_window)).sum() / max(volume.tail(cmf_window).sum(), 1)
    obv = (returns.fillna(0).apply(lambda value: 1 if value > 0 else (-1 if value < 0 else 0)) * volume.fillna(0)).cumsum()
    obv_window = 20
    obv_slope = (obv.iloc[-1] - obv.iloc[-obv_window - 1]) / max(volume.tail(obv_window).sum(), 1)
    rolling_volume = volume.shift(1).rolling(20, min_periods=5).mean()
    rvol = volume / rolling_volume
    shock_window = 20
    shock_slice = returns.tail(shock_window)
    shock_index = int(shock_slice.idxmin())
    shock_row = rows[shock_index]

    def return_for(days: int) -> float | None:
        return rn(pct(close.iloc[-1], close.iloc[-days - 1]), 2) if len(close) > days else None

    ret5 = return_for(5)
    high20 = high.tail(20).max()
    low20 = low.tail(20).min()
    high5 = high.tail(5).max()
    low5 = low.tail(5).min()
    current = close.iloc[-1]
    rsi_value = finite(rsi.iloc[-1])
    metrics = {
        "symbol": symbol,
        "available": True,
        "historyBars": len(rows),
        "minimumHistoryBars": 35,
        "priceAsOf": rows[-1]["date"],
        "close": rn(current, 4),
        "livePrice": rn((live or {}).get("close"), 4),
        "livePriceAsOf": (live or {}).get("date"),
        "barStatus": "live_partial_available" if live else "closed",
        "ret1dPct": return_for(1),
        "ret3dPct": return_for(3),
        "ret5dPct": ret5,
        "ret10dPct": return_for(10),
        "ret20dPct": return_for(20),
        "relative5dPct": rn(ret5 - benchmark_return5, 2)
        if ret5 is not None and benchmark_return5 is not None else None,
        "ema8": rn(ema8.iloc[-1], 4),
        "ema21": rn(ema21.iloc[-1], 4),
        "ema34": rn(ema34.iloc[-1], 4),
        "sma50": rn(close.tail(50).mean(), 4) if len(close) >= 50 else None,
        "sma200": rn(close.tail(200).mean(), 4) if len(close) >= 200 else None,
        "vsEma8Pct": rn(pct(current, ema8.iloc[-1]), 2),
        "vsEma21Pct": rn(pct(current, ema21.iloc[-1]), 2),
        "ema21Slope5Pct": rn(pct(ema21.iloc[-1], ema21.iloc[-6]), 2) if len(ema21) > 5 else None,
        "atr14": rn(atr14.iloc[-1], 4),
        "atr14Pct": rn((atr14.iloc[-1] / current) * 100, 2),
        "rsi14": rn(rsi_value, 1),
        "rvol20": rn(rvol.iloc[-1], 2),
        "avgVolume5Vs20": rn(volume.tail(5).mean() / max(volume.tail(min(20, len(volume))).mean(), 1), 2),
        "cmf20": rn(cmf, 3),
        "latestCloseLocation": rn(clv.iloc[-1], 3),
        "avgCloseLocation5": rn(clv.tail(5).mean(), 3),
        "latestGapPct": rn(pct(open_price.iloc[-1], close.iloc[-2]), 2),
        "latestHighToClosePct": rn(pct(close.iloc[-1], high.iloc[-1]), 2),
        "obvSlope20": rn(obv_slope, 3),
        "drawdownFrom20dHighPct": rn(pct(current, high20), 2),
        "bounceFrom20dLowPct": rn(pct(current, low20), 2),
        "shock": {
            "date": shock_row["date"],
            "returnPct": rn(shock_slice.loc[shock_index] * 100, 2),
            "rvol20": rn(rvol.loc[shock_index], 2),
        },
        "levels": {
            "support5d": rn(low5, 4),
            "invalidation20dLow": rn(low20, 4),
            "reclaimEma8": rn(ema8.iloc[-1], 4),
            "confirmEma21": rn(ema21.iloc[-1], 4),
            "firstSupply5dHigh": rn(high5, 4),
            "stretchSupply20dHigh": rn(high20, 4),
        },
    }
    return metrics


def classify_regime(metrics: Mapping[str, Any]) -> str:
    if not metrics.get("available"):
        return "BLOCK_DATA"
    values = {
        name: finite(metrics.get(name))
        for name in ("vsEma8Pct", "vsEma21Pct", "ema21Slope5Pct", "drawdownFrom20dHighPct", "ret3dPct", "rsi14")
    }
    if any(value is None for value in values.values()):
        return "BLOCK_DATA"
    vs8 = values["vsEma8Pct"]
    vs21 = values["vsEma21Pct"]
    slope = values["ema21Slope5Pct"]
    drawdown = values["drawdownFrom20dHighPct"]
    ret3 = values["ret3dPct"]
    rsi = values["rsi14"]
    if vs21 > 0 and vs8 > 0 and slope > 0:
        return "BULL_TREND"
    if vs21 < 0 and drawdown <= -15 and rsi < 42:
        return "RELIEF_RALLY" if ret3 > 3 else "LIQUIDATION"
    if vs21 < 0 and vs8 > 0:
        return "REPAIR"
    if vs21 < 0:
        return "TREND_BREAK"
    return "CHOP"


def descriptive_price_snapshot(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return non-signal window statistics for newly listed instruments."""
    usable = [row for row in rows if finite(row.get("close")) is not None]
    if len(usable) < 5:
        return None
    recent = usable[-20:]
    close = finite(usable[-1].get("close"))
    highs = [finite(row.get("high")) for row in recent]
    highs = [value for value in highs if value is not None]
    if close is None or not highs:
        return None
    return {
        "asOf": usable[-1].get("date"),
        "historyBars": len(usable),
        "close": rn(close, 4),
        "drawdownFrom20dHighPct": rn(pct(close, max(highs)), 2),
        "signalEligible": False,
        "caveat": "Descriptive price pain only; a short listing history is not used for regime confirmation.",
    }


def flow_summary(rows: list[dict[str, Any]], as_of: str | None = None) -> dict[str, Any] | None:
    usable = [row for row in rows if not as_of or row["date"] <= as_of]
    if not usable:
        return None
    result: dict[str, Any] = {
        "asOf": usable[-1]["date"],
        "source": "Naver Finance investor table (KRX-derived secondary source)",
        "directness": "observed_category_flow_secondary_source",
        "residualCaveat": "individualOtherResidual is the negative of institution plus foreign; it is not broker-tagged retail flow",
    }
    fields = ("institutionNetShares", "foreignNetShares", "individualOtherResidualNetShares")
    for window in (1, 5, 20):
        selected = usable[-window:]
        result[f"{window}d"] = {
            field: rn(sum(finite(row.get(field)) or 0 for row in selected), 0)
            for field in fields
        }
        result[f"{window}d"].update({
            field.replace("Shares", "NotionalKrw"): rn(sum(
                (finite(row.get(field)) or 0) * (finite(row.get("close")) or 0)
                for row in selected
            ), 0) for field in fields
        })
    latest_pct = finite(usable[-1].get("foreignOwnershipPct"))
    for window in (5, 20):
        observation_count = min(len(usable), window + 1)
        complete = len(usable) >= window + 1
        earlier = finite(usable[-window - 1].get("foreignOwnershipPct")) if complete else None
        result[f"foreignOwnershipChange{window}dPp"] = rn(latest_pct - earlier, 3) \
            if latest_pct is not None and earlier is not None else None
        result[f"foreignOwnershipChange{window}dObservations"] = observation_count
        result[f"foreignOwnershipChange{window}dComplete"] = complete
    result["foreignOwnershipPct"] = rn(latest_pct, 3)
    return result


def finra_summary(rows: list[dict[str, Any]], as_of: str | None = None) -> dict[str, Any] | None:
    usable = [row for row in rows if not as_of or row["date"] <= as_of]
    if not usable:
        return None
    ratios = [finite(row.get("shortVolumePct")) for row in usable]
    ratios = [value for value in ratios if value is not None]
    if not ratios:
        return None
    avg20 = statistics.mean(ratios[-20:])
    deviation = statistics.pstdev(ratios[-20:]) if len(ratios[-20:]) > 1 else 0
    return {
        "asOf": usable[-1]["date"],
        "latestPct": rn(ratios[-1], 1),
        "avg5Pct": rn(statistics.mean(ratios[-5:]), 1),
        "avg20Pct": rn(avg20, 1),
        "latestZ20": rn((ratios[-1] - avg20) / deviation, 2) if deviation else 0.0,
        "sessions": len(ratios),
        "source": "FINRA consolidated daily short-sale-volume file",
        "directness": "observed_off_exchange_trade_marking",
        "caveat": "This is off-exchange short-sale volume, not short interest and not a directional position change.",
    }


def krx_short_summary(
    rows: list[dict[str, Any]], price_rows: list[dict[str, Any]],
    shares_outstanding: float | None = None, as_of: str | None = None,
    shares_outstanding_as_of: str | None = None,
    shares_outstanding_source: str | None = None,
    shares_outstanding_valid_through: str | None = None,
) -> dict[str, Any] | None:
    """Summarize official KRX short transactions without treating them as positions."""
    usable = [dict(row) for row in rows if row.get("date") and (not as_of or row["date"] <= as_of)]
    price_volume = {
        row.get("date"): finite(row.get("volume"))
        for row in price_rows if row.get("date") and (not as_of or row["date"] <= as_of)
    }
    observations = []
    for row in usable:
        short_volume = finite(row.get("shortVolume"))
        total_volume = price_volume.get(row.get("date"))
        if short_volume is None or total_volume in (None, 0):
            continue
        observations.append({**row, "marketVolume": total_volume,
                             "shortTransactionPct": short_volume / total_volume * 100.0})
    if not observations:
        return None

    recent = observations[-20:]
    short_total = sum(finite(row.get("shortVolume")) or 0 for row in recent)
    volume_total = sum(finite(row.get("marketVolume")) or 0 for row in recent)
    ratios = [finite(row.get("shortTransactionPct")) for row in recent]
    ratios = [value for value in ratios if value is not None]
    average = statistics.mean(ratios) if ratios else None
    deviation = statistics.pstdev(ratios) if len(ratios) > 1 else 0.0
    latest_ratio = finite(observations[-1].get("shortTransactionPct"))
    balance_rows = [
        row for row in usable if finite(row.get("reportedNetShortBalanceShares")) is not None
    ]
    balance = balance_rows[-1] if balance_rows else {}
    balance_shares = finite(balance.get("reportedNetShortBalanceShares"))
    outstanding = finite(shares_outstanding)
    denominator_metric_date = balance.get("date") or observations[-1]["date"]
    denominator_verified = verified_share_denominator(
        outstanding, shares_outstanding_as_of, shares_outstanding_source,
        denominator_metric_date, shares_outstanding_valid_through,
    )
    return {
        "asOf": observations[-1]["date"],
        "latestShortVolumeShares": rn(observations[-1].get("shortVolume"), 0),
        "latestShortTransactionPct": rn(latest_ratio, 2),
        "weighted20dShortTransactionPct": rn(short_total / volume_total * 100.0, 2)
        if volume_total else None,
        "average20dDailyPct": rn(average, 2),
        "latestZ20": rn((latest_ratio - average) / deviation, 2)
        if latest_ratio is not None and average is not None and deviation else 0.0,
        "reportedNetShortBalanceAsOf": balance.get("date"),
        "reportedNetShortBalanceShares": rn(balance_shares, 0),
        "reportedNetShortBalancePctOutstanding": rn(balance_shares / outstanding * 100.0, 4)
        if balance_shares is not None and denominator_verified else None,
        "sharesOutstandingDenominator": rn(outstanding, 0) if denominator_verified else None,
        "sharesOutstandingAsOf": shares_outstanding_as_of if denominator_verified else None,
        "sharesOutstandingValidThrough": shares_outstanding_valid_through if denominator_verified else None,
        "sharesOutstandingSource": shares_outstanding_source if denominator_verified else None,
        "denominatorVerified": denominator_verified,
        "source": "KRX short-selling transaction and reported net-short-balance table",
        "directness": "official_transaction_volume_and_threshold_reported_balance",
        "caveat": (
            "Short transaction volume is not a new short position. The reported net-short balance is T+2 "
            "and covers positions subject to Korean reporting thresholds, not every borrow or hedge."
        ),
    }


def securities_lending_summary(
    rows: list[dict[str, Any]], shares_outstanding: float | None = None,
    as_of: str | None = None, shares_outstanding_as_of: str | None = None,
    shares_outstanding_source: str | None = None,
    shares_outstanding_valid_through: str | None = None,
) -> dict[str, Any] | None:
    usable = [dict(row) for row in rows if row.get("date") and (not as_of or row["date"] <= as_of)]
    usable = [row for row in usable if finite(row.get("loanBalanceShares")) is not None]
    if not usable:
        return None
    latest = usable[-1]
    latest_balance = finite(latest.get("loanBalanceShares"))
    outstanding = finite(shares_outstanding)
    denominator_verified = verified_share_denominator(
        outstanding, shares_outstanding_as_of, shares_outstanding_source,
        latest.get("date"), shares_outstanding_valid_through,
    )
    ytd = [row for row in usable if str(row.get("date", ""))[:4] == str(latest.get("date", ""))[:4]]
    return {
        "asOf": latest.get("date"),
        "loanBalanceShares": rn(latest_balance, 0),
        "loanBalancePctOutstanding": rn(latest_balance / outstanding * 100.0, 3)
        if latest_balance is not None and denominator_verified else None,
        "sharesOutstandingDenominator": rn(outstanding, 0) if denominator_verified else None,
        "sharesOutstandingAsOf": shares_outstanding_as_of if denominator_verified else None,
        "sharesOutstandingValidThrough": shares_outstanding_valid_through if denominator_verified else None,
        "sharesOutstandingSource": shares_outstanding_source if denominator_verified else None,
        "denominatorVerified": denominator_verified,
        "newLoanShares": rn(latest.get("newLoanShares"), 0),
        "returnedLoanShares": rn(latest.get("returnedLoanShares"), 0),
        "loanBalance5dChangePct": _change_for_sessions(usable, "loanBalanceShares", 5),
        "loanBalance20dChangePct": _change_for_sessions(usable, "loanBalanceShares", 20),
        "loanBalanceYtdChangePct": rn(pct(
            latest_balance, (ytd[0] if ytd else usable[0]).get("loanBalanceShares"),
        ), 2),
        "source": "Korea Securities Depository SEIBRO stock-loan balance table",
        "directness": "official_securities_lending_balance",
        "caveat": (
            "Borrowed shares are inventory availability, not synonymous with directional short interest; "
            "they can support market making, settlement, arbitrage, hedging, or sub-threshold shorts."
        ),
    }


def _change_for_sessions(rows: list[dict[str, Any]], field: str, sessions: int) -> float | None:
    values = [(row.get("date"), finite(row.get(field))) for row in rows]
    values = [(date, value) for date, value in values if value is not None]
    if len(values) < 2:
        return None
    baseline = values[max(0, len(values) - sessions - 1)][1]
    return rn(pct(values[-1][1], baseline), 2)


def kofia_leverage_summary(
    credit_rows: list[dict[str, Any]], funds_rows: list[dict[str, Any]],
    as_of: str | None = None,
) -> dict[str, Any] | None:
    """Combine official market-wide margin credit, deposits, and forced liquidations."""
    credit = [dict(row) for row in credit_rows if row.get("date") and (not as_of or row["date"] <= as_of)]
    funds = [dict(row) for row in funds_rows if row.get("date") and (not as_of or row["date"] <= as_of)]
    credit_by_date = {row["date"]: row for row in credit}
    funds_by_date = {row["date"]: row for row in funds}
    common_dates = sorted(set(credit_by_date) & set(funds_by_date))
    if not common_dates:
        return None
    date = common_dates[-1]
    credit = [credit_by_date[item] for item in common_dates]
    funds = [funds_by_date[item] for item in common_dates]
    current_credit, current_funds = credit[-1], funds[-1]

    total_field = "marginCreditTotalMillionKrw"
    kospi_field = "marginCreditKospiMillionKrw"
    deposits_field = "customerDepositMillionKrw"
    recent_credit = [row for row in credit[-30:] if finite(row.get(total_field)) is not None]
    recent_kospi = [row for row in credit[-30:] if finite(row.get(kospi_field)) is not None]
    recent_deposits = [row for row in funds[-30:] if finite(row.get(deposits_field)) is not None]
    peak_total = max(recent_credit, key=lambda row: finite(row.get(total_field))) if recent_credit else {}
    peak_kospi = max(recent_kospi, key=lambda row: finite(row.get(kospi_field))) if recent_kospi else {}
    peak_deposits = max(recent_deposits, key=lambda row: finite(row.get(deposits_field))) if recent_deposits else {}

    total = finite(current_credit.get(total_field))
    kospi = finite(current_credit.get(kospi_field))
    deposits = finite(current_funds.get(deposits_field))
    ratio = total / deposits * 100.0 if total is not None and deposits else None
    ratio_rows = []
    for credit_row, funds_row in zip(credit, funds):
        credit_value = finite(credit_row.get(total_field))
        deposit_value = finite(funds_row.get(deposits_field))
        ratio_rows.append({
            "date": credit_row["date"],
            "marginToDepositsPct": credit_value / deposit_value * 100.0
            if credit_value is not None and deposit_value else None,
        })
    forced = [row for row in funds[-20:] if finite(row.get("forcedLiquidationMillionKrw")) is not None]
    forced_max = max(forced, key=lambda row: finite(row.get("forcedLiquidationMillionKrw"))) if forced else {}
    total_from_peak = pct(total, peak_total.get(total_field))
    ratio_change20 = None
    ratio_values = [finite(row.get("marginToDepositsPct")) for row in ratio_rows]
    ratio_values = [value for value in ratio_values if value is not None]
    if len(ratio_values) >= 2:
        ratio_change20 = ratio_values[-1] - ratio_values[max(0, len(ratio_values) - 21)]
    current_forced = finite(current_funds.get("forcedLiquidationMillionKrw"))
    return {
        "asOf": date,
        "totalMarginCreditTrillionKrw": rn(total / 1_000_000, 3) if total is not None else None,
        "totalMarginCredit5dChangePct": _change_for_sessions(credit, total_field, 5),
        "totalMarginCredit20dChangePct": _change_for_sessions(credit, total_field, 20),
        "totalMarginCreditFrom30dPeakPct": rn(total_from_peak, 2),
        "totalMarginCreditPeak30Date": peak_total.get("date"),
        "kospiMarginCreditTrillionKrw": rn(kospi / 1_000_000, 3) if kospi is not None else None,
        "kospiMarginCreditFrom30dPeakPct": rn(pct(kospi, peak_kospi.get(kospi_field)), 2),
        "customerDepositsTrillionKrw": rn(deposits / 1_000_000, 3) if deposits is not None else None,
        "customerDepositsFrom30dPeakPct": rn(pct(deposits, peak_deposits.get(deposits_field)), 2),
        "marginCreditToCustomerDepositsPct": rn(ratio, 2),
        "marginCreditToDeposits20dChangePp": rn(ratio_change20, 2),
        "forcedLiquidationBillionKrw": rn(current_forced / 1_000, 3)
        if current_forced is not None else None,
        "forcedLiquidationRatioPct": rn(current_funds.get("forcedLiquidationRatioPct"), 2),
        "forcedLiquidationDataStatus": "available" if current_forced is not None else "missing_latest",
        "max20dForcedLiquidationBillionKrw": rn(
            (finite(forced_max.get("forcedLiquidationMillionKrw")) or 0) / 1_000, 3
        ) if forced_max else None,
        "max20dForcedLiquidationDate": forced_max.get("date"),
        "marketWideCreditClearingConfirmed": bool(
            total_from_peak is not None and total_from_peak <= -15
            and ratio_change20 is not None and ratio_change20 < 0
        ),
        "clearingRule": "At least a 15% decline from the recent margin-credit peak plus a falling credit/deposit ratio.",
        "source": "KOFIA FreeSIS official securities-company customer-funds and credit series",
        "directness": "official_market_wide_balance_series",
        "caveat": "Market-wide KOSPI/KOSDAQ balances do not isolate SK hynix or prove who was liquidated.",
    }


def score_symbol(
    metrics: Mapping[str, Any],
    flows: Mapping[str, Any] | None,
    retail: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if not metrics.get("available"):
        return {"washoutPainScore": None, "absorptionScore": None, "distributionRiskScore": None}
    drawdown = abs(min(finite(metrics.get("drawdownFrom20dHighPct")) or 0, 0))
    shock = abs(min(finite((metrics.get("shock") or {}).get("returnPct")) or 0, 0))
    shock_rvol = finite((metrics.get("shock") or {}).get("rvol20")) or 1
    rsi = finite(metrics.get("rsi14")) or 50
    washout = min(drawdown / 25 * 35, 35) + min(shock / 15 * 25, 25)
    washout += min(max(shock_rvol - 1, 0) / 1.0 * 20, 20)
    washout += min(max(45 - rsi, 0) / 20 * 20, 20)

    absorption = 50.0
    absorption += (finite(metrics.get("cmf20")) or 0) * 30
    absorption += (finite(metrics.get("avgCloseLocation5")) or 0) * 15
    absorption += (finite(metrics.get("latestCloseLocation")) or 0) * 8
    absorption += min(max(finite(metrics.get("relative5dPct")) or 0, -10), 10)
    absorption += 8 if (finite(metrics.get("vsEma8Pct")) or 0) > 0 else -8
    absorption += 10 if (finite(metrics.get("vsEma21Pct")) or 0) > 0 else -10

    distribution = 50.0 - (finite(metrics.get("cmf20")) or 0) * 25
    distribution -= (finite(metrics.get("avgCloseLocation5")) or 0) * 15
    distribution -= (finite(metrics.get("latestCloseLocation")) or 0) * 10
    distribution += 10 if (finite(metrics.get("vsEma8Pct")) or 0) < 0 else -5
    distribution += 10 if (finite(metrics.get("vsEma21Pct")) or 0) < 0 else -5
    if flows:
        foreign5 = finite((flows.get("5d") or {}).get("foreignNetShares")) or 0
        institution5 = finite((flows.get("5d") or {}).get("institutionNetShares")) or 0
        absorption += 10 if foreign5 > 0 else (-10 if foreign5 < 0 else 0)
        absorption += 7 if institution5 > 0 else (-7 if institution5 < 0 else 0)
        distribution += 12 if foreign5 < 0 else -8
        distribution += 8 if institution5 < 0 else -5
    if retail:
        sentiment = finite(retail.get("sentimentScore")) or 0
        if sentiment < 0:
            absorption -= 5
            distribution += 5
        elif sentiment > 0:
            absorption += 5
            distribution -= 5
    return {
        "washoutPainScore": int(round(max(0, min(100, washout)))),
        "absorptionScore": int(round(max(0, min(100, absorption)))),
        "distributionRiskScore": int(round(max(0, min(100, distribution)))),
    }


def action_gate(regime: str, metrics: Mapping[str, Any], scores: Mapping[str, Any]) -> dict[str, Any]:
    if regime == "BLOCK_DATA":
        return {"label": "BLOCK", "reason": "closed daily evidence is unavailable"}
    if regime == "BULL_TREND" and (scores.get("absorptionScore") or 0) >= 55:
        return {
            "label": "ALLOW_REVIEW",
            "reason": "daily trend is repaired; sizing and invalidation still require manual review",
        }
    if regime in {"LIQUIDATION", "TREND_BREAK"}:
        return {
            "label": "WATCH",
            "reason": "pain is visible but EMA21/trend repair has not confirmed",
        }
    return {"label": "WATCH", "reason": "setup is transitional or choppy"}


def leverage_summary(
    symbols: Mapping[str, Mapping[str, Any]], market_margin: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    rows = [symbols[symbol] for symbol in LEVERAGE_SYMBOLS if symbol in symbols]
    drawdowns = [
        finite((row.get("technical") or {}).get("drawdownFrom20dHighPct"))
        if finite((row.get("technical") or {}).get("drawdownFrom20dHighPct")) is not None
        else finite((row.get("descriptivePrice") or {}).get("drawdownFrom20dHighPct"))
        for row in rows
    ]
    drawdowns = [value for value in drawdowns if value is not None]
    residual5 = sum(
        finite((((row.get("investorFlow") or {}).get("5d") or {}).get("individualOtherResidualNetShares"))) or 0
        for row in rows
    )
    why_not = [
        "ETF net share creation/redemption and shares outstanding are not connected.",
        "Investor-category residual flow is not the same as final beneficial ownership.",
    ]
    if market_margin:
        total_drop = finite(market_margin.get("totalMarginCreditFrom30dPeakPct"))
        kospi_drop = finite(market_margin.get("kospiMarginCreditFrom30dPeakPct"))
        ratio_change = finite(market_margin.get("marginCreditToDeposits20dChangePp"))
        if total_drop is not None:
            why_not.insert(0, f"Market-wide margin credit is only {total_drop:.2f}% below its recent peak.")
        if kospi_drop is not None:
            why_not.insert(1, f"KOSPI margin credit is only {kospi_drop:.2f}% below its recent peak.")
        if ratio_change is not None and ratio_change > 0:
            why_not.insert(2, f"Margin credit/customer deposits rose {ratio_change:.2f} percentage points over the comparison window.")
    else:
        why_not.insert(0, "Current KOFIA margin/credit balances are unavailable.")
    return {
        "instrumentCount": len(rows),
        "averageDrawdownFrom20dHighPct": rn(statistics.mean(drawdowns), 2) if drawdowns else None,
        "individualOtherResidual5dShares": rn(residual5, 0),
        "retailOtherStillNetBuying5d": residual5 > 0,
        "painConfirmed": bool(drawdowns and statistics.mean(drawdowns) <= -30),
        "positionClearingConfirmed": False,
        "marketMargin": dict(market_margin) if market_margin else None,
        "whyNotConfirmed": why_not,
    }


def adr_parity(
    config: Mapping[str, Any], prices: Mapping[str, list[dict[str, Any]]],
    symbols: Mapping[str, Mapping[str, Any]], now: dt.datetime, as_of: str | None,
) -> dict[str, Any]:
    master = config.get("skHynixAdr") or {}
    local_security = symbols.get(master.get("localSymbol")) or {}
    local = local_security.get("technical") or {}
    adr_symbol = None
    adr_row = None
    for candidate in (master.get("regularSymbol"), master.get("whenIssuedSymbol")):
        if not candidate:
            continue
        closed, live = closed_and_live_rows(prices.get(candidate) or [], candidate, now, as_of)
        candidate_row = live or (closed[-1] if closed else None)
        if candidate_row:
            adr_symbol, adr_row = candidate, candidate_row
            break
    fx_rows, fx_live = closed_and_live_rows(
        prices.get(master.get("currencyPair", "KRW=X")) or [],
        master.get("currencyPair", "KRW=X"), now, as_of,
    )
    fx_row = fx_live or (fx_rows[-1] if fx_rows else None)
    local_price = finite(local.get("livePrice")) or finite(local.get("close"))
    adr_price = finite((adr_row or {}).get("close"))
    fx = finite((fx_row or {}).get("close"))
    ratio = finite(master.get("adsPerLocalShare"))
    ratio_source = str(master.get("ratioSource") or "")
    ratio_verified = bool(ratio and ratio > 0 and ratio_source.startswith(("https://", "http://")))
    if not ratio_verified:
        return {
            "status": "ratio_unverified",
            "adrSymbol": adr_symbol or master.get("whenIssuedSymbol"),
            "regularSymbol": master.get("regularSymbol"),
            "adsPerLocalShare": rn(ratio, 6),
            "ratioVerified": False,
            "listingSource": master.get("listingSource"),
            "ratioSource": master.get("ratioSource"),
            "conversionCaveat": master.get("conversionCaveat"),
        }
    offer = finite(master.get("offerPriceUsd"))
    new_shares = finite(master.get("newCommonShares"))
    issued_shares = finite(local_security.get("sharesIssued"))
    issued_as_of = local_security.get("sharesIssuedAsOf")
    issued_valid_through = local_security.get("sharesIssuedValidThrough")
    issued_source = local_security.get("sharesIssuedSource")
    local_price_as_of = local.get("livePriceAsOf") or local.get("priceAsOf") or as_of
    shares_verified = verified_share_denominator(
        issued_shares, issued_as_of, issued_source, local_price_as_of, issued_valid_through,
    )
    existing_shares = issued_shares if shares_verified else None
    anchor = {
        "offerPriceUsd": rn(offer, 2),
        "offeredAds": rn(master.get("offeredAds"), 0),
        "newCommonShares": rn(new_shares, 0),
        "newSharesPctPreOffering": rn(new_shares / existing_shares * 100.0, 3)
        if new_shares is not None and existing_shares else None,
        "newSharesPctPostOffering": rn(new_shares / (existing_shares + new_shares) * 100.0, 3)
        if new_shares is not None and existing_shares else None,
        "sharesIssuedDenominator": rn(existing_shares, 0),
        "sharesIssuedAsOf": issued_as_of if shares_verified else None,
        "sharesIssuedValidThrough": issued_valid_through if shares_verified else None,
        "sharesIssuedSource": issued_source if shares_verified else None,
        "sharesIssuedDenominatorVerified": shares_verified,
        "bookIndicationsMultiple": rn(master.get("bookIndicationsMultiple"), 1),
        "offerPriceSource": master.get("offerPriceSource"),
        "offerPriceStatus": master.get("offerPriceStatus"),
        "offerPriceCaveat": master.get("offerPriceCaveat"),
        "bookIndicationsCaveat": master.get("bookIndicationsCaveat"),
        "stabilizationSource": master.get("stabilizationSource"),
        "stabilizationCaveat": master.get("stabilizationCaveat"),
    }
    if offer is not None and fx is not None:
        offer_implied_local = offer * ratio * fx
        anchor.update({
            "offerImpliedLocalKrw": rn(offer_implied_local, 2),
            "offerPremiumDiscountPct": rn(pct(offer_implied_local, local_price), 2)
            if local_price is not None else None,
            "offerFxKrwPerUsd": rn(fx, 4),
            "offerFxAsOf": (fx_row or {}).get("date"),
        })
    if None in (local_price, adr_price, fx):
        return {
            "status": "pre_listing_offer_anchor" if offer is not None and fx is not None else "pre_listing_or_price_unavailable",
            "adrSymbol": adr_symbol or master.get("whenIssuedSymbol"),
            "regularSymbol": master.get("regularSymbol"),
            "adsPerLocalShare": ratio,
            "ratioVerified": ratio_verified,
            "listingSource": master.get("listingSource"),
            "ratioSource": master.get("ratioSource"),
            "conversionCaveat": master.get("conversionCaveat"),
            **anchor,
        }
    implied_local = adr_price * ratio * fx
    return {
        "status": "available_non_synchronous_proxy",
        "adrSymbol": adr_symbol,
        "adrPriceUsd": rn(adr_price, 4),
        "adrPriceAsOf": (adr_row or {}).get("date"),
        "localPriceKrw": rn(local_price, 2),
        "localPriceAsOf": local.get("livePriceAsOf") or local.get("priceAsOf"),
        "krwPerUsd": rn(fx, 4),
        "fxAsOf": (fx_row or {}).get("date"),
        "adsPerLocalShare": ratio,
        "adrImpliedLocalKrw": rn(implied_local, 2),
        "premiumDiscountPct": rn(pct(implied_local, local_price), 2),
        "directionKnown": False,
        "ratioVerified": ratio_verified,
        "listingSource": master.get("listingSource"),
        "ratioSource": master.get("ratioSource"),
        "conversionCaveat": master.get("conversionCaveat"),
        **anchor,
    }


def hypothesis_audit(
    symbols: Mapping[str, Mapping[str, Any]], leverage: Mapping[str, Any],
) -> list[dict[str, Any]]:
    hynix = symbols.get("000660.KS") or {}
    flow = hynix.get("investorFlow") or {}
    technical = hynix.get("technical") or {}
    krx_short = hynix.get("krxShort") or {}
    lending = hynix.get("securitiesLending") or {}
    foreign5 = finite((flow.get("5d") or {}).get("foreignNetShares"))
    institution5 = finite((flow.get("5d") or {}).get("institutionNetShares"))
    residual5 = finite(leverage.get("individualOtherResidual5dShares"))
    market_margin = leverage.get("marketMargin") or {}

    leverage_support = []
    leverage_counter = []
    if leverage.get("painConfirmed"):
        leverage_support.append(
            f"Three Korean 2x proxies average {leverage.get('averageDrawdownFrom20dHighPct')}% from their 20-day highs."
        )
    shock = (technical.get("shock") or {}).get("returnPct")
    if shock is not None:
        leverage_support.append(f"SK hynix's worst recent closed day was {shock}%.")
    if residual5 is not None and residual5 > 0:
        leverage_counter.append(
            f"The 2x products still show +{residual5:,.0f} shares of five-day individual/other residual net buying."
        )
    margin_from_peak = finite(market_margin.get("totalMarginCreditFrom30dPeakPct"))
    kospi_from_peak = finite(market_margin.get("kospiMarginCreditFrom30dPeakPct"))
    deposit_from_peak = finite(market_margin.get("customerDepositsFrom30dPeakPct"))
    ratio_change = finite(market_margin.get("marginCreditToDeposits20dChangePp"))
    if margin_from_peak is not None:
        leverage_support.append(
            f"Official market-wide margin credit did decline {abs(min(margin_from_peak, 0)):.2f}% from its recent peak."
        )
        if margin_from_peak > -15:
            leverage_counter.append(
                f"Official market-wide margin credit is only {abs(margin_from_peak):.2f}% below its recent peak, short of the monitor's 15% clearing rule."
            )
    if kospi_from_peak is not None and kospi_from_peak > -15:
        leverage_counter.append(f"KOSPI margin credit is only {abs(kospi_from_peak):.2f}% below its recent peak.")
    if deposit_from_peak is not None and margin_from_peak is not None and deposit_from_peak < margin_from_peak:
        leverage_counter.append(
            f"Customer deposits fell {abs(deposit_from_peak):.2f}%, faster than margin credit; the cash buffer deteriorated."
        )
    if ratio_change is not None and ratio_change > 0:
        leverage_counter.append(
            f"Margin credit/customer deposits rose {ratio_change:.2f} percentage points rather than de-risking."
        )
    if not market_margin:
        leverage_counter.append("Current margin-credit balances and ETF share redemptions are not connected.")

    if market_margin:
        leverage_verdict = "MIXED" if market_margin.get("marketWideCreditClearingConfirmed") else "REJECTED"
        leverage_confidence = "high" if not market_margin.get("marketWideCreditClearingConfirmed") else "medium"
        missing_leverage = ["SK hynix-specific margin balance", "daily ETF shares outstanding and creations/redemptions"]
    else:
        leverage_verdict = "MIXED" if leverage_support else "UNKNOWN"
        leverage_confidence = "medium" if leverage_support else "low"
        missing_leverage = ["KOFIA margin/credit balance", "daily ETF shares outstanding and creations/redemptions"]

    accumulation_support = []
    accumulation_counter = []
    if institution5 is not None and institution5 > 0:
        accumulation_support.append(f"Domestic institutions bought {institution5:,.0f} SK hynix shares net over five sessions.")
    if foreign5 is not None and foreign5 < 0:
        accumulation_counter.append(f"Foreign investors sold {abs(foreign5):,.0f} SK hynix shares net over five sessions.")
    elif foreign5 is not None and foreign5 > 0:
        accumulation_support.append(f"Foreign investors bought {foreign5:,.0f} SK hynix shares net over five sessions.")
    wall_street_verdict = "SUPPORTED" if (foreign5 or 0) > 0 and (institution5 or 0) > 0 else (
        "REJECTED" if (foreign5 or 0) < 0 and (institution5 or 0) <= 0 else "MIXED"
    )

    regime = hynix.get("regime")
    levels = technical.get("levels") or {}
    rally_support = []
    rally_counter = []
    if regime in {"RELIEF_RALLY", "REPAIR"}:
        rally_support.append(f"Current regime is {regime}, so a reflex rally is mechanically plausible.")
    if (finite(technical.get("vsEma21Pct")) or 0) < 0:
        rally_counter.append(
            f"Price remains below EMA21; {levels.get('confirmEma21')} is a repair level, not a promised target."
        )
    rally_counter.append("Dealer inventory direction and first-day ADR allocation flipping are not public.")

    short_support = []
    short_counter = []
    lending_change = finite(lending.get("loanBalance20dChangePct"))
    if lending_change is not None and lending_change > 0:
        short_support.append(
            f"SK hynix securities-lending balances rose {lending_change:.2f}% over 20 sessions, increasing borrow availability."
        )
    krx_z = finite(krx_short.get("latestZ20"))
    krx_balance_pct = finite(krx_short.get("reportedNetShortBalancePctOutstanding"))
    if krx_z is not None and abs(krx_z) < 2:
        short_counter.append(f"Latest KRX short-transaction share is only {krx_z:.2f} standard deviations from its 20-day norm.")
    if krx_balance_pct is not None:
        short_counter.append(
            f"The latest threshold-reported SK hynix net-short balance is just {krx_balance_pct:.4f}% of shares outstanding."
        )
    us_z = [
        finite((row.get("finraShortVolume") or {}).get("latestZ20"))
        for row in symbols.values() if row.get("symbol") in US_OPTION_SYMBOLS
    ]
    us_z = [value for value in us_z if value is not None]
    if us_z and max(abs(value) for value in us_z) < 2:
        short_counter.append("All four U.S. names' FINRA short-volume readings are within two standard deviations of their 20-day norms.")
    abnormal_short_verdict = "REJECTED" if short_counter and not (
        (krx_z is not None and abs(krx_z) >= 2) or any(abs(value) >= 2 for value in us_z)
    ) else "MIXED"

    return [
        {
            "id": "leverage_washed_out",
            "claim": "The leverage has been washed clean.",
            "verdict": leverage_verdict,
            "confidence": leverage_confidence,
            "supportingEvidence": leverage_support,
            "counterEvidence": leverage_counter,
            "missingDecisiveData": missing_leverage,
            "whatWouldConfirm": "A sustained decline in margin balances and leveraged-ETF shares outstanding, followed by price repair without renewed retail residual buying.",
            "whatWouldFalsify": "Retail/other keeps adding leveraged units or margin balances re-expand while price remains below EMA21.",
        },
        {
            "id": "engineered_short_attack",
            "claim": "Abnormal short selling proves market makers engineered the washout.",
            "verdict": abnormal_short_verdict,
            "confidence": "medium" if krx_short and us_z else "low",
            "supportingEvidence": short_support,
            "counterEvidence": short_counter + [
                "Securities lending includes market making, settlement, arbitrage, and hedging; it is not directional short interest."
            ],
            "missingDecisiveData": ["borrow fees and utilization", "signed order flow", "regulatory beneficial-owner audit"],
            "whatWouldConfirm": "Persistent >2 z-score short transaction shares, rising reported net shorts and borrow utilization, failed delivery flags, and regulator-grade signed flow.",
            "whatWouldFalsify": "Normal short-volume shares, low reported balances, orderly settlement, and price moves explained by ETF rebalancing and broad factor flow.",
        },
        {
            "id": "wall_street_accumulation",
            "claim": "Wall Street/market makers sold to wash out leverage and then accumulated SK hynix low.",
            "verdict": wall_street_verdict,
            "confidence": "medium" if flow else "low",
            "supportingEvidence": accumulation_support,
            "counterEvidence": accumulation_counter + ["Public data identifies investor categories, not market-maker beneficial inventory or intent."],
            "missingDecisiveData": ["dealer inventory", "prime-broker financing", "stock-specific beneficial-owner flow"],
            "whatWouldConfirm": "Foreign and institutional multi-day net buying plus positive CMF/OBV and an EMA21 reclaim on closed bars.",
            "whatWouldFalsify": "Foreign ownership and foreign net buying continue to fall through rebounds.",
        },
        {
            "id": "rally_then_distribution",
            "claim": "The next move is a markup followed by distribution.",
            "verdict": "UNKNOWN",
            "confidence": "low",
            "supportingEvidence": rally_support,
            "counterEvidence": rally_counter,
            "missingDecisiveData": ["SKHYV/SKHY opening tape", "allocation-flip volume", "signed option/customer flow"],
            "whatWouldConfirm": "Price reaches a supply zone, then foreign/institutional flow turns negative with high-volume upper wicks or failed VWAP/EMA21 holds.",
            "whatWouldFalsify": "Supply zones are accepted on volume while foreign/institutional ownership rises and pullbacks hold EMA21/VWAP.",
        },
    ]


def group_summary(symbols: Mapping[str, Mapping[str, Any]], members: Iterable[str]) -> dict[str, Any]:
    rows = [symbols[symbol] for symbol in members if symbol in symbols]
    technical = [row.get("technical") or {} for row in rows if (row.get("technical") or {}).get("available")]
    return {
        "members": [row.get("symbol") for row in rows],
        "count": len(technical),
        "aboveEma8Count": sum((finite(row.get("vsEma8Pct")) or 0) > 0 for row in technical),
        "aboveEma21Count": sum((finite(row.get("vsEma21Pct")) or 0) > 0 for row in technical),
        "average5dReturnPct": rn(statistics.mean(
            [finite(row.get("ret5dPct")) or 0 for row in technical]
        ), 2) if technical else None,
    }


def refresh_cache(config: Mapping[str, Any], cache: dict[str, Any], args: argparse.Namespace, now: dt.datetime) -> dict[str, Any]:
    updated = copy.deepcopy(cache or {})
    updated.setdefault("schemaVersion", 1)
    updated.setdefault("priceHistory", {})
    updated.setdefault("investorFlow", {})
    updated.setdefault("krxShort", {})
    updated.setdefault("securitiesLending", {})
    updated.setdefault("kofiaLeverage", {})
    updated.setdefault("finraShortVolume", {})
    updated.setdefault("shortInterest", {})
    updated.setdefault("options", {})
    updated.setdefault("retailActivity", {})
    updated["updatedAt"] = now.isoformat(timespec="seconds")
    errors: dict[str, str] = {}

    fresh_prices = fetch_price_cache(config, args.period, now)
    updated["priceHistory"].update(fresh_prices)
    expected_symbols = {item["symbol"] for item in config.get("instruments") or []}
    expected_symbols.update(config.get("benchmarks") or [])
    missing_prices = sorted(symbol for symbol in expected_symbols if symbol not in fresh_prices)
    if missing_prices:
        errors["priceHistory"] = "missing fresh response: " + ", ".join(missing_prices)

    client = requests.Session()
    korea_end = now.astimezone(KST).date()
    korea_start = korea_end - dt.timedelta(days=90)
    for item in config.get("instruments") or []:
        code = item.get("naverCode")
        if not code:
            continue
        try:
            rows = fetch_naver_investor(str(code), pages=args.naver_pages, session=client)
            if rows:
                updated["investorFlow"][item["symbol"]] = rows
        except Exception as exc:
            errors[f"naver:{item['symbol']}"] = f"{type(exc).__name__}: {exc}"
        isin = item.get("krxIsin")
        if isin:
            try:
                rows = fetch_krx_short(
                    str(code), str(isin), korea_start, korea_end, session=client,
                )
                if rows:
                    updated["krxShort"][item["symbol"]] = rows
            except Exception as exc:
                errors[f"krxShort:{item['symbol']}"] = f"{type(exc).__name__}: {exc}"
        seibro_name = item.get("seibroName")
        if seibro_name:
            try:
                rows = fetch_seibro_loan(str(code), str(seibro_name), session=client)
                if rows:
                    updated["securitiesLending"][item["symbol"]] = rows
            except Exception as exc:
                errors[f"seibro:{item['symbol']}"] = f"{type(exc).__name__}: {exc}"

    try:
        credit_payload = fetch_kofia_series(
            KOFIA_CREDIT_OBJECT, korea_start, korea_end, session=client,
        )
        funds_payload = fetch_kofia_series(
            KOFIA_FUNDS_OBJECT, korea_start, korea_end, session=client,
        )
        credit_rows = parse_kofia_credit_payload(credit_payload)
        funds_rows = parse_kofia_funds_payload(funds_payload)
        if credit_rows and funds_rows:
            updated["kofiaLeverage"] = {"credit": credit_rows, "funds": funds_rows}
        else:
            errors["kofiaLeverage"] = "official response contained no usable rows"
    except Exception as exc:
        errors["kofiaLeverage"] = f"{type(exc).__name__}: {exc}"

    finra = fetch_finra_short_volume(US_OPTION_SYMBOLS, now.astimezone(ET).date(), session=client)
    updated["finraShortVolume"].update(finra)
    try:
        updated["retailActivity"] = fetch_nasdaq_retail_tracker(client)
    except Exception as exc:
        errors["retailActivity"] = f"{type(exc).__name__}: {exc}"
    try:
        updated["shortInterest"].update(fetch_short_interest(sorted(US_OPTION_SYMBOLS)))
    except Exception as exc:
        errors["shortInterest"] = f"{type(exc).__name__}: {exc}"
    if not args.skip_options:
        try:
            updated["options"].update(fetch_options(sorted(US_OPTION_SYMBOLS), now))
        except Exception as exc:
            errors["options"] = f"{type(exc).__name__}: {exc}"
    updated["fetchErrors"] = errors
    return updated


def build_document(
    config: Mapping[str, Any], cache: Mapping[str, Any], now: dt.datetime, as_of: str | None = None,
) -> dict[str, Any]:
    prices = cache.get("priceHistory") or {}
    benchmark_metrics: dict[str, dict[str, Any]] = {}
    for symbol in config.get("benchmarks") or []:
        closed, live = closed_and_live_rows(prices.get(symbol) or [], symbol, now, as_of)
        benchmark_metrics[symbol] = technical_metrics(symbol, closed, live)

    symbols: dict[str, dict[str, Any]] = {}
    instrument_lookup = {item["symbol"]: item for item in config.get("instruments") or []}
    for symbol, item in instrument_lookup.items():
        closed, live = closed_and_live_rows(prices.get(symbol) or [], symbol, now, as_of)
        benchmark = benchmark_metrics.get(item.get("benchmark")) or {}
        metrics = technical_metrics(symbol, closed, live, finite(benchmark.get("ret5dPct")))
        flow = flow_summary((cache.get("investorFlow") or {}).get(symbol) or [], metrics.get("priceAsOf") or as_of)
        regime = classify_regime(metrics)
        retail = (cache.get("retailActivity") or {}).get(symbol)
        options = validate_cached_option_summary((cache.get("options") or {}).get(symbol))
        scores = score_symbol(metrics, flow, retail)
        symbols[symbol] = {
            **item,
            "technical": metrics,
            "descriptivePrice": descriptive_price_snapshot(closed)
            if item.get("instrumentType") == "leveraged_etf" else None,
            "investorFlow": flow,
            "krxShort": krx_short_summary(
                (cache.get("krxShort") or {}).get(symbol) or [], closed,
                finite(item.get("sharesOutstanding")), metrics.get("priceAsOf") or as_of,
                item.get("sharesOutstandingAsOf"), item.get("sharesOutstandingSource"),
                item.get("sharesOutstandingValidThrough"),
            ),
            "securitiesLending": securities_lending_summary(
                (cache.get("securitiesLending") or {}).get(symbol) or [],
                finite(item.get("sharesOutstanding")), metrics.get("priceAsOf") or as_of,
                item.get("sharesOutstandingAsOf"), item.get("sharesOutstandingSource"),
                item.get("sharesOutstandingValidThrough"),
            ),
            "finraShortVolume": finra_summary(
                (cache.get("finraShortVolume") or {}).get(symbol) or [], metrics.get("priceAsOf") or as_of,
            ),
            "shortInterest": (cache.get("shortInterest") or {}).get(symbol),
            "retailActivity": retail,
            "options": options,
            "regime": regime,
            "scores": scores,
            "action": action_gate(regime, metrics, scores),
        }

    korea_price_as_of = max(filter(None, [
        (row.get("technical") or {}).get("priceAsOf")
        for row in symbols.values() if row.get("market") == "Korea"
    ]), default=None)
    kofia = cache.get("kofiaLeverage") or {}
    market_margin = kofia_leverage_summary(
        kofia.get("credit") or [], kofia.get("funds") or [], as_of or korea_price_as_of,
    )
    leverage = leverage_summary(symbols, market_margin)
    hypotheses = hypothesis_audit(symbols, leverage)
    us_date_candidates = [
        (row.get("technical") or {}).get("priceAsOf")
        for row in symbols.values()
        if row.get("decisionSymbol") and row.get("market") == "United States"
    ]
    korea_date_candidates = [
        (row.get("technical") or {}).get("priceAsOf")
        for row in symbols.values()
        if row.get("decisionSymbol") and row.get("market") == "Korea"
    ]
    # The dashboard's portfolio reference date is a U.S. close.  Korea's next
    # calendar-day close can already exist before the following U.S. session;
    # keep it in source-specific freshness without making the artifact appear
    # one day after the portfolio snapshot.
    us_as_of = max(filter(None, us_date_candidates), default=None)
    korea_as_of = max(filter(None, korea_date_candidates), default=None)
    market_as_of = as_of or us_as_of or korea_as_of or now.date().isoformat()
    options_usable = sum(
        ((row.get("options") or {}).get("dataQuality") == "usable_proxy")
        for row in symbols.values()
    )
    research_grade = bool(
        symbols.get("000660.KS", {}).get("investorFlow")
        and (symbols.get("000660.KS", {}).get("technical") or {}).get("available")
    )
    # The two claims the user most wants to test still require stock-specific
    # margin/ETF share data and signed dealer flow.  Until those feeds exist the artifact must not
    # advertise itself as an autonomous decision-grade signal.
    decision_grade = False
    data_gaps = []
    if not market_margin:
        data_gaps.append({
            "id": "korea_market_margin_credit",
            "severity": "critical_for_washout_claim",
            "gap": "The official KOFIA market-wide margin/credit and forced-liquidation series was unavailable in this run.",
            "upgradePath": "Retry FreeSIS or import a dated official export; the monitor fails closed when it is absent.",
        })
    data_gaps.extend([
        {
            "id": "sk_hynix_margin_credit",
            "severity": "material_for_stock_specific_claim",
            "gap": "Market-wide KOFIA balances are connected, but current SK hynix-specific margin balances are not public in the feed.",
            "upgradePath": "Use a licensed Korean broker/market data feed with issue-level credit balances.",
        },
        {
            "id": "leveraged_etf_shares",
            "severity": "critical_for_washout_claim",
            "gap": "No daily leveraged-ETF shares outstanding, creation/redemption, or swap exposure.",
            "upgradePath": "Import issuer PCF/NAV/share-count history or a licensed ETF-flow feed.",
        },
        {
            "id": "dealer_inventory",
            "severity": "structurally_unobservable_publicly",
            "gap": "Open interest has no customer/dealer sign; public data cannot identify dealer net gamma or intent.",
            "upgradePath": "Use OPRA-classified trade analytics/Trade Alert/LiveVol and retain scenario bounds rather than a certain sign.",
        },
        {
            "id": "us_retail_flow",
            "severity": "material",
            "gap": "Nasdaq RTAT covers only the daily top five; symbols absent from that list remain unknown.",
            "upgradePath": "Use a licensed full-universe retail-flow vendor for complete brokerage-tagged coverage.",
        },
        {
            "id": "short_interest_freshness",
            "severity": "material",
            "gap": "Exchange short interest is twice-monthly and delayed; FINRA daily short volume is not a position.",
            "upgradePath": "Add licensed consolidated exchange short interest and securities-lending utilization/borrow cost.",
        },
    ])
    return sanitize({
        "schemaVersion": 1,
        "title": "Memory Flow & Dealer-Lens Monitor",
        "generatedAt": now.isoformat(timespec="seconds"),
        "asOf": market_as_of,
        "decisionGrade": decision_grade,
        "researchOnly": True,
        "researchGrade": research_grade,
        "dataFreshness": {
            "price": {
                "asOf": market_as_of,
                "usClosedAsOf": us_as_of,
                "koreaClosedAsOf": korea_as_of,
                "source": "Yahoo Finance daily OHLCV",
                "status": "source_specific_market_closes",
            },
            "koreaInvestor": {"asOf": (symbols.get("000660.KS", {}).get("investorFlow") or {}).get("asOf"), "source": "Naver Finance KRX-derived table", "status": "secondary_source"},
            "krxShort": {
                "asOf": max(filter(None, [((row.get("krxShort") or {}).get("asOf")) for row in symbols.values()]), default=None),
                "source": "KRX", "status": "official_transactions_and_threshold_balance",
            },
            "securitiesLending": {
                "asOf": max(filter(None, [((row.get("securitiesLending") or {}).get("asOf")) for row in symbols.values()]), default=None),
                "source": "KSD SEIBRO", "status": "official_stock_loan_balance",
            },
            "kofiaLeverage": {
                "asOf": (market_margin or {}).get("asOf"), "source": "KOFIA FreeSIS",
                "status": "official_market_wide_balance" if market_margin else "unavailable",
            },
            "finra": {"asOf": max(filter(None, [((row.get("finraShortVolume") or {}).get("asOf")) for row in symbols.values()]), default=None), "source": "FINRA", "status": "official_off_exchange_volume"},
            "options": {"usableSymbols": options_usable, "totalSymbols": len(US_OPTION_SYMBOLS), "status": "scenario_only"},
            "usRetail": {
                "asOf": max(filter(None, [((row.get("retailActivity") or {}).get("asOf")) for row in symbols.values()]), default=None),
                "source": "Nasdaq Retail Trading Activity Tracker",
                "status": "top_five_only",
            },
            "cacheUpdatedAt": cache.get("updatedAt"),
            "fetchErrors": cache.get("fetchErrors") or {},
        },
        "benchmarks": benchmark_metrics,
        "groups": {
            "koreaMemory": group_summary(symbols, ["000660.KS", "005930.KS"]),
            "usMemoryAndStorage": group_summary(symbols, ["MU", "SNDK", "WDC", "STX"]),
            "skHynixLeverage": leverage,
        },
        "symbols": symbols,
        "crossMarket": {
            "skHynixAdrParity": adr_parity(config, prices, symbols, now, as_of),
        },
        "hypotheses": hypotheses,
        "decision": {
            "labels": {symbol: (row.get("action") or {}).get("label") for symbol, row in symbols.items() if row.get("decisionSymbol")},
            "rule": "ALLOW_REVIEW requires a repaired daily trend and supportive absorption proxy; WATCH is not an entry instruction.",
        },
        "methodology": {
            "evidenceStack": ["observed investor category flow", "market mechanics", "behavioral inference"],
            "observed": [
                "KRX-derived investor-category net shares", "official KRX short transactions and reported balance",
                "official KSD SEIBRO securities-lending balances",
                "official KOFIA margin credit, customer deposits, and forced liquidations",
                "FINRA off-exchange short-sale volume", "daily OHLCV",
            ],
            "proxy": ["CMF20", "OBV slope", "relative strength", "EMA repair", "leveraged-ETF price drawdown"],
            "inferred": ["washout pain", "absorption", "distribution risk", "dealer scenario"],
            "hardRules": [
                "Price pain alone cannot confirm leverage clearing; balances, cash buffers, and ETF share counts must agree.",
                "Open interest without participant sign cannot confirm dealer net gamma.",
                "Targets are purpose-based zones, not a forecast of the top.",
                "A live partial Korean bar is never used as a closed-bar confirmation.",
                "Share ratios require sourced, date-valid denominators; issued shares and outstanding shares are not interchangeable.",
                "ADR parity requires a sourced ADS ratio, and a reported offer anchor is not a traded or guaranteed-arbitrage price.",
            ],
        },
        "dataGaps": data_gaps,
        "disclaimer": "Research decision support only; not investment advice. Public market data cannot reveal a market maker's private inventory or intent.",
    })


def _fmt(value: Any, digits: int = 1, suffix: str = "") -> str:
    number = finite(value)
    return "—" if number is None else f"{number:,.{digits}f}{suffix}"


def render_report(document: Mapping[str, Any]) -> str:
    lines = [
        "# Memory Flow & Dealer-Lens Monitor",
        "",
        f"- Generated: {document.get('generatedAt')}",
        f"- U.S. portfolio reference as of: {document.get('asOf')}",
        f"- Korea closed as of: {((document.get('dataFreshness') or {}).get('price') or {}).get('koreaClosedAsOf')}",
        "- The monitor separates observed flows, market proxies, and inference. It does not claim to see dealer intent.",
        "",
        "## Hypothesis Audit",
        "",
        "| Hypothesis | Verdict | Confidence | Main evidence |",
        "| --- | --- | --- | --- |",
    ]
    for item in document.get("hypotheses") or []:
        evidence = "; ".join((item.get("supportingEvidence") or []) + (item.get("counterEvidence") or [])[:1])
        lines.append(f"| {item.get('claim')} | {item.get('verdict')} | {item.get('confidence')} | {evidence or 'insufficient evidence'} |")

    lines.extend(["", "## Closed-Bar Market Structure", "", "| Symbol | Regime | Close | 5D | vs EMA8 | vs EMA21 | Latest CLV | CMF20 | Washout pain | Absorption | Distribution risk | Gate |", "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |"])
    for symbol, row in (document.get("symbols") or {}).items():
        if not row.get("decisionSymbol"):
            continue
        technical, scores = row.get("technical") or {}, row.get("scores") or {}
        lines.append(
            f"| {symbol} | {row.get('regime')} | {_fmt(technical.get('close'), 2)} | "
            f"{_fmt(technical.get('ret5dPct'), 1, '%')} | {_fmt(technical.get('vsEma8Pct'), 1, '%')} | "
            f"{_fmt(technical.get('vsEma21Pct'), 1, '%')} | {_fmt(technical.get('latestCloseLocation'), 2)} | "
            f"{_fmt(technical.get('cmf20'), 2)} | "
            f"{_fmt(scores.get('washoutPainScore'), 0)} | {_fmt(scores.get('absorptionScore'), 0)} | "
            f"{_fmt(scores.get('distributionRiskScore'), 0)} | {(row.get('action') or {}).get('label')} |"
        )

    hynix = (document.get("symbols") or {}).get("000660.KS") or {}
    flow = hynix.get("investorFlow") or {}
    lines.extend(["", "## SK hynix Investor Categories", "", "| Window | Institution net shares | Foreign net shares | Individual/other residual |", "| --- | ---: | ---: | ---: |"])
    for window in ("1d", "5d", "20d"):
        row = flow.get(window) or {}
        lines.append(
            f"| {window} | {_fmt(row.get('institutionNetShares'), 0)} | {_fmt(row.get('foreignNetShares'), 0)} | "
            f"{_fmt(row.get('individualOtherResidualNetShares'), 0)} |"
        )
    lines.append("")
    lines.append(f"Foreign ownership: {_fmt(flow.get('foreignOwnershipPct'), 2, '%')} · 20D change {_fmt(flow.get('foreignOwnershipChange20dPp'), 2, ' pp')}")

    leverage = ((document.get("groups") or {}).get("skHynixLeverage") or {})
    margin = leverage.get("marketMargin") or {}
    lines.extend([
        "", "## Leverage-Clearing Check", "",
        f"- 2x proxy average drawdown from 20D high: {_fmt(leverage.get('averageDrawdownFrom20dHighPct'), 1, '%')}",
        f"- Five-day individual/other residual net shares: {_fmt(leverage.get('individualOtherResidual5dShares'), 0)}",
        f"- KOFIA total margin credit: {_fmt(margin.get('totalMarginCreditTrillionKrw'), 3, ' tn KRW')} · from 30-session peak {_fmt(margin.get('totalMarginCreditFrom30dPeakPct'), 2, '%')}",
        f"- KOFIA KOSPI margin credit: {_fmt(margin.get('kospiMarginCreditTrillionKrw'), 3, ' tn KRW')} · from 30-session peak {_fmt(margin.get('kospiMarginCreditFrom30dPeakPct'), 2, '%')}",
        f"- Customer deposits: {_fmt(margin.get('customerDepositsTrillionKrw'), 3, ' tn KRW')} · margin/deposits {_fmt(margin.get('marginCreditToCustomerDepositsPct'), 2, '%')}",
        f"- Latest forced liquidation: {_fmt(margin.get('forcedLiquidationBillionKrw'), 3, ' bn KRW')} · ratio {_fmt(margin.get('forcedLiquidationRatioPct'), 1, '%')}",
        f"- Pain confirmed: {leverage.get('painConfirmed')} · position clearing confirmed: {leverage.get('positionClearingConfirmed')}",
        "",
        "## SK hynix Official Short-Sale Check",
        "",
    ])
    krx_short = hynix.get("krxShort") or {}
    lending = hynix.get("securitiesLending") or {}
    lines.extend([
        f"- Latest short transaction share: {_fmt(krx_short.get('latestShortTransactionPct'), 2, '%')}",
        f"- Weighted 20-session short transaction share: {_fmt(krx_short.get('weighted20dShortTransactionPct'), 2, '%')}",
        f"- Reported net-short balance: {_fmt(krx_short.get('reportedNetShortBalanceShares'), 0, ' shares')} · {_fmt(krx_short.get('reportedNetShortBalancePctOutstanding'), 4, '% of shares')}",
        f"- KSD securities-lending balance: {_fmt(lending.get('loanBalanceShares'), 0, ' shares')} · {_fmt(lending.get('loanBalancePctOutstanding'), 3, '% of shares')} · 20-session change {_fmt(lending.get('loanBalance20dChangePct'), 2, '%')}",
        "- Short transaction volume is not a position; the reported balance is threshold-based and T+2.",
        "- Borrowed shares are not all directional shorts; they also support settlement, arbitrage, hedging, and market making.",
        "",
        "## SK hynix Level Ladder",
        "",
    ])
    levels = ((hynix.get("technical") or {}).get("levels") or {})
    for key, label in (
        ("invalidation20dLow", "Invalidation / 20D low"),
        ("support5d", "Near support / 5D low"),
        ("reclaimEma8", "First reclaim / EMA8"),
        ("confirmEma21", "Trend repair / EMA21"),
        ("firstSupply5dHigh", "First supply / 5D high"),
        ("stretchSupply20dHigh", "Stretch supply / 20D high"),
    ):
        lines.append(f"- {label}: {_fmt(levels.get(key), 0)} KRW")

    parity = ((document.get("crossMarket") or {}).get("skHynixAdrParity") or {})
    lines.extend([
        "", "## SKHYV / SKHY ADR Anchor", "",
        f"- Status: {parity.get('status')} · verified ratio {_fmt(parity.get('adsPerLocalShare'), 0)} ADS per common share",
        f"- Offer anchor: ${_fmt(parity.get('offerPriceUsd'), 2)} · implied local {_fmt(parity.get('offerImpliedLocalKrw'), 0, ' KRW')} · non-synchronous premium {_fmt(parity.get('offerPremiumDiscountPct'), 2, '%')}",
        f"- Offer-source status: {parity.get('offerPriceStatus') or 'unavailable'} · {parity.get('offerPriceCaveat') or 'No source caveat supplied.'}",
        f"- New common shares: {_fmt(parity.get('newCommonShares'), 0)} · {_fmt(parity.get('newSharesPctPreOffering'), 2, '% of pre-offer shares')}",
        f"- Dilution denominator verified: {bool(parity.get('sharesIssuedDenominatorVerified'))} · issued shares {_fmt(parity.get('sharesIssuedDenominator'), 0)} as of {parity.get('sharesIssuedAsOf') or '—'}",
        f"- Book indication: {_fmt(parity.get('bookIndicationsMultiple'), 1, 'x')} · not secondary-market ownership or guaranteed demand.",
        f"- Stabilization: {parity.get('stabilizationCaveat') or 'unavailable'}",
    ])

    lines.extend(["", "## Data Gaps", ""])
    for gap in document.get("dataGaps") or []:
        lines.append(f"- **{gap.get('id')}**: {gap.get('gap')} Upgrade: {gap.get('upgradePath')}")
    lines.extend(["", f"> {document.get('disclaimer')}", ""])
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build the memory-stock flow and dealer-lens artifact.")
    parser.add_argument("--universe", default=str(DEFAULT_UNIVERSE))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--report", default=str(DEFAULT_REPORT))
    parser.add_argument("--cache", default=str(DEFAULT_CACHE))
    parser.add_argument("--period", default="1y")
    parser.add_argument("--as-of", help="closed-bar cutoff YYYY-MM-DD")
    parser.add_argument("--no-fetch", action="store_true", help="recompute from private cache without network")
    parser.add_argument("--skip-options", action="store_true", help="skip best-effort option-chain snapshot")
    parser.add_argument("--naver-pages", type=int, default=3)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.as_of:
        try:
            dt.date.fromisoformat(args.as_of)
        except ValueError as exc:
            raise SystemExit("!! --as-of must be YYYY-MM-DD") from exc
    config = load_json(args.universe)
    if not isinstance(config, dict) or config.get("schemaVersion") != 1:
        raise SystemExit("!! memory-flow universe must be a schemaVersion 1 JSON object")
    cache = load_json(args.cache, default={}) or {}
    now = dt.datetime.now().astimezone()
    if args.no_fetch:
        if not cache.get("priceHistory"):
            raise SystemExit("!! --no-fetch requires an existing memory_flow_cache.json")
    else:
        cache = refresh_cache(config, cache, args, now)
        atomic_write_json(args.cache, sanitize(cache))
    document = build_document(config, cache, now, args.as_of)
    atomic_write_json(args.out, document)
    atomic_write_text(args.report, render_report(document))
    print(f"Memory flow artifact: {args.out}")
    print(f"Report: {args.report}")
    print(f"As of {document['asOf']} · decisionGrade={document['decisionGrade']}")
    for item in document.get("hypotheses") or []:
        print(f"  {item['id']}: {item['verdict']} ({item['confidence']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

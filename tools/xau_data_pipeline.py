#!/usr/bin/env python3
"""Fetch XAU data and compute the local 10-indicator dashboard inputs.

This script is intentionally read-only toward brokerage/trading accounts. It only
fetches public market/macro data and writes local CSV/JSON files.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import html
import http.cookiejar
import json
import math
import re
import statistics
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Any
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_STORE = PROJECT_ROOT / "python-data-store"
DATA_RUNS = PROJECT_ROOT / "python-data-runs"
CALGARY_TZ = ZoneInfo("America/Edmonton")

YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?{params}"
YAHOO_CRUMB_URL = "https://query1.finance.yahoo.com/v1/test/getcrumb"
YAHOO_OPTIONS_URL = "https://query2.finance.yahoo.com/v7/finance/options/{symbol}?{params}"
FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?{params}"
CFTC_LEGACY_METALS_URL = "https://www.cftc.gov/dea/futures/deacmxsf.htm"
SPDR_GLD_ARCHIVE_URL = "https://api.spdrgoldshares.com/api/v1/historical-archive?exchange=NYSE&lang=en&product=gld"
WGC_GOLD_ETF_PAGE_URL = "https://www.gold.org/goldhub/data/gold-etfs-holdings-and-flows"
TROY_OUNCES_PER_METRIC_TONNE = 32150.746568627

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json,text/plain,text/html,*/*",
}

YAHOO_SERIES = [
    {"symbol": "GC=F", "intervals": ["1d", "1h"], "role": "gold_futures"},
    {"symbol": "SI=F", "intervals": ["1d", "1h"], "role": "silver_futures"},
    {"symbol": "GLD", "intervals": ["1d"], "role": "gold_etf_volume_proxy"},
    {"symbol": "DX-Y.NYB", "intervals": ["1d"], "role": "dxy"},
    {"symbol": "^GVZ", "intervals": ["1d"], "role": "gold_volatility_index"},
]

FRED_SERIES = {
    "DFII10": "10y_real_yield",
    "T10YIE": "10y_breakeven_inflation",
    "T5YIE": "5y_breakeven_inflation",
}

OHLCV_FIELDS = [
    "timestamp_utc",
    "timestamp_calgary",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "adjclose",
]


class FetchError(RuntimeError):
    """Raised when a network fetch fails."""


def now_calgary() -> dt.datetime:
    return dt.datetime.now(CALGARY_TZ)


def rel(path: Path) -> str:
    return str(path.relative_to(PROJECT_ROOT))


def utc_from_iso(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)


def parse_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        if math.isnan(float(value)):
            return None
        return float(value)
    text = str(value).strip().replace(",", "")
    if text in {"", ".", "nan", "None", "null"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_int(value: Any) -> int | None:
    numeric = parse_float(value)
    if numeric is None:
        return None
    return int(numeric)


def clean_symbol(symbol: str) -> str:
    return "".join(ch for ch in symbol if ch.isalnum())


def yahoo_store_path(symbol: str, interval: str) -> Path:
    return DATA_STORE / "yahoo" / symbol / f"yahoo-{clean_symbol(symbol)}-{interval}.csv"


def fred_store_path(series_id: str) -> Path:
    return DATA_STORE / "fred" / series_id / f"fred-{series_id}.csv"


def cftc_store_path() -> Path:
    return DATA_STORE / "cftc" / "cftc-gold-commercial-net.csv"


def gld_etf_flow_store_path() -> Path:
    return DATA_STORE / "ssga" / "GLD" / "ssga-GLD-etf-flow.csv"


def gld_archive_store_path() -> Path:
    return DATA_STORE / "ssga" / "GLD" / "US_GLD_Archive_EN.xlsx"


def wgc_global_etf_flow_store_path() -> Path:
    return DATA_STORE / "wgc" / "gold-etf-flows" / "wgc-global-gold-etf-flow.csv"


def wgc_global_etf_latest_json_store_path() -> Path:
    return DATA_STORE / "wgc" / "gold-etf-flows" / "wgc-global-gold-etf-flow-latest.json"


def options_cache_path(symbol: str) -> Path:
    return DATA_STORE / "options" / symbol / f"yahoo-{clean_symbol(symbol)}-options-put-call-latest.json"


def ratio_store_path(interval: str) -> Path:
    return (
        DATA_STORE
        / "analysis-raw"
        / "spot"
        / "oanda"
        / "gold-silver-ratio"
        / f"oanda-XAUUSD-XAGUSD-ratio-{interval}.csv"
    )


def spot_store_path(instrument: str, interval: str) -> Path:
    return (
        DATA_STORE
        / "analysis-raw"
        / "spot"
        / "oanda"
        / instrument
        / f"oanda-{instrument.replace('_', '')}-2y-{interval}-ohlc-spot.csv"
    )


def legacy_oanda_store_path(instrument: str, interval: str) -> Path:
    return DATA_STORE / "oanda" / instrument / f"oanda-{instrument.replace('_', '')}-{interval}.csv"


def oanda_store_path(interval: str) -> Path:
    return spot_store_path("XAU_USD", interval)


def http_get(
    url: str,
    *,
    opener: urllib.request.OpenerDirector | None = None,
    timeout: int = 12,
    headers: dict[str, str] | None = None,
) -> bytes:
    req = urllib.request.Request(url, headers=headers or HEADERS)
    try:
        if opener:
            with opener.open(req, timeout=timeout) as response:
                return response.read()
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.read()
    except (urllib.error.URLError, TimeoutError) as exc:
        raise FetchError(f"Fetch failed for {url}: {exc}") from exc


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write_csv_rows(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def fetch_yahoo_chart(symbol: str, interval: str, range_: str) -> dict[str, Any]:
    params = urllib.parse.urlencode(
        {
            "range": range_,
            "interval": interval,
            "includePrePost": "false",
            "events": "history",
        }
    )
    url = YAHOO_CHART_URL.format(symbol=urllib.parse.quote(symbol), params=params)
    payload = json.loads(http_get(url).decode("utf-8"))
    error = payload.get("chart", {}).get("error")
    if error:
        raise FetchError(f"Yahoo returned an error for {symbol} {interval}: {error}")
    return payload


def parse_yahoo_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    result = payload["chart"]["result"][0]
    timestamps = result.get("timestamp") or []
    quote = result["indicators"]["quote"][0]
    adjclose = (result.get("indicators", {}).get("adjclose") or [{}])[0].get("adjclose")
    rows: list[dict[str, Any]] = []

    for index, timestamp in enumerate(timestamps):
        utc_time = dt.datetime.fromtimestamp(timestamp, dt.UTC)
        calgary_time = utc_time.astimezone(CALGARY_TZ)
        row = {
            "timestamp_utc": utc_time.isoformat(),
            "timestamp_calgary": calgary_time.isoformat(),
            "open": quote.get("open", [None] * len(timestamps))[index],
            "high": quote.get("high", [None] * len(timestamps))[index],
            "low": quote.get("low", [None] * len(timestamps))[index],
            "close": quote.get("close", [None] * len(timestamps))[index],
            "volume": quote.get("volume", [None] * len(timestamps))[index],
            "adjclose": adjclose[index] if adjclose else None,
        }
        if row["close"] is not None:
            rows.append(row)
    return rows


def merge_time_series(
    store_path: Path,
    fetched_rows: list[dict[str, Any]],
    fieldnames: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    existing = read_csv_rows(store_path)
    existing_by_ts = {row["timestamp_utc"]: row for row in existing if row.get("timestamp_utc")}
    new_rows = [row for row in fetched_rows if row.get("timestamp_utc") not in existing_by_ts]

    merged_by_ts: dict[str, dict[str, Any]] = dict(existing_by_ts)
    for row in fetched_rows:
        timestamp = row.get("timestamp_utc")
        if timestamp:
            merged_by_ts[timestamp] = row

    merged = sorted(merged_by_ts.values(), key=lambda row: row["timestamp_utc"])
    write_csv_rows(store_path, merged, fieldnames)
    return merged, new_rows, len(existing)


def fetch_and_update_yahoo(
    symbol: str,
    interval: str,
    range_: str,
    run_raw_dir: Path,
) -> dict[str, Any]:
    store_path = yahoo_store_path(symbol, interval)
    payload = fetch_yahoo_chart(symbol, interval, range_)
    rows = parse_yahoo_rows(payload)
    merged, new_rows, previous_count = merge_time_series(store_path, rows, OHLCV_FIELDS)

    increment_file = None
    if new_rows:
        increment_path = run_raw_dir / f"yahoo-increment-{clean_symbol(symbol)}-{interval}.csv"
        write_csv_rows(increment_path, new_rows, OHLCV_FIELDS)
        increment_file = rel(increment_path)

    return {
        "symbol": symbol,
        "interval": interval,
        "range_requested": range_,
        "store_file": rel(store_path),
        "fetched_rows": len(rows),
        "previous_store_rows": previous_count,
        "store_rows": len(merged),
        "new_rows": len(new_rows),
        "increment_file": increment_file,
        "first_store_timestamp": merged[0]["timestamp_calgary"] if merged else None,
        "last_store_timestamp": merged[-1]["timestamp_calgary"] if merged else None,
        "last_close": parse_float(merged[-1].get("close")) if merged else None,
    }


def fetch_fred_csv(series_id: str) -> list[dict[str, Any]]:
    observation_start = (dt.date.today() - dt.timedelta(days=365 * 3)).isoformat()
    params = urllib.parse.urlencode({"id": series_id, "cosd": observation_start})
    url = FRED_CSV_URL.format(params=params)
    try:
        result = subprocess.run(
            ["curl", "-L", "--max-time", "20", "-sS", url],
            check=True,
            capture_output=True,
            text=True,
            timeout=25,
        )
        text = result.stdout
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise FetchError(f"FRED curl fetch failed for {series_id}: {exc}") from exc
    if not text.strip():
        raise FetchError(f"FRED returned an empty CSV for {series_id}.")
    rows: list[dict[str, Any]] = []
    for row in csv.DictReader(text.splitlines()):
        value = parse_float(row.get(series_id))
        if value is None:
            continue
        rows.append({"date": row["observation_date"], "value": value})
    return rows


def update_fred_series(series_id: str, run_raw_dir: Path) -> dict[str, Any]:
    store_path = fred_store_path(series_id)
    existing = read_csv_rows(store_path)
    existing_by_date = {row["date"]: row for row in existing if row.get("date")}
    rows = fetch_fred_csv(series_id)
    new_rows = [row for row in rows if row["date"] not in existing_by_date]

    merged_by_date = dict(existing_by_date)
    for row in rows:
        merged_by_date[row["date"]] = row
    merged = sorted(merged_by_date.values(), key=lambda row: row["date"])
    write_csv_rows(store_path, merged, ["date", "value"])

    increment_file = None
    if new_rows:
        increment_path = run_raw_dir / f"fred-increment-{series_id}.csv"
        write_csv_rows(increment_path, new_rows, ["date", "value"])
        increment_file = rel(increment_path)

    return {
        "series_id": series_id,
        "role": FRED_SERIES.get(series_id),
        "store_file": rel(store_path),
        "fetched_rows": len(rows),
        "store_rows": len(merged),
        "new_rows": len(new_rows),
        "increment_file": increment_file,
        "latest_date": merged[-1]["date"] if merged else None,
        "latest_value": parse_float(merged[-1].get("value")) if merged else None,
    }


def parse_cftc_gold(text: str, fetched_at: str) -> dict[str, Any]:
    pattern = re.compile(
        r"GOLD - COMMODITY EXCHANGE INC\..*?"
        r"POSITIONS AS OF\s+([0-9/]+).*?"
        r"OPEN INTEREST:\s*([0-9,]+)\s*"
        r"COMMITMENTS\s*([\s0-9,\-]+?)\s*"
        r"CHANGES FROM\s+([0-9/]+).*?"
        r"\(CHANGE IN OPEN INTEREST:\s*([0-9,\-]+)\)\s*"
        r"([\s0-9,\-]+?)\s*"
        r"PERCENT OF OPEN INTEREST",
        re.DOTALL,
    )
    match = pattern.search(text)
    if not match:
        raise FetchError("Could not parse GOLD block from CFTC legacy metals report.")

    def numbers(block: str) -> list[int]:
        return [int(value.replace(",", "")) for value in re.findall(r"-?[\d,]+", block)]

    report_date = parse_cftc_date(match.group(1))
    open_interest = int(match.group(2).replace(",", ""))
    commitments = numbers(match.group(3))
    changes = numbers(match.group(6))
    if len(commitments) < 9:
        raise FetchError("CFTC GOLD commitments row did not contain enough columns.")

    commercial_long = commitments[3]
    commercial_short = commitments[4]
    commercial_net = commercial_long - commercial_short
    parsed = {
        "report_date": report_date.isoformat(),
        "open_interest": open_interest,
        "noncommercial_long": commitments[0],
        "noncommercial_short": commitments[1],
        "noncommercial_spreads": commitments[2],
        "commercial_long": commercial_long,
        "commercial_short": commercial_short,
        "commercial_net": commercial_net,
        "commercial_net_short": commercial_short - commercial_long,
        "total_long": commitments[5],
        "total_short": commitments[6],
        "nonreportable_long": commitments[7],
        "nonreportable_short": commitments[8],
        "change_from_date": parse_cftc_date(match.group(4)).isoformat(),
        "change_open_interest": int(match.group(5).replace(",", "")),
        "commercial_long_change": changes[3] if len(changes) > 4 else None,
        "commercial_short_change": changes[4] if len(changes) > 4 else None,
        "source": CFTC_LEGACY_METALS_URL,
        "fetched_at_calgary": fetched_at,
    }
    return parsed


def parse_cftc_date(value: str) -> dt.date:
    month, day, year = [int(part) for part in value.split("/")]
    full_year = 2000 + year if year < 80 else 1900 + year
    return dt.date(full_year, month, day)


def update_cftc(run_raw_dir: Path, fetched_at: str) -> dict[str, Any]:
    text = http_get(CFTC_LEGACY_METALS_URL).decode("utf-8", errors="ignore")
    raw_path = run_raw_dir / "cftc-deacmxsf.html"
    raw_path.write_text(text, encoding="utf-8")

    parsed = parse_cftc_gold(text, fetched_at)
    store_path = cftc_store_path()
    existing = read_csv_rows(store_path)
    by_date = {row["report_date"]: row for row in existing if row.get("report_date")}
    is_new = parsed["report_date"] not in by_date
    by_date[parsed["report_date"]] = parsed
    merged = sorted(by_date.values(), key=lambda row: row["report_date"])
    write_csv_rows(store_path, merged, list(parsed.keys()))

    return {
        "store_file": rel(store_path),
        "raw_file": rel(raw_path),
        "store_rows": len(merged),
        "new_rows": 1 if is_new else 0,
        "latest": parsed,
    }


def xlsx_shared_strings(zipped: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in zipped.namelist():
        return []
    namespace = {"a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    root = ET.fromstring(zipped.read("xl/sharedStrings.xml"))
    strings: list[str] = []
    for item in root.findall("a:si", namespace):
        strings.append("".join(node.text or "" for node in item.findall(".//a:t", namespace)))
    return strings


def xlsx_sheet_path(zipped: zipfile.ZipFile, sheet_name: str) -> str:
    namespace = {
        "a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
        "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    }
    workbook = ET.fromstring(zipped.read("xl/workbook.xml"))
    relationships = ET.fromstring(zipped.read("xl/_rels/workbook.xml.rels"))
    id_to_target = {node.attrib["Id"]: node.attrib["Target"] for node in relationships}
    for sheet in workbook.findall("a:sheets/a:sheet", namespace):
        if sheet.attrib.get("name") != sheet_name:
            continue
        rel_id = sheet.attrib.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
        target = id_to_target.get(rel_id or "")
        if not target:
            break
        if target.startswith("/"):
            return target.lstrip("/")
        return f"xl/{target}" if not target.startswith("xl/") else target
    raise FetchError(f"Could not find XLSX sheet: {sheet_name}")


def xlsx_column_index(cell_ref: str) -> int:
    match = re.match(r"([A-Z]+)", cell_ref)
    if not match:
        return 0
    index = 0
    for char in match.group(1):
        index = index * 26 + ord(char) - ord("A") + 1
    return index - 1


def read_xlsx_sheet(path: Path, sheet_name: str) -> list[list[str]]:
    namespace = {"a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with zipfile.ZipFile(path) as zipped:
        shared_strings = xlsx_shared_strings(zipped)
        sheet_xml = zipped.read(xlsx_sheet_path(zipped, sheet_name))
        root = ET.fromstring(sheet_xml)

        def cell_value(cell: ET.Element) -> str:
            if cell.attrib.get("t") == "inlineStr":
                return "".join(node.text or "" for node in cell.findall(".//a:t", namespace))
            value = cell.find("a:v", namespace)
            if value is None:
                return ""
            text = value.text or ""
            if cell.attrib.get("t") == "s":
                try:
                    return shared_strings[int(text)]
                except (ValueError, IndexError):
                    return text
            return text

        rows: list[list[str]] = []
        for row in root.findall("a:sheetData/a:row", namespace):
            values_by_index: dict[int, str] = {}
            for cell in row.findall("a:c", namespace):
                values_by_index[xlsx_column_index(cell.attrib.get("r", ""))] = cell_value(cell)
            if not values_by_index:
                rows.append([])
                continue
            max_index = max(values_by_index)
            rows.append([values_by_index.get(index, "") for index in range(max_index + 1)])
        return rows


def parse_gld_archive_date(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    for date_format in ["%d-%b-%Y", "%d-%B-%Y", "%Y-%m-%d"]:
        try:
            return dt.datetime.strptime(text, date_format).date().isoformat()
        except ValueError:
            pass
    numeric = parse_float(text)
    if numeric is not None:
        excel_epoch = dt.date(1899, 12, 30)
        return (excel_epoch + dt.timedelta(days=int(numeric))).isoformat()
    return None


def signed_number(value: Any, digits: int = 2, suffix: str = "") -> str:
    parsed = parse_float(value)
    if parsed is None:
        return "N/A"
    sign = "+" if parsed > 0 else ""
    return f"{sign}{parsed:,.{digits}f}{suffix}"


def format_compact_usd(value: Any) -> str:
    parsed = parse_float(value)
    if parsed is None:
        return "N/A"
    sign = "+" if parsed > 0 else "-" if parsed < 0 else ""
    absolute = abs(parsed)
    if absolute >= 1_000_000_000:
        return f"{sign}${absolute / 1_000_000_000:.2f}B"
    if absolute >= 1_000_000:
        return f"{sign}${absolute / 1_000_000:.1f}M"
    return f"{sign}${absolute:,.0f}"


def fetch_spdr_gld_archive(run_raw_dir: Path) -> Path:
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,*/*",
        "Referer": "https://www.spdrgoldshares.com/usa/gld/",
    }
    payload = http_get(SPDR_GLD_ARCHIVE_URL, timeout=30, headers=headers)
    run_raw_path = run_raw_dir / "spdr-gld-historical-archive.xlsx"
    run_raw_path.write_bytes(payload)
    store_path = gld_archive_store_path()
    store_path.parent.mkdir(parents=True, exist_ok=True)
    store_path.write_bytes(payload)
    return run_raw_path


def update_gld_etf_flow(run_raw_dir: Path) -> dict[str, Any]:
    raw_path = fetch_spdr_gld_archive(run_raw_dir)
    rows = read_xlsx_sheet(raw_path, "US GLD Historical Archive")
    if not rows:
        raise FetchError("SPDR GLD archive sheet is empty.")

    headers = [str(value).strip() for value in rows[0]]
    normalized: list[dict[str, Any]] = []
    for row in rows[1:]:
        record = {headers[index]: row[index] if index < len(row) else "" for index in range(len(headers))}
        date_text = parse_gld_archive_date(record.get("Date"))
        tonnes = parse_float(record.get("Tonnes of Gold"))
        total_ounces = parse_float(record.get("Total Ounces of Gold in the Trust"))
        if not date_text or tonnes is None or total_ounces is None:
            continue
        normalized.append(
            {
                "date": date_text,
                "closing_price": parse_float(record.get("Closing Price")),
                "ounces_per_share": parse_float(record.get("Ounces of Gold per Share")),
                "nav_per_share_1030_nyt": parse_float(record.get("NAV/Share at 10:30am NYT")),
                "indicative_price_415_nyt": parse_float(record.get("Indicative Price per Share at 4:15pm NYT")),
                "bid_ask_mid_415_nyt": parse_float(record.get("Mid point of bid/ask spread at 4:15pm NYT")),
                "premium_discount_pct": parse_float(record.get("Premium/Discount of GLD Mid Point vs Indicative Value of GLD at 4:15pm NYT")),
                "daily_share_volume": parse_float(record.get("Daily Share Volume")),
                "total_ounces": total_ounces,
                "tonnes": tonnes,
                "total_nav": parse_float(record.get("Total Net Asset Value in the Trust")),
            }
        )

    normalized = sorted(normalized, key=lambda item: item["date"])
    for index, row in enumerate(normalized):
        implied_gold_price = None
        if row.get("ounces_per_share"):
            implied_gold_price = row.get("nav_per_share_1030_nyt") / row.get("ounces_per_share")
        row["implied_gold_price_per_oz"] = implied_gold_price
        for lookback in [1, 5, 20]:
            previous = normalized[index - lookback] if index >= lookback else None
            ounces_change = row["total_ounces"] - previous["total_ounces"] if previous is not None else None
            row[f"ounces_change_{lookback}d"] = ounces_change
            row[f"tonnes_change_{lookback}d"] = (
                ounces_change / TROY_OUNCES_PER_METRIC_TONNE if ounces_change is not None else None
            )
            row[f"estimated_flow_usd_{lookback}d"] = (
                ounces_change * implied_gold_price
                if ounces_change is not None and implied_gold_price is not None
                else None
            )

    fieldnames = [
        "date",
        "closing_price",
        "ounces_per_share",
        "nav_per_share_1030_nyt",
        "indicative_price_415_nyt",
        "bid_ask_mid_415_nyt",
        "premium_discount_pct",
        "daily_share_volume",
        "total_ounces",
        "tonnes",
        "total_nav",
        "implied_gold_price_per_oz",
        "tonnes_change_1d",
        "tonnes_change_5d",
        "tonnes_change_20d",
        "ounces_change_1d",
        "ounces_change_5d",
        "ounces_change_20d",
        "estimated_flow_usd_1d",
        "estimated_flow_usd_5d",
        "estimated_flow_usd_20d",
    ]
    store_path = gld_etf_flow_store_path()
    write_csv_rows(store_path, normalized, fieldnames)
    latest = normalized[-1] if normalized else {}
    return {
        "store_file": rel(store_path),
        "archive_store_file": rel(gld_archive_store_path()),
        "raw_file": rel(raw_path),
        "source": "SPDR Gold Shares Historical Archive",
        "source_url": SPDR_GLD_ARCHIVE_URL,
        "rows": len(normalized),
        "latest": latest,
    }


def gld_etf_flow_latest() -> dict[str, Any]:
    rows = read_csv_rows(gld_etf_flow_store_path())
    if not rows:
        return {
            "status": "unavailable",
            "source": "SPDR Gold Shares Historical Archive",
            "note": "本地尚未抓取到 GLD 官方持仓历史。",
        }
    latest = rows[-1]
    return {
        "status": "available",
        "source": "SPDR Gold Shares Historical Archive",
        "date": latest.get("date"),
        "tonnes": parse_float(latest.get("tonnes")),
        "total_ounces": parse_float(latest.get("total_ounces")),
        "tonnes_change_1d": parse_float(latest.get("tonnes_change_1d")),
        "tonnes_change_5d": parse_float(latest.get("tonnes_change_5d")),
        "tonnes_change_20d": parse_float(latest.get("tonnes_change_20d")),
        "estimated_flow_usd_1d": parse_float(latest.get("estimated_flow_usd_1d")),
        "estimated_flow_usd_5d": parse_float(latest.get("estimated_flow_usd_5d")),
        "estimated_flow_usd_20d": parse_float(latest.get("estimated_flow_usd_20d")),
        "implied_gold_price_per_oz": parse_float(latest.get("implied_gold_price_per_oz")),
        "total_nav": parse_float(latest.get("total_nav")),
    }


def fetch_wgc_global_etf_flow_payload(run_raw_dir: Path) -> dict[str, Any]:
    page = http_get(
        WGC_GOLD_ETF_PAGE_URL,
        timeout=25,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "text/html,*/*",
        },
    ).decode("utf-8")
    endpoint_match = re.search(r'data-chart-data-endpoint="([^"]*archive-tablegroup/all[^"]*)"', page)
    if not endpoint_match:
        raise FetchError("WGC global ETF flow archive endpoint not found on the data page.")
    endpoint = html.unescape(endpoint_match.group(1))
    payload = json.loads(
        http_get(
            endpoint,
            timeout=30,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Accept": "application/json,text/plain,*/*",
                "Referer": WGC_GOLD_ETF_PAGE_URL,
            },
        ).decode("utf-8")
    )
    payload["_source_endpoint"] = endpoint
    payload["_fetched_at_calgary"] = now_calgary().isoformat()
    raw_path = run_raw_dir / "wgc-global-gold-etf-flow.json"
    write_json(raw_path, payload)
    write_json(wgc_global_etf_latest_json_store_path(), payload)
    return payload


def parse_wgc_global_etf_flow_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    regional = payload.get("chartData", {}).get("data", {}).get("regional", {})
    if not isinstance(regional, dict):
        raise FetchError("WGC global ETF flow payload missing regional data.")

    period_keys = {
        "Weekly": "weekly",
        "Monthly": "monthly",
        "Quarterly": "quarterly",
        "Year to date": "ytd",
    }
    rows: list[dict[str, Any]] = []
    fetched_at = payload.get("_fetched_at_calgary")
    for period_label, period_block in regional.items():
        period_key = period_keys.get(period_label, clean_symbol(str(period_label)).lower())
        table = period_block.get("data", {}) if isinstance(period_block, dict) else {}
        as_of_date = period_block.get("asOfDate") if isinstance(period_block, dict) else None
        period_title = period_block.get("periodTitle") if isinstance(period_block, dict) else None
        for row_key, row_values in table.items():
            if row_key == "columns" or not isinstance(row_values, list) or len(row_values) < 6:
                continue
            rows.append(
                {
                    "period": period_key,
                    "period_label": period_label,
                    "period_title": period_title,
                    "as_of_date": as_of_date,
                    "region": row_values[0],
                    "aum_bn": parse_float(row_values[1]),
                    "fund_flows_usd_mn": parse_float(row_values[2]),
                    "holdings_tonnes": parse_float(row_values[3]),
                    "demand_tonnes": parse_float(row_values[4]),
                    "demand_pct_holdings": parse_float(row_values[5]),
                    "fetched_at_calgary": fetched_at,
                }
            )
    if not rows:
        raise FetchError("WGC global ETF flow payload produced no rows.")
    return rows


def update_wgc_global_etf_flow(run_raw_dir: Path) -> dict[str, Any]:
    payload = fetch_wgc_global_etf_flow_payload(run_raw_dir)
    rows = parse_wgc_global_etf_flow_payload(payload)
    fieldnames = [
        "period",
        "period_label",
        "period_title",
        "as_of_date",
        "region",
        "aum_bn",
        "fund_flows_usd_mn",
        "holdings_tonnes",
        "demand_tonnes",
        "demand_pct_holdings",
        "fetched_at_calgary",
    ]
    store_path = wgc_global_etf_flow_store_path()
    write_csv_rows(store_path, rows, fieldnames)
    total_rows = {row["period"]: row for row in rows if row.get("region") == "Total"}
    return {
        "store_file": rel(store_path),
        "latest_json_store_file": rel(wgc_global_etf_latest_json_store_path()),
        "raw_file": rel(run_raw_dir / "wgc-global-gold-etf-flow.json"),
        "source": "World Gold Council Gold ETFs holdings and flows",
        "source_url": WGC_GOLD_ETF_PAGE_URL,
        "source_endpoint": payload.get("_source_endpoint"),
        "rows": len(rows),
        "latest": total_rows,
    }


def wgc_global_etf_flow_latest() -> dict[str, Any]:
    rows = read_csv_rows(wgc_global_etf_flow_store_path())
    if not rows:
        return {
            "status": "unavailable",
            "source": "World Gold Council Gold ETFs holdings and flows",
            "source_url": WGC_GOLD_ETF_PAGE_URL,
            "note": "本地尚未抓取到 WGC 全球黄金 ETF 资金流。",
        }
    periods: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row.get("region") != "Total":
            continue
        periods[row.get("period") or "unknown"] = {
            "period_label": row.get("period_label"),
            "period_title": row.get("period_title"),
            "as_of_date": row.get("as_of_date"),
            "aum_bn": parse_float(row.get("aum_bn")),
            "fund_flows_usd_mn": parse_float(row.get("fund_flows_usd_mn")),
            "holdings_tonnes": parse_float(row.get("holdings_tonnes")),
            "demand_tonnes": parse_float(row.get("demand_tonnes")),
            "demand_pct_holdings": parse_float(row.get("demand_pct_holdings")),
            "fetched_at_calgary": row.get("fetched_at_calgary"),
        }
    if not periods:
        return {
            "status": "unavailable",
            "source": "World Gold Council Gold ETFs holdings and flows",
            "source_url": WGC_GOLD_ETF_PAGE_URL,
            "note": "WGC 本地数据没有 Total 汇总行。",
        }
    return {
        "status": "available",
        "source": "World Gold Council Gold ETFs holdings and flows",
        "source_url": WGC_GOLD_ETF_PAGE_URL,
        "periods": periods,
    }


def make_yahoo_opener(timeout: int) -> tuple[urllib.request.OpenerDirector, str]:
    cookie_jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))
    try:
        crumb = http_get(YAHOO_CRUMB_URL, opener=opener, timeout=timeout).decode("utf-8").strip()
    except FetchError:
        # Yahoo sometimes returns 401 until a normal finance page sets cookies.
        http_get(
            "https://finance.yahoo.com/quote/GLD/options/",
            opener=opener,
            timeout=timeout,
            headers={"User-Agent": "Mozilla/5.0", "Accept": "text/html,*/*"},
        )
        crumb = http_get(YAHOO_CRUMB_URL, opener=opener, timeout=timeout).decode("utf-8").strip()
    if not crumb:
        raise FetchError("Yahoo crumb endpoint returned an empty crumb.")
    return opener, crumb


def fetch_yahoo_options_put_call(symbol: str, max_expirations: int, timeout: int) -> dict[str, Any]:
    requested_at = now_calgary()
    opener, crumb = make_yahoo_opener(timeout)

    def fetch(date_value: int | None = None) -> dict[str, Any]:
        params_data: dict[str, Any] = {"crumb": crumb, "formatted": "false"}
        if date_value is not None:
            params_data["date"] = date_value
        params = urllib.parse.urlencode(params_data)
        url = YAHOO_OPTIONS_URL.format(symbol=urllib.parse.quote(symbol), params=params)
        return json.loads(http_get(url, opener=opener, timeout=timeout).decode("utf-8"))

    first = fetch()
    result = first["optionChain"]["result"][0]
    expirations = result.get("expirationDates", [])[:max_expirations]
    payloads = [first]
    for expiration in expirations[1:]:
        payloads.append(fetch(expiration))

    call_volume = put_volume = call_oi = put_oi = 0
    expirations_used: list[str] = []
    for payload in payloads:
        chain = payload["optionChain"]["result"][0]
        options = chain.get("options") or []
        if not options:
            continue
        block = options[0]
        expiration = block.get("expirationDate")
        if expiration:
            expirations_used.append(dt.datetime.fromtimestamp(expiration, dt.UTC).date().isoformat())
        for option in block.get("calls") or []:
            call_volume += parse_int(option.get("volume")) or 0
            call_oi += parse_int(option.get("openInterest")) or 0
        for option in block.get("puts") or []:
            put_volume += parse_int(option.get("volume")) or 0
            put_oi += parse_int(option.get("openInterest")) or 0

    return {
        "symbol": symbol,
        "source": "Yahoo Finance options chain",
        "proxy_note": "GLD options proxy for gold sentiment; not COMEX GC futures options.",
        "data_state": "fresh",
        "fetched_at_calgary": now_calgary().isoformat(),
        "requested_at_calgary": requested_at.isoformat(),
        "expirations_used": expirations_used,
        "call_volume": call_volume,
        "put_volume": put_volume,
        "put_call_volume_ratio": put_volume / call_volume if call_volume else None,
        "call_open_interest": call_oi,
        "put_open_interest": put_oi,
        "put_call_open_interest_ratio": put_oi / call_oi if call_oi else None,
    }


def cache_options_result(options_result: dict[str, Any]) -> None:
    symbol = options_result.get("symbol")
    if not symbol or options_result.get("status"):
        return
    write_json(options_cache_path(str(symbol)), options_result)


def load_cached_options_result(symbol: str, data_state: str) -> dict[str, Any] | None:
    path = options_cache_path(symbol)
    if not path.exists():
        return None
    payload = read_json(path)
    if not isinstance(payload, dict) or payload.get("status"):
        return None
    payload = dict(payload)
    payload["data_state"] = data_state
    payload["cache_file"] = rel(path)
    if not payload.get("fetched_at_calgary"):
        modified = dt.datetime.fromtimestamp(path.stat().st_mtime, CALGARY_TZ)
        payload["fetched_at_calgary"] = modified.isoformat()
    payload["cache_loaded_at_calgary"] = now_calgary().isoformat()
    return payload


def update_gold_silver_ratio(interval: str) -> dict[str, Any]:
    xau_rows = read_csv_rows(spot_store_path("XAU_USD", interval))
    xag_rows = read_csv_rows(spot_store_path("XAG_USD", interval))
    source = "OANDA XAU_USD / XAG_USD spot"
    gold_key = "xauusd_close"
    silver_key = "xagusd_close"

    if not xau_rows or not xag_rows:
        xau_rows = read_csv_rows(yahoo_store_path("GC=F", interval))
        xag_rows = read_csv_rows(yahoo_store_path("SI=F", interval))
        source = "fallback Yahoo GC=F / SI=F futures"
        gold_key = "gc_close"
        silver_key = "si_close"

    if interval == "1d":
        xau_by_key = {row["timestamp_calgary"][:10]: row for row in xau_rows}
        xag_by_key = {row["timestamp_calgary"][:10]: row for row in xag_rows}
    else:
        xau_by_key = {row["timestamp_utc"][:13]: row for row in xau_rows}
        xag_by_key = {row["timestamp_utc"][:13]: row for row in xag_rows}

    ratio_rows: list[dict[str, Any]] = []
    for key in sorted(set(xau_by_key) & set(xag_by_key)):
        xau_close = parse_float(xau_by_key[key].get("close"))
        xag_close = parse_float(xag_by_key[key].get("close"))
        if not xau_close or not xag_close:
            continue
        ratio_rows.append(
            {
                "timestamp_key": key,
                "timestamp_utc": xau_by_key[key]["timestamp_utc"],
                "timestamp_calgary": xau_by_key[key]["timestamp_calgary"],
                gold_key: xau_close,
                silver_key: xag_close,
                "gold_silver_ratio": xau_close / xag_close,
                "source": source,
            }
        )

    path = ratio_store_path(interval)
    write_csv_rows(
        path,
        ratio_rows,
        [
            "timestamp_key",
            "timestamp_utc",
            "timestamp_calgary",
            "xauusd_close",
            "xagusd_close",
            "gc_close",
            "si_close",
            "gold_silver_ratio",
            "source",
        ],
    )
    return {
        "interval": interval,
        "store_file": rel(path),
        "rows": len(ratio_rows),
        "latest": ratio_rows[-1] if ratio_rows else None,
    }


def closes(rows: list[dict[str, Any]]) -> list[float]:
    return [value for value in (parse_float(row.get("close")) for row in rows) if value is not None]


def volumes(rows: list[dict[str, Any]]) -> list[float]:
    return [parse_float(row.get("volume")) or 0.0 for row in rows]


def latest_row(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    return rows[-1] if rows else None


def rsi_wilder(values: list[float], period: int = 14) -> float | None:
    if len(values) <= period:
        return None
    gains = []
    losses = []
    for index in range(1, period + 1):
        change = values[index] - values[index - 1]
        gains.append(max(change, 0.0))
        losses.append(abs(min(change, 0.0)))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    for index in range(period + 1, len(values)):
        change = values[index] - values[index - 1]
        gain = max(change, 0.0)
        loss = abs(min(change, 0.0))
        avg_gain = ((avg_gain * (period - 1)) + gain) / period
        avg_loss = ((avg_loss * (period - 1)) + loss) / period
    if avg_loss == 0:
        return 100.0
    rs_value = avg_gain / avg_loss
    return 100 - (100 / (1 + rs_value))


def bollinger_percent_b(values: list[float], period: int = 20, multiplier: float = 2.0) -> dict[str, float | None]:
    if len(values) < period:
        return {"percent_b": None, "middle": None, "upper": None, "lower": None}
    window = values[-period:]
    middle = sum(window) / period
    std = statistics.pstdev(window)
    upper = middle + multiplier * std
    lower = middle - multiplier * std
    if upper == lower:
        percent_b = None
    else:
        percent_b = (values[-1] - lower) / (upper - lower)
    return {"percent_b": percent_b, "middle": middle, "upper": upper, "lower": lower}


def td_setup(values: list[float]) -> dict[str, Any]:
    if len(values) < 5:
        return {"direction": None, "count": None, "display": None}
    buy_count = 0
    sell_count = 0
    direction = None
    for index in range(4, len(values)):
        if values[index] > values[index - 4]:
            sell_count += 1
            buy_count = 0
            direction = "sell"
        elif values[index] < values[index - 4]:
            buy_count += 1
            sell_count = 0
            direction = "buy"
        else:
            buy_count = 0
            sell_count = 0
            direction = None
    count = sell_count if direction == "sell" else buy_count if direction == "buy" else None
    prefix = "S" if direction == "sell" else "B" if direction == "buy" else "-"
    return {"direction": direction, "count": count, "display": f"{prefix}{count}" if count else None}


def td_countdown(rows: list[dict[str, Any]], direction: str | None, lookback: int = 40) -> dict[str, Any]:
    if len(rows) < 3 or direction not in {"buy", "sell"}:
        return {"direction": direction, "count": None, "display": None}

    recent = rows[-lookback:]
    count = 0
    for index in range(2, len(recent)):
        close_value = parse_float(recent[index].get("close"))
        if close_value is None:
            continue
        if direction == "buy":
            low_two_back = parse_float(recent[index - 2].get("low"))
            if low_two_back is not None and close_value <= low_two_back:
                count += 1
        else:
            high_two_back = parse_float(recent[index - 2].get("high"))
            if high_two_back is not None and close_value >= high_two_back:
                count += 1
    count = min(count, 13)
    prefix = "BCD" if direction == "buy" else "SCD"
    return {"direction": direction, "count": count, "display": f"{prefix}{count}"}


def td9_cd13(rows: list[dict[str, Any]]) -> dict[str, Any]:
    setup = td_setup(closes(rows))
    countdown = td_countdown(rows, setup.get("direction"))
    display = None
    if setup.get("display") and countdown.get("display"):
        display = f"{setup['display']}/{countdown['display']}"
    return {"setup": setup, "countdown": countdown, "display": display}


def resample_weekly(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for row in rows:
        date_value = dt.datetime.fromisoformat(row["timestamp_calgary"]).date()
        iso_year, iso_week, _ = date_value.isocalendar()
        grouped.setdefault((iso_year, iso_week), []).append(row)

    weekly_rows: list[dict[str, Any]] = []
    for key in sorted(grouped):
        bucket = sorted(grouped[key], key=lambda row: row["timestamp_utc"])
        first = bucket[0]
        last = bucket[-1]
        highs = [parse_float(row.get("high")) for row in bucket if parse_float(row.get("high")) is not None]
        lows = [parse_float(row.get("low")) for row in bucket if parse_float(row.get("low")) is not None]
        weekly_rows.append(
            {
                "timestamp_utc": last["timestamp_utc"],
                "timestamp_calgary": last["timestamp_calgary"],
                "open": parse_float(first.get("open")),
                "high": max(highs) if highs else None,
                "low": min(lows) if lows else None,
                "close": parse_float(last.get("close")),
                "volume": sum(parse_float(row.get("volume")) or 0 for row in bucket),
            }
        )
    return weekly_rows


def obv_vpt(rows: list[dict[str, Any]], lookback: int = 20) -> dict[str, Any]:
    values = closes(rows)
    vols = volumes(rows)
    if len(values) < lookback + 1 or len(values) != len(vols):
        return {
            "status": "insufficient_data",
            "timestamp_calgary": (rows[-1].get("timestamp_calgary") if rows else None),
        }

    obv_values = [0.0]
    vpt_values = [0.0]
    for index in range(1, len(values)):
        if values[index] > values[index - 1]:
            obv_values.append(obv_values[-1] + vols[index])
        elif values[index] < values[index - 1]:
            obv_values.append(obv_values[-1] - vols[index])
        else:
            obv_values.append(obv_values[-1])

        previous_close = values[index - 1]
        if previous_close:
            vpt_values.append(vpt_values[-1] + vols[index] * (values[index] - previous_close) / previous_close)
        else:
            vpt_values.append(vpt_values[-1])

    price_change_pct = (values[-1] / values[-lookback - 1] - 1) * 100
    obv_delta = obv_values[-1] - obv_values[-lookback - 1]
    vpt_delta = vpt_values[-1] - vpt_values[-lookback - 1]
    obv_direction = "UP" if obv_delta > 0 else "DN" if obv_delta < 0 else "FLAT"
    vpt_direction = "UP" if vpt_delta > 0 else "DN" if vpt_delta < 0 else "FLAT"

    if price_change_pct > 0 and (obv_delta < 0 or vpt_delta < 0):
        signal = "bearish_volume_divergence"
    elif price_change_pct < 0 and (obv_delta > 0 or vpt_delta > 0):
        signal = "bullish_accumulation_divergence"
    else:
        signal = "volume_price_aligned"

    return {
        "lookback_bars": lookback,
        "price_change_pct": price_change_pct,
        "obv_latest": obv_values[-1],
        "obv_delta": obv_delta,
        "obv_direction": obv_direction,
        "vpt_latest": vpt_values[-1],
        "vpt_delta": vpt_delta,
        "vpt_direction": vpt_direction,
        "display": f"V:{vpt_direction} | O:{obv_direction}",
        "signal": signal,
        "timestamp_calgary": rows[-1].get("timestamp_calgary") if rows else None,
    }


def percentile_rank(values: list[float], latest: float) -> float | None:
    if not values:
        return None
    below_or_equal = sum(1 for value in values if value <= latest)
    return below_or_equal / len(values) * 100


def fred_latest(series_id: str) -> dict[str, Any]:
    rows = read_csv_rows(fred_store_path(series_id))
    if not rows:
        return {"series_id": series_id, "latest_date": None, "latest_value": None}
    latest = rows[-1]
    values = [parse_float(row.get("value")) for row in rows if parse_float(row.get("value")) is not None]
    latest_value = parse_float(latest.get("value"))
    return {
        "series_id": series_id,
        "latest_date": latest.get("date"),
        "latest_value": latest_value,
        "change_5_obs": latest_value - values[-6] if latest_value is not None and len(values) >= 6 else None,
        "change_20_obs": latest_value - values[-21] if latest_value is not None and len(values) >= 21 else None,
    }


def load_ohlcv(symbol: str, interval: str) -> list[dict[str, Any]]:
    return read_csv_rows(yahoo_store_path(symbol, interval))


def load_spot_ohlc(instrument: str, interval: str) -> list[dict[str, Any]]:
    rows = read_csv_rows(spot_store_path(instrument, interval))
    if rows:
        return rows
    return read_csv_rows(legacy_oanda_store_path(instrument, interval))


def merge_spot_price_with_volume(
    price_rows: list[dict[str, Any]],
    volume_rows: list[dict[str, Any]],
    interval: str,
) -> list[dict[str, Any]]:
    if interval == "1d":
        volume_by_key = {row["timestamp_calgary"][:10]: row for row in volume_rows if row.get("timestamp_calgary")}
        key_for_price = lambda row: row["timestamp_calgary"][:10]
    else:
        volume_by_key = {row["timestamp_utc"][:13]: row for row in volume_rows if row.get("timestamp_utc")}
        key_for_price = lambda row: row["timestamp_utc"][:13]

    merged: list[dict[str, Any]] = []
    for price_row in price_rows:
        if not price_row.get("timestamp_calgary") or not price_row.get("timestamp_utc"):
            continue
        volume_row = volume_by_key.get(key_for_price(price_row))
        if not volume_row:
            continue
        merged.append(
            {
                "timestamp_utc": price_row.get("timestamp_utc"),
                "timestamp_calgary": price_row.get("timestamp_calgary"),
                "open": price_row.get("open"),
                "high": price_row.get("high"),
                "low": price_row.get("low"),
                "close": price_row.get("close"),
                "volume": volume_row.get("volume"),
            }
        )
    return merged


def compute_indicators(options_result: dict[str, Any] | None) -> dict[str, Any]:
    xau_daily = load_spot_ohlc("XAU_USD", "1d")
    xau_hourly = load_spot_ohlc("XAU_USD", "1h")
    xag_daily = load_spot_ohlc("XAG_USD", "1d")
    xag_hourly = load_spot_ohlc("XAG_USD", "1h")
    gc_daily = load_ohlcv("GC=F", "1d")
    gld_daily = load_ohlcv("GLD", "1d")
    dxy_daily = load_ohlcv("DX-Y.NYB", "1d")
    gvz_daily = load_ohlcv("^GVZ", "1d")
    ratio_daily = read_csv_rows(ratio_store_path("1d"))
    ratio_hourly = read_csv_rows(ratio_store_path("1h"))
    cot_rows = read_csv_rows(cftc_store_path())

    primary_daily = xau_daily or gc_daily
    primary_source = "OANDA XAU_USD spot_mid" if xau_daily else "fallback Yahoo GC=F futures"
    primary_weekly = resample_weekly(primary_daily)
    primary_closes = closes(primary_daily)
    weekly_closes = closes(primary_weekly)
    dxy_closes = closes(dxy_daily)
    gvz_closes = closes(gvz_daily)
    volume_proxy_rows = merge_spot_price_with_volume(primary_daily, gc_daily, "1d")
    if not volume_proxy_rows:
        volume_proxy_rows = gld_daily

    latest_primary = latest_row(primary_daily) or {}
    latest_primary_weekly = latest_row(primary_weekly) or {}
    latest_xau_spot = latest_row(xau_hourly) or latest_primary
    latest_xag = latest_row(xag_daily) or {}
    latest_xag_spot = latest_row(xag_hourly) or latest_xag
    latest_gc = latest_row(gc_daily) or {}
    latest_dxy = latest_row(dxy_daily) or {}
    latest_gvz = latest_row(gvz_daily) or {}
    latest_ratio = latest_row(ratio_hourly) or latest_row(ratio_daily) or {}
    latest_cot = latest_row(cot_rows) or {}

    bb = bollinger_percent_b(primary_closes)
    gvz_latest = parse_float(latest_gvz.get("close"))
    gvz_window = gvz_closes[-252:] if len(gvz_closes) >= 20 else gvz_closes
    dxy_latest = parse_float(latest_dxy.get("close"))

    indicators = {
        "market_reference": {
            "gold_source": primary_source,
            "gold_price": parse_float(latest_xau_spot.get("close")),
            "gold_day_high": parse_float(latest_primary.get("high")),
            "gold_day_low": parse_float(latest_primary.get("low")),
            "timestamp_calgary": latest_xau_spot.get("timestamp_calgary"),
            "gold_daily_timestamp_calgary": latest_primary.get("timestamp_calgary"),
            "gold_weekly_timestamp_calgary": latest_primary_weekly.get("timestamp_calgary"),
            "xagusd_price": parse_float(latest_xag_spot.get("close")),
            "xagusd_timestamp_calgary": latest_xag_spot.get("timestamp_calgary"),
            "gc_futures_price": parse_float(latest_gc.get("close")),
            "gc_futures_volume": parse_float(latest_gc.get("volume")),
            "gc_timestamp_calgary": latest_gc.get("timestamp_calgary"),
        },
        "core_indicators": {
            "rsi": {
                "daily": rsi_wilder(primary_closes),
                "weekly": rsi_wilder(weekly_closes),
                "source": f"computed_from_{primary_source}",
            },
            "td9_cd13": {
                "daily": td9_cd13(primary_daily),
                "weekly": td9_cd13(primary_weekly),
                "source": f"computed_from_{primary_source}",
                "note": "TD9 计数 + 简化 CD13 倒计时近似计算。",
            },
            "bollinger_percent_b": {
                **bb,
                "source": f"computed_from_{primary_source}",
            },
            "vpt_obv": {
                "futures_volume_proxy": obv_vpt(volume_proxy_rows),
                "source": "computed_from_XAU_USD_spot_close_with_GC=F_exchange_volume_proxy",
            },
            "gold_silver_ratio": {
                "value": parse_float(latest_ratio.get("gold_silver_ratio")),
                "xauusd_close": parse_float(latest_ratio.get("xauusd_close") or latest_ratio.get("gc_close")),
                "xagusd_close": parse_float(latest_ratio.get("xagusd_close") or latest_ratio.get("si_close")),
                "timestamp_calgary": latest_ratio.get("timestamp_calgary"),
                "source": latest_ratio.get("source") or "OANDA XAU_USD / XAG_USD spot",
            },
            "dxy": {
                "value": dxy_latest,
                "timestamp_calgary": latest_dxy.get("timestamp_calgary"),
                "change_20_bars": dxy_latest - dxy_closes[-21] if dxy_latest is not None and len(dxy_closes) >= 21 else None,
                "source": "Yahoo DX-Y.NYB",
            },
            "real_yield_10y": {
                **fred_latest("DFII10"),
                "source": "FRED DFII10",
            },
            "gvz": {
                "value": gvz_latest,
                "timestamp_calgary": latest_gvz.get("timestamp_calgary"),
                "percentile_1y": percentile_rank(gvz_window, gvz_latest) if gvz_latest is not None else None,
                "source": "Yahoo ^GVZ",
            },
            "cot_commercial_net": {
                "report_date": latest_cot.get("report_date"),
                "commercial_long": parse_int(latest_cot.get("commercial_long")),
                "commercial_short": parse_int(latest_cot.get("commercial_short")),
                "commercial_net": parse_int(latest_cot.get("commercial_net")),
                "commercial_net_short": parse_int(latest_cot.get("commercial_net_short")),
                "source": "CFTC legacy futures-only metals report",
            },
            "put_call_ratio": {
                "gld_options_proxy": options_result,
                "source": "Yahoo GLD options chain when available",
            },
        },
        "extra_indicators": {
            "breakeven_inflation": {
                "t10yie": fred_latest("T10YIE"),
                "t5yie": fred_latest("T5YIE"),
                "source": "FRED",
            },
            "gld_etf_flow": {
                **gld_etf_flow_latest(),
            },
            "wgc_global_etf_flow": {
                **wgc_global_etf_flow_latest(),
            },
            "wgc_central_bank_gold": {
                "status": "not_automated_in_this_script",
                "note": "WGC 央行购金是低频数据，常见形态是月度/季度 XLSX 或网页数据。",
            },
        },
    }
    return indicators


def score_model(indicators: dict[str, Any]) -> dict[str, Any]:
    core = indicators["core_indicators"]
    bearish = 0
    rebound = 0
    notes: list[str] = []

    rsi_daily = core["rsi"]["daily"]
    rsi_weekly = core["rsi"]["weekly"]
    if rsi_daily is not None:
        if rsi_daily >= 70:
            bearish += 2
            notes.append("Daily RSI is overbought.")
        elif rsi_daily <= 30:
            rebound += 2
            notes.append("Daily RSI is oversold.")
    if rsi_weekly is not None:
        if rsi_weekly >= 70:
            bearish += 1
        elif rsi_weekly <= 30:
            rebound += 1

    td_daily = core["td9_cd13"]["daily"]["setup"]
    td_weekly = core["td9_cd13"]["weekly"]["setup"]
    for label, setup in [("Daily", td_daily), ("Weekly", td_weekly)]:
        direction = setup.get("direction")
        count = setup.get("count") or 0
        if direction == "sell" and count >= 9:
            bearish += 2
            notes.append(f"{label} TD sell setup is extended.")
        if direction == "buy" and count >= 9:
            rebound += 2
            notes.append(f"{label} TD buy setup is extended.")

    percent_b = core["bollinger_percent_b"]["percent_b"]
    if percent_b is not None:
        if percent_b > 1:
            bearish += 2
            notes.append("Bollinger %B is above the upper band.")
        elif percent_b < 0:
            rebound += 2
            notes.append("Bollinger %B is below the lower band.")

    volume_signal = core["vpt_obv"]["futures_volume_proxy"].get("signal")
    if volume_signal == "bearish_volume_divergence":
        bearish += 2
        notes.append("XAUUSD price with GC futures volume proxy shows bearish volume divergence.")
    elif volume_signal == "bullish_accumulation_divergence":
        rebound += 2
        notes.append("XAUUSD price with GC futures volume proxy shows accumulation divergence.")

    dxy_change = core["dxy"].get("change_20_bars")
    if dxy_change is not None:
        if dxy_change > 0:
            bearish += 1
        elif dxy_change < 0:
            rebound += 1

    real_yield_change = core["real_yield_10y"].get("change_20_obs")
    if real_yield_change is not None:
        if real_yield_change > 0:
            bearish += 2
        elif real_yield_change < 0:
            rebound += 2

    gvz_percentile = core["gvz"].get("percentile_1y")
    if gvz_percentile is not None and gvz_percentile >= 80:
        rebound += 1
        bearish += 1
        notes.append("GVZ is in a high-volatility zone.")

    pc = core["put_call_ratio"]["gld_options_proxy"] or {}
    pc_oi = pc.get("put_call_open_interest_ratio")
    if pc_oi is not None:
        if pc_oi < 0.65:
            bearish += 1
            notes.append("GLD options P/C proxy is low, suggesting complacency.")
        elif pc_oi > 1.2:
            rebound += 1
            notes.append("GLD options P/C proxy is high, suggesting fear hedging.")

    total = bearish + rebound
    if total == 0:
        primary = "neutral / insufficient signal"
        probability = 50
    elif bearish > rebound:
        primary = "downside pressure / rebound failure risk"
        probability = round(50 + min(35, (bearish - rebound) / max(total, 1) * 50))
    elif rebound > bearish:
        primary = "oversold rebound / downside exhaustion risk"
        probability = round(50 + min(35, (rebound - bearish) / max(total, 1) * 50))
    else:
        primary = "range-bound / mixed signal"
        probability = 50

    confidence = min(90, 50 + total * 4)
    return {
        "model_state_en": primary,
        "model_state_zh": {
            "downside pressure / rebound failure risk": "下行压力 / 反弹乏力风险",
            "oversold rebound / downside exhaustion risk": "超卖反抽 / 下跌衰竭风险",
            "range-bound / mixed signal": "横盘震荡 / 信号混合",
            "neutral / insufficient signal": "中性 / 信号不足",
        }.get(primary, primary),
        "primary_path_1_2w": primary,
        "primary_path_probability": probability,
        "state_confidence": confidence,
        "score": {
            "bearish_pressure": bearish,
            "rebound_pressure": rebound,
        },
        "notes": notes,
        "warning": "This is a mechanical indicator summary, not investment advice and not a replacement for the final human/LLM report interpretation.",
    }


def build_data_lineage(indicators: dict[str, Any]) -> dict[str, Any]:
    series = []
    for instrument, interval in [
        ("XAU_USD", "1d"),
        ("XAU_USD", "1h"),
        ("XAG_USD", "1d"),
        ("XAG_USD", "1h"),
    ]:
        path = spot_store_path(instrument, interval)
        rows = read_csv_rows(path)
        series.append(
            {
                "name": f"OANDA {instrument} {interval}",
                "source_store": rel(path),
                "used_from": rows[0]["timestamp_calgary"] if rows else None,
                "used_to": rows[-1]["timestamp_calgary"] if rows else None,
                "rows_used": len(rows),
            }
        )
    for symbol, interval in [
        ("GC=F", "1d"),
        ("GC=F", "1h"),
        ("SI=F", "1d"),
        ("SI=F", "1h"),
        ("GLD", "1d"),
        ("DX-Y.NYB", "1d"),
        ("^GVZ", "1d"),
    ]:
        path = yahoo_store_path(symbol, interval)
        rows = read_csv_rows(path)
        series.append(
            {
                "name": f"{symbol} {interval}",
                "source_store": rel(path),
                "used_from": rows[0]["timestamp_calgary"] if rows else None,
                "used_to": rows[-1]["timestamp_calgary"] if rows else None,
                "rows_used": len(rows),
            }
        )
    for series_id in FRED_SERIES:
        path = fred_store_path(series_id)
        rows = read_csv_rows(path)
        series.append(
            {
                "name": series_id,
                "source_store": rel(path),
                "used_from": rows[0]["date"] if rows else None,
                "used_to": rows[-1]["date"] if rows else None,
                "rows_used": len(rows),
            }
        )
    series.append(
        {
            "name": "CFTC gold commercial net",
            "source_store": rel(cftc_store_path()),
            "used_to": indicators["core_indicators"]["cot_commercial_net"].get("report_date"),
            "rows_used": len(read_csv_rows(cftc_store_path())),
        }
    )
    gld_flow_rows = read_csv_rows(gld_etf_flow_store_path())
    series.append(
        {
            "name": "SPDR GLD ETF flow",
            "source_store": rel(gld_etf_flow_store_path()),
            "used_from": gld_flow_rows[0]["date"] if gld_flow_rows else None,
            "used_to": gld_flow_rows[-1]["date"] if gld_flow_rows else None,
            "rows_used": len(gld_flow_rows),
        }
    )
    wgc_etf_rows = read_csv_rows(wgc_global_etf_flow_store_path())
    wgc_as_of_dates = sorted({row.get("as_of_date") for row in wgc_etf_rows if row.get("as_of_date")})
    series.append(
        {
            "name": "WGC global gold ETF flow",
            "source_store": rel(wgc_global_etf_flow_store_path()),
            "used_from": wgc_as_of_dates[0] if wgc_as_of_dates else None,
            "used_to": wgc_as_of_dates[-1] if wgc_as_of_dates else None,
            "rows_used": len(wgc_etf_rows),
        }
    )
    return {"series": series}


def format_number(value: Any, digits: int = 2, suffix: str = "") -> str:
    parsed = parse_float(value)
    if parsed is None:
        return "N/A"
    return f"{parsed:,.{digits}f}{suffix}"


def format_int(value: Any) -> str:
    parsed = parse_int(value)
    if parsed is None:
        return "N/A"
    return f"{parsed:,}"


def format_calgary_datetime(value: Any) -> str:
    if not value:
        return "N/A"
    try:
        parsed = dt.datetime.fromisoformat(str(value))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=CALGARY_TZ)
        parsed = parsed.astimezone(CALGARY_TZ)
        return parsed.strftime("%m/%d/%Y %H:%M:%S Calgary")
    except ValueError:
        return str(value)


def format_date(value: Any) -> str:
    if not value:
        return "N/A"
    try:
        parsed = dt.date.fromisoformat(str(value)[:10])
        return parsed.strftime("%m/%d/%Y")
    except ValueError:
        return str(value)


def next_weekday(value: dt.date) -> dt.date:
    next_day = value + dt.timedelta(days=1)
    while next_day.weekday() >= 5:
        next_day += dt.timedelta(days=1)
    return next_day


def next_cot_text(report_date_text: str | None) -> str:
    if not report_date_text:
        return "每周五通常公布；本次无可用报告日期"
    try:
        report_date = dt.date.fromisoformat(report_date_text)
    except ValueError:
        return "每周五通常公布；遇美国假期可能顺延"
    next_as_of = report_date + dt.timedelta(days=7)
    expected_release = next_as_of + dt.timedelta(days=3)
    return (
        f"下一期数据截止日约 {format_date(next_as_of.isoformat())}；"
        f"通常 {format_date(expected_release.isoformat())} 周五公布，遇美国假期顺延"
    )


def fred_next_text(latest_date_text: str | None) -> str:
    if not latest_date_text:
        return "美国工作日更新；本次未取得最新日期"
    try:
        latest_date = dt.date.fromisoformat(latest_date_text)
    except ValueError:
        return "美国工作日更新"
    return f"预计下一个美国工作日 {format_date(next_weekday(latest_date).isoformat())} 更新"


def html_escape(value: Any) -> str:
    if value is None:
        return "N/A"
    return html.escape(str(value), quote=True)


def human_status(value: Any) -> str:
    if value in {None, "", "N/A"}:
        return "N/A"
    mapping = {
        "not_automated_in_this_script": "本版脚本未自动化",
        "unavailable": "本次不可用",
    }
    return mapping.get(str(value), str(value))


def human_signal(value: Any) -> str:
    mapping = {
        "volume_price_aligned": "量价同步",
        "bearish_volume_divergence": "量价背离偏空",
        "bullish_accumulation_divergence": "量价背离偏多/吸筹",
        "insufficient_data": "数据不足",
    }
    if value is None:
        return "N/A"
    return mapping.get(str(value), str(value))


def find_yahoo_update(summary: dict[str, Any], symbol: str, interval: str) -> dict[str, Any] | None:
    for update in summary.get("store_updates", {}).get("yahoo", []):
        if update.get("symbol") == symbol and update.get("interval") == interval:
            return update
    return None


def render_status_badge(value: Any) -> str:
    if value in {None, "", "N/A"}:
        return '<span class="badge muted">N/A</span>'
    return f'<span class="badge">{html_escape(value)}</span>'


def render_metric_card(label: str, value: str, meta: str = "") -> str:
    return f"""
      <section class="metric-card">
        <div class="metric-label">{html_escape(label)}</div>
        <div class="metric-value">{html_escape(value)}</div>
        <div class="metric-meta">{html_escape(meta)}</div>
      </section>
    """


def render_table(headers: list[str], rows: list[list[str]]) -> str:
    header_html = "".join(f"<th>{html_escape(header)}</th>" for header in headers)
    row_html = []
    for row in rows:
        row_html.append("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>")
    return f"""
      <table>
        <thead><tr>{header_html}</tr></thead>
        <tbody>{''.join(row_html)}</tbody>
      </table>
    """


def render_data_html(summary: dict[str, Any], indicators: dict[str, Any], data_lineage: dict[str, Any]) -> str:
    core = indicators["core_indicators"]
    extra = indicators.get("extra_indicators", {})
    market = indicators["market_reference"]

    gc_update = find_yahoo_update(summary, "GC=F", "1d")
    si_update = find_yahoo_update(summary, "SI=F", "1d")
    gld_update = find_yahoo_update(summary, "GLD", "1d")

    gold_price = format_number(market.get("gold_price"), 2)
    gold_time = format_calgary_datetime(market.get("timestamp_calgary"))
    gold_daily_time = format_calgary_datetime(market.get("gold_daily_timestamp_calgary") or market.get("timestamp_calgary"))
    gold_weekly_time = format_calgary_datetime(market.get("gold_weekly_timestamp_calgary") or market.get("gold_daily_timestamp_calgary"))
    silver_price = format_number(core["gold_silver_ratio"].get("xagusd_close") or market.get("xagusd_price"), 3)
    silver_time = format_calgary_datetime(
        core["gold_silver_ratio"].get("timestamp_calgary") or market.get("xagusd_timestamp_calgary")
    )
    ratio = format_number(core["gold_silver_ratio"].get("value"), 2)
    ratio_time = format_calgary_datetime(core["gold_silver_ratio"].get("timestamp_calgary"))
    dxy = format_number(core["dxy"].get("value"), 2)
    dxy_time = format_calgary_datetime(core["dxy"].get("timestamp_calgary"))
    gvz = format_number(core["gvz"].get("value"), 2)
    gvz_time = format_calgary_datetime(core["gvz"].get("timestamp_calgary"))
    cot_net_short = format_int(core["cot_commercial_net"].get("commercial_net_short"))
    gc_futures_price = format_number(market.get("gc_futures_price"), 2)
    gc_futures_volume = format_int(market.get("gc_futures_volume"))
    gc_futures_time = format_calgary_datetime(market.get("gc_timestamp_calgary"))

    rsi = core["rsi"]
    td = core["td9_cd13"]
    bb = core["bollinger_percent_b"]
    vpt_obv = core["vpt_obv"]["futures_volume_proxy"]
    vpt_obv_time = format_calgary_datetime(vpt_obv.get("timestamp_calgary") or market.get("gc_timestamp_calgary"))
    real_yield = core["real_yield_10y"]
    pc_proxy = core["put_call_ratio"].get("gld_options_proxy")
    bei = extra.get("breakeven_inflation", {})
    t10yie = bei.get("t10yie", {})
    t5yie = bei.get("t5yie", {})
    cot = core["cot_commercial_net"]
    cot_time = f"数据截止 {format_date(cot.get('report_date'))}"
    gld_flow = extra.get("gld_etf_flow", {})
    wgc_global_flow = extra.get("wgc_global_etf_flow", {})
    gld_flow_time = f"官方日期 {format_date(gld_flow.get('date'))}" if gld_flow.get("date") else "N/A"
    real_yield_time = f"官方日期 {format_date(real_yield.get('latest_date'))}"
    bei_time = (
        f"T10YIE {format_date(t10yie.get('latest_date'))} / "
        f"T5YIE {format_date(t5yie.get('latest_date'))}"
    )
    wgc_periods = wgc_global_flow.get("periods", {}) if isinstance(wgc_global_flow.get("periods"), dict) else {}
    wgc_weekly = wgc_periods.get("weekly", {})
    wgc_monthly = wgc_periods.get("monthly", {})
    wgc_ytd = wgc_periods.get("ytd", {})

    def format_wgc_period_flow(period: dict[str, Any]) -> str:
        if not period:
            return "N/A"
        usd_mn = parse_float(period.get("fund_flows_usd_mn"))
        usd = usd_mn * 1_000_000 if usd_mn is not None else None
        return f"{signed_number(period.get('demand_tonnes'), 2, 't')} / {format_compact_usd(usd)}"

    if wgc_global_flow.get("status") == "available":
        wgc_reference_period = wgc_weekly or wgc_monthly or wgc_ytd
        wgc_global_flow_value = (
            f"周 {format_wgc_period_flow(wgc_weekly)} / "
            f"月 {format_wgc_period_flow(wgc_monthly)} / "
            f"持仓 {format_number(wgc_reference_period.get('holdings_tonnes'), 2, 't')}"
        )
        wgc_global_flow_time = (
            f"周 {format_date(wgc_weekly.get('as_of_date'))} / "
            f"月 {format_date(wgc_monthly.get('as_of_date'))}"
        )
        wgc_global_flow_note = (
            "WGC 全球100+实物黄金ETF及类似产品汇总；"
            f"YTD {format_wgc_period_flow(wgc_ytd)}；"
            "单位为需求吨数与资金流美元。"
        )
        wgc_global_flow_source_status = (
            f"周截至 {format_date(wgc_weekly.get('as_of_date'))} / "
            f"月截至 {format_date(wgc_monthly.get('as_of_date'))}"
        )
    else:
        wgc_global_flow_value = html_escape(human_status(wgc_global_flow.get("status", "unavailable")))
        wgc_global_flow_time = "N/A"
        wgc_global_flow_note = html_escape(wgc_global_flow.get("note", "本次未取得 WGC 全球黄金 ETF 资金流。"))
        wgc_global_flow_source_status = wgc_global_flow_value

    if gld_flow.get("status") == "available":
        gld_flow_value = (
            f"1日 {signed_number(gld_flow.get('tonnes_change_1d'), 2, 't')} / "
            f"估算 {format_compact_usd(gld_flow.get('estimated_flow_usd_1d'))} / "
            f"5日 {signed_number(gld_flow.get('tonnes_change_5d'), 2, 't')} / "
            f"持仓 {format_number(gld_flow.get('tonnes'), 2, 't')}"
        )
        gld_flow_note = (
            f"SPDR GLD 官方持仓；最新日期 {format_date(gld_flow.get('date'))}；"
            f"1日变化按 Total Ounces 计算；"
            f"美元为按 GLD 隐含金价 {format_number(gld_flow.get('implied_gold_price_per_oz'), 2)} 美元/盎司估算；"
            f"20日变化 {signed_number(gld_flow.get('tonnes_change_20d'), 2, 't')} / "
            f"{format_compact_usd(gld_flow.get('estimated_flow_usd_20d'))}"
        )
        gld_flow_source_status = "本次已获取"
    else:
        gld_flow_value = html_escape(human_status(gld_flow.get("status", "unavailable")))
        gld_flow_note = html_escape(gld_flow.get("note", "本次未取得 SPDR GLD 官方持仓历史。"))
        gld_flow_source_status = gld_flow_value

    pc_source_status = "本次未获取"
    pc_data_time = "N/A"
    pc_note = "GLD 期权代理指标；不是官方 OTC 黄金期权，也不是 COMEX GC 期权。"
    if pc_proxy and not pc_proxy.get("status"):
        pc_state = pc_proxy.get("data_state")
        pc_time = format_calgary_datetime(pc_proxy.get("fetched_at_calgary"))
        pc_data_time = pc_time
        if pc_state == "fresh":
            pc_source_status = f"本次已获取；抓取时间 {pc_time}"
            pc_note = f"Yahoo GLD options 本次新抓；到期日 {', '.join(pc_proxy.get('expirations_used') or [])}"
        elif pc_state == "cached_after_fetch_error":
            pc_source_status = f"本次抓取失败；沿用上次成功值 {pc_time}"
            pc_note = (
                "Yahoo GLD options 本次不可用，页面沿用本地最近一次成功值；"
                f"本次错误：{pc_proxy.get('current_fetch_error', 'N/A')}"
            )
        elif pc_state == "cached_after_skip":
            pc_source_status = f"本次跳过抓取；沿用上次成功值 {pc_time}"
            pc_note = "启动参数跳过 Yahoo GLD options，本页沿用本地最近一次成功值。"
        else:
            pc_source_status = f"缓存/历史值；数据时间 {pc_time}"
            pc_note = "GLD 期权代理指标；不是官方 OTC 黄金期权，也不是 COMEX GC 期权。"
    elif pc_proxy and pc_proxy.get("status"):
        pc_source_status = human_status(pc_proxy.get("status"))
        pc_note = html_escape(pc_proxy.get("error", pc_note))

    source_type_rows = [
        ["行情计算", "5项", "RSI、TD9/CD13、布林带%B、VPT/OBV、金银比；价格基准为XAUUSD/XAGUSD现货，量能用期货成交量代理"],
        ["行情直接抓取", "2项", "DXY、GVZ；直接从行情源抓读数"],
        ["外部发布/抓取数据", "6项", "10Y实际利率、COT、GLD期权P/C代理、BEI、WGC全球ETF Flow、GLD ETF Flow；必须等对应网站/机构发布"],
    ]

    quote_policy_rows = [
        [
            "主分析价格",
            "XAUUSD 现货",
            "OANDA XAU_USD spot_mid",
            "RSI、TD、布林带等价格类指标全部以此为基础",
        ],
        [
            "金银比",
            "XAUUSD / XAGUSD 现货",
            "OANDA XAU_USD 与 XAG_USD",
            "优先使用现货金银比；如果现货银价不可用才降级到GC/SI期货代理",
        ],
        [
            "量能/成交量",
            "期货/ETF 真实成交量代理",
            "Yahoo GC=F、SI=F、GLD",
            "XAUUSD现货没有全市场真实成交量；VPT/OBV使用XAUUSD价格配GC期货成交量代理",
        ],
    ]

    source_rows = [
        [
            "OANDA XAU_USD 现货主价格",
            "交易时段/日内",
            gold_time,
            "下一根1h/日线K；RSI、TD、布林带等价格类指标以此计算",
        ],
        [
            "OANDA XAG_USD 现货银价",
            "交易时段/日内",
            silver_time,
            "下一根1h/日线K；用于现货金银比",
        ],
        [
            "Yahoo GC=F 黄金期货成交量代理",
            "交易时段/日内",
            format_calgary_datetime((gc_update or {}).get("last_store_timestamp") or market.get("gc_timestamp_calgary")),
            "下一个交易时段/下一根K线；用于量能代理，不作为主价格",
        ],
        [
            "Yahoo SI=F 白银期货成交量代理",
            "交易时段/日内",
            format_calgary_datetime((si_update or {}).get("last_store_timestamp")),
            "下一个交易时段/下一根K线；备用量能/白银期货参考",
        ],
        [
            "GLD 价格与成交量",
            "美股交易日",
            format_calgary_datetime((gld_update or {}).get("last_store_timestamp")),
            "下一个美股交易日盘后形成完整日线",
        ],
        [
            "DXY 美元指数",
            "交易日",
            format_calgary_datetime(core["dxy"].get("timestamp_calgary")),
            "下一个交易日/下一根K线",
        ],
        [
            "GVZ 黄金波动率指数",
            "美股交易日",
            format_calgary_datetime(core["gvz"].get("timestamp_calgary")),
            "下一个美股交易日",
        ],
        [
            "FRED DFII10 10Y实际利率",
            "美国工作日",
            format_date(real_yield.get("latest_date")),
            fred_next_text(real_yield.get("latest_date")),
        ],
        [
            "FRED T10YIE/T5YIE BEI",
            "美国工作日",
            f"T10YIE {format_date(t10yie.get('latest_date'))} / T5YIE {format_date(t5yie.get('latest_date'))}",
            fred_next_text(t10yie.get("latest_date") or t5yie.get("latest_date")),
        ],
        [
            "CFTC COT 黄金商业持仓",
            "每周",
            f"数据截止 {format_date(cot.get('report_date'))}",
            next_cot_text(cot.get("report_date")),
        ],
        [
            "GLD 期权 P/C 代理",
            "期权交易日；可用性取决于 Yahoo options",
            pc_source_status,
            "交易所上市 GLD ETF 期权数据；用于黄金情绪/拥挤度代理，不等同于官方 OTC 黄金期权",
        ],
        [
            "WGC 全球黄金ETF资金流",
            "每周/月度",
            wgc_global_flow_source_status,
            "数据来自 World Gold Council 官方 ETF flows 页面；覆盖全球100+实物支持黄金ETF及类似产品",
        ],
        [
            "GLD ETF 持仓/资金流",
            "美股交易日；通常收盘后/晚间更新",
            f"最新日期 {format_date(gld_flow.get('date'))}" if gld_flow.get("date") else gld_flow_source_status,
            "数据来自 SPDR GLD 官方 Historical Archive；用持仓吨数变化估算流入/流出",
        ],
    ]

    pc_value = "N/A"
    if pc_proxy and not pc_proxy.get("status"):
        pc_value = (
            f"成交量P/C {format_number(pc_proxy.get('put_call_volume_ratio'), 2)} / "
            f"未平仓P/C {format_number(pc_proxy.get('put_call_open_interest_ratio'), 2)}"
        )
    elif pc_proxy and pc_proxy.get("status"):
        pc_value = html_escape(pc_proxy.get("status"))

    latest_gold_value = f"${gold_price}"
    indicator_rows = [
        [
            "*",
            "最新金价/抓取时间",
            "行情直接抓取",
            latest_gold_value,
            gold_time,
            "XAU_USD 现货最新1h读数；时间为 Calgary 时间",
        ],
        [
            "1",
            "RSI 日/周",
            "行情计算",
            f"D {format_number(rsi.get('daily'), 1)} / W {format_number(rsi.get('weekly'), 1)}",
            f"D {gold_daily_time} / W {gold_weekly_time}",
            "由 XAU_USD 现货日线/周线计算",
        ],
        [
            "2",
            "TD9/CD13 日/周",
            "行情计算",
            f"D {html_escape(td['daily'].get('display'))} / W {html_escape(td['weekly'].get('display'))}",
            f"D {gold_daily_time} / W {gold_weekly_time}",
            html_escape(td.get("note", "")),
        ],
        [
            "3",
            "布林带 %B",
            "行情计算",
            format_number(bb.get("percent_b"), 3),
            gold_daily_time,
            f"Upper {format_number(bb.get('upper'), 2)} / Lower {format_number(bb.get('lower'), 2)}",
        ],
        [
            "4",
            "VPT/OBV 量价",
            "行情计算",
            html_escape(vpt_obv.get("display", "N/A")),
            vpt_obv_time,
            f"XAUUSD价格 + GC期货真实成交量代理；{html_escape(human_signal(vpt_obv.get('signal')))}",
        ],
        [
            "5",
            "金银比",
            "行情计算",
            ratio,
            ratio_time,
            f"XAU {format_number(core['gold_silver_ratio'].get('xauusd_close'), 2)} / XAG {format_number(core['gold_silver_ratio'].get('xagusd_close'), 3)}",
        ],
        [
            "6",
            "DXY 美元指数",
            "行情直接抓取",
            dxy,
            dxy_time,
            f"20根K线变化 {format_number(core['dxy'].get('change_20_bars'), 2)}",
        ],
        [
            "7",
            "10Y 实际利率",
            "外部发布数据",
            format_number(real_yield.get("latest_value"), 2, "%"),
            real_yield_time,
            f"最新日期 {format_date(real_yield.get('latest_date'))}",
        ],
        [
            "8",
            "GVZ 黄金波动率",
            "行情直接抓取",
            gvz,
            gvz_time,
            f"1年分位 {format_number(core['gvz'].get('percentile_1y'), 1, '%')}",
        ],
        [
            "9",
            "COT 商业净空",
            "外部发布数据",
            f"净值 {format_int(cot.get('commercial_net'))} / 净空 {cot_net_short}",
            cot_time,
            f"商业多头 {format_int(cot.get('commercial_long'))} / 商业空头 {format_int(cot.get('commercial_short'))}",
        ],
        [
            "10",
            "GLD 期权 P/C 代理",
            "外部发布数据",
            pc_value,
            pc_data_time,
            html_escape(pc_note),
        ],
        [
            "+",
            "BEI 通胀预期",
            "外部发布数据",
            f"10Y {format_number(t10yie.get('latest_value'), 2, '%')} / 5Y {format_number(t5yie.get('latest_value'), 2, '%')}",
            bei_time,
            "FRED T10YIE / T5YIE",
        ],
        [
            "+",
            "WGC 全球ETF资金流",
            "外部发布数据",
            html_escape(wgc_global_flow_value),
            wgc_global_flow_time,
            html_escape(wgc_global_flow_note),
        ],
        [
            "+",
            "GLD ETF Flow",
            "外部发布数据",
            html_escape(gld_flow_value),
            gld_flow_time,
            html_escape(gld_flow_note),
        ],
        [
            "+",
            "WGC 央行购金",
            "待接入发布数据",
            html_escape(human_status(extra.get("wgc_central_bank_gold", {}).get("status", "not_automated"))),
            "N/A",
            html_escape(extra.get("wgc_central_bank_gold", {}).get("note", "")),
        ],
    ]

    error_rows = summary.get("errors") or []
    errors_html = "".join(
        f"<li><strong>{html_escape(error.get('source'))}</strong>: {html_escape(error.get('error'))}</li>"
        for error in error_rows
    ) or "<li>无</li>"

    lineage_rows = [
        [
            html_escape(item.get("name")),
            html_escape(item.get("source_store")),
            html_escape(item.get("used_from")),
            html_escape(item.get("used_to")),
            html_escape(item.get("rows_used")),
        ]
        for item in data_lineage.get("series", [])
    ]

    display_indicator_rows = [row for row in indicator_rows if row[1] != "WGC 央行购金"]
    copy_excluded_labels = {"BEI 通胀预期", "WGC 全球ETF资金流", "GLD ETF Flow"}
    copy_indicator_rows = [row for row in display_indicator_rows if row[1] not in copy_excluded_labels]
    copy_rows = [[html_escape(row[1]), row[3], html_escape(row[4])] for row in copy_indicator_rows]
    note_rows = [[html_escape(row[1]), row[5]] for row in display_indicator_rows]

    def plain_text(value: Any) -> str:
        return html.unescape(re.sub(r"<[^>]+>", "", str(value))).strip()

    metric_cards = [
        render_metric_card(row[1], plain_text(row[3]), f"数据时间: {plain_text(row[4])} | {plain_text(row[5])}")
        for row in display_indicator_rows
    ]
    copy_text = "\n".join(
        f"{plain_text(row[1])}\t{plain_text(row[3])}\t{plain_text(row[4])}"
        for row in copy_indicator_rows
    )
    copy_text_json = json.dumps(copy_text, ensure_ascii=False)

    generated_at = format_calgary_datetime(summary.get("calgary_fetch_completed_at"))
    requested_at = format_calgary_datetime(summary.get("calgary_requested_at"))

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>XAU 数据抓取结果 - {html_escape(generated_at)}</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #0b111b;
      --panel: #151d2a;
      --panel-2: #101722;
      --line: #2b384d;
      --text: #eef3fb;
      --muted: #94a3b8;
      --gold: #ffd166;
      --cyan: #8ee7f2;
      --green: #6ee7b7;
      --red: #ff8a8a;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      line-height: 1.5;
    }}
    main {{
      width: min(1440px, calc(100vw - 40px));
      margin: 0 auto;
      padding: 32px 0 56px;
    }}
    header {{
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 24px;
      align-items: end;
      padding-bottom: 24px;
      border-bottom: 1px solid var(--line);
    }}
    h1 {{
      margin: 0 0 10px;
      font-size: clamp(34px, 5vw, 72px);
      line-height: 0.95;
      letter-spacing: 0;
    }}
    h2 {{
      margin: 34px 0 14px;
      font-size: 24px;
      letter-spacing: 0;
    }}
    .subtitle, .meta, .metric-meta, td small {{
      color: var(--muted);
    }}
    .mode {{
      border: 1px solid var(--line);
      background: var(--panel);
      padding: 14px 18px;
      min-width: 320px;
    }}
    .mode strong {{
      color: var(--gold);
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(4, minmax(220px, 1fr));
      gap: 14px;
      margin-top: 24px;
    }}
    .metric-card {{
      background: var(--panel);
      border: 1px solid var(--line);
      padding: 18px;
      min-height: 150px;
    }}
    .metric-label {{
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: .08em;
      font-size: 13px;
      font-weight: 700;
    }}
    .metric-value {{
      margin-top: 8px;
      font-size: clamp(21px, 2vw, 32px);
      line-height: 1.12;
      font-weight: 800;
      color: var(--gold);
      overflow-wrap: anywhere;
      word-break: break-word;
    }}
    .panel {{
      background: var(--panel-2);
      border: 1px solid var(--line);
      padding: 18px;
      margin-top: 14px;
    }}
    .section-head {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin: 34px 0 14px;
    }}
    .section-head h2 {{
      margin: 0;
    }}
    .copy-button {{
      appearance: none;
      border: 1px solid rgba(142, 231, 242, .45);
      background: rgba(142, 231, 242, .12);
      color: var(--cyan);
      font: inherit;
      font-weight: 800;
      padding: 10px 14px;
      cursor: pointer;
    }}
    .copy-box {{
      width: 100%;
      min-height: 170px;
      resize: vertical;
      border: 1px solid var(--line);
      background: #0d1520;
      color: var(--text);
      padding: 14px;
      font: 15px/1.55 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      margin-bottom: 14px;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      table-layout: fixed;
      background: var(--panel-2);
      border: 1px solid var(--line);
    }}
    th, td {{
      padding: 13px 12px;
      border-bottom: 1px solid var(--line);
      vertical-align: top;
      overflow-wrap: anywhere;
    }}
    th {{
      color: var(--muted);
      text-align: left;
      font-size: 13px;
      text-transform: uppercase;
      letter-spacing: .06em;
      background: #111a27;
    }}
    tr:last-child td {{ border-bottom: 0; }}
    .badge {{
      display: inline-block;
      padding: 3px 8px;
      background: rgba(142, 231, 242, .14);
      color: var(--cyan);
      border: 1px solid rgba(142, 231, 242, .35);
      font-size: 12px;
      font-weight: 700;
    }}
    .badge.muted {{
      color: var(--muted);
      border-color: var(--line);
      background: transparent;
    }}
    ul {{
      margin: 0;
      padding-left: 20px;
      color: var(--muted);
    }}
    a {{ color: var(--cyan); }}
    @media (max-width: 900px) {{
      main {{ width: min(100vw - 24px, 720px); padding-top: 22px; }}
      header {{ grid-template-columns: 1fr; }}
      .mode {{ min-width: 0; }}
      .grid {{ grid-template-columns: 1fr; }}
      .section-head {{ align-items: stretch; flex-direction: column; }}
      .copy-button {{ width: 100%; }}
      table {{ table-layout: auto; }}
      th, td {{ font-size: 14px; }}
    }}
  </style>
</head>
<body>
  <main>
    <header>
      <div>
        <h1>XAU 数据抓取结果</h1>
        <div class="subtitle">纯数据页，只展示本次抓取与计算读数，不包含走势分析。</div>
      </div>
      <div class="mode">
        <div>抓取完成：<strong>{html_escape(generated_at)}</strong></div>
        <div class="meta">发起时间：{html_escape(requested_at)}</div>
        <div class="meta">Run ID：{html_escape(summary.get("run_id"))}</div>
      </div>
    </header>

    <section class="section-head">
      <h2>可复制读数</h2>
      <button class="copy-button" type="button" id="copy-button">复制名称+结果</button>
    </section>
    <textarea class="copy-box" id="copy-text" readonly>{html_escape(copy_text)}</textarea>
    {render_table(["名称", "结果", "数据时间"], copy_rows)}

    <h2>指标总览</h2>
    <section class="grid">
      {''.join(metric_cards)}
    </section>

    <h2>口径备注</h2>
    {render_table(["指标", "备注"], note_rows)}

    <h2>更新频率与下一次预计更新</h2>
    {render_table(["数据源", "更新频率", "本次数据时间/公布时间", "下一次预计更新"], source_rows)}

    <h2>报价口径说明</h2>
    {render_table(["项目", "当前口径", "来源", "说明"], quote_policy_rows)}

    <h2>数据来源分类</h2>
    {render_table(["类型", "数量", "说明"], source_type_rows)}

    <h2>数据来源范围</h2>
    {render_table(["序列", "本地文件", "使用起点", "使用终点", "行数"], lineage_rows)}

    <h2>本次抓取提示</h2>
    <section class="panel">
      <ul>{errors_html}</ul>
    </section>
  </main>
  <script>
    const copyPayload = {copy_text_json};
    const copyButton = document.getElementById("copy-button");
    const copyText = document.getElementById("copy-text");
    copyButton.addEventListener("click", async () => {{
      try {{
        await navigator.clipboard.writeText(copyPayload);
        copyButton.textContent = "已复制";
      }} catch (error) {{
        copyText.focus();
        copyText.select();
        document.execCommand("copy");
        copyButton.textContent = "已选中/已复制";
      }}
      window.setTimeout(() => {{
        copyButton.textContent = "复制名称+结果";
      }}, 1800);
    }});
  </script>
</body>
</html>
"""


def run_pipeline(args: argparse.Namespace) -> int:
    requested_at = now_calgary()
    run_dir = DATA_RUNS / requested_at.strftime("%Y-%m-%d") / f"{requested_at.strftime('%H%M%S')}-calgary-data-pipeline"
    raw_dir = run_dir / "raw"
    computed_dir = run_dir / "computed"
    raw_dir.mkdir(parents=True, exist_ok=True)
    computed_dir.mkdir(parents=True, exist_ok=True)

    fetch_started_at = now_calgary()
    yahoo_results = []
    errors = []

    for config in YAHOO_SERIES:
        symbol = config["symbol"]
        for interval in config["intervals"]:
            store_path = yahoo_store_path(symbol, interval)
            if interval == "1h":
                range_ = args.bootstrap_range if not store_path.exists() else args.hourly_update_range
            else:
                range_ = args.bootstrap_range if not store_path.exists() else args.daily_update_range
            try:
                yahoo_results.append(fetch_and_update_yahoo(symbol, interval, range_, raw_dir))
            except Exception as exc:  # noqa: BLE001 - keep pipeline alive and report source failures.
                errors.append({"source": f"Yahoo {symbol} {interval}", "error": str(exc)})

    ratio_results = []
    for interval in ["1d", "1h"]:
        try:
            ratio_results.append(update_gold_silver_ratio(interval))
        except Exception as exc:  # noqa: BLE001
            errors.append({"source": f"gold_silver_ratio {interval}", "error": str(exc)})

    fred_results = []
    if args.skip_fred:
        errors.append({"source": "FRED", "error": "Skipped by --skip-fred."})
    else:
        for series_id in FRED_SERIES:
            try:
                fred_results.append(update_fred_series(series_id, raw_dir))
            except Exception as exc:  # noqa: BLE001
                errors.append({"source": f"FRED {series_id}", "error": str(exc)})

    cftc_result = None
    if args.skip_cftc:
        errors.append({"source": "CFTC", "error": "Skipped by --skip-cftc."})
    else:
        try:
            cftc_result = update_cftc(raw_dir, now_calgary().isoformat())
        except Exception as exc:  # noqa: BLE001
            errors.append({"source": "CFTC legacy metals", "error": str(exc)})

    options_result = None
    if args.skip_options:
        options_result = load_cached_options_result("GLD", "cached_after_skip")
        if options_result:
            errors.append({
                "source": "Yahoo GLD options",
                "error": "Skipped by --skip-options; using latest cached successful value.",
            })
        else:
            errors.append({"source": "Yahoo GLD options", "error": "Skipped by --skip-options; no cached value available."})
    else:
        try:
            options_result = fetch_yahoo_options_put_call("GLD", args.option_expirations, args.options_timeout)
            write_json(raw_dir / "yahoo-options-GLD-put-call.json", options_result)
            cache_options_result(options_result)
        except Exception as exc:  # noqa: BLE001
            errors.append({"source": "Yahoo GLD options", "error": str(exc)})
            options_result = load_cached_options_result("GLD", "cached_after_fetch_error")
            if options_result:
                options_result["current_fetch_error"] = str(exc)
            else:
                options_result = {"status": "unavailable", "error": str(exc)}

    gld_flow_result = None
    if args.skip_gld_flow:
        errors.append({"source": "SPDR GLD ETF flow", "error": "Skipped by --skip-gld-flow."})
    else:
        try:
            gld_flow_result = update_gld_etf_flow(raw_dir)
        except Exception as exc:  # noqa: BLE001
            errors.append({"source": "SPDR GLD ETF flow", "error": str(exc)})

    wgc_global_etf_flow_result = None
    if args.skip_wgc_global_etf_flow:
        errors.append({"source": "WGC global gold ETF flow", "error": "Skipped by --skip-wgc-global-etf-flow."})
    else:
        try:
            wgc_global_etf_flow_result = update_wgc_global_etf_flow(raw_dir)
        except Exception as exc:  # noqa: BLE001
            errors.append({"source": "WGC global gold ETF flow", "error": str(exc)})

    indicators = compute_indicators(options_result)
    model_conclusion = score_model(indicators)
    data_lineage = build_data_lineage(indicators)
    fetch_completed_at = now_calgary()

    indicators_path = computed_dir / "indicators.json"
    model_path = computed_dir / "model-conclusion.json"
    lineage_path = computed_dir / "data-lineage.json"
    html_path = run_dir / "data-result.html"
    latest_html_path = DATA_RUNS / "latest-data-result.html"
    write_json(indicators_path, indicators)
    write_json(model_path, model_conclusion)
    write_json(lineage_path, data_lineage)

    summary = {
        "run_id": f"{requested_at.strftime('%Y-%m-%d')}/{run_dir.name}",
        "timezone": "America/Edmonton",
        "calgary_requested_at": requested_at.isoformat(),
        "calgary_fetch_started_at": fetch_started_at.isoformat(),
        "calgary_fetch_completed_at": fetch_completed_at.isoformat(),
        "analysis_scope": "xau_10_indicator_data_pipeline",
        "store_updates": {
            "yahoo": yahoo_results,
            "gold_silver_ratio": ratio_results,
            "fred": fred_results,
            "cftc": cftc_result,
            "options": options_result,
            "gld_etf_flow": gld_flow_result,
            "wgc_global_etf_flow": wgc_global_etf_flow_result,
        },
        "computed_files": {
            "indicators": rel(indicators_path),
            "model_conclusion": rel(model_path),
            "data_lineage": rel(lineage_path),
            "data_html": rel(html_path),
            "latest_data_html": rel(latest_html_path),
        },
        "errors": errors,
    }
    html_content = render_data_html(summary, indicators, data_lineage)
    write_text(html_path, html_content)
    write_text(latest_html_path, html_content)

    summary_path = run_dir / "summary.json"
    write_json(summary_path, summary)

    if args.open_html:
        subprocess.run(["open", str(html_path)], check=False)

    print(json.dumps({
        "summary": rel(summary_path),
        "indicators": rel(indicators_path),
        "model_conclusion": rel(model_path),
        "data_html": rel(html_path),
        "latest_data_html": rel(latest_html_path),
        "errors": errors,
    }, ensure_ascii=False, indent=2))
    return 1 if args.strict and errors else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--bootstrap-range",
        default="1y",
        help="Yahoo range used when a store file does not exist. Default: 1y.",
    )
    parser.add_argument(
        "--daily-update-range",
        default="1mo",
        help="Yahoo range used to refresh existing daily store files. Default: 1mo.",
    )
    parser.add_argument(
        "--hourly-update-range",
        default="5d",
        help="Yahoo range used to refresh existing 1h store files. Default: 5d.",
    )
    parser.add_argument(
        "--option-expirations",
        type=int,
        default=4,
        help="Number of GLD option expirations to include in the P/C proxy. Default: 4.",
    )
    parser.add_argument(
        "--options-timeout",
        type=int,
        default=15,
        help="Per-request timeout for Yahoo GLD options chain fetch. Default: 15 seconds.",
    )
    parser.add_argument(
        "--skip-options",
        action="store_true",
        help="Skip Yahoo GLD options chain fetch.",
    )
    parser.add_argument(
        "--skip-fred",
        action="store_true",
        help="Skip FRED macro series fetch and use any existing local values.",
    )
    parser.add_argument(
        "--skip-cftc",
        action="store_true",
        help="Skip CFTC COT fetch and use any existing local values.",
    )
    parser.add_argument(
        "--skip-gld-flow",
        action="store_true",
        help="Skip SPDR GLD ETF holdings/flow fetch and use any existing local values.",
    )
    parser.add_argument(
        "--skip-wgc-global-etf-flow",
        action="store_true",
        help="Skip WGC global gold ETF flow fetch and use any existing local values.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Return a non-zero exit code when any optional source fails.",
    )
    parser.add_argument(
        "--open-html",
        action="store_true",
        help="Open the generated data HTML page after the pipeline finishes.",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return run_pipeline(args)


if __name__ == "__main__":
    raise SystemExit(main())

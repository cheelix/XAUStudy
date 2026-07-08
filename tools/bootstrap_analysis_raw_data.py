#!/usr/bin/env python3
"""Bootstrap two years of gold/silver futures OHLCV for analysis raw data."""

from __future__ import annotations

import csv
import datetime as dt
import json
import math
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = PROJECT_ROOT / "python-data-store" / "analysis-raw"
CALGARY_TZ = ZoneInfo("America/Edmonton")
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?{params}"
HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json,text/plain,*/*",
}

INSTRUMENTS = [
    {
        "symbol": "GC=F",
        "asset": "gold",
        "name": "COMEX Gold Futures continuous front-month proxy",
    },
    {
        "symbol": "SI=F",
        "asset": "silver",
        "name": "COMEX Silver Futures continuous front-month proxy",
    },
]

INTERVALS = [
    {"interval": "1h", "range": "2y", "label": "1h"},
    {"interval": "1d", "range": "2y", "label": "1d"},
]

FIELDNAMES = [
    "timestamp_utc",
    "timestamp_calgary",
    "source",
    "symbol",
    "asset",
    "instrument_name",
    "interval",
    "price_type",
    "volume_type",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "adjclose",
]


def clean_symbol(symbol: str) -> str:
    return "".join(ch for ch in symbol if ch.isalnum())


def parse_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        if math.isnan(float(value)):
            return None
        return float(value)
    try:
        return float(str(value).replace(",", "").strip())
    except ValueError:
        return None


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
    request = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    error = payload.get("chart", {}).get("error")
    if error:
        raise RuntimeError(f"Yahoo error for {symbol} {interval}: {error}")
    return payload


def parse_yahoo_rows(payload: dict[str, Any], instrument: dict[str, str], interval: str) -> list[dict[str, Any]]:
    result = payload["chart"]["result"][0]
    timestamps = result.get("timestamp") or []
    quote = result["indicators"]["quote"][0]
    adjclose = (result.get("indicators", {}).get("adjclose") or [{}])[0].get("adjclose")

    rows: list[dict[str, Any]] = []
    for index, timestamp in enumerate(timestamps):
        utc_time = dt.datetime.fromtimestamp(timestamp, dt.UTC)
        calgary_time = utc_time.astimezone(CALGARY_TZ)
        close_value = parse_float((quote.get("close") or [None] * len(timestamps))[index])
        if close_value is None:
            continue
        rows.append(
            {
                "timestamp_utc": utc_time.isoformat(),
                "timestamp_calgary": calgary_time.isoformat(),
                "source": "Yahoo Finance chart endpoint",
                "symbol": instrument["symbol"],
                "asset": instrument["asset"],
                "instrument_name": instrument["name"],
                "interval": interval,
                "price_type": "COMEX futures continuous front-month proxy",
                "volume_type": "exchange_reported_contract_volume",
                "open": parse_float((quote.get("open") or [None] * len(timestamps))[index]),
                "high": parse_float((quote.get("high") or [None] * len(timestamps))[index]),
                "low": parse_float((quote.get("low") or [None] * len(timestamps))[index]),
                "close": close_value,
                "volume": parse_float((quote.get("volume") or [None] * len(timestamps))[index]),
                "adjclose": parse_float(adjclose[index]) if adjclose else None,
            }
        )
    return rows


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    fetched_at = dt.datetime.now(CALGARY_TZ)
    manifest: dict[str, Any] = {
        "dataset": "analysis_raw_gold_silver_futures_2y",
        "created_at_calgary": fetched_at.isoformat(),
        "purpose": "Canonical local raw OHLCV input for XAU analysis calculations.",
        "volume_policy": {
            "included": "exchange_reported_contract_volume from COMEX futures proxies GC=F and SI=F",
            "excluded": "spot broker/platform volume such as OANDA XAU_USD tick/platform volume",
        },
        "files": [],
    }

    for instrument in INSTRUMENTS:
        for interval_config in INTERVALS:
            interval = interval_config["interval"]
            label = interval_config["label"]
            range_ = interval_config["range"]
            payload = fetch_yahoo_chart(instrument["symbol"], interval, range_)
            rows = parse_yahoo_rows(payload, instrument, label)

            symbol_clean = clean_symbol(instrument["symbol"])
            output_path = (
                DATA_ROOT
                / "futures"
                / "yahoo"
                / instrument["symbol"]
                / f"yahoo-{symbol_clean}-{range_}-{label}-ohlcv-real-volume.csv"
            )
            metadata_path = output_path.with_suffix(".metadata.json")

            write_csv(output_path, rows)
            metadata = {
                "symbol": instrument["symbol"],
                "asset": instrument["asset"],
                "instrument_name": instrument["name"],
                "source": "Yahoo Finance chart endpoint",
                "range_requested": range_,
                "interval": label,
                "price_type": "COMEX futures continuous front-month proxy",
                "volume_type": "exchange_reported_contract_volume",
                "rows": len(rows),
                "first_timestamp_calgary": rows[0]["timestamp_calgary"] if rows else None,
                "last_timestamp_calgary": rows[-1]["timestamp_calgary"] if rows else None,
                "last_close": rows[-1]["close"] if rows else None,
                "created_at_calgary": fetched_at.isoformat(),
            }
            write_json(metadata_path, metadata)
            manifest["files"].append(
                {
                    "csv": str(output_path.relative_to(PROJECT_ROOT)),
                    "metadata": str(metadata_path.relative_to(PROJECT_ROOT)),
                    **metadata,
                }
            )

    manifest_path = DATA_ROOT / "manifest.json"
    write_json(manifest_path, manifest)
    print(json.dumps({"manifest": str(manifest_path.relative_to(PROJECT_ROOT)), "files": manifest["files"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

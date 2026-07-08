#!/usr/bin/env python3
"""Fetch OANDA XAU_USD/XAG_USD spot candles into the Python-only data store."""

from __future__ import annotations

import csv
import datetime as dt
import json
import math
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SECRET_FILE = PROJECT_ROOT / "secrets" / "oanda.env"
DATA_STORE = PROJECT_ROOT / "python-data-store"
DATA_RUNS = PROJECT_ROOT / "python-data-runs"
CALGARY_TZ = ZoneInfo("America/Edmonton")

DEFAULT_INSTRUMENTS = ["XAU_USD", "XAG_USD"]
INTERVALS = [
    {"granularity": "H1", "label": "1h", "chunk_days": 90},
    {"granularity": "D", "label": "1d", "chunk_days": 760},
]
FIELDNAMES = [
    "timestamp_utc",
    "timestamp_calgary",
    "source",
    "instrument",
    "asset",
    "interval",
    "price_type",
    "volume_type",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "complete",
]


def load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def config() -> dict[str, Any]:
    file_values = load_env_file(SECRET_FILE)
    env = {**file_values, **{key: value for key, value in os.environ.items() if key.startswith("OANDA_")}}
    token = env.get("OANDA_API_TOKEN", "")
    if not token:
        raise RuntimeError("Missing OANDA_API_TOKEN. Run: zsh tools/setup_oanda_token.command")
    environment = env.get("OANDA_ENV", "practice").lower()
    if environment not in {"practice", "live"}:
        raise RuntimeError("OANDA_ENV must be practice or live.")
    base_url = "https://api-fxpractice.oanda.com" if environment == "practice" else "https://api-fxtrade.oanda.com"
    instruments_text = env.get("OANDA_INSTRUMENTS", "")
    instruments = [item.strip() for item in instruments_text.split(",") if item.strip()]
    if not instruments:
        instruments = DEFAULT_INSTRUMENTS
    return {
        "token": token,
        "environment": environment,
        "base_url": base_url,
        "account_id": env.get("OANDA_ACCOUNT_ID", ""),
        "instruments": instruments,
    }


def request_json(base_url: str, token: str, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    query = f"?{urllib.parse.urlencode(params)}" if params else ""
    url = f"{base_url}{path}{query}"
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "xau-local-pipeline/1.1",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"OANDA HTTP {exc.code} for {path}: {body}") from exc


def detect_account_id(cfg: dict[str, Any]) -> str:
    if cfg.get("account_id"):
        return str(cfg["account_id"])
    payload = request_json(cfg["base_url"], cfg["token"], "/v3/accounts")
    accounts = payload.get("accounts") or []
    if not accounts:
        raise RuntimeError("No OANDA accounts returned for this token.")
    return accounts[0]["id"]


def parse_oanda_time(value: str) -> tuple[str, str]:
    normalized = value.replace("Z", "+00:00")
    if "." in normalized:
        head, tail = normalized.split(".", 1)
        fraction = tail
        timezone = ""
        for marker in ["+", "-"]:
            if marker in tail:
                fraction, timezone_tail = tail.split(marker, 1)
                timezone = marker + timezone_tail
                break
        normalized = f"{head}.{fraction[:6].ljust(6, '0')}{timezone}"
    parsed = dt.datetime.fromisoformat(normalized)
    utc_time = parsed.astimezone(dt.UTC)
    calgary_time = utc_time.astimezone(CALGARY_TZ)
    return utc_time.isoformat(), calgary_time.isoformat()


def format_rfc3339(value: dt.datetime) -> str:
    return value.astimezone(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def instrument_asset(instrument: str) -> str:
    if instrument == "XAU_USD":
        return "gold"
    if instrument == "XAG_USD":
        return "silver"
    return instrument.lower()


def clean_instrument(instrument: str) -> str:
    return instrument.replace("_", "")


def spot_store_path(instrument: str, interval: str) -> Path:
    return (
        DATA_STORE
        / "analysis-raw"
        / "spot"
        / "oanda"
        / instrument
        / f"oanda-{clean_instrument(instrument)}-2y-{interval}-ohlc-spot.csv"
    )


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def parse_utc(value: str) -> dt.datetime:
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
    try:
        return float(str(value).replace(",", "").strip())
    except ValueError:
        return None


def fetch_candles_chunk(
    cfg: dict[str, Any],
    instrument: str,
    granularity: str,
    start: dt.datetime,
    end: dt.datetime,
) -> list[dict[str, Any]]:
    payload = request_json(
        cfg["base_url"],
        cfg["token"],
        f"/v3/instruments/{instrument}/candles",
        {
            "price": "M",
            "granularity": granularity,
            "from": format_rfc3339(start),
            "to": format_rfc3339(end),
            "includeFirst": "true",
        },
    )
    rows: list[dict[str, Any]] = []
    for candle in payload.get("candles") or []:
        mid = candle.get("mid") or {}
        utc_time, calgary_time = parse_oanda_time(candle["time"])
        rows.append(
            {
                "timestamp_utc": utc_time,
                "timestamp_calgary": calgary_time,
                "source": "OANDA v20 candles",
                "instrument": instrument,
                "asset": instrument_asset(instrument),
                "interval": "1h" if granularity == "H1" else "1d",
                "price_type": "spot_mid",
                "volume_type": "oanda_platform_tick_volume_not_used_as_true_market_volume",
                "open": parse_float(mid.get("o")),
                "high": parse_float(mid.get("h")),
                "low": parse_float(mid.get("l")),
                "close": parse_float(mid.get("c")),
                "volume": parse_float(candle.get("volume")),
                "complete": candle.get("complete"),
            }
        )
    return rows


def fetch_candles_between(
    cfg: dict[str, Any],
    instrument: str,
    granularity: str,
    start: dt.datetime,
    end: dt.datetime,
    chunk_days: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    cursor = start
    while cursor < end:
        chunk_end = min(cursor + dt.timedelta(days=chunk_days), end)
        rows.extend(fetch_candles_chunk(cfg, instrument, granularity, cursor, chunk_end))
        cursor = chunk_end
    by_ts = {row["timestamp_utc"]: row for row in rows if row.get("timestamp_utc")}
    return [by_ts[key] for key in sorted(by_ts)]


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def merge_rows(path: Path, fetched_rows: list[dict[str, Any]], cutoff_utc: dt.datetime) -> tuple[list[dict[str, Any]], int]:
    existing = read_csv_rows(path)
    existing_by_ts = {row["timestamp_utc"]: row for row in existing if row.get("timestamp_utc")}
    merged_by_ts: dict[str, dict[str, Any]] = dict(existing_by_ts)
    for row in fetched_rows:
        timestamp = row.get("timestamp_utc")
        if timestamp:
            merged_by_ts[timestamp] = row
    merged = []
    for timestamp in sorted(merged_by_ts):
        if parse_utc(timestamp) >= cutoff_utc:
            merged.append(merged_by_ts[timestamp])
    write_csv(path, merged)
    new_rows = sum(1 for row in merged if row.get("timestamp_utc") not in existing_by_ts)
    return merged, new_rows


def fetch_latest_pricing(cfg: dict[str, Any], account_id: str) -> dict[str, Any]:
    instruments = ",".join(cfg["instruments"])
    try:
        return request_json(
            cfg["base_url"],
            cfg["token"],
            f"/v3/accounts/{account_id}/pricing",
            {"instruments": instruments},
        )
    except Exception as exc:  # noqa: BLE001 - keep candle refresh useful even if pricing fails.
        return {"status": "unavailable", "error": str(exc)}


def update_instrument_interval(
    cfg: dict[str, Any],
    instrument: str,
    interval_config: dict[str, Any],
    now_utc: dt.datetime,
) -> dict[str, Any]:
    interval = interval_config["label"]
    store_path = spot_store_path(instrument, interval)
    existing = read_csv_rows(store_path)
    cutoff_utc = now_utc - dt.timedelta(days=735)
    if existing:
        last_utc = parse_utc(existing[-1]["timestamp_utc"])
        fetch_start = max(cutoff_utc, last_utc - dt.timedelta(days=7))
    else:
        fetch_start = cutoff_utc
    rows = fetch_candles_between(
        cfg,
        instrument,
        interval_config["granularity"],
        fetch_start,
        now_utc,
        interval_config["chunk_days"],
    )
    merged, new_rows = merge_rows(store_path, rows, cutoff_utc)
    metadata_path = store_path.with_suffix(".metadata.json")
    metadata = {
        "instrument": instrument,
        "asset": instrument_asset(instrument),
        "source": "OANDA v20 candles",
        "interval": interval,
        "price_type": "spot_mid",
        "volume_policy": "OANDA volume is broker/platform tick volume and is not used as true market volume.",
        "store_file": str(store_path.relative_to(PROJECT_ROOT)),
        "rows": len(merged),
        "new_rows": new_rows,
        "first_timestamp_calgary": merged[0]["timestamp_calgary"] if merged else None,
        "last_timestamp_calgary": merged[-1]["timestamp_calgary"] if merged else None,
        "last_close": parse_float(merged[-1].get("close")) if merged else None,
        "refreshed_at_calgary": now_utc.astimezone(CALGARY_TZ).isoformat(),
    }
    write_json(metadata_path, metadata)
    return {
        **metadata,
        "metadata_file": str(metadata_path.relative_to(PROJECT_ROOT)),
        "fetched_rows": len(rows),
    }


def update_manifest(outputs: list[dict[str, Any]], fetched_at: dt.datetime) -> Path:
    manifest_path = DATA_STORE / "analysis-raw" / "spot" / "oanda" / "manifest.json"
    manifest = {
        "dataset": "analysis_raw_spot_oanda_xau_xag_2y",
        "updated_at_calgary": fetched_at.isoformat(),
        "purpose": "Primary spot price input for XAUUSD analysis calculations.",
        "price_policy": {
            "primary_gold_price": "OANDA XAU_USD spot_mid",
            "primary_silver_price": "OANDA XAG_USD spot_mid",
            "gold_silver_ratio": "XAU_USD / XAG_USD when both are available",
        },
        "volume_policy": {
            "oanda_volume": "broker/platform tick volume; stored for reference but excluded from true volume indicators",
            "true_volume_proxy": "use GC=F/SI=F/GLD exchange or ETF volume series as auxiliary inputs",
        },
        "files": outputs,
    }
    write_json(manifest_path, manifest)
    return manifest_path


def main() -> int:
    cfg = config()
    account_id = detect_account_id(cfg)
    cfg["account_id"] = account_id

    requested_at = dt.datetime.now(CALGARY_TZ)
    now_utc = requested_at.astimezone(dt.UTC)
    run_dir = DATA_RUNS / requested_at.strftime("%Y-%m-%d") / f"{requested_at.strftime('%H%M%S')}-calgary-oanda-spot"
    run_dir.mkdir(parents=True, exist_ok=True)

    pricing = fetch_latest_pricing(cfg, account_id)
    outputs: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for instrument in cfg["instruments"]:
        for interval_config in INTERVALS:
            try:
                outputs.append(update_instrument_interval(cfg, instrument, interval_config, now_utc))
            except Exception as exc:  # noqa: BLE001 - continue other instruments/intervals.
                errors.append({"source": f"OANDA {instrument} {interval_config['label']}", "error": str(exc)})

    manifest_path = update_manifest(outputs, requested_at)
    summary = {
        "run_type": "oanda_spot_fetch",
        "environment": cfg["environment"],
        "instruments": cfg["instruments"],
        "account_id_masked": f"{account_id[:4]}...{account_id[-4:]}" if len(account_id) > 8 else "***",
        "calgary_requested_at": requested_at.isoformat(),
        "latest_pricing": pricing,
        "manifest": str(manifest_path.relative_to(PROJECT_ROOT)),
        "outputs": outputs,
        "errors": errors,
    }
    summary_path = run_dir / "summary.json"
    write_json(summary_path, summary)
    print(
        json.dumps(
            {
                "summary": str(summary_path.relative_to(PROJECT_ROOT)),
                "manifest": str(manifest_path.relative_to(PROJECT_ROOT)),
                "outputs": outputs,
                "errors": errors,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 1 if errors and not outputs else 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Bounded-memory CSV candle ingestion for large local history files."""
from __future__ import annotations

import csv
import os
from datetime import timezone
from typing import Any

from sqlalchemy.orm import Session

from app.data.providers import normalize_row, parse_timeframe_minutes
from app.models.database import MarketData

_HEADER_ALIASES = {
    "gmt_time": "timestamp",
    "date_time": "timestamp",
    "datetime_utc": "timestamp",
    "timestamp_utc": "timestamp",
    "tick_volume": "volume",
}


def _normalize_headers(row: dict[str, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for header, value in row.items():
        if header is None:
            continue
        key = header.strip().lower().replace(" ", "_")
        key = _HEADER_ALIASES.get(key, key)
        normalized[key] = value.strip() if isinstance(value, str) else value
    if not normalized.get("timestamp") and normalized.get("date") and normalized.get("time"):
        normalized["timestamp"] = f"{normalized['date']} {normalized['time']}"
    return normalized


def _database_timestamp(timestamp):
    return timestamp.astimezone(timezone.utc).replace(tzinfo=None)


def import_csv(
    db: Session,
    file_path: str,
    symbol: str,
    timeframe: str,
    *,
    batch_size: int = 5000,
) -> dict[str, int]:
    """Import one CSV incrementally; commit each batch and skip existing bars."""
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    parse_timeframe_minutes(timeframe)
    if not os.path.isfile(file_path):
        raise FileNotFoundError(file_path)

    symbol = symbol.strip().upper()
    if not symbol:
        raise ValueError("symbol must not be empty")

    totals = {"rows_read": 0, "imported": 0, "duplicates": 0}
    batch: list[dict[str, Any]] = []

    def save_batch() -> None:
        if not batch:
            return
        timestamps = [_database_timestamp(candle["timestamp"]) for candle in batch]
        existing = set(
            db.query(MarketData.timestamp)
            .filter(
                MarketData.symbol == symbol,
                MarketData.timeframe == timeframe,
                MarketData.timestamp.in_(timestamps),
            )
            .all()
        )
        existing_timestamps = {row[0] for row in existing}
        seen: set[Any] = set()
        mappings = []
        for candle, timestamp in zip(batch, timestamps):
            if timestamp in existing_timestamps or timestamp in seen:
                totals["duplicates"] += 1
                continue
            seen.add(timestamp)
            mappings.append({
                "symbol": symbol,
                "timeframe": timeframe,
                "timestamp": timestamp,
                "open": candle["open"],
                "high": candle["high"],
                "low": candle["low"],
                "close": candle["close"],
                "volume": candle["volume"],
                "bid": candle["bid"],
                "ask": candle["ask"],
                "spread": candle["spread"],
                "source": "import",
            })
        try:
            if mappings:
                db.bulk_insert_mappings(MarketData, mappings)
            db.commit()
        except Exception:
            db.rollback()
            raise
        totals["imported"] += len(mappings)
        batch.clear()

    with open(file_path, "r", encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        if not reader.fieldnames:
            raise ValueError("CSV must include a header row")
        for line_number, row in enumerate(reader, start=2):
            totals["rows_read"] += 1
            try:
                candle = normalize_row(symbol, timeframe, _normalize_headers(row))
            except ValueError as exc:
                raise ValueError(f"invalid CSV row at line {line_number}: {exc}") from exc
            batch.append(candle)
            if len(batch) >= batch_size:
                save_batch()
    save_batch()
    return totals

"""Bounded-memory conversion of sequential month-partitioned bid/ask ticks."""
from __future__ import annotations

import math
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from sqlalchemy.orm import Session

from app.data.providers import parse_timeframe_minutes
from app.models.database import MarketData


class _Candle:
    def __init__(self, timestamp: datetime, bid: float, ask: float,
                 bid_volume: float | None, ask_volume: float | None) -> None:
        self.timestamp = timestamp
        self.open = self.high = self.low = self.close = bid
        self.bid = bid
        self.ask = ask
        self.volume = (bid_volume or 0.0) + (ask_volume or 0.0)
        self.has_volume = bid_volume is not None or ask_volume is not None
        self.spread_total = ask - bid
        self.tick_count = 1

    def add(self, bid: float, ask: float, bid_volume: float | None,
            ask_volume: float | None) -> None:
        self.high = max(self.high, bid)
        self.low = min(self.low, bid)
        self.close = bid
        self.bid = bid
        self.ask = ask
        if bid_volume is not None or ask_volume is not None:
            self.volume += (bid_volume or 0.0) + (ask_volume or 0.0)
            self.has_volume = True
        self.spread_total += ask - bid
        self.tick_count += 1

    def row(self, symbol: str, timeframe: str) -> dict[str, Any]:
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "timestamp": self.timestamp,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume if self.has_volume else None,
            "bid": self.bid,
            "ask": self.ask,
            "spread": self.spread_total / self.tick_count,
            "source": "import_ticks",
        }

    @classmethod
    def from_aggregate(cls, row: dict[str, Any]) -> "_Candle":
        candle = cls(row["bucket"], row["open"], row["ask"], row["volume"], 0.0)
        candle.high = row["high"]
        candle.low = row["low"]
        candle.close = row["close"]
        candle.bid = row["bid"]
        candle.spread_total = row["_spread_total"]
        candle.tick_count = row["_tick_count"]
        candle.has_volume = row["volume"] is not None
        return candle

    def merge_aggregate(self, row: dict[str, Any]) -> None:
        self.high = max(self.high, row["high"])
        self.low = min(self.low, row["low"])
        self.close = row["close"]
        self.bid = row["bid"]
        self.ask = row["ask"]
        if row["volume"] is not None:
            self.volume += row["volume"]
            self.has_volume = True
        self.spread_total += row["_spread_total"]
        self.tick_count += row["_tick_count"]


def _parse_tick(fields: list[bytes], columns: dict[str, int], path: Path, line_number: int,
                target_year: int, target_month: int, source_month: int
                ) -> tuple[datetime, float, float, float | None, float | None]:
    try:
        raw_time = fields[columns["time"]].strip()
        if (len(raw_time) >= 19 and raw_time[4:5] == b"-" and raw_time[7:8] == b"-"
                and raw_time[10:11] == b" " and raw_time[13:14] == b":"
                and raw_time[16:17] == b":"):
            microseconds = 0
            if len(raw_time) > 19:
                if raw_time[19:20] == b".":
                    fraction = raw_time[20:]
                    if not fraction.isdigit():
                        raise ValueError("invalid fractional seconds")
                    microseconds = int(fraction[:6].ljust(6, b"0"))
                else:
                    raise ValueError("timezone-qualified ticks are not supported in this export")
            tick_time = datetime(
                int(raw_time[0:4]), int(raw_time[5:7]), int(raw_time[8:10]),
                int(raw_time[11:13]), int(raw_time[14:16]), int(raw_time[17:19]), microseconds,
            )
        else:
            tick_time = datetime.fromisoformat(raw_time.decode("ascii").replace("Z", "+00:00"))
        if tick_time.month != source_month:
            raise ValueError(f"expected source month {source_month}, found {tick_time.month}")
        tick_time = tick_time.replace(year=target_year, month=target_month, tzinfo=None)
        bid = float(fields[columns["bid"]])
        ask = float(fields[columns["ask"]])
        raw_bid_volume = fields[columns["bid_volume"]] if "bid_volume" in columns else b""
        raw_ask_volume = fields[columns["ask_volume"]] if "ask_volume" in columns else b""
        bid_volume = float(raw_bid_volume) if raw_bid_volume else None
        ask_volume = float(raw_ask_volume) if raw_ask_volume else None
    except (IndexError, KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"{path.name}:{line_number}: invalid tick row: {exc}") from exc
    values = [bid, ask] + [value for value in (bid_volume, ask_volume) if value is not None]
    if not all(math.isfinite(value) for value in values) or bid <= 0 or ask <= 0:
        raise ValueError(f"{path.name}:{line_number}: non-finite or non-positive quote")
    if ask < bid:
        raise ValueError(f"{path.name}:{line_number}: ask is below bid")
    return tick_time, bid, ask, bid_volume, ask_volume


def _bar_start(timestamp: datetime, interval_minutes: int) -> datetime:
    minute_of_day = timestamp.hour * 60 + timestamp.minute
    bucket_minute = (minute_of_day // interval_minutes) * interval_minutes
    return timestamp.replace(hour=bucket_minute // 60, minute=bucket_minute % 60,
                             second=0, microsecond=0, tzinfo=None)


def import_tick_months(
    db: Session,
    input_dir: str | os.PathLike[str],
    symbol: str = "XAUUSD",
    timeframe: str = "4h",
    *,
    year: int = 2026,
    source_month: int = 1,
    batch_size: int = 5000,
    progress: Callable[[str, int], None] | None = None,
) -> dict[str, Any]:
    """Import files named 1.csv..12.csv as Jan..Dec, fixing repeated source months.

    Files are processed in filename order. The master-merged file must not be
    used because its repeated January timestamps interleave month segments.
    """
    interval = parse_timeframe_minutes(timeframe)
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    if not 1 <= source_month <= 12 or not 1 <= year <= 9999:
        raise ValueError("year and source_month are invalid")
    symbol = symbol.strip().upper()
    if not symbol:
        raise ValueError("symbol must not be empty")

    root = Path(input_dir)
    files = sorted(
        (path for path in root.glob("*.csv") if path.stem.isdigit()),
        key=lambda path: int(path.stem),
    )
    if not files:
        raise FileNotFoundError(f"no numbered month CSV files in {root}")
    months = [int(path.stem) for path in files]
    if months != list(range(months[0], months[-1] + 1)):
        raise ValueError("numbered month files must be a contiguous sequence")

    stats: dict[str, Any] = {
        "files": len(files),
        "ticks_read": 0,
        "candles_generated": 0,
        "candles_imported": 0,
        "duplicates": 0,
        "first_timestamp": None,
        "last_timestamp": None,
    }
    candle_batch: list[dict[str, Any]] = []
    active: _Candle | None = None
    previous_tick: datetime | None = None
    line_count = 0

    import polars as pl

    def read_batches(path: Path, target_month: int):
        nonlocal active, line_count, previous_tick
        with path.open("rb") as source:
            original_headers = [
                name.strip()
                for name in source.readline().lstrip(b"\xef\xbb\xbf").rstrip(b"\r\n").decode("ascii").split(",")
            ]
        headers = [
            name.lower() for name in original_headers
        ]
        required = {"time", "bid", "ask"}
        if not required.issubset(headers):
            raise ValueError(f"{path.name}: required columns are Time, Bid, Ask")
        available_volumes = [name for name in ("bid_volume", "ask_volume") if name in headers]
        columns = [original_headers[headers.index(name)] for name in ("time", "bid", "ask", *available_volumes)]
        column_names = {name: original_headers[headers.index(name)] for name in headers}
        overrides = {column_names["time"]: pl.String,
                     column_names["bid"]: pl.Float64,
                     column_names["ask"]: pl.Float64}
        for name in columns[3:]:
            overrides[name] = pl.Float64
        reader = pl.read_csv_batched(
            path,
            columns=columns,
            schema_overrides=overrides,
            batch_size=250_000,
            low_memory=True,
            infer_schema_length=0,
            try_parse_dates=False,
        )
        while batches := reader.next_batches(1):
            for frame in batches:
                if frame.height == 0:
                    continue
                parsed = frame.with_columns(
                    pl.col("Time").str.strptime(
                        pl.Datetime("us"), format="%Y-%m-%d %H:%M:%S%.f", strict=True,
                    ).alias("_source_time")
                )
                if not parsed["_source_time"].is_sorted():
                    raise ValueError(f"{path.name}: tick timestamps are not sorted")
                first_source = parsed["_source_time"][0]
                if first_source.month != source_month or parsed["_source_time"][-1].month != source_month:
                    raise ValueError(f"{path.name}: source timestamps do not match expected month {source_month}")

                expressions = [
                    pl.col("_source_time").dt.replace(year=year, month=target_month).alias("timestamp"),
                    pl.col("Bid").is_finite().all().alias("_finite_bid"),
                    pl.col("Ask").is_finite().all().alias("_finite_ask"),
                    (pl.col("Ask") >= pl.col("Bid")).all().alias("_valid_spread"),
                ]
                if available_volumes:
                    for name in available_volumes:
                        expressions.append(pl.col(column_names[name]).fill_null(0.0))
                parsed = parsed.with_columns(expressions)
                if not parsed.select("_finite_bid", "_finite_ask", "_valid_spread").row(0) == (True, True, True):
                    raise ValueError(f"{path.name}: found a non-finite quote or ask below bid")
                first_target = parsed["timestamp"][0]
                if previous_tick is not None and first_target < previous_tick:
                    raise ValueError(f"{path.name}: rebased ticks are not sorted across batches")
                previous_tick = parsed["timestamp"][-1]
                stats["first_timestamp"] = stats["first_timestamp"] or parsed["timestamp"][0].isoformat()
                stats["last_timestamp"] = parsed["timestamp"][-1].isoformat()
                stats["ticks_read"] += frame.height
                line_count += frame.height

                volume_expr = None
                if available_volumes:
                    volume_expr = sum(pl.col(column_names[name]).fill_null(0.0) for name in available_volumes)
                aggregations = [
                    pl.col("Bid").first().alias("open"),
                    pl.col("Bid").max().alias("high"),
                    pl.col("Bid").min().alias("low"),
                    pl.col("Bid").last().alias("close"),
                    pl.col("Bid").last().alias("bid"),
                    pl.col("Ask").last().alias("ask"),
                    (pl.col("Ask") - pl.col("Bid")).sum().alias("_spread_total"),
                    pl.len().alias("_tick_count"),
                ]
                if volume_expr is not None:
                    aggregations.append(volume_expr.sum().alias("volume"))
                grouped = (
                    parsed.with_columns(pl.col("timestamp").dt.truncate(f"{interval}m").alias("bucket"))
                    .group_by("bucket", maintain_order=True)
                    .agg(aggregations)
                )
                for row in grouped.iter_rows(named=True):
                    row["volume"] = row.get("volume")
                    if active is None:
                        active = _Candle.from_aggregate(row)
                    elif row["bucket"] == active.timestamp:
                        active.merge_aggregate(row)
                    else:
                        emit_candle(active)
                        active = _Candle.from_aggregate(row)

    def save_batch() -> None:
        if not candle_batch:
            return
        timestamps = [row["timestamp"] for row in candle_batch]
        existing = {
            row[0]
            for row in db.query(MarketData.timestamp)
            .filter(
                MarketData.symbol == symbol,
                MarketData.timeframe == timeframe,
                MarketData.timestamp.in_(timestamps),
            )
            .all()
        }
        seen: set[datetime] = set()
        mappings = []
        for row in candle_batch:
            timestamp = row["timestamp"]
            if timestamp in existing or timestamp in seen:
                stats["duplicates"] += 1
                continue
            seen.add(timestamp)
            mappings.append(row)
        try:
            if mappings:
                db.bulk_insert_mappings(MarketData, mappings)
            db.commit()
        except Exception:
            db.rollback()
            raise
        stats["candles_imported"] += len(mappings)
        candle_batch.clear()

    def emit_candle(candle: _Candle) -> None:
        stats["candles_generated"] += 1
        candle_batch.append(candle.row(symbol, timeframe))
        if len(candle_batch) >= batch_size:
            save_batch()

    for path in files:
        target_month = int(path.stem)
        line_count = 0
        read_batches(path, target_month)
        if progress:
            progress(path.name, line_count)
        if active is not None:
            emit_candle(active)
            active = None
        save_batch()

    return stats

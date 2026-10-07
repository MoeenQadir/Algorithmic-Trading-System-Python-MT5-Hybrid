"""Timeframe resampling utilities (Milestone 1).

Resamples lower-timeframe candles to higher timeframes using standard
OHLCV aggregation. Maintains deterministic behavior with no look-ahead.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from app.utils.timeutils import to_utc

VALID_TIMEFRAMES = {"1m", "5m", "15m", "30m", "1h", "4h", "1d"}
TIMEFRAME_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}


def _timeframe_key(ts: datetime, timeframe_minutes: int) -> datetime:
    """Round timestamp down to the start of the timeframe bucket."""
    total_minutes = ts.hour * 60 + ts.minute
    bucket_start = (total_minutes // timeframe_minutes) * timeframe_minutes
    return ts.replace(hour=bucket_start // 60, minute=bucket_start % 60, second=0, microsecond=0)


def resample_candles(
    candles: list[dict[str, Any]],
    target_timeframe: str,
) -> list[dict[str, Any]]:
    """Resample candles from lower to higher timeframe.

    Args:
        candles: List of normalized candle dicts (must have timestamp, open, high, low, close, volume)
        target_timeframe: Target timeframe string (e.g., "1h", "4h", "1d")

    Returns:
        List of resampled candles, sorted chronologically
    """
    if not candles:
        return []

    target_minutes = TIMEFRAME_MINUTES.get(target_timeframe)
    if target_minutes is None:
        raise ValueError(f"Invalid target timeframe: {target_timeframe}")

    source_minutes = TIMEFRAME_MINUTES.get(candles[0].get("timeframe", "1m"), 1)
    if target_minutes <= source_minutes:
        raise ValueError(f"Target timeframe ({target_timeframe}) must be higher than source ({candles[0].get('timeframe', 'unknown')})")

    # Group by target timeframe bucket
    buckets: dict[datetime, list[dict[str, Any]]] = defaultdict(list)

    for c in candles:
        ts = c["timestamp"]
        if not isinstance(ts, datetime):
            continue
        bucket_key = _timeframe_key(ts, target_minutes)
        buckets[bucket_key].append(c)

    # Aggregate each bucket
    resampled: list[dict[str, Any]] = []
    for bucket_ts in sorted(buckets.keys()):
        bucket_candles = buckets[bucket_ts]
        if not bucket_candles:
            continue

        # Sort by timestamp to get correct open/close
        bucket_candles.sort(key=lambda x: x["timestamp"])

        first = bucket_candles[0]
        last = bucket_candles[-1]

        opens = [c.get("open") for c in bucket_candles if c.get("open") is not None]
        highs = [c.get("high") for c in bucket_candles if c.get("high") is not None]
        lows = [c.get("low") for c in bucket_candles if c.get("low") is not None]
        closes = [c.get("close") for c in bucket_candles if c.get("close") is not None]
        volumes = [c.get("volume") for c in bucket_candles if c.get("volume") is not None]

        bids = [c.get("bid") for c in bucket_candles if c.get("bid") is not None]
        asks = [c.get("ask") for c in bucket_candles if c.get("ask") is not None]
        spreads = [c.get("spread") for c in bucket_candles if c.get("spread") is not None]

        # Aggregate OHLCV
        open_price = opens[0] if opens else None
        high_price = max(highs) if highs else None
        low_price = min(lows) if lows else None
        close_price = closes[-1] if closes else None
        volume = sum(volumes) if volumes else None
        bid = bids[-1] if bids else None
        ask = asks[-1] if asks else None
        spread = spreads[-1] if spreads else None

        if open_price is None or high_price is None or low_price is None or close_price is None:
            continue  # Skip incomplete buckets

        resampled.append({
            "timestamp": to_utc(bucket_ts),
            "symbol": first.get("symbol", "UNKNOWN"),
            "timeframe": target_timeframe,
            "open": open_price,
            "high": high_price,
            "low": low_price,
            "close": close_price,
            "volume": volume,
            "bid": bid,
            "ask": ask,
            "spread": spread,
            "source": f"resampled_{target_timeframe}",
        })

    return resampled


def resample_to_all_timeframes(
    candles: list[dict[str, Any]],
    source_timeframe: str = "1m",
) -> dict[str, list[dict[str, Any]]]:
    """Resample source candles to all higher timeframes."""
    if source_timeframe not in TIMEFRAME_MINUTES:
        raise ValueError(f"Invalid source timeframe: {source_timeframe}")

    source_minutes = TIMEFRAME_MINUTES[source_timeframe]
    result: dict[str, list[dict[str, Any]]] = {}

    for tf, minutes in TIMEFRAME_MINUTES.items():
        if minutes > source_minutes:
            result[tf] = resample_candles(candles, tf)

    return result
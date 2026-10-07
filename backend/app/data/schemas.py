"""Normalized market-data schemas. Unavailable fields stay None — never fabricated."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class Candle(BaseModel):
    timestamp: datetime
    symbol: str
    timeframe: str
    open: float
    high: float
    low: float
    close: float
    volume: Optional[float] = None
    bid: Optional[float] = None
    ask: Optional[float] = None
    spread: Optional[float] = None
    source: str = "local"


class Quote(BaseModel):
    symbol: str
    timestamp: datetime
    bid: Optional[float] = None
    ask: Optional[float] = None
    last: Optional[float] = None
    spread: Optional[float] = None
    source: str = "local"


VALID_TIMEFRAMES = {"1m", "5m", "15m", "30m", "1h", "4h", "1d"}
TIMEFRAME_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}


def validate_timeframe(tf: str) -> str:
    tf = tf.strip().lower()
    if tf not in VALID_TIMEFRAMES:
        raise ValueError(f"Unsupported timeframe '{tf}'. Valid: {sorted(VALID_TIMEFRAMES)}")
    return tf


def spread_pct(bid: Optional[float], ask: Optional[float]) -> Optional[float]:
    """Relative spread = (ask-bid)/mid. None when quotes unavailable."""
    if bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
        return None
    mid = (bid + ask) / 2
    if mid <= 0:
        return None
    return (ask - bid) / mid

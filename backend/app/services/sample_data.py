"""Synthetic sample data (Milestone 29).

GENERATES CLEARLY-LABELED SAMPLE DATA so the project works immediately.
This is NOT real market data. The `source` column is set to "SAMPLE_DATA"
and every row is deterministic (fixed seed) so tests are reproducible.

The sample series is engineered to contain occasional liquidity sweeps,
structure breaks and displacement so the strategy engine can find setups.
"""
from __future__ import annotations

import math
from datetime import timedelta

from app.config import get_settings
from app.utils.timeutils import utcnow


def generate_sample_candles(symbol: str = "US100", timeframe: str = "15m", n: int = 1200,
                            seed: int = 42) -> list[dict]:
    """Deterministic pseudo-market with trends, ranges, sweeps and displacement."""
    import random

    rng = random.Random(seed)
    minutes = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}[timeframe]
    end = utcnow().replace(second=0, microsecond=0)
    # align to timeframe boundary
    ts = end - timedelta(minutes=minutes * n)
    price = 15000.0
    candles: list[dict] = []
    regime = "range"
    regime_left = 0
    trend_drift = 0.0

    for i in range(n):
        if regime_left <= 0:
            regime = rng.choice(["range", "uptrend", "downtrend", "volatile"])
            regime_left = rng.randint(40, 120)
            trend_drift = {"range": 0.0, "uptrend": 4.0, "downtrend": -4.0, "volatile": 0.0}[regime]
        regime_left -= 1

        vol = 12.0 if regime == "volatile" else 6.0
        open_price = price
        change = rng.gauss(trend_drift, vol)
        close_price = open_price + change
        wick = abs(rng.gauss(0, vol / 2))
        high = max(open_price, close_price) + wick
        low = min(open_price, close_price) - wick

        # engineered displacement candles
        if rng.random() < 0.03:
            body = (vol * rng.uniform(2, 4)) * (1 if rng.random() < 0.6 else -1)
            close_price = open_price + body
            high = max(open_price, close_price) + wick * 0.5
            low = min(open_price, close_price) - wick * 0.5

        # engineered liquidity sweep: pierce previous extreme then close back inside
        if i > 40 and rng.random() < 0.02 and candles:
            prev_hi = max(c["high"] for c in candles[-20:])
            prev_lo = min(c["low"] for c in candles[-20:])
            if rng.random() < 0.5:
                high = prev_hi + rng.uniform(1, 5)
                close_price = prev_hi - rng.uniform(1, 4)
                if close_price < open_price:
                    open_price = close_price + rng.uniform(2, 6)
            else:
                low = prev_lo - rng.uniform(1, 5)
                close_price = prev_lo + rng.uniform(1, 4)
                if close_price > open_price:
                    open_price = close_price - rng.uniform(2, 6)
            high = max(high, open_price, close_price)
            low = min(low, open_price, close_price)

        volume = max(1.0, rng.gauss(1000, 200)) * (2.5 if abs(close_price - open_price) > vol * 1.5 else 1.0)
        spread = round(rng.uniform(0.5, 1.5), 2)
        candles.append({
            "timestamp": ts + timedelta(minutes=minutes * i),
            "symbol": symbol.upper(), "timeframe": timeframe,
            "open": round(open_price, 2), "high": round(high, 2),
            "low": round(low, 2), "close": round(close_price, 2),
            "volume": round(volume, 1), "bid": round(close_price - spread / 2, 2),
            "ask": round(close_price + spread / 2, 2), "spread": spread,
        })
        price = close_price
    return candles


def seed_if_empty() -> int:
    """Seed the default symbol/timeframe with SAMPLE DATA if DB is empty."""
    from app.models.database import MarketData, get_session_factory

    s = get_settings()
    SessionLocal = get_session_factory()
    db = SessionLocal()
    try:
        existing = db.query(MarketData).filter(
            MarketData.symbol == s.default_symbol.upper(),
            MarketData.timeframe == s.default_timeframe,
        ).count()
        if existing > 0:
            return 0
        candles = generate_sample_candles(s.default_symbol, s.default_timeframe, n=1200)
        for c in candles:
            db.add(MarketData(
                symbol=c["symbol"], timeframe=c["timeframe"], timestamp=c["timestamp"],
                open=c["open"], high=c["high"], low=c["low"], close=c["close"],
                volume=c["volume"], bid=c["bid"], ask=c["ask"], spread=c["spread"],
                source="SAMPLE_DATA",
            ))
        db.commit()
        return len(candles)
    finally:
        db.close()


def seed_symbol(symbol: str, timeframe: str, n: int = 1200) -> int:
    """Explicitly seed a symbol/timeframe with SAMPLE DATA."""
    from app.models.database import MarketData, get_session_factory

    SessionLocal = get_session_factory()
    db = SessionLocal()
    try:
        candles = generate_sample_candles(symbol, timeframe, n=n)
        for c in candles:
            db.add(MarketData(
                symbol=c["symbol"], timeframe=c["timeframe"], timestamp=c["timestamp"],
                open=c["open"], high=c["high"], low=c["low"], close=c["close"],
                volume=c["volume"], bid=c["bid"], ask=c["ask"], spread=c["spread"],
                source="SAMPLE_DATA",
            ))
        db.commit()
        return len(candles)
    finally:
        db.close()

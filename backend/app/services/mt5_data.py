from __future__ import annotations

import MetaTrader5 as mt5
from datetime import datetime, timezone
import polars as pl
from app.models.database import MarketData, get_session_factory
from app.config import get_settings


def download_mt5_data(symbol: str, timeframe: str, start_date: str, end_date: str) -> int:
    """Download historical data from MT5 and store in SQLite database."""
    settings = get_settings()
    
    # Initialize MT5 connection with credentials if provided
    if settings.mt5_login and settings.mt5_password and settings.mt5_server:
        if not mt5.initialize(
            login=settings.mt5_login,
            password=settings.mt5_password,
            server=settings.mt5_server,
            path=settings.mt5_path if settings.mt5_path else None
        ):
            raise Exception(f"Failed to initialize MT5 with credentials: {mt5.last_error()}")
    else:
        # Try without credentials (if MT5 is already running)
        if not mt5.initialize():
            raise Exception(f"Failed to initialize MT5: {mt5.last_error()}")

    # Convert timeframe to MT5 format
    mt5_timeframe = {
        "M1": mt5.TIMEFRAME_M1,
        "M5": mt5.TIMEFRAME_M5,
        "M15": mt5.TIMEFRAME_M15,
        "M30": mt5.TIMEFRAME_M30,
        "H1": mt5.TIMEFRAME_H1,
        "H4": mt5.TIMEFRAME_H4,
        "D1": mt5.TIMEFRAME_D1,
        "W1": mt5.TIMEFRAME_W1,
        "MN1": mt5.TIMEFRAME_MN1,
    }.get(timeframe.upper(), mt5.TIMEFRAME_M15)

    # Convert dates to datetime objects
    start = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end = datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    # Download data from MT5
    rates = mt5.copy_rates_range(symbol, mt5_timeframe, start, end)
    mt5.shutdown()

    if rates is None or len(rates) == 0:
        raise Exception(f"No data received from MT5 for {symbol} {timeframe}")

    # Convert to Polars DataFrame
    df = pl.from_numpy(rates)
    df = df.rename({
        "time": "timestamp",
        "open": "open",
        "high": "high",
        "low": "low",
        "close": "close",
        "tick_volume": "volume",
        "spread": "spread",
        "real_volume": "real_volume",
    })

    # Convert timestamp to datetime and format
    df = df.with_columns(
        pl.col("timestamp").map_elements(lambda x: datetime.fromtimestamp(x, tz=timezone.utc).isoformat())
    )

    # Store in SQLite database
    SessionLocal = get_session_factory()
    db = SessionLocal()
    try:
        for row in df.iter_rows(named=True):
            db.add(MarketData(
                symbol=symbol.upper(),
                timeframe=timeframe,
                timestamp=row["timestamp"],
                open=row["open"],
                high=row["high"],
                low=row["low"],
                close=row["close"],
                volume=row["volume"],
                bid=row["close"] - (row["spread"] / 2),
                ask=row["close"] + (row["spread"] / 2),
                spread=row["spread"],
                source="MT5",
            ))
        db.commit()
        return len(df)
    finally:
        db.close()
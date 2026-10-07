"""MetaTrader 5 market data provider using Polars for zero-copy processing.

Performance notes:
- mt5.copy_rates_from_pos() returns a raw NumPy structured array.
- The array is passed directly to polars.from_numpy() (zero-copy construction).
- All transformations are vectorized Polars column operations.
- No Pandas anywhere in the pipeline.

Graceful degradation:
- MetaTrader5 is Windows-only. On Linux/Mac/Docker (or when the terminal is
  closed) MT5_AVAILABLE is False and every method returns empty data so the
  provider fallback chain can move to the next provider without errors.
"""

from __future__ import annotations

import logging
import platform
from datetime import datetime, timezone, timedelta
from typing import Any, List, Optional

import numpy as np
import polars as pl

from .providers import MarketDataProvider as BaseDataProvider

logger = logging.getLogger(__name__)

# Try to import MetaTrader5, gracefully handle failure on non-Windows.
try:
    if platform.system() == "Windows":
        import MetaTrader5 as mt5

        MT5_AVAILABLE = True
    else:
        MT5_AVAILABLE = False
        mt5 = None  # type: ignore
except ImportError:
    MT5_AVAILABLE = False
    mt5 = None  # type: ignore


class MT5DataProvider(BaseDataProvider):
    """MetaTrader 5 OHLC provider with zero-copy Polars processing.

    Only works on Windows when the MetaTrader5 terminal is installed and
    running. On other platforms, or when the terminal is closed, every method
    returns empty data so the fallback chain moves on seamlessly.
    """

    name = "mt5"

    # Broker-specific symbol suffixes to probe (in order).
    SYMBOL_SUFFIXES = ["", "m", ".a", ".b", "_c", "_d", ".ecn", ".pro", ".raw"]

    def __init__(self) -> None:
        self._initialized = False
        if MT5_AVAILABLE and platform.system() == "Windows":
            self._initialize_mt5()

    # ------------------------------------------------------------------ setup
    def _initialize_mt5(self) -> bool:
        """Initialize the connection to the MetaTrader5 terminal."""
        if not MT5_AVAILABLE or mt5 is None:
            return False
        try:
            if not mt5.initialize():
                logger.error("MT5 initialize() failed: %s", mt5.last_error())
                return False
            account_info = mt5.account_info()
            if account_info is None:
                logger.error("MT5 account_info() failed: %s", mt5.last_error())
                mt5.shutdown()
                return False
            self._initialized = True
            logger.info("MT5 provider initialized for account %s", account_info.login)
            return True
        except Exception as exc:
            logger.exception("Failed to initialize MT5: %s", exc)
            return False

    def is_available(self) -> bool:
        """True only when MetaTrader5 is present AND a terminal is reachable."""
        if not MT5_AVAILABLE or mt5 is None:
            return False
        if platform.system() != "Windows":
            return False
        if not self._initialized:
            return self._initialize_mt5()
        try:
            return mt5.terminal_info() is not None
        except Exception:
            self._initialized = False
            return False

    def shutdown(self) -> None:
        """Close the MT5 terminal connection."""
        if self._initialized and MT5_AVAILABLE and mt5 is not None:
            try:
                mt5.shutdown()
                self._initialized = False
                logger.info("MT5 provider shutdown complete")
            except Exception as exc:
                logger.exception("Error shutting down MT5: %s", exc)

    # --------------------------------------------------------------- helpers
    def _timeframe_constant(self, timeframe: str):
        """Lazily resolve a framework timeframe string to its MT5 constant.

        Built lazily (not at import time) because the MT5 constants only exist
        when the MetaTrader5 module is installed.
        """
        if not MT5_AVAILABLE or mt5 is None:
            return None
        mapping = {
            "1m": mt5.TIMEFRAME_M1,
            "5m": mt5.TIMEFRAME_M5,
            "15m": mt5.TIMEFRAME_M15,
            "30m": mt5.TIMEFRAME_M30,
            "1h": mt5.TIMEFRAME_H1,
            "4h": mt5.TIMEFRAME_H4,
            "1d": mt5.TIMEFRAME_D1,
        }
        return mapping.get(timeframe.strip().lower())

    def _normalize_symbol(self, symbol: str) -> str:
        """Resolve broker-specific symbol aliases (XAUUSD, XAUUSDm, XAUUSD.a...).

        Probes the terminal for each suffix variant and returns the first
        symbol that exists and is visible. Falls back to the original symbol.
        """
        if not self.is_available():
            return symbol

        base_symbol = symbol.strip().upper()
        # Strip a known broker suffix from an already-suffixed symbol.
        for suffix in self.SYMBOL_SUFFIXES:
            if suffix and base_symbol.endswith(suffix.upper()):
                base_symbol = base_symbol[: -len(suffix)]
                break

        for suffix in self.SYMBOL_SUFFIXES:
            candidate = base_symbol + suffix
            try:
                info = mt5.symbol_info(candidate)
                if info is not None and info.visible:
                    if candidate != symbol:
                        logger.debug("MT5 symbol %s resolved to %s", symbol, candidate)
                    return candidate
            except Exception:
                continue
        return symbol

    @staticmethod
    def _rates_to_polars(rates: np.ndarray) -> pl.DataFrame:
        """Zero-copy Polars construction + vectorized UTC/OHLCV normalization."""
        df = pl.from_numpy(rates)
        return df.select(
            [
                # MT5 'time' is unix seconds -> microseconds for Polars Datetime,
                # then attach the UTC timezone (wall-clock values are UTC).
                (
                    (pl.col("time").cast(pl.Int64) * 1_000_000)
                    .cast(pl.Datetime("us"))
                    .dt.replace_time_zone("UTC")
                ).alias("timestamp"),
                pl.col("open").cast(pl.Float64),
                pl.col("high").cast(pl.Float64),
                pl.col("low").cast(pl.Float64),
                pl.col("close").cast(pl.Float64),
                pl.col("tick_volume").cast(pl.Int64).alias("volume"),
            ]
        )

    # ------------------------------------------------------------------ data
    def get_ohlcv(self, symbol: str, timeframe: str, limit: int = 500) -> List[dict[str, Any]]:
        """Recent candles straight from the MT5 terminal (zero-copy Polars)."""
        if not self.is_available():
            return []
        mt5_timeframe = self._timeframe_constant(timeframe)
        if mt5_timeframe is None:
            logger.warning("Unsupported timeframe for MT5: %s", timeframe)
            return []

        resolved = self._normalize_symbol(symbol)
        try:
            rates = mt5.copy_rates_from_pos(resolved, mt5_timeframe, 0, max(1, limit))
            if rates is None or len(rates) == 0:
                logger.warning("MT5 returned no data for %s %s", resolved, timeframe)
                return []
            df = self._rates_to_polars(rates)
            result = df.to_dicts()
            logger.debug("MT5: %d candles for %s %s", len(result), resolved, timeframe)
            return result
        except Exception as exc:
            logger.exception("MT5 get_ohlcv failed for %s %s: %s", symbol, timeframe, exc)
            return []

    def get_historical(self, symbol: str, timeframe: str, start: datetime, end: datetime) -> List[dict[str, Any]]:
        """Historical candles within [start, end], vectorized-filtered by Polars."""
        if not self.is_available():
            return []
        mt5_timeframe = self._timeframe_constant(timeframe)
        if mt5_timeframe is None:
            return []

        start_utc = start if start.tzinfo else start.replace(tzinfo=timezone.utc)
        end_utc = end if end.tzinfo else end.replace(tzinfo=timezone.utc)

        resolved = self._normalize_symbol(symbol)
        all_rates: List[Any] = []
        # Request in chunks to avoid MT5 terminal limits
        chunk_size = 100000  # bars per request
        # We'll iterate backwards from end to start
        current_end = end_utc
        while current_end > start_utc:
            # Calculate how many bars we need to go back from current_end to reach start_utc
            # We approximate using the timeframe minutes, but we'll just request chunk_size bars each time
            try:
                rates = mt5.copy_rates_from_pos(resolved, mt5_timeframe, 0, chunk_size)
                if rates is None or len(rates) == 0:
                    # No more data available
                    break
                df = self._rates_to_polars(rates)
                # Filter to only those within [start_utc, current_end]
                df = df.filter((pl.col("time") >= pl.lit(start_utc)) & (pl.col("time") <= pl.lit(current_end)))
                if df.is_empty():
                    # If the chunk is entirely before start_utc, we can break
                    break
                all_rates.extend(df.to_dicts())
                # Update current_end to the earliest timestamp in this chunk minus one timeframe interval
                # to avoid overlap and move backwards
                oldest_in_chunk = df["time"].min()
                # Convert to datetime
                oldest_dt = oldest_in_chunk.to_pydatetime()
                # Move current_end to just before the oldest bar in this chunk
                current_end = oldest_dt - timedelta(minutes=1)
                # If we've gone past start_utc, we can break
                if current_end < start_utc:
                    break
            except Exception as exc:
                logger.exception("MT5 get_historical chunk failed for %s %s: %s", symbol, timeframe, exc)
                break

        if not all_rates:
            return []
        # Combine and sort by time
        combined_df = pl.DataFrame(all_rates)
        # Filter again to ensure within range (should already be)
        combined_df = combined_df.filter((pl.col("time") >= pl.lit(start_utc)) & (pl.col("time") <= pl.lit(end_utc)))
        return combined_df.sort("time").to_dicts()

    def get_quote(self, symbol: str) -> Optional[dict[str, Any]]:
        """Latest bid/ask tick from the MT5 terminal."""
        if not self.is_available():
            return None
        resolved = self._normalize_symbol(symbol)
        try:
            tick = mt5.symbol_info_tick(resolved)
            if tick is None:
                return None
            ts = tick.time
            if isinstance(ts, datetime):
                ts_utc = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
            else:
                ts_utc = datetime.fromtimestamp(float(ts), tz=timezone.utc)
            return {
                "symbol": resolved.upper(),
                "bid": float(tick.bid),
                "ask": float(tick.ask),
                "last": float(tick.last),
                "spread": round(float(tick.ask) - float(tick.bid), 10),
                "timestamp": ts_utc.isoformat(),
                "source": self.name,
            }
        except Exception as exc:
            logger.exception("MT5 get_quote failed for %s: %s", symbol, exc)
            return None

    def get_supported_timeframes(self) -> list[str]:
        return ["1m", "5m", "15m", "30m", "1h", "4h", "1d"]

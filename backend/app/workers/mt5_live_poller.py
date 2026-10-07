"""Background worker for polling MT5 for live 1m data and triggering signal generation."""
from __future__ import annotations

import time
import threading
from datetime import datetime, timezone
from typing import Optional

import logging

from app.data.providers import get_provider
from app.models.database import MarketData, get_session_factory
from app.services.pipeline import TradingPipeline
from app.utils.timeutils import to_utc, utcnow
from app.config import get_settings

logger = logging.getLogger(__name__)


class MT5LivePoller:
    """Polls MT5 for new completed candles and triggers signal generation."""

    def __init__(self, poll_interval: int = 30):
        self.poll_interval = poll_interval
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.settings = get_settings()
        self.pipeline = TradingPipeline()
        # Track last processed candle timestamp per symbol/timeframe to avoid duplicates
        self._last_processed: dict[str, datetime] = {}

    def start(self) -> None:
        """Start the polling thread."""
        if self._thread is not None and self._thread.is_alive():
            logger.warning("MT5 live poller is already running")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="MT5LivePoller", daemon=True)
        self._thread.start()
        logger.info("MT5 live poller started")

    def stop(self) -> None:
        """Stop the polling thread."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
        logger.info("MT5 live poller stopped")

    def _run(self) -> None:
        """Main polling loop."""
        while not self._stop_event.is_set():
            try:
                self._poll_and_process()
            except Exception as e:
                logger.exception("Error in MT5 live poller loop: %s", e)
            # Sleep in small increments to check for stop event frequently
            for _ in range(self.poll_interval):
                if self._stop_event.is_set():
                    break
                time.sleep(1)

    def _poll_and_process(self) -> None:
        """Poll MT5 for new data and process."""
        # Only poll if MT5 is available and configured as a provider
        if not self._is_mt5_available():
            return

        symbol = self.settings.default_symbol
        timeframe = self.settings.default_timeframe

        # Get the last processed timestamp for this symbol/timeframe
        key = f"{symbol}:{timeframe}"
        last_processed = self._last_processed.get(key)

        # Fetch the last 2 candles from MT5 (to handle the forming candle)
        try:
            provider = get_provider("mt5")
            candles = provider.get_ohlcv(symbol, timeframe, limit=2)
        except Exception as e:
            logger.error("Failed to fetch data from MT5 for %s %s: %s", symbol, timeframe, e)
            return

        if not candles or len(candles) < 2:
            logger.warning("Insufficient data from MT5 for %s %s", symbol, timeframe)
            return

        # Sort candles by timestamp (oldest first) - MT5 provider uses "timestamp" field
        candles.sort(key=lambda x: x.get("timestamp") or datetime.min.replace(tzinfo=timezone.utc))

        # The second last candle is the most recent completed candle (assuming the last is forming)
        completed_candle = candles[-2]
        completed_timestamp = completed_candle.get("timestamp")

        # Check if we've already processed this candle
        if last_processed and completed_timestamp <= last_processed:
            return

        # Persist the completed candle
        try:
            # Add symbol and timeframe to the candle data for persistence
            candle_to_persist = completed_candle.copy()
            candle_to_persist["symbol"] = symbol
            candle_to_persist["timeframe"] = timeframe
            candle_to_persist["source"] = "mt5"
            
            added = self.pipeline.persist_candles([candle_to_persist])
            if added > 0:
                logger.info("Persisted new completed candle for %s %s at %s", symbol, timeframe, completed_timestamp)
                # Update last processed timestamp
                self._last_processed[key] = completed_timestamp
                # Trigger signal generation (without execution)
                result = self.pipeline.run_cycle(symbol, timeframe, execute=False)
                # Log the result (in a real system, this would be pushed to a stream/websocket)
                logger.info(
                    "Signal generated for %s %s: %s",
                    symbol,
                    timeframe,
                    result.get("decision", {}).get("action", "UNKNOWN"),
                )
        except Exception as e:
            logger.error("Failed to process candle for %s %s: %s", symbol, timeframe, e)

    def _is_mt5_available(self) -> bool:
        """Check if MT5 provider is available and configured."""
        # Check if MT5 is in the provider chain or is the main provider
        provider_chain = self.settings.market_data_provider_chain
        if "mt5" not in provider_chain:
            return False
        # Try to get the MT5 provider to see if it's initialized
        try:
            provider = get_provider("mt5")
            return provider.is_available()
        except Exception:
            return False


# Global instance
_poller: Optional[MT5LivePoller] = None


def start_mt5_live_poller() -> None:
    """Start the global MT5 live poller."""
    global _poller
    if _poller is None:
        _poller = MT5LivePoller(
            poll_interval=get_settings().real_time_poll_interval_sec
        )
        _poller.start()


def stop_mt5_live_poller() -> None:
    """Stop the global MT5 live poller."""
    global _poller
    if _poller is not None:
        _poller.stop()
        _poller = None
"""Real-time market data worker (Milestone 17 + Phases 37/38).

Polls market data providers on a configurable interval, processes completed
candles through the authoritative CompletedCandleEngine pipeline, and triggers
the full trading cycle. Implements:
- Exponential backoff on transient failures
- Heartbeat tracking for health monitoring
- Data mode awareness (DEMO/OFFLINE/LOCAL/REMOTE_REAL_DATA)
- No synthetic fallback in REMOTE_REAL_DATA mode (session pauses instead)
- Durable last-processed timestamp + DB dedup (no duplicate analysis)
"""
from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import Any, Optional

from app.config import get_settings
from app.data.providers import get_provider, parse_timeframe_minutes
from app.models.database import MarketData, get_session_factory
from app.services.pipeline import TradingPipeline, provider_health_report
from app.services.real_market import CompletedCandleEngine, RealMarketSession, get_session_manager
from app.utils.timeutils import utcnow

logger = logging.getLogger("trading.worker")


class DataMode:
    """Session data modes."""
    DEMO = "DEMO"
    OFFLINE = "OFFLINE"
    LOCAL = "LOCAL"
    REMOTE_REAL_DATA = "REMOTE_REAL_DATA"


class RealTimeWorker:
    """Hardened real-time data polling worker.

    Responsibilities:
    1. Poll the configured data provider at regular intervals
    2. Detect completed candles (candle timestamp + interval < now)
    3. Persist new candles via CompletedCandleEngine (dedup + quality gates)
    4. Trigger the trading pipeline exactly once per new completed candle
    5. Track heartbeats for health monitoring
    6. Implement exponential backoff on failures
    7. PAUSE the real-data session on provider failure (no synthetic fallback)
    """

    def __init__(self, symbol: str | None = None, timeframe: str | None = None,
                 poll_interval_sec: float | None = None, data_mode: str | None = None) -> None:
        s = get_settings()
        self.symbol = (symbol or s.default_symbol).upper()
        self.timeframe = timeframe or s.default_timeframe
        self.poll_interval = poll_interval_sec or s.real_time_poll_interval_sec
        self.data_mode = data_mode or self._resolve_data_mode()
        self.running = False
        self.heartbeat: Optional[Any] = None
        self.last_candle_time: Optional[Any] = None
        self.consecutive_failures: int = 0
        self.max_retries: int = s.real_time_max_retries
        self.pipeline = TradingPipeline()
        self._startup_time: Optional[Any] = None
        self.candle_engine = CompletedCandleEngine(self.symbol, self.timeframe)
        self.session: RealMarketSession = get_session_manager()

    def _resolve_data_mode(self) -> str:
        """Determine the data mode based on configuration."""
        s = get_settings()
        if s.market_data_provider in ("local", "stooq", "twelvedata"):
            if s.twelve_data_api_key and s.market_data_provider == "twelvedata":
                return DataMode.REMOTE_REAL_DATA
            return DataMode.LOCAL
        return DataMode.OFFLINE

    def start(self) -> None:
        """Start the real-time worker loop."""
        self.running = True
        self._startup_time = utcnow()
        self.session.resume()
        logger.info("RealTimeWorker started: symbol=%s timeframe=%s mode=%s interval=%ds",
                    self.symbol, self.timeframe, self.data_mode, self.poll_interval)

    def stop(self) -> None:
        """Stop the real-time worker loop."""
        self.running = False
        logger.info("RealTimeWorker stopped")

    def run_once(self) -> dict[str, Any]:
        """Execute a single polling cycle. Returns status dict."""
        self.heartbeat = utcnow()
        result: dict[str, Any] = {
            "timestamp": self.heartbeat.isoformat(),
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "data_mode": self.data_mode,
            "status": "ok",
            "new_candles": 0,
            "pipeline_triggered": False,
        }

        try:
            if self.data_mode == DataMode.REMOTE_REAL_DATA:
                result = self._poll_remote(result)
            else:
                result = self._poll_local(result)
            return result
        except Exception as exc:
            self.consecutive_failures += 1
            result["status"] = "error"
            result["error"] = str(exc)[:300]
            result["backoff_sec"] = self._compute_backoff()
            logger.warning("Worker cycle failed (%d consecutive): %s",
                           self.consecutive_failures, exc)
            return result

    # ------------------------------------------------------------------ remote
    def _poll_remote(self, result: dict[str, Any]) -> dict[str, Any]:
        """Poll remote provider with strict no-fallback policy (Phase 38).

        Provider failures PAUSE the session; cached data is never substituted
        and never labelled REAL DATA.
        """
        provider = get_provider()
        session = self.session

        # Fetch latest candles from provider
        try:
            candles = provider.get_ohlcv(self.symbol, self.timeframe, limit=5)
        except Exception as exc:
            category = getattr(exc, "category", "unknown")
            session.pause(f"provider fetch failed: {exc}", category)
            provider_health_report("market_data", "DEGRADED", f"real-data poll failed: {exc}")
            result["status"] = "paused"
            result["pause_reason"] = session.pause_reason
            result["session"] = session.status(worker_status=self.get_status())
            return result

        if not candles:
            session.pause("provider returned no data", "no_data")
            result["status"] = "paused"
            result["pause_reason"] = session.pause_reason
            result["session"] = session.status(worker_status=self.get_status())
            return result

        # Authoritative completed-candle processing (Phase 37)
        outcome = self.candle_engine.process(candles, source=provider.name)
        result["candle_outcome"] = {k: v for k, v in outcome.items() if k != "quality"}
        result["new_candles"] = outcome["persisted"]
        self.consecutive_failures = 0

        if outcome.get("quality_blocked"):
            session.pause("quality gate blocked incoming candles", "quality")
            result["status"] = "paused"
            result["pause_reason"] = session.pause_reason
            result["session"] = session.status(worker_status=self.get_status())
            return result

        if outcome["new_completed_candle"]:
            session.resume()
            session.on_candle_processed(outcome.get("last_processed_at"))
            result["pipeline_triggered"] = True
            pipeline_result = self.pipeline.run_cycle(self.symbol, self.timeframe, execute=True)
            decision = pipeline_result.get("decision", {})
            session.on_analysis(decision.get("action") or decision.get("direction"))
            result["pipeline"] = pipeline_result
        elif session.paused:
            # provider recovered but nothing new yet
            session.resume()

        result["session"] = session.status(worker_status=self.get_status())
        return result

    # ------------------------------------------------------------------ local
    def _poll_local(self, result: dict[str, Any]) -> dict[str, Any]:
        """Poll local database (offline/demo mode)."""
        db = get_session_factory()()
        try:
            last_candle = (
                db.query(MarketData)
                .filter(MarketData.symbol == self.symbol, MarketData.timeframe == self.timeframe)
                .order_by(MarketData.timestamp.desc())
                .first()
            )
            if last_candle:
                self.last_candle_time = last_candle.timestamp
                self.session.on_candle_processed(last_candle.timestamp)
                # Trigger pipeline for demo/offline
                pipeline_result = self.pipeline.run_cycle(self.symbol, self.timeframe, execute=True)
                result["pipeline_triggered"] = True
                result["pipeline"] = pipeline_result
            else:
                result["status"] = "no_data"
                result["message"] = "No candles in local database"
        finally:
            db.close()

        self.consecutive_failures = 0
        result["session"] = self.session.status(worker_status=self.get_status())
        return result

    def _persist_candle(self, candle: dict[str, Any]) -> int:
        """Legacy single-candle persist (kept for compatibility). Returns 1 if new."""
        engine = CompletedCandleEngine(self.symbol, self.timeframe)
        outcome = engine.process([candle], source=candle.get("source", "remote"))
        return outcome["persisted"]

    def _compute_backoff(self) -> float:
        """Exponential backoff: min(2^failures, max_backoff)."""
        return min(2 ** self.consecutive_failures, get_settings().real_time_max_backoff_sec)

    def get_status(self) -> dict[str, Any]:
        """Return worker health status."""
        s = get_settings()
        return {
            "running": self.running,
            "state": "RUNNING" if self.running else ("PAUSED" if self.session.paused else "STOPPED"),
            "heartbeat": self.heartbeat.isoformat() if self.heartbeat else None,
            "last_candle_time": self.last_candle_time.isoformat() if self.last_candle_time else None,
            "consecutive_failures": self.consecutive_failures,
            "data_mode": self.data_mode,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "poll_interval_sec": self.poll_interval,
            "startup_time": self._startup_time.isoformat() if self._startup_time else None,
            "backoff_sec": self._compute_backoff() if self.consecutive_failures else 0,
        }


# --- Module-level singleton and run function ---

_worker: RealTimeWorker | None = None


def get_worker() -> RealTimeWorker:
    """Get or create the singleton worker instance."""
    global _worker
    if _worker is None:
        _worker = RealTimeWorker()
    return _worker


def run_real_time_worker() -> None:
    """Main entry point for the real-time worker loop.

    Run as: python -m app.services.real_time_worker
    Or: python scripts/real_time_worker.py
    """
    import signal

    worker = get_worker()
    worker.start()

    def _handle_signal(signum, frame):
        logger.info("Received signal %d, stopping worker...", signum)
        worker.stop()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    try:
        while worker.running:
            result = worker.run_once()
            if result.get("status") == "error":
                backoff = result.get("backoff_sec", worker.poll_interval)
                logger.debug("Backing off for %.1f sec", backoff)
                time.sleep(backoff)
            else:
                time.sleep(worker.poll_interval)
    except KeyboardInterrupt:
        worker.stop()

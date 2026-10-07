"""FastAPI application entrypoint.

- Initializes the database on startup (SQLite by default; zero setup).
- CORS restricted to configured origins.
- Seeds sample data on first run so the dashboard works immediately.
"""
from __future__ import annotations

import logging
import asyncio
from datetime import timedelta

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.utils.logging_config import setup_logging

setup_logging()
logger = logging.getLogger("trading.main")

from app.api.routes import router, _log_event  # noqa: E402
from app.routers.settings import router as settings_router
from app.models.database import init_db  # noqa: E402
from app.services.pipeline import TradingPipeline  # noqa: E402
from app.utils.timeutils import utcnow  # noqa: E402

settings = get_settings()
pipeline = TradingPipeline()

app = FastAPI(
    title="Multi-Agent Trading System",
    description="Local-first multi-agent trading research and paper-trading platform",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def on_startup() -> None:
    init_db()
    logger.info("database initialized (paper_trading=%s live_trading=%s)",
                settings.paper_trading, settings.live_trading)
    if settings.live_trading:
        logger.error("LIVE_TRADING is enabled but no live broker exists; all orders stay in paper mode")
    _log_event("INFO", "backend", "backend started", {"env": settings.env})
    try:
        from app.services.sample_data import seed_if_empty

        seed_if_empty()
    except Exception as exc:
        logger.warning("sample data seeding skipped: %s", exc)
    if settings.market_data_provider.lower() == "twelvedata" and settings.twelve_data_api_key:
        app.state.live_signal_task = asyncio.create_task(_live_signal_loop())
        logger.info("read-only live signal polling enabled for %s %s",
                    settings.default_symbol, settings.default_timeframe)
    else:
        app.state.live_signal_task = None
    
    # Start MT5 live poller for real-time data and signal generation
    from app.workers.mt5_live_poller import start_mt5_live_poller
    start_mt5_live_poller()
    logger.info("MT5 live poller started for real-time data and signal generation")


@app.on_event("shutdown")
async def on_shutdown() -> None:
    task = getattr(app.state, "live_signal_task", None)
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    
    # Stop MT5 live poller
    from app.workers.mt5_live_poller import stop_mt5_live_poller
    stop_mt5_live_poller()


def _collect_completed_bars() -> int:
    from app.data.providers import get_provider, parse_timeframe_minutes

    symbol = settings.default_symbol.upper()
    timeframe = settings.default_timeframe
    provider = get_provider("twelvedata")
    candles = provider.get_ohlcv(symbol, timeframe, limit=120)
    now = utcnow()
    completed = [
        candle for candle in candles
        if candle["timestamp"] + timedelta(minutes=parse_timeframe_minutes(timeframe)) <= now
    ]
    added = pipeline.persist_candles(completed) if completed else 0
    if added:
        result = pipeline.run_cycle(symbol, timeframe, execute=False)
        logger.info("scheduled signal cycle for %s %s: %s",
                    symbol, timeframe, result.get("decision", {}).get("action", result.get("status")))
    return added


async def _live_signal_loop() -> None:
    interval = max(settings.polling_interval, 15)
    while True:
        try:
            await asyncio.to_thread(_collect_completed_bars)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("live market polling failed; retrying after interval")
        await asyncio.sleep(interval)


app.include_router(router)
app.include_router(settings_router)


@app.get("/")
def root():
    return {
        "service": "Multi-Agent Trading System",
        "docs": "/docs",
        "health": "/health",
        "paper_trading": settings.paper_trading,
        "live_trading": settings.live_trading,
    }

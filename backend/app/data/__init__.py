"""Data layer (Milestones 1-5).

Exports:
- providers: MarketDataProvider implementations (local, stooq, twelvedata)
- cache: FileCache with TTL, namespaces, dedup, stale fallback
- resilience: RateLimitManager, CircuitBreaker, ProviderHealthManager
- schemas: Candle, Quote, validation helpers
- quality: QualityReport, analyze_candles, detect_gaps
- resample: resample_candles, resample_to_all_timeframes
- service: HistoricalDataService, get_historical_service
- tick_import, csv_import: data import utilities
"""
from app.data.providers import (
    MarketDataProvider,
    LocalFileProvider,
    StooqProvider,
    TwelveDataProvider,
    ProviderRegistry,
    get_provider,
    normalize_row,
    parse_timeframe_minutes,
    VALID_TIMEFRAMES,
    build_default_registry,
)

from app.data.cache import FileCache

from app.data.resilience import (
    RateLimitManager,
    rate_limit_manager,
    ProviderHealthManager,
    provider_health,
    resilient_request,
)

from app.data.schemas import Candle, Quote, VALID_TIMEFRAMES, TIMEFRAME_MINUTES, validate_timeframe, spread_pct

from app.data.quality import QualityReport, analyze_candles, detect_gaps, validate_candle_schema

from app.data.resample import resample_candles, resample_to_all_timeframes, TIMEFRAME_MINUTES

from app.data.service import HistoricalDataService, get_historical_service

__all__ = [
    # Providers
    "MarketDataProvider",
    "LocalFileProvider",
    "StooqProvider",
    "TwelveDataProvider",
    "ProviderRegistry",
    "get_provider",
    "normalize_row",
    "parse_timeframe_minutes",
    "VALID_TIMEFRAMES",
    "build_default_registry",
    # Cache
    "FileCache",
    # Resilience
    "RateLimitManager",
    "rate_limit_manager",
    "ProviderHealthManager",
    "provider_health",
    "resilient_request",
    # Schemas
    "Candle",
    "Quote",
    "TIMEFRAME_MINUTES",
    "validate_timeframe",
    "spread_pct",
    # Quality
    "QualityReport",
    "analyze_candles",
    "detect_gaps",
    "validate_candle_schema",
    # Resample
    "resample_candles",
    "resample_to_all_timeframes",
    # Service
    "HistoricalDataService",
    "get_historical_service",
]
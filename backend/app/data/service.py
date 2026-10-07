"""Historical data service (Milestone 1).

Orchestrates the full historical data pipeline:
- Multi-provider fallback chain
- Cache consultation before external requests
- Schema validation
- Data quality analysis
- Timeframe resampling
- Persistence to SQLite
- On-demand resampling from 1m data
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional

from app.data.cache import cache
from app.data.quality import QualityReport, analyze_candles, detect_gaps, validate_candle_schema, QualityThresholds, check_quality_gates, QualityGateError
from app.data.resample import resample_candles, TIMEFRAME_MINUTES
from app.data.providers import get_provider, MarketDataProvider, normalize_row
from app.data.resilience import provider_health
from app.models.database import MarketData, get_session_factory
from app.utils.timeutils import to_utc, utcnow
from app.config import get_settings
import logging

logger = logging.getLogger(__name__)


class HistoricalDataService:
    """Service layer for historical market data with multi-provider support."""

    def __init__(self, provider_names: Optional[list[str]] = None) -> None:
        """Initialize with optional provider fallback chain.

        Args:
            provider_names: Ordered list of provider names to try (fallback chain).
                            If None, uses the chain from settings (MARKET_DATA_PROVIDER_CHAIN).
        """
        settings = get_settings()
        if provider_names is None:
            # Use the chain from settings
            chain_str = settings.market_data_provider_chain
            provider_names = [name.strip() for name in chain_str.split(",") if name.strip()]

        self._providers: list[MarketDataProvider] = []
        self._provider_names: list[str] = provider_names or []
        for name in self._provider_names:
            try:
                self._providers.append(get_provider(name))
            except KeyError:
                pass

    def _get_provider(self, index: int = 0) -> MarketDataProvider:
        """Get the provider at the given index in the fallback chain."""
        if not self._providers:
            return get_provider()
        if index < len(self._providers):
            return self._providers[index]
        return self._providers[-1]

    def _persist_candles(self, candles: list[dict[str, Any]]) -> None:
        """Persist candles to SQLite database."""
        if not candles:
            return
        SessionLocal = get_session_factory()
        db = SessionLocal()
        try:
            for candle in candles:
                # Ensure we have the required fields
                candle_data = {
                    "symbol": candle["symbol"].upper(),
                    "timeframe": candle["timeframe"],
                    "timestamp": candle["timestamp"],
                    "open": candle["open"],
                    "high": candle["high"],
                    "low": candle["low"],
                    "close": candle["close"],
                    "volume": candle.get("volume"),
                    "source": candle.get("source", "unknown"),
                }
                # Add optional fields if present
                if "bid" in candle and candle["bid"] is not None:
                    candle_data["bid"] = candle["bid"]
                if "ask" in candle and candle["ask"] is not None:
                    candle_data["ask"] = candle["ask"]
                if "spread" in candle and candle["spread"] is not None:
                    candle_data["spread"] = candle["spread"]
                
                db.merge(MarketData(**candle_data))
            db.commit()
            logger.debug(f"Persisted {len(candles)} candles to DB")
        except Exception as e:
            db.rollback()
            logger.error(f"Failed to persist market data: {e}")
        finally:
            db.close()

    def _get_from_db(
        self,
        symbol: str,
        timeframe: str,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        limit: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        """Read candles from SQLite database."""
        SessionLocal = get_session_factory()
        db = SessionLocal()
        try:
            query = db.query(MarketData).filter(
                MarketData.symbol == symbol.upper(),
                MarketData.timeframe == timeframe,
            )
            if start and end:
                query = query.filter(MarketData.timestamp >= start, MarketData.timestamp <= end)
            elif start:
                query = query.filter(MarketData.timestamp >= start)
            elif end:
                query = query.filter(MarketData.timestamp <= end)

            query = query.order_by(MarketData.timestamp)

            if limit:
                query = query.limit(limit)

            rows = query.all()
            # Convert to list of dicts
            candles = []
            for row in rows:
                candle = {
                    "timestamp": row.timestamp,
                    "symbol": row.symbol,
                    "timeframe": row.timeframe,
                    "open": row.open,
                    "high": row.high,
                    "low": row.low,
                    "close": row.close,
                    "volume": row.volume,
                    "source": row.source,
                }
                if row.bid is not None:
                    candle["bid"] = row.bid
                if row.ask is not None:
                    candle["ask"] = row.ask
                if row.spread is not None:
                    candle["spread"] = row.spread
                candles.append(candle)
            logger.debug(f"Read {len(candles)} candles from DB for {symbol} {timeframe}")
            return candles
        except Exception as e:
            logger.error(f"Failed to read market data from DB: {e}")
            return []
        finally:
            db.close()

    def get_historical(
        self,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
        validate: bool = True,
        quality_report: bool = False,
    ) -> tuple[list[dict[str, Any]], Optional[QualityReport]]:
        """Fetch historical candles with optional validation and quality report.

        Returns:
            Tuple of (candles, quality_report). quality_report is None if not requested.
        """
        candles = self._fetch_with_fallback(symbol, timeframe, start, end)

        if validate and candles:
            # Remove any candles that fail schema validation
            candles = [c for c in candles if self._is_valid_candle(c)]
             
            # Apply quality gates if enabled
            settings = get_settings()
            if settings.quality_gates_enabled:
                try:
                    thresholds = QualityThresholds.from_settings()
                    expected_minutes = TIMEFRAME_MINUTES.get(timeframe, 15)
                    # Validate quality - this will raise QualityGateError if thresholds not met
                    validate_quality_or_raise(candles, symbol, timeframe, expected_minutes, thresholds)
                except QualityGateError as e:
                    # Report the failure but don't return empty data yet - we'll return what we have
                    # and let higher layers decide what to do
                    provider_health.report(
                        "market_data", 
                        "DEGRADED", 
                        f"quality gate failed for {symbol} {timeframe}: {str(e)}"
                    )
                    # Optionally, we could return empty here, but let's return the data for now
                    # The calling code can check the quality report if requested

        report: Optional[QualityReport] = None
        if quality_report and candles:
            expected_minutes = TIMEFRAME_MINUTES.get(timeframe, 15)
            report = analyze_candles(candles, symbol, timeframe, expected_minutes)

        return candles, report

    def get_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        limit: int = 500,
        resample_from: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Fetch recent candles, optionally resampling from a lower timeframe.

        Args:
            symbol: Ticker symbol
            timeframe: Target timeframe
            limit: Number of candles to return
            resample_from: If provided, fetch from this lower timeframe and resample

        Returns:
            List of normalized candles
        """
        if resample_from is not None:
            source_minutes = TIMEFRAME_MINUTES.get(resample_from, 1)
            target_minutes = TIMEFRAME_MINUTES.get(timeframe, 15)
            # Fetch enough source candles to cover the target limit
            fetch_limit = limit * (target_minutes // source_minutes) + target_minutes
            source_candles = self._fetch_ohlcv_with_fallback(symbol, resample_from, fetch_limit)
            resampled = resample_candles(source_candles, timeframe)
            return resampled[-limit:] if limit else resampled

        # Try cache first
        cache_key = f"ohlcv:{symbol}:{timeframe}:{limit}"
        cached = cache.get(cache_key, "data")
        if cached is not None:
            return self._decode_candles(cached)

        # Check DB
        db_data = self._get_from_db(symbol, timeframe, limit=limit)
        if db_data:
            cache.set(cache_key, self._encode_candles(db_data), "data")
            return db_data

        # Try to resample from 1m data if requested timeframe is not 1m
        if timeframe != "1m":
            # Calculate how many 1m candles we need to resample to get 'limit' candles of target timeframe
            target_minutes = TIMEFRAME_MINUTES.get(timeframe, 15)
            source_limit = limit * target_minutes + target_minutes  # Add extra for alignment
            source_candles = self.get_ohlcv(symbol, "1m", limit=source_limit)
            if source_candles:
                resampled = resample_candles(source_candles, timeframe)
                # Take the last 'limit' candles
                resampled = resampled[-limit:] if limit else resampled
                # Cache the resampled data
                cache.set(cache_key, self._encode_candles(resampled), "data")
                # Also persist to DB for future use
                self._persist_candles(resampled)
                return resampled

        # Try providers in order
        providers = self._providers if self._providers else [get_provider()]
        for i, provider in enumerate(providers):
            try:
                candles = provider.get_ohlcv(symbol, timeframe, limit)
                if candles:
                    cache.set(cache_key, self._encode_candles(candles), "data")
                    # Persist to DB
                    self._persist_candles(candles)
                    return candles
            except Exception as exc:
                logger.warning(f"Provider {provider.name} failed for {symbol} {timeframe}: {exc}")
                continue

        # Offline fallback: stale cache
        stale = cache.get_stale(cache_key, "data")
        if stale:
            return self._decode_candles(stale)

        return []

    def get_latest(self, symbol: str, timeframe: str) -> Optional[dict[str, Any]]:
        """Get the most recent candle."""
        candles = self.get_ohlcv(symbol, timeframe, limit=1)
        return candles[-1] if candles else None

    def get_data_quality(
        self,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
    ) -> QualityReport:
        """Generate a data quality report for a date range."""
        candles, _ = self.get_historical(symbol, timeframe, start, end, validate=False, quality_report=True)
        expected_minutes = TIMEFRAME_MINUTES.get(timeframe, 15)
        return analyze_candles(candles, symbol, timeframe, expected_minutes)

    def get_gaps(
        self,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
        max_gap_candles: int = 3,
    ) -> list[dict[str, Any]]:
        """Detect data gaps in a date range."""
        candles, _ = self.get_historical(symbol, timeframe, start, end, validate=False)
        expected_minutes = TIMEFRAME_MINUTES.get(timeframe, 15)
        return detect_gaps(candles, expected_minutes, max_gap_candles)

    def _fetch_with_fallback(
        self,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
    ) -> list[dict[str, Any]]:
        """Fetch historical data with provider fallback."""
        start, end = to_utc(start), to_utc(end)

        # Check cache first
        cache_key = f"historical:{symbol}:{timeframe}:{start:%Y%m%d%H%M}:{end:%Y%m%d%H%M}"
        cached = cache.get(cache_key, "data")
        if cached is not None:
            return self._decode_candles(cached)

        # Check DB
        db_data = self._get_from_db(symbol, timeframe, start=start, end=end)
        if db_data:
            cache.set(cache_key, self._encode_candles(db_data), "data")
            return db_data

        # Try providers in order
        errors: list[str] = []
        providers = self._providers if self._providers else [get_provider()]
        for i, provider in enumerate(providers):
            try:
                candles = provider.get_historical(symbol, timeframe, start, end)
                if candles:
                    cache.set(cache_key, self._encode_candles(candles), "data")
                    # Persist to DB
                    self._persist_candles(candles)
                    return candles
                errors.append(f"{provider.name}: no data")
            except Exception as exc:
                errors.append(f"{provider.name}: {exc}")
                continue

        # Offline fallback: stale cache
        stale = cache.get_stale(cache_key, "data")
        if stale:
            provider_health.report("market_data", "DEGRADED", "using stale cached data")
            return self._decode_candles(stale)

        if errors:
            provider_health.report("market_data", "ERROR", f"all providers failed: {'; '.join(errors[:3])}")
        return []

    def _fetch_ohlcv_with_fallback(
        self,
        symbol: str,
        timeframe: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Fetch recent candles with provider fallback."""
        # Check cache first
        cache_key = f"ohlcv:{symbol}:{timeframe}:{limit}"
        cached = cache.get(cache_key, "data")
        if cached is not None:
            return self._decode_candles(cached)

        # Check DB
        db_data = self._get_from_db(symbol, timeframe, limit=limit)
        if db_data:
            cache.set(cache_key, self._encode_candles(db_data), "data")
            return db_data

        # Try providers in order
        providers = self._providers if self._providers else [get_provider()]
        for i, provider in enumerate(providers):
            try:
                candles = provider.get_ohlcv(symbol, timeframe, limit)
                if candles:
                    cache.set(cache_key, self._encode_candles(candles), "data")
                    # Persist to DB
                    self._persist_candles(candles)
                    return candles
            except Exception as exc:
                logger.warning(f"Provider {provider.name} failed for {symbol} {timeframe}: {exc}")
                continue

        # Offline fallback: stale cache
        stale = cache.get_stale(cache_key, "data")
        if stale:
            return self._decode_candles(stale)

        return []

    @staticmethod
    def _encode_candles(candles: list[dict[str, Any]]) -> list[list[Any]]:
        """Encode candles for cache storage."""
        return [
            [c["timestamp"].isoformat(), c["open"], c["high"], c["low"], c["close"], c.get("volume")]
            for c in candles
        ]

    @staticmethod
    def _decode_candles(payload: Any) -> list[list[Any]]:
        """Decode candles from cache storage."""
        candles = []
        if not isinstance(payload, list):
            return candles
        for row in payload:
            if not isinstance(row, list) or len(row) < 5:
                continue
            try:
                ts_str = row[0]
                if isinstance(ts_str, str):
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                else:
                    continue
                candles.append({
                    "timestamp": to_utc(ts),
                    "open": float(row[1]),
                    "high": float(row[2]),
                    "low": float(row[3]),
                    "close": float(row[4]),
                    "volume": float(row[5]) if len(row) > 5 and row[5] is not None else None,
                    "source": "cache",
                })
            except (ValueError, TypeError, IndexError):
                continue
        return candles

    @staticmethod
    def _is_valid_candle(c: dict[str, Any]) -> bool:
        """Check if a candle has valid schema."""
        required = ("timestamp", "open", "high", "low", "close")
        for field in required:
            if field not in c or c.get(field) is None:
                return False
        # OHLC consistency
        o, h, l, cl = c["open"], c["high"], c["low"], c["close"]
        return h >= l and h >= o and h >= cl and l <= o and l <= cl


# Singleton service instance
_service: Optional[HistoricalDataService] = None


def get_historical_service(provider_names: Optional[list[str]] = None) -> HistoricalDataService:
    """Get the global historical data service instance."""
    global _service
    if _service is None or (provider_names and _service._provider_names != provider_names):
        _service = HistoricalDataService(provider_names)
    return _service


def validate_quality_or_raise(
    candles: list[dict[str, Any]],
    symbol: str,
    timeframe: str,
    expected_minutes: int,
    thresholds: QualityThresholds,
) -> None:
    """Validate candle data against quality thresholds, raising QualityGateError if thresholds not met."""
    report = analyze_candles(candles, symbol, timeframe, expected_minutes)
    errors = []
    
    if report.completeness_pct < thresholds.min_completeness_pct:
        errors.append(f"completeness {report.completeness_pct:.1f}% < {thresholds.min_completeness_pct}%")
    
    if report.ohlc_violations_pct > thresholds.max_ohlc_violations_pct:
        errors.append(f"OHLC violations {report.ohlc_violations_pct:.1f}% > {thresholds.max_ohlc_violations_pct}%")
    
    if report.anomaly_pct > thresholds.max_anomaly_pct:
        errors.append(f"anomalies {report.anomaly_pct:.1f}% > {thresholds.max_anomaly_pct}%")
    
    if report.max_price_jump_pct > thresholds.max_price_jump_pct:
        errors.append(f"max price jump {report.max_price_jump_pct:.1f}% > {thresholds.max_price_jump_pct}%")
    
    if len(candles) < thresholds.min_candles_required:
        errors.append(f"candles {len(candles)} < {thresholds.min_candles_required}")
    
    if errors:
        raise QualityGateError(f"Quality gates failed for {symbol} {timeframe}: {'; '.join(errors)}")
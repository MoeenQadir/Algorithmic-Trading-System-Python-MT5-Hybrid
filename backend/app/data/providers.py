"""Market data engine (Milestone 3).

- MarketDataProvider: replaceable abstraction (get_quote / get_ohlcv /
  get_historical / get_latest).
- normalize_ohlcv: every row is normalized to the standard schema. Missing
  fields are None. Data is never fabricated.
- LocalFileProvider: reads CSV / Parquet / JSON from data/raw.
- StooqProvider: free daily HTTP provider (https://stooq.com) with caching,
  used only when configured (MARKET_DATA_PROVIDER=stooq).
"""
from __future__ import annotations

import csv
import json
import logging
import os
import threading
import time
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx

from app.data.cache import cache
from app.data.resilience import (
    ProviderError,
    ProviderRateLimited,
    RateLimitWindow,
    provider_health,
    rate_limit_manager,
    resilient_request,
)
from app.utils.timeutils import to_utc, utcnow

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
RAW_DIR = os.path.join(ROOT, "data", "raw")

STANDARD_COLUMNS = ["timestamp", "symbol", "timeframe", "open", "high", "low", "close", "volume", "bid", "ask", "spread"]

VALID_TIMEFRAMES = {"1m", "5m", "15m", "30m", "1h", "4h", "1d"}


def parse_timeframe_minutes(timeframe: str) -> int:
    tf = timeframe.strip().lower()
    table = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}
    if tf not in table:
        raise ValueError(f"unsupported timeframe '{timeframe}'. valid: {sorted(table)}")
    return table[tf]


def normalize_row(symbol: str, timeframe: str, row: dict[str, Any]) -> dict[str, Any]:
    """Coerce an arbitrary candle row into the standard schema. No fabrication."""

    def _num(value: Any) -> Optional[float]:
        if value is None or value == "":
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    ts_raw = row.get("timestamp") or row.get("time") or row.get("date") or row.get("datetime")
    if isinstance(ts_raw, datetime):
        ts = to_utc(ts_raw)
    elif isinstance(ts_raw, (int, float)) and ts_raw and ts_raw > 1_000_000_000:
        seconds = float(ts_raw) / 1000.0 if ts_raw > 1e11 else float(ts_raw)
        ts = to_utc(datetime.fromtimestamp(seconds, tz=timezone.utc))
    else:
        ts = None
        if isinstance(ts_raw, str):
            text = ts_raw.strip().replace("Z", "+00:00")
            try:
                ts = to_utc(datetime.fromisoformat(text))
            except ValueError:
                pass
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y.%m.%d %H:%M:%S", "%Y.%m.%d %H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y") if ts is None else ():
                try:
                    ts = to_utc(datetime.strptime(text, fmt))
                    break
                except ValueError:
                    continue

    o, h, l, c = (_num(row.get(k)) for k in ("open", "high", "low", "close"))
    vol = _num(row.get("volume") if row.get("volume") not in ("", "0x") else None)
    bid, ask = _num(row.get("bid")), _num(row.get("ask"))
    spread = _num(row.get("spread"))
    if spread is None and bid is not None and ask is not None:
        spread = round(ask - bid, 10)
    if ts is None or o is None or h is None or l is None or c is None:
        raise ValueError(f"row missing required timestamp/open/high/low/close: {row!r}")
    if not (h >= l and h >= o and h >= c and l <= o and l <= c):
        raise ValueError(f"OHLC inconsistent in row: {row!r}")
    return {
        "timestamp": ts,
        "symbol": symbol.upper(),
        "timeframe": timeframe,
        "open": o,
        "high": h,
        "low": l,
        "close": c,
        "volume": vol,
        "bid": bid,
        "ask": ask,
        "spread": spread,
    }


class MarketDataProvider(ABC):
    """Replaceable market-data abstraction."""

    name: str = "abstract"

    @abstractmethod
    def get_quote(self, symbol: str) -> dict[str, Any] | None:
        """Latest quote (bid/ask/last). None when unavailable."""

    @abstractmethod
    def get_ohlcv(self, symbol: str, timeframe: str, limit: int = 500) -> list[dict[str, Any]]:
        """Recent candles."""

    @abstractmethod
    def get_historical(self, symbol: str, timeframe: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
        """Historical candles in [start, end]."""

    def get_latest(self, symbol: str, timeframe: str) -> dict[str, Any] | None:
        candles = self.get_ohlcv(symbol, timeframe, limit=1)
        return candles[-1] if candles else None

    def get_supported_timeframes(self) -> list[str]:
        """Get list of timeframes supported by this provider.
        
        Returns:
            List of timeframe strings (e.g., ['1m', '5m', '15m', '1h', '4h', '1d'])
        """
        # Default implementation - override in subclasses
        return ["1m", "5m", "15m", "30m", "1h", "4h", "1d"]


class LocalFileProvider(MarketDataProvider):
    """Reads candles from data/raw/{SYMBOL}_{timeframe}.csv|parquet|json.

    Expected columns: timestamp, open, high, low, close, [volume, bid, ask].
    This provider is fully offline.
    """

    name = "local"

    def _files(self, symbol: str, timeframe: str) -> list[str]:
        base = os.path.join(RAW_DIR, f"{symbol.upper()}_{timeframe}")
        return [f"{base}.csv", f"{base}.parquet", f"{base}.json"]

    def get_ohlcv(self, symbol: str, timeframe: str, limit: int = 500) -> list[dict[str, Any]]:
        candles = self._load(symbol, timeframe)
        return candles[-limit:] if limit else candles

    def get_historical(self, symbol: str, timeframe: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
        start, end = to_utc(start), to_utc(end)
        return [c for c in self._load(symbol, timeframe) if start <= c["timestamp"] <= end]

    def get_quote(self, symbol: str) -> dict[str, Any] | None:
        for tf in ("15m", "1h", "1d", "5m", "1m"):
            candles = self._load(symbol, tf)
            if candles:
                last = candles[-1]
                return {
                    "symbol": symbol.upper(),
                    "bid": last.get("bid"),
                    "ask": last.get("ask"),
                    "last": last["close"],
                    "spread": last.get("spread"),
                    "timestamp": last["timestamp"].isoformat(),
                    "source": self.name,
                }
        return None

    def _load(self, symbol: str, timeframe: str) -> list[dict[str, Any]]:
        for path in self._files(symbol, timeframe):
            if os.path.exists(path):
                rows = self._read_file(path)
                return [normalize_row(symbol, timeframe, r) for r in rows]
        return []

    @staticmethod
    def _read_file(path: str) -> list[dict[str, Any]]:
        ext = os.path.splitext(path)[1].lower()
        if ext == ".csv":
            with open(path, "r", encoding="utf-8-sig", newline="") as fh:
                return list(csv.DictReader(fh))
        if ext == ".json":
            with open(path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
            return payload if isinstance(payload, list) else payload.get("candles", [])
        if ext == ".parquet":
            import polars as pl

            return pl.read_parquet(path).to_dicts()
        raise ValueError(f"unsupported data file type: {path}")


class StooqProvider(MarketDataProvider):
    """Free daily-data provider (stooq.com). Daily timeframe only ('1d').

    All requests go through the cache + rate-limit protection layer.
    """

    name = "stooq"
    BASE = "https://stooq.com/q/d/l/"

    def get_ohlcv(self, symbol: str, timeframe: str, limit: int = 500) -> list[dict[str, Any]]:
        if timeframe != "1d":
            return []  # stooq free endpoint serves daily bars only; no fabrication
        end = utcnow()
        start = end - timedelta(days=max(limit, 30) * 2)
        return self.get_historical(symbol, timeframe, start, end)[-limit:]

    def get_historical(self, symbol: str, timeframe: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
        if timeframe != "1d":
            return []
        key = f"stooq:{symbol}:{start:%Y%m%d}:{end:%Y%m%d}"
        cached = cache.get(key, "data")
        if cached is not None:
            return self._decode_cached(symbol, cached)
        try:
            return self._fetch(symbol, start, end, key)
        except Exception as exc:  # offline fallback: stale cache
            stale = cache.get_stale(key, "data")
            provider_health.report("market_data", "DEGRADED", f"stooq unavailable: {exc}; using cached data if any")
            return self._decode_cached(symbol, stale or [])

    @staticmethod
    def _decode_cached(symbol: str, payload: Any) -> list[dict[str, Any]]:
        candles = []
        for row in payload if isinstance(payload, list) else []:
            try:
                if isinstance(row, dict):
                    candles.append(normalize_row(symbol, "1d", row))
                elif isinstance(row, list) and len(row) >= 5:
                    candles.append(normalize_row(symbol, "1d", {
                        "timestamp": row[0], "open": row[1], "high": row[2],
                        "low": row[3], "close": row[4],
                        "volume": row[5] if len(row) > 5 else None,
                    }))
            except (TypeError, ValueError):
                continue
        return candles

    def _fetch(self, symbol: str, start: datetime, end: datetime, key: str) -> list[dict[str, Any]]:
        def do_get(**kw: Any) -> httpx.Response:
            return httpx.get(self.BASE, params={"s": symbol, "d1": start.strftime("%Y%m%d"), "d2": end.strftime("%Y%m%d"), "i": "d"}, follow_redirects=True, **kw)

        resp = resilient_request(do_get, "stooq")
        text = resp.text.strip()
        if text.lower().startswith("no data"):
            raise ValueError(f"stooq has no data for {symbol}")
        rows = list(csv.DictReader(text.splitlines()))
        candles = [normalize_row(symbol, "1d", r) for r in rows]
        cache.set(key, [[c["timestamp"].isoformat(), c["open"], c["high"], c["low"], c["close"], c["volume"]] for c in candles], "data")
        provider_health.report("market_data", "OK", f"stooq returned {len(candles)} daily candles for {symbol}")
        return candles

    def get_quote(self, symbol: str) -> dict[str, Any] | None:
        candles = self.get_ohlcv(symbol, "1d", limit=1)
        if not candles:
            return None
        last = candles[-1]
        return {"symbol": symbol.upper(), "bid": None, "ask": None, "last": last["close"], "spread": None,
                "timestamp": last["timestamp"].isoformat(), "source": self.name}

    def get_supported_timeframes(self) -> list[str]:
        """Stooq only supports daily timeframe."""
        return ["1d"]


class TwelveDataProvider(MarketDataProvider):
    """Intraday OHLC from Twelve Data with a credit-aware rate-limit model.

    Phase 35 hardening:
    - RateLimitWindow: minute + optional daily credit windows with per-endpoint
      weighted costs, pre-flight checks and post-flight synchronization.
    - Synchronization from documented response headers (api-credits-used /
      api-credits-left, case-insensitive); header data always overrides the
      local estimate.
    - Optional /api_usage synchronization (explicit, cached, never polled).
    - Structured machine-readable error categories.
    - Local remaining credits never exceed provider-reported remaining credits.
    """

    name = "twelvedata"
    BASE = "https://api.twelvedata.com"
    INTERVALS = {
        "1m": "1min", "5m": "5min", "15m": "15min", "30m": "30min",
        "1h": "1h", "2h": "2h", "4h": "4h", "1d": "1day",
    }

    # Error categories (machine-readable)
    ERROR_CATEGORIES = (
        "auth", "rate_limit", "invalid_symbol", "invalid_request", "not_entitled",
        "server", "network", "timeout", "malformed_response", "unknown",
    )

    # Map vendor error codes/messages -> (category, machine_code)
    _AUTH_CODES = {"401", "403", "404"}
    _AUTH_MESSAGES = ("invalid api key", "api key", "unauthorized", "not authorized")

    def __init__(
        self,
        api_key: str | None = None,
        rpm: int | None = None,
        rpd: int | None = None,
        endpoint_costs: dict[str, int] | None = None,
    ) -> None:
        from app.config import get_settings

        s = get_settings()
        if api_key is None:
            api_key = s.twelve_data_api_key
        self.api_key = api_key.strip() if api_key else ""
        self._client: httpx.Client | None = None
        self._client_lock = threading.Lock()
        self._logger = logging.getLogger("trading.providers.twelvedata")

        # --- Phase 35: credit-aware rate limit window ---
        minute_limit = rpm if rpm is not None else s.twelvedata_minute_limit
        daily_limit = rpd if rpd is not None else s.twelvedata_daily_limit
        costs = endpoint_costs or {
            "default": s.twelvedata_default_credit_cost,
            "time_series": s.twelvedata_time_series_credit_cost,
            "quote": s.twelvedata_quote_credit_cost,
            "symbol_search": s.twelvedata_symbol_search_credit_cost,
            "api_usage": s.twelvedata_api_usage_credit_cost,
        }
        self.rate_limit_window = RateLimitWindow(
            name=self.name,
            minute_limit=minute_limit,
            daily_limit=daily_limit,
            default_cost=costs.get("default", 1),
            endpoint_costs={k: v for k, v in costs.items() if k != "default"},
        )
        self._default_cost = costs.get("default", 1)

        # /api_usage sync state (explicit + cached, never polled automatically)
        self._api_usage_endpoint = s.twelvedata_api_usage_endpoint or "/api_usage"
        self._api_usage_cache_seconds = max(1, s.twelvedata_api_usage_cache_seconds)
        self._api_usage_max_interval = max(self._api_usage_cache_seconds, s.twelvedata_api_usage_max_interval_sec)
        self._api_usage_cache: dict[str, Any] | None = None
        self._api_usage_cache_at: float = 0.0
        self._last_api_usage_attempt: float = 0.0

        # Last known provider error (machine-readable)
        self.last_error: dict[str, Any] | None = None

    # ------------------------------------------------------------------ config
    @classmethod
    def from_settings(cls) -> "TwelveDataProvider":
        """Build a provider from config (explicit construction path)."""
        return cls()

    def set_rate_limits(self, rpm: int | None = None, rpd: int | None = None) -> None:
        """Update rate limit configuration at runtime."""
        if rpm is not None:
            self.rate_limit_window.minute_limit = rpm
        if rpd is not None:
            self.rate_limit_window.daily_limit = rpd
        self._logger.info("Twelve Data rate limits updated: rpm=%s, rpd=%s", rpm, rpd)

    def set_endpoint_cost(self, endpoint: str, cost: int) -> None:
        """Update the credit cost for a specific endpoint."""
        self.rate_limit_window.set_endpoint_cost(endpoint, cost)

    # ------------------------------------------------------------------ client
    def _get_client(self) -> httpx.Client:
        """Get or create persistent HTTP client with connection pooling."""
        if self._client is None:
            with self._client_lock:
                if self._client is None:
                    self._client = httpx.Client(
                        timeout=httpx.Timeout(connect=5.0, read=20.0, write=10.0, pool=5.0),
                        limits=httpx.Limits(max_keepalive_connections=5, max_connections=10),
                        follow_redirects=True,
                    )
        return self._client

    def close(self) -> None:
        """Close the persistent HTTP client."""
        with self._client_lock:
            if self._client is not None:
                self._client.close()
                self._client = None

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    # ------------------------------------------------------------------ errors
    def _categorize_error(self, payload: dict[str, Any]) -> tuple[str, str, str]:
        """Map a Twelve Data error payload to (category, machine_code, message)."""
        code = str(payload.get("code", "unknown"))
        message = str(payload.get("message", "Unknown error"))
        msg_lower = message.lower()

        if code in self._AUTH_CODES or any(hint in msg_lower for hint in self._AUTH_MESSAGES):
            return "auth", "TD_AUTH_FAILED", message
        if "rate limit" in msg_lower or "too many requests" in msg_lower or "run out of credits" in msg_lower:
            return "rate_limit", "TD_RATE_LIMITED", message
        if "not entitled" in msg_lower or "plan does not" in msg_lower or "upgrade" in msg_lower:
            return "not_entitled", "TD_NOT_ENTITLED", message
        if "invalid symbol" in msg_lower or "symbol_not_found" in code or "unknown symbol" in msg_lower:
            return "invalid_symbol", "TD_INVALID_SYMBOL", message
        if "invalid" in msg_lower or "required parameter" in msg_lower:
            return "invalid_request", "TD_INVALID_REQUEST", message
        return "unknown", "TD_ERROR_" + code, message

    def _handle_error(self, payload: dict[str, Any], context: str) -> Exception:
        """Categorize Twelve Data errors into structured ProviderError exceptions."""
        category, machine_code, message = self._categorize_error(payload)
        self.last_error = {
            "category": category,
            "code": machine_code,
            "message": message,
            "endpoint": context,
        }
        if category == "rate_limit":
            self._logger.warning("Twelve Data rate limited: %s", message)
            provider_health.report("market_data", "DEGRADED", "Twelve Data rate limited: " + message)
            exc: Exception = ProviderError(
                "Twelve Data rate limit exceeded: " + message,
                category="rate_limit", code=machine_code, provider=self.name,
            )
        elif category == "auth":
            self._logger.error("Twelve Data auth error: %s", message)
            provider_health.report("market_data", "ERROR", "Twelve Data auth failure: " + message)
            exc = ProviderError(
                "Twelve Data authentication failed: " + message,
                category="auth", code=machine_code, provider=self.name,
            )
        elif category == "invalid_symbol":
            self._logger.warning("Twelve Data invalid symbol: %s", message)
            exc = ProviderError(
                "Twelve Data symbol not found: " + message,
                category="invalid_symbol", code=machine_code, provider=self.name,
            )
        elif category == "not_entitled":
            self._logger.warning("Twelve Data not entitled: %s", message)
            provider_health.report("market_data", "DEGRADED", "Twelve Data not entitled: " + message)
            exc = ProviderError(
                "Twelve Data not entitled: " + message,
                category="not_entitled", code=machine_code, provider=self.name,
            )
        else:
            self._logger.error("Twelve Data error (%s): %s", machine_code, message)
            exc = ProviderError(
                "Twelve Data " + context + " failed: " + message + " (code: " + machine_code + ")",
                category=category, code=machine_code, provider=self.name,
            )
        return exc

    # ------------------------------------------------------------------ symbols
    @staticmethod
    def _vendor_symbol(symbol: str) -> str:
        normalized = symbol.strip().upper()
        mapping = {
            "XAUUSD": "XAU/USD",
            "BTCUSD": "BTC/USD",
            "ETHUSD": "ETH/USD",
            "USOIL": "USOIL",
            "UKOIL": "UKOIL",
            "XAGUSD": "XAG/USD",
            "XPTUSD": "XPT/USD",
            "XPDUSD": "XPD/USD",
        }
        return mapping.get(normalized, normalized)

    def _validate_symbol(self, symbol: str) -> str:
        """Validate and convert symbol to Twelve Data format."""
        vendor = self._vendor_symbol(symbol)
        if not vendor or len(vendor) < 3:
            raise ProviderError(
                "Invalid symbol for Twelve Data: " + symbol,
                category="invalid_request", code="TD_INVALID_SYMBOL_FORMAT", provider=self.name,
            )
        return vendor

    # ------------------------------------------------------- request lifecycle
    def _pre_flight(self, endpoint: str) -> None:
        """Estimate cost, check local credit window, block if RATE_LIMITED.

        Raises:
            ProviderRateLimited: local credits insufficient or cooldown active.
        """
        window = self.rate_limit_window
        cost = window.get_cost(endpoint)
        if not window.check_available(cost):
            reason = window.insufficient_reason(cost)
            self._logger.warning("Twelve Data pre-flight blocked (%s): %s", endpoint, reason)
            raise ProviderRateLimited(
                "local rate limit block: " + reason,
                provider=self.name, retry_after=window.wait_time(),
            )

    def _post_flight(self, response: httpx.Response, endpoint: str) -> None:
        """Synchronize credit state from response headers (authoritative)."""
        window = self.rate_limit_window
        if response.status_code == 429:
            retry_after = window.parse_headers(response.headers).get("retry_after")
            wait = window.on_rate_limited(retry_after)
            self._logger.warning("Twelve Data 429; cooldown %.1fs", wait)
            provider_health.report("market_data", "DEGRADED", "Twelve Data 429; cooldown " + str(int(wait)) + "s")
            return
        if response.status_code != 200:
            return
        try:
            window.sync_from_headers(response.headers)
        except Exception as exc:  # header sync must never break a good response
            self._logger.debug("header sync failed: %s", exc)
            window.mark_request_consumed(window.get_cost(endpoint))
        # Phase 35: add provider credit synchronization after every Twelve Data request
        self._sync_credits(response.headers)

    def _sync_credits(self, headers: httpx.Headers) -> None:
        """Synchronize credit state from response headers."""
        window = self.rate_limit_window
        try:
            window.sync_from_headers(headers)
        except Exception as exc:
            self._logger.debug("credit sync failed: %s", exc)

    def _fetch_api_usage_payload(self, force: bool = False) -> dict[str, Any] | None:
        """Fetch /api_usage with caching. Never called automatically per request."""
        if not self.api_key:
            return None
        now = time.time()
        if not force and self._api_usage_cache is not None:
            if now - self._api_usage_cache_at < self._api_usage_cache_seconds:
                return self._api_usage_cache
        # Hard floor between attempts even when forced repeatedly.
        if now - self._last_api_usage_attempt < self._api_usage_max_interval:
            return self._api_usage_cache
        self._last_api_usage_attempt = now

        window = self.rate_limit_window
        cost = window.get_cost("api_usage")
        if not window.check_available(cost):
            self._logger.warning("Twelve Data /api_usage skipped: insufficient credits")
            return self._api_usage_cache

        try:
            client = self._get_client()
            resp = client.get(
                self.BASE + self._api_usage_endpoint,
                params={"apikey": self.api_key},
                timeout=10.0,
            )
            if resp.status_code == 429:
                retry_after = window.parse_headers(resp.headers).get("retry_after")
                window.on_rate_limited(retry_after)
                return self._api_usage_cache
            if resp.status_code != 200:
                self._logger.debug("Twelve Data api_usage returned %d", resp.status_code)
                return self._api_usage_cache
            window.sync_from_headers(resp.headers)
            data = resp.json()
            if isinstance(data, dict) and data.get("status") == "error":
                self.last_error = {
                    "category": "unknown",
                    "code": "TD_ERROR_" + str(data.get("code")),
                    "message": data.get("message"),
                    "endpoint": "api_usage",
                }
                return self._api_usage_cache
            self._api_usage_cache = data if isinstance(data, dict) else None
            self._api_usage_cache_at = time.time()
            if isinstance(data, dict):
                window.sync_from_usage_payload(data)
            return self._api_usage_cache
        except Exception as exc:
            self._logger.debug("Failed to fetch account usage: %s", exc)
            return self._api_usage_cache

    async def get_api_usage(self, force: bool = False) -> dict[str, Any] | None:
        """Explicit /api_usage query (Phase 35.3). Cached; consumes 1 credit.

        Parses only fields the API actually returns; unknown fields -> None.
        Never called automatically on market-data requests.
        """
        payload = self._fetch_api_usage_payload(force=force)
        if not isinstance(payload, dict):
            return None

        def _num(value: Any) -> float | None:
            try:
                f = float(value)
                return f if f == f else None  # filter NaN
            except (TypeError, ValueError):
                return None

        day = payload.get("current_day") if isinstance(payload.get("current_day"), dict) else {}
        month = payload.get("current_month") if isinstance(payload.get("current_month"), dict) else {}

        day_reset = day.get("timestamp_unix")
        month_reset = month.get("timestamp_unix")
        return {
            "endpoint": self._api_usage_endpoint,
            "cached": (time.time() - self._api_usage_cache_at) < self._api_usage_cache_seconds and self._api_usage_cache_at > 0,
            "cached_at": datetime.fromtimestamp(self._api_usage_cache_at, tz=timezone.utc).isoformat() if self._api_usage_cache_at else None,
            "current_day": {
                "used": _num(day.get("current_value")),
                "remaining": _num(day.get("credits_left")),
                "limit": _num(day.get("total_value")),
                "reset_at": datetime.fromtimestamp(float(day_reset), tz=timezone.utc).isoformat() if isinstance(day_reset, (int, float)) else None,
            },
            "current_month": {
                "used": _num(month.get("current_value")),
                "remaining": _num(month.get("credits_left")),
                "limit": _num(month.get("total_value")),
                "reset_at": datetime.fromtimestamp(float(month_reset), tz=timezone.utc).isoformat() if isinstance(month_reset, (int, float)) else None,
            },
        }

    def get_api_usage_sync(self, force: bool = False) -> dict[str, Any] | None:
        """Synchronous variant of get_api_usage for scripts/tests."""
        import asyncio

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop and loop.is_running():
            raise RuntimeError("use 'await get_api_usage()' inside a running event loop")
        return asyncio.get_event_loop().run_until_complete(self.get_api_usage(force=force))

    def get_rate_limit_status(self) -> dict[str, Any]:
        """Structured rate-limit status (Phase 35.4).

        Only includes fields actually known. status=UNKNOWN until the provider
        has reported real credit state.
        """
        status = self.rate_limit_window.get_status()
        status["api_usage_cached"] = self._api_usage_cache is not None
        status["api_usage_endpoint"] = self._api_usage_endpoint
        return status

    # ------------------------------------------------------------------ data
    def _fetch(self, symbol: str, timeframe: str, limit: int,
               start: datetime | None = None, end: datetime | None = None) -> list[dict[str, Any]]:
        if not self.api_key:
            raise RuntimeError("TWELVE_DATA_API_KEY is required for the Twelve Data provider")
        interval = self.INTERVALS.get(timeframe)
        if interval is None:
            raise ProviderError(
                "Twelve Data does not support timeframe '" + timeframe + "'",
                category="invalid_request", code="TD_UNSUPPORTED_TIMEFRAME", provider=self.name,
            )

        vendor_symbol = self._validate_symbol(symbol)

        params: dict[str, Any] = {
            "symbol": vendor_symbol,
            "interval": interval,
            "outputsize": min(max(limit, 1), 5000),
            "order": "asc",
            "timezone": "UTC",
        }
        if start is not None:
            params["start_date"] = to_utc(start).strftime("%Y-%m-%d %H:%M:%S")
        if end is not None:
            params["end_date"] = to_utc(end).strftime("%Y-%m-%d %H:%M:%S")

        # Phase 35.5: pre-flight local credit check -> post-flight header sync
        self._pre_flight("time_series")
        window = self.rate_limit_window
        window.mark_request_consumed(window.get_cost("time_series"))

        def do_get(**kw: Any) -> httpx.Response:
            client = self._get_client()
            return client.get(
                self.BASE + "/time_series",
                params=params,
                headers={"Authorization": "apikey " + self.api_key},
                **kw,
            )

        response = resilient_request(do_get, self.name)
        self._post_flight(response, "time_series")

        try:
            payload = response.json()
        except ValueError as exc:
            self.last_error = {"category": "malformed_response", "code": "TD_MALFORMED_JSON", "message": str(exc), "endpoint": "time_series"}
            raise ProviderError(
                "Twelve Data returned malformed JSON: " + str(exc),
                category="malformed_response", code="TD_MALFORMED_JSON", provider=self.name,
            ) from exc

        if payload.get("status") == "error" or ("code" in payload and "values" not in payload):
            error = self._handle_error(payload, "time_series")
            if isinstance(error, ProviderError) and error.category == "rate_limit":
                self.rate_limit_window.on_rate_limited()
            raise error

        values = payload.get("values")
        if not isinstance(values, list):
            self.last_error = {"category": "malformed_response", "code": "TD_NO_VALUES", "message": "no candle values", "endpoint": "time_series"}
            raise ProviderError(
                "Twelve Data response did not contain candle values",
                category="malformed_response", code="TD_NO_VALUES", provider=self.name,
            )

        candles = []
        for row in values:
            try:
                candle = normalize_row(symbol, timeframe, row)
                candle["source"] = self.name
                candles.append(candle)
            except (TypeError, ValueError) as exc:
                self._logger.warning("Skipping malformed candle row: %s", exc)
                continue

        self._logger.info("Twelve Data returned %d candles for %s %s", len(candles), symbol, timeframe)
        provider_health.report("market_data", "OK", "twelvedata returned " + str(len(candles)) + " candles for " + symbol + " " + timeframe)
        return candles

    def get_ohlcv(self, symbol: str, timeframe: str, limit: int = 500) -> list[dict[str, Any]]:
        return self._fetch(symbol, timeframe, limit)[-limit:]

    def get_historical(self, symbol: str, timeframe: str, start: datetime,
                       end: datetime) -> list[dict[str, Any]]:
        return self._fetch(symbol, timeframe, 5000, start, end)

    def get_quote(self, symbol: str) -> dict[str, Any] | None:
        """Get real-time quote for a symbol."""
        if not self.api_key:
            return None

        vendor_symbol = self._validate_symbol(symbol)

        def do_get(**kw: Any) -> httpx.Response:
            client = self._get_client()
            return client.get(
                self.BASE + "/quote",
                params={"symbol": vendor_symbol, "apikey": self.api_key},
                **kw,
            )

        try:
            self._pre_flight("quote")
            window = self.rate_limit_window
            window.mark_request_consumed(window.get_cost("quote"))
            response = resilient_request(do_get, self.name)
            self._post_flight(response, "quote")
            payload = response.json()

            if payload.get("status") == "error" or "code" in payload:
                self._logger.warning("Twelve Data quote error: %s", payload.get("message", "unknown"))
                return None

            ts_raw = payload.get("timestamp")
            ts = None
            if isinstance(ts_raw, str):
                try:
                    ts = to_utc(datetime.fromisoformat(ts_raw.replace("Z", "+00:00")))
                except ValueError:
                    ts = None
            return {
                "symbol": symbol.upper(),
                "bid": payload.get("bid"),
                "ask": payload.get("ask"),
                "last": payload.get("close") or payload.get("price"),
                "spread": payload.get("spread"),
                "timestamp": ts.isoformat() if ts else utcnow().isoformat(),
                "source": self.name,
            }
        except ProviderRateLimited:
            raise
        except Exception as exc:
            self._logger.error("Failed to get quote for %s: %s", symbol, exc)
            return None

    def health_check(self) -> dict[str, Any]:
        """Health check for Twelve Data provider."""
        if not self.api_key:
            return {"status": "ERROR", "message": "TWELVE_DATA_API_KEY not configured", "provider": self.name, "healthy": False}

        try:
            start = time.time()
            candles = self.get_ohlcv("AAPL", "1d", limit=1)
            latency = int((time.time() - start) * 1000)
            if candles:
                return {"status": "OK", "message": "Health check passed", "provider": self.name, "latency_ms": latency, "healthy": True}
            return {"status": "DEGRADED", "message": "No data returned", "provider": self.name, "latency_ms": latency, "healthy": False}
        except Exception as exc:
            return {"status": "ERROR", "message": str(exc), "provider": self.name, "healthy": False}

    def get_supported_timeframes(self) -> list[str]:
        """Return list of supported timeframes for this provider."""
        return list(self.INTERVALS.keys())

    # ------------------------------------------------------------------ Phase 36
    def check_capability(self, symbol: str, timeframe: str = "1d") -> dict[str, Any]:
        """Explicit capability discovery (Phase 36).

        Verifies symbol recognition, endpoint availability, entitlement and
        timeframe support with a minimal real request. Never assumes a symbol
        is usable merely because it appears in a catalog.
        """
        if not self.api_key:
            return {
                "provider": self.name, "symbol": symbol, "timeframe": timeframe,
                "available": False, "reason": "CONFIG_REQUIRED: TWELVE_DATA_API_KEY missing",
            }
        if timeframe not in self.INTERVALS:
            return {
                "provider": self.name, "symbol": symbol, "timeframe": timeframe,
                "available": False, "reason": "TIMEFRAME_NOT_SUPPORTED",
            }
        try:
            candles = self.get_ohlcv(symbol, timeframe, limit=1)
            if candles:
                return {
                    "provider": self.name, "symbol": symbol, "timeframe": timeframe,
                    "available": True, "reason": "OK",
                }
            return {
                "provider": self.name, "symbol": symbol, "timeframe": timeframe,
                "available": False, "reason": "NO_DATA_AVAILABLE",
            }
        except ProviderError as exc:
            reason = {
                "auth": "AUTH_FAILED",
                "rate_limit": "RATE_LIMITED",
                "not_entitled": "NOT_ENTITLED",
                "invalid_symbol": "SYMBOL_NOT_RECOGNIZED",
            }.get(exc.category, "ERROR: " + exc.category)
            return {
                "provider": self.name, "symbol": symbol, "timeframe": timeframe,
                "available": False, "reason": reason,
            }
        except Exception as exc:
            return {
                "provider": self.name, "symbol": symbol, "timeframe": timeframe,
                "available": False, "reason": "ERROR: " + str(exc),
            }

    # Backwards-compatible alias (Phase 31-34 API)
    def check_symbol_entitlement(self, symbol: str, timeframe: str = "1d") -> dict[str, Any]:
        return self.check_capability(symbol, timeframe)

class ProviderRegistry:
    """Thread-safe registry of providers with a default provider."""

    def __init__(self) -> None:
        self._providers: dict[str, MarketDataProvider] = {}
        self._default: str | None = None
        self._lock = threading.Lock()

    def register(self, provider: MarketDataProvider, default: bool = False) -> None:
        with self._lock:
            self._providers[provider.name] = provider
            if default or self._default is None:
                self._default = provider.name

    def get(self, name: str | None = None) -> MarketDataProvider:
        with self._lock:
            name = name or self._default
            if name is None or name not in self._providers:
                raise KeyError(f"no market data provider named '{name}'")
            return self._providers[name]


def build_default_registry() -> ProviderRegistry:
      from app.config import get_settings
      from .mt5_provider import MT5DataProvider

      registry = ProviderRegistry()
      registry.register(LocalFileProvider())
      registry.register(MT5DataProvider())  # MT5 provider (Polars-based)
      registry.register(StooqProvider())
      registry.register(TwelveDataProvider())
      preferred = get_settings().market_data_provider.lower()
      try:
          registry.get(preferred)
      except KeyError:
          preferred = "local"
      registry._default = preferred
      return registry


_registry: ProviderRegistry | None = None
_registry_lock = threading.Lock()


def get_provider(name: str | None = None) -> MarketDataProvider:
    global _registry
    with _registry_lock:
        if _registry is None:
            _registry = build_default_registry()
    return _registry.get(name)

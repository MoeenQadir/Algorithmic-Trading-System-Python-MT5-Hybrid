"""Phase 35 tests: Twelve Data credit model.

Covers:
- header parsing (documented api-credits-used / api-credits-left)
- header case variants
- used credits / remaining credits accounting
- minute window reset
- weighted requests (endpoint-specific costs)
- insufficient local credits blocking
- /api_usage parsing + caching
- 429 handling with Retry-After and exponential default
- network failure / malformed response / auth failure / invalid symbol
- stale credit state indicator
"""
from __future__ import annotations

import time
from typing import Any

import pytest

from app.data.resilience import (
    ProviderError,
    ProviderRateLimited,
    RateLimitWindow,
)
from app.data.providers import TwelveDataProvider


# ------------------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------------------
class FakeHeaders:
    """Case-insensitive multi-value header container (like httpx.Headers)."""

    def __init__(self, data: dict[str, str]) -> None:
        self._data = {k.lower(): v for k, v in data.items()}

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key.lower(), default)

    def items(self):
        return self._data.items()


class FakeResponse:
    def __init__(self, status_code: int = 200, headers: dict | None = None, payload: Any = None) -> None:
        self.status_code = status_code
        self.headers = FakeHeaders(headers or {})
        self._payload = payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")

    def json(self) -> Any:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def make_provider(rpm: int = 8, rpd: int = 800, monkeypatch=None) -> TwelveDataProvider:
    """Build a provider without touching global settings caches."""
    provider = TwelveDataProvider.__new__(TwelveDataProvider)
    provider.api_key = "test-key"
    import httpx
    import logging
    import threading

    provider._client = None
    provider._client_lock = threading.Lock()
    provider._logger = logging.getLogger("test.twelvedata")
    provider.rate_limit_window = RateLimitWindow(
        name="twelvedata", minute_limit=rpm, daily_limit=rpd, default_cost=1,
        endpoint_costs={"time_series": 1, "quote": 1, "api_usage": 1},
    )
    provider._api_usage_endpoint = "/api_usage"
    provider._api_usage_cache_seconds = 300
    provider._api_usage_max_interval = 3600
    provider._api_usage_cache = None
    provider._api_usage_cache_at = 0.0
    provider._last_api_usage_attempt = 0.0
    provider.last_error = None
    return provider


# ------------------------------------------------------------------------------
# RateLimitWindow basics
# ------------------------------------------------------------------------------
def test_window_check_consume_and_status():
    w = RateLimitWindow("t", minute_limit=2, daily_limit=10)
    assert w.check_available(1)
    assert w.consume(1)
    assert w.consume(1)
    assert not w.check_available(1)  # minute limit exhausted (2/2)
    assert "minute_limit_exhausted" in w.insufficient_reason(1)
    status = w.get_status()
    assert status["credits_used"] == 2
    assert status["minute_limit"] == 2
    # No provider data seen yet -> status UNKNOWN, no invented remaining
    assert status["status"] == "UNKNOWN"
    assert "credits_remaining" not in status
    assert status["stale"] is True


def test_window_minute_reset():
    w = RateLimitWindow("t", minute_limit=1, daily_limit=None)
    assert w.consume(1)
    assert not w.check_available(1)
    w._minute_reset_at = time.time() - 1  # force window roll
    assert w.check_available(1)


def test_window_weighted_requests():
    w = RateLimitWindow("t", minute_limit=8, default_cost=1)
    w.set_endpoint_cost("heavy", 5)
    assert w.get_cost("heavy") == 5
    assert w.get_cost("unknown_endpoint") == 1
    assert w.consume(5)
    assert not w.consume(4)  # 5 used of 8 -> 4 more would exceed
    assert w.consume(3)


def test_window_daily_limit_blocks():
    w = RateLimitWindow("t", minute_limit=100, daily_limit=3)
    assert w.consume(2)
    assert not w.consume(2)  # would exceed daily 3
    assert w.consume(1)
    assert "daily_limit_exhausted" in w.insufficient_reason(1)


# ------------------------------------------------------------------------------
# Header parsing (documented Twelve Data headers, case-insensitive)
# ------------------------------------------------------------------------------
def test_parse_headers_documented_names():
    parsed = RateLimitWindow.parse_headers(FakeHeaders({
        "API-Credits-Used": "1",
        "api-credits-left": "799",
    }))
    assert parsed["credits_used"] == 1
    assert parsed["credits_left"] == 799


def test_parse_headers_case_variants_and_whitespace():
    parsed = RateLimitWindow.parse_headers(FakeHeaders({
        "Api-Credits-Used": "2",
        "Api-Credits-Left": "  798 ",
    }))
    assert parsed["credits_used"] == 2
    assert parsed["credits_left"] == 798


def test_parse_headers_garbage_values_are_ignored():
    parsed = RateLimitWindow.parse_headers(FakeHeaders({
        "api-credits-used": "not-a-number",
        "api-credits-left": "",
    }))
    assert parsed["credits_used"] is None
    assert parsed["credits_left"] is None


def test_sync_from_headers_updates_state():
    w = RateLimitWindow("t", minute_limit=8, daily_limit=800)
    status = w.sync_from_headers(FakeHeaders({"api-credits-used": "1", "api-credits-left": "799"}))
    assert w.get_status()["credits_remaining"] == 799
    assert status["source"] == "response_headers"
    assert status["last_synced_at"] is not None
    assert status["status"] == "HEALTHY"
    assert status["stale"] is False


def test_local_remaining_never_exceeds_provider_reported():
    w = RateLimitWindow("t", minute_limit=8, daily_limit=800)
    w.sync_from_headers(FakeHeaders({"api-credits-used": "1", "api-credits-left": "500"}))
    # A later header reporting fewer credits must lower the stored value
    w.sync_from_headers(FakeHeaders({"api-credits-used": "1", "api-credits-left": "490"}))
    assert w.get_status()["credits_remaining"] == 490
    # A higher reported value never raises the remaining above last report
    w.sync_from_headers(FakeHeaders({"api-credits-used": "1", "api-credits-left": "600"}))
    assert w.get_status()["credits_remaining"] == 490


def test_mark_request_consumed_when_no_headers():
    w = RateLimitWindow("t", minute_limit=8)
    w.mark_request_consumed(1)
    assert w.get_status()["credits_used"] == 1


# ------------------------------------------------------------------------------
# 429 / Retry-After / exponential backoff
# ------------------------------------------------------------------------------
def test_on_rate_limited_uses_retry_after():
    w = RateLimitWindow("t", minute_limit=8)
    wait = w.on_rate_limited(12.0)
    assert wait == 12.0
    assert not w.check_available(1)
    assert w.get_status()["status"] == "RATE_LIMITED"
    assert w.get_status()["retry_after"] == 12.0


def test_on_rate_limited_exponential_default():
    w = RateLimitWindow("t", minute_limit=8)
    w1 = w.on_rate_limited(None)
    w.clear_cooldown()
    w2 = w.on_rate_limited(None)
    assert 0 < w1 <= 2.0
    assert 2.0 < w2 <= 4.0


# ------------------------------------------------------------------------------
# Provider request lifecycle
# ------------------------------------------------------------------------------
def test_provider_preflight_blocks_when_exhausted():
    provider = make_provider(rpm=1)
    provider.rate_limit_window.consume(1)
    with pytest.raises(ProviderRateLimited):
        provider._pre_flight("time_series")


def test_provider_post_flight_syncs_headers(monkeypatch):
    provider = make_provider(rpm=8)
    resp = FakeResponse(200, {"api-credits-used": "1", "api-credits-left": "799"}, {"values": []})
    provider._post_flight(resp, "time_series")
    status = provider.get_rate_limit_status()
    assert status["credits_remaining"] == 799
    assert status["source"] == "response_headers"
    assert status["provider"] == "twelvedata"


def test_provider_post_flight_429_enters_cooldown():
    provider = make_provider(rpm=8)
    resp = FakeResponse(429, {"Retry-After": "30"}, None)
    provider._post_flight(resp, "time_series")
    assert not provider.rate_limit_window.check_available(1)
    assert provider.get_rate_limit_status()["status"] == "RATE_LIMITED"


def test_fetch_full_flow_credit_sync(monkeypatch):
    provider = make_provider(rpm=8)

    class FakeClient:
        def get(self, url, **kwargs):
            return FakeResponse(200, {"api-credits-used": "1", "api-credits-left": "799"}, {
                "status": "ok",
                "values": [{
                    "datetime": "2026-09-29 12:15:00",
                    "open": "2600.10", "high": "2602.00", "low": "2599.50", "close": "2601.25",
                }],
            })

    monkeypatch.setattr(TwelveDataProvider, "_get_client", lambda self: FakeClient())
    candles = provider.get_ohlcv("XAUUSD", "15m", limit=10)
    assert len(candles) == 1
    assert candles[0]["close"] == 2601.25
    status = provider.get_rate_limit_status()
    assert status["credits_remaining"] == 799


def test_fetch_rate_limited_payload(monkeypatch):
    provider = make_provider(rpm=8)
    provider.rate_limit_window.clear_cooldown()

    class FakeClient:
        def get(self, url, **kwargs):
            return FakeResponse(200, {}, {"status": "error", "code": "429", "message": "rate limit reached"})

    monkeypatch.setattr(TwelveDataProvider, "_get_client", lambda self: FakeClient())
    with pytest.raises(ProviderError) as exc:
        provider.get_ohlcv("XAUUSD", "15m", limit=1)
    assert exc.value.category == "rate_limit"
    assert not provider.rate_limit_window.check_available(1)  # cooldown engaged


def test_fetch_malformed_json(monkeypatch):
    provider = make_provider(rpm=8)

    class FakeClient:
        def get(self, url, **kwargs):
            return FakeResponse(200, {}, ValueError("bad json"))

    monkeypatch.setattr(TwelveDataProvider, "_get_client", lambda self: FakeClient())
    with pytest.raises(ProviderError) as exc:
        provider.get_ohlcv("XAUUSD", "15m", limit=1)
    assert exc.value.category == "malformed_response"
    assert provider.last_error["code"] == "TD_MALFORMED_JSON"


def test_fetch_auth_failure(monkeypatch):
    provider = make_provider(rpm=8)

    class FakeClient:
        def get(self, url, **kwargs):
            return FakeResponse(200, {}, {"status": "error", "code": "401", "message": "invalid api key"})

    monkeypatch.setattr(TwelveDataProvider, "_get_client", lambda self: FakeClient())
    with pytest.raises(ProviderError) as exc:
        provider.get_ohlcv("XAUUSD", "15m", limit=1)
    assert exc.value.category == "auth"
    assert exc.value.code == "TD_AUTH_FAILED"


def test_fetch_invalid_symbol(monkeypatch):
    provider = make_provider(rpm=8)

    class FakeClient:
        def get(self, url, **kwargs):
            return FakeResponse(200, {}, {"status": "error", "code": "404", "message": "invalid symbol for exchange"})

    monkeypatch.setattr(TwelveDataProvider, "_get_client", lambda self: FakeClient())
    with pytest.raises(ProviderError) as exc:
        provider.get_ohlcv("BADSYM", "15m", limit=1)
    assert exc.value.category in {"invalid_symbol", "auth"}


def test_fetch_network_failure(monkeypatch):
    import httpx
    provider = make_provider(rpm=8)

    class FakeClient:
        def get(self, url, **kwargs):
            raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(TwelveDataProvider, "_get_client", lambda self: FakeClient())
    with pytest.raises((httpx.ConnectError, RuntimeError)):
        provider.get_ohlcv("XAUUSD", "15m", limit=1)


# ------------------------------------------------------------------------------
# /api_usage (explicit, cached, never polled)
# ------------------------------------------------------------------------------
def test_get_api_usage_parses_known_fields_and_caches(monkeypatch):
    provider = make_provider(rpm=8)
    calls = {"n": 0}

    class FakeClient:
        def get(self, url, **kwargs):
            calls["n"] += 1
            return FakeResponse(200, {"api-credits-used": "1", "api-credits-left": "799"}, {
                "current_day": {"current_value": 42, "total_value": 800, "credits_left": 758, "timestamp_unix": 1759262400},
                "current_month": {"current_value": 100, "total_value": 800, "credits_left": 700, "timestamp_unix": 1759262400},
            })

    monkeypatch.setattr(TwelveDataProvider, "_get_client", lambda self: FakeClient())
    usage = provider.get_api_usage_sync()
    assert usage is not None
    assert usage["current_day"]["used"] == 42
    assert usage["current_day"]["remaining"] == 758
    assert usage["current_day"]["limit"] == 800
    assert usage["current_month"]["used"] == 100
    # Second call within cache window does not hit the network again
    usage2 = provider.get_api_usage_sync()
    assert calls["n"] == 1
    assert usage2["cached"] is True


def test_get_api_usage_unknown_fields_return_none(monkeypatch):
    provider = make_provider(rpm=8)

    class FakeClient:
        def get(self, url, **kwargs):
            return FakeResponse(200, {}, {"weird_field": "x"})

    monkeypatch.setattr(TwelveDataProvider, "_get_client", lambda self: FakeClient())
    usage = provider.get_api_usage_sync()
    assert usage is not None
    assert usage["current_day"]["used"] is None
    assert usage["current_day"]["remaining"] is None
    assert usage["current_day"]["reset_at"] is None


def test_get_api_usage_requires_key():
    provider = make_provider(rpm=8)
    provider.api_key = ""
    assert provider._fetch_api_usage_payload() is None


def test_api_usage_never_called_by_market_data_request(monkeypatch):
    from app.data.resilience import rate_limit_manager
    rate_limit_manager._states.pop("twelvedata", None)  # clear global cooldown from prior tests
    provider = make_provider(rpm=8)
    usage_calls = {"n": 0}

    class FakeClient:
        def get(self, url, **kwargs):
            if "api_usage" in url:
                usage_calls["n"] += 1
                raise AssertionError("/api_usage must not be called by market-data requests")
            return FakeResponse(200, {"api-credits-used": "1", "api-credits-left": "799"}, {
                "values": [{"datetime": "2026-09-29 12:15:00", "open": "1", "high": "1", "low": "1", "close": "1"}],
            })

    monkeypatch.setattr(TwelveDataProvider, "_get_client", lambda self: FakeClient())
    provider.get_ohlcv("XAUUSD", "15m", limit=2)
    assert usage_calls["n"] == 0


def test_api_usage_respects_hard_floor_between_attempts(monkeypatch):
    provider = make_provider(rpm=8)
    provider._api_usage_cache_seconds = 0
    provider._api_usage_max_interval = 3600
    calls = {"n": 0}

    class FakeClient:
        def get(self, url, **kwargs):
            calls["n"] += 1
            return FakeResponse(200, {}, {"current_day": {"current_value": 1, "total_value": 800, "credits_left": 799}})

    monkeypatch.setattr(TwelveDataProvider, "_get_client", lambda self: FakeClient())
    provider._fetch_api_usage_payload(force=True)
    provider._fetch_api_usage_payload(force=True)
    assert calls["n"] == 1  # hard floor prevented a second call


def test_stale_credit_state_indicator(monkeypatch):
    provider = make_provider(rpm=8)
    resp = FakeResponse(200, {"api-credits-used": "1", "api-credits-left": "799"}, {"values": []})
    provider._post_flight(resp, "time_series")
    status = provider.get_rate_limit_status()
    assert status["stale"] is False
    # Simulate time passing beyond the staleness threshold
    provider.rate_limit_window._last_synced_at = "2020-01-01T00:00:00+00:00"
    status = provider.get_rate_limit_status()
    assert status["stale"] is True


def test_provider_status_unknown_before_any_sync():
    provider = make_provider(rpm=8)
    status = provider.get_rate_limit_status()
    assert status["status"] == "UNKNOWN"
    assert "credits_remaining" not in status


# ------------------------------------------------------------------------------
# Error categories
# ------------------------------------------------------------------------------
def test_error_categories_match_spec():
    expected = {
        "auth", "rate_limit", "invalid_symbol", "invalid_request", "not_entitled",
        "server", "network", "timeout", "malformed_response", "unknown",
    }
    assert set(TwelveDataProvider.ERROR_CATEGORIES) == expected
    assert ProviderError.CATEGORIES == expected


def test_provider_error_to_dict():
    err = ProviderError("boom", category="server", code="X1", provider="twelvedata")
    d = err.to_dict()
    assert d["category"] == "server"
    assert d["code"] == "X1"
    assert d["provider"] == "twelvedata"


def test_invalid_category_falls_back_to_unknown():
    err = ProviderError("x", category="nonsense")
    assert err.category == "unknown"

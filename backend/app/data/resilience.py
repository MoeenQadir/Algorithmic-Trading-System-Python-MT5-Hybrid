"""Rate-limit protection and provider health (Milestone 5).

Components:
- RateLimitManager: tracks 429 / Retry-After per provider, blocks requests
  until the cooldown expires. Never retries immediately after a 429.
- CircuitBreaker: opens after consecutive failures, half-opens after cooldown.
- ProviderHealthManager: OK / DEGRADED / OFFLINE / ERROR per component with
  error counts and last-success timestamps (surfaced on the health page).
- resilient_request: throttled retry with exponential backoff + jitter,
  cache fallback handled by the caller.
"""
from __future__ import annotations

import math
import random
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Iterable, Optional

import httpx

from app.utils.timeutils import utcnow


@dataclass
class _ProviderState:
    blocked_until: float = 0.0
    last_retry_after: float = 0.0
    rate_limit_events: int = 0
    consecutive_failures: int = 0
    last_error: str = ""
    last_success: Optional[datetime] = None
    last_attempt: Optional[datetime] = None
    error_count: int = 0
    request_count: int = 0
    breaker_open_until: float = 0.0


class RateLimitManager:
    """Tracks per-provider rate-limit state. Thread-safe."""

    def __init__(self) -> None:
        self._states: dict[str, _ProviderState] = {}
        self._lock = threading.Lock()

    def _state(self, provider: str) -> _ProviderState:
        with self._lock:
            return self._states.setdefault(provider, _ProviderState())

    def can_request(self, provider: str) -> bool:
        st = self._state(provider)
        return time.time() >= st.blocked_until and time.time() >= st.breaker_open_until

    def wait_time(self, provider: str) -> float:
        st = self._state(provider)
        return max(0.0, st.blocked_until - time.time(), st.breaker_open_until - time.time())

    def on_rate_limited(self, provider: str, retry_after: Optional[float] = None) -> None:
        st = self._state(provider)
        wait = retry_after if retry_after and retry_after > 0 else min(300.0, 2.0 ** min(st.rate_limit_events, 6))
        with self._lock:
            st.rate_limit_events += 1
            st.last_retry_after = wait
            st.blocked_until = time.time() + wait
            st.last_error = "rate_limited"

    def on_failure(self, provider: str, error: str, max_failures: int = 4, cooldown: float = 120.0) -> None:
        st = self._state(provider)
        with self._lock:
            st.consecutive_failures += 1
            st.error_count += 1
            st.last_error = error[:200]
            if st.consecutive_failures >= max_failures:
                st.breaker_open_until = time.time() + cooldown

    def on_success(self, provider: str) -> None:
        st = self._state(provider)
        with self._lock:
            st.consecutive_failures = 0
            st.last_error = ""
            st.last_success = utcnow()
            st.request_count += 1

    def on_attempt(self, provider: str) -> None:
        st = self._state(provider)
        with self._lock:
            st.last_attempt = utcnow()

    def snapshot(self, provider: str) -> dict[str, Any]:
        st = self._state(provider)
        limited = st.blocked_until > time.time()
        breaker_open = st.breaker_open_until > time.time()
        if breaker_open:
            status = "OFFLINE"
        elif limited or st.consecutive_failures > 0:
            status = "DEGRADED"
        elif st.last_success is not None:
            status = "OK"
        else:
            status = "OK"  # never used yet is not an error
        return {
            "provider": provider,
            "status": status,
            "rate_limited": limited,
            "retry_in_seconds": round(self.wait_time(provider), 1),
            "rate_limit_events": st.rate_limit_events,
            "consecutive_failures": st.consecutive_failures,
            "error_count": st.error_count,
            "request_count": st.request_count,
            "last_error": st.last_error,
            "last_success": st.last_success.isoformat() if st.last_success else None,
            "last_attempt": st.last_attempt.isoformat() if st.last_attempt else None,
        }


rate_limit_manager = RateLimitManager()


class RateLimitWindow:
    """Reusable credit-aware rate-limit window (Phase 35).

    Tracks a mandatory minute window and an optional daily window of credits,
    with configurable limits and per-endpoint credit costs. Credits are
    consumed locally before a request and then synchronized with authoritative
    provider state (response headers or /api_usage payload).

    Invariants:
    - Local remaining credits never exceed provider-reported remaining credits.
    - When no authoritative data has been seen yet the status is UNKNOWN and
      no quota values are invented.
    - Weighted requests are supported: consume(cost) with cost > 1.

    Thread-safe.
    """

    HEADER_CREDITS_USED = "api-credits-used"
    HEADER_CREDITS_LEFT = "api-credits-left"
    HEADER_CREDITS_RESET = "api-credits-reset"
    HEADER_RATE_LIMIT = "api-ratelimit-limit"
    HEADER_RETRY_AFTER = "retry-after"

    def __init__(
        self,
        name: str = "provider",
        minute_limit: int | None = None,
        daily_limit: int | None = None,
        default_cost: int = 1,
        endpoint_costs: dict[str, int] | None = None,
    ) -> None:
        self.name = name
        self._lock = threading.RLock()
        self.minute_limit = minute_limit  # None = unknown until provider reports
        self.daily_limit = daily_limit  # None = unknown or no daily window

        # Local counters (estimated)
        self._minute_used = 0
        self._minute_reset_at = time.time() + 60.0
        self._daily_used = 0
        self._daily_reset_at = time.time() + 86400.0

        # Authoritative provider state (never fabricated)
        self._credits_remaining: Optional[int] = None  # provider-reported remaining
        self._daily_remaining: Optional[int] = None
        self._last_synced_at: Optional[str] = None
        self._last_headers: dict[str, str] = {}
        self._retry_after: Optional[float] = None

        # Costs
        self.default_cost = max(0, int(default_cost))
        self.endpoint_costs: dict[str, int] = dict(endpoint_costs or {})

        # Cooldown after a 429 (exponential backoff)
        self._cooldown_until = 0.0
        self._rate_limit_events = 0

    # ------------------------------------------------------------------ costs
    def get_cost(self, endpoint: str | None = None) -> int:
        with self._lock:
            if endpoint and endpoint in self.endpoint_costs:
                return max(0, int(self.endpoint_costs[endpoint]))
            return self.default_cost

    def set_endpoint_cost(self, endpoint: str, cost: int) -> None:
        with self._lock:
            self.endpoint_costs[endpoint] = max(0, int(cost))

    # --------------------------------------------------------------- checking
    def check_available(self, cost: int = 1) -> bool:
        """True if `cost` credits can be spent now without exceeding limits."""
        with self._lock:
            self._roll_windows()
            if time.time() < self._cooldown_until:
                return False
            if self._minute_limit() is not None:
                if self._minute_projected_used() + max(0, cost) > self._minute_limit():
                    return False
            if self.daily_limit is not None:
                if self._daily_projected_used() + max(0, cost) > self.daily_limit:
                    return False
            return True

    def consume(self, cost: int = 1) -> bool:
        """Atomically check and consume `cost` credits locally."""
        with self._lock:
            if not self.check_available(cost):
                return False
            self._minute_used += max(0, cost)
            self._daily_used += max(0, cost)
            return True

    def wait_time(self) -> float:
        """Seconds until the next attempt is plausible (cooldown or window reset)."""
        with self._lock:
            now = time.time()
            waits = [self._cooldown_until - now]
            if self._minute_limit() is not None:
                waits.append(self._minute_reset_at - now)
            if self.daily_limit is not None:
                waits.append(self._daily_reset_at - now)
            return max(0.0, *waits)

    def insufficient_reason(self, cost: int = 1) -> str:
        """Human-readable reason when check_available fails."""
        with self._lock:
            if time.time() < self._cooldown_until:
                return f"rate_limit_cooldown:{round(self._cooldown_until - time.time(), 1)}s"
            if self._minute_limit() is not None and self._minute_projected_used() + max(0, cost) > self._minute_limit():
                return f"minute_limit_exhausted:{self._minute_used}/{self._minute_limit()}"
            if self.daily_limit is not None and self._daily_projected_used() + max(0, cost) > self.daily_limit:
                return f"daily_limit_exhausted:{self._daily_used}/{self.daily_limit}"
            return "ok"

    # ------------------------------------------------------------- window math
    def _roll_windows(self) -> None:
        now = time.time()
        if now >= self._minute_reset_at:
            self._minute_used = 0
            self._minute_reset_at = now + 60.0
        if self.daily_limit is not None and now >= self._daily_reset_at:
            self._daily_used = 0
            self._daily_reset_at = now + 86400.0

    def _minute_limit(self) -> Optional[int]:
        return self.minute_limit

    def _minute_projected_used(self) -> int:
        return self._minute_used

    def _daily_projected_used(self) -> int:
        return self._daily_used

    # ------------------------------------------------------------ synchronization
    def sync_from_headers(self, headers: Any) -> dict[str, Any]:
        """Synchronize state from provider response headers (case-insensitive).

        Twelve Data documented headers: api-credits-used, api-credits-left.
        Only values actually present are applied; nothing is invented.
        """
        parsed = self.parse_headers(headers)
        with self._lock:
            self._last_headers = {k.lower(): v for k, v in parsed["raw"].items()}
            if parsed["credits_used"] is not None:
                # Authoritative: the request consumed exactly this many credits.
                self._minute_used += parsed["credits_used"]
                self._daily_used += parsed["credits_used"]
            if parsed["credits_left"] is not None:
                # Never claim more remaining credits than the provider reports.
                prev = self._credits_remaining
                self._credits_remaining = min(prev, parsed["credits_left"]) if prev is not None else parsed["credits_left"]
            if parsed["rate_limit"] is not None:
                self.minute_limit = parsed["rate_limit"]
            if parsed["reset_seconds"] is not None:
                now = time.time()
                self._minute_reset_at = max(self._minute_reset_at, now + parsed["reset_seconds"])
            if parsed["retry_after"] is not None:
                self._retry_after = parsed["retry_after"]
            self._last_synced_at = utcnow().isoformat()
            return self._status_locked(source="response_headers")

    @classmethod
    def parse_headers(cls, headers: Any) -> dict[str, Any]:
        """Parse provider headers case-insensitively into typed values."""
        raw: dict[str, str] = {}
        get = getattr(headers, "get", None)
        if callable(get):
            try:
                items = headers.items()  # httpx.Headers / email.Message style
            except Exception:
                items = []
            for k, v in items:
                try:
                    raw[str(k).lower()] = str(v)
                except Exception:
                    continue
        elif isinstance(headers, dict):
            raw = {str(k).lower(): str(v) for k, v in headers.items()}

        def _int(names: Iterable[str]) -> Optional[int]:
            for n in names:
                v = raw.get(n)
                if v is None or v == "":
                    continue
                try:
                    return int(float(v))
                except (TypeError, ValueError):
                    continue
            return None

        def _float(names: Iterable[str]) -> Optional[float]:
            for n in names:
                v = raw.get(n)
                if v is None or v == "":
                    continue
                try:
                    return float(v)
                except (TypeError, ValueError):
                    continue
            return None

        return {
            "raw": raw,
            "credits_used": _int([cls.HEADER_CREDITS_USED, "x-api-credits-used"]),
            "credits_left": _int([cls.HEADER_CREDITS_LEFT, "x-api-credits-left"]),
            "reset_seconds": _int([cls.HEADER_CREDITS_RESET, "x-api-credits-reset"]),
            "rate_limit": _int([cls.HEADER_RATE_LIMIT, "x-api-ratelimit-limit"]),
            "retry_after": _float([cls.HEADER_RETRY_AFTER, "x-retry-after"]),
        }

    def sync_from_usage_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Synchronize state from a /api_usage payload.

        Only documented/known fields are read; unknown fields are ignored and
        never invented. Twelve Data's /api_usage returns per-period credit
        usage under keys like current_day/current_month and per-endpoint
        breakdowns; we extract totals defensively.
        """
        if not isinstance(payload, dict):
            return self._status_locked(source="api_usage", stale=self._last_synced_at is not None)
        with self._lock:
            # Known top-level shapes: {"current_day": {...}, "current_month": {...}}
            day = payload.get("current_day")
            if isinstance(day, dict):
                used = day.get("current_value")
                left = day.get("credits_left")
                limit = day.get("total_value")
                if isinstance(used, (int, float)):
                    self._daily_used = int(used)
                if isinstance(limit, (int, float)):
                    self.daily_limit = int(limit)
                if isinstance(left, (int, float)):
                    self._daily_remaining = int(left)
                    self._credits_remaining = min(
                        self._credits_remaining if self._credits_remaining is not None else int(left),
                        int(left),
                    )
                if day.get("timestamp_unix") is not None:
                    try:
                        self._daily_reset_at = max(self._daily_reset_at, float(day["timestamp_unix"]) + 86400.0)
                    except (TypeError, ValueError):
                        pass
            month = payload.get("current_month")
            if isinstance(month, dict):
                left = month.get("credits_left")
                if isinstance(left, (int, float)):
                    self._daily_remaining = int(left)
            self._last_synced_at = utcnow().isoformat()
            return self._status_locked(source="api_usage")

    # --------------------------------------------------------------- 429 / cooldown
    def on_rate_limited(self, retry_after: float | None = None) -> float:
        """Enter cooldown after a 429. Exponential backoff when no header."""
        with self._lock:
            self._rate_limit_events += 1
            wait = retry_after if retry_after and retry_after > 0 else min(300.0, 2.0 ** min(self._rate_limit_events, 6))
            self._retry_after = wait
            self._cooldown_until = time.time() + wait
            return wait

    def clear_cooldown(self) -> None:
        with self._lock:
            self._cooldown_until = 0.0

    # ------------------------------------------------------------------ status
    def _status_locked(self, source: str | None = None, stale: bool | None = None) -> dict[str, Any]:
        """Build status dict. Only include fields actually known."""
        now_iso = utcnow().isoformat()
        status: dict[str, Any] = {"provider": self.name}

        is_stale = bool(stale)
        if stale is None:
            # stale if last sync is older than 10 minutes
            if self._last_synced_at is None:
                is_stale = True
            else:
                try:
                    last_dt = datetime.fromisoformat(self._last_synced_at)
                    is_stale = (utcnow() - last_dt).total_seconds() > 600
                except ValueError:
                    is_stale = True

        if source:
            status["source"] = source
        elif self._last_synced_at:
            status["source"] = "response_headers"

        if self._last_synced_at:
            status["last_synced_at"] = self._last_synced_at
            status["stale"] = is_stale
        else:
            status["stale"] = True

        known = self._credits_remaining is not None or self._last_synced_at is not None
        status["status"] = "HEALTHY" if known else "UNKNOWN"
        if time.time() < self._cooldown_until:
            status["status"] = "RATE_LIMITED"
        if self._minute_limit() is not None:
            status["minute_limit"] = self._minute_limit()
            status["credits_used"] = self._minute_used
        if self._credits_remaining is not None:
            status["credits_remaining"] = self._credits_remaining
        if self.daily_limit is not None:
            status["daily_limit"] = self.daily_limit
            status["daily_used"] = self._daily_used
        if self._daily_remaining is not None:
            status["daily_remaining"] = self._daily_remaining
        if self._retry_after is not None:
            status["retry_after"] = self._retry_after
        return status

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            return self._status_locked()

    def from_response_headers(self, headers: Any) -> dict[str, Any]:
        """Alias for sync_from_headers (spec-named helper)."""
        return self.sync_from_headers(headers)

    def last_headers(self) -> dict[str, str]:
        with self._lock:
            return dict(self._last_headers)

    def mark_request_consumed(self, cost: int) -> None:
        """Record an estimate when the provider did not return credit headers.

        Used to keep local counters advancing; provider data always overrides.
        """
        with self._lock:
            self._minute_used += max(0, int(cost))
            self._daily_used += max(0, int(cost))

    # ---------------------------------------------------------------- health
    def health_ratio(self) -> float | None:
        """Remaining credits as a ratio 0..1 when known, else None."""
        with self._lock:
            if self._credits_remaining is None:
                return None
            limit = self.minute_limit if self.minute_limit is not None else self.daily_limit
            if not limit:
                return None
            return max(0.0, min(1.0, self._credits_remaining / limit))


def _clamp_non_negative(value: float) -> float:
    return value if value >= 0 and math.isfinite(value) else 0.0


class ProviderError(Exception):
    """Structured provider error with a machine-readable category.

    Categories: auth | rate_limit | invalid_symbol | invalid_request |
    not_entitled | server | network | timeout | malformed_response | unknown
    """

    CATEGORIES = {
        "auth", "rate_limit", "invalid_symbol", "invalid_request", "not_entitled",
        "server", "network", "timeout", "malformed_response", "unknown",
    }

    def __init__(
        self,
        message: str,
        *,
        category: str = "unknown",
        code: str = "UNKNOWN",
        provider: str = "",
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category if category in self.CATEGORIES else "unknown"
        self.code = code
        self.provider = provider
        self.retry_after = retry_after

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "code": self.code,
            "provider": self.provider,
            "message": str(self),
            "retry_after": self.retry_after,
        }


class ProviderRateLimited(ProviderError):
    """Raised when a provider request is blocked (429 cooldown or local credit window)."""

    def __init__(
        self,
        message: str,
        *,
        provider: str = "",
        retry_after: float | None = None,
        code: str = "RATE_LIMITED",
    ) -> None:
        super().__init__(
            message,
            category="rate_limit",
            code=code,
            provider=provider,
            retry_after=retry_after,
        )


class ProviderHealthManager:
    """Simple component-level health registry for the system-health page."""

    def __init__(self) -> None:
        self._components: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def report(self, component: str, status: str, message: str = "") -> None:
        status = status if status in {"OK", "DEGRADED", "OFFLINE", "ERROR"} else "ERROR"
        with self._lock:
            entry = self._components.setdefault(component, {"error_count": 0, "warning_count": 0})
            entry.update({"status": status, "message": message[:300], "updated_at": utcnow().isoformat()})
            if status in {"ERROR", "OFFLINE"}:
                entry["error_count"] += 1
            elif status == "DEGRADED":
                entry["warning_count"] += 1

    def get(self, component: str) -> dict[str, Any]:
        with self._lock:
            entry = self._components.get(component)
            if not entry:
                return {"status": "OK", "message": "no events", "error_count": 0, "warning_count": 0}
            return dict(entry)

    def all(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {k: dict(v) for k, v in self._components.items()}


provider_health = ProviderHealthManager()


def resilient_request(
    fn: Callable[..., httpx.Response],
    provider: str,
    *,
    max_retries: int = 3,
    base_delay: float = 1.0,
    timeout: float = 20.0,
    **kwargs: Any,
) -> httpx.Response:
    """HTTP request with throttling, backoff, jitter, 429 and timeout handling.

    Raises the last exception only after retries are exhausted; callers are
    expected to fall back to cache/local data. Never retries a 429 before its
    Retry-After window.
    """
    rl = rate_limit_manager
    delay = base_delay
    last_exc: Optional[Exception] = None
    for attempt in range(max_retries + 1):
        if not rl.can_request(provider):
            raise RuntimeError(f"provider '{provider}' is rate-limited; retry in {rl.wait_time(provider):.0f}s")
        rl.on_attempt(provider)
        try:
            resp = fn(timeout=timeout, **kwargs)
            if resp.status_code == 429:
                retry_after = None
                ra = resp.headers.get("Retry-After")
                if ra:
                    try:
                        retry_after = float(ra)
                    except ValueError:
                        retry_after = None
                rl.on_rate_limited(provider, retry_after)
                last_exc = RuntimeError(f"429 rate limited by {provider}")
                # do NOT loop-hammer: single backoff then continue only if retries remain
                if attempt < max_retries:
                    time.sleep(max(rl.wait_time(provider), delay))
                    delay *= 2
                continue
            resp.raise_for_status()
            rl.on_success(provider)
            return resp
        except httpx.TimeoutException as exc:
            rl.on_failure(provider, f"timeout: {exc}")
            last_exc = exc
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code >= 500:
                rl.on_failure(provider, f"http {exc.response.status_code}")
                last_exc = exc
            else:
                rl.on_failure(provider, f"http {exc.response.status_code}")
                raise  # client errors will not fix themselves
        except (httpx.HTTPError, OSError) as exc:
            rl.on_failure(provider, str(exc))
            last_exc = exc
        if attempt < max_retries:
            time.sleep(delay + random.uniform(0, delay * 0.5))
            delay *= 2
    assert last_exc is not None
    raise last_exc

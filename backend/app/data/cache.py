"""Local file cache (Milestone 4).

Rules enforced here:
- Before any external API request the caller MUST consult this cache.
- Only missing data is fetched; responses are persisted and reused.
- TTLs are configurable; different namespaces have different TTLs.
- A request-deduplication lock prevents duplicate concurrent fetches.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from typing import Any, Callable, Optional

from app.config import get_settings

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
CACHE_ROOT = os.path.join(ROOT, "data", "cache")

_NAMESPACES = ("data", "news", "llm")

_inflight_locks: dict[str, threading.Lock] = {}
_inflight_guard = threading.Lock()


def _ns_dir(namespace: str) -> str:
    d = os.path.join(CACHE_ROOT, namespace)
    os.makedirs(d, exist_ok=True)
    return d


def _key_path(key: str, namespace: str) -> str:
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
    return os.path.join(_ns_dir(namespace), f"{digest}.json")


class FileCache:
    """JSON file cache with per-entry TTL and namespaces data/news/llm."""

    def __init__(self) -> None:
        s = get_settings()
        self.ttl = {
            "data": s.cache_ttl_ohlcv,
            "news": s.cache_ttl_news,
            "llm": s.cache_ttl_llm,
        }

    def get(self, key: str, namespace: str = "data") -> Optional[Any]:
        path = _key_path(key, namespace)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return None
        stored_at = float(payload.get("stored_at", 0))
        ttl = float(payload.get("ttl", self.ttl.get(namespace, 300)))
        if time.time() - stored_at > ttl:
            return None
        return payload.get("value")

    def get_stale(self, key: str, namespace: str = "data") -> Optional[Any]:
        """Return cached value even if expired (used as offline fallback)."""
        path = _key_path(key, namespace)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
            return payload.get("value")
        except (OSError, json.JSONDecodeError):
            return None

    def set(self, key: str, value: Any, namespace: str = "data", ttl: Optional[int] = None) -> None:
        path = _key_path(key, namespace)
        tmp = f"{path}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"stored_at": time.time(), "ttl": ttl or self.ttl.get(namespace, 300), "value": value}, fh)
            os.replace(tmp, path)
        except OSError:
            pass  # cache write failures must never break the app

    def invalidate(self, key: str, namespace: str = "data") -> None:
        try:
            os.remove(_key_path(key, namespace))
        except OSError:
            pass

    def cached_or_fetch(
        self,
        key: str,
        namespace: str,
        fetch_fn: Callable[[], Any],
        ttl: Optional[int] = None,
    ) -> tuple[Any, bool]:
        """Return (value, from_cache). Deduplicates concurrent fetches for the same key.

        Falls back to stale cache when the fetcher fails (offline / rate-limited).
        """
        fresh = self.get(key, namespace)
        if fresh is not None:
            return fresh, True

        with _inflight_guard:
            lock = _inflight_locks.setdefault(key, threading.Lock())
        with lock:
            fresh = self.get(key, namespace)
            if fresh is not None:
                return fresh, True
            try:
                value = fetch_fn()
                self.set(key, value, namespace, ttl)
                return value, False
            except Exception:
                stale = self.get_stale(key, namespace)
                if stale is not None:
                    return stale, True
                raise


cache = FileCache()

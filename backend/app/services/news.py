"""News service (Milestone 11).

Principles:
- Provider abstraction; free RSS feeds only (no paid API required).
- Aggressive caching + deduplication.
- If no provider is configured or fetching fails: status="unavailable".
  Never invent news, never fabricate sentiment.
"""
from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
from datetime import timedelta
from typing import Any, Optional

import httpx

from app.config import get_settings
from app.data.cache import cache
from app.data.resilience import provider_health, resilient_request
from app.utils.timeutils import utcnow

_TAG_RE = re.compile(r"<[^>]+>")


class NewsService:
    name = "rss"

    def configured(self) -> bool:
        return len(get_settings().news_rss_list) > 0

    def _fetch_feed(self, url: str) -> list[dict[str, Any]]:
        key = f"rss:{url}"

        def do_get(**kw: Any) -> httpx.Response:
            return httpx.get(url, follow_redirects=True, **kw)

        cached = cache.get(key, "news")
        if cached is not None:
            return cached

        resp = resilient_request(do_get, "news_rss", timeout=15.0)
        root = ET.fromstring(resp.content)
        items: list[dict[str, Any]] = []
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            pub = (item.findtext("pubDate") or "").strip()
            if not title:
                continue
            items.append({
                "headline": _TAG_RE.sub("", title)[:300],
                "url": link,
                "published_at": pub,
                "source": url.split("/")[2] if "//" in url else url,
            })
        for entry in root.iter("entry"):  # Atom feeds
            title = (entry.findtext("title") or "").strip()
            link_el = entry.find("link")
            link = link_el.get("href", "") if link_el is not None else ""
            pub = (entry.findtext("published") or "").strip()
            if title:
                items.append({
                    "headline": _TAG_RE.sub("", title)[:300],
                    "url": link,
                    "published_at": pub,
                    "source": url.split("/")[2] if "//" in url else url,
                })
        return items

    def get_recent_news(self, max_age_hours: int = 24) -> dict[str, Any]:
        """Returns {status, items}. status=unavailable when nothing configured/fetched."""
        if not self.configured():
            return {"status": "unavailable", "items": []}
        all_items: list[dict[str, Any]] = []
        errors = 0
        for url in get_settings().news_rss_list:
            try:
                all_items.extend(self._fetch_feed(url))
            except Exception:
                errors += 1
        if not all_items and errors:
            provider_health.report("news", "OFFLINE", "all configured news feeds failed")
            return {"status": "error", "items": []}
        if not all_items:
            return {"status": "unavailable", "items": []}

        # dedupe by normalized headline
        seen: set[str] = set()
        items: list[dict[str, Any]] = []
        for item in all_items:
            norm = re.sub(r"\s+", " ", item["headline"].lower()).strip()
            digest = hashlib.sha256(norm.encode()).hexdigest()[:24]
            if digest in seen:
                continue
            seen.add(digest)
            item["dedup_key"] = digest
            items.append(item)

        provider_health.report("news", "OK", f"{len(items)} unique headlines cached")
        return {"status": "ok", "items": items[:50]}


news_service = NewsService()

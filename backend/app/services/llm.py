"""OpenRouter LLM client (Milestones 24-25).

- Works with any OpenRouter-compatible endpoint; model comes from config.
- All responses must parse into the provided Pydantic schema. Malformed or
  failed calls NEVER fabricate a result: the caller gets None and must
  degrade gracefully (fail safe -> WAIT).
- Retries with backoff; honors 429 Retry-After; falls back to a configured
  fallback model; caches identical prompts (namespace 'llm').
"""
from __future__ import annotations

import json
import logging
import random
import time
from typing import Any, Optional, Type, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from app.config import get_settings
from app.data.cache import cache
from app.data.resilience import provider_health, rate_limit_manager, resilient_request

logger = logging.getLogger("trading.llm")
T = TypeVar("T", bound=BaseModel)

API_URL = "https://openrouter.ai/api/v1/chat/completions"


def llm_available() -> bool:
    return get_settings().llm_configured


def _schema_hint(schema: Type[T]) -> str:
    try:
        return json.dumps(schema.model_json_schema(), separators=(",", ":"))
    except Exception:
        return "{}"


def _extract_json(text: str) -> Optional[dict[str, Any]]:
    """Extract the first JSON object from a model response, tolerating code fences."""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


class LLMClient:
    """Thin OpenRouter client returning Pydantic-validated structures."""

    def _headers(self) -> dict[str, str]:
        s = get_settings()
        return {
            "Authorization": f"Bearer {s.openrouter_api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "http://localhost:3000",
            "X-Title": "local-trading-framework",
        }

    def _call_model(self, model: str, system: str, user: str, max_tokens: int) -> str:
        s = get_settings()

        def do_post(**kw: Any) -> httpx.Response:
            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "max_tokens": max_tokens,
                "temperature": 0.2,
            }
            return httpx.post(API_URL, headers=self._headers(), json=payload, **kw)

        resp = resilient_request(do_post, "openrouter", timeout=float(s.openrouter_timeout))
        data = resp.json()
        choices = data.get("choices") or []
        if not choices:
            raise ValueError("LLM response contained no choices")
        content = (choices[0].get("message") or {}).get("content")
        if not content:
            raise ValueError("LLM response contained no content")
        return content

    def ask_structured(
        self,
        prompt: str,
        schema: Type[T],
        *,
        system: str = "You are a precise trading analysis assistant.",
        use_cache: bool = True,
        max_tokens: int = 700,
    ) -> Optional[T]:
        """Ask the LLM and validate the answer against `schema`.

        Returns None on: missing API key, network failure, rate limit,
        invalid JSON, schema violation, or fallback-model failure.
        Never raises, never fabricates.
        """
        s = get_settings()
        if not s.llm_configured:
            provider_health.report("llm", "OFFLINE", "AI PROVIDER NOT CONFIGURED")
            return None

        cache_key = f"llm:{s.openrouter_model}:{hashlib_key(prompt, system)}"
        if use_cache:
            cached = cache.get(cache_key, "llm")
            if cached is not None:
                try:
                    return schema.model_validate(cached)
                except ValidationError:
                    cache.invalidate(cache_key, "llm")

        system_full = (
            f"{system}\n\nReply with a single JSON object only, conforming exactly to this JSON schema "
            f"(no prose, no markdown fences): {_schema_hint(schema)}"
        )

        raw = self._try_model(s.openrouter_model, system_full, prompt, max_tokens)
        if raw is None and s.openrouter_fallback_model:
            raw = self._try_model(s.openrouter_fallback_model, system_full, prompt, max_tokens)

        if raw is None:
            return None

        parsed = _extract_json(raw)
        if parsed is None:
            provider_health.report("llm", "DEGRADED", "LLM returned unparseable response")
            return None
        try:
            result = schema.model_validate(parsed)
        except ValidationError as exc:
            provider_health.report("llm", "DEGRADED", f"LLM response failed schema validation: {exc.error_count()} errors")
            return None

        if use_cache:
            cache.set(cache_key, parsed, "llm")
        return result

    def _try_model(self, model: str, system_full: str, prompt: str, max_tokens: int) -> Optional[str]:
        s = get_settings()
        delay = 1.0
        for attempt in range(max(1, s.llm_max_retries + 1)):
            if not rate_limit_manager.can_request("openrouter"):
                wait = rate_limit_manager.wait_time("openrouter")
                if wait > 45:
                    provider_health.report("llm", "DEGRADED", f"rate limited, waiting {wait:.0f}s")
                    return None
                time.sleep(min(wait, 5.0))
            try:
                return self._call_model(model, system_full, prompt, max_tokens)
            except Exception as exc:
                logger.warning("LLM call failed (attempt %s/%s): %s", attempt + 1, s.llm_max_retries + 1, exc)
                if attempt < s.llm_max_retries:
                    time.sleep(delay + random.uniform(0, delay * 0.4))
                    delay *= 2
        provider_health.report("llm", "ERROR", f"LLM unavailable after retries: {exc_summary()}")
        return None


def hashlib_key(prompt: str, system: str) -> str:
    import hashlib

    return hashlib.sha256((system + "||" + prompt).encode("utf-8")).hexdigest()[:24]


def exc_summary() -> str:
    """Placeholder-free last-error summary from the rate limit manager."""
    snap = rate_limit_manager.snapshot("openrouter")
    return snap.get("last_error") or "unknown error"


llm_client = LLMClient()

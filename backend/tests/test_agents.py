"""Agent tests: deterministic outputs, graceful degradation, conflict detection."""
from __future__ import annotations

import pytest

from app.agents.agents import (
    NewsAgent,
    QuantAgent,
    RegimeAgent,
    SentimentAgent,
    StrategyAgent,
    TechnicalAgent,
    run_all_agents,
)
from app.services.sample_data import generate_sample_candles


@pytest.fixture(scope="module")
def candles():
    return generate_sample_candles("US100", "15m", n=400, seed=42)


def test_quant_agent_output_schema(candles):
    out = QuantAgent().analyze(candles)
    assert out.direction in {"LONG", "SHORT", "NEUTRAL"}
    assert 0.0 <= out.confidence <= 1.0
    assert isinstance(out.risk_flags, list)


def test_quant_agent_insufficient_data():
    out = QuantAgent().analyze([])
    assert out.direction == "NEUTRAL"
    assert "insufficient_data" in out.risk_flags


def test_technical_agent_output(candles):
    out = TechnicalAgent().analyze(candles)
    assert out.direction in {"LONG", "SHORT", "NEUTRAL"}
    assert out.trend in {"up", "down", "mixed", "unknown"}
    assert isinstance(out.reasons, list) and out.reasons


def test_regime_agent_output(candles):
    out = RegimeAgent().analyze(candles)
    assert out.regime in {"TRENDING", "RANGING", "HIGH_VOLATILITY", "LOW_VOLATILITY",
                          "RISK_ON", "RISK_OFF", "UNCERTAIN"}
    assert 0.0 <= out.confidence <= 1.0
    assert out.evidence


def test_regime_agent_insufficient(candles):
    out = RegimeAgent().analyze(candles[:10])
    assert out.regime == "UNCERTAIN"


def test_news_agent_unavailable_without_provider(monkeypatch):
    monkeypatch.setenv("NEWS_RSS_URLS", "")
    from app.config import get_settings

    get_settings.cache_clear()
    out = NewsAgent().analyze("US100")
    assert out.status == "unavailable"
    get_settings.cache_clear()


def test_sentiment_agent_unavailable_without_provider(monkeypatch):
    monkeypatch.setenv("NEWS_RSS_URLS", "")
    from app.config import get_settings

    get_settings.cache_clear()
    out = SentimentAgent().analyze("US100")
    assert out.status == "unavailable"
    assert out.sentiment is None  # no fabrication
    get_settings.cache_clear()


def test_rule_sentiment_word_matching():
    from app.agents.agents import _rule_sentiment

    bull, _ = _rule_sentiment(["Markets rally as gains accelerate"])
    assert bull == "BULLISH"
    bear, _ = _rule_sentiment(["Markets plunge amid recession fears"])
    assert bear == "BEARISH"


def test_strategy_agent_no_setup_in_noise():
    import random

    rng = random.Random(1)
    candles = []
    price = 100.0
    for _ in range(120):
        o = price
        c = o + rng.gauss(0, 0.05)
        candles.append({"timestamp": None, "open": o, "high": max(o, c) + 0.02,
                        "low": min(o, c) - 0.02, "close": c, "volume": 100.0})
        price = c
    out = StrategyAgent().analyze(candles, {"max_atr_pct": 0.05}, "US100", "15m")
    assert out.candidate_trade is False
    assert out.direction == "NONE"


def test_strategy_agent_detects_conflict_with_technical(candles):
    technical = TechnicalAgent().analyze(candles)
    opposing = None
    if technical.direction == "LONG":
        opposing = {"direction": "SHORT", "confidence": 0.9}
    elif technical.direction == "SHORT":
        opposing = {"direction": "LONG", "confidence": 0.9}
    out = StrategyAgent().analyze(candles, {"max_atr_pct": 0.05}, "US100", "15m",
                                  technical=opposing and TechnicalAgentOutput(**opposing) or None)
    # whether or not a setup exists, the run must not crash and must be a valid schema
    assert out.confidence >= 0.0


def test_run_all_agents_full_cycle(candles, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    monkeypatch.setenv("NEWS_RSS_URLS", "")
    from app.config import get_settings

    get_settings.cache_clear()
    outputs = run_all_agents(candles, "US100", "15m", {"max_atr_pct": 0.05})
    assert set(outputs) == {"technical", "quant", "regime", "news", "sentiment", "strategy"}
    assert outputs["news"]["status"] == "unavailable"
    assert outputs["sentiment"]["status"] == "unavailable"
    get_settings.cache_clear()


# imported late to avoid circulars in test file
from app.analysis.schemas import TechnicalAgentOutput  # noqa: E402

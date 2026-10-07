"""Trading agents (Milestones 8-12).

Agents receive structured, deterministic data and produce strict Pydantic
outputs. They NEVER execute orders and NEVER modify risk limits.

LLM policy (cost + safety):
- QuantAgent / RegimeAgent / TechnicalAgent: deterministic by default. The
  LLM may add interpretation only when llm_call_mode is ALWAYS or ON_SIGNAL
  and a key is configured; its output is merged as text, never as numbers.
- NewsAgent / SentimentAgent: LLM used for sentiment interpretation when
  configured; otherwise rule-based on real headlines only.
- StrategyAgent: deterministic evaluation + optional LLM narrative.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

import numpy as np
from pydantic import BaseModel

from app.analysis import indicators as ta
from app.analysis.schemas import (
    NewsAgentOutput,
    QuantAgentOutput,
    RegimeAgentOutput,
    SentimentAgentOutput,
    StrategyAgentOutput,
    StrategySignal,
    TechnicalAgentOutput,
)
from app.analysis.strategy import evaluate_setup
from app.config import get_settings
from app.services.llm import llm_client, llm_available
from app.services.news import news_service

logger = logging.getLogger("trading.agents")


def _wants_llm(stage: str = "signal") -> bool:
    """Decide whether an LLM call is warranted. Deterministic-first policy."""
    s = get_settings()
    if not s.llm_configured:
        return False
    mode = s.llm_call_mode_value
    if mode == "ALWAYS":
        return True
    if mode == "MANUAL":
        return False
    if mode == "ON_SIGNAL":
        return stage == "signal"
    if mode == "ON_CONFLICT":
        return stage == "conflict"
    return False


# ------------------------------------------------------------------------------
# QuantAgent (Milestone 8)
# ------------------------------------------------------------------------------
class QuantAgent:
    name = "quant"

    def analyze(self, candles: list[dict[str, Any]]) -> QuantAgentOutput:
        if len(candles) < 30:
            return QuantAgentOutput(direction="NEUTRAL", confidence=0.0,
                                    risk_flags=["insufficient_data"], supporting_metrics={})
        closes = ta._closes(candles)
        rets = np.diff(closes) / closes[:-1]
        vol = float(np.std(rets) * np.sqrt(252)) if len(rets) > 2 else None
        mom10 = ta.momentum(closes, 10)[-1]
        rsi14 = ta.rsi(closes, 14)[-1]
        metrics: dict[str, Any] = {
            "volatility_annualized": round(vol, 4) if vol is not None else None,
            "momentum_10": float(mom10) if not np.isnan(mom10) else None,
            "rsi14": float(rsi14) if not np.isnan(rsi14) else None,
            "candle_count": len(candles),
        }

        score = 0.0
        risk_flags: list[str] = []
        if metrics["momentum_10"] is not None:
            score += 0.4 if metrics["momentum_10"] > 0 else -0.4
        if metrics["rsi14"] is not None:
            if metrics["rsi14"] > 70:
                score -= 0.2
                risk_flags.append("rsi_overbought")
            elif metrics["rsi14"] < 30:
                score += 0.2
                risk_flags.append("rsi_oversold")
            else:
                score += 0.2 if metrics["rsi14"] > 55 else (-0.2 if metrics["rsi14"] < 45 else 0.0)
        if vol is not None and vol > 0.6:
            risk_flags.append("high_volatility")
            score *= 0.7

        direction = "LONG" if score > 0.15 else ("SHORT" if score < -0.15 else "NEUTRAL")
        confidence = min(0.85, abs(score) + 0.2) if direction != "NEUTRAL" else 0.3

        # Optional LLM interpretation of the calculated statistics (never numbers)
        if _wants_llm("signal"):
            interp = llm_client.ask_structured(
                "Interpret these trading statistics briefly: " + str(metrics),
                _Interpretation,
            )
            if interp is not None:
                metrics["llm_interpretation"] = interp.note

        return QuantAgentOutput(direction=direction, confidence=round(confidence, 2),
                                expected_value=None, risk_flags=risk_flags,
                                supporting_metrics=metrics)


class _Interpretation(BaseModel):
    note: str


# ------------------------------------------------------------------------------
# TechnicalAgent (Milestone 9)
# ------------------------------------------------------------------------------
class TechnicalAgent:
    name = "technical"

    def analyze(self, candles: list[dict[str, Any]], swing_lookback: int = 3) -> TechnicalAgentOutput:
        if len(candles) < 30:
            return TechnicalAgentOutput(direction="NEUTRAL", confidence=0.0,
                                        reasons=["insufficient data"])
        snap = ta.market_structure_summary(candles, swing_lookback)
        trend, structure = snap["trend"], snap["structure"]
        reasons: list[str] = [
            f"trend={trend}", f"structure={structure}",
            f"rsi14={snap['rsi14']:.1f}" if snap["rsi14"] is not None else "rsi unavailable",
        ]
        invalidations: list[str] = []

        direction = "NEUTRAL"
        conf = 0.2
        if trend == "up" and "bullish" in (structure or ""):
            direction, conf = "LONG", 0.7
        elif trend == "down" and "bearish" in (structure or ""):
            direction, conf = "SHORT", 0.7
        elif trend == "up":
            direction, conf = "LONG", 0.45
        elif trend == "down":
            direction, conf = "SHORT", 0.45

        if snap["rsi14"] is not None:
            if snap["rsi14"] > 75:
                conf = min(conf, 0.35)
                invalidations.append("RSI overbought")
            elif snap["rsi14"] < 25:
                conf = min(conf, 0.35)
                invalidations.append("RSI oversold")

        entry_zone = None
        stop = target = None
        last_close = snap["last_close"]
        atr14 = snap["atr14"]
        if direction != "NEUTRAL" and atr14:
            if direction == "LONG":
                entry_zone = [last_close - 0.5 * atr14, last_close + 0.2 * atr14]
                stop = last_close - 1.5 * atr14
                target = last_close + 3.0 * atr14
            else:
                entry_zone = [last_close - 0.2 * atr14, last_close + 0.5 * atr14]
                stop = last_close + 1.5 * atr14
                target = last_close - 3.0 * atr14

        if _wants_llm("signal"):
            interp = llm_client.ask_structured(
                "One-sentence technical read: trend=" + str(trend) + " structure=" + str(structure),
                _Interpretation,
            )
            if interp:
                reasons.append("llm: " + interp.note)

        return TechnicalAgentOutput(
            direction=direction, confidence=round(conf, 2), trend=trend, structure=structure,
            entry_zone=entry_zone, stop=stop, target=target, reasons=reasons, invalidations=invalidations,
        )


# ------------------------------------------------------------------------------
# RegimeAgent (Milestone 10)
# ------------------------------------------------------------------------------
class RegimeAgent:
    name = "regime"

    def analyze(self, candles: list[dict[str, Any]]) -> RegimeAgentOutput:
        if len(candles) < 40:
            return RegimeAgentOutput(regime="UNCERTAIN", confidence=0.0,
                                     evidence=["insufficient data"], risk_flags=["insufficient_data"])
        closes = ta._closes(candles)
        ema20, ema50 = ta.ema(closes, 20), ta.ema(closes, 50)
        atr14 = ta.atr(candles, 14)
        evidence: list[str] = []
        risk_flags: list[str] = []

        e20 = float(ema20[-1]) if not np.isnan(ema20[-1]) else None
        e50 = float(ema50[-1]) if not np.isnan(ema50[-1]) else None
        a = float(atr14[-1]) if not np.isnan(atr14[-1]) else None
        a_hist = atr14[~np.isnan(atr14)]
        atr_ratio = float(a / np.mean(a_hist)) if a and len(a_hist) > 5 and np.mean(a_hist) > 0 else 1.0

        trending = False
        if e20 and e50:
            sep = abs(e20 - e50) / e50
            trending = sep > 0.004
            evidence.append(f"ema20/ema50 separation {sep:.4f}")
        vol_state = "HIGH_VOLATILITY" if atr_ratio > 1.4 else ("LOW_VOLATILITY" if atr_ratio < 0.7 else "normal")
        evidence.append(f"ATR vs its mean: {atr_ratio:.2f}")
        if vol_state == "HIGH_VOLATILITY":
            risk_flags.append("volatility_spike")

        if trending and vol_state != "HIGH_VOLATILITY":
            regime, conf = "TRENDING", 0.7
        elif vol_state == "HIGH_VOLATILITY":
            regime, conf = "HIGH_VOLATILITY", 0.65
        elif vol_state == "LOW_VOLATILITY" and not trending:
            regime, conf = "RANGING", 0.6
        elif trending:
            regime, conf = "TRENDING", 0.55
        else:
            regime, conf = "RANGING", 0.5

        # RISK_ON / RISK_OFF heuristic from medium-term drift vs volatility
        if len(closes) > 60:
            drift = (closes[-1] - closes[-60]) / closes[-60]
            base_vol = float(np.std((closes[-60:] / closes[-61:-1]))) if len(closes) > 61 else 0.0
            if drift > max(0.02, 3 * base_vol):
                regime, conf = "RISK_ON", min(0.7, conf)
                evidence.append(f"60-bar drift {drift:+.3f}")
            elif drift < -max(0.02, 3 * base_vol):
                regime, conf = "RISK_OFF", min(0.7, conf)
                evidence.append(f"60-bar drift {drift:+.3f}")

        return RegimeAgentOutput(regime=regime, confidence=round(conf, 2),
                                 evidence=evidence, risk_flags=risk_flags)


# ------------------------------------------------------------------------------
# NewsAgent / SentimentAgent (Milestone 11)
# ------------------------------------------------------------------------------
_BULL_WORDS = {"beat", "beats", "rally", "surge", "gain", "gains", "rise", "rises", "jump", "jumps",
               "record", "strong", "growth", "optimism", "upgrade", "upbeat", "soar", "soars"}
_BEAR_WORDS = {"miss", "misses", "fall", "falls", "drop", "drops", "plunge", "plunges", "weak",
               "recession", "fear", "fears", "downgrade", "slump", "crisis", "warns", "sink", "sinks"}


def _rule_sentiment(headlines: list[str]) -> tuple[str, float]:
    score = 0
    for h in headlines:
        words = set(h.lower().split())
        score += len(words & _BULL_WORDS) - len(words & _BEAR_WORDS)
    if score > 0:
        return "BULLISH", min(0.8, 0.4 + 0.05 * score)
    if score < 0:
        return "BEARISH", min(0.8, 0.4 + 0.05 * abs(score))
    return "NEUTRAL", 0.3


class NewsAgent:
    name = "news"

    def analyze(self, symbol: str = "") -> NewsAgentOutput:
        if not news_service.configured():
            return NewsAgentOutput(status="unavailable", confidence=0.0,
                                   summary="no news provider configured", risk_flags=[])
        feed = news_service.get_recent_news()
        if feed["status"] != "ok" or not feed["items"]:
            return NewsAgentOutput(status=feed["status"] if feed["status"] in {"ok", "error", "unavailable"} else "unavailable",
                                   confidence=0.0, summary="news unavailable", risk_flags=["news_unavailable"])
        headlines = [item["headline"] for item in feed["items"]]
        sentiment, conf = _rule_sentiment(headlines)

        if _wants_llm("signal"):
            llm_out = llm_client.ask_structured(
                "Headlines:\n" + "\n".join(headlines[:20]) + "\n\nOverall sentiment for trading?",
                _SentimentChoice,
            )
            if llm_out is not None:
                sentiment, conf = llm_out.sentiment, llm_out.confidence

        flags = []
        low_words = ("war", "crisis", "crash", "emergency", "default")
        if any(any(w in h.lower() for w in low_words) for h in headlines):
            flags.append("high_impact_headlines")
        return NewsAgentOutput(status="ok", sentiment=sentiment, confidence=round(conf, 2),
                               headline_count=len(headlines),
                               summary=f"{len(headlines)} recent headlines analyzed",
                               risk_flags=flags)


class _SentimentChoice(BaseModel):
    sentiment: str
    confidence: float


class SentimentAgent:
    name = "sentiment"

    def analyze(self, symbol: str = "") -> SentimentAgentOutput:
        if not news_service.configured():
            return SentimentAgentOutput(status="unavailable", confidence=0.0, source="none",
                                        risk_flags=["sentiment_unavailable"])
        feed = news_service.get_recent_news()
        if feed["status"] != "ok" or not feed["items"]:
            return SentimentAgentOutput(status=feed["status"] if feed["status"] in {"ok", "error"} else "unavailable",
                                        confidence=0.0, source="none", risk_flags=["sentiment_unavailable"])
        headlines = [item["headline"] for item in feed["items"]]
        sentiment, conf = _rule_sentiment(headlines)
        return SentimentAgentOutput(status="ok", sentiment=sentiment, confidence=round(conf, 2),
                                    source="rss_rule_based", risk_flags=[])


# ------------------------------------------------------------------------------
# StrategyAgent (Milestone 12)
# ------------------------------------------------------------------------------
class StrategyAgent:
    name = "strategy"

    def analyze(self, candles: list[dict[str, Any]], params: dict[str, Any] | None = None,
                symbol: str = "", timeframe: str = "",
                technical: TechnicalAgentOutput | None = None,
                quant: QuantAgentOutput | None = None,
                regime: RegimeAgentOutput | None = None,
                news: NewsAgentOutput | None = None,
                sentiment: SentimentAgentOutput | None = None) -> StrategyAgentOutput:
        signal: StrategySignal = evaluate_setup(candles, params, symbol, timeframe)
        reasons = list(signal.reasons)
        conflicts: list[str] = []
        invalidations = list(signal.invalidations)

        if not signal.setup_valid:
            return StrategyAgentOutput(candidate_trade=False, direction="NONE", confidence=signal.confidence,
                                       reasons=reasons, conflicts=conflicts, invalidation_conditions=invalidations)

        conflicts = self._agent_conflicts(signal.direction, technical, quant, regime, news, sentiment)
        # conflicts reduce confidence; hard conflicts invalidate the trade
        hard = [c for c in conflicts if c.startswith("HARD")]
        conf = signal.confidence * (0.7 ** len([c for c in conflicts if not c.startswith("HARD")]))
        if hard:
            return StrategyAgentOutput(candidate_trade=False, direction=signal.direction,
                                       entry=signal.entry, stop=signal.stop_loss, target=signal.take_profit,
                                       risk_reward=signal.risk_reward, confidence=round(conf, 2),
                                       reasons=reasons + ["hard agent conflict"], conflicts=conflicts,
                                       invalidation_conditions=invalidations)

        if _wants_llm("signal"):
            interp = llm_client.ask_structured(
                "Setup: " + "; ".join(reasons[:6]) + ". Any reason to skip this trade?",
                _Interpretation,
            )
            if interp:
                reasons.append("llm: " + interp.note)

        return StrategyAgentOutput(
            candidate_trade=True, direction=signal.direction, entry=signal.entry,
            stop=signal.stop_loss, target=signal.take_profit, risk_reward=signal.risk_reward,
            confidence=round(min(0.9, conf), 2), reasons=reasons, conflicts=conflicts,
            invalidation_conditions=invalidations,
        )

    @staticmethod
    def _agent_conflicts(direction: str, technical: TechnicalAgentOutput | None,
                         quant: QuantAgentOutput | None, regime: RegimeAgentOutput | None,
                         news: NewsAgentOutput | None, sentiment: SentimentAgentOutput | None) -> list[str]:
        conflicts: list[str] = []
        wanted = "LONG" if direction == "LONG" else "SHORT"
        against = "SHORT" if direction == "LONG" else "LONG"
        if technical and technical.direction == against and technical.confidence > 0.5:
            conflicts.append(f"SOFT: technical agent favors {against} ({technical.confidence:.2f})")
        if quant and quant.direction == against and quant.confidence > 0.5:
            conflicts.append(f"SOFT: quant agent favors {against} ({quant.confidence:.2f})")
        if regime:
            if regime.regime == "HIGH_VOLATILITY":
                conflicts.append("HARD: high-volatility regime")
            if regime.regime == "RISK_OFF" and direction == "LONG":
                conflicts.append("SOFT: risk-off regime against long")
            if regime.regime == "RISK_ON" and direction == "SHORT":
                conflicts.append("SOFT: risk-on regime against short")
        if news and news.status == "ok":
            if news.sentiment == ("BEARISH" if direction == "LONG" else "BULLISH") and news.confidence > 0.6:
                conflicts.append("SOFT: news sentiment against setup")
            if "high_impact_headlines" in news.risk_flags:
                conflicts.append("HARD: high-impact news in flight")
        if sentiment and sentiment.status == "ok":
            if sentiment.sentiment == ("BEARISH" if direction == "LONG" else "BULLISH") and sentiment.confidence > 0.7:
                conflicts.append("SOFT: sentiment against setup")
        return conflicts


def run_all_agents(candles: list[dict[str, Any]], symbol: str, timeframe: str,
                   strategy_params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Convenience orchestrator returning every agent output as dicts."""
    tech = TechnicalAgent().analyze(candles)
    quant = QuantAgent().analyze(candles)
    regime = RegimeAgent().analyze(candles)
    news = NewsAgent().analyze(symbol)
    sentiment = SentimentAgent().analyze(symbol)
    strategy = StrategyAgent().analyze(candles, strategy_params, symbol, timeframe,
                                       technical=tech, quant=quant, regime=regime,
                                       news=news, sentiment=sentiment)
    return {
        "technical": tech.model_dump(),
        "quant": quant.model_dump(),
        "regime": regime.model_dump(),
        "news": news.model_dump(),
        "sentiment": sentiment.model_dump(),
        "strategy": strategy.model_dump(),
    }

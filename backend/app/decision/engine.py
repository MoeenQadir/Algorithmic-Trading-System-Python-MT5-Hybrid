"""Decision engine (Milestone 13).

Configurable weighted evidence - never simple majority vote. The LLM can add
narrative but cannot bypass deterministic validation. If required inputs are
missing or conflicts are irresolvable: fail safe -> WAIT.
"""
from __future__ import annotations

import logging
from typing import Any

from app.analysis.schemas import DecisionOutput
from app.config import get_settings

logger = logging.getLogger("trading.decision")


def _dir_of(agent: dict[str, Any]) -> str:
    return str(agent.get("direction") or "NEUTRAL")


def _conf_of(agent: dict[str, Any]) -> float:
    try:
        return float(agent.get("confidence") or 0.0)
    except (TypeError, ValueError):
        return 0.0


class DecisionEngine:
    name = "decision"

    def weights(self) -> dict[str, float]:
        s = get_settings()
        return {
            "technical": max(0.0, s.technical_weight),
            "quant": max(0.0, s.quant_weight),
            "strategy": max(0.0, s.strategy_weight),
            "regime": max(0.0, s.regime_weight),
            "news": max(0.0, s.news_weight),
            "sentiment": max(0.0, s.sentiment_weight),
        }

    def decide(self, agent_outputs: dict[str, Any], symbol: str, timeframe: str) -> DecisionOutput:
        s = get_settings()
        tech = agent_outputs.get("technical") or {}
        quant = agent_outputs.get("quant") or {}
        strategy = agent_outputs.get("strategy") or {}
        regime = agent_outputs.get("regime") or {}
        news = agent_outputs.get("news") or {}
        sentiment = agent_outputs.get("sentiment") or {}

        weights = self.weights()
        evidence: dict[str, Any] = {"weights": weights, "agents": {}}
        conflicts: list[str] = []

        score = 0.0
        total_weight = 0.0
        # voters: technical + quant. The strategy agent is the GATE (it defines
        # the setup and levels) - it is not double-counted as a voter.
        for name, agent, weight in (
            ("technical", tech, weights["technical"]),
            ("quant", quant, weights["quant"]),
        ):
            d = _dir_of(agent)
            c = _conf_of(agent)
            evidence["agents"][name] = {"direction": d, "confidence": c, "weight": weight}
            if d == "NEUTRAL" or weight <= 0:
                continue
            signed = c * weight * (1 if d == "LONG" else -1)
            score += signed
            total_weight += weight

        # regime/news/sentiment act as modifiers, not voters
        r_dir = str(regime.get("regime") or "")
        r_conf = _conf_of(regime)
        if r_dir in {"TRENDING", "RISK_ON"}:
            evidence["agents"]["regime"] = {"regime": r_dir, "confidence": r_conf}
            score *= 1.0 + 0.15 * r_conf
        elif r_dir in {"RISK_OFF", "HIGH_VOLATILITY"}:
            evidence["agents"]["regime"] = {"regime": r_dir, "confidence": r_conf}
            score *= 1.0 - 0.25 * r_conf
        for key, agent in (("news", news), ("sentiment", sentiment)):
            if agent.get("status") == "ok" and agent.get("sentiment"):
                sgn = 1 if agent["sentiment"] == "BULLISH" else (-1 if agent["sentiment"] == "BEARISH" else 0)
                w = weights[key] * _conf_of(agent)
                score += sgn * w * 0.5
                evidence["agents"][key] = {"sentiment": agent["sentiment"], "confidence": agent.get("confidence")}

        # strategy candidate is required for BUY/SELL
        candidate = bool(strategy.get("candidate_trade"))
        strategy_dir = _dir_of(strategy)
        strategy_conf = _conf_of(strategy)

        normalized = score / total_weight if total_weight > 0 else 0.0
        # setup strength (strategy confidence + weight) scales overall confidence
        strategy_conf = _conf_of(strategy)
        setup_factor = 0.7 + 0.3 * min(1.0, strategy_conf * max(0.0, weights["strategy"]))
        confidence = min(0.95, abs(normalized) * setup_factor)
        if confidence < s.decision_threshold or not candidate:
            reason = (
                f"weighted score {normalized:+.3f} below threshold {s.decision_threshold}"
                if confidence < s.decision_threshold
                else "strategy reports no valid setup"
            )
            if strategy.get("conflicts"):
                conflicts.extend(strategy["conflicts"])
            return DecisionOutput(action="WAIT", direction="NONE", confidence=round(confidence, 2),
                                  evidence=evidence, agent_conflicts=conflicts, decision_reason=reason)

        direction = strategy_dir
        if direction not in {"LONG", "SHORT"}:
            return DecisionOutput(action="WAIT", direction="NONE", confidence=round(confidence, 2),
                                  evidence=evidence, agent_conflicts=conflicts,
                                  decision_reason="strategy direction unclear")

        entry, stop, target = strategy.get("entry"), strategy.get("stop"), strategy.get("target")
        if entry is None or stop is None or target is None or entry <= 0 or stop <= 0 or target <= 0:
            return DecisionOutput(action="WAIT", direction="NONE", confidence=round(confidence, 2),
                                  evidence=evidence, agent_conflicts=conflicts,
                                  decision_reason="incomplete entry/stop/target - fail safe WAIT")

        rr = abs(target - entry) / abs(entry - stop) if abs(entry - stop) > 0 else 0.0
        if rr < s.min_risk_reward:
            conflicts.append(f"RR {rr:.2f} below configured minimum {s.min_risk_reward}")
            return DecisionOutput(action="REJECT", direction=direction, confidence=round(confidence, 2),
                                  entry=entry, stop_loss=stop, take_profit=target, risk_reward=round(rr, 2),
                                  evidence=evidence, agent_conflicts=conflicts,
                                  decision_reason="risk/reward below minimum")

        if strategy_dir == "LONG" and score < 0:
            conflicts.append("direction conflict: strategy long but weighted evidence bearish")
            return DecisionOutput(action="WAIT", direction="NONE", confidence=round(confidence, 2),
                                  evidence=evidence, agent_conflicts=conflicts,
                                  decision_reason="evidence contradicts setup direction - WAIT")

        if strategy_dir == "SHORT" and score > 0:
            conflicts.append("direction conflict: strategy short but weighted evidence bullish")
            return DecisionOutput(action="WAIT", direction="NONE", confidence=round(confidence, 2),
                                  evidence=evidence, agent_conflicts=conflicts,
                                  decision_reason="evidence contradicts setup direction - WAIT")

        action = "BUY" if direction == "LONG" else "SELL"
        return DecisionOutput(
            action=action, direction=direction, confidence=round(confidence, 2),
            entry=entry, stop_loss=stop, take_profit=target, risk_reward=round(rr, 2),
            evidence=evidence, agent_conflicts=conflicts,
            decision_reason=f"weighted evidence {normalized:+.3f} supports {direction} with valid setup",
        )


decision_engine = DecisionEngine()

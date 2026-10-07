"""Shared Pydantic schemas (strict, validated) for agent outputs.

Every LLM response is parsed into one of these schemas; malformed responses
are rejected, never trusted.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class StrategySignal(BaseModel):
    """Output of the strategy engine (Milestone 7)."""

    symbol: str
    timeframe: str
    direction: Literal["LONG", "SHORT", "NONE"]
    setup_valid: bool
    entry: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    risk_reward: Optional[float] = None
    confidence: float = Field(ge=0.0, le=1.0, default=0.0)
    reasons: list[str] = []
    invalidations: list[str] = []
    timestamp: Optional[datetime] = None


class QuantAgentOutput(BaseModel):
    """Milestone 8 strict schema."""

    direction: Literal["LONG", "SHORT", "NEUTRAL"]
    confidence: float = Field(ge=0.0, le=1.0)
    expected_value: Optional[float] = None
    risk_flags: list[str] = []
    supporting_metrics: dict[str, Any] = {}


class TechnicalAgentOutput(BaseModel):
    """Milestone 9 strict schema."""

    direction: Literal["LONG", "SHORT", "NEUTRAL"]
    confidence: float = Field(ge=0.0, le=1.0)
    trend: str = "unknown"
    structure: str = "unknown"
    entry_zone: Optional[list[float]] = None
    stop: Optional[float] = None
    target: Optional[float] = None
    reasons: list[str] = []
    invalidations: list[str] = []


class RegimeAgentOutput(BaseModel):
    """Milestone 10 strict schema."""

    regime: Literal["TRENDING", "RANGING", "HIGH_VOLATILITY", "LOW_VOLATILITY", "RISK_ON", "RISK_OFF", "UNCERTAIN"]
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[str] = []
    risk_flags: list[str] = []


class NewsAgentOutput(BaseModel):
    """Milestone 11 strict schema. status=unavailable means: no fake data."""

    status: Literal["ok", "unavailable", "error"]
    sentiment: Optional[Literal["BULLISH", "BEARISH", "NEUTRAL"]] = None
    confidence: float = Field(ge=0.0, le=1.0, default=0.0)
    headline_count: int = 0
    summary: str = ""
    risk_flags: list[str] = []


class SentimentAgentOutput(BaseModel):
    status: Literal["ok", "unavailable", "error"]
    sentiment: Optional[Literal["BULLISH", "BEARISH", "NEUTRAL"]] = None
    confidence: float = Field(ge=0.0, le=1.0, default=0.0)
    source: str = "none"
    risk_flags: list[str] = []


class StrategyAgentOutput(BaseModel):
    """Milestone 12 strict schema."""

    candidate_trade: bool
    direction: Literal["LONG", "SHORT", "NONE"]
    entry: Optional[float] = None
    stop: Optional[float] = None
    target: Optional[float] = None
    risk_reward: Optional[float] = None
    confidence: float = Field(ge=0.0, le=1.0, default=0.0)
    reasons: list[str] = []
    conflicts: list[str] = []
    invalidation_conditions: list[str] = []


class DecisionOutput(BaseModel):
    """Milestone 13 strict schema."""

    action: Literal["BUY", "SELL", "WAIT", "REJECT"]
    direction: Literal["LONG", "SHORT", "NONE"]
    confidence: float = Field(ge=0.0, le=1.0)
    entry: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    risk_reward: Optional[float] = None
    evidence: dict[str, Any] = {}
    agent_conflicts: list[str] = []
    decision_reason: str = ""


class RiskEngineOutput(BaseModel):
    """Milestone 14/15 strict schema."""

    verdict: Literal["APPROVED", "REJECTED", "REDUCED_SIZE", "WAIT"]
    violations: list[str] = []
    position_size: Optional[float] = None
    risk_amount: Optional[float] = None
    quantity: Optional[float] = None
    notional: Optional[float] = None
    max_loss: Optional[float] = None
    details: dict[str, Any] = {}

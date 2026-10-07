"""Trade journal (Milestone 19) and agent performance tracking (Milestone 18).

Every trade records the full audit trail: decision, agent outputs, risk
checks, execution info, exit, P&L, R multiple, reason.
Agent predictions are stored per decision and evaluated against trade
outcomes; statistics require a configurable minimum sample size.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.database import AgentPerformance, AgentSignal, RiskCheck, Trade, TradingDecision
from app.utils.timeutils import utcnow

logger = logging.getLogger("trading.journal")


class JournalService:
    def record_decision(self, db: Session, decision: dict[str, Any], agent_outputs: dict[str, Any],
                        symbol: str, timeframe: str) -> TradingDecision:
        row = TradingDecision(
            symbol=symbol, timeframe=timeframe,
            action=decision.get("action", "WAIT"),
            direction=decision.get("direction", "NONE"),
            confidence=float(decision.get("confidence") or 0.0),
            entry=decision.get("entry"), stop_loss=decision.get("stop_loss"),
            take_profit=decision.get("take_profit"), risk_reward=decision.get("risk_reward"),
            evidence=json.dumps(decision.get("evidence") or {}, default=str),
            agent_conflicts=json.dumps(decision.get("agent_conflicts") or []),
            decision_reason=str(decision.get("decision_reason") or ""),
        )
        db.add(row)
        db.flush()
        for name, output in agent_outputs.items():
            if not isinstance(output, dict):
                continue
            direction = output.get("direction") or output.get("sentiment") or output.get("regime") or "NEUTRAL"
            db.add(AgentSignal(
                agent_name=name, symbol=symbol, timeframe=timeframe,
                direction=str(direction)[:10],
                confidence=float(output.get("confidence") or 0.0),
                payload=json.dumps(output, default=str),
                llm_used=bool(output.get("llm_used", False)),
                decision_id=row.id,
            ))
        db.flush()
        return row

    def record_risk_check(self, db: Session, decision_id: Optional[int], symbol: str,
                          risk_output: Any) -> RiskCheck:
        violations = getattr(risk_output, "violations", None) or (risk_output.get("violations") if isinstance(risk_output, dict) else [])
        verdict = getattr(risk_output, "verdict", None) or (risk_output.get("verdict") if isinstance(risk_output, dict) else "WAIT")
        row = RiskCheck(
            decision_id=decision_id, symbol=symbol, verdict=str(verdict),
            violations=json.dumps(violations or []),
            position_size=getattr(risk_output, "position_size", None),
            risk_amount=getattr(risk_output, "risk_amount", None),
            details=json.dumps(getattr(risk_output, "details", {}) or {}, default=str),
        )
        db.add(row)
        db.flush()
        return row

    def attach_audit_to_trade(self, db: Session, trade: Trade, decision_id: Optional[int],
                              agent_outputs: dict[str, Any], risk_output: Any,
                              execution_info: dict[str, Any]) -> None:
        trade.decision_id = decision_id
        trade.agent_outputs = json.dumps(agent_outputs, default=str)
        if risk_output is not None:
            trade.risk_checks = json.dumps({
                "verdict": getattr(risk_output, "verdict", None),
                "violations": getattr(risk_output, "violations", []),
                "risk_amount": getattr(risk_output, "risk_amount", None),
                "quantity": getattr(risk_output, "quantity", None),
            }, default=str)
        trade.execution_info = json.dumps(execution_info, default=str)
        db.flush()

    def record_agent_predictions(self, db: Session, decision_id: int, symbol: str,
                                 agent_outputs: dict[str, Any], direction: str) -> None:
        for name, output in agent_outputs.items():
            if not isinstance(output, dict):
                continue
            pred = output.get("direction")
            if pred not in {"LONG", "SHORT", "NEUTRAL"}:
                continue
            db.add(AgentPerformance(
                agent_name=name, symbol=symbol, timestamp=utcnow(),
                prediction=str(pred), confidence=float(output.get("confidence") or 0.0),
                decision_id=decision_id,
            ))
        db.flush()


class AgentPerformanceService:
    def evaluate_open_predictions(self, db: Session) -> int:
        """Match closed trades to stored predictions and record outcomes."""
        evaluated = 0
        pending = db.query(AgentPerformance).filter(AgentPerformance.outcome.is_(None)).all()
        for rec in pending:
            trade = None
            if rec.trade_id:
                trade = db.get(Trade, rec.trade_id)
            if trade is None and rec.decision_id:
                trade = (
                    db.query(Trade)
                    .filter(Trade.decision_id == rec.decision_id, Trade.status == "closed")
                    .order_by(Trade.exit_time.desc())
                    .first()
                )
            if trade is None:
                continue
            if trade.direction == rec.prediction:
                rec.outcome = "correct" if (trade.pnl or 0) > 0 else "neutral" if (trade.pnl or 0) == 0 else "incorrect"
            elif rec.prediction == "NEUTRAL":
                rec.outcome = "neutral"
            else:
                rec.outcome = "correct" if (trade.pnl or 0) < 0 else "incorrect"
            rec.pnl_contribution = trade.pnl
            rec.r_contribution = trade.r_multiple
            rec.evaluated_at = utcnow()
            evaluated += 1
        db.flush()
        return evaluated

    def summary(self, db: Session) -> list[dict[str, Any]]:
        s = get_settings()
        min_sample = s.min_agent_sample_size
        out: list[dict[str, Any]] = []
        rows = db.query(AgentPerformance).all()
        by_agent: dict[str, list[AgentPerformance]] = {}
        for r in rows:
            by_agent.setdefault(r.agent_name, []).append(r)
        for agent, recs in by_agent.items():
            evaluated = [r for r in recs if r.outcome is not None]
            n = len(evaluated)
            correct = sum(1 for r in evaluated if r.outcome == "correct")
            incorrect = sum(1 for r in evaluated if r.outcome == "incorrect")
            neutral = n - correct - incorrect
            fp = sum(1 for r in evaluated if r.outcome == "incorrect" and r.prediction != "NEUTRAL")
            fn = sum(1 for r in evaluated if r.outcome == "incorrect" and r.prediction == "NEUTRAL")
            rs = [r.r_contribution for r in evaluated if r.r_contribution is not None]
            out.append({
                "agent_name": agent,
                "predictions": len(recs),
                "evaluated": n,
                "accuracy": round(correct / n, 4) if n else None,
                "false_positive_rate": round(fp / n, 4) if n else None,
                "false_negative_rate": round(fn / n, 4) if n else None,
                "historical_expectancy_r": round(sum(rs) / len(rs), 4) if rs else None,
                "sample_reliable": n >= min_sample,
                "min_sample_size": min_sample,
                "note": None if n >= min_sample else f"sample {n} < {min_sample}: statistics not yet reliable; weights unchanged",
            })
        return out


journal_service = JournalService()
agent_performance_service = AgentPerformanceService()

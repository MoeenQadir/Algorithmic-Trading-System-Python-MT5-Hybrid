"""Trading pipeline orchestrator: DATA -> ANALYSIS -> AGENTS -> DECISION ->
RISK -> EXECUTION -> JOURNAL -> PERFORMANCE.

Fail-safe policy everywhere: missing data, unavailable providers or failed
risk checks never crash the loop; they produce WAIT and a clear status.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.agents.agents import run_all_agents
from app.backtesting.engine import BacktestEngine
from app.config import get_settings
from app.data.providers import get_provider, parse_timeframe_minutes
from app.decision.engine import decision_engine
from app.execution.broker import get_broker
from app.models.database import MarketData, PortfolioSnapshot, Trade
from app.risk.engine import risk_engine
from app.services.journal import agent_performance_service, journal_service
from app.utils.timeutils import utcnow

logger = logging.getLogger("trading.pipeline")


class TradingPipeline:
    def get_candles(self, symbol: str, timeframe: str, limit: int = 400) -> list[dict[str, Any]]:
        """Candles from the database first (offline-first), provider as fallback."""
        db: Session
        from app.models.database import get_session_factory

        SessionLocal = get_session_factory()
        db = SessionLocal()
        try:
            rows = (
                db.query(MarketData)
                .filter(MarketData.symbol == symbol.upper(), MarketData.timeframe == timeframe)
                .order_by(MarketData.timestamp.desc())
                .limit(limit)
                .all()
            )
            candles = [
                {
                    "timestamp": r.timestamp, "symbol": r.symbol, "timeframe": r.timeframe,
                    "open": r.open, "high": r.high, "low": r.low, "close": r.close,
                    "volume": r.volume, "bid": r.bid, "ask": r.ask, "spread": r.spread,
                    "source": r.source,
                }
                for r in reversed(rows)
            ]
        finally:
            db.close()

        if candles:
            return candles

        provider = get_provider()
        try:
            candles = provider.get_ohlcv(symbol, timeframe, limit=limit)
        except Exception as exc:
            logger.warning("provider fetch failed for %s %s: %s", symbol, timeframe, exc)
            provider_health_report("market_data", "DEGRADED", str(exc))
            return []
        if candles:
            self.persist_candles(candles)
        return candles

    def persist_candles(self, candles: list[dict[str, Any]]) -> int:
        from app.models.database import get_session_factory

        SessionLocal = get_session_factory()
        db = SessionLocal()
        added = 0
        try:
            existing = {
                (r.symbol, r.timeframe, r.timestamp)
                for r in db.query(MarketData).filter(
                    MarketData.symbol == candles[0]["symbol"].upper(),
                    MarketData.timeframe == candles[0]["timeframe"],
                ).all()
                if candles
            }
            for c in candles:
                key = (c["symbol"].upper(), c["timeframe"], c["timestamp"])
                if key in existing:
                    continue
                db.add(MarketData(
                    symbol=c["symbol"].upper(), timeframe=c["timeframe"], timestamp=c["timestamp"],
                    open=c["open"], high=c["high"], low=c["low"], close=c["close"],
                    volume=c.get("volume"), bid=c.get("bid"), ask=c.get("ask"),
                    spread=c.get("spread"), source=c.get("source") or "import",
                ))
                added += 1
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
        return added

    def last_candle_time(self, symbol: str, timeframe: str):
        from app.models.database import get_session_factory

        SessionLocal = get_session_factory()
        db = SessionLocal()
        try:
            row = (
                db.query(MarketData)
                .filter(MarketData.symbol == symbol.upper(), MarketData.timeframe == timeframe)
                .order_by(MarketData.timestamp.desc())
                .first()
            )
            return row.timestamp if row else None
        finally:
            db.close()

    def run_cycle(self, symbol: str | None = None, timeframe: str | None = None,
                  execute: bool = True) -> dict[str, Any]:
        """One full analysis cycle. Never raises: failures become WAIT status."""
        s = get_settings()
        symbol = (symbol or s.default_symbol).upper()
        timeframe = timeframe or s.default_timeframe
        result: dict[str, Any] = {"symbol": symbol, "timeframe": timeframe,
                                  "timestamp": utcnow().isoformat(), "status": "ok"}
        try:
            candles = self.get_candles(symbol, timeframe, limit=400)
            if len(candles) < 60:
                result.update({
                    "status": "WAIT",
                    "decision": {"action": "WAIT", "decision_reason": f"insufficient market data ({len(candles)} candles)"},
                    "agents": {},
                })
                return result

            agent_outputs = run_all_agents(candles, symbol, timeframe, None)
            decision = decision_engine.decide(agent_outputs, symbol, timeframe).model_dump()
            result["agents"] = agent_outputs
            result["decision"] = decision

            # persist decision + agent signals + predictions
            from app.models.database import get_session_factory

            SessionLocal = get_session_factory()
            db = SessionLocal()
            try:
                decision_row = journal_service.record_decision(db, decision, agent_outputs, symbol, timeframe)
                db.commit()
                decision_id = decision_row.id
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()

            # risk evaluation for actionable decisions
            if decision.get("action") in {"BUY", "SELL"} and execute:
                last_ts = candles[-1]["timestamp"]
                from app.models.database import get_session_factory as _gsf

                db2 = _gsf()()
                try:
                    broker = get_broker(db2)
                    account = PaperAccountReader().read()
                    risk_out = risk_engine.evaluate(
                        db2, {**decision, "symbol": symbol},
                        account_equity=account["equity"],
                        current_price=candles[-1]["close"],
                        spread=candles[-1].get("spread"),
                        last_candle_time=last_ts,
                    )
                    journal_service.record_risk_check(db2, decision_id, symbol, risk_out)
                    if risk_out.verdict in {"APPROVED", "REDUCED_SIZE"}:
                        side = "BUY" if decision["action"] == "BUY" else "SELL"
                        exec_result = broker.place_order(
                            symbol, side, risk_out.quantity or 0.0, "market",
                            stop_price=decision.get("stop_loss"),
                            take_profit=decision.get("take_profit"),
                            require_stops=True,  # Phase 39: strategy path must always carry SL/TP
                        )
                        trade = (
                            db2.query(Trade).filter(Trade.status == "open").order_by(Trade.entry_time.desc()).first()
                        )
                        if trade:
                            journal_service.attach_audit_to_trade(db2, trade, decision_id, agent_outputs, risk_out,
                                                                  exec_result)
                        journal_service.record_agent_predictions(db2, decision_id, symbol, agent_outputs,
                                                                 decision.get("direction", "NONE"))
                        result["execution"] = exec_result
                    result["risk"] = risk_out.model_dump()
                    db2.commit()
                except Exception:
                    db2.rollback()
                    raise
                finally:
                    db2.close()
            elif decision.get("action") in {"BUY", "SELL"}:
                result["risk"] = {"verdict": "SKIPPED", "violations": ["scheduled signal-only cycle"]}
                result["execution"] = {"status": "disabled", "reason": "signal-only cycle"}
            else:
                result["risk"] = {"verdict": "SKIPPED", "violations": ["decision not actionable"]}

            # stops/targets maintenance + snapshot
            from app.models.database import get_session_factory as _gsf

            broker_db = _gsf()()
            try:
                broker2 = get_broker(broker_db)
                broker2.check_stops_and_targets()
                self._snapshot(broker2)
                result["broker_stops_checked"] = True
            finally:
                broker_db.close()
        except Exception as exc:
            logger.exception("pipeline cycle failed")
            result["status"] = "ERROR"
            result["error"] = str(exc)[:300]
        return result

    def _snapshot(self, broker) -> None:
        from app.models.database import get_session_factory

        SessionLocal = get_session_factory()
        db = SessionLocal()
        try:
            account = broker.get_account()
            db.add(PortfolioSnapshot(
                equity=account["equity"], cash=account["cash"],
                open_positions=account["open_positions"],
                unrealized_pnl=account["unrealized_pnl"],
                realized_pnl=account["realized_pnl"],
            ))
            db.commit()
        finally:
            db.close()


class PaperAccountReader:
    """Reads account equity from open trades/cash without a live broker."""

    def read(self) -> dict[str, Any]:
        from app.models.database import get_session_factory

        SessionLocal = get_session_factory()
        db = SessionLocal()
        try:
            from app.execution.broker import PaperBroker

            broker = PaperBroker(db)
            return broker.get_account()
        finally:
            db.close()


def provider_health_report(component: str, status: str, message: str) -> None:
    from app.data.resilience import provider_health

    provider_health.report(component, status, message)

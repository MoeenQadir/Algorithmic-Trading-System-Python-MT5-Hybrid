"""REST API (Milestone 20).

Endpoints: /health /status /market/{symbol} /signals /agents /decisions
/positions /orders /trades /portfolio /performance /backtests
/backtests/run /paper/orders /paper/reset /config. No secrets exposed.
"""
from __future__ import annotations

import logging
from typing import Any, Optional
import json

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session as DBSession

from app.config import get_settings
from app.data.providers import get_provider, parse_timeframe_minutes
from app.data.service import get_historical_service
from app.models.database import (
    AgentSignal,
    Alert,
    BacktestRun,
    BacktestTrade,
    MarketData,
    Order,
    Position,
    SystemEvent,
    Trade,
    TradingDecision,
    WatchlistItem,
    get_db,
)
from app.services.pipeline import TradingPipeline
from app.utils.timeutils import utcnow, to_utc
from datetime import datetime, timezone

router = APIRouter()
logger = logging.getLogger("trading.api")
pipeline = TradingPipeline()


def _log_event(level: str, component: str, message: str, details: dict | None = None) -> None:
    db: DBSession
    from app.models.database import get_session_factory

    SessionLocal = get_session_factory()
    db = SessionLocal()
    try:
        import json

        db.add(SystemEvent(level=level, component=component, message=message[:500],
                           details=json.dumps(details or {}, default=str)))
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


# ------------------------------------------------------------------------------
# Health & status
# ------------------------------------------------------------------------------
@router.get("/health", tags=["system"])
def health():
    s = get_settings()
    return {
        "status": "ok",
        "service": "trading-backend",
        "paper_trading": s.paper_trading,
        "live_trading": s.live_trading,
        "env": s.env,
        "time": __import__("app.utils.timeutils", fromlist=["utcnow"]).utcnow().isoformat(),
        "llm_configured": s.llm_configured,
    }


@router.get("/status", tags=["system"])
def status(db: DBSession = Depends(get_db)):
    s = get_settings()
    from app.data.resilience import provider_health, rate_limit_manager

    open_positions = db.query(Position).filter(Position.status == "open").count()
    last_candle = (
        db.query(Trade).order_by(Trade.entry_time.desc()).first()
    )
    return {
        "paper_trading": s.paper_trading,
        "live_trading": s.live_trading,
        "default_symbol": s.default_symbol,
        "default_timeframe": s.default_timeframe,
        "live_signal_polling": s.market_data_provider.lower() == "twelvedata" and bool(s.twelve_data_api_key),
        "open_positions": open_positions,
        "last_trade_time": last_candle.entry_time.isoformat() if last_candle else None,
        "providers": {
            "market_data": rate_limit_manager.snapshot(s.market_data_provider),
            "openrouter": rate_limit_manager.snapshot("openrouter"),
            "news": rate_limit_manager.snapshot("news_rss"),
        },
        "component_health": provider_health.all(),
    }


@router.get("/health/detailed", tags=["system"])
def health_detailed(db: DBSession = Depends(get_db)):
    s = get_settings()
    from app.data.resilience import provider_health, rate_limit_manager
    from sqlalchemy import text
    from app.utils.timeutils import utcnow, to_utc
    from app.models.database import MarketData

    components: dict[str, dict[str, Any]] = {}

    # 1. Backend
    components["backend"] = {"status": "HEALTHY", "message": "FastAPI engine active"}

    # 2. Database
    try:
        db.execute(text("SELECT 1"))
        components["database"] = {"status": "HEALTHY", "message": "database connected and responsive"}
    except Exception as exc:
        components["database"] = {"status": "DOWN", "message": f"database error: {exc}"}

    # 3. Provider
    prov_name = s.market_data_provider.lower()
    if prov_name == "twelvedata":
        if not s.twelve_data_api_key:
            components["provider"] = {"status": "DISABLED", "message": "twelvedata api key missing (offline/local fallback)"}
        else:
            snap = rate_limit_manager.snapshot("twelvedata")
            if snap.get("rate_limited"):
                components["provider"] = {"status": "DEGRADED", "message": "twelvedata rate limited"}
            elif snap.get("status") == "OFFLINE":
                components["provider"] = {"status": "OFFLINE", "message": snap.get("last_error") or "provider offline"}
            else:
                components["provider"] = {"status": "HEALTHY", "message": "twelvedata ready"}
    elif prov_name in ("mt5", "local", "stooq"):
        components["provider"] = {"status": "HEALTHY", "message": f"provider={prov_name}"}
    else:
        components["provider"] = {"status": "OFFLINE", "message": f"unknown provider {prov_name}"}

    # 4. Market data freshness
    latest_candle = (
        db.query(MarketData)
        .filter(MarketData.symbol == s.default_symbol.upper())
        .order_by(MarketData.timestamp.desc())
        .first()
    )
    if latest_candle and latest_candle.timestamp:
        age_sec = (utcnow() - to_utc(latest_candle.timestamp)).total_seconds()
        if age_sec <= s.max_data_age_minutes * 60:
            components["market_data_freshness"] = {
                "status": "HEALTHY",
                "message": f"latest candle age {round(age_sec / 60, 1)}m within limit {s.max_data_age_minutes}m",
                "last_candle_time": latest_candle.timestamp.isoformat(),
            }
        elif age_sec <= s.max_data_age_minutes * 60 * 6:
            components["market_data_freshness"] = {
                "status": "DEGRADED",
                "message": f"candle data age {round(age_sec / 60, 1)}m exceeds limit (cached)",
                "last_candle_time": latest_candle.timestamp.isoformat(),
            }
        else:
            components["market_data_freshness"] = {
                "status": "OFFLINE",
                "message": f"candle data age {round(age_sec / 3600, 1)}h is ancient",
                "last_candle_time": latest_candle.timestamp.isoformat(),
            }
    else:
        components["market_data_freshness"] = {
            "status": "OFFLINE",
            "message": "no market candles found in database",
            "last_candle_time": None,
        }

    # 5. Polling worker
    try:
        from app.services.real_time_worker import get_worker
        worker = get_worker()
        wstatus = worker.get_status()
        if wstatus.get("running"):
            components["polling_worker"] = {"status": "HEALTHY", "message": "polling worker running"}
        elif s.market_data_provider == "twelvedata" and not s.twelve_data_api_key:
            components["polling_worker"] = {"status": "DISABLED", "message": "polling worker idle (requires api key)"}
        else:
            components["polling_worker"] = {"status": "OFFLINE", "message": "polling worker stopped"}
    except Exception as exc:
        components["polling_worker"] = {"status": "DOWN", "message": f"worker error: {exc}"}

    # 6. Strategy
    components["strategy"] = {"status": "HEALTHY", "message": "deterministic SMC/ICT strategy active"}

    # 7. Agents
    components["agents"] = {"status": "HEALTHY", "message": "multi-agent consensus active"}

    # 8. Risk engine
    components["risk_engine"] = {"status": "HEALTHY", "message": "deterministic risk engine active as final authority"}

    # 9. Paper broker
    if s.paper_trading:
        components["paper_broker"] = {"status": "HEALTHY", "message": "paper broker ready"}
    else:
        components["paper_broker"] = {"status": "DISABLED", "message": "paper broker disabled"}

    # 10. LLM
    if s.llm_configured:
        llm_snap = rate_limit_manager.snapshot("openrouter")
        if llm_snap.get("rate_limited"):
            components["llm"] = {"status": "DEGRADED", "message": "openrouter rate limited"}
        else:
            components["llm"] = {"status": "HEALTHY", "message": f"openrouter model={s.openrouter_model}"}
    else:
        components["llm"] = {"status": "DISABLED", "message": "openrouter api key not configured (deterministic mode)"}

    # 11. News
    if s.news_rss_urls:
        components["news"] = {"status": "HEALTHY", "message": "rss feeds configured"}
    else:
        components["news"] = {"status": "DISABLED", "message": "rss feeds empty (sentiment neutral)"}

    # 12. Sentiment
    components["sentiment"] = {"status": "HEALTHY" if s.news_rss_urls else "DISABLED", "message": "sentiment agent active" if s.news_rss_urls else "sentiment neutral"}

    statuses = [c["status"] for c in components.values()]
    if components["database"]["status"] == "DOWN" or components["backend"]["status"] == "DOWN":
        overall = "DOWN"
    elif "DEGRADED" in statuses:
        overall = "DEGRADED"
    else:
        overall = "HEALTHY"

    return {
        "status": overall,
        "service": "trading-backend",
        "timestamp": utcnow().isoformat(),
        "components": components,
    }


@router.get("/risk", tags=["risk"])
def risk_info(db: DBSession = Depends(get_db)):
    s = get_settings()
    from app.utils.timeutils import utcnow

    open_positions = db.query(Position).filter(Position.status == "open").all()
    day_start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    today_trades = db.query(Trade).filter(Trade.exit_time >= day_start, Trade.status == "closed").all()
    realized_today = sum((t.pnl or 0.0) for t in today_trades)
    account_equity = s.paper_account_balance
    daily_loss_limit = account_equity * s.max_daily_loss

    open_notional = sum((p.quantity or 0) * (p.entry_price or 0) for p in open_positions)
    symbol_exposures: dict[str, float] = {}
    for p in open_positions:
        symbol_exposures[p.symbol] = symbol_exposures.get(p.symbol, 0.0) + (p.quantity or 0) * (p.entry_price or 0)

    return {
        "status": "active",
        "final_authority": "RiskEngine (deterministic, no LLM bypass)",
        "limits": {
            "max_risk_per_trade": s.max_risk_per_trade,
            "max_daily_loss": s.max_daily_loss,
            "max_consecutive_losses": s.max_consecutive_losses,
            "max_open_positions": s.max_open_positions,
            "min_risk_reward": s.min_risk_reward,
            "max_position_size": s.max_position_size,
            "max_symbol_exposure": s.max_symbol_exposure,
            "max_total_exposure": s.max_total_exposure,
            "max_spread_pct": s.max_spread_pct,
            "max_data_age_minutes": s.max_data_age_minutes,
            "enforce_market_hours": s.enforce_market_hours,
            "news_blackout_minutes": s.news_blackout_minutes,
        },
        "current_state": {
            "account_equity": account_equity,
            "open_positions_count": len(open_positions),
            "max_open_positions": s.max_open_positions,
            "realized_pnl_today": round(realized_today, 2),
            "daily_loss_limit": round(daily_loss_limit, 2),
            "daily_loss_remaining": round(daily_loss_limit + realized_today, 2),
            "total_open_notional": round(open_notional, 2),
            "symbol_exposures": {k: round(v, 2) for k, v in symbol_exposures.items()},
        },
    }


# ------------------------------------------------------------------------------
# Market data
# ------------------------------------------------------------------------------
@router.get("/market/{symbol}", tags=["market"])
def market(symbol: str, timeframe: str = Query(None), limit: int = Query(200, ge=1, le=2000)):
    s = get_settings()
    tf = timeframe or s.default_timeframe
    try:
        parse_timeframe_minutes(tf)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    candles = pipeline.get_candles(symbol.upper(), tf, limit=limit)
    sources = sorted({c.get("source") or get_provider().name for c in candles})
    source_labels = [
        "SAMPLE DATA" if source == "SAMPLE_DATA" else "LOCAL IMPORT" if source.startswith("import") else source
        for source in sources
    ]
    return {
        "symbol": symbol.upper(), "timeframe": tf, "count": len(candles),
        "source": " + ".join(source_labels) if source_labels else get_provider().name,
        "sources": sources,
        "is_sample": "SAMPLE_DATA" in sources,
        "candles": [
            {
                "timestamp": c["timestamp"].isoformat(), "open": c["open"], "high": c["high"],
                "low": c["low"], "close": c["close"], "volume": c.get("volume"),
            }
            for c in candles
        ],
    }


@router.post("/market/import", tags=["market"])
def market_import(symbol: str = Query(...), timeframe: str = Query(...)):
    """Import candles from data/raw files into the database (offline-first)."""
    try:
        parse_timeframe_minutes(timeframe)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    provider = get_provider("local")
    candles = provider.get_ohlcv(symbol.upper(), timeframe, limit=0)
    if not candles:
        raise HTTPException(status_code=404, detail=f"no local data file found for {symbol.upper()} {timeframe} in data/raw")
    added = pipeline.persist_candles(candles)
    _log_event("INFO", "market_data", f"imported {added} candles for {symbol.upper()} {timeframe}")
    return {"symbol": symbol.upper(), "timeframe": timeframe, "rows": len(candles), "imported": added}


# ------------------------------------------------------------------------------
# Market data quality & resampling (Milestone 2)
# ------------------------------------------------------------------------------
@router.get("/market/{symbol}/quality", tags=["market"])
def market_quality(
    symbol: str,
    timeframe: str = Query(None),
    start: str = Query(None),
    end: str = Query(None),
    limit: int = Query(500, ge=1, le=5000),
):
    """Get data quality report for a symbol/timeframe."""
    s = get_settings()
    tf = timeframe or s.default_timeframe
    try:
        parse_timeframe_minutes(tf)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    service = get_historical_service()
    from datetime import datetime
    start_dt = datetime.fromisoformat(start.replace("Z", "+00:00")) if start else None
    end_dt = datetime.fromisoformat(end.replace("Z", "+00:00")) if end else None

    # If no start/end, use recent candles
    if start_dt is None or end_dt is None:
        candles = pipeline.get_candles(symbol.upper(), tf, limit=limit)
        expected_minutes = parse_timeframe_minutes(tf)
        from app.data.quality import analyze_candles
        report = analyze_candles(candles, symbol.upper(), tf, expected_minutes)
    else:
        report = service.get_data_quality(symbol.upper(), tf, start_dt, end_dt)
    return report.to_dict()


@router.get("/market/{symbol}/gaps", tags=["market"])
def market_gaps(
    symbol: str,
    timeframe: str = Query(None),
    start: str = Query(None),
    end: str = Query(None),
    max_gap: int = Query(3, ge=1),
):
    """Detect gaps in market data."""
    s = get_settings()
    tf = timeframe or s.default_timeframe
    try:
        parse_timeframe_minutes(tf)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    service = get_historical_service()
    from datetime import datetime
    start_dt = datetime.fromisoformat(start.replace("Z", "+00:00")) if start else None
    end_dt = datetime.fromisoformat(end.replace("Z", "+00:00")) if end else None

    if start_dt is None or end_dt is None:
        candles = pipeline.get_candles(symbol.upper(), tf, limit=500)
        expected_minutes = parse_timeframe_minutes(tf)
        from app.data.quality import detect_gaps
        gaps = detect_gaps(candles, expected_minutes, max_gap)
    else:
        gaps = service.get_gaps(symbol.upper(), tf, start_dt, end_dt, max_gap)
    return {"symbol": symbol.upper(), "timeframe": tf, "gaps": gaps, "gap_count": len(gaps)}


@router.get("/market/{symbol}/resample", tags=["market"])
def market_resample(
    symbol: str,
    source_timeframe: str = Query(...),
    target_timeframe: str = Query(...),
    limit: int = Query(200, ge=1, le=2000),
):
    """Resample candles from source timeframe to target timeframe."""
    s = get_settings()
    try:
        parse_timeframe_minutes(source_timeframe)
        parse_timeframe_minutes(target_timeframe)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    service = get_historical_service()
    candles = service.get_ohlcv(symbol.upper(), source_timeframe, limit=limit, resample_from=source_timeframe)
    return {"symbol": symbol.upper(), "source_timeframe": source_timeframe, "target_timeframe": target_timeframe,
            "count": len(candles), "candles": candles}


# ------------------------------------------------------------------------------
# Agents / signals / decisions
# ------------------------------------------------------------------------------
@router.get("/signals", tags=["agents"])
def signals(symbol: str = Query(None), limit: int = Query(100, ge=1, le=500), db: DBSession = Depends(get_db)):
    q = db.query(AgentSignal).order_by(AgentSignal.created_at.desc())
    if symbol:
        q = q.filter(AgentSignal.symbol == symbol.upper())
    rows = q.limit(limit).all()
    return {"count": len(rows), "signals": [
        {"id": r.id, "agent_name": r.agent_name, "symbol": r.symbol, "timeframe": r.timeframe,
         "direction": r.direction, "confidence": r.confidence, "llm_used": r.llm_used,
         "created_at": r.created_at.isoformat(), "payload": _safe_json(r.payload)}
        for r in rows
    ]}


@router.get("/agents", tags=["agents"])
def agents(symbol: str = Query(None), timeframe: str = Query(None)):
    """Run a live agent cycle on current data and return every agent's output."""
    s = get_settings()
    sym = (symbol or s.default_symbol).upper()
    tf = timeframe or s.default_timeframe
    candles = pipeline.get_candles(sym, tf, limit=400)
    if len(candles) < 60:
        return {"symbol": sym, "timeframe": tf, "status": "WAIT",
                "message": f"insufficient data ({len(candles)} candles)", "agents": {}}
    from app.agents.agents import run_all_agents

    outputs = run_all_agents(candles, sym, tf, None)
    return {"symbol": sym, "timeframe": tf, "status": "ok", "candles_used": len(candles), "agents": outputs}


@router.get("/decisions", tags=["decisions"])
def decisions(symbol: str = Query(None), limit: int = Query(50, ge=1, le=500), db: DBSession = Depends(get_db)):
    q = db.query(TradingDecision).order_by(TradingDecision.created_at.desc())
    if symbol:
        q = q.filter(TradingDecision.symbol == symbol.upper())
    rows = q.limit(limit).all()
    return {"count": len(rows), "decisions": [_decision_dict(r) for r in rows]}


def _decision_dict(r: TradingDecision) -> dict:
    return {
        "id": r.id, "symbol": r.symbol, "timeframe": r.timeframe, "action": r.action,
        "direction": r.direction, "confidence": r.confidence, "entry": r.entry,
        "stop_loss": r.stop_loss, "take_profit": r.take_profit, "risk_reward": r.risk_reward,
        "evidence": _safe_json(r.evidence), "agent_conflicts": _safe_json(r.agent_conflicts),
        "decision_reason": r.decision_reason, "created_at": r.created_at.isoformat(),
    }


@router.post("/cycle", tags=["decisions"])
def run_cycle(symbol: str = Query(None), timeframe: str = Query(None)):
    """Run one full pipeline cycle: agents -> decision -> risk -> execution -> journal."""
    result = pipeline.run_cycle(symbol, timeframe)
    _log_event("INFO" if result.get("status") == "ok" else "WARNING", "pipeline",
               f"cycle for {result.get('symbol')}: status={result.get('status')}")
    return result


# ------------------------------------------------------------------------------
# Positions / orders / trades / portfolio
# ------------------------------------------------------------------------------
@router.get("/positions", tags=["trading"])
def positions(db: DBSession = Depends(get_db)):
    from app.execution.broker import get_broker

    db2 = get_session()
    try:
        broker = get_broker(db2)
        return {"count": len(broker.get_positions()), "positions": broker.get_positions()}
    finally:
        db2.close()


@router.get("/orders", tags=["trading"])
def orders(status: str = Query(None), limit: int = Query(100, ge=1, le=500), db: DBSession = Depends(get_db)):
    q = db.query(Order).order_by(Order.created_at.desc())
    if status:
        q = q.filter(Order.status == status)
    rows = q.limit(limit).all()
    return {"count": len(rows), "orders": [
        {"id": r.id, "broker_order_id": r.broker_order_id, "symbol": r.symbol, "side": r.side,
         "order_type": r.order_type, "quantity": r.quantity, "status": r.status,
         "filled_quantity": r.filled_quantity, "avg_fill_price": r.avg_fill_price,
         "created_at": r.created_at.isoformat()}
        for r in rows
    ]}


@router.get("/trades", tags=["trading"])
def trades(status: str = Query(None), symbol: str = Query(None), limit: int = Query(100, ge=1, le=500),
           db: DBSession = Depends(get_db)):
    q = db.query(Trade).order_by(Trade.entry_time.desc())
    if status:
        q = q.filter(Trade.status == status)
    if symbol:
        q = q.filter(Trade.symbol == symbol.upper())
    rows = q.limit(limit).all()
    return {"count": len(rows), "trades": [
        {"id": r.id, "symbol": r.symbol, "direction": r.direction, "status": r.status,
         "entry_time": r.entry_time.isoformat() if r.entry_time else None,
         "exit_time": r.exit_time.isoformat() if r.exit_time else None,
         "entry_price": r.entry_price, "exit_price": r.exit_price, "quantity": r.quantity,
         "stop_loss": r.stop_loss, "take_profit": r.take_profit,
         "pnl": r.pnl, "r_multiple": r.r_multiple, "result": r.result, "reason": r.reason,
         "decision_id": r.decision_id}
        for r in rows
    ]}


@router.get("/portfolio", tags=["trading"])
def portfolio(db: DBSession = Depends(get_db)):
    from app.execution.broker import get_broker
    from app.models.database import PortfolioSnapshot

    db2 = get_session()
    try:
        broker = get_broker(db2)
        account = broker.get_account()
        snapshots = db2.query(PortfolioSnapshot).order_by(PortfolioSnapshot.taken_at.desc()).limit(500).all()
        return {
            "account": account,
            "snapshots": [
                {"equity": s_.equity, "taken_at": s_.taken_at.isoformat()} for s_ in reversed(snapshots)
            ],
        }
    finally:
        db2.close()


@router.get("/performance", tags=["analytics"])
def performance(db: DBSession = Depends(get_db)):
    from app.services.journal import agent_performance_service

    trades_all = db.query(Trade).filter(Trade.status == "closed").order_by(Trade.exit_time.asc()).all()
    wins = [t for t in trades_all if (t.pnl or 0) > 0]
    losses = [t for t in trades_all if (t.pnl or 0) < 0]
    gross_win = sum(t.pnl for t in wins)
    gross_loss = abs(sum(t.pnl for t in losses))
    rs = [t.r_multiple for t in trades_all if t.r_multiple is not None]
    total = len(trades_all)
    return {
        "trading": {
            "total_trades": total,
            "win_rate": round(len(wins) / total, 4) if total else None,
            "profit_factor": round(gross_win / gross_loss, 3) if gross_loss > 0 else None,
            "expectancy_r": round(sum(rs) / len(rs), 4) if rs else None,
            "total_pnl": round(sum(t.pnl or 0 for t in trades_all), 2),
            "total_r": round(sum(rs), 2) if rs else 0.0,
        },
        "agents": agent_performance_service.summary(db),
    }


def get_session() -> DBSession:
    from app.models.database import get_session_factory

    return get_session_factory()()


def _safe_json(text: str) -> Any:
    import json

    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return {}


# ------------------------------------------------------------------------------
# Paper trading endpoints
# ------------------------------------------------------------------------------
class OrderRequest(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=20)
    side: str = Field(..., pattern="^(BUY|SELL)$")
    quantity: float = Field(..., gt=0)
    order_type: str = Field("market", pattern="^(market|limit|stop)$")
    limit_price: float | None = None
    stop_price: float | None = None
    take_profit: float | None = None


@router.post("/paper/orders", tags=["paper"])
def paper_order(req: OrderRequest, db: DBSession = Depends(get_db)):
    from app.execution.broker import get_broker

    broker = get_broker(db)
    result = broker.place_order(req.symbol.upper(), req.side, req.quantity, req.order_type,
                                req.limit_price, req.stop_price, req.take_profit)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "order rejected"))
    return result


@router.post("/paper/close/{position_id}", tags=["paper"])
def paper_close(position_id: int, db: DBSession = Depends(get_db)):
    from app.execution.broker import get_broker

    broker = get_broker(db)
    result = broker.close_position(position_id)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "close failed"))
    return result


@router.post("/paper/reset", tags=["paper"])
def paper_reset(confirm: bool = Query(False)):
    """Reset the paper account: deletes paper trades/orders/positions/snapshots."""
    if not confirm:
        raise HTTPException(status_code=400, detail="pass ?confirm=true to reset the paper account")
    from app.models.database import get_session_factory

    SessionLocal = get_session_factory()
    db = SessionLocal()
    try:
        deleted = {}
        from app.models.database import Fill, PortfolioSnapshot

        db.query(Trade).update({Trade.position_id: None}, synchronize_session=False)
        db.query(Position).update({Position.trade_id: None}, synchronize_session=False)
        for model in (Trade, Fill, Order, Position, PortfolioSnapshot):
            count = db.query(model).delete()
            deleted[model.__tablename__] = count
        db.commit()
        _log_event("WARNING", "paper", f"paper account reset: {deleted}")
        return {"ok": True, "deleted": deleted}
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


# ------------------------------------------------------------------------------
# Backtests
# ------------------------------------------------------------------------------
class BacktestRequest(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=20)
    timeframe: str = Field(..., min_length=2, max_length=10)
    start: str | None = None
    end: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)


# ------------------------------------------------------------------------------
# Watchlist (Milestone 21)
# ------------------------------------------------------------------------------
class WatchlistAddRequest(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=20)
    timeframe: str = Field(default="1h")


@router.get("/watchlist", tags=["watchlist"])
def get_watchlist(db: DBSession = Depends(get_db)):
    """Get all watchlist items."""
    items = db.query(WatchlistItem).order_by(WatchlistItem.created_at.desc()).all()
    return {
        "count": len(items),
        "items": [
            {"id": item.id, "symbol": item.symbol, "timeframe": item.timeframe, 
             "created_at": item.created_at.isoformat()}
            for item in items
        ]
    }


@router.post("/watchlist/add", tags=["watchlist"])
def add_to_watchlist(req: WatchlistAddRequest, db: DBSession = Depends(get_db)):
    """Add a symbol/timeframe to the watchlist."""
    # Check if already exists
    existing = db.query(WatchlistItem).filter(
        WatchlistItem.symbol == req.symbol.upper(),
        WatchlistItem.timeframe == req.timeframe
    ).first()
    
    if existing:
        return {"id": existing.id, "message": "Already in watchlist"}
    
    item = WatchlistItem(
        symbol=req.symbol.upper(),
        timeframe=req.timeframe
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return {"id": item.id, "symbol": item.symbol, "timeframe": item.timeframe, 
            "created_at": item.created_at.isoformat()}


@router.delete("/watchlist/remove/{item_id}", tags=["watchlist"])
def remove_from_watchlist(item_id: int, db: DBSession = Depends(get_db)):
    """Remove an item from the watchlist."""
    item = db.query(WatchlistItem).filter(WatchlistItem.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Watchlist item not found")
    
    db.delete(item)
    db.commit()
    return {"id": item_id, "message": "Removed from watchlist"}


# ------------------------------------------------------------------------------
# Alerts (Milestone 23)
# ------------------------------------------------------------------------------
class AlertCreateRequest(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=20)
    timeframe: str = Field(default="1h")
    alert_type: str = Field(default="price")
    condition: str = Field(default="above")
    target_value: float
    message: str = ""


@router.get("/alerts", tags=["alerts"])
def get_alerts(
    symbol: str = Query(None),
    active: bool = Query(True),
    limit: int = Query(50, ge=1, le=200),
    db: DBSession = Depends(get_db),
):
    """Get all alerts."""
    q = db.query(Alert)
    if symbol:
        q = q.filter(Alert.symbol == symbol.upper())
    if active:
        q = q.filter(Alert.is_active == True)
    rows = q.order_by(Alert.created_at.desc()).limit(limit).all()
    return {
        "count": len(rows),
        "alerts": [
            {"id": a.id, "symbol": a.symbol, "timeframe": a.timeframe,
             "alert_type": a.alert_type, "condition": a.condition,
             "target_value": a.target_value, "current_value": a.current_value,
             "message": a.message, "is_active": a.is_active,
             "triggered_at": a.triggered_at.isoformat() if a.triggered_at else None,
             "created_at": a.created_at.isoformat()}
            for a in rows
        ]
    }


@router.post("/alerts", tags=["alerts"])
def create_alert(req: AlertCreateRequest, db: DBSession = Depends(get_db)):
    """Create a new alert."""
    alert = Alert(
        symbol=req.symbol.upper(),
        timeframe=req.timeframe,
        alert_type=req.alert_type,
        condition=req.condition,
        target_value=req.target_value,
        message=req.message,
    )
    db.add(alert)
    db.commit()
    db.refresh(alert)
    return {
        "id": alert.id, "symbol": alert.symbol, "timeframe": alert.timeframe,
        "alert_type": alert.alert_type, "condition": alert.condition,
        "target_value": alert.target_value, "is_active": alert.is_active,
        "created_at": alert.created_at.isoformat()
    }


@router.delete("/alerts/{alert_id}", tags=["alerts"])
def delete_alert(alert_id: int, db: DBSession = Depends(get_db)):
    """Delete an alert."""
    alert = db.query(Alert).filter(Alert.id == alert_id).first()
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")
    db.delete(alert)
    db.commit()
    return {"id": alert_id, "message": "Alert deleted"}


@router.post("/alerts/{alert_id}/toggle", tags=["alerts"])
def toggle_alert(alert_id: int, db: DBSession = Depends(get_db)):
    """Toggle alert active status."""
    alert = db.query(Alert).filter(Alert.id == alert_id).first()
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")
    alert.is_active = not alert.is_active
    db.commit()
    return {"id": alert_id, "is_active": alert.is_active}


@router.post("/alerts/check", tags=["alerts"])
def check_alerts(db: DBSession = Depends(get_db)):
    """Check all active alerts against current prices."""
    alerts = db.query(Alert).filter(Alert.is_active == True).all()
    triggered = []
    for alert in alerts:
        try:
            candles = pipeline.get_candles(alert.symbol, alert.timeframe, limit=1)
            if not candles:
                continue
            current_price = candles[0]["close"]
            alert.current_value = current_price
            
            should_trigger = False
            if alert.condition == "above" and current_price > alert.target_value:
                should_trigger = True
            elif alert.condition == "below" and current_price < alert.target_value:
                should_trigger = True
            elif alert.condition == "crosses_up" and current_price > alert.target_value:
                should_trigger = True
            elif alert.condition == "crosses_down" and current_price < alert.target_value:
                should_trigger = True
            
            if should_trigger:
                alert.triggered_at = utcnow_helper()
                alert.is_active = False
                _log_event("INFO", "alert", f"Alert triggered: {alert.symbol} {alert.condition} {alert.target_value}",
                           {"alert_id": alert.id, "current_price": current_price})
                triggered.append({
                    "id": alert.id, "symbol": alert.symbol,
                    "condition": alert.condition, "target_value": alert.target_value,
                    "current_value": current_price
                })
        except Exception:
            continue
    
    db.commit()
    return {"checked": len(alerts), "triggered": len(triggered), "details": triggered}


@router.get("/backtests", tags=["backtests"])
def list_backtests(limit: int = Query(50, ge=1, le=200), db: DBSession = Depends(get_db)):
    rows = db.query(BacktestRun).order_by(BacktestRun.created_at.desc()).limit(limit).all()
    return {"count": len(rows), "backtests": [
        {"id": r.id, "symbol": r.symbol, "timeframe": r.timeframe, "start": r.start.isoformat(),
         "end": r.end.isoformat(), "status": r.status, "trade_count": r.trade_count,
         "win_rate": r.win_rate, "profit_factor": r.profit_factor, "expectancy_r": r.expectancy_r,
         "max_drawdown": r.max_drawdown, "total_pnl": r.total_pnl,
         "starting_equity": r.starting_equity, "ending_equity": r.ending_equity,
         "sharpe": r.sharpe, "sortino": r.sortino, "created_at": r.created_at.isoformat()}
        for r in rows
    ]}


@router.get("/backtests/{run_id}", tags=["backtests"])
def get_backtest(run_id: int, db: DBSession = Depends(get_db)):
    run = db.get(BacktestRun, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="backtest not found")
    bt_trades = db.query(BacktestTrade).filter(BacktestTrade.run_id == run_id).order_by(BacktestTrade.entry_time.asc()).all()
    return {
        "run": {
            "id": run.id, "symbol": run.symbol, "timeframe": run.timeframe,
            "start": run.start.isoformat(), "end": run.end.isoformat(),
            "params": _safe_json(run.params), "status": run.status,
            "starting_equity": run.starting_equity, "ending_equity": run.ending_equity,
            "total_pnl": run.total_pnl, "trade_count": run.trade_count,
            "win_rate": run.win_rate, "profit_factor": run.profit_factor,
            "expectancy_r": run.expectancy_r, "max_drawdown": run.max_drawdown,
            "sharpe": run.sharpe, "sortino": run.sortino,
            "equity_curve": _safe_json(run.equity_curve),
        },
        "trades": [
            {"id": t.id, "direction": t.direction, "entry_time": t.entry_time.isoformat(),
             "exit_time": t.exit_time.isoformat() if t.exit_time else None,
             "entry_price": t.entry_price, "exit_price": t.exit_price, "quantity": t.quantity,
             "pnl": t.pnl, "r_multiple": t.r_multiple, "exit_reason": t.exit_reason,
             "mae": t.mae, "mfe": t.mfe}
            for t in bt_trades
        ],
    }


@router.post("/backtests/run", tags=["backtests"])
def run_backtest(req: BacktestRequest, db: DBSession = Depends(get_db)):
    try:
        parse_timeframe_minutes(req.timeframe)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    from datetime import datetime

    candles = pipeline.get_candles(req.symbol.upper(), req.timeframe, limit=400)
    if len(candles) < 150:
        raise HTTPException(status_code=400, detail=f"need at least 150 candles, have {len(candles)}. Import data via /market/import.")

    filtered = candles
    if req.start or req.end:
        def _parse(dt: str) -> Any:
            return datetime.fromisoformat(dt.replace("Z", "+00:00"))

        start_dt = _parse(req.start) if req.start else None
        end_dt = _parse(req.end) if req.end else None
        filtered = [c for c in candles if (start_dt is None or c["timestamp"] >= start_dt) and (end_dt is None or c["timestamp"] <= end_dt)]

    from app.backtesting.engine import BacktestEngine

    engine = BacktestEngine(req.params)
    result = engine.run(filtered, req.symbol.upper(), req.timeframe)
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])

    run = result["run"]
    row = BacktestRun(
        symbol=req.symbol.upper(), timeframe=req.timeframe,
        start=datetime.fromisoformat(run["start"]) if run["start"] else utcnow_helper(),
        end=datetime.fromisoformat(run["end"]) if run["end"] else utcnow_helper(),
        params=_json_dump(run["params"]), status="completed",
        starting_equity=run["starting_equity"], ending_equity=run["ending_equity"],
        total_pnl=run["total_pnl"], trade_count=run["trade_count"],
        win_rate=run["win_rate"], profit_factor=run["profit_factor"] or 0.0,
        expectancy_r=run["expectancy_r"], max_drawdown=run["max_drawdown"],
        sharpe=run["sharpe"], sortino=run["sortino"],
        equity_curve=_json_dump(result["equity_curve"]),
    )
    db.add(row)
    db.flush()
    for t in result["trades"]:
        db.add(BacktestTrade(
            run_id=row.id, symbol=t["symbol"], direction=t["direction"],
            entry_time=t["entry_time"], exit_time=t["exit_time"],
            entry_price=t["entry_price"], exit_price=t["exit_price"],
            stop_loss=t["stop_loss"], take_profit=t["take_profit"], quantity=t["quantity"],
            pnl=t["pnl"], r_multiple=t["r_multiple"], exit_reason=t["exit_reason"],
            mae=t["mae"], mfe=t["mfe"],
        ))
    db.commit()
    return {"run_id": row.id, "run": run, "trades": result["trades"]}


# ------------------------------------------------------------------------------
# Walk-forward analysis (Milestone 24)
# ------------------------------------------------------------------------------
class WalkForwardRequest(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=20)
    timeframe: str = Field(..., min_length=2, max_length=10)
    train_periods: int = Field(default=4, ge=2, le=20)
    test_size: int = Field(default=50, ge=10, le=500)
    params: dict[str, Any] = Field(default_factory=dict)


@router.post("/backtests/walk-forward", tags=["backtests"])
def run_walk_forward(req: WalkForwardRequest, db: DBSession = Depends(get_db)):
    """Run walk-forward analysis: split data into train/test windows, backtest each."""
    try:
        parse_timeframe_minutes(req.timeframe)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    from datetime import datetime

    candles = pipeline.get_candles(req.symbol.upper(), req.timeframe, limit=400)
    total = len(candles)
    
    if total < req.train_periods * req.test_size * 2:
        raise HTTPException(
            status_code=400, 
            detail=f"Not enough candles for walk-forward: need {req.train_periods * req.test_size * 2}, have {total}"
        )
    
    results = []
    test_size = req.test_size
    train_size = min(200, total // req.train_periods)
    
    for i in range(req.train_periods):
        train_end = (i + 1) * train_size
        test_start = train_end
        test_end = min(train_end + test_size, total)
        
        if test_start >= total or test_end <= test_start:
            break
        
        train_candles = candles[:train_end]
        test_candles = candles[test_start:test_end]
        
        if len(test_candles) < 50:
            continue
        
        from app.backtesting.engine import BacktestEngine
        engine = BacktestEngine(req.params)
        result = engine.run(test_candles, req.symbol.upper(), req.timeframe)
        
        if result.get("error"):
            continue
        
        run = result["run"]
        results.append({
            "period": i + 1,
            "train_end": train_candles[-1]["timestamp"].isoformat(),
            "test_start": test_candles[0]["timestamp"].isoformat(),
            "test_end": test_candles[-1]["timestamp"].isoformat(),
            "trade_count": run["trade_count"],
            "total_pnl": run["total_pnl"],
            "win_rate": run["win_rate"],
            "profit_factor": run["profit_factor"],
            "max_drawdown": run["max_drawdown"],
            "sharpe": run["sharpe"],
        })
    
    # Calculate aggregate metrics
    if results:
        total_trades = sum(r["trade_count"] for r in results)
        avg_pnl = sum(r["total_pnl"] for r in results) / len(results)
        avg_win_rate = sum(r["win_rate"] for r in results) / len(results)
        avg_profit_factor = sum(r["profit_factor"] for r in results if r["profit_factor"] > 0) / max(
            1, len([r for r in results if r["profit_factor"] > 0])
        )
        avg_sharpe = sum(r["sharpe"] for r in results if r["sharpe"] is not None) / max(
            1, len([r for r in results if r["sharpe"] is not None])
        )
        consistency = len([r for r in results if r["total_pnl"] > 0]) / len(results)
    else:
        total_trades = 0
        avg_pnl = 0
        avg_win_rate = 0
        avg_profit_factor = 0
        avg_sharpe = 0
        consistency = 0
    
    return {
        "symbol": req.symbol.upper(),
        "timeframe": req.timeframe,
        "periods": req.train_periods,
        "test_size": test_size,
        "results": results,
        "summary": {
            "total_trades": total_trades,
            "avg_pnl": round(avg_pnl, 2),
            "avg_win_rate": round(avg_win_rate, 4),
            "avg_profit_factor": round(avg_profit_factor, 3),
            "avg_sharpe": round(avg_sharpe, 3),
            "consistency": round(consistency, 4),
            "num_periods": len(results),
        }
    }


# ------------------------------------------------------------------------------
# Experiment tracking (Milestone 25)
# ------------------------------------------------------------------------------
class ExperimentCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: str = Field(default="")
    tags: list[str] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)
    metrics: dict[str, Any] = Field(default_factory=dict)


class ExperimentUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = None
    tags: list[str] | None = None
    config: dict[str, Any] | None = None
    metrics: dict[str, Any] | None = None
    status: str | None = Field(default=None, pattern="^(running|completed|failed|archived)$")


class DownloadRequest(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=20)
    timeframe: str = Field(..., pattern="^(1m|5m|15m|30m|1h|4h|1d)$")
    start_date: str | None = None
    end_date: str | None = None
    limit: int | None = Field(default=None, ge=1, le=1000000)


@router.get("/experiments", tags=["experiments"])
def list_experiments(
    status: str | None = None,
    limit: int = Query(default=50, le=200),
    db: DBSession = Depends(get_db),
):
    """List experiments with optional status filter."""
    from app.models.database import Experiment
    q = db.query(Experiment).order_by(Experiment.created_at.desc())
    if status:
        q = q.filter(Experiment.status == status)
    exps = q.limit(limit).all()
    return {
        "experiments": [
            {
                "id": e.id,
                "name": e.name,
                "description": e.description,
                "tags": json.loads(e.tags) if e.tags else [],
                "config": json.loads(e.config) if e.config else {},
                "metrics": json.loads(e.metrics) if e.metrics else {},
                "status": e.status,
                "created_at": e.created_at.isoformat() if e.created_at else None,
                "updated_at": e.updated_at.isoformat() if e.updated_at else None,
                "completed_at": e.completed_at.isoformat() if e.completed_at else None,
            }
            for e in exps
        ]
    }


@router.post("/experiments", tags=["experiments"])
def create_experiment(req: ExperimentCreateRequest, db: DBSession = Depends(get_db)):
    """Create a new experiment."""
    import json
    from app.models.database import Experiment
    from app.utils.timeutils import utcnow
    
    exp = Experiment(
        name=req.name,
        description=req.description,
        tags=json.dumps(req.tags),
        config=json.dumps(req.config),
        metrics=json.dumps(req.metrics),
        status="running",
    )
    db.add(exp)
    db.commit()
    db.refresh(exp)
    return {
        "id": exp.id,
        "name": exp.name,
        "description": exp.description,
        "tags": req.tags,
        "config": req.config,
        "metrics": req.metrics,
        "status": exp.status,
        "created_at": exp.created_at.isoformat() if exp.created_at else None,
    }


@router.get("/experiments/{exp_id}", tags=["experiments"])
def get_experiment(exp_id: int, db: DBSession = Depends(get_db)):
    """Get experiment by ID."""
    from app.models.database import Experiment
    exp = db.query(Experiment).filter(Experiment.id == exp_id).first()
    if not exp:
        raise HTTPException(status_code=404, detail="Experiment not found")
    import json
    return {
        "id": exp.id,
        "name": exp.name,
        "description": exp.description,
        "tags": json.loads(exp.tags) if exp.tags else [],
        "config": json.loads(exp.config) if exp.config else {},
        "metrics": json.loads(exp.metrics) if exp.metrics else {},
        "status": exp.status,
        "created_at": exp.created_at.isoformat() if exp.created_at else None,
        "updated_at": exp.updated_at.isoformat() if exp.updated_at else None,
        "completed_at": exp.completed_at.isoformat() if exp.completed_at else None,
    }


@router.patch("/experiments/{exp_id}", tags=["experiments"])
def update_experiment(exp_id: int, req: ExperimentUpdateRequest, db: DBSession = Depends(get_db)):
    """Update experiment."""
    import json
    from app.models.database import Experiment
    from app.utils.timeutils import utcnow
    
    exp = db.query(Experiment).filter(Experiment.id == exp_id).first()
    if not exp:
        raise HTTPException(status_code=404, detail="Experiment not found")
    
    if req.name is not None:
        exp.name = req.name
    if req.description is not None:
        exp.description = req.description
    if req.tags is not None:
        exp.tags = json.dumps(req.tags)
    if req.config is not None:
        exp.config = json.dumps(req.config)
    if req.metrics is not None:
        exp.metrics = json.dumps(req.metrics)
    if req.status is not None:
        exp.status = req.status
        if req.status == "completed":
            exp.completed_at = utcnow()
    exp.updated_at = utcnow()
    
    db.commit()
    db.refresh(exp)
    return {
        "id": exp.id,
        "name": exp.name,
        "description": exp.description,
        "tags": json.loads(exp.tags) if exp.tags else [],
        "config": json.loads(exp.config) if exp.config else {},
        "metrics": json.loads(exp.metrics) if exp.metrics else {},
        "status": exp.status,
        "created_at": exp.created_at.isoformat() if exp.created_at else None,
        "updated_at": exp.updated_at.isoformat() if exp.updated_at else None,
        "completed_at": exp.completed_at.isoformat() if exp.completed_at else None,
    }


@router.delete("/experiments/{exp_id}", tags=["experiments"])
def delete_experiment(exp_id: int, db: DBSession = Depends(get_db)):
    """Delete experiment."""
    from app.models.database import Experiment
    exp = db.query(Experiment).filter(Experiment.id == exp_id).first()
    if not exp:
        raise HTTPException(status_code=404, detail="Experiment not found")
    db.delete(exp)
    db.commit()
    return {"ok": True}


# ------------------------------------------------------------------------------
# Real-market session (Phases 36/38/40)
# ------------------------------------------------------------------------------
@router.get("/session/status", tags=["session"])
def session_status():
    """Real-market session status: mode, provider, credits, worker, last signal."""
    from app.data.resilience import rate_limit_manager
    from app.services.real_market import get_session_manager

    s = get_settings()
    session = get_session_manager()
    rl_status: dict[str, Any] = {}
    try:
        provider = get_provider("twelvedata")
        if hasattr(provider, "get_rate_limit_status"):
            rl_status = provider.get_rate_limit_status()
    except Exception:
        rl_status = rate_limit_manager.snapshot(s.market_data_provider)
    worker_status: dict[str, Any] = {}
    try:
        from app.services.real_time_worker import get_worker
        worker_status = get_worker().get_status()
    except Exception:
        pass
    return session.status(worker_status=worker_status, rate_limit_status=rl_status)


@router.get("/providers/twelvedata/capability", tags=["session"])
def provider_capability(symbol: str = Query(None), timeframe: str = Query(None)):
    """Phase 36: explicit capability discovery (symbol/endpoint/entitlement/timeframe)."""
    s = get_settings()
    sym = (symbol or s.default_symbol).upper()
    tf = timeframe or s.default_timeframe
    try:
        provider = get_provider("twelvedata")
    except KeyError:
        return {"provider": "twelvedata", "symbol": sym, "timeframe": tf,
                "available": False, "reason": "PROVIDER_NOT_REGISTERED"}
    if not hasattr(provider, "check_capability"):
        return {"provider": provider.name, "symbol": sym, "timeframe": tf,
                "available": False, "reason": "CAPABILITY_CHECK_NOT_SUPPORTED"}
    return provider.check_capability(sym, tf)


@router.get("/providers/twelvedata/credits", tags=["session"])
def provider_credits():
    """Phase 35.4: structured rate-limit / credit status."""
    try:
        provider = get_provider("twelvedata")
        if hasattr(provider, "get_rate_limit_status"):
            return provider.get_rate_limit_status()
    except KeyError:
        pass
    return {"provider": "twelvedata", "status": "UNKNOWN", "stale": True}


@router.post("/providers/twelvedata/api-usage", tags=["session"])
def provider_api_usage():
    """Phase 35.3: explicit /api_usage synchronization (consumes 1 credit; cached).

    Never called automatically by the platform.
    """
    import asyncio

    try:
        provider = get_provider("twelvedata")
    except KeyError:
        raise HTTPException(status_code=404, detail="twelvedata provider not registered")
    if not hasattr(provider, "get_api_usage"):
        raise HTTPException(status_code=404, detail="provider does not support api usage")
    try:
        return asyncio.run(provider.get_api_usage(force=False)) or {"status": "UNAVAILABLE"}
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@router.get("/readiness", tags=["system"])
def readiness():
    """Phase 40: aggregated real-market paper readiness report."""
    from app.services.real_market import readiness_service

    return readiness_service.evaluate()


# ------------------------------------------------------------------------------
# Config
# ------------------------------------------------------------------------------
@router.get("/config", tags=["system"])
def get_config():
    """Non-secret runtime configuration (safe to display)."""
    s = get_settings()
    return {
        "paper_trading": s.paper_trading, "live_trading": s.live_trading,
        "default_symbol": s.default_symbol, "default_timeframe": s.default_timeframe,
        "risk": {
            "max_risk_per_trade": s.max_risk_per_trade, "max_daily_loss": s.max_daily_loss,
            "max_consecutive_losses": s.max_consecutive_losses,
            "max_open_positions": s.max_open_positions, "min_risk_reward": s.min_risk_reward,
            "max_position_size": s.max_position_size, "max_symbol_exposure": s.max_symbol_exposure,
            "max_total_exposure": s.max_total_exposure, "risk_percentage": s.risk_percentage,
        },
        "agent_weights": {
            "technical": s.technical_weight, "quant": s.quant_weight, "strategy": s.strategy_weight,
            "regime": s.regime_weight, "news": s.news_weight, "sentiment": s.sentiment_weight,
            "decision_threshold": s.decision_threshold,
        },
        "strategy": {
            "swing_lookback": s.swing_lookback, "sweep_max_age": s.sweep_max_age,
            "displacement_required": s.displacement_required,
            "displacement_atr": s.displacement_atr, "stop_buffer_atr": s.stop_buffer_atr,
        },
        "execution": {
            "paper_account_balance": s.paper_account_balance,
            "paper_commission": s.paper_commission, "paper_slippage": s.paper_slippage,
            "paper_spread_pct": s.paper_spread_pct,
        },
        "llm": {"configured": s.llm_configured, "model": s.openrouter_model,
                "call_mode": s.llm_call_mode_value},
        "market_data_provider": s.market_data_provider,
        "news_configured": len(s.news_rss_list) > 0,
    }


@router.get("/data/inventory", tags=["data"])
def data_inventory(db: DBSession = Depends(get_db)):
    """Get inventory of all available market data in the database."""
    try:
        from sqlalchemy import func
        
        # Query for each symbol/timeframe combination
        results = db.query(
            MarketData.symbol,
            MarketData.timeframe,
            func.count(MarketData.id).label('total_candles'),
            func.min(MarketData.timestamp).label('earliest_timestamp'),
            func.max(MarketData.timestamp).label('latest_timestamp')
        ).group_by(
            MarketData.symbol,
            MarketData.timeframe
        ).all()
        
        inventory = []
        for row in results:
            # Determine status based on data freshness
            settings = get_settings()
            age_minutes = None
            status = "UNKNOWN"
            
            if row.latest_timestamp:
                age_seconds = (utcnow() - to_utc(row.latest_timestamp)).total_seconds()
                age_minutes = age_seconds / 60
                
                if age_minutes <= settings.max_data_age_minutes:
                    status = "FRESH"
                elif age_minutes <= settings.max_data_age_minutes * 6:
                    status = "STALE"
                else:
                    status = "ANCIENT"
            else:
                status = "NO_DATA"
            
            inventory.append({
                "symbol": row.symbol,
                "timeframe": row.timeframe,
                "total_candles": row.total_candles,
                "earliest_timestamp": row.earliest_timestamp.isoformat() if row.earliest_timestamp else None,
                "latest_timestamp": row.latest_timestamp.isoformat() if row.latest_timestamp else None,
                "status": status,
                "age_minutes": round(age_minutes, 1) if age_minutes is not None else None
            })
        
        return {"data": inventory}
    except Exception as e:
        logger.error(f"Error in data_inventory: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/data/download", tags=["data"])
def download_market_data(
    req: DownloadRequest,
    db: DBSession = Depends(get_db)
):
    """Download market data for a symbol/timeframe from MT5 and persist to database."""
    try:
        service = get_historical_service()
        # If both start and end dates are provided, use historical data method
        if req.start_date and req.end_date:
            start_dt = datetime.fromisoformat(req.start_date).replace(tzinfo=timezone.utc)
            end_dt = datetime.fromisoformat(req.end_date).replace(tzinfo=timezone.utc)
            candles, _ = service.get_historical(
                symbol=req.symbol,
                timeframe=req.timeframe,
                start=start_dt,
                end=end_dt,
                validate=False,
                quality_report=False
            )
        else:
            # Otherwise, fetch recent candles with a high limit
            limit_to_use = req.limit if req.limit is not None else 1000000
            candles = service.get_ohlcv(
                symbol=req.symbol,
                timeframe=req.timeframe,
                limit=limit_to_use
            )
        
        # Persist candles to DB
        service._persist_candles(candles)
        
        return {
            "status": "success",
            "symbol": req.symbol.upper(),
            "timeframe": req.timeframe,
            "candles_downloaded": len(candles),
            "message": f"Downloaded {len(candles)} candles for {req.symbol} {req.timeframe}"
        }
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to download market data: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/data/delete", tags=["data"])
def delete_market_data(
    symbol: str,
    timeframe: str,
    db: DBSession = Depends(get_db)
):
    """Delete market data for a symbol/timeframe from the database."""
    try:
        deleted_count = db.query(MarketData).filter(
            MarketData.symbol == symbol,
            MarketData.timeframe == timeframe
        ).delete(synchronize_session=False)
        db.commit()
        return {
            "status": "success",
            "deleted_count": deleted_count,
            "message": f"Deleted {deleted_count} candles for {symbol} {timeframe}"
        }
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to delete market data: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/config/update", tags=["system"])
def update_config(req: ConfigUpdateRequest, db: DBSession = Depends(get_db)):
    """Update runtime configuration (persists to user_settings)."""
    from app.models.database import UserSettings
    import json

    updates = {}
    if req.risk is not None:
        updates["risk"] = req.risk
    if req.agent_weights is not None:
        updates["agent_weights"] = req.agent_weights
    if req.strategy is not None:
        updates["strategy"] = req.strategy
    if req.execution is not None:
        updates["execution"] = req.execution
    if req.default_symbol is not None:
        updates["default_symbol"] = req.default_symbol
    if req.default_timeframe is not None:
        updates["default_timeframe"] = req.default_timeframe

    for key, value in updates.items():
        existing = db.query(UserSettings).filter(UserSettings.key == key).first()
        if existing:
            existing.value = json.dumps(value, default=str)
        else:
            db.add(UserSettings(key=key, value=json.dumps(value, default=str)))

    db.commit()
    _log_event("INFO", "config", "Configuration updated", {"keys": list(updates.keys())})
    return {"ok": True, "updated": list(updates.keys())}


def _json_dump(obj: Any) -> str:
    import json

    return json.dumps(obj, default=str)


def utcnow_helper():
    from app.utils.timeutils import utcnow

    return utcnow()

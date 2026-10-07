#!/usr/bin/env python3
"""Seed/demo data script for Trading Framework (M29).

Usage:
    python scripts/seed.py

This script populates the database with sample data for demo/offline mode:
- Watchlist items (XAUUSD, EURUSD, GBPUSD)
- Alerts
- Sample backtest runs with trades
- Sample agent signals and decisions
- Sample open positions
- Sample system events
"""

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).parent.parent
BACKEND = ROOT / "backend"

# Add backend to path
sys.path.insert(0, str(BACKEND))

from app.models.database import (
    WatchlistItem,
    Alert,
    Experiment,
    BacktestRun,
    BacktestTrade,
    AgentSignal,
    TradingDecision,
    Position,
    SystemEvent,
    get_session_factory,
    init_db,
)
from app.utils.timeutils import utcnow


def create_demo_watchlist(db):
    """Create demo watchlist items."""
    print("Creating demo watchlist items...")
    
    items = [
        {"symbol": "XAUUSD", "timeframe": "15m"},
        {"symbol": "XAUUSD", "timeframe": "1h"},
        {"symbol": "XAUUSD", "timeframe": "4h"},
        {"symbol": "EURUSD", "timeframe": "1h"},
        {"symbol": "GBPUSD", "timeframe": "1h"},
        {"symbol": "USDJPY", "timeframe": "1h"},
    ]
    
    created = 0
    for item in items:
        existing = db.query(WatchlistItem).filter_by(symbol=item["symbol"], timeframe=item["timeframe"]).first()
        if not existing:
            db.add(WatchlistItem(**item))
            created += 1
    db.commit()
    print(f"  Created {created} watchlist items")


def create_demo_alerts(db):
    """Create demo alerts."""
    print("Creating demo alerts...")
    
    alerts = [
        {
            "symbol": "XAUUSD",
            "timeframe": "1h",
            "alert_type": "price",
            "condition": "above",
            "target_value": 2000.0,
            "current_value": 2015.5,
            "message": "XAUUSD above $2000 - bullish continuation",
            "is_active": True,
        },
        {
            "symbol": "XAUUSD",
            "timeframe": "15m",
            "alert_type": "indicator",
            "condition": "below",
            "target_value": 30.0,
            "current_value": 28.5,
            "message": "RSI oversold - potential reversal",
            "is_active": True,
        },
        {
            "symbol": "EURUSD",
            "timeframe": "1h",
            "alert_type": "price",
            "condition": "above",
            "target_value": 1.0900,
            "current_value": 1.0855,
            "message": "EURUSD approaching resistance at 1.09",
            "is_active": False,
        },
        {
            "symbol": "XAUUSD",
            "timeframe": "15m",
            "alert_type": "signal",
            "condition": "equals",
            "target_value": 1.0,
            "current_value": 1.0,
            "message": "Multi-agent BUY consensus reached",
            "is_active": True,
            "triggered_at": utcnow() - timedelta(minutes=30),
        },
    ]
    
    created = 0
    for alert in alerts:
        db.add(Alert(**alert))
        created += 1
    db.commit()
    print(f"  Created {created} alerts")


def create_demo_experiments(db):
    """Create demo experiments."""
    print("Creating demo experiments...")
    
    now = utcnow()
    
    experiments = [
        Experiment(
            name="Baseline Strategy v1",
            description="Initial baseline with default parameters",
            tags='["baseline", "default"]',
            config=json.dumps({
                "risk": {"max_risk_per_trade_pct": 1.0, "max_daily_drawdown_pct": 5.0},
                "strategy": {"sl_atr_mult": 1.5, "tp_atr_mult": 3.0},
            }),
            metrics=json.dumps({
                "total_return": 0.15,
                "sharpe": 1.2,
                "max_drawdown": 0.08,
                "win_rate": 0.52,
            }),
            status="completed",
            created_at=now - timedelta(days=7),
            completed_at=now - timedelta(days=2),
        ),
        Experiment(
            name="Aggressive SL/TP",
            description="Tighter stops, wider targets",
            tags='["aggressive", "sl_tp_test"]',
            config=json.dumps({
                "risk": {"max_risk_per_trade_pct": 1.5, "max_daily_drawdown_pct": 7.0},
                "strategy": {"sl_atr_mult": 1.0, "tp_atr_mult": 4.0},
            }),
            metrics="{}",
            status="running",
            created_at=now - timedelta(days=1),
        ),
        Experiment(
            name="Conservative Config",
            description="Lower risk, tighter targets",
            tags='["conservative", "low_risk"]',
            config=json.dumps({
                "risk": {"max_risk_per_trade_pct": 0.5, "max_daily_drawdown_pct": 3.0},
                "strategy": {"sl_atr_mult": 2.0, "tp_atr_mult": 2.0},
            }),
            metrics=json.dumps({
                "total_return": 0.08,
                "sharpe": 1.5,
                "max_drawdown": 0.04,
                "win_rate": 0.58,
            }),
            status="completed",
            created_at=now - timedelta(days=5),
            completed_at=now - timedelta(days=1),
        ),
    ]
    
    for exp in experiments:
        existing = db.query(Experiment).filter_by(name=exp.name).first()
        if not existing:
            db.add(exp)
    db.commit()
    print(f"  Created {len(experiments)} experiments")


def create_demo_backtests(db):
    """Create demo backtest runs."""
    print("Creating demo backtest runs...")
    
    now = utcnow()
    
    runs_data = [
        {
            "symbol": "XAUUSD",
            "timeframe": "15m",
            "start": now - timedelta(days=180),
            "end": now,
            "params": json.dumps({
                "risk": {"max_risk_per_trade_pct": 1.0, "max_daily_drawdown_pct": 5.0},
                "strategy": {"sl_atr_mult": 1.5, "tp_atr_mult": 3.0},
                "agents": {"weights": {"technical": 0.4, "quant": 0.3, "sentiment": 0.3}},
            }),
            "status": "completed",
            "starting_equity": 100000.0,
            "ending_equity": 123000.0,
            "total_pnl": 23000.0,
            "trade_count": 142,
            "win_rate": 0.54,
            "profit_factor": 1.8,
            "expectancy_r": 0.42,
            "max_drawdown": 0.11,
            "sharpe": 1.45,
            "sortino": 1.8,
            "equity_curve": json.dumps([
                [ (now - timedelta(days=180 - i)).isoformat(), round(100000 * (1 + 0.23 * (i / 180)), 2) ] 
                for i in range(0, 181, 7)
            ]),
            "trades_count": 10,
        },
        {
            "symbol": "EURUSD",
            "timeframe": "1h",
            "start": now - timedelta(days=90),
            "end": now,
            "params": json.dumps({
                "risk": {"max_risk_per_trade_pct": 1.0, "max_daily_drawdown_pct": 5.0},
                "strategy": {"sl_atr_mult": 1.5, "tp_atr_mult": 3.0},
                "agents": {"weights": {"technical": 0.4, "quant": 0.3, "sentiment": 0.3}},
            }),
            "status": "completed",
            "starting_equity": 50000.0,
            "ending_equity": 56000.0,
            "total_pnl": 6000.0,
            "trade_count": 89,
            "win_rate": 0.51,
            "profit_factor": 1.4,
            "expectancy_r": 0.28,
            "max_drawdown": 0.07,
            "sharpe": 1.1,
            "sortino": 1.4,
            "equity_curve": json.dumps([
                [ (now - timedelta(days=90 - i)).isoformat(), round(50000 * (1 + 0.12 * (i / 90)), 2) ] 
                for i in range(0, 91, 5)
            ]),
            "trades_count": 8,
        },
        {
            "symbol": "XAUUSD",
            "timeframe": "4h",
            "start": now - timedelta(days=365),
            "end": now,
            "params": json.dumps({
                "risk": {"max_risk_per_trade_pct": 1.0, "max_daily_drawdown_pct": 5.0},
                "strategy": {"sl_atr_mult": 2.0, "tp_atr_mult": 3.0},
                "agents": {"weights": {"technical": 0.5, "quant": 0.5}},
            }),
            "status": "completed",
            "starting_equity": 100000.0,
            "ending_equity": 131000.0,
            "total_pnl": 31000.0,
            "trade_count": 67,
            "win_rate": 0.56,
            "profit_factor": 2.1,
            "expectancy_r": 0.55,
            "max_drawdown": 0.15,
            "sharpe": 1.65,
            "sortino": 2.0,
            "equity_curve": json.dumps([
                [ (now - timedelta(days=365 - i)).isoformat(), round(100000 * (1 + 0.31 * (i / 365)), 2) ] 
                for i in range(0, 366, 30)
            ]),
            "trades_count": 5,
        },
    ]
    
    for run_data in runs_data:
        trades_count = run_data.pop("trades_count")
        run = BacktestRun(**run_data)
        db.add(run)
        db.flush()
        
        # Add sample trades for this run
        for i in range(trades_count):
            entry_time = run.start + timedelta(days=(i * (run.day_count() if hasattr(run, 'day_count') else 30)))
            exit_time = entry_time + timedelta(hours=4)
            direction = "LONG" if i % 2 == 0 else "SHORT"
            entry_price = 2000 + (i * 10)
            exit_price = entry_price + (30 if direction == "LONG" else -25)
            pnl = (exit_price - entry_price) * 10 * (1 if direction == "LONG" else -1)
            
            db.add(BacktestTrade(
                run_id=run.id,
                symbol=run.symbol,
                direction=direction,
                entry_time=entry_time,
                exit_time=exit_time,
                entry_price=entry_price,
                exit_price=exit_price,
                stop_loss=entry_price - (30 if direction == "LONG" else -30),
                take_profit=entry_price + (60 if direction == "LONG" else -60),
                quantity=10.0,
                pnl=pnl,
                r_multiple=pnl / 100.0,
                exit_reason="tp" if i % 3 != 0 else "sl",
            ))
    
    db.commit()
    print(f"  Created {len(runs_data)} backtest runs with sample trades")


def create_demo_agent_signals(db):
    """Create demo agent signals."""
    print("Creating demo agent signals...")
    
    now = utcnow()
    agents = ["technical", "quant", "sentiment", "fundamental"]
    symbols = ["XAUUSD", "EURUSD", "GBPUSD"]
    timeframes = ["15m", "1h", "4h"]
    
    signals = []
    for i in range(50):
        agent = agents[i % len(agents)]
        symbol = symbols[i % len(symbols)]
        timeframe = timeframes[i % len(timeframes)]
        direction = "LONG" if i % 3 != 0 else "SHORT"
        
        signals.append(AgentSignal(
            agent_name=agent,
            symbol=symbol,
            timeframe=timeframe,
            direction=direction,
            confidence=round(0.5 + (i % 50) / 100, 2),
            payload=json.dumps({
                "direction": direction,
                "confidence": round(0.5 + (i % 50) / 100, 2),
                "reasoning": f"{agent.capitalize()} analysis indicates {direction} opportunity",
                "features": {
                    "rsi": 50 + (i % 20) - 10,
                    "macd": (i % 10) - 5,
                    "trend": "bullish" if direction == "LONG" else "bearish",
                },
            }),
            llm_used=(agent in ["sentiment", "fundamental"]),
            created_at=now - timedelta(minutes=i * 30),
        ))
    
    db.add_all(signals)
    db.commit()
    print(f"  Created {len(signals)} agent signals")


def create_demo_decisions(db):
    """Create demo trading decisions."""
    print("Creating demo trading decisions...")
    
    now = utcnow()
    
    decisions = []
    for i in range(20):
        direction = "LONG" if i % 2 == 0 else "SHORT"
        action = "BUY" if direction == "LONG" else "SELL"
        
        decisions.append(TradingDecision(
            symbol="XAUUSD",
            timeframe="15m",
            action=action,
            direction=direction,
            confidence=round(0.6 + (i % 40) / 100, 2),
            entry=2000 + i * 5,
            stop_loss=2000 + i * 5 - (30 if direction == "LONG" else -30),
            take_profit=2000 + i * 5 + (60 if direction == "LONG" else -60),
            risk_reward=2.0,
            evidence=json.dumps({
                "technical": {"score": 0.75, "trend": "bullish"},
                "quant": {"score": 0.68, "momentum": "positive"},
                "sentiment": {"score": 0.62, "news_sentiment": "positive"},
            }),
            agent_conflicts="[]",
            decision_reason=f"Combined agent consensus: {action} with high confidence",
            created_at=now - timedelta(hours=i * 2),
        ))
    
    db.add_all(decisions)
    db.commit()
    print(f"  Created {len(decisions)} trading decisions")


def create_demo_positions(db):
    """Create demo open positions."""
    print("Creating demo positions...")
    
    now = utcnow()
    
    positions = [
        Position(
            symbol="XAUUSD",
            direction="LONG",
            quantity=0.5,
            entry_price=2015.50,
            stop_loss=1985.00,
            take_profit=2075.00,
            opened_at=now - timedelta(hours=3),
            status="open",
        ),
        Position(
            symbol="EURUSD",
            direction="SHORT",
            quantity=1.0,
            entry_price=1.0850,
            stop_loss=1.0950,
            take_profit=1.0650,
            opened_at=now - timedelta(hours=12),
            status="open",
        ),
    ]
    
    for pos in positions:
        existing = db.query(Position).filter_by(symbol=pos.symbol, direction=pos.direction, status="open").first()
        if not existing:
            db.add(pos)
    db.commit()
    print(f"  Created {len(positions)} open positions")


def create_demo_system_events(db):
    """Create demo system events."""
    print("Creating demo system events...")
    
    now = utcnow()
    
    events = [
        SystemEvent(
            level="INFO",
            component="api",
            message="Application started",
            details='{"version": "1.0.0"}',
            created_at=now - timedelta(hours=2),
        ),
        SystemEvent(
            level="INFO",
            component="data",
            message="Historical data loaded for XAUUSD 15m",
            details='{"bars": 5000}',
            created_at=now - timedelta(hours=1, minutes=45),
        ),
        SystemEvent(
            level="INFO",
            component="pipeline",
            message="Analysis cycle completed",
            details='{"signals": 12, "decisions": 3}',
            created_at=now - timedelta(minutes=30),
        ),
        SystemEvent(
            level="WARNING",
            component="provider",
            message="TwelveData rate limit approaching",
            details='{"remaining": 15}',
            created_at=now - timedelta(minutes=15),
        ),
        SystemEvent(
            level="INFO",
            component="paper",
            message="Paper order filled: BUY 0.5 XAUUSD @ 2015.50",
            details='{"order_id": "paper-123"}',
            created_at=now - timedelta(minutes=10),
        ),
        SystemEvent(
            level="INFO",
            component="backtest",
            message="Backtest run completed: XAUUSD 15m - 6 Month Walk-Forward",
            details='{"return": 0.23, "sharpe": 1.45}',
            created_at=now - timedelta(minutes=5),
        ),
    ]
    
    db.add_all(events)
    db.commit()
    print(f"  Created {len(events)} system events")


def main():
    print("=== Trading Framework Seed Data ===\n")
    
    # Initialize database
    init_db()
    
    SessionLocal = get_session_factory()
    db = SessionLocal()
    
    try:
        create_demo_watchlist(db)
        create_demo_alerts(db)
        create_demo_experiments(db)
        create_demo_backtests(db)
        create_demo_agent_signals(db)
        create_demo_decisions(db)
        create_demo_positions(db)
        create_demo_system_events(db)
        
        print("\n=== Seed Data Complete ===")
        print("Demo data has been populated in the database.")
        print("You can now run the application and see sample data in the UI.")
        
    except Exception as e:
        print(f"\nError: {e}")
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
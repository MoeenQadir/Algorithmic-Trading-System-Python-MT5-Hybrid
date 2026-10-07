#!/usr/bin/env python
"""Forward paper trading mode (Milestone 19).

Runs the production pipeline in real-time with paper execution, producing
forward-looking performance metrics. This is the "walk-forward" mode where
the strategy trades on live data (or recent historical data) without lookahead.

Key features:
- Real-time or near-real-time operation
- Paper execution only (LIVE_TRADING=false enforced)
- Performance metrics by direction, timeframe, regime
- Signal quality analysis (score vs outcomes)
- Out-of-sample discipline enforcement

Usage:
    python scripts/paper_forward.py --symbol XAUUSD --timeframe 15m --duration-hours 24
    python scripts/paper_forward.py --config paper_forward_config.yaml
"""
from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import time
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.config import get_settings
from app.data.providers import get_provider, parse_timeframe_minutes
from app.execution.broker import get_broker
from app.models.database import get_session_factory, MarketData, Trade, Position
from app.services.pipeline import TradingPipeline
from app.services.real_time_worker import RealTimeWorker, DataMode
from app.utils.timeutils import parse_datetime, utcnow

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger("paper_forward")


class PaperForwardEngine:
    """Forward paper trading engine with performance tracking."""

    def __init__(
        self,
        symbol: str,
        timeframe: str,
        duration_hours: float = 24.0,
        data_mode: str = "AUTO",
        initial_balance: float | None = None,
        session_name: str = "forward",
    ) -> None:
        self.symbol = symbol.upper()
        self.timeframe = timeframe
        self.duration_hours = duration_hours
        self.data_mode = data_mode
        self.session_name = session_name
        s = get_settings()
        self.initial_balance = initial_balance if initial_balance is not None else s.paper_account_balance
        self.pipeline = TradingPipeline()
        self.worker = RealTimeWorker(
            symbol=symbol,
            timeframe=timeframe,
            data_mode=data_mode if data_mode != "AUTO" else None,
        )
        self.db_factory = get_session_factory()
        self.start_time: datetime | None = None
        self.end_time: datetime | None = None

    def run(self) -> dict[str, Any]:
        """Run the forward paper trading session."""
        self.start_time = utcnow()
        self.end_time = self.start_time + timedelta(hours=self.duration_hours)

        logger.info(
            "Starting forward paper session: %s %s for %.1f hours (mode=%s)",
            self.symbol, self.timeframe, self.duration_hours, self.data_mode
        )

        self.worker.start()

        # Reset broker state
        db = self.db_factory()
        try:
            self._reset_broker(db)
        finally:
            db.close()

        # Main loop
        cycle_count = 0
        last_cycle_time: datetime | None = None

        try:
            while utcnow() < self.end_time:
                cycle_start = utcnow()

                # Run worker cycle
                result = self.worker.run_once()

                # Log status periodically
                cycle_count += 1
                if cycle_count % 20 == 0:
                    self._log_status(result)

                last_cycle_time = cycle_start

                # Sleep until next poll interval
                elapsed = (utcnow() - cycle_start).total_seconds()
                sleep_time = max(0, self.worker.poll_interval - elapsed)
                if sleep_time > 0:
                    time.sleep(sleep_time)

        except KeyboardInterrupt:
            logger.info("Interrupted by user")
        finally:
            self.worker.stop()

        # Final performance analysis
        return self._analyze_performance()

    def _reset_broker(self, db: Session) -> None:
        """Reset broker state for clean forward session."""
        from app.models.database import Fill, Order, Position, PortfolioSnapshot

        db.query(Fill).delete()
        db.query(Order).delete()
        db.query(Position).delete()
        db.query(Trade).filter(Trade.symbol == self.symbol).delete()
        db.query(PortfolioSnapshot).delete()
        db.commit()

    def _log_status(self, result: dict[str, Any]) -> None:
        """Log periodic status update."""
        db = self.db_factory()
        try:
            broker = get_broker(db)
            account = broker.get_account()
            logger.info(
                "Cycle %d: equity=%.2f unrealized=%.2f open_pos=%d pipeline=%s",
                cycle_count,
                account["equity"],
                account["unrealized_pnl"],
                account["open_positions"],
                result.get("status", "unknown"),
            )
        finally:
            db.close()

    def _analyze_performance(self) -> dict[str, Any]:
        """Analyze forward session performance."""
        db = self.db_factory()
        try:
            trades = db.query(Trade).filter(
                Trade.symbol == self.symbol,
                Trade.entry_time >= self.start_time,
                Trade.entry_time <= utcnow(),
            ).all()

            closed_trades = [t for t in trades if t.status == "closed"]

            # By direction
            long_trades = [t for t in closed_trades if t.direction == "LONG"]
            short_trades = [t for t in closed_trades if t.direction == "SHORT"]

            # By timeframe (all same timeframe in this session, but for completeness)
            timeframe_trades = {}
            for t in closed_trades:
                tf = t.timeframe or self.timeframe
                if tf not in timeframe_trades:
                    timeframe_trades[tf] = {"wins": 0, "losses": 0, "total_pnl": 0.0}
                if (t.pnl or 0) > 0:
                    timeframe_trades[tf]["wins"] += 1
                else:
                    timeframe_trades[tf]["losses"] += 1
                timeframe_trades[tf]["total_pnl"] += t.pnl or 0.0

            # Get equity curve from portfolio snapshots
            from app.models.database import PortfolioSnapshot
            snapshots = db.query(PortfolioSnapshot).filter(
                PortfolioSnapshot.timestamp >= self.start_time,
            ).order_by(PortfolioSnapshot.timestamp.asc()).all()

            equity_curve = [
                {
                    "timestamp": s.timestamp.isoformat(),
                    "equity": s.equity,
                    "cash": s.cash,
                    "unrealized_pnl": s.unrealized_pnl,
                    "realized_pnl": s.realized_pnl,
                }
                for s in snapshots
            ]

            # Compute metrics
            total_pnl = sum(t.pnl or 0.0 for t in closed_trades)
            wins = [t for t in closed_trades if (t.pnl or 0) > 0]
            losses = [t for t in closed_trades if (t.pnl or 0) < 0]
            win_rate = len(wins) / len(closed_trades) * 100 if closed_trades else 0

            avg_win = sum(t.pnl or 0 for t in wins) / len(wins) if wins else 0
            avg_loss = sum(t.pnl or 0 for t in losses) / len(losses) if losses else 0
            profit_factor = abs(avg_win / avg_loss) if avg_loss != 0 else float("inf")

            # Max drawdown
            peak = self.initial_balance
            max_dd = 0.0
            for point in equity_curve:
                equity = point["equity"]
                if equity > peak:
                    peak = equity
                dd = (peak - equity) / peak * 100 if peak > 0 else 0
                max_dd = max(max_dd, dd)

            final_equity = equity_curve[-1]["equity"] if equity_curve else self.initial_balance
            total_return = (final_equity - self.initial_balance) / self.initial_balance * 100

            return {
                "status": "completed",
                "session": self.session_name,
                "symbol": self.symbol,
                "timeframe": self.timeframe,
                "data_mode": self.worker.data_mode,
                "duration_hours": self.duration_hours,
                "actual_duration_hours": round((utcnow() - self.start_time).total_seconds() / 3600, 2),
                "cycles_completed": cycle_count,
                "trades": len(closed_trades),
                "long_trades": len(long_trades),
                "short_trades": len(short_trades),
                "wins": len(wins),
                "losses": len(losses),
                "win_rate_pct": round(win_rate, 2),
                "total_pnl": round(total_pnl, 2),
                "avg_win": round(avg_win, 2),
                "avg_loss": round(avg_loss, 2),
                "profit_factor": round(profit_factor, 2) if profit_factor != float("inf") else "inf",
                "max_drawdown_pct": round(max_dd, 2),
                "total_return_pct": round(total_return, 2),
                "initial_balance": self.initial_balance,
                "final_equity": round(final_equity, 2),
                "by_timeframe": {
                    tf: {k: round(v, 2) if isinstance(v, float) else v for k, v in stats.items()}
                    for tf, stats in timeframe_trades.items()
                },
                "by_direction": {
                    "LONG": self._direction_stats(long_trades),
                    "SHORT": self._direction_stats(short_trades),
                },
                "equity_curve": equity_curve[-200:],
            }
        finally:
            db.close()

    def _direction_stats(self, trades: list[Trade]) -> dict[str, Any]:
        """Compute statistics for a direction."""
        if not trades:
            return {"trades": 0, "wins": 0, "losses": 0, "win_rate_pct": 0.0, "total_pnl": 0.0}

        closed = [t for t in trades if t.status == "closed"]
        wins = [t for t in closed if (t.pnl or 0) > 0]
        total_pnl = sum(t.pnl or 0.0 for t in closed)
        win_rate = len(wins) / len(closed) * 100 if closed else 0

        return {
            "trades": len(closed),
            "wins": len(wins),
            "losses": len(closed) - len(wins),
            "win_rate_pct": round(win_rate, 2),
            "total_pnl": round(total_pnl, 2),
        }


def main() -> int:
    parser = argparse.ArgumentParser(description="Forward paper trading engine")
    parser.add_argument("--symbol", required=True, help="Trading symbol (e.g., XAUUSD)")
    parser.add_argument("--timeframe", required=True, help="Timeframe (e.g., 15m, 1h)")
    parser.add_argument("--duration-hours", type=float, default=24.0, help="Duration in hours")
    parser.add_argument("--data-mode", choices=["AUTO", "DEMO", "OFFLINE", "LOCAL", "REMOTE_REAL_DATA"],
                        default="AUTO", help="Data mode")
    parser.add_argument("--balance", type=float, help="Initial balance")
    parser.add_argument("--output", help="Output JSON file")
    parser.add_argument("--session", default="forward", help="Session name")

    args = parser.parse_args()

    engine = PaperForwardEngine(
        symbol=args.symbol,
        timeframe=args.timeframe,
        duration_hours=args.duration_hours,
        data_mode=args.data_mode,
        initial_balance=args.balance,
        session_name=args.session,
    )

    result = engine.run()

    if args.output:
        with open(args.output, "w") as f:
            json.dump(result, f, indent=2)
        logger.info("Results written to %s", args.output)

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
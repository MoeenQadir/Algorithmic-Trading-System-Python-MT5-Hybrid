#!/usr/bin/env python
"""Historical replay engine (Milestone 18).

Runs the exact same pipeline (DATA -> ANALYSIS -> AGENTS -> DECISION ->
RISK -> EXECUTION -> JOURNAL) over historical data as would run in real-time.
This ensures backtest/replay consistency - same decisions for same data.

Usage:
    python scripts/replay.py --symbol XAUUSD --timeframe 15m --start 2024-01-01 --end 2024-12-31
    python scripts/replay.py --config replay_config.yaml
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime
from typing import Any, Optional

# Allow `app.*` imports (the backend package root) regardless of caller cwd.
_BACKEND_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend")
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)

from sqlalchemy.orm import Session

from app.backtesting.engine import BacktestEngine
from app.config import get_settings
from app.execution.broker import get_broker
from app.models.database import get_session_factory, Trade
from app.services.pipeline import TradingPipeline
from app.utils.timeutils import parse_datetime, utcnow

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger("replay")


class ReplayEngine:
    """Historical replay engine that runs the production pipeline over past data."""

    def __init__(
        self,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
        initial_balance: float | None = None,
        session_name: str = "replay",
    ) -> None:
        self.symbol = symbol.upper()
        self.timeframe = timeframe
        self.start = start
        self.end = end
        self.session_name = session_name
        s = get_settings()
        self.initial_balance = initial_balance if initial_balance is not None else s.paper_account_balance
        self.pipeline = TradingPipeline()
        self.db_factory = get_session_factory()

    def run(self) -> dict[str, Any]:
        """Run the replay and return summary statistics."""
        logger.info(
            "Starting replay: %s %s from %s to %s",
            self.symbol, self.timeframe, self.start.date(), self.end.date()
        )

        db = self.db_factory()
        try:
            # Reset broker state for clean replay
            broker = get_broker(db)
            # Clear existing trades/positions for this replay session
            # (In production, we'd use a separate database or session isolation)
            self._reset_broker(db)

            # Get all candles in range
            candles = self._load_candles(db)
            if not candles:
                logger.warning("No candles found for %s %s in range", self.symbol, self.timeframe)
                return {"status": "no_data", "trades": 0}

            logger.info("Loaded %d candles for replay", len(candles))

            # Run pipeline for each completed candle
            trades_executed = 0
            equity_curve: list[dict[str, Any]] = []

            for i in range(len(candles)):
                # Build candle window up to current index (simulating real-time)
                window = candles[:i + 1]
                if len(window) < 60:
                    continue  # Skip until we have enough data

                # Run pipeline cycle with this window
                # We override get_candles to use our window
                result = self._run_cycle_with_window(window)

                # Track equity
                account = broker.get_account()
                equity_curve.append({
                    "timestamp": window[-1]["timestamp"].isoformat(),
                    "equity": account["equity"],
                    "cash": account["cash"],
                    "unrealized_pnl": account["unrealized_pnl"],
                    "realized_pnl": account["realized_pnl"],
                })

                if result.get("execution", {}).get("ok"):
                    trades_executed += 1

                # Also check stops/targets on each candle
                broker.check_stops_and_targets()

            # Final snapshot
            final_account = broker.get_account()

            # Collect trades
            trades = db.query(Trade).filter(
                Trade.symbol == self.symbol,
                Trade.entry_time >= self.start,
                Trade.entry_time <= self.end,
            ).all()

            summary = self._compute_summary(trades, equity_curve, final_account)

            logger.info("Replay complete: %d trades executed", trades_executed)
            return summary

        finally:
            db.close()

    def _reset_broker(self, db: Session) -> None:
        """Reset broker state for clean replay."""
        from app.models.database import Fill, Order, Position, PortfolioSnapshot

        db.query(Fill).delete()
        db.query(Order).delete()
        db.query(Position).delete()
        db.query(Trade).filter(Trade.symbol == self.symbol).delete()
        db.query(PortfolioSnapshot).delete()
        db.commit()

    def _load_candles(self, db: Session) -> list[dict[str, Any]]:
        """Load candles from database for the replay window."""
        from app.models.database import MarketData

        rows = (
            db.query(MarketData)
            .filter(
                MarketData.symbol == self.symbol,
                MarketData.timeframe == self.timeframe,
                MarketData.timestamp >= self.start,
                MarketData.timestamp <= self.end,
            )
            .order_by(MarketData.timestamp.asc())
            .all()
        )

        return [
            {
                "timestamp": r.timestamp,
                "symbol": r.symbol,
                "timeframe": r.timeframe,
                "open": r.open,
                "high": r.high,
                "low": r.low,
                "close": r.close,
                "volume": r.volume,
                "bid": r.bid,
                "ask": r.ask,
                "spread": r.spread,
                "source": r.source,
            }
            for r in rows
        ]

    def _run_cycle_with_window(self, candles: list[dict[str, Any]]) -> dict[str, Any]:
        """Run a single pipeline cycle using the provided candle window."""
        # Temporarily monkey-patch get_candles to return our window
        original_get_candles = self.pipeline.get_candles

        def mock_get_candles(symbol: str, timeframe: str, limit: int = 400) -> list[dict[str, Any]]:
            return candles[-limit:]

        self.pipeline.get_candles = mock_get_candles
        try:
            return self.pipeline.run_cycle(self.symbol, self.timeframe, execute=True)
        finally:
            self.pipeline.get_candles = original_get_candles

    def _compute_summary(
        self,
        trades: list[Trade],
        equity_curve: list[dict[str, Any]],
        final_account: dict[str, Any],
    ) -> dict[str, Any]:
        """Compute replay summary statistics."""
        closed_trades = [t for t in trades if t.status == "closed"]

        if not closed_trades:
            return {
                "status": "completed",
                "trades": 0,
                "initial_balance": self.initial_balance,
                "final_equity": final_account["equity"],
                "total_return_pct": 0.0,
            }

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

        total_return = (final_account["equity"] - self.initial_balance) / self.initial_balance * 100

        return {
            "status": "completed",
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "period": f"{self.start.date()} to {self.end.date()}",
            "trades": len(closed_trades),
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
            "final_equity": round(final_account["equity"], 2),
            "equity_curve": equity_curve[-100:],  # Last 100 points
        }


def main() -> int:
    parser = argparse.ArgumentParser(description="Historical replay engine")
    parser.add_argument("--symbol", required=True, help="Trading symbol (e.g., XAUUSD)")
    parser.add_argument("--timeframe", required=True, help="Timeframe (e.g., 15m, 1h)")
    parser.add_argument("--start", required=True, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", required=True, help="End date (YYYY-MM-DD)")
    parser.add_argument("--balance", type=float, help="Initial balance")
    parser.add_argument("--output", help="Output JSON file")
    parser.default_argument = None
    parser.add_argument("--session", default="replay", help="Session name")

    args = parser.parse_args()

    try:
        start = parse_datetime(args.start)
        end = parse_datetime(args.end)
    except Exception as e:
        logger.error("Invalid date format: %s", e)
        return 1

    engine = ReplayEngine(
        symbol=args.symbol,
        timeframe=args.timeframe,
        start=start,
        end=end,
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
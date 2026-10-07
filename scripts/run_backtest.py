"""Run a local backtest from candles already present in the application database."""
from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
os.chdir(ROOT)

from app.backtesting.engine import BacktestEngine
from app.models.database import MarketData, get_session_factory, init_db


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="US100")
    parser.add_argument("--timeframe", default="15m")
    parser.add_argument("--limit", type=int, default=5000)
    parser.add_argument("--equity", type=float, default=10000.0)
    args = parser.parse_args()
    if args.limit < 150 or args.equity <= 0:
        parser.error("--limit must be at least 150 and --equity must be positive")

    init_db()
    session = get_session_factory()()
    try:
        rows = (
            session.query(MarketData)
            .filter(MarketData.symbol == args.symbol.upper(), MarketData.timeframe == args.timeframe)
            .order_by(MarketData.timestamp.asc())
            .limit(args.limit)
            .all()
        )
        candles = [
            {"timestamp": row.timestamp, "open": row.open, "high": row.high,
             "low": row.low, "close": row.close, "volume": row.volume}
            for row in rows
        ]
        result = BacktestEngine({"starting_equity": args.equity, "max_atr_pct": 0.05}).run(
            candles, args.symbol.upper(), args.timeframe
        )
        if result.get("error"):
            print(result["error"], file=sys.stderr)
            return 1
        print(json.dumps({"run": result["run"], "trades": result["trades"]}, indent=2, default=str))
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())

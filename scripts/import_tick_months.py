"""Rebuild sequential 1.csv..N.csv tick months into SQLite OHLC candles."""
from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
os.chdir(ROOT)

from app.data.tick_import import import_tick_months
from app.models.database import get_session_factory, init_db


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", required=True)
    parser.add_argument("--symbol", default="XAUUSD")
    parser.add_argument("--timeframe", default="4h")
    parser.add_argument("--year", type=int, default=2026)
    parser.add_argument("--source-month", type=int, default=1,
                        help="month recorded in every source file before applying file-number months")
    parser.add_argument("--batch-size", type=int, default=5000,
                        help="maximum candle rows held before committing")
    args = parser.parse_args()

    init_db()
    session = get_session_factory()()

    def report_file(name: str, ticks: int) -> None:
        print(f"{name}: read {ticks:,} ticks", file=sys.stderr, flush=True)

    try:
        stats = import_tick_months(
            session,
            args.input_dir,
            args.symbol,
            args.timeframe,
            year=args.year,
            source_month=args.source_month,
            batch_size=args.batch_size,
            progress=report_file,
        )
        print(json.dumps({
            "symbol": args.symbol.upper(),
            "timeframe": args.timeframe,
            "year": args.year,
            **stats,
        }, indent=2))
        return 0
    except (OSError, ValueError) as exc:
        print(f"Tick import failed: {exc}", file=sys.stderr)
        return 1
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())

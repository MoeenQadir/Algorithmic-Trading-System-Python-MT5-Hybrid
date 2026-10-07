"""Stream a local CSV candle file into the configured database."""
from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
os.chdir(ROOT)

from app.data.csv_import import import_csv
from app.models.database import get_session_factory, init_db


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", required=True, help="Path to a UTF-8 CSV candle file")
    parser.add_argument("--symbol", default="XAUUSD")
    parser.add_argument("--timeframe", default="15m")
    parser.add_argument("--batch-size", type=int, default=5000)
    args = parser.parse_args()

    init_db()
    session = get_session_factory()()
    try:
        result = import_csv(session, args.file, args.symbol, args.timeframe, batch_size=args.batch_size)
        print(json.dumps({"symbol": args.symbol.upper(), "timeframe": args.timeframe, **result}, indent=2))
        return 0
    except (OSError, ValueError) as exc:
        print(f"Import failed: {exc}", file=sys.stderr)
        return 1
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())

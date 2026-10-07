#!/usr/bin/env python
"""Real-time worker entry point.

Run the real-time market data worker that polls providers, processes
completed candles, and triggers the trading pipeline.

Usage:
    python scripts/real_time_worker.py --symbol XAUUSD --timeframe 15m
    python scripts/real_time_worker.py --data-mode REMOTE_REAL_DATA
"""
from __future__ import annotations

import argparse
import logging
import sys

from app.config import get_settings
from app.services.real_time_worker import run_real_time_worker, RealTimeWorker, DataMode
from app.utils.timeutils import utcnow

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger("worker_script")


def main() -> int:
    parser = argparse.ArgumentParser(description="Real-time market data worker")
    parser.add_argument("--symbol", help="Trading symbol (default: from config)")
    parser.add_argument("--timeframe", help="Timeframe (default: from config)")
    parser.add_argument("--interval", type=int, help="Poll interval in seconds")
    parser.add_argument("--data-mode", choices=["AUTO", "DEMO", "OFFLINE", "LOCAL", "REMOTE_REAL_DATA"],
                        default="AUTO", help="Data mode")
    parser.add_argument("--max-cycles", type=int, default=0, help="Max cycles before exit (0=infinite)")

    args = parser.parse_args()

    s = get_settings()
    symbol = args.symbol or s.default_symbol
    timeframe = args.timeframe or s.default_timeframe
    interval = args.interval or s.real_time_poll_interval_sec
    data_mode = args.data_mode

    logger.info(
        "Starting worker: symbol=%s timeframe=%s interval=%ds mode=%s",
        symbol, timeframe, interval, data_mode
    )

    # Create worker with custom settings
    worker = RealTimeWorker(
        symbol=symbol,
        timeframe=timeframe,
        poll_interval_sec=interval,
        data_mode=data_mode if data_mode != "AUTO" else None,
    )
    worker.start()

    cycle_count = 0
    try:
        while worker.running:
            result = worker.run_once()

            cycle_count += 1
            if args.max_cycles > 0 and cycle_count >= args.max_cycles:
                logger.info("Reached max cycles (%d), stopping", args.max_cycles)
                worker.stop()
                break

            if result.get("status") == "error":
                backoff = result.get("backoff_sec", worker.poll_interval)
                logger.debug("Backing off for %.1f sec", backoff)
                import time
                time.sleep(backoff)
            else:
                import time
                time.sleep(worker.poll_interval)

    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        worker.stop()

    return 0


if __name__ == "__main__":
    sys.exit(main())
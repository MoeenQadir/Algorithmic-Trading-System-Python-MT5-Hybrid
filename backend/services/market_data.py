import sqlite3
import polars as pl
from datetime import datetime, timedelta
from typing import List, Dict
from .providers.mt5_provider import MT5Provider

class MarketDataService:
    def __init__(self, db_path: str = 'market_data.db'):
        self.db = sqlite3.connect(db_path)
        self.mt5 = MT5Provider()
        self._init_db()

    def _init_db(self):
        cursor = self.db.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS candles (
                symbol TEXT,
                timeframe TEXT,
                timestamp INTEGER,
                open REAL,
                high REAL,
                low REAL,
                close REAL,
                volume INTEGER,
                PRIMARY KEY (symbol, timeframe, timestamp)
            )
        ''')
        self.db.commit()

    def get_candles(self, symbol: str, timeframe: str, limit: int = 1000) -> pl.DataFrame:
        # Try to get from DB first
        df = pl.read_sql(
            f"SELECT * FROM candles WHERE symbol = '{symbol}' AND timeframe = '{timeframe}' ORDER BY timestamp DESC LIMIT {limit}",
            self.db
        )

        if df.is_empty():
            # Fallback to MT5 if no data
            end = datetime.now()
            start = end - timedelta(days=30)
            candles = self.mt5.fetch_candles(symbol, timeframe, start, end)
            if candles:
                self._save_candles(symbol, timeframe, candles)
                return self.get_candles(symbol, timeframe, limit)

        return df

    def resample_candles(self, df: pl.DataFrame, target_tf: str) -> pl.DataFrame:
        if df.is_empty():
            return df

        return df.with_columns(
            pl.col("timestamp").dt.truncate(target_tf).alias("timeframe")
        ).group_by("timeframe").agg([
            pl.col("open").first(),
            pl.col("high").max(),
            pl.col("low").min(),
            pl.col("close").last(),
            pl.col("volume").sum()
        ])

    def _save_candles(self, symbol: str, timeframe: str, candles: List[Dict]):
        # Implementation for saving candles to DB
        pass

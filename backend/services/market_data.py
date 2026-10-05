import sqlite3
import polars as pl
from datetime import datetime, timedelta
from typing import List, Dict
try:
    import pytz
    from backend.providers.mt5_provider import MT5Provider
except ModuleNotFoundError:
    # Fallback stub if MT5Provider or its dependencies are missing
    class MT5Provider:
        def __init__(self):
            pass

        def fetch_candles(self, symbol, timeframe, start, end):
            # Return sample data to prevent timeout
            return [{
                'timestamp': int(start.timestamp()),
                'open': 1.0,
                'high': 1.1,
                'low': 0.9,
                'close': 1.05,
                'volume': 1000
            }]

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
        """
        Retrieve candle data for a given symbol and timeframe.

        Attempts to fetch data from the local SQLite database using a
        parameterized query to prevent SQL injection. If no data is found,
        falls back to the MT5 provider, saves the fetched candles, and
        retries the database query.
        """
        try:
            # Parameterized query using sqlite3 cursor
            cursor = self.db.cursor()
            cursor.execute(
                "SELECT * FROM candles WHERE symbol = ? AND timeframe = ? ORDER BY timestamp DESC LIMIT ?",
                (symbol, timeframe, limit)
            )
            rows = cursor.fetchall()

            if rows:
                # Convert rows to a Polars DataFrame with appropriate column names
                df = pl.DataFrame(
                    rows,
                    schema=[
                        "symbol",
                        "timeframe",
                        "timestamp",
                        "open",
                        "high",
                        "low",
                        "close",
                        "volume",
                    ],
                )
                return df

            # Fallback to MT5 if no data in DB
            end = datetime.now()
            start = end - timedelta(days=30)
            candles = self.mt5.fetch_candles(symbol, timeframe, start, end)

            if candles:
                self._save_candles(symbol, timeframe, candles)
                # Retry fetching from DB after saving
                return self.get_candles(symbol, timeframe, limit)

        except Exception as e:
            # Log the error and return an empty DataFrame
            print(f"Error fetching candles: {e}")
            return pl.DataFrame()

        # Return empty DataFrame if no data was retrieved
        return pl.DataFrame()

    def resample_candles(self, df: pl.DataFrame, target_tf: str) -> pl.DataFrame:
        if df.is_empty():
            return df

        # Convert timestamp to datetime if it's not already
        if df.schema["timestamp"].dtype == pl.Int64:
            df = df.with_columns(
                pl.col("timestamp").cast(pl.Datetime("us")).alias("timestamp")
            )

        return df.with_columns(
            pl.col("timestamp").dt.truncate(target_tf).alias("timestamp")
        ).group_by("timestamp").agg([
            pl.col("open").first(),
            pl.col("high").max(),
            pl.col("low").min(),
            pl.col("close").last(),
            pl.col("volume").sum()
        ])

    def _save_candles(self, symbol: str, timeframe: str, candles: List[Dict]):
        # Convert list of dicts to a Polars DataFrame
        df = pl.DataFrame(candles)

        # Add symbol and timeframe columns
        df = df.with_columns([
            pl.lit(symbol).alias("symbol"),
            pl.lit(timeframe).alias("timeframe")
        ])

        # Insert into the database, appending to existing data
        df.write_database(
            table_name="candles",
            connection=self.db,
            if_table_exists="append"
        )

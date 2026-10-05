import MetaTrader5 as mt5
from datetime import datetime, timedelta
import pytz

class MT5Provider:
    def __init__(self):
        self.connected = False

    def connect(self):
        if not self.connected:
            self.connected = mt5.initialize()
        return self.connected

    def fetch_candles(self, symbol: str, timeframe: str, start_date: datetime, end_date: datetime):
        if not self.connect():
            raise ConnectionError("Failed to connect to MT5")

        tf_mapping = {
            '1m': mt5.TIMEFRAME_M1,
            '5m': mt5.TIMEFRAME_M5,
            '15m': mt5.TIMEFRAME_M15,
            '30m': mt5.TIMEFRAME_M30,
            '1h': mt5.TIMEFRAME_H1,
            '4h': mt5.TIMEFRAME_H4,
            '1d': mt5.TIMEFRAME_D1
        }

        rates = mt5.copy_rates_range(
            symbol,
            tf_mapping[timeframe],
            start_date,
            end_date
        )

        return rates if rates is not None else []

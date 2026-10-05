import time
from datetime import datetime
from .services.market_data import MarketDataService
from .providers.mt5_provider import MT5Provider

class MT5LivePoller:
    def __init__(self):
        self.data_service = MarketDataService()
        self.mt5 = MT5Provider()
        self.active_symbols = ['XAUUSD', 'EURUSD', 'US100']

    def run(self):
        while True:
            try:
                for symbol in self.active_symbols:
                    self._check_new_data(symbol)
                time.sleep(15)  # Check every 15 seconds
            except Exception as e:
                print(f"Polling error: {e}")
                time.sleep(60)

    def _check_new_data(self, symbol: str):
        latest_db = self.data_service.get_candles(symbol, '1m', 1)
        last_ts = latest_db['timestamp'][0] if not latest_db.is_empty() else 0

        new_data = self.mt5.fetch_candles(
            symbol,
            '1m',
            datetime.fromtimestamp(last_ts) if last_ts else datetime.now() - timedelta(minutes=5),
            datetime.now()
        )

        if new_data:
            self.data_service._save_candles(symbol, '1m', new_data)
            self._trigger_analysis(symbol)

    def _trigger_analysis(self, symbol: str):
        # Implementation to trigger analysis pipeline
        pass

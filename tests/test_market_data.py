import pytest
from backend.services.market_data import MarketDataService
from datetime import datetime, timedelta

@pytest.fixture
def data_service():
    return MarketDataService(':memory:')

def test_fetch_missing_data(data_service):
    df = data_service.get_candles('EURUSD', '1h')
    assert not df.is_empty()
    assert 'open' in df.columns
    assert len(df) > 0

def test_resample_data(data_service):
    # Test resampling logic
    pass

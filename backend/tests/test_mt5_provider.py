"""Unit tests for MT5DataProvider with mocked MetaTrader5.

Strategy:
- The real MetaTrader5 module is Windows-only and not installed in CI, so we
  patch `app.data.mt5_provider.mt5` and `MT5_AVAILABLE` for every test.
- Sample data mirrors the exact NumPy structured array that
  `mt5.copy_rates_from_pos()` returns.
"""

from __future__ import annotations

import platform
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from app.data import mt5_provider as mt5_mod
from app.data.mt5_provider import MT5DataProvider

SAMPLE_DTYPE = np.dtype([
    ("time", np.int64),
    ("open", np.float64),
    ("high", np.float64),
    ("low", np.float64),
    ("close", np.float64),
    ("tick_volume", np.int64),
    ("spread", np.int32),
    ("real_volume", np.int64),
])


def _make_rates(count: int = 3, base_ts: int = 1760000000, step: int = 900) -> np.ndarray:
    """Build a sample MT5 structured array (15m candles by default)."""
    rows = []
    for i in range(count):
        o = 1800.0 + i
        rows.append((
            base_ts + i * step, o, o + 10.0, o - 5.0, o + 5.0,
            100 + i * 50, 1, 100 + i * 50,
        ))
    return np.array(rows, dtype=SAMPLE_DTYPE)


@pytest.fixture
def mock_mt5():
    """Patch the MT5 module + availability flag inside the provider module."""
    fake = MagicMock()
    fake.TIMEFRAME_M1 = 1
    fake.TIMEFRAME_M5 = 5
    fake.TIMEFRAME_M15 = 15
    fake.TIMEFRAME_M30 = 30
    fake.TIMEFRAME_H1 = 60
    fake.TIMEFRAME_H4 = 240
    fake.TIMEFRAME_D1 = 1440
    fake.initialize.return_value = True
    fake.account_info.return_value = MagicMock(login=12345)
    fake.terminal_info.return_value = MagicMock()
    fake.symbol_info.return_value = MagicMock(visible=True)
    with patch.object(mt5_mod, "mt5", fake), \
         patch.object(mt5_mod, "MT5_AVAILABLE", True), \
         patch.object(mt5_mod.platform, "system", return_value="Windows"):
        yield fake


@pytest.fixture
def provider(mock_mt5):
    """Available provider instance (mocked terminal, forced-initialized)."""
    p = MT5DataProvider()
    p._initialized = True
    return p


class TestAvailability:
    def test_unavailable_when_module_missing(self):
        with patch.object(mt5_mod, "MT5_AVAILABLE", False):
            p = MT5DataProvider()
            assert p.is_available() is False

    def test_unavailable_on_linux(self):
        fake = MagicMock()
        with patch.object(mt5_mod, "mt5", fake), \
             patch.object(mt5_mod, "MT5_AVAILABLE", True), \
             patch.object(mt5_mod.platform, "system", return_value="Linux"):
            p = MT5DataProvider()
            assert p.is_available() is False

    def test_unavailable_when_initialize_fails(self):
        fake = MagicMock()
        fake.initialize.return_value = False
        fake.last_error.return_value = (1, "not connected")
        with patch.object(mt5_mod, "mt5", fake), \
             patch.object(mt5_mod, "MT5_AVAILABLE", True), \
             patch.object(mt5_mod.platform, "system", return_value="Windows"):
            p = MT5DataProvider()
            assert p.is_available() is False

    def test_available_when_terminal_up(self, provider):
        assert provider.is_available() is True


class TestTimeframeMapping:
    def test_framework_to_mt5_constants(self, provider, mock_mt5):
        assert provider._timeframe_constant("1m") == mock_mt5.TIMEFRAME_M1
        assert provider._timeframe_constant("5m") == mock_mt5.TIMEFRAME_M5
        assert provider._timeframe_constant("15m") == mock_mt5.TIMEFRAME_M15
        assert provider._timeframe_constant("30m") == mock_mt5.TIMEFRAME_M30
        assert provider._timeframe_constant("1h") == mock_mt5.TIMEFRAME_H1
        assert provider._timeframe_constant("4h") == mock_mt5.TIMEFRAME_H4
        assert provider._timeframe_constant("1d") == mock_mt5.TIMEFRAME_D1

    def test_unknown_timeframe_returns_none(self, provider):
        assert provider._timeframe_constant("1w") is None
        assert provider._timeframe_constant("bogus") is None


class TestSymbolNormalization:
    def test_resolves_broker_suffix(self, provider, mock_mt5):
        """XAUUSD -> XAUUSDm when only the suffixed symbol is visible."""
        def _info(sym):
            info = MagicMock()
            info.visible = sym == "XAUUSDm"
            return info
        mock_mt5.symbol_info.side_effect = _info
        assert provider._normalize_symbol("XAUUSD") == "XAUUSDm"

    def test_lowercased_symbol_uppercased(self, provider, mock_mt5):
        assert provider._normalize_symbol("xauusd") == "XAUUSD"

    def test_bare_symbol_wins_when_both_exist(self, provider, mock_mt5):
        # Mock reports every variant visible -> the unsuffixed base wins.
        assert provider._normalize_symbol("XAUUSDm") == "XAUUSD"

    def test_falls_back_to_original(self, provider, mock_mt5):
        mock_mt5.symbol_info.side_effect = lambda s: None
        assert provider._normalize_symbol("GOLD") == "GOLD"

    def test_returns_input_when_unavailable(self):
        with patch.object(mt5_mod, "MT5_AVAILABLE", False):
            p = MT5DataProvider()
            assert p._normalize_symbol("XAUUSD") == "XAUUSD"


class TestGetOhlcv:
    def test_zero_copy_polars_pipeline(self, provider, mock_mt5):
        rates = _make_rates(3)
        mock_mt5.copy_rates_from_pos.return_value = rates
        mock_mt5.symbol_info.side_effect = lambda s: MagicMock(visible=(s == "XAUUSD"))

        result = provider.get_ohlcv("XAUUSD", "15m", limit=100)

        assert len(result) == 3
        first, second, third = result
        # UTC timestamp conversion
        assert first["time"] == datetime.fromtimestamp(rates[0]["time"], tz=timezone.utc)
        assert first["time"].tzinfo is not None
        # OHLCV passthrough
        assert (first["open"], first["high"], first["low"], first["close"]) == (1800.0, 1810.0, 1795.0, 1805.0)
        assert (second["open"], second["close"]) == (1801.0, 1806.0)
        assert (third["open"], third["close"]) == (1802.0, 1807.0)
        # Volume from tick_volume
        assert (first["volume"], second["volume"], third["volume"]) == (100, 150, 200)
        assert all(isinstance(c["volume"], int) for c in result)
        # Mocked call used the correct MT5 constant + normalized symbol
        mock_mt5.copy_rates_from_pos.assert_called_once_with(
            "XAUUSD", mock_mt5.TIMEFRAME_M15, 0, 100
        )

    def test_returns_empty_when_unavailable(self):
        with patch.object(mt5_mod, "MT5_AVAILABLE", False):
            p = MT5DataProvider()
            assert p.get_ohlcv("XAUUSD", "15m") == []

    def test_returns_empty_on_unsupported_timeframe(self, provider):
        assert provider.get_ohlcv("XAUUSD", "1w") == []

    def test_returns_empty_when_terminal_has_no_bars(self, provider, mock_mt5):
        mock_mt5.copy_rates_from_pos.return_value = None
        assert provider.get_ohlcv("XAUUSD", "15m") == []

    def test_returns_empty_on_mt5_exception(self, provider, mock_mt5):
        mock_mt5.copy_rates_from_pos.side_effect = RuntimeError("terminal busy")
        assert provider.get_ohlcv("XAUUSD", "15m") == []


class TestGetHistorical:
    def test_filters_by_date_range(self, provider, mock_mt5):
        rates = _make_rates(4)
        mock_mt5.copy_rates_from_pos.return_value = rates
        mock_mt5.symbol_info.side_effect = lambda s: MagicMock(visible=(s == "XAUUSD"))

        start = datetime.fromtimestamp(rates[1]["time"], tz=timezone.utc)
        end = datetime.fromtimestamp(rates[2]["time"], tz=timezone.utc)
        result = provider.get_historical("XAUUSD", "15m", start, end)

        assert len(result) == 2
        assert result[0]["time"] >= start
        assert result[1]["time"] <= end
        assert result[0]["time"] < result[1]["time"]

    def test_naive_datetimes_are_treated_as_utc(self, provider, mock_mt5):
        rates = _make_rates(2)
        mock_mt5.copy_rates_from_pos.return_value = rates
        mock_mt5.symbol_info.side_effect = lambda s: MagicMock(visible=(s == "XAUUSD"))

        start = datetime.fromtimestamp(rates[0]["time"], tz=timezone.utc).replace(tzinfo=None)
        end = datetime.fromtimestamp(rates[1]["time"], tz=timezone.utc).replace(tzinfo=None)
        result = provider.get_historical("XAUUSD", "15m", start, end)
        assert len(result) == 2

    def test_empty_when_unavailable(self):
        with patch.object(mt5_mod, "MT5_AVAILABLE", False):
            p = MT5DataProvider()
            result = p.get_historical(
                "XAUUSD", "15m",
                datetime(2026, 1, 1, tzinfo=timezone.utc),
                datetime(2026, 1, 2, tzinfo=timezone.utc),
            )
            assert result == []


class TestGetQuote:
    def test_quote_from_tick(self, provider, mock_mt5):
        tick = MagicMock()
        tick.bid = 1805.5
        tick.ask = 1805.7
        tick.last = 1805.6
        tick.time = 1791194400
        mock_mt5.symbol_info_tick.return_value = tick
        mock_mt5.symbol_info.side_effect = lambda s: MagicMock(visible=(s == "XAUUSD"))

        q = provider.get_quote("XAUUSD")

        assert q is not None
        assert q["bid"] == 1805.5
        assert q["ask"] == 1805.7
        assert q["last"] == 1805.6
        assert q["spread"] == 0.2
        assert q["source"] == "mt5"
        assert q["timestamp"] == datetime.fromtimestamp(1791194400, tz=timezone.utc).isoformat()

    def test_quote_none_when_unavailable(self):
        with patch.object(mt5_mod, "MT5_AVAILABLE", False):
            p = MT5DataProvider()
            assert p.get_quote("XAUUSD") is None

    def test_quote_none_when_tick_missing(self, provider, mock_mt5):
        mock_mt5.symbol_info_tick.return_value = None
        assert provider.get_quote("XAUUSD") is None


class TestShutdown:
    def test_shutdown_calls_mt5_shutdown(self, provider, mock_mt5):
        provider.shutdown()
        mock_mt5.shutdown.assert_called_once()
        assert provider._initialized is False

    def test_shutdown_is_idempotent(self, provider, mock_mt5):
        provider.shutdown()
        provider.shutdown()
        assert mock_mt5.shutdown.call_count == 1


class TestRegistryIntegration:
    def test_mt5_registered_in_default_registry(self):
        from app.data.providers import build_default_registry

        registry = build_default_registry()
        assert "mt5" in registry._providers
        assert registry.get("mt5").name == "mt5"

    def test_config_default_chain_includes_mt5(self):
        from app.config import Settings

        s = Settings()
        assert "mt5" in s.market_data_provider_chain.split(",")

    def test_service_fallback_chain_uses_mt5(self):
        from app.data.service import HistoricalDataService

        svc = HistoricalDataService(["local", "mt5", "stooq", "twelvedata"])
        assert [p.name for p in svc._providers] == ["local", "mt5", "stooq", "twelvedata"]

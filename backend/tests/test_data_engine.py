"""Tests for data quality, resampling, and historical service (Milestone 1)."""
from datetime import datetime, timedelta, timezone
import pytest

from app.data.quality import analyze_candles, detect_gaps, validate_candle_schema
from app.data.resample import resample_candles, resample_to_all_timeframes, TIMEFRAME_MINUTES
from app.data.service import HistoricalDataService, get_historical_service


def make_candles(count: int, start: datetime | None = None, interval_minutes: int = 15, price: float = 100.0):
    """Generate synthetic candles for testing."""
    if start is None:
        start = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    candles = []
    for i in range(count):
        ts = start + timedelta(minutes=i * interval_minutes)
        o = price + i * 0.1
        h = o + 0.5
        l = o - 0.5
        c = o + 0.3
        candles.append({
            "timestamp": ts,
            "symbol": "TEST",
            "timeframe": "15m",
            "open": o,
            "high": h,
            "low": l,
            "close": c,
            "volume": 1000 + i,
            "source": "test",
        })
    return candles


class TestQualityAnalysis:
    def test_analyze_valid_candles(self):
        candles = make_candles(50)
        report = analyze_candles(candles, "TEST", "15m", 15)
        assert report.candle_count == 50
        assert report.completeness_pct > 90
        assert report.ohlc_violations == 0
        assert report.duplicate_timestamps == []
        assert report.anomalies == []
        assert report.start is not None
        assert report.end is not None

    def test_analyze_empty_candles(self):
        report = analyze_candles([], "TEST", "15m", 15)
        assert report.candle_count == 0
        assert report.start is None
        assert report.end is None

    def test_detect_duplicates(self):
        candles = make_candles(10)
        # Add duplicate
        dup = dict(candles[0])
        candles.append(dup)
        report = analyze_candles(candles, "TEST", "15m", 15)
        assert len(report.duplicate_timestamps) == 1

    def test_detect_ohlc_violations(self):
        candles = make_candles(10)
        # Make one candle violate OHLC: high < low
        candles[5]["high"] = 50.0
        candles[5]["low"] = 100.0
        report = analyze_candles(candles, "TEST", "15m", 15)
        assert report.ohlc_violations >= 1

    def test_detect_extreme_price_jump(self):
        candles = make_candles(10)
        # Create a 60% jump
        candles[5]["close"] = candles[4]["close"] * 1.6
        report = analyze_candles(candles, "TEST", "15m", 15)
        assert any(a["type"] == "extreme_price_jump" for a in report.anomalies)

    def test_detect_invalid_price(self):
        candles = make_candles(10)
        candles[3]["close"] = -1.0
        report = analyze_candles(candles, "TEST", "15m", 15)
        assert any(a["type"] == "invalid_price" for a in report.anomalies)

    def test_completeness_calculation(self):
        # 20 candles at 15m = 5 hours span, expected = 20 candles
        candles = make_candles(20, interval_minutes=15)
        report = analyze_candles(candles, "TEST", "15m", 15)
        assert report.completeness_pct >= 90

    def test_completeness_with_missing(self):
        # Create candles with gaps
        candles = make_candles(10, interval_minutes=15)
        # Remove every other candle to create gaps
        sparse = candles[::2]
        report = analyze_candles(sparse, "TEST", "15m", 15)
        assert report.missing_candles > 0
        assert report.completeness_pct < 60

    def test_volume_stats(self):
        candles = make_candles(20)
        report = analyze_candles(candles, "TEST", "15m", 15)
        assert report.volume_stats["count"] == 20
        assert report.volume_stats["min"] > 0
        assert report.volume_stats["max"] > report.volume_stats["min"]
        assert report.volume_stats["avg"] > 0

    def test_to_dict(self):
        candles = make_candles(10)
        report = analyze_candles(candles, "TEST", "15m", 15)
        d = report.to_dict()
        assert d["symbol"] == "TEST"
        assert d["candle_count"] == 10
        assert "start" in d
        assert "end" in d
        assert "completeness_pct" in d


class TestGapDetection:
    def test_no_gaps_in_continuous_data(self):
        candles = make_candles(50, interval_minutes=15)
        gaps = detect_gaps(candles, 15, max_gap_candles=3)
        assert gaps == []

    def test_detect_gap(self):
        candles = make_candles(20, interval_minutes=15)
        # Remove candles 5-9 (indices 5 through 9) to create a 5-candle gap
        candles = candles[:5] + candles[10:]
        gaps = detect_gaps(candles, 15, max_gap_candles=2)
        assert len(gaps) >= 1
        assert gaps[0]["gap_candles"] > 2

    def test_single_candle_no_gaps(self):
        candles = make_candles(1)
        gaps = detect_gaps(candles, 15)
        assert gaps == []


class TestSchemaValidation:
    def test_valid_candles(self):
        candles = make_candles(10)
        errors = validate_candle_schema(candles)
        assert errors == []

    def test_missing_fields(self):
        candles = make_candles(10)
        del candles[3]["open"]
        del candles[5]["close"]
        errors = validate_candle_schema(candles)
        assert len(errors) == 2
        assert "open" in errors[0]["missing_fields"]
        assert "close" in errors[1]["missing_fields"]


class TestResampling:
    def test_resample_15m_to_1h(self):
        # 4 x 15m candles = 1 x 1h candle
        start = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        candles = []
        for i in range(4):
            ts = start + timedelta(minutes=i * 15)
            candles.append({
                "timestamp": ts,
                "symbol": "TEST",
                "timeframe": "15m",
                "open": 100.0 + i,
                "high": 105.0 + i,
                "low": 98.0 + i,
                "close": 102.0 + i,
                "volume": 100,
            })

        resampled = resample_candles(candles, "1h")
        assert len(resampled) == 1
        r = resampled[0]
        assert r["open"] == 100.0
        assert r["high"] == max(105.0 + i for i in range(4))
        assert r["low"] == min(98.0 + i for i in range(4))
        assert r["close"] == 102.0 + 3
        assert r["volume"] == 400
        assert r["timeframe"] == "1h"
        assert r["timestamp"] == start

    def test_resample_multiple_buckets(self):
        # 8 x 15m candles = 2 x 1h candles
        start = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        candles = []
        for i in range(8):
            ts = start + timedelta(minutes=i * 15)
            candles.append({
                "timestamp": ts,
                "symbol": "TEST",
                "timeframe": "15m",
                "open": 100.0 + i,
                "high": 105.0 + i,
                "low": 98.0 + i,
                "close": 102.0 + i,
                "volume": 100,
            })

        resampled = resample_candles(candles, "1h")
        assert len(resampled) == 2
        # First hour: candles 0-3
        assert resampled[0]["timestamp"] == start
        # Second hour: candles 4-7
        assert resampled[1]["timestamp"] == start + timedelta(hours=1)

    def test_resample_empty(self):
        result = resample_candles([], "1h")
        assert result == []

    def test_resample_same_timeframe_raises(self):
        candles = make_candles(10)
        with pytest.raises(ValueError):
            resample_candles(candles, "15m")  # same timeframe, not higher

    def test_resample_invalid_target_raises(self):
        candles = make_candles(10)
        with pytest.raises(ValueError):
            resample_candles(candles, "invalid")

    def test_resample_to_all_timeframes(self):
        start = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        candles = []
        for i in range(96):  # 24 hours of 15m data
            ts = start + timedelta(minutes=i * 15)
            candles.append({
                "timestamp": ts,
                "symbol": "TEST",
                "timeframe": "15m",
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.5,
                "volume": 100,
            })

        result = resample_to_all_timeframes(candles, source_timeframe="15m")
        assert "1h" in result
        assert "4h" in result
        assert "1d" in result
        # 24 hours of 15m = 24 x 1h
        assert len(result["1h"]) == 24
        # 24 hours of 15m = 6 x 4h
        assert len(result["4h"]) == 6
        # 24 hours of 15m = 1 x 1d
        assert len(result["1d"]) == 1

    def test_resample_preserves_symbol(self):
        start = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        candles = []
        for i in range(4):
            ts = start + timedelta(minutes=i * 15)
            candles.append({
                "timestamp": ts,
                "symbol": "XAUUSD",
                "timeframe": "15m",
                "open": 2000.0,
                "high": 2010.0,
                "low": 1990.0,
                "close": 2005.0,
                "volume": 500,
            })

        resampled = resample_candles(candles, "1h")
        assert resampled[0]["symbol"] == "XAUUSD"

    def test_resample_missing_volume(self):
        start = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        candles = []
        for i in range(4):
            ts = start + timedelta(minutes=i * 15)
            candles.append({
                "timestamp": ts,
                "symbol": "TEST",
                "timeframe": "15m",
                "open": 100.0,
                "high": 105.0,
                "low": 98.0,
                "close": 102.0,
                "volume": None,
            })

        resampled = resample_candles(candles, "1h")
        assert len(resampled) == 1
        assert resampled[0]["volume"] is None


class TestHistoricalService:
    def test_service_init(self):
        svc = HistoricalDataService()
        assert svc is not None

    def test_service_with_providers(self):
        svc = HistoricalDataService(provider_names=["local", "stooq"])
        assert len(svc._providers) >= 1

    def test_service_get_latest_empty(self):
        svc = HistoricalDataService()
        result = svc.get_latest("NONEXISTENT_SYMBOL_XYZ", "15m")
        assert result is None

    def test_service_encode_decode_roundtrip(self):
        candles = make_candles(5)
        encoded = HistoricalDataService._encode_candles(candles)
        decoded = HistoricalDataService._decode_candles(encoded)
        assert len(decoded) == 5
        assert decoded[0]["close"] == candles[0]["close"]
        assert decoded[0]["timestamp"] == candles[0]["timestamp"]

    def test_service_validate_candle(self):
        assert HistoricalDataService._is_valid_candle({
            "timestamp": datetime.now(timezone.utc),
            "open": 100.0, "high": 105.0, "low": 98.0, "close": 102.0,
        })
        # Invalid: high < low
        assert not HistoricalDataService._is_valid_candle({
            "timestamp": datetime.now(timezone.utc),
            "open": 100.0, "high": 90.0, "low": 98.0, "close": 102.0,
        })
        # Missing field
        assert not HistoricalDataService._is_valid_candle({
            "timestamp": datetime.now(timezone.utc),
            "open": 100.0, "high": 105.0, "low": 98.0,
        })

    def test_get_historical_service_singleton(self):
        s1 = get_historical_service()
        s2 = get_historical_service()
        assert s1 is s2


class TestIntegration:
    def test_full_pipeline_quality_resample(self):
        """Test the full pipeline: generate candles -> quality report -> resample."""
        start = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        candles = []
        for i in range(48):  # 12 hours of 15m data
            ts = start + timedelta(minutes=i * 15)
            candles.append({
                "timestamp": ts,
                "symbol": "TEST",
                "timeframe": "15m",
                "open": 100.0 + i * 0.1,
                "high": 101.0 + i * 0.1,
                "low": 99.0 + i * 0.1,
                "close": 100.5 + i * 0.1,
                "volume": 1000,
            })

        # Quality report
        report = analyze_candles(candles, "TEST", "15m", 15)
        assert report.candle_count == 48
        assert report.completeness_pct > 90

        # Resample to 1h
        resampled = resample_candles(candles, "1h")
        assert len(resampled) == 12

        # Resample to 4h
        resampled_4h = resample_candles(candles, "4h")
        assert len(resampled_4h) == 3

        # Gaps
        gaps = detect_gaps(candles, 15, max_gap_candles=3)
        assert gaps == []

    def test_quality_report_to_dict_serializable(self):
        import json
        candles = make_candles(20)
        report = analyze_candles(candles, "TEST", "15m", 15)
        d = report.to_dict()
        # Should be JSON serializable
        serialized = json.dumps(d)
        deserialized = json.loads(serialized)
        assert deserialized["candle_count"] == 20

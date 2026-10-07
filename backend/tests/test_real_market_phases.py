"""Phase 36-40 tests: capability, completed candles, session, broker, readiness."""
from __future__ import annotations

from datetime import timedelta, timezone
from typing import Any

import pytest

from app.utils.timeutils import utcnow


# ------------------------------------------------------------------------------
# Phase 37: CompletedCandleEngine
# ------------------------------------------------------------------------------
def _candle(ts, close=100.0, spread=0.0):
    return {
        "timestamp": ts, "open": close - 0.5, "high": close + 1.0,
        "low": close - 1.0, "close": close, "volume": 10.0,
        "bid": close - spread, "ask": close + spread, "source": "test",
    }


def test_engine_never_persists_incomplete_candle(test_db):
    from app.services.real_market import CompletedCandleEngine

    engine = CompletedCandleEngine("XAUUSD", "15m")
    now = utcnow()
    incomplete = _candle(now - timedelta(minutes=1))  # opens 1 min ago, not completed
    out = engine.process([incomplete], source="test")
    assert out["completed_count"] == 0
    assert out["persisted"] == 0
    assert out["new_completed_candle"] is False


def test_engine_persists_completed_once_and_dedups(test_db):
    from app.services.real_market import CompletedCandleEngine

    engine = CompletedCandleEngine("XAUUSD", "15m")
    ts = utcnow().replace(tzinfo=timezone.utc) - timedelta(minutes=30)
    candle = _candle(ts)

    out1 = engine.process([candle], source="test")
    assert out1["persisted"] == 1
    assert out1["new_completed_candle"] is True

    # Same candle again -> duplicate skipped, no new analysis trigger
    out2 = engine.process([candle], source="test")
    assert out2["persisted"] == 0
    assert out2["duplicates_skipped"] == 1
    assert out2["new_completed_candle"] is False

    # Fresh engine instance (restart) still dedups via durable DB state
    engine2 = CompletedCandleEngine("XAUUSD", "15m")
    out3 = engine2.process([candle], source="test")
    assert out3["persisted"] == 0
    assert out3["duplicates_skipped"] == 1


def test_engine_skips_invalid_rows(test_db):
    from app.services.real_market import CompletedCandleEngine

    engine = CompletedCandleEngine("XAUUSD", "15m")
    bad = _candle(utcnow() - timedelta(minutes=30))
    bad["high"] = 1.0  # OHLC inconsistent
    out = engine.process([bad], source="test")
    assert out["invalid_skipped"] == 1
    assert out["persisted"] == 0


def test_engine_preserves_provider_metadata(test_db):
    from app.models.database import MarketData, get_session_factory
    from app.services.real_market import CompletedCandleEngine
    from app.utils.timeutils import to_utc

    engine = CompletedCandleEngine("XAUUSD", "15m")
    ts = utcnow().replace(microsecond=0, tzinfo=timezone.utc) - timedelta(minutes=30)
    candle = _candle(ts, spread=0.05)
    engine.process([candle], source="myprovider")

    db = get_session_factory()()
    row = db.query(MarketData).filter(MarketData.symbol == "XAUUSD").order_by(MarketData.timestamp.desc()).first()
    db.close()
    assert row is not None
    assert row.source == "myprovider"
    assert to_utc(row.timestamp) == ts


# ------------------------------------------------------------------------------
# Phase 38: RealMarketSession
# ------------------------------------------------------------------------------
def test_session_pause_resume_cycle(monkeypatch):
    from app.config import get_settings
    from app.services.real_market import RealMarketSession

    s = RealMarketSession(symbol="XAUUSD", timeframe="15m")
    assert not s.paused
    s.on_candle_processed(utcnow())
    s.pause("provider 429", category="rate_limit")
    assert s.paused
    assert s.pause_reason == "provider 429"
    assert s.last_failure_category == "rate_limit"
    st = s.status()
    assert st["mode"] in {"CACHE", "OFFLINE"}  # never claims REAL while paused
    assert st["paused"] is True

    s.resume()
    assert not s.paused
    # Simulate a real-data configuration so resumed mode can be REAL
    cfg = get_settings()
    monkeypatch.setattr(cfg, "market_data_provider", "twelvedata")
    monkeypatch.setattr(cfg, "twelve_data_api_key", "k")
    st2 = s.status()
    assert st2["mode"] == "REAL_MARKET_DATA"
    assert st2["real_market_data_enabled"] is True


def test_session_mode_label_rules():
    from app.services.real_market import RealMarketSession

    s = RealMarketSession(symbol="XAUUSD", timeframe="15m")
    # No candle ever -> OFFLINE
    assert s.classify_data_age(None) == "OFFLINE"
    # Fresh candle -> REAL
    assert s.classify_data_age(utcnow()) == "REAL_MARKET_DATA"
    # Stale candle (default threshold 600s; 15 min old) -> CACHE, never REAL DATA
    assert s.classify_data_age(utcnow() - timedelta(minutes=15)) == "CACHE"
    # Ancient candle -> OFFLINE
    assert s.classify_data_age(utcnow() - timedelta(days=3)) == "OFFLINE"


# ------------------------------------------------------------------------------
# Phase 39: broker hardening
# ------------------------------------------------------------------------------
def test_validate_stop_targets_geometry():
    from app.execution.broker import validate_stop_targets

    # BUY: SL < ENTRY < TP
    ok, _ = validate_stop_targets("BUY", 99.0, 103.0, entry_hint=100.0)
    assert ok
    ok, err = validate_stop_targets("BUY", 100.5, 103.0, entry_hint=100.0)
    assert not ok and "SL < ENTRY < TP" in err
    # SELL: TP < ENTRY < SL
    ok, _ = validate_stop_targets("SELL", 101.0, 97.0, entry_hint=100.0)
    assert ok
    ok, err = validate_stop_targets("SELL", 99.0, 101.5, entry_hint=100.0)
    assert not ok and "TP < ENTRY < SL" in err
    # Missing SL rejected when required
    ok, err = validate_stop_targets("BUY", None, 103.0, require_stops=True)
    assert not ok and "missing" in err
    # NaN / Infinity rejected
    ok, err = validate_stop_targets("BUY", float("nan"), 103.0, entry_hint=100.0)
    assert not ok and "invalid" in err
    ok, _ = validate_stop_targets("BUY", float("inf"), 103.0, entry_hint=100.0)
    assert not ok
    # Zero/negative quantity handled in place_order
    db = _fresh_db()
    from app.execution.broker import PaperBroker

    broker = PaperBroker(db)
    res = broker.place_order("XAUUSD", "BUY", float("nan"), "market",
                             stop_price=99.0, take_profit=103.0)
    assert not res["ok"] and "quantity" in res["error"]
    db.close()


def test_broker_rejects_missing_sl_tp_when_required(test_db):
    from app.execution.broker import PaperBroker

    db = test_db()
    _seed_price(db, 100.0)
    broker = PaperBroker(db)
    res = broker.place_order("XAUUSD", "BUY", 1.0, "market", require_stops=True)
    assert not res["ok"] and "missing" in res["error"]
    # With valid stops it passes
    res2 = broker.place_order("XAUUSD", "BUY", 1.0, "market",
                              stop_price=99.0, take_profit=103.0, require_stops=True)
    assert res2["ok"]
    db.close()


def test_broker_rejects_geometry_violation_at_fill(test_db):
    from app.execution.broker import PaperBroker

    db = test_db()
    _seed_price(db, 100.0)
    broker = PaperBroker(db)
    # BUY with SL above entry -> rejected at fill time
    res = broker.place_order("XAUUSD", "BUY", 1.0, "market",
                             stop_price=101.0, take_profit=103.0)
    assert not res["ok"]
    assert broker.get_positions() == []
    db.close()


def _seed_price(db, price: float) -> None:
    from app.models.database import MarketData

    db.add(MarketData(symbol="XAUUSD", timeframe="15m", timestamp=utcnow(),
                      open=price, high=price + 1, low=price - 1, close=price, source="test"))
    db.commit()


def _fresh_db():
    from app.models.database import get_session_factory

    return get_session_factory()()


# ------------------------------------------------------------------------------
# Phase 40: Readiness service
# ------------------------------------------------------------------------------
def test_readiness_structure_and_overall_states(monkeypatch):
    from app.services.real_market import readiness_service

    report = readiness_service.evaluate()
    assert report["overall"] in {"READY_FOR_REAL_MARKET_PAPER", "CONFIG_REQUIRED", "DEGRADED", "NOT_READY"}
    assert "MARKET_DATA" in report["checks"]
    assert "PAPER_BROKER" in report["checks"]
    assert "SECURITY" in report["checks"]
    # Every check has a valid state
    for name, check in report["checks"].items():
        assert check["state"] in {"PASS", "FAIL", "DEGRADED", "DISABLED", "CONFIG_REQUIRED"}, name


def test_readiness_fails_when_live_trading_enabled(monkeypatch):
    from app.config import get_settings
    from app.services.real_market import ReadinessService

    s = get_settings()
    monkeypatch.setattr(s, "live_trading", True)
    svc = ReadinessService()
    report = svc.evaluate()
    assert report["overall"] in {"NOT_READY", "CONFIG_REQUIRED"}
    assert report["checks"]["SECURITY"]["state"] == "FAIL"


# ------------------------------------------------------------------------------
# Phase 36: capability discovery
# ------------------------------------------------------------------------------
def test_capability_requires_api_key():
    from app.data.providers import TwelveDataProvider

    provider = TwelveDataProvider.__new__(TwelveDataProvider)
    provider.api_key = ""
    provider.name = "twelvedata"
    provider.INTERVALS = TwelveDataProvider.INTERVALS
    cap = provider.check_capability("XAUUSD", "15m")
    assert cap["available"] is False
    assert "CONFIG_REQUIRED" in cap["reason"]


def test_capability_unsupported_timeframe():
    from app.data.providers import TwelveDataProvider

    provider = TwelveDataProvider.__new__(TwelveDataProvider)
    provider.api_key = "k"
    provider.name = "twelvedata"
    provider.INTERVALS = TwelveDataProvider.INTERVALS
    cap = provider.check_capability("XAUUSD", "7m")
    assert cap["available"] is False
    assert cap["reason"] == "TIMEFRAME_NOT_SUPPORTED"


def test_capability_maps_provider_errors(monkeypatch):
    from app.data.providers import TwelveDataProvider
    from app.data.resilience import ProviderError

    provider = TwelveDataProvider.__new__(TwelveDataProvider)
    provider.api_key = "k"
    provider.name = "twelvedata"
    provider.INTERVALS = TwelveDataProvider.INTERVALS

    def raise_not_entitled(*args, **kwargs):
        raise ProviderError("plan does not include", category="not_entitled", code="TD_NOT_ENTITLED")

    monkeypatch.setattr(provider, "get_ohlcv", raise_not_entitled)
    cap = provider.check_capability("XAUUSD", "15m")
    assert cap["available"] is False
    assert cap["reason"] == "NOT_ENTITLED"


# ------------------------------------------------------------------------------
# API endpoints
# ------------------------------------------------------------------------------
def test_api_endpoints_exist(test_db, monkeypatch):
    import os

    os.environ.setdefault("DATABASE_URL", "sqlite://")
    from fastapi.testclient import TestClient

    import main as backend_main

    client = TestClient(backend_main.app)
    r = client.get("/readiness")
    assert r.status_code == 200
    body = r.json()
    assert "overall" in body and "checks" in body
    r2 = client.get("/session/status")
    assert r2.status_code == 200
    body2 = r2.json()
    assert "mode" in body2 and "worker" in body2 and "credits" in body2

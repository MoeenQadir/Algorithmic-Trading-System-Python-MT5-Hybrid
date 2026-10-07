"""Tests: paper broker, risk engine, database models, cache, rate limiting, news/LLM failure."""
from __future__ import annotations

import pytest

from app.models.database import MarketData, Order, Position, Trade


# ------------------------------------------------------------------------------
# Paper broker
# ------------------------------------------------------------------------------
def _seed_price(db, symbol="US100", price=100.0):
    from app.utils.timeutils import utcnow

    db.add(MarketData(symbol=symbol, timeframe="15m", timestamp=utcnow(),
                      open=price, high=price + 1, low=price - 1, close=price,
                      volume=1000, source="test"))
    db.commit()


def test_paper_market_order_buy(db):
    from app.execution.broker import PaperBroker

    _seed_price(db)
    broker = PaperBroker(db, starting_balance=10000.0)
    res = broker.place_order("US100", "BUY", 10)
    assert res["ok"]
    order = res["order"]
    assert order["status"] == "filled"
    assert order["side"] == "BUY"
    positions = broker.get_positions()
    assert len(positions) == 1
    assert positions[0]["direction"] == "LONG"
    assert positions[0]["quantity"] == 10


def test_paper_close_position_pnl(db):
    from app.execution.broker import PaperBroker

    _seed_price(db, price=100.0)
    broker = PaperBroker(db, starting_balance=10000.0)
    broker.place_order("US100", "BUY", 10, stop_price=99.0)
    # price rises to 102 -> long P&L positive
    _seed_price(db, price=102.0)
    pos = broker.get_positions()[0]
    res = broker.close_position(pos["id"], reason="test_exit")
    assert res["ok"]
    assert res["pnl"] > 0
    trades = db.query(Trade).filter(Trade.status == "closed").all()
    assert len(trades) == 1
    assert trades[0].result == "win"
    assert trades[0].r_multiple is not None


def test_paper_stop_loss_triggers(db):
    from app.execution.broker import PaperBroker

    _seed_price(db, price=100.0)
    broker = PaperBroker(db, starting_balance=10000.0)
    broker.place_order("US100", "BUY", 10)
    pos = broker.get_positions()[0]
    # set a stop at 98
    p = db.get(Position, pos["id"])
    p.stop_loss = 98.0
    db.commit()
    _seed_price(db, price=97.0)
    closed = broker.check_stops_and_targets()
    assert len(closed) == 1
    assert closed[0]["reason"] == "stop_loss"
    assert closed[0]["pnl"] < 0


def test_paper_take_profit_triggers(db):
    from app.execution.broker import PaperBroker

    _seed_price(db, price=100.0)
    broker = PaperBroker(db, starting_balance=10000.0)
    broker.place_order("US100", "BUY", 10, take_profit=103.0)
    _seed_price(db, price=104.0)
    closed = broker.check_stops_and_targets()
    assert len(closed) == 1
    assert closed[0]["reason"] == "take_profit"


def test_paper_limit_order_rests_then_fills(db):
    from app.execution.broker import PaperBroker

    _seed_price(db, price=100.0)
    broker = PaperBroker(db, starting_balance=10000.0)
    res = broker.place_order("US100", "BUY", 5, order_type="limit", limit_price=99.0)
    assert res["ok"]
    assert res["order"]["status"] == "pending"
    fills = broker.process_pending_orders()
    assert fills == []  # price still 100 > 99
    _seed_price(db, price=98.5)
    fills = broker.process_pending_orders()
    assert len(fills) == 1
    assert fills[0]["filled"]


def test_paper_cancel_order(db):
    from app.execution.broker import PaperBroker

    _seed_price(db, price=100.0)
    broker = PaperBroker(db, starting_balance=10000.0)
    res = broker.place_order("US100", "BUY", 5, order_type="limit", limit_price=99.0)
    broker_order_id = res["order"]["broker_order_id"]
    assert broker.cancel_order(broker_order_id)
    assert broker.get_order(broker_order_id)["status"] == "cancelled"
    assert not broker.cancel_order(broker_order_id)  # already cancelled


def test_paper_modify_order(db):
    from app.execution.broker import PaperBroker

    _seed_price(db, price=100.0)
    broker = PaperBroker(db, starting_balance=10000.0)
    res = broker.place_order("US100", "BUY", 5, order_type="limit", limit_price=99.0)
    broker_order_id = res["order"]["broker_order_id"]
    assert broker.modify_order(broker_order_id, limit_price=98.0)
    assert broker.get_order(broker_order_id)["limit_price"] == 98.0


def test_paper_invalid_orders_rejected(db):
    from app.execution.broker import PaperBroker

    _seed_price(db)
    broker = PaperBroker(db, starting_balance=10000.0)
    assert not broker.place_order("US100", "SIDEWAYS", 10)["ok"]
    assert not broker.place_order("US100", "BUY", -5)["ok"]
    assert not broker.place_order("US100", "BUY", 10, order_type="weird")["ok"]


def test_paper_account_equity(db):
    from app.execution.broker import PaperBroker

    _seed_price(db, price=100.0)
    broker = PaperBroker(db, starting_balance=10000.0)
    account = broker.get_account()
    assert account["equity"] == pytest.approx(10000.0, abs=1.0)
    assert account["paper"] is True
    broker.place_order("US100", "BUY", 10)
    account2 = broker.get_account()
    # equity ~ unchanged (tiny spread/slippage), but position is open
    assert account2["open_positions"] == 1


def test_no_market_data_order_rejected(db):
    from app.execution.broker import PaperBroker

    broker = PaperBroker(db, starting_balance=10000.0)
    res = broker.place_order("NODATA", "BUY", 10)
    assert not res["ok"]


# ------------------------------------------------------------------------------
# Risk engine integration
# ------------------------------------------------------------------------------
def test_risk_engine_approves_valid_trade(db):
    from app.risk.engine import risk_engine

    out = risk_engine.evaluate(
        db,
        {"action": "BUY", "symbol": "US100", "entry": 100.0, "stop_loss": 99.0, "take_profit": 103.0},
        account_equity=10000.0,
        current_price=100.0,
        spread=0.05,
        last_candle_time=None,
    )
    assert out.verdict in {"APPROVED", "REDUCED_SIZE"}
    assert out.quantity and out.quantity > 0


def test_risk_engine_rejects_bad_rr(db):
    from app.risk.engine import risk_engine

    out = risk_engine.evaluate(
        db,
        {"action": "BUY", "symbol": "US100", "entry": 100.0, "stop_loss": 99.0, "take_profit": 100.5},
        account_equity=10000.0,
    )
    assert out.verdict == "REJECTED"
    assert any("risk_reward" in v for v in out.violations)


def test_risk_engine_max_open_positions(db):
    from app.utils.timeutils import utcnow

    from app.risk.engine import risk_engine

    for i in range(3):
        db.add(Position(symbol=f"S{i}", direction="LONG", quantity=1, entry_price=100,
                        status="open", opened_at=utcnow()))
    db.commit()
    out = risk_engine.evaluate(
        db,
        {"action": "BUY", "symbol": "US100", "entry": 100.0, "stop_loss": 99.0, "take_profit": 103.0},
        account_equity=10000.0,
    )
    assert out.verdict == "REJECTED"
    assert any("open positions" in v for v in out.violations)


def test_risk_engine_same_symbol_exposure(db):
    from app.utils.timeutils import utcnow

    from app.risk.engine import risk_engine

    db.add(Position(symbol="US100", direction="LONG", quantity=1, entry_price=100,
                    status="open", opened_at=utcnow()))
    db.commit()
    out = risk_engine.evaluate(
        db,
        {"action": "BUY", "symbol": "US100", "entry": 100.0, "stop_loss": 99.0, "take_profit": 103.0},
        account_equity=10000.0,
    )
    assert out.verdict == "REJECTED"


def test_risk_engine_caps_symbol_exposure(db, monkeypatch):
    from app.config import get_settings
    from app.risk.engine import risk_engine

    monkeypatch.setenv("MAX_SYMBOL_EXPOSURE", "0.1")
    get_settings.cache_clear()
    try:
        out = risk_engine.evaluate(
            db,
            {"action": "BUY", "symbol": "US100", "entry": 100.0, "stop_loss": 99.9, "take_profit": 100.2},
            account_equity=10000.0,
        )
    finally:
        get_settings.cache_clear()
    assert out.verdict == "REDUCED_SIZE"
    assert out.position_size <= 1000.0
    assert out.quantity == pytest.approx(10.0)


def test_risk_engine_daily_loss_halt(db):
    from app.utils.timeutils import utcnow

    from app.risk.engine import risk_engine

    db.add(Trade(symbol="US100", direction="LONG", entry_time=utcnow(), exit_time=utcnow(),
                 entry_price=100, quantity=1, pnl=-200.0, status="closed"))
    db.commit()
    out = risk_engine.evaluate(
        db,
        {"action": "BUY", "symbol": "US100", "entry": 100.0, "stop_loss": 99.0, "take_profit": 103.0},
        account_equity=10000.0,  # 2% daily loss > 1.2% limit
    )
    assert out.verdict == "REJECTED"
    assert any("daily loss" in v for v in out.violations)


def test_risk_engine_consecutive_losses(db):
    from app.utils.timeutils import utcnow

    from app.risk.engine import risk_engine

    for i in range(3):
        db.add(Trade(symbol="US100", direction="LONG", entry_time=utcnow(), exit_time=utcnow(),
                     entry_price=100, quantity=1, pnl=-10.0, status="closed"))
    db.commit()
    out = risk_engine.evaluate(
        db,
        {"action": "BUY", "symbol": "US100", "entry": 100.0, "stop_loss": 99.0, "take_profit": 103.0},
        account_equity=10000.0,
    )
    assert out.verdict == "REJECTED"
    assert any("consecutive" in v for v in out.violations)


def test_risk_engine_stale_data_waits(db):
    from datetime import timedelta

    from app.risk.engine import risk_engine
    from app.utils.timeutils import utcnow

    old = utcnow() - timedelta(hours=12)
    out = risk_engine.evaluate(
        db,
        {"action": "BUY", "symbol": "US100", "entry": 100.0, "stop_loss": 99.0, "take_profit": 103.0},
        account_equity=10000.0, last_candle_time=old,
    )
    assert out.verdict == "WAIT"
    assert any("stale" in v for v in out.violations)


def test_risk_engine_accepts_sqlite_naive_candle_timestamp(db):
    from datetime import timedelta

    from app.risk.engine import risk_engine
    from app.utils.timeutils import utcnow

    old_naive = (utcnow() - timedelta(hours=12)).replace(tzinfo=None)
    out = risk_engine.evaluate(
        db,
        {"action": "BUY", "symbol": "US100", "entry": 100.0, "stop_loss": 99.0, "take_profit": 103.0},
        account_equity=10000.0,
        last_candle_time=old_naive,
    )
    assert out.verdict == "WAIT"
    assert any("stale" in violation for violation in out.violations)


def test_risk_engine_wide_spread_waits(db):
    from app.risk.engine import risk_engine

    out = risk_engine.evaluate(
        db,
        {"action": "BUY", "symbol": "US100", "entry": 100.0, "stop_loss": 99.0, "take_profit": 103.0},
        account_equity=10000.0, current_price=100.0, spread=0.5,  # 0.5% > 0.15% limit
    )
    assert out.verdict == "WAIT"


def test_risk_engine_overrides_ai(db):
    """The Risk Engine must be able to veto an AI decision."""
    from app.risk.engine import risk_engine

    out = risk_engine.evaluate(
        db,
        {"action": "BUY", "symbol": "US100", "entry": 100.0, "stop_loss": 100.0, "take_profit": 200.0},
        account_equity=10000.0,
    )
    assert out.verdict == "REJECTED"  # zero stop distance -> reject even if AI says BUY


# ------------------------------------------------------------------------------
# Database
# ------------------------------------------------------------------------------
def test_db_roundtrip(db):
    from app.utils.timeutils import utcnow

    db.add(MarketData(symbol="US100", timeframe="15m", timestamp=utcnow(),
                      open=1, high=2, low=0.5, close=1.5, volume=10))
    db.commit()
    row = db.query(MarketData).first()
    assert row.symbol == "US100"
    assert row.close == 1.5


def test_db_indexes_exist(test_db):
    from app.models.database import Base

    table = Base.metadata.tables["market_data"]
    index_names = {idx.name for idx in table.indexes}
    assert any("symbol" in n for n in index_names)
    assert any("tf" in n for n in index_names)


def test_db_unique_dedup_key(db):
    from app.models.database import NewsEvent
    from app.utils.timeutils import utcnow

    db.add(NewsEvent(headline="Test", source="x", published_at=utcnow(), dedup_key="abc"))
    db.commit()
    db.add(NewsEvent(headline="Test", source="x", published_at=utcnow(), dedup_key="abc"))
    with pytest.raises(Exception):
        db.commit()
    db.rollback()


# ------------------------------------------------------------------------------
# Cache
# ------------------------------------------------------------------------------
def test_cache_set_get_roundtrip(monkeypatch, tmp_path):
    import app.data.cache as cache_mod

    cache_mod.CACHE_ROOT = str(tmp_path / "cache")
    c = cache_mod.FileCache()
    c.set("k", {"a": 1}, "data", ttl=60)
    assert c.get("k", "data") == {"a": 1}


def test_cache_ttl_expiry(monkeypatch, tmp_path):
    import app.data.cache as cache_mod

    cache_mod.CACHE_ROOT = str(tmp_path / "cache")
    c = cache_mod.FileCache()
    c.set("k", 42, "data", ttl=-1)  # already expired
    assert c.get("k", "data") is None
    assert c.get_stale("k", "data") == 42  # stale fallback available


def test_cache_cached_or_fetch(monkeypatch, tmp_path):
    import app.data.cache as cache_mod

    cache_mod.CACHE_ROOT = str(tmp_path / "cache")
    c = cache_mod.FileCache()
    calls = []

    def fetch():
        calls.append(1)
        return "value"

    v1, cached1 = c.cached_or_fetch("k", "data", fetch)
    v2, cached2 = c.cached_or_fetch("k", "data", fetch)
    assert v1 == v2 == "value"
    assert cached1 is False and cached2 is True
    assert len(calls) == 1  # deduplicated: fetcher ran once


def test_cache_fetch_failure_uses_stale(monkeypatch, tmp_path):
    import app.data.cache as cache_mod

    cache_mod.CACHE_ROOT = str(tmp_path / "cache")
    c = cache_mod.FileCache()
    c.set("k", "old", "data", ttl=-1)

    def fail():
        raise RuntimeError("network down")

    v, cached = c.cached_or_fetch("k", "data", fail)
    assert v == "old" and cached is True


def test_stooq_uses_fresh_cached_range(monkeypatch, tmp_path):
    from datetime import datetime, timezone

    import app.data.cache as cache_mod
    from app.data.providers import StooqProvider

    cache_mod.CACHE_ROOT = str(tmp_path / "cache")
    key = "stooq:US100:20260901:20260930"
    cache_mod.cache.set(key, [["2026-09-29T00:00:00+00:00", 100, 103, 99, 102, 500]], "data")
    provider = StooqProvider()
    monkeypatch.setattr(provider, "_fetch", lambda *args: pytest.fail("fresh cache must prevent a provider request"))

    candles = provider.get_historical(
        "US100", "1d",
        datetime(2026, 9, 1, tzinfo=timezone.utc),
        datetime(2026, 9, 30, tzinfo=timezone.utc),
    )
    assert len(candles) == 1
    assert candles[0]["close"] == 102
    assert candles[0]["timestamp"].isoformat() == "2026-09-29T00:00:00+00:00"


def test_twelvedata_normalizes_xauusd_intraday_candles(monkeypatch):
    import app.data.providers as providers
    from app.data.providers import TwelveDataProvider

    class Response:
        status_code = 200
        headers = {}

        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def json():
            return {
                "status": "ok",
                "values": [{
                    "datetime": "2026-09-29 12:15:00",
                    "open": "2600.10", "high": "2602.00", "low": "2599.50", "close": "2601.25",
                }],
            }

    request = {}

    class FakeClient:
        def get(self, url, **kwargs):
            request.update(url=url, **kwargs)
            return Response()

        def close(self):
            pass

    def fake_get_client(self):
        return FakeClient()

    # Mock the _get_client method to return our fake client
    monkeypatch.setattr(TwelveDataProvider, "_get_client", fake_get_client)
    provider = TwelveDataProvider("test-key")
    candles = provider.get_ohlcv("XAUUSD", "15m", limit=10)

    assert request["params"]["symbol"] == "XAU/USD"
    assert request["params"]["interval"] == "15min"
    assert request["headers"]["Authorization"] == "apikey test-key"
    assert candles[0]["symbol"] == "XAUUSD"
    assert candles[0]["timeframe"] == "15m"
    assert candles[0]["close"] == 2601.25
    assert candles[0]["source"] == "twelvedata"
    provider.close()


def test_twelvedata_requires_api_key():
    from app.data.providers import TwelveDataProvider

    with pytest.raises(RuntimeError, match="TWELVE_DATA_API_KEY"):
        TwelveDataProvider("").get_ohlcv("XAUUSD", "1m", limit=1)


# ------------------------------------------------------------------------------
# Rate-limit manager
# ------------------------------------------------------------------------------
def test_rate_limit_429_blocks_then_clears():
    from app.data.resilience import RateLimitManager

    rl = RateLimitManager()
    rl.on_rate_limited("prov", retry_after=1.0)
    assert not rl.can_request("prov")
    import time as _t

    _t.sleep(1.1)
    assert rl.can_request("prov")


def test_rate_limit_exponential_default():
    from app.data.resilience import RateLimitManager

    rl = RateLimitManager()
    rl.on_rate_limited("prov")  # no Retry-After -> exponential
    assert rl.wait_time("prov") > 0
    snap = rl.snapshot("prov")
    assert snap["status"] == "DEGRADED"
    assert snap["rate_limit_events"] == 1


def test_circuit_breaker_opens_after_failures():
    from app.data.resilience import RateLimitManager

    rl = RateLimitManager()
    for _ in range(5):
        rl.on_failure("prov", "boom")
    snap = rl.snapshot("prov")
    assert snap["status"] == "OFFLINE"  # breaker open
    assert not rl.can_request("prov")


def test_provider_health_levels():
    from app.data.resilience import ProviderHealthManager

    ph = ProviderHealthManager()
    ph.report("db", "OK", "fine")
    ph.report("api", "DEGRADED", "slow")
    ph.report("net", "OFFLINE", "down")
    assert ph.get("db")["status"] == "OK"
    assert ph.get("api")["warning_count"] == 1
    assert ph.get("net")["error_count"] == 1
    assert ph.get("missing")["status"] == "OK"


# ------------------------------------------------------------------------------
# News & LLM failure handling
# ------------------------------------------------------------------------------
def test_news_unavailable_when_not_configured(monkeypatch):
    from app.agents.agents import NewsAgent

    monkeypatch.setenv("NEWS_RSS_URLS", "")
    from app.config import get_settings

    get_settings.cache_clear()
    out = NewsAgent().analyze("US100")
    assert out.status == "unavailable"
    assert out.sentiment is None  # never fabricated


def test_llm_returns_none_without_key(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    from app.config import get_settings

    get_settings.cache_clear()
    from app.services.llm import llm_available, llm_client
    from pydantic import BaseModel

    class Schema(BaseModel):
        note: str

    assert llm_available() is False
    assert llm_client.ask_structured("hi", Schema) is None
    get_settings.cache_clear()


def test_llm_rejects_malformed_json(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-123")
    from app.config import get_settings

    get_settings.cache_clear()
    from app.services import llm as llm_mod
    from pydantic import BaseModel

    class Schema(BaseModel):
        note: str

    client = llm_mod.LLMClient()
    monkeypatch.setattr(client, "_try_model", lambda *a, **k: "this is not json at all")
    out = client.ask_structured("hi", Schema, use_cache=False)
    assert out is None  # rejected, not fabricated
    get_settings.cache_clear()


def test_llm_parses_fenced_json(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-123")
    from app.config import get_settings

    get_settings.cache_clear()
    from app.services import llm as llm_mod
    from pydantic import BaseModel

    class Schema(BaseModel):
        note: str

    client = llm_mod.LLMClient()
    raw = '```json\n{"note": "all good"}\n```'
    monkeypatch.setattr(client, "_try_model", lambda *a, **k: raw)
    out = client.ask_structured("hi", Schema, use_cache=False)
    assert out is not None and out.note == "all good"
    get_settings.cache_clear()


def test_llm_schema_violation_rejected(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-123")
    from app.config import get_settings

    get_settings.cache_clear()
    from app.services import llm as llm_mod
    from pydantic import BaseModel

    class Schema(BaseModel):
        note: str
        score: float

    client = llm_mod.LLMClient()
    monkeypatch.setattr(client, "_try_model", lambda *a, **k: '{"note": "x", "score": "not-a-number"}')
    assert client.ask_structured("hi", Schema, use_cache=False) is None
    get_settings.cache_clear()


def test_extract_json_helper():
    from app.services.llm import _extract_json

    assert _extract_json('noise {"a": 1} noise') == {"a": 1}
    assert _extract_json("no json here") is None
    assert _extract_json('{"a": {"b": 2}}') == {"a": {"b": 2}}


# ------------------------------------------------------------------------------
# Offline / providers
# ------------------------------------------------------------------------------
def test_local_provider_reads_csv(tmp_path, monkeypatch):
    import app.data.providers as prov_mod

    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    csv = raw_dir / "TEST_15m.csv"
    csv.write_text(
        "timestamp,open,high,low,close,volume\n"
        "2024-01-01 10:00:00,100,101,99,100.5,1234\n"
        "2024-01-01 10:15:00,100.5,102,100,101.5,1500\n"
    )
    monkeypatch.setattr(prov_mod, "RAW_DIR", str(raw_dir))
    provider = prov_mod.LocalFileProvider()
    candles = provider.get_ohlcv("TEST", "15m")
    assert len(candles) == 2
    assert candles[-1]["close"] == 101.5
    assert candles[-1]["volume"] == 1500.0
    assert provider.get_latest("TEST", "15m")["close"] == 101.5


def test_local_provider_missing_file_returns_empty(tmp_path, monkeypatch):
    import app.data.providers as prov_mod

    monkeypatch.setattr(prov_mod, "RAW_DIR", str(tmp_path))
    provider = prov_mod.LocalFileProvider()
    assert provider.get_ohlcv("MISSING", "15m") == []


def test_normalize_row_rejects_inconsistent_ohlc():
    from app.data.providers import normalize_row

    with pytest.raises(ValueError):
        normalize_row("X", "15m", {"timestamp": "2024-01-01 10:00:00", "open": 10,
                                   "high": 5, "low": 20, "close": 11})


def test_normalize_row_rejects_missing_fields():
    from app.data.providers import normalize_row

    with pytest.raises(ValueError):
        normalize_row("X", "15m", {"open": 10, "high": 11, "low": 9})

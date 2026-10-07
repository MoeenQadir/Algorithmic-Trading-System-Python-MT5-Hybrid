"""Shared pytest fixtures: isolated temp SQLite DB, deterministic sample candles."""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.models import database as dbmod  # noqa: E402
from app.services.sample_data import generate_sample_candles  # noqa: E402
from app.utils.timeutils import utcnow  # noqa: E402


@pytest.fixture()
def test_db(monkeypatch):
    """Fresh SQLite DB per test; also isolates cached data files."""
    tmp = tempfile.mkdtemp(prefix="trading_test_")
    url = f"sqlite:///{tmp}/test.db"
    monkeypatch.setenv("DATABASE_URL", url)
    dbmod._engine = None
    dbmod._SessionLocal = None
    import app.data.cache as cache_mod

    monkeypatch.setattr(cache_mod, "CACHE_ROOT", os.path.join(tmp, "cache"))
    dbmod.init_db(url)
    SessionLocal = dbmod.get_session_factory()
    yield SessionLocal
    dbmod._engine = None
    dbmod._SessionLocal = None


@pytest.fixture()
def candles():
    """1200 deterministic sample candles (15m US100)."""
    return generate_sample_candles("US100", "15m", n=1200, seed=42)


@pytest.fixture()
def db(test_db):
    session = test_db()
    yield session
    session.close()


def _make_timestamped_candles(candles: list[dict]) -> list[dict]:
    """Add ascending timestamps to candle list."""
    start = utcnow() - timedelta(minutes=15 * len(candles))
    for i, c in enumerate(candles):
        c = dict(c)
        c["timestamp"] = start + timedelta(minutes=15 * i)
    return candles


@pytest.fixture()
def long_setup_candles():
    """Deterministic candles that produce a valid LONG setup with SL/TP."""
    candles = []
    n = 200
    for i in range(n):
        if i < n - 4:
            o = 100.0
            c = 100.0 + (0.4 if i % 2 == 0 else -0.4)
            high = max(o, c) + 0.1
            low = min(o, c) - 0.1
        elif i == n - 4:
            o, c, high, low = 100.0, 100.1, 100.2, 98.5
        elif i == n - 3:
            o, c, high, low = 100.1, 100.5, 100.6, 99.9
        elif i == n - 2:
            o, c, high, low = 100.5, 103.5, 103.7, 100.3
        else:
            o, c, high, low = 103.5, 104.5, 104.7, 103.3
        candles.append({
            "open": round(o, 4), "high": round(high, 4), "low": round(low, 4),
            "close": round(c, 4), "volume": 1000.0,
        })
    return _make_timestamped_candles(candles)


@pytest.fixture()
def short_setup_candles():
    """Deterministic candles that produce a valid SHORT setup with SL/TP."""
    candles = []
    n = 200
    for i in range(n):
        if i < n - 4:
            o = 100.0
            c = 100.0 + (0.4 if i % 2 == 0 else -0.4)
            high = max(o, c) + 0.1
            low = min(o, c) - 0.1
        elif i == n - 4:
            o, c, high, low = 100.0, 99.9, 101.5, 99.8
        elif i == n - 3:
            o, c, high, low = 99.9, 99.5, 100.1, 99.4
        elif i == n - 2:
            o, c, high, low = 99.5, 96.5, 99.7, 96.3
        else:
            o, c, high, low = 96.5, 95.5, 96.7, 95.3
        candles.append({
            "open": round(o, 4), "high": round(high, 4), "low": round(low, 4),
            "close": round(c, 4), "volume": 1000.0,
        })
    return _make_timestamped_candles(candles)

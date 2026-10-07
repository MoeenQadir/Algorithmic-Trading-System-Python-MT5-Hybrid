from __future__ import annotations

import pytest

from app.data.tick_import import import_tick_months
from app.models.database import MarketData


def test_tick_month_import_rebases_months_and_aggregates_bid_candles(db, tmp_path):
    (tmp_path / "1.csv").write_text(
        "Time,Bid,Ask,Bid_Volume,Ask_Volume\n"
        "2026-01-31 23:59:50.000,10,12,0.1,0.2\n"
        "2026-01-31 23:59:59.000,11,12,0.2,0.3\n",
        encoding="utf-8",
    )
    (tmp_path / "2.csv").write_text(
        "Time,Bid,Ask,Bid_Volume,Ask_Volume\n"
        "2026-01-01 00:00:00.000,12,13,0.3,0.4\n"
        "2026-01-01 04:00:00.000,13,14,0.4,0.5\n",
        encoding="utf-8",
    )

    result = import_tick_months(db, tmp_path, timeframe="4h", batch_size=1)
    assert result["ticks_read"] == 4
    assert result["candles_generated"] == 3
    assert result["candles_imported"] == 3

    rows = db.query(MarketData).filter(MarketData.symbol == "XAUUSD").order_by(MarketData.timestamp).all()
    assert [row.timestamp.isoformat() for row in rows] == [
        "2026-01-31T20:00:00", "2026-02-01T00:00:00", "2026-02-01T04:00:00",
    ]
    first = rows[0]
    assert (first.open, first.high, first.low, first.close) == (10, 11, 10, 11)
    assert first.volume == pytest.approx(0.8)
    assert first.spread == pytest.approx(1.5)
    assert all(row.source == "import_ticks" for row in rows)

    repeated = import_tick_months(db, tmp_path, timeframe="4h", batch_size=2)
    assert repeated["candles_imported"] == 0
    assert repeated["duplicates"] == 3


def test_tick_import_rejects_missing_month_file(db, tmp_path):
    (tmp_path / "1.csv").write_text("Time,Bid,Ask\n2026-01-01 00:00:00,10,11\n", encoding="utf-8")
    (tmp_path / "3.csv").write_text("Time,Bid,Ask\n2026-01-01 00:00:00,10,11\n", encoding="utf-8")
    with pytest.raises(ValueError, match="contiguous sequence"):
        import_tick_months(db, tmp_path)

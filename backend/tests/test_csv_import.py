from __future__ import annotations

from app.data.csv_import import import_csv
from app.models.database import MarketData


def test_large_csv_import_batches_and_skips_duplicates(db, tmp_path):
    csv_file = tmp_path / "xauusd.csv"
    csv_file.write_text(
        "Gmt time,Open,High,Low,Close,Tick volume\n"
        "2025-01-01 00:00:00,2600,2602,2599,2601,100\n"
        "2025-01-01 00:15:00,2601,2604,2600,2603,120\n"
        "2025-01-01 00:15:00,2601,2604,2600,2603,120\n",
        encoding="utf-8",
    )

    first = import_csv(db, str(csv_file), "xauusd", "15m", batch_size=1)
    assert first == {"rows_read": 3, "imported": 2, "duplicates": 1}

    second = import_csv(db, str(csv_file), "XAUUSD", "15m", batch_size=2)
    assert second == {"rows_read": 3, "imported": 0, "duplicates": 3}

    rows = db.query(MarketData).filter(MarketData.symbol == "XAUUSD").all()
    assert len(rows) == 2
    assert all(row.source == "import" for row in rows)
    assert rows[0].volume == 100


def test_import_combines_split_date_time_and_dotted_timestamp(db, tmp_path):
    csv_file = tmp_path / "split_time.csv"
    csv_file.write_text(
        "Date,Time,Open,High,Low,Close\n"
        "2025.01.01,00:00:00,2600,2602,2599,2601\n",
        encoding="utf-8",
    )

    result = import_csv(db, str(csv_file), "XAUUSD", "1h", batch_size=10)
    row = db.query(MarketData).filter(MarketData.symbol == "XAUUSD").one()
    assert result["imported"] == 1
    assert row.timestamp.isoformat() == "2025-01-01T00:00:00"

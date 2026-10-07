import sqlite3
import os

roots = [
    r"E:\Trading Framework\trading.db",
    r"E:\Trading Framework\backend\trading.db"
]

for path in roots:
    print(f"Checking {path}")
    if not os.path.exists(path):
        print("  File does not exist")
        continue
    try:
        db = sqlite3.connect(path)
        cur = db.cursor()
        cur.execute("SELECT COUNT(*), MIN(timestamp), MAX(timestamp) FROM market_data WHERE symbol = 'XAUUSD' AND timeframe = '15m'")
        row = cur.fetchone()
        print(f"  XAUUSD 15m: count={row[0]}, min={row[1]}, max={row[2]}")
        cur.execute("SELECT COUNT(*) FROM market_data WHERE symbol = 'XAUUSD' AND timeframe = '15m' AND source = 'SAMPLE_DATA'")
        sample_count = cur.fetchone()[0]
        print(f"  SAMPLE_DATA count: {sample_count}")
        db.close()
    except Exception as e:
        print(f"  Error: {e}")
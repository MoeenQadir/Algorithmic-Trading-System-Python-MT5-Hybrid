import sys, os
sys.path.insert(0, r"E:\Trading Framework\backend")

# ensure .env loads
os.chdir(r"E:\Trading Framework")
from app.config import get_settings
s = get_settings()
print("DATABASE_URL:", s.database_url)

from app.services.sample_data import seed_symbol
count = seed_symbol("XAUUSD", "15m", n=2000)
print(f"Seeded {count}")

import sqlite3
db = sqlite3.connect(r"E:\Trading Framework\backend\trading.db")
cur = db.cursor()
cur.execute("SELECT COUNT(*), MIN(timestamp), MAX(timestamp), source FROM market_data WHERE symbol='XAUUSD' AND timeframe='15m' GROUP BY source")
for row in cur.fetchall():
    print(row)
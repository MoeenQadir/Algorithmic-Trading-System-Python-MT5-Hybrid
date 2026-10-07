import sqlite3

db = sqlite3.connect(r"E:\Trading Framework\backend\trading.db")
cur = db.cursor()
cur.execute("SELECT DISTINCT symbol, timeframe FROM market_data")
rows = cur.fetchall()
print("symbols/timeframes:", rows)

for sym, tf in rows:
    cur.execute(
        "SELECT COUNT(*), MIN(timestamp), MAX(timestamp) FROM market_data WHERE symbol = ? AND timeframe = ?",
        (sym, tf),
    )
    print(f"{sym} {tf}: count/min/max =", cur.fetchone())

# Check XAUUSD specifically
cur.execute(
    "SELECT COUNT(*), MIN(timestamp), MAX(timestamp) FROM market_data WHERE symbol = 'XAUUSD' AND timeframe = '15m'"
)
print("XAUUSD 15m:", cur.fetchone())
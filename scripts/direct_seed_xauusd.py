import sys
import os
import sqlite3
import random
from datetime import datetime, timedelta

# Directly seed the backend database
script_dir = os.path.dirname(os.path.abspath(__file__))
backend_db_path = os.path.join(script_dir, '..', 'backend', 'trading.db')
backend_db_path = os.path.normpath(backend_db_path)
print(f"Seeding backend database directly: {backend_db_path}")

# Check if file exists
if not os.path.exists(backend_db_path):
    print("ERROR: Backend database file does not exist!")
    print(f"Looking for: {backend_db_path}")
    print(f"Script dir: {script_dir}")
    sys.exit(1)

# Connect to database
db = sqlite3.connect(backend_db_path)
cur = db.cursor()

# Clear existing XAUUSD 15m data (optional)
cur.execute("DELETE FROM market_data WHERE symbol = 'XAUUSD' AND timeframe = '15m'")
deleted = cur.rowcount
print(f"Deleted {deleted} existing XAUUSD 15m records")

# Generate 2000 synthetic candles similar to the sample data generator
symbol = "XAUUSD"
timeframe = "15m"
n = 2000
seed = 12345  # Different seed to avoid conflicts

rng = random.Random(seed)
minutes = 15  # 15m timeframe
end = datetime.now().replace(second=0, microsecond=0)
# align to timeframe boundary
ts = end - timedelta(minutes=minutes * n)
price = 2000.0  # Starting price for XAUUSD (around $2000/oz)
candles = []
regime = "range"
regime_left = 0
trend_drift = 0.0

for i in range(n):
    if regime_left <= 0:
        regime = rng.choice(["range", "uptrend", "downtrend", "volatile"])
        regime_left = rng.randint(40, 120)
        trend_drift = {"range": 0.0, "uptrend": 4.0, "downtrend": -4.0, "volatile": 0.0}[regime]
    regime_left -= 1

    vol = 12.0 if regime == "volatile" else 6.0
    open_price = price
    change = rng.gauss(trend_drift, vol)
    close_price = open_price + change
    wick = abs(rng.gauss(0, vol / 2))
    high = max(open_price, close_price) + wick
    low = min(open_price, close_price) - wick

    # engineered displacement candles
    if rng.random() < 0.03:
        body = (vol * rng.uniform(2, 4)) * (1 if rng.random() < 0.6 else -1)
        close_price = open_price + body
        high = max(open_price, close_price) + wick * 0.5
        low = min(open_price, close_price) - wick * 0.5

    # engineered liquidity sweep: pierce previous extreme then close back inside
    if i > 40 and rng.random() < 0.02 and candles:
        prev_hi = max(c["high"] for c in candles[-20:])
        prev_lo = min(c["low"] for c in candles[-20:])
        if rng.random() < 0.5:
            high = prev_hi + rng.uniform(1, 5)
            close_price = prev_hi - rng.uniform(1, 4)
            if close_price < open_price:
                open_price = close_price + rng.uniform(2, 6)
        else:
            low = prev_lo - rng.uniform(1, 5)
            close_price = prev_lo + rng.uniform(1, 4)
            if close_price > open_price:
                open_price = close_price - rng.uniform(2, 6)
        high = max(high, open_price, close_price)
        low = min(low, open_price, close_price)

    volume = max(1.0, rng.gauss(1000, 200)) * (2.5 if abs(close_price - open_price) > vol * 1.5 else 1.0)
    spread = round(rng.uniform(0.5, 1.5), 2)
    
    candle = {
        "timestamp": ts + timedelta(minutes=minutes * i),
        "symbol": symbol.upper(), 
        "timeframe": timeframe,
        "open": round(open_price, 2), 
        "high": round(high, 2),
        "low": round(low, 2), 
        "close": round(close_price, 2),
        "volume": round(volume, 1), 
        "bid": round(close_price - spread / 2, 2),
        "ask": round(close_price + spread / 2, 2), 
        "spread": spread,
        "source": "SAMPLE_DATA"
    }
    candles.append(candle)
    price = close_price

# Insert all candles
insert_sql = """
INSERT INTO market_data 
(timestamp, symbol, timeframe, open, high, low, close, volume, bid, ask, spread, source)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

for c in candles:
    cur.execute(insert_sql, (
        c["timestamp"], c["symbol"], c["timeframe"], 
        c["open"], c["high"], c["low"], c["close"],
        c["volume"], c["bid"], c["ask"], c["spread"], c["source"]
    ))

db.commit()
print(f"Inserted {len(candles)} XAUUSD 15m SAMPLE_DATA candles")

# Verify
cur.execute("""
SELECT COUNT(*), MIN(timestamp), MAX(timestamp) 
FROM market_data 
WHERE symbol = 'XAUUSD' AND timeframe = '15m' AND source = 'SAMPLE_DATA'
""")
row = cur.fetchone()
print(f"Verification: count={row[0]}, min={row[1]}, max={row[2]}")

db.close()
"""Verify SL/TP triggers for BUY and SELL positions."""
import sqlite3
from datetime import datetime
from unittest.mock import patch

from app.utils.timeutils import utcnow
from app.models.database import Base, MarketData, Trade
from app.execution.broker import PaperBroker
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


# Create engine and session
engine = create_engine("sqlite:///:memory:", future=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)
Base.metadata.create_all(engine)


def make_session():
    return SessionLocal()


# Test 1: BUY with SL and TP - hit TP
print("=== Test 1: BUY hit TP ===")
db = make_session()
db.add(MarketData(symbol="XAUUSD", timeframe="15m", timestamp=utcnow(),
                  open=2600, high=2601, low=2599, close=2600, volume=1000, source="test"))
db.commit()

broker = PaperBroker(db, starting_balance=10000.0)
broker.place_order("XAUUSD", "BUY", 1, "market", stop_price=2595, take_profit=2650)
pos = broker.get_positions()[0]
print(f"Position: dir={pos['direction']}, entry={pos['entry_price']}, SL={pos['stop_loss']}, TP={pos['take_profit']}")
print(f"Quantity: {pos['quantity']}")

# Move price to TP and close
with patch.object(broker, "_last_price", return_value=2650.0):
    result = broker.check_stops_and_targets()
    print(f"check_stops result: {result}")
    close_result = broker.close_position(pos["id"], reason="tp_hit")
    print(f"close_result: {close_result}")
    trade = db.query(Trade).filter(Trade.status == "closed").first()
    print(f"Trade P&L: {trade.pnl}, result: {trade.result}, r_multiple: {trade.r_multiple}")

db.close()

# Test 2: SELL with SL and TP - hit SL
print("\n=== Test 2: SELL hit SL ===")
db2 = make_session()
db2.add(MarketData(symbol="XAUUSD", timeframe="15m", timestamp=utcnow(),
                  open=2600, high=2601, low=2599, close=2600, volume=1000, source="test"))
db2.commit()
broker2 = PaperBroker(db2, starting_balance=10000.0)
broker2.place_order("XAUUSD", "SELL", 1, "market", stop_price=2605, take_profit=2550)
pos2 = broker2.get_positions()[0]
print(f"Position: dir={pos2['direction']}, entry={pos2['entry_price']}, SL={pos2['stop_loss']}, TP={pos2['take_profit']}")

# Move price to SL and close
with patch.object(broker2, "_last_price", return_value=2595.0):
    result2 = broker2.check_stops_and_targets()
    print(f"check_stops result: {result2}")
    close_result2 = broker2.close_position(pos2["id"], reason="sl_hit")
    print(f"close_result: {close_result2}")
    trade2 = db2.query(Trade).filter(Trade.status == "closed").first()
    print(f"Trade P&L: {trade2.pnl}, result: {trade2.result}, r_multiple: {trade2.r_multiple}")

db2.close()

# Test 3: Same-candle SL/TP collision
print("\n=== Test 3: Same-candle SL/TP collision ===")
db3 = make_session()
db3.add(MarketData(symbol="XAUUSD", timeframe="15m", timestamp=utcnow(),
                  open=2600, high=2601, low=2599, close=2600, volume=1000, source="test"))
db3.commit()
broker3 = PaperBroker(db3, starting_balance=10000.0)
# Place a BUY with SL and TP very close together
broker3.place_order("XAUUSD", "BUY", 1, "market", stop_price=2599.50, take_profit=2600.50)
pos3 = broker3.get_positions()[0]
print(f"Position: dir={pos3['direction']}, entry={pos3['entry_price']}, SL={pos3['stop_loss']}, TP={pos3['take_profit']}")

# At the same price as entry - which hits first?
# For LONG: SL hit if price <= stop_loss, TP hit if price >= take_profit
# With entry=2600, SL=2599.50, TP=2600.50
# If price moves to 2599.50 first -> SL hit
# If price moves to 2600.50 first -> TP hit
# Same-collision: if price is exactly at entry and both SL/TP are equidistant
with patch.object(broker3, "_last_price", return_value=2599.50):
    result3 = broker3.check_stops_and_targets()
    print(f"check_stops result (price=2599.50): {result3}")
    if result3:
        close_result3 = broker3.close_position(pos3["id"], reason=result3[0]["reason"])
        trade3 = db3.query(Trade).filter(Trade.status == "closed").first()
        print(f"Closed via: {trade3.result if trade3 else 'none'}, P&L: {trade3.pnl if trade3 else 'N/A'}")

db3.close()

# Test 4: Spread and slippage
print("\n=== Test 4: Spread and slippage ===")
db4 = make_session()
db4.add(MarketData(symbol="XAUUSD", timeframe="15m", timestamp=utcnow(),
                  open=2600, high=2601, low=2599, close=2600, volume=1000, source="test"))
db4.commit()
broker4 = PaperBroker(db4, starting_balance=10000.0, paper_spread_pct=0.002, paper_slippage=0.001)
# Set tighter spread/slippage for testing
broker4.place_order("XAUUSD", "BUY", 1, "market", stop_price=2595, take_profit=2650)
pos4 = broker4.get_positions()[0]
print(f"Position: dir={pos4['direction']}, entry={pos4['entry_price']}, SL={pos4['stop_loss']}, TP={pos4['take_profit']}")

# Hit TP with spread/slippage adjusted price
with patch.object(broker4, "_last_price", return_value=2650.0):
    result4 = broker4.check_stops_and_targets()
    close_result4 = broker4.close_position(pos4["id"], reason="tp_hit")
    trade4 = db4.query(Trade).filter(Trade.status == "closed").first()
    print(f"Trade P&L: {trade4.pnl}, r_multiple: {trade4.r_multiple}")

db4.close()

print("\n=== All P&L/exit verification tests complete ===")
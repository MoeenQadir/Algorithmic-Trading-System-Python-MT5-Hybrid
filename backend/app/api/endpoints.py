from datetime import datetime
from fastapi import APIRouter
from backend.services.market_data import MarketDataService

router = APIRouter()

@router.get("/data/inventory")
def get_inventory():
    service = MarketDataService()
    cursor = service.db.cursor()
    cursor.execute(
        """
        SELECT symbol, timeframe, COUNT(*) as count, MIN(timestamp) as from_time, MAX(timestamp) as to_time
        FROM candles
        GROUP BY symbol, timeframe
        """
    )
    rows = cursor.fetchall()
    return [
        {
            "symbol": row[0],
            "timeframe": row[1],
            "count": row[2],
            "from": datetime.fromtimestamp(row[3]).isoformat(),
            "to": datetime.fromtimestamp(row[4]).isoformat(),
        }
        for row in rows
    ]

import sys
import polars as pl
from datetime import datetime, timezone
import os

# Make sure we're using the backend directory for imports
backend_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'backend')
if backend_path not in sys.path:
    sys.path.insert(0, backend_path)

# Now we can import from app
from app.services.mt5_data import download_mt5_data
from app.config import get_settings

# Check which database the settings point to
s = get_settings()
print("Settings DATABASE_URL:", s.database_url)

# Download XAUUSD M15 and H1 candles for 2026 from MT5
count = download_mt5_data("XAUUSD", "M15", "2026-01-01", "2026-12-31")
print(f"Seeded {count} XAUUSD 15m candles (SAMPLE_DATA)")

# Also directly seed the backend database to make sure
backend_db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'backend', 'trading.db')
print(f"Seeding backend database directly: {backend_db_path}")

# Temporarily override DATABASE_URL to point to backend database
os.environ["DATABASE_URL"] = f"sqlite:///{backend_db_path}"

# Reload settings to pick up the new DATABASE_URL
from importlib import reload
import app.config
reload(app.config)
from app.config import get_settings as get_settings_reloaded
s2 = get_settings_reloaded()
print("Reloaded settings DATABASE_URL:", s2.database_url)

# Seed again with the backend database
count2 = download_mt5_data("XAUUSD", "H1", "2026-01-01", "2026-12-31")
print(f"Seeded {count2} XAUUSD 15m candles (SAMPLE_DATA) into backend database")
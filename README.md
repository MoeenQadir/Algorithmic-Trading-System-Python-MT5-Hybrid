# Hi-Tech (Trading System)

A local-first research and paper-trading modular monolith by **Hi-Tech** — founded and led by **Moeen Ul Qadir (CEO)**. The default is SQLite, local/sample data, deterministic analysis, and paper execution. It does not route live orders. This project is for research and education; it does not promise profitability.

## Architecture

`Market data -> analysis -> agents -> weighted decision -> deterministic risk veto -> PaperBroker -> journal/performance`

- `backend/app/data`: replaceable providers, local file loading, cache, rate limits, health state.
- `backend/app/analysis`: indicators and configurable setup detection.
- `backend/app/agents`, `decision`, `risk`: structured agent outputs, weighted decisions, deterministic risk and sizing.
- `backend/app/execution`: paper broker only. The isolated Robinhood adapter is disabled and is not a working live integration.
- `backend/app/backtesting`: historical event loop with visible-candle slices and next-bar entry fills.
- `backend/app/models`: SQLAlchemy models. SQLite is zero-setup; PostgreSQL URLs are supported by SQLAlchemy.
- `frontend`: Next.js research console consuming the FastAPI API.

## Features (Milestones 21-30)

- **M21 Watchlist**: Track symbols/timeframes, persist across sessions
- **M22 Strategy Config UI**: Full configuration editor for risk, agents, strategy, execution, general settings
- **M23 Alerts**: Price, indicator, signal, and risk alerts with cooldowns and trigger history
- **M24 Walk-Forward Robustness**: Visualize walk-forward backtest results with train/test splits
- **M25 Experiment Tracking**: Track strategy experiments with config snapshots, metrics, and status
- **M26 System Health Dashboard**: Real-time provider health, rate limits, component status, system events
- **M27 Charting Enhancements**: SMA 20/50, VWAP, volume histogram overlays on candlestick charts
- **M28 Offline Mode Indicators**: Clear LOCAL CACHE / RATE LIMITED / OFFLINE status pills in header
- **M29 Seed Data**: One-command demo data population for immediate dashboard exploration
- **M30 Documentation**: This README covers setup, usage, and all features

## Windows Setup

Requirements: Python 3.12+, Node.js 20+ (Node 24 also works), npm. Docker and Redis are not required.

**Option A: One-command setup (recommended)**

```powershell
python scripts/setup.py
```

This creates the virtual environment, installs all dependencies, runs migrations, and creates `.env` from `.env.example`.

**Option B: Manual setup**

1. Copy `.env.example` to `.env`. OpenRouter is optional; keep `OPENROUTER_API_KEY` empty for deterministic/offline analysis.
2. Install dependencies and apply the schema migration before the first startup:

```powershell
.\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt
cd frontend
npm install
cd ..
.\.venv\Scripts\python.exe -m alembic -c backend\alembic.ini upgrade head
```

For an existing database created before Alembic was added, back it up first, verify its schema matches the current models, then baseline it with `python -m alembic -c backend\alembic.ini stamp head`. Do not run the initial migration against an existing unversioned schema without stamping it.

3. From the project root, run `start.bat` or `./start.ps1` in PowerShell. The scripts start the backend on `127.0.0.1:8000`, the dashboard on `127.0.0.1:3000` (or the next free local port), and open the dashboard.

Backend API docs: <http://127.0.0.1:8000/docs>. Dashboard: <http://127.0.0.1:3000/dashboard>.

### Seed Demo Data (Optional)

Populate the database with sample watchlist items, alerts, experiments, backtests, agent signals, decisions, positions, and system events:

```powershell
.\.venv\Scripts\python.exe scripts\seed.py
```

## Environment

The full set of options and safe defaults is in `.env.example`. Important values:

- `DATABASE_URL=sqlite:///./trading.db`
- `PAPER_TRADING=true`, `LIVE_TRADING=false`
- `DEFAULT_SYMBOL=US100`, `DEFAULT_TIMEFRAME=15m`
- `MAX_RISK_PER_TRADE=0.004`, `MAX_DAILY_LOSS=0.012`
- `OPENROUTER_API_KEY=` (optional), `OPENROUTER_MODEL=openrouter/free`
- `LLM_CALL_MODE=ON_SIGNAL`
- `MARKET_DATA_PROVIDER=local` by default; `stooq` provides daily data, while `twelvedata` can poll intraday bars when configured.

Secrets belong only in `.env`; never put provider keys in frontend environment variables or `.env.example`. The API config endpoint deliberately omits secrets. If a real key was ever placed in `.env.example`, revoke it and create a replacement before continuing.

## Market Data and Offline Use

The system seeds clearly labeled deterministic **SAMPLE DATA** locally on first startup. It is synthetic, not actual exchange data. Small CSV/JSON and Parquet files can be read locally. For multi-gigabyte CSVs, use the streaming importer below; do not use the `/market/import` endpoint, which is intended for smaller local files.

Historical analysis, indicators, strategies, backtests, paper trading against stored candles, dashboard, journal, and performance work locally once data is present. News and OpenRouter interpretation are optional. External market/news/LLM calls are cached and rate-limited; an unavailable provider degrades the service rather than inventing data. Cache TTLs are configurable with `CACHE_TTL_OHLCV`, `CACHE_TTL_NEWS`, and `CACHE_TTL_LLM`.

### Intraday XAU/USD Signals

Twelve Data may be used as an opt-in intraday candle source. The backend polls at `POLLING_INTERVAL` seconds, stores completed bars, and runs a read-only analysis cycle when a new bar closes. Scheduled cycles journal decisions but do not place paper or live orders. This is candle polling, not a tick-by-tick WebSocket stream; signal cadence is limited by the selected timeframe and provider freshness.

1. Create a Twelve Data API key and confirm your account can access `XAU/USD` intraday time series. API access and streaming entitlements vary by plan.
2. Put the key only in the ignored root `.env`; never commit it or put it in frontend configuration:

```dotenv
MARKET_DATA_PROVIDER=twelvedata
TWELVE_DATA_API_KEY=your_key_here
DEFAULT_SYMBOL=XAUUSD
DEFAULT_TIMEFRAME=15m
POLLING_INTERVAL=60
PAPER_TRADING=true
LIVE_TRADING=false
```

3. Restart the backend and check `GET /status` for `live_signal_polling: true`, then inspect `GET /market/XAUUSD?timeframe=15m` and `GET /signals?symbol=XAUUSD`. An entitlement, authentication, or quota error appears in provider health/logs; it will not silently substitute prices.

The current XAUUSD backtest is only four trades with negative expectancy, so live polling should initially be used for observation and paper research, not as a performance claim. Stooq remains daily-only. OpenRouter is an optional language-model service, not a market-price feed.

## Dashboard Usage

Navigate to <http://127.0.0.1:3000/dashboard> for the research console.

**Navigation Sections:**

| Section | Description |
|---------|-------------|
| **Dashboard** | Portfolio metrics, equity curve, agent performance, recent trades |
| **Market** | Candlestick chart with SMA/VWAP/volume, quality/gaps/resample tabs, data provenance |
| **Signals** | Recent agent signals, decisions, and risk checks |
| **Backtests** | List and run backtests, click for detail with equity curve and trades |
| **Walk-Forward** | Configure and run walk-forward analysis, view train/test period results |
| **Experiments** | Track strategy experiments with config snapshots and metrics |
| **Performance** | Closed trade statistics, agent accuracy, expectancy |
| **Watchlist** | Manage tracked symbols/timeframes |
| **Config** | Edit risk, agent weights, strategy parameters, execution settings |
| **Alerts** | Create and manage price/indicator/signal/risk alerts |
| **Health** | System status, provider health, rate limits, recent events |

**Key Features:**

- **Market Chart**: Toggle SMA 20/50, VWAP, and volume overlay
- **Data Source Pill**: Header shows `LOCAL CACHE` (green), `RATE LIMITED` (amber), or `OFFLINE` (red)
- **Walk-Forward**: Configure train/test windows, run analysis, view period-by-period metrics
- **Experiments**: Create experiments, update status, compare metrics across runs
- **Alerts**: Set conditions (above/below/crosses), cooldowns, view trigger history
- **Health Dashboard**: Real-time provider status, rate limit snapshots, component health, event log

## API Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /health` | Basic service health |
| `GET /status` | Full system status with provider health, rate limits |
| `GET /market/{symbol}` | Candle data with quality/gaps/resample info |
| `GET /signals` | Recent agent signals |
| `GET /agents` | Agent performance metrics |
| `GET /decisions` | Recent trading decisions |
| `POST /cycle` | Run one analysis cycle |
| `GET /backtests` | List backtest runs |
| `POST /backtests/run` | Run a new backtest |
| `POST /backtests/walk-forward` | Run walk-forward analysis |
| `GET /experiments` | List experiments |
| `POST /experiments` | Create experiment |
| `PATCH /experiments/{id}` | Update experiment status/metrics |
| `GET /watchlist` | List watchlist items |
| `POST /watchlist` | Add watchlist item |
| `DELETE /watchlist/{id}` | Remove watchlist item |
| `GET /alerts` | List alerts |
| `POST /alerts` | Create alert |
| `DELETE /alerts/{id}` | Delete alert |
| `PATCH /alerts/{id}/toggle` | Toggle alert active state |
| `POST /alerts/check` | Check alerts against current data |
| `GET /config` | Get current configuration |
| `POST /config/update` | Update configuration |
| `GET /positions` | Open paper positions |
| `POST /paper/orders` | Place paper order |
| `POST /paper/close/{id}` | Close paper position |
| `POST /paper/reset` | Reset paper account |

## Import and Test XAUUSD History

1. Revoke any OpenRouter key that was stored in `.env.example`, then create a replacement in your OpenRouter account. Do not paste it into chat or commit it.
2. Stop the running backend before importing. From the project root, copy the template if needed and edit the root `.env`:

```powershell
Copy-Item .env.example .env
notepad .env
```

Set `OPENROUTER_API_KEY` to the newly rotated key, and set `DEFAULT_SYMBOL=XAUUSD`, `DEFAULT_TIMEFRAME=4h`, `MARKET_DATA_PROVIDER=local`, and `LLM_CALL_MODE=MANUAL` while you validate historical data. Keep `PAPER_TRADING=true`, `LIVE_TRADING=false`, and the default SQLite `DATABASE_URL`. Backtests are deterministic and do not need OpenRouter.

3. Your supplied files have tick columns `Time,Bid,Ask,Bid_Volume,Ask_Volume`, not candle OHLC. Inspection found every tick timestamp in all eight numbered files and the merged master says January 2026. Since you confirmed `1.csv` through `8.csv` correspond to January through August, the tick importer maps each numbered file to its filename month. It assumes the timestamps are UTC; confirm that matches the data vendor before treating the dates as UTC. It imports the numbered files only. Do not import the merged master: its repeated January timestamps interleave the eight series.

4. Stop the backend, then convert and import the eight numbered files directly from their current location:

```powershell
.\.venv\Scripts\python.exe scripts\import_tick_months.py --input-dir "D:\Trading\Gold data\Gold CSV" --symbol XAUUSD --timeframe 4h --year 2026 --source-month 1 --batch-size 5000
```

The converter reads bounded Polars batches, uses **Bid** for OHLC, uses `Bid_Volume + Ask_Volume` for candle volume, and retains average spread and the last quote. It commits each month and is safe to rerun. Make sure there is several GB of free space for SQLite. Do not start the app with XAUUSD as its default until after importing, or startup may seed synthetic XAUUSD candles.

5. Verify the newest bars and their provenance:

```powershell
$market = Invoke-RestMethod "http://127.0.0.1:8000/market/XAUUSD?timeframe=4h&limit=3"
$market | Select-Object symbol,timeframe,count,source,is_sample
$market.candles
```

For a clean real-data test, expect `source` to show `LOCAL IMPORT` and `is_sample` to be `False`. If it says `SAMPLE DATA` or `is_sample` is `True`, stop and inspect the imported timestamps/source rows before interpreting any result.

6. Start the services with `start.ps1` or `start.bat`. Open the dashboard at the printed URL, select `XAUUSD` and `4h`, then use **Run analysis**. Alternatively, run one cycle from PowerShell:

```powershell
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/cycle?symbol=XAUUSD&timeframe=4h" | ConvertTo-Json -Depth 8
```

7. Run a backtest over the imported bars:

```powershell
.\.venv\Scripts\python.exe scripts\run_backtest.py --symbol XAUUSD --timeframe 4h --limit 5000 --equity 10000
```

The imported dataset contained 62,162,136 ticks and produced 1,057 four-hour candles from January 1 through August 31, 2026. The first backtest completed with 4 trades, 0% win rate, -$167.26 P&L, 1.67% max drawdown, and -1.03R expectancy on a $10,000 starting balance. This is a negative, very small-sample result; it is not evidence for live use. Backtests use Python calculations and do not need OpenRouter.

- For finer controls, use `POST /backtests/run` in <http://127.0.0.1:8000/docs>; its current API path evaluates at most 5,000 recent candles. The CLI is the route for a larger local range.
- This is paper/research testing only. Do not infer future performance from one year of historical results.

## Quick Start Commands

```powershell
# Run one analysis cycle
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/cycle?symbol=US100&timeframe=15m"

# Run a backtest via API
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/backtests/run -ContentType 'application/json' -Body '{"symbol":"US100","timeframe":"15m","params":{"max_atr_pct":0.05,"starting_equity":10000}}'

# Place paper order
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/paper/orders -ContentType 'application/json' -Body '{"symbol":"US100","side":"BUY","quantity":0.1,"order_type":"market"}'

# Reset paper account
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/paper/reset?confirm=true"
```

Use paper execution only after local prices are available. `POST /paper/orders` accepts `symbol`, `side` (`BUY`/`SELL`), `quantity`, and optional `order_type`, `limit_price`, `stop_price`, and `take_profit`. Close a position with `POST /paper/close/{position_id}`. Do not use this as a live-trading service.

Inspect positions, orders, trades, provider health, and performance in the dashboard or API.

## Providers and Extensions

Implement `MarketDataProvider` in `backend/app/data/providers.py` for another source, preserving the normalized candle schema and checking cache before outbound calls. News providers implement the provider interface in `backend/app/services/news.py`; no configured news means `unavailable`, not fabricated sentiment. Agents return Pydantic-validated structures in `backend/app/agents/agents.py`. Add broker implementations against `BrokerInterface` in `backend/app/execution/broker.py`; live routing remains intentionally disabled until a real adapter, authentication, risk review, and explicit configuration are built and tested.

## Tests and Checks

```powershell
# Backend tests (162 tests)
.\.venv\Scripts\python.exe -m pytest backend\tests -q

# Frontend typecheck and build
cd frontend
npm run typecheck
npm run build
```

The backend test suite includes indicator, strategy, sizing/risk, broker, database, caching, rate-limit, LLM failure, API, backtest, and end-to-end workflow tests.

## Limitations and Safety

- Sample data is synthetic. Stooq is daily-only. Twelve Data intraday access requires a valid key and symbol entitlement.
- The Robinhood adapter is deliberately nonfunctional and disabled. `LIVE_TRADING=true` does not activate live routing; the application continues to use paper execution.
- PostgreSQL support is connection-URL based; the baseline migration is generated from SQLAlchemy metadata and has been verified on SQLite. SQLite tables are also created automatically at backend startup for zero-setup operation.
- The frontend is a local research console, not a broker terminal. It does not submit orders automatically and should not be exposed to the public internet.
- Free providers may impose their own limits. Caching and backoff reduce calls but cannot change provider policies.
- Trading involves substantial risk. No strategy, backtest, or agent output guarantees future results.
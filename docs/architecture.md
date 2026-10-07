# Architecture Notes

This repository is a local-first modular monolith. FastAPI serves the API and coordinates a single research cycle; SQLAlchemy is the persisted source of truth; SQLite is the no-setup default. Schema revisions live under `backend/migrations`; apply them from the repository root with `python -m alembic -c backend/alembic.ini upgrade head`.

## Data and decisions

Providers normalize candles, optionally use the local file cache, and report failures through rate-limit/provider-health managers. The analysis engine computes indicators in Python. Agents emit structured evidence; the decision engine weighs that evidence but does not override the deterministic risk engine. The risk engine validates risk/reward, price freshness, spread, daily losses, consecutive losses, open positions, and position sizing before execution.

## Execution and backtesting

Only `PaperBroker` is instantiated. A market order needs a locally available price; fills, commissions, positions, and journal rows are stored. The backtester processes ascending candles, limits each evaluation to the current candle prefix, enters no earlier than the next candle open, and resolves same-bar stop/target ambiguity conservatively by checking the stop first.

## Boundaries

The web app calls the local REST API and never receives LLM credentials. Optional news and OpenRouter integrations can fail without disabling deterministic analysis. No Docker, Redis, paid API, or cloud service is required for the core workflow.

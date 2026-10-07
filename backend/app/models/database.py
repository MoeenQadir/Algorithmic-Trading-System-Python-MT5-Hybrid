"""SQLAlchemy ORM models. All timestamps are UTC.

This is the source of truth for the whole platform: candles, news, agent
signals, decisions, risk checks, orders, fills, positions, trades, portfolio
snapshots, backtests, agent performance and system events.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    create_engine,
    event,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

from app.utils.timeutils import utcnow


class Base(DeclarativeBase):
    pass


def utc_now_col() -> datetime:
    return utcnow()


# ------------------------------------------------------------------------------
# Watchlist
# ------------------------------------------------------------------------------
class WatchlistItem(Base):
    __tablename__ = "watchlist_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(20), index=True)
    timeframe: Mapped[str] = mapped_column(String(10), default="1h")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_col, index=True)


# ------------------------------------------------------------------------------
# Alerts
# ------------------------------------------------------------------------------
class Alert(Base):
    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(20), index=True)
    timeframe: Mapped[str] = mapped_column(String(10), default="1h")
    alert_type: Mapped[str] = mapped_column(String(30), index=True)  # price|indicator|signal|risk
    condition: Mapped[str] = mapped_column(String(10))  # above|below|crosses_up|crosses_down|equals
    target_value: Mapped[float] = mapped_column(Float)
    current_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    message: Mapped[str] = mapped_column(Text, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    triggered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_col, index=True)


# ------------------------------------------------------------------------------
# Settings
# ------------------------------------------------------------------------------
class UserSettings(Base):
    __tablename__ = "user_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_col, onupdate=utc_now_col)


class SystemSettings(Base):
    __tablename__ = "system_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    value: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(50), default="general")
    description: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_col, onupdate=utc_now_col)


# ------------------------------------------------------------------------------
# Market data
# ------------------------------------------------------------------------------
class MarketData(Base):
    __tablename__ = "market_data"
    __table_args__ = (
        Index("ix_market_data_symbol_ts", "symbol", "timestamp"),
        Index("ix_market_data_symbol_tf_ts", "symbol", "timeframe", "timestamp"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(20), index=True)
    timeframe: Mapped[str] = mapped_column(String(10), index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, index=True)
    open: Mapped[float] = mapped_column(Float)
    high: Mapped[float] = mapped_column(Float)
    low: Mapped[float] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    volume: Mapped[float | None] = mapped_column(Float, nullable=True)
    bid: Mapped[float | None] = mapped_column(Float, nullable=True)
    ask: Mapped[float | None] = mapped_column(Float, nullable=True)
    spread: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[str] = mapped_column(String(30), default="local")


class NewsEvent(Base):
    __tablename__ = "news_events"
    __table_args__ = (Index("ix_news_published_ts", "published_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    headline: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(100))
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    symbols: Mapped[str] = mapped_column(String(200), default="")
    sentiment_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    impact: Mapped[str | None] = mapped_column(String(20), nullable=True)  # low|medium|high
    dedup_key: Mapped[str] = mapped_column(String(200), unique=True, index=True)


# ------------------------------------------------------------------------------
# Agents / decisions
# ------------------------------------------------------------------------------
class AgentSignal(Base):
    __tablename__ = "agent_signals"
    __table_args__ = (
        Index("ix_agent_signals_agent_ts", "agent_name", "created_at"),
        Index("ix_agent_signals_symbol_agent", "symbol", "agent_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    agent_name: Mapped[str] = mapped_column(String(50), index=True)
    symbol: Mapped[str] = mapped_column(String(20), index=True)
    timeframe: Mapped[str] = mapped_column(String(10))
    direction: Mapped[str] = mapped_column(String(10))  # LONG|SHORT|NEUTRAL|WAIT
    confidence: Mapped[float] = mapped_column(Float)
    payload: Mapped[str] = mapped_column(Text, default="{}")  # full agent output JSON
    llm_used: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_col, index=True)
    decision_id: Mapped[int | None] = mapped_column(ForeignKey("trading_decisions.id"), nullable=True, index=True)

    decision: Mapped["TradingDecision | None"] = relationship(back_populates="signals")


class TradingDecision(Base):
    __tablename__ = "trading_decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(20), index=True)
    timeframe: Mapped[str] = mapped_column(String(10))
    action: Mapped[str] = mapped_column(String(10))  # BUY|SELL|WAIT|REJECT
    direction: Mapped[str] = mapped_column(String(10))
    confidence: Mapped[float] = mapped_column(Float)
    entry: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    take_profit: Mapped[float | None] = mapped_column(Float, nullable=True)
    risk_reward: Mapped[float | None] = mapped_column(Float, nullable=True)
    evidence: Mapped[str] = mapped_column(Text, default="{}")
    agent_conflicts: Mapped[str] = mapped_column(Text, default="[]")
    decision_reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_col, index=True)

    signals: Mapped[list["AgentSignal"]] = relationship(back_populates="decision")
    risk_checks: Mapped[list["RiskCheck"]] = relationship(back_populates="decision")


class RiskCheck(Base):
    __tablename__ = "risk_checks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    decision_id: Mapped[int | None] = mapped_column(ForeignKey("trading_decisions.id"), nullable=True, index=True)
    symbol: Mapped[str] = mapped_column(String(20), index=True)
    verdict: Mapped[str] = mapped_column(String(20))  # APPROVED|REJECTED|REDUCED_SIZE|WAIT
    violations: Mapped[str] = mapped_column(Text, default="[]")  # JSON list
    position_size: Mapped[float | None] = mapped_column(Float, nullable=True)
    risk_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    details: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_col, index=True)

    decision: Mapped[TradingDecision | None] = relationship(back_populates="risk_checks")


# ------------------------------------------------------------------------------
# Orders / positions / trades
# ------------------------------------------------------------------------------
class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    broker_order_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    symbol: Mapped[str] = mapped_column(String(20), index=True)
    side: Mapped[str] = mapped_column(String(10))  # BUY|SELL
    order_type: Mapped[str] = mapped_column(String(10), default="market")  # market|limit|stop
    quantity: Mapped[float] = mapped_column(Float)
    limit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    take_profit: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending|filled|cancelled|rejected
    filled_quantity: Mapped[float] = mapped_column(Float, default=0.0)
    avg_fill_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    related_position_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_col, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_col, onupdate=utc_now_col)

    fills: Mapped[list["Fill"]] = relationship(back_populates="order")
    trade: Mapped["Trade | None"] = relationship(back_populates="order")


class Fill(Base):
    __tablename__ = "fills"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    quantity: Mapped[float] = mapped_column(Float)
    price: Mapped[float] = mapped_column(Float)
    commission: Mapped[float] = mapped_column(Float, default=0.0)
    slippage: Mapped[float] = mapped_column(Float, default=0.0)
    filled_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_col, index=True)

    order: Mapped[Order] = relationship(back_populates="fills")


class Position(Base):
    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(20), index=True)
    direction: Mapped[str] = mapped_column(String(10))  # LONG|SHORT
    quantity: Mapped[float] = mapped_column(Float)
    entry_price: Mapped[float] = mapped_column(Float)
    stop_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    take_profit: Mapped[float | None] = mapped_column(Float, nullable=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_col, index=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(10), default="open", index=True)  # open|closed
    close_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    trade_id: Mapped[int | None] = mapped_column(ForeignKey("trades.id"), nullable=True, index=True)

    trade: Mapped["Trade | None"] = relationship(foreign_keys=[trade_id], uselist=False)


class Trade(Base):
    __tablename__ = "trades"
    __table_args__ = (Index("ix_trades_symbol_ts", "symbol", "entry_time"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(20), index=True)
    direction: Mapped[str] = mapped_column(String(10))
    entry_time: Mapped[datetime] = mapped_column(DateTime, default=utc_now_col, index=True)
    exit_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    entry_price: Mapped[float] = mapped_column(Float)
    stop_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    take_profit: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    quantity: Mapped[float] = mapped_column(Float)
    risk_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    r_multiple: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="open")  # open|closed
    result: Mapped[str | None] = mapped_column(String(10), nullable=True)  # win|loss|breakeven
    reason: Mapped[str] = mapped_column(Text, default="")  # exit reason / decision reason
    decision_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    position_id: Mapped[int | None] = mapped_column(ForeignKey("positions.id"), nullable=True, index=True)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"), nullable=True, index=True)
    agent_outputs: Mapped[str] = mapped_column(Text, default="{}")  # audit: agent outputs at entry
    risk_checks: Mapped[str] = mapped_column(Text, default="[]")  # audit: risk check results
    execution_info: Mapped[str] = mapped_column(Text, default="{}")  # fills, fees, slippage

    order: Mapped[Order | None] = relationship(back_populates="trade")
    position: Mapped[Position | None] = relationship(foreign_keys=[position_id], uselist=False)


class PortfolioSnapshot(Base):
    __tablename__ = "portfolio_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    equity: Mapped[float] = mapped_column(Float)
    cash: Mapped[float] = mapped_column(Float)
    open_positions: Mapped[int] = mapped_column(Integer, default=0)
    unrealized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    daily_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    taken_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_col, index=True)


# ------------------------------------------------------------------------------
# Backtesting
# ------------------------------------------------------------------------------
class BacktestRun(Base):
    __tablename__ = "backtest_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(20), index=True)
    timeframe: Mapped[str] = mapped_column(String(10))
    start: Mapped[datetime] = mapped_column(DateTime)
    end: Mapped[datetime] = mapped_column(DateTime)
    params: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(20), default="completed")
    starting_equity: Mapped[float] = mapped_column(Float)
    ending_equity: Mapped[float] = mapped_column(Float)
    total_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    trade_count: Mapped[int] = mapped_column(Integer, default=0)
    win_rate: Mapped[float] = mapped_column(Float, default=0.0)
    profit_factor: Mapped[float] = mapped_column(Float, default=0.0)
    expectancy_r: Mapped[float] = mapped_column(Float, default=0.0)
    max_drawdown: Mapped[float] = mapped_column(Float, default=0.0)
    sharpe: Mapped[float | None] = mapped_column(Float, nullable=True)
    sortino: Mapped[float | None] = mapped_column(Float, nullable=True)
    equity_curve: Mapped[str] = mapped_column(Text, default="[]")  # JSON [[ts, equity], ...]
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_col, index=True)

    trades: Mapped[list["BacktestTrade"]] = relationship(back_populates="run")


class BacktestTrade(Base):
    __tablename__ = "backtest_trades"
    __table_args__ = (Index("ix_backtest_trades_run", "run_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("backtest_runs.id"), index=True)
    symbol: Mapped[str] = mapped_column(String(20))
    direction: Mapped[str] = mapped_column(String(10))
    entry_time: Mapped[datetime] = mapped_column(DateTime, index=True)
    exit_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    entry_price: Mapped[float] = mapped_column(Float)
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    take_profit: Mapped[float | None] = mapped_column(Float, nullable=True)
    quantity: Mapped[float] = mapped_column(Float)
    pnl: Mapped[float] = mapped_column(Float, default=0.0)
    r_multiple: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_reason: Mapped[str] = mapped_column(String(30), default="")
    mae: Mapped[float | None] = mapped_column(Float, nullable=True)  # max adverse excursion
    mfe: Mapped[float | None] = mapped_column(Float, nullable=True)  # max favorable excursion

    run: Mapped[BacktestRun] = relationship(back_populates="trades")


# ------------------------------------------------------------------------------
# Agent performance / system events
# ------------------------------------------------------------------------------
class AgentPerformance(Base):
    __tablename__ = "agent_performance"
    __table_args__ = (Index("ix_agent_perf_agent_ts", "agent_name", "timestamp"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    agent_name: Mapped[str] = mapped_column(String(50), index=True)
    symbol: Mapped[str] = mapped_column(String(20), index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=utc_now_col)
    prediction: Mapped[str] = mapped_column(String(10))  # LONG|SHORT|NEUTRAL
    confidence: Mapped[float] = mapped_column(Float)
    decision_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    trade_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    outcome: Mapped[str | None] = mapped_column(String(10), nullable=True)  # correct|incorrect|neutral
    pnl_contribution: Mapped[float | None] = mapped_column(Float, nullable=True)
    r_contribution: Mapped[float | None] = mapped_column(Float, nullable=True)
    evaluated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class SystemEvent(Base):
    __tablename__ = "system_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    level: Mapped[str] = mapped_column(String(10), index=True)  # INFO|WARNING|ERROR
    component: Mapped[str] = mapped_column(String(50), index=True)
    message: Mapped[str] = mapped_column(Text)
    details: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_col, index=True)


# ------------------------------------------------------------------------------
# Idempotency keys for duplicate prevention (Milestone 18)
# ------------------------------------------------------------------------------
class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    operation: Mapped[str] = mapped_column(String(50), index=True)  # place_order|close_position|etc
    symbol: Mapped[str] = mapped_column(String(20), index=True)
    payload_hash: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_col, index=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    result: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON result


# ------------------------------------------------------------------------------
# Experiment tracking (Milestone 25)
# ------------------------------------------------------------------------------
class Experiment(Base):
    __tablename__ = "experiments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    tags: Mapped[str] = mapped_column(Text, default="[]")  # JSON array of strings
    config: Mapped[str] = mapped_column(Text, default="{}")  # JSON config snapshot
    metrics: Mapped[str] = mapped_column(Text, default="{}")  # JSON metrics
    status: Mapped[str] = mapped_column(String(20), default="running")  # running|completed|failed|archived
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_col, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now_col, onupdate=utc_now_col)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


# ------------------------------------------------------------------------------
# Engine / session management
# ------------------------------------------------------------------------------
_engine = None
_SessionLocal = None


def _configure_sqlite_pragmas(dbapi_connection, _record) -> None:  # pragma: no cover
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.close()


def get_engine(database_url: str | None = None):
    global _engine
    if database_url is None:
        database_url = None  # resolved lazily below
    from app.config import get_settings

    url = database_url or get_settings().database_url
    if _engine is None or database_url is not None:
        kwargs: dict = {"future": True}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False}
        _engine = create_engine(url, **kwargs)
        if url.startswith("sqlite"):
            event.listen(_engine, "connect", _configure_sqlite_pragmas)
    return _engine


def get_session_factory(engine=None):
    global _SessionLocal
    if _SessionLocal is None or engine is not None:
        _SessionLocal = sessionmaker(bind=engine or get_engine(), expire_on_commit=False, future=True)
    return _SessionLocal


def init_db(database_url: str | None = None) -> None:
    """Create all tables. SQLite-friendly; migrations via Alembic optional."""
    engine = get_engine(database_url)
    Base.metadata.create_all(engine)


def get_db():
    """FastAPI dependency yielding a session."""
    SessionLocal = get_session_factory()
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

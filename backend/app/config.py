"""Application settings loaded from environment / .env (pydantic-settings).

Design rules:
- No secret is required to start the app (OpenRouter key is optional).
- LIVE_TRADING must be explicitly enabled; default is paper-only.
- All risk limits are plain numbers enforced deterministically by the Risk Engine.
"""
from __future__ import annotations

from functools import lru_cache
from typing import List

from pydantic_settings import BaseSettings, SettingsConfigDict


def _env_list(name: str, default: str = "") -> List[str]:
    raw = os.environ.get(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


import os  # noqa: E402  (kept after docstring imports for clarity)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False)

    # --- AI / OpenRouter (optional) ---
    openrouter_api_key: str = ""
    openrouter_model: str = "openrouter/free"
    openrouter_fallback_model: str = ""
    llm_call_mode: str = "ON_SIGNAL"  # ALWAYS | ON_SIGNAL | ON_CONFLICT | MANUAL
    openrouter_timeout: int = 30
    llm_max_retries: int = 2

    # --- Database ---
    database_url: str = "sqlite:///./trading.db"

    # --- Trading mode ---
    paper_trading: bool = True
    live_trading: bool = False

    # --- Defaults ---
    default_symbol: str = "US100"
    default_timeframe: str = "15m"

    # --- Risk limits (deterministic; enforced by RiskEngine) ---
    max_risk_per_trade: float = 0.004
    max_daily_loss: float = 0.012
    max_consecutive_losses: int = 3
    max_open_positions: int = 3
    min_risk_reward: float = 2.0
    max_position_size: float = 50000.0
    max_symbol_exposure: float = 0.5
    max_total_exposure: float = 1.0
    max_spread_pct: float = 0.0015
    max_data_age_minutes: int = 120
    enforce_market_hours: bool = False
    news_blackout_minutes: int = 30

    # --- Position sizing ---
    risk_percentage: float = 0.004

    # --- Paper account / execution model ---
    paper_account_balance: float = 10000.0
    paper_commission: float = 0.0005
    paper_slippage: float = 0.0002
    paper_spread_pct: float = 0.0002

    # --- Decision engine weights ---
    technical_weight: float = 1.0
    quant_weight: float = 0.8
    strategy_weight: float = 1.5
    regime_weight: float = 0.6
    news_weight: float = 0.4
    sentiment_weight: float = 0.3
    decision_threshold: float = 0.35

    # --- Strategy parameters ---
    swing_lookback: int = 3
    sweep_max_age: int = 20
    displacement_required: bool = True
    displacement_atr: float = 1.2
    stop_buffer_atr: float = 0.25

    # --- Market data ---
    market_data_provider: str = "mt5"  # local | mt5 | stooq | twelvedata
    market_data_provider_chain: str = "mt5,local,stooq,twelvedata"  # comma-separated fallback order
    
    # --- MT5 settings ---
    mt5_login: int = 0
    mt5_password: str = ""
    mt5_server: str = ""
    mt5_path: str = ""
    
    twelve_data_api_key: str = ""
    polling_interval: int = 60
    cache_ttl_ohlcv: int = 300
    cache_ttl_news: int = 900
    cache_ttl_llm: int = 86400

    # --- Twelve Data credit model (Phase 35) ---
    twelvedata_minute_limit: int = 8
    twelvedata_daily_limit: int = 800
    twelvedata_default_credit_cost: int = 1
    twelvedata_time_series_credit_cost: int = 1
    twelvedata_quote_credit_cost: int = 1
    twelvedata_symbol_search_credit_cost: int = 1
    twelvedata_api_usage_credit_cost: int = 1
    twelvedata_api_usage_endpoint: str = "/api_usage"
    twelvedata_api_usage_cache_seconds: int = 300
    twelvedata_api_usage_max_interval_sec: int = 3600  # hard floor between /api_usage calls

    # --- Readiness / session gating (Phase 38-40) ---
    realmarket_pause_on_provider_failure: bool = True
    realmarket_stale_after_sec: int = 600

    # --- Real-time worker ---
    real_time_poll_interval_sec: int = 30
    real_time_max_retries: int = 5
    real_time_max_backoff_sec: int = 300

    # --- Data quality gates ---
    quality_min_completeness_pct: float = 80.0
    quality_max_ohlc_violations_pct: float = 5.0
    quality_max_anomaly_pct: float = 5.0
    quality_max_price_jump_pct: float = 50.0
    quality_min_candles_required: int = 10
    quality_gates_enabled: bool = True

    # --- News (optional RSS feeds, comma separated) ---
    news_rss_urls: str = ""

    # --- Misc ---
    env: str = "development"
    debug: bool = False
    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"
    min_agent_sample_size: int = 50

    @property
    def cors_origin_list(self) -> List[str]:
        return _env_list("CORS_ORIGINS", self.cors_origins)

    @property
    def news_rss_list(self) -> List[str]:
        return _env_list("NEWS_RSS_URLS", self.news_rss_urls)

    @property
    def llm_configured(self) -> bool:
        return bool(self.openrouter_api_key.strip())

    @property
    def llm_call_mode_value(self) -> str:
        mode = self.llm_call_mode.strip().upper()
        return mode if mode in {"ALWAYS", "ON_SIGNAL", "ON_CONFLICT", "MANUAL"} else "ON_SIGNAL"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

"""
Application configuration.

Sources of truth:
- `.env` for secrets and runtime flags
- `config/risk.yaml`, `config/instruments.yaml`, `config/strategies.yaml` for tunables

All settings are validated on import. Misconfig => app fails to start (fail-closed).
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import quote_plus

import yaml
from pydantic import BaseModel, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG_DIR = REPO_ROOT / "config"


class AppSettings(BaseSettings):
    """Environment-driven settings (loaded from .env)."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- App ---
    app_env: Literal["development", "staging", "production"] = "development"
    app_log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    app_timezone: str = "Asia/Kolkata"

    # --- Live-trading lock #1 (env flag) ---
    live_trading: bool = False

    # --- Capital ---
    trading_capital_inr: float = Field(default=100_000, gt=0)

    # --- Upstox ---
    upstox_api_key: SecretStr
    upstox_api_secret: SecretStr
    upstox_redirect_uri: str
    upstox_base_url: str = "https://api.upstox.com/v2"
    upstox_ws_url: str = "wss://api.upstox.com/v2/feed/market-data-feed"

    # --- AI advisor backend ---
    # "anthropic" → direct Anthropic API (uses ANTHROPIC_API_KEY)
    # "bedrock"   → AWS Bedrock (uses IAM role on EC2, or AWS_ACCESS_KEY_ID/SECRET locally)
    advisor_backend: Literal["anthropic", "bedrock"] = "anthropic"

    # --- Anthropic (direct API) ---
    anthropic_api_key: SecretStr
    anthropic_model: str = "claude-opus-4-7"

    # --- AWS Bedrock (only used when advisor_backend == "bedrock") ---
    aws_region: str = "ap-south-1"
    # Claude Haiku 4.5 on Bedrock is only invocable via inference profile, not
    # the foundation model ID. As of 2026-05, the available profile for Haiku 4.5
    # in Mumbai is `global.anthropic.claude-haiku-4-5-20251001-v1:0`.
    # Verify available profiles in your account via:
    #   aws bedrock list-inference-profiles --region ap-south-1
    bedrock_model_id: str = "global.anthropic.claude-haiku-4-5-20251001-v1:0"

    # --- Postgres ---
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "trading_agent"
    postgres_user: str = "trading_agent"
    postgres_password: SecretStr

    # --- Redis ---
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_password: SecretStr | None = None
    redis_db: int = 0

    # --- Token encryption ---
    token_encryption_key: SecretStr

    # --- Alerts ---
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None
    discord_webhook_url: SecretStr | None = None

    # --- Dashboard HTTP Basic Auth (for publicly-tunneled dashboards) ---
    # When both are set, all /dashboard and /control routes require these credentials.
    # When unset/empty, no auth is required (dev mode, local-only access).
    dashboard_username: str = ""
    dashboard_password: SecretStr | None = None

    @property
    def database_url(self) -> str:
        pw = quote_plus(self.postgres_password.get_secret_value())
        user = quote_plus(self.postgres_user)
        return (
            f"postgresql+asyncpg://{user}:{pw}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def database_url_sync(self) -> str:
        """Sync URL for Alembic. Uses psycopg v3 (has Python 3.14 wheels)."""
        pw = quote_plus(self.postgres_password.get_secret_value())
        user = quote_plus(self.postgres_user)
        return (
            f"postgresql+psycopg://{user}:{pw}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def redis_url(self) -> str:
        auth = ""
        if self.redis_password is not None:
            auth = f":{self.redis_password.get_secret_value()}@"
        return f"redis://{auth}{self.redis_host}:{self.redis_port}/{self.redis_db}"

    @field_validator("upstox_redirect_uri")
    @classmethod
    def _validate_redirect(cls, v: str) -> str:
        if not v.startswith(("http://", "https://")):
            raise ValueError("UPSTOX_REDIRECT_URI must be a URL")
        return v


# ---------------- YAML-backed configs ----------------

class RiskConfig(BaseModel):
    """config/risk.yaml — capital risk caps. All percentages are fractions of TRADING_CAPITAL_INR."""

    # --- Loss caps ---
    daily_max_loss_pct: float = Field(gt=0, lt=1)
    rolling_drawdown_pct: float = Field(gt=0, lt=1)
    rolling_drawdown_window_days: int = Field(gt=0)
    per_trade_max_risk_pct: float = Field(gt=0, lt=1)
    max_concurrent_positions: int = Field(gt=0)
    max_trades_per_day: int = Field(gt=0)
    consecutive_loss_lockout: int = Field(gt=0)

    # --- Slippage kill ---
    slippage_kill_threshold_bps: float = Field(gt=0)
    slippage_kill_consecutive: int = Field(gt=0)

    # --- Liquidity / spread ---
    max_spread_bps: float = Field(gt=0)
    min_liquidity_score: float = Field(ge=0, le=1)
    min_top5_depth_lots: int = Field(gt=0)
    min_strike_oi: int = Field(gt=0)

    # --- Anti-FOMO ---
    max_atr_consumed_at_entry: float = Field(gt=0, le=1)
    max_vwap_deviation_sigma: float = Field(gt=0)
    max_consecutive_signal_candles: int = Field(gt=0)
    opportunity_ttl_seconds: int = Field(gt=0)
    cooldown_after_fast_mover_sec: int = Field(ge=0)
    fast_mover_atr_multiple: float = Field(gt=0)
    no_reentry_same_direction_after_stop: bool

    # --- Volatility kill ---
    india_vix_ceiling: float = Field(gt=0)
    intraday_move_ceiling_pct: float = Field(gt=0, lt=1)

    # --- Data freshness ---
    stale_tick_max_age_sec: float = Field(gt=0)
    broker_health_max_age_sec: float = Field(gt=0)

    # --- Time windows ---
    entry_window_start: str
    entry_window_end: str
    forced_exit_time: str

    # --- Execution / slippage discipline ---
    max_estimated_slippage_bps: float = Field(gt=0)
    fill_timeout_ms: int = Field(gt=0)
    fill_improve_max_ticks: int = Field(ge=0)
    fill_improve_step_ticks: int = Field(gt=0)
    allow_market_orders_for_entries: bool
    require_pullback_for_trend_entries: bool

    # --- Trailing stop / profit-taking (Phase 4 Position Manager) ---
    initial_stop_atr_multiple: float = Field(gt=0)
    breakeven_trigger_r_multiple: float = Field(gt=0)
    trail_method: Literal["atr_chandelier", "structure_swing", "ema_trail"]
    trail_atr_period: int = Field(gt=0)
    trail_atr_multiple: float = Field(gt=0)
    trail_only_in_profit: bool
    partial_profit_r_multiple: float = Field(gt=0)
    partial_profit_exit_fraction: float = Field(gt=0, lt=1)
    runner_giveback_atr_multiple: float = Field(gt=0)


class InstrumentConfig(BaseModel):
    name: str
    upstox_instrument_key: str
    exchange: Literal["NSE_INDEX", "BSE_INDEX"]
    lot_size: int = Field(gt=0)
    tick_size: float = Field(gt=0)
    expiry_weekday: int = Field(ge=0, le=6)  # Mon=0
    enabled: bool = True


class InstrumentsConfig(BaseModel):
    instruments: list[InstrumentConfig]


class StrategiesConfig(BaseModel):
    momentum_breakout_enabled: bool = True
    trend_continuation_enabled: bool = True
    volatility_expansion_enabled: bool = True
    gap_continuation_enabled: bool = True
    event_driven_enabled: bool = False  # Phase 4+


def _load_yaml(filename: str) -> dict:
    path = CONFIG_DIR / filename
    if not path.exists():
        raise FileNotFoundError(f"Required config file missing: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


@lru_cache(maxsize=1)
def get_settings() -> AppSettings:
    return AppSettings()  # type: ignore[call-arg]


@lru_cache(maxsize=1)
def get_risk_config() -> RiskConfig:
    return RiskConfig.model_validate(_load_yaml("risk.yaml"))


@lru_cache(maxsize=1)
def get_instruments_config() -> InstrumentsConfig:
    return InstrumentsConfig.model_validate(_load_yaml("instruments.yaml"))


@lru_cache(maxsize=1)
def get_strategies_config() -> StrategiesConfig:
    return StrategiesConfig.model_validate(_load_yaml("strategies.yaml"))

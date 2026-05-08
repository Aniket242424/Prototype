"""DTOs for Phase 2 engines. Match the SQLAlchemy schemas in models.py."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from trading_agent.core.constants import Direction, Regime


class IndicatorSnapshot(BaseModel):
    """Cached indicator values used by both Regime and Opportunity."""
    model_config = ConfigDict(frozen=True)

    underlying: str
    ts: datetime

    # Trend
    ema9: float | None = None
    ema21: float | None = None
    ema50: float | None = None
    vwap: float | None = None
    price_vwap_dev_sigma: float | None = None  # how many sigmas above/below VWAP

    # Volatility
    atr14: float | None = None       # absolute, in price units
    atr_pct: float | None = None     # ATR as % of spot
    rv5: float | None = None         # realized vol over last 5 min (annualized %)
    rv15: float | None = None
    rv60: float | None = None

    # Trend strength
    adx14: float | None = None
    plus_di: float | None = None
    minus_di: float | None = None

    # Persistence
    consec_up_candles: int = 0
    consec_down_candles: int = 0
    candles_in_buffer: int = 0


class RegimeState(BaseModel):
    """One regime classification result."""
    model_config = ConfigDict(frozen=True)

    underlying: str
    regime: Regime
    confidence: float = Field(ge=0, le=1)
    components: dict[str, float | str | None]
    ts: datetime


class OIBuildup(BaseModel):
    """OI buildup classification on one strike side."""
    model_config = ConfigDict(frozen=True)

    label: str          # "long_buildup" | "short_buildup" | "short_covering" | "long_unwinding" | "neutral"
    price_change_pct: float
    oi_change_pct: float


class OptionsIntel(BaseModel):
    """Per-underlying derived intel from latest chain snapshot."""
    model_config = ConfigDict(frozen=True)

    underlying: str
    ts: datetime
    expiry: date
    spot: float
    atm_strike: float

    # IV
    atm_call_iv: float | None = None
    atm_put_iv: float | None = None
    iv_rank_30d: float | None = None     # 0..1
    iv_percentile_30d: float | None = None  # 0..1
    iv_skew: float | None = None          # put_iv - call_iv at ATM

    # OI
    total_call_oi: int = 0
    total_put_oi: int = 0
    pcr_oi: float | None = None
    pcr_volume: float | None = None
    max_pain_strike: float | None = None

    # OI buildup vs previous snapshot
    atm_call_buildup: OIBuildup | None = None
    atm_put_buildup: OIBuildup | None = None

    # Liquidity / spread on ATM strikes (bps of mid)
    atm_call_spread_bps: float | None = None
    atm_put_spread_bps: float | None = None

    # Gamma exposure
    total_gamma_exposure: float | None = None  # rough proxy: sum(|gamma| × OI)


class OpportunityScore(BaseModel):
    """Per-dimension breakdown of opportunity score."""
    model_config = ConfigDict(frozen=True)

    momentum_quality: float = 0.0
    vol_expansion_prob: float = 0.0
    liquidity_quality: float = 0.0
    spread_tightness: float = 0.0
    slippage_risk: float = 0.0           # 1 = best, 0 = worst (inverse)
    iv_conditions: float = 0.0
    regime_favorability: float = 0.0
    trend_quality: float = 0.0
    risk_reward_profile: float = 0.0


class Opportunity(BaseModel):
    """A ranked tradeable opportunity. At most one is 'active' at a time."""
    model_config = ConfigDict(frozen=True)

    underlying: str
    direction: Direction
    score: float = Field(ge=0, le=1)
    components: OpportunityScore
    recommended_expiry: date
    recommended_strike_band: dict[str, Decimal]  # {"low": ..., "high": ...}
    ts: datetime

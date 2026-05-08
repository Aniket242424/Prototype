"""Test the opportunity scorer with synthetic regime + intel inputs."""
from __future__ import annotations

from datetime import date, datetime, timezone

from trading_agent.core.constants import Direction, Regime
from trading_agent.regime.dtos import (
    IndicatorSnapshot,
    OptionsIntel,
    RegimeState,
)
from trading_agent.regime.opportunity import composite_score, score_opportunity


def _ind(**kw) -> IndicatorSnapshot:
    base = dict(
        underlying="NIFTY",
        ts=datetime(2026, 5, 12, 10, 0, tzinfo=timezone.utc),
        candles_in_buffer=60,
        atr14=50.0, atr_pct=0.4,
        adx14=28, plus_di=25, minus_di=14,
        rv5=22, rv15=20, rv60=18,
        price_vwap_dev_sigma=1.5,
        consec_up_candles=3, consec_down_candles=0,
        ema9=24050, ema21=24000, ema50=23950, vwap=24000,
    )
    base.update(kw)
    return IndicatorSnapshot(**base)


def _regime(**kw) -> RegimeState:
    base = dict(
        underlying="NIFTY",
        regime=Regime.TREND_UP,
        confidence=0.7,
        components={},
        ts=datetime(2026, 5, 12, 10, 0, tzinfo=timezone.utc),
    )
    base.update(kw)
    return RegimeState(**base)


def _intel(**kw) -> OptionsIntel:
    base = dict(
        underlying="NIFTY",
        ts=datetime(2026, 5, 12, 10, 0, tzinfo=timezone.utc),
        expiry=date(2026, 5, 12),
        spot=24100.0,
        atm_strike=24100.0,
        atm_call_iv=14.0,
        atm_put_iv=14.5,
        iv_rank_30d=0.2,
        iv_percentile_30d=0.2,
        total_call_oi=2_500_000,
        total_put_oi=2_800_000,
        pcr_oi=1.12,
        pcr_volume=0.95,
        max_pain_strike=24050.0,
        atm_call_spread_bps=8.0,
        atm_put_spread_bps=10.0,
        total_gamma_exposure=1_500_000.0,
    )
    base.update(kw)
    return OptionsIntel(**base)


def test_strong_uptrend_scores_high():
    s, direction = score_opportunity(_ind(), _regime(), _intel())
    cs = composite_score(s)
    assert direction == Direction.LONG
    assert cs > 0.6
    assert s.regime_favorability >= 0.55


def test_choppy_regime_kills_score():
    s, _ = score_opportunity(_ind(), _regime(regime=Regime.CHOPPY, confidence=0.55), _intel())
    cs = composite_score(s)
    assert s.regime_favorability == 0.0
    assert cs < 0.6


def test_high_iv_pct_lowers_iv_score():
    intel_high_iv = _intel(iv_percentile_30d=0.85)
    s, _ = score_opportunity(_ind(), _regime(), intel_high_iv)
    assert s.iv_conditions < 0.3


def test_wide_spread_kills_liquidity():
    intel_wide = _intel(atm_call_spread_bps=80.0, atm_put_spread_bps=80.0)
    s, _ = score_opportunity(_ind(), _regime(), intel_wide)
    assert s.spread_tightness == 0.0
    assert s.slippage_risk == 0.0


def test_direction_from_minus_di_when_no_clear_trend():
    ind = _ind(plus_di=10, minus_di=25)
    s, direction = score_opportunity(ind, _regime(regime=Regime.RANGE, confidence=0.5), _intel())
    assert direction == Direction.SHORT

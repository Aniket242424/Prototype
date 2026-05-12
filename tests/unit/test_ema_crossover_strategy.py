"""
Tests for EMA Crossover Trend strategy.

Each test builds a StrategyContext with synthetic indicators that pass all
filters EXCEPT the one being tested. Then asserts that the targeted filter
rejects (via observable: evaluate returns None, and we can inspect logs).

The strategy is pure-functional — no DB, no Redis, no I/O. These tests
are fully isolated.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from trading_agent.core.constants import Direction, OptionType, Regime
from trading_agent.regime.dtos import (
    IndicatorSnapshot,
    Opportunity,
    OpportunityScore,
    OptionsIntel,
    RegimeState,
)
from trading_agent.strategy.base import StrategyContext, StrategyReject
from trading_agent.strategy.ema_crossover import (
    EMACrossoverConfig,
    EMACrossoverTrendStrategy,
)


# ---------- Fixtures: build a "default happy path" context ----------

def _ind(**kw) -> IndicatorSnapshot:
    base = dict(
        underlying="NIFTY",
        ts=datetime(2026, 5, 12, 11, 0, tzinfo=timezone.utc),
        candles_in_buffer=60,
        # EMA stack — fast above slow for LONG
        ema9=24100.0,
        ema21=24050.0,
        ema50=24000.0,
        # VWAP slightly below price → positive sigma for LONG
        vwap=24080.0,
        price_vwap_dev_sigma=0.8,
        # Volatility
        atr14=80.0,
        atr_pct=0.33,
        rv5=22.0, rv15=20.0, rv60=18.0,
        # Trend strength
        adx14=28.0,
        plus_di=26.0,
        minus_di=12.0,
        # Persistence — 2 candles in trend direction
        consec_up_candles=2,
        consec_down_candles=0,
    )
    base.update(kw)
    return IndicatorSnapshot(**base)


def _regime(**kw) -> RegimeState:
    base = dict(
        underlying="NIFTY",
        regime=Regime.TREND_UP,
        confidence=0.7,
        components={},
        ts=datetime(2026, 5, 12, 11, 0, tzinfo=timezone.utc),
    )
    base.update(kw)
    return RegimeState(**base)


def _opportunity(**kw) -> Opportunity:
    base = dict(
        underlying="NIFTY",
        direction=Direction.LONG,
        score=0.72,
        components=OpportunityScore(),
        recommended_expiry=date(2026, 5, 19),
        recommended_strike_band={"low": Decimal("23900"), "high": Decimal("24100")},
        ts=datetime(2026, 5, 12, 11, 0, tzinfo=timezone.utc),
    )
    base.update(kw)
    return Opportunity(**base)


def _intel(**kw) -> OptionsIntel:
    base = dict(
        underlying="NIFTY",
        ts=datetime(2026, 5, 12, 11, 0, tzinfo=timezone.utc),
        expiry=date(2026, 5, 19),
        spot=24120.0,
        atm_strike=24100.0,
        atm_call_iv=14.0,
        atm_put_iv=14.5,
        iv_rank_30d=0.4,
        iv_percentile_30d=0.4,
    )
    base.update(kw)
    return OptionsIntel(**base)


def _ctx(direction=Direction.LONG, **overrides) -> StrategyContext:
    """Build a 'happy path' context. Pass kwargs to override specific fields."""
    return StrategyContext(
        underlying="NIFTY",
        direction=direction,
        opportunity=overrides.get("opportunity", _opportunity(direction=direction)),
        regime=overrides.get("regime", _regime()),
        indicators=overrides.get("indicators", _ind()),
        intel=overrides.get("intel", _intel()),
        ts=datetime(2026, 5, 12, 11, 0, tzinfo=timezone.utc),
    )


# ---------- Tests ----------

def test_default_happy_path_long_produces_signal():
    s = EMACrossoverTrendStrategy()
    signal = s.evaluate(_ctx())
    assert signal is not None
    assert signal.strategy_name == "ema_crossover_trend"
    assert signal.direction == Direction.LONG
    assert signal.option_type == OptionType.CE
    assert signal.stop_underlying < Decimal(str(_intel().spot))
    assert signal.target_underlying > Decimal(str(_intel().spot))
    assert 0 < signal.confidence <= 1
    assert "trigger" in signal.rationale


def test_default_happy_path_short_produces_pe_signal():
    short_ind = _ind(
        ema9=24050.0, ema21=24100.0,        # fast below slow
        price_vwap_dev_sigma=-0.8,
        vwap=24120.0,
        consec_up_candles=0,
        consec_down_candles=2,
        plus_di=12.0, minus_di=26.0,
    )
    short_regime = _regime(regime=Regime.TREND_DOWN)
    short_opp = _opportunity(direction=Direction.SHORT)
    short_intel = _intel(spot=24080.0)

    ctx = _ctx(
        direction=Direction.SHORT,
        indicators=short_ind,
        regime=short_regime,
        opportunity=short_opp,
        intel=short_intel,
    )
    signal = EMACrossoverTrendStrategy().evaluate(ctx)
    assert signal is not None
    assert signal.direction == Direction.SHORT
    assert signal.option_type == OptionType.PE
    assert signal.stop_underlying > Decimal(str(short_intel.spot))
    assert signal.target_underlying < Decimal(str(short_intel.spot))


def test_rejects_when_indicators_not_ready():
    bad = _ind(ema9=None, ema21=None, adx14=None, candles_in_buffer=4)
    assert EMACrossoverTrendStrategy().evaluate(_ctx(indicators=bad)) is None


def test_rejects_when_ema_stack_wrong_for_long():
    bad = _ind(ema9=24050.0, ema21=24100.0)   # fast BELOW slow but direction LONG
    assert EMACrossoverTrendStrategy().evaluate(_ctx(indicators=bad)) is None


def test_rejects_when_regime_choppy():
    bad = _regime(regime=Regime.CHOPPY)
    assert EMACrossoverTrendStrategy().evaluate(_ctx(regime=bad)) is None


def test_rejects_when_regime_trending_wrong_way():
    bad = _regime(regime=Regime.TREND_DOWN)
    assert EMACrossoverTrendStrategy().evaluate(_ctx(regime=bad)) is None


def test_rejects_when_adx_too_low():
    bad = _ind(adx14=18.0)
    assert EMACrossoverTrendStrategy().evaluate(_ctx(indicators=bad)) is None


def test_rejects_when_persistence_too_low():
    bad = _ind(consec_up_candles=1)   # need >= 2
    assert EMACrossoverTrendStrategy().evaluate(_ctx(indicators=bad)) is None


def test_rejects_when_persistence_too_high_no_chase():
    bad = _ind(consec_up_candles=6)   # mature leg, anti-FOMO
    assert EMACrossoverTrendStrategy().evaluate(_ctx(indicators=bad)) is None


def test_rejects_when_no_pullback_extended_from_ema():
    # spot is far above ema9 → no pullback → reject
    far_intel = _intel(spot=24300.0)   # way above ema9=24100
    assert EMACrossoverTrendStrategy().evaluate(_ctx(intel=far_intel)) is None


def test_rejects_when_vwap_against_direction():
    bad = _ind(price_vwap_dev_sigma=-0.5)   # LONG needs +sigma
    assert EMACrossoverTrendStrategy().evaluate(_ctx(indicators=bad)) is None


def test_rejects_when_vwap_extended_beyond_15_sigma():
    bad = _ind(price_vwap_dev_sigma=2.5)
    assert EMACrossoverTrendStrategy().evaluate(_ctx(indicators=bad)) is None


def test_configurable_periods_work_with_alternate_values():
    cfg = EMACrossoverConfig(fast_period=10, slow_period=20, min_adx14=20.0)
    s = EMACrossoverTrendStrategy(cfg)
    signal = s.evaluate(_ctx())
    assert signal is not None
    assert signal.rationale["fast_period"] == 10
    assert signal.rationale["slow_period"] == 20


def test_pullback_check_can_be_disabled():
    cfg = EMACrossoverConfig(require_pullback=False)
    # With extended spot, default config rejects; disabled should pass
    far_intel = _intel(spot=24300.0)
    s_default = EMACrossoverTrendStrategy()
    assert s_default.evaluate(_ctx(intel=far_intel)) is None
    s_no_pb = EMACrossoverTrendStrategy(cfg)
    assert s_no_pb.evaluate(_ctx(intel=far_intel)) is not None


def test_confidence_is_product_of_inputs():
    s = EMACrossoverTrendStrategy()
    # Default config has base_confidence=0.65, regime=0.7, opportunity=0.72
    # Expected: 0.65 × 0.7 × 0.72 ≈ 0.328
    signal = s.evaluate(_ctx())
    assert signal is not None
    expected = 0.65 * 0.7 * 0.72
    assert abs(signal.confidence - expected) < 0.01


def test_invalidation_when_regime_flips():
    s = EMACrossoverTrendStrategy()
    signal = s.evaluate(_ctx())
    assert signal is not None
    # Now flip the regime
    flipped_ctx = _ctx(regime=_regime(regime=Regime.TREND_DOWN, confidence=0.8))
    reason = s.invalidation(signal, flipped_ctx)
    assert reason is not None
    assert "regime" in reason.lower()


def test_invalidation_when_spot_crosses_back_through_slow_ema():
    s = EMACrossoverTrendStrategy()
    signal = s.evaluate(_ctx())
    assert signal is not None
    # Spot drops below ema21=24050
    below_intel = _intel(spot=24000.0)
    bad_ctx = _ctx(intel=below_intel)
    reason = s.invalidation(signal, bad_ctx)
    assert reason is not None
    assert "below" in reason.lower() or "ema" in reason.lower()


def test_invalidation_returns_none_when_still_valid():
    s = EMACrossoverTrendStrategy()
    signal = s.evaluate(_ctx())
    assert signal is not None
    # Same context — should still be valid
    reason = s.invalidation(signal, _ctx())
    assert reason is None


def test_strategy_reject_exception_carries_code():
    err = StrategyReject("TEST_CODE", "test reason")
    assert err.code == "TEST_CODE"
    assert err.reason == "test reason"
    assert "test reason" in str(err)

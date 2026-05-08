"""Test the regime classifier rules in isolation (no DB, no Redis)."""
from __future__ import annotations

from datetime import datetime, timezone

from trading_agent.core.constants import Regime
from trading_agent.regime.dtos import IndicatorSnapshot
from trading_agent.regime.regime_engine import classify_regime


def _ind(**kwargs) -> IndicatorSnapshot:
    base = dict(
        underlying="NIFTY",
        ts=datetime(2026, 5, 12, 10, 0, tzinfo=timezone.utc),
        candles_in_buffer=60,
    )
    base.update(kwargs)
    return IndicatorSnapshot(**base)


def test_insufficient_candles_yields_choppy():
    ind = _ind(candles_in_buffer=5)
    rs = classify_regime(ind, vix_value=15.0)
    assert rs.regime == Regime.CHOPPY
    assert rs.confidence < 0.5


def test_event_driven_when_vix_panicked():
    ind = _ind(adx14=18.0, plus_di=10, minus_di=8)
    rs = classify_regime(ind, vix_value=28.0)
    assert rs.regime == Regime.EVENT_DRIVEN
    assert rs.confidence > 0.5


def test_vol_expansion_when_rv_ratio_high():
    ind = _ind(rv5=40.0, rv60=20.0, adx14=18, plus_di=15, minus_di=12)
    rs = classify_regime(ind, vix_value=15.0)
    assert rs.regime == Regime.VOL_EXPANSION


def test_vol_compression_when_rv_ratio_low():
    ind = _ind(rv5=8.0, rv60=20.0, adx14=18)
    rs = classify_regime(ind, vix_value=15.0)
    assert rs.regime == Regime.VOL_COMPRESSION


def test_trend_up_with_high_adx_and_plus_di():
    ind = _ind(adx14=30.0, plus_di=28, minus_di=12, rv5=18, rv60=18)
    rs = classify_regime(ind, vix_value=15.0)
    assert rs.regime == Regime.TREND_UP


def test_trend_down_with_high_adx_and_minus_di():
    ind = _ind(adx14=30.0, plus_di=12, minus_di=28, rv5=18, rv60=18)
    rs = classify_regime(ind, vix_value=15.0)
    assert rs.regime == Regime.TREND_DOWN


def test_choppy_when_low_adx():
    ind = _ind(adx14=10.0, plus_di=12, minus_di=11, rv5=18, rv60=18)
    rs = classify_regime(ind, vix_value=15.0)
    assert rs.regime == Regime.CHOPPY


def test_range_when_neutral_adx():
    ind = _ind(adx14=18.0, plus_di=15, minus_di=14, rv5=18, rv60=18)
    rs = classify_regime(ind, vix_value=15.0)
    # Not high-enough ADX for trend, not low enough for choppy → RANGE
    assert rs.regime == Regime.RANGE

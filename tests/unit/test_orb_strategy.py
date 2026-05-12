"""
Tests for Opening Range Breakout (ORB) strategy.

Each test builds a happy-path StrategyContext, then mutates one field to
trigger the targeted filter rejection. Asserts that evaluate() returns
None when a filter rejects, and a StrategySignal when all pass.
"""
from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

from trading_agent.core.constants import Direction, OptionType, Regime
from trading_agent.core.time_utils import IST
from trading_agent.regime.dtos import (
    IndicatorSnapshot,
    Opportunity,
    OpportunityScore,
    OptionsIntel,
    RegimeState,
)
from trading_agent.strategy.base import StrategyContext
from trading_agent.strategy.orb import ORBConfig, ORBStrategy


# ---------- Fixtures: build a "default happy path" context ----------

def _make_ts(hour: int, minute: int) -> datetime:
    """Build a market-hours IST datetime."""
    return datetime(2026, 5, 12, hour, minute, tzinfo=IST)


def _ind(**kw) -> IndicatorSnapshot:
    base = dict(
        underlying="NIFTY",
        ts=_make_ts(9, 45),
        candles_in_buffer=30,
        ema9=24100.0,
        ema21=24050.0,
        ema50=24000.0,
        vwap=24080.0,
        price_vwap_dev_sigma=0.5,
        atr14=80.0,
        atr_pct=0.33,
        rv5=22.0, rv15=20.0, rv60=18.0,
        adx14=24.0,
        plus_di=24.0,
        minus_di=14.0,
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
        ts=_make_ts(9, 45),
    )
    base.update(kw)
    return RegimeState(**base)


def _opportunity(**kw) -> Opportunity:
    base = dict(
        underlying="NIFTY",
        direction=Direction.LONG,
        score=0.7,
        components=OpportunityScore(),
        recommended_expiry=date(2026, 5, 19),
        recommended_strike_band={"low": Decimal("23900"), "high": Decimal("24200")},
        ts=_make_ts(9, 45),
    )
    base.update(kw)
    return Opportunity(**base)


def _intel(spot: float = 24105.0, **kw) -> OptionsIntel:
    base = dict(
        underlying="NIFTY",
        ts=_make_ts(9, 45),
        expiry=date(2026, 5, 19),
        spot=spot,
        atm_strike=24100.0,
        atm_call_iv=14.0,
        atm_put_iv=14.5,
        iv_rank_30d=0.4,
        iv_percentile_30d=0.4,
    )
    base.update(kw)
    return OptionsIntel(**base)


def _ctx(
    direction: Direction = Direction.LONG,
    ts: datetime = None,
    opening_range_high: float = 24090.0,
    opening_range_low: float = 24010.0,
    opening_range_formed: bool = True,
    volume_ratio: float | None = 1.5,
    intel_spot: float = 24105.0,
    **overrides,
) -> StrategyContext:
    """Happy-path long breakout: spot 24105 above opening range 24010-24090."""
    return StrategyContext(
        underlying="NIFTY",
        direction=direction,
        opportunity=overrides.get("opportunity", _opportunity(direction=direction)),
        regime=overrides.get("regime", _regime()),
        indicators=overrides.get("indicators", _ind()),
        intel=overrides.get("intel", _intel(spot=intel_spot)),
        ts=ts or _make_ts(9, 45),
        opening_range_high=opening_range_high,
        opening_range_low=opening_range_low,
        opening_range_formed=opening_range_formed,
        volume_ratio=volume_ratio,
    )


# ---------- Tests ----------

def test_happy_path_long_breakout_produces_signal():
    s = ORBStrategy()
    signal = s.evaluate(_ctx())
    assert signal is not None
    assert signal.strategy_name == "orb"
    assert signal.direction == Direction.LONG
    assert signal.option_type == OptionType.CE
    # Stop = range low; target = range_high + 1.5×width
    assert signal.stop_underlying == Decimal("24010.0")
    # target = 24090 + 1.5 × 80 = 24210
    assert signal.target_underlying == Decimal("24210.0")
    assert "opening_range_breakout" == signal.rationale["trigger"]


def test_happy_path_short_breakdown_produces_pe_signal():
    s = ORBStrategy()
    # Spot below range low for SHORT breakdown
    ctx = _ctx(
        direction=Direction.SHORT,
        intel_spot=24000.0,
        regime=_regime(regime=Regime.TREND_DOWN),
        opportunity=_opportunity(direction=Direction.SHORT),
    )
    signal = s.evaluate(ctx)
    assert signal is not None
    assert signal.direction == Direction.SHORT
    assert signal.option_type == OptionType.PE
    assert signal.stop_underlying == Decimal("24090.0")
    # target = 24010 - 1.5 × 80 = 23890
    assert signal.target_underlying == Decimal("23890.0")


def test_rejects_when_too_early_before_0930():
    s = ORBStrategy()
    ctx = _ctx(ts=_make_ts(9, 25))
    assert s.evaluate(ctx) is None


def test_rejects_when_too_late_after_1030():
    s = ORBStrategy()
    ctx = _ctx(ts=_make_ts(11, 0))
    assert s.evaluate(ctx) is None


def test_rejects_when_range_not_formed():
    s = ORBStrategy()
    ctx = _ctx(opening_range_formed=False)
    assert s.evaluate(ctx) is None


def test_rejects_when_range_data_missing():
    s = ORBStrategy()
    ctx = _ctx(opening_range_high=None, opening_range_low=None)
    assert s.evaluate(ctx) is None


def test_rejects_when_range_too_narrow():
    s = ORBStrategy()
    # Range of just 30 pts on 24000 spot = 0.125% < min 0.3%
    ctx = _ctx(opening_range_high=24080.0, opening_range_low=24050.0, intel_spot=24090.0)
    assert s.evaluate(ctx) is None


def test_rejects_when_regime_choppy():
    s = ORBStrategy()
    ctx = _ctx(regime=_regime(regime=Regime.CHOPPY))
    assert s.evaluate(ctx) is None


def test_rejects_when_adx_too_low():
    s = ORBStrategy()
    ctx = _ctx(indicators=_ind(adx14=15.0))
    assert s.evaluate(ctx) is None


def test_rejects_when_spot_inside_range_no_breakout():
    s = ORBStrategy()
    # Spot 24050 is INSIDE 24010-24090 range
    ctx = _ctx(intel_spot=24050.0)
    assert s.evaluate(ctx) is None


def test_rejects_when_spot_touches_but_not_breaks():
    s = ORBStrategy()
    # Spot exactly at range_high — not enough breakout (need + 1 tick)
    ctx = _ctx(intel_spot=24090.0)
    assert s.evaluate(ctx) is None


def test_rejects_when_volume_too_low():
    s = ORBStrategy()
    ctx = _ctx(volume_ratio=0.8)
    assert s.evaluate(ctx) is None


def test_passes_when_volume_unknown_lenient_mode():
    """Index data often lacks volume — strategy should not reject on missing data."""
    s = ORBStrategy()
    ctx = _ctx(volume_ratio=None)
    signal = s.evaluate(ctx)
    assert signal is not None


def test_invalidation_when_spot_reenters_range_long():
    s = ORBStrategy()
    signal = s.evaluate(_ctx())
    assert signal is not None
    # Spot drops back inside range
    bad = _ctx(intel_spot=24050.0)
    reason = s.invalidation(signal, bad)
    assert reason is not None
    assert "re-entered" in reason.lower() or "range" in reason.lower()


def test_invalidation_returns_none_when_still_outside_range():
    s = ORBStrategy()
    signal = s.evaluate(_ctx())
    assert signal is not None
    # Spot still above range
    still_above = _ctx(intel_spot=24150.0)
    assert s.invalidation(signal, still_above) is None


def test_configurable_target_rr():
    cfg = ORBConfig(target_rr=2.0)
    s = ORBStrategy(cfg)
    signal = s.evaluate(_ctx())
    assert signal is not None
    # target = 24090 + 2.0 × 80 = 24250
    assert signal.target_underlying == Decimal("24250.0")


def test_orb_signal_confidence_uses_inputs():
    s = ORBStrategy()
    signal = s.evaluate(_ctx())
    assert signal is not None
    # 0.7 × 0.7 × 0.7 = 0.343
    expected = 0.7 * 0.7 * 0.7
    assert abs(signal.confidence - expected) < 0.01

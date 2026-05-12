"""Tests for Volatility Expansion strategy."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

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
from trading_agent.strategy.vol_expansion import VolExpansionConfig, VolExpansionStrategy


def _make_ts(hour: int, minute: int) -> datetime:
    return datetime(2026, 5, 12, hour, minute, tzinfo=IST)


def _ind(**kw) -> IndicatorSnapshot:
    base = dict(
        underlying="NIFTY",
        ts=_make_ts(11, 0),
        candles_in_buffer=50,
        ema9=24100.0, ema21=24050.0, ema50=24000.0,
        vwap=24080.0,
        price_vwap_dev_sigma=0.6,
        atr14=80.0, atr_pct=0.33,
        # rv ratio: 30/15 = 2.0 → vol expansion
        rv5=30.0, rv15=20.0, rv60=15.0,
        adx14=22.0,
        plus_di=24.0,
        minus_di=12.0,
        consec_up_candles=2,
        consec_down_candles=0,
    )
    base.update(kw)
    return IndicatorSnapshot(**base)


def _regime(**kw) -> RegimeState:
    base = dict(
        underlying="NIFTY",
        regime=Regime.VOL_EXPANSION,
        confidence=0.7,
        components={},
        ts=_make_ts(11, 0),
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
        recommended_strike_band={"low": Decimal("23950"), "high": Decimal("24150")},
        ts=_make_ts(11, 0),
    )
    base.update(kw)
    return Opportunity(**base)


def _intel(spot: float = 24120.0, iv_pct: float = 0.4) -> OptionsIntel:
    return OptionsIntel(
        underlying="NIFTY",
        ts=_make_ts(11, 0),
        expiry=date(2026, 5, 19),
        spot=spot,
        atm_strike=24100.0,
        atm_call_iv=14.0,
        atm_put_iv=14.5,
        iv_rank_30d=iv_pct,
        iv_percentile_30d=iv_pct,
    )


def _ctx(
    direction: Direction = Direction.LONG,
    ts: datetime = None,
    **overrides,
) -> StrategyContext:
    return StrategyContext(
        underlying="NIFTY",
        direction=direction,
        opportunity=overrides.get("opportunity", _opportunity(direction=direction)),
        regime=overrides.get("regime", _regime()),
        indicators=overrides.get("indicators", _ind()),
        intel=overrides.get("intel", _intel()),
        ts=ts or _make_ts(11, 0),
    )


def test_happy_path_long_vol_expansion_signal():
    s = VolExpansionStrategy()
    signal = s.evaluate(_ctx())
    assert signal is not None
    assert signal.direction == Direction.LONG
    assert signal.option_type == OptionType.CE
    assert signal.stop_underlying < Decimal("24120.0")
    assert signal.target_underlying > Decimal("24120.0")
    assert signal.rationale["trigger"] == "vol_expansion"


def test_short_vol_expansion_with_minus_di_dominant():
    s = VolExpansionStrategy()
    ctx = _ctx(
        direction=Direction.SHORT,
        indicators=_ind(plus_di=12.0, minus_di=24.0, price_vwap_dev_sigma=-0.6),
        opportunity=_opportunity(direction=Direction.SHORT),
        intel=_intel(spot=24050.0),
    )
    signal = s.evaluate(ctx)
    assert signal is not None
    assert signal.direction == Direction.SHORT
    assert signal.option_type == OptionType.PE


def test_rejects_when_regime_not_vol_exp():
    s = VolExpansionStrategy()
    assert s.evaluate(_ctx(regime=_regime(regime=Regime.TREND_UP))) is None


def test_rejects_when_rv_ratio_below_threshold():
    s = VolExpansionStrategy()
    bad = _ind(rv5=18.0, rv60=15.0)   # ratio 1.2 < 1.6
    assert s.evaluate(_ctx(indicators=bad)) is None


def test_rejects_when_di_against_direction():
    s = VolExpansionStrategy()
    bad = _ind(plus_di=10.0, minus_di=24.0)   # -DI dominant but direction LONG
    assert s.evaluate(_ctx(indicators=bad)) is None


def test_rejects_when_adx_too_low():
    s = VolExpansionStrategy()
    assert s.evaluate(_ctx(indicators=_ind(adx14=12.0))) is None


def test_rejects_when_iv_percentile_too_high():
    s = VolExpansionStrategy()
    assert s.evaluate(_ctx(intel=_intel(iv_pct=0.85))) is None


def test_lenient_when_iv_data_missing():
    s = VolExpansionStrategy()
    bare_intel = OptionsIntel(
        underlying="NIFTY",
        ts=_make_ts(11, 0),
        expiry=date(2026, 5, 19),
        spot=24120.0,
        atm_strike=24100.0,
        iv_percentile_30d=None,
    )
    signal = s.evaluate(_ctx(intel=bare_intel))
    assert signal is not None   # missing IV → lenient pass


def test_rejects_when_too_early_before_0930():
    s = VolExpansionStrategy()
    assert s.evaluate(_ctx(ts=_make_ts(9, 20))) is None


def test_rejects_when_too_late_after_1400():
    s = VolExpansionStrategy()
    assert s.evaluate(_ctx(ts=_make_ts(14, 15))) is None


def test_rejects_when_vwap_against_direction():
    s = VolExpansionStrategy()
    bad = _ind(price_vwap_dev_sigma=-0.6)   # LONG needs +sigma
    assert s.evaluate(_ctx(indicators=bad)) is None


def test_invalidation_when_regime_normalizes():
    s = VolExpansionStrategy()
    signal = s.evaluate(_ctx())
    assert signal is not None
    after_ctx = _ctx(regime=_regime(regime=Regime.CHOPPY))
    reason = s.invalidation(signal, after_ctx)
    assert reason is not None


def test_invalidation_none_when_still_expanding():
    s = VolExpansionStrategy()
    signal = s.evaluate(_ctx())
    assert signal is not None
    assert s.invalidation(signal, _ctx()) is None


def test_tighter_stop_than_ema_crossover():
    """Vol-exp uses 1.0×ATR; EMA-crossover uses 1.5×ATR. Verify vol-exp is tighter."""
    s = VolExpansionStrategy()
    signal = s.evaluate(_ctx())
    assert signal is not None
    spot = 24120.0
    risk_per_lot = spot - float(signal.stop_underlying)
    # ATR=80; 1.0×ATR=80. Risk should be ≤ 80.
    assert risk_per_lot <= 80 + 1   # allow rounding

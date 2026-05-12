"""Tests for Gap Continuation strategy."""
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
from trading_agent.strategy.gap_continuation import (
    GapContinuationConfig,
    GapContinuationStrategy,
)


def _make_ts(hour: int, minute: int) -> datetime:
    return datetime(2026, 5, 12, hour, minute, tzinfo=IST)


def _ind(**kw) -> IndicatorSnapshot:
    base = dict(
        underlying="NIFTY",
        ts=_make_ts(10, 0),
        candles_in_buffer=40,
        ema9=24150.0, ema21=24100.0, ema50=24050.0,
        vwap=24130.0,
        price_vwap_dev_sigma=0.4,
        atr14=60.0, atr_pct=0.25,
        rv5=20.0, rv15=18.0, rv60=16.0,
        adx14=25.0,
        plus_di=26.0,
        minus_di=12.0,
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
        ts=_make_ts(10, 0),
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
        recommended_strike_band={"low": Decimal("24000"), "high": Decimal("24200")},
        ts=_make_ts(10, 0),
    )
    base.update(kw)
    return Opportunity(**base)


def _intel(spot: float = 24160.0) -> OptionsIntel:
    return OptionsIntel(
        underlying="NIFTY",
        ts=_make_ts(10, 0),
        expiry=date(2026, 5, 19),
        spot=spot,
        atm_strike=24150.0,
        atm_call_iv=14.0,
        atm_put_iv=14.5,
        iv_rank_30d=0.4,
        iv_percentile_30d=0.4,
    )


def _ctx(
    direction: Direction = Direction.LONG,
    ts: datetime = None,
    gap_pct: float | None = 0.8,
    session_open: float | None = 24150.0,
    intel_spot: float = 24160.0,
    **overrides,
) -> StrategyContext:
    """Happy-path: NIFTY gapped up 0.8% (close 23990 → open 24150), spot 24160 (slight pullback)."""
    return StrategyContext(
        underlying="NIFTY",
        direction=direction,
        opportunity=overrides.get("opportunity", _opportunity(direction=direction)),
        regime=overrides.get("regime", _regime()),
        indicators=overrides.get("indicators", _ind()),
        intel=overrides.get("intel", _intel(spot=intel_spot)),
        ts=ts or _make_ts(10, 0),
        gap_pct=gap_pct,
        session_open=session_open,
    )


def test_happy_path_long_gap_up_signal():
    s = GapContinuationStrategy()
    signal = s.evaluate(_ctx())
    assert signal is not None
    assert signal.direction == Direction.LONG
    assert signal.option_type == OptionType.CE
    assert signal.stop_underlying == Decimal("24150.0")    # session_open = stop
    # spot 24160, stop 24150, risk = 10, target = 24160 + 2*10 = 24180
    assert signal.target_underlying == Decimal("24180.0")


def test_happy_path_short_gap_down_signal():
    s = GapContinuationStrategy()
    ctx = _ctx(
        direction=Direction.SHORT,
        gap_pct=-0.8,
        session_open=24050.0,
        intel_spot=24040.0,
        regime=_regime(regime=Regime.TREND_DOWN),
        opportunity=_opportunity(direction=Direction.SHORT),
        indicators=_ind(plus_di=12.0, minus_di=24.0, price_vwap_dev_sigma=-0.4,
                        consec_up_candles=0, consec_down_candles=2),
    )
    signal = s.evaluate(ctx)
    assert signal is not None
    assert signal.direction == Direction.SHORT
    assert signal.option_type == OptionType.PE
    assert signal.stop_underlying == Decimal("24050.0")
    # spot 24040, stop 24050, target = 24040 - 2*10 = 24020
    assert signal.target_underlying == Decimal("24020.0")


def test_rejects_when_gap_data_missing():
    s = GapContinuationStrategy()
    assert s.evaluate(_ctx(gap_pct=None, session_open=None)) is None


def test_rejects_when_gap_too_small():
    s = GapContinuationStrategy()
    assert s.evaluate(_ctx(gap_pct=0.3)) is None    # < 0.5%


def test_rejects_when_direction_mismatches_gap():
    s = GapContinuationStrategy()
    # Direction LONG but gap is DOWN
    assert s.evaluate(_ctx(direction=Direction.LONG, gap_pct=-0.8)) is None


def test_rejects_when_regime_choppy():
    s = GapContinuationStrategy()
    assert s.evaluate(_ctx(regime=_regime(regime=Regime.CHOPPY))) is None


def test_rejects_when_adx_too_low():
    s = GapContinuationStrategy()
    assert s.evaluate(_ctx(indicators=_ind(adx14=15.0))) is None


def test_rejects_when_gap_filled_long():
    s = GapContinuationStrategy()
    # spot dropped below session_open → gap filled
    assert s.evaluate(_ctx(intel_spot=24140.0)) is None


def test_rejects_when_chasing_far_from_session_open():
    s = GapContinuationStrategy()
    # spot 24300 vs session_open 24150 = 150 pts away.
    # ATR=60; max distance = 60 → 150 > 60 → reject
    assert s.evaluate(_ctx(intel_spot=24300.0)) is None


def test_rejects_when_too_early():
    s = GapContinuationStrategy()
    assert s.evaluate(_ctx(ts=_make_ts(9, 20))) is None


def test_rejects_when_too_late_after_noon():
    s = GapContinuationStrategy()
    assert s.evaluate(_ctx(ts=_make_ts(12, 30))) is None


def test_invalidation_when_gap_fills_after_entry():
    s = GapContinuationStrategy()
    signal = s.evaluate(_ctx())
    assert signal is not None
    # spot drops to session_open level → gap fill
    after_ctx = _ctx(intel_spot=24145.0)
    reason = s.invalidation(signal, after_ctx)
    assert reason is not None
    assert "gap fill" in reason.lower()


def test_invalidation_none_when_gap_still_holds():
    s = GapContinuationStrategy()
    signal = s.evaluate(_ctx())
    assert signal is not None
    assert s.invalidation(signal, _ctx()) is None


def test_stop_is_session_open():
    """Stop should always be at session_open (the gap fill level)."""
    s = GapContinuationStrategy()
    signal = s.evaluate(_ctx())
    assert signal is not None
    assert signal.stop_underlying == Decimal("24150.0")

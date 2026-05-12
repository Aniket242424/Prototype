"""Tests for the pure-functional position management rules."""
from __future__ import annotations

from datetime import datetime, time
from decimal import Decimal

from trading_agent.core.constants import Direction
from trading_agent.core.time_utils import IST
from trading_agent.position.dtos import (
    ExitTrigger,
    PositionLifecycleStage,
    PositionState,
)
from trading_agent.position.rules import (
    check_current_stop,
    check_forced_time_exit,
    check_runner_giveback,
    check_stock_no_overnight,
    check_target,
    new_chandelier_stop,
    should_move_to_breakeven,
    should_take_partial,
    unrealized_r_multiple,
    update_peak,
)


def _pos(direction: Direction = Direction.LONG, **kw) -> PositionState:
    """Build a default LONG position: entry 24000, stop 23950, target 24100, ATR 60."""
    base = dict(
        db_id=1,
        instrument_key="NSE_FO|TEST",
        underlying="NIFTY",
        strategy_name="ema_crossover_trend",
        direction=direction,
        qty_initial=25,
        qty_remaining=25,
        avg_entry_premium=Decimal("100"),
        entry_underlying=Decimal("24000"),
        initial_stop_underlying=Decimal("23950") if direction == Direction.LONG else Decimal("24050"),
        target_underlying=Decimal("24100") if direction == Direction.LONG else Decimal("23900"),
        stage=PositionLifecycleStage.HARD_STOP,
        current_stop_underlying=Decimal("23950") if direction == Direction.LONG else Decimal("24050"),
        peak_underlying=Decimal("24000"),
        atr_at_entry=60.0,
        opened_at=datetime(2026, 5, 12, 11, 0, tzinfo=IST),
        breakeven_trigger_r_multiple=1.0,
        partial_profit_r_multiple=1.5,
        partial_profit_exit_fraction=0.5,
        trail_atr_multiple=2.0,
        runner_giveback_atr_multiple=1.0,
    )
    base.update(kw)
    return PositionState(**base)


# ============================================================
# unrealized_r_multiple
# ============================================================

def test_r_multiple_long_breakeven():
    p = _pos()
    assert unrealized_r_multiple(p, Decimal("24000")) == 0.0


def test_r_multiple_long_at_target_is_2():
    p = _pos()
    # entry 24000, stop 23950 → R=50; target 24100 → +100 = 2R
    assert unrealized_r_multiple(p, Decimal("24100")) == 2.0


def test_r_multiple_short_inverts_correctly():
    p = _pos(direction=Direction.SHORT)
    # entry 24000, stop 24050 → R=50; spot drops to 23900 → +100 = +2R for short
    assert unrealized_r_multiple(p, Decimal("23900")) == 2.0


# ============================================================
# check_current_stop
# ============================================================

def test_long_current_stop_hit_at_or_below_stop():
    p = _pos()
    result = check_current_stop(p, Decimal("23950"))
    assert result is not None
    assert result[0] == ExitTrigger.HARD_STOP_HIT


def test_long_no_stop_hit_above_stop():
    p = _pos()
    assert check_current_stop(p, Decimal("24010")) is None


def test_short_current_stop_hit_at_or_above_stop():
    p = _pos(direction=Direction.SHORT)
    result = check_current_stop(p, Decimal("24050"))
    assert result is not None
    assert result[0] == ExitTrigger.HARD_STOP_HIT


def test_stop_trigger_changes_with_lifecycle_stage_BE():
    p = _pos(stage=PositionLifecycleStage.BREAKEVEN)
    result = check_current_stop(p, Decimal("23900"))
    assert result is not None
    assert result[0] == ExitTrigger.BREAKEVEN_STOP_HIT


def test_stop_trigger_in_trail_stage_is_chandelier():
    p = _pos(stage=PositionLifecycleStage.PARTIAL_AND_TRAIL)
    result = check_current_stop(p, Decimal("23800"))
    assert result is not None
    assert result[0] == ExitTrigger.CHANDELIER_TRAIL_HIT


# ============================================================
# check_target
# ============================================================

def test_target_hit_long():
    p = _pos()
    result = check_target(p, Decimal("24100"))
    assert result is not None
    assert result[0] == ExitTrigger.TARGET_HIT


def test_target_hit_short():
    p = _pos(direction=Direction.SHORT)
    result = check_target(p, Decimal("23900"))
    assert result is not None
    assert result[0] == ExitTrigger.TARGET_HIT


def test_target_not_checked_in_partial_trail_stage():
    """In trail stage, chandelier is the active rule, not the original target."""
    p = _pos(stage=PositionLifecycleStage.PARTIAL_AND_TRAIL)
    assert check_target(p, Decimal("24200")) is None


# ============================================================
# check_runner_giveback
# ============================================================

def test_giveback_only_applies_in_trail_stage():
    p = _pos(stage=PositionLifecycleStage.BREAKEVEN, peak_underlying=Decimal("24200"))
    # Way below peak, but not in trail stage → no giveback rule
    assert check_runner_giveback(p, Decimal("24100")) is None


def test_giveback_triggers_when_below_peak_minus_atr():
    p = _pos(
        stage=PositionLifecycleStage.PARTIAL_AND_TRAIL,
        peak_underlying=Decimal("24200"),
    )
    # ATR 60, giveback multiple 1.0 → threshold 24200 - 60 = 24140
    result = check_runner_giveback(p, Decimal("24135"))
    assert result is not None
    assert result[0] == ExitTrigger.RUNNER_GIVEBACK


def test_giveback_does_not_trigger_within_atr_of_peak():
    p = _pos(
        stage=PositionLifecycleStage.PARTIAL_AND_TRAIL,
        peak_underlying=Decimal("24200"),
    )
    assert check_runner_giveback(p, Decimal("24150")) is None


# ============================================================
# Stage transitions
# ============================================================

def test_breakeven_trigger_at_1R():
    p = _pos()
    # +1R = 24050 (entry 24000, R=50)
    assert should_move_to_breakeven(p, Decimal("24050")) is True


def test_breakeven_not_triggered_below_1R():
    p = _pos()
    assert should_move_to_breakeven(p, Decimal("24040")) is False


def test_breakeven_only_triggers_in_hard_stop_stage():
    p = _pos(stage=PositionLifecycleStage.BREAKEVEN)
    assert should_move_to_breakeven(p, Decimal("24200")) is False


def test_partial_trigger_at_15R():
    p = _pos(stage=PositionLifecycleStage.BREAKEVEN)
    # +1.5R = 24075 (entry 24000, R=50)
    assert should_take_partial(p, Decimal("24075")) is True


def test_partial_not_triggered_below_15R():
    p = _pos(stage=PositionLifecycleStage.BREAKEVEN)
    assert should_take_partial(p, Decimal("24070")) is False


def test_partial_only_triggers_in_breakeven_stage():
    """If we're still in HARD_STOP, partial doesn't fire."""
    p = _pos(stage=PositionLifecycleStage.HARD_STOP)
    assert should_take_partial(p, Decimal("24200")) is False


# ============================================================
# Chandelier stop
# ============================================================

def test_chandelier_long_below_peak_by_2_atr():
    p = _pos(
        direction=Direction.LONG,
        peak_underlying=Decimal("24200"),
    )
    # ATR 60, multiplier 2.0 → 2×60 = 120 below peak → 24080
    assert new_chandelier_stop(p, Decimal("24200")) == Decimal("24080.0")


def test_chandelier_short_above_peak_by_2_atr():
    p = _pos(
        direction=Direction.SHORT,
        peak_underlying=Decimal("23800"),
    )
    assert new_chandelier_stop(p, Decimal("23800")) == Decimal("23920.0")


# ============================================================
# update_peak
# ============================================================

def test_peak_ratchets_up_for_long():
    p = _pos(peak_underlying=Decimal("24200"))
    assert update_peak(p, Decimal("24250")) == Decimal("24250")
    assert update_peak(p, Decimal("24100")) == Decimal("24200")   # doesn't go back


def test_peak_ratchets_down_for_short():
    p = _pos(direction=Direction.SHORT, peak_underlying=Decimal("23800"))
    assert update_peak(p, Decimal("23750")) == Decimal("23750")
    assert update_peak(p, Decimal("23900")) == Decimal("23800")


# ============================================================
# Time-based exits
# ============================================================

def test_forced_time_exit_after_cutoff():
    ts = datetime(2026, 5, 12, 15, 16, tzinfo=IST)
    result = check_forced_time_exit(ts, "15:15")
    assert result is not None
    assert result[0] == ExitTrigger.FORCED_TIME_EXIT


def test_forced_time_exit_before_cutoff():
    ts = datetime(2026, 5, 12, 14, 30, tzinfo=IST)
    assert check_forced_time_exit(ts, "15:15") is None


def test_stock_no_overnight_after_close():
    p = _pos(is_stock_option=True)
    ts = datetime(2026, 5, 12, 15, 35, tzinfo=IST)
    result = check_stock_no_overnight(p, ts)
    assert result is not None
    assert result[0] == ExitTrigger.NO_OVERNIGHT_STOCK


def test_stock_no_overnight_pre_close_no_trigger():
    p = _pos(is_stock_option=True)
    ts = datetime(2026, 5, 12, 14, 0, tzinfo=IST)
    assert check_stock_no_overnight(p, ts) is None


def test_stock_no_overnight_doesnt_apply_to_index():
    p = _pos(is_stock_option=False)
    ts = datetime(2026, 5, 12, 23, 0, tzinfo=IST)   # well past close
    assert check_stock_no_overnight(p, ts) is None

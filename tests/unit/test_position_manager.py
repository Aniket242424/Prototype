"""Tests for PositionManager state transitions + tick evaluation."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from redis.asyncio import Redis

from trading_agent.core.config import RiskConfig
from trading_agent.core.constants import Direction
from trading_agent.core.time_utils import IST
from trading_agent.position.dtos import (
    ExitTrigger,
    PositionLifecycleStage,
    PositionState,
)
from trading_agent.position.manager import PositionManager


def _risk_cfg() -> RiskConfig:
    """Minimal risk config — only the fields PositionManager reads."""
    return RiskConfig.model_validate({
        "daily_max_loss_pct": 0.02,
        "rolling_drawdown_pct": 0.05,
        "rolling_drawdown_window_days": 5,
        "per_trade_max_risk_pct": 0.005,
        "max_concurrent_positions": 1,
        "max_trades_per_day": 3,
        "consecutive_loss_lockout": 2,
        "slippage_kill_threshold_bps": 30.0,
        "slippage_kill_consecutive": 3,
        "max_spread_bps": 50.0,
        "min_liquidity_score": 0.55,
        "min_top5_depth_lots": 5,
        "min_strike_oi": 1000,
        "max_atr_consumed_at_entry": 0.6,
        "max_vwap_deviation_sigma": 1.5,
        "max_consecutive_signal_candles": 4,
        "opportunity_ttl_seconds": 90,
        "cooldown_after_fast_mover_sec": 180,
        "fast_mover_atr_multiple": 1.2,
        "no_reentry_same_direction_after_stop": True,
        "india_vix_ceiling": 30.0,
        "intraday_move_ceiling_pct": 0.025,
        "stale_tick_max_age_sec": 5.0,
        "broker_health_max_age_sec": 30.0,
        "entry_window_start": "09:20",
        "entry_window_end": "14:30",
        "forced_exit_time": "15:15",
        "max_estimated_slippage_bps": 20.0,
        "fill_timeout_ms": 2000,
        "fill_improve_max_ticks": 3,
        "fill_improve_step_ticks": 1,
        "allow_market_orders_for_entries": False,
        "require_pullback_for_trend_entries": True,
        "initial_stop_atr_multiple": 1.5,
        "breakeven_trigger_r_multiple": 1.0,
        "trail_method": "atr_chandelier",
        "trail_atr_period": 14,
        "trail_atr_multiple": 2.0,
        "trail_only_in_profit": True,
        "partial_profit_r_multiple": 1.5,
        "partial_profit_exit_fraction": 0.5,
        "runner_giveback_atr_multiple": 1.0,
    })


def _make_position(direction: Direction = Direction.LONG, **kw) -> PositionState:
    """Build a default LONG position with NIFTY-like numbers."""
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
        initial_stop_underlying=(Decimal("23950") if direction == Direction.LONG else Decimal("24050")),
        target_underlying=(Decimal("24100") if direction == Direction.LONG else Decimal("23900")),
        stage=PositionLifecycleStage.HARD_STOP,
        current_stop_underlying=(Decimal("23950") if direction == Direction.LONG else Decimal("24050")),
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


def _make_redis_mock(kill_switch_tripped: bool = False) -> Redis:
    """A Redis mock that returns the kill switch state."""
    redis = AsyncMock(spec=Redis)
    redis.get = AsyncMock(return_value=b"1" if kill_switch_tripped else None)
    return redis


def _trade_time() -> datetime:
    """Mid-session, well before forced exit."""
    return datetime(2026, 5, 12, 12, 0, tzinfo=IST)


# ============================================================
# Registry management
# ============================================================

async def test_add_and_query_position():
    pm = PositionManager(_make_redis_mock(), _risk_cfg())
    pos = _make_position()
    await pm.add_position(pos)
    assert len(pm.open_positions()) == 1
    assert pm.positions_on("NIFTY") == [pos]


async def test_remove_position():
    pm = PositionManager(_make_redis_mock(), _risk_cfg())
    pos = _make_position()
    await pm.add_position(pos)
    await pm.remove_position(pos.db_id)
    assert pm.open_positions() == []


async def test_positions_on_filters_by_underlying():
    pm = PositionManager(_make_redis_mock(), _risk_cfg())
    nifty_pos = _make_position(db_id=1, underlying="NIFTY")
    sensex_pos = _make_position(db_id=2, underlying="SENSEX")
    await pm.add_position(nifty_pos)
    await pm.add_position(sensex_pos)
    assert pm.positions_on("NIFTY") == [nifty_pos]
    assert pm.positions_on("SENSEX") == [sensex_pos]


# ============================================================
# evaluate_tick — exit triggers
# ============================================================

async def test_kill_switch_tripped_exits_all_positions():
    pm = PositionManager(_make_redis_mock(kill_switch_tripped=True), _risk_cfg())
    pos = _make_position()
    await pm.add_position(pos)
    decisions = await pm.evaluate_tick("NIFTY", Decimal("24020"), ts=_trade_time())
    assert len(decisions) == 1
    assert decisions[0].trigger == ExitTrigger.KILL_SWITCH


async def test_forced_time_exit_triggers_after_cutoff():
    pm = PositionManager(_make_redis_mock(), _risk_cfg())
    await pm.add_position(_make_position())
    late_ts = datetime(2026, 5, 12, 15, 20, tzinfo=IST)
    decisions = await pm.evaluate_tick("NIFTY", Decimal("24020"), ts=late_ts)
    assert len(decisions) == 1
    assert decisions[0].trigger == ExitTrigger.FORCED_TIME_EXIT


async def test_hard_stop_hit_fires_exit():
    pm = PositionManager(_make_redis_mock(), _risk_cfg())
    await pm.add_position(_make_position())
    decisions = await pm.evaluate_tick("NIFTY", Decimal("23945"), ts=_trade_time())
    assert len(decisions) == 1
    assert decisions[0].trigger == ExitTrigger.HARD_STOP_HIT


async def test_at_target_position_transitions_through_partial_no_immediate_exit():
    """
    At +2R (= target in our default), the position silently transitions:
        HARD_STOP → BREAKEVEN → PARTIAL_AND_TRAIL
    In PARTIAL_AND_TRAIL the chandelier trail is active, not the original target.
    No immediate exit fires (the runner is now riding behind a chandelier stop).
    """
    pm = PositionManager(_make_redis_mock(), _risk_cfg())
    await pm.add_position(_make_position())
    decisions = await pm.evaluate_tick("NIFTY", Decimal("24100"), ts=_trade_time())
    assert decisions == []   # silent transition, chandelier now active
    p = pm._positions[1]
    assert p.stage == PositionLifecycleStage.PARTIAL_AND_TRAIL
    # Chandelier: peak 24100 - 2×60 = 23980
    assert p.current_stop_underlying == Decimal("23980")


async def test_target_hit_fires_when_partial_disabled_via_high_threshold():
    """
    Configure partial threshold ABOVE the target — now we can never reach
    partial-trail before hitting target. Target-hit rule fires as full exit.
    """
    pm = PositionManager(_make_redis_mock(), _risk_cfg())
    # Position with partial threshold set to 3R (target is at 2R)
    pos = _make_position(partial_profit_r_multiple=3.0)
    await pm.add_position(pos)
    decisions = await pm.evaluate_tick("NIFTY", Decimal("24100"), ts=_trade_time())
    assert len(decisions) == 1
    assert decisions[0].trigger == ExitTrigger.TARGET_HIT


async def test_evaluate_tick_skips_other_underlying():
    pm = PositionManager(_make_redis_mock(), _risk_cfg())
    await pm.add_position(_make_position(underlying="SENSEX"))
    decisions = await pm.evaluate_tick("NIFTY", Decimal("23945"), ts=_trade_time())
    assert decisions == []


async def test_no_exit_when_spot_between_stop_and_target():
    pm = PositionManager(_make_redis_mock(), _risk_cfg())
    await pm.add_position(_make_position())
    decisions = await pm.evaluate_tick("NIFTY", Decimal("24020"), ts=_trade_time())
    assert decisions == []


# ============================================================
# Stage transitions (silent — checked via state mutation)
# ============================================================

async def test_breakeven_transition_at_1r_moves_stop_to_entry():
    pm = PositionManager(_make_redis_mock(), _risk_cfg())
    pos = _make_position()
    await pm.add_position(pos)
    # +1R = 24050. No exit fires; stage transitions silently.
    decisions = await pm.evaluate_tick("NIFTY", Decimal("24050"), ts=_trade_time())
    assert decisions == []   # silent transition
    # State should now be BREAKEVEN with stop at entry
    p = pm._positions[1]
    assert p.stage == PositionLifecycleStage.BREAKEVEN
    assert p.current_stop_underlying == Decimal("24000")


async def test_partial_transition_at_15r_switches_to_trail():
    pm = PositionManager(_make_redis_mock(), _risk_cfg())
    pos = _make_position(stage=PositionLifecycleStage.BREAKEVEN,
                          current_stop_underlying=Decimal("24000"))
    await pm.add_position(pos)
    # +1.5R = 24075. Silent transition to PARTIAL_AND_TRAIL.
    decisions = await pm.evaluate_tick("NIFTY", Decimal("24075"), ts=_trade_time())
    p = pm._positions[1]
    assert p.stage == PositionLifecycleStage.PARTIAL_AND_TRAIL
    # current_stop_underlying should now be chandelier: peak - 2×ATR = 24075 - 120 = 23955
    assert p.current_stop_underlying == Decimal("23955")


async def test_chandelier_ratchets_forward_on_new_peak():
    pm = PositionManager(_make_redis_mock(), _risk_cfg())
    pos = _make_position(
        stage=PositionLifecycleStage.PARTIAL_AND_TRAIL,
        peak_underlying=Decimal("24150"),
        current_stop_underlying=Decimal("24030"),   # 24150 - 120
    )
    await pm.add_position(pos)
    # New peak at 24200 → new stop = 24200 - 120 = 24080
    await pm.evaluate_tick("NIFTY", Decimal("24200"), ts=_trade_time())
    p = pm._positions[1]
    assert p.peak_underlying == Decimal("24200")
    assert p.current_stop_underlying == Decimal("24080")


async def test_chandelier_does_not_ratchet_backward():
    pm = PositionManager(_make_redis_mock(), _risk_cfg())
    pos = _make_position(
        stage=PositionLifecycleStage.PARTIAL_AND_TRAIL,
        peak_underlying=Decimal("24200"),
        current_stop_underlying=Decimal("24080"),
    )
    await pm.add_position(pos)
    # Spot drops to 24150 — peak holds, stop unchanged (NO backward move)
    await pm.evaluate_tick("NIFTY", Decimal("24150"), ts=_trade_time())
    p = pm._positions[1]
    assert p.peak_underlying == Decimal("24200")          # peak unchanged
    assert p.current_stop_underlying == Decimal("24080")  # stop unchanged


# ============================================================
# Strategy invalidation callback
# ============================================================

async def test_invalidation_callback_triggers_exit():
    async def always_invalidates(pos: PositionState) -> str | None:
        return "regime flipped to TREND_DOWN"

    pm = PositionManager(
        _make_redis_mock(),
        _risk_cfg(),
        invalidation_check=always_invalidates,
    )
    await pm.add_position(_make_position())
    decisions = await pm.evaluate_tick("NIFTY", Decimal("24020"), ts=_trade_time())
    assert len(decisions) == 1
    assert decisions[0].trigger == ExitTrigger.STRATEGY_INVALIDATION


async def test_invalidation_returning_none_does_not_trigger():
    async def never_invalidates(pos: PositionState) -> str | None:
        return None

    pm = PositionManager(
        _make_redis_mock(),
        _risk_cfg(),
        invalidation_check=never_invalidates,
    )
    await pm.add_position(_make_position())
    decisions = await pm.evaluate_tick("NIFTY", Decimal("24020"), ts=_trade_time())
    assert decisions == []


# ============================================================
# Lifecycle helpers
# ============================================================

async def test_apply_partial_fill_reduces_qty():
    pm = PositionManager(_make_redis_mock(), _risk_cfg())
    pos = _make_position(qty_initial=50, qty_remaining=50)
    await pm.add_position(pos)
    await pm.apply_partial_fill(pos.db_id, qty_closed=25)
    assert pm._positions[pos.db_id].qty_remaining == 25


async def test_apply_close_marks_closed():
    pm = PositionManager(_make_redis_mock(), _risk_cfg())
    pos = _make_position()
    await pm.add_position(pos)
    await pm.apply_close(pos.db_id)
    assert pm._positions[pos.db_id].stage == PositionLifecycleStage.CLOSED
    assert pm._positions[pos.db_id].closed_at is not None
    # No longer counted as open
    assert pm.open_positions() == []

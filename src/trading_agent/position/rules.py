"""
Pure-functional position management rules.

Each rule is an isolated function: takes a PositionState + current market
data, returns either an ExitTrigger (with reason) or None. No I/O, no state
mutation here — that lives in `manager.py`. Pure functions are trivial to
test and reason about.

The order in which the PositionManager calls these matters — the higher-
priority triggers (kill switch, forced time, stock-no-overnight) ALWAYS
override the trade-mechanics triggers (stop, target, etc.). This priority
ordering is enforced by the manager, not by the rules themselves.
"""
from __future__ import annotations

from datetime import datetime, time
from decimal import Decimal

from trading_agent.core.constants import Direction
from trading_agent.core.time_utils import IST, parse_hhmm, to_ist
from trading_agent.position.dtos import (
    ExitTrigger,
    PositionLifecycleStage,
    PositionState,
)


# ============================================================
# Helpers: signed R-multiple math
# ============================================================

def unrealized_r_multiple(pos: PositionState, current_underlying: Decimal) -> float:
    """
    How many R (initial risk) of profit is the position showing right now?

    R_initial = abs(entry_underlying - initial_stop_underlying)
    For LONG:  profit = current - entry → R = profit / R_initial
    For SHORT: profit = entry - current → R = profit / R_initial

    Returns 0.0 if R_initial is zero (degenerate input).
    """
    r_initial = abs(pos.entry_underlying - pos.initial_stop_underlying)
    if r_initial <= 0:
        return 0.0
    if pos.direction == Direction.LONG:
        profit_pts = current_underlying - pos.entry_underlying
    else:
        profit_pts = pos.entry_underlying - current_underlying
    return float(profit_pts / r_initial)


# ============================================================
# Individual exit rules — each returns (ExitTrigger, reason) or None
# ============================================================

def check_current_stop(
    pos: PositionState, current_underlying: Decimal
) -> tuple[ExitTrigger, str] | None:
    """
    Most fundamental rule: spot crosses through the *current* stop level.
    Trigger code varies by lifecycle stage to make analytics easier later.
    """
    if pos.direction == Direction.LONG:
        if current_underlying <= pos.current_stop_underlying:
            trigger = {
                PositionLifecycleStage.HARD_STOP: ExitTrigger.HARD_STOP_HIT,
                PositionLifecycleStage.BREAKEVEN: ExitTrigger.BREAKEVEN_STOP_HIT,
                PositionLifecycleStage.PARTIAL_AND_TRAIL: ExitTrigger.CHANDELIER_TRAIL_HIT,
            }.get(pos.stage, ExitTrigger.HARD_STOP_HIT)
            return trigger, (
                f"LONG spot {current_underlying} ≤ stop {pos.current_stop_underlying} "
                f"(stage={pos.stage.value})"
            )
    else:   # SHORT (long-PE in our universe)
        if current_underlying >= pos.current_stop_underlying:
            trigger = {
                PositionLifecycleStage.HARD_STOP: ExitTrigger.HARD_STOP_HIT,
                PositionLifecycleStage.BREAKEVEN: ExitTrigger.BREAKEVEN_STOP_HIT,
                PositionLifecycleStage.PARTIAL_AND_TRAIL: ExitTrigger.CHANDELIER_TRAIL_HIT,
            }.get(pos.stage, ExitTrigger.HARD_STOP_HIT)
            return trigger, (
                f"SHORT spot {current_underlying} ≥ stop {pos.current_stop_underlying} "
                f"(stage={pos.stage.value})"
            )
    return None


def check_target(
    pos: PositionState, current_underlying: Decimal
) -> tuple[ExitTrigger, str] | None:
    """
    Target hit — initial 1:2 RR level.

    Only checked while in HARD_STOP or BREAKEVEN stages; after partial+trail,
    the chandelier trail takes over (we're letting the runner ride past target).
    """
    if pos.stage == PositionLifecycleStage.PARTIAL_AND_TRAIL:
        return None
    if pos.direction == Direction.LONG:
        if current_underlying >= pos.target_underlying:
            return ExitTrigger.TARGET_HIT, (
                f"LONG spot {current_underlying} ≥ target {pos.target_underlying}"
            )
    else:
        if current_underlying <= pos.target_underlying:
            return ExitTrigger.TARGET_HIT, (
                f"SHORT spot {current_underlying} ≤ target {pos.target_underlying}"
            )
    return None


def check_runner_giveback(
    pos: PositionState, current_underlying: Decimal
) -> tuple[ExitTrigger, str] | None:
    """
    After we've taken partial profit and are riding the runner, cap the
    giveback from peak at 1×ATR (configurable). Stops a winner from turning
    into a breakeven on a parabolic move that reverses sharply.

    Only applies in PARTIAL_AND_TRAIL stage.
    """
    if pos.stage != PositionLifecycleStage.PARTIAL_AND_TRAIL:
        return None
    giveback_pts = Decimal(str(pos.runner_giveback_atr_multiple * pos.atr_at_entry))
    if pos.direction == Direction.LONG:
        threshold = pos.peak_underlying - giveback_pts
        if current_underlying <= threshold:
            return ExitTrigger.RUNNER_GIVEBACK, (
                f"LONG gave back {pos.peak_underlying - current_underlying:.2f} from peak "
                f"{pos.peak_underlying} (cap {giveback_pts})"
            )
    else:
        threshold = pos.peak_underlying + giveback_pts
        if current_underlying >= threshold:
            return ExitTrigger.RUNNER_GIVEBACK, (
                f"SHORT gave back {current_underlying - pos.peak_underlying:.2f} from peak "
                f"{pos.peak_underlying} (cap {giveback_pts})"
            )
    return None


def check_forced_time_exit(
    now_ts: datetime, forced_exit_hhmm: str
) -> tuple[ExitTrigger, str] | None:
    """
    Force all positions flat by `forced_exit_hhmm` IST (default 15:15).

    No overnight day-trade positions, period. The Indian options market
    closes at 15:30 with theta accelerating sharply in the last 30 min —
    we get out earlier to avoid that compression.
    """
    now_time = to_ist(now_ts).time()
    cutoff = parse_hhmm(forced_exit_hhmm)
    if now_time >= cutoff:
        return ExitTrigger.FORCED_TIME_EXIT, (
            f"current {now_time} ≥ forced exit {cutoff} (IST)"
        )
    return None


def check_stock_no_overnight(
    pos: PositionState, now_ts: datetime, market_close_hhmm: str = "15:30"
) -> tuple[ExitTrigger, str] | None:
    """
    Stock options are NEVER held overnight. Hard rule from scope-v2 memory
    note — single-stock gap risk is too large for any trail/stop to bound.

    Index options can be held overnight in principle (we still day-trade,
    but the no-overnight RULE is stock-specific). For now both are flat by
    forced_time_exit at 15:15.
    """
    if not pos.is_stock_option:
        return None
    # Same logic as forced time exit but with stock-specific code
    now_time = to_ist(now_ts).time()
    cutoff = parse_hhmm(market_close_hhmm)
    if now_time >= cutoff:
        return ExitTrigger.NO_OVERNIGHT_STOCK, (
            f"stock option at {now_time} ≥ market close {cutoff} — never overnight"
        )
    return None


# ============================================================
# Stage-transition helpers — manager calls these to update state
# (these MUTATE PositionState; manager handles re-evaluating after)
# ============================================================

def should_move_to_breakeven(
    pos: PositionState, current_underlying: Decimal
) -> bool:
    """
    Move stop to breakeven (entry) once +1R (configurable) is achieved.

    Only relevant in HARD_STOP stage — once we've moved past BE we don't
    move back.
    """
    if pos.stage != PositionLifecycleStage.HARD_STOP:
        return False
    r = unrealized_r_multiple(pos, current_underlying)
    return r >= pos.breakeven_trigger_r_multiple


def should_take_partial(
    pos: PositionState, current_underlying: Decimal
) -> bool:
    """
    Take partial profit at +1.5R (configurable). Switches to PARTIAL_AND_TRAIL.
    """
    if pos.stage != PositionLifecycleStage.BREAKEVEN:
        return False
    r = unrealized_r_multiple(pos, current_underlying)
    return r >= pos.partial_profit_r_multiple


def new_chandelier_stop(
    pos: PositionState, current_underlying: Decimal
) -> Decimal:
    """
    Compute the chandelier trailing stop given the current peak.

    LONG:  stop = peak - trail_atr_multiple × atr_at_entry
    SHORT: stop = peak + trail_atr_multiple × atr_at_entry

    Only RATCHETS in the favorable direction (longs: stop only moves up;
    shorts: only moves down). Caller handles the ratchet by comparing to
    current_stop_underlying.
    """
    trail_pts = Decimal(str(pos.trail_atr_multiple * pos.atr_at_entry))
    if pos.direction == Direction.LONG:
        return pos.peak_underlying - trail_pts
    return pos.peak_underlying + trail_pts


def update_peak(
    pos: PositionState, current_underlying: Decimal
) -> Decimal:
    """
    Return new peak_underlying — high-water for LONG, low-water for SHORT.
    """
    if pos.direction == Direction.LONG:
        return max(pos.peak_underlying, current_underlying)
    return min(pos.peak_underlying, current_underlying)

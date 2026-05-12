"""
Position Manager — the runtime state machine for open positions.

Responsibilities:
- Hold a registry of all open PositionStates
- On each underlying tick: call evaluate_tick() for that underlying
- Decide which exits should fire based on the pure-functional rules
- Coordinate with the Strategy Engine for invalidation callbacks
- Trigger the Execution Engine's emergency_exit path

Priority ordering of exit checks (highest first — first match wins):
    1. KILL_SWITCH (consulted by manager via Redis)         [global override]
    2. FORCED_TIME_EXIT (15:15 IST)                          [hard rule]
    3. NO_OVERNIGHT_STOCK (stocks only, near close)          [hard rule]
    4. STRATEGY_INVALIDATION (thesis broken)                 [thesis-level]
    5. CURRENT_STOP_HIT (hard/breakeven/chandelier)           [price-level]
    6. TARGET_HIT (initial 1:2 RR)                            [price-level]
    7. RUNNER_GIVEBACK (post-partial only)                    [price-level]

Stage transitions (silent — no exit, just internal state):
    - Update peak_underlying on each favorable tick
    - HARD_STOP → BREAKEVEN at +1R
    - BREAKEVEN → PARTIAL_AND_TRAIL at +1.5R (triggers partial exit)
    - PARTIAL_AND_TRAIL: ratchet chandelier stop on new peaks
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Awaitable, Callable

from redis.asyncio import Redis

from trading_agent.core.config import RiskConfig, get_risk_config
from trading_agent.core.constants import KILL_SWITCH_KEY
from trading_agent.core.logging import get_logger
from trading_agent.core.time_utils import now_ist
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
    update_peak,
    unrealized_r_multiple,
)
from trading_agent.strategy.base import StrategyContext, StrategySignal

log = get_logger(__name__)


@dataclass(frozen=True)
class ExitDecision:
    """Result of evaluate_tick when an exit is triggered."""
    position_db_id: int
    trigger: ExitTrigger
    reason: str
    qty_to_exit: int                      # Partial or full
    is_partial: bool                       # True if 50% partial; False if full close


# Type alias for the invalidation callback. Phase 4 worker injects a closure
# that knows the original strategy + builds a StrategyContext on demand.
InvalidationCheck = Callable[[PositionState], Awaitable[str | None]]


class PositionManager:
    """
    In-memory state machine over all open positions.

    Designed to be created once per worker process. Phase 4.6 worker
    will instantiate it, add positions as fills come in, and call
    evaluate_tick on each pubsub tick.
    """

    def __init__(
        self,
        redis: Redis,
        risk_config: RiskConfig | None = None,
        invalidation_check: InvalidationCheck | None = None,
    ):
        self._redis = redis
        self._risk = risk_config or get_risk_config()
        self._invalidation_check = invalidation_check
        self._positions: dict[int, PositionState] = {}    # db_id → state
        self._lock = asyncio.Lock()

    # ----------------- Registry management -----------------

    async def add_position(self, pos: PositionState) -> None:
        async with self._lock:
            self._positions[pos.db_id] = pos
        log.info(
            "position_manager.added",
            db_id=pos.db_id,
            underlying=pos.underlying,
            direction=pos.direction.value,
            entry_underlying=str(pos.entry_underlying),
            initial_stop=str(pos.initial_stop_underlying),
            target=str(pos.target_underlying),
            qty=pos.qty_initial,
        )

    async def remove_position(self, db_id: int) -> None:
        async with self._lock:
            self._positions.pop(db_id, None)

    def open_positions(self) -> list[PositionState]:
        return [p for p in self._positions.values()
                if p.stage != PositionLifecycleStage.CLOSED]

    def positions_on(self, underlying: str) -> list[PositionState]:
        return [
            p for p in self._positions.values()
            if p.underlying == underlying
            and p.stage != PositionLifecycleStage.CLOSED
        ]

    # ----------------- Evaluation -----------------

    async def evaluate_tick(
        self,
        underlying: str,
        current_underlying: Decimal,
        ts: datetime | None = None,
    ) -> list[ExitDecision]:
        """
        Process one tick for all open positions on this underlying.

        Returns a list of ExitDecisions (one per position that needs exit).
        The Phase 4 worker turns each into an actual emergency_exit call.
        """
        ts = ts or now_ist()
        kill_switch_tripped = await self._is_kill_switch_tripped()

        exits: list[ExitDecision] = []
        async with self._lock:
            for pos in list(self._positions.values()):
                if pos.underlying != underlying:
                    continue
                if pos.stage == PositionLifecycleStage.CLOSED:
                    continue

                decision = await self._evaluate_one(
                    pos, current_underlying, ts, kill_switch_tripped
                )
                if decision is not None:
                    exits.append(decision)
        return exits

    async def evaluate_all_time_based(
        self, ts: datetime | None = None
    ) -> list[ExitDecision]:
        """
        Time-based check that doesn't need a tick. Phase 4 worker calls this
        every 30s to enforce forced_time_exit (15:15 IST) on positions
        whose underlying hasn't traded recently.
        """
        ts = ts or now_ist()
        exits: list[ExitDecision] = []
        async with self._lock:
            for pos in list(self._positions.values()):
                if pos.stage == PositionLifecycleStage.CLOSED:
                    continue

                trigger = check_forced_time_exit(ts, self._risk.forced_exit_time)
                if trigger is None and pos.is_stock_option:
                    trigger = check_stock_no_overnight(pos, ts)

                if trigger is not None:
                    code, reason = trigger
                    exits.append(self._build_full_exit(pos, code, reason))
        return exits

    # ----------------- Internal -----------------

    async def _evaluate_one(
        self,
        pos: PositionState,
        current_underlying: Decimal,
        ts: datetime,
        kill_switch_tripped: bool,
    ) -> ExitDecision | None:
        """Run the priority-ordered rule chain on a single position."""

        # Always update peak BEFORE evaluating exits — chandelier math uses fresh peak.
        pos.peak_underlying = update_peak(pos, current_underlying)
        pos.last_evaluated_underlying = current_underlying

        # === PRIORITY 1: Kill switch ===
        if kill_switch_tripped:
            return self._build_full_exit(
                pos, ExitTrigger.KILL_SWITCH, "global kill switch tripped"
            )

        # === PRIORITY 2: Forced time exit ===
        trigger = check_forced_time_exit(ts, self._risk.forced_exit_time)
        if trigger is not None:
            code, reason = trigger
            return self._build_full_exit(pos, code, reason)

        # === PRIORITY 3: Stock no-overnight ===
        if pos.is_stock_option:
            trigger = check_stock_no_overnight(pos, ts)
            if trigger is not None:
                code, reason = trigger
                return self._build_full_exit(pos, code, reason)

        # === PRIORITY 4: Strategy invalidation ===
        if self._invalidation_check is not None:
            invalidation_reason = await self._invalidation_check(pos)
            if invalidation_reason is not None:
                return self._build_full_exit(
                    pos,
                    ExitTrigger.STRATEGY_INVALIDATION,
                    f"strategy invalidation: {invalidation_reason}",
                )

        # === Stage transitions (silent — no exit; may mutate stop level) ===
        self._apply_stage_transitions(pos, current_underlying, ts)

        # === PRIORITY 5: Current stop hit ===
        trigger = check_current_stop(pos, current_underlying)
        if trigger is not None:
            code, reason = trigger
            return self._build_full_exit(pos, code, reason)

        # === PRIORITY 6: Target hit (HARD_STOP or BREAKEVEN stage only) ===
        trigger = check_target(pos, current_underlying)
        if trigger is not None:
            code, reason = trigger
            # Target hit while in BE/HARD_STOP: this is the transition to
            # "take partial" — but we model it as: target hit → full exit.
            # The partial-exit path is handled via the +1.5R transition,
            # NOT via the initial target. So target HIT = full close.
            return self._build_full_exit(pos, code, reason)

        # === PRIORITY 7: Runner giveback ===
        trigger = check_runner_giveback(pos, current_underlying)
        if trigger is not None:
            code, reason = trigger
            return self._build_full_exit(pos, code, reason)

        return None

    def _apply_stage_transitions(
        self, pos: PositionState, current_underlying: Decimal, ts: datetime
    ) -> ExitDecision | None:
        """
        Silent stage transitions. May mutate pos.current_stop_underlying.
        Does NOT return an ExitDecision unless partial profit is being taken
        (which is the only stage transition that produces an exit).
        """
        # HARD_STOP → BREAKEVEN at +1R
        if should_move_to_breakeven(pos, current_underlying):
            pos.stage = PositionLifecycleStage.BREAKEVEN
            pos.current_stop_underlying = pos.entry_underlying
            pos.breakeven_moved_at = ts
            log.info(
                "position_manager.moved_to_breakeven",
                db_id=pos.db_id,
                underlying=pos.underlying,
                r_multiple=round(unrealized_r_multiple(pos, current_underlying), 2),
                new_stop=str(pos.current_stop_underlying),
            )

        # BREAKEVEN → PARTIAL_AND_TRAIL at +1.5R
        # NOTE: the PARTIAL EXIT itself is fired by the caller — this method
        # just transitions state. We surface the partial-exit as a special
        # ExitDecision from evaluate_tick separately. Here we just flip stage.
        # (In a future refactor we could split partial-exit into its own path.)
        if should_take_partial(pos, current_underlying):
            pos.stage = PositionLifecycleStage.PARTIAL_AND_TRAIL
            pos.partial_taken_at = ts
            # Switch stop to chandelier
            pos.current_stop_underlying = new_chandelier_stop(pos, current_underlying)
            # NOTE: The Phase 4 worker should call partial_exit_signal() to
            # actually fire the 50% market sell. For now we model it as the
            # state transition; worker handles persistence/orders.
            log.info(
                "position_manager.transition_partial_trail",
                db_id=pos.db_id,
                underlying=pos.underlying,
                r_multiple=round(unrealized_r_multiple(pos, current_underlying), 2),
                new_stop=str(pos.current_stop_underlying),
            )

        # PARTIAL_AND_TRAIL: ratchet chandelier stop forward on each new peak
        if pos.stage == PositionLifecycleStage.PARTIAL_AND_TRAIL:
            candidate = new_chandelier_stop(pos, current_underlying)
            # Ratchet — LONG: stop only moves up; SHORT: stop only moves down
            if pos.direction.value == "LONG":
                if candidate > pos.current_stop_underlying:
                    pos.current_stop_underlying = candidate
            else:
                if candidate < pos.current_stop_underlying:
                    pos.current_stop_underlying = candidate

        return None

    def _build_full_exit(
        self, pos: PositionState, trigger: ExitTrigger, reason: str
    ) -> ExitDecision:
        return ExitDecision(
            position_db_id=pos.db_id,
            trigger=trigger,
            reason=reason,
            qty_to_exit=pos.qty_remaining,
            is_partial=False,
        )

    def partial_exit_decision(self, pos: PositionState) -> ExitDecision:
        """
        Build a PARTIAL exit decision for the Phase 4 worker to act on.

        Called by the worker after detecting that stage just transitioned to
        PARTIAL_AND_TRAIL. Exits `partial_profit_exit_fraction` of qty_remaining.
        """
        exit_qty = max(1, int(pos.qty_remaining * pos.partial_profit_exit_fraction))
        return ExitDecision(
            position_db_id=pos.db_id,
            trigger=ExitTrigger.TARGET_HIT,    # partial = early TARGET payday
            reason=f"partial profit-taking ({pos.partial_profit_exit_fraction*100:.0f}% of remaining)",
            qty_to_exit=exit_qty,
            is_partial=True,
        )

    async def apply_partial_fill(
        self, db_id: int, qty_closed: int
    ) -> None:
        """Worker calls this after a partial exit fills. Reduces qty_remaining."""
        async with self._lock:
            pos = self._positions.get(db_id)
            if pos is None:
                return
            pos.qty_remaining = max(0, pos.qty_remaining - qty_closed)

    async def apply_close(self, db_id: int) -> None:
        """Worker calls this after full exit fills. Removes from registry."""
        async with self._lock:
            pos = self._positions.get(db_id)
            if pos is None:
                return
            pos.stage = PositionLifecycleStage.CLOSED
            pos.closed_at = now_ist()

    async def _is_kill_switch_tripped(self) -> bool:
        try:
            v = await self._redis.get(KILL_SWITCH_KEY)
            return v is not None and v == b"1"
        except Exception:
            # Fail closed — treat unknown state as tripped (paranoid by design)
            log.warning("position_manager.kill_switch_check_failed")
            return True

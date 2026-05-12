"""Typed contracts internal to the Position Manager."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from trading_agent.core.constants import Direction


class PositionLifecycleStage(StrEnum):
    """
    Where in its lifecycle a position currently is. Drives which exit rules
    apply on each tick evaluation.
    """

    HARD_STOP = "HARD_STOP"        # Initial — stop at strategy.stop_underlying
    BREAKEVEN = "BREAKEVEN"         # +1R achieved — stop moved to entry
    PARTIAL_AND_TRAIL = "PARTIAL_AND_TRAIL"  # 50% taken at +1.5R, trailing remainder
    CLOSED = "CLOSED"                # Final — no further evaluation


class ExitTrigger(StrEnum):
    """
    Which rule fired the exit. Persisted to positions.metadata for analytics —
    helps Phase 5 / Phase 6 learning identify which exit types are working.
    """

    HARD_STOP_HIT = "HARD_STOP_HIT"
    BREAKEVEN_STOP_HIT = "BREAKEVEN_STOP_HIT"
    CHANDELIER_TRAIL_HIT = "CHANDELIER_TRAIL_HIT"
    TARGET_HIT = "TARGET_HIT"
    RUNNER_GIVEBACK = "RUNNER_GIVEBACK"
    FORCED_TIME_EXIT = "FORCED_TIME_EXIT"        # 15:15 IST hard rule
    NO_OVERNIGHT_STOCK = "NO_OVERNIGHT_STOCK"     # stock options never overnight
    STRATEGY_INVALIDATION = "STRATEGY_INVALIDATION"
    KILL_SWITCH = "KILL_SWITCH"


class PositionState(BaseModel):
    """
    Live tracking of a single open position.

    Created by the Phase 4 worker after a successful Execution fill.
    Mutated in-place during evaluate_tick cycles (Pydantic model_copy used
    to preserve immutability semantics where possible).
    """

    model_config = ConfigDict(frozen=False)   # mutable — manager updates this in place

    # Identity
    db_id: int                              # PositionRow.id
    instrument_key: str                      # Option contract: NSE_FO|...
    underlying: str                          # NIFTY / SENSEX / etc.

    # Trade details
    strategy_name: str
    direction: Direction
    qty_initial: int                         # In contracts
    qty_remaining: int                        # After any partial exits
    avg_entry_premium: Decimal                # Per-contract option premium paid
    entry_underlying: Decimal                 # Underlying spot at entry

    # Strategy-set exit levels (on the UNDERLYING, not premium)
    initial_stop_underlying: Decimal
    target_underlying: Decimal

    # Mutable state
    stage: PositionLifecycleStage = PositionLifecycleStage.HARD_STOP
    current_stop_underlying: Decimal           # Ratchets forward through lifecycle
    peak_underlying: Decimal                    # High water mark (longs) / low (shorts)
    last_evaluated_underlying: Decimal | None = None

    # ATR at entry — frozen reference for chandelier math (not stale ATR later)
    atr_at_entry: float = Field(gt=0)

    # Lifecycle bookkeeping
    opened_at: datetime
    breakeven_moved_at: datetime | None = None
    partial_taken_at: datetime | None = None
    closed_at: datetime | None = None

    # Configuration snapshot — frozen at entry so config changes don't affect
    # live positions mid-trade
    breakeven_trigger_r_multiple: float
    partial_profit_r_multiple: float
    partial_profit_exit_fraction: float
    trail_atr_multiple: float
    runner_giveback_atr_multiple: float

    # Optional provenance for invalidation callbacks
    is_stock_option: bool = False

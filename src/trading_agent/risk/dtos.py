"""
Typed contracts at the Risk Engine boundary.

TradeIntent carries a list of Legs to support multi-leg structures (spreads,
hedged buys) without rewriting the framework later. Phase 3 ships SINGLE-LEG
intents only; Phase 8 enables multi-leg strategies. The framework is ready
either way (per multi-leg-framework-decision memory note).
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from trading_agent.core.constants import (
    Direction,
    OptionType,
    OrderSide,
    OrderStatus,
    OrderType,
)


class Leg(BaseModel):
    """One leg of a trade. Single-leg intents have exactly one Leg."""

    model_config = ConfigDict(frozen=True)

    side: OrderSide                     # BUY for option buying (always for now)
    option_type: OptionType             # CE or PE
    strike: Decimal
    expiry: date
    instrument_key: str                  # Upstox NSE_FO|... or BSE_FO|...
    target_qty: int                      # In contracts (lots × lot_size)
    target_premium: Decimal              # Limit price target


class TradeIntent(BaseModel):
    """
    A concrete trade proposal from the Strategy Engine to the Risk Engine.

    `legs` always has at least one entry. Single-leg buying intents (Phase 3-7)
    have len(legs) == 1. Multi-leg structures (Phase 8 spreads) have 2+.
    """

    model_config = ConfigDict(frozen=True)

    strategy_name: str                   # e.g., "ema_crossover_trend"
    underlying: str                       # NIFTY / BANKNIFTY / SENSEX / RELIANCE / ...
    direction: Direction                  # LONG (CE) / SHORT (PE)
    legs: list[Leg]

    # Underlying-level stop/target (Position Manager monitors underlying price)
    stop_underlying: Decimal              # In underlying price, not premium
    target_underlying: Decimal

    # Strategy confidence (0..1) — used by Risk Engine for sizing
    confidence: float = Field(ge=0, le=1)

    # Provenance — links back to the Opportunity row in DB
    opportunity_id: int | None = None

    # When the strategy emitted this intent
    ts: datetime


class RiskDecision(BaseModel):
    """
    Output of `RiskEngine.evaluate(intent)`.

    Approve or reject. Always persisted to `risk_decisions` table whether
    approved or rejected — rejections are valuable analytics.
    """

    model_config = ConfigDict(frozen=True)

    approved: bool
    code: str                            # "OK", "KILL_SWITCH", "DAILY_LOSS_CAP", etc.
    reason: str                           # Human-readable explanation
    sized_qty: int | None = None         # In contracts (if approved); None if rejected
    sized_lots: int | None = None        # In lots (sized_qty / lot_size)
    max_outlay_inr: Decimal | None = None  # If approved: premium × sized_qty
    inputs_snapshot: dict                  # The 16-check inputs at decision time
    ts: datetime


class Fill(BaseModel):
    """A single fill from the broker (or paper-mode simulator)."""

    model_config = ConfigDict(frozen=True)

    leg_index: int                       # Index into TradeIntent.legs
    qty: int                              # Contracts filled in this fill
    price: Decimal                        # Per-contract fill price
    fee: Decimal = Decimal("0")
    ts: datetime
    is_paper: bool


class ExecutionResult(BaseModel):
    """
    Output of `ExecutionEngine.execute(approved_decision)`. Phase 3.2.

    Records what actually happened with the broker (or paper-mode sim).
    """

    model_config = ConfigDict(frozen=True)

    order_id: int                        # FK to orders table
    status: OrderStatus
    fills: list[Fill] = Field(default_factory=list)
    realized_slippage_bps: float         # (fill_vwap - reference_mid) / reference_mid * 10000
    reference_mid: Decimal                # Mid price at order submission time
    estimated_slippage_bps: float        # What the slippage estimator predicted
    rejection_reason: str | None = None
    is_paper: bool

"""
Strategy framework — the contract that every Phase 4 strategy implements.

Design notes:
- Strategy is a Protocol, not an ABC. Concrete strategies are plain classes;
  duck-typing keeps them easy to write and test.
- Each strategy is pure-functional at its core: `evaluate(ctx) -> StrategySignal | None`.
  No I/O inside the strategy. The Phase 4 worker handles persistence + downstream
  routing.
- `invalidation(intent, ctx) -> str | None` lets the Position Manager call back
  to ask "is this still a valid trade premise?" — returns a rejection reason
  if invalidated. Phase 4.4 Position Manager wires this in.
- StrategyReject is used internally to short-circuit a strategy's filter
  chain with structured rejection metadata. Exposed for testing strategy
  filter logic in isolation.

Why a protocol rather than abstract base class:
- Tests can build minimal mock strategies without inheriting machinery
- Forward-compat with structural subtyping
- Strategies are intentionally simple — they don't need framework code
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from trading_agent.core.constants import Direction, OptionType, OrderSide
from trading_agent.regime.dtos import (
    IndicatorSnapshot,
    Opportunity,
    OptionsIntel,
    RegimeState,
)
from trading_agent.risk.dtos import Leg, TradeIntent


@dataclass(frozen=True)
class StrategyContext:
    """
    Read-only bundle the Phase 4 worker passes to every strategy.

    Strategies inspect this context, evaluate their entry triggers + filters,
    and return either a StrategySignal (proposing a trade) or None (no setup).
    """

    underlying: str
    direction: Direction              # From the Opportunity
    opportunity: Opportunity           # Full opportunity row
    regime: RegimeState                 # Current regime classification
    indicators: IndicatorSnapshot       # Current indicator snapshot
    intel: OptionsIntel | None          # None if intel not yet computed
    ts: datetime                         # When this evaluation is running


class StrategyReject(Exception):
    """
    Raised inside a strategy's filter chain to short-circuit with a structured reason.
    The Phase 4 worker catches this and converts to a logged-rejection (no trade).
    Tests assert on .code to verify the right filter rejected.
    """

    def __init__(self, code: str, reason: str):
        super().__init__(reason)
        self.code = code
        self.reason = reason


class StrategySignal(BaseModel):
    """
    Output of `Strategy.evaluate(ctx)`.

    The Phase 4 worker promotes this to a TradeIntent (which the Risk Engine
    consumes). The split exists so the strategy doesn't need to know about
    strike selection or order sizing — those are Risk Engine concerns. The
    strategy just describes its setup; the Risk Engine decides the size and
    the strike selector picks ATM/ITM1/ITM2.
    """

    model_config = ConfigDict(frozen=True)

    strategy_name: str                  # e.g. "ema_crossover_trend"
    underlying: str
    direction: Direction
    option_type: OptionType             # CE for LONG, PE for SHORT

    # Stop/target ARE on the underlying (not on premium) — Position Manager
    # monitors underlying and fires exits when these levels are crossed.
    stop_underlying: Decimal
    target_underlying: Decimal

    # Strategy's confidence in this setup (0..1). Multiplied with opportunity
    # score by the Risk Engine for sizing.
    confidence: float = Field(ge=0, le=1)

    # Why this strategy fired — structured for analytics + post-mortem.
    rationale: dict
    ts: datetime

    def to_trade_intent_with_leg(self, leg: Leg) -> TradeIntent:
        """
        Once the strike selector has picked a concrete contract, the Phase 4
        worker calls this to build the final TradeIntent. Strategy-side
        information (stops, confidence, rationale) is preserved.
        """
        return TradeIntent(
            strategy_name=self.strategy_name,
            underlying=self.underlying,
            direction=self.direction,
            legs=[leg],
            stop_underlying=self.stop_underlying,
            target_underlying=self.target_underlying,
            confidence=self.confidence,
            ts=self.ts,
        )


class Strategy(Protocol):
    """
    The contract every strategy implements.

    `name` is the strategy_name used in logs, DB rows, and config flags.
    `evaluate` is the entry-trigger + filter-chain logic.
    `invalidation` is called by the Position Manager when monitoring an open
    position — returns a non-empty string if the strategy's thesis has been
    invalidated, in which case the position should be closed.
    """

    name: str

    def evaluate(self, ctx: StrategyContext) -> StrategySignal | None:
        """
        Return a StrategySignal if the strategy's entry conditions are met,
        else None. Should raise StrategyReject internally with structured
        codes; the worker catches those.
        """
        ...

    def invalidation(
        self,
        signal: StrategySignal,
        ctx: StrategyContext,
    ) -> str | None:
        """
        Called by the Position Manager when monitoring an open position.
        Return None if the trade premise is still valid; return a reason
        string to trigger an early exit. Default: only the standard
        stop-loss trigger applies (Position Manager handles that separately).
        """
        ...

"""
Position Manager — Phase 4.4.

Manages open positions through their three-stage lifecycle (hard stop →
breakeven → partial-and-trail). Translates underlying-level exit triggers
to MARKET exits on the option contract via the Execution Engine.

The Position Manager runs continuously while ANY position is open.
On each underlying tick, it evaluates 6 exit triggers:

  1. Hard stop / current stop hit
  2. Target hit
  3. Time-based forced exit (15:15 IST)
  4. Stock-options no-overnight enforcement
  5. Runner gives back > 1×ATR from peak (post-partial only)
  6. Strategy invalidation (strategy.invalidation() returns non-None)

State transitions handled silently between evaluations:
  - +1R reached → move stop to breakeven
  - +1.5R reached → partial exit 50%, switch to ATR chandelier trail
  - New peak → ratchet chandelier stop forward
"""
from trading_agent.position.dtos import (
    ExitTrigger,
    PositionLifecycleStage,
    PositionState,
)
from trading_agent.position.manager import PositionManager

__all__ = [
    "ExitTrigger",
    "PositionLifecycleStage",
    "PositionState",
    "PositionManager",
]

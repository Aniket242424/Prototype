"""
Strategy Engine — Phase 4.

Converts an Opportunity (from Phase 2 ranker) into a concrete TradeIntent
(consumed by Phase 3 Risk Engine). Each strategy is a class implementing
the Strategy protocol. Phase 4 ships 5 strategies; this module hosts them
plus the framework they share.
"""
from trading_agent.strategy.base import (
    Strategy,
    StrategyContext,
    StrategyReject,
    StrategySignal,
)

__all__ = [
    "Strategy",
    "StrategyContext",
    "StrategyReject",
    "StrategySignal",
]

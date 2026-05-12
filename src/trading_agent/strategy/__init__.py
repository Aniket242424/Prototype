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
from trading_agent.strategy.ema_crossover import (
    EMACrossoverConfig,
    EMACrossoverTrendStrategy,
)
from trading_agent.strategy.gap_continuation import (
    GapContinuationConfig,
    GapContinuationStrategy,
)
from trading_agent.strategy.orb import ORBConfig, ORBStrategy
from trading_agent.strategy.registry import build_enabled_strategies
from trading_agent.strategy.vol_expansion import (
    VolExpansionConfig,
    VolExpansionStrategy,
)

__all__ = [
    "Strategy",
    "StrategyContext",
    "StrategyReject",
    "StrategySignal",
    "EMACrossoverConfig",
    "EMACrossoverTrendStrategy",
    "GapContinuationConfig",
    "GapContinuationStrategy",
    "ORBConfig",
    "ORBStrategy",
    "VolExpansionConfig",
    "VolExpansionStrategy",
    "build_enabled_strategies",
]

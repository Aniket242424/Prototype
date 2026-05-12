"""
Risk Engine — Phase 3.

The only path from a signal (Opportunity) to a broker order. 16 deterministic
checks gate every trade. The live-trading 3-lock check is the first gate;
nothing else runs until it's satisfied. See docs/architecture/risk_design.md.
"""
from trading_agent.risk.dtos import (
    ExecutionResult,
    Fill,
    Leg,
    RiskDecision,
    TradeIntent,
)

__all__ = [
    "ExecutionResult",
    "Fill",
    "Leg",
    "RiskDecision",
    "TradeIntent",
]

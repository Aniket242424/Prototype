"""
Low-Slippage Execution Engine — Phase 3.2.

Converts an approved RiskDecision into actual fills (real or simulated paper).
LIMIT-first placement, tick-walk on partial fills, MARKET only for emergency
exits. Spread + depth aware. Multi-leg atomic execution per Option B framework.

The Execution Engine NEVER bypasses the Risk Engine. It only consumes
RiskDecision(approved=True). Phase 3.1's gate is the only path to here.
"""
from trading_agent.execution.state_machine import (
    OrderEvent,
    OrderState,
    StateMachineError,
    transition,
)

__all__ = [
    "OrderEvent",
    "OrderState",
    "StateMachineError",
    "transition",
]

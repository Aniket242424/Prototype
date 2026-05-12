"""
Order state machine.

Strict transitions: any invalid transition raises StateMachineError. This
is the safety net — bugs in the Execution Engine can't accidentally fire a
"send order" on an already-filled or rejected order.

States:
    NEW       — created, not yet sent
    SENT      — submitted to broker, awaiting fill/timeout
    PARTIAL   — some quantity filled, remainder open
    FILLED    — fully filled
    CANCELLED — operator/timeout cancelled before fill
    REJECTED  — broker rejected (e.g., margin, instrument restriction)

Events drive transitions. The state machine is pure — no I/O.
"""
from __future__ import annotations

from enum import StrEnum

from trading_agent.core.exceptions import ExecutionError


class OrderState(StrEnum):
    NEW = "NEW"
    SENT = "SENT"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


class OrderEvent(StrEnum):
    SUBMIT = "SUBMIT"                 # NEW → SENT
    ACK = "ACK"                       # SENT → SENT (idempotent — broker confirmed receipt)
    PARTIAL_FILL = "PARTIAL_FILL"     # SENT|PARTIAL → PARTIAL
    FILL = "FILL"                     # SENT|PARTIAL → FILLED
    CANCEL = "CANCEL"                 # SENT|PARTIAL → CANCELLED
    REJECT = "REJECT"                 # SENT → REJECTED


class StateMachineError(ExecutionError):
    """Raised when an event is invalid for the current state."""


_TRANSITIONS: dict[tuple[OrderState, OrderEvent], OrderState] = {
    (OrderState.NEW, OrderEvent.SUBMIT):           OrderState.SENT,
    (OrderState.SENT, OrderEvent.ACK):             OrderState.SENT,
    (OrderState.SENT, OrderEvent.PARTIAL_FILL):    OrderState.PARTIAL,
    (OrderState.SENT, OrderEvent.FILL):            OrderState.FILLED,
    (OrderState.SENT, OrderEvent.CANCEL):          OrderState.CANCELLED,
    (OrderState.SENT, OrderEvent.REJECT):          OrderState.REJECTED,
    (OrderState.PARTIAL, OrderEvent.PARTIAL_FILL): OrderState.PARTIAL,
    (OrderState.PARTIAL, OrderEvent.FILL):         OrderState.FILLED,
    (OrderState.PARTIAL, OrderEvent.CANCEL):       OrderState.CANCELLED,
}


def transition(state: OrderState, event: OrderEvent) -> OrderState:
    """
    Apply event to state. Returns new state. Raises StateMachineError on
    invalid transition.
    """
    key = (state, event)
    if key not in _TRANSITIONS:
        raise StateMachineError(
            f"Invalid transition: {state.value} + {event.value}"
        )
    return _TRANSITIONS[key]


def is_terminal(state: OrderState) -> bool:
    """A terminal state cannot transition further (FILLED/CANCELLED/REJECTED)."""
    return state in {OrderState.FILLED, OrderState.CANCELLED, OrderState.REJECTED}


def is_active(state: OrderState) -> bool:
    """An active state has an open order at the broker (SENT or PARTIAL)."""
    return state in {OrderState.SENT, OrderState.PARTIAL}

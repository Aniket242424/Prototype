"""DTOs internal to the Execution Engine."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from trading_agent.core.constants import OrderType
from trading_agent.execution.state_machine import OrderState


class OrderRequest(BaseModel):
    """What the Execution Engine sends to the broker (real or paper)."""

    model_config = ConfigDict(frozen=True)

    instrument_key: str
    qty: int                              # In contracts
    order_type: OrderType                 # LIMIT or MARKET
    price: Decimal | None = None          # None for MARKET
    side: str                              # "BUY" or "SELL"


class OrderResponse(BaseModel):
    """Broker's response after submission."""

    model_config = ConfigDict(frozen=True)

    broker_order_id: str
    accepted: bool
    rejection_reason: str | None = None
    ts: datetime


class FillEvent(BaseModel):
    """Broker reports a fill (partial or complete)."""

    model_config = ConfigDict(frozen=True)

    broker_order_id: str
    filled_qty: int                       # Quantity filled in THIS event
    fill_price: Decimal                    # Average price for this fill
    cumulative_filled: int                 # Total filled so far on this order
    is_complete: bool                      # True when cumulative_filled == request.qty
    ts: datetime


class OrderRecord(BaseModel):
    """Live tracking of an order through its lifecycle."""

    model_config = ConfigDict(frozen=True)

    db_id: int
    broker_order_id: str | None
    request: OrderRequest
    state: OrderState
    cumulative_filled: int = 0
    avg_fill_price: Decimal | None = None
    reference_mid: Decimal                 # Mid at submission — for slippage math
    estimated_slippage_bps: float
    submitted_at: datetime | None = None
    finalized_at: datetime | None = None
    rejection_reason: str | None = None
    is_paper: bool

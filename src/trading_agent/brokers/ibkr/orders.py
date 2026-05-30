"""
IBKR order placement — Phase G.2.2.

Thin async wrapper for placing/cancelling orders + tracking fills against
the IB Gateway. Designed for paper trading first; the same code works
on a live account once it's funded.

Order flow:
    1. Resolve InstrumentSpec → qualified Contract
    2. Build ib_async Order (MKT, LMT, STP) with quantity + side
    3. ib.placeOrder() → returns Trade object
    4. Await fills via Trade.filledEvent — or wait_for_fill() with timeout

Safety:
- Refuses to place orders if Read-Only API is enabled (would error out
  anyway, but we surface the message clearly)
- Does NOT enable any market-data prerequisites — the calling code is
  responsible for making sure the contract is qualified
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Literal

from ib_async.contract import Contract
from ib_async.order import LimitOrder, MarketOrder, Order, StopOrder, Trade

from trading_agent.brokers.ibkr.client import IBKRClient
from trading_agent.brokers.ibkr.instruments import InstrumentSpec, by_symbol
from trading_agent.core.logging import get_logger

log = get_logger(__name__)


Side = Literal["BUY", "SELL"]


@dataclass(frozen=True)
class PlacedOrder:
    """Lightweight handle returned after placing an order. Has the underlying ib_async Trade."""
    symbol: str
    side: Side
    quantity: float
    order_type: str
    limit_price: float | None
    stop_price: float | None
    trade: Trade  # ib_async Trade object — has .order, .orderStatus, .fills, .filledEvent

    @property
    def order_id(self) -> int:
        return self.trade.order.orderId

    @property
    def status(self) -> str:
        return self.trade.orderStatus.status

    @property
    def filled_qty(self) -> float:
        return float(self.trade.orderStatus.filled or 0)

    @property
    def avg_fill_price(self) -> float:
        return float(self.trade.orderStatus.avgFillPrice or 0)

    @property
    def is_filled(self) -> bool:
        return self.status == "Filled"

    @property
    def is_cancelled(self) -> bool:
        return self.status in ("Cancelled", "ApiCancelled")


def _build_order(
    side: Side,
    quantity: float,
    order_type: str,
    limit_price: float | None,
    stop_price: float | None,
) -> Order:
    """Build the ib_async Order object."""
    ot = order_type.upper()
    if ot == "MKT":
        return MarketOrder(side, quantity)
    if ot == "LMT":
        if limit_price is None:
            raise ValueError("LMT order requires limit_price")
        return LimitOrder(side, quantity, limit_price)
    if ot == "STP":
        if stop_price is None:
            raise ValueError("STP order requires stop_price")
        return StopOrder(side, quantity, stop_price)
    raise ValueError(f"Unsupported order type: {order_type}. Use MKT, LMT, or STP.")


async def place_order(
    client: IBKRClient,
    symbol: str,
    side: Side,
    quantity: float,
    order_type: str = "MKT",
    limit_price: float | None = None,
    stop_price: float | None = None,
    transmit: bool = True,
) -> PlacedOrder:
    """
    Place an order against the configured account.

    Args:
        client: connected IBKRClient (call connect() first)
        symbol: internal symbol — must be in instruments.UNIVERSE
        side: "BUY" or "SELL"
        quantity: number of shares (stocks) or contracts (futures)
        order_type: "MKT" | "LMT" | "STP"
        limit_price: required for LMT
        stop_price:  required for STP
        transmit: if False, order is staged but not sent (useful for testing)

    Returns:
        PlacedOrder — wraps the ib_async Trade. Use wait_for_fill() to
        block until filled.

    Raises:
        IBKRConnectionError: if not connected
        KeyError: if symbol unknown
        ValueError: if order params are inconsistent
        Exception: passes through any IBKR API errors
    """
    if not client.connected:
        from trading_agent.brokers.ibkr.client import IBKRConnectionError
        raise IBKRConnectionError("Client not connected. Call connect() first.")

    if client.config.readonly:
        raise RuntimeError(
            "Cannot place orders — IBKR_READONLY=true in env. "
            "Set IBKR_READONLY=false (and uncheck Read-Only API in Gateway settings)."
        )

    spec: InstrumentSpec = by_symbol(symbol)

    # Qualify (resolves conId, exchange, etc.). Required before placing.
    contract: Contract = await client.qualify_contract(spec.contract)

    order = _build_order(side, quantity, order_type, limit_price, stop_price)
    order.transmit = transmit

    log.info(
        "ibkr.order.placing",
        symbol=symbol,
        side=side,
        quantity=quantity,
        order_type=order_type.upper(),
        limit_price=limit_price,
        stop_price=stop_price,
        contract_conId=contract.conId,
    )

    trade: Trade = client.ib.placeOrder(contract, order)
    log.info("ibkr.order.placed", order_id=trade.order.orderId, status=trade.orderStatus.status)

    return PlacedOrder(
        symbol=symbol,
        side=side,
        quantity=quantity,
        order_type=order_type.upper(),
        limit_price=limit_price,
        stop_price=stop_price,
        trade=trade,
    )


async def wait_for_fill(
    placed: PlacedOrder,
    timeout_sec: float = 30.0,
    poll_sec: float = 0.5,
) -> PlacedOrder:
    """
    Wait until the order reaches a terminal state (Filled, Cancelled, etc).
    Returns the same PlacedOrder (its underlying Trade is mutated in place).
    """
    terminal_statuses = {"Filled", "Cancelled", "ApiCancelled", "Inactive"}
    elapsed = 0.0
    while elapsed < timeout_sec:
        if placed.status in terminal_statuses:
            return placed
        await asyncio.sleep(poll_sec)
        elapsed += poll_sec
    return placed  # caller checks status


async def cancel(client: IBKRClient, placed: PlacedOrder) -> PlacedOrder:
    """Cancel an open order. No-op if already terminal."""
    if placed.is_filled or placed.is_cancelled:
        return placed
    client.ib.cancelOrder(placed.trade.order)
    log.info("ibkr.order.cancel_requested", order_id=placed.order_id)
    return placed


async def open_positions(client: IBKRClient) -> list[dict]:
    """Return current open positions for the configured account."""
    if not client.connected:
        from trading_agent.brokers.ibkr.client import IBKRConnectionError
        raise IBKRConnectionError("Client not connected.")
    positions = client.ib.positions()
    return [
        {
            "account": p.account,
            "symbol": p.contract.symbol,
            "secType": p.contract.secType,
            "exchange": p.contract.exchange,
            "currency": p.contract.currency,
            "position": float(p.position),
            "avg_cost": float(p.avgCost),
        }
        for p in positions
    ]

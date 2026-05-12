"""Tests for the paper-mode broker simulator."""
from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from trading_agent.core.constants import OrderType
from trading_agent.execution.broker import MarketContext, PaperBroker
from trading_agent.execution.dtos import OrderRequest


@pytest.fixture
def broker() -> PaperBroker:
    return PaperBroker(seed=42)   # deterministic


@pytest.fixture
def market() -> MarketContext:
    return MarketContext(
        bid=Decimal("100.0"),
        ask=Decimal("100.2"),
        bid_qty=1000,
        ask_qty=1000,
        ltp=Decimal("100.1"),
    )


async def test_market_buy_fills_immediately(broker, market):
    req = OrderRequest(
        instrument_key="NSE_FO|TEST",
        qty=25,
        order_type=OrderType.MARKET,
        side="BUY",
    )
    resp = await broker.submit(req, market)
    assert resp.accepted
    assert resp.broker_order_id.startswith("PAPER-")

    fills = await broker.poll_fills(resp.broker_order_id)
    assert len(fills) == 1
    assert fills[0].is_complete is True
    assert fills[0].filled_qty == 25
    # BUY at ask + noise → at or slightly above 100.2
    assert fills[0].fill_price >= Decimal("100.2")


async def test_market_sell_fills_at_bid_or_below(broker, market):
    req = OrderRequest(
        instrument_key="NSE_FO|TEST",
        qty=25,
        order_type=OrderType.MARKET,
        side="SELL",
    )
    resp = await broker.submit(req, market)
    fills = await broker.poll_fills(resp.broker_order_id)
    assert len(fills) == 1
    assert fills[0].fill_price <= Decimal("100.0")


async def test_limit_buy_at_ask_fills(broker, market):
    """LIMIT BUY at ask should fill (we cross the spread)."""
    req = OrderRequest(
        instrument_key="NSE_FO|TEST",
        qty=25,
        order_type=OrderType.LIMIT,
        price=Decimal("100.2"),     # at ask → fills
        side="BUY",
    )
    resp = await broker.submit(req, market)
    fills = await broker.poll_fills(resp.broker_order_id)
    assert len(fills) >= 1
    # Cumulative should reach 25
    cum = fills[-1].cumulative_filled
    assert cum == 25


async def test_limit_buy_at_mid_does_not_fill(broker, market):
    """LIMIT BUY at mid (below ask) should NOT fill in our simple model."""
    req = OrderRequest(
        instrument_key="NSE_FO|TEST",
        qty=25,
        order_type=OrderType.LIMIT,
        price=Decimal("100.1"),     # mid, below ask 100.2
        side="BUY",
    )
    resp = await broker.submit(req, market)
    fills = await broker.poll_fills(resp.broker_order_id)
    assert fills == []


async def test_cancel_open_order(broker, market):
    req = OrderRequest(
        instrument_key="NSE_FO|TEST",
        qty=25,
        order_type=OrderType.LIMIT,
        price=Decimal("100.1"),     # won't fill
        side="BUY",
    )
    resp = await broker.submit(req, market)
    cancelled = await broker.cancel(resp.broker_order_id)
    assert cancelled is True


async def test_cancel_unknown_order_returns_false(broker):
    cancelled = await broker.cancel("NONEXISTENT")
    assert cancelled is False


async def test_paper_broker_flag():
    b = PaperBroker()
    assert b.is_paper is True

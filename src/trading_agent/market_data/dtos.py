"""Typed DTOs that flow between market_data submodules and downstream consumers."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class Tick(BaseModel):
    """A single trade/quote update for one instrument."""

    model_config = ConfigDict(frozen=True)

    instrument_key: str
    ts: datetime                         # exchange timestamp (IST-aware)
    ltp: Decimal
    bid: Decimal | None = None
    ask: Decimal | None = None
    bid_qty: int | None = None
    ask_qty: int | None = None
    volume: int | None = None
    oi: int | None = None
    cp: Decimal | None = None            # previous close (LTPC mode emits this)


class FeedFrame(BaseModel):
    """A decoded WS frame containing multiple tick updates."""

    model_config = ConfigDict(frozen=True)

    feed_type: str                       # initial_feed / live_feed / market_info
    current_ts: datetime
    ticks: list[Tick]

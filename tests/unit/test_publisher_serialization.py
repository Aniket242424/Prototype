"""Test that Tick → JSON serialization round-trips cleanly."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import orjson

from trading_agent.market_data.dtos import Tick
from trading_agent.market_data.publisher import _tick_to_payload


def test_tick_payload_roundtrip():
    t = Tick(
        instrument_key="NSE_INDEX|Nifty 50",
        ts=datetime(2026, 5, 9, 9, 30, 0, tzinfo=timezone.utc),
        ltp=Decimal("22500.55"),
        cp=Decimal("22480.10"),
        bid=Decimal("22500.40"),
        ask=Decimal("22500.70"),
        bid_qty=100,
        ask_qty=200,
    )
    payload = _tick_to_payload(t)
    decoded = orjson.loads(payload)
    assert decoded["instrument_key"] == "NSE_INDEX|Nifty 50"
    assert decoded["ltp"] == "22500.55"
    assert decoded["cp"] == "22480.10"
    assert decoded["bid_qty"] == 100
    assert decoded["volume"] is None


def test_tick_payload_minimal_fields():
    t = Tick(
        instrument_key="NSE_INDEX|Nifty Bank",
        ts=datetime(2026, 5, 9, 9, 30, 0, tzinfo=timezone.utc),
        ltp=Decimal("48000"),
    )
    decoded = orjson.loads(_tick_to_payload(t))
    assert decoded["bid"] is None
    assert decoded["ask"] is None
    assert decoded["oi"] is None

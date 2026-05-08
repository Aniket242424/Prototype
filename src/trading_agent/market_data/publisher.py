"""
Redis pubsub publisher for ticks.

Channel: `md:tick:{instrument_key}` — one channel per instrument so consumers
subscribe selectively (the Regime Engine on NIFTY doesn't need BANKNIFTY ticks).

Payload: orjson-serialized Tick — extremely fast.
"""
from __future__ import annotations

from collections.abc import Sequence

import orjson
from redis.asyncio import Redis

from trading_agent.core.constants import CHAN_TICK
from trading_agent.core.logging import get_logger
from trading_agent.market_data.dtos import Tick

log = get_logger(__name__)


def _tick_to_payload(t: Tick) -> bytes:
    """Serialize Tick to bytes via orjson (handles Decimal + datetime)."""
    return orjson.dumps(
        {
            "instrument_key": t.instrument_key,
            "ts": t.ts.isoformat(),
            "ltp": str(t.ltp),
            "bid": str(t.bid) if t.bid is not None else None,
            "ask": str(t.ask) if t.ask is not None else None,
            "bid_qty": t.bid_qty,
            "ask_qty": t.ask_qty,
            "volume": t.volume,
            "oi": t.oi,
            "cp": str(t.cp) if t.cp is not None else None,
        }
    )


class TickPublisher:
    def __init__(self, redis: Redis):
        self._redis = redis
        self._published = 0

    async def publish(self, ticks: Sequence[Tick]) -> None:
        if not ticks:
            return
        async with self._redis.pipeline(transaction=False) as pipe:
            for t in ticks:
                channel = CHAN_TICK.format(instrument_key=t.instrument_key)
                pipe.publish(channel, _tick_to_payload(t))
            await pipe.execute()
        self._published += len(ticks)

    @property
    def published_total(self) -> int:
        return self._published

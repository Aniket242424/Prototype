"""
Per-instrument freshness tracker.

Maintains the last-tick-time for each instrument in Redis (`md:last_tick_ts:{ik}`)
and exposes an `is_stale()` query the Risk Engine consults before approving an
entry. Outside market hours, staleness is expected — callers should check
market-open status before acting on staleness.

Why Redis (not in-process)? Multiple consumer processes (regime, opportunity,
risk) may want to read freshness independent of the producer process.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Iterable

from redis.asyncio import Redis

from trading_agent.core.logging import get_logger
from trading_agent.core.time_utils import now_ist
from trading_agent.market_data.dtos import Tick

log = get_logger(__name__)

_KEY_PREFIX = "md:last_tick_ts:"


class StalenessTracker:
    def __init__(self, redis: Redis, max_age_sec: float = 5.0):
        self._redis = redis
        self._max_age_sec = max_age_sec

    async def update(self, ticks: Iterable[Tick]) -> None:
        now_iso = now_ist().isoformat()
        async with self._redis.pipeline(transaction=False) as pipe:
            for t in ticks:
                pipe.set(f"{_KEY_PREFIX}{t.instrument_key}", now_iso)
            await pipe.execute()

    async def last_tick_at(self, instrument_key: str) -> datetime | None:
        v = await self._redis.get(f"{_KEY_PREFIX}{instrument_key}")
        if v is None:
            return None
        return datetime.fromisoformat(v.decode())

    async def is_stale(self, instrument_key: str) -> bool:
        ts = await self.last_tick_at(instrument_key)
        if ts is None:
            return True
        return (now_ist() - ts) > timedelta(seconds=self._max_age_sec)

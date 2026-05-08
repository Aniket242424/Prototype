"""
Per-underlying rolling tick buffer.

Hydrated on startup from `market_data_ticks` (last N minutes), then kept
fresh by subscribing to `md:tick:{instrument_key}` pubsub from Redis.

Provides a thread-safe (asyncio-safe) snapshot DataFrame view for
indicator computation.
"""
from __future__ import annotations

import asyncio
import json
from collections import deque
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Iterable

import pandas as pd
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from trading_agent.core.constants import CHAN_TICK
from trading_agent.core.logging import get_logger
from trading_agent.infrastructure.models import MarketDataTickRow

log = get_logger(__name__)


class TickBuffer:
    """One buffer per underlying. Stores raw ticks; aggregates to candles on demand."""

    def __init__(self, instrument_key: str, max_minutes: int = 90):
        self._key = instrument_key
        self._buf: deque[dict] = deque()
        self._max_age = timedelta(minutes=max_minutes)
        self._lock = asyncio.Lock()

    @property
    def instrument_key(self) -> str:
        return self._key

    async def hydrate_from_db(self, session_factory: async_sessionmaker) -> int:
        cutoff = datetime.now(tz=timezone.utc) - self._max_age
        async with session_factory() as session:
            rows = (await session.execute(
                select(MarketDataTickRow)
                .where(MarketDataTickRow.instrument_key == self._key)
                .where(MarketDataTickRow.ts >= cutoff)
                .order_by(MarketDataTickRow.ts.asc())
            )).scalars().all()
        async with self._lock:
            self._buf.clear()
            for r in rows:
                self._buf.append({
                    "ts": r.ts,
                    "ltp": float(r.ltp),
                    "volume": int(r.volume) if r.volume is not None else 0,
                })
        log.info("tick_buffer.hydrated", instrument=self._key, ticks=len(rows))
        return len(rows)

    async def add(self, ts: datetime, ltp: float, volume: int = 0) -> None:
        async with self._lock:
            self._buf.append({"ts": ts, "ltp": ltp, "volume": volume})
            self._evict_old_locked()

    def _evict_old_locked(self) -> None:
        if not self._buf:
            return
        cutoff = datetime.now(tz=timezone.utc) - self._max_age
        while self._buf and self._buf[0]["ts"] < cutoff:
            self._buf.popleft()

    async def to_dataframe(self) -> pd.DataFrame:
        async with self._lock:
            self._evict_old_locked()
            data = list(self._buf)
        if not data:
            return pd.DataFrame(columns=["ts", "ltp", "volume"])
        df = pd.DataFrame(data)
        df["ts"] = pd.to_datetime(df["ts"], utc=True)
        return df

    async def latest_price(self) -> float | None:
        async with self._lock:
            return self._buf[-1]["ltp"] if self._buf else None


class TickBufferPool:
    """A buffer per underlying, fed by a single Redis pubsub subscription."""

    def __init__(
        self,
        redis: Redis,
        session_factory: async_sessionmaker,
        instrument_keys: Iterable[str],
        max_minutes: int = 90,
    ):
        self._redis = redis
        self._session_factory = session_factory
        self._buffers: dict[str, TickBuffer] = {
            k: TickBuffer(k, max_minutes=max_minutes) for k in instrument_keys
        }
        self._stop = asyncio.Event()
        self._listener_task: asyncio.Task | None = None

    def get(self, instrument_key: str) -> TickBuffer | None:
        return self._buffers.get(instrument_key)

    @property
    def buffers(self) -> dict[str, TickBuffer]:
        return self._buffers

    async def start(self) -> None:
        # Hydrate all in parallel
        await asyncio.gather(*(
            buf.hydrate_from_db(self._session_factory) for buf in self._buffers.values()
        ))
        self._stop.clear()
        self._listener_task = asyncio.create_task(self._listen(), name="tick-buffer-listener")

    async def stop(self) -> None:
        self._stop.set()
        if self._listener_task:
            self._listener_task.cancel()
            try:
                await self._listener_task
            except asyncio.CancelledError:
                pass

    async def _listen(self) -> None:
        pubsub = self._redis.pubsub()
        channels = [CHAN_TICK.format(instrument_key=k) for k in self._buffers]
        await pubsub.subscribe(*channels)
        log.info("tick_buffer.listening", channels=len(channels))
        try:
            while not self._stop.is_set():
                msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if msg is None or msg.get("type") != "message":
                    continue
                channel = msg["channel"].decode() if isinstance(msg["channel"], bytes) else msg["channel"]
                # channel is "md:tick:NSE_INDEX|Nifty 50" — extract instrument_key
                instrument_key = channel.split(":", 2)[2] if channel.count(":") >= 2 else None
                buf = self._buffers.get(instrument_key) if instrument_key else None
                if buf is None:
                    continue
                try:
                    payload = json.loads(msg["data"])
                    ts = datetime.fromisoformat(payload["ts"])
                    ltp = float(Decimal(payload["ltp"]))
                    volume = int(payload.get("volume") or 0)
                    await buf.add(ts, ltp, volume)
                except Exception as e:
                    log.warning("tick_buffer.parse_failed", error=str(e))
        finally:
            await pubsub.unsubscribe()
            await pubsub.aclose()

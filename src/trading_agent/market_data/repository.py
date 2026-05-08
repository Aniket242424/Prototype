"""
Tick persistence with batched bulk inserts.

The WS feed produces 5–50 ticks/sec across our 5 underlyings. Writing one row
per tick burns DB IO. We buffer in-memory and flush every `flush_interval_sec`
seconds OR every `flush_size` ticks, whichever first.

The flush is a single `INSERT ... VALUES (...), (...), ...` statement using
asyncpg's `executemany` semantics under SQLAlchemy. Indexes on
(instrument_key, ts) make per-instrument queries fast for downstream consumers.
"""
from __future__ import annotations

import asyncio
from collections.abc import Sequence
from decimal import Decimal

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import async_sessionmaker

from trading_agent.core.logging import get_logger
from trading_agent.infrastructure.models import MarketDataTickRow
from trading_agent.market_data.dtos import Tick

log = get_logger(__name__)


class TickRepository:
    def __init__(
        self,
        session_factory: async_sessionmaker,
        flush_size: int = 200,
        flush_interval_sec: float = 1.0,
    ):
        self._session_factory = session_factory
        self._flush_size = flush_size
        self._flush_interval = flush_interval_sec
        self._buf: list[Tick] = []
        self._lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._flusher_task: asyncio.Task | None = None
        self._total_persisted = 0

    async def start(self) -> None:
        self._stop.clear()
        self._flusher_task = asyncio.create_task(self._periodic_flush(), name="tick-flusher")

    async def stop(self) -> None:
        self._stop.set()
        if self._flusher_task:
            self._flusher_task.cancel()
            try:
                await self._flusher_task
            except asyncio.CancelledError:
                pass
        await self._flush()

    async def append(self, ticks: Sequence[Tick]) -> None:
        if not ticks:
            return
        async with self._lock:
            self._buf.extend(ticks)
            if len(self._buf) >= self._flush_size:
                await self._flush_locked()

    async def _periodic_flush(self) -> None:
        try:
            while not self._stop.is_set():
                await asyncio.sleep(self._flush_interval)
                async with self._lock:
                    if self._buf:
                        await self._flush_locked()
        except asyncio.CancelledError:
            pass

    async def _flush(self) -> None:
        async with self._lock:
            await self._flush_locked()

    async def _flush_locked(self) -> None:
        if not self._buf:
            return
        rows = [
            {
                "instrument_key": t.instrument_key,
                "ts": t.ts,
                "ltp": t.ltp,
                "bid": t.bid,
                "ask": t.ask,
                "bid_qty": t.bid_qty,
                "ask_qty": t.ask_qty,
                "volume": t.volume,
                "oi": t.oi,
            }
            for t in self._buf
        ]
        n = len(rows)
        try:
            async with self._session_factory() as session:
                await session.execute(insert(MarketDataTickRow), rows)
                await session.commit()
        except Exception as e:
            log.error("tick_repo.flush_failed", error=str(e), buffered=n)
            raise
        self._total_persisted += n
        self._buf.clear()
        log.debug("tick_repo.flushed", count=n, total_persisted=self._total_persisted)

    @property
    def total_persisted(self) -> int:
        return self._total_persisted

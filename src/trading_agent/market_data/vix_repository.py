"""
India VIX persistence — separate from regular ticks because the schema is
simpler (one value per timestamp) and downstream consumers (Regime Engine,
Risk Engine vol-kill) query it at coarser granularity than tick stream.

VIX ticks come through the same Upstox WS feed but are routed here by
matching on instrument_key. Persists to `india_vix` table; publishes on
Redis channel `md:vix`.
"""
from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal

import orjson
from redis.asyncio import Redis
from sqlalchemy import insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import async_sessionmaker

from trading_agent.core.constants import CHAN_VIX
from trading_agent.core.logging import get_logger
from trading_agent.infrastructure.models import IndiaVixRow
from trading_agent.market_data.dtos import Tick

log = get_logger(__name__)


class VixRepository:
    """Buffer + flush VIX values to Postgres + publish to Redis."""

    def __init__(
        self,
        session_factory: async_sessionmaker,
        redis: Redis,
        flush_interval_sec: float = 5.0,
    ):
        self._session_factory = session_factory
        self._redis = redis
        self._flush_interval = flush_interval_sec
        self._buf: list[tuple[datetime, Decimal]] = []
        self._lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._total_persisted = 0
        self._latest_value: Decimal | None = None

    async def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(self._periodic_flush(), name="vix-flusher")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self._flush()

    async def append(self, ticks: Sequence[Tick]) -> None:
        if not ticks:
            return
        async with self._lock:
            for t in ticks:
                self._buf.append((t.ts, t.ltp))
                self._latest_value = t.ltp
            # Publish each VIX update immediately so consumers can react fast.
        await self._publish(ticks)

    async def _publish(self, ticks: Sequence[Tick]) -> None:
        async with self._redis.pipeline(transaction=False) as pipe:
            for t in ticks:
                pipe.publish(
                    CHAN_VIX,
                    orjson.dumps({"ts": t.ts.isoformat(), "value": str(t.ltp)}),
                )
            # Cache latest value for cheap reads (Regime Engine, Risk vol-kill).
            if ticks:
                pipe.set("md:vix:latest", str(ticks[-1].ltp), ex=600)
            await pipe.execute()

    async def _periodic_flush(self) -> None:
        try:
            while not self._stop.is_set():
                await asyncio.sleep(self._flush_interval)
                await self._flush()
        except asyncio.CancelledError:
            pass

    async def _flush(self) -> None:
        async with self._lock:
            if not self._buf:
                return
            rows = [{"ts": ts, "value": value} for ts, value in self._buf]
            n = len(rows)
            try:
                async with self._session_factory() as session:
                    # Use ON CONFLICT DO NOTHING since `ts` is the PK and Upstox
                    # may resend the same timestamp on initial-feed snapshots.
                    stmt = pg_insert(IndiaVixRow).values(rows)
                    stmt = stmt.on_conflict_do_nothing(index_elements=["ts"])
                    await session.execute(stmt)
                    await session.commit()
            except Exception as e:
                log.error("vix_repo.flush_failed", error=str(e), buffered=n)
                raise
            self._total_persisted += n
            self._buf.clear()
            log.debug("vix_repo.flushed", count=n, total_persisted=self._total_persisted, latest=str(self._latest_value))

    @property
    def latest_value(self) -> Decimal | None:
        return self._latest_value

    @property
    def total_persisted(self) -> int:
        return self._total_persisted

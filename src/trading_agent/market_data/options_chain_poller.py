"""
Options chain snapshot poller.

Polls `/v2/option/chain` for each enabled underlying at the active expiry,
every `interval_sec` seconds. Persists each snapshot to
`options_chain_snapshots` (JSONB) and publishes on Redis channel
`md:chain:{underlying_name}` for downstream consumers (Phase 2 Options Intel).

Why REST and not WS? Upstox WS supports streaming individual contracts but
doesn't provide a "chain" stream. Polling at 30s during market hours is the
standard pattern and is cheap.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Iterable

import orjson
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import async_sessionmaker

from trading_agent.core.config import InstrumentConfig
from trading_agent.core.constants import CHAN_CHAIN
from trading_agent.core.logging import get_logger
from trading_agent.core.time_utils import is_market_open, now_ist
from trading_agent.infrastructure.models import OptionsChainSnapshotRow
from trading_agent.market_data.expiry_resolver import ExpiryResolver
from trading_agent.market_data.upstox_rest import UpstoxRestClient

log = get_logger(__name__)


class OptionsChainPoller:
    def __init__(
        self,
        rest: UpstoxRestClient,
        resolver: ExpiryResolver,
        session_factory: async_sessionmaker,
        redis: Redis,
        instruments: Iterable[InstrumentConfig],
        interval_sec: float = 30.0,
        idle_interval_sec: float = 300.0,
    ):
        self._rest = rest
        self._resolver = resolver
        self._session_factory = session_factory
        self._redis = redis
        self._instruments = list(instruments)
        self._interval_sec = interval_sec        # during market hours
        self._idle_interval_sec = idle_interval_sec  # off-hours
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._snapshots_persisted = 0

    async def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="chain-poller")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    @property
    def snapshots_persisted(self) -> int:
        return self._snapshots_persisted

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.error("chain_poller.iteration_failed", error=str(e))
            interval = self._interval_sec if is_market_open() else self._idle_interval_sec
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    async def _poll_once(self) -> None:
        for inst in self._instruments:
            if not inst.enabled:
                continue
            try:
                expiry = await self._resolver.active_expiry(inst.upstox_instrument_key)
                if expiry is None:
                    log.warning("chain_poller.no_active_expiry", underlying=inst.name)
                    continue
                rows = await self._rest.option_chain(inst.upstox_instrument_key, expiry)
                if not rows:
                    log.debug("chain_poller.empty_chain", underlying=inst.name, expiry=expiry.isoformat())
                    continue
                await self._persist_and_publish(inst, expiry, rows)
            except Exception as e:
                log.warning("chain_poller.fetch_failed", underlying=inst.name, error=str(e))

    async def _persist_and_publish(
        self, inst: InstrumentConfig, expiry: date, rows: list[dict]
    ) -> None:
        ts = now_ist()
        # Pull underlying spot from any row (all rows in the chain share it).
        spot_raw = rows[0].get("underlying_spot_price") if rows else None
        underlying_spot = Decimal(str(spot_raw)) if spot_raw is not None else Decimal("0")

        # The full chain is large (100+ strikes × CE/PE); store as-is in JSONB.
        # Phase 2 reads this, computes Greeks-based intel, persists derived metrics
        # in a smaller summary row.
        snapshot = {
            "ts": ts.isoformat(),
            "underlying_name": inst.name,
            "underlying_instrument_key": inst.upstox_instrument_key,
            "expiry": expiry.isoformat(),
            "underlying_spot": str(underlying_spot),
            "strike_count": len(rows),
            "strikes": rows,
        }

        # Persist
        async with self._session_factory() as session:
            session.add(OptionsChainSnapshotRow(
                underlying=inst.name,
                expiry=datetime.combine(expiry, datetime.min.time(), tzinfo=timezone.utc),
                ts=ts,
                underlying_spot=underlying_spot,
                chain=snapshot,
            ))
            await session.commit()
        self._snapshots_persisted += 1

        # Publish
        channel = CHAN_CHAIN.format(underlying=inst.name)
        await self._redis.publish(channel, orjson.dumps({
            "underlying": inst.name,
            "expiry": expiry.isoformat(),
            "ts": ts.isoformat(),
            "spot": str(underlying_spot),
            "strike_count": len(rows),
        }))

        log.info(
            "chain_poller.snapshot",
            underlying=inst.name,
            expiry=expiry.isoformat(),
            strikes=len(rows),
            spot=str(underlying_spot),
        )

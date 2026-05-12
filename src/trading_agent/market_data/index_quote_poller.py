"""
INDEX quote poller — pragmatic workaround for Upstox WS not streaming indices.

Indices (NIFTY, BANKNIFTY, etc.) are computed values, not traded instruments,
so Upstox V3 WS doesn't push live LTP updates after the initial-feed snapshot.

This poller hits the REST `/v2/market-quote/ltp` endpoint every `interval_sec`
(default 2s) with all 5 underlying keys in ONE call. Synthesizes Tick objects
and routes them through the same persistence + publish + staleness path as
WS ticks would have.

2-second granularity is fine for our use case — regime engine works on
1-minute candles. Adds ~5 KB/s of bandwidth and ~1 RPS of Upstox API quota.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Iterable

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import async_sessionmaker

from trading_agent.core.config import InstrumentConfig
from trading_agent.core.logging import get_logger
from trading_agent.core.time_utils import is_market_open, now_ist
from trading_agent.market_data.dtos import Tick
from trading_agent.market_data.publisher import TickPublisher
from trading_agent.market_data.repository import TickRepository
from trading_agent.market_data.staleness import StalenessTracker
from trading_agent.market_data.upstox_rest import UpstoxRestClient

log = get_logger(__name__)


class IndexQuotePoller:
    """Poll multi-quote LTP for indices; route results as synthetic ticks."""

    def __init__(
        self,
        rest: UpstoxRestClient,
        repository: TickRepository,
        publisher: TickPublisher,
        staleness: StalenessTracker,
        instruments: Iterable[InstrumentConfig],
        interval_sec: float = 2.0,
        idle_interval_sec: float = 30.0,
    ):
        self._rest = rest
        self._repo = repository
        self._publisher = publisher
        self._staleness = staleness
        self._instruments = [i for i in instruments if i.enabled]
        self._interval_sec = interval_sec
        self._idle_interval_sec = idle_interval_sec
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._ticks_polled = 0
        self._poll_errors = 0

    @property
    def ticks_polled(self) -> int:
        return self._ticks_polled

    async def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="index-quote-poller")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        keys = [i.upstox_instrument_key for i in self._instruments]
        # Upstox returns keys with ':' separator (not '|'). Build mapping for lookup.
        # Example: input "NSE_INDEX|Nifty 50" → response key "NSE_INDEX:Nifty 50"
        key_to_orig = {k.replace("|", ":"): k for k in keys}

        while not self._stop.is_set():
            try:
                data = await self._rest.multi_quote_ltp(keys)
                if data:
                    await self._handle_response(data, key_to_orig)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._poll_errors += 1
                log.warning("index_poller.failed", error=str(e), total_errors=self._poll_errors)

            interval = self._interval_sec if is_market_open() else self._idle_interval_sec
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    async def _handle_response(
        self, data: dict, key_to_orig: dict[str, str]
    ) -> None:
        ts = now_ist()
        ticks: list[Tick] = []
        for resp_key, payload in data.items():
            orig_key = key_to_orig.get(resp_key)
            if orig_key is None:
                # Upstox sometimes returns the same key format we sent; check both
                orig_key = key_to_orig.get(resp_key.replace(":", "|"))
            if orig_key is None:
                continue
            ltp = payload.get("last_price")
            if ltp is None or ltp == 0:
                continue
            ticks.append(Tick(
                instrument_key=orig_key,
                ts=ts,
                ltp=Decimal(str(ltp)),
            ))
        if not ticks:
            return
        self._ticks_polled += len(ticks)
        # Route through the SAME path as WS ticks would take
        await asyncio.gather(
            self._repo.append(ticks),
            self._publisher.publish(ticks),
            self._staleness.update(ticks),
        )

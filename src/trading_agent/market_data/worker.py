"""
Market Data worker — long-running async process.

Pipeline:
    Upstox v3 WS feed
        → decode protobuf into Ticks
        → batch-persist to Postgres
        → publish to Redis (md:tick:{instrument_key})
        → update staleness tracker

Run:
    py -3.14 -m trading_agent.market_data.worker
or via Docker Compose service `market_data_worker`.
"""
from __future__ import annotations

import asyncio
import signal

import click

from trading_agent.core.config import get_instruments_config, get_settings
from trading_agent.core.constants import INDIA_VIX_INSTRUMENT_KEY
from trading_agent.core.logging import configure_logging, get_logger
from trading_agent.core.time_utils import is_market_open, now_ist
from trading_agent.infrastructure.db import SessionLocal
from trading_agent.infrastructure.redis_client import make_redis
from trading_agent.market_data.expiry_resolver import ExpiryResolver
from trading_agent.market_data.options_chain_poller import OptionsChainPoller
from trading_agent.market_data.publisher import TickPublisher
from trading_agent.market_data.repository import TickRepository
from trading_agent.market_data.staleness import StalenessTracker
from trading_agent.market_data.upstox_rest import UpstoxRestClient
from trading_agent.market_data.upstox_ws import UpstoxWebSocketClient
from trading_agent.market_data.vix_repository import VixRepository

log = get_logger(__name__)


async def run() -> None:
    settings = get_settings()
    instruments = get_instruments_config()
    enabled_keys = [
        i.upstox_instrument_key for i in instruments.instruments if i.enabled
    ]
    # Subscribe to underlyings + India VIX on the same feed.
    subscribe_keys = enabled_keys + [INDIA_VIX_INSTRUMENT_KEY]
    log.info(
        "worker.starting",
        env=settings.app_env,
        instrument_count=len(enabled_keys),
        vix_subscribed=True,
        market_open=is_market_open(),
        ist_now=now_ist().isoformat(),
    )

    rest = UpstoxRestClient(settings)
    ws = UpstoxWebSocketClient(rest, instrument_keys=subscribe_keys, mode="ltpc")

    redis = make_redis()
    repo = TickRepository(SessionLocal, flush_size=200, flush_interval_sec=1.0)
    vix_repo = VixRepository(SessionLocal, redis, flush_interval_sec=5.0)
    publisher = TickPublisher(redis)
    staleness = StalenessTracker(redis, max_age_sec=5.0)
    expiry_resolver = ExpiryResolver(rest, cache_ttl_sec=300)
    chain_poller = OptionsChainPoller(
        rest=rest,
        resolver=expiry_resolver,
        session_factory=SessionLocal,
        redis=redis,
        instruments=instruments.instruments,
        interval_sec=30.0,
        idle_interval_sec=300.0,
    )

    await repo.start()
    await vix_repo.start()
    await chain_poller.start()

    # Worker heartbeat (consumed by dashboard) — TTL > heartbeat interval so a
    # crash makes the key expire and dashboard shows the worker as down.
    async def _heartbeat() -> None:
        try:
            while True:
                await redis.set("worker:market_data:heartbeat", now_ist().isoformat(), ex=10)
                await asyncio.sleep(3)
        except asyncio.CancelledError:
            pass

    heartbeat_task = asyncio.create_task(_heartbeat(), name="md-heartbeat")

    # Graceful shutdown plumbing
    stop_event = asyncio.Event()

    def _signal_handler() -> None:
        log.info("worker.signal_received")
        ws.stop()
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            # Windows: signal handlers limited; we rely on KeyboardInterrupt instead
            pass

    frame_count = 0
    tick_count = 0
    vix_tick_count = 0
    try:
        async for frame in ws.frames():
            frame_count += 1
            if not frame.ticks:
                continue

            # Split: route VIX ticks to vix_repo, everything else to repo.
            vix_ticks = [t for t in frame.ticks if t.instrument_key == INDIA_VIX_INSTRUMENT_KEY]
            other_ticks = [t for t in frame.ticks if t.instrument_key != INDIA_VIX_INSTRUMENT_KEY]
            tick_count += len(other_ticks)
            vix_tick_count += len(vix_ticks)

            tasks = []
            if other_ticks:
                tasks.extend([
                    repo.append(other_ticks),
                    publisher.publish(other_ticks),
                    staleness.update(other_ticks),
                ])
            if vix_ticks:
                tasks.append(vix_repo.append(vix_ticks))
            if tasks:
                await asyncio.gather(*tasks)

            if frame_count % 50 == 0:
                log.info(
                    "worker.heartbeat",
                    frames=frame_count,
                    ticks=tick_count,
                    vix_ticks=vix_tick_count,
                    vix_latest=str(vix_repo.latest_value) if vix_repo.latest_value else None,
                    persisted=repo.total_persisted,
                    published=publisher.published_total,
                    chain_snapshots=chain_poller.snapshots_persisted,
                )
    except KeyboardInterrupt:
        log.info("worker.keyboard_interrupt")
    finally:
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass
        await chain_poller.stop()
        await repo.stop()
        await vix_repo.stop()
        await redis.aclose()
        log.info(
            "worker.stopped",
            frames=frame_count,
            ticks=tick_count,
            vix_ticks=vix_tick_count,
            persisted=repo.total_persisted,
            vix_persisted=vix_repo.total_persisted,
            chain_snapshots=chain_poller.snapshots_persisted,
            published=publisher.published_total,
        )


@click.command()
def main() -> None:
    """Run the Market Data worker."""
    configure_logging()
    asyncio.run(run())


if __name__ == "__main__":
    main()

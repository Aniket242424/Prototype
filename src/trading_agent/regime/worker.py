"""
Phase 2 worker — Regime + Options Intelligence + Opportunity Ranking.

Runs three engines as concurrent asyncio tasks within a single process:
  1. Regime  : every 30s, classify each underlying from tick buffer + VIX
  2. Intel   : every 30s, derive intel from latest chain snapshot
  3. Ranker  : every 30s (offset 5s), score all underlyings, emit top opportunity

The market_data_worker must be running for this to have ticks/chains to read.
Run:
    py -3.14 -u -m trading_agent.regime.worker
"""
from __future__ import annotations

import asyncio
import signal
from datetime import datetime, timezone

import click
import orjson
from sqlalchemy import select

import trading_agent.regime.options_intel as oi
from trading_agent.core.config import get_instruments_config, get_settings
from trading_agent.core.constants import INDIA_VIX_INSTRUMENT_KEY
from trading_agent.core.logging import configure_logging, get_logger
from trading_agent.core.time_utils import is_market_open, now_ist
from trading_agent.infrastructure.db import SessionLocal
from trading_agent.infrastructure.models import IndiaVixRow
from trading_agent.infrastructure.redis_client import make_redis
from trading_agent.regime.dtos import IndicatorSnapshot
from trading_agent.regime.opportunity import OpportunityEngine
from trading_agent.regime.options_intel import OptionsIntelEngine
from trading_agent.regime.regime_engine import RegimeEngine, compute_indicators
from trading_agent.regime.tick_buffer import TickBufferPool

log = get_logger(__name__)

REGIME_INTERVAL_SEC = 30.0
INTEL_INTERVAL_SEC = 30.0
RANKER_INTERVAL_SEC = 30.0
RANKER_OFFSET_SEC = 5.0      # so ranker reads regime/intel results from current cycle


async def _read_latest_vix(session_factory) -> float | None:
    try:
        async with session_factory() as session:
            row = (await session.execute(
                select(IndiaVixRow).order_by(IndiaVixRow.ts.desc()).limit(1)
            )).scalar_one_or_none()
        if row is None:
            return None
        return float(row.value)
    except Exception:
        return None


async def run() -> None:
    settings = get_settings()
    instruments = get_instruments_config()
    enabled = [i for i in instruments.instruments if i.enabled]
    enabled_keys = [i.upstox_instrument_key for i in enabled]
    name_by_key = {i.upstox_instrument_key: i.name for i in enabled}

    log.info(
        "regime_worker.starting",
        env=settings.app_env,
        underlyings=len(enabled),
        market_open=is_market_open(),
        ist_now=now_ist().isoformat(),
    )

    redis = make_redis()
    pool = TickBufferPool(redis, SessionLocal, instrument_keys=enabled_keys, max_minutes=90)
    await pool.start()

    regime_engine = RegimeEngine(SessionLocal, redis)
    intel_engine = OptionsIntelEngine(SessionLocal, redis)
    opp_engine = OpportunityEngine(SessionLocal, redis)

    stop_event = asyncio.Event()

    def _signal() -> None:
        log.info("regime_worker.signal")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal)
        except NotImplementedError:
            pass

    # In-memory caches updated by each cycle and consumed by ranker
    indicators_by_underlying: dict[str, IndicatorSnapshot] = {}

    async def _heartbeat() -> None:
        try:
            while not stop_event.is_set():
                await redis.set("worker:regime:heartbeat", now_ist().isoformat(), ex=10)
                await asyncio.sleep(3)
        except asyncio.CancelledError:
            pass

    async def regime_cycle() -> None:
        try:
            while not stop_event.is_set():
                vix_value = await _read_latest_vix(SessionLocal)
                vix_value_redis = await redis.get("md:vix:latest")
                if vix_value_redis is not None:
                    try:
                        vix_value = float(vix_value_redis.decode())
                    except Exception:
                        pass
                for inst in enabled:
                    buf = pool.get(inst.upstox_instrument_key)
                    if buf is None:
                        continue
                    try:
                        ticks_df = await buf.to_dataframe()
                        ind = compute_indicators(inst.name, ticks_df)
                        indicators_by_underlying[inst.name] = ind
                        regime = regime_engine.classify_and_persist if False else None  # type: ignore # noqa
                        # Use evaluate which both computes AND persists+publishes
                        await regime_engine.evaluate(inst.name, buf, vix_value)
                    except Exception as e:
                        log.warning("regime_cycle.failed", underlying=inst.name, error=str(e))
                log.info(
                    "regime_cycle.done",
                    underlyings=list(regime_engine.latest.keys()),
                )
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=REGIME_INTERVAL_SEC)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            pass

    async def intel_cycle() -> None:
        try:
            while not stop_event.is_set():
                for inst in enabled:
                    try:
                        await intel_engine.evaluate(inst.name)
                    except Exception as e:
                        log.warning("intel_cycle.failed", underlying=inst.name, error=str(e))
                log.info(
                    "intel_cycle.done",
                    underlyings=list(intel_engine.latest.keys()),
                )
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=INTEL_INTERVAL_SEC)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            pass

    async def ranker_cycle() -> None:
        # Initial offset so first cycle reads regime/intel results
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=RANKER_OFFSET_SEC)
        except asyncio.TimeoutError:
            pass
        try:
            while not stop_event.is_set():
                try:
                    opp = await opp_engine.evaluate_all(
                        indicators=indicators_by_underlying,
                        regimes=regime_engine.latest,
                        intels=intel_engine.latest,
                    )
                    if opp:
                        log.info(
                            "ranker_cycle.emit",
                            underlying=opp.underlying,
                            direction=opp.direction.value,
                            score=opp.score,
                        )
                    else:
                        log.info("ranker_cycle.no_opportunity_above_threshold")
                except Exception as e:
                    log.warning("ranker_cycle.failed", error=str(e))
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=RANKER_INTERVAL_SEC)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            pass

    hb_task = asyncio.create_task(_heartbeat(), name="regime-hb")
    rg_task = asyncio.create_task(regime_cycle(), name="regime-cycle")
    in_task = asyncio.create_task(intel_cycle(), name="intel-cycle")
    rk_task = asyncio.create_task(ranker_cycle(), name="ranker-cycle")

    try:
        await stop_event.wait()
    except KeyboardInterrupt:
        pass
    finally:
        for t in (hb_task, rg_task, in_task, rk_task):
            t.cancel()
        await asyncio.gather(hb_task, rg_task, in_task, rk_task, return_exceptions=True)
        await pool.stop()
        await redis.aclose()
        log.info("regime_worker.stopped")


@click.command()
def main() -> None:
    configure_logging()
    asyncio.run(run())


if __name__ == "__main__":
    main()

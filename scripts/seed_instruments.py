"""Idempotent seeding of `instruments` table from config/instruments.yaml."""
from __future__ import annotations

import asyncio
from decimal import Decimal

import click

from trading_agent.core.config import get_instruments_config
from trading_agent.core.logging import configure_logging, get_logger
from trading_agent.infrastructure.db import session_scope
from trading_agent.infrastructure.models import InstrumentRow


async def _seed() -> None:
    log = get_logger(__name__)
    cfg = get_instruments_config()
    async with session_scope() as session:
        for inst in cfg.instruments:
            row = await session.get(InstrumentRow, inst.upstox_instrument_key)
            if row is None:
                session.add(InstrumentRow(
                    instrument_key=inst.upstox_instrument_key,
                    name=inst.name,
                    exchange=inst.exchange,
                    lot_size=inst.lot_size,
                    tick_size=Decimal(str(inst.tick_size)),
                    expiry_weekday=inst.expiry_weekday,
                    enabled=inst.enabled,
                ))
                log.info("instrument.seeded", name=inst.name)
            else:
                row.lot_size = inst.lot_size
                row.tick_size = Decimal(str(inst.tick_size))
                row.expiry_weekday = inst.expiry_weekday
                row.enabled = inst.enabled
                row.exchange = inst.exchange
                log.info("instrument.updated", name=inst.name)
        await session.commit()


@click.command()
def main() -> None:
    """Seed/refresh the instruments table from config."""
    configure_logging()
    asyncio.run(_seed())
    click.echo(click.style("[OK] Instruments seeded.", fg="green"))


if __name__ == "__main__":
    main()

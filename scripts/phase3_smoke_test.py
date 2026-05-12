"""
Phase 3 end-to-end smoke test.

Runs a synthetic TradeIntent through the Risk Engine, and if approved,
through the Execution Engine (in paper mode). Prints every decision detail
so you can see exactly what's happening.

Use cases:
- Off-hours: demonstrates which gates reject and why (MARKET_CLOSED,
  OUTSIDE_WINDOW, STALE_DATA, etc.) — useful to verify the gates work
- Market hours: shows full approval + paper fill flow

Run:
    py -3.14 scripts/phase3_smoke_test.py
    py -3.14 scripts/phase3_smoke_test.py --underlying SENSEX --premium 80
    py -3.14 scripts/phase3_smoke_test.py --inject-staleness   # mock fresh tick
"""
from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta
from decimal import Decimal

import click

from trading_agent.core.config import get_instruments_config, get_settings
from trading_agent.core.constants import Direction, OptionType, OrderSide
from trading_agent.core.kill_switch import KillSwitch
from trading_agent.core.logging import configure_logging, get_logger
from trading_agent.core.time_utils import is_market_open, now_ist
from trading_agent.execution.broker import PaperBroker
from trading_agent.execution.engine import ExecutionEngine
from trading_agent.infrastructure.db import SessionLocal
from trading_agent.infrastructure.redis_client import make_redis
from trading_agent.risk.dtos import Leg, TradeIntent
from trading_agent.risk.engine import RiskEngine
from trading_agent.risk.live_trading_gate import check_live_trading_status

log = get_logger(__name__)


def _build_synthetic_intent(
    underlying: str,
    premium: float,
    confidence: float,
    strike: float,
) -> TradeIntent:
    """Construct a TradeIntent that mirrors what Phase 4 Strategy Engine will emit."""
    today = now_ist().date()
    # Use Friday this week as a plausible expiry (real expiry would come from chain)
    days_to_friday = (4 - today.weekday()) % 7
    if days_to_friday == 0:
        days_to_friday = 7
    expiry = today + timedelta(days=days_to_friday)

    leg = Leg(
        side=OrderSide.BUY,
        option_type=OptionType.CE,
        strike=Decimal(str(strike)),
        expiry=expiry,
        instrument_key=f"NSE_FO|SYNTH-{underlying}-{int(strike)}",
        target_qty=10,           # placeholder, sizer will recompute
        target_premium=Decimal(str(premium)),
    )
    return TradeIntent(
        strategy_name="smoke_test",
        underlying=underlying,
        direction=Direction.LONG,
        legs=[leg],
        stop_underlying=Decimal("0"),       # not used by Risk Engine in 3.1
        target_underlying=Decimal("0"),
        confidence=confidence,
        ts=now_ist(),
    )


async def _print_section(title: str) -> None:
    click.echo("")
    click.echo(click.style(f"=== {title} ===", fg="cyan", bold=True))


async def _run(
    underlying: str,
    premium: float,
    confidence: float,
    strike: float,
    inject_staleness: bool,
    trip_kill_switch: bool,
    skip_execution: bool,
) -> int:
    configure_logging()
    settings = get_settings()
    instruments = get_instruments_config()

    redis = make_redis()
    try:
        await _print_section("Environment")
        click.echo(f"  IST now:           {now_ist().isoformat()}")
        click.echo(f"  Market open:       {is_market_open()}")
        click.echo(f"  App env:           {settings.app_env}")
        click.echo(f"  Capital:           Rs.{settings.trading_capital_inr:,.0f}")
        click.echo(f"  LIVE_TRADING env:  {settings.live_trading}")

        # Live-trading 3-lock status
        await _print_section("Live-trading 3-lock check")
        lt = await check_live_trading_status()
        click.echo(f"  authorized:        {lt.authorized}")
        click.echo(f"  env_LIVE_TRADING:  {lt.env_live_trading}")
        click.echo(f"  file_present:      {lt.file_present}")
        click.echo(f"  db_sha_matches:    {lt.db_sha_matches}")
        click.echo(f"  reason:            {lt.reason}")

        # Kill switch handling
        ks = KillSwitch(redis)
        if trip_kill_switch:
            await ks.trip("smoke_test_manual", source="phase3_smoke_test.py")
            click.echo(click.style("  Kill switch TRIPPED for this run", fg="red"))
        ks_state = await ks.state()
        await _print_section("Kill switch")
        click.echo(f"  tripped:           {ks_state.tripped}")
        click.echo(f"  reason:            {ks_state.reason}")

        # Staleness injection
        if inject_staleness:
            inst_key = next(
                (i.upstox_instrument_key for i in instruments.instruments if i.name == underlying),
                None,
            )
            if inst_key:
                await redis.set(f"md:last_tick_ts:{inst_key}", now_ist().isoformat())
                click.echo(click.style(
                    f"  Injected fresh tick timestamp for {inst_key}",
                    fg="yellow",
                ))

        # Build synthetic intent
        await _print_section("Synthetic TradeIntent")
        intent = _build_synthetic_intent(underlying, premium, confidence, strike)
        click.echo(f"  strategy:          {intent.strategy_name}")
        click.echo(f"  underlying:        {intent.underlying}")
        click.echo(f"  direction:         {intent.direction.value}")
        click.echo(f"  confidence:        {intent.confidence}")
        click.echo(f"  legs:              {len(intent.legs)}")
        for i, leg in enumerate(intent.legs):
            click.echo(f"  leg[{i}]:  {leg.option_type.value} K={leg.strike} "
                       f"premium={leg.target_premium} qty={leg.target_qty}")

        # Run Risk Engine
        await _print_section("Risk Engine evaluation")
        risk_engine = RiskEngine(SessionLocal, redis)
        decision = await risk_engine.evaluate(intent)

        color = "green" if decision.approved else "yellow"
        click.echo(click.style(f"  decision:          {decision.approved}", fg=color))
        click.echo(f"  code:              {decision.code}")
        click.echo(f"  reason:            {decision.reason}")
        if decision.approved:
            click.echo(f"  sized_qty:         {decision.sized_qty}")
            click.echo(f"  sized_lots:        {decision.sized_lots}")
            click.echo(f"  max_outlay_inr:    Rs.{decision.max_outlay_inr:,.2f}")

        await _print_section("Risk decision inputs snapshot")
        click.echo(json.dumps(decision.inputs_snapshot, indent=2, default=str))

        # Run Execution if approved + not skipped
        if decision.approved and not skip_execution:
            await _print_section("Execution Engine (paper mode)")
            broker = PaperBroker(seed=42)
            executor = ExecutionEngine(broker, SessionLocal, redis)
            result = await executor.execute(intent, decision)

            click.echo(f"  status:            {result.status.value}")
            click.echo(f"  is_paper:          {result.is_paper}")
            click.echo(f"  reference_mid:     {result.reference_mid}")
            click.echo(f"  estimated_slippage_bps: {result.estimated_slippage_bps}")
            click.echo(f"  realized_slippage_bps:  {result.realized_slippage_bps}")
            if result.rejection_reason:
                click.echo(click.style(
                    f"  rejection:         {result.rejection_reason}",
                    fg="red",
                ))
            click.echo(f"  fills:             {len(result.fills)}")
            for f in result.fills:
                click.echo(f"     qty={f.qty}  price={f.price}  ts={f.ts}")
        elif decision.approved and skip_execution:
            click.echo("  [SKIPPED] execution not run (--skip-execution)")

        # Cleanup kill switch if we tripped it
        if trip_kill_switch:
            await ks.reset(operator="phase3_smoke_test.py")
            click.echo(click.style("\n  Kill switch reset.", fg="yellow"))

        await _print_section("Done")
        return 0 if decision.approved or not _strict_mode() else 1
    finally:
        await redis.aclose()


def _strict_mode() -> bool:
    """If True, the script exits non-zero on rejection. Default: False (rejections are valid)."""
    import os
    return os.environ.get("PHASE3_SMOKE_STRICT", "").lower() in ("1", "true", "yes")


@click.command()
@click.option("--underlying", default="SENSEX", help="Index to test (default: SENSEX — fits ₹1,500 cap)")
@click.option("--premium", default=80.0, type=float, help="Option premium in ₹ (default: 80)")
@click.option("--strike", default=75000.0, type=float, help="Strike price (default: 75000)")
@click.option("--confidence", default=0.7, type=float, help="Strategy confidence 0..1 (default: 0.7)")
@click.option("--inject-staleness", is_flag=True, help="Mock a fresh tick TS to bypass staleness gate")
@click.option("--trip-kill-switch", is_flag=True, help="Trip the kill switch before evaluation (demos KILL_SWITCH rejection)")
@click.option("--skip-execution", is_flag=True, help="Run only Risk Engine, skip Execution Engine even if approved")
def main(
    underlying: str,
    premium: float,
    strike: float,
    confidence: float,
    inject_staleness: bool,
    trip_kill_switch: bool,
    skip_execution: bool,
) -> None:
    """Phase 3 smoke test — synthetic trade through Risk + Execution."""
    rc = asyncio.run(_run(
        underlying=underlying,
        premium=premium,
        confidence=confidence,
        strike=strike,
        inject_staleness=inject_staleness,
        trip_kill_switch=trip_kill_switch,
        skip_execution=skip_execution,
    ))
    raise SystemExit(rc)


if __name__ == "__main__":
    main()

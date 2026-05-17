"""Backtest runner for EOD-Premium NIFTY strategy."""
from __future__ import annotations

import asyncio
from datetime import date, datetime
from pathlib import Path

import click

from trading_agent.backtesting.bar_fetcher import BarFetcher
from trading_agent.backtesting.engine import BacktestEngine
from trading_agent.backtesting.reporter import (
    format_text_summary, write_equity_curve_csv, write_summary_json, write_trades_csv,
)
from trading_agent.backtesting.run import INSTRUMENT_KEYS
from trading_agent.backtesting.strategies.eod_premium import EodPremiumStrategy
from trading_agent.core.config import REPO_ROOT
from trading_agent.core.logging import configure_logging, get_logger

log = get_logger(__name__)


@click.command()
@click.option("--start", type=str, required=True)
@click.option("--end", type=str, required=True)
@click.option(
    "--output-dir",
    type=click.Path(),
    default=str(REPO_ROOT / "reports" / "backtests_eod_premium"),
)
@click.option("--force-refresh", is_flag=True)
def main(start, end, output_dir, force_refresh):
    """Run EOD-Premium backtest on NIFTY."""
    configure_logging()
    asyncio.run(_run(
        start_date=date.fromisoformat(start),
        end_date=date.fromisoformat(end),
        output_dir=Path(output_dir),
        force_refresh=force_refresh,
    ))


async def _run(start_date, end_date, output_dir, force_refresh):
    instrument_key = INSTRUMENT_KEYS["NIFTY"]
    log.info("backtest_eod_premium.starting", start=str(start_date), end=str(end_date))

    fetcher = BarFetcher()
    bars = await fetcher.fetch_range(instrument_key, start_date, end_date, force_refresh)
    log.info("backtest_eod_premium.bars_loaded", count=len(bars))
    if not bars:
        click.echo("No bars loaded.", err=True)
        return

    strategy = EodPremiumStrategy()
    engine = BacktestEngine(underlying="NIFTY", strategy=strategy)
    results = engine.run(bars)

    tag = f"NIFTY_EODPremium_{start_date}_{end_date}_{datetime.now().strftime('%H%M%S')}"
    run_dir = output_dir / tag
    write_summary_json(results, run_dir / "summary.json")
    write_trades_csv(results, run_dir / "trades.csv")
    write_equity_curve_csv(results, run_dir / "equity.csv")

    click.echo(format_text_summary(results))
    click.echo(f"\n📁 Results: {run_dir}")


if __name__ == "__main__":
    main()

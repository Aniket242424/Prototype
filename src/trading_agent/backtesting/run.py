"""
Backtest CLI runner — Phase 5.

Usage:
    python -m trading_agent.backtesting.run \\
        --underlying NIFTY \\
        --start 2026-04-01 \\
        --end 2026-04-30 \\
        [--stop-pct 0.0035] \\
        [--target-rr 2.0] \\
        [--output-dir reports/backtests]

What it does:
1. Fetches/loads cached 1-min bars for the underlying over [start, end].
2. Runs the BacktestEngine against those bars.
3. Writes summary JSON + trades CSV + equity curve CSV to the output dir.
4. Prints a pretty text summary to stdout.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime
from pathlib import Path

import click

from trading_agent.backtesting.bar_fetcher import BarFetcher
from trading_agent.backtesting.engine import BacktestEngine
from trading_agent.backtesting.reporter import (
    format_text_summary,
    write_equity_curve_csv,
    write_summary_json,
    write_trades_csv,
)
from trading_agent.core.config import REPO_ROOT
from trading_agent.core.logging import configure_logging, get_logger

log = get_logger(__name__)


# Map underlying short names to Upstox instrument keys
INSTRUMENT_KEYS = {
    "NIFTY":     "NSE_INDEX|Nifty 50",
    "BANKNIFTY": "NSE_INDEX|Nifty Bank",
    "FINNIFTY":  "NSE_INDEX|Nifty Fin Service",
    "SENSEX":    "BSE_INDEX|SENSEX",
    "BANKEX":    "BSE_INDEX|BANKEX",
}


@click.command()
@click.option(
    "--underlying",
    type=click.Choice(list(INSTRUMENT_KEYS.keys())),
    required=True,
    help="Index to backtest.",
)
@click.option("--start", type=str, required=True, help="Start date YYYY-MM-DD.")
@click.option("--end", type=str, required=True, help="End date YYYY-MM-DD (inclusive).")
@click.option("--stop-pct", type=float, default=0.0035, help="Stop distance as fraction of underlying (default 0.0035 = 0.35%).")
@click.option("--target-rr", type=float, default=2.0, help="Risk-reward ratio for target (default 2.0).")
@click.option(
    "--output-dir",
    type=click.Path(),
    default=str(REPO_ROOT / "reports" / "backtests"),
    help="Directory for output files.",
)
@click.option("--force-refresh", is_flag=True, help="Re-download bars even if cached.")
def main(underlying, start, end, stop_pct, target_rr, output_dir, force_refresh):
    """Run a backtest and write results to disk."""
    configure_logging()
    start_date = date.fromisoformat(start)
    end_date = date.fromisoformat(end)

    asyncio.run(_run_backtest(
        underlying=underlying,
        start_date=start_date,
        end_date=end_date,
        stop_pct=stop_pct,
        target_rr=target_rr,
        output_dir=Path(output_dir),
        force_refresh=force_refresh,
    ))


async def _run_backtest(
    underlying: str,
    start_date: date,
    end_date: date,
    stop_pct: float,
    target_rr: float,
    output_dir: Path,
    force_refresh: bool,
):
    instrument_key = INSTRUMENT_KEYS[underlying]
    log.info(
        "backtest.starting",
        underlying=underlying,
        start=start_date.isoformat(),
        end=end_date.isoformat(),
        stop_pct=stop_pct,
        target_rr=target_rr,
    )

    fetcher = BarFetcher()
    bars = await fetcher.fetch_range(
        instrument_key=instrument_key,
        start=start_date,
        end=end_date,
        force_refresh=force_refresh,
    )
    log.info("backtest.bars_loaded", count=len(bars))

    if not bars:
        click.echo("No bars loaded — check the date range and Upstox token validity.", err=True)
        return

    engine = BacktestEngine(
        underlying=underlying,
        stop_pct=stop_pct,
        target_rr=target_rr,
    )
    results = engine.run(bars)

    # Write outputs
    tag = f"{underlying}_{start_date.isoformat()}_{end_date.isoformat()}_{datetime.now().strftime('%H%M%S')}"
    run_dir = output_dir / tag
    write_summary_json(results, run_dir / "summary.json")
    write_trades_csv(results, run_dir / "trades.csv")
    write_equity_curve_csv(results, run_dir / "equity.csv")

    # Console summary
    click.echo(format_text_summary(results))
    click.echo(f"\n📁 Results written to: {run_dir}")


if __name__ == "__main__":
    main()

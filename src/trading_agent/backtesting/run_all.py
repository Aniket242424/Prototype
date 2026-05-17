"""
Run ALL strategies on a given underlying + period with REALISTIC transaction costs.

Each strategy is run independently with its own engine instance.
Output: side-by-side comparison + compounded ₹ returns.

Usage:
    python -m trading_agent.backtesting.run_all --underlying NIFTY --start 2025-05-16 --end 2026-05-15
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime
from pathlib import Path

import click

from trading_agent.backtesting.bar_fetcher import BarFetcher
from trading_agent.backtesting.engine import BacktestEngine
from trading_agent.backtesting.reporter import format_text_summary, write_summary_json, write_trades_csv
from trading_agent.backtesting.run import INSTRUMENT_KEYS
from trading_agent.backtesting.strategies.ema_crossover import EmaCrossoverStrategy
from trading_agent.backtesting.strategies.eod_momentum import EndOfDayMomentumStrategy
from trading_agent.backtesting.strategies.eod_premium import EodPremiumStrategy
from trading_agent.backtesting.strategies.eod_mtf_trend import EodMtfTrendStrategy
from trading_agent.backtesting.transaction_costs import REALISTIC_NIFTY_ITM
from trading_agent.core.config import REPO_ROOT
from trading_agent.core.logging import configure_logging, get_logger

log = get_logger(__name__)


STRATEGIES = [
    ("ema_crossover", EmaCrossoverStrategy()),
    ("eod_momentum",  EndOfDayMomentumStrategy()),
    ("eod_premium",   EodPremiumStrategy()),
    ("eod_mtf_trend", EodMtfTrendStrategy()),
]


@click.command()
@click.option("--underlying", type=click.Choice(list(INSTRUMENT_KEYS.keys())), required=True)
@click.option("--start", type=str, required=True)
@click.option("--end", type=str, required=True)
@click.option("--output-dir", type=click.Path(),
              default=str(REPO_ROOT / "reports" / "backtests_all"))
def main(underlying, start, end, output_dir):
    configure_logging()
    asyncio.run(_run(
        underlying=underlying,
        start_date=date.fromisoformat(start),
        end_date=date.fromisoformat(end),
        output_dir=Path(output_dir),
    ))


async def _run(underlying, start_date, end_date, output_dir):
    fetcher = BarFetcher()
    bars = await fetcher.fetch_range(
        INSTRUMENT_KEYS[underlying], start_date, end_date, force_refresh=False
    )
    log.info("run_all.bars_loaded", count=len(bars))
    if not bars:
        click.echo("No bars loaded.", err=True)
        return

    costs = REALISTIC_NIFTY_ITM
    click.echo(costs.describe())
    click.echo()

    results_by_name = {}
    for name, strategy in STRATEGIES:
        engine = BacktestEngine(underlying=underlying, strategy=strategy, costs=costs)
        results = engine.run(bars)
        results_by_name[name] = results
        tag = f"{underlying}_{name}_{start_date}_{end_date}_{datetime.now().strftime('%H%M%S')}"
        run_dir = output_dir / tag
        write_summary_json(results, run_dir / "summary.json")
        write_trades_csv(results, run_dir / "trades.csv")
        click.echo(format_text_summary(results))
        click.echo()

    # Side-by-side table
    click.echo("=" * 90)
    click.echo("SIDE-BY-SIDE (WITH REALISTIC COSTS):")
    click.echo("-" * 90)
    click.echo(f"{'Strategy':20s} {'Trades':>7s} {'Win%':>6s} {'PF':>6s} {'Exp/trade':>10s} {'TotalR':>8s} {'MaxDD':>8s} {'Sharpe':>7s}")
    for name, results in results_by_name.items():
        pf = f"{results.profit_factor:.2f}" if results.profit_factor != float("inf") else "inf"
        click.echo(
            f"{name:20s} {results.total_trades:>7d} "
            f"{results.win_rate*100:>5.1f}% "
            f"{pf:>6s} "
            f"{results.expectancy_r:>+9.3f}R "
            f"{results.total_r:>+7.2f}R "
            f"{results.max_drawdown_r:>7.2f}R "
            f"{results.sharpe:>7.2f}"
        )

    # Compound rupee summary at 0.5% risk per trade
    click.echo()
    click.echo("=" * 90)
    click.echo("COMPOUNDED ₹ RETURNS (Rs 3,00,000 start, 0.5% risk per trade):")
    click.echo("-" * 90)
    for name, results in results_by_name.items():
        capital = 300_000.0
        for t in results.trades:
            risk = capital * 0.005
            capital += risk * t.r_multiple
        total_pnl = capital - 300_000
        pct = (capital - 300_000) / 300_000 * 100
        click.echo(f"  {name:20s}  end=Rs {capital:>12,.0f}  pnl=Rs {total_pnl:>+9,.0f}  return={pct:>+6.2f}%")


if __name__ == "__main__":
    main()

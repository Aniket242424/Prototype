"""
Backtest results reporter — Phase 5.

Serializes BacktestResults to:
- Summary JSON (metrics for dashboard/api consumption)
- Trades CSV (each closed trade as a row)
- Equity curve CSV (cumulative R after each trade)
- Pretty text summary (for console output)
"""
from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path

from trading_agent.backtesting.dtos import BacktestResults


def write_summary_json(results: BacktestResults, path: Path) -> None:
    """Serializable summary (skips the trades list itself — use write_trades_csv for that)."""
    payload = {
        "underlying": results.underlying,
        "start_date": results.start_date.isoformat(),
        "end_date": results.end_date.isoformat(),
        "total_bars": results.total_bars,
        "metrics": {
            "total_trades": results.total_trades,
            "wins": results.wins,
            "losses": results.losses,
            "breakeven": results.breakeven,
            "win_rate": round(results.win_rate, 4),
            "avg_win_r": round(results.avg_win_r, 4),
            "avg_loss_r": round(results.avg_loss_r, 4),
            "profit_factor": (
                round(results.profit_factor, 4)
                if results.profit_factor != float("inf")
                else "inf"
            ),
            "sharpe": round(results.sharpe, 4),
            "max_drawdown_r": round(results.max_drawdown_r, 4),
            "total_r": round(results.total_r, 4),
            "expectancy_r": round(results.expectancy_r, 4),
        },
        "by_strategy": results.by_strategy,
        "generated_at": datetime.now().isoformat(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def write_trades_csv(results: BacktestResults, path: Path) -> None:
    """One row per closed trade with all key fields."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "underlying", "strategy", "direction",
            "entry_ts", "entry_price",
            "exit_ts", "exit_price",
            "stop_price", "target_price",
            "exit_reason", "r_multiple", "hold_minutes", "bars_held",
        ])
        for t in results.trades:
            writer.writerow([
                t.underlying, t.strategy_name, t.direction,
                t.entry_ts.isoformat(), float(t.entry_price),
                t.exit_ts.isoformat(), float(t.exit_price),
                float(t.stop_price), float(t.target_price),
                t.exit_reason, round(t.r_multiple, 4), t.hold_minutes, t.bars_held,
            ])


def write_equity_curve_csv(results: BacktestResults, path: Path) -> None:
    """Cumulative R after each trade — easy to plot in Excel."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["trade_n", "exit_ts", "trade_r", "cumulative_r"])
        running = 0.0
        for i, t in enumerate(results.trades, start=1):
            running += t.r_multiple
            writer.writerow([i, t.exit_ts.isoformat(), round(t.r_multiple, 4), round(running, 4)])


def format_text_summary(results: BacktestResults) -> str:
    """Human-readable console output."""
    pf = (
        f"{results.profit_factor:.2f}"
        if results.profit_factor != float("inf")
        else "inf (no losses)"
    )
    lines = [
        "=" * 60,
        f"BACKTEST RESULTS — {results.underlying}",
        f"Period: {results.start_date.date()} to {results.end_date.date()}",
        f"Bars processed: {results.total_bars:,}",
        "-" * 60,
        f"Total trades:      {results.total_trades}",
        f"Wins / Losses / BE: {results.wins} / {results.losses} / {results.breakeven}",
        f"Win rate:          {results.win_rate * 100:.1f}%",
        f"Avg win:           {results.avg_win_r:+.2f}R",
        f"Avg loss:          {results.avg_loss_r:+.2f}R",
        f"Profit factor:     {pf}",
        f"Expectancy:        {results.expectancy_r:+.3f}R per trade",
        f"Total return:      {results.total_r:+.2f}R",
        f"Max drawdown:      {results.max_drawdown_r:.2f}R",
        f"Sharpe (annual):   {results.sharpe:.2f}",
        "-" * 60,
        "By strategy:",
    ]
    for strat, s in results.by_strategy.items():
        lines.append(
            f"  {strat:25s} trades={s['trades']:3d}  "
            f"win_rate={s['win_rate']*100:5.1f}%  total_r={s['total_r']:+.2f}"
        )
    lines.append("=" * 60)
    return "\n".join(lines)

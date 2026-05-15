"""DTOs for the backtesting engine — Phase 5."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True)
class Bar:
    """A single OHLCV bar from Upstox historical-candle API."""
    ts: datetime                # bar open timestamp (IST)
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    oi: int = 0                 # open interest (for derivatives; 0 for cash indices)


@dataclass(frozen=True)
class BacktestTrade:
    """One closed trade in a backtest. PnL expressed in R-multiples."""
    underlying: str
    strategy_name: str
    direction: str              # "LONG" | "SHORT"
    entry_ts: datetime
    entry_price: Decimal
    exit_ts: datetime
    exit_price: Decimal
    stop_price: Decimal         # initial stop
    target_price: Decimal       # initial target
    exit_reason: str            # "TARGET_HIT" | "HARD_STOP_HIT" | "FORCED_TIME_EXIT" | etc.
    r_multiple: float           # signed: +1.5 means +1.5R win, -1.0 means full stop-out
    hold_minutes: int
    bars_held: int


@dataclass
class BacktestResults:
    """Aggregate results from a backtest run."""
    underlying: str
    start_date: datetime
    end_date: datetime
    total_bars: int
    total_trades: int
    wins: int
    losses: int
    breakeven: int

    win_rate: float                 # 0.0-1.0
    avg_win_r: float                # mean R of winning trades
    avg_loss_r: float               # mean R of losing trades (negative)
    profit_factor: float            # gross_win / abs(gross_loss); higher = better
    sharpe: float                   # daily Sharpe of equity curve
    max_drawdown_r: float           # worst peak-to-trough drawdown in R

    total_r: float                  # cumulative R achieved
    expectancy_r: float             # total_r / total_trades

    # Per-strategy breakdown { strategy_name: {trades, win_rate, total_r, ...} }
    by_strategy: dict[str, dict] = field(default_factory=dict)

    trades: list[BacktestTrade] = field(default_factory=list)

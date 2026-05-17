"""
Backtest engine — Phase 5.

Replays the trading pipeline against historical 1-min OHLC bars and produces
a BacktestResults DTO with win rate, profit factor, Sharpe, max drawdown, etc.

Now strategy-pluggable: pass any BacktestStrategy (see strategies/base.py).
Default is EmaCrossoverStrategy for backward compatibility with V1.

Design:
- Iterates bars in chronological order.
- Each bar checks exits FIRST (so a bar that opens AND hits stop is impossible).
- PnL accounted in R-multiples (1R = abs(entry - stop)) — sidesteps the
  no-historical-option-premium problem on retail Upstox.
- Single position at a time per engine instance (matches production max_concurrent=1).
- Pessimistic: if a single bar's range touches BOTH stop and target, stop wins
  (avoids optimistic backtest bias).
"""
from __future__ import annotations

import statistics
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Optional

from trading_agent.backtesting.dtos import Bar, BacktestResults, BacktestTrade
from trading_agent.backtesting.strategies.base import BacktestStrategy, EntryDecision
from trading_agent.backtesting.strategies.ema_crossover import EmaCrossoverStrategy
from trading_agent.backtesting.transaction_costs import ZERO_COST, TransactionCostModel
from trading_agent.core.logging import get_logger
from trading_agent.core.time_utils import IST

log = get_logger(__name__)


# Slippage assumption (applied adversely to entry & favorably to no one)
SLIPPAGE_TICKS = 1
TICK_SIZE = Decimal("0.05")


@dataclass
class _OpenPosition:
    """Mutable in-flight position tracked during a backtest."""
    underlying: str
    strategy_name: str
    direction: str
    entry_ts: datetime
    entry_price: Decimal
    stop_price: Decimal
    target_price: Decimal
    initial_r: Decimal
    bars_held: int = 0


class BacktestEngine:
    """
    Self-contained backtest. No DB / Redis / workers required.

    Usage:
        engine = BacktestEngine(underlying="NIFTY", strategy=EndOfDayMomentumStrategy())
        results = engine.run(bars)

    Backward-compat (V1 API still works):
        engine = BacktestEngine(underlying="NIFTY", stop_pct=0.0035, target_rr=2.0)
        results = engine.run(bars)
        # → uses EmaCrossoverStrategy with the given stop/target
    """

    def __init__(
        self,
        underlying: str,
        strategy: Optional[BacktestStrategy] = None,
        costs: Optional[TransactionCostModel] = None,
        # ---- V1-style convenience params (used only when strategy=None) ----
        stop_pct: float = 0.0035,
        target_rr: float = 2.0,
        ema_short: int = 9,
        ema_long: int = 21,
        adx_threshold: float = 20.0,  # kept for back-compat; unused
        min_history_bars: int = 30,
    ):
        self.underlying = underlying
        if strategy is None:
            strategy = EmaCrossoverStrategy(
                ema_short=ema_short,
                ema_long=ema_long,
                stop_pct=stop_pct,
                target_rr=target_rr,
                min_history_bars=min_history_bars,
            )
        self.strategy: BacktestStrategy = strategy
        # Default to ZERO_COST so legacy tests (which assumed no fees) still pass.
        # Real backtests should pass a realistic TransactionCostModel.
        self.costs: TransactionCostModel = costs if costs is not None else ZERO_COST

        # Keep these as attributes for tests that introspect them
        self.stop_pct = Decimal(str(stop_pct))
        self.target_rr = Decimal(str(target_rr))
        self.min_history_bars = min_history_bars

        # State
        self._history: list[Bar] = []
        # Keep last N closes for fast indicator access in tests (mirrors V1 contract)
        self._closes: deque[Decimal] = deque(maxlen=max(ema_long * 3, 100))
        self._highs: deque[Decimal] = deque(maxlen=15)
        self._lows: deque[Decimal] = deque(maxlen=15)
        self._open_position: Optional[_OpenPosition] = None
        self._trades: list[BacktestTrade] = []

    # ============================================================
    # Entry / exit
    # ============================================================

    def _try_open(self, bar: Bar) -> None:
        if self._open_position is not None:
            return
        if not self.strategy.is_entry_time(bar.ts):
            return
        if self.strategy.is_force_exit_time(bar.ts):
            return

        decision = self.strategy.should_open(bar, self._history)
        if decision is None:
            return

        # Compute entry price with adverse 1-tick slippage
        slip = TICK_SIZE * SLIPPAGE_TICKS
        if decision.direction.value == "LONG":
            entry = bar.close + slip
            stop = entry * (Decimal(1) - Decimal(str(decision.stop_pct)))
        else:
            entry = bar.close - slip
            stop = entry * (Decimal(1) + Decimal(str(decision.stop_pct)))

        initial_r = abs(entry - stop)
        target = entry + (Decimal(1) if decision.direction.value == "LONG" else Decimal(-1)) * \
                 initial_r * Decimal(str(decision.target_rr))

        self._open_position = _OpenPosition(
            underlying=self.underlying,
            strategy_name=self.strategy.name,
            direction=decision.direction.value,
            entry_ts=bar.ts,
            entry_price=entry,
            stop_price=stop,
            target_price=target,
            initial_r=initial_r,
        )

    def _check_exits(self, bar: Bar) -> None:
        if self._open_position is None:
            return
        pos = self._open_position
        pos.bars_held += 1

        # Forced time exit always wins
        if self.strategy.is_force_exit_time(bar.ts):
            self._close_position(bar, bar.close, "FORCED_TIME_EXIT")
            return

        # Intra-bar stop/target — pessimistic tie-break: stop wins
        if pos.direction == "LONG":
            stop_hit = bar.low <= pos.stop_price
            target_hit = bar.high >= pos.target_price
            if stop_hit:
                self._close_position(bar, pos.stop_price, "HARD_STOP_HIT")
                return
            if target_hit:
                self._close_position(bar, pos.target_price, "TARGET_HIT")
                return
        else:
            stop_hit = bar.high >= pos.stop_price
            target_hit = bar.low <= pos.target_price
            if stop_hit:
                self._close_position(bar, pos.stop_price, "HARD_STOP_HIT")
                return
            if target_hit:
                self._close_position(bar, pos.target_price, "TARGET_HIT")
                return

    def _close_position(self, bar: Bar, exit_price: Decimal, reason: str) -> None:
        pos = self._open_position
        if pos is None:
            return
        if pos.direction == "LONG":
            pnl_pts = exit_price - pos.entry_price
        else:
            pnl_pts = pos.entry_price - exit_price
        # Deduct round-trip transaction cost (spread + brokerage + STT + GST)
        # expressed in underlying points so it directly subtracts from pnl_pts.
        cost_pts = Decimal(str(self.costs.cost_in_underlying_pts()))
        pnl_pts_after_costs = pnl_pts - cost_pts
        r = float(pnl_pts_after_costs / pos.initial_r) if pos.initial_r > 0 else 0.0
        hold_min = int((bar.ts - pos.entry_ts).total_seconds() // 60)
        self._trades.append(BacktestTrade(
            underlying=pos.underlying,
            strategy_name=pos.strategy_name,
            direction=pos.direction,
            entry_ts=pos.entry_ts,
            entry_price=pos.entry_price,
            exit_ts=bar.ts,
            exit_price=exit_price,
            stop_price=pos.stop_price,
            target_price=pos.target_price,
            exit_reason=reason,
            r_multiple=r,
            hold_minutes=hold_min,
            bars_held=pos.bars_held,
        ))
        self._open_position = None

    # ============================================================
    # Main replay loop
    # ============================================================

    def run(self, bars: list[Bar]) -> BacktestResults:
        if not bars:
            return self._empty_results()

        current_session = None
        for bar in bars:
            bar_date = bar.ts.astimezone(IST).date()
            if bar_date != current_session:
                # New trading day — reset per-day state
                self.strategy.on_session_start(bar_date)
                current_session = bar_date

            # Exit checks first (so a bar can't open AND stop-out in the same bar)
            self._check_exits(bar)

            # Let the strategy update its session state for this bar (VWAP, day high/low, etc.)
            self.strategy.on_bar(bar)

            # Update legacy state buffers (kept for tests that introspect them)
            self._closes.append(bar.close)
            self._highs.append(bar.high)
            self._lows.append(bar.low)

            # Try entry on this bar
            self._try_open(bar)

            # Add to history AFTER entry attempt (so next iter sees this bar)
            self._history.append(bar)

        # Close any still-open position at last bar's close
        if self._open_position is not None:
            last = bars[-1]
            self._close_position(last, last.close, "BACKTEST_END")

        return self._compile_results(bars)

    # ============================================================
    # Results
    # ============================================================

    def _empty_results(self) -> BacktestResults:
        return BacktestResults(
            underlying=self.underlying,
            start_date=datetime.now(IST),
            end_date=datetime.now(IST),
            total_bars=0, total_trades=0,
            wins=0, losses=0, breakeven=0,
            win_rate=0.0, avg_win_r=0.0, avg_loss_r=0.0,
            profit_factor=0.0, sharpe=0.0, max_drawdown_r=0.0,
            total_r=0.0, expectancy_r=0.0,
        )

    def _compile_results(self, bars: list[Bar]) -> BacktestResults:
        trades = self._trades
        n = len(trades)
        if n == 0:
            r = self._empty_results()
            r.start_date = bars[0].ts
            r.end_date = bars[-1].ts
            r.total_bars = len(bars)
            return r

        wins = [t for t in trades if t.r_multiple > 0.01]
        losses = [t for t in trades if t.r_multiple < -0.01]
        breakeven = n - len(wins) - len(losses)
        win_rate = len(wins) / n
        avg_win_r = statistics.mean(t.r_multiple for t in wins) if wins else 0.0
        avg_loss_r = statistics.mean(t.r_multiple for t in losses) if losses else 0.0
        gross_win = sum(t.r_multiple for t in wins)
        gross_loss = abs(sum(t.r_multiple for t in losses))
        profit_factor = (gross_win / gross_loss) if gross_loss > 0 else float("inf")
        total_r = sum(t.r_multiple for t in trades)
        expectancy_r = total_r / n

        equity = []
        running = 0.0
        for t in trades:
            running += t.r_multiple
            equity.append(running)
        max_dd = 0.0
        peak = 0.0
        for e in equity:
            peak = max(peak, e)
            max_dd = max(max_dd, peak - e)

        if n >= 2:
            r_series = [t.r_multiple for t in trades]
            mean_r = statistics.mean(r_series)
            stdev_r = statistics.stdev(r_series)
            days_span = max(1, (trades[-1].entry_ts - trades[0].entry_ts).days)
            trades_per_year = n * 250 / days_span
            sharpe = (mean_r / stdev_r) * (trades_per_year ** 0.5) if stdev_r > 0 else 0.0
        else:
            sharpe = 0.0

        by_strategy: dict[str, dict] = {}
        for t in trades:
            s = by_strategy.setdefault(t.strategy_name, {"trades": 0, "wins": 0, "total_r": 0.0})
            s["trades"] += 1
            if t.r_multiple > 0:
                s["wins"] += 1
            s["total_r"] += t.r_multiple
        for s in by_strategy.values():
            s["win_rate"] = s["wins"] / s["trades"] if s["trades"] else 0.0

        return BacktestResults(
            underlying=self.underlying,
            start_date=bars[0].ts,
            end_date=bars[-1].ts,
            total_bars=len(bars),
            total_trades=n,
            wins=len(wins),
            losses=len(losses),
            breakeven=breakeven,
            win_rate=win_rate,
            avg_win_r=avg_win_r,
            avg_loss_r=avg_loss_r,
            profit_factor=profit_factor,
            sharpe=sharpe,
            max_drawdown_r=max_dd,
            total_r=total_r,
            expectancy_r=expectancy_r,
            by_strategy=by_strategy,
            trades=trades,
        )

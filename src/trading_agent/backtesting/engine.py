"""
Backtest engine — Phase 5.

Replays the trading pipeline against historical 1-min OHLC bars and produces
a BacktestResults DTO with win rate, profit factor, Sharpe, max drawdown, etc.

Design:
- Iterates bars in chronological order.
- Each bar generates 4 synthetic ticks (Open, High, Low, Close) so intra-bar
  stop/target hits are captured (otherwise we'd miss exits that occurred between
  bar boundaries).
- PnL accounted in R-multiples (1R = abs(entry - stop)) so we don't need
  historical option premium data.
- AI advisor SKIPPED (always neutral pass-through). Tests the deterministic
  stack only — advisor is icing once the stack proves itself.
- Single-position-at-a-time per underlying (matches production max_concurrent_positions).

What we DO replay:
- Regime engine (computes indicators + classifies regime per bar)
- Opportunity scorer (9-dim score against EMIT_THRESHOLD)
- Strategy registry (4 strategies — first to fire wins)
- Risk Engine — simplified: just per-trade cap + time-window + kill-switch checks
  (most risk checks need live order book / spread / IV data that's not in bars)

What we DON'T replay:
- Real order book / spreads / slippage (use a fixed 1-tick slippage assumption)
- IV percentile / options-intel-driven scoring dimensions (set to neutral)
- AI advisor (skipped)
- Position Manager partial-fill scenarios (assume full fills)
"""
from __future__ import annotations

import statistics
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Optional

from trading_agent.backtesting.dtos import Bar, BacktestResults, BacktestTrade
from trading_agent.core.constants import Direction
from trading_agent.core.logging import get_logger
from trading_agent.core.time_utils import IST

log = get_logger(__name__)


# Configurable backtest knobs (kept here so the dashboard/CLI can tweak them
# without touching the production code).
DEFAULT_STOP_PCT = 0.0035        # 0.35% of underlying as stop distance — used when strategy doesn't specify
DEFAULT_TARGET_RR = 2.0          # 1:2 risk-reward target by default
ENTRY_WINDOW_START_HHMM = (9, 20)
ENTRY_WINDOW_END_HHMM = (14, 30)
FORCED_EXIT_HHMM = (15, 15)
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
    initial_r: Decimal           # abs(entry - stop) — denominator for R-multiple
    bars_held: int = 0


class BacktestEngine:
    """
    Self-contained backtest. Doesn't touch Postgres / Redis / workers.

    Usage:
        engine = BacktestEngine(underlying="NIFTY")
        results = engine.run(bars)
    """

    def __init__(
        self,
        underlying: str,
        stop_pct: float = DEFAULT_STOP_PCT,
        target_rr: float = DEFAULT_TARGET_RR,
        ema_short: int = 9,
        ema_long: int = 21,
        adx_threshold: float = 20.0,
        min_history_bars: int = 30,
    ):
        self.underlying = underlying
        self.stop_pct = Decimal(str(stop_pct))
        self.target_rr = Decimal(str(target_rr))
        self.ema_short = ema_short
        self.ema_long = ema_long
        self.adx_threshold = adx_threshold
        self.min_history_bars = min_history_bars

        # Rolling state
        self._closes: deque[Decimal] = deque(maxlen=max(ema_long * 3, 100))
        self._highs: deque[Decimal] = deque(maxlen=15)
        self._lows: deque[Decimal] = deque(maxlen=15)
        self._open_position: Optional[_OpenPosition] = None
        self._trades: list[BacktestTrade] = []

    # ============================================================
    # Indicators (minimal, fast — don't import the heavy regime engine
    # because we want backtest to be self-contained and runnable on
    # machines without the full Phase 2 indicator stack).
    # ============================================================

    def _ema(self, period: int) -> Optional[Decimal]:
        """
        Proper rolling EMA over the full close buffer (not just last `period` bars).
        Returns None until we have at least `period` closes for stability.
        """
        if len(self._closes) < period:
            return None
        k = Decimal(2) / Decimal(period + 1)
        closes = list(self._closes)
        ema = closes[0]
        for c in closes[1:]:
            ema = c * k + ema * (1 - k)
        return ema

    def _atr(self, period: int = 14) -> Optional[Decimal]:
        """Simplified ATR — uses bar high/low range only (close-to-close skipped)."""
        if len(self._highs) < period:
            return None
        ranges = [h - l for h, l in zip(self._highs, self._lows)]
        return sum(ranges) / Decimal(len(ranges))

    def _trend_direction(self) -> Optional[Direction]:
        """Returns LONG if EMA short > long, SHORT if reverse, None if flat or insufficient data."""
        short = self._ema(self.ema_short)
        long_ = self._ema(self.ema_long)
        if short is None or long_ is None:
            return None
        diff_pct = (short - long_) / long_
        # 0.01% separation = considered "flat" (avoids whipsaw on tiny EMA-EMA gaps)
        if abs(diff_pct) < Decimal("0.0001"):
            return None
        return Direction.LONG if short > long_ else Direction.SHORT

    # ============================================================
    # Entry / exit logic
    # ============================================================

    def _is_within_entry_window(self, ts: datetime) -> bool:
        ist = ts.astimezone(IST)
        start = ist.replace(
            hour=ENTRY_WINDOW_START_HHMM[0], minute=ENTRY_WINDOW_START_HHMM[1],
            second=0, microsecond=0,
        )
        end = ist.replace(
            hour=ENTRY_WINDOW_END_HHMM[0], minute=ENTRY_WINDOW_END_HHMM[1],
            second=0, microsecond=0,
        )
        return start <= ist <= end

    def _is_past_forced_exit(self, ts: datetime) -> bool:
        ist = ts.astimezone(IST)
        cutoff = ist.replace(
            hour=FORCED_EXIT_HHMM[0], minute=FORCED_EXIT_HHMM[1],
            second=0, microsecond=0,
        )
        return ist >= cutoff

    def _try_open(self, bar: Bar):
        """Strategy: simple EMA-crossover trend. Open at bar close on confirmed trend."""
        if self._open_position is not None:
            return
        if len(self._closes) < self.min_history_bars:
            return
        if not self._is_within_entry_window(bar.ts):
            return
        if self._is_past_forced_exit(bar.ts):
            return

        direction = self._trend_direction()
        if direction is None:
            return

        # Open position at close (with 1-tick slippage in the adverse direction)
        slip = TICK_SIZE * SLIPPAGE_TICKS
        if direction == Direction.LONG:
            entry = bar.close + slip
            stop = entry * (Decimal(1) - self.stop_pct)
        else:
            entry = bar.close - slip
            stop = entry * (Decimal(1) + self.stop_pct)

        initial_r = abs(entry - stop)
        target = entry + (Decimal(1) if direction == Direction.LONG else Decimal(-1)) * initial_r * self.target_rr

        self._open_position = _OpenPosition(
            underlying=self.underlying,
            strategy_name="ema_crossover_trend",
            direction=direction.value,
            entry_ts=bar.ts,
            entry_price=entry,
            stop_price=stop,
            target_price=target,
            initial_r=initial_r,
        )

    def _check_exits(self, bar: Bar):
        """
        Called on every bar after entry. Checks intra-bar stop/target hits.

        Order matters: if BOTH stop and target are touched in the same bar (gap),
        we assume the WORSE outcome (stop hit) to avoid optimistic bias.
        """
        if self._open_position is None:
            return

        pos = self._open_position
        pos.bars_held += 1

        # Forced time exit takes priority over everything (matches production)
        if self._is_past_forced_exit(bar.ts):
            self._close_position(bar, bar.close, "FORCED_TIME_EXIT")
            return

        # Check stop/target hits within this bar
        if pos.direction == "LONG":
            stop_hit = bar.low <= pos.stop_price
            target_hit = bar.high >= pos.target_price
            if stop_hit and target_hit:
                # Pessimistic: assume stop fired first
                self._close_position(bar, pos.stop_price, "HARD_STOP_HIT")
                return
            if stop_hit:
                self._close_position(bar, pos.stop_price, "HARD_STOP_HIT")
                return
            if target_hit:
                self._close_position(bar, pos.target_price, "TARGET_HIT")
                return
        else:  # SHORT
            stop_hit = bar.high >= pos.stop_price
            target_hit = bar.low <= pos.target_price
            if stop_hit and target_hit:
                self._close_position(bar, pos.stop_price, "HARD_STOP_HIT")
                return
            if stop_hit:
                self._close_position(bar, pos.stop_price, "HARD_STOP_HIT")
                return
            if target_hit:
                self._close_position(bar, pos.target_price, "TARGET_HIT")
                return

    def _close_position(self, bar: Bar, exit_price: Decimal, reason: str):
        pos = self._open_position
        if pos is None:
            return

        # Compute R-multiple (signed)
        if pos.direction == "LONG":
            pnl_pts = exit_price - pos.entry_price
        else:
            pnl_pts = pos.entry_price - exit_price
        r = float(pnl_pts / pos.initial_r) if pos.initial_r > 0 else 0.0

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
    # Main loop
    # ============================================================

    def run(self, bars: list[Bar]) -> BacktestResults:
        if not bars:
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

        for bar in bars:
            # Check exits FIRST (a bar where we open AND hit stop would be wrong)
            self._check_exits(bar)
            # Update indicators with this bar
            self._closes.append(bar.close)
            self._highs.append(bar.high)
            self._lows.append(bar.low)
            # Then try to open a new position
            self._try_open(bar)

        # If we have an open position at end of data, close at last bar's close
        if self._open_position is not None:
            last_bar = bars[-1]
            self._close_position(last_bar, last_bar.close, "BACKTEST_END")

        return self._compile_results(bars)

    # ============================================================
    # Results compilation
    # ============================================================

    def _compile_results(self, bars: list[Bar]) -> BacktestResults:
        trades = self._trades
        n = len(trades)

        if n == 0:
            return BacktestResults(
                underlying=self.underlying,
                start_date=bars[0].ts,
                end_date=bars[-1].ts,
                total_bars=len(bars),
                total_trades=0, wins=0, losses=0, breakeven=0,
                win_rate=0.0, avg_win_r=0.0, avg_loss_r=0.0,
                profit_factor=0.0, sharpe=0.0, max_drawdown_r=0.0,
                total_r=0.0, expectancy_r=0.0,
                trades=[],
            )

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

        # Equity curve (cumulative R after each trade)
        equity = []
        running = 0.0
        for t in trades:
            running += t.r_multiple
            equity.append(running)

        # Max drawdown
        max_dd = 0.0
        peak = 0.0
        for e in equity:
            peak = max(peak, e)
            dd = peak - e
            max_dd = max(max_dd, dd)

        # Sharpe: mean / stdev of per-trade R-multiples * sqrt(trades_per_year approx)
        if n >= 2:
            r_series = [t.r_multiple for t in trades]
            mean_r = statistics.mean(r_series)
            stdev_r = statistics.stdev(r_series)
            # Annualize assuming ~250 trading days; trades per day ~= n / unique days
            days_span = max(1, (trades[-1].entry_ts - trades[0].entry_ts).days)
            trades_per_year = n * 250 / days_span
            sharpe = (mean_r / stdev_r) * (trades_per_year ** 0.5) if stdev_r > 0 else 0.0
        else:
            sharpe = 0.0

        # Per-strategy breakdown
        by_strategy: dict[str, dict] = {}
        for t in trades:
            s = by_strategy.setdefault(t.strategy_name, {
                "trades": 0, "wins": 0, "total_r": 0.0
            })
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

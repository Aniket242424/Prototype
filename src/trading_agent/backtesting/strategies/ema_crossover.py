"""
EMA crossover trend strategy — backtest plugin.

Extracted from the original built-in BacktestEngine logic so the engine
becomes strategy-agnostic. Same behavior as before refactor.

Rules:
- Entry window 09:20-14:30 IST
- Force exit at 15:15 IST
- EMA(9) vs EMA(21) with 0.01% separation threshold to call a trend
- Stop = stop_pct fraction of underlying
- Target = stop_distance * target_rr
"""
from __future__ import annotations

from datetime import datetime, time
from decimal import Decimal
from typing import Optional

from trading_agent.backtesting.dtos import Bar
from trading_agent.backtesting.strategies.base import EntryDecision
from trading_agent.core.constants import Direction
from trading_agent.core.time_utils import IST


class EmaCrossoverStrategy:
    """EMA(9/21) crossover trend follow."""

    name = "ema_crossover_trend"

    def __init__(
        self,
        ema_short: int = 9,
        ema_long: int = 21,
        flat_threshold_pct: float = 0.0001,  # 0.01% separation = "flat"
        stop_pct: float = 0.0035,
        target_rr: float = 2.0,
        min_history_bars: int = 30,
        entry_window_start: time = time(9, 20),
        entry_window_end: time = time(14, 30),
        force_exit_time: time = time(15, 15),
    ):
        self.ema_short = ema_short
        self.ema_long = ema_long
        self.flat_threshold = Decimal(str(flat_threshold_pct))
        self.stop_pct = stop_pct
        self.target_rr = target_rr
        self.min_history_bars = min_history_bars
        self.entry_window_start = entry_window_start
        self.entry_window_end = entry_window_end
        self.force_exit_time = force_exit_time

    def is_entry_time(self, ts: datetime) -> bool:
        ist = ts.astimezone(IST).time()
        return self.entry_window_start <= ist <= self.entry_window_end

    def is_force_exit_time(self, ts: datetime) -> bool:
        return ts.astimezone(IST).time() >= self.force_exit_time

    def should_open(self, bar: Bar, history: list[Bar]) -> Optional[EntryDecision]:
        if len(history) < self.min_history_bars:
            return None
        # Include current bar in indicator computation
        all_bars = history + [bar]
        short = _ema(all_bars, self.ema_short)
        long_ = _ema(all_bars, self.ema_long)
        if short is None or long_ is None:
            return None
        diff_pct = (short - long_) / long_
        if abs(diff_pct) < self.flat_threshold:
            return None
        direction = Direction.LONG if short > long_ else Direction.SHORT
        return EntryDecision(
            direction=direction,
            stop_pct=self.stop_pct,
            target_rr=self.target_rr,
            rationale=f"EMA({self.ema_short})={float(short):.2f} vs EMA({self.ema_long})={float(long_):.2f}",
        )


def _ema(bars: list[Bar], period: int) -> Optional[Decimal]:
    if len(bars) < period:
        return None
    k = Decimal(2) / Decimal(period + 1)
    closes = [b.close for b in bars]
    ema = closes[0]
    for c in closes[1:]:
        ema = c * k + ema * (1 - k)
    return ema

"""
End-of-Day Momentum Continuation strategy (EOD-MC) — Phase 5 R&D.

HYPOTHESIS
==========
In Indian index markets, the last 45 minutes (14:45 - 15:30 IST) often shows
continuation moves when:
  - Strong intraday trend exists (sustained directional pressure)
  - Volatility is expanding (institutional positioning, options gamma squeeze)
  - Price is on the right side of a medium-term EMA
  - No exhaustion signals (no doji, no rejection candles)

WHY THIS COULD WORK
- Institutional flows late-day (index futures rollover, options pinning)
- Squeeze effects: weak holders forced to cover before close
- Self-fulfilling momentum from retail trend chasers

WHY IT COULD FAIL
- Theta acceleration murders option buyers as 15:30 approaches
- Bid-ask spreads widen near close
- Counter-trend mean reversion to VWAP
- Forced 15:25 exit leaves only ~10 min for trade to develop

ENTRY RULES (all must be true)
1. Time within [14:45, 15:10] IST
2. Of the last 5 bars (excluding current), at least 4 closed in the trend direction
3. Current close is on the right side of EMA(20)
4. ATR(5) > ATR(20) * 1.1  (volatility expanding)
5. Current bar is NOT a doji: |close - open| >= 0.35 * (high - low)
6. Current bar is in the same direction as the trend
   (i.e., we enter on a clean continuation candle, not a fade)

POSITION SIZING
- Stop:  max(0.20% of underlying, 0.8 * ATR(14))   — tight, time is enemy
- Target: 2.0x stop distance                        — 1:2 RR
- Force exit: 15:25 IST (5 min before close, cushion for fills)

EXPECTED CHARACTERISTICS
- Few trades (1-2 per day max)
- Short hold time (5-30 min)
- Win rate target: 55-65% (validated by backtest, not assumed)
- Avg win < theoretical 2R due to early profit-taking by forced 15:25 exit
"""
from __future__ import annotations

from datetime import datetime, time
from decimal import Decimal
from typing import Optional

from trading_agent.backtesting.dtos import Bar
from trading_agent.backtesting.strategies.base import EntryDecision
from trading_agent.core.constants import Direction
from trading_agent.core.time_utils import IST


# Strategy-tunable knobs (kept here so calibration loops can sweep them)
ENTRY_WINDOW_START = time(14, 45)
ENTRY_WINDOW_END = time(15, 10)
FORCE_EXIT_TIME = time(15, 25)

EMA_PERIOD = 20
ATR_SHORT = 5
ATR_LONG = 20
VOL_EXPANSION_MULTIPLE = 1.10

TREND_LOOKBACK_BARS = 5
TREND_DIRECTIONAL_THRESHOLD = 4   # 4 of last 5 same direction
NON_DOJI_BODY_RATIO = 0.35        # body must be >= 35% of full range
MIN_HISTORY_BARS = ATR_LONG + 2

STOP_PCT_MIN = 0.0020              # 0.20% floor
STOP_ATR_MULTIPLE = 0.8
TARGET_RR = 2.0


class EndOfDayMomentumStrategy:
    """Pluggable BacktestStrategy implementing EOD-MC."""

    name = "eod_momentum"

    # ---------------------- Time windows ----------------------

    def is_entry_time(self, ts: datetime) -> bool:
        ist = ts.astimezone(IST).time()
        return ENTRY_WINDOW_START <= ist <= ENTRY_WINDOW_END

    def is_force_exit_time(self, ts: datetime) -> bool:
        ist = ts.astimezone(IST).time()
        return ist >= FORCE_EXIT_TIME

    # ---------------------- Entry logic ----------------------

    def should_open(self, bar: Bar, history: list[Bar]) -> Optional[EntryDecision]:
        # Need enough history for indicators
        if len(history) < MIN_HISTORY_BARS:
            return None

        # Compute indicators
        ema20 = _ema(history + [bar], EMA_PERIOD)
        atr_short = _atr(history[-ATR_SHORT:] + [bar])
        atr_long = _atr(history[-ATR_LONG:] + [bar])
        if ema20 is None or atr_short is None or atr_long is None:
            return None

        # Rule 4: volatility must be expanding
        if atr_short < atr_long * Decimal(str(VOL_EXPANSION_MULTIPLE)):
            return None

        # Rule 5: current bar must NOT be a doji
        full_range = bar.high - bar.low
        if full_range <= 0:
            return None  # degenerate bar
        body = abs(bar.close - bar.open)
        if body < full_range * Decimal(str(NON_DOJI_BODY_RATIO)):
            return None

        # Rule 2: 4 of last 5 bars in same direction
        last5 = history[-TREND_LOOKBACK_BARS:]
        long_bars = sum(1 for b in last5 if b.close > b.open)
        short_bars = sum(1 for b in last5 if b.close < b.open)

        if long_bars >= TREND_DIRECTIONAL_THRESHOLD:
            trend_dir = Direction.LONG
        elif short_bars >= TREND_DIRECTIONAL_THRESHOLD:
            trend_dir = Direction.SHORT
        else:
            return None

        # Rule 3: price must be on the right side of EMA20
        if trend_dir == Direction.LONG and bar.close <= ema20:
            return None
        if trend_dir == Direction.SHORT and bar.close >= ema20:
            return None

        # Rule 6: current bar must be in the same direction as the trend
        # (don't enter LONG on a red candle even if the prior trend was up)
        current_bar_is_long = bar.close > bar.open
        if trend_dir == Direction.LONG and not current_bar_is_long:
            return None
        if trend_dir == Direction.SHORT and current_bar_is_long:
            return None

        # All checks pass — compute stop sizing
        underlying_pct_stop = STOP_PCT_MIN
        atr_based_stop_pct = float(atr_long * Decimal(str(STOP_ATR_MULTIPLE)) / bar.close)
        final_stop_pct = max(underlying_pct_stop, atr_based_stop_pct)

        return EntryDecision(
            direction=trend_dir,
            stop_pct=final_stop_pct,
            target_rr=TARGET_RR,
            rationale=(
                f"EOD-MC {trend_dir.value}: {long_bars if trend_dir == Direction.LONG else short_bars}/5 trend bars, "
                f"ATR exp {float(atr_short / atr_long):.2f}x, "
                f"body/range {float(body / full_range):.2f}, "
                f"close vs EMA20: {float(bar.close - ema20):.2f}"
            ),
        )


# ============================================================
# Indicator helpers (kept local — no dependency on production engine)
# ============================================================

def _ema(bars: list[Bar], period: int) -> Optional[Decimal]:
    if len(bars) < period:
        return None
    k = Decimal(2) / Decimal(period + 1)
    closes = [b.close for b in bars]
    ema = closes[0]
    for c in closes[1:]:
        ema = c * k + ema * (1 - k)
    return ema


def _atr(bars: list[Bar]) -> Optional[Decimal]:
    if len(bars) < 1:
        return None
    ranges = [b.high - b.low for b in bars]
    return sum(ranges) / Decimal(len(ranges))

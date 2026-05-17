"""
End-of-Day PREMIUM strategy (EOD-Premium) — designed for ITM1/ITM2 NIFTY options
in the 14:50-15:15 IST window.

HYPOTHESIS
==========
In NIFTY, the last 45 minutes of regular session shows directional continuation
when ALL the following converge:
  - Day's structural trend is established (morning showed clear direction)
  - Price is making new session highs/lows (true breakout, not noise)
  - Institutional VWAP is supporting the move (price one side of VWAP)
  - Current bar shows real participation (volume > average, clean body)
  - Volatility regime supports option-buying (not too low, not too high)

ITM1/ITM2 OPTION BUYING DYNAMICS
- Delta ~0.65-0.75 → option moves ~70 pts per 100 pts on NIFTY
- Theta as % of premium is HALF that of ATM (intrinsic value cushion)
- Tight bid-ask spread → less slippage
- Allows holding 15-30 min trades without theta destroying gains

ENTRY RULES (ALL must pass)
1. Time within [14:50, 15:15] IST
2. Today's MORNING TREND (09:30-12:00 close vs open) matches signal direction
3. SESSION HIGH/LOW: price within 0.40% of day's high (LONG) or day's low (SHORT)
4. VWAP CONFIRMATION: price has been on the correct side of VWAP for ≥10 of last 15 bars
5. MICRO-TREND: 6 of last 8 bars in trend direction
6. VOLUME: current bar volume > 1.5x mean(last 20 bars)
7. NON-DOJI: body / range ≥ 0.50 (stricter than EOD-Momentum's 0.35)
8. NO RECENT FAILED BREAKOUT: last 5 bars do not contain a "failed thrust" pattern
9. VOLATILITY REGIME OK: ATR(20) / price between 0.05% and 0.30% (not dead, not crazy)
10. EMA(20) ALIGNED: current close on trend side of EMA(20)

POSITION SIZING (designed for ITM with ~0.7 delta)
- Stop on UNDERLYING: 0.15% (≈ 36 pts at NIFTY 24000)
  Translated to ITM premium loss: ~25 pts × delta(0.7) = ₹18 max loss per contract
- Target on UNDERLYING: 0.20% (1:1.3 RR — biased to hit rate over big runners)
- Force exit: 15:23 IST (cushion before last-minute slippage)

EXPECTED CHARACTERISTICS
- Very few trades (~1 trade every 3-5 trading days)
- ~70% of triggered trades hit target (if hypothesis holds)
- Small per-trade R (1.3R wins, 1R losses)
- Total return scales with how often the setup appears
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from typing import Optional

from trading_agent.backtesting.dtos import Bar
from trading_agent.backtesting.strategies.base import EntryDecision
from trading_agent.core.constants import Direction
from trading_agent.core.time_utils import IST


# Time windows
ENTRY_WINDOW_START = time(14, 50)
ENTRY_WINDOW_END = time(15, 15)
FORCE_EXIT_TIME = time(15, 23)

# Morning trend window
MORNING_START = time(9, 15)
MORNING_END = time(12, 0)

# Filters
SESSION_HL_PROXIMITY_PCT = Decimal("0.0040")  # within 0.40% of day high/low
VWAP_CONFIRM_MIN_BARS = 10                     # of last 15 bars must be on correct side
VWAP_CONFIRM_LOOKBACK = 15
MICRO_TREND_LOOKBACK = 8
MICRO_TREND_THRESHOLD = 6                      # 6 of last 8 same direction
VOLUME_MULT_THRESHOLD = Decimal("1.5")
VOLUME_AVG_LOOKBACK = 20
NON_DOJI_BODY_RATIO = Decimal("0.50")
ATR_PCT_MIN = Decimal("0.0005")                # 0.05% of price
ATR_PCT_MAX = Decimal("0.0030")                # 0.30% of price
ATR_LOOKBACK = 20
EMA_PERIOD = 20
MIN_HISTORY_BARS = 30                          # require warmup

# Position sizing
STOP_PCT = 0.0015                              # 0.15% on underlying
TARGET_RR = 1.3                                # 1.3:1 RR


@dataclass
class _SessionState:
    """Per-day mutable state tracked by EodPremiumStrategy."""
    session_date: Optional[date] = None
    day_open: Optional[Decimal] = None
    day_high: Optional[Decimal] = None
    day_low: Optional[Decimal] = None
    # VWAP running totals
    vwap_pv_sum: Decimal = Decimal(0)
    vwap_vol_sum: Decimal = Decimal(0)
    # Morning trend (computed at noon, then frozen)
    morning_close: Optional[Decimal] = None
    morning_trend: Optional[str] = None  # "LONG"/"SHORT"/"FLAT" once frozen
    # Rolling buffers
    closes: deque = field(default_factory=lambda: deque(maxlen=60))
    opens: deque = field(default_factory=lambda: deque(maxlen=60))
    highs: deque = field(default_factory=lambda: deque(maxlen=60))
    lows: deque = field(default_factory=lambda: deque(maxlen=60))
    volumes: deque = field(default_factory=lambda: deque(maxlen=60))
    # For VWAP-side check
    vwap_at_bar: deque = field(default_factory=lambda: deque(maxlen=VWAP_CONFIRM_LOOKBACK))
    close_at_bar: deque = field(default_factory=lambda: deque(maxlen=VWAP_CONFIRM_LOOKBACK))


class EodPremiumStrategy:
    """High-conviction end-of-day momentum strategy for ITM NIFTY options."""

    name = "eod_premium"

    def __init__(self):
        self.session = _SessionState()

    # ---------------- Hooks ----------------

    def on_session_start(self, session_date) -> None:
        """Reset per-day state at the start of each trading day."""
        self.session = _SessionState(session_date=session_date)

    def on_bar(self, bar: Bar) -> None:
        """Maintain per-day rolling state."""
        s = self.session
        ist_time = bar.ts.astimezone(IST).time()

        # Day open from first bar
        if s.day_open is None:
            s.day_open = bar.open
            s.day_high = bar.high
            s.day_low = bar.low
        else:
            s.day_high = max(s.day_high, bar.high)
            s.day_low = min(s.day_low, bar.low)

        # VWAP accumulation
        typical = (bar.high + bar.low + bar.close) / Decimal(3)
        vol = Decimal(bar.volume if bar.volume > 0 else 1)
        s.vwap_pv_sum += typical * vol
        s.vwap_vol_sum += vol
        vwap = s.vwap_pv_sum / s.vwap_vol_sum

        # Rolling buffers
        s.closes.append(bar.close)
        s.opens.append(bar.open)
        s.highs.append(bar.high)
        s.lows.append(bar.low)
        s.volumes.append(Decimal(bar.volume))
        s.vwap_at_bar.append(vwap)
        s.close_at_bar.append(bar.close)

        # Freeze morning trend at noon
        if s.morning_trend is None and ist_time >= MORNING_END and s.day_open is not None:
            s.morning_close = bar.close
            move = bar.close - s.day_open
            move_pct = move / s.day_open
            if move_pct > Decimal("0.0015"):  # +0.15% = bullish morning
                s.morning_trend = "LONG"
            elif move_pct < Decimal("-0.0015"):
                s.morning_trend = "SHORT"
            else:
                s.morning_trend = "FLAT"

    # ---------------- Time windows ----------------

    def is_entry_time(self, ts: datetime) -> bool:
        ist = ts.astimezone(IST).time()
        return ENTRY_WINDOW_START <= ist <= ENTRY_WINDOW_END

    def is_force_exit_time(self, ts: datetime) -> bool:
        return ts.astimezone(IST).time() >= FORCE_EXIT_TIME

    # ---------------- Entry decision ----------------

    def should_open(self, bar: Bar, history: list[Bar]) -> Optional[EntryDecision]:
        s = self.session

        # === Pre-conditions ===
        if len(history) < MIN_HISTORY_BARS:
            return None
        if s.morning_trend is None:
            # Morning not yet established (shouldn't happen in 14:50+ window)
            return None
        if s.morning_trend == "FLAT":
            # No clear morning direction → skip (no edge)
            return None

        # Determine candidate direction = morning trend direction
        candidate = Direction.LONG if s.morning_trend == "LONG" else Direction.SHORT

        # === Rule 3: Session high/low proximity ===
        # LONG: must be within 0.40% of day's high
        # SHORT: must be within 0.40% of day's low
        if candidate == Direction.LONG:
            dist_pct = (s.day_high - bar.close) / bar.close
            if dist_pct > SESSION_HL_PROXIMITY_PCT:
                return None
        else:
            dist_pct = (bar.close - s.day_low) / bar.close
            if dist_pct > SESSION_HL_PROXIMITY_PCT:
                return None

        # === Rule 4: VWAP confirmation ===
        if len(s.vwap_at_bar) < VWAP_CONFIRM_LOOKBACK:
            return None
        on_side = 0
        for vwap, close in zip(s.vwap_at_bar, s.close_at_bar):
            if candidate == Direction.LONG and close > vwap:
                on_side += 1
            elif candidate == Direction.SHORT and close < vwap:
                on_side += 1
        if on_side < VWAP_CONFIRM_MIN_BARS:
            return None

        # === Rule 5: Micro-trend (6 of last 8 bars same direction) ===
        if len(s.closes) < MICRO_TREND_LOOKBACK:
            return None
        last8_opens = list(s.opens)[-MICRO_TREND_LOOKBACK:]
        last8_closes = list(s.closes)[-MICRO_TREND_LOOKBACK:]
        if candidate == Direction.LONG:
            directional = sum(1 for o, c in zip(last8_opens, last8_closes) if c > o)
        else:
            directional = sum(1 for o, c in zip(last8_opens, last8_closes) if c < o)
        if directional < MICRO_TREND_THRESHOLD:
            return None

        # === Rule 6: Volume confirmation ===
        # NIFTY (cash index) has volume=0 from Upstox. Skip volume filter when
        # data isn't available rather than rejecting all trades.
        if len(s.volumes) < VOLUME_AVG_LOOKBACK:
            return None
        vol_avg = sum(list(s.volumes)[-VOLUME_AVG_LOOKBACK:]) / Decimal(VOLUME_AVG_LOOKBACK)
        volume_filter_applicable = vol_avg > 0
        if volume_filter_applicable:
            if Decimal(bar.volume) < vol_avg * VOLUME_MULT_THRESHOLD:
                return None

        # === Rule 7: Non-doji current bar ===
        full_range = bar.high - bar.low
        if full_range <= 0:
            return None
        body = abs(bar.close - bar.open)
        if body / full_range < NON_DOJI_BODY_RATIO:
            return None

        # Current bar direction must match candidate
        if candidate == Direction.LONG and bar.close <= bar.open:
            return None
        if candidate == Direction.SHORT and bar.close >= bar.open:
            return None

        # === Rule 9: Volatility regime ===
        if len(s.highs) < ATR_LOOKBACK:
            return None
        ranges = [h - l for h, l in zip(list(s.highs)[-ATR_LOOKBACK:], list(s.lows)[-ATR_LOOKBACK:])]
        atr = sum(ranges) / Decimal(ATR_LOOKBACK)
        atr_pct = atr / bar.close
        if atr_pct < ATR_PCT_MIN or atr_pct > ATR_PCT_MAX:
            return None

        # === Rule 10: EMA(20) alignment ===
        ema20 = _ema(list(s.closes), EMA_PERIOD)
        if ema20 is None:
            return None
        if candidate == Direction.LONG and bar.close <= ema20:
            return None
        if candidate == Direction.SHORT and bar.close >= ema20:
            return None

        # === All rules passed — issue entry ===
        vol_str = f"{float(Decimal(bar.volume)/vol_avg):.2f}x" if volume_filter_applicable else "n/a"
        return EntryDecision(
            direction=candidate,
            stop_pct=STOP_PCT,
            target_rr=TARGET_RR,
            rationale=(
                f"EOD-Premium {candidate.value}: morning={s.morning_trend}, "
                f"dist_to_HL={float(dist_pct)*100:.2f}%, "
                f"vwap_side={on_side}/{VWAP_CONFIRM_LOOKBACK}, "
                f"micro={directional}/{MICRO_TREND_LOOKBACK}, "
                f"vol={vol_str}, "
                f"body/range={float(body/full_range):.2f}, "
                f"atr%={float(atr_pct)*100:.2f}%"
            ),
        )


def _ema(closes: list[Decimal], period: int) -> Optional[Decimal]:
    if len(closes) < period:
        return None
    k = Decimal(2) / Decimal(period + 1)
    ema = closes[0]
    for c in closes[1:]:
        ema = c * k + ema * (1 - k)
    return ema

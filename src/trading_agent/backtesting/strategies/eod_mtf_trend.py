"""
End-of-Day Multi-Timeframe Trend strategy (EOD-MTF) — Phase 5 R&D.

HYPOTHESIS
==========
Combining MULTIPLE timeframes for trend confirmation is one of the few
durable edges in retail trading. The idea:

  - 15-minute trend = the "weather" (slow, dominant)
  - 5-minute trend  = the "wind" (medium confirmation)
  - 1-minute bar    = the "leaf" (precise entry timing)

A trade only fires when ALL THREE align. This is what professional discretionary
traders look for; we just systematize it.

For the 14:45-15:25 IST EOD window specifically:
  - 15-min trend from 11:30 to 14:45 should be clearly directional
  - 5-min trend in the last 30-60 min should confirm
  - 1-min entry candle should be a clean continuation pattern

ENTRY RULES (all must pass)
1. Time within [14:50, 15:15] IST
2. 15-min EMA(5) vs EMA(20): clear separation (>0.15%) in candidate direction
3. 5-min EMA(9) vs EMA(21): aligned with 15-min direction
4. Last 5 of last 7 fifteen-min bars closed in candidate direction
5. Current 1-min bar matches direction (close > open for LONG)
6. Current 1-min bar body >= 50% of range (no doji)
7. Price one side of session VWAP for last 30 minutes (institutional flow)
8. Price within 0.45% of session high (LONG) or low (SHORT)
9. ATR(20) on 1-min between 0.04% and 0.30% of price (avoid dead / blow-off vol)

POSITION SIZING (for ITM1/ITM2 NIFTY options, delta ~0.7)
- Stop: 0.18% of underlying (~43 pts at 24000)
- Target: 1:1.5 RR (slight bias to win rate)
- Force exit: 15:25 IST
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from decimal import Decimal
from typing import Optional

from trading_agent.backtesting.dtos import Bar
from trading_agent.backtesting.strategies.base import EntryDecision
from trading_agent.core.constants import Direction
from trading_agent.core.time_utils import IST


# Time windows
ENTRY_WINDOW_START = time(14, 50)
ENTRY_WINDOW_END = time(15, 15)
FORCE_EXIT_TIME = time(15, 25)

# Indicator params
EMA15_SHORT = 5    # 15-min EMA(5)
EMA15_LONG = 20    # 15-min EMA(20)
EMA5_SHORT = 9     # 5-min EMA(9)
EMA5_LONG = 21     # 5-min EMA(21)
TREND_SEP_THRESHOLD = Decimal("0.0015")  # 0.15% min separation for "clear trend"

# 15-min bar history
H15_TREND_LOOKBACK = 7
H15_DIRECTIONAL_REQ = 5
H15_BARS_TO_KEEP = 30

# 5-min bar history
H5_BARS_TO_KEEP = 60

# 1-min filters
ATR_LOOKBACK = 20
ATR_PCT_MIN = Decimal("0.0004")
ATR_PCT_MAX = Decimal("0.0030")
NON_DOJI_BODY_RATIO = Decimal("0.50")
SESSION_HL_PROXIMITY = Decimal("0.0045")
VWAP_CONFIRM_LOOKBACK = 30
VWAP_CONFIRM_MIN_BARS = 22  # of last 30 1-min bars must be on right side

# Position sizing
STOP_PCT = 0.0018
TARGET_RR = 1.5


@dataclass
class _Bar15:
    """Aggregated 15-min bar."""
    ts: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal


@dataclass
class _Bar5:
    """Aggregated 5-min bar."""
    ts: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal


@dataclass
class _MTFSession:
    session_date: Optional[datetime] = None
    day_open: Optional[Decimal] = None
    day_high: Optional[Decimal] = None
    day_low: Optional[Decimal] = None
    vwap_pv_sum: Decimal = Decimal(0)
    vwap_vol_sum: Decimal = Decimal(0)
    # 1-min buffers
    closes_1m: deque = field(default_factory=lambda: deque(maxlen=60))
    highs_1m: deque = field(default_factory=lambda: deque(maxlen=60))
    lows_1m: deque = field(default_factory=lambda: deque(maxlen=60))
    vwap_at_bar: deque = field(default_factory=lambda: deque(maxlen=VWAP_CONFIRM_LOOKBACK))
    close_at_bar: deque = field(default_factory=lambda: deque(maxlen=VWAP_CONFIRM_LOOKBACK))
    # 5-min and 15-min aggregations
    bars_5m: deque = field(default_factory=lambda: deque(maxlen=H5_BARS_TO_KEEP))
    bars_15m: deque = field(default_factory=lambda: deque(maxlen=H15_BARS_TO_KEEP))
    # In-flight aggregation buckets
    _5m_bucket: Optional[_Bar5] = None
    _15m_bucket: Optional[_Bar15] = None
    _5m_bucket_start: Optional[datetime] = None
    _15m_bucket_start: Optional[datetime] = None


class EodMtfTrendStrategy:
    """Multi-timeframe trend follower for the EOD window."""

    name = "eod_mtf_trend"

    def __init__(self):
        self.session = _MTFSession()

    # ---- Hooks ----

    def on_session_start(self, session_date) -> None:
        self.session = _MTFSession(session_date=session_date)

    def on_bar(self, bar: Bar) -> None:
        """Maintain per-day session state + aggregate 1-min bars into 5-min and 15-min."""
        s = self.session

        # Day open/high/low
        if s.day_open is None:
            s.day_open = bar.open
            s.day_high = bar.high
            s.day_low = bar.low
        else:
            s.day_high = max(s.day_high, bar.high)
            s.day_low = min(s.day_low, bar.low)

        # VWAP (cumulative typical-price weighted by volume; falls back to count for indices with vol=0)
        typical = (bar.high + bar.low + bar.close) / Decimal(3)
        vol = Decimal(bar.volume if bar.volume > 0 else 1)
        s.vwap_pv_sum += typical * vol
        s.vwap_vol_sum += vol
        vwap = s.vwap_pv_sum / s.vwap_vol_sum

        # 1-min buffers
        s.closes_1m.append(bar.close)
        s.highs_1m.append(bar.high)
        s.lows_1m.append(bar.low)
        s.vwap_at_bar.append(vwap)
        s.close_at_bar.append(bar.close)

        # Aggregate into 5-min bars (anchored at minute % 5 == 0)
        bar_minute = bar.ts.astimezone(IST).replace(second=0, microsecond=0)
        five_min_anchor = bar_minute.replace(minute=(bar_minute.minute // 5) * 5)
        self._update_aggregate(s, bar, "_5m_bucket", "_5m_bucket_start", "bars_5m",
                               _Bar5, five_min_anchor, 5)

        # Aggregate into 15-min bars (anchored at minute % 15 == 0)
        fifteen_min_anchor = bar_minute.replace(minute=(bar_minute.minute // 15) * 15)
        self._update_aggregate(s, bar, "_15m_bucket", "_15m_bucket_start", "bars_15m",
                               _Bar15, fifteen_min_anchor, 15)

    def _update_aggregate(self, s, bar: Bar, bucket_attr: str, start_attr: str,
                          out_deque_attr: str, bar_cls, anchor_ts, period_min: int):
        bucket = getattr(s, bucket_attr)
        bucket_start = getattr(s, start_attr)
        if bucket is None or bucket_start != anchor_ts:
            # Flush previous bucket into output deque
            if bucket is not None:
                getattr(s, out_deque_attr).append(bucket)
            # Start new bucket
            new_bucket = bar_cls(
                ts=anchor_ts, open=bar.open, high=bar.high, low=bar.low, close=bar.close
            )
            setattr(s, bucket_attr, new_bucket)
            setattr(s, start_attr, anchor_ts)
        else:
            # Extend existing bucket
            bucket.high = max(bucket.high, bar.high)
            bucket.low = min(bucket.low, bar.low)
            bucket.close = bar.close

    # ---- Time windows ----

    def is_entry_time(self, ts: datetime) -> bool:
        ist = ts.astimezone(IST).time()
        return ENTRY_WINDOW_START <= ist <= ENTRY_WINDOW_END

    def is_force_exit_time(self, ts: datetime) -> bool:
        return ts.astimezone(IST).time() >= FORCE_EXIT_TIME

    # ---- Entry ----

    def should_open(self, bar: Bar, history: list[Bar]) -> Optional[EntryDecision]:
        s = self.session

        # === Rule 2: 15-min EMA trend ===
        bars15 = list(s.bars_15m)
        if len(bars15) < EMA15_LONG:
            return None
        closes_15 = [b.close for b in bars15]
        ema15_short = _ema(closes_15, EMA15_SHORT)
        ema15_long = _ema(closes_15, EMA15_LONG)
        if ema15_short is None or ema15_long is None:
            return None
        sep_15 = (ema15_short - ema15_long) / ema15_long
        if abs(sep_15) < TREND_SEP_THRESHOLD:
            return None
        candidate = Direction.LONG if sep_15 > 0 else Direction.SHORT

        # === Rule 3: 5-min EMA aligned with 15-min ===
        bars5 = list(s.bars_5m)
        if len(bars5) < EMA5_LONG:
            return None
        closes_5 = [b.close for b in bars5]
        ema5_short = _ema(closes_5, EMA5_SHORT)
        ema5_long = _ema(closes_5, EMA5_LONG)
        if ema5_short is None or ema5_long is None:
            return None
        if candidate == Direction.LONG and ema5_short <= ema5_long:
            return None
        if candidate == Direction.SHORT and ema5_short >= ema5_long:
            return None

        # === Rule 4: 5 of last 7 fifteen-min bars in candidate direction ===
        last7 = bars15[-H15_TREND_LOOKBACK:]
        if candidate == Direction.LONG:
            directional = sum(1 for b in last7 if b.close > b.open)
        else:
            directional = sum(1 for b in last7 if b.close < b.open)
        if directional < H15_DIRECTIONAL_REQ:
            return None

        # === Rule 5+6: Current 1-min bar non-doji + correct direction ===
        full_range = bar.high - bar.low
        if full_range <= 0:
            return None
        body = abs(bar.close - bar.open)
        if body / full_range < NON_DOJI_BODY_RATIO:
            return None
        if candidate == Direction.LONG and bar.close <= bar.open:
            return None
        if candidate == Direction.SHORT and bar.close >= bar.open:
            return None

        # === Rule 7: VWAP confirmation ===
        if len(s.vwap_at_bar) < VWAP_CONFIRM_LOOKBACK:
            return None
        on_side = 0
        for v, c in zip(s.vwap_at_bar, s.close_at_bar):
            if candidate == Direction.LONG and c > v:
                on_side += 1
            elif candidate == Direction.SHORT and c < v:
                on_side += 1
        if on_side < VWAP_CONFIRM_MIN_BARS:
            return None

        # === Rule 8: session H/L proximity ===
        if candidate == Direction.LONG:
            dist_pct = (s.day_high - bar.close) / bar.close
        else:
            dist_pct = (bar.close - s.day_low) / bar.close
        if dist_pct > SESSION_HL_PROXIMITY:
            return None

        # === Rule 9: ATR regime ===
        if len(s.highs_1m) < ATR_LOOKBACK:
            return None
        ranges = [h - l for h, l in zip(list(s.highs_1m)[-ATR_LOOKBACK:],
                                         list(s.lows_1m)[-ATR_LOOKBACK:])]
        atr = sum(ranges) / Decimal(ATR_LOOKBACK)
        atr_pct = atr / bar.close
        if atr_pct < ATR_PCT_MIN or atr_pct > ATR_PCT_MAX:
            return None

        # === All checks passed ===
        return EntryDecision(
            direction=candidate,
            stop_pct=STOP_PCT,
            target_rr=TARGET_RR,
            rationale=(
                f"EOD-MTF {candidate.value}: 15m_sep={float(sep_15)*100:.2f}%, "
                f"5m_aligned, 15m_trend={directional}/{H15_TREND_LOOKBACK}, "
                f"vwap={on_side}/{VWAP_CONFIRM_LOOKBACK}, "
                f"dist_HL={float(dist_pct)*100:.2f}%, "
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

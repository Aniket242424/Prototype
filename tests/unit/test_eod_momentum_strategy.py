"""Tests for the End-of-Day Momentum Continuation strategy."""
from __future__ import annotations

from datetime import datetime, time, timedelta
from decimal import Decimal

import pytest

from trading_agent.backtesting.dtos import Bar
from trading_agent.backtesting.engine import BacktestEngine
from trading_agent.backtesting.strategies.eod_momentum import EndOfDayMomentumStrategy
from trading_agent.core.constants import Direction
from trading_agent.core.time_utils import IST


def _bar(ts: datetime, o: float, h: float, l: float, c: float, vol: int = 1000) -> Bar:
    return Bar(
        ts=ts,
        open=Decimal(str(o)),
        high=Decimal(str(h)),
        low=Decimal(str(l)),
        close=Decimal(str(c)),
        volume=vol,
    )


def _t(hour: int, minute: int = 0, day: int = 15) -> datetime:
    return datetime(2026, 5, day, hour, minute, tzinfo=IST)


# ============================================================
# Time-window enforcement
# ============================================================

def test_eod_is_entry_time_within_window():
    s = EndOfDayMomentumStrategy()
    assert s.is_entry_time(_t(14, 45)) is True
    assert s.is_entry_time(_t(15, 0)) is True
    assert s.is_entry_time(_t(15, 10)) is True


def test_eod_is_entry_time_outside_window():
    s = EndOfDayMomentumStrategy()
    assert s.is_entry_time(_t(14, 44)) is False
    assert s.is_entry_time(_t(15, 11)) is False
    assert s.is_entry_time(_t(9, 30)) is False
    assert s.is_entry_time(_t(13, 0)) is False


def test_eod_force_exit_at_1525():
    s = EndOfDayMomentumStrategy()
    assert s.is_force_exit_time(_t(15, 25)) is True
    assert s.is_force_exit_time(_t(15, 30)) is True
    assert s.is_force_exit_time(_t(15, 24)) is False
    assert s.is_force_exit_time(_t(15, 0)) is False


# ============================================================
# Entry rules — should_open
# ============================================================

def _build_uptrend_history(start_price: float = 24000.0, count: int = 25) -> list[Bar]:
    """4-of-5 uptrend bars + steady close > EMA20 + expanding volatility."""
    bars = []
    base = _t(14, 20)  # 25 min before entry window opens
    p = start_price
    for i in range(count):
        ts = base + timedelta(minutes=i)
        o = p
        # Bias: mostly green candles, gentle uptrend
        c = p + 3.0 if i % 5 != 4 else p + 1.0   # bar index 4, 9, ... still up but smaller
        h = max(o, c) + 0.5
        l = min(o, c) - 0.3
        bars.append(_bar(ts, o, h, l, c))
        p = c
    return bars


def test_eod_rejects_when_not_enough_history():
    s = EndOfDayMomentumStrategy()
    history = _build_uptrend_history(count=10)  # < MIN_HISTORY_BARS (22)
    cur = _bar(_t(14, 50), 24050, 24060, 24048, 24058)
    assert s.should_open(cur, history) is None


def test_eod_opens_long_on_clean_uptrend():
    s = EndOfDayMomentumStrategy()
    history = _build_uptrend_history(start_price=24000.0, count=25)
    last_close = float(history[-1].close)
    # Strong continuation candle (large green body, well above EMA)
    cur = _bar(_t(14, 50), last_close, last_close + 8, last_close - 0.2, last_close + 6)
    decision = s.should_open(cur, history)
    assert decision is not None, f"Expected entry; got rejection. History close range: {history[0].close} -> {history[-1].close}"
    assert decision.direction == Direction.LONG
    assert "EOD-MC LONG" in decision.rationale


def test_eod_rejects_doji_candle():
    s = EndOfDayMomentumStrategy()
    history = _build_uptrend_history(count=25)
    last_close = float(history[-1].close)
    # Doji: tiny body, wide wicks → body/range < 35%
    doji = _bar(_t(14, 50), last_close, last_close + 5, last_close - 5, last_close + 0.3)
    decision = s.should_open(doji, history)
    assert decision is None  # body is small vs range


def test_eod_rejects_when_trend_is_mixed():
    """3 up + 2 down in last 5 → no clear trend → no entry."""
    s = EndOfDayMomentumStrategy()
    history = _build_uptrend_history(count=20)
    # Add 5 alternating bars to muddy the recent trend
    last = history[-1]
    p = float(last.close)
    base_ts = last.ts + timedelta(minutes=1)
    alt = []
    for i in range(5):
        ts = base_ts + timedelta(minutes=i)
        if i % 2 == 0:
            # green
            alt.append(_bar(ts, p, p + 5, p - 0.5, p + 4))
            p = p + 4
        else:
            # red
            alt.append(_bar(ts, p, p + 0.5, p - 5, p - 4))
            p = p - 4
    history += alt
    last_close = float(history[-1].close)
    cur = _bar(_t(14, 50), last_close, last_close + 5, last_close - 0.5, last_close + 4)
    decision = s.should_open(cur, history)
    assert decision is None  # 3 of 5 not enough (needs 4+)


def test_eod_rejects_when_long_signal_but_red_candle():
    """4/5 trend was up, EMA20 below, BUT current bar is RED → reject (don't fade)."""
    s = EndOfDayMomentumStrategy()
    history = _build_uptrend_history(count=25)
    last_close = float(history[-1].close)
    red_cur = _bar(_t(14, 50), last_close + 2, last_close + 2.5, last_close - 4, last_close - 3)
    decision = s.should_open(red_cur, history)
    assert decision is None


def test_eod_short_on_clean_downtrend():
    s = EndOfDayMomentumStrategy()
    # Build downtrend history
    bars = []
    base = _t(14, 20)
    p = 24000.0
    for i in range(25):
        ts = base + timedelta(minutes=i)
        o = p
        c = p - 3.0 if i % 5 != 4 else p - 1.0
        h = max(o, c) + 0.3
        l = min(o, c) - 0.5
        bars.append(_bar(ts, o, h, l, c))
        p = c
    last_close = float(bars[-1].close)
    # Strong continuation red candle
    cur = _bar(_t(14, 50), last_close, last_close + 0.2, last_close - 8, last_close - 6)
    decision = s.should_open(cur, bars)
    assert decision is not None
    assert decision.direction == Direction.SHORT


def test_eod_decision_includes_atr_based_stop():
    """When ATR is very high (volatile day), stop should EXPAND beyond the 0.20% floor."""
    s = EndOfDayMomentumStrategy()
    # Need ATR(20) * 0.8 / price > 0.002, i.e. ATR > 60 pts at NIFTY 24000
    # Build VERY wide ranging bars (~80-100 range) so ATR-based stop dominates.
    bars = []
    base = _t(14, 20)
    p = 24000.0
    # 20 narrow uptrend bars first (small ATR baseline)
    # range ~30, body ~20 → body/range 0.67 (passes doji check)
    for i in range(20):
        ts = base + timedelta(minutes=i)
        o = p
        c = p + 20
        h = c + 5
        l = o - 5
        bars.append(_bar(ts, o, h, l, c))
        p = c
    # 5 very wide bars at the END (volatility EXPANSION: last 5 ATR > rolling 20 ATR)
    # range ~100, body ~80 → body/range 0.8
    for i in range(5):
        ts = base + timedelta(minutes=20 + i)
        o = p
        c = p + 80
        h = c + 10
        l = o - 10
        bars.append(_bar(ts, o, h, l, c))
        p = c
    last_close = float(bars[-1].close)
    cur = _bar(_t(14, 50), last_close, last_close + 120, last_close - 5, last_close + 100)
    decision = s.should_open(cur, bars)
    assert decision is not None, "Strategy unexpectedly rejected entry on a clean uptrend"
    # Stop must be at least the 0.20% floor (and sensible upper bound)
    assert 0.002 <= decision.stop_pct <= 0.05, (
        f"Stop pct {decision.stop_pct} out of sensible range"
    )
    assert decision.target_rr == 2.0
    assert "ATR exp" in decision.rationale


# ============================================================
# Engine integration with EOD strategy
# ============================================================

def test_engine_with_eod_strategy_runs_without_error():
    """End-to-end smoke: plug EOD strategy into engine, run on contrived data."""
    s = EndOfDayMomentumStrategy()
    engine = BacktestEngine(underlying="NIFTY", strategy=s)
    # Build a day of mostly noise bars + clean 14:45-15:05 uptrend
    bars = []
    base = _t(9, 30)
    p = 24000.0
    for i in range(400):
        ts = base + timedelta(minutes=i)
        # Noise during the day
        delta = 0.5 if i % 2 == 0 else -0.4
        bars.append(_bar(ts, p, p + 0.5, p - 0.5, p + delta))
        p += delta
    # Strong uptrend right before/during EOD window
    base2 = _t(14, 30)
    for i in range(45):
        ts = base2 + timedelta(minutes=i)
        bars.append(_bar(ts, p, p + 4, p - 0.3, p + 3))
        p += 3
    bars.sort(key=lambda b: b.ts)
    results = engine.run(bars)
    # We just want it to run without exception; trade count may be 0 or more
    assert results.total_bars > 0


def test_engine_force_exits_eod_position_at_1525():
    """EOD strategy's force_exit_time (15:25) is respected by the engine."""
    s = EndOfDayMomentumStrategy()
    engine = BacktestEngine(underlying="NIFTY", strategy=s)
    # 20 narrow uptrend bars, then 4 wider uptrend bars (vol expansion)
    bars = []
    base = _t(14, 20)
    p = 24000.0
    for i in range(20):
        ts = base + timedelta(minutes=i)
        bars.append(_bar(ts, p, p + 2.5, p - 0.5, p + 2))
        p += 2
    for i in range(4):
        ts = base + timedelta(minutes=20 + i)
        bars.append(_bar(ts, p, p + 8, p - 0.5, p + 6))
        p += 6
    # Trigger bar at 14:50 — strong green continuation with body well above noise
    last_close = p
    bars.append(_bar(_t(14, 50), last_close, last_close + 10, last_close - 0.5, last_close + 8))
    flat_p = last_close + 8
    # Flat bars from 14:51 to ~15:24 (won't hit stop or target — small range)
    for i in range(33):
        ts = _t(14, 51) + timedelta(minutes=i)
        bars.append(_bar(ts, flat_p, flat_p + 0.3, flat_p - 0.3, flat_p + 0.05))
    # Bar AT 15:25 → triggers force exit
    bars.append(_bar(_t(15, 25), flat_p, flat_p + 0.5, flat_p - 0.5, flat_p))

    results = engine.run(bars)
    forced = [t for t in results.trades if t.exit_reason == "FORCED_TIME_EXIT"]
    assert len(forced) >= 1

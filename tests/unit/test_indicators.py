"""
Tests for indicator helpers — synthetic candles, deterministic outputs.

These don't require live data; they exercise the math in isolation. Phase 5
backtest will integration-test them against real series.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from trading_agent.regime.indicators import (
    adx,
    atr,
    candles_from_ticks,
    consecutive_direction,
    ema,
    realized_vol_annualized,
    true_range,
)


def _make_candles(
    n: int = 60,
    start_price: float = 24000.0,
    drift: float = 1.0,    # per minute
    noise: float = 5.0,
    start: datetime | None = None,
) -> pd.DataFrame:
    """Build n synthetic 1-min candles with mild upward drift + Gaussian noise."""
    rng = np.random.default_rng(seed=42)
    base = start or datetime(2026, 5, 12, 9, 15, tzinfo=timezone.utc)
    rows = []
    price = start_price
    for i in range(n):
        ts = base + timedelta(minutes=i)
        o = price
        rand = rng.normal(0, 1)
        c = o + drift + noise * rand
        h = max(o, c) + abs(noise * 0.3)
        l = min(o, c) - abs(noise * 0.3)
        rows.append({"ts": ts, "open": o, "high": h, "low": l, "close": c, "volume": 1000})
        price = c
    return pd.DataFrame(rows)


def test_candles_from_ticks_aggregates_correctly():
    base = datetime(2026, 5, 12, 9, 15, tzinfo=timezone.utc)
    ticks = pd.DataFrame([
        {"ts": base + timedelta(seconds=10), "ltp": 100.0, "volume": 0},
        {"ts": base + timedelta(seconds=30), "ltp": 102.0, "volume": 100},
        {"ts": base + timedelta(seconds=50), "ltp": 101.5, "volume": 200},
        {"ts": base + timedelta(minutes=1, seconds=10), "ltp": 103.0, "volume": 250},
        {"ts": base + timedelta(minutes=1, seconds=40), "ltp": 104.5, "volume": 350},
    ])
    candles = candles_from_ticks(ticks, freq="1min")
    assert len(candles) == 2
    first = candles.iloc[0]
    assert first["open"] == 100.0
    assert first["high"] == 102.0
    assert first["low"] == 100.0
    assert first["close"] == 101.5


def test_ema_monotonic_on_rising_prices():
    s = pd.Series([100.0 + i for i in range(20)])
    e = ema(s, span=5)
    diffs = e.diff().dropna()
    assert (diffs >= 0).all()


def test_atr_positive_on_oscillating_prices():
    candles = _make_candles(n=30, drift=0.0, noise=10.0)
    a = atr(candles, period=14)
    assert a.iloc[-1] > 0


def test_adx_signals_uptrend_correctly():
    # Strong upward drift, low noise → +DI > -DI
    candles = _make_candles(n=60, drift=10.0, noise=2.0)
    out = adx(candles, period=14)
    assert out["plus_di"].iloc[-1] > out["minus_di"].iloc[-1]
    assert out["adx"].iloc[-1] > 15


def test_realized_vol_returns_float_or_none():
    candles = _make_candles(n=30)
    rv = realized_vol_annualized(candles, window_minutes=15)
    assert rv is None or isinstance(rv, float)
    if rv is not None:
        assert rv > 0


def test_consecutive_direction_picks_streak():
    # Last 3 candles all green
    df = pd.DataFrame([
        {"open": 100, "close": 99},      # red
        {"open": 99,  "close": 100},     # green
        {"open": 100, "close": 101},     # green
        {"open": 101, "close": 102},     # green
    ])
    up, down = consecutive_direction(df)
    assert up == 3
    assert down == 0


def test_consecutive_direction_no_streak_when_mixed():
    df = pd.DataFrame([
        {"open": 100, "close": 99},
        {"open": 99,  "close": 100},
        {"open": 100, "close": 99},      # last is red → streak 1 down
    ])
    up, down = consecutive_direction(df)
    assert up == 0
    assert down == 1


def test_true_range_clean():
    df = pd.DataFrame([
        {"high": 102, "low": 99,  "close": 100},
        {"high": 105, "low": 100, "close": 104},
        {"high": 110, "low": 103, "close": 109},
    ])
    tr = true_range(df)
    # First row is high-low (no prev close)
    assert tr.iloc[0] == 3.0
    # Second: max(105-100=5, |105-100|=5, |100-100|=0) = 5
    assert tr.iloc[1] == 5.0
    # Third: max(110-103=7, |110-104|=6, |103-104|=1) = 7
    assert tr.iloc[2] == 7.0

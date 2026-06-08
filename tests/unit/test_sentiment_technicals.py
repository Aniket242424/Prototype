"""
Adversarial unit tests for the sentiment agent's DETERMINISTIC technicals
(scripts/run_sentiment_agent.py). These are the "no-hallucination" core — any bug
here feeds a wrong number straight to the LLM and the dashboard, so the tests
deliberately probe edge cases that try to BREAK the functions:
  - RSI on degenerate series (all-up / all-down / flat / too-short)
  - EMA warmup gating
  - support hold-rate / resistance reject-rate counting + division safety
  - CRDS level ranking incl. the below-all-EMAs (Bitcoin) path + "always a support"
  - the bounce detector's false-positive guards (wick-then-crash, broken levels)
  - bias normalisation + directional grading
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "scripts"))
import run_sentiment_agent as rsa  # noqa: E402


# ----------------------------- helpers -----------------------------
def _frame(closes, lows=None, highs=None) -> pd.DataFrame:
    closes = list(map(float, closes))
    idx = pd.date_range("2018-01-01", periods=len(closes), freq="D")
    return pd.DataFrame(
        {"Close": closes,
         "Low": list(map(float, lows)) if lows is not None else closes,
         "High": list(map(float, highs)) if highs is not None else closes},
        index=idx,
    )


def _ema(closes, span) -> np.ndarray:
    return pd.Series(list(map(float, closes))).ewm(span=span, adjust=True).mean().values


# ============================ RSI ============================
def test_rsi_in_bounds_and_reference():
    # A mild oscillating series — RSI must always be in [0, 100].
    s = pd.Series([100, 101, 100.5, 102, 101.5, 103, 102, 104, 103.5, 105,
                   104, 106, 105, 107, 106.5, 108, 107, 109, 108.5, 110] * 3)
    r = rsa._rsi(s)
    assert 0.0 <= r <= 100.0


def test_rsi_all_gains_pegs_high():
    s = pd.Series(np.linspace(100, 200, 60))      # strictly up
    assert rsa._rsi(s) > 99.0                       # no division blow-up; ~100


def test_rsi_all_losses_pegs_low():
    s = pd.Series(np.linspace(200, 100, 60))      # strictly down
    assert rsa._rsi(s) < 1.0


def test_rsi_flat_series_does_not_crash():
    # Degenerate: a perfectly flat market. Must return a finite number in range
    # (documents current behaviour — flat -> ~0 due to the up=dn=0 path).
    s = pd.Series([100.0] * 60)
    r = rsa._rsi(s)
    assert np.isfinite(r) and 0.0 <= r <= 100.0


def test_rsi_short_series_no_exception():
    # Fewer bars than the period — must not raise.
    r = rsa._rsi(pd.Series([100.0, 101.0, 99.0]))
    assert np.isfinite(r)


# ============================ EMA level / warmup ============================
def test_ema_level_returns_none_below_min_bars():
    s = pd.Series(np.linspace(100, 110, 40))      # 40 bars < 3*20
    val, status, slope = rsa._ema_level(s, 20)
    assert val is None and status == "n/a"


def test_ema_level_converges_with_enough_bars():
    s = pd.Series(np.linspace(100, 110, 200))     # 200 >= 3*20 and >= 5*20
    val, status, slope = rsa._ema_level(s, 20)
    assert val is not None and status == "converged" and slope == "rising"


# ============================ hold / reject stats ============================
def test_hold_stats_rate_none_until_enough_tests():
    s = pd.Series(np.linspace(100, 130, 80))      # smooth up, no real pullbacks
    ema = s.ewm(span=20, adjust=True).mean()
    out = rsa._hold_stats(s, ema, hivol=False)
    assert out["rate"] is None                     # <4 tests -> never over-claim
    assert out["tests"] >= 0 and out["held"] <= out["tests"]


def test_hold_stats_no_divide_by_zero_on_short_series():
    s = pd.Series([100.0, 101.0])
    out = rsa._hold_stats(s, s.ewm(span=20, adjust=True).mean(), hivol=False)
    assert out == {"tests": 0, "held": 0, "rate": None}


def test_reject_stats_short_series_safe():
    s = pd.Series([100.0, 99.0])
    out = rsa._reject_stats(s, s.ewm(span=20, adjust=True).mean(), hivol=False)
    assert out["rate"] is None and out["tests"] == 0


def test_hold_and_reject_rates_within_0_100():
    rng = np.random.RandomState(0)
    closes = 100 + np.cumsum(rng.randn(400))
    s = pd.Series(closes)
    ema = s.ewm(span=20, adjust=True).mean()
    h = rsa._hold_stats(s, ema, hivol=False)
    r = rsa._reject_stats(s, ema, hivol=False)
    for out in (h, r):
        if out["rate"] is not None:
            assert 0 <= out["rate"] <= 100


# ============================ CRDS levels ============================
def test_compute_levels_no_ema_support_falls_back_to_swing_low():
    # Bitcoin breakdown: price below EVERY EMA -> no dynamic support, nearest EMA is
    # RESISTANCE, and nearest_support MUST fall back to the swing low (never None).
    price = 100.0
    matrix = [
        {"tf": "Daily", "span": 20, "value": 110.0, "slope": "falling", "status": "converged"},
        {"tf": "Daily", "span": 50, "value": 120.0, "slope": "falling", "status": "converged"},
        {"tf": "Weekly", "span": 50, "value": 130.0, "slope": "falling", "status": "converged"},
    ]
    lv = rsa._compute_levels(matrix, price, swing_low=95.0, hivol=False, hold_by_label={})
    assert lv["no_ema_support"] is True
    assert lv["nearest_support"] is not None
    assert lv["nearest_support"]["value"] == 95.0
    assert lv["controlling_resistance"]["value"] == 110.0     # nearest overhead EMA


def test_compute_levels_always_returns_a_support_and_ranks_nearest():
    price = 100.0
    matrix = [
        {"tf": "Daily", "span": 20, "value": 99.0, "slope": "rising", "status": "converged"},   # support, nearest
        {"tf": "Daily", "span": 50, "value": 90.0, "slope": "rising", "status": "converged"},   # support, deeper
        {"tf": "Daily", "span": 200, "value": 105.0, "slope": "rising", "status": "converged"},  # resistance
    ]
    lv = rsa._compute_levels(matrix, price, swing_low=85.0, hivol=False, hold_by_label={})
    assert lv["no_ema_support"] is False
    assert lv["nearest_support"]["value"] == 99.0
    assert lv["controlling_resistance"]["value"] == 105.0


def test_compute_levels_empty_matrix_does_not_crash():
    lv = rsa._compute_levels([], 100.0, swing_low=90.0, hivol=False, hold_by_label={})
    assert lv["nearest_support"]["value"] == 90.0      # swing-low fallback, no max() on empty
    assert lv["no_ema_support"] is True
    assert lv["controlling_resistance"] is None


# ============================ bounce detector ============================
def test_scan_bounces_rejects_wick_then_close_below():
    # THE false-positive the adversarial review found: a bar wicks well above the EMA
    # but CLOSES below it (failed support / dead-cat). The old code took the max HIGH
    # and logged a fake "+X% bounce". The fix checks the close-break BEFORE counting
    # the high, so this must NOT appear as a bounce.
    n = 160
    closes = list(np.linspace(100.0, 240.0, n))    # smooth uptrend, no natural touches
    crash = n - 4
    base = closes[crash - 1]
    for j in range(crash, n):
        closes[j] = base * 0.85                      # hard drop: closes now well below the EMA
    span = 20
    ema = _ema(closes, span)
    i = crash - 1                                    # last uptrend bar: close > ema, "held"
    lows = list(closes); highs = list(closes)
    lows[i] = ema[i]                                 # low dips to touch the EMA
    highs[crash] = ema[crash] * 1.12                 # the TRAP: a +12% wick on the crash bar
    df = _frame(closes, lows, highs)
    res = rsa._scan_bounces(df, "Daily", hivol=False, min_rally=2.0, lookback=150, win=12)
    crafted = df.index[i].date().isoformat()
    assert all(b["date"] != crafted for b in res), f"FALSE bounce logged: {res}"


def test_scan_bounces_finds_a_real_held_bounce():
    n = 170
    closes = list(np.linspace(100.0, 240.0, n))
    span = 20
    ema = _ema(closes, span)
    i = n - 8
    lows = list(closes); highs = list(closes)
    lows[i] = ema[i]                                 # touch
    for j in range(i + 1, min(i + 1 + 12, n)):
        highs[j] = closes[j] * 1.06                  # genuine rally, closes stay above EMA (held)
    df = _frame(closes, lows, highs)
    res = rsa._scan_bounces(df, "Daily", hivol=False, min_rally=2.0, lookback=150, win=12)
    assert any(b["date"] == df.index[i].date().isoformat() for b in res)


def test_scan_bounces_flags_broken_level():
    # A clean bounce early on, but price ends BELOW that EMA -> the bounce must be
    # tagged currently='broken' (never sold as a live launchpad).
    n = 200
    up = list(np.linspace(100.0, 180.0, 140))
    down = list(np.linspace(180.0, 110.0, 60))       # later breaks down below the EMAs
    closes = up + down
    span = 20
    ema = _ema(closes, span)
    i = 130                                           # a touch during the uptrend
    lows = list(closes); highs = list(closes)
    lows[i] = ema[i]
    for j in range(i + 1, i + 13):
        highs[j] = closes[j] * 1.05
    df = _frame(closes, lows, highs)
    res = rsa._scan_bounces(df, "Daily", hivol=False, min_rally=2.0, lookback=200, win=12)
    assert res, "expected at least one historical bounce"
    assert all(b["currently"] in ("support", "broken") for b in res)
    assert any(b["currently"] == "broken" for b in res)


def test_scan_bounces_empty_on_short_frame():
    df = _frame([100.0, 101.0, 102.0])
    assert rsa._scan_bounces(df, "Daily", hivol=False, min_rally=2.0, lookback=150, win=12) == []


# ============================ bias grading ============================
@pytest.mark.parametrize("raw,expected", [
    ("bullish", "bullish"), ("BEARISH", "bearish"), ("neutral", "neutral"),
    ("neutral-to-bearish", "bearish"), ("slightly bullish", "bullish"),
    ("uptrend", "neutral"), ("", "neutral"), (None, "neutral"),
])
def test_norm_bias(raw, expected):
    assert rsa._norm_bias(raw) == expected


def test_bias_correct_directions():
    thr = 0.3
    assert rsa._bias_correct("bullish", 1.0, thr) is True
    assert rsa._bias_correct("bullish", -1.0, thr) is False
    assert rsa._bias_correct("bearish", -1.0, thr) is True
    assert rsa._bias_correct("bearish", 0.1, thr) is False     # barely up -> bearish wrong
    assert rsa._bias_correct("neutral", 0.1, thr) is True      # within band
    assert rsa._bias_correct("neutral", 1.0, thr) is False     # moved too much


def test_bias_correct_threshold_boundary():
    # exactly at the threshold: bullish needs > thr (strict), neutral needs <= thr.
    assert rsa._bias_correct("bullish", 0.3, 0.3) is False
    assert rsa._bias_correct("neutral", 0.3, 0.3) is True

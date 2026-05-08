"""
Technical indicator helpers — pandas-backed, deterministic, side-effect-free.

All functions accept a DataFrame of 1-minute candles with columns
[ts, open, high, low, close, volume] and return either a Series or scalar.
Pure functions — easy to unit-test.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd


def candles_from_ticks(ticks: pd.DataFrame, freq: str = "1min") -> pd.DataFrame:
    """
    Aggregate a tick frame to OHLCV candles.

    `ticks` must have columns: ts (datetime, IST or UTC, doesn't matter as long
    as consistent), ltp (float), volume (Int64 nullable, optional).
    """
    if ticks.empty:
        return pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume"])
    df = ticks.copy()
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df = df.set_index("ts")
    ohlc = df["ltp"].resample(freq).ohlc()
    if "volume" in df.columns:
        vol = df["volume"].resample(freq).last().diff().clip(lower=0).fillna(0)
    else:
        vol = pd.Series(0, index=ohlc.index)
    out = ohlc.copy()
    out["volume"] = vol
    out = out.dropna(subset=["open"])  # drop empty buckets
    out = out.reset_index().rename(columns={"index": "ts"})
    return out


def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def true_range(df: pd.DataFrame) -> pd.Series:
    """TR = max(high-low, abs(high-prev_close), abs(low-prev_close))."""
    pc = df["close"].shift(1)
    a = df["high"] - df["low"]
    b = (df["high"] - pc).abs()
    c = (df["low"] - pc).abs()
    return pd.concat([a, b, c], axis=1).max(axis=1)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    return true_range(df).ewm(alpha=1.0 / period, adjust=False).mean()


def adx(df: pd.DataFrame, period: int = 14) -> dict[str, pd.Series]:
    """
    Wilder's ADX, +DI, -DI.

    Returns dict with keys 'adx', 'plus_di', 'minus_di'.
    """
    hi, lo, _ = df["high"], df["low"], df["close"]
    up_move = hi.diff()
    down_move = -lo.diff()
    plus_dm = ((up_move > down_move) & (up_move > 0)) * up_move
    minus_dm = ((down_move > up_move) & (down_move > 0)) * down_move

    tr = true_range(df)
    atr_ = tr.ewm(alpha=1.0 / period, adjust=False).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1.0 / period, adjust=False).mean() / atr_.replace(0, np.nan)
    minus_di = 100 * minus_dm.ewm(alpha=1.0 / period, adjust=False).mean() / atr_.replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx_ = dx.ewm(alpha=1.0 / period, adjust=False).mean()
    return {"adx": adx_, "plus_di": plus_di, "minus_di": minus_di}


def vwap_session(df: pd.DataFrame, session_open_ts) -> pd.Series:
    """
    Cumulative VWAP from session open. `session_open_ts` is timezone-aware.
    Falls back to typical-price × volume sum if volume is zero.
    """
    typical = (df["high"] + df["low"] + df["close"]) / 3
    mask = df["ts"] >= session_open_ts
    sub = df[mask]
    cum_pv = (typical[mask] * sub["volume"]).cumsum()
    cum_v = sub["volume"].cumsum()
    vwap = cum_pv / cum_v.replace(0, np.nan)
    out = pd.Series(np.nan, index=df.index, dtype=float)
    out.loc[mask] = vwap.values
    return out


def realized_vol_annualized(df: pd.DataFrame, window_minutes: int) -> float | None:
    """
    Annualized realized vol over the last `window_minutes` minutes of close prices.
    Returns % (e.g., 18.5 means 18.5% annualized).
    """
    if df.empty or len(df) < 2:
        return None
    tail = df.tail(window_minutes + 1)
    if len(tail) < 2:
        return None
    log_ret = np.log(tail["close"] / tail["close"].shift(1)).dropna()
    if log_ret.empty or log_ret.std() == 0 or math.isnan(log_ret.std()):
        return None
    # Indian market = ~375 minutes/day × 252 trading days
    minutes_per_year = 375 * 252
    annualization_factor = math.sqrt(minutes_per_year / 1)  # since each obs is 1-min
    return float(log_ret.std() * annualization_factor * 100)


def consecutive_direction(df: pd.DataFrame) -> tuple[int, int]:
    """
    Counts the most-recent consecutive up-candles and down-candles.
    A green candle has close > open; red has close < open. Doji counts as 0.
    Returns (consec_up, consec_down) — at most one of these is nonzero.
    """
    if df.empty:
        return (0, 0)
    closes = df["close"].values
    opens = df["open"].values
    up = down = 0
    for i in range(len(df) - 1, -1, -1):
        if closes[i] > opens[i]:
            if down > 0:
                break
            up += 1
        elif closes[i] < opens[i]:
            if up > 0:
                break
            down += 1
        else:
            break
    return (up, down)


def price_vwap_deviation_sigma(df: pd.DataFrame, vwap_col: str = "vwap") -> float | None:
    """How many standard deviations is the latest close above (positive) or below VWAP?"""
    if df.empty or vwap_col not in df.columns:
        return None
    diff = df["close"] - df[vwap_col]
    diff = diff.dropna()
    if len(diff) < 5 or diff.std() == 0 or math.isnan(diff.std()):
        return None
    return float(diff.iloc[-1] / diff.std())

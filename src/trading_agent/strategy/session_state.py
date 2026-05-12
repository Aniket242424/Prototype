"""
Session-level state computation for strategies that need it.

The Phase 4 worker calls these functions on each evaluation cycle to
compute the inputs that go into `StrategyContext` (opening_range_high/low,
gap_pct, session_open, volume_ratio).

These are PURE functions on top of the tick buffer / chain data. The
worker handles I/O and caching.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from decimal import Decimal

import pandas as pd

from trading_agent.core.time_utils import IST, to_ist

OPENING_RANGE_START_IST = time(9, 15)
OPENING_RANGE_END_IST = time(9, 30)


@dataclass(frozen=True)
class SessionState:
    """Per-underlying session state for the current trading day."""

    opening_range_high: float | None
    opening_range_low: float | None
    opening_range_formed: bool
    session_open: float | None        # First tick of the day at 09:15
    previous_close: float | None       # Yesterday's close (for gap math)
    gap_pct: float | None              # 100 * (session_open - prev_close) / prev_close


def compute_session_state(
    ticks_df: pd.DataFrame,
    previous_close: float | None,
    now_ts: datetime | None = None,
) -> SessionState:
    """
    Build a SessionState from the day's tick stream.

    - `ticks_df` is the buffer for ONE underlying. Columns: ts (UTC-aware), ltp.
    - `previous_close` is yesterday's settlement (None if we don't know yet).
    - `now_ts` defaults to current IST time. Pass for testability.

    Returns a SessionState with as much computed as possible. Strategies
    handle None values themselves.
    """
    if ticks_df.empty:
        return SessionState(
            opening_range_high=None,
            opening_range_low=None,
            opening_range_formed=False,
            session_open=None,
            previous_close=previous_close,
            gap_pct=None,
        )

    # Make sure ts is comparable
    df = ticks_df.copy()
    df["ts"] = pd.to_datetime(df["ts"], utc=True)

    # Find today's session open (first tick on or after 09:15 IST today)
    today_ist = to_ist(now_ts) if now_ts else None
    today_date = today_ist.date() if today_ist else None
    if today_date is not None:
        session_start_ist = datetime.combine(today_date, OPENING_RANGE_START_IST, tzinfo=IST)
        session_start_utc = session_start_ist.astimezone(df["ts"].iloc[0].tzinfo)
        today_mask = df["ts"] >= session_start_utc
        today_df = df[today_mask]
    else:
        today_df = df

    if today_df.empty:
        return SessionState(
            opening_range_high=None,
            opening_range_low=None,
            opening_range_formed=False,
            session_open=None,
            previous_close=previous_close,
            gap_pct=None,
        )

    session_open = float(today_df["ltp"].iloc[0])

    # Compute opening range: ticks between 09:15 and 09:30 IST
    if today_date is not None:
        range_end_ist = datetime.combine(today_date, OPENING_RANGE_END_IST, tzinfo=IST)
        range_end_utc = range_end_ist.astimezone(today_df["ts"].iloc[0].tzinfo)
        range_mask = today_df["ts"] < range_end_utc
        range_df = today_df[range_mask]
        range_window_complete = (
            to_ist(now_ts).time() >= OPENING_RANGE_END_IST if now_ts else False
        )
    else:
        range_df = today_df
        range_window_complete = False

    if not range_df.empty:
        opening_range_high = float(range_df["ltp"].max())
        opening_range_low = float(range_df["ltp"].min())
    else:
        opening_range_high = None
        opening_range_low = None

    # Gap % = (session_open - prev_close) / prev_close * 100
    if previous_close is not None and previous_close > 0:
        gap_pct = (session_open - previous_close) / previous_close * 100
    else:
        gap_pct = None

    return SessionState(
        opening_range_high=opening_range_high,
        opening_range_low=opening_range_low,
        opening_range_formed=range_window_complete,
        session_open=session_open,
        previous_close=previous_close,
        gap_pct=gap_pct,
    )

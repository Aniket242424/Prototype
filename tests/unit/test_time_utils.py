"""Sanity tests on IST handling and market hours."""
from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

from trading_agent.core.time_utils import (
    IST,
    is_market_open,
    is_trading_day,
    parse_hhmm,
    upstox_token_expiry_ist,
)


def test_parse_hhmm():
    assert parse_hhmm("09:15") == time(9, 15)
    assert parse_hhmm("15:30") == time(15, 30)


def test_is_market_open_weekday_in_window():
    dt = datetime(2026, 5, 11, 10, 0, tzinfo=IST)  # Monday 10:00 IST
    assert is_market_open(dt) is True


def test_is_market_open_weekday_after_close():
    dt = datetime(2026, 5, 11, 16, 0, tzinfo=IST)
    assert is_market_open(dt) is False


def test_is_market_open_weekend():
    dt = datetime(2026, 5, 9, 11, 0, tzinfo=IST)  # Saturday
    assert is_market_open(dt) is False


def test_is_trading_day_weekday():
    assert is_trading_day(datetime(2026, 5, 11, tzinfo=IST).date()) is True
    assert is_trading_day(datetime(2026, 5, 9, tzinfo=IST).date()) is False  # Sat


def test_token_expiry_returns_future():
    expiry = upstox_token_expiry_ist()
    assert expiry.hour == 3 and expiry.minute == 30
    assert expiry.tzinfo == IST or expiry.tzinfo == ZoneInfo("Asia/Kolkata")

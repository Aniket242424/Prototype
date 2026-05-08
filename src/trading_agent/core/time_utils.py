"""
IST-aware time utilities. The Indian market lives in Asia/Kolkata; everything
that touches market hours, expiry, or daily-cap windows goes through here.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from trading_agent.core.constants import NSE_HOLIDAYS_2026

IST = ZoneInfo("Asia/Kolkata")

MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)


def now_ist() -> datetime:
    return datetime.now(tz=IST)


def to_ist(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=IST)
    return dt.astimezone(IST)


def is_weekday(d: date) -> bool:
    return d.weekday() < 5


def is_market_holiday(d: date) -> bool:
    return d.isoformat() in NSE_HOLIDAYS_2026


def is_trading_day(d: date) -> bool:
    return is_weekday(d) and not is_market_holiday(d)


def is_market_open(at: datetime | None = None) -> bool:
    at = at or now_ist()
    at = to_ist(at)
    if not is_trading_day(at.date()):
        return False
    return MARKET_OPEN <= at.time() <= MARKET_CLOSE


def parse_hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def in_window(start_hhmm: str, end_hhmm: str, at: datetime | None = None) -> bool:
    at = to_ist(at or now_ist())
    return parse_hhmm(start_hhmm) <= at.time() <= parse_hhmm(end_hhmm)


def upstox_token_expiry_ist(today: date | None = None) -> datetime:
    """Upstox tokens expire at 03:30 IST every day."""
    today = today or now_ist().date()
    expiry = datetime.combine(today, time(3, 30), tzinfo=IST)
    if now_ist() > expiry:
        expiry += timedelta(days=1)
    return expiry


def seconds_until(when: datetime) -> float:
    delta = when - now_ist()
    return max(0.0, delta.total_seconds())

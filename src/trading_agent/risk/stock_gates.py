"""
Stock-options-specific risk gates.

These ONLY apply when a trade involves an individual stock option (not an
index option). Indices don't have:
- Earnings events (so no blackout needed)
- Sector concentration risk (NIFTY already diversifies internally)
- Single-stock gap risk

Single stocks DO have all of those, so we add extra protection.

Per the scope-v2 decision: stock options first-class from Phase 3 onwards.
Gates ship enabled but the universe is empty until config/instruments.yaml
is extended with stock entries — until then, these gates are no-ops.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Iterable

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from trading_agent.core.logging import get_logger
from trading_agent.infrastructure.models import PositionRow

log = get_logger(__name__)


# Curated universe — Phase 3.1 ships empty. Will populate when stock options
# are enabled in config/instruments.yaml (per scope v2: top 30 liquid F&O names).
# Keys must match the `underlying` column in our DB.
TOP_30_STOCK_WHITELIST: frozenset[str] = frozenset({
    # Placeholders — populate when activating stock options:
    # "RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS",
    # "SBIN", "AXISBANK", "BHARTIARTL", "ITC", "KOTAKBANK",
    # ... + 20 more midcaps including BSE, MCX, COFORGE per user request
})


# Sector mapping for the universe. Used for per-sector concentration cap.
# Format: underlying_name → sector_code
STOCK_SECTOR: dict[str, str] = {
    # "RELIANCE": "ENERGY",
    # "HDFCBANK": "BANKING",
    # "ICICIBANK": "BANKING",
    # "SBIN": "BANKING",
    # "AXISBANK": "BANKING",
    # "KOTAKBANK": "BANKING",
    # "INFY": "IT",
    # "TCS": "IT",
    # "WIPRO": "IT",
    # "COFORGE": "IT",
    # "BHARTIARTL": "TELECOM",
    # "ITC": "FMCG",
    # "BSE": "EXCHANGES",
    # "MCX": "EXCHANGES",
    # ...
}


def is_stock_option(underlying_name: str) -> bool:
    """True if the underlying is a stock (not an index)."""
    INDICES = frozenset({"NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX", "BANKEX"})
    return underlying_name not in INDICES


def check_universe_whitelist(underlying_name: str) -> tuple[bool, str]:
    """
    Stock-options gate: must be in the curated top-30 whitelist.

    Returns (allowed, reason).
    """
    if not is_stock_option(underlying_name):
        return True, "n/a (index)"
    if not TOP_30_STOCK_WHITELIST:
        return False, "Stock options universe empty (no stocks whitelisted yet)"
    if underlying_name in TOP_30_STOCK_WHITELIST:
        return True, f"in whitelist"
    return False, f"{underlying_name} not in top-30 whitelist"


# --- Earnings calendar — stub for Phase 3.1 ---
# Phase 7 will integrate a real earnings calendar feed (Sensibull, Tickertape, etc).
# Until then we maintain a manual list in this dict — operator updates weekly.
# Format: underlying_name → list of earnings dates within next 30 days
EARNINGS_CALENDAR: dict[str, list[date]] = {
    # "RELIANCE": [date(2026, 5, 15)],
    # ...
}


def check_earnings_blackout(
    underlying_name: str,
    today: date,
    blackout_days: int = 7,
) -> tuple[bool, str]:
    """
    Stock-options gate: reject if earnings are within ±blackout_days.

    Pre-earnings IV is bloated (you pay a premium); post-earnings IV crashes
    (you lose value even if direction is right). Disciplined option buyers
    skip the whole window.
    """
    if not is_stock_option(underlying_name):
        return True, "n/a (index)"

    earnings_dates = EARNINGS_CALENDAR.get(underlying_name, [])
    for ed in earnings_dates:
        days_to_earnings = (ed - today).days
        if abs(days_to_earnings) <= blackout_days:
            return False, f"earnings on {ed.isoformat()} ({days_to_earnings:+d} days)"
    return True, "no earnings within ±{blackout_days}d"


async def check_sector_concentration(
    underlying_name: str,
    session_factory: async_sessionmaker,
    max_open_per_sector: int = 1,
) -> tuple[bool, str]:
    """
    Stock-options gate: max N open positions per sector.

    Prevents "load up on banks" mistakes when banking sector rallies.
    Default: max 1 open per sector at a time.
    """
    if not is_stock_option(underlying_name):
        return True, "n/a (index)"

    sector = STOCK_SECTOR.get(underlying_name)
    if sector is None:
        return False, f"no sector mapping for {underlying_name}"

    sector_members = [u for u, s in STOCK_SECTOR.items() if s == sector]
    if not sector_members:
        return True, "sector empty"

    async with session_factory() as session:
        open_count = (await session.execute(
            select(func.count(PositionRow.id))
            .where(PositionRow.is_open.is_(True))
            .where(PositionRow.underlying.in_(sector_members))
        )).scalar() or 0

    if open_count >= max_open_per_sector:
        return False, f"{open_count} open in sector {sector} ≥ cap {max_open_per_sector}"
    return True, f"{open_count}/{max_open_per_sector} open in sector {sector}"


def check_no_overnight_stock_option(underlying_name: str) -> tuple[bool, str]:
    """
    Stock options must NEVER be held overnight (gap risk).

    This is a hard rule — even with stops, single-stock news can gap 5-15%.
    Enforced at order placement (Risk Engine checks) AND at 15:15 IST forced
    exit (Position Manager).

    For Risk Engine: we just enforce this is a day-trade by rejecting any
    intent that doesn't have an intraday exit plan. The Position Manager
    handles the actual forced exit.
    """
    if not is_stock_option(underlying_name):
        return True, "n/a (index)"
    # Phase 3.1: always allow at the gate; Position Manager enforces the exit
    return True, "intraday only (enforced at exit by Position Manager)"

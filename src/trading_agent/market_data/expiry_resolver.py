"""
Pick the active expiry for an underlying.

Strategy: fetch the actual list of contracts from Upstox (canonical source —
NSE/BSE change weekly schedules from time to time, hardcoded weekday config
goes stale). Cache results for a few minutes so we don't hit /option/contract
on every chain poll.

"Active expiry" rules:
- Use the NEAREST future expiry (today inclusive only if it's a real expiry day
  and we're pre-15:30 IST).
- Skip expiry days that are also today after 15:00 IST — too close to settlement
  for any new entries.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, time
from typing import Sequence

from trading_agent.core.logging import get_logger
from trading_agent.core.time_utils import IST, now_ist
from trading_agent.market_data.upstox_rest import UpstoxRestClient

log = get_logger(__name__)


class ExpiryResolver:
    def __init__(self, rest: UpstoxRestClient, cache_ttl_sec: int = 300):
        self._rest = rest
        self._cache: dict[str, tuple[datetime, list[date]]] = {}  # key -> (fetched_at, expiries)
        self._cache_ttl = cache_ttl_sec
        self._lock = asyncio.Lock()

    async def expiries_for(self, underlying_instrument_key: str) -> list[date]:
        """All future expiries for an underlying, sorted ascending. Cached."""
        async with self._lock:
            cached = self._cache.get(underlying_instrument_key)
            if cached and (now_ist() - cached[0]).total_seconds() < self._cache_ttl:
                return cached[1]

        contracts = await self._rest.option_contracts(underlying_instrument_key)
        today_ist = now_ist().date()
        expiries: set[date] = set()
        for c in contracts:
            try:
                d = datetime.strptime(c["expiry"], "%Y-%m-%d").date()
            except (KeyError, ValueError):
                continue
            if d >= today_ist:
                expiries.add(d)
        result = sorted(expiries)

        async with self._lock:
            self._cache[underlying_instrument_key] = (now_ist(), result)
        log.debug(
            "expiry_resolver.refreshed",
            underlying=underlying_instrument_key,
            count=len(result),
            nearest=result[0].isoformat() if result else None,
        )
        return result

    async def active_expiry(self, underlying_instrument_key: str) -> date | None:
        """
        Pick the expiry to actively trade right now.

        - Always the nearest future expiry.
        - On the day of expiry: only valid before 15:00 IST. After that we
          skip to the next expiry — too close to settlement for fresh entries.
        """
        expiries = await self.expiries_for(underlying_instrument_key)
        if not expiries:
            return None

        nearest = expiries[0]
        today_ist = now_ist().date()
        if nearest == today_ist:
            cutoff = datetime.combine(today_ist, time(15, 0), tzinfo=IST)
            if now_ist() >= cutoff:
                return expiries[1] if len(expiries) > 1 else None
        return nearest

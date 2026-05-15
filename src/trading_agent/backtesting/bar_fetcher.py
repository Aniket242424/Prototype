"""
Historical OHLC bar fetcher for backtesting — Phase 5.

Pulls 1-minute candles from the Upstox historical-candle API:
  GET /v2/historical-candle/{instrument_key}/{interval}/{to_date}/{from_date}

Caches each (instrument_key, day) chunk to disk as JSON so repeat backtests
don't re-pull. Cache location: data/backtest_bars/{instrument_key}/{YYYY-MM-DD}.json
where slashes/pipes in instrument_key are replaced with underscores.

Authentication: uses the latest valid token from TokenManager (the same one
the live workers use). If no token, raises TokenExpiredError.

Notes:
- Upstox returns at most ~30 days per call; we batch by day to keep cache
  granularity sensible and survive 5xx retries.
- For "to_date < from_date" Upstox returns empty; we tolerate that.
- All timestamps are returned in IST (Upstox already does this).
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx

from trading_agent.auth.token_manager import TokenManager
from trading_agent.backtesting.dtos import Bar
from trading_agent.core.config import REPO_ROOT, AppSettings, get_settings
from trading_agent.core.logging import get_logger
from trading_agent.core.time_utils import IST
from trading_agent.infrastructure.db import session_scope

log = get_logger(__name__)


CACHE_DIR = REPO_ROOT / "data" / "backtest_bars"
INTERVAL = "1minute"


def _safe_key(instrument_key: str) -> str:
    """Filesystem-safe version of instrument_key like 'NSE_INDEX|Nifty 50'."""
    return instrument_key.replace("|", "_").replace(" ", "_").replace("/", "_")


def _cache_path(instrument_key: str, day: date) -> Path:
    safe = _safe_key(instrument_key)
    return CACHE_DIR / safe / f"{day.isoformat()}.json"


def _parse_upstox_bar(raw: list) -> Bar:
    """
    Upstox returns each candle as [ts_iso, open, high, low, close, volume, oi].
    Parse into our Bar DTO.
    """
    return Bar(
        ts=datetime.fromisoformat(raw[0]).astimezone(IST),
        open=Decimal(str(raw[1])),
        high=Decimal(str(raw[2])),
        low=Decimal(str(raw[3])),
        close=Decimal(str(raw[4])),
        volume=int(raw[5]),
        oi=int(raw[6]) if len(raw) >= 7 else 0,
    )


class BarFetcher:
    """
    Fetches and caches historical bars from Upstox.

    Usage:
        fetcher = BarFetcher()
        bars = await fetcher.fetch_range(
            instrument_key="NSE_INDEX|Nifty 50",
            start=date(2026, 4, 1),
            end=date(2026, 5, 1),
        )
    """

    def __init__(
        self,
        settings: AppSettings | None = None,
        token_manager: TokenManager | None = None,
        cache_dir: Path | None = None,
    ):
        self._settings = settings or get_settings()
        self._token_manager = token_manager or TokenManager(self._settings)
        self._cache_dir = cache_dir or CACHE_DIR
        self._cache_dir.mkdir(parents=True, exist_ok=True)

    async def fetch_range(
        self,
        instrument_key: str,
        start: date,
        end: date,
        force_refresh: bool = False,
    ) -> list[Bar]:
        """
        Fetch bars for [start, end] inclusive. Iterates day-by-day to leverage
        per-day caching. Skips weekends (no Indian market data).

        Returns bars sorted by timestamp ascending.
        """
        all_bars: list[Bar] = []
        cur = start
        while cur <= end:
            if cur.weekday() < 5:  # Mon=0 ... Fri=4
                day_bars = await self._fetch_day(instrument_key, cur, force_refresh)
                all_bars.extend(day_bars)
            cur += timedelta(days=1)
        all_bars.sort(key=lambda b: b.ts)
        return all_bars

    async def _fetch_day(
        self,
        instrument_key: str,
        day: date,
        force_refresh: bool,
    ) -> list[Bar]:
        cache_file = _cache_path(instrument_key, day)
        if not force_refresh and cache_file.exists():
            try:
                raw_list = json.loads(cache_file.read_text(encoding="utf-8"))
                return [_parse_upstox_bar(r) for r in raw_list]
            except Exception as e:
                log.warning("backtest.cache_corrupt", path=str(cache_file), error=str(e))
                # fall through to re-fetch

        access_token = await self._get_token()
        url = (
            f"{self._settings.upstox_base_url}/historical-candle/"
            f"{instrument_key}/{INTERVAL}/{day.isoformat()}/{day.isoformat()}"
        )
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {access_token}",
        }

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                r = await client.get(url, headers=headers)
        except Exception as e:
            log.warning("backtest.fetch_failed", day=day.isoformat(), error=str(e))
            return []

        if r.status_code != 200:
            log.warning(
                "backtest.fetch_non200",
                day=day.isoformat(),
                status=r.status_code,
                body=r.text[:200],
            )
            return []

        payload = r.json()
        candles = payload.get("data", {}).get("candles", [])
        # Cache as-is for replay-fidelity
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(candles), encoding="utf-8")
        log.info(
            "backtest.bars_cached",
            day=day.isoformat(),
            instrument_key=instrument_key,
            count=len(candles),
        )
        return [_parse_upstox_bar(c) for c in candles]

    async def _get_token(self) -> str:
        """Fetch the most recent valid token from DB."""
        async with session_scope() as session:
            from sqlalchemy import select
            from trading_agent.infrastructure.models import TokenRow
            row = (
                await session.execute(
                    select(TokenRow).order_by(TokenRow.issued_at.desc()).limit(1)
                )
            ).scalar_one_or_none()
            if row is None:
                from trading_agent.core.exceptions import TokenExpiredError
                raise TokenExpiredError("No token in DB. Run upstox auth first.")
            return await self._token_manager.get_valid_or_raise(session, row.user_id)

"""Persistence for pre-market briefings."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trading_agent.core.logging import get_logger
from trading_agent.core.time_utils import IST
from trading_agent.infrastructure.db import session_scope
from trading_agent.infrastructure.models import PremarketBriefingRow
from trading_agent.premarket.dtos import (
    Impact,
    PremarketBriefing,
    Sentiment,
)

log = get_logger(__name__)


def _date_to_dt(d: date) -> datetime:
    """Coerce a date to an IST-anchored datetime at 00:00 for primary-key storage."""
    return datetime(d.year, d.month, d.day, tzinfo=IST)


async def save_briefing(session: AsyncSession, briefing: PremarketBriefing) -> None:
    """Upsert today's briefing — replace if it already exists (re-runs allowed)."""
    pk = _date_to_dt(briefing.briefing_date)
    row = await session.get(PremarketBriefingRow, pk)
    if row is None:
        row = PremarketBriefingRow(briefing_date=pk)
        session.add(row)

    row.generated_at = briefing.generated_at
    row.sentiment = briefing.sentiment.value
    row.conviction = Decimal(str(briefing.conviction))
    row.overall_impact = briefing.overall_impact.value
    row.position_size_multiplier = Decimal(str(briefing.position_size_multiplier))
    row.skip_trading = briefing.skip_trading
    row.nifty_bias = briefing.nifty_bias.value
    row.banknifty_bias = briefing.banknifty_bias.value
    row.intraday_phases = briefing.intraday_phases
    row.headlines_summary = briefing.headlines_summary
    row.rationale = briefing.rationale
    row.agent_messages = briefing.agent_messages
    row.tools_used = briefing.tools_used
    row.tokens_used = briefing.tokens_used
    row.cost_inr = Decimal(str(briefing.cost_inr))

    await session.commit()
    log.info(
        "premarket.briefing.saved",
        briefing_date=briefing.briefing_date.isoformat(),
        sentiment=briefing.sentiment.value,
        conviction=briefing.conviction,
    )


async def load_briefing(
    session: AsyncSession, briefing_date: date
) -> PremarketBriefing | None:
    """Load a briefing for a specific date. Returns None if not stored."""
    row = await session.get(PremarketBriefingRow, _date_to_dt(briefing_date))
    if row is None:
        return None
    return _row_to_briefing(row)


async def load_latest_briefing(session: AsyncSession) -> PremarketBriefing | None:
    """Load the most recent briefing (any date). Used by dashboard + strategy worker."""
    result = await session.execute(
        select(PremarketBriefingRow)
        .order_by(PremarketBriefingRow.briefing_date.desc())
        .limit(1)
    )
    row = result.scalar_one_or_none()
    if row is None:
        return None
    return _row_to_briefing(row)


async def load_latest_briefing_via_scope() -> PremarketBriefing | None:
    """Convenience: open a session, load latest, close. Use from non-async-session contexts."""
    async with session_scope() as session:
        return await load_latest_briefing(session)


def _row_to_briefing(row: PremarketBriefingRow) -> PremarketBriefing:
    # Convert to IST before taking .date() — the PK is IST midnight stored as
    # UTC under the hood, so naively calling .date() on the UTC datetime
    # would silently roll back one day.
    return PremarketBriefing(
        briefing_date=row.briefing_date.astimezone(IST).date(),
        generated_at=row.generated_at,
        sentiment=Sentiment(row.sentiment),
        conviction=float(row.conviction),
        overall_impact=Impact(row.overall_impact),
        position_size_multiplier=float(row.position_size_multiplier),
        skip_trading=row.skip_trading,
        nifty_bias=Sentiment(row.nifty_bias),
        banknifty_bias=Sentiment(row.banknifty_bias),
        intraday_phases=row.intraday_phases or {},
        headlines_summary=row.headlines_summary,
        rationale=row.rationale,
        agent_messages=row.agent_messages or [],
        tools_used=row.tools_used or [],
        tokens_used=row.tokens_used or 0,
        cost_inr=float(row.cost_inr or 0),
    )

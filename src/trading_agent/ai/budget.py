"""
Per-agent LLM token budget — operator-managed quota.

Operator sets an `allowance` (tokens) per agent via the dashboard. Each
agent's pre-call check computes `consumed = sum(tokens) since refilled_at`
from llm_usage_log and refuses to call Claude when consumed >= allowance.

Operator clicks "Refill" on the dashboard → updates refilled_at to now,
resetting the consumption counter to zero (without touching historical
usage log).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from trading_agent.core.logging import get_logger
from trading_agent.core.time_utils import now_ist
from trading_agent.infrastructure.db import session_scope
from trading_agent.infrastructure.models import (
    AgentTokenBudgetRow,
    LlmUsageLogRow,
)

log = get_logger(__name__)


class BudgetExhaustedError(Exception):
    """Raised by check_budget_or_raise when an agent has consumed its allowance."""
    def __init__(self, agent_name: str, allowance: int, consumed: int):
        self.agent_name = agent_name
        self.allowance = allowance
        self.consumed = consumed
        super().__init__(
            f"Budget exhausted for '{agent_name}': "
            f"consumed {consumed:,} / {allowance:,} tokens since last refill. "
            f"Refill via the dashboard."
        )


@dataclass(frozen=True)
class BudgetState:
    agent_name: str
    allowance: int
    consumed: int
    remaining: int
    refilled_at: datetime
    exhausted: bool


# ============================================================
# Read
# ============================================================

async def get_budget(session: AsyncSession, agent_name: str) -> BudgetState | None:
    """Return the budget state for an agent, or None if no budget is configured."""
    row = await session.get(AgentTokenBudgetRow, agent_name)
    if row is None:
        return None

    # Sum tokens (in + out) for this agent since the last refill
    result = await session.execute(
        select(
            func.coalesce(
                func.sum(LlmUsageLogRow.tokens_in + LlmUsageLogRow.tokens_out),
                0,
            )
        ).where(
            LlmUsageLogRow.agent_name == agent_name,
            LlmUsageLogRow.ts >= row.refilled_at,
        )
    )
    consumed = int(result.scalar() or 0)
    remaining = max(0, row.allowance - consumed)

    return BudgetState(
        agent_name=row.agent_name,
        allowance=row.allowance,
        consumed=consumed,
        remaining=remaining,
        refilled_at=row.refilled_at,
        exhausted=consumed >= row.allowance,
    )


async def list_budgets() -> list[BudgetState]:
    """Return budget state for every configured agent (for dashboard)."""
    async with session_scope() as session:
        rows = (await session.execute(select(AgentTokenBudgetRow))).scalars().all()
        out: list[BudgetState] = []
        for r in rows:
            b = await get_budget(session, r.agent_name)
            if b is not None:
                out.append(b)
    return out


# ============================================================
# Write
# ============================================================

async def set_budget(
    agent_name: str,
    allowance: int,
    notes: str | None = None,
    reset_refill: bool = True,
) -> BudgetState:
    """
    Create or update an agent's budget. If reset_refill=True (default),
    refilled_at is set to NOW, which clears consumed back to zero.
    """
    if allowance < 0:
        raise ValueError(f"allowance must be >= 0 (got {allowance})")
    async with session_scope() as session:
        row = await session.get(AgentTokenBudgetRow, agent_name)
        if row is None:
            row = AgentTokenBudgetRow(
                agent_name=agent_name,
                allowance=allowance,
                refilled_at=now_ist(),
                notes=notes,
            )
            session.add(row)
        else:
            row.allowance = allowance
            if reset_refill:
                row.refilled_at = now_ist()
            if notes is not None:
                row.notes = notes
        await session.commit()
        b = await get_budget(session, agent_name)
    log.info(
        "agent_budget.set",
        agent_name=agent_name,
        allowance=allowance,
        reset_refill=reset_refill,
    )
    return b


async def refill_budget(agent_name: str) -> BudgetState:
    """
    Reset consumed counter to zero (sets refilled_at to now). Allowance
    stays the same — operator just topping up. Raises if agent has no
    budget configured.
    """
    async with session_scope() as session:
        row = await session.get(AgentTokenBudgetRow, agent_name)
        if row is None:
            raise ValueError(f"No budget configured for agent '{agent_name}'")
        row.refilled_at = now_ist()
        await session.commit()
        b = await get_budget(session, agent_name)
    log.info("agent_budget.refilled", agent_name=agent_name)
    return b


# ============================================================
# Enforcement (called by agents BEFORE invoking Claude)
# ============================================================

async def check_budget_or_raise(agent_name: str) -> None:
    """
    Pre-call gate for agents. Raises BudgetExhaustedError if the agent
    has consumed its allowance. Returns silently if:
      - no budget is configured for this agent (unlimited mode), OR
      - budget is set and consumed < allowance.

    Never raises on DB errors (fail open — usage logging shouldn't gate
    actual operations). Logs warnings instead.
    """
    try:
        async with session_scope() as session:
            b = await get_budget(session, agent_name)
    except Exception as e:
        log.warning("agent_budget.check_failed", agent_name=agent_name, error=str(e))
        return  # fail-open

    if b is None:
        return  # no budget = unlimited

    if b.exhausted:
        raise BudgetExhaustedError(
            agent_name=b.agent_name,
            allowance=b.allowance,
            consumed=b.consumed,
        )

"""
LLM usage logger + aggregations.

Each agent calls `record_llm_call(...)` after every Claude invocation. The
dashboard reads aggregations via `usage_summary(days=7)`.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select

from trading_agent.core.logging import get_logger
from trading_agent.core.time_utils import IST, now_ist
from trading_agent.infrastructure.db import session_scope
from trading_agent.infrastructure.models import LlmUsageLogRow

log = get_logger(__name__)


# ============================================================
# Recording
# ============================================================

async def record_llm_call(
    agent_name: str,
    backend: str,
    model: str,
    tokens_in: int,
    tokens_out: int,
    cost_inr: float,
    latency_ms: int,
    success: bool = True,
    error: str | None = None,
) -> None:
    """
    Append one usage row. Never raises — usage logging must not break the
    calling agent's main path.

    Args:
        agent_name: "premarket_briefing", "ai_advisor", future agents...
        backend: "anthropic" or "bedrock"
        model: the actual model ID used
        tokens_in / tokens_out: from response.usage
        cost_inr: pre-computed rupee cost
        latency_ms: end-to-end wall time
        success: False if Claude returned an error
        error: error message if success=False (truncated to 500 chars)
    """
    try:
        async with session_scope() as session:
            row = LlmUsageLogRow(
                agent_name=agent_name,
                backend=backend,
                model=model,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost_inr=Decimal(str(cost_inr)),
                latency_ms=latency_ms,
                success=success,
                error=(error[:500] if error else None),
            )
            session.add(row)
            await session.commit()
    except Exception as e:
        # Don't propagate; just log
        log.warning(
            "llm_usage.record_failed",
            agent_name=agent_name,
            error=str(e),
        )


# ============================================================
# Aggregations (read by dashboard)
# ============================================================

async def usage_summary(days: int = 7) -> dict:
    """
    Return a summary for the last N days.

    Shape:
        {
            "window_days": 7,
            "total": {"calls": ..., "tokens": ..., "cost_inr": ..., "failures": ...},
            "by_agent": [
                {"agent_name": "...", "calls": ..., "tokens": ..., "cost_inr": ...},
                ...
            ]
        }
    Returns empty-ish skeleton on DB errors.
    """
    cutoff = now_ist() - timedelta(days=days)
    try:
        async with session_scope() as session:
            # Totals
            total_q = await session.execute(
                select(
                    func.count(LlmUsageLogRow.id),
                    func.coalesce(func.sum(LlmUsageLogRow.tokens_in + LlmUsageLogRow.tokens_out), 0),
                    func.coalesce(func.sum(LlmUsageLogRow.cost_inr), 0),
                    func.coalesce(func.sum(func.cast(~LlmUsageLogRow.success, sa_int())), 0),
                ).where(LlmUsageLogRow.ts >= cutoff)
            )
            total_row = total_q.one()
            total_calls, total_tokens, total_cost, total_fail = total_row

            # Per-agent breakdown
            per_q = await session.execute(
                select(
                    LlmUsageLogRow.agent_name,
                    func.count(LlmUsageLogRow.id).label("calls"),
                    func.sum(LlmUsageLogRow.tokens_in + LlmUsageLogRow.tokens_out).label("tokens"),
                    func.sum(LlmUsageLogRow.cost_inr).label("cost"),
                )
                .where(LlmUsageLogRow.ts >= cutoff)
                .group_by(LlmUsageLogRow.agent_name)
                .order_by(func.sum(LlmUsageLogRow.cost_inr).desc())
            )
            by_agent = [
                {
                    "agent_name": r.agent_name,
                    "calls": int(r.calls),
                    "tokens": int(r.tokens),
                    "cost_inr": float(r.cost),
                }
                for r in per_q
            ]
    except Exception as e:
        log.warning("llm_usage.summary_failed", error=str(e))
        return {
            "window_days": days,
            "total": {"calls": 0, "tokens": 0, "cost_inr": 0.0, "failures": 0},
            "by_agent": [],
        }

    return {
        "window_days": days,
        "total": {
            "calls": int(total_calls or 0),
            "tokens": int(total_tokens or 0),
            "cost_inr": float(total_cost or 0),
            "failures": int(total_fail or 0),
        },
        "by_agent": by_agent,
    }


# Tiny helper — SQLAlchemy needs a Type ref for func.cast.
def sa_int():
    from sqlalchemy import Integer
    return Integer

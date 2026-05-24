"""Tool: get_strategy_performance — how has THIS strategy performed lately?"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select

from trading_agent.core.logging import get_logger
from trading_agent.core.time_utils import now_ist
from trading_agent.infrastructure.db import session_scope
from trading_agent.infrastructure.models import (
    OrderRow,
    PositionRow,
    StrategySignalRow,
)

log = get_logger(__name__)


TOOL_DEFINITION = {
    "name": "get_strategy_performance",
    "description": (
        "Returns recent performance of the strategy that's about to fire "
        "(e.g., ema_crossover_trend). Win rate, total PnL, recent signals "
        "count. Useful to spot 'strategy has been losing all week — be "
        "skeptical' patterns. Default lookback 14 days."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "days": {
                "type": "integer",
                "description": "Lookback window in days. Default 14.",
                "default": 14,
                "minimum": 1,
                "maximum": 90,
            },
        },
    },
}


async def get_strategy_performance(input_dict: dict, signal) -> dict:
    """Returns aggregated stats for `signal.strategy_name`."""
    days = int(input_dict.get("days", 14))
    days = max(1, min(90, days))
    cutoff = now_ist() - timedelta(days=days)

    strategy_name = signal.strategy_name

    try:
        async with session_scope() as session:
            # Signals fired
            signal_count = (
                await session.execute(
                    select(func.count())
                    .select_from(StrategySignalRow)
                    .where(
                        StrategySignalRow.strategy_name == strategy_name,
                        StrategySignalRow.ts >= cutoff,
                    )
                )
            ).scalar() or 0

            # Closed positions tied to this strategy via metadata (best-effort).
            # Schema stores strategy_name in the metadata_ JSONB. Fall back to
            # all closed positions if the query shape isn't right.
            from sqlalchemy.dialects.postgresql import JSONB
            from sqlalchemy import cast
            rows = (
                await session.execute(
                    select(PositionRow)
                    .where(
                        PositionRow.is_open == False,  # noqa: E712
                        PositionRow.closed_at.isnot(None),
                        PositionRow.closed_at >= cutoff,
                        cast(PositionRow.metadata_, JSONB)["strategy_name"].astext == strategy_name,
                    )
                    .order_by(PositionRow.closed_at.desc())
                    .limit(50)
                )
            ).scalars().all()
    except Exception as e:
        log.warning("strategy_perf.query_failed", error=str(e))
        return {"ok": False, "error": f"DB query failed: {e}"}

    if not rows:
        return {
            "ok": True,
            "strategy_name": strategy_name,
            "lookback_days": days,
            "signals_count": int(signal_count),
            "closed_trades_count": 0,
            "note": "no closed trades tagged with this strategy in window",
        }

    wins = 0
    losses = 0
    total_pnl = 0.0
    for r in rows:
        pnl = float(r.pnl_inr or 0)
        total_pnl += pnl
        if pnl > 0:
            wins += 1
        elif pnl < 0:
            losses += 1
    n = len(rows)
    return {
        "ok": True,
        "strategy_name": strategy_name,
        "lookback_days": days,
        "signals_count": int(signal_count),
        "closed_trades_count": n,
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / n, 3) if n else 0.0,
        "total_pnl_inr": round(total_pnl, 2),
        "avg_pnl_inr": round(total_pnl / n, 2) if n else 0.0,
    }

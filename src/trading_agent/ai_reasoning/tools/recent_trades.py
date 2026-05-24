"""Tool: get_recent_trades — closed positions on this underlying in the last N days."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from trading_agent.core.logging import get_logger
from trading_agent.core.time_utils import now_ist
from trading_agent.infrastructure.db import session_scope
from trading_agent.infrastructure.models import PositionRow

log = get_logger(__name__)


TOOL_DEFINITION = {
    "name": "get_recent_trades",
    "description": (
        "Returns the last N closed positions on the current trade's "
        "underlying (e.g. NIFTY, BANKNIFTY). Use this to spot patterns "
        "like 'just stopped out twice today' or 'win rate trending down "
        "this week'. Default lookback 5 days."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "days": {
                "type": "integer",
                "description": "Lookback window in days. Default 5.",
                "default": 5,
                "minimum": 1,
                "maximum": 30,
            },
        },
    },
}


async def get_recent_trades(input_dict: dict, signal) -> dict:
    """Returns recent closed positions on `signal.underlying`."""
    days = int(input_dict.get("days", 5))
    days = max(1, min(30, days))
    cutoff = now_ist() - timedelta(days=days)

    try:
        async with session_scope() as session:
            rows = (
                await session.execute(
                    select(PositionRow)
                    .where(
                        PositionRow.underlying == signal.underlying,
                        PositionRow.is_open == False,  # noqa: E712
                        PositionRow.closed_at.isnot(None),
                        PositionRow.closed_at >= cutoff,
                    )
                    .order_by(PositionRow.closed_at.desc())
                    .limit(20)
                )
            ).scalars().all()
    except Exception as e:
        return {"ok": False, "error": f"DB query failed: {e}"}

    if not rows:
        return {
            "ok": True,
            "underlying": signal.underlying,
            "lookback_days": days,
            "count": 0,
            "note": "no closed trades in window",
        }

    trades = []
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
        trades.append({
            "direction": r.direction,
            "entry": float(r.avg_entry_price),
            "exit": float(r.avg_exit_price or 0),
            "pnl_inr": pnl,
            "opened_at": r.opened_at.isoformat(),
            "closed_at": r.closed_at.isoformat() if r.closed_at else None,
        })
    n = len(trades)
    return {
        "ok": True,
        "underlying": signal.underlying,
        "lookback_days": days,
        "count": n,
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / n, 3) if n else 0.0,
        "total_pnl_inr": round(total_pnl, 2),
        "trades": trades[:10],  # cap detail to keep tokens low
    }

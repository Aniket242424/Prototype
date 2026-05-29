"""Tool: get_today_briefing — fetch the pre-market agent's call for today."""
from __future__ import annotations

from trading_agent.core.logging import get_logger
from trading_agent.core.time_utils import now_ist
from trading_agent.premarket.storage import load_latest_briefing_via_scope

log = get_logger(__name__)


TOOL_DEFINITION = {
    "name": "get_today_briefing",
    "description": (
        "Returns the pre-market agent's call for today: overall sentiment "
        "(STRONG_BULL/BULL/NEUTRAL/BEAR/STRONG_BEAR), per-index bias, "
        "conviction, position-size multiplier, intraday phases, and "
        "rationale. Use this when the trade proposal could conflict with "
        "the morning agent's view — a big red flag."
    ),
    "input_schema": {
        "type": "object",
        "properties": {},
    },
}


async def get_today_briefing(input_dict: dict, signal) -> dict:
    """No input args needed. Returns latest briefing or notes that none exists."""
    try:
        b = await load_latest_briefing_via_scope()
    except Exception as e:
        return {"ok": False, "error": f"failed to load briefing: {e}"}

    if b is None:
        return {"ok": True, "available": False, "note": "no briefing stored yet"}

    today = now_ist().date()
    age_days = (today - b.briefing_date).days
    return {
        "ok": True,
        "available": True,
        "briefing_date": b.briefing_date.isoformat(),
        "age_days": age_days,  # 0 = today, >0 = stale
        "sentiment": b.sentiment.value,
        "conviction": round(b.conviction, 2),
        "overall_impact": b.overall_impact.value,
        "position_size_multiplier": round(b.position_size_multiplier, 2),
        "skip_trading": b.skip_trading,
        "nifty_bias": b.nifty_bias.value,
        "banknifty_bias": b.banknifty_bias.value,
        "intraday_phases": b.intraday_phases,
        "rationale": b.rationale,
    }

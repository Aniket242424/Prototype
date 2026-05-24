"""
Tool layer for the pre-market briefing agent.

Each module defines:
1. A pure async Python function (the actual implementation)
2. An Anthropic-compatible tool definition dict (`TOOL_DEFINITION`) that the
   agent receives — name, description, input_schema

The agent layer (../agent.py) discovers tools via the TOOLS registry below
and dispatches Claude's tool_use blocks to the right implementation.

Tools must be:
- Deterministic given their inputs (so reasoning trace replays cleanly)
- JSON-serializable outputs (the agent receives them as tool_result content)
- Single-purpose (don't combine — let the agent compose)
- Resilient (return structured error info instead of raising; the agent
  decides whether to retry or pivot)
"""
from __future__ import annotations

from trading_agent.premarket.tools.calendar import (
    TOOL_DEFINITION as CALENDAR_TOOL,
    get_calendar_events,
)
from trading_agent.premarket.tools.market_state import (
    TOOL_DEFINITION as MARKET_STATE_TOOL,
    get_market_state,
)


# Tool registry consumed by agent.py
# Maps tool_name -> (anthropic tool definition, async implementation)
TOOLS = {
    "get_calendar_events": (CALENDAR_TOOL, get_calendar_events),
    "get_market_state": (MARKET_STATE_TOOL, get_market_state),
}


def all_tool_definitions() -> list[dict]:
    """Return the list of tool definitions to pass to Anthropic's client."""
    return [defn for defn, _ in TOOLS.values()]

"""
Tool layer for the agentic AI advisor.

Each tool:
- Is a single-purpose async function `impl(input_dict, signal)` that returns
  a JSON-serializable dict (with `ok: bool` for the agent to check).
- Has an Anthropic tool-use schema (`TOOL_DEFINITION`) so Claude knows when
  to call it.

The `signal` parameter is passed implicitly — gives tools access to the
underlying + strategy name they're advising on, so the agent doesn't have
to pass them every call.
"""
from trading_agent.ai_reasoning.tools.today_briefing import (
    TOOL_DEFINITION as BRIEFING_TOOL,
    get_today_briefing,
)
from trading_agent.ai_reasoning.tools.recent_trades import (
    TOOL_DEFINITION as RECENT_TRADES_TOOL,
    get_recent_trades,
)
from trading_agent.ai_reasoning.tools.strategy_perf import (
    TOOL_DEFINITION as STRATEGY_PERF_TOOL,
    get_strategy_performance,
)


# Registry consumed by agentic_advisor.py
TOOLS = {
    "get_today_briefing": (BRIEFING_TOOL, get_today_briefing),
    "get_recent_trades": (RECENT_TRADES_TOOL, get_recent_trades),
    "get_strategy_performance": (STRATEGY_PERF_TOOL, get_strategy_performance),
}


def all_tool_definitions() -> list[dict]:
    """Return the list of Anthropic tool defs to pass to client.messages.create."""
    return [defn for defn, _ in TOOLS.values()]

"""
Pre-market briefing agent — Claude orchestrator (Anthropic tool-use API).

Architecture: the agent reads a system prompt that defines its role and
constraints, then runs a tool-use loop with the deterministic tools in
trading_agent.premarket.tools. Claude decides which tools to call, in
what order, with what arguments — it's not a hardcoded pipeline.

Output: a PremarketBriefing DTO with the agent's final structured call,
plus the full reasoning trace (every tool_use + tool_result block) for
forensics.

Limits:
- max_turns: bounds the tool-use loop (prevents runaway agents)
- max_tokens: caps each LLM response
- Strict DTO validation on the final structured output
"""
from __future__ import annotations

import asyncio
import json
from datetime import date, datetime
from typing import Any

import time

from pydantic import ValidationError

from trading_agent.ai import get_llm_client, get_model_id
from trading_agent.ai.usage import record_llm_call
from trading_agent.core.config import AppSettings, get_settings
from trading_agent.core.logging import get_logger
from trading_agent.core.time_utils import IST, now_ist
from trading_agent.premarket.dtos import (
    Impact,
    PremarketBriefing,
    Sentiment,
)
from trading_agent.premarket.tools import TOOLS, all_tool_definitions

log = get_logger(__name__)


# ============================================================
# Cost tracking (Claude Sonnet 4.6 list pricing as of 2026-05)
# ============================================================
# Update when Anthropic publishes new rates.
_INR_PER_USD = 84.0
_PRICE_PER_MTOK_INPUT_USD = 3.0
_PRICE_PER_MTOK_OUTPUT_USD = 15.0


def _cost_inr(input_tokens: int, output_tokens: int) -> float:
    usd = (
        input_tokens * _PRICE_PER_MTOK_INPUT_USD / 1_000_000
        + output_tokens * _PRICE_PER_MTOK_OUTPUT_USD / 1_000_000
    )
    return round(usd * _INR_PER_USD, 2)


# ============================================================
# System prompt — defines the agent's role + output contract
# ============================================================

SYSTEM_PROMPT = """You are the pre-market briefing agent for an Indian options-buying trading bot. You run once daily at 08:30 IST, 45 minutes before NSE opens at 09:15.

Your job: produce a structured briefing that biases the bot's trading day. You have access to deterministic tools to fetch calendar events and overnight market data. Use them strategically — different days need different focus.

PRINCIPLES:
1. DECIDE WHAT KIND OF DAY IT IS FIRST. Call get_calendar_events for today (and the next 2 days for context). If today has HIGH/EXTREME impact event (RBI policy, FOMC, budget), your analysis must center on that event. If quiet, focus on overnight market state.
2. THE BIGGEST OVERNIGHT SIGNAL IS GIFT NIFTY (if available), THEN US CLOSE, THEN ASIA MARKETS IN PROGRESS. Use get_market_state to fetch these. Don't fetch everything — fetch what's relevant for today's call.
3. BE HONEST ABOUT CONVICTION. If signals conflict (e.g., US +1% but Asia -1.5%), say so and lower conviction. Set position_size_multiplier accordingly.
4. SKIP TRADING ONLY for EXTREME impact + ambiguous direction. Otherwise reduce size; don't go to zero.
5. INTRADAY PHASES MATTER. RBI announcement at 10:00 IST means 09:15-10:00 is anxious flat; 10:00-12:00 is volatile reaction; afternoon depends. Use the intraday_phases field.

YOU MUST PRODUCE FINAL OUTPUT IN THIS EXACT JSON SCHEMA (no markdown, no commentary, pure JSON in your final message):

{
  "sentiment": "STRONG_BULL" | "BULL" | "NEUTRAL" | "BEAR" | "STRONG_BEAR",
  "conviction": float between 0.0 and 1.0,
  "overall_impact": "LOW" | "MEDIUM" | "HIGH" | "EXTREME",
  "position_size_multiplier": float between 0.0 and 1.5,
  "skip_trading": boolean,
  "nifty_bias": "STRONG_BULL" | "BULL" | "NEUTRAL" | "BEAR" | "STRONG_BEAR",
  "banknifty_bias": "STRONG_BULL" | "BULL" | "NEUTRAL" | "BEAR" | "STRONG_BEAR",
  "intraday_phases": {"HH:MM-HH:MM": "BULL|BEAR|NEUTRAL", ...},
  "headlines_summary": "1-2 sentence summary of the overnight setup",
  "rationale": "1 paragraph explaining your reasoning — what you saw, how you weighted signals, what changed your mind"
}

OPERATIONAL LIMITS:
- You have at most 6 tool-use rounds. Plan accordingly.
- If a tool returns ok:false, try once more with different args, then proceed with what you have.
- Don't fabricate data. If GIFT NIFTY is missing (it often is on Yahoo), note that and use US/Asia as proxies.
- Your audience is the bot, not a human reader. Be terse in rationale (under 80 words). Precision > polish.
"""


# ============================================================
# Tool dispatcher
# ============================================================

async def _dispatch_tool(tool_name: str, tool_input: dict) -> str:
    """Invoke a tool by name and return its JSON-serialized result."""
    if tool_name not in TOOLS:
        return json.dumps({"ok": False, "error": f"unknown tool: {tool_name}"})
    _, impl = TOOLS[tool_name]
    try:
        result = await impl(**tool_input)
    except TypeError as e:
        return json.dumps({"ok": False, "error": f"bad arguments to {tool_name}: {e}"})
    except Exception as e:
        log.exception("premarket.tool.exception", tool_name=tool_name)
        return json.dumps({"ok": False, "error": f"tool failed: {e}"})
    return json.dumps(result, default=str)


# ============================================================
# Agent runner
# ============================================================

async def run_briefing_agent(
    settings: AppSettings | None = None,
    briefing_date: date | None = None,
    max_turns: int = 6,
    max_tokens_per_turn: int = 2000,
) -> PremarketBriefing:
    """
    Run the agent once and return a structured PremarketBriefing.

    Raises:
        ValueError: if Claude returns malformed JSON or the schema is invalid
        RuntimeError: if the tool-use loop exceeds max_turns without a final answer
    """
    settings = settings or get_settings()
    briefing_date = briefing_date or now_ist().date()

    client = get_llm_client(settings)
    model_id = get_model_id(settings)

    # Bootstrap user message — gives the agent a date anchor + task
    user_msg = (
        f"Briefing date: {briefing_date.isoformat()}. Current IST time: "
        f"{now_ist().isoformat(timespec='seconds')}.\n\n"
        f"Produce the pre-market briefing for today following your system "
        f"prompt exactly. Begin by understanding what kind of day this is."
    )
    messages: list[dict[str, Any]] = [{"role": "user", "content": user_msg}]
    tools_used: list[str] = []
    total_in = 0
    total_out = 0

    final_text: str | None = None
    for turn in range(max_turns):
        log.info("premarket.agent.turn", turn=turn + 1, max_turns=max_turns)
        turn_started = time.monotonic()
        turn_success = True
        turn_error: str | None = None
        turn_in = 0
        turn_out = 0
        try:
            resp = await client.messages.create(
                model=model_id,
                max_tokens=max_tokens_per_turn,
                system=SYSTEM_PROMPT,
                tools=all_tool_definitions(),
                messages=messages,
            )
            turn_in = resp.usage.input_tokens
            turn_out = resp.usage.output_tokens
        except Exception as e:
            turn_success = False
            turn_error = str(e)[:500]
            raise
        finally:
            # Always log usage — even on failure (turn_in/out are 0 in that case)
            await record_llm_call(
                agent_name="premarket_briefing",
                backend=settings.advisor_backend,
                model=model_id,
                tokens_in=turn_in,
                tokens_out=turn_out,
                cost_inr=_cost_inr(turn_in, turn_out),
                latency_ms=int((time.monotonic() - turn_started) * 1000),
                success=turn_success,
                error=turn_error,
            )
        total_in += turn_in
        total_out += turn_out

        # Append the assistant turn to the conversation
        messages.append({"role": "assistant", "content": resp.content})

        # If Claude is done (stop_reason='end_turn'), grab the final text
        if resp.stop_reason == "end_turn":
            for block in resp.content:
                if block.type == "text":
                    final_text = block.text
                    break
            break

        # If Claude wants to call tools, dispatch them and feed results back
        if resp.stop_reason == "tool_use":
            tool_results: list[dict] = []
            for block in resp.content:
                if block.type == "tool_use":
                    tools_used.append(block.name)
                    log.info(
                        "premarket.agent.tool_call",
                        tool_name=block.name,
                        input=block.input,
                    )
                    tool_output = await _dispatch_tool(block.name, dict(block.input))
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": tool_output,
                    })
            if not tool_results:
                # No actual tool_use blocks despite the stop_reason — abort
                break
            messages.append({"role": "user", "content": tool_results})
            continue

        # Unexpected stop reason (max_tokens, etc.) — break with what we have
        log.warning("premarket.agent.unexpected_stop", stop_reason=resp.stop_reason)
        break
    else:
        raise RuntimeError(
            f"Agent exceeded {max_turns} turns without producing a final answer"
        )

    if not final_text:
        raise RuntimeError("Agent finished without producing final text")

    # Parse the final JSON output
    structured = _parse_final_output(final_text)

    return PremarketBriefing(
        briefing_date=briefing_date,
        generated_at=now_ist(),
        sentiment=structured["sentiment"],
        conviction=structured["conviction"],
        overall_impact=structured["overall_impact"],
        position_size_multiplier=structured["position_size_multiplier"],
        skip_trading=structured["skip_trading"],
        nifty_bias=structured["nifty_bias"],
        banknifty_bias=structured["banknifty_bias"],
        intraday_phases=structured.get("intraday_phases", {}),
        headlines_summary=structured.get("headlines_summary"),
        rationale=structured["rationale"],
        agent_messages=_serialize_messages(messages),
        tools_used=tools_used,
        tokens_used=total_in + total_out,
        cost_inr=_cost_inr(total_in, total_out),
    )


# ============================================================
# Output parsing
# ============================================================

def _parse_final_output(text: str) -> dict:
    """
    Extract the JSON object from the agent's final text response.
    Tolerates surrounding whitespace or stray prose (we asked for pure JSON
    but agents drift).
    """
    text = text.strip()

    # Try direct parse first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Find the first { and last } and try in between
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError(f"Agent output contained no JSON object: {text[:200]}")
    snippet = text[start : end + 1]
    try:
        return json.loads(snippet)
    except json.JSONDecodeError as e:
        raise ValueError(f"Agent output JSON malformed: {e}\nText: {text[:200]}")


def _serialize_messages(messages: list[dict]) -> list[dict]:
    """Convert Anthropic content blocks (objects) to JSON-able dicts for storage."""
    out = []
    for m in messages:
        content = m["content"]
        if isinstance(content, str):
            out.append({"role": m["role"], "content": content})
            continue
        serialized_blocks = []
        for block in content:
            if isinstance(block, dict):
                serialized_blocks.append(block)
            elif hasattr(block, "model_dump"):
                serialized_blocks.append(block.model_dump())
            elif hasattr(block, "__dict__"):
                serialized_blocks.append({"type": getattr(block, "type", "?"), **block.__dict__})
            else:
                serialized_blocks.append({"repr": repr(block)})
        out.append({"role": m["role"], "content": serialized_blocks})
    return out

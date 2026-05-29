"""
Agentic Claude advisor — Phase 7.2.

Replaces the single-shot prompt → JSON pattern with a proper agentic flow:
Claude orchestrates a tool-use loop, deciding dynamically which evidence to
fetch (today's pre-market call, recent trades on this underlying, this
strategy's recent performance) before issuing its veto/pass verdict.

Output shape is identical to the original single-shot advisor
(`AdvisorDecision`) so the Risk Engine + strategy worker need no changes.

Failure modes (all return neutral pass-through `AdvisorDecision`):
- Budget exhausted (agent quota hit) — caller proceeds without advisor input
- LLM API error
- Malformed JSON in final message
- Max turns exceeded

Instrumented via `ai.usage.record_llm_call` + `ai.budget.check_budget_or_raise`
so every turn shows up on the dashboard.
"""
from __future__ import annotations

import json
import time
from datetime import date, datetime
from typing import Any

from trading_agent.ai import get_llm_client, get_model_id
from trading_agent.ai.budget import BudgetExhaustedError, check_budget_or_raise
from trading_agent.ai.usage import record_llm_call
from trading_agent.ai_reasoning.dtos import AdvisorDecision
from trading_agent.ai_reasoning.tools import (
    TOOLS as ADVISOR_TOOLS,
    all_tool_definitions,
)
from trading_agent.core.config import AppSettings, get_settings
from trading_agent.core.logging import get_logger
from trading_agent.core.time_utils import now_ist
from trading_agent.regime.dtos import (
    IndicatorSnapshot,
    Opportunity,
    OptionsIntel,
    RegimeState,
)
from trading_agent.strategy.base import StrategySignal

log = get_logger(__name__)


AGENT_NAME = "ai_advisor"

# Pricing for cost tracking (Claude Sonnet 4.6 rates as of 2026-05).
# Used by record_llm_call. Update when Anthropic changes rates.
_INR_PER_USD = 84.0
_PRICE_PER_MTOK_INPUT_USD = 3.0
_PRICE_PER_MTOK_OUTPUT_USD = 15.0


def _cost_inr(input_tokens: int, output_tokens: int) -> float:
    usd = (
        input_tokens * _PRICE_PER_MTOK_INPUT_USD / 1_000_000
        + output_tokens * _PRICE_PER_MTOK_OUTPUT_USD / 1_000_000
    )
    return round(usd * _INR_PER_USD, 4)


SYSTEM_PROMPT = """You are the AI advisor for a real-money Indian options-buying trading bot. The bot is about to enter a trade. Your job: give it a second opinion — should it proceed, downsize, or veto?

You will receive:
- A trade proposal (strategy + direction + underlying + stop/target prices)
- Market context (regime, options intel, technical indicators, opportunity score)

You have tools to fetch additional evidence:
- `get_today_briefing`: today's pre-market call (sentiment, conviction, intraday phases). Critical — if the morning agent said BEAR and this is a LONG, that's a big red flag.
- `get_recent_trades`: closed positions on this underlying in the last N days. Look for: was the bot just stopped out? Win rate trending down?
- `get_strategy_performance`: this strategy's track record recently. If profit factor < 1 lately, advisor_score should drop.

PRINCIPLES:
1. CALL ONLY THE TOOLS YOU NEED. On a clean setup with great context, you may not need any. On a borderline call, fetch the briefing first.
2. BE TERSE. The bot has 5 seconds to decide. Don't ruminate.
3. BIAS TOWARD THE DETERMINISTIC STACK. Your default advisor_score is 0.7-0.85 (mild support). Veto (< 0.55) only when you see real conflict. NO_TRADE only on hard contradictions (e.g., strategy is LONG but briefing said STRONG_BEAR + stopped out twice today).
4. CITE EVIDENCE. Rationale must mention which tool result(s) drove your call.

YOU MUST PRODUCE FINAL OUTPUT IN THIS EXACT JSON SCHEMA (pure JSON in your final message, no markdown):
{
  "decision": "CALL" | "PUT" | "NO_TRADE",
  "advisor_score": float 0.0-1.0,
  "confidence": float 0.0-1.0,
  "rationale": "1-2 sentences, must cite evidence",
  "warnings": ["short string", ...] up to 3 items
}

OPERATIONAL LIMITS:
- Max 4 tool-use rounds.
- If a tool fails (`ok: false`), proceed without it.
- decision: CALL/PUT must match the proposal's direction unless you're vetoing (NO_TRADE).
- advisor_score < 0.55 = veto by Risk Engine. Use sparingly.
"""


# ============================================================
# Public entry point — drop-in for existing ClaudeAdvisor.evaluate
# ============================================================

async def evaluate(
    signal: StrategySignal,
    regime: RegimeState,
    intel: OptionsIntel | None,
    indicators: IndicatorSnapshot,
    opportunity: Opportunity,
    settings: AppSettings | None = None,
    max_turns: int = 4,
    max_tokens_per_turn: int = 800,
) -> AdvisorDecision:
    """
    Agentic version of the advisor evaluate. Same return type as the
    single-shot advisor — never raises, always returns a structured
    `AdvisorDecision`.
    """
    settings = settings or get_settings()
    default_letter = "CALL" if signal.direction.value == "LONG" else "PUT"
    model_id = get_model_id(settings)

    # Budget gate
    try:
        await check_budget_or_raise(AGENT_NAME)
    except BudgetExhaustedError as e:
        log.warning("ai_advisor.budget_exhausted", error=str(e))
        return _neutral_fallback(default_letter, model_id, f"Budget exhausted: {e}")

    # Build the agent's bootstrap message describing the trade
    user_msg = _build_bootstrap(signal, regime, intel, indicators, opportunity)

    try:
        client = get_llm_client(settings)
    except Exception as e:
        log.warning("ai_advisor.client_init_failed", error=str(e))
        return _neutral_fallback(default_letter, model_id, f"LLM client init failed: {e}")

    messages: list[dict[str, Any]] = [{"role": "user", "content": user_msg}]

    final_text: str | None = None
    for turn in range(max_turns):
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
            log.warning("ai_advisor.api_failed", turn=turn + 1, error=turn_error)
            await record_llm_call(
                agent_name=AGENT_NAME, backend=settings.advisor_backend,
                model=model_id, tokens_in=0, tokens_out=0, cost_inr=0.0,
                latency_ms=int((time.monotonic() - turn_started) * 1000),
                success=False, error=turn_error,
            )
            return _neutral_fallback(default_letter, model_id, f"API error: {turn_error}")

        await record_llm_call(
            agent_name=AGENT_NAME, backend=settings.advisor_backend,
            model=model_id, tokens_in=turn_in, tokens_out=turn_out,
            cost_inr=_cost_inr(turn_in, turn_out),
            latency_ms=int((time.monotonic() - turn_started) * 1000),
            success=True,
        )

        messages.append({"role": "assistant", "content": resp.content})

        if resp.stop_reason == "end_turn":
            for block in resp.content:
                if getattr(block, "type", None) == "text":
                    final_text = block.text
                    break
            break

        if resp.stop_reason == "tool_use":
            tool_results: list[dict] = []
            for block in resp.content:
                if getattr(block, "type", None) == "tool_use":
                    try:
                        out = await _dispatch_tool(block.name, dict(block.input), signal)
                    except Exception as e:
                        out = json.dumps({"ok": False, "error": f"tool exception: {e}"})
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": out,
                    })
            if not tool_results:
                break
            messages.append({"role": "user", "content": tool_results})
            continue

        log.warning("ai_advisor.unexpected_stop", stop_reason=resp.stop_reason)
        break

    if not final_text:
        return _neutral_fallback(default_letter, model_id, "no final text after tool-use loop")

    return _parse_final(final_text, default_letter, model_id)


# ============================================================
# Internal helpers
# ============================================================

def _neutral_fallback(decision_letter: str, model: str, reason: str) -> AdvisorDecision:
    """Mirrors the single-shot advisor's fallback. score=1.0 = pass-through."""
    return AdvisorDecision(
        decision=decision_letter,
        confidence=0.5,
        advisor_score=1.0,
        rationale=f"Neutral fallback (pass-through): {reason}",
        warnings=[],
        model=model,
        raw_response_text="",
        ts=now_ist(),
    )


def _build_bootstrap(
    signal: StrategySignal,
    regime: RegimeState,
    intel: OptionsIntel | None,
    indicators: IndicatorSnapshot,
    opportunity: Opportunity,
) -> str:
    """Compact one-shot context that fits in a single user turn."""
    intel_line = (
        f"IV %ile (30d): {intel.iv_percentile_30d:.2f}, "
        f"ATM CE/PE spread: {intel.atm_ce_spread_bps:.1f}/{intel.atm_pe_spread_bps:.1f} bps"
        if intel else "options intel: unavailable"
    )
    return (
        f"Trade proposal for review:\n"
        f"  Strategy:   {signal.strategy_name}\n"
        f"  Underlying: {signal.underlying}\n"
        f"  Direction:  {signal.direction.value}\n"
        f"  Stop:       {signal.stop_underlying}\n"
        f"  Target:     {signal.target_underlying}\n"
        f"\n"
        f"Market context:\n"
        f"  Regime: {regime.regime.value} (conf {regime.confidence:.2f})\n"
        f"  Opportunity score: {opportunity.score:.2f}\n"
        f"  Indicators: ADX14={indicators.adx14:.1f}, "
        f"RV5={indicators.rv5:.4f}, RV60={indicators.rv60:.4f}\n"
        f"  {intel_line}\n"
        f"\n"
        f"Use tools as needed, then return your decision JSON exactly per the schema."
    )


async def _dispatch_tool(tool_name: str, tool_input: dict, signal: StrategySignal) -> str:
    """Run a tool and return its JSON-serialized result string."""
    if tool_name not in ADVISOR_TOOLS:
        return json.dumps({"ok": False, "error": f"unknown tool: {tool_name}"})
    _, impl = ADVISOR_TOOLS[tool_name]
    # Tools accept (input_dict, signal) — signal is implicit context (underlying, strategy_name)
    try:
        result = await impl(input_dict=tool_input, signal=signal)
    except TypeError as e:
        return json.dumps({"ok": False, "error": f"bad arguments: {e}"})
    return json.dumps(result, default=str)


def _parse_final(text: str, default_letter: str, model: str) -> AdvisorDecision:
    """Extract JSON from final assistant text; fall back to neutral on any error."""
    text = text.strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return _neutral_fallback(default_letter, model, "no JSON in final text")
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError as e:
            return _neutral_fallback(default_letter, model, f"malformed JSON: {e}")

    try:
        decision = data["decision"]
        if decision not in ("CALL", "PUT", "NO_TRADE"):
            return _neutral_fallback(default_letter, model, f"invalid decision: {decision}")

        advisor_score = max(0.0, min(1.0, float(data.get("advisor_score", 0.5))))
        confidence = max(0.0, min(1.0, float(data.get("confidence", 0.5))))
        rationale = str(data.get("rationale", ""))[:500]
        warnings_raw = data.get("warnings", [])
        warnings = (
            [str(w)[:120] for w in warnings_raw][:10]
            if isinstance(warnings_raw, list) else []
        )

        return AdvisorDecision(
            decision=decision,
            confidence=confidence,
            advisor_score=advisor_score,
            rationale=rationale,
            warnings=warnings,
            model=model,
            raw_response_text=text[:2000],
            ts=now_ist(),
        )
    except (KeyError, ValueError, TypeError) as e:
        return _neutral_fallback(default_letter, model, f"schema validation: {e}")

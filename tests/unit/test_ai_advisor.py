"""Tests for the Claude AI advisor (mocked API)."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from trading_agent.ai_reasoning.advisor import ClaudeAdvisor
from trading_agent.ai_reasoning.dtos import AdvisorDecision
from trading_agent.ai_reasoning.prompts import SYSTEM_PROMPT, build_user_prompt
from trading_agent.core.constants import Direction, OptionType, Regime
from trading_agent.regime.dtos import (
    IndicatorSnapshot,
    Opportunity,
    OpportunityScore,
    OptionsIntel,
    RegimeState,
)
from trading_agent.strategy.base import StrategySignal


# ---------- Fixtures ----------

def _signal() -> StrategySignal:
    return StrategySignal(
        strategy_name="ema_crossover_trend",
        underlying="NIFTY",
        direction=Direction.LONG,
        option_type=OptionType.CE,
        stop_underlying=Decimal("23950"),
        target_underlying=Decimal("24100"),
        confidence=0.65,
        rationale={"trigger": "ema_crossover", "adx14": 28.0},
        ts=datetime(2026, 5, 13, 11, 0, tzinfo=timezone.utc),
    )


def _regime() -> RegimeState:
    return RegimeState(
        underlying="NIFTY",
        regime=Regime.TREND_UP,
        confidence=0.7,
        components={},
        ts=datetime(2026, 5, 13, 11, 0, tzinfo=timezone.utc),
    )


def _intel() -> OptionsIntel:
    return OptionsIntel(
        underlying="NIFTY",
        ts=datetime(2026, 5, 13, 11, 0, tzinfo=timezone.utc),
        expiry=date(2026, 5, 20),
        spot=24000.0,
        atm_strike=24000.0,
        atm_call_iv=14.0,
        atm_put_iv=14.5,
        iv_rank_30d=0.4,
        iv_percentile_30d=0.4,
        atm_call_spread_bps=15.0,
        atm_put_spread_bps=18.0,
    )


def _indicators() -> IndicatorSnapshot:
    return IndicatorSnapshot(
        underlying="NIFTY",
        ts=datetime(2026, 5, 13, 11, 0, tzinfo=timezone.utc),
        candles_in_buffer=60,
        ema9=24050.0, ema21=24000.0, ema50=23950.0,
        vwap=24020.0, price_vwap_dev_sigma=0.6,
        atr14=80.0, atr_pct=0.33,
        rv5=22.0, rv15=20.0, rv60=18.0,
        adx14=28.0, plus_di=26.0, minus_di=12.0,
        consec_up_candles=2, consec_down_candles=0,
    )


def _opportunity() -> Opportunity:
    return Opportunity(
        underlying="NIFTY",
        direction=Direction.LONG,
        score=0.7,
        components=OpportunityScore(),
        recommended_expiry=date(2026, 5, 20),
        recommended_strike_band={"low": Decimal("23900"), "high": Decimal("24100")},
        ts=datetime(2026, 5, 13, 11, 0, tzinfo=timezone.utc),
    )


def _mock_claude_client(response_text: str):
    """Build a mock Anthropic client that returns the given text."""
    mock_block = MagicMock()
    mock_block.text = response_text
    mock_msg = MagicMock()
    mock_msg.content = [mock_block]

    client = MagicMock()
    client.messages = MagicMock()
    client.messages.create = AsyncMock(return_value=mock_msg)
    return client


# ---------- Prompt builder tests ----------

def test_system_prompt_is_static_and_non_empty():
    assert len(SYSTEM_PROMPT) > 100
    assert "VETO-ONLY" in SYSTEM_PROMPT
    assert "JSON" in SYSTEM_PROMPT


def test_user_prompt_includes_all_key_inputs():
    prompt = build_user_prompt(
        _signal(), _regime(), _intel(), _indicators(), _opportunity()
    )
    # Should reference core fields
    assert "NIFTY" in prompt
    assert "ema_crossover_trend" in prompt
    assert "TREND_UP" in prompt
    assert "24000" in prompt        # spot or strike
    assert "iv_percentile_30d" in prompt


def test_user_prompt_handles_missing_intel():
    """When intel is None, prompt still builds cleanly."""
    prompt = build_user_prompt(
        _signal(), _regime(), None, _indicators(), _opportunity()
    )
    assert "NIFTY" in prompt
    # Intel fields should be present but null
    assert "null" in prompt or "None" in prompt


# ---------- Parsing tests ----------

async def test_advisor_parses_valid_json_response():
    response = (
        '{"decision": "CALL", "confidence": 0.72, "advisor_score": 0.68, '
        '"rationale": "Trend looks clean", "warnings": []}'
    )
    advisor = ClaudeAdvisor(client_override=_mock_claude_client(response))
    decision = await advisor.evaluate(
        _signal(), _regime(), _intel(), _indicators(), _opportunity()
    )
    assert decision.decision == "CALL"
    assert decision.confidence == 0.72
    assert decision.advisor_score == 0.68
    assert "clean" in decision.rationale.lower()
    assert decision.warnings == []
    assert decision.vetoes_trade is False


async def test_advisor_handles_no_trade_decision():
    response = (
        '{"decision": "NO_TRADE", "confidence": 0.8, "advisor_score": 0.3, '
        '"rationale": "Choppy regime", "warnings": ["VIX rising"]}'
    )
    advisor = ClaudeAdvisor(client_override=_mock_claude_client(response))
    decision = await advisor.evaluate(
        _signal(), _regime(), _intel(), _indicators(), _opportunity()
    )
    assert decision.decision == "NO_TRADE"
    assert decision.vetoes_trade is True
    assert "VIX rising" in decision.warnings


async def test_advisor_handles_code_fence_wrapped_json():
    """Models sometimes wrap JSON in ```json ... ```. Parser should strip."""
    response = (
        '```json\n'
        '{"decision": "CALL", "confidence": 0.7, "advisor_score": 0.6, '
        '"rationale": "OK", "warnings": []}\n'
        '```'
    )
    advisor = ClaudeAdvisor(client_override=_mock_claude_client(response))
    decision = await advisor.evaluate(
        _signal(), _regime(), _intel(), _indicators(), _opportunity()
    )
    assert decision.decision == "CALL"
    assert decision.advisor_score == 0.6


async def test_advisor_falls_back_on_invalid_json():
    response = "Sure, this looks like a good trade! Buy it."
    advisor = ClaudeAdvisor(client_override=_mock_claude_client(response))
    decision = await advisor.evaluate(
        _signal(), _regime(), _intel(), _indicators(), _opportunity()
    )
    # Should fall back to neutral (advisor_score=0.5, doesn't veto)
    assert decision.advisor_score == 0.5
    assert "fallback" in decision.rationale.lower()


async def test_advisor_falls_back_on_invalid_decision_value():
    response = '{"decision": "MAYBE", "confidence": 0.7, "advisor_score": 0.6}'
    advisor = ClaudeAdvisor(client_override=_mock_claude_client(response))
    decision = await advisor.evaluate(
        _signal(), _regime(), _intel(), _indicators(), _opportunity()
    )
    assert decision.advisor_score == 0.5
    assert decision.decision in ("CALL", "PUT", "NO_TRADE")


async def test_advisor_clamps_scores_to_0_1_range():
    response = '{"decision": "CALL", "confidence": 1.5, "advisor_score": -0.2}'
    advisor = ClaudeAdvisor(client_override=_mock_claude_client(response))
    decision = await advisor.evaluate(
        _signal(), _regime(), _intel(), _indicators(), _opportunity()
    )
    assert 0.0 <= decision.confidence <= 1.0
    assert 0.0 <= decision.advisor_score <= 1.0


async def test_advisor_falls_back_on_api_exception():
    """Anthropic API down → neutral fallback, no exception propagates."""
    client = MagicMock()
    client.messages = MagicMock()
    client.messages.create = AsyncMock(side_effect=RuntimeError("503 Service Unavailable"))
    advisor = ClaudeAdvisor(client_override=client)
    decision = await advisor.evaluate(
        _signal(), _regime(), _intel(), _indicators(), _opportunity()
    )
    assert decision.advisor_score == 0.5
    assert "API error" in decision.rationale


async def test_advisor_falls_back_on_empty_response():
    advisor = ClaudeAdvisor(client_override=_mock_claude_client(""))
    decision = await advisor.evaluate(
        _signal(), _regime(), _intel(), _indicators(), _opportunity()
    )
    assert decision.advisor_score == 0.5


async def test_veto_threshold_at_055():
    """advisor_score 0.54 → vetoes; 0.55 → does not."""
    advisor = ClaudeAdvisor(client_override=_mock_claude_client(
        '{"decision": "CALL", "confidence": 0.7, "advisor_score": 0.54, "rationale": "weak"}'
    ))
    d1 = await advisor.evaluate(
        _signal(), _regime(), _intel(), _indicators(), _opportunity()
    )
    assert d1.vetoes_trade is True

    advisor2 = ClaudeAdvisor(client_override=_mock_claude_client(
        '{"decision": "CALL", "confidence": 0.7, "advisor_score": 0.55, "rationale": "ok"}'
    ))
    d2 = await advisor2.evaluate(
        _signal(), _regime(), _intel(), _indicators(), _opportunity()
    )
    assert d2.vetoes_trade is False


async def test_warnings_capped_at_10_items():
    big_warnings = [f"warn-{i}" for i in range(20)]
    response = (
        '{"decision": "CALL", "confidence": 0.7, "advisor_score": 0.7, '
        '"rationale": "OK", "warnings": ' + str(big_warnings).replace("'", '"') + '}'
    )
    advisor = ClaudeAdvisor(client_override=_mock_claude_client(response))
    decision = await advisor.evaluate(
        _signal(), _regime(), _intel(), _indicators(), _opportunity()
    )
    assert len(decision.warnings) <= 10


async def test_advisor_disabled_when_no_api_key():
    """If anthropic_api_key is 'replace_me', advisor is disabled."""
    from trading_agent.core.config import get_settings
    settings = get_settings()
    if settings.anthropic_api_key.get_secret_value() != "replace_me":
        pytest.skip("anthropic_api_key is set — can't test disabled-advisor path")

    advisor = ClaudeAdvisor()    # no client override
    decision = await advisor.evaluate(
        _signal(), _regime(), _intel(), _indicators(), _opportunity()
    )
    assert decision.advisor_score == 0.5
    assert "disabled" in decision.rationale.lower()

"""
Claude advisor — calls the Anthropic API for a structured second opinion.

Resilience properties:
- Timeout: 5s. AI cannot block trading on slow responses.
- Invalid JSON: fallback to NEUTRAL advisor_score=0.5 (doesn't veto, doesn't
  upgrade — lets deterministic stack decide alone).
- API failure: same neutral fallback.
- Caching: hash of (signal + context) is cached in Redis for 60s to avoid
  redundant calls within the same opportunity-evaluation cycle.

The advisor is OPTIONAL. If you don't have an ANTHROPIC_API_KEY set, you
can run the system without it — the worker just skips advisor calls and
uses advisor_score=0.5 default everywhere.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

import orjson
from redis.asyncio import Redis

from trading_agent.ai_reasoning.dtos import AdvisorDecision
from trading_agent.ai_reasoning.prompts import SYSTEM_PROMPT, build_user_prompt
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


# Sentinel returned when API fails / disabled / invalid response
def _neutral_fallback(decision_letter: str, model: str, reason: str) -> AdvisorDecision:
    return AdvisorDecision(
        decision=decision_letter,
        confidence=0.5,
        advisor_score=0.5,
        rationale=f"Neutral fallback: {reason}",
        warnings=[],
        model=model,
        raw_response_text="",
        ts=now_ist(),
    )


def _hash_inputs(
    signal: StrategySignal,
    regime: RegimeState,
    intel: OptionsIntel | None,
    indicators: IndicatorSnapshot,
    opportunity: Opportunity,
) -> str:
    """Stable hash for caching identical advisor queries."""
    payload = {
        "strategy": signal.strategy_name,
        "underlying": signal.underlying,
        "direction": signal.direction.value,
        "stop": str(signal.stop_underlying),
        "target": str(signal.target_underlying),
        "regime": regime.regime.value,
        "regime_conf": regime.confidence,
        "opp_score": opportunity.score,
        "iv_pct": intel.iv_percentile_30d if intel else None,
        "adx": indicators.adx14,
        "rv_ratio": (indicators.rv5 / indicators.rv60)
                    if (indicators.rv5 and indicators.rv60) else None,
    }
    raw = orjson.dumps(payload, option=orjson.OPT_SORT_KEYS)
    return hashlib.sha256(raw).hexdigest()[:16]


class ClaudeAdvisor:
    """
    Wraps the Anthropic API for trade-proposal evaluation.

    Pass `redis=None` to disable caching (useful in tests).
    Pass `client_override` to inject a mock for testing.
    """

    def __init__(
        self,
        settings: AppSettings | None = None,
        redis: Redis | None = None,
        client_override: Any = None,
        timeout_sec: float = 5.0,
        cache_ttl_sec: int = 60,
        max_tokens: int = 600,
    ):
        self._settings = settings or get_settings()
        self._redis = redis
        self._timeout_sec = timeout_sec
        self._cache_ttl_sec = cache_ttl_sec
        self._max_tokens = max_tokens
        # Lazy: import anthropic only when needed (and not in tests with mock)
        self._client = client_override
        self._enabled = bool(
            client_override is not None
            or self._settings.anthropic_api_key.get_secret_value() not in ("", "replace_me")
        )

    async def evaluate(
        self,
        signal: StrategySignal,
        regime: RegimeState,
        intel: OptionsIntel | None,
        indicators: IndicatorSnapshot,
        opportunity: Opportunity,
    ) -> AdvisorDecision:
        """
        Get Claude's veto-or-pass opinion on this trade proposal.

        Always returns an AdvisorDecision. Never raises — failures become
        neutral fallbacks so the trading path stays alive.
        """
        # Sensible default decision letter based on direction
        default_letter = "CALL" if signal.direction.value == "LONG" else "PUT"
        model = self._settings.anthropic_model

        if not self._enabled:
            return _neutral_fallback(default_letter, model, "advisor disabled (no API key)")

        # Cache lookup
        cache_key = None
        if self._redis is not None:
            h = _hash_inputs(signal, regime, intel, indicators, opportunity)
            cache_key = f"advisor:cache:{h}"
            try:
                cached = await self._redis.get(cache_key)
                if cached:
                    try:
                        parsed = AdvisorDecision.model_validate_json(cached)
                        log.debug("advisor.cache_hit", hash=h)
                        return parsed
                    except Exception:
                        pass  # cache invalid, fall through to fresh call
            except Exception:
                pass  # Redis down — don't block on cache

        # Build prompts
        user_prompt = build_user_prompt(
            signal, regime, intel, indicators, opportunity
        )

        # API call
        try:
            client = await self._get_client()
            response_text = await self._call_claude(client, user_prompt)
        except Exception as e:
            log.warning("advisor.api_failed", error=str(e))
            return _neutral_fallback(default_letter, model, f"API error: {e}")

        # Parse response
        decision = self._parse_response(response_text, default_letter, model)

        # Cache the parsed result
        if cache_key and self._redis is not None:
            try:
                await self._redis.set(
                    cache_key, decision.model_dump_json(), ex=self._cache_ttl_sec
                )
            except Exception:
                pass

        return decision

    # ----------------- Internal -----------------

    async def _get_client(self):
        if self._client is not None:
            return self._client
        # Lazy import to avoid hard dependency in tests
        from anthropic import AsyncAnthropic
        self._client = AsyncAnthropic(
            api_key=self._settings.anthropic_api_key.get_secret_value(),
            timeout=self._timeout_sec,
        )
        return self._client

    async def _call_claude(self, client, user_prompt: str) -> str:
        """Make the actual API call. Returns the text content."""
        resp = await client.messages.create(
            model=self._settings.anthropic_model,
            max_tokens=self._max_tokens,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
        # Anthropic SDK returns content as list of blocks; pull the text
        text_parts = []
        for block in resp.content:
            if hasattr(block, "text"):
                text_parts.append(block.text)
        return "".join(text_parts).strip()

    def _parse_response(
        self, raw_text: str, default_letter: str, model: str
    ) -> AdvisorDecision:
        """Strict JSON parse with fallback to neutral on any failure."""
        if not raw_text:
            return _neutral_fallback(default_letter, model, "empty response")

        # Strip code fences if model wrapped JSON in ```json ... ```
        cleaned = raw_text
        if cleaned.startswith("```"):
            # remove opening fence
            cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
            # remove closing fence
            if cleaned.rstrip().endswith("```"):
                cleaned = cleaned.rstrip()[:-3]
            cleaned = cleaned.strip()

        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError as e:
            log.warning("advisor.json_parse_failed", error=str(e), raw=raw_text[:200])
            return _neutral_fallback(default_letter, model, "JSON parse failed")

        try:
            decision = data["decision"]
            if decision not in ("CALL", "PUT", "NO_TRADE"):
                return _neutral_fallback(default_letter, model, f"invalid decision: {decision}")

            advisor_score = float(data.get("advisor_score", 0.5))
            confidence = float(data.get("confidence", 0.5))
            rationale = str(data.get("rationale", ""))[:500]
            warnings_raw = data.get("warnings", [])
            warnings = [str(w)[:120] for w in warnings_raw][:10] if isinstance(warnings_raw, list) else []

            advisor_score = max(0.0, min(1.0, advisor_score))
            confidence = max(0.0, min(1.0, confidence))

            return AdvisorDecision(
                decision=decision,
                confidence=confidence,
                advisor_score=advisor_score,
                rationale=rationale,
                warnings=warnings,
                model=model,
                raw_response_text=raw_text[:2000],
                ts=now_ist(),
            )
        except (KeyError, ValueError, TypeError) as e:
            log.warning("advisor.schema_validation_failed", error=str(e))
            return _neutral_fallback(default_letter, model, f"schema validation: {e}")

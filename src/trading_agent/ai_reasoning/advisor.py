"""
Claude advisor — structured second opinion via direct Anthropic API or AWS Bedrock.

Backends (selected by `advisor_backend` setting):
  - "anthropic" (default): direct Anthropic API; needs ANTHROPIC_API_KEY.
  - "bedrock":             AWS Bedrock; needs IAM permissions (instance role
                            on EC2, or AWS_ACCESS_KEY_ID/SECRET locally). Costs
                            hit your AWS bill instead of Anthropic invoice.

Resilience properties:
- Timeout: 5s. AI cannot block trading on slow responses.
- Invalid JSON: fallback to NEUTRAL advisor_score=1.0 (pass-through, no veto, no sizing penalty).
- API failure (either backend): same neutral fallback.
- Caching: hash of (signal + context) cached in Redis for 60s.

The advisor is OPTIONAL — if the backend isn't reachable (no API key for
Anthropic, or no IAM perms for Bedrock), the worker falls through to the
deterministic stack alone.
"""
from __future__ import annotations

import asyncio
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


# Sentinel returned when API fails / disabled / invalid response.
#
# advisor_score=1.0 (not 0.5) is INTENTIONAL: when the advisor abstains we
# want the deterministic stack to decide alone — that means no veto
# (vetoes_trade checks score < 0.55) AND no sizing penalty (Risk Engine
# uses advisor_score as a confidence multiplier). The `rationale` field
# preserves the "this was a fallback, not a real opinion" audit trail.
def _neutral_fallback(decision_letter: str, model: str, reason: str) -> AdvisorDecision:
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
    Trade-proposal evaluator. Backend = anthropic (direct) | bedrock (AWS).

    Pass `redis=None` to disable caching (useful in tests).
    Pass `client_override` to inject a mock for testing — when set, mock is used
    regardless of backend, so existing tests don't depend on backend choice.
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
        self._backend = self._settings.advisor_backend
        # Mock client for tests (Anthropic SDK-shaped); when set, bypasses real backends
        self._client = client_override
        self._bedrock_client = None  # lazy-init

        if client_override is not None:
            self._enabled = True
        elif self._backend == "bedrock":
            # Bedrock uses IAM role/keys discovered by boto3 — always assume enabled.
            # If perms are wrong the actual invoke_model call will fail and the
            # neutral fallback kicks in.
            self._enabled = True
        else:
            # Direct Anthropic API
            self._enabled = bool(
                self._settings.anthropic_api_key.get_secret_value()
                not in ("", "replace_me")
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
        # `model` is used in the AdvisorDecision audit field. Reflects what we actually called.
        model = (
            self._settings.bedrock_model_id
            if (self._backend == "bedrock" and self._client is None)
            else self._settings.anthropic_model
        )

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

        # API call — route to the chosen backend (mock client always wins for tests)
        try:
            if self._client is not None:
                response_text = await self._call_anthropic(self._client, user_prompt)
            elif self._backend == "bedrock":
                response_text = await self._call_bedrock(user_prompt)
            else:
                client = await self._get_anthropic_client()
                response_text = await self._call_anthropic(client, user_prompt)
        except Exception as e:
            log.warning("advisor.api_failed", backend=self._backend, error=str(e))
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

    async def _get_anthropic_client(self):
        if self._client is not None:
            return self._client
        # Lazy import to avoid hard dependency in tests
        from anthropic import AsyncAnthropic
        self._client = AsyncAnthropic(
            api_key=self._settings.anthropic_api_key.get_secret_value(),
            timeout=self._timeout_sec,
        )
        return self._client

    async def _call_anthropic(self, client, user_prompt: str) -> str:
        """Direct Anthropic API call. Returns the text content."""
        resp = await client.messages.create(
            model=self._settings.anthropic_model,
            max_tokens=self._max_tokens,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
        text_parts = []
        for block in resp.content:
            if hasattr(block, "text"):
                text_parts.append(block.text)
        return "".join(text_parts).strip()

    async def _call_bedrock(self, user_prompt: str) -> str:
        """
        AWS Bedrock invoke_model call. Uses asyncio.to_thread to keep the event
        loop free since boto3 is sync.

        Bedrock's Anthropic models accept the same Messages API format as the
        direct API, just wrapped under invoke_model body with an anthropic_version
        marker. Response shape is also nearly identical.
        """
        body_dict = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": self._max_tokens,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": user_prompt}],
        }

        def _invoke_sync() -> str:
            import boto3  # lazy import — only required when Bedrock is the backend

            if self._bedrock_client is None:
                self._bedrock_client = boto3.client(
                    "bedrock-runtime",
                    region_name=self._settings.aws_region,
                )
            resp = self._bedrock_client.invoke_model(
                modelId=self._settings.bedrock_model_id,
                body=json.dumps(body_dict),
                accept="application/json",
                contentType="application/json",
            )
            payload = json.loads(resp["body"].read())
            # Anthropic-on-Bedrock returns {"content": [{"type":"text","text":"..."}], ...}
            parts = [
                b.get("text", "")
                for b in payload.get("content", [])
                if b.get("type") == "text"
            ]
            return "".join(parts).strip()

        return await asyncio.wait_for(
            asyncio.to_thread(_invoke_sync), timeout=self._timeout_sec
        )

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

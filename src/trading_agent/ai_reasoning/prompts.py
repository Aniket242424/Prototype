"""
Prompt builders for the Claude advisor.

Two prompts:
  - SYSTEM: defines role + output schema. Static — same for every call.
  - USER:   structured JSON of the trade proposal + market context.

Why structured JSON inputs instead of free-text descriptions:
  - Deterministic — same inputs give same prompt (good for cache hits)
  - No hallucination of phantom market data
  - Easy to extend with new fields

Why structured JSON output:
  - Strict schema validation on parse
  - No "the assistant said it was a good trade" subjective interpretation
  - Failures detectable: invalid JSON = treated as fallback (neutral score)
"""
from __future__ import annotations

import json
from typing import Any

from trading_agent.regime.dtos import (
    IndicatorSnapshot,
    Opportunity,
    OptionsIntel,
    RegimeState,
)
from trading_agent.strategy.base import StrategySignal


SYSTEM_PROMPT = """\
You are an institutional options strategist with 20+ years of experience trading Indian index options (NIFTY, BANKNIFTY, FINNIFTY, SENSEX, BANKEX).

You are evaluating a trade proposal that has ALREADY passed a deterministic signal stack: regime classifier, options-intel engine, opportunity ranker, and strategy filters. The trade has reached your desk for a final sanity check before the risk engine runs its 16 deterministic safety gates.

Your authority is VETO-ONLY. You can downgrade the advisor_score (which the risk engine will use as a multiplier), or set decision to NO_TRADE — but you cannot upgrade a trade that the deterministic stack already approved.

Output STRICT JSON with this schema, nothing else:
{
  "decision": "CALL" | "PUT" | "NO_TRADE",
  "confidence": <float in 0..1, your confidence in this decision>,
  "advisor_score": <float in 0..1, how attractive the setup looks vs typical setups>,
  "rationale": "<one or two sentences, the most important reasoning>",
  "warnings": ["<short flag 1>", "<short flag 2>", ...]
}

If advisor_score < 0.55 or decision == "NO_TRADE", the trade will be vetoed.

Things to weigh:
- Regime confidence and persistence (CHOPPY/VOL_COMPRESSION = veto)
- IV percentile vs typical — bloated IV = veto for buyers
- Spread tightness on the chosen strike
- Time-of-day appropriateness (late-day option-buying loses to theta)
- VWAP/momentum alignment
- Anything that looks like FOMO chasing an extended move

Things you do NOT need to check (deterministic stack already did):
- Daily loss cap, max trades/day, kill switch, stale data — risk engine handles
- Strike selection (ATM/ITM1/ITM2 chosen elsewhere)
- Position sizing (sized after your advice)

Output ONLY the JSON, no other text.\
"""


def build_user_prompt(
    signal: StrategySignal,
    regime: RegimeState,
    intel: OptionsIntel | None,
    indicators: IndicatorSnapshot,
    opportunity: Opportunity,
    additional_context: dict[str, Any] | None = None,
) -> str:
    """Build the structured trade-proposal JSON the model receives."""
    body: dict[str, Any] = {
        "trade_proposal": {
            "strategy": signal.strategy_name,
            "underlying": signal.underlying,
            "direction": signal.direction.value,
            "option_type": signal.option_type.value,
            "stop_underlying": str(signal.stop_underlying),
            "target_underlying": str(signal.target_underlying),
            "strategy_confidence": signal.confidence,
            "rationale_from_strategy": signal.rationale,
        },
        "regime": {
            "label": regime.regime.value,
            "confidence": regime.confidence,
            "components": regime.components,
        },
        "indicators": {
            "ema9": indicators.ema9,
            "ema21": indicators.ema21,
            "ema50": indicators.ema50,
            "vwap": indicators.vwap,
            "vwap_sigma": indicators.price_vwap_dev_sigma,
            "adx14": indicators.adx14,
            "plus_di": indicators.plus_di,
            "minus_di": indicators.minus_di,
            "atr14": indicators.atr14,
            "atr_pct_of_spot": indicators.atr_pct,
            "rv5": indicators.rv5,
            "rv15": indicators.rv15,
            "rv60": indicators.rv60,
            "consec_up_candles": indicators.consec_up_candles,
            "consec_down_candles": indicators.consec_down_candles,
        },
        "options_intel": {
            "spot": intel.spot if intel else None,
            "atm_strike": intel.atm_strike if intel else None,
            "atm_call_iv": intel.atm_call_iv if intel else None,
            "atm_put_iv": intel.atm_put_iv if intel else None,
            "iv_rank_30d": intel.iv_rank_30d if intel else None,
            "iv_percentile_30d": intel.iv_percentile_30d if intel else None,
            "iv_skew": intel.iv_skew if intel else None,
            "pcr_oi": intel.pcr_oi if intel else None,
            "pcr_volume": intel.pcr_volume if intel else None,
            "max_pain_strike": intel.max_pain_strike if intel else None,
            "atm_call_spread_bps": intel.atm_call_spread_bps if intel else None,
            "atm_put_spread_bps": intel.atm_put_spread_bps if intel else None,
            "total_gamma_exposure": intel.total_gamma_exposure if intel else None,
        },
        "opportunity": {
            "score": opportunity.score,
            "components": opportunity.components.model_dump() if opportunity.components else {},
        },
        "timestamp_ist": signal.ts.isoformat(),
    }
    if additional_context:
        body["additional_context"] = additional_context

    return (
        "Evaluate the following trade proposal and respond with the JSON "
        "schema described in the system prompt.\n\n"
        + json.dumps(body, indent=2, default=str)
    )

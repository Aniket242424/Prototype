"""
AI Reasoning Engine — Phase 4.5.

Claude as an institutional options strategist. Veto-only authority — can
DOWNGRADE a setup's confidence/score, never UPGRADE one that the
deterministic stack rejected.

The advisor consumes a StrategySignal + market context and returns a
structured JSON decision with:
  decision: CALL | PUT | NO_TRADE
  confidence: 0..1
  advisor_score: 0..1
  rationale: short text
  warnings: list of risk observations

Hard rules (enforced in Risk Engine, not here):
  - advisor_score < 0.55 → Risk Engine vetoes the trade
  - decision: NO_TRADE → Risk Engine vetoes
  - Claude API down/timeout → advisor_score=0.5 default (neutral, doesn't block)

Why veto-only: Claude is NOT a primary signal source. It's a sanity check
on top of the deterministic stack. If it tries to upgrade a marginal
setup, we'd be susceptible to LLM hallucination during real money trading.
By restricting its authority to veto, the worst case is "missed a trade
we'd have taken anyway" — never "took a trade we shouldn't have."
"""
from trading_agent.ai_reasoning.dtos import AdvisorDecision
from trading_agent.ai_reasoning.advisor import ClaudeAdvisor

__all__ = ["AdvisorDecision", "ClaudeAdvisor"]

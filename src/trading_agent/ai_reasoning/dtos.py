"""DTOs for the AI advisor."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class AdvisorDecision(BaseModel):
    """
    Output of `ClaudeAdvisor.evaluate(signal, ctx)`.

    Mirrors the `ai_decisions` table schema for direct persistence.
    """

    model_config = ConfigDict(frozen=True)

    decision: Literal["CALL", "PUT", "NO_TRADE"]
    confidence: float = Field(ge=0, le=1)
    advisor_score: float = Field(ge=0, le=1)
    rationale: str
    warnings: list[str]
    model: str
    raw_response_text: str
    ts: datetime

    @property
    def vetoes_trade(self) -> bool:
        """Convenience: True if Risk Engine should reject based on this advisor output."""
        return self.decision == "NO_TRADE" or self.advisor_score < 0.55

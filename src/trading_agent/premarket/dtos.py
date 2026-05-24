"""DTOs for the pre-market briefing agent — Phase 7."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field


# ============================================================
# Sentiment & impact taxonomies
# ============================================================

class Sentiment(StrEnum):
    """Directional bias for the trading day."""
    STRONG_BULL = "STRONG_BULL"
    BULL = "BULL"
    NEUTRAL = "NEUTRAL"
    BEAR = "BEAR"
    STRONG_BEAR = "STRONG_BEAR"


class Impact(StrEnum):
    """Expected volatility impact of an event on the day."""
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    EXTREME = "EXTREME"  # e.g., RBI policy + budget on same day


# ============================================================
# Tool outputs (returned by deterministic tools to the agent)
# ============================================================

class CalendarEvent(BaseModel):
    """A scheduled event that affects the trading day."""
    date: date
    event_type: str = Field(description="rbi_policy | fomc | budget | nifty_expiry | banknifty_expiry | earnings | inflation_data | gdp_data")
    title: str
    impact: Impact
    market_relevance: str = Field(
        description="Which market(s) primarily affected: NIFTY | NIFTY_BANK | SECTOR_AUTO | GLOBAL | etc."
    )
    notes: str | None = None


class MarketIndicator(BaseModel):
    """A single overnight/pre-market price snapshot."""
    symbol: str = Field(description="Yahoo ticker e.g. ^GSPC, ^IXIC, ^N225, ^HSI, GIFTNIFTY")
    label: str = Field(description="Human-readable: S&P 500, NASDAQ, Nikkei 225, Hang Seng, GIFT NIFTY")
    close: Decimal
    change_pct: float
    last_update: datetime
    relevance_to_nifty: str = Field(
        description="LEAD (e.g. GIFT NIFTY), SYMPATHY (e.g. Nikkei), CONTEXT (e.g. crude)"
    )


class MarketState(BaseModel):
    """Aggregated overnight market state — what the agent sees."""
    captured_at: datetime
    indicators: list[MarketIndicator]
    summary: str = Field(description="One-line summary, e.g. 'GIFT NIFTY +0.8%, US closed flat, Asia mixed'")


# ============================================================
# Final agent output
# ============================================================

class PremarketBriefing(BaseModel):
    """
    The agent's final output for the trading day.
    Persisted to Postgres + pushed to Telegram + read by strategy worker.
    """
    briefing_date: date
    generated_at: datetime

    # The headline call — what the day looks like
    sentiment: Sentiment
    conviction: float = Field(ge=0.0, le=1.0, description="Agent's confidence in the call")
    overall_impact: Impact

    # Position-sizing & risk recommendations the agent derives
    position_size_multiplier: float = Field(
        ge=0.0, le=1.5,
        description="Multiply normal position size by this. 1.0 = normal, 0.5 = halve, 0.0 = stand down"
    )
    skip_trading: bool = Field(
        description="True if agent recommends NOT trading at all today (e.g., extreme uncertainty + high vol event)"
    )

    # Direction bias for each underlying we trade
    nifty_bias: Sentiment
    banknifty_bias: Sentiment

    # Time-of-day nuance (e.g., bearish open, neutral afternoon)
    intraday_phases: dict[str, str] = Field(
        default_factory=dict,
        description="Phase → bias, e.g. {'09:15-10:30': 'BEAR', '10:30-15:30': 'NEUTRAL'}"
    )

    # Provenance — what the agent saw + reasoned about
    key_events_today: list[CalendarEvent] = Field(default_factory=list)
    market_state: MarketState | None = None
    headlines_summary: str | None = None

    # Reasoning trace — full agent transcript for forensics
    rationale: str = Field(description="One-paragraph human-readable summary of why the agent chose this call")
    agent_messages: list[dict] = Field(
        default_factory=list,
        description="Full Anthropic messages array including tool_use and tool_result blocks"
    )
    tools_used: list[str] = Field(default_factory=list)
    tokens_used: int = 0
    cost_inr: float = 0.0


# ============================================================
# Tool-use schemas — these are what Claude sees in tool definitions
# ============================================================

# Each tool's input_schema is defined alongside its implementation in tools/*.py
# This file just defines the OUTPUT shapes returned BACK to the agent.

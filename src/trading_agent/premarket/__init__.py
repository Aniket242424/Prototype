"""
Pre-market briefing agent — Phase 7 (agentic architecture).

Runs once daily at 08:30 IST (~45 min before NSE open). Claude orchestrates
the briefing using Anthropic tool-use — it decides dynamically which tools
to call based on what kind of day it is (RBI policy, FOMC, expiry, earnings,
quiet day). Output is a structured PremarketBriefing + reasoning trace.

Architecture (see [[agentic-architecture-decision]] in auto-memory):

    ┌──────────────────────────────────────┐
    │   Pre-market Agent (Claude)          │  ← agent.py
    │   - reads system prompt              │
    │   - calls tools dynamically          │
    │   - reasons in multiple steps        │
    └──────────┬───────────────────────────┘
               │ Anthropic tool-use API
    ┌──────────┴───────────────────────────┐
    │  Tool layer (deterministic)          │
    ├──────────────────────────────────────┤
    │  get_calendar_events                 │  ← tools/calendar.py
    │  get_market_state                    │  ← tools/market_state.py
    │  get_news_headlines (Day 3+)         │  ← tools/news.py
    │  get_vix_history (Day 3+)            │
    │  Each tool: JSON schema + pure       │
    │  Python function, testable alone     │
    └──────────────────────────────────────┘

Output is persisted to Postgres + pushed to Telegram. Strategy worker reads
the latest briefing at 09:15 to bias direction + position sizing; risk
engine uses HIGH-impact days as a hard veto.

Submodules:
- dtos        — PremarketBriefing, CalendarEvent, MarketState DTOs
- tools/      — LLM-callable tools (each is a single-purpose primitive)
- agent       — Claude orchestrator using Anthropic tool-use API
- worker      — 08:30 IST scheduled job runner
"""

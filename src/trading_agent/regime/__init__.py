"""
Phase 2 — Regime, Options Intelligence, and Opportunity Ranking.

Despite the name, this package now hosts ALL three Phase 2 engines because
they share heavy infrastructure (tick buffer, indicators, chain reading)
and run in a single worker process.

Submodules:
- dtos              — Pydantic DTOs that flow between engines and downstream
- indicators        — ATR, ADX, VWAP, realized-vol, EMA helpers (pandas-backed)
- tick_buffer       — Per-underlying rolling buffer hydrated from DB + pubsub
- regime_engine     — Classifier producing RegimeState every 30s
- options_intel     — Greeks/IV/OI/PCR/max-pain analysis on chain snapshots
- opportunity       — 9-dimensional scorer producing one top Opportunity
- worker            — Orchestrator running all three as concurrent asyncio tasks
"""

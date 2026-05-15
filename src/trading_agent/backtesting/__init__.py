"""
Backtesting Engine — Phase 5.

Replays the production pipeline (regime → opportunity → strategy → risk →
execution → position manager) against historical OHLC bars from Upstox.

Design choices:
- 1-minute OHLC bars for the 5 indices (no historical option premiums available
  on Upstox retail tier, so we account in R-multiples: 1R = abs(entry - stop)).
- AI advisor is SKIPPED in backtest (advisor_score=1.0 neutral pass-through)
  to avoid burning real API credits on what is essentially a calibration loop.
- Disk-cached JSON for fetched bars so repeat runs don't re-pull from Upstox.

Entry points:
- python -m trading_agent.backtesting.ingest --start ... --end ... --underlying NIFTY
- python -m trading_agent.backtesting.run    --start ... --end ... --underlying NIFTY
"""

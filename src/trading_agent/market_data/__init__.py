"""
Market Data Engine — Phase 1.

Live ingestion of ticks (Upstox v3 WebSocket / protobuf), persistence to Postgres,
publication on Redis pubsub channels, per-instrument staleness tracking.

Run the worker via:
    py -3.14 -m trading_agent.market_data.worker
"""
from trading_agent.market_data.dtos import Tick

__all__ = ["Tick"]

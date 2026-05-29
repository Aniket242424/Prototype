"""
Interactive Brokers integration — Phase G.2.

Connects to IB Gateway (running locally or in Docker) via the ib_async
library. Used for US market access: Micro index futures (MES/MNQ/MYM),
metals futures (MGC/SIL), and individual stocks (AAPL/TSLA/etc.).

Architecture:
    Gateway (local process)  ←socket→  client.py (async wrapper)
                                            ↓
                                       market_data.py / orders.py
                                            ↓
                                       Postgres market_data_ticks
                                       + Redis pubsub

Connection settings (env):
    IBKR_GATEWAY_HOST  default 127.0.0.1
    IBKR_GATEWAY_PORT  default 4002 (paper). Live = 4001.
    IBKR_CLIENT_ID     default 1. Each connecting process needs unique ID.
"""
from trading_agent.brokers.ibkr.client import (
    IBKRClient,
    IBKRConnectionError,
    get_client,
)

__all__ = ["IBKRClient", "IBKRConnectionError", "get_client"]

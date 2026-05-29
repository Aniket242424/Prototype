"""
Broker integrations.

Each broker is a self-contained submodule that adapts the broker's native
API to the trading_agent's internal interfaces (market data, orders, positions).

Currently:
- upstox: Indian markets (NSE, BSE, MCX) — Phase 0+
- ibkr: US markets (CME futures, NYSE/NASDAQ stocks) via IB Gateway — Phase G.2
"""

"""
Pluggable strategies for the backtest engine.

A strategy implements the BacktestStrategy protocol — it sees the bar history
and decides whether to open a position. Different strategies can be plugged
into the same BacktestEngine without touching the replay loop.
"""

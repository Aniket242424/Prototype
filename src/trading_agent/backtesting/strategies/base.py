"""
BacktestStrategy protocol — pluggable into BacktestEngine.

Each strategy decides:
1. When it's allowed to consider entries (entry window)
2. Whether the current bar history suggests a LONG/SHORT/skip
3. How wide a stop to use
4. What risk-reward target to aim for
5. When (if ever) to force-exit
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from decimal import Decimal
from typing import Optional, Protocol

from trading_agent.backtesting.dtos import Bar
from trading_agent.core.constants import Direction


@dataclass(frozen=True)
class EntryDecision:
    """Returned by a strategy's should_open() when it wants to enter."""
    direction: Direction
    stop_pct: float                 # fraction of underlying for stop distance
    target_rr: float                # 1:N risk-reward for target
    rationale: str = ""             # human-readable why


class BacktestStrategy(Protocol):
    """Pluggable strategy interface."""

    name: str

    def is_entry_time(self, ts: datetime) -> bool:
        """Is this timestamp within the strategy's entry window?"""
        ...

    def is_force_exit_time(self, ts: datetime) -> bool:
        """Should any open position be force-closed at this timestamp?"""
        ...

    def should_open(self, bar: Bar, history: list[Bar]) -> Optional[EntryDecision]:
        """
        Decide whether to open a new position based on this bar + recent history.

        Args:
          bar: the just-closed bar
          history: list of recent bars, oldest first, NOT including `bar`
                   (i.e., `bar` is the "current" bar to act on)

        Returns:
          EntryDecision if entering, None to skip.
        """
        ...

    # ---- Optional hooks (provide no-op defaults if your strategy doesn't need them) ----

    def on_session_start(self, session_date) -> None:
        """Called once at the first bar of each new trading day. Reset per-day state here."""
        ...

    def on_bar(self, bar: Bar) -> None:
        """
        Called for EVERY bar in chronological order (regardless of entry window).
        Strategies that maintain session-wide state (VWAP, day-high/low, morning
        trend, etc.) can update it here.
        """
        ...

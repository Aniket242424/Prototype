"""
IBKR contract definitions — Phase G.2 starter symbols.

We focus on Micro futures (small enough margin for ~$2,400 capital) and
two liquid US stocks to test the integration end-to-end.

Notes on contract selection:
- For continuous Micro index futures, IB returns a `cont_future` family.
  For order placement we'd need the specific front-month contract. For
  market data subscription we can use the continuous form. We'll handle
  the front-month resolution in orders.py later.
- For US stocks, use `Stock(symbol, "SMART", "USD")` — SMART is IBKR's
  smart router that finds best venue (NASDAQ / NYSE / Edge / etc.).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ib_async.contract import ContFuture, Contract, Stock


@dataclass(frozen=True)
class InstrumentSpec:
    """Our internal handle on a tradable instrument."""
    symbol: str               # our internal symbol — what we use in DB / logs
    contract: Contract        # the ib_async contract object
    asset_class: Literal["future_index", "future_metal", "stock"]
    tick_size: float          # minimum price increment
    multiplier: float         # contract multiplier (1 for stock; $5 for MES; etc.)
    description: str


# ============================================================
# Phase G.2 universe (small enough to start with ~$2,400 capital)
# ============================================================

# Micro US Index Futures (CME) — ~5-10× smaller margin than full size
_MES = InstrumentSpec(
    symbol="MES",
    contract=ContFuture("MES", "CME", currency="USD"),
    asset_class="future_index",
    tick_size=0.25,
    multiplier=5.0,
    description="Micro E-mini S&P 500",
)
_MNQ = InstrumentSpec(
    symbol="MNQ",
    contract=ContFuture("MNQ", "CME", currency="USD"),
    asset_class="future_index",
    tick_size=0.25,
    multiplier=2.0,
    description="Micro E-mini NASDAQ 100",
)
_MYM = InstrumentSpec(
    symbol="MYM",
    contract=ContFuture("MYM", "CBOT", currency="USD"),
    asset_class="future_index",
    tick_size=1.0,
    multiplier=0.5,
    description="Micro E-mini Dow",
)

# Micro metals futures (COMEX)
_MGC = InstrumentSpec(
    symbol="MGC",
    contract=ContFuture("MGC", "COMEX", currency="USD"),
    asset_class="future_metal",
    tick_size=0.10,
    multiplier=10.0,
    description="Micro Gold",
)
_SIL = InstrumentSpec(
    symbol="SIL",
    contract=ContFuture("SIL", "COMEX", currency="USD"),
    asset_class="future_metal",
    tick_size=0.005,
    multiplier=1000.0,
    description="Mini Silver (1,000 oz)",
)

# US Stocks (SMART routing)
_AAPL = InstrumentSpec(
    symbol="AAPL",
    contract=Stock("AAPL", "SMART", "USD"),
    asset_class="stock",
    tick_size=0.01,
    multiplier=1.0,
    description="Apple Inc.",
)
_TSLA = InstrumentSpec(
    symbol="TSLA",
    contract=Stock("TSLA", "SMART", "USD"),
    asset_class="stock",
    tick_size=0.01,
    multiplier=1.0,
    description="Tesla Inc.",
)


# Registry — lookup by our internal symbol
UNIVERSE: dict[str, InstrumentSpec] = {
    spec.symbol: spec
    for spec in [_MES, _MNQ, _MYM, _MGC, _SIL, _AAPL, _TSLA]
}


def by_symbol(symbol: str) -> InstrumentSpec:
    """Look up an InstrumentSpec by our internal symbol."""
    if symbol not in UNIVERSE:
        raise KeyError(f"Unknown IBKR symbol: {symbol}. Known: {sorted(UNIVERSE)}")
    return UNIVERSE[symbol]


def all_symbols() -> list[str]:
    """List of all currently-supported symbols."""
    return sorted(UNIVERSE)

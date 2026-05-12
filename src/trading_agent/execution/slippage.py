"""
Slippage estimation + realized-slippage measurement.

Pre-flight: given current top-of-book, predict bps slippage for our intended
trade size. If predicted > config.max_estimated_slippage_bps, the Execution
Engine aborts the placement (signal becomes dead, not retried at any cost).

Post-fill: compare realized fill VWAP to reference mid, log to slippage_log.
Phase 6 will use this for the slippage-history kill switch.

Slippage components (typical):
  - Half-spread crossed (paying ask, or selling at bid)
  - Depth impact (eating multiple levels)
  - Volatility-conditional noise (during fast moves)
  - Latency (the price moves while our order is in flight)

Phase 3.2 ships a simple model: half-spread + linear-depth-impact. Phase 5
backtest will calibrate against realized historical fills if/when we have
that data.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class SlippageEstimate:
    estimated_bps: float                   # Pre-flight prediction
    reference_mid: Decimal                  # Mid at estimation time
    spread_bps: float                       # Current bid/ask spread in bps
    depth_lots_consumed: float              # How many lots of top-N depth we'd need
    note: str                               # "ok" or short explanation


def estimate_slippage_bps(
    side: str,                             # "BUY" or "SELL"
    target_qty_contracts: int,
    lot_size: int,
    bid: Decimal,
    ask: Decimal,
    bid_qty: int | None,
    ask_qty: int | None,
    volatility_factor: float = 1.0,        # 1.0 normal, >1.0 during vol spikes
) -> SlippageEstimate:
    """
    Pure function — predict slippage in bps for a given order against current top-of-book.

    Simple model:
      slip_bps = half_spread_bps × volatility_factor + depth_impact_bps

    half_spread_bps = ((ask - bid) / mid) × 10000 / 2
    depth_impact_bps = 5 × (qty / available_qty_on_our_side) bps
                       (i.e. eating into the book adds ~5bps per "level")
    """
    mid = (bid + ask) / 2
    if mid <= 0:
        return SlippageEstimate(
            estimated_bps=999.0,
            reference_mid=Decimal("0"),
            spread_bps=999.0,
            depth_lots_consumed=0.0,
            note="invalid_mid",
        )

    spread_bps = float((ask - bid) / mid) * 10000
    half_spread_bps = spread_bps / 2

    # Available qty on our side (BUY consumes ask depth; SELL consumes bid depth)
    available = ask_qty if side == "BUY" else bid_qty
    if available is None or available <= 0:
        return SlippageEstimate(
            estimated_bps=999.0,
            reference_mid=mid,
            spread_bps=spread_bps,
            depth_lots_consumed=0.0,
            note="no_depth_info",
        )

    consumed_ratio = target_qty_contracts / max(1, available)
    depth_impact_bps = 5.0 * max(0.0, consumed_ratio - 1.0) if consumed_ratio > 1.0 else 0.0

    estimated = half_spread_bps * volatility_factor + depth_impact_bps
    return SlippageEstimate(
        estimated_bps=round(estimated, 2),
        reference_mid=mid,
        spread_bps=round(spread_bps, 2),
        depth_lots_consumed=round(consumed_ratio, 2),
        note="ok",
    )


def realized_slippage_bps(
    fill_vwap: Decimal,
    reference_mid: Decimal,
    side: str,
) -> float:
    """
    Compute realized slippage in bps after fill.

    For BUY:  positive bps means we paid above mid (bad)
    For SELL: positive bps means we received below mid (bad)
    """
    if reference_mid <= 0:
        return 0.0
    delta = float((fill_vwap - reference_mid) / reference_mid) * 10000
    if side == "SELL":
        delta = -delta
    return round(delta, 2)

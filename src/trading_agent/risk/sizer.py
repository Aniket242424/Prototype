"""
Position sizer.

Translates a capital + per-trade-cap + chosen contract premium into a
concrete `sized_qty` (in contracts) for the order.

Sizing rule (per docs/architecture/risk_design.md):
    base_qty   = floor(per_trade_max_risk_pct * capital / (premium * lot_size)) * lot_size
    confidence = min(opportunity.score, advisor_score)
    sized_qty  = max(lot_size, floor(base_qty * sqrt(confidence)))

Notes:
- Output is in CONTRACTS, not lots. sized_qty must always be a multiple of lot_size.
- Floor at 1 lot — if 1 lot doesn't fit per_trade_max_risk, return 0 (Risk Engine rejects).
- Conservative bias: rounding always DOWN, never up.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal

from trading_agent.core.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class SizingResult:
    sized_qty: int                       # In contracts; 0 if 1 lot doesn't fit
    sized_lots: int                       # sized_qty / lot_size
    max_outlay_inr: Decimal               # premium × sized_qty
    base_qty: int                         # Pre-confidence-adjustment qty
    confidence_used: float
    reason: str                           # Why this size (or 0)


def size_position(
    capital_inr: Decimal,
    per_trade_max_risk_pct: float,
    premium: Decimal,
    lot_size: int,
    confidence: float,
    max_lots_cap: int = 10,              # safety ceiling regardless of math
) -> SizingResult:
    """
    Compute position size given budget and chosen contract.

    Parameters mirror the Risk Engine's caller signature. Pure function — no I/O.
    """
    if capital_inr <= 0 or premium <= 0 or lot_size <= 0:
        return SizingResult(
            sized_qty=0, sized_lots=0, max_outlay_inr=Decimal("0"),
            base_qty=0, confidence_used=0.0,
            reason="invalid_inputs",
        )

    per_trade_budget = capital_inr * Decimal(str(per_trade_max_risk_pct))
    one_lot_outlay = premium * lot_size

    if one_lot_outlay > per_trade_budget:
        return SizingResult(
            sized_qty=0, sized_lots=0, max_outlay_inr=Decimal("0"),
            base_qty=0, confidence_used=0.0,
            reason=f"one_lot_outlay {one_lot_outlay:.2f} exceeds budget {per_trade_budget:.2f}",
        )

    # How many lots fit the budget?
    max_lots_by_budget = int(per_trade_budget // one_lot_outlay)
    # Apply safety ceiling
    base_lots = min(max_lots_by_budget, max_lots_cap)

    # Confidence-weighted: sqrt makes it less aggressive
    # confidence=1.0 → use full base_lots
    # confidence=0.5 → use ~70% of base_lots
    # confidence=0.25 → use ~50%
    safe_confidence = max(0.01, min(1.0, confidence))   # avoid log/sqrt of 0
    confidence_factor = math.sqrt(safe_confidence)
    adjusted_lots = max(1, int(base_lots * confidence_factor))

    sized_qty = adjusted_lots * lot_size
    max_outlay = premium * sized_qty

    return SizingResult(
        sized_qty=sized_qty,
        sized_lots=adjusted_lots,
        max_outlay_inr=max_outlay,
        base_qty=base_lots * lot_size,
        confidence_used=safe_confidence,
        reason=f"sized {adjusted_lots} lot(s) from {base_lots} max (conf={safe_confidence:.2f})",
    )

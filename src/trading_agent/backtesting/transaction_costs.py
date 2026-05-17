"""
Transaction cost model for backtesting — converts real-world option-trading
costs into an R-multiple haircut applied to each closed trade.

The default values represent:
- NIFTY ITM1/ITM2 options (delta ~0.7, lot 65)
- Upstox retail brokerage (flat Rs 20/order)
- Indian taxes (STT 0.0625% on sell side of options + GST 18% on brokerage/exchange)
- Realistic bid-ask spread (~1.5 pts on option premium per round trip)

The cost is computed in UNDERLYING POINTS (so it can be subtracted from a trade's
R-multiple, where 1R = stop distance in underlying points).

Tune these to model your actual broker/cost setup.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TransactionCostModel:
    # --- Spread ---
    # Round-trip spread cost on the OPTION PREMIUM (pay 0.5-1pt above mid each leg).
    # Default 1.5 pts = ~₹1.5 per share on a typical ITM strike.
    option_spread_pts_roundtrip: float = 1.5

    # --- Option Greeks proxy ---
    # ITM1/ITM2 NIFTY options typically have delta ~0.65-0.75.
    # We use this to convert option-premium-pts back to underlying-pts.
    option_delta_proxy: float = 0.70

    # --- Lot + premium for STT calc ---
    lot_size: int = 65
    avg_premium_inr: float = 120.0

    # --- Broker fees ---
    brokerage_inr_per_roundtrip: float = 40.0     # Upstox: ~Rs 20 per executed order × 2 legs
    stt_pct_on_sell_premium: float = 0.000625      # 0.0625% on sell side of options (Indian regulation)
    other_fees_pct_of_brokerage: float = 0.20      # exchange + SEBI + GST rolled into 20% on top

    def cost_in_underlying_pts(self) -> float:
        """Total round-trip cost expressed in UNDERLYING POINTS per trade.

        This is what the engine subtracts from each trade's PnL_points before
        computing the R-multiple.
        """
        # 1. Spread (already on option premium → convert to underlying-equivalent)
        spread_in_underlying = self.option_spread_pts_roundtrip / max(self.option_delta_proxy, 1e-6)

        # 2. Brokerage + STT + GST + exchange — all in rupees, convert to underlying pts
        stt_inr = self.avg_premium_inr * self.lot_size * self.stt_pct_on_sell_premium
        other_inr = self.brokerage_inr_per_roundtrip * (1.0 + self.other_fees_pct_of_brokerage)
        total_fees_inr = stt_inr + other_inr

        # 1 underlying point ≈ lot_size × delta rupees of option value
        inr_per_underlying_pt = self.lot_size * self.option_delta_proxy
        fees_in_underlying = total_fees_inr / max(inr_per_underlying_pt, 1.0)

        return spread_in_underlying + fees_in_underlying

    def describe(self) -> str:
        """Human-readable summary of the cost components."""
        spread = self.option_spread_pts_roundtrip / max(self.option_delta_proxy, 1e-6)
        stt = self.avg_premium_inr * self.lot_size * self.stt_pct_on_sell_premium
        other = self.brokerage_inr_per_roundtrip * (1.0 + self.other_fees_pct_of_brokerage)
        fees_inr = stt + other
        fees_pts = fees_inr / (self.lot_size * self.option_delta_proxy)
        total = self.cost_in_underlying_pts()
        return (
            f"Transaction costs per round-trip:\n"
            f"  Spread:    {self.option_spread_pts_roundtrip:.2f} option-pts "
            f"= {spread:.2f} underlying-pts\n"
            f"  Brokerage+exch+GST: Rs {other:.2f}\n"
            f"  STT (sell side):    Rs {stt:.2f}\n"
            f"  Total fees Rs:      Rs {fees_inr:.2f} "
            f"= {fees_pts:.2f} underlying-pts\n"
            f"  GRAND TOTAL:        {total:.2f} underlying-pts per round trip"
        )


# Convenient presets
ZERO_COST = TransactionCostModel(
    option_spread_pts_roundtrip=0.0,
    brokerage_inr_per_roundtrip=0.0,
    stt_pct_on_sell_premium=0.0,
    other_fees_pct_of_brokerage=0.0,
)

REALISTIC_NIFTY_ITM = TransactionCostModel()   # uses class defaults

CONSERVATIVE = TransactionCostModel(
    option_spread_pts_roundtrip=2.5,   # worse spreads
    brokerage_inr_per_roundtrip=50.0,  # higher brokerage
)

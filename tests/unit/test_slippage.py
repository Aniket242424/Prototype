"""Tests for slippage estimator + realized-slippage measurement."""
from __future__ import annotations

from decimal import Decimal

from trading_agent.execution.slippage import (
    estimate_slippage_bps,
    realized_slippage_bps,
)


def test_estimate_tight_spread_small_size_low_slippage():
    """Tight spread + thin order → ~half_spread bps."""
    e = estimate_slippage_bps(
        side="BUY",
        target_qty_contracts=25,
        lot_size=25,
        bid=Decimal("100.0"),
        ask=Decimal("100.2"),     # 20 bps spread
        bid_qty=1000,
        ask_qty=1000,
        volatility_factor=1.0,
    )
    # Half spread = 10 bps. Order << depth → no impact.
    assert 8 < e.estimated_bps < 12


def test_estimate_wide_spread_punishes_bps():
    """Wide spread → high half-spread bps."""
    e = estimate_slippage_bps(
        side="BUY",
        target_qty_contracts=25,
        lot_size=25,
        bid=Decimal("100"),
        ask=Decimal("105"),       # 500 bps spread
        bid_qty=1000,
        ask_qty=1000,
    )
    # Half spread = ~250 bps
    assert e.estimated_bps > 200


def test_estimate_consuming_depth_adds_impact():
    """Order > available qty → depth impact bps added."""
    e = estimate_slippage_bps(
        side="BUY",
        target_qty_contracts=500,
        lot_size=25,
        bid=Decimal("100"),
        ask=Decimal("100.2"),     # 20 bps spread, half = 10 bps
        bid_qty=1000,
        ask_qty=100,              # we want 500, only 100 available → 5x ratio
    )
    # half_spread 10 + depth_impact 5*(5-1) = 30 bps
    assert e.estimated_bps > 25


def test_estimate_invalid_mid():
    e = estimate_slippage_bps(
        side="BUY",
        target_qty_contracts=25,
        lot_size=25,
        bid=Decimal("0"),
        ask=Decimal("0"),
        bid_qty=100,
        ask_qty=100,
    )
    assert e.note == "invalid_mid"


def test_estimate_missing_depth_info():
    e = estimate_slippage_bps(
        side="BUY",
        target_qty_contracts=25,
        lot_size=25,
        bid=Decimal("100"),
        ask=Decimal("100.2"),
        bid_qty=None,
        ask_qty=None,
    )
    assert e.note == "no_depth_info"


def test_realized_slippage_buy_above_mid_is_positive():
    bps = realized_slippage_bps(
        fill_vwap=Decimal("100.5"),
        reference_mid=Decimal("100.0"),
        side="BUY",
    )
    # Paid 50 bps above mid → +50 bps slippage (bad)
    assert bps == 50.0


def test_realized_slippage_sell_below_mid_is_positive():
    bps = realized_slippage_bps(
        fill_vwap=Decimal("99.5"),
        reference_mid=Decimal("100.0"),
        side="SELL",
    )
    # Received 50 bps below mid → +50 bps slippage (bad)
    assert bps == 50.0


def test_realized_slippage_buy_below_mid_is_negative_good():
    bps = realized_slippage_bps(
        fill_vwap=Decimal("99.5"),
        reference_mid=Decimal("100.0"),
        side="BUY",
    )
    # Got it below mid → negative slippage (rare, good)
    assert bps < 0


def test_realized_slippage_zero_mid_returns_zero():
    bps = realized_slippage_bps(
        fill_vwap=Decimal("100"),
        reference_mid=Decimal("0"),
        side="BUY",
    )
    assert bps == 0.0

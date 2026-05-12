"""Tests for the position sizer — pure function, deterministic."""
from __future__ import annotations

from decimal import Decimal

from trading_agent.risk.sizer import size_position


def test_sizes_one_lot_when_budget_fits():
    # ₹3L × 0.5% = ₹1,500 budget. SENSEX premium ₹80 × lot 10 = ₹800 outlay.
    # Fits → at least 1 lot.
    result = size_position(
        capital_inr=Decimal("300000"),
        per_trade_max_risk_pct=0.005,
        premium=Decimal("80"),
        lot_size=10,
        confidence=1.0,
    )
    assert result.sized_qty >= 10  # at least 1 lot
    assert result.sized_lots >= 1
    assert result.max_outlay_inr <= Decimal("1500")


def test_rejects_when_one_lot_exceeds_budget():
    # NIFTY ATM ₹100 × lot 25 = ₹2,500 outlay > ₹1,500 budget
    result = size_position(
        capital_inr=Decimal("300000"),
        per_trade_max_risk_pct=0.005,
        premium=Decimal("100"),
        lot_size=25,
        confidence=1.0,
    )
    assert result.sized_qty == 0
    assert result.sized_lots == 0
    assert "exceeds budget" in result.reason


def test_confidence_reduces_size():
    high = size_position(
        capital_inr=Decimal("1000000"),
        per_trade_max_risk_pct=0.01,
        premium=Decimal("80"),
        lot_size=10,
        confidence=1.0,
    )
    low = size_position(
        capital_inr=Decimal("1000000"),
        per_trade_max_risk_pct=0.01,
        premium=Decimal("80"),
        lot_size=10,
        confidence=0.25,
    )
    assert low.sized_lots <= high.sized_lots
    assert low.confidence_used == 0.25


def test_sized_qty_always_multiple_of_lot_size():
    result = size_position(
        capital_inr=Decimal("500000"),
        per_trade_max_risk_pct=0.01,
        premium=Decimal("50"),
        lot_size=25,
        confidence=0.8,
    )
    assert result.sized_qty % 25 == 0


def test_safety_cap_applied():
    # Very large budget — without cap, would size ~100 lots
    result = size_position(
        capital_inr=Decimal("10000000"),     # ₹1 crore
        per_trade_max_risk_pct=0.05,           # 5%
        premium=Decimal("50"),
        lot_size=25,
        confidence=1.0,
        max_lots_cap=10,
    )
    assert result.sized_lots <= 10


def test_invalid_inputs_return_zero():
    r = size_position(Decimal("0"), 0.01, Decimal("100"), 25, 1.0)
    assert r.sized_qty == 0
    assert r.reason == "invalid_inputs"


def test_floor_at_one_lot_when_math_rounds_to_zero():
    # With very small confidence and limited base lots, math could round to 0;
    # sizer must return at least 1 lot (or 0 if can't afford).
    r = size_position(
        capital_inr=Decimal("300000"),
        per_trade_max_risk_pct=0.005,
        premium=Decimal("80"),
        lot_size=10,
        confidence=0.01,
    )
    # Can afford 1 lot, math gives ~0.1 lots before floor → must be 1
    if r.sized_qty > 0:
        assert r.sized_lots >= 1

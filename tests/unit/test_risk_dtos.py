"""Sanity tests for the typed contracts at the Risk Engine boundary."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from trading_agent.core.constants import Direction, OptionType, OrderSide, OrderStatus
from trading_agent.risk.dtos import (
    ExecutionResult,
    Fill,
    Leg,
    RiskDecision,
    TradeIntent,
)


def _leg() -> Leg:
    return Leg(
        side=OrderSide.BUY,
        option_type=OptionType.CE,
        strike=Decimal("24000"),
        expiry=date(2026, 5, 15),
        instrument_key="NSE_FO|12345",
        target_qty=25,
        target_premium=Decimal("100.50"),
    )


def test_leg_is_frozen():
    leg = _leg()
    import pydantic
    try:
        leg.target_qty = 50
    except (pydantic.ValidationError, AttributeError, TypeError):
        pass
    else:
        raise AssertionError("Leg should be frozen")


def test_trade_intent_round_trip():
    intent = TradeIntent(
        strategy_name="ema_crossover_trend",
        underlying="NIFTY",
        direction=Direction.LONG,
        legs=[_leg()],
        stop_underlying=Decimal("23950"),
        target_underlying=Decimal("24150"),
        confidence=0.68,
        ts=datetime(2026, 5, 12, 13, 30, tzinfo=timezone.utc),
    )
    assert len(intent.legs) == 1
    assert intent.confidence == 0.68
    assert intent.direction == Direction.LONG


def test_trade_intent_supports_multi_leg():
    """Multi-leg spread structure (Phase 8) — framework ready."""
    intent = TradeIntent(
        strategy_name="bear_call_spread",   # Phase 8 — not enabled yet
        underlying="NIFTY",
        direction=Direction.SHORT,
        legs=[
            Leg(
                side=OrderSide.SELL,
                option_type=OptionType.CE,
                strike=Decimal("24000"),
                expiry=date(2026, 5, 15),
                instrument_key="NSE_FO|short-leg",
                target_qty=25,
                target_premium=Decimal("100"),
            ),
            Leg(
                side=OrderSide.BUY,
                option_type=OptionType.CE,
                strike=Decimal("24200"),
                expiry=date(2026, 5, 15),
                instrument_key="NSE_FO|long-leg",
                target_qty=25,
                target_premium=Decimal("40"),
            ),
        ],
        stop_underlying=Decimal("24100"),
        target_underlying=Decimal("23900"),
        confidence=0.7,
        ts=datetime(2026, 5, 12, 13, 30, tzinfo=timezone.utc),
    )
    assert len(intent.legs) == 2
    assert intent.legs[0].side == OrderSide.SELL
    assert intent.legs[1].side == OrderSide.BUY


def test_risk_decision_approved_carries_size():
    decision = RiskDecision(
        approved=True,
        code="OK_PAPER",
        reason="Approved in paper mode",
        sized_qty=25,
        sized_lots=1,
        max_outlay_inr=Decimal("2500"),
        inputs_snapshot={"test": "value"},
        ts=datetime(2026, 5, 12, 13, 30, tzinfo=timezone.utc),
    )
    assert decision.approved is True
    assert decision.sized_qty == 25
    assert decision.code == "OK_PAPER"


def test_risk_decision_rejection_has_none_size():
    decision = RiskDecision(
        approved=False,
        code="DAILY_LOSS_CAP",
        reason="Daily loss cap hit",
        sized_qty=None,
        sized_lots=None,
        max_outlay_inr=None,
        inputs_snapshot={"daily_loss_inr": -6500},
        ts=datetime(2026, 5, 12, 13, 30, tzinfo=timezone.utc),
    )
    assert decision.approved is False
    assert decision.sized_qty is None


def test_execution_result_basic():
    result = ExecutionResult(
        order_id=42,
        status=OrderStatus.FILLED,
        fills=[
            Fill(
                leg_index=0, qty=25, price=Decimal("100.55"),
                fee=Decimal("0.5"),
                ts=datetime(2026, 5, 12, 13, 35, tzinfo=timezone.utc),
                is_paper=True,
            )
        ],
        realized_slippage_bps=12.5,
        reference_mid=Decimal("100.50"),
        estimated_slippage_bps=10.0,
        is_paper=True,
    )
    assert result.status == OrderStatus.FILLED
    assert len(result.fills) == 1
    assert result.fills[0].is_paper is True

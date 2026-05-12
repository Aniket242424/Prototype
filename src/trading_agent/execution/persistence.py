"""
Persistence for Execution Engine — keeps it separate from the in-memory
fill loop so the engine logic stays readable + testable.

Writes to:
  - orders                — one row per Order submission (including walks)
  - executions            — one row per Fill event
  - positions             — one row per opened position (and updates on close)
  - slippage_log          — one row per filled order

All writes are best-effort: persistence failures log warnings but don't
crash the trading path. The Risk Engine has already approved this trade;
losing audit data is preferable to losing the position.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import async_sessionmaker

from trading_agent.core.constants import OrderStatus
from trading_agent.core.logging import get_logger
from trading_agent.core.time_utils import now_ist
from trading_agent.execution.dtos import OrderRecord
from trading_agent.infrastructure.models import (
    ExecutionRow,
    OrderRow,
    PositionRow,
    SlippageLogRow,
)
from trading_agent.risk.dtos import (
    ExecutionResult,
    Fill,
    Leg,
    RiskDecision,
    TradeIntent,
)

log = get_logger(__name__)


class ExecutionPersistence:
    """Best-effort persistence helper."""

    def __init__(self, session_factory: async_sessionmaker):
        self._session_factory = session_factory

    async def create_order_row(
        self,
        intent: TradeIntent,
        leg: Leg,
        target_qty: int,
        limit_price: Decimal,
        risk_decision_db_id: int | None,
        is_paper: bool,
    ) -> int | None:
        """Insert orders row, return its id (None on failure)."""
        try:
            async with self._session_factory() as session:
                row = OrderRow(
                    risk_decision_id=risk_decision_db_id,
                    broker_order_id=None,
                    instrument_key=leg.instrument_key,
                    side="BUY" if leg.side.value == "BUY" else "SELL",
                    order_type="LIMIT",
                    qty=target_qty,
                    limit_price=limit_price,
                    status="NEW",
                    is_paper=is_paper,
                )
                session.add(row)
                await session.commit()
                await session.refresh(row)
                return row.id
        except Exception as e:
            log.warning("persist.order_failed", error=str(e))
            return None

    async def update_order_status(
        self,
        order_db_id: int,
        status: str,
        broker_order_id: str | None = None,
        rejection_reason: str | None = None,
        submitted_at: datetime | None = None,
        finalized_at: datetime | None = None,
    ) -> None:
        if order_db_id <= 0:
            return
        try:
            async with self._session_factory() as session:
                row = await session.get(OrderRow, order_db_id)
                if row is None:
                    return
                row.status = status
                if broker_order_id:
                    row.broker_order_id = broker_order_id
                if rejection_reason:
                    row.rejection_reason = rejection_reason
                if submitted_at:
                    row.submitted_at = submitted_at
                if finalized_at:
                    row.finalized_at = finalized_at
                await session.commit()
        except Exception as e:
            log.warning("persist.order_status_failed", error=str(e))

    async def write_fills(self, order_db_id: int, fills: list[Fill]) -> None:
        if order_db_id <= 0 or not fills:
            return
        try:
            async with self._session_factory() as session:
                for f in fills:
                    session.add(ExecutionRow(
                        order_id=order_db_id,
                        fill_qty=f.qty,
                        fill_price=f.price,
                        fee=f.fee,
                        ts=f.ts,
                    ))
                await session.commit()
        except Exception as e:
            log.warning("persist.fills_failed", error=str(e))

    async def write_position(
        self,
        intent: TradeIntent,
        leg: Leg,
        result: ExecutionResult,
        initial_stop_premium: Decimal | None = None,
        target_premium: Decimal | None = None,
    ) -> int | None:
        """Open a position row after a successful fill."""
        if result.status != OrderStatus.FILLED or not result.fills:
            return None
        total_qty = sum(f.qty for f in result.fills)
        total_value = sum(f.qty * f.price for f in result.fills)
        avg_price = (total_value / total_qty).quantize(Decimal("0.01")) if total_qty else Decimal("0")

        try:
            async with self._session_factory() as session:
                row = PositionRow(
                    instrument_key=leg.instrument_key,
                    underlying=intent.underlying,
                    direction=intent.direction.value,
                    qty=total_qty,
                    avg_entry_price=avg_price,
                    initial_stop=initial_stop_premium,
                    target=target_premium,
                    is_open=True,
                    is_paper=result.is_paper,
                    opened_at=now_ist(),
                    metadata_={
                        "strategy_name": intent.strategy_name,
                        "confidence": intent.confidence,
                        "reference_mid": str(result.reference_mid),
                        "realized_slippage_bps": result.realized_slippage_bps,
                    },
                )
                session.add(row)
                await session.commit()
                await session.refresh(row)
                return row.id
        except Exception as e:
            log.warning("persist.position_failed", error=str(e))
            return None

    async def write_slippage_log(
        self,
        order_db_id: int,
        result: ExecutionResult,
        spread_bps_at_entry: float,
    ) -> None:
        if order_db_id <= 0 or result.status != OrderStatus.FILLED:
            return
        try:
            async with self._session_factory() as session:
                session.add(SlippageLogRow(
                    order_id=order_db_id,
                    reference_mid=result.reference_mid,
                    estimated_slippage_bps=Decimal(str(result.estimated_slippage_bps)),
                    realized_slippage_bps=Decimal(str(result.realized_slippage_bps)),
                    spread_bps_at_entry=Decimal(str(spread_bps_at_entry)),
                ))
                await session.commit()
        except Exception as e:
            log.warning("persist.slippage_failed", error=str(e))

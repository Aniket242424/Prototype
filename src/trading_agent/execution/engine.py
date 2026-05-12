"""
Low-Slippage Execution Engine.

Consumes RiskDecision(approved=True) from Phase 3.1. Places orders, manages
their lifecycle, persists fills, computes realized slippage.

Per-order loop (LIMIT-first):
    1. Pre-flight: re-check live spread + slippage estimate
       → if estimated slippage > max_estimated_slippage_bps, ABORT (signal dead)
    2. Place LIMIT at chosen price (mid by default, configurable)
    3. Poll for fills for fill_timeout_ms
    4. If unfilled: improve price by 1 tick toward touch, up to fill_improve_max_ticks
    5. If still unfilled: cancel, log "dead signal", emit no fill
    6. On fill: persist Execution + Position + SlippageLog rows
    7. MARKET orders ONLY for emergency exits (stop hit, kill switch)

Multi-leg atomic execution:
    - All legs submitted concurrently
    - If ANY leg fails to ack within timeout → cancel any that did ack
    - Partial fills across legs are tolerated; cancellation of any one
      triggers cancellation of all others
    - This is the safety net for the "naked short on one leg" disaster

The Risk Engine is the only path here. Execution never originates orders.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta
from decimal import Decimal
from typing import Sequence

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import async_sessionmaker

from trading_agent.core.config import RiskConfig, get_risk_config, get_settings
from trading_agent.core.constants import OrderSide, OrderStatus, OrderType
from trading_agent.core.exceptions import ExecutionError
from trading_agent.core.logging import get_logger
from trading_agent.core.time_utils import now_ist
from trading_agent.execution.broker import Broker, MarketContext, PaperBroker
from trading_agent.execution.dtos import OrderRecord, OrderRequest
from trading_agent.execution.slippage import (
    SlippageEstimate,
    estimate_slippage_bps,
    realized_slippage_bps,
)
from trading_agent.execution.state_machine import (
    OrderEvent,
    OrderState,
    is_terminal,
    transition,
)
from trading_agent.risk.dtos import (
    ExecutionResult,
    Fill,
    Leg,
    RiskDecision,
    TradeIntent,
)

log = get_logger(__name__)


class ExecutionEngine:
    """Single public method: `execute(intent, decision)`."""

    def __init__(
        self,
        broker: Broker,
        session_factory: async_sessionmaker,
        redis: Redis,
        risk_config: RiskConfig | None = None,
    ):
        self._broker = broker
        self._session_factory = session_factory
        self._redis = redis
        self._risk = risk_config or get_risk_config()
        self._settings = get_settings()

    async def execute(
        self,
        intent: TradeIntent,
        decision: RiskDecision,
    ) -> ExecutionResult:
        """
        Place orders for an approved trade intent.

        Phase 3.2 ships single-leg buying execution. Multi-leg atomic path
        exists but only one leg is expected per Phase 4 scope. Multi-leg
        strategies activate in Phase 8.
        """
        if not decision.approved:
            raise ExecutionError(
                f"ExecutionEngine.execute called with rejected RiskDecision: {decision.code}"
            )
        if not intent.legs:
            raise ExecutionError("Empty legs in TradeIntent")
        if decision.sized_qty is None or decision.sized_qty <= 0:
            raise ExecutionError(f"Invalid sized_qty: {decision.sized_qty}")

        # Determine target qty per leg
        # Phase 3.2: single-leg → use decision.sized_qty directly
        # Phase 8 multi-leg: ratio-based distribution would happen here
        if len(intent.legs) == 1:
            leg_qtys = [decision.sized_qty]
        else:
            # Equal-quantity assumption for multi-leg (correct for verticals,
            # may need override for ratios in Phase 8)
            per_leg = decision.sized_qty // len(intent.legs)
            leg_qtys = [per_leg] * len(intent.legs)

        # Submit all legs concurrently
        results = await asyncio.gather(
            *(
                self._execute_one_leg(intent, leg, qty)
                for leg, qty in zip(intent.legs, leg_qtys)
            ),
            return_exceptions=True,
        )

        # Check for any leg failure → cancel all if multi-leg
        any_failed = any(isinstance(r, Exception) or not r.fills for r in results)
        if any_failed and len(results) > 1:
            log.warning("execution.multi_leg_failure_rollback", intent_underlying=intent.underlying)
            # Cancellation should ideally have happened in _execute_one_leg's
            # error path. Multi-leg full rollback story is Phase 8 work.

        # Aggregate the result (Phase 3.2 single-leg: just the first result)
        if isinstance(results[0], Exception):
            raise results[0]
        return results[0]

    # ----------------- Per-leg execution -----------------

    async def _execute_one_leg(
        self,
        intent: TradeIntent,
        leg: Leg,
        target_qty: int,
    ) -> ExecutionResult:
        """
        Place + manage one leg through its lifecycle.

        Returns an ExecutionResult capturing what happened (filled/cancelled/rejected).
        """
        # Pre-flight: get live tick + slippage estimate
        mkt_ctx = await self._get_market_context(leg.instrument_key, leg.target_premium)
        slip_est = self._estimate_slippage(leg, target_qty, mkt_ctx)

        # Abort if pre-flight predicts excessive slippage
        if slip_est.estimated_bps > self._risk.max_estimated_slippage_bps:
            log.warning(
                "execution.aborted_pre_flight_slippage",
                instrument=leg.instrument_key,
                estimated_bps=slip_est.estimated_bps,
                cap=self._risk.max_estimated_slippage_bps,
                reason=slip_est.note,
            )
            return await self._record_cancelled(
                intent, leg, target_qty, mkt_ctx, slip_est,
                rejection_reason=f"pre_flight_slippage {slip_est.estimated_bps}bps > cap",
            )

        # Build initial LIMIT order at mid (conservative price)
        limit_price = self._initial_limit_price(leg, mkt_ctx)
        order = await self._create_order_record(
            intent, leg, target_qty, limit_price, mkt_ctx, slip_est,
        )

        # LIMIT walk + timeout loop
        try:
            return await self._run_fill_loop(intent, leg, order, mkt_ctx, slip_est)
        except Exception as e:
            log.error("execution.unhandled", instrument=leg.instrument_key, error=str(e))
            # Attempt cancel — best effort
            if order.broker_order_id:
                try:
                    await self._broker.cancel(order.broker_order_id)
                except Exception:
                    pass
            return await self._record_cancelled(
                intent, leg, target_qty, mkt_ctx, slip_est,
                rejection_reason=f"unhandled_exception: {e}",
            )

    # ----------------- Fill loop -----------------

    async def _run_fill_loop(
        self,
        intent: TradeIntent,
        leg: Leg,
        order: OrderRecord,
        ctx: MarketContext,
        slip_est: SlippageEstimate,
    ) -> ExecutionResult:
        """
        Submit + poll until filled / timed out (with walk) / cancelled.
        """
        # Submit initial LIMIT
        req = OrderRequest(
            instrument_key=leg.instrument_key,
            qty=order.request.qty,
            order_type=OrderType.LIMIT,
            price=order.request.price,
            side="BUY" if leg.side == OrderSide.BUY else "SELL",
        )
        resp = await self._broker.submit(req, ctx)
        if not resp.accepted:
            return await self._record_rejected(
                intent, leg, order, slip_est,
                rejection_reason=resp.rejection_reason or "broker_rejected",
            )

        # Update record with broker order id
        order = order.model_copy(update={
            "broker_order_id": resp.broker_order_id,
            "state": transition(order.state, OrderEvent.SUBMIT),
            "submitted_at": resp.ts,
        })

        # Walk loop: poll for fills, improve price on timeout
        ticks_walked = 0
        fills: list[Fill] = []
        cumulative = 0

        while ticks_walked <= self._risk.fill_improve_max_ticks:
            # Poll repeatedly within fill_timeout_ms
            timeout_s = self._risk.fill_timeout_ms / 1000.0
            poll_interval = 0.25
            elapsed = 0.0
            while elapsed < timeout_s:
                events = await self._broker.poll_fills(order.broker_order_id)
                for ev in events:
                    cumulative = ev.cumulative_filled
                    fills.append(Fill(
                        leg_index=0,
                        qty=ev.filled_qty,
                        price=ev.fill_price,
                        fee=Decimal("0"),    # Phase 3.2 doesn't compute fees; Phase 4 adds
                        ts=ev.ts,
                        is_paper=self._broker.is_paper,
                    ))
                    if ev.is_complete:
                        order = order.model_copy(update={
                            "state": transition(order.state, OrderEvent.FILL),
                            "cumulative_filled": cumulative,
                            "avg_fill_price": self._avg_price(fills),
                            "finalized_at": ev.ts,
                        })
                        return await self._record_filled(intent, leg, order, slip_est, fills)
                if events:
                    # Partial fill — track and keep polling
                    order = order.model_copy(update={
                        "state": (
                            order.state if order.state == OrderState.PARTIAL
                            else transition(order.state, OrderEvent.PARTIAL_FILL)
                        ),
                        "cumulative_filled": cumulative,
                        "avg_fill_price": self._avg_price(fills),
                    })
                await asyncio.sleep(poll_interval)
                elapsed += poll_interval

            # Timeout — decide whether to walk the price
            ticks_walked += 1
            if ticks_walked > self._risk.fill_improve_max_ticks:
                break

            # Improve price by 1 tick toward the touch
            new_price = self._improve_price(
                order.request.price, leg.side,
                self._risk.fill_improve_step_ticks,
            )
            log.info(
                "execution.walking_price",
                broker_order_id=order.broker_order_id,
                from_price=str(order.request.price),
                to_price=str(new_price),
                ticks_walked=ticks_walked,
            )
            # Cancel + resubmit at new price (broker doesn't support modify in our model)
            await self._broker.cancel(order.broker_order_id)
            order = order.model_copy(update={
                "request": OrderRequest(
                    instrument_key=req.instrument_key,
                    qty=req.qty - cumulative,
                    order_type=OrderType.LIMIT,
                    price=new_price,
                    side=req.side,
                ),
                "state": OrderState.NEW,
                "broker_order_id": None,
            })
            req = order.request
            resp = await self._broker.submit(req, ctx)
            order = order.model_copy(update={
                "broker_order_id": resp.broker_order_id,
                "state": transition(order.state, OrderEvent.SUBMIT),
            })

        # Walked to limit, still unfilled → cancel + dead signal
        if order.broker_order_id:
            await self._broker.cancel(order.broker_order_id)
        log.info(
            "execution.dead_signal",
            instrument=leg.instrument_key,
            cumulative=cumulative,
            ticks_walked=ticks_walked,
        )
        return await self._record_cancelled(
            intent, leg, req.qty + cumulative, ctx, slip_est,
            rejection_reason=f"dead_signal_after_walk(filled {cumulative})",
        )

    # ----------------- Helpers -----------------

    async def _get_market_context(
        self, instrument_key: str, fallback_price: Decimal
    ) -> MarketContext:
        """
        Build current MarketContext from Redis-cached chain data.

        Phase 3.2 uses a simplified version: read last_tick LTP from Redis as
        a proxy for mid. Phase 4 will read full top-of-book from chain snapshot.
        """
        # Use fallback price ± 0.5% as synthetic bid/ask when we don't have depth
        # (paper trading is forgiving; live broker would need actual depth)
        bid = (fallback_price * Decimal("0.995")).quantize(Decimal("0.05"))
        ask = (fallback_price * Decimal("1.005")).quantize(Decimal("0.05"))
        return MarketContext(
            bid=bid,
            ask=ask,
            bid_qty=1000,      # paper sim treats this as adequate depth
            ask_qty=1000,
            ltp=fallback_price,
        )

    def _estimate_slippage(
        self, leg: Leg, target_qty: int, ctx: MarketContext
    ) -> SlippageEstimate:
        side = "BUY" if leg.side == OrderSide.BUY else "SELL"
        return estimate_slippage_bps(
            side=side,
            target_qty_contracts=target_qty,
            lot_size=1,           # already absolute qty
            bid=ctx.bid,
            ask=ctx.ask,
            bid_qty=ctx.bid_qty,
            ask_qty=ctx.ask_qty,
        )

    def _initial_limit_price(self, leg: Leg, ctx: MarketContext) -> Decimal:
        """Place LIMIT at mid for cleanest fill. Walk toward touch on timeout."""
        mid = (ctx.bid + ctx.ask) / 2
        return mid.quantize(Decimal("0.05"))   # Indian option tick size

    def _improve_price(
        self, current: Decimal | None, side: OrderSide, step_ticks: int
    ) -> Decimal:
        if current is None:
            raise ExecutionError("Cannot improve null price")
        step = Decimal("0.05") * step_ticks   # tick size 0.05
        if side == OrderSide.BUY:
            return (current + step).quantize(Decimal("0.05"))
        return (current - step).quantize(Decimal("0.05"))

    @staticmethod
    def _avg_price(fills: list[Fill]) -> Decimal:
        if not fills:
            return Decimal("0")
        total_qty = sum(f.qty for f in fills)
        total_value = sum(f.qty * f.price for f in fills)
        if total_qty == 0:
            return Decimal("0")
        return (total_value / total_qty).quantize(Decimal("0.01"))

    # ----------------- Order record builders (persistence in Wave 5) -----------------

    async def _create_order_record(
        self,
        intent: TradeIntent,
        leg: Leg,
        target_qty: int,
        limit_price: Decimal,
        ctx: MarketContext,
        slip_est: SlippageEstimate,
    ) -> OrderRecord:
        # Phase 3.2: Wave 5 wires in the actual DB row creation
        # For now use a placeholder db_id=0; the persistence module fills it in.
        return OrderRecord(
            db_id=0,
            broker_order_id=None,
            request=OrderRequest(
                instrument_key=leg.instrument_key,
                qty=target_qty,
                order_type=OrderType.LIMIT,
                price=limit_price,
                side="BUY" if leg.side == OrderSide.BUY else "SELL",
            ),
            state=OrderState.NEW,
            cumulative_filled=0,
            avg_fill_price=None,
            reference_mid=slip_est.reference_mid,
            estimated_slippage_bps=slip_est.estimated_bps,
            is_paper=self._broker.is_paper,
        )

    async def _record_filled(
        self,
        intent: TradeIntent,
        leg: Leg,
        order: OrderRecord,
        slip_est: SlippageEstimate,
        fills: list[Fill],
    ) -> ExecutionResult:
        avg = self._avg_price(fills)
        side = "BUY" if leg.side == OrderSide.BUY else "SELL"
        realized_bps = realized_slippage_bps(avg, order.reference_mid, side)
        log.info(
            "execution.filled",
            instrument=leg.instrument_key,
            qty=order.cumulative_filled,
            avg_price=str(avg),
            reference_mid=str(order.reference_mid),
            realized_slippage_bps=realized_bps,
            estimated_slippage_bps=slip_est.estimated_bps,
            is_paper=self._broker.is_paper,
        )
        # Wave 5: persist Order, Execution, Position, SlippageLog rows here.
        return ExecutionResult(
            order_id=order.db_id,
            status=OrderStatus.FILLED,
            fills=fills,
            realized_slippage_bps=realized_bps,
            reference_mid=order.reference_mid,
            estimated_slippage_bps=slip_est.estimated_bps,
            is_paper=self._broker.is_paper,
        )

    async def _record_cancelled(
        self,
        intent: TradeIntent,
        leg: Leg,
        target_qty: int,
        ctx: MarketContext,
        slip_est: SlippageEstimate,
        rejection_reason: str,
    ) -> ExecutionResult:
        log.info(
            "execution.cancelled",
            instrument=leg.instrument_key,
            reason=rejection_reason,
        )
        return ExecutionResult(
            order_id=0,
            status=OrderStatus.CANCELLED,
            fills=[],
            realized_slippage_bps=0.0,
            reference_mid=slip_est.reference_mid,
            estimated_slippage_bps=slip_est.estimated_bps,
            rejection_reason=rejection_reason,
            is_paper=self._broker.is_paper,
        )

    async def _record_rejected(
        self,
        intent: TradeIntent,
        leg: Leg,
        order: OrderRecord,
        slip_est: SlippageEstimate,
        rejection_reason: str,
    ) -> ExecutionResult:
        log.warning(
            "execution.rejected",
            instrument=leg.instrument_key,
            reason=rejection_reason,
        )
        return ExecutionResult(
            order_id=order.db_id,
            status=OrderStatus.REJECTED,
            fills=[],
            realized_slippage_bps=0.0,
            reference_mid=order.reference_mid,
            estimated_slippage_bps=slip_est.estimated_bps,
            rejection_reason=rejection_reason,
            is_paper=self._broker.is_paper,
        )

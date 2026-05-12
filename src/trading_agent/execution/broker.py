"""
Broker adapter — abstract interface + paper-mode + live-mode stub.

The Execution Engine talks to ONE broker interface. Whether that's a paper
simulator or a real Upstox client is decided at runtime based on the live-
trading 3-lock gate. Phase 3.2 ships PaperBroker fully; LiveBroker is a stub
that raises until Phase 6 (real money cutover) wires it up.
"""
from __future__ import annotations

import asyncio
import random
import uuid
from abc import ABC, abstractmethod
from datetime import datetime
from decimal import Decimal

from trading_agent.core.exceptions import ExecutionError
from trading_agent.core.logging import get_logger
from trading_agent.core.time_utils import now_ist
from trading_agent.execution.dtos import FillEvent, OrderRequest, OrderResponse

log = get_logger(__name__)


class Broker(ABC):
    """Abstract broker. Both PaperBroker and LiveBroker satisfy this."""

    is_paper: bool = False

    @abstractmethod
    async def submit(
        self,
        request: OrderRequest,
        market_context: "MarketContext",
    ) -> OrderResponse:
        """Submit an order. Returns ack + broker_order_id (or rejection)."""

    @abstractmethod
    async def poll_fills(
        self, broker_order_id: str
    ) -> list[FillEvent]:
        """Poll for fills since last call. Returns 0+ events."""

    @abstractmethod
    async def cancel(self, broker_order_id: str) -> bool:
        """Cancel an open order. Returns True if cancelled, False if already terminal."""


class MarketContext:
    """Current market microstructure at order submission time. Used by paper sim."""

    def __init__(
        self,
        bid: Decimal,
        ask: Decimal,
        bid_qty: int | None,
        ask_qty: int | None,
        ltp: Decimal,
    ):
        self.bid = bid
        self.ask = ask
        self.bid_qty = bid_qty
        self.ask_qty = ask_qty
        self.ltp = ltp


# ============================================================
# Paper broker — simulates realistic fills
# ============================================================

class PaperBroker(Broker):
    """
    Simulates a broker for paper trades.

    Realistic-enough fill model:
      - LIMIT orders fill if our price >= ask (BUY) or <= bid (SELL) within
        a few cycles of poll_fills(). Otherwise sits unfilled.
      - MARKET orders fill immediately at ask (BUY) or bid (SELL) with a small
        random adverse-selection noise (1-5 bps).
      - Adds a 50-150ms randomized latency to mimic broker round-trip.
      - Occasionally splits fills into 2 partial events (~20% of the time).

    All state in-memory. Process restart = lose paper order state. Acceptable
    for paper trading where each session is independent.
    """

    is_paper = True

    def __init__(self, seed: int | None = None):
        self._orders: dict[str, dict] = {}     # broker_order_id → {request, context, ...}
        self._fills_pending: dict[str, list[FillEvent]] = {}
        # Seeded RNG for reproducibility of paper sessions
        self._rng = random.Random(seed)

    async def submit(
        self, request: OrderRequest, market_context: MarketContext
    ) -> OrderResponse:
        # Simulate latency
        await asyncio.sleep(self._rng.uniform(0.05, 0.15))

        broker_id = f"PAPER-{uuid.uuid4().hex[:12]}"
        ts = now_ist()

        # Decide if/when this order fills
        fills = self._simulate_fills(request, market_context, ts)
        self._orders[broker_id] = {
            "request": request,
            "context": market_context,
            "ts": ts,
        }
        self._fills_pending[broker_id] = fills

        log.info(
            "paper_broker.submitted",
            broker_id=broker_id,
            instrument=request.instrument_key,
            qty=request.qty,
            order_type=request.order_type.value,
            limit_price=str(request.price) if request.price else None,
            fills_planned=len(fills),
        )
        return OrderResponse(
            broker_order_id=broker_id,
            accepted=True,
            ts=ts,
        )

    def _simulate_fills(
        self,
        request: OrderRequest,
        ctx: MarketContext,
        submission_ts: datetime,
    ) -> list[FillEvent]:
        """Decide how this order fills based on current market and order params."""
        from trading_agent.core.constants import OrderType
        ts = submission_ts

        # MARKET order: fills immediately at touch + tiny adverse noise
        if request.order_type == OrderType.MARKET:
            base = ctx.ask if request.side == "BUY" else ctx.bid
            # 1-5 bps adverse-selection noise
            noise_bps = self._rng.uniform(0, 5)
            adverse_factor = (1 + noise_bps / 10000) if request.side == "BUY" else (1 - noise_bps / 10000)
            fill_price = (base * Decimal(str(adverse_factor))).quantize(Decimal("0.01"))
            return [FillEvent(
                broker_order_id="placeholder",   # patched by caller
                filled_qty=request.qty,
                fill_price=fill_price,
                cumulative_filled=request.qty,
                is_complete=True,
                ts=ts,
            )]

        # LIMIT order: fill conditions
        if request.price is None:
            return []   # invalid; caller should reject
        limit = request.price
        will_fill = (
            (request.side == "BUY" and limit >= ctx.ask) or
            (request.side == "SELL" and limit <= ctx.bid)
        )
        if not will_fill:
            # Order rests at the book; ~30% chance of fill within a few poll cycles
            # if the price doesn't move. Modeled by returning no fills initially.
            return []

        # Will fill — possibly split
        if self._rng.random() < 0.20 and request.qty >= 2:
            # Two-part fill
            split = max(1, request.qty // 2)
            return [
                FillEvent(
                    broker_order_id="placeholder",
                    filled_qty=split,
                    fill_price=limit,
                    cumulative_filled=split,
                    is_complete=False,
                    ts=ts,
                ),
                FillEvent(
                    broker_order_id="placeholder",
                    filled_qty=request.qty - split,
                    fill_price=limit,
                    cumulative_filled=request.qty,
                    is_complete=True,
                    ts=ts,
                ),
            ]
        # Single fill at limit price
        return [FillEvent(
            broker_order_id="placeholder",
            filled_qty=request.qty,
            fill_price=limit,
            cumulative_filled=request.qty,
            is_complete=True,
            ts=ts,
        )]

    async def poll_fills(self, broker_order_id: str) -> list[FillEvent]:
        """Return any pending fills and clear them. Caller must record cumulative state."""
        pending = self._fills_pending.get(broker_order_id, [])
        if pending:
            # Patch the broker_id and return + clear
            patched = [
                FillEvent(
                    broker_order_id=broker_order_id,
                    filled_qty=f.filled_qty,
                    fill_price=f.fill_price,
                    cumulative_filled=f.cumulative_filled,
                    is_complete=f.is_complete,
                    ts=f.ts,
                )
                for f in pending
            ]
            self._fills_pending[broker_order_id] = []
            return patched
        return []

    async def cancel(self, broker_order_id: str) -> bool:
        if broker_order_id not in self._orders:
            return False
        # If still has pending fills, those are aborted
        had_pending = bool(self._fills_pending.get(broker_order_id))
        self._fills_pending[broker_order_id] = []
        log.info("paper_broker.cancelled", broker_id=broker_order_id, had_pending=had_pending)
        return True


# ============================================================
# Live broker — stub, gated by live-trading 3-lock
# ============================================================

class LiveBroker(Broker):
    """
    Real Upstox broker. INTENTIONAL STUB — raises on every method.

    Wiring this up requires:
      1. Live-trading 3-lock fully passing (env + file + DB-SHA)
      2. Phase 6 risk-related smoke tests passed
      3. Operator explicit go-ahead in code (not just env flag)

    Until then: the Execution Engine routes to PaperBroker regardless of
    LIVE_TRADING env var. This is a safety net beyond the 3-lock.
    """

    is_paper = False

    async def submit(self, request: OrderRequest, market_context: MarketContext) -> OrderResponse:
        raise ExecutionError(
            "LiveBroker not yet implemented. Real-money trading requires Phase 6 + "
            "operator explicit go-ahead. Currently PaperBroker is the only available "
            "broker — the Execution Engine should never have reached LiveBroker."
        )

    async def poll_fills(self, broker_order_id: str) -> list[FillEvent]:
        raise ExecutionError("LiveBroker stub — see submit() docstring")

    async def cancel(self, broker_order_id: str) -> bool:
        raise ExecutionError("LiveBroker stub — see submit() docstring")

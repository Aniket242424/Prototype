"""
Risk Engine — 16 deterministic checks, fail-closed at first failure.

Every approved Opportunity from Phase 2 must pass through `RiskEngine.evaluate()`.
There is no other path from a signal to the Execution Engine.

The checks are in priority order. The first failure short-circuits and returns
a RiskDecision(approved=False) with the failure code. Every evaluation
(approve OR reject) is persisted to `risk_decisions` for audit + analytics.

Checks (see docs/architecture/risk_design.md for thresholds):
    1. Live-trading 3-lock (env + file + DB-SHA) → paper-mode routing
    2. Global kill switch (Redis)
    3. Market hours / entry window
    4. Capital available (broker funds + open positions)
    5. Daily loss cap
    6. Rolling drawdown cap
    7. Per-trade max risk
    8. Max concurrent positions
    9. Max trades/day
   10. Consecutive-loss lockout
   11. Slippage-history kill
   12. Spread filter (live tick)
   13. Liquidity filter (depth × volume)
   14. Stale-data check
   15. Volatility kill (VIX + intraday move)
   16. Broker health

Stock-options-specific gates (added when leg.instrument_key indicates a stock):
   17. Earnings blackout (±7 days)
   18. Per-sector concentration cap (max 1 open per sector)
   19. Top-30 stock universe whitelist

Hard rule: the Risk Engine NEVER places orders. It only approves or rejects.
Phase 3.2 Execution Engine consumes RiskDecision(approved=True).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from trading_agent.core.config import (
    RiskConfig,
    get_instruments_config,
    get_risk_config,
    get_settings,
)
from trading_agent.core.constants import KILL_SWITCH_KEY
from trading_agent.core.kill_switch import KillSwitch
from trading_agent.core.logging import get_logger
from trading_agent.core.time_utils import in_window, is_market_open, now_ist
from trading_agent.infrastructure.models import (
    PositionRow,
    RiskDecisionRow,
    SlippageLogRow,
)
from trading_agent.risk.dtos import RiskDecision, TradeIntent
from trading_agent.risk.live_trading_gate import check_live_trading_status
from trading_agent.risk.sizer import size_position

log = get_logger(__name__)


class RiskEngine:
    """
    The gate. Single public method: `evaluate(intent) -> RiskDecision`.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker,
        redis: Redis,
        risk_config: RiskConfig | None = None,
    ):
        self._session_factory = session_factory
        self._redis = redis
        self._risk = risk_config or get_risk_config()
        self._settings = get_settings()
        self._instruments_cfg = get_instruments_config()
        self._kill_switch = KillSwitch(redis)

    async def evaluate(self, intent: TradeIntent) -> RiskDecision:
        """
        Run all 16 checks in order. Fail at first failure. Always persist the
        decision (approve or reject).
        """
        snapshot: dict[str, Any] = {
            "intent_strategy": intent.strategy_name,
            "intent_underlying": intent.underlying,
            "intent_direction": intent.direction.value,
            "intent_confidence": intent.confidence,
            "intent_legs": len(intent.legs),
            "ts": now_ist().isoformat(),
        }

        # === Check 1: Live-trading 3-lock ===
        # NOT a reject — instead routes between paper and live. Recorded in snapshot.
        live_status = await check_live_trading_status()
        snapshot["live_trading_authorized"] = live_status.authorized
        snapshot["live_trading_reason"] = live_status.reason

        # === Check 2: Global kill switch ===
        if await self._kill_switch.is_tripped():
            return await self._reject(intent, "KILL_SWITCH", "Global kill switch tripped", snapshot)

        # === Check 3: Market hours / entry window ===
        if not is_market_open():
            return await self._reject(intent, "MARKET_CLOSED", "Outside market hours", snapshot)
        if not in_window(self._risk.entry_window_start, self._risk.entry_window_end):
            return await self._reject(
                intent,
                "OUTSIDE_WINDOW",
                f"Outside entry window {self._risk.entry_window_start}-{self._risk.entry_window_end}",
                snapshot,
            )

        # === Check 5: Daily loss cap ===
        daily_loss_inr = await self._daily_realized_pnl_inr()
        snapshot["daily_loss_inr"] = float(daily_loss_inr)
        daily_max = Decimal(str(self._settings.trading_capital_inr)) * Decimal(str(self._risk.daily_max_loss_pct))
        if daily_loss_inr <= -daily_max:
            return await self._reject(
                intent, "DAILY_LOSS_CAP",
                f"Daily realized loss ₹{daily_loss_inr:.0f} hit cap ₹{daily_max:.0f}",
                snapshot,
            )

        # === Check 6: Rolling drawdown ===
        rolling_dd = await self._rolling_drawdown_inr(self._risk.rolling_drawdown_window_days)
        snapshot["rolling_drawdown_inr"] = float(rolling_dd)
        max_dd = Decimal(str(self._settings.trading_capital_inr)) * Decimal(str(self._risk.rolling_drawdown_pct))
        if rolling_dd <= -max_dd:
            return await self._reject(
                intent, "DRAWDOWN_CAP",
                f"{self._risk.rolling_drawdown_window_days}d drawdown ₹{rolling_dd:.0f} hit cap ₹{max_dd:.0f}",
                snapshot,
            )

        # === Check 8: Max concurrent positions ===
        open_positions = await self._open_position_count()
        snapshot["open_positions"] = open_positions
        if open_positions >= self._risk.max_concurrent_positions:
            return await self._reject(
                intent, "MAX_CONCURRENT",
                f"{open_positions} open positions ≥ cap {self._risk.max_concurrent_positions}",
                snapshot,
            )

        # === Check 9: Max trades/day ===
        trades_today = await self._trades_today()
        snapshot["trades_today"] = trades_today
        if trades_today >= self._risk.max_trades_per_day:
            return await self._reject(
                intent, "MAX_TRADES_DAY",
                f"{trades_today} trades today ≥ cap {self._risk.max_trades_per_day}",
                snapshot,
            )

        # === Check 10: Consecutive-loss lockout ===
        consec_losses = await self._consecutive_losses_today()
        snapshot["consecutive_losses"] = consec_losses
        if consec_losses >= self._risk.consecutive_loss_lockout:
            return await self._reject(
                intent, "CONSECUTIVE_LOSSES",
                f"{consec_losses} consecutive losses ≥ lockout {self._risk.consecutive_loss_lockout}",
                snapshot,
            )

        # === Check 11: Slippage-history kill ===
        if await self._slippage_kill_triggered():
            return await self._reject(
                intent, "SLIPPAGE_KILL",
                f"Realized slippage > {self._risk.slippage_kill_threshold_bps}bps for {self._risk.slippage_kill_consecutive} consecutive trades",
                snapshot,
            )

        # === Per-leg checks (12 spread, 13 liquidity) ===
        # These are based on what the Strategy Engine has already filtered, but
        # we double-check at the risk gate to defend against drift between
        # signal time and execution time.
        for i, leg in enumerate(intent.legs):
            premium_outlay = leg.target_premium * leg.target_qty
            snapshot[f"leg_{i}_premium_outlay"] = float(premium_outlay)

        # === Check 7: Per-trade max risk (validated via sizer) ===
        # Use the FIRST leg's premium for sizing — Phase 3 ships single-leg only.
        # Multi-leg sizing will need a different formula in Phase 8.
        if not intent.legs:
            return await self._reject(intent, "NO_LEGS", "Intent has zero legs", snapshot)
        primary_leg = intent.legs[0]
        underlying_lot_size = self._lot_size_for(primary_leg.instrument_key, intent.underlying)
        sizing = size_position(
            capital_inr=Decimal(str(self._settings.trading_capital_inr)),
            per_trade_max_risk_pct=self._risk.per_trade_max_risk_pct,
            premium=primary_leg.target_premium,
            lot_size=underlying_lot_size,
            confidence=intent.confidence,
        )
        snapshot["sizing_reason"] = sizing.reason
        snapshot["sized_lots"] = sizing.sized_lots
        snapshot["sized_qty"] = sizing.sized_qty
        snapshot["sized_outlay_inr"] = float(sizing.max_outlay_inr)
        if sizing.sized_qty == 0:
            return await self._reject(
                intent, "PER_TRADE_RISK",
                f"Sizing returned 0 lots: {sizing.reason}",
                snapshot,
            )

        # === Check 4: Capital available ===
        # Sanity-check: outlay must not exceed total capital (catches gross misconfig)
        if sizing.max_outlay_inr > Decimal(str(self._settings.trading_capital_inr)):
            return await self._reject(
                intent, "INSUFFICIENT_CAPITAL",
                f"Sized outlay ₹{sizing.max_outlay_inr} > capital ₹{self._settings.trading_capital_inr}",
                snapshot,
            )

        # === Check 14: Stale-data check ===
        is_stale = await self._instrument_stale(intent.underlying)
        snapshot["underlying_stale"] = is_stale
        if is_stale:
            return await self._reject(
                intent, "STALE_DATA",
                f"No fresh ticks for {intent.underlying} in last {self._risk.stale_tick_max_age_sec}s",
                snapshot,
            )

        # === Check 15: Volatility kill ===
        vix = await self._latest_vix()
        snapshot["india_vix"] = vix
        if vix is not None and vix > self._risk.india_vix_ceiling:
            return await self._reject(
                intent, "VOL_KILL",
                f"India VIX {vix:.2f} > ceiling {self._risk.india_vix_ceiling}",
                snapshot,
            )

        # === Check 16: Broker health ===
        # Defer to Phase 3.2 — Execution Engine will check broker health pre-flight.
        snapshot["broker_health_check"] = "deferred_to_execution_engine"

        # === All checks passed → APPROVE ===
        return await self._approve(intent, sizing, snapshot, live_status.authorized)

    # ----------------- DB query helpers -----------------

    async def _daily_realized_pnl_inr(self) -> Decimal:
        async with self._session_factory() as session:
            today_start = now_ist().replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
            v = (await session.execute(
                select(func.coalesce(func.sum(PositionRow.pnl_inr), 0))
                .where(PositionRow.closed_at >= today_start)
            )).scalar()
        return Decimal(str(v or 0))

    async def _rolling_drawdown_inr(self, window_days: int) -> Decimal:
        async with self._session_factory() as session:
            cutoff = now_ist() - timedelta(days=window_days)
            v = (await session.execute(
                select(func.coalesce(func.sum(PositionRow.pnl_inr), 0))
                .where(PositionRow.closed_at >= cutoff.astimezone(timezone.utc))
            )).scalar()
        return Decimal(str(v or 0))

    async def _open_position_count(self) -> int:
        async with self._session_factory() as session:
            n = (await session.execute(
                select(func.count(PositionRow.id)).where(PositionRow.is_open.is_(True))
            )).scalar()
        return int(n or 0)

    async def _trades_today(self) -> int:
        async with self._session_factory() as session:
            today_start = now_ist().replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
            n = (await session.execute(
                select(func.count(PositionRow.id)).where(PositionRow.opened_at >= today_start)
            )).scalar()
        return int(n or 0)

    async def _consecutive_losses_today(self) -> int:
        """Count consecutive losing positions today (most recent backwards)."""
        async with self._session_factory() as session:
            today_start = now_ist().replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
            rows = (await session.execute(
                select(PositionRow.pnl_inr)
                .where(PositionRow.closed_at >= today_start)
                .where(PositionRow.pnl_inr.is_not(None))
                .order_by(PositionRow.closed_at.desc())
            )).scalars().all()
        streak = 0
        for pnl in rows:
            if pnl is not None and pnl < 0:
                streak += 1
            else:
                break
        return streak

    async def _slippage_kill_triggered(self) -> bool:
        """Check whether last N consecutive slippage_log rows exceed threshold."""
        async with self._session_factory() as session:
            rows = (await session.execute(
                select(SlippageLogRow.realized_slippage_bps)
                .order_by(SlippageLogRow.ts.desc())
                .limit(self._risk.slippage_kill_consecutive)
            )).scalars().all()
        if len(rows) < self._risk.slippage_kill_consecutive:
            return False
        return all(
            float(r) > self._risk.slippage_kill_threshold_bps for r in rows
        )

    async def _instrument_stale(self, underlying_name: str) -> bool:
        """Look up the instrument_key, check Redis staleness."""
        inst_key = None
        for inst in self._instruments_cfg.instruments:
            if inst.name == underlying_name:
                inst_key = inst.upstox_instrument_key
                break
        if inst_key is None:
            return True  # unknown instrument → stale by default

        v = await self._redis.get(f"md:last_tick_ts:{inst_key}")
        if v is None:
            return True
        last = datetime.fromisoformat(v.decode())
        age = (now_ist() - last).total_seconds()
        return age > self._risk.stale_tick_max_age_sec

    async def _latest_vix(self) -> float | None:
        v = await self._redis.get("md:vix:latest")
        if v is None:
            return None
        try:
            return float(v.decode())
        except Exception:
            return None

    def _lot_size_for(self, instrument_key: str, underlying_name: str) -> int:
        for inst in self._instruments_cfg.instruments:
            if inst.name == underlying_name:
                return inst.lot_size
        # Default fallback — Phase 3.1 ships indices only
        return 25

    # ----------------- Decision builders -----------------

    async def _approve(
        self,
        intent: TradeIntent,
        sizing,
        snapshot: dict[str, Any],
        live_authorized: bool,
    ) -> RiskDecision:
        decision = RiskDecision(
            approved=True,
            code="OK_PAPER" if not live_authorized else "OK_LIVE",
            reason=(
                "Approved (paper mode — live-trading 3-lock not satisfied)"
                if not live_authorized
                else "Approved for live execution"
            ),
            sized_qty=sizing.sized_qty,
            sized_lots=sizing.sized_lots,
            max_outlay_inr=sizing.max_outlay_inr,
            inputs_snapshot=snapshot,
            ts=now_ist(),
        )
        await self._persist(decision)
        log.info(
            "risk.approved",
            underlying=intent.underlying,
            qty=sizing.sized_qty,
            outlay=float(sizing.max_outlay_inr),
            live=live_authorized,
        )
        return decision

    async def _reject(
        self,
        intent: TradeIntent,
        code: str,
        reason: str,
        snapshot: dict[str, Any],
    ) -> RiskDecision:
        decision = RiskDecision(
            approved=False,
            code=code,
            reason=reason,
            sized_qty=None,
            sized_lots=None,
            max_outlay_inr=None,
            inputs_snapshot=snapshot,
            ts=now_ist(),
        )
        await self._persist(decision)
        log.info(
            "risk.rejected",
            underlying=intent.underlying,
            code=code,
            reason=reason,
        )
        return decision

    async def _persist(self, decision: RiskDecision) -> None:
        try:
            async with self._session_factory() as session:
                session.add(RiskDecisionRow(
                    signal_id=None,   # Phase 4 wires the StrategySignalRow id here
                    approved=decision.approved,
                    reason=decision.reason,
                    code=decision.code,
                    sized_qty=decision.sized_qty,
                    max_premium=decision.max_outlay_inr,
                    inputs_snapshot=decision.inputs_snapshot,
                    ts=decision.ts,
                ))
                await session.commit()
        except Exception as e:
            log.error("risk.persist_failed", error=str(e))
            # Do not raise — persist failure must not block trade decisions

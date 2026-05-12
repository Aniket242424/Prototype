"""
Gap Continuation strategy — Phase 4 strategy #4.

When the underlying gaps significantly (today's open vs yesterday's close)
in one direction AND then continues in that direction after a brief
pullback, that's a tradeable setup. Gaps reflect overnight information flow
— news, global markets, earnings (for stocks). When they hold, they
typically continue.

The trap to avoid: many gaps FILL within the first hour. We don't trade
the gap itself — we trade the CONTINUATION after the gap has held through
a pullback near session_open or VWAP.

Filters:
1. Gap > 0.5% on the underlying (configurable)
2. Direction matches gap direction (long for gap up, short for gap down)
3. Regime must agree (TREND_UP/DOWN or VOL_EXPANSION)
4. Pullback completed — spot near session_open or VWAP, not chasing
5. Spot still on the gap side (gap hasn't filled)
6. ADX ≥ 20
7. Time window: 09:30-12:00 IST (best in first 90 min after open)

Stop: gap fill level — if the gap fills, our thesis is invalidated.
Target: 1:2 RR from current spot.
"""
from __future__ import annotations

from datetime import time
from decimal import Decimal

from pydantic import BaseModel, Field

from trading_agent.core.constants import Direction, OptionType, Regime
from trading_agent.core.logging import get_logger
from trading_agent.core.time_utils import to_ist
from trading_agent.strategy.base import (
    StrategyContext,
    StrategyReject,
    StrategySignal,
)

log = get_logger(__name__)

GAP_CONT_WINDOW_START = time(9, 30)
GAP_CONT_WINDOW_END = time(12, 0)


class GapContinuationConfig(BaseModel):
    """Tunable parameters."""

    min_gap_pct: float = Field(default=0.5, gt=0)            # 0.5% gap minimum
    max_distance_from_session_open_atr: float = Field(default=1.0, gt=0)
    min_adx14: float = Field(default=20.0, ge=0)
    target_rr: float = Field(default=2.0, gt=0)
    base_confidence: float = Field(default=0.65, ge=0, le=1)
    require_pullback: bool = True


class GapContinuationStrategy:
    """Gap Continuation — Strategy protocol implementation."""

    name = "gap_continuation"

    def __init__(self, cfg: GapContinuationConfig | None = None):
        self._cfg = cfg or GapContinuationConfig()

    def evaluate(self, ctx: StrategyContext) -> StrategySignal | None:
        try:
            self._check_time_window(ctx)
            self._check_gap_data(ctx)
            self._check_gap_size(ctx)
            self._check_direction_matches_gap(ctx)
            self._check_regime(ctx)
            self._check_adx(ctx)
            self._check_gap_not_filled(ctx)
            if self._cfg.require_pullback:
                self._check_pullback_to_session_open(ctx)
            return self._build_signal(ctx)
        except StrategyReject as e:
            log.debug(
                "gap_continuation.rejected",
                underlying=ctx.underlying,
                code=e.code,
                reason=e.reason,
            )
            return None

    def invalidation(
        self, signal: StrategySignal, ctx: StrategyContext
    ) -> str | None:
        """
        Premise invalidated if the gap fills. The gap fill level is what
        we used as the stop, but we also report invalidation distinctly
        so Position Manager logs it as "thesis broken" vs "stop hit by noise".
        """
        if ctx.session_open is None:
            return None
        spot = ctx.intel.spot if ctx.intel else None
        if spot is None:
            return None
        # For a gap-up, the gap "fill" is when spot drops back to session_open.
        # (Strictly the gap fills at yesterday's close, but session_open is
        #  the tighter, more actionable level — and where smart money tracks it.)
        if signal.direction == Direction.LONG and spot < ctx.session_open:
            return f"spot {spot} below session_open {ctx.session_open} — gap fill"
        if signal.direction == Direction.SHORT and spot > ctx.session_open:
            return f"spot {spot} above session_open {ctx.session_open} — gap fill"
        return None

    # ---------------- Filters ----------------

    def _check_time_window(self, ctx: StrategyContext) -> None:
        ist_now = to_ist(ctx.ts).time()
        if ist_now < GAP_CONT_WINDOW_START:
            raise StrategyReject("TOO_EARLY", f"current {ist_now} < {GAP_CONT_WINDOW_START}")
        if ist_now > GAP_CONT_WINDOW_END:
            raise StrategyReject("TOO_LATE", f"current {ist_now} > {GAP_CONT_WINDOW_END}")

    def _check_gap_data(self, ctx: StrategyContext) -> None:
        if ctx.gap_pct is None or ctx.session_open is None:
            raise StrategyReject(
                "GAP_DATA_MISSING",
                "gap_pct or session_open not in context (worker hasn't computed)",
            )

    def _check_gap_size(self, ctx: StrategyContext) -> None:
        if abs(ctx.gap_pct) < self._cfg.min_gap_pct:
            raise StrategyReject(
                "GAP_TOO_SMALL",
                f"|gap| {abs(ctx.gap_pct):.2f}% < min {self._cfg.min_gap_pct:.2f}%",
            )

    def _check_direction_matches_gap(self, ctx: StrategyContext) -> None:
        if ctx.direction == Direction.LONG and ctx.gap_pct <= 0:
            raise StrategyReject(
                "DIRECTION_MISMATCH",
                f"direction LONG but gap {ctx.gap_pct:+.2f}% is down",
            )
        if ctx.direction == Direction.SHORT and ctx.gap_pct >= 0:
            raise StrategyReject(
                "DIRECTION_MISMATCH",
                f"direction SHORT but gap {ctx.gap_pct:+.2f}% is up",
            )

    def _check_regime(self, ctx: StrategyContext) -> None:
        favorable = {
            Direction.LONG: {Regime.TREND_UP, Regime.VOL_EXPANSION, Regime.EVENT_DRIVEN},
            Direction.SHORT: {Regime.TREND_DOWN, Regime.VOL_EXPANSION, Regime.EVENT_DRIVEN},
        }
        if ctx.regime.regime not in favorable[ctx.direction]:
            raise StrategyReject(
                "REGIME",
                f"regime {ctx.regime.regime.value} not favorable for {ctx.direction.value}",
            )

    def _check_adx(self, ctx: StrategyContext) -> None:
        if ctx.indicators.adx14 is None:
            raise StrategyReject("ADX_UNAVAILABLE", "ADX not computed")
        if ctx.indicators.adx14 < self._cfg.min_adx14:
            raise StrategyReject(
                "ADX_TOO_LOW",
                f"ADX {ctx.indicators.adx14:.1f} < min {self._cfg.min_adx14}",
            )

    def _check_gap_not_filled(self, ctx: StrategyContext) -> None:
        """The gap must STILL be intact — spot still on the gap side of session_open."""
        spot = ctx.intel.spot if ctx.intel else None
        if spot is None:
            raise StrategyReject("SPOT_UNAVAILABLE", "spot not available")
        if ctx.direction == Direction.LONG and spot <= ctx.session_open:
            raise StrategyReject(
                "GAP_FILLED",
                f"spot {spot} ≤ session_open {ctx.session_open} — gap already filled",
            )
        if ctx.direction == Direction.SHORT and spot >= ctx.session_open:
            raise StrategyReject(
                "GAP_FILLED",
                f"spot {spot} ≥ session_open {ctx.session_open} — gap already filled",
            )

    def _check_pullback_to_session_open(self, ctx: StrategyContext) -> None:
        """
        Spot should be close to session_open — within max_distance_from_open_atr × ATR.
        Far from session_open = chasing.
        """
        spot = ctx.intel.spot if ctx.intel else None
        atr = ctx.indicators.atr14
        if spot is None or atr is None or atr <= 0:
            raise StrategyReject("PULLBACK_UNKNOWN", "spot/ATR not available for pullback check")
        distance = abs(spot - ctx.session_open)
        max_distance = self._cfg.max_distance_from_session_open_atr * atr
        if distance > max_distance:
            raise StrategyReject(
                "NO_PULLBACK",
                f"distance from session_open {distance:.2f} > {max_distance:.2f} (chasing)",
            )

    # ---------------- Signal construction ----------------

    def _build_signal(self, ctx: StrategyContext) -> StrategySignal:
        spot = ctx.intel.spot
        # Stop = gap fill level (session_open). If gap fills, exit.
        stop = ctx.session_open
        if ctx.direction == Direction.LONG:
            target = spot + self._cfg.target_rr * (spot - stop)
            option_type = OptionType.CE
        else:
            target = spot - self._cfg.target_rr * (stop - spot)
            option_type = OptionType.PE

        confidence = (
            self._cfg.base_confidence
            * ctx.regime.confidence
            * ctx.opportunity.score
        )
        confidence = max(0.0, min(1.0, confidence))

        rationale = {
            "trigger": "gap_continuation",
            "gap_pct": round(ctx.gap_pct, 2),
            "session_open": ctx.session_open,
            "spot_at_signal": spot,
            "distance_from_open": round(abs(spot - ctx.session_open), 2),
            "adx14": round(ctx.indicators.adx14, 2) if ctx.indicators.adx14 else None,
            "regime_confidence": ctx.regime.confidence,
            "rr": self._cfg.target_rr,
        }

        return StrategySignal(
            strategy_name=self.name,
            underlying=ctx.underlying,
            direction=ctx.direction,
            option_type=option_type,
            stop_underlying=Decimal(str(round(stop, 2))),
            target_underlying=Decimal(str(round(target, 2))),
            confidence=round(confidence, 3),
            rationale=rationale,
            ts=ctx.ts,
        )

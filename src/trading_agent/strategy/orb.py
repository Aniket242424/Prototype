"""
Opening Range Breakout (ORB) strategy — Phase 4 strategy #2.

The Indian market's first 15 minutes (09:15-09:30 IST) sets the day's
opening range. Disciplined breakouts ABOVE that range high (long) or
BELOW the range low (short) within the next 60 minutes have a documented
edge for option BUYERS.

Why this works for retail option buyers (when many things don't):
- TIME-BOUNDED setup: fires once per underlying per day, max
- The opening range itself IS the stop level — no arbitrary ATR multiplier
- Trading WITH confirmed momentum (the breakout already happened),
  not anticipating it (the EMA-crossover trap)
- Less crowded than EMA crossovers (which every YouTube channel teaches)
- Single-trade-per-day cap aligns naturally with our risk philosophy

Entry rules:
- Active window: 09:30-10:30 IST (we DON'T enter at 09:15 — spreads
  wide, signals unreliable until range is formed)
- Trigger: spot breaks above range_high (long) or below range_low (short)
- Filters:
    1. Range formed (09:30+ time check)
    2. Direction matches breakout side
    3. Range width > 0.3% of spot (too-narrow range = noise)
    4. ADX ≥ 20 (some trend strength)
    5. Volume confirmation (breakout candle volume > 1.2× avg)
    6. Spot > range_high (or < range_low) by at least 1 tick (not just touching)

Stop: opposite side of opening range.
Target: range_width × 1.5 to 2.0 from breakout level.

Position Manager will trail this with ATR chandelier (Phase 4.4).
"""
from __future__ import annotations

from datetime import datetime, time
from decimal import Decimal

from pydantic import BaseModel, Field

from trading_agent.core.constants import Direction, OptionType, Regime
from trading_agent.core.logging import get_logger
from trading_agent.core.time_utils import IST, to_ist
from trading_agent.strategy.base import (
    StrategyContext,
    StrategyReject,
    StrategySignal,
)

log = get_logger(__name__)

ORB_FORM_START = time(9, 15)
ORB_FORM_END = time(9, 30)
ORB_ENTRY_WINDOW_END = time(10, 30)


class ORBConfig(BaseModel):
    """Tunable parameters for ORB."""

    min_range_pct_of_spot: float = Field(default=0.003, gt=0)     # 0.3%
    min_adx14: float = Field(default=20.0, ge=0)
    min_volume_ratio: float = Field(default=1.2, gt=0)             # vs 20-period avg
    target_rr: float = Field(default=1.5, gt=0)
    base_confidence: float = Field(default=0.7, ge=0, le=1)


class ORBStrategy:
    """Opening Range Breakout — Strategy protocol implementation."""

    name = "orb"

    def __init__(self, cfg: ORBConfig | None = None):
        self._cfg = cfg or ORBConfig()

    def evaluate(self, ctx: StrategyContext) -> StrategySignal | None:
        try:
            self._check_time_window(ctx)
            self._check_range_data(ctx)
            self._check_range_width(ctx)
            self._check_regime(ctx)
            self._check_adx(ctx)
            spot = self._check_breakout_direction(ctx)
            self._check_volume(ctx)
            return self._build_signal(ctx, spot)
        except StrategyReject as e:
            log.debug(
                "orb.rejected",
                underlying=ctx.underlying,
                code=e.code,
                reason=e.reason,
            )
            return None

    def invalidation(
        self, signal: StrategySignal, ctx: StrategyContext
    ) -> str | None:
        """
        Premise invalidated if spot crosses back into the opening range.
        That means the breakout failed — exit.
        """
        if ctx.opening_range_high is None or ctx.opening_range_low is None:
            return None
        spot = ctx.intel.spot if ctx.intel else None
        if spot is None:
            return None
        if signal.direction == Direction.LONG and spot < ctx.opening_range_high:
            return f"spot {spot} re-entered opening range (high={ctx.opening_range_high})"
        if signal.direction == Direction.SHORT and spot > ctx.opening_range_low:
            return f"spot {spot} re-entered opening range (low={ctx.opening_range_low})"
        return None

    # ---------------- Filter chain ----------------

    def _check_time_window(self, ctx: StrategyContext) -> None:
        """ORB only fires 09:30-10:30 IST."""
        ist_now = to_ist(ctx.ts).time()
        if ist_now < ORB_FORM_END:
            raise StrategyReject(
                "ORB_TOO_EARLY",
                f"current time {ist_now} before ORB entry window opens ({ORB_FORM_END})",
            )
        if ist_now > ORB_ENTRY_WINDOW_END:
            raise StrategyReject(
                "ORB_TOO_LATE",
                f"current time {ist_now} after ORB entry window closes ({ORB_ENTRY_WINDOW_END})",
            )

    def _check_range_data(self, ctx: StrategyContext) -> None:
        if not ctx.opening_range_formed:
            raise StrategyReject(
                "RANGE_NOT_FORMED",
                "opening range not yet computed by Phase 4 worker",
            )
        if ctx.opening_range_high is None or ctx.opening_range_low is None:
            raise StrategyReject(
                "RANGE_DATA_MISSING",
                "opening_range_high / opening_range_low not in context",
            )
        if ctx.opening_range_high <= ctx.opening_range_low:
            raise StrategyReject(
                "RANGE_INVERTED",
                f"high {ctx.opening_range_high} <= low {ctx.opening_range_low}",
            )

    def _check_range_width(self, ctx: StrategyContext) -> None:
        """Range must be wide enough to be meaningful (not noise)."""
        spot = ctx.intel.spot if ctx.intel else None
        if spot is None or spot <= 0:
            raise StrategyReject("SPOT_UNKNOWN", "spot price not available")
        width = ctx.opening_range_high - ctx.opening_range_low
        width_pct = width / spot
        if width_pct < self._cfg.min_range_pct_of_spot:
            raise StrategyReject(
                "RANGE_TOO_NARROW",
                f"range {width:.2f} = {width_pct*100:.2f}% of spot, < min {self._cfg.min_range_pct_of_spot*100:.2f}%",
            )

    def _check_regime(self, ctx: StrategyContext) -> None:
        """ORB needs at least some directional bias. CHOPPY hard-rejects."""
        if ctx.regime.regime == Regime.CHOPPY:
            raise StrategyReject(
                "REGIME_CHOPPY", "regime is CHOPPY — ORB breakouts unreliable"
            )

    def _check_adx(self, ctx: StrategyContext) -> None:
        adx = ctx.indicators.adx14
        if adx is None:
            raise StrategyReject("ADX_UNKNOWN", "ADX not yet computed")
        if adx < self._cfg.min_adx14:
            raise StrategyReject(
                "ADX_TOO_LOW",
                f"ADX {adx:.1f} < min {self._cfg.min_adx14}",
            )

    def _check_breakout_direction(self, ctx: StrategyContext) -> float:
        """Spot must have ACTUALLY broken out, not just touched the level."""
        spot = ctx.intel.spot if ctx.intel else None
        if spot is None:
            raise StrategyReject("SPOT_UNKNOWN", "spot not available")
        # 1 tick buffer (₹0.05) to require real breakout
        if ctx.direction == Direction.LONG:
            if spot <= ctx.opening_range_high + 0.05:
                raise StrategyReject(
                    "NO_BREAKOUT",
                    f"spot {spot} not above range_high {ctx.opening_range_high}",
                )
        else:  # SHORT
            if spot >= ctx.opening_range_low - 0.05:
                raise StrategyReject(
                    "NO_BREAKOUT",
                    f"spot {spot} not below range_low {ctx.opening_range_low}",
                )
        return spot

    def _check_volume(self, ctx: StrategyContext) -> None:
        """Breakout needs volume confirmation."""
        if ctx.volume_ratio is None:
            # Volume data not always available — pass through if missing
            # (Phase 4 worker logs a warning; this is intentional lenience
            #  for index instruments where volume data is sparse).
            return
        if ctx.volume_ratio < self._cfg.min_volume_ratio:
            raise StrategyReject(
                "VOLUME_TOO_LOW",
                f"volume ratio {ctx.volume_ratio:.2f} < min {self._cfg.min_volume_ratio}",
            )

    # ---------------- Signal construction ----------------

    def _build_signal(self, ctx: StrategyContext, spot: float) -> StrategySignal:
        """All filters passed. Stop = opposite side of range. Target = RR × width."""
        width = ctx.opening_range_high - ctx.opening_range_low

        if ctx.direction == Direction.LONG:
            stop = ctx.opening_range_low
            target = ctx.opening_range_high + self._cfg.target_rr * width
            option_type = OptionType.CE
        else:
            stop = ctx.opening_range_high
            target = ctx.opening_range_low - self._cfg.target_rr * width
            option_type = OptionType.PE

        confidence = (
            self._cfg.base_confidence
            * ctx.regime.confidence
            * ctx.opportunity.score
        )
        confidence = max(0.0, min(1.0, confidence))

        rationale = {
            "trigger": "opening_range_breakout",
            "range_high": ctx.opening_range_high,
            "range_low": ctx.opening_range_low,
            "range_width_pts": round(width, 2),
            "spot_at_breakout": spot,
            "adx14": round(ctx.indicators.adx14, 2) if ctx.indicators.adx14 else None,
            "volume_ratio": round(ctx.volume_ratio, 2) if ctx.volume_ratio else None,
            "rr": self._cfg.target_rr,
            "regime_confidence": ctx.regime.confidence,
            "opportunity_score": ctx.opportunity.score,
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

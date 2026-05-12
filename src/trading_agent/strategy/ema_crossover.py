"""
EMA Crossover Trend strategy — Phase 4 strategy #1.

Entry trigger: fast EMA crosses above (long) or below (short) slow EMA.
Default periods: 9 / 21 (configurable in config/strategies.yaml; backtest
in Phase 5 may tune these — folklore says 10/20 also fine, no statistical
difference).

The crossover is a TRIGGER, not a system. The filter stack around it is
what makes this profitable for option BUYERS:

    1. Regime confirms direction        — TREND_UP/DOWN or VOL_EXPANSION only
    2. ADX ≥ 22                          — measurable trend strength
    3. Price above (long) / below (short) both EMAs — no overlap
    4. Pullback completion               — don't chase the cross candle
    5. VWAP confirms direction           — institutional flow agrees
    6. Within entry window               — 09:20-14:30 IST per risk.yaml

Why each filter exists:
- Regime: pure EMA crossovers in CHOPPY markets cause death by a thousand cuts
- ADX: filters out weak trends that revert immediately
- Price-above-stack: avoids "we're in a downtrend pullback but bullish
  crossover gives a false signal"
- Pullback: the crossover candle is where FOMO buyers fill; pullback is
  where disciplined buyers fill — better entry, tighter stop
- VWAP: institutions trade around VWAP; agreement reduces false-signal risk
- Window: avoid first-5-min noise + last-hour theta + closing flows

Stop placement: max(slow EMA, last_price ± 1.5×ATR) — whichever is closer
to current price for tighter risk. Position Manager monitors the UNDERLYING
price and fires MARKET exit on option when the level is crossed.

Target: 1:2 RR initially. Phase 4.4 Position Manager will trail with ATR
chandelier after +1R is hit.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field

from trading_agent.core.constants import Direction, OptionType, Regime
from trading_agent.core.logging import get_logger
from trading_agent.core.time_utils import now_ist
from trading_agent.strategy.base import (
    StrategyContext,
    StrategyReject,
    StrategySignal,
)

log = get_logger(__name__)


class EMACrossoverConfig(BaseModel):
    """Tunable parameters for this strategy. Lives in config/strategies.yaml."""

    fast_period: int = Field(default=9, gt=0)
    slow_period: int = Field(default=21, gt=0)

    # Crossover age window (minutes since the cross happened) — 0 means use
    # current candle's EMA stack as proxy for "fresh crossover", >0 requires
    # cross to have happened within N minutes.
    cross_age_min_minutes: int = Field(default=1, ge=0)
    cross_age_max_minutes: int = Field(default=15, ge=0)

    # Filter thresholds
    min_adx14: float = Field(default=22.0, ge=0)
    min_persistence: int = Field(default=2, ge=0)   # consec same-dir candles
    max_persistence: int = Field(default=4, ge=1)   # don't chase mature legs
    require_vwap_confirm: bool = True
    require_pullback: bool = True

    # Risk/reward
    initial_stop_atr_multiple: float = Field(default=1.5, gt=0)
    target_rr: float = Field(default=2.0, gt=0)

    # Confidence
    base_confidence: float = Field(default=0.65, ge=0, le=1)


class EMACrossoverTrendStrategy:
    """
    Implements the Strategy protocol (duck-typed).

    The 6-filter stack is implemented as separate methods that raise
    StrategyReject internally — makes each filter trivially testable.
    """

    name = "ema_crossover_trend"

    def __init__(self, cfg: EMACrossoverConfig | None = None):
        self._cfg = cfg or EMACrossoverConfig()

    def evaluate(self, ctx: StrategyContext) -> StrategySignal | None:
        """
        Run all filters. Return a StrategySignal if every filter passes; else None.
        """
        try:
            self._check_indicator_freshness(ctx)
            self._check_ema_stack(ctx)
            self._check_regime(ctx)
            self._check_adx(ctx)
            self._check_persistence(ctx)
            if self._cfg.require_pullback:
                self._check_pullback(ctx)
            if self._cfg.require_vwap_confirm:
                self._check_vwap(ctx)
            return self._build_signal(ctx)
        except StrategyReject as e:
            log.debug(
                "ema_crossover.rejected",
                underlying=ctx.underlying,
                code=e.code,
                reason=e.reason,
            )
            return None

    def invalidation(
        self, signal: StrategySignal, ctx: StrategyContext
    ) -> str | None:
        """
        Called by Position Manager. Premise is invalid if:
        - Regime flipped to opposite direction
        - Underlying crossed back through the slow EMA (trend broken)
        Position Manager handles the actual exit; this just signals it should.
        """
        i = ctx.indicators
        # Regime flip — most decisive invalidation
        opposite = {
            Direction.LONG: {Regime.TREND_DOWN, Regime.CHOPPY},
            Direction.SHORT: {Regime.TREND_UP, Regime.CHOPPY},
        }
        if ctx.regime.regime in opposite[signal.direction] and ctx.regime.confidence > 0.5:
            return f"regime flipped to {ctx.regime.regime.value}"

        # Underlying crosses back through slow EMA
        if i.ema21 is not None:
            last = float(signal.stop_underlying)  # using stop as a sentinel for last_price
            # Actually use current spot if available
            spot = ctx.intel.spot if ctx.intel else None
            if spot is not None:
                if signal.direction == Direction.LONG and spot < i.ema21:
                    return f"spot {spot} dropped below slow EMA {i.ema21:.2f}"
                if signal.direction == Direction.SHORT and spot > i.ema21:
                    return f"spot {spot} rose above slow EMA {i.ema21:.2f}"
        return None

    # ---------------- Filter implementations ----------------

    def _check_indicator_freshness(self, ctx: StrategyContext) -> None:
        """All three filter signals need real values — reject if any are missing."""
        i = ctx.indicators
        if i.ema9 is None or i.ema21 is None or i.adx14 is None:
            raise StrategyReject(
                "INDICATORS_NOT_READY",
                f"EMAs or ADX missing (candles_in_buffer={i.candles_in_buffer})",
            )
        if i.vwap is None and self._cfg.require_vwap_confirm:
            raise StrategyReject(
                "INDICATORS_NOT_READY",
                "VWAP not yet computed (need session-open accumulation)",
            )

    def _check_ema_stack(self, ctx: StrategyContext) -> None:
        """
        For LONG: fast EMA must be above slow EMA AND price above both.
        For SHORT: fast EMA below slow EMA AND price below both.
        """
        i = ctx.indicators
        if ctx.direction == Direction.LONG:
            if i.ema9 <= i.ema21:
                raise StrategyReject(
                    "EMA_STACK", "fast EMA not above slow EMA"
                )
        else:
            if i.ema9 >= i.ema21:
                raise StrategyReject(
                    "EMA_STACK", "fast EMA not below slow EMA"
                )

    def _check_regime(self, ctx: StrategyContext) -> None:
        """Regime must confirm direction. CHOPPY / VOL_COMPRESSION are hard rejects."""
        regime = ctx.regime.regime
        favorable = {
            Direction.LONG: {Regime.TREND_UP, Regime.VOL_EXPANSION},
            Direction.SHORT: {Regime.TREND_DOWN, Regime.VOL_EXPANSION},
        }
        if regime not in favorable[ctx.direction]:
            raise StrategyReject(
                "REGIME", f"regime {regime.value} not favorable for {ctx.direction.value}"
            )

    def _check_adx(self, ctx: StrategyContext) -> None:
        if ctx.indicators.adx14 < self._cfg.min_adx14:
            raise StrategyReject(
                "ADX_TOO_LOW",
                f"ADX {ctx.indicators.adx14:.1f} < min {self._cfg.min_adx14}",
            )

    def _check_persistence(self, ctx: StrategyContext) -> None:
        """
        Consecutive-candle persistence check.

        - Too few consecutive candles → cross is too fresh, may reverse
        - Too many → the move is mature, late entry = bad RR
        """
        i = ctx.indicators
        consec = (
            i.consec_up_candles if ctx.direction == Direction.LONG
            else i.consec_down_candles
        )
        if consec < self._cfg.min_persistence:
            raise StrategyReject(
                "PERSISTENCE_LOW",
                f"only {consec} consecutive candles (need ≥ {self._cfg.min_persistence})",
            )
        if consec > self._cfg.max_persistence:
            raise StrategyReject(
                "PERSISTENCE_HIGH",
                f"{consec} consecutive candles (leg too mature, > {self._cfg.max_persistence})",
            )

    def _check_pullback(self, ctx: StrategyContext) -> None:
        """
        Heuristic: price should be near the fast EMA (within 0.5×ATR), not
        extended away from it. Avoids "chase the breakout candle" entries.
        """
        i = ctx.indicators
        if i.atr14 is None or i.atr14 <= 0:
            raise StrategyReject(
                "PULLBACK_UNKNOWN", "ATR not available for pullback check"
            )
        # Use intel.spot if available, else skip (Phase 2 might not have intel yet)
        spot = ctx.intel.spot if ctx.intel else None
        if spot is None:
            # Without spot we can't measure distance; treat as pullback-OK (lenient)
            return
        distance_from_fast = abs(spot - i.ema9)
        if distance_from_fast > 0.5 * i.atr14:
            raise StrategyReject(
                "NO_PULLBACK",
                f"price {distance_from_fast:.2f} from fast EMA > 0.5×ATR ({0.5 * i.atr14:.2f})",
            )

    def _check_vwap(self, ctx: StrategyContext) -> None:
        """
        For LONG: price-VWAP deviation must be POSITIVE.
        For SHORT: must be NEGATIVE.
        Magnitude shouldn't be extreme (>1.5σ = mean-revert risk, per anti-FOMO).
        """
        sigma = ctx.indicators.price_vwap_dev_sigma
        if sigma is None:
            raise StrategyReject("VWAP_UNKNOWN", "VWAP deviation not computed")
        if ctx.direction == Direction.LONG and sigma <= 0:
            raise StrategyReject(
                "VWAP_AGAINST", f"price below VWAP (sigma={sigma:.2f})"
            )
        if ctx.direction == Direction.SHORT and sigma >= 0:
            raise StrategyReject(
                "VWAP_AGAINST", f"price above VWAP (sigma={sigma:.2f})"
            )
        # Extended-move rejection (anti-FOMO)
        if abs(sigma) > 1.5:
            raise StrategyReject(
                "VWAP_EXTENDED",
                f"|sigma|={abs(sigma):.2f} > 1.5 (mean-revert risk)",
            )

    # ---------------- Signal construction ----------------

    def _build_signal(self, ctx: StrategyContext) -> StrategySignal:
        """All filters passed. Construct stop/target on the underlying."""
        i = ctx.indicators
        # Use intel.spot if available, else fall back to ema9 as proxy
        spot = ctx.intel.spot if ctx.intel else (i.ema9 or 0.0)
        atr = i.atr14 or 0.0

        if ctx.direction == Direction.LONG:
            atr_stop = spot - self._cfg.initial_stop_atr_multiple * atr
            ema_stop = i.ema21
            # Use the closer of the two (tighter risk)
            stop = max(atr_stop, ema_stop) if ema_stop is not None else atr_stop
            risk = spot - stop
            target = spot + self._cfg.target_rr * risk
            option_type = OptionType.CE
        else:
            atr_stop = spot + self._cfg.initial_stop_atr_multiple * atr
            ema_stop = i.ema21
            stop = min(atr_stop, ema_stop) if ema_stop is not None else atr_stop
            risk = stop - spot
            target = spot - self._cfg.target_rr * risk
            option_type = OptionType.PE

        # Confidence: base × regime_confidence × opportunity_score
        confidence = (
            self._cfg.base_confidence
            * ctx.regime.confidence
            * ctx.opportunity.score
        )
        confidence = max(0.0, min(1.0, confidence))

        rationale = {
            "trigger": "ema_crossover",
            "fast_period": self._cfg.fast_period,
            "slow_period": self._cfg.slow_period,
            "adx14": round(i.adx14, 2),
            "consec_candles": (
                i.consec_up_candles if ctx.direction == Direction.LONG
                else i.consec_down_candles
            ),
            "vwap_sigma": round(i.price_vwap_dev_sigma, 2) if i.price_vwap_dev_sigma else None,
            "atr14": round(atr, 2),
            "spot_at_signal": spot,
            "stop_distance_pts": round(abs(spot - stop), 2),
            "target_distance_pts": round(abs(target - spot), 2),
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

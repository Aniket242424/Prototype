"""
Volatility Expansion strategy — Phase 4 strategy #3.

Fires when realized volatility has SHARPLY expanded vs the recent baseline
(regime engine signals VOL_EXPANSION when rv5/rv60 > 1.6). These moves
tend to overshoot — buying option premium during them captures the
acceleration, but also gets hurt fast if direction is wrong.

Key differences vs EMA Crossover Trend:
- Tighter stop (1.0×ATR vs 1.5×ATR) — vol can revert as fast as it expanded
- Higher target RR (2.5 vs 2.0) — vol moves overshoot
- Hard rejects on high IV percentile (>60) — would pay extra for premium
  that's about to get crushed when vol normalizes
- No pullback requirement — we WANT the breakout candle entry here

Risk vs reward: lower win rate (~35-40%) but much bigger winners.

Filters:
1. Regime is VOL_EXPANSION (or EVENT_DRIVEN with confirmed direction)
2. rv5/rv60 ratio is genuinely elevated (verified from indicators, not just trust regime)
3. +DI vs -DI alignment with direction
4. IV percentile ≤ 60% (avoid IV-crush trap)
5. ADX ≥ 18 (lower threshold than EMA crossover — vol moves can have weak ADX)
6. Time-of-day: 09:30-14:00 (vol expansion in last 90 min is often closing-flow noise)
7. VWAP confirmation — direction aligns
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

VOL_EXP_WINDOW_START = time(9, 30)
VOL_EXP_WINDOW_END = time(14, 0)


class VolExpansionConfig(BaseModel):
    """Tunable parameters."""

    min_rv_ratio: float = Field(default=1.6, gt=0)
    min_adx14: float = Field(default=18.0, ge=0)
    max_iv_percentile: float = Field(default=0.60, gt=0, le=1)
    initial_stop_atr_multiple: float = Field(default=1.0, gt=0)
    target_rr: float = Field(default=2.5, gt=0)
    base_confidence: float = Field(default=0.6, ge=0, le=1)
    require_vwap_confirm: bool = True


class VolExpansionStrategy:
    """Volatility Expansion — Strategy protocol implementation."""

    name = "vol_expansion"

    def __init__(self, cfg: VolExpansionConfig | None = None):
        self._cfg = cfg or VolExpansionConfig()

    def evaluate(self, ctx: StrategyContext) -> StrategySignal | None:
        try:
            self._check_time_window(ctx)
            self._check_regime(ctx)
            self._check_rv_ratio(ctx)
            self._check_di_alignment(ctx)
            self._check_adx(ctx)
            self._check_iv(ctx)
            if self._cfg.require_vwap_confirm:
                self._check_vwap(ctx)
            return self._build_signal(ctx)
        except StrategyReject as e:
            log.debug(
                "vol_expansion.rejected",
                underlying=ctx.underlying,
                code=e.code,
                reason=e.reason,
            )
            return None

    def invalidation(
        self, signal: StrategySignal, ctx: StrategyContext
    ) -> str | None:
        """
        Premise invalidated if regime returns to non-expansion. Vol-expansion
        trades are short-duration — when vol normalizes, our edge evaporates.
        """
        if ctx.regime.regime not in (Regime.VOL_EXPANSION, Regime.EVENT_DRIVEN,
                                       Regime.TREND_UP, Regime.TREND_DOWN):
            return f"regime {ctx.regime.regime.value} — vol expansion ended"
        return None

    # ---------------- Filters ----------------

    def _check_time_window(self, ctx: StrategyContext) -> None:
        ist_now = to_ist(ctx.ts).time()
        if ist_now < VOL_EXP_WINDOW_START:
            raise StrategyReject(
                "TOO_EARLY", f"current {ist_now} < window start {VOL_EXP_WINDOW_START}"
            )
        if ist_now > VOL_EXP_WINDOW_END:
            raise StrategyReject(
                "TOO_LATE", f"current {ist_now} > window end {VOL_EXP_WINDOW_END}"
            )

    def _check_regime(self, ctx: StrategyContext) -> None:
        if ctx.regime.regime not in (Regime.VOL_EXPANSION, Regime.EVENT_DRIVEN):
            raise StrategyReject(
                "REGIME_NOT_VOL_EXP",
                f"regime {ctx.regime.regime.value} not VOL_EXPANSION/EVENT_DRIVEN",
            )

    def _check_rv_ratio(self, ctx: StrategyContext) -> None:
        i = ctx.indicators
        if i.rv5 is None or i.rv60 is None or i.rv60 <= 0:
            raise StrategyReject(
                "RV_UNAVAILABLE", "realized vol short or long window not computed"
            )
        ratio = i.rv5 / i.rv60
        if ratio < self._cfg.min_rv_ratio:
            raise StrategyReject(
                "RV_TOO_LOW",
                f"rv5/rv60 = {ratio:.2f} < min {self._cfg.min_rv_ratio}",
            )

    def _check_di_alignment(self, ctx: StrategyContext) -> None:
        i = ctx.indicators
        if i.plus_di is None or i.minus_di is None:
            raise StrategyReject("DI_UNAVAILABLE", "+DI/-DI not computed")
        if ctx.direction == Direction.LONG and i.plus_di <= i.minus_di:
            raise StrategyReject(
                "DI_DOWN", f"+DI {i.plus_di:.1f} ≤ -DI {i.minus_di:.1f}, against LONG"
            )
        if ctx.direction == Direction.SHORT and i.minus_di <= i.plus_di:
            raise StrategyReject(
                "DI_UP", f"-DI {i.minus_di:.1f} ≤ +DI {i.plus_di:.1f}, against SHORT"
            )

    def _check_adx(self, ctx: StrategyContext) -> None:
        if ctx.indicators.adx14 is None:
            raise StrategyReject("ADX_UNAVAILABLE", "ADX not computed")
        if ctx.indicators.adx14 < self._cfg.min_adx14:
            raise StrategyReject(
                "ADX_TOO_LOW",
                f"ADX {ctx.indicators.adx14:.1f} < min {self._cfg.min_adx14}",
            )

    def _check_iv(self, ctx: StrategyContext) -> None:
        """High IV percentile = bloated premium = IV-crush risk. Reject."""
        if ctx.intel is None or ctx.intel.iv_percentile_30d is None:
            # Without IV history, can't filter; pass through (lenient)
            return
        if ctx.intel.iv_percentile_30d > self._cfg.max_iv_percentile:
            raise StrategyReject(
                "IV_TOO_HIGH",
                f"IV percentile {ctx.intel.iv_percentile_30d:.2f} > max {self._cfg.max_iv_percentile}",
            )

    def _check_vwap(self, ctx: StrategyContext) -> None:
        """Direction must agree with VWAP sigma. No extension cap (vol-exp by design is extended)."""
        sigma = ctx.indicators.price_vwap_dev_sigma
        if sigma is None:
            raise StrategyReject("VWAP_UNAVAILABLE", "VWAP not computed")
        if ctx.direction == Direction.LONG and sigma <= 0:
            raise StrategyReject("VWAP_AGAINST", f"VWAP sigma {sigma:.2f} ≤ 0 for LONG")
        if ctx.direction == Direction.SHORT and sigma >= 0:
            raise StrategyReject("VWAP_AGAINST", f"VWAP sigma {sigma:.2f} ≥ 0 for SHORT")

    # ---------------- Signal construction ----------------

    def _build_signal(self, ctx: StrategyContext) -> StrategySignal:
        i = ctx.indicators
        spot = ctx.intel.spot if ctx.intel else (i.ema9 or 0.0)
        atr = i.atr14 or 0.0

        if ctx.direction == Direction.LONG:
            stop = spot - self._cfg.initial_stop_atr_multiple * atr
            target = spot + self._cfg.target_rr * (spot - stop)
            option_type = OptionType.CE
        else:
            stop = spot + self._cfg.initial_stop_atr_multiple * atr
            target = spot - self._cfg.target_rr * (stop - spot)
            option_type = OptionType.PE

        confidence = (
            self._cfg.base_confidence
            * ctx.regime.confidence
            * ctx.opportunity.score
        )
        confidence = max(0.0, min(1.0, confidence))

        rv_ratio = (i.rv5 / i.rv60) if (i.rv5 and i.rv60 and i.rv60 > 0) else None

        rationale = {
            "trigger": "vol_expansion",
            "rv5": round(i.rv5, 2) if i.rv5 else None,
            "rv60": round(i.rv60, 2) if i.rv60 else None,
            "rv_ratio": round(rv_ratio, 2) if rv_ratio else None,
            "adx14": round(i.adx14, 2) if i.adx14 else None,
            "plus_di": round(i.plus_di, 2) if i.plus_di else None,
            "minus_di": round(i.minus_di, 2) if i.minus_di else None,
            "iv_percentile_30d": ctx.intel.iv_percentile_30d if ctx.intel else None,
            "spot_at_signal": spot,
            "rr": self._cfg.target_rr,
            "regime_confidence": ctx.regime.confidence,
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

"""
Opportunity Ranking Engine.

Combines Regime + Options Intel + Indicators into a 9-dimensional score.
At most ONE underlying is "active" at a time — the highest-scoring above
threshold. The rest are ranked but not emitted as actionable opportunities.

This is the "what to trade if anything" brain. Phase 4 Strategy Engine
turns an Opportunity into a concrete TradeIntent (which strike, target, stop).

Score dimensions (each 0..1, weighted):
  1. Momentum quality            — price-VWAP deviation × ADX
  2. Vol expansion probability   — rv5/rv60 ratio
  3. Liquidity quality           — ATM OI + spread tightness
  4. Spread tightness            — ATM bid/ask spread bps
  5. Slippage risk (1=best)      — combination of spread + depth
  6. IV conditions               — favors lower IV percentile for buying
  7. Regime favorability         — TREND_UP/DOWN best, CHOPPY/COMPRESSION = 0
  8. Trend quality               — ADX × directional persistence
  9. Risk-reward profile         — estimated R:R given strike + stop
"""
from __future__ import annotations

import math
from datetime import datetime
from decimal import Decimal

import orjson
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import async_sessionmaker

from trading_agent.core.config import get_risk_config
from trading_agent.core.constants import (
    CHAN_OPPORTUNITY,
    SUPPRESSED_REGIMES,
    Direction,
    Regime,
)
from trading_agent.core.logging import get_logger
from trading_agent.core.time_utils import now_ist
from trading_agent.infrastructure.models import OpportunityRow
from trading_agent.regime.dtos import (
    IndicatorSnapshot,
    Opportunity,
    OpportunityScore,
    OptionsIntel,
    RegimeState,
)

log = get_logger(__name__)

# Per-dimension weights — sum to 1.0. First-cut. Phase 5 will re-tune.
WEIGHTS = {
    "momentum_quality":   0.18,
    "vol_expansion_prob": 0.10,
    "liquidity_quality":  0.10,
    "spread_tightness":   0.08,
    "slippage_risk":      0.10,
    "iv_conditions":      0.10,
    "regime_favorability": 0.16,
    "trend_quality":      0.12,
    "risk_reward_profile": 0.06,
}

EMIT_THRESHOLD = 0.65


def _clamp01(x: float) -> float:
    if x is None or math.isnan(x) or math.isinf(x):
        return 0.0
    return max(0.0, min(1.0, x))


def _direction_from_inputs(
    indicators: IndicatorSnapshot, regime: RegimeState
) -> Direction:
    """LONG if regime/indicators say up, SHORT (i.e., long-PUT) if down."""
    if regime.regime == Regime.TREND_UP:
        return Direction.LONG
    if regime.regime == Regime.TREND_DOWN:
        return Direction.SHORT
    # Vol expansion / event-driven — let indicator stack decide direction
    plus = indicators.plus_di or 0
    minus = indicators.minus_di or 0
    if plus >= minus:
        return Direction.LONG
    return Direction.SHORT


def score_opportunity(
    indicators: IndicatorSnapshot,
    regime: RegimeState,
    intel: OptionsIntel | None,
) -> tuple[OpportunityScore, Direction]:
    """Compute the 9-dim score for one underlying. No I/O."""
    direction = _direction_from_inputs(indicators, regime)

    # 1. Momentum quality
    pv_sigma = indicators.price_vwap_dev_sigma or 0.0
    if direction == Direction.LONG:
        momentum_q = _clamp01((pv_sigma - 0.0) / 2.0)  # 0σ → 0.0, 2σ → 1.0
    else:
        momentum_q = _clamp01((-pv_sigma - 0.0) / 2.0)

    # 2. Vol expansion probability
    if indicators.rv5 and indicators.rv60 and indicators.rv60 > 0:
        ratio = indicators.rv5 / indicators.rv60
        # Best around 1.6 (clear expansion); below 1.0 → 0
        vol_expansion_p = _clamp01((ratio - 1.0) / 1.0)
    else:
        vol_expansion_p = 0.0

    # 3. Liquidity quality (from intel: spread bps + OI)
    if intel:
        ce_spread = intel.atm_call_spread_bps or 999
        pe_spread = intel.atm_put_spread_bps or 999
        spread_use = ce_spread if direction == Direction.LONG else pe_spread
        # 5bps→1.0, 50bps→0.0
        spread_score = _clamp01(1.0 - (spread_use - 5) / 45)
        # OI score: rough — total side OI; calibrated against ₹3L capital trades
        side_oi = intel.total_call_oi if direction == Direction.LONG else intel.total_put_oi
        oi_score = _clamp01(side_oi / 5_000_000)  # 5M side OI → 1.0
        liquidity_q = (spread_score + oi_score) / 2
    else:
        spread_score = 0.0
        liquidity_q = 0.0

    # 4. Spread tightness (already captured but expose separately)
    spread_tight = spread_score if intel else 0.0

    # 5. Slippage risk (1 - estimated slippage as fraction)
    # Simple proxy: spread/2 is the round-trip cost. Below 10bps → 1.0; 50bps → 0.
    if intel:
        sp = intel.atm_call_spread_bps if direction == Direction.LONG else intel.atm_put_spread_bps
        if sp is not None:
            slip_risk = _clamp01(1.0 - (sp - 10) / 40)
        else:
            slip_risk = 0.0
    else:
        slip_risk = 0.0

    # 6. IV conditions (favor lower IV for option buying)
    if intel and intel.iv_percentile_30d is not None:
        # Best at IV percentile <= 0.3, worst at >= 0.7
        iv_score = _clamp01(1.0 - (intel.iv_percentile_30d - 0.3) / 0.4)
    else:
        iv_score = 0.5  # neutral when no history

    # 7. Regime favorability
    if regime.regime in (Regime.TREND_UP, Regime.TREND_DOWN):
        regime_fav = regime.confidence
    elif regime.regime == Regime.VOL_EXPANSION:
        regime_fav = 0.7 * regime.confidence
    elif regime.regime == Regime.EVENT_DRIVEN:
        regime_fav = 0.5 * regime.confidence
    elif regime.regime == Regime.RANGE:
        regime_fav = 0.2 * regime.confidence
    elif regime.regime in SUPPRESSED_REGIMES:
        regime_fav = 0.0  # hard suppression
    else:
        regime_fav = 0.0

    # 8. Trend quality
    adx = indicators.adx14 or 0
    persistence = (indicators.consec_up_candles
                   if direction == Direction.LONG
                   else indicators.consec_down_candles) or 0
    trend_q = _clamp01((adx - 22) / 30) * _clamp01(persistence / 4)

    # 9. Risk-reward profile
    # Rough proxy: ATR as % of spot. Higher ATR → larger expected profit window.
    if indicators.atr_pct:
        rr = _clamp01((indicators.atr_pct - 0.2) / 0.6)
    else:
        rr = 0.0

    return OpportunityScore(
        momentum_quality=round(momentum_q, 3),
        vol_expansion_prob=round(vol_expansion_p, 3),
        liquidity_quality=round(liquidity_q, 3),
        spread_tightness=round(spread_tight, 3),
        slippage_risk=round(slip_risk, 3),
        iv_conditions=round(iv_score, 3),
        regime_favorability=round(regime_fav, 3),
        trend_quality=round(trend_q, 3),
        risk_reward_profile=round(rr, 3),
    ), direction


def composite_score(s: OpportunityScore) -> float:
    """Weighted sum of the 9 dimensions."""
    total = (
        s.momentum_quality      * WEIGHTS["momentum_quality"] +
        s.vol_expansion_prob    * WEIGHTS["vol_expansion_prob"] +
        s.liquidity_quality     * WEIGHTS["liquidity_quality"] +
        s.spread_tightness      * WEIGHTS["spread_tightness"] +
        s.slippage_risk         * WEIGHTS["slippage_risk"] +
        s.iv_conditions         * WEIGHTS["iv_conditions"] +
        s.regime_favorability   * WEIGHTS["regime_favorability"] +
        s.trend_quality         * WEIGHTS["trend_quality"] +
        s.risk_reward_profile   * WEIGHTS["risk_reward_profile"]
    )
    return round(min(1.0, max(0.0, total)), 4)


class OpportunityEngine:
    def __init__(self, session_factory: async_sessionmaker, redis: Redis):
        self._session_factory = session_factory
        self._redis = redis
        self._latest_top: Opportunity | None = None
        self._risk_cfg = get_risk_config()

    @property
    def latest(self) -> Opportunity | None:
        return self._latest_top

    async def evaluate_all(
        self,
        indicators: dict[str, IndicatorSnapshot],
        regimes: dict[str, RegimeState],
        intels: dict[str, OptionsIntel],
    ) -> Opportunity | None:
        """
        Score every underlying that has both regime + indicators. Return the
        single top scorer above threshold; persist + publish only it.

        All others are scored but not emitted (visible via ranking_log if added later).
        """
        candidates: list[tuple[float, Opportunity]] = []
        for u, regime in regimes.items():
            ind = indicators.get(u)
            if ind is None:
                continue
            intel = intels.get(u)
            scores, direction = score_opportunity(ind, regime, intel)
            comp = composite_score(scores)

            # Build candidate even if below threshold so we can rank/log
            opportunity = Opportunity(
                underlying=u,
                direction=direction,
                score=comp,
                components=scores,
                recommended_expiry=intel.expiry if intel else regime.ts.date(),
                recommended_strike_band={
                    "low": Decimal(str(intel.atm_strike - 100)) if intel else Decimal("0"),
                    "high": Decimal(str(intel.atm_strike + 100)) if intel else Decimal("0"),
                } if intel else {"low": Decimal("0"), "high": Decimal("0")},
                ts=now_ist(),
            )
            candidates.append((comp, opportunity))

        if not candidates:
            return None

        candidates.sort(key=lambda x: x[0], reverse=True)
        top_score, top_opp = candidates[0]

        # Cache full ranking for dashboard
        ranking = [
            {
                "underlying": o.underlying,
                "direction": o.direction.value,
                "score": s,
            }
            for s, o in candidates
        ]
        await self._redis.set("opportunity:ranking", orjson.dumps(ranking), ex=120)

        if top_score < EMIT_THRESHOLD:
            self._latest_top = None
            await self._redis.delete("opportunity:active")
            return None

        await self._persist_and_publish(top_opp)
        self._latest_top = top_opp
        return top_opp

    async def _persist_and_publish(self, opp: Opportunity) -> None:
        try:
            async with self._session_factory() as session:
                session.add(OpportunityRow(
                    underlying=opp.underlying,
                    direction=opp.direction.value,
                    score=Decimal(str(opp.score)),
                    components=opp.components.model_dump(),
                    recommended_expiry=datetime.combine(
                        opp.recommended_expiry,
                        datetime.min.time(),
                    ),
                    recommended_strike_band={
                        k: str(v) for k, v in opp.recommended_strike_band.items()
                    },
                    ts=opp.ts,
                ))
                await session.commit()
        except Exception as e:
            log.warning("opportunity.persist_failed", error=str(e))

        payload = orjson.dumps({
            "underlying": opp.underlying,
            "direction": opp.direction.value,
            "score": opp.score,
            "components": opp.components.model_dump(),
            "ts": opp.ts.isoformat(),
            "recommended_expiry": opp.recommended_expiry.isoformat(),
            "recommended_strike_band": {
                k: str(v) for k, v in opp.recommended_strike_band.items()
            },
        })
        await self._redis.publish(CHAN_OPPORTUNITY, payload)
        await self._redis.set("opportunity:active", payload, ex=120)

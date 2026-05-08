"""
Regime classifier — deterministic, heuristic-driven first cut.

Classifies each underlying into one of:
  TREND_UP / TREND_DOWN / RANGE / CHOPPY / VOL_EXPANSION / VOL_COMPRESSION / EVENT_DRIVEN

These thresholds are first-cut educated guesses. Phase 5 backtest will
calibrate them against historical regime-strategy outcomes; until then they
are honest defaults that bias toward "don't trade" when ambiguous.

Inputs (all derived from tick buffer):
- ATR(14) on 1-min candles → volatility level
- ADX(14), +DI, -DI → trend strength + direction
- RV(5min, 15min, 60min) ratio → vol expansion vs compression
- VWAP deviation in σ → trend persistence vs mean reversion
- Consecutive same-direction candles → momentum persistence

The classifier returns the highest-confidence label among candidates.
Confidence reflects how cleanly the inputs match the rule, NOT a calibrated
probability. Use confidence × score gating downstream.
"""
from __future__ import annotations

import math
from datetime import datetime
from decimal import Decimal

import orjson
import pandas as pd
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import async_sessionmaker

from trading_agent.core.constants import CHAN_REGIME, Regime
from trading_agent.core.logging import get_logger
from trading_agent.core.time_utils import IST, now_ist
from trading_agent.infrastructure.models import RegimeStateRow
from trading_agent.regime.dtos import IndicatorSnapshot, RegimeState
from trading_agent.regime.indicators import (
    adx,
    atr,
    candles_from_ticks,
    consecutive_direction,
    ema,
    price_vwap_deviation_sigma,
    realized_vol_annualized,
    vwap_session,
)
from trading_agent.regime.tick_buffer import TickBuffer

log = get_logger(__name__)


def compute_indicators(underlying_name: str, ticks_df: pd.DataFrame) -> IndicatorSnapshot:
    """Compute the full indicator snapshot from a tick frame. Pure function."""
    ts_now = now_ist()
    if ticks_df.empty:
        return IndicatorSnapshot(underlying=underlying_name, ts=ts_now)

    candles = candles_from_ticks(ticks_df, freq="1min")
    if len(candles) < 5:
        return IndicatorSnapshot(
            underlying=underlying_name, ts=ts_now, candles_in_buffer=len(candles)
        )

    candles = candles.sort_values("ts").reset_index(drop=True)

    # EMAs
    closes = candles["close"]
    e9 = ema(closes, 9).iloc[-1] if len(candles) >= 9 else None
    e21 = ema(closes, 21).iloc[-1] if len(candles) >= 21 else None
    e50 = ema(closes, 50).iloc[-1] if len(candles) >= 50 else None

    # VWAP from session open (09:15 IST today)
    today_open = ts_now.replace(hour=9, minute=15, second=0, microsecond=0).astimezone(
        candles["ts"].iloc[0].tzinfo
    )
    candles["vwap"] = vwap_session(candles, today_open)
    vwap_latest = candles["vwap"].iloc[-1] if "vwap" in candles else None
    if vwap_latest is not None and (math.isnan(vwap_latest) or math.isinf(vwap_latest)):
        vwap_latest = None
    pv_sigma = price_vwap_deviation_sigma(candles)

    # ATR
    atr14 = atr(candles, 14).iloc[-1] if len(candles) >= 15 else None
    spot = float(closes.iloc[-1])
    atr_pct = (atr14 / spot * 100) if (atr14 and spot) else None

    # ADX
    adx_dict = adx(candles, 14)
    adx14 = adx_dict["adx"].iloc[-1] if len(candles) >= 28 else None
    plus_di = adx_dict["plus_di"].iloc[-1] if len(candles) >= 28 else None
    minus_di = adx_dict["minus_di"].iloc[-1] if len(candles) >= 28 else None

    # Realized vol
    rv5 = realized_vol_annualized(candles, 5)
    rv15 = realized_vol_annualized(candles, 15)
    rv60 = realized_vol_annualized(candles, 60)

    up_streak, down_streak = consecutive_direction(candles)

    def _f(x):
        if x is None:
            return None
        try:
            x = float(x)
        except Exception:
            return None
        if math.isnan(x) or math.isinf(x):
            return None
        return x

    return IndicatorSnapshot(
        underlying=underlying_name,
        ts=ts_now,
        ema9=_f(e9),
        ema21=_f(e21),
        ema50=_f(e50),
        vwap=_f(vwap_latest),
        price_vwap_dev_sigma=_f(pv_sigma),
        atr14=_f(atr14),
        atr_pct=_f(atr_pct),
        rv5=_f(rv5),
        rv15=_f(rv15),
        rv60=_f(rv60),
        adx14=_f(adx14),
        plus_di=_f(plus_di),
        minus_di=_f(minus_di),
        consec_up_candles=up_streak,
        consec_down_candles=down_streak,
        candles_in_buffer=len(candles),
    )


# ------------------ Classification rules ------------------

ADX_TREND_THRESHOLD = 22.0    # ADX above this = directional regime
ADX_CHOPPY_CEILING = 15.0     # ADX below this in low-vol = CHOPPY

VIX_PANIC_LEVEL = 22.0        # India VIX above → EVENT_DRIVEN takes over
VIX_CALM_LEVEL = 14.0

RV_EXPANSION_RATIO = 1.6      # rv5 / rv60 above → vol expansion
RV_COMPRESSION_RATIO = 0.6    # rv5 / rv60 below → vol compression


def classify_regime(
    indicators: IndicatorSnapshot,
    vix_value: float | None,
) -> RegimeState:
    """
    Apply rules in priority order. First match wins.

    Priority:
      1. EVENT_DRIVEN (VIX panic OR major intraday move) — overrides everything
      2. VOL_EXPANSION / VOL_COMPRESSION (vol ratio extremes)
      3. TREND_UP / TREND_DOWN (ADX > threshold + DI confirmation)
      4. RANGE (low ADX, sufficient candles)
      5. CHOPPY (insufficient information OR very low ADX)
    """
    components: dict[str, float | str | None] = {
        "atr14": indicators.atr14,
        "adx14": indicators.adx14,
        "plus_di": indicators.plus_di,
        "minus_di": indicators.minus_di,
        "rv5": indicators.rv5,
        "rv15": indicators.rv15,
        "rv60": indicators.rv60,
        "vwap_sigma": indicators.price_vwap_dev_sigma,
        "consec_up": indicators.consec_up_candles,
        "consec_down": indicators.consec_down_candles,
        "candles": indicators.candles_in_buffer,
        "vix": vix_value,
    }

    # Insufficient data → CHOPPY with low confidence (don't trade)
    if indicators.candles_in_buffer < 30:
        return RegimeState(
            underlying=indicators.underlying,
            regime=Regime.CHOPPY,
            confidence=0.3,
            components={**components, "reason": "insufficient_candles"},
            ts=indicators.ts,
        )

    # 1. EVENT_DRIVEN
    if vix_value is not None and vix_value >= VIX_PANIC_LEVEL:
        confidence = min(1.0, (vix_value - VIX_PANIC_LEVEL) / 10 + 0.6)
        return RegimeState(
            underlying=indicators.underlying,
            regime=Regime.EVENT_DRIVEN,
            confidence=round(confidence, 3),
            components={**components, "reason": "vix_panic"},
            ts=indicators.ts,
        )

    # 2. Vol expansion / compression
    if indicators.rv5 and indicators.rv60 and indicators.rv60 > 0:
        ratio = indicators.rv5 / indicators.rv60
        components["rv_ratio"] = round(ratio, 3)
        if ratio >= RV_EXPANSION_RATIO:
            return RegimeState(
                underlying=indicators.underlying,
                regime=Regime.VOL_EXPANSION,
                confidence=min(1.0, (ratio - RV_EXPANSION_RATIO) / 1.0 + 0.6),
                components={**components, "reason": "rv_expansion"},
                ts=indicators.ts,
            )
        if ratio <= RV_COMPRESSION_RATIO:
            return RegimeState(
                underlying=indicators.underlying,
                regime=Regime.VOL_COMPRESSION,
                confidence=min(1.0, (RV_COMPRESSION_RATIO - ratio) / 0.5 + 0.6),
                components={**components, "reason": "rv_compression"},
                ts=indicators.ts,
            )

    # 3. Directional trend via ADX
    if indicators.adx14 is not None and indicators.adx14 >= ADX_TREND_THRESHOLD:
        plus = indicators.plus_di or 0
        minus = indicators.minus_di or 0
        if plus > minus:
            confidence = min(
                1.0,
                ((indicators.adx14 - ADX_TREND_THRESHOLD) / 30) + 0.55,
            )
            return RegimeState(
                underlying=indicators.underlying,
                regime=Regime.TREND_UP,
                confidence=round(confidence, 3),
                components={**components, "reason": "adx_plusdi"},
                ts=indicators.ts,
            )
        else:
            confidence = min(
                1.0,
                ((indicators.adx14 - ADX_TREND_THRESHOLD) / 30) + 0.55,
            )
            return RegimeState(
                underlying=indicators.underlying,
                regime=Regime.TREND_DOWN,
                confidence=round(confidence, 3),
                components={**components, "reason": "adx_minusdi"},
                ts=indicators.ts,
            )

    # 4. RANGE — low ADX but sufficient candles, not extreme vol
    if indicators.adx14 is not None and indicators.adx14 < ADX_CHOPPY_CEILING:
        return RegimeState(
            underlying=indicators.underlying,
            regime=Regime.CHOPPY,
            confidence=0.55,
            components={**components, "reason": "adx_low"},
            ts=indicators.ts,
        )
    return RegimeState(
        underlying=indicators.underlying,
        regime=Regime.RANGE,
        confidence=0.5,
        components={**components, "reason": "adx_neutral"},
        ts=indicators.ts,
    )


# ------------------ Engine class ------------------

class RegimeEngine:
    def __init__(
        self,
        session_factory: async_sessionmaker,
        redis: Redis,
    ):
        self._session_factory = session_factory
        self._redis = redis
        self._latest: dict[str, RegimeState] = {}

    @property
    def latest(self) -> dict[str, RegimeState]:
        return self._latest

    async def _read_vix(self) -> float | None:
        """Latest VIX from Redis pubsub-cached channel; falls back to None if absent."""
        v = await self._redis.get("md:vix:latest")
        if v is None:
            return None
        try:
            return float(v.decode())
        except Exception:
            return None

    async def evaluate(
        self,
        underlying_name: str,
        tick_buffer: TickBuffer,
        vix_value: float | None,
    ) -> RegimeState:
        ticks_df = await tick_buffer.to_dataframe()
        indicators = compute_indicators(underlying_name, ticks_df)
        regime = classify_regime(indicators, vix_value)
        await self._persist_and_publish(regime)
        self._latest[underlying_name] = regime
        return regime

    async def _persist_and_publish(self, regime: RegimeState) -> None:
        try:
            async with self._session_factory() as session:
                session.add(RegimeStateRow(
                    underlying=regime.underlying,
                    regime=regime.regime.value,
                    confidence=Decimal(str(regime.confidence)),
                    components={k: (v if not isinstance(v, float) or not math.isnan(v) else None) for k, v in regime.components.items()},
                    ts=regime.ts,
                ))
                await session.commit()
        except Exception as e:
            log.warning("regime.persist_failed", error=str(e), underlying=regime.underlying)

        channel = CHAN_REGIME.format(underlying=regime.underlying)
        await self._redis.publish(channel, orjson.dumps({
            "underlying": regime.underlying,
            "regime": regime.regime.value,
            "confidence": regime.confidence,
            "ts": regime.ts.isoformat(),
        }))
        # Cache last regime per underlying (read by Opportunity engine without pubsub roundtrip)
        await self._redis.set(
            f"regime:{regime.underlying}:latest",
            orjson.dumps({
                "regime": regime.regime.value,
                "confidence": regime.confidence,
                "ts": regime.ts.isoformat(),
                "components": regime.components,
            }),
            ex=120,
        )

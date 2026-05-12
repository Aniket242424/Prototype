"""
Phase 4 worker — the orchestrator that runs the full trading pipeline.

Pipeline per cycle (every 30s during market hours):
    1. Read latest opportunity from `opportunity:active` Redis key
    2. Read regime + intel + indicators for that underlying
    3. Compute session state (opening_range, gap_pct, session_open)
    4. Build StrategyContext
    5. For each enabled strategy, call evaluate(ctx)
    6. Pick highest-confidence signal (if any)
    7. Ask Claude advisor (veto-only)
    8. Build TradeIntent
    9. Risk Engine evaluates → RiskDecision
   10. If approved: Execution Engine places paper order
   11. If filled: register position with Position Manager

Plus a SEPARATE async task that runs every 1s during market hours:
    - For each open position, get current spot from Redis tick cache
    - Call PositionManager.evaluate_tick() for the underlying
    - On any ExitDecision: call ExecutionEngine.emergency_exit()
    - Persist position close + PnL

Run:
    py -3.14 -u -m trading_agent.strategy.worker
"""
from __future__ import annotations

import asyncio
import signal
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import click
import orjson
from sqlalchemy import select

from trading_agent.ai_reasoning.advisor import ClaudeAdvisor
from trading_agent.core.config import get_instruments_config, get_settings
from trading_agent.core.constants import (
    CHAN_OPPORTUNITY,
    Direction,
    OrderSide,
)
from trading_agent.core.logging import configure_logging, get_logger
from trading_agent.core.time_utils import is_market_open, now_ist
from trading_agent.execution.broker import PaperBroker
from trading_agent.execution.engine import ExecutionEngine
from trading_agent.execution.persistence import ExecutionPersistence
from trading_agent.infrastructure.db import SessionLocal
from trading_agent.infrastructure.models import (
    IndiaVixRow,
    MarketDataTickRow,
    OpportunityRow,
    OptionsChainSnapshotRow,
    PositionRow,
    RegimeStateRow,
)
from trading_agent.infrastructure.redis_client import make_redis
from trading_agent.position.dtos import PositionLifecycleStage, PositionState
from trading_agent.position.manager import PositionManager
from trading_agent.regime.dtos import (
    IndicatorSnapshot,
    Opportunity,
    OpportunityScore,
    OptionsIntel,
    RegimeState,
)
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
from trading_agent.regime.tick_buffer import TickBufferPool
from trading_agent.risk.dtos import Leg, TradeIntent
from trading_agent.risk.engine import RiskEngine
from trading_agent.strategy.base import StrategyContext
from trading_agent.strategy.registry import build_enabled_strategies
from trading_agent.strategy.session_state import compute_session_state

log = get_logger(__name__)


SIGNAL_CYCLE_SEC = 30.0
POSITION_CYCLE_SEC = 1.0


async def run() -> None:
    settings = get_settings()
    instruments_cfg = get_instruments_config()
    enabled = [i for i in instruments_cfg.instruments if i.enabled]
    enabled_keys = [i.upstox_instrument_key for i in enabled]

    log.info(
        "phase4_worker.starting",
        env=settings.app_env,
        underlyings=len(enabled),
        market_open=is_market_open(),
    )

    redis = make_redis()
    pool = TickBufferPool(redis, SessionLocal, instrument_keys=enabled_keys, max_minutes=180)
    await pool.start()

    strategies = build_enabled_strategies()
    log.info("phase4_worker.strategies_loaded", count=len(strategies),
             names=list(strategies.keys()))

    advisor = ClaudeAdvisor(settings, redis=redis)
    risk = RiskEngine(SessionLocal, redis)
    broker = PaperBroker(seed=int(datetime.now().timestamp()) % 100000)
    execution = ExecutionEngine(broker, SessionLocal, redis)
    persistence = ExecutionPersistence(SessionLocal)
    position_mgr = PositionManager(redis)

    stop_event = asyncio.Event()

    def _signal() -> None:
        log.info("phase4_worker.signal")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal)
        except NotImplementedError:
            pass

    async def _heartbeat() -> None:
        try:
            while not stop_event.is_set():
                await redis.set("worker:phase4:heartbeat", now_ist().isoformat(), ex=10)
                await asyncio.sleep(3)
        except asyncio.CancelledError:
            pass

    # =========================================================
    # Signal cycle — every 30s during market hours
    # =========================================================
    async def signal_cycle() -> None:
        try:
            while not stop_event.is_set():
                try:
                    await _run_signal_cycle(
                        redis, pool, strategies, advisor, risk, execution,
                        persistence, position_mgr, instruments_cfg,
                    )
                except Exception as e:
                    log.error("phase4_worker.signal_cycle_failed", error=str(e))
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=SIGNAL_CYCLE_SEC)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            pass

    # =========================================================
    # Position cycle — every 1s during market hours, manages open positions
    # =========================================================
    async def position_cycle() -> None:
        try:
            while not stop_event.is_set():
                try:
                    await _run_position_cycle(
                        redis, execution, persistence, position_mgr,
                    )
                except Exception as e:
                    log.error("phase4_worker.position_cycle_failed", error=str(e))
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=POSITION_CYCLE_SEC)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            pass

    hb_task = asyncio.create_task(_heartbeat(), name="phase4-hb")
    sig_task = asyncio.create_task(signal_cycle(), name="phase4-signal")
    pos_task = asyncio.create_task(position_cycle(), name="phase4-position")

    try:
        await stop_event.wait()
    except KeyboardInterrupt:
        pass
    finally:
        for t in (hb_task, sig_task, pos_task):
            t.cancel()
        await asyncio.gather(hb_task, sig_task, pos_task, return_exceptions=True)
        await pool.stop()
        await redis.aclose()
        log.info("phase4_worker.stopped")


# =================================================================
# Signal cycle: opportunity → strategy → advisor → risk → execution
# =================================================================

async def _run_signal_cycle(
    redis, pool, strategies, advisor, risk, execution, persistence,
    position_mgr, instruments_cfg,
):
    # 1. Read active opportunity
    opp_raw = await redis.get("opportunity:active")
    if opp_raw is None:
        return  # nothing to evaluate
    opp_payload = orjson.loads(opp_raw)
    underlying = opp_payload["underlying"]

    # 2. Pull regime / intel / indicators from Redis caches
    regime_raw = await redis.get(f"regime:{underlying}:latest")
    intel_raw = await redis.get(f"intel:{underlying}:latest")
    if regime_raw is None:
        return  # incomplete state
    regime_data = orjson.loads(regime_raw)

    # Build proper DTOs
    from trading_agent.core.constants import Regime
    regime = RegimeState(
        underlying=underlying,
        regime=Regime(regime_data["regime"]),
        confidence=float(regime_data["confidence"]),
        components=regime_data.get("components", {}),
        ts=datetime.fromisoformat(regime_data["ts"]),
    )

    intel = None
    if intel_raw is not None:
        from datetime import date as _date
        i = orjson.loads(intel_raw)
        intel = OptionsIntel(
            underlying=underlying,
            ts=datetime.fromisoformat(i["ts"]),
            expiry=datetime.fromisoformat(i["ts"]).date(),  # best-effort
            spot=float(i.get("spot") or 0.0),
            atm_strike=float(i.get("atm_strike") or 0.0),
            atm_call_iv=i.get("atm_call_iv"),
            atm_put_iv=i.get("atm_put_iv"),
            iv_rank_30d=i.get("iv_rank_30d"),
            iv_percentile_30d=i.get("iv_percentile_30d"),
            iv_skew=i.get("iv_skew"),
            pcr_oi=i.get("pcr_oi"),
            pcr_volume=i.get("pcr_volume"),
            max_pain_strike=i.get("max_pain_strike"),
            atm_call_spread_bps=i.get("atm_call_spread_bps"),
            atm_put_spread_bps=i.get("atm_put_spread_bps"),
            total_gamma_exposure=i.get("total_gamma_exposure"),
        )

    # 3. Get instrument key for tick buffer
    inst_key = None
    for inst in instruments_cfg.instruments:
        if inst.name == underlying:
            inst_key = inst.upstox_instrument_key
            break
    if inst_key is None:
        return

    buf = pool.get(inst_key)
    if buf is None:
        return
    ticks_df = await buf.to_dataframe()

    # 4. Compute indicators on the fly
    indicators = _compute_indicators_simple(underlying, ticks_df)

    # 5. Compute session state (gap, opening range)
    prev_close = await _previous_close_for(underlying)
    session = compute_session_state(ticks_df, prev_close, now_ts=now_ist())

    # 6. Build Opportunity DTO
    direction = Direction.LONG if opp_payload["direction"] == "LONG" else Direction.SHORT
    opp = Opportunity(
        underlying=underlying,
        direction=direction,
        score=float(opp_payload["score"]),
        components=OpportunityScore(**opp_payload.get("components", {})),
        recommended_expiry=datetime.fromisoformat(opp_payload["recommended_expiry"]).date()
            if isinstance(opp_payload.get("recommended_expiry"), str)
            else now_ist().date(),
        recommended_strike_band={
            "low": Decimal(opp_payload["recommended_strike_band"]["low"]),
            "high": Decimal(opp_payload["recommended_strike_band"]["high"]),
        } if opp_payload.get("recommended_strike_band") else {"low": Decimal("0"), "high": Decimal("0")},
        ts=datetime.fromisoformat(opp_payload["ts"]),
    )

    # 7. Build StrategyContext
    ctx = StrategyContext(
        underlying=underlying,
        direction=direction,
        opportunity=opp,
        regime=regime,
        indicators=indicators,
        intel=intel,
        ts=now_ist(),
        opening_range_high=session.opening_range_high,
        opening_range_low=session.opening_range_low,
        opening_range_formed=session.opening_range_formed,
        gap_pct=session.gap_pct,
        session_open=session.session_open,
        volume_ratio=None,
    )

    # 8. Evaluate all strategies; pick highest-confidence signal
    best_signal = None
    for name, strategy in strategies.items():
        try:
            sig = strategy.evaluate(ctx)
            if sig is not None:
                if best_signal is None or sig.confidence > best_signal.confidence:
                    best_signal = sig
        except Exception as e:
            log.warning("phase4_worker.strategy_failed", strategy=name, error=str(e))

    if best_signal is None:
        return  # no strategy fired

    log.info(
        "phase4_worker.signal_emitted",
        underlying=underlying,
        strategy=best_signal.strategy_name,
        confidence=best_signal.confidence,
    )

    # 9. Claude advisor
    advisor_decision = await advisor.evaluate(
        best_signal, regime, intel, indicators, opp
    )
    log.info(
        "phase4_worker.advisor_decision",
        decision=advisor_decision.decision,
        advisor_score=advisor_decision.advisor_score,
        vetoes=advisor_decision.vetoes_trade,
    )
    if advisor_decision.vetoes_trade:
        log.info("phase4_worker.advisor_vetoed",
                 reason=advisor_decision.rationale)
        return

    # 10. Build TradeIntent (Phase 3 single-leg: use intel.atm_strike as placeholder)
    # Real strike picking happens in Risk Engine via SmartStrikeSelector — but
    # in Phase 3.1 sizer we just use leg.target_premium directly. For Phase 4
    # we synthesize a placeholder strike + a guess premium; Risk Engine sizes
    # against this. Phase 4.7 (future) will run the smart strike selector here.
    placeholder_premium = Decimal("80")  # SENSEX/FINNIFTY-ish; Risk Engine sizes
    placeholder_strike = Decimal(str(intel.atm_strike if intel and intel.atm_strike else 0))
    leg = Leg(
        side=OrderSide.BUY,
        option_type=best_signal.option_type,
        strike=placeholder_strike,
        expiry=opp.recommended_expiry,
        instrument_key=f"NSE_FO|SYNTH-{underlying}-{int(placeholder_strike)}",
        target_qty=10,
        target_premium=placeholder_premium,
    )
    intent = TradeIntent(
        strategy_name=best_signal.strategy_name,
        underlying=underlying,
        direction=direction,
        legs=[leg],
        stop_underlying=best_signal.stop_underlying,
        target_underlying=best_signal.target_underlying,
        confidence=best_signal.confidence * advisor_decision.advisor_score,
        ts=now_ist(),
    )

    # 11. Risk Engine
    decision = await risk.evaluate(intent)
    if not decision.approved:
        log.info("phase4_worker.risk_rejected",
                 code=decision.code, reason=decision.reason)
        return

    # 12. Execution Engine (paper)
    result = await execution.execute(intent, decision)
    if not result.fills:
        log.info("phase4_worker.no_fill",
                 status=result.status.value,
                 rejection=result.rejection_reason)
        return

    # 13. Register position with PositionManager
    fill_avg = result.fills[0].price
    spot = Decimal(str(intel.spot)) if intel and intel.spot else placeholder_strike

    pos_state = PositionState(
        db_id=0,           # Phase 4.6 persists separately; using ephemeral id
        instrument_key=leg.instrument_key,
        underlying=underlying,
        strategy_name=best_signal.strategy_name,
        direction=direction,
        qty_initial=decision.sized_qty,
        qty_remaining=decision.sized_qty,
        avg_entry_premium=fill_avg,
        entry_underlying=spot,
        initial_stop_underlying=best_signal.stop_underlying,
        target_underlying=best_signal.target_underlying,
        stage=PositionLifecycleStage.HARD_STOP,
        current_stop_underlying=best_signal.stop_underlying,
        peak_underlying=spot,
        atr_at_entry=indicators.atr14 or 1.0,
        opened_at=now_ist(),
        breakeven_trigger_r_multiple=1.0,
        partial_profit_r_multiple=1.5,
        partial_profit_exit_fraction=0.5,
        trail_atr_multiple=2.0,
        runner_giveback_atr_multiple=1.0,
        is_stock_option=False,
    )
    await position_mgr.add_position(pos_state)
    log.info("phase4_worker.position_opened",
             underlying=underlying,
             qty=decision.sized_qty,
             entry_premium=str(fill_avg),
             stop=str(best_signal.stop_underlying),
             target=str(best_signal.target_underlying))


# =================================================================
# Position cycle: tick-driven exit evaluation for open positions
# =================================================================

async def _run_position_cycle(redis, execution, persistence, position_mgr):
    # Time-based exits (don't need a tick)
    time_exits = await position_mgr.evaluate_all_time_based()
    for decision in time_exits:
        await _process_exit(redis, execution, position_mgr, decision)

    # Tick-driven exits — read current spot from Redis cache
    for pos in position_mgr.open_positions():
        spot_raw = await redis.get(f"md:last_ltp:{pos.underlying}")
        if spot_raw is None:
            # Use intel cache as fallback
            intel_raw = await redis.get(f"intel:{pos.underlying}:latest")
            if intel_raw is None:
                continue
            try:
                intel_data = orjson.loads(intel_raw)
                spot = Decimal(str(intel_data.get("spot") or 0))
            except Exception:
                continue
        else:
            try:
                spot = Decimal(spot_raw.decode())
            except Exception:
                continue
        if spot <= 0:
            continue
        decisions = await position_mgr.evaluate_tick(pos.underlying, spot)
        for d in decisions:
            await _process_exit(redis, execution, position_mgr, d)


async def _process_exit(redis, execution, position_mgr, decision):
    """Fire emergency exit for a position and clean up state."""
    # Find the position state
    pos = None
    for p in position_mgr.open_positions():
        if p.db_id == decision.position_db_id:
            pos = p
            break
    if pos is None:
        return

    result = await execution.emergency_exit(
        instrument_key=pos.instrument_key,
        qty=decision.qty_to_exit,
        side=OrderSide.SELL,    # close a BUY position
        reference_premium=pos.avg_entry_premium,
        reason=f"{decision.trigger.value}: {decision.reason}",
    )

    if result.status.value == "FILLED":
        if decision.is_partial:
            await position_mgr.apply_partial_fill(pos.db_id, decision.qty_to_exit)
        else:
            await position_mgr.apply_close(pos.db_id)
        log.info(
            "phase4_worker.exit_filled",
            underlying=pos.underlying,
            trigger=decision.trigger.value,
            qty=decision.qty_to_exit,
            is_partial=decision.is_partial,
        )


# =================================================================
# Helpers
# =================================================================

async def _previous_close_for(underlying: str) -> float | None:
    """Best-effort previous-close lookup from market_data_ticks."""
    try:
        async with SessionLocal() as session:
            today_start_ist = now_ist().replace(hour=0, minute=0, second=0, microsecond=0)
            today_start_utc = today_start_ist.astimezone(timezone.utc)
            # Look at ticks before today (yesterday's close)
            from trading_agent.core.config import get_instruments_config
            inst_cfg = get_instruments_config()
            inst_key = None
            for i in inst_cfg.instruments:
                if i.name == underlying:
                    inst_key = i.upstox_instrument_key
                    break
            if inst_key is None:
                return None
            row = (await session.execute(
                select(MarketDataTickRow)
                .where(MarketDataTickRow.instrument_key == inst_key)
                .where(MarketDataTickRow.ts < today_start_utc)
                .order_by(MarketDataTickRow.ts.desc())
                .limit(1)
            )).scalar_one_or_none()
            if row is None:
                return None
            return float(row.ltp)
    except Exception:
        return None


def _compute_indicators_simple(underlying_name: str, ticks_df) -> IndicatorSnapshot:
    """Compute IndicatorSnapshot from tick buffer (similar to regime_engine.compute_indicators)."""
    ts_now = now_ist()
    if ticks_df.empty:
        return IndicatorSnapshot(underlying=underlying_name, ts=ts_now)

    candles = candles_from_ticks(ticks_df, freq="1min")
    if len(candles) < 5:
        return IndicatorSnapshot(
            underlying=underlying_name, ts=ts_now,
            candles_in_buffer=len(candles),
        )
    candles = candles.sort_values("ts").reset_index(drop=True)

    closes = candles["close"]
    e9 = float(ema(closes, 9).iloc[-1]) if len(candles) >= 9 else None
    e21 = float(ema(closes, 21).iloc[-1]) if len(candles) >= 21 else None
    e50 = float(ema(closes, 50).iloc[-1]) if len(candles) >= 50 else None

    today_open = ts_now.replace(hour=9, minute=15, second=0, microsecond=0).astimezone(
        candles["ts"].iloc[0].tzinfo
    )
    candles["vwap"] = vwap_session(candles, today_open)
    vw = candles["vwap"].iloc[-1] if "vwap" in candles else None
    if vw is not None:
        try:
            vw = float(vw)
        except (TypeError, ValueError):
            vw = None
    pv_sigma = price_vwap_deviation_sigma(candles)

    a14 = atr(candles, 14).iloc[-1] if len(candles) >= 15 else None
    spot = float(closes.iloc[-1])
    atr_pct = (float(a14) / spot * 100) if (a14 and spot) else None

    adx_dict = adx(candles, 14)
    a_adx = float(adx_dict["adx"].iloc[-1]) if len(candles) >= 28 else None
    p_di = float(adx_dict["plus_di"].iloc[-1]) if len(candles) >= 28 else None
    m_di = float(adx_dict["minus_di"].iloc[-1]) if len(candles) >= 28 else None

    rv5 = realized_vol_annualized(candles, 5)
    rv15 = realized_vol_annualized(candles, 15)
    rv60 = realized_vol_annualized(candles, 60)
    up, down = consecutive_direction(candles)

    return IndicatorSnapshot(
        underlying=underlying_name,
        ts=ts_now,
        ema9=e9, ema21=e21, ema50=e50,
        vwap=vw, price_vwap_dev_sigma=pv_sigma,
        atr14=float(a14) if a14 else None, atr_pct=atr_pct,
        rv5=rv5, rv15=rv15, rv60=rv60,
        adx14=a_adx, plus_di=p_di, minus_di=m_di,
        consec_up_candles=up, consec_down_candles=down,
        candles_in_buffer=len(candles),
    )


@click.command()
def main() -> None:
    """Run the Phase 4 worker (orchestrator)."""
    configure_logging()
    asyncio.run(run())


if __name__ == "__main__":
    main()

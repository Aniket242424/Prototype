"""Tests for the smart strike selector — pure function on chain dicts."""
from __future__ import annotations

from decimal import Decimal

import pytest

from trading_agent.core.config import RiskConfig
from trading_agent.core.constants import Direction, OptionType, Regime
from trading_agent.risk.strike_selector import (
    _atm_index,
    _strike_at_offset,
    select_strike,
)


def _risk_cfg() -> RiskConfig:
    return RiskConfig.model_validate({
        "daily_max_loss_pct": 0.02,
        "rolling_drawdown_pct": 0.05,
        "rolling_drawdown_window_days": 5,
        "per_trade_max_risk_pct": 0.005,
        "max_concurrent_positions": 1,
        "max_trades_per_day": 3,
        "consecutive_loss_lockout": 2,
        "slippage_kill_threshold_bps": 30.0,
        "slippage_kill_consecutive": 3,
        "max_spread_bps": 50.0,
        "min_liquidity_score": 0.55,
        "min_top5_depth_lots": 5,
        "min_strike_oi": 1000,
        "max_atr_consumed_at_entry": 0.6,
        "max_vwap_deviation_sigma": 1.5,
        "max_consecutive_signal_candles": 4,
        "opportunity_ttl_seconds": 90,
        "cooldown_after_fast_mover_sec": 180,
        "fast_mover_atr_multiple": 1.2,
        "no_reentry_same_direction_after_stop": True,
        "india_vix_ceiling": 30.0,
        "intraday_move_ceiling_pct": 0.025,
        "stale_tick_max_age_sec": 5.0,
        "broker_health_max_age_sec": 30.0,
        "entry_window_start": "09:20",
        "entry_window_end": "14:30",
        "forced_exit_time": "15:15",
        "max_estimated_slippage_bps": 20.0,
        "fill_timeout_ms": 2000,
        "fill_improve_max_ticks": 3,
        "fill_improve_step_ticks": 1,
        "allow_market_orders_for_entries": False,
        "require_pullback_for_trend_entries": True,
        "initial_stop_atr_multiple": 1.5,
        "breakeven_trigger_r_multiple": 1.0,
        "trail_method": "atr_chandelier",
        "trail_atr_period": 14,
        "trail_atr_multiple": 2.0,
        "trail_only_in_profit": True,
        "partial_profit_r_multiple": 1.5,
        "partial_profit_exit_fraction": 0.5,
        "runner_giveback_atr_multiple": 1.0,
    })


def _build_strike(strike_price: float, ce_ltp: float, pe_ltp: float, oi: int = 100_000) -> dict:
    """Build a chain row with realistic-shaped CE + PE data.
    Spread is 0.1% (20 bps) — tight enough to pass max_spread_bps=50 cap.
    """
    return {
        "expiry": "2026-05-15",
        "strike_price": strike_price,
        "underlying_spot_price": 24000,
        "call_options": {
            "instrument_key": f"NSE_FO|CE-{int(strike_price)}",
            "market_data": {
                "ltp": ce_ltp,
                "bid_price": ce_ltp * 0.999,    # 10 bps below mid
                "ask_price": ce_ltp * 1.001,    # 10 bps above mid → 20 bps spread
                "oi": oi,
            },
            "option_greeks": {"iv": 14.0, "delta": 0.5},
        },
        "put_options": {
            "instrument_key": f"NSE_FO|PE-{int(strike_price)}",
            "market_data": {
                "ltp": pe_ltp,
                "bid_price": pe_ltp * 0.999,
                "ask_price": pe_ltp * 1.001,
                "oi": oi,
            },
            "option_greeks": {"iv": 14.5, "delta": -0.5},
        },
    }


def _chain(spot: float = 24000.0) -> dict:
    # Strikes 50 apart around 24000 (NIFTY-like)
    return {
        "underlying_spot": spot,
        "strikes": [
            _build_strike(23900, 180, 30),    # ITM CE / OTM PE
            _build_strike(23950, 140, 50),    # ITM1 CE
            _build_strike(24000, 100, 100),   # ATM
            _build_strike(24050, 70, 140),    # OTM CE / ITM1 PE
            _build_strike(24100, 50, 180),    # OTM CE / ITM2 PE
        ],
    }


def test_atm_index_picks_closest():
    chain = _chain()
    idx = _atm_index(chain["strikes"], 24000.0)
    assert chain["strikes"][idx]["strike_price"] == 24000


def test_strike_at_offset_long_itm1():
    chain = _chain()
    atm_idx = _atm_index(chain["strikes"], 24000)
    row = _strike_at_offset(chain["strikes"], 24000, atm_idx, Direction.LONG, "ITM1")
    # LONG CE → ITM = lower strike
    assert row["strike_price"] == 23950


def test_strike_at_offset_short_itm1():
    chain = _chain()
    atm_idx = _atm_index(chain["strikes"], 24000)
    row = _strike_at_offset(chain["strikes"], 24000, atm_idx, Direction.SHORT, "ITM1")
    # SHORT PE → ITM = higher strike
    assert row["strike_price"] == 24050


def test_select_strike_picks_itm1_in_trend_regime():
    """In TREND_UP, default preference is ITM1 first."""
    selected = select_strike(
        chain_snapshot=_chain(),
        direction=Direction.LONG,
        regime=Regime.TREND_UP,
        is_last_hour=False,
        iv_percentile_30d=0.4,
        risk_cfg=_risk_cfg(),
        per_trade_max_outlay_inr=Decimal("5000"),     # large enough for any strike
        underlying_lot_size=25,
    )
    assert selected is not None
    assert selected.option_type == OptionType.CE
    assert selected.selected_offset == "ITM1"
    assert selected.strike == Decimal("23950")


def test_select_strike_picks_atm_in_vol_expansion():
    """VOL_EXPANSION → prefer ATM for gamma."""
    selected = select_strike(
        chain_snapshot=_chain(),
        direction=Direction.LONG,
        regime=Regime.VOL_EXPANSION,
        is_last_hour=False,
        iv_percentile_30d=0.4,
        risk_cfg=_risk_cfg(),
        per_trade_max_outlay_inr=Decimal("5000"),
        underlying_lot_size=25,
    )
    assert selected is not None
    assert selected.selected_offset == "ATM"


def test_select_strike_picks_itm2_when_iv_rich():
    """High IV percentile → prefer ITM2 for less IV-crush exposure."""
    selected = select_strike(
        chain_snapshot=_chain(),
        direction=Direction.SHORT,         # PE
        regime=Regime.TREND_DOWN,
        is_last_hour=False,
        iv_percentile_30d=0.85,
        risk_cfg=_risk_cfg(),
        per_trade_max_outlay_inr=Decimal("10000"),
        underlying_lot_size=25,
    )
    assert selected is not None
    assert selected.selected_offset == "ITM2"
    # SHORT ITM2 = strike 24100 (2 strikes higher than 24000 ATM)
    assert selected.strike == Decimal("24100")


def test_select_strike_returns_none_when_nothing_fits_cap():
    """All premiums × lot exceed budget → None."""
    selected = select_strike(
        chain_snapshot=_chain(),
        direction=Direction.LONG,
        regime=Regime.TREND_UP,
        is_last_hour=False,
        iv_percentile_30d=0.4,
        risk_cfg=_risk_cfg(),
        per_trade_max_outlay_inr=Decimal("500"),     # too small for any premium × 25
        underlying_lot_size=25,
    )
    assert selected is None


def test_select_strike_skips_illiquid_oi():
    """A strike with low OI gets skipped."""
    chain = _chain()
    # Make the ITM1 strike illiquid
    chain["strikes"][1]["call_options"]["market_data"]["oi"] = 100  # below min_strike_oi=1000

    selected = select_strike(
        chain_snapshot=chain,
        direction=Direction.LONG,
        regime=Regime.TREND_UP,
        is_last_hour=False,
        iv_percentile_30d=0.4,
        risk_cfg=_risk_cfg(),
        per_trade_max_outlay_inr=Decimal("5000"),
        underlying_lot_size=25,
    )
    # Should fall through preference list and pick ATM (next preference)
    assert selected is not None
    assert selected.selected_offset == "ATM"

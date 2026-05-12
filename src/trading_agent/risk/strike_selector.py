"""
Smart strike selector — picks ATM / ITM1 / ITM2 based on context.

Inputs: latest chain snapshot, opportunity (direction), capital, per-trade cap,
regime, time-of-day, IV percentile. Output: the contract that fits cleanest
within those constraints.

Hard rules (never violated):
- NEVER far-OTM (selector returns None rather than picking OTM)
- Must fit per-trade outlay cap (1-lot outlay ≤ cap)
- Must satisfy liquidity gates (spread bps + min OI on the strike)

Soft preferences (regime/time dependent):
- VOL_EXPANSION or last hour → prefer ATM (need gamma)
- TREND_UP/DOWN steady → prefer ITM1 (less theta drag)
- High IV percentile (>0.7) → prefer ITM2 (less IV-crush exposure)
- Default → ITM1
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal

from trading_agent.core.config import RiskConfig
from trading_agent.core.constants import Direction, OptionType, Regime
from trading_agent.core.logging import get_logger

log = get_logger(__name__)

StrikeChoice = Literal["ATM", "ITM1", "ITM2", "OTM1"]


@dataclass(frozen=True)
class SelectedStrike:
    """Result of strike selection. None when nothing fits the constraints."""

    instrument_key: str
    strike: Decimal
    expiry: date
    option_type: OptionType
    premium: Decimal
    lot_size: int
    one_lot_outlay: Decimal
    spread_bps: float
    oi: int
    iv: float | None
    delta: float | None
    selected_offset: StrikeChoice          # which moneyness was chosen
    moneyness_pct: float                    # 100 * (strike - spot) / spot


def _strike_at_offset(
    strikes: list[dict],
    spot: float,
    atm_idx: int,
    direction: Direction,
    offset: StrikeChoice,
) -> dict | None:
    """
    Pick a strike at the requested offset from ATM.

    For LONG (buying CE):  ITM = LOWER strike, OTM = HIGHER strike
    For SHORT (buying PE): ITM = HIGHER strike, OTM = LOWER strike
    """
    if offset == "ATM":
        return strikes[atm_idx] if 0 <= atm_idx < len(strikes) else None

    # Direction of offset on the strike-index axis
    # Strikes are sorted ascending. ITM for CE = index lower; ITM for PE = index higher.
    sign = -1 if direction == Direction.LONG else 1
    distances = {"ITM1": 1, "ITM2": 2, "OTM1": -1}
    delta_idx = sign * distances[offset]
    target = atm_idx + delta_idx
    if 0 <= target < len(strikes):
        return strikes[target]
    return None


def _atm_index(strikes: list[dict], spot: float) -> int:
    best = 0
    best_diff = float("inf")
    for i, s in enumerate(strikes):
        d = abs(float(s.get("strike_price", 0)) - spot)
        if d < best_diff:
            best_diff = d
            best = i
    return best


def _safe_f(x) -> float | None:
    if x is None:
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _evaluate_strike(
    strike_row: dict,
    direction: Direction,
    spot: float,
    risk_cfg: RiskConfig,
    per_trade_max_outlay_inr: Decimal,
    underlying_lot_size: int,
) -> SelectedStrike | None:
    """Build a SelectedStrike if the row passes liquidity + capital filters, else None."""
    if not strike_row:
        return None

    side = "call_options" if direction == Direction.LONG else "put_options"
    opt = strike_row.get(side) or {}
    md = opt.get("market_data") or {}
    grk = opt.get("option_greeks") or {}

    bid = _safe_f(md.get("bid_price"))
    ask = _safe_f(md.get("ask_price"))
    ltp = _safe_f(md.get("ltp"))
    oi = int(_safe_f(md.get("oi")) or 0)
    iv = _safe_f(grk.get("iv"))
    delta = _safe_f(grk.get("delta"))

    if not bid or not ask or ask <= 0 or bid <= 0 or ask < bid:
        return None
    mid = (bid + ask) / 2
    if mid <= 0:
        return None
    spread_bps = ((ask - bid) / mid) * 10000

    # Use mid (or LTP) as the reference premium for outlay math
    premium = Decimal(str(ltp if ltp and ltp > 0 else mid))
    one_lot_outlay = premium * underlying_lot_size

    # Filter 1: spread
    if spread_bps > risk_cfg.max_spread_bps:
        return None
    # Filter 2: OI
    if oi < risk_cfg.min_strike_oi:
        return None
    # Filter 3: 1-lot fits per-trade cap
    if one_lot_outlay > per_trade_max_outlay_inr:
        return None

    strike_value = Decimal(str(strike_row["strike_price"]))
    moneyness_pct = 100 * (float(strike_value) - spot) / spot if spot else 0.0

    return SelectedStrike(
        instrument_key=opt.get("instrument_key", ""),
        strike=strike_value,
        expiry=date.fromisoformat(strike_row["expiry"]) if strike_row.get("expiry") else date.today(),
        option_type=OptionType.CE if direction == Direction.LONG else OptionType.PE,
        premium=premium,
        lot_size=underlying_lot_size,
        one_lot_outlay=one_lot_outlay,
        spread_bps=spread_bps,
        oi=oi,
        iv=iv,
        delta=delta,
        selected_offset="ATM",   # overridden by caller
        moneyness_pct=moneyness_pct,
    )


def select_strike(
    chain_snapshot: dict,
    direction: Direction,
    regime: Regime,
    is_last_hour: bool,
    iv_percentile_30d: float | None,
    risk_cfg: RiskConfig,
    per_trade_max_outlay_inr: Decimal,
    underlying_lot_size: int,
) -> SelectedStrike | None:
    """
    Pick the best contract for the given context.

    Returns None if NOTHING in the chain fits within risk/liquidity constraints
    — in which case the Risk Engine will reject the trade ("no affordable strike").
    """
    strikes = chain_snapshot.get("strikes") or []
    spot = _safe_f(chain_snapshot.get("underlying_spot"))
    if not strikes or spot is None:
        return None

    atm_idx = _atm_index(strikes, spot)

    # Build preference order based on context
    preferences: list[StrikeChoice]
    if regime == Regime.VOL_EXPANSION or is_last_hour:
        # Need gamma → ATM first
        preferences = ["ATM", "ITM1"]
    elif iv_percentile_30d is not None and iv_percentile_30d > 0.7:
        # IV is rich → less IV-crush exposure via ITM2
        preferences = ["ITM2", "ITM1", "ATM"]
    elif regime in (Regime.TREND_UP, Regime.TREND_DOWN):
        # Steady trend → ITM1 has best theta-vs-cost ratio
        preferences = ["ITM1", "ATM", "ITM2"]
    else:
        # Default
        preferences = ["ITM1", "ATM"]

    for choice in preferences:
        row = _strike_at_offset(strikes, spot, atm_idx, direction, choice)
        candidate = _evaluate_strike(
            row, direction, spot, risk_cfg, per_trade_max_outlay_inr, underlying_lot_size
        ) if row else None
        if candidate is not None:
            # Patch the selected offset metadata
            return SelectedStrike(
                **{**candidate.__dict__, "selected_offset": choice}
            )

    return None  # nothing fits

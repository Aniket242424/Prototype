"""
Delta Exchange India — options brokerage/fee model.

Source: Delta's official fee schedule (verified 2026-06-04)
  https://www.delta.exchange/support/solutions/articles/80001177864

Rules:
  - Trading fee per leg = 0.010% of NOTIONAL, where notional = spot * qty(BTC).
  - Capped at 3.5% of the PREMIUM value of that leg (the lower of the two applies;
    the cap binds on cheap, deep-OTM legs).
  - 18% GST is added on top of the computed fee.
  - NO trading fee on a leg that expires OUT-OF-THE-MONEY (zero intrinsic).
    So a winning iron condor (settles between the shorts) pays entry fees only.

We do NOT compute income tax here — that's a year-end filing matter, handled
outside the bot.

All amounts in USD (Delta is USDT-settled).
"""
from __future__ import annotations

FEE_RATE_NOTIONAL = 0.0001     # 0.010% of notional
PREMIUM_CAP_PCT = 0.035        # capped at 3.5% of premium
GST = 0.18                     # 18% GST on the fee


def leg_fee_usd(
    spot: float,
    premium_per_btc: float,
    lots: int,
    contract_value: float,
) -> float:
    """
    Brokerage (incl. GST) for ONE option leg trade (entry or a non-OTM settle).

    spot:            underlying spot at the time of the trade
    premium_per_btc: option price quoted per 1.0 BTC
    lots:            number of contracts
    contract_value:  BTC per lot (0.001 for Delta BTC options)
    """
    qty_btc = lots * contract_value
    notional = spot * qty_btc
    fee_notional = FEE_RATE_NOTIONAL * notional
    premium_value = premium_per_btc * qty_btc
    fee_cap = PREMIUM_CAP_PCT * premium_value
    fee = min(fee_notional, fee_cap) if premium_value > 0 else fee_notional
    return fee * (1.0 + GST)


def entry_fees_usd(legs: list[dict] | dict, spot: float, lots: int) -> float:
    """
    Total entry brokerage for all 4 condor legs. Accepts either a list of leg
    dicts (state.legs) or a dict of {role: Leg-like}. Each leg needs
    'price_per_btc' and 'contract_value'.
    """
    items = legs.values() if isinstance(legs, dict) else legs
    total = 0.0
    for leg in items:
        ppb = _get(leg, "price_per_btc")
        cv = _get(leg, "contract_value") or 0.001
        total += leg_fee_usd(spot, ppb, lots, cv)
    return total


def settlement_fees_usd(legs: list[dict], spot_settle: float, lots: int) -> float:
    """
    Brokerage at expiry. Legs that finish OTM pay NOTHING (Delta rule). Only
    ITM legs incur a settlement fee, charged on their intrinsic value as premium.
    """
    total = 0.0
    for leg in legs:
        strike = _get(leg, "strike")
        otype = _get(leg, "option_type")
        cv = _get(leg, "contract_value") or 0.001
        if otype == "call":
            intrinsic = max(0.0, spot_settle - strike)
        else:
            intrinsic = max(0.0, strike - spot_settle)
        if intrinsic <= 0:
            continue  # OTM -> no fee
        total += leg_fee_usd(spot_settle, intrinsic, lots, cv)
    return total


def _get(leg, key):
    """Read a field from either a dataclass-like object or a dict."""
    if isinstance(leg, dict):
        v = leg.get(key)
    else:
        v = getattr(leg, key, None)
    try:
        return float(v) if key in ("price_per_btc", "contract_value", "strike") else v
    except (TypeError, ValueError):
        return 0.0

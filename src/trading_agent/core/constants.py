"""Stable constants shared across modules."""
from __future__ import annotations

from enum import StrEnum

# --- Underlyings ---
UNDERLYINGS: tuple[str, ...] = ("NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX", "BANKEX")


class Direction(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"  # for option-buying: long-CALL or long-PUT — kept here for symmetry


class OptionType(StrEnum):
    CE = "CE"
    PE = "PE"


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"
    LIMIT_IOC = "LIMIT_IOC"


class OrderStatus(StrEnum):
    NEW = "NEW"
    SENT = "SENT"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


class Regime(StrEnum):
    TREND_UP = "TREND_UP"
    TREND_DOWN = "TREND_DOWN"
    RANGE = "RANGE"
    CHOPPY = "CHOPPY"
    VOL_EXPANSION = "VOL_EXPANSION"
    VOL_COMPRESSION = "VOL_COMPRESSION"
    EVENT_DRIVEN = "EVENT_DRIVEN"


# Regimes during which option BUYING is suppressed by hard rule.
SUPPRESSED_REGIMES: frozenset[Regime] = frozenset(
    {Regime.CHOPPY, Regime.VOL_COMPRESSION}
)


# --- Redis keys / pubsub channels ---
KILL_SWITCH_KEY = "kill_switch:global"
KILL_SWITCH_REASON_KEY = "kill_switch:reason"
KILL_SWITCH_TRIPPED_AT_KEY = "kill_switch:tripped_at"

CHAN_TICK = "md:tick:{instrument_key}"
CHAN_CHAIN = "md:chain:{underlying}"
CHAN_VIX = "md:vix"
CHAN_REGIME = "regime:{underlying}"
CHAN_OPPORTUNITY = "opportunity"
CHAN_POSITION = "position:{event}"  # event in {open, close, update}
CHAN_KILL_SWITCH = "kill_switch:events"


# --- India VIX ---
INDIA_VIX_INSTRUMENT_KEY: str = "NSE_INDEX|India VIX"


# --- Misc ---
NSE_HOLIDAYS_2026: frozenset[str] = frozenset({
    # Stub — load from a maintained source in Phase 1.
    # Format: "YYYY-MM-DD"
})

"""Sanity tests on constants/enums."""
from __future__ import annotations

from trading_agent.core.constants import SUPPRESSED_REGIMES, UNDERLYINGS, Regime


def test_underlyings_complete():
    assert set(UNDERLYINGS) == {"NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX", "BANKEX"}


def test_suppressed_regimes_includes_choppy():
    assert Regime.CHOPPY in SUPPRESSED_REGIMES
    assert Regime.VOL_COMPRESSION in SUPPRESSED_REGIMES
    assert Regime.TREND_UP not in SUPPRESSED_REGIMES

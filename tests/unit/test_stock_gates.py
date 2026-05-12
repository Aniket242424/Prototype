"""Tests for stock-options-specific risk gates."""
from __future__ import annotations

from datetime import date

from trading_agent.risk.stock_gates import (
    EARNINGS_CALENDAR,
    TOP_30_STOCK_WHITELIST,
    check_earnings_blackout,
    check_no_overnight_stock_option,
    check_universe_whitelist,
    is_stock_option,
)


def test_is_stock_option_recognizes_indices():
    assert is_stock_option("NIFTY") is False
    assert is_stock_option("BANKNIFTY") is False
    assert is_stock_option("FINNIFTY") is False
    assert is_stock_option("SENSEX") is False
    assert is_stock_option("BANKEX") is False


def test_is_stock_option_recognizes_stocks():
    assert is_stock_option("RELIANCE") is True
    assert is_stock_option("HDFCBANK") is True
    assert is_stock_option("BSE") is True


def test_universe_whitelist_passes_index():
    allowed, reason = check_universe_whitelist("NIFTY")
    assert allowed is True
    assert "n/a" in reason


def test_universe_whitelist_rejects_empty_universe_for_stock():
    # Until config/instruments.yaml is extended, whitelist is empty
    allowed, reason = check_universe_whitelist("RELIANCE")
    assert allowed is False
    assert "empty" in reason or "not in" in reason


def test_earnings_blackout_passes_index():
    allowed, _ = check_earnings_blackout("NIFTY", date(2026, 5, 13))
    assert allowed is True


def test_earnings_blackout_passes_stock_with_no_earnings():
    # Stock not in EARNINGS_CALENDAR → no blackout
    allowed, _ = check_earnings_blackout("RELIANCE", date(2026, 5, 13))
    assert allowed is True


def test_earnings_blackout_rejects_when_within_window():
    # Inject a temporary calendar entry to test the logic
    EARNINGS_CALENDAR["TESTSTOCK"] = [date(2026, 5, 15)]
    try:
        allowed, reason = check_earnings_blackout(
            "TESTSTOCK", date(2026, 5, 13), blackout_days=7
        )
        assert allowed is False
        assert "2026-05-15" in reason
    finally:
        EARNINGS_CALENDAR.pop("TESTSTOCK", None)


def test_earnings_blackout_allows_when_outside_window():
    EARNINGS_CALENDAR["TESTSTOCK"] = [date(2026, 6, 1)]
    try:
        allowed, _ = check_earnings_blackout(
            "TESTSTOCK", date(2026, 5, 13), blackout_days=7
        )
        assert allowed is True
    finally:
        EARNINGS_CALENDAR.pop("TESTSTOCK", None)


def test_no_overnight_passes_index_unconditionally():
    allowed, _ = check_no_overnight_stock_option("NIFTY")
    assert allowed is True


def test_no_overnight_passes_stock_at_risk_engine_time():
    # Risk Engine doesn't reject — Position Manager enforces the 15:15 exit
    allowed, reason = check_no_overnight_stock_option("RELIANCE")
    assert allowed is True
    assert "intraday" in reason.lower()

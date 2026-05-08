"""Test options intel pure helpers in isolation."""
from __future__ import annotations

from trading_agent.regime.options_intel import (
    _atm_strike_index,
    _classify_buildup,
    _gamma_exposure_proxy,
    _iv_rank_pct,
    _max_pain,
    _spread_bps,
)


def test_atm_strike_index_picks_closest():
    strikes = [
        {"strike_price": 24000},
        {"strike_price": 24100},
        {"strike_price": 24200},
        {"strike_price": 24300},
    ]
    assert _atm_strike_index(strikes, 24149) == 1   # 24100 is closer
    assert _atm_strike_index(strikes, 24151) == 2   # 24200 is closer
    assert _atm_strike_index(strikes, 24300) == 3
    assert _atm_strike_index([], 24000) == -1


def test_max_pain_picks_strike_with_min_writer_pain():
    # Construct a chain where max pain should be at strike 24200:
    # all the OI is concentrated at strike 24200 on both sides → minimum pain there.
    strikes = [
        {
            "strike_price": K,
            "call_options": {"market_data": {"oi": (10000 if K == 24200 else 0)}},
            "put_options":  {"market_data": {"oi": (10000 if K == 24200 else 0)}},
        }
        for K in (24000, 24100, 24200, 24300, 24400)
    ]
    assert _max_pain(strikes) == 24200


def test_spread_bps_basic():
    assert _spread_bps({"bid_price": 99.5, "ask_price": 100.5}) == 100.0  # (1 / 100) * 10000
    assert _spread_bps({"bid_price": 0, "ask_price": 100.5}) is None
    assert _spread_bps({"bid_price": 100, "ask_price": 99}) is None  # crossed


def test_classify_buildup_long_buildup():
    b = _classify_buildup(price_change_pct=2.0, oi_change_pct=3.0)
    assert b.label == "long_buildup"


def test_classify_buildup_short_buildup():
    b = _classify_buildup(price_change_pct=-2.0, oi_change_pct=3.0)
    assert b.label == "short_buildup"


def test_classify_buildup_short_covering():
    b = _classify_buildup(price_change_pct=2.0, oi_change_pct=-3.0)
    assert b.label == "short_covering"


def test_classify_buildup_long_unwinding():
    b = _classify_buildup(price_change_pct=-2.0, oi_change_pct=-3.0)
    assert b.label == "long_unwinding"


def test_classify_buildup_neutral_when_small():
    b = _classify_buildup(price_change_pct=0.1, oi_change_pct=0.1)
    assert b.label == "neutral"


def test_iv_rank_pct_basic():
    history = [10.0, 12.0, 14.0, 16.0, 18.0]
    rank, pct = _iv_rank_pct(history, current=14.0)
    assert rank == (14.0 - 10.0) / (18.0 - 10.0)
    assert pct == 2 / 5  # 2 values strictly below 14


def test_iv_rank_pct_insufficient_history():
    rank, pct = _iv_rank_pct([10.0], 12.0)
    assert rank is None and pct is None


def test_gamma_exposure_proxy_sums_correctly():
    strikes = [
        {
            "call_options": {"market_data": {"oi": 100}, "option_greeks": {"gamma": 0.5}},
            "put_options":  {"market_data": {"oi": 200}, "option_greeks": {"gamma": -0.4}},
        },
    ]
    g = _gamma_exposure_proxy(strikes)
    assert g == 0.5 * 100 + 0.4 * 200

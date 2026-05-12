"""Tests for the strategy registry."""
from __future__ import annotations

from trading_agent.core.config import StrategiesConfig
from trading_agent.strategy.registry import build_enabled_strategies


def test_registry_builds_both_strategies_when_enabled():
    cfg = StrategiesConfig(
        momentum_breakout_enabled=True,
        trend_continuation_enabled=True,
        volatility_expansion_enabled=False,
        gap_continuation_enabled=False,
        event_driven_enabled=False,
    )
    strategies = build_enabled_strategies(cfg)
    assert "orb" in strategies
    assert "ema_crossover_trend" in strategies
    assert len(strategies) == 2


def test_registry_skips_disabled_strategies():
    cfg = StrategiesConfig(
        momentum_breakout_enabled=False,
        trend_continuation_enabled=True,
        volatility_expansion_enabled=False,
        gap_continuation_enabled=False,
        event_driven_enabled=False,
    )
    strategies = build_enabled_strategies(cfg)
    assert "ema_crossover_trend" in strategies
    assert "orb" not in strategies


def test_registry_returns_empty_when_all_disabled():
    cfg = StrategiesConfig(
        momentum_breakout_enabled=False,
        trend_continuation_enabled=False,
        volatility_expansion_enabled=False,
        gap_continuation_enabled=False,
        event_driven_enabled=False,
    )
    strategies = build_enabled_strategies(cfg)
    assert strategies == {}


def test_registry_strategy_names_match_class_attribute():
    cfg = StrategiesConfig(
        momentum_breakout_enabled=True,
        trend_continuation_enabled=True,
        volatility_expansion_enabled=False,
        gap_continuation_enabled=False,
        event_driven_enabled=False,
    )
    strategies = build_enabled_strategies(cfg)
    for name, strat in strategies.items():
        assert name == strat.name


def test_registry_builds_all_four_phase4_strategies():
    """Phase 4.3: vol_expansion + gap_continuation now plug in."""
    cfg = StrategiesConfig(
        momentum_breakout_enabled=True,
        trend_continuation_enabled=True,
        volatility_expansion_enabled=True,
        gap_continuation_enabled=True,
        event_driven_enabled=False,
    )
    strategies = build_enabled_strategies(cfg)
    assert "ema_crossover_trend" in strategies
    assert "orb" in strategies
    assert "vol_expansion" in strategies
    assert "gap_continuation" in strategies
    assert len(strategies) == 4


def test_registry_vol_expansion_only():
    cfg = StrategiesConfig(
        momentum_breakout_enabled=False,
        trend_continuation_enabled=False,
        volatility_expansion_enabled=True,
        gap_continuation_enabled=False,
        event_driven_enabled=False,
    )
    strategies = build_enabled_strategies(cfg)
    assert "vol_expansion" in strategies
    assert len(strategies) == 1


def test_registry_gap_continuation_only():
    cfg = StrategiesConfig(
        momentum_breakout_enabled=False,
        trend_continuation_enabled=False,
        volatility_expansion_enabled=False,
        gap_continuation_enabled=True,
        event_driven_enabled=False,
    )
    strategies = build_enabled_strategies(cfg)
    assert "gap_continuation" in strategies
    assert len(strategies) == 1

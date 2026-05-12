"""
Strategy registry — central mapping of strategy_name → Strategy instance.

Driven by `config/strategies.yaml`. Each strategy has an enable/disable
flag; only enabled strategies get evaluated by the Phase 4 worker.

Why a registry rather than hard-coding the list:
- Strategies can be toggled per-environment (e.g., disable ORB in backtest)
- New strategies (Phase 4.3 vol-expansion, gap-continuation, etc.) plug in
  by adding one line here + their config flag
- Tests can register mock strategies easily

The registry is NOT a singleton — each Phase 4 worker process builds its
own. This keeps test isolation clean.
"""
from __future__ import annotations

from trading_agent.core.config import StrategiesConfig, get_strategies_config
from trading_agent.core.logging import get_logger
from trading_agent.strategy.base import Strategy
from trading_agent.strategy.ema_crossover import EMACrossoverConfig, EMACrossoverTrendStrategy
from trading_agent.strategy.gap_continuation import GapContinuationConfig, GapContinuationStrategy
from trading_agent.strategy.orb import ORBConfig, ORBStrategy
from trading_agent.strategy.vol_expansion import VolExpansionConfig, VolExpansionStrategy

log = get_logger(__name__)


def build_enabled_strategies(
    cfg: StrategiesConfig | None = None,
) -> dict[str, Strategy]:
    """
    Returns a dict of {strategy_name: strategy_instance} for all strategies
    enabled in config/strategies.yaml.

    Phase 4.2 ships two strategies; the others raise NotImplementedError
    until their phases land. Disabled strategies are simply absent from
    the returned dict.
    """
    cfg = cfg or get_strategies_config()
    enabled: dict[str, Strategy] = {}

    if cfg.trend_continuation_enabled:
        enabled["ema_crossover_trend"] = EMACrossoverTrendStrategy(EMACrossoverConfig())
        log.info("registry.enabled", strategy="ema_crossover_trend")

    if cfg.momentum_breakout_enabled:
        enabled["orb"] = ORBStrategy(ORBConfig())
        log.info("registry.enabled", strategy="orb")

    if cfg.volatility_expansion_enabled:
        enabled["vol_expansion"] = VolExpansionStrategy(VolExpansionConfig())
        log.info("registry.enabled", strategy="vol_expansion")

    if cfg.gap_continuation_enabled:
        enabled["gap_continuation"] = GapContinuationStrategy(GapContinuationConfig())
        log.info("registry.enabled", strategy="gap_continuation")

    # Phase 5+ event-driven strategy (deferred until news/calendar feed exists):
    # if cfg.event_driven_enabled:
    #     enabled["event_driven"] = EventDrivenStrategy()

    if not enabled:
        log.warning("registry.empty",
                    note="no strategies enabled — Phase 4 worker will emit no signals")

    return enabled

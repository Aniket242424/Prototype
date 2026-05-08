# Data Flow

A complete lifecycle from a single tick to a closed position. Phase markers indicate which modules are live in each phase.

```
   ┌──────── Upstox WS ───────┐
   │                          │
   ▼                          ▼
[Tick]                  [ChainSnapshot]
   │                          │
   ├── persist (Postgres) ────┤             [Phase 1]
   │                          │
   ├── publish md:tick:{ik} ──┤
   │   publish md:chain:{u} ──┤
   ▼                          ▼
[Regime Engine]    [Options Intel]          [Phase 2]
   │ regime+conf       │ greeks, IV, OI buildup
   ▼                  ▼
[Opportunity Ranking] (consumes both)        [Phase 2]
   │ Opportunity (winner only)
   ▼
[Strategy Engine] picks strategy             [Phase 4]
   │ TradeIntent
   ▼
[AI Reasoning] (Claude advisor)              [Phase 4]
   │ AdvisorOutput (veto-only)
   ▼
[Risk Engine] (16 deterministic checks)      [Phase 3]
   │ RiskDecision(approve=true, sized_qty=1)
   ▼
[Execution Engine]                           [Phase 3]
   │ LIMIT @ mid → monitor → improve → fill
   │ Order, Execution, SlippageLog rows
   ▼
[Position Manager]                           [Phase 4]
   │ logical stop + ATR trail + target
   │ on tick: re-evaluate exits
   ▼
[Exit] (target / stop / theta / kill)
   │ Execution Engine emergency-exits
   │ Position closed; PnL recorded
   ▼
[Learning Engine] (offline, weekly)          [Phase 6]
   │ aggregate by regime × strategy × time-of-day
   │ feedback into Opportunity Ranking weights
```

## Provenance

Every row downstream of a tick references its upstream sources via FK:

```
opportunities ◄ ai_decisions
opportunities ◄ strategy_signals ◄ risk_decisions ◄ orders ◄ executions
                                                          ◄ slippage_log
                                                                    └► positions
```

This makes post-mortems on any trade reduce to a chain of joins, not log archaeology.

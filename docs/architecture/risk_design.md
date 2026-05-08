# Risk Engine — Detailed Design

> Status: Phase 3 will implement. This doc defines what Phase 3 must build.

The Risk Engine is the **only** path from a `TradeIntent` to the `Execution Engine`. There is no other entry point. If the Risk Engine is bypassed, that's a bug, not a feature.

## The 16 deterministic checks

Run in this exact order. **Fail-closed at first failure.**

| # | Check | Source | Failure code |
|---|---|---|---|
| 1 | Live-trading 3-lock | env, file, DB | `LIVE_NOT_AUTHORIZED` |
| 2 | Global kill switch | Redis | `KILL_SWITCH` |
| 3 | Market-hours / entry window | clock + risk.yaml | `OUTSIDE_WINDOW` |
| 4 | Capital available | broker funds + position book | `INSUFFICIENT_CAPITAL` |
| 5 | Daily loss cap | pnl_daily | `DAILY_LOSS_CAP` |
| 6 | Rolling drawdown cap | pnl_daily(window) | `DRAWDOWN_CAP` |
| 7 | Per-trade max risk | intent + risk.yaml | `PER_TRADE_RISK` |
| 8 | Max concurrent positions | positions(open) | `MAX_CONCURRENT` |
| 9 | Max trades/day | orders(today) | `MAX_TRADES_DAY` |
| 10 | Consecutive-loss lockout | positions(today) | `CONSECUTIVE_LOSSES` |
| 11 | Slippage-history kill | slippage_log(recent) | `SLIPPAGE_KILL` |
| 12 | Spread filter | live tick | `SPREAD_TOO_WIDE` |
| 13 | Liquidity filter | depth × volume | `LOW_LIQUIDITY` |
| 14 | Stale-data check | last tick age | `STALE_DATA` |
| 15 | Volatility kill | India VIX + intraday move | `VOL_KILL` |
| 16 | Broker health | last successful API ts | `BROKER_UNHEALTHY` |

Every check writes a `risk_decisions` row whether it approves or rejects. Rejections are valuable analytics — they tell us what we'd have traded vs. what we did trade.

## Position sizing

After all gates pass, sizing applies:

```
base_qty   = floor(per_trade_max_risk_pct * capital / (premium * lot_size))
confidence = min(opportunity_score, advisor_score)
sized_qty  = max(1, floor(base_qty * sqrt(confidence)))   # never zero
```

Then clamp by:
- max_concurrent_positions (already 1 in Phase 0 caps)
- broker margin requirement
- exchange-imposed lot limits

## Why deterministic, not learned

The Risk Engine is the part of the system that protects capital. Learned policies — even good ones — cannot offer the guarantees we need (a learned model can produce arbitrary outputs on out-of-distribution inputs). Deterministic gates are auditable, testable, and have provable upper bounds on bad outcomes.

The Opportunity Ranking and Strategy Engines can — and will — incorporate learning over time. The Risk Engine will not.

## Failure-mode matrix

| If this check raises an exception | Behavior |
|---|---|
| Any check (1–16) | Treat as REJECTION. Trip kill switch on UNHANDLED exceptions. |
| Live-trading 3-lock check raises | Hard-fail. Never proceed to broker. |
| Kill switch read fails (Redis down) | Treat as TRIPPED. Reject. |
| DB write of risk_decision fails | Reject. Without provenance, we don't trade. |

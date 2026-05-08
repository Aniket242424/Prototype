# Runbook — Global Kill Switch

The kill switch is a single Redis key (`kill_switch:global`). When set to `1`, the Risk Engine rejects all new entries and the Execution Engine attempts emergency exits on open positions. **Tripping is automated; resetting is manual.**

## Trip (operator)

```bash
curl -X POST http://localhost:8000/control/kill-switch/trip \
  -H 'Content-Type: application/json' \
  -d '{"reason":"manual halt","source":"operator"}'
```

## Inspect

```bash
curl http://localhost:8000/control/kill-switch
```

## Reset (operator only)

Resetting the kill switch is intentionally a separate action — it requires identifying yourself:

```bash
curl -X POST http://localhost:8000/control/kill-switch/reset \
  -H 'Content-Type: application/json' \
  -d '{"operator":"aniket"}'
```

## What auto-trips it (Phase 3+)

- Daily loss cap breached
- Rolling drawdown cap breached
- Slippage-history kill (N consecutive trades > threshold)
- India VIX > ceiling
- Broker disconnect > grace period
- Any unhandled exception in Risk Engine, Execution Engine, or Position Manager

## What does NOT auto-trip it

- A single losing trade
- Spread widening on one strike (per-instrument suppression instead)
- Stale tick (per-instrument suppression instead)

The kill switch is a sledgehammer — reserve it for system-wide problems.

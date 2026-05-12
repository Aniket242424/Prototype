# Phase 4 testing playbook

Phase 4 is the **Strategy + Position Manager + AI Advisor + Worker
Orchestrator** layer. It sits between Phase 2 (signal generation) and
Phase 3 (risk + execution), turning opportunities into actual paper trades
with disciplined exit management.

> Phase 4 ships paper-only. The live-trading 3-lock (Phase 3.1) remains
> closed; LiveBroker still raises on every method.

---

## Sub-phases

| Sub-phase | Component |
|---|---|
| 4.1 | Strategy framework + EMA Crossover Trend |
| 4.2 | ORB + strategy registry + config integration |
| 4.3 | Vol Expansion + Gap Continuation strategies |
| 4.4 | Position Manager + ExecutionEngine.emergency_exit() |
| 4.5 | Claude AI advisor (veto-only) |
| 4.6 | Phase 4 worker orchestrator + dashboard panels |

## 1. Automated unit tests

```powershell
py -3.14 -m pytest tests/unit/ -v
```

Expected: **all Phase 4 tests passing**. Per-module breakdown:

| File | Sub-phase | Tests |
|---|---|---|
| `test_ema_crossover_strategy.py` | 4.1 | 19 — every filter rejection + happy paths + invalidation |
| `test_strategy_registry.py` | 4.2 | 7 — flag-driven enable/disable, all-four wiring |
| `test_orb_strategy.py` | 4.2 | 17 — time-window, range data, breakout confirm, volume filter |
| `test_vol_expansion_strategy.py` | 4.3 | 14 — RV ratio, DI alignment, IV percentile, time window |
| `test_gap_continuation_strategy.py` | 4.3 | 13 — gap size, direction match, pullback, gap-filled detection |
| `test_position_rules.py` | 4.4 | 30 — R-multiple math, stop/target/chandelier/giveback/time rules |
| `test_position_manager.py` | 4.4 | 17 — state transitions, invalidation, kill switch, partial fills |
| `test_ai_advisor.py` | 4.5 | 14 — JSON parsing, fallbacks, code-fence handling, veto threshold |

## 2. Phase 4 worker smoke test (no live market needed)

The Phase 4 worker is the orchestrator that wires everything together. To
verify it runs end-to-end:

```powershell
# 1. Bring up infra (postgres + redis must be running)
docker compose up -d postgres redis

# 2. Ensure Phase 2 + Phase 1 workers are also running so signals/state exist
$env:PYTHONUNBUFFERED='1'
py -3.14 -u -m trading_agent.market_data.worker
# (in another terminal)
py -3.14 -u -m trading_agent.regime.worker
# (in another terminal)
py -3.14 -u -m trading_agent.strategy.worker
```

What to verify after a few minutes of market hours:
- Dashboard at http://localhost:8000/dashboard shows:
  - **Phase 4 worker: running** in the AI advisor panel
  - Recent strategy signals appearing as they fire
  - Risk decisions appearing for each signal (approved or rejected)
  - Recent orders + fills (paper)
  - Open positions detail (with stop/target/entry premium)
  - AI advisor decisions with rationale
- Worker log shows the full pipeline: `signal_emitted` → `advisor_decision` →
  `risk_rejected`/`risk_approved` → (if approved) `position_opened` →
  (eventually) `exit_filled`

## 3. End-to-end paper-trade smoke (works off-hours)

The Phase 3 smoke test (`scripts/phase3_smoke_test.py`) covers the
synthetic-trade path through Risk + Execution. Phase 4 adds the Strategy
Engine + Position Manager on top. For full E2E off-hours:

```powershell
# Run Phase 3 smoke (verifies Risk + Execution) — works off-hours with bypass
py -3.14 scripts/phase3_smoke_test.py --bypass-time-checks --inject-staleness --underlying SENSEX --premium 80
```

This confirms the Risk + Execution layer that Phase 4 worker calls into.
The Phase 4 worker itself can only be smoke-tested DURING market hours
(09:15-15:30 IST) because it consumes real opportunities from Phase 2,
which requires real tick data.

## 4. Manual verification scenarios

### Scenario A — Strategy filter rejection
1. Start all four workers during market hours
2. Watch dashboard for "no signal" cycles
3. Worker log should show `strategy.failed` or filter rejection codes
4. Phase 2 may still emit an opportunity but no strategy fires

### Scenario B — AI advisor veto
- If Claude API is configured, watch the advisor decisions panel
- A NO_TRADE decision OR advisor_score < 0.55 should result in
  `advisor_vetoed` log line + no order placement

### Scenario C — Position management
1. Once any paper position opens, watch the dashboard "Open positions" panel
2. As underlying moves toward target, look for:
   - `moved_to_breakeven` log line at +1R
   - `transition_partial_trail` log line at +1.5R (and partial exit fires)
3. As underlying moves toward stop, look for:
   - Exit at `HARD_STOP_HIT` / `BREAKEVEN_STOP_HIT` / `CHANDELIER_TRAIL_HIT`

### Scenario D — Forced 15:15 exit
- Any open positions at 15:15 IST will be force-exited automatically
- Worker log: `FORCED_TIME_EXIT` trigger
- All positions flat by close

## 5. DB queries

```powershell
# Today's signals by strategy
docker compose exec -T postgres psql -U trading_agent -d trading_agent -c "
SELECT strategy_name, COUNT(*)
FROM strategy_signals
WHERE ts >= CURRENT_DATE
GROUP BY strategy_name;"

# Today's AI advisor decisions
docker compose exec -T postgres psql -U trading_agent -d trading_agent -c "
SELECT decision, COUNT(*),
       ROUND(AVG(advisor_score)::numeric, 2) AS avg_score
FROM ai_decisions
WHERE ts >= CURRENT_DATE
GROUP BY decision;"

# Open positions and their stops
docker compose exec -T postgres psql -U trading_agent -d trading_agent -c "
SELECT id, underlying, direction, qty,
       avg_entry_price, initial_stop, target,
       is_paper, opened_at
FROM positions
WHERE is_open = TRUE
ORDER BY opened_at DESC;"

# Today's exits with PnL
docker compose exec -T postgres psql -U trading_agent -d trading_agent -c "
SELECT id, underlying, direction, qty,
       avg_entry_price, avg_exit_price,
       pnl_inr,
       closed_at - opened_at AS hold_time
FROM positions
WHERE closed_at >= CURRENT_DATE
ORDER BY closed_at DESC;"
```

## 6. Pre-merge checklist for promoting `phase-4` → `trading-agent`

- [ ] All unit tests pass (`pytest tests/unit/`)
- [ ] Phase 3 smoke test still works off-hours
- [ ] Phase 4 worker starts cleanly and writes heartbeat
- [ ] Dashboard shows Phase 4 panels correctly populated
- [ ] At least one full market session of Phase 4 worker running with at
      least 1 risk decision recorded (approved or rejected)
- [ ] Live-trading status still shows `authorized: false`
- [ ] CI green on the head commit of `phase-4`

## 7. What's still NOT validated

| Limitation | Resolution |
|---|---|
| Smart strike selector wired in Phase 4 worker | Currently uses placeholder strike — Phase 4.7 / Phase 5 work |
| Real chain-based bid/ask | Engine uses synthetic ±1-tick spread — Phase 5 |
| Backtested edge | No backtest engine yet — Phase 5 |
| Real broker behavior | Paper only — Phase 6 + live cutover decision |
| 30+ days of validated paper trading | Run system to accumulate data |

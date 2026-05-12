# Phase 3 testing playbook

How to verify the Risk Engine + Execution Engine work correctly. Covers
automated tests, manual smoke tests (off-hours and live), specific
rejection-scenario tests, and DB sanity queries.

> **Phase 3 = Risk Engine (Phase 3.1) + Execution Engine (Phase 3.2).** It
> ships paper-only execution. Live trading is gated behind the 3-lock —
> if any check fails, the system stays in paper mode regardless of which
> branch is deployed.

---

## 1. Automated unit tests (the floor)

```powershell
cd "c:\Users\91996\Documents\Aniket Latest\Trading_Agent"
py -3.14 -m pytest tests/unit/ -v
```

Expected: **105/105 passing**. Sub-suites that specifically cover Phase 3:

| File | What it covers |
|---|---|
| `tests/unit/test_risk_dtos.py` | TradeIntent, Leg, RiskDecision, Fill, ExecutionResult contracts |
| `tests/unit/test_sizer.py` | Position sizing: budget fit, confidence weighting, lot-multiple, safety cap |
| `tests/unit/test_strike_selector.py` | ATM/ITM1/ITM2 selection by regime, IV, capital, liquidity |
| `tests/unit/test_stock_gates.py` | Earnings blackout, sector cap, top-30 whitelist |
| `tests/unit/test_execution_state_machine.py` | Order state transitions; invalid transitions raise |
| `tests/unit/test_slippage.py` | Pre-flight estimation + realized bps math |
| `tests/unit/test_paper_broker.py` | Paper broker fills, cancels, partial behavior |

Unit tests run in ~3 seconds, no DB or Redis needed. **CI on every push.**

---

## 2. End-to-end smoke test (manual, deterministic)

The script `scripts/phase3_smoke_test.py` runs a **synthetic TradeIntent**
through the Risk Engine, and if approved, through the Execution Engine in
paper mode. Prints every decision detail. Use it to verify the wiring
works without waiting for a real opportunity to fire.

### Prerequisites

```powershell
# Docker stack running
docker compose up -d postgres redis

# Token valid (run if needed)
py -3.14 scripts/upstox_auth_cli.py

# Verify
py -3.14 -c "from trading_agent.infrastructure.db import session_scope; print('db ok')"
```

### Run modes

**Default — what most rejections look like off-hours:**
```powershell
py -3.14 scripts/phase3_smoke_test.py
```
Expected output includes a rejection with code `MARKET_CLOSED` or
`OUTSIDE_WINDOW` if you're not in the 09:20–14:30 IST window.

**During market hours — full approval + paper fill:**
```powershell
py -3.14 scripts/phase3_smoke_test.py --underlying SENSEX --premium 80
```
Expected: `decision: True`, `code: OK_PAPER`, sized lots > 0, an
ExecutionResult with `status: FILLED` and a paper fill at the synthetic
price.

**Demo specific rejections:**

| Scenario | Command | Expected code |
|---|---|---|
| Kill switch tripped | `--trip-kill-switch` | `KILL_SWITCH` |
| Stale data | (run off-hours, no `--inject-staleness`) | `STALE_DATA` |
| Outside market hours | (run after 15:30 IST) | `MARKET_CLOSED` |
| Outside entry window | (run 14:30–15:30 IST during market) | `OUTSIDE_WINDOW` |
| Premium too rich for cap | `--underlying NIFTY --premium 200` | `PER_TRADE_RISK` (1 lot × ₹200 × 25 = ₹5,000 > ₹1,500 cap) |
| Per-leg outlay exceeds capital | extreme premium values | `INSUFFICIENT_CAPITAL` |

The smoke test resets any kill switch it tripped before exiting.

**Skip execution (Risk Engine only):**
```powershell
py -3.14 scripts/phase3_smoke_test.py --skip-execution
```
Useful when you want to inspect risk decisions without simulating fills.

### Reading the output

The script prints 5 sections per run:

1. **Environment** — current time, capital, env flags
2. **Live-trading 3-lock check** — three locks individually + overall
3. **Kill switch** — current state
4. **Synthetic TradeIntent** — what we're proposing to trade
5. **Risk Engine evaluation** — the decision + full inputs snapshot
6. **Execution Engine** (if approved) — paper fill details + slippage

If you see a rejection you didn't expect, the `inputs snapshot` shows
exactly which check fired and the values it saw.

---

## 3. Live-market verification (during 09:20–14:30 IST)

Smoke test gives synthetic data; live verification confirms the full
pipeline works against real ticks.

### Step-by-step

```powershell
# Terminal 1 — control plane + dashboard
py -3.14 -m uvicorn trading_agent.api.main:app --port 8000

# Terminal 2 — market data worker (ticks, chain, VIX)
$env:PYTHONUNBUFFERED='1'; $env:PYTHONIOENCODING='utf-8'
py -3.14 -u -m trading_agent.market_data.worker

# Terminal 3 — regime + intel + opportunity worker
$env:PYTHONUNBUFFERED='1'; $env:PYTHONIOENCODING='utf-8'
py -3.14 -u -m trading_agent.regime.worker

# Terminal 4 — smoke test (after market data warmed up ~5 min)
py -3.14 scripts/phase3_smoke_test.py --underlying SENSEX --premium 80
```

Expected end-to-end: TradeIntent → 16 Risk checks all pass → sized 1 lot
SENSEX → paper LIMIT placed at mid → fills in 50-150ms → realized
slippage logged. Dashboard at http://localhost:8000/dashboard shows
the trade.

---

## 4. Database sanity queries

After running smoke tests (or live trades), verify rows landed correctly:

```powershell
# Recent risk decisions (approve + reject)
docker compose exec -T postgres psql -U trading_agent -d trading_agent -c "
SELECT to_char(ts, 'HH24:MI:SS') AS at,
       approved, code,
       sized_qty,
       LEFT(reason, 60) AS reason
FROM risk_decisions
ORDER BY ts DESC LIMIT 10;"
```

```powershell
# Recent orders + fills
docker compose exec -T postgres psql -U trading_agent -d trading_agent -c "
SELECT o.id, o.instrument_key, o.side, o.order_type,
       o.qty, o.limit_price, o.status, o.is_paper,
       COUNT(e.id) AS fill_count
FROM orders o
LEFT JOIN executions e ON e.order_id = o.id
GROUP BY o.id
ORDER BY o.id DESC LIMIT 10;"
```

```powershell
# Slippage analytics
docker compose exec -T postgres psql -U trading_agent -d trading_agent -c "
SELECT order_id,
       estimated_slippage_bps AS est,
       realized_slippage_bps  AS real,
       realized_slippage_bps - estimated_slippage_bps AS drift,
       spread_bps_at_entry
FROM slippage_log
ORDER BY ts DESC LIMIT 10;"
```

```powershell
# Open positions
docker compose exec -T postgres psql -U trading_agent -d trading_agent -c "
SELECT id, underlying, direction, qty, avg_entry_price,
       initial_stop, target, is_paper,
       opened_at
FROM positions
WHERE is_open = TRUE
ORDER BY opened_at DESC;"
```

---

## 5. Live-trading 3-lock verification (paper-mode safety net)

This is the most important safety check. The system must REFUSE to send
real orders unless all three locks are satisfied.

```powershell
# Check current lock state
curl http://localhost:8000/control/live-trading-status
```

Expected JSON:
```json
{
  "authorized": false,
  "locks": {
    "env_LIVE_TRADING": false,
    "file_present": true,
    "db_sha_matches": false
  },
  ...
}
```

**`authorized: false` is correct.** It means even if Phase 4 Strategy
Engine emits a perfectly valid trade today, the Execution Engine will
route to PaperBroker, never LiveBroker. The LiveBroker class itself
raises ExecutionError on every method — a defense-in-depth layer
beyond the lock check.

**Do NOT sign the acknowledgment yet.** That's a Phase 5+ step after
backtest validation.

---

## 6. Specific test scenarios to run before merging to `trading-agent`

These are the smoke-test runs that should pass cleanly before this
branch is promoted to production-ready:

- [ ] `py -3.14 -m pytest tests/unit/` → 105 passed
- [ ] `py -3.14 scripts/phase3_smoke_test.py --skip-execution` → produces a rejection or approval with full snapshot
- [ ] `py -3.14 scripts/phase3_smoke_test.py --trip-kill-switch` → rejected with code `KILL_SWITCH`
- [ ] During market hours: full approval + paper fill flow on SENSEX
- [ ] During market hours: `--underlying NIFTY --premium 200` rejects with `PER_TRADE_RISK`
- [ ] Dashboard `/control/live-trading-status` returns `authorized: false`
- [ ] `risk_decisions` table has rows for every smoke test run
- [ ] `orders`, `executions`, `positions`, `slippage_log` tables have rows for every approved + executed smoke test
- [ ] CI on GitHub is green for the head commit on `phase-3` branch

---

## 7. What this testing CANNOT tell you

Some things the test suite is structurally unable to validate:

| Limitation | Why | Resolution |
|---|---|---|
| Whether the system is profitable | No backtest engine yet | Phase 5 |
| Whether thresholds are calibrated correctly | No historical data | Phase 5 walk-forward |
| Real broker behavior (rejections, latency, partial fills under load) | Paper broker is a simplification | Live with tiny capital, post-Phase-5 |
| Risk gates under extreme conditions (circuit, gap-through-stop) | Tests use synthetic clean data | Live observation over months |
| Multi-leg atomic execution | Multi-leg strategies don't ship until Phase 8 | Phase 8 |

These are **known limitations**, not bugs. The current testing surface
validates that the **plumbing is correct** — not that the strategy
has edge.

---

## 8. Quick reference

| Goal | Command |
|---|---|
| Run all unit tests | `py -3.14 -m pytest tests/unit/` |
| Smoke-test Risk + Execution (synthetic) | `py -3.14 scripts/phase3_smoke_test.py` |
| Smoke-test rejection paths | `--trip-kill-switch`, `--underlying NIFTY --premium 200`, run off-hours |
| Bring up infra | `docker compose up -d postgres redis` |
| Refresh Upstox token (daily) | `py -3.14 scripts/upstox_auth_cli.py` |
| Check live-trading status | `curl http://localhost:8000/control/live-trading-status` |
| View risk decisions | DB query in section 4 |
| View paper fills | DB query in section 4 |
| Dashboard | http://localhost:8000/dashboard |

# Trading_Agent — Architecture

**Authoritative design document.** All implementation decisions trace back here. If code disagrees with this document, fix the code or update this document — never let them diverge silently.

**Version:** 0.1 (Phase 0)
**Owner:** Aniket
**Last revised:** 2026-05-09

---

## 1. North-star principles

These are non-negotiable. Every module is designed to honor them; reviews reject changes that violate them.

| # | Principle | Concrete consequence |
|---|---|---|
| 1 | **Survivability > returns** | Daily-loss kill switch fires before any "comeback" trade. No martingale, no averaging losers. |
| 2 | **Determinism > intelligence** | Risk and execution gates are deterministic Python. Claude is an *advisor*; it cannot bypass risk caps or place orders. |
| 3 | **Slippage is a first-class cost** | Pre-trade slippage estimation gates entry. Realized vs. estimated slippage is logged per fill and feeds a kill switch. |
| 4 | **Liquidity is a hard filter** | Spread, depth, and OI thresholds reject trades before strategy logic even sees them. |
| 5 | **Trade less, but better** | Hard cap on trades/day. Consecutive-loss lockout. Choppy-regime suppression. |
| 6 | **Three locks for live** | Live orders require `LIVE_TRADING=true` AND `ACKNOWLEDGMENT.md` present AND its SHA recorded in DB. |
| 7 | **Every decision is auditable** | Every order has a traceable lineage: tick → regime → opportunity → AI score → risk decision → execution. Stored immutably. |
| 8 | **Fail closed** | Broker disconnect, stale data, kill-switch trip, or any unhandled exception → halt new entries, not "continue best-effort". |

---

## 2. System topology

```
                              ┌────────────────────────┐
                              │    Upstox API (REST)   │
                              └───────────┬────────────┘
                                          │
                    ┌─────────────────────┼─────────────────────┐
                    │                     │                     │
              ┌─────▼─────┐         ┌─────▼─────┐         ┌─────▼──────┐
              │  Auth /   │         │  Market   │         │ Execution  │
              │  Token    │         │  Data WS  │         │ Gateway    │
              │  Manager  │         │  Worker   │         │ (orders)   │
              └─────┬─────┘         └─────┬─────┘         └─────┬──────┘
                    │                     │                     │
                    │              ┌──────▼──────┐              │
                    │              │   Redis     │              │
                    │              │ (live state │              │
                    │              │  + pubsub)  │              │
                    │              └──────┬──────┘              │
                    │                     │                     │
        ┌───────────┴─────────────────────┼─────────────────────┴────────────┐
        │                                 │                                  │
   ┌────▼────┐  ┌──────────┐  ┌──────────▼──────────┐  ┌───────────┐  ┌─────▼─────┐
   │ Regime  │  │Opportunity│  │ Options Intel      │  │ Strategy  │  │ Risk      │
   │ Engine  │  │ Ranking   │  │ (Greeks, IV rank,  │  │ Engine    │  │ Engine    │
   │         │  │ Engine    │  │  PCR, max pain)    │  │           │  │ (gate)    │
   └────┬────┘  └─────┬─────┘  └─────────┬──────────┘  └─────┬─────┘  └─────┬─────┘
        │             │                  │                    │              │
        └─────────────┴──────────┬───────┴────────────────────┘              │
                                 │                                            │
                          ┌──────▼────────┐                                   │
                          │ AI Reasoning  │ ◄───── Claude API (advisor only)  │
                          │   (advisor)   │                                   │
                          └──────┬────────┘                                   │
                                 │                                            │
                                 └────────────────► Risk Engine ◄─────────────┘
                                                         │
                                                  ┌──────▼──────┐
                                                  │ Execution   │
                                                  │ Engine      │
                                                  │ (slippage-  │
                                                  │  aware)     │
                                                  └──────┬──────┘
                                                         │
                                                  ┌──────▼──────┐
                                                  │  Postgres   │
                                                  │ (audit log) │
                                                  └─────────────┘
```

**Process model (Phase 1+):** each "Worker" above is an independent asyncio process started by Docker Compose. They communicate via Redis pubsub for live signals and Postgres for durable state. The FastAPI control plane (`api`) exposes health, kill-switch, and operational endpoints. **Phase 0 ships only the control plane and shared infrastructure.**

---

## 3. Module contracts

Every module exposes a small, typed Pydantic interface. These are the seams — change a module's internals freely; change its contract carefully.

### 3.1 Market Data Engine (Phase 1)
**Responsibility:** ingest live ticks, options chain snapshots, India VIX, and order-book depth from Upstox; persist to Postgres; publish to Redis.

**Outputs (Redis pubsub channels):**
- `md:tick:{instrument_key}` — `Tick(ts, ltp, bid, ask, bid_qty, ask_qty, volume, oi)`
- `md:chain:{underlying}` — `ChainSnapshot(ts, underlying_spot, expiry, strikes[])`
- `md:vix` — `VIX(ts, value)`

**Resilience:** auto-reconnect with exponential backoff; gap-detection (sequence number / timestamp); stale-data flag (no tick > 5s during market hours → mark instrument stale, suppress trading on it).

### 3.2 Regime Engine (Phase 2)
**Responsibility:** classify each underlying into one of `{TREND_UP, TREND_DOWN, RANGE, CHOPPY, VOL_EXPANSION, VOL_COMPRESSION, EVENT_DRIVEN}` every 30s.

**Inputs:** ATR(14), ADX(14), realized vol (5/15/60min windows), VWAP deviation, India VIX delta, futures basis, breadth (advances/declines for the index constituents — Phase 2.5).

**Output:** `RegimeState(underlying, regime, confidence, components{...}, ts)`. Persisted to `regime_states` table; published on `regime:{underlying}`.

**Hard rule:** if regime is `CHOPPY` or `VOL_COMPRESSION`, opportunity scoring caps at 0.3 and Risk Engine rejects all option-buying signals.

### 3.3 Options Intelligence Engine (Phase 2)
**Responsibility:** compute Greeks (Black-Scholes with realized IV), IV rank, IV percentile, PCR, max pain, OI buildup classification (long/short buildup, short covering, call/put writing), and gamma-squeeze probability.

**Inputs:** options chain snapshot + 30-day IV history.

**Outputs:** per-strike enriched chain, plus underlying-level summary metrics. Cached in Redis; persisted on snapshot intervals (every 1 minute during market hours).

### 3.4 Opportunity Ranking Engine (Phase 2)
**Responsibility:** continuously score the 5 underlyings on 9 dimensions and emit at most ONE active opportunity at a time.

**Score dimensions (each 0–1, weighted):**
- momentum quality (price vs. VWAP, EMA stack, persistence)
- volatility expansion probability (realized-vol acceleration vs. IV)
- liquidity quality (depth × volume × OI)
- spread tightness (bid/ask spread bps)
- slippage risk (estimated from spread + depth)
- IV conditions (favors sub-50 IV percentile for buying)
- regime favorability (from Regime Engine)
- trend quality (ADX × directional persistence)
- risk-reward profile (estimated R:R given strike + stop)

**Output:** `Opportunity(underlying, direction, score, components{}, recommended_expiry, recommended_strike_band, ts)`. Only the highest-scoring opportunity above threshold (default 0.65) is forwarded to the Strategy Engine.

### 3.5 Strategy Engine (Phase 4)
**Responsibility:** translate an Opportunity into a concrete `TradeIntent` (which strike, which expiry, target premium, stop, target).

**Strategies (Phase 4):**
1. Momentum breakout
2. Trend continuation
3. Volatility expansion
4. Gap continuation
5. Event-driven momentum

Each strategy implements a common interface:
```python
class Strategy(Protocol):
    name: str
    def evaluate(self, opp: Opportunity, intel: OptionsIntel) -> TradeIntent | None: ...
    def invalidation(self, intent: TradeIntent, tick: Tick) -> bool: ...
```

**Strike selection bias:** ATM or 1-strike ITM, never far OTM. Liquidity score must exceed `config.strategies.min_liquidity_score`.

### 3.6 AI Reasoning Engine (Phase 4)
**Responsibility:** ask Claude to score the setup quality, flag dangerous trades, and recommend `CALL | PUT | NO_TRADE`. Output is structured JSON.

**Input prompt** receives only structured data (regime state, opportunity components, intel summary, recent trade outcomes for context). No raw price text.

**Output schema (strict):**
```json
{
  "decision": "CALL" | "PUT" | "NO_TRADE",
  "confidence": 0.0..1.0,
  "rationale": "short text",
  "warnings": ["list of risks observed"],
  "advisor_score": 0.0..1.0
}
```

**Hard rule:** `advisor_score < 0.55` → veto. `decision: NO_TRADE` → veto. AI veto is **one-way**: AI cannot upgrade a trade the deterministic stack rejected, only veto one it accepted.

### 3.7 Risk Engine (Phase 3) — *the most important module*
**Responsibility:** the gate. Every order proposal passes through `RiskEngine.evaluate(intent) -> RiskDecision`. There is no other path to the Execution Engine.

**Deterministic checks (in order — fail-closed at first failure):**
1. Live-trading 3-lock check (env, file, DB-SHA).
2. Global kill-switch check (Redis key `kill_switch:global`).
3. Market-hours check (configurable entry window, e.g. 09:20–14:30 IST).
4. Capital available (cash buffer, margin requirement).
5. Daily loss cap (`config.risk.daily_max_loss_pct`).
6. Drawdown cap (rolling 5-day).
7. Per-trade risk cap (premium × lot × lots ≤ `per_trade_max_risk_pct` × capital).
8. Max concurrent positions.
9. Max trades/day.
10. Consecutive-loss lockout.
11. Slippage-history kill switch.
12. Spread filter (live spread bps ≤ `config.risk.max_spread_bps`).
13. Liquidity filter (depth × volume ≥ threshold).
14. Stale-data check (last tick age ≤ 5s).
15. Volatility kill switch (India VIX above ceiling, or >X% intraday move).
16. Broker connection health (last successful Upstox call ≤ 30s).

**Output:** `RiskDecision(approve: bool, reason: str, sized_qty: int, max_premium: float, ...)`. Persisted with full input snapshot.

**Position-sizing rule:** Kelly-fraction-capped, but with hard floor at `per_trade_max_risk_pct`. Confidence-weighted: `qty = base_qty * min(opportunity.score, advisor_score) ** 0.5`.

### 3.8 Low-Slippage Execution Engine (Phase 3)
**Responsibility:** convert an approved RiskDecision into actual broker fills with minimal slippage.

**Pre-flight (per order):**
- Recompute live spread, depth, slippage estimate.
- If estimated slippage > `config.execution.max_slippage_bps`, abort and re-queue once; second abort → notify and skip.
- Decide order type: prefer LIMIT at midpoint or 1-tick on the maker side; fall back to LIMIT-IOC near the touch if depth is thin; MARKET only as emergency exit.

**Execution loop (per order):**
1. Place LIMIT at chosen price.
2. Monitor for `config.execution.fill_timeout_ms` (default 2000ms).
3. If unfilled, decide: improve price by 1 tick (up to N times), or cancel + re-evaluate.
4. On partial fill, hold remainder up to `fill_timeout_ms × 2`, then cancel.
5. On final fill, log realized slippage = (fill_vwap − reference_mid) / reference_mid.

**Emergency exit:** kill-switch trip OR stop-loss trigger → MARKET sell across exchange-available depth, log slippage and continue.

**Fail-closed:** any unhandled exception in the execution loop → place an immediate emergency exit if a position is open, then halt new entries.

### 3.9 Position Management (Phase 4)
**Responsibility:** while a position is open, manage stop and target dynamically.

- **Initial stop:** placed as a logical stop in `positions` table (NOT a broker stop-loss order — Indian options stop-loss orders behave poorly during fast moves; we monitor in code and fire MARKET exits).
- **Trailing logic:** ATR-based trail on the underlying, translated to option premium via delta.
- **Partial profit:** at +50% premium, exit half; trail the rest.
- **Theta cutoff:** if entered intraday, force exit by 15:15 IST (pre-close).
- **Expiry safeguard:** no holding overnight on expiry day.

### 3.10 Backtesting Engine (Phase 5)
**Responsibility:** replay historical ticks + chains through the full pipeline (Regime → Opportunity → Strategy → Risk → simulated Execution) with realistic fills.

- Slippage model: spread-based + depth-impact + volatility-conditional noise.
- Latency simulation: configurable round-trip delay between signal and order acknowledgment.
- Walk-forward: train on rolling 90-day window, validate on next 30 days.
- Monte Carlo on trade-sequence variance.

### 3.11 Learning Engine (Phase 6)
**Responsibility:** post-hoc analysis. Identify which (regime × strategy × time-of-day) cells outperform and feed weights back into the Opportunity Ranking Engine.

**Not online learning** — periodic offline retraining only, with human approval before promoting weights.

### 3.12 Monitoring & Alerting (Phase 6)
- Per-component liveness (heartbeat into Redis every 5s).
- PnL dashboard (FastAPI + simple frontend).
- Telegram + Discord alerts for: trade events, risk-engine rejections (rate), kill-switch trips, broker disconnects, slippage anomalies, token expiry warning.

---

## 4. Data flow — one trade end-to-end

```
1.  WS tick arrives at Market Data worker
       └─► persisted to market_data_ticks
       └─► published on md:tick:NIFTY_INDEX
       └─► published on md:chain:NIFTY (if it triggers a snapshot)

2.  Regime Engine subscriber recomputes regime every 30s
       └─► publishes regime:NIFTY = TREND_UP

3.  Options Intel updates Greeks, IV rank, OI buildup
       └─► publishes intel:NIFTY

4.  Opportunity Ranking re-scores all 5 underlyings on each regime/intel update
       └─► emits Opportunity(underlying=NIFTY, score=0.71, direction=long)

5.  Strategy Engine consumes Opportunity, picks Momentum-Breakout
       └─► proposes TradeIntent(NIFTY 24600 CE, 1 lot, stop=-30%, target=+60%)

6.  AI Reasoning is invoked with structured context
       └─► returns {decision: CALL, confidence: 0.68, advisor_score: 0.71}

7.  Risk Engine runs 16 deterministic checks
       └─► RiskDecision(approve=true, sized_qty=1, max_premium=180.50)

8.  Execution Engine places LIMIT at mid; fills in 240ms; logs slippage 8bps
       └─► persists order, execution, position rows
       └─► publishes position:open on Redis

9.  Position Manager attaches dynamic stop/target
       └─► monitors tick stream; trails on ATR

10. Exit triggers (target / stop / theta cutoff / kill switch)
       └─► Execution Engine emergency-exits; PnL recorded; position closed
       └─► Learning Engine logs outcome with full provenance
```

---

## 5. Database schema (Postgres) — Phase 0

Schema lives in `src/trading_agent/infrastructure/models.py`; migration `alembic/versions/0001_initial_schema.py`.

| Table | Purpose | Notes |
|---|---|---|
| `users` | single operator (multi-user is out of scope) | seeded on first migration |
| `tokens` | encrypted Upstox access/refresh tokens | Fernet-encrypted at rest |
| `acknowledgment_log` | live-trading SHA-256 acks (lock #3) | append-only |
| `instruments` | NIFTY/BANKNIFTY/FINNIFTY/SENSEX/BANKEX configs | lot size, tick size, exchange code, expiry day |
| `market_data_ticks` | tick stream (Phase 1) | TimescaleDB candidate; Phase 1 will partition by day |
| `options_chain_snapshots` | full chain at snapshot intervals | one row per (underlying, expiry, ts) with strikes JSONB |
| `india_vix` | India VIX series | |
| `regime_states` | regime classifications | one row per (underlying, ts) |
| `opportunities` | ranked candidates | provenance for every trade |
| `ai_decisions` | Claude advisor outputs | full prompt + response stored |
| `risk_decisions` | risk-engine evaluations | every approve and every reject |
| `orders` | order lifecycle | NEW → SENT → PARTIAL → FILLED / CANCELLED / REJECTED |
| `executions` | individual fills | one order may have multiple |
| `positions` | open + closed positions | links to orders |
| `pnl_daily` | daily realized + unrealized snapshot | end-of-day aggregation |
| `slippage_log` | per-execution slippage analytics | feeds slippage kill switch |
| `strategy_signals` | strategy-emitted intents | even ones risk rejected |
| `kill_switch_events` | history of kill-switch trips | who/what/why |
| `audit_log` | immutable narrative log | append-only; one row per significant event |

**Why immutable audit_log + per-decision tables:** when a trade goes wrong (or right), we want to reconstruct exactly why. Storing the regime snapshot, opportunity score components, AI prompt+response, and risk-decision inputs each in their own table makes post-mortems straightforward without spelunking through logs.

---

## 6. Failure modes and responses

| Failure | Detection | Response |
|---|---|---|
| WS disconnect | heartbeat / no tick > 5s | reconnect with backoff; mark instrument stale; Risk Engine rejects entries on stale instruments |
| Token expires (3:30 AM IST daily) | API 401 | trigger token-manager refresh; if refresh fails, halt new entries and alert |
| Postgres down | connection error | retry with backoff; halt new entries (cannot persist provenance); existing positions still managed in-memory |
| Redis down | connection error | halt new entries; degraded mode — direct DB writes; alert |
| Claude API down | timeout / 5xx | proceed without AI advisor (downgrade `advisor_score` to 0.5 default); never block trading on AI |
| Broker order rejected | API response | log, alert; no automatic retry on rejected orders (could indicate underlying issue) |
| Spread blowout mid-trade | tick monitor | if open position spread > 2× entry spread, downgrade exit to MARKET |
| Unhandled exception in any engine | global handler | halt new entries; if position open, attempt emergency exit; alert |
| Power/host loss | external | positions remain at broker; daily caps protect; operator recovery via runbook |

---

## 7. Configuration layout

`.env` — secrets only (API keys, DB password, encryption key, `LIVE_TRADING` flag, capital).
`config/instruments.yaml` — per-underlying metadata (lot size, tick, expiry day, exchange).
`config/risk.yaml` — risk caps as % of capital.
`config/strategies.yaml` — strategy enable/disable, per-strategy thresholds (Phase 4).

Pydantic-Settings loads `.env` and the YAML files together, with strict validation. Misconfig → app fails to start. Never run with defaults silently.

---

## 8. Phase plan (summary)

| Phase | Modules | Exit criteria |
|---|---|---|
| 0 | Scaffold, DB, Docker, Auth, Kill switch | `docker compose up`, `make migrate`, `make auth`, `make probe` all green |
| 1 | Market Data Engine | live ticks + chain snapshots persisting; gap detection working |
| 2 | Regime + Opportunity + Options Intel | one Opportunity emitted per ranking pass during market hours |
| 3 | Risk Engine + Low-Slippage Execution | paper-mode E2E trades flowing; slippage analytics computed |
| 4 | Strategy + Position + AI Reasoning | full E2E paper PnL; Claude veto pathway exercised |
| 5 | Backtesting + Walk-forward + Monte Carlo | 12 months of out-of-sample backtest with realistic slippage |
| 6 | Monitoring + Learning + Live cutover | live-trading 3-lock validated; alerts firing; weekly report generated |

---

## 9. Out of scope (intentional)

- Equity / futures / commodities. Options on indices only.
- Multi-broker. Upstox only. (Adapter pattern in `execution/` makes future broker addition possible but not pursued.)
- Multi-tenant / multi-user.
- Selling / writing options. Buying only.
- Spreads / multi-leg strategies. Single-leg only. (Multi-leg is a Phase 7+ extension.)
- Pre-market / post-market trading.
- Web UI beyond a minimal monitoring dashboard.

---

## 10. Open questions / known limitations

1. **TimescaleDB vs. plain Postgres for tick data.** Plain Postgres in Phase 0 for simplicity; revisit if tick-rate ingestion becomes IO-bound. Decision: defer to Phase 1 once we measure real ingestion rates.
2. **Multi-process orchestration.** Phase 0 ships a single FastAPI process. Phase 1 splits into worker processes via Compose service definitions. Considered Celery; rejected as heavyweight for our use case — asyncio + Redis pubsub is sufficient.
3. **Claude prompt token budget.** Calling Claude on every Opportunity is expensive. Phase 4 will batch evaluations (one call per minute, scoring all candidates) and use prompt caching aggressively.
4. **India VIX is computed off NIFTY options.** It's not a perfect risk gauge for BANKNIFTY/SENSEX. Phase 2 will add per-underlying realized-vol fallback.
5. **Daily token re-auth is interactive.** Until we wire up an automated headless flow, the operator runs `make auth` every morning. This is a known operational tax.

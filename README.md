# Trading_Agent

Autonomous institutional-grade options-buying platform for Indian index derivatives (NIFTY, BANKNIFTY, FINNIFTY, SENSEX, BANKEX) on the Upstox API.

> **Status:** Phase 0 (foundation). No live order logic is wired yet. See [ARCHITECTURE.md](ARCHITECTURE.md) for the full design and phase plan.

---

## What this is

A modular, asyncio-based trading platform whose stated priorities are:

1. Survivability
2. Execution quality
3. Slippage minimization
4. Controlled drawdown
5. Risk-adjusted return
6. Adaptability

It is **not** a high-frequency scalper, a martingale grid, or a "buy every breakout" bot. It is designed to trade *less* but *better*: fewer trades, higher quality, deterministic risk controls.

## Repo layout (Phase 0)

```
trading_agent/
├── ARCHITECTURE.md            # Authoritative design document
├── ACKNOWLEDGMENT.md          # Live-trading risk acknowledgment (must be signed)
├── docker-compose.yml         # Postgres + Redis + app
├── Dockerfile
├── pyproject.toml
├── requirements.txt
├── .env.example
├── alembic.ini
├── alembic/                   # DB migrations
├── config/                    # YAML config (instruments, risk caps, strategies)
├── scripts/                   # CLI utilities (auth, capability probe, acknowledge)
├── src/trading_agent/
│   ├── core/                  # config, logging, kill switch, time/IST utils
│   ├── auth/                  # Upstox OAuth2 + daily token refresh
│   ├── infrastructure/        # SQLAlchemy + Redis clients, ORM models
│   ├── api/                   # FastAPI app (health, control plane)
│   ├── market_data/           # Phase 1 — stubbed
│   ├── regime/                # Phase 2 — stubbed
│   ├── opportunity/           # Phase 2 — stubbed
│   ├── options_intel/         # Phase 2 — stubbed
│   ├── risk/                  # Phase 3 — stubbed
│   ├── execution/             # Phase 3 — stubbed
│   ├── strategy/              # Phase 4 — stubbed
│   ├── ai_reasoning/          # Phase 4 — stubbed (Claude as advisor)
│   ├── position/              # Phase 4 — stubbed
│   ├── backtesting/           # Phase 5 — stubbed
│   ├── learning/              # Phase 6 — stubbed
│   └── monitoring/            # Phase 6 — stubbed
├── tests/
└── docs/                      # Per-module deep dives, runbooks
```

## Quickstart (local)

```bash
# 1. Copy env template and fill in Upstox credentials
cp .env.example .env
# edit .env: UPSTOX_API_KEY, UPSTOX_API_SECRET, UPSTOX_REDIRECT_URI, ANTHROPIC_API_KEY

# 2. Bring up Postgres + Redis
docker compose up -d postgres redis

# 3. Install Python deps (Python 3.11+)
pip install -e ".[dev]"

# 4. Run DB migrations
alembic upgrade head

# 5. First-time Upstox auth (interactive — opens browser)
python scripts/upstox_auth_cli.py

# 6. Probe what your Upstox tier can stream
python scripts/verify_upstox_capabilities.py

# 7. Start the FastAPI control plane
uvicorn trading_agent.api.main:app --reload --port 8000

# 8. Health check
curl http://localhost:8000/health
```

## Live-trading authorization (3-lock gate)

`LIVE_TRADING=true` in `.env` is necessary but **not sufficient**. To actually place real orders the system requires three independent locks:

1. `LIVE_TRADING=true` in environment.
2. `ACKNOWLEDGMENT.md` present in repo root, unmodified.
3. SHA-256 of `ACKNOWLEDGMENT.md` recorded in the `acknowledgment_log` table via `python scripts/acknowledge_live_trading.py`.

Until all three are satisfied, the execution engine refuses to send orders to Upstox. Paper mode is the default.

## Important constraints

- Upstox access tokens expire **daily at 3:30 AM IST** — the token manager handles this but you'll re-auth via browser each morning unless you wire up a refresh-token persistence flow (see `docs/runbooks/token_refresh.md`).
- Indian options expire weekly (NIFTY, BANKNIFTY, FINNIFTY, SENSEX, BANKEX) and monthly. The system avoids holding positions across expiry and weekend by default.
- Market hours: 09:15–15:30 IST, Mon–Fri (ex-holidays). Trade entry window is configurable in `config/risk.yaml`.

## Documentation

- [ARCHITECTURE.md](ARCHITECTURE.md) — system design, data flow, failure modes
- [docs/architecture/risk_design.md](docs/architecture/risk_design.md) — risk controls in detail
- [docs/runbooks/](docs/runbooks/) — operational procedures
- [docs/deployment/](docs/deployment/) — local + VPS deployment

## Phase plan

| Phase | Scope | Status |
|---|---|---|
| 0 | Scaffold + DB + Docker + Upstox auth + kill switch | ✅ Done |
| 1 | Market Data Engine (live ingest, options chain, IV/Greeks, India VIX) | ⏳ Next |
| 2 | Regime + Opportunity Ranking + Options Intelligence | ⏳ |
| 3 | Risk Engine + Low-Slippage Execution Engine | ⏳ |
| 4 | Strategy + Position Manager + Claude AI Reasoning | ⏳ |
| 5 | Backtesting + Walk-forward + Monte Carlo | ⏳ |
| 6 | Monitoring + Learning + Live cutover | ⏳ |

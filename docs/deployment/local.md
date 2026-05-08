# Local Deployment

## Prerequisites
- Python 3.11+
- Docker Desktop (or Docker Engine + Compose v2)
- Upstox developer app with `redirect_uri = http://localhost:8000/auth/upstox/callback`
- Anthropic API key (Phase 4+ uses Claude; Phase 0 doesn't *need* it but config requires the env var)

## Initial setup (one-time)

```bash
# 1. Configure
cp .env.example .env
# Edit .env: fill UPSTOX_*, ANTHROPIC_API_KEY, POSTGRES_PASSWORD, TOKEN_ENCRYPTION_KEY
# Generate the Fernet key:
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# 2. Bring up Postgres + Redis (and the app)
docker compose up -d

# 3. Install Python deps locally (so you can run scripts/* outside the container)
pip install -e ".[dev]"

# 4. Run migrations (Compose `app` service does this on start, but useful for local dev)
make migrate

# 5. Seed instruments
python scripts/seed_instruments.py

# 6. First-time Upstox auth
make auth

# 7. Verify capabilities
make probe

# 8. (Optional) Sign live-trading acknowledgment
#    Only do this after you've confirmed paper-mode behavior in later phases.
#    For Phase 0, leave LIVE_TRADING=false.
make acknowledge
```

## Daily operation

```bash
# Each morning before 09:15 IST:
make auth          # token refresh
make probe         # sanity check

# Health checks any time:
curl http://localhost:8000/health
curl http://localhost:8000/readiness
curl http://localhost:8000/control/live-trading-status
```

## Tearing down

```bash
docker compose down              # stop containers, keep volumes
docker compose down -v           # also delete Postgres + Redis volumes (DESTRUCTIVE)
```

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `make migrate` errors `connection refused` | Postgres not up | `docker compose ps`; `docker compose up -d postgres` |
| `/readiness` returns `db: false` | Postgres healthy but app can't reach | Check `POSTGRES_HOST` in `.env` (`localhost` for local Python, `postgres` for in-container) |
| `make probe` returns 401 | Token expired | `make auth` |
| `make auth` redirects to wrong URL | `UPSTOX_REDIRECT_URI` mismatch | Match Upstox dev portal exactly |
| `LiveTradingNotAuthorized` even with `LIVE_TRADING=true` | Lock #2 or #3 missing | `curl /control/live-trading-status` to see which lock fails |

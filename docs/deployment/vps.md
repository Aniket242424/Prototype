# VPS Deployment (single-node Docker Compose)

> Status: target for after Phase 3 is paper-validated. Not the Phase 0 path.

## Recommended provider/region
- Mumbai (ap-south-1 / equivalent) for low latency to NSE/BSE.
- 4 vCPU / 8 GB RAM minimum (room for Postgres, Redis, app, room to add workers in Phase 1+).

## Hardening checklist
- [ ] Non-root user, SSH key auth only, fail2ban, ufw.
- [ ] Postgres + Redis NOT exposed to public — bind to `127.0.0.1` or compose network only.
- [ ] App behind nginx + TLS (Let's Encrypt) if exposing the control plane externally.
- [ ] `.env` permissions `600`; `secrets/` directory `700`.
- [ ] Daily DB dumps (cron) → encrypted off-box backup.
- [ ] Monitoring: at minimum, healthcheck.io ping for `/health` every 1 min.

## Deploy
```bash
# On VPS, after initial hardening:
git clone <your-repo> /opt/trading_agent
cd /opt/trading_agent
cp .env.example .env  # then edit
docker compose up -d
docker compose logs -f app
```

## Operational ground rules
- Live trading on VPS: do **not** enable until you've run paper mode for at least 30 trading days post-Phase-3 with positive expectancy.
- Auth still requires interactive browser login each morning unless you wire up a headless flow. Consider a small jump-host pattern (run `make auth` from your laptop, the token lands in VPS Postgres via the network — only feasible if app is reachable; otherwise SSH-tunnel and run on VPS directly).
- Time sync: `chrony` or `systemd-timesyncd` running. Order timestamps depend on it.

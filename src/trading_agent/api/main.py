"""
FastAPI application — Phase 0 control plane.

Endpoints:
- GET  /health                         liveness + dependency check
- GET  /readiness                      strict readiness (DB + Redis + token validity)
- GET  /auth/upstox/login              build authorize URL
- GET  /auth/upstox/callback           OAuth2 redirect handler
- POST /control/kill-switch/trip       trip the kill switch
- POST /control/kill-switch/reset      reset (operator)
- GET  /control/kill-switch            current state
- GET  /control/live-trading-status    live-trading 3-lock report
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI

from trading_agent.api.routers import auth, control, dashboard, health
from trading_agent.core.config import get_settings
from trading_agent.core.logging import configure_logging, get_logger
from trading_agent.monitoring.telegram_alerter import alert as telegram_alert
from trading_agent.monitoring.token_watcher import token_watcher_loop
from trading_agent.supervisor import get_supervisor

configure_logging()
log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    log.info(
        "app.starting",
        env=settings.app_env,
        live_trading=settings.live_trading,
        capital_inr=settings.trading_capital_inr,
    )
    # Auto-start all workers on API boot so the system comes up as a unit.
    # Idempotent: start_all() skips workers that are already running.
    sup = get_supervisor()
    result = sup.start_all()
    log.info("supervisor.auto_started", result=result)

    # Background monitoring task: watches Upstox token expiry, alerts via Telegram
    # when re-auth is needed. No-op if Telegram is not configured.
    token_watcher_task = asyncio.create_task(token_watcher_loop())
    log.info("token_watcher.spawned")

    # Boot-time Telegram heartbeat so operator sees the bot is alive after deploys
    await telegram_alert(
        "info",
        f"<b>🚀 Trading_Agent online</b>\n"
        f"env=<code>{settings.app_env}</code> | "
        f"capital=₹{int(settings.trading_capital_inr):,} | "
        f"live_trading=<code>{settings.live_trading}</code>",
        dedup_key="boot",
        silent=True,
    )

    try:
        yield
    finally:
        log.info("app.stopping")
        token_watcher_task.cancel()
        try:
            await token_watcher_task
        except asyncio.CancelledError:
            pass
        sup.stop_all()


app = FastAPI(
    title="Trading_Agent — Phase 0 Control Plane",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(health.router)
app.include_router(auth.router)
app.include_router(control.router)
app.include_router(dashboard.router)

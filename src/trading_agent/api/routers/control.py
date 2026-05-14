"""Operational control plane: kill switch, live-trading status, worker supervisor."""
from __future__ import annotations

import asyncio
import hashlib
import os
import sys
import threading
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from trading_agent.api.auth_basic import verify_credentials
from trading_agent.core.config import REPO_ROOT, get_settings
from trading_agent.core.kill_switch import KillSwitch
from trading_agent.infrastructure.db import session_scope
from trading_agent.infrastructure.models import AcknowledgmentLogRow
from trading_agent.infrastructure.redis_client import make_redis
from trading_agent.supervisor import WORKERS, get_supervisor

router = APIRouter(
    prefix="/control", tags=["control"], dependencies=[Depends(verify_credentials)]
)


class TripPayload(BaseModel):
    reason: str
    source: str = "api"


class ResetPayload(BaseModel):
    operator: str


@router.get("/kill-switch")
async def get_kill_switch():
    r = make_redis()
    try:
        ks = KillSwitch(r)
        state = await ks.state()
    finally:
        await r.aclose()
    return {
        "tripped": state.tripped,
        "reason": state.reason,
        "tripped_at": state.tripped_at.isoformat() if state.tripped_at else None,
    }


@router.post("/kill-switch/trip")
async def trip(payload: TripPayload):
    r = make_redis()
    try:
        ks = KillSwitch(r)
        state = await ks.trip(reason=payload.reason, source=payload.source)
    finally:
        await r.aclose()
    return {"tripped": state.tripped, "reason": state.reason}


@router.post("/kill-switch/reset")
async def reset(payload: ResetPayload):
    r = make_redis()
    try:
        ks = KillSwitch(r)
        state = await ks.reset(operator=payload.operator)
    finally:
        await r.aclose()
    return {"tripped": state.tripped}


# ============================================================
# Worker supervisor — start/stop/restart trading workers from the dashboard
# ============================================================

def _check_worker_name(name: str) -> None:
    if name not in WORKERS:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown worker '{name}'. Valid: {sorted(WORKERS.keys())}",
        )


@router.get("/workers")
async def workers_status():
    """Status of all 3 workers (running/stopped, pid, started_at, log path)."""
    return get_supervisor().status()


@router.post("/workers/start")
async def workers_start_all():
    """Start any workers that are not already running."""
    return get_supervisor().start_all()


@router.post("/workers/stop")
async def workers_stop_all():
    """Stop all running workers."""
    return get_supervisor().stop_all()


@router.post("/workers/restart")
async def workers_restart_all():
    """Stop + start all workers (use after config changes)."""
    return get_supervisor().restart_all()


@router.post("/workers/{name}/start")
async def workers_start_one(name: str):
    _check_worker_name(name)
    return get_supervisor().start(name)


@router.post("/workers/{name}/stop")
async def workers_stop_one(name: str):
    _check_worker_name(name)
    return get_supervisor().stop(name)


@router.post("/workers/{name}/restart")
async def workers_restart_one(name: str):
    _check_worker_name(name)
    return get_supervisor().restart(name)


@router.get("/workers/{name}/logs")
async def workers_tail_log(name: str, lines: int = 50):
    _check_worker_name(name)
    return {"name": name, "lines": get_supervisor().tail_log(name, lines=lines)}


# ============================================================
# API self-restart — workers cleaned up, then exit(0).
# A watchdog (start.bat) restarts the API process.
# ============================================================

@router.post("/api/restart")
async def api_restart():
    """
    Cleanly stop all workers, then exit the API process.
    The start.bat watchdog will respawn the API within ~2s.
    """
    sup = get_supervisor()
    sup.stop_all()

    # Schedule a delayed os._exit so this response can flush to the client
    def _delayed_exit():
        import time
        time.sleep(0.5)
        os._exit(0)

    threading.Thread(target=_delayed_exit, daemon=True).start()
    return {"ok": True, "status": "exiting", "respawn_via": "start.bat watchdog"}


@router.get("/live-trading-status")
async def live_trading_status():
    """Reports the 3-lock state. Live orders require all three == True."""
    settings = get_settings()
    ack_path = REPO_ROOT / "ACKNOWLEDGMENT.md"

    lock1_env = settings.live_trading
    lock2_file = ack_path.exists()

    lock3_db = False
    file_sha = None
    db_sha = None
    if lock2_file:
        file_sha = hashlib.sha256(ack_path.read_bytes()).hexdigest()
        async with session_scope() as session:
            row = (
                await session.execute(
                    select(AcknowledgmentLogRow)
                    .where(AcknowledgmentLogRow.revoked_at.is_(None))
                    .order_by(AcknowledgmentLogRow.signed_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if row:
                db_sha = row.file_sha256
                lock3_db = row.file_sha256 == file_sha

    authorized = lock1_env and lock2_file and lock3_db
    return {
        "authorized": authorized,
        "locks": {
            "env_LIVE_TRADING": lock1_env,
            "file_present": lock2_file,
            "db_sha_matches": lock3_db,
        },
        "details": {
            "ack_file_sha256": file_sha,
            "db_sha256": db_sha,
        },
    }

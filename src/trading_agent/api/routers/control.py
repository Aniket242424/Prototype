"""Operational control plane: kill switch, live-trading status."""
from __future__ import annotations

import hashlib
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from trading_agent.core.config import REPO_ROOT, get_settings
from trading_agent.core.kill_switch import KillSwitch
from trading_agent.infrastructure.db import session_scope
from trading_agent.infrastructure.models import AcknowledgmentLogRow
from trading_agent.infrastructure.redis_client import make_redis

router = APIRouter(prefix="/control", tags=["control"])


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

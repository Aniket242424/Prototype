"""Health + readiness endpoints."""
from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import text

from trading_agent.core.time_utils import now_ist
from trading_agent.infrastructure.db import session_scope
from trading_agent.infrastructure.redis_client import make_redis, ping as redis_ping

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict:
    return {"status": "ok", "ts": now_ist().isoformat()}


@router.get("/readiness")
async def readiness() -> dict:
    db_ok = False
    redis_ok = False
    try:
        async with session_scope() as s:
            await s.execute(text("SELECT 1"))
            db_ok = True
    except Exception:
        db_ok = False

    r = make_redis()
    try:
        redis_ok = await redis_ping(r)
    finally:
        await r.aclose()

    overall = db_ok and redis_ok
    return {
        "status": "ready" if overall else "degraded",
        "db": db_ok,
        "redis": redis_ok,
        "ts": now_ist().isoformat(),
    }

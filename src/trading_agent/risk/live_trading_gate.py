"""
Live-trading 3-lock gate.

Check #1 of the Risk Engine. Three independent locks must ALL be true before
any real order can leave the process:

  1. `LIVE_TRADING=true` in environment
  2. `ACKNOWLEDGMENT.md` file present in repo root, unmodified
  3. SHA-256 of that file recorded in `acknowledgment_log` table

Until then, the system runs in paper mode. The Risk Engine's first check
calls `assert_live_trading_authorized()` and routes to paper mode if it fails.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select

from trading_agent.core.config import REPO_ROOT, get_settings
from trading_agent.core.logging import get_logger
from trading_agent.infrastructure.db import session_scope
from trading_agent.infrastructure.models import AcknowledgmentLogRow

log = get_logger(__name__)


@dataclass(frozen=True)
class LiveTradingStatus:
    authorized: bool
    env_live_trading: bool
    file_present: bool
    db_sha_matches: bool
    file_sha256: str | None
    db_sha256: str | None
    reason: str


async def check_live_trading_status() -> LiveTradingStatus:
    """
    Returns the state of all three locks. The Risk Engine consults this
    on every evaluation. Cheap enough to call every trade.
    """
    settings = get_settings()
    lock1 = settings.live_trading

    ack_path: Path = REPO_ROOT / "ACKNOWLEDGMENT.md"
    lock2 = ack_path.exists()
    file_sha = None
    if lock2:
        file_sha = hashlib.sha256(ack_path.read_bytes()).hexdigest()

    lock3 = False
    db_sha = None
    if lock2:
        try:
            async with session_scope() as session:
                latest = (await session.execute(
                    select(AcknowledgmentLogRow)
                    .where(AcknowledgmentLogRow.revoked_at.is_(None))
                    .order_by(AcknowledgmentLogRow.signed_at.desc())
                    .limit(1)
                )).scalar_one_or_none()
                if latest:
                    db_sha = latest.file_sha256
                    lock3 = (db_sha == file_sha)
        except Exception as e:
            log.warning("live_trading.db_check_failed", error=str(e))

    authorized = lock1 and lock2 and lock3

    if authorized:
        reason = "All three locks satisfied — live orders permitted"
    elif not lock1:
        reason = "LIVE_TRADING=false in env (paper mode)"
    elif not lock2:
        reason = "ACKNOWLEDGMENT.md not found in repo root"
    elif not lock3:
        reason = "DB SHA does not match current ACKNOWLEDGMENT.md (sign via scripts/acknowledge_live_trading.py)"
    else:
        reason = "Unknown state"

    return LiveTradingStatus(
        authorized=authorized,
        env_live_trading=lock1,
        file_present=lock2,
        db_sha_matches=lock3,
        file_sha256=file_sha,
        db_sha256=db_sha,
        reason=reason,
    )


async def is_live_trading_authorized() -> bool:
    """Convenience boolean — Risk Engine uses this to route between live/paper."""
    status = await check_live_trading_status()
    return status.authorized

"""
Global kill switch.

Backed by a Redis key so any process (control plane, market-data worker,
execution worker) can trip or observe it. Tripping is one-way per session
— resetting requires explicit operator action via CLI or API.

The Risk Engine consults the kill switch as the SECOND check in its gate
(after the live-trading 3-lock). If tripped, the system halts all NEW
entries and attempts emergency exits on open positions.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final

from redis.asyncio import Redis

from trading_agent.core.constants import (
    CHAN_KILL_SWITCH,
    KILL_SWITCH_KEY,
    KILL_SWITCH_REASON_KEY,
    KILL_SWITCH_TRIPPED_AT_KEY,
)
from trading_agent.core.logging import get_logger
from trading_agent.core.time_utils import now_ist

log = get_logger(__name__)


@dataclass(frozen=True)
class KillSwitchState:
    tripped: bool
    reason: str | None
    tripped_at: datetime | None


class KillSwitch:
    """Async kill-switch primitive over Redis."""

    KEY: Final = KILL_SWITCH_KEY

    def __init__(self, redis: Redis):
        self._redis = redis

    async def state(self) -> KillSwitchState:
        async with self._redis.pipeline(transaction=False) as pipe:
            pipe.get(KILL_SWITCH_KEY)
            pipe.get(KILL_SWITCH_REASON_KEY)
            pipe.get(KILL_SWITCH_TRIPPED_AT_KEY)
            tripped, reason, tripped_at = await pipe.execute()
        return KillSwitchState(
            tripped=bool(tripped) and tripped == b"1",
            reason=reason.decode() if reason else None,
            tripped_at=datetime.fromisoformat(tripped_at.decode()) if tripped_at else None,
        )

    async def is_tripped(self) -> bool:
        v = await self._redis.get(KILL_SWITCH_KEY)
        return bool(v) and v == b"1"

    async def trip(self, reason: str, source: str = "unknown") -> KillSwitchState:
        now = now_ist().isoformat()
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.set(KILL_SWITCH_KEY, "1")
            pipe.set(KILL_SWITCH_REASON_KEY, reason)
            pipe.set(KILL_SWITCH_TRIPPED_AT_KEY, now)
            await pipe.execute()
        await self._redis.publish(
            CHAN_KILL_SWITCH,
            f'{{"event":"trip","reason":"{reason}","source":"{source}","ts":"{now}"}}',
        )
        log.error("kill_switch.tripped", reason=reason, source=source)
        return await self.state()

    async def reset(self, operator: str) -> KillSwitchState:
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.delete(KILL_SWITCH_KEY)
            pipe.delete(KILL_SWITCH_REASON_KEY)
            pipe.delete(KILL_SWITCH_TRIPPED_AT_KEY)
            await pipe.execute()
        await self._redis.publish(
            CHAN_KILL_SWITCH,
            f'{{"event":"reset","operator":"{operator}","ts":"{now_ist().isoformat()}"}}',
        )
        log.warning("kill_switch.reset", operator=operator)
        return await self.state()

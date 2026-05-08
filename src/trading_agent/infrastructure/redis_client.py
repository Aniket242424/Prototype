"""Redis async client factory + ping helper."""
from __future__ import annotations

from redis.asyncio import Redis

from trading_agent.core.config import get_settings

_settings = get_settings()


def make_redis() -> Redis:
    return Redis.from_url(
        _settings.redis_url,
        decode_responses=False,
        health_check_interval=15,
        socket_keepalive=True,
    )


async def ping(client: Redis) -> bool:
    try:
        return bool(await client.ping())
    except Exception:
        return False

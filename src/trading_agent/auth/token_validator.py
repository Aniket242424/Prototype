"""
Upstox access-token validator.

Given a raw access token (as pasted by the operator from the Upstox "Generate"
button on https://account.upstox.com/developer/apps), verify it works by
calling GET /v2/user/profile and extract the user_id / name / email.

Used by the Telegram listener's /token handler — we never store a token
without first confirming Upstox actually accepts it.
"""
from __future__ import annotations

from dataclasses import dataclass

import httpx

from trading_agent.core.config import AppSettings, get_settings
from trading_agent.core.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class ValidatedToken:
    access_token: str
    user_id: str
    user_name: str | None
    email: str | None
    broker: str


class TokenValidationError(Exception):
    """The token was rejected by Upstox or had a malformed response."""


async def validate_access_token(
    access_token: str,
    settings: AppSettings | None = None,
    timeout_sec: float = 10.0,
) -> ValidatedToken:
    """
    Verify the token by calling Upstox /v2/user/profile.

    Returns ValidatedToken on success. Raises TokenValidationError on any
    Upstox-side failure (401/4xx/5xx) or malformed response.
    """
    settings = settings or get_settings()
    url = f"{settings.upstox_base_url}/user/profile"
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {access_token}",
    }

    try:
        async with httpx.AsyncClient(timeout=timeout_sec) as client:
            r = await client.get(url, headers=headers)
    except Exception as e:
        raise TokenValidationError(f"Could not reach Upstox: {e}") from e

    if r.status_code != 200:
        raise TokenValidationError(
            f"Upstox rejected the token (HTTP {r.status_code}): {r.text[:200]}"
        )

    try:
        payload = r.json()
        data = payload["data"]
    except Exception as e:
        raise TokenValidationError(f"Malformed Upstox profile response: {e}") from e

    user_id = data.get("user_id")
    if not user_id:
        raise TokenValidationError("Upstox profile response missing user_id")

    return ValidatedToken(
        access_token=access_token,
        user_id=user_id,
        user_name=data.get("user_name"),
        email=data.get("email"),
        broker=data.get("broker", "UPSTOX"),
    )

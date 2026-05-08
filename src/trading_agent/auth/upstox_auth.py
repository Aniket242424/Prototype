"""
Upstox OAuth2 authorization-code flow.

Reference: https://upstox.com/developer/api-documentation/login

Flow:
1. Build authorize URL → user opens in browser → Upstox redirects to
   UPSTOX_REDIRECT_URI?code=XXX
2. Exchange code for access_token via POST /v2/login/authorization/token
3. Persist token (encrypted) via TokenManager.

Tokens expire daily at 03:30 IST. The TokenManager surfaces an `is_valid()`
check; the typical operational pattern is `make auth` each morning.
"""
from __future__ import annotations

import urllib.parse
from dataclasses import dataclass

import httpx

from trading_agent.core.config import AppSettings
from trading_agent.core.exceptions import AuthError
from trading_agent.core.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class UpstoxToken:
    access_token: str
    extended_token: str | None
    user_id: str
    user_name: str | None
    email: str | None
    broker: str
    issued_at_ist_iso: str


class UpstoxAuth:
    """Stateless helper for the OAuth2 dance. Token persistence is TokenManager's job."""

    AUTHORIZE_PATH = "/login/authorization/dialog"
    TOKEN_PATH = "/login/authorization/token"

    def __init__(self, settings: AppSettings):
        self._settings = settings

    def authorize_url(self, state: str | None = None) -> str:
        params = {
            "client_id": self._settings.upstox_api_key.get_secret_value(),
            "redirect_uri": self._settings.upstox_redirect_uri,
            "response_type": "code",
        }
        if state:
            params["state"] = state
        return f"{self._settings.upstox_base_url}{self.AUTHORIZE_PATH}?{urllib.parse.urlencode(params)}"

    async def exchange_code(self, code: str) -> UpstoxToken:
        url = f"{self._settings.upstox_base_url}{self.TOKEN_PATH}"
        data = {
            "code": code,
            "client_id": self._settings.upstox_api_key.get_secret_value(),
            "client_secret": self._settings.upstox_api_secret.get_secret_value(),
            "redirect_uri": self._settings.upstox_redirect_uri,
            "grant_type": "authorization_code",
        }
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(url, data=data, headers=headers)
        if r.status_code != 200:
            log.error("upstox.token_exchange_failed", status=r.status_code, body=r.text[:500])
            raise AuthError(f"Upstox token exchange failed: {r.status_code} {r.text}")
        payload = r.json()
        from trading_agent.core.time_utils import now_ist
        return UpstoxToken(
            access_token=payload["access_token"],
            extended_token=payload.get("extended_token"),
            user_id=payload.get("user_id", ""),
            user_name=payload.get("user_name"),
            email=payload.get("email"),
            broker=payload.get("broker", "UPSTOX"),
            issued_at_ist_iso=now_ist().isoformat(),
        )

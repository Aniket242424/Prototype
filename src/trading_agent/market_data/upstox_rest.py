"""
Upstox REST helpers used by the Market Data worker.

- `authorize_ws()`        — fetches one-time WS URL (v3)
- `option_contracts()`    — list of all option contracts for an underlying (v2)
- `option_chain()`        — chain at a specific expiry (v2): strikes × {CE, PE} × {market_data, greeks}
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import httpx
from sqlalchemy import select

from trading_agent.auth.token_manager import TokenManager
from trading_agent.core.config import AppSettings
from trading_agent.core.exceptions import AuthError, MarketDataError
from trading_agent.core.logging import get_logger
from trading_agent.infrastructure.db import session_scope
from trading_agent.infrastructure.models import TokenRow

log = get_logger(__name__)


@dataclass(frozen=True)
class WsAuthorization:
    redirect_uri: str


class UpstoxRestClient:
    def __init__(self, settings: AppSettings):
        self._settings = settings
        self._tm = TokenManager(settings)
        # Strip trailing /v2 (or any version suffix) so we can address /v2 or /v3 explicitly
        self._api_root = settings.upstox_base_url.rsplit("/", 1)[0]

    async def _bearer_token(self) -> str:
        async with session_scope() as session:
            row = (await session.execute(select(TokenRow).limit(1))).scalar_one_or_none()
            if row is None:
                raise AuthError("No token in DB. Run `make auth` first.")
            return await self._tm.get_valid_or_raise(session, row.user_id)

    async def authorize_ws(self) -> WsAuthorization:
        token = await self._bearer_token()
        url = f"{self._api_root}/v3/feed/market-data-feed/authorize"
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                url,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            )
        if r.status_code != 200:
            log.error("ws_authorize.failed", status=r.status_code, body=r.text[:300])
            raise AuthError(f"WS authorize failed: {r.status_code} {r.text[:200]}")
        body = r.json().get("data", {})
        uri = body.get("authorized_redirect_uri") or body.get("authorizedRedirectUri")
        if not uri:
            raise AuthError(f"WS authorize response missing redirect URI: {body}")
        return WsAuthorization(redirect_uri=uri)

    async def multi_quote_ltp(self, instrument_keys: list[str]) -> dict[str, dict]:
        """
        Fetch LTP for multiple instruments in one call. Used for index polling
        because Upstox WS doesn't push live updates for INDEX instruments
        (they're computed values, not traded). Endpoint returns ~10ms.

        Returns: { "NSE_INDEX:Nifty 50": {"last_price": 23598.4, ...}, ... }
                  Note the colon in returned key vs pipe in input key — Upstox quirk.
        """
        token = await self._bearer_token()
        url = f"{self._api_root}/v2/market-quote/ltp"
        # Upstox accepts comma-separated instrument keys in a single query param
        params = {"instrument_key": ",".join(instrument_keys)}
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(
                url,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                params=params,
            )
        if r.status_code != 200:
            raise MarketDataError(f"multi_quote_ltp failed: {r.status_code} {r.text[:200]}")
        return r.json().get("data") or {}

    async def option_contracts(self, underlying_instrument_key: str) -> list[dict]:
        """List all option contracts for one underlying. Used to discover available expiries."""
        token = await self._bearer_token()
        url = f"{self._api_root}/v2/option/contract"
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(
                url,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                params={"instrument_key": underlying_instrument_key},
            )
        if r.status_code != 200:
            log.error("option_contracts.failed", status=r.status_code, body=r.text[:300])
            raise MarketDataError(f"option_contracts failed: {r.status_code}")
        return r.json().get("data") or []

    async def option_chain(
        self, underlying_instrument_key: str, expiry: date
    ) -> list[dict]:
        """
        Fetch the chain at a specific expiry. Returns a list of strike rows;
        each row has `call_options` and `put_options` with `market_data` + `option_greeks`.
        """
        token = await self._bearer_token()
        url = f"{self._api_root}/v2/option/chain"
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(
                url,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                params={
                    "instrument_key": underlying_instrument_key,
                    "expiry_date": expiry.isoformat(),
                },
            )
        if r.status_code != 200:
            log.error(
                "option_chain.failed",
                status=r.status_code,
                underlying=underlying_instrument_key,
                expiry=expiry.isoformat(),
                body=r.text[:300],
            )
            raise MarketDataError(f"option_chain failed: {r.status_code}")
        return r.json().get("data") or []

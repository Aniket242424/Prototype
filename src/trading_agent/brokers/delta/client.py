"""
Delta Exchange India REST API client — Iron Condor bot.

Handles authentication (HMAC-SHA256), rate limiting, and provides
clean async methods for everything the Iron Condor bot needs:
  - get_option_chain()     -> live strikes, bids, asks, IV
  - get_ticker()           -> current BTC/ETH price
  - place_order()          -> market or limit option orders
  - cancel_order()         -> cancel by order_id
  - get_positions()        -> open positions
  - get_wallet_balance()   -> available margin

Authentication:
  Every request signs:  method + timestamp + path + body
  using HMAC-SHA256 with the API secret, sent as headers:
    api-key: <key>
    timestamp: <unix_ms>
    signature: <hex>

Rate limits (Delta India):
  - REST: 300 req/min per IP (5 req/sec)
  - We stay well under this — one chain fetch per minute max.

Docs: https://docs.india.delta.exchange
"""
from __future__ import annotations

import hashlib
import hmac
import os
import time
from typing import Any
from urllib.parse import urlencode

import httpx

from trading_agent.core.logging import get_logger

log = get_logger(__name__)

BASE_URL = os.getenv("DELTA_BASE_URL", "https://api.india.delta.exchange")
TIMEOUT = 10.0   # seconds


class DeltaError(Exception):
    """Raised when Delta API returns a non-2xx response or error payload."""
    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"Delta API error {status}: [{code}] {message}")
        self.status = status
        self.code = code
        self.message = message


class DeltaClient:
    """
    Async Delta Exchange India REST client.

    Usage:
        async with DeltaClient.from_env() as client:
            ticker = await client.get_ticker("BTCUSD")
    """

    def __init__(self, api_key: str, api_secret: str, base_url: str = BASE_URL):
        self._key = api_key
        self._secret = api_secret
        self._base = base_url.rstrip("/")
        self._http: httpx.AsyncClient | None = None

    @classmethod
    def from_env(cls) -> "DeltaClient":
        key = os.environ["DELTA_API_KEY"]
        secret = os.environ["DELTA_API_SECRET"]
        base = os.getenv("DELTA_BASE_URL", BASE_URL)
        return cls(key, secret, base)

    async def __aenter__(self) -> "DeltaClient":
        self._http = httpx.AsyncClient(timeout=TIMEOUT)
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self._http:
            await self._http.aclose()

    # ------------------------------------------------------------------ #
    # Auth
    # ------------------------------------------------------------------ #

    def _sign(self, method: str, path: str, body: str = "") -> dict[str, str]:
        ts = str(int(time.time()))
        message = method.upper() + ts + path + body
        sig = hmac.new(
            self._secret.encode(), message.encode(), hashlib.sha256
        ).hexdigest()
        return {
            "api-key": self._key,
            "timestamp": ts,
            "signature": sig,
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------------ #
    # Low-level request
    # ------------------------------------------------------------------ #

    async def _request(
        self,
        method: str,
        path: str,
        params: dict | None = None,
        json: dict | None = None,
    ) -> Any:
        assert self._http is not None, "Use 'async with DeltaClient()' context manager"
        qs = ("?" + urlencode(params)) if params else ""
        body_str = ""
        if json:
            import json as _json
            body_str = _json.dumps(json, separators=(",", ":"))
        headers = self._sign(method, path + qs, body_str)
        url = self._base + path
        resp = await self._http.request(
            method, url, params=params,
            content=body_str.encode() if body_str else None,
            headers=headers,
        )
        if resp.status_code == 429:
            log.warning("delta.rate_limit", path=path)
            raise DeltaError(429, "rate_limit", "Rate limited — slow down requests")
        data = resp.json()
        if resp.status_code >= 400 or (isinstance(data, dict) and data.get("success") is False):
            code = data.get("error", {}).get("code", "unknown") if isinstance(data, dict) else "unknown"
            msg = data.get("error", {}).get("context", str(data)) if isinstance(data, dict) else str(data)
            raise DeltaError(resp.status_code, code, msg)
        return data.get("result", data) if isinstance(data, dict) else data

    # ------------------------------------------------------------------ #
    # Market data
    # ------------------------------------------------------------------ #

    async def get_ticker(self, symbol: str = "BTCUSD") -> dict:
        """Return latest ticker for a perpetual or spot symbol."""
        data = await self._request("GET", f"/v2/tickers/{symbol}")
        return data

    async def get_spot_price(self, symbol: str = "BTCUSD") -> float:
        """Convenience: return float close/mark price."""
        t = await self.get_ticker(symbol)
        # Delta returns mark_price or close
        return float(t.get("mark_price") or t.get("close") or t["last_price"])

    async def get_products(self, contract_type: str = "call_options") -> list[dict]:
        """
        List all active products of a given type.
        contract_type: 'call_options' | 'put_options' | 'perpetual_futures' etc.
        """
        data = await self._request("GET", "/v2/products", params={
            "contract_type": contract_type,
            "state": "live",
        })
        return data if isinstance(data, list) else data.get("products", [])

    async def get_option_chain(
        self,
        underlying: str = "BTCUSD",
        expiry_date: str | None = None,
    ) -> list[dict]:
        """
        Fetch the full option chain (calls + puts) for a given underlying
        and expiry date string (e.g. '040626' for 04 Jun 26).
        Returns list of product dicts with current mark_price, bid, ask, iv.
        """
        calls = await self.get_products("call_options")
        puts = await self.get_products("put_options")
        products = calls + puts
        # Filter by underlying
        filtered = [
            p for p in products
            if p.get("underlying_asset", {}).get("symbol", "").upper() == underlying.upper()
            or underlying.upper() in p.get("symbol", "").upper()
        ]
        if expiry_date:
            filtered = [p for p in filtered if expiry_date in p.get("symbol", "")]
        return filtered

    async def get_option_ticker(self, symbol: str) -> dict:
        """Get bid/ask/mark/iv for a specific option symbol."""
        return await self._request("GET", f"/v2/tickers/{symbol}")

    async def get_wallet_balance(self) -> dict:
        """Return wallet balances (available margin, total, etc.)."""
        data = await self._request("GET", "/v2/wallet/balances")
        return data

    # ------------------------------------------------------------------ #
    # Trading
    # ------------------------------------------------------------------ #

    async def place_order(
        self,
        product_id: int,
        side: str,               # "buy" or "sell"
        size: int,               # number of lots
        order_type: str = "market_order",
        limit_price: float | None = None,
        time_in_force: str = "gtc",
        reduce_only: bool = False,
    ) -> dict:
        """
        Place an order on Delta India.

        Args:
            product_id: Delta's numeric product ID (from get_products())
            side: "buy" (long) or "sell" (short)
            size: number of contracts (lots)
            order_type: "market_order" | "limit_order"
            limit_price: required for limit orders
            time_in_force: "gtc" | "ioc" | "fok"
            reduce_only: True to only reduce an existing position

        Returns:
            Order dict with id, status, avg_fill_price, etc.
        """
        payload: dict[str, Any] = {
            "product_id": product_id,
            "side": side.lower(),
            "size": size,
            "order_type": order_type,
            "time_in_force": time_in_force,
            "reduce_only": reduce_only,
        }
        if limit_price is not None:
            payload["limit_price"] = str(limit_price)
        log.info(
            "delta.order.placing",
            product_id=product_id, side=side, size=size,
            order_type=order_type, limit_price=limit_price,
        )
        result = await self._request("POST", "/v2/orders", json=payload)
        log.info("delta.order.placed", order_id=result.get("id"), status=result.get("state"))
        return result

    async def cancel_order(self, order_id: int, product_id: int) -> dict:
        """Cancel an open order by ID."""
        result = await self._request("DELETE", f"/v2/orders/{order_id}", json={
            "product_id": product_id,
        })
        log.info("delta.order.cancelled", order_id=order_id)
        return result

    async def get_open_orders(self, product_id: int | None = None) -> list[dict]:
        """List all open orders, optionally filtered by product."""
        params = {}
        if product_id:
            params["product_id"] = product_id
        data = await self._request("GET", "/v2/orders", params=params or None)
        return data if isinstance(data, list) else []

    async def get_positions(self) -> list[dict]:
        """Return all open positions."""
        data = await self._request("GET", "/v2/positions")
        return data if isinstance(data, list) else []

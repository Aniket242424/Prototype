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
        """Convenience: return float mark/close/last price. Raises if none present."""
        t = await self.get_ticker(symbol)
        price = t.get("mark_price") or t.get("close") or t.get("last_price")
        if price in (None, "", 0, "0", "0.0"):
            raise DeltaError(0, "no_price", f"No usable price field in ticker for {symbol}: {t}")
        return float(price)

    async def get_products(
        self,
        contract_type: str = "call_options",
        state: str | None = "live",
    ) -> list[dict]:
        """
        List products. NOTE: Delta India ignores `contract_type` server-side and
        returns the full catalogue regardless (verified 2026-06-03); we keep the
        param for API parity but callers must filter the result themselves.

        state: 'live' (default, currently-tradeable) or None to omit the filter
               (needed to find a contract AFTER it has settled/expired).
        """
        params: dict = {"contract_type": contract_type}
        if state:
            params["state"] = state
        data = await self._request("GET", "/v2/products", params=params)
        return data if isinstance(data, list) else data.get("products", [])

    async def get_product_by_symbol(self, symbol: str) -> dict | None:
        """
        Find a single product by exact symbol across ALL states (no state filter),
        so it works for settled/expired contracts too. Returns None if not found.
        """
        products = await self.get_products("call_options", state=None)
        for p in products:
            if p.get("symbol") == symbol:
                return p
        return None

    async def get_option_chain(
        self,
        underlying: str = "BTC",
        expiry_date: str | None = None,
    ) -> list[dict]:
        """
        Fetch the daily/weekly option chain (calls + puts) for an underlying.

        IMPORTANT: Delta India's /v2/products endpoint ignores the
        `contract_type` query param and returns ALL ~1200 products in one
        list (verified live 2026-06-03). It also includes unrelated product
        families ('MV-' move options, perpetuals like 'BTCUSD'). We therefore
        fetch ONCE and filter strictly by the daily-option symbol grammar:

            C-<UNDERLYING>-<STRIKE>-<DDMMYY>   (call)
            P-<UNDERLYING>-<STRIKE>-<DDMMYY>   (put)

        e.g. 'C-BTC-67400-040626' = BTC 67400 call expiring 04 Jun 26.

        Args:
            underlying: asset token used in the symbol, e.g. 'BTC' or 'ETH'
                        (NOT 'BTCUSD' — that's the perp).
            expiry_date: optional 'DDMMYY' string to filter a single expiry.

        Returns each matching product enriched with parsed fields:
            option_type: 'call' | 'put'
            strike: float
            expiry: 'DDMMYY' str
        plus the raw product dict (id, symbol, settlement_time, contract_value...).
        """
        # One fetch returns the full catalogue (filter param is ignored server-side).
        products = await self.get_products("call_options")
        out: list[dict] = []
        for p in products:
            sym = p.get("symbol", "")
            parts = sym.split("-")
            if len(parts) != 4:
                continue
            kind, under, strike_str, exp = parts
            if kind not in ("C", "P"):
                continue
            if under.upper() != underlying.upper():
                continue
            # Expiry must be a well-formed DDMMYY (6 digits); skip anything else
            # so a malformed symbol can never crash the date math downstream.
            if not (len(exp) == 6 and exp.isdigit()):
                continue
            if expiry_date and exp != expiry_date:
                continue
            try:
                strike = float(p.get("strike_price") or strike_str)
            except (TypeError, ValueError):
                continue
            if strike <= 0:
                continue
            out.append({
                **p,
                "option_type": "call" if kind == "C" else "put",
                "strike": strike,
                "expiry": exp,
            })
        return out

    async def list_expiries(self, underlying: str = "BTC") -> list[str]:
        """
        Return the sorted-by-date list of available 'DDMMYY' expiry strings
        for an underlying's daily/weekly options.
        """
        chain = await self.get_option_chain(underlying)
        expiries = {p["expiry"] for p in chain}

        def _key(ddmmyy: str) -> tuple[int, int, int]:
            d, m, y = int(ddmmyy[0:2]), int(ddmmyy[2:4]), int(ddmmyy[4:6])
            return (y, m, d)

        return sorted(expiries, key=_key)

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

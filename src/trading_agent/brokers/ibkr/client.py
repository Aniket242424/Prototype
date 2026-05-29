"""
IBKR client — thin async wrapper around ib_async.IB.

Owns a single connection to IB Gateway. Reconnects on failure with
exponential backoff. Other modules (market_data, orders) borrow the
underlying IB instance via `client.ib`.

Connection settings (read from env, with sensible defaults):
    IBKR_GATEWAY_HOST = 127.0.0.1
    IBKR_GATEWAY_PORT = 4002       (paper. Live=4001. TWS Paper=7497. TWS Live=7496.)
    IBKR_CLIENT_ID    = 1          (each separate connecting process needs a unique ID)
    IBKR_READONLY     = false      (set true to enforce read-only at the client level)
    IBKR_ACCOUNT      = ""         (paper acct e.g. DUQ522790; left blank = auto-detect)
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Optional

from ib_async import IB
from ib_async.contract import Contract

from trading_agent.core.logging import get_logger

log = get_logger(__name__)


class IBKRConnectionError(Exception):
    """Raised when we can't establish a connection to the Gateway."""


@dataclass(frozen=True)
class IBKRConfig:
    host: str
    port: int
    client_id: int
    readonly: bool
    account: str  # may be ""

    @classmethod
    def from_env(cls) -> "IBKRConfig":
        return cls(
            host=os.environ.get("IBKR_GATEWAY_HOST", "127.0.0.1"),
            port=int(os.environ.get("IBKR_GATEWAY_PORT", "4002")),
            client_id=int(os.environ.get("IBKR_CLIENT_ID", "1")),
            readonly=os.environ.get("IBKR_READONLY", "false").lower() == "true",
            account=os.environ.get("IBKR_ACCOUNT", ""),
        )


class IBKRClient:
    """
    Single-shared-connection wrapper. Construct once, call connect(),
    re-use the same `ib` handle across modules.

    Resilience:
    - connect() retries with exponential backoff (5 attempts, 1s → 16s)
    - reconnect() is safe to call repeatedly; idempotent if already connected
    - disconnect() always safe
    """

    def __init__(self, config: Optional[IBKRConfig] = None):
        self._config = config or IBKRConfig.from_env()
        self._ib = IB()

    @property
    def ib(self) -> IB:
        """The underlying ib_async.IB instance. Use for any operation."""
        return self._ib

    @property
    def config(self) -> IBKRConfig:
        return self._config

    @property
    def connected(self) -> bool:
        return self._ib.isConnected()

    async def connect(self, timeout_sec: float = 10.0) -> None:
        """Connect with exponential backoff. Raises IBKRConnectionError on permanent failure."""
        if self._ib.isConnected():
            log.info("ibkr.already_connected", host=self._config.host, port=self._config.port)
            return

        delays = [1, 2, 4, 8, 16]  # 5 attempts total
        last_err: Exception | None = None
        for attempt, delay in enumerate(delays, start=1):
            try:
                log.info(
                    "ibkr.connecting",
                    host=self._config.host,
                    port=self._config.port,
                    client_id=self._config.client_id,
                    readonly=self._config.readonly,
                    attempt=attempt,
                )
                await self._ib.connectAsync(
                    host=self._config.host,
                    port=self._config.port,
                    clientId=self._config.client_id,
                    timeout=timeout_sec,
                    readonly=self._config.readonly,
                )
                # Verify by checking accounts list
                accounts = self._ib.managedAccounts()
                log.info(
                    "ibkr.connected",
                    host=self._config.host,
                    port=self._config.port,
                    accounts=accounts,
                    server_version=self._ib.client.serverVersion(),
                )
                return
            except Exception as e:
                last_err = e
                log.warning(
                    "ibkr.connect_failed",
                    attempt=attempt,
                    error=str(e),
                    retry_in_sec=delay if attempt < len(delays) else None,
                )
                if attempt < len(delays):
                    await asyncio.sleep(delay)
        raise IBKRConnectionError(
            f"Could not connect to IB Gateway at {self._config.host}:{self._config.port} "
            f"after {len(delays)} attempts. Last error: {last_err}"
        )

    async def disconnect(self) -> None:
        """Idempotent disconnect."""
        if self._ib.isConnected():
            self._ib.disconnect()
            log.info("ibkr.disconnected")

    async def account_summary(self) -> dict:
        """
        Return a dict of key account metrics for the active account.
        Useful for smoke tests + dashboard heartbeat.
        """
        if not self._ib.isConnected():
            raise IBKRConnectionError("Not connected. Call connect() first.")
        # Pick the first managed account if multiple
        accounts = self._ib.managedAccounts()
        if not accounts:
            return {"error": "No managed accounts on this Gateway"}
        account = self._config.account or accounts[0]
        items = await self._ib.accountSummaryAsync(account=account)
        # Reshape AccountValue rows into a flat dict {tag: (value, currency)}
        out: dict[str, dict] = {"account": account, "values": {}}
        for v in items:
            out["values"][v.tag] = {"value": v.value, "currency": v.currency}
        return out

    async def qualify_contract(self, contract: Contract) -> Contract:
        """
        Resolve a Contract spec (e.g., conId, full month for a future) using
        IB's contract lookup. Required before subscribing to market data.
        """
        if not self._ib.isConnected():
            raise IBKRConnectionError("Not connected. Call connect() first.")
        qualified = await self._ib.qualifyContractsAsync(contract)
        if not qualified:
            raise ValueError(f"IBKR could not qualify contract: {contract}")
        return qualified[0]


# ============================================================
# Singleton for app-wide reuse
# ============================================================

_singleton: IBKRClient | None = None


def get_client() -> IBKRClient:
    """Return the process-wide IBKR client. Build on first call."""
    global _singleton
    if _singleton is None:
        _singleton = IBKRClient()
    return _singleton

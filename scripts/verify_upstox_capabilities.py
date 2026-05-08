"""
Probe what the current Upstox account can actually access.

Phase 0 baseline checks (extends in Phase 1 once Market Data Engine lands):
  - User profile fetch
  - Funds & margin
  - Instruments master fetch (light sample)
  - Quote on each configured underlying
  - WebSocket auth URL fetch (the precursor to streaming)

Run: `python scripts/verify_upstox_capabilities.py`
"""
from __future__ import annotations

import asyncio

import click
import httpx
from sqlalchemy import select

from trading_agent.auth.token_manager import TokenManager
from trading_agent.core.config import get_instruments_config, get_settings
from trading_agent.core.logging import configure_logging, get_logger
from trading_agent.infrastructure.db import session_scope
from trading_agent.infrastructure.models import TokenRow

CHECKS = [
    ("user_profile",       "/user/profile",                      "/v2"),
    ("user_funds",         "/user/get-funds-and-margin",         "/v2"),  # 423 outside 5:30AM–12AM IST
    ("ws_authorize_url",   "/feed/market-data-feed/authorize",   "/v3"),  # v2 retired
]


async def _run() -> int:
    settings = get_settings()
    instruments = get_instruments_config()
    tm = TokenManager(settings)
    log = get_logger(__name__)

    async with session_scope() as session:
        token_row = (await session.execute(select(TokenRow).limit(1))).scalar_one_or_none()
        if token_row is None:
            click.echo(click.style("No token found. Run `make auth` first.", fg="red"))
            return 1
        access_token = await tm.get_valid_or_raise(session, token_row.user_id)

    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    failures = 0
    # The base_url already ends in /v2; we strip and re-add the version per-check
    api_root = settings.upstox_base_url.rsplit("/", 1)[0]  # https://api.upstox.com
    async with httpx.AsyncClient(timeout=10.0) as client:
        for name, path, version in CHECKS:
            url = f"{api_root}{version}{path}"
            try:
                r = await client.get(url, headers=headers)
                ok = r.status_code == 200
                marker = "[OK]  " if ok else "[FAIL]"
                color = "green" if ok else "red"
                click.echo(click.style(f"  {marker} {name:<22} status={r.status_code}", fg=color))
                if not ok:
                    failures += 1
                    log.warning("probe.failed", name=name, status=r.status_code, body=r.text[:300])
            except Exception as e:
                click.echo(click.style(f"  [FAIL] {name:<22} error={e}", fg="red"))
                failures += 1

        click.echo("\n  Quote sanity-check on configured underlyings:")
        for inst in instruments.instruments:
            if not inst.enabled:
                continue
            try:
                r = await client.get(
                    "/market-quote/ltp",
                    headers=headers,
                    params={"instrument_key": inst.upstox_instrument_key},
                )
                if r.status_code == 200:
                    click.echo(click.style(
                        f"    [OK]   {inst.name:<10} {inst.upstox_instrument_key}",
                        fg="green",
                    ))
                else:
                    click.echo(click.style(
                        f"    [FAIL] {inst.name:<10} status={r.status_code} body={r.text[:120]}",
                        fg="yellow",
                    ))
            except Exception as e:
                click.echo(click.style(f"    [FAIL] {inst.name:<10} error={e}", fg="red"))

    return 0 if failures == 0 else 2


@click.command()
def main() -> None:
    """Probe Upstox API for the current access token."""
    configure_logging()
    rc = asyncio.run(_run())
    raise SystemExit(rc)


if __name__ == "__main__":
    main()

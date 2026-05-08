"""
Interactive Upstox OAuth2 — first-time and daily re-auth.

Flow:
1. Print authorize URL (also opens browser).
2. User logs in, Upstox redirects to UPSTOX_REDIRECT_URI?code=XXX.
3. Either: paste the full callback URL when prompted (works without local server),
   OR run `make run` first and let the FastAPI /auth/upstox/callback endpoint capture it.

Either path persists the token to Postgres via TokenManager.
"""
from __future__ import annotations

import asyncio
import sys
import urllib.parse
import webbrowser

import click

from trading_agent.auth.token_manager import TokenManager
from trading_agent.auth.upstox_auth import UpstoxAuth
from trading_agent.core.config import get_settings
from trading_agent.core.logging import configure_logging, get_logger
from trading_agent.infrastructure.db import session_scope
from trading_agent.infrastructure.models import UserRow


async def _exchange_and_save(code: str) -> None:
    settings = get_settings()
    auth = UpstoxAuth(settings)
    tm = TokenManager(settings)

    token = await auth.exchange_code(code)
    async with session_scope() as session:
        if await session.get(UserRow, token.user_id) is None:
            session.add(UserRow(
                user_id=token.user_id,
                display_name=token.user_name,
                email=token.email,
            ))
            await session.commit()
        await tm.save(session, token)
    click.echo(click.style(f"[OK] Token saved for user_id={token.user_id}", fg="green"))


@click.command()
@click.option("--no-browser", is_flag=True, help="Don't auto-open browser.")
def main(no_browser: bool) -> None:
    """Run Upstox OAuth2 dance and persist token."""
    configure_logging()
    log = get_logger(__name__)

    settings = get_settings()
    auth = UpstoxAuth(settings)
    url = auth.authorize_url()

    click.echo("\nUpstox authorize URL:")
    click.echo(click.style(f"  {url}\n", fg="cyan"))

    if not no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    click.echo("After login, Upstox will redirect to your UPSTOX_REDIRECT_URI.")
    click.echo("Paste either the FULL callback URL or just the `code` parameter:\n")
    raw = click.prompt("callback URL or code", type=str).strip()

    if raw.startswith("http"):
        parsed = urllib.parse.urlparse(raw)
        qs = urllib.parse.parse_qs(parsed.query)
        if "code" not in qs:
            click.echo(click.style("No `code` parameter in URL.", fg="red"))
            sys.exit(1)
        code = qs["code"][0]
    else:
        code = raw

    try:
        asyncio.run(_exchange_and_save(code))
    except Exception as e:
        log.error("auth.cli.failed", error=str(e))
        click.echo(click.style(f"[FAIL] {e}", fg="red"))
        sys.exit(2)


if __name__ == "__main__":
    main()

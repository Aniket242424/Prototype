"""
Live-trading authorization — sign ACKNOWLEDGMENT.md.

This is lock #3 of the 3-lock gate. After running this:
  - The DB stores the SHA-256 of ACKNOWLEDGMENT.md plus its full text.
  - Live orders are permitted ONLY while file SHA matches the latest signed row.
  - Modifying ACKNOWLEDGMENT.md invalidates the lock until re-signed.

Revoke via: SQL `UPDATE acknowledgment_log SET revoked_at = now() WHERE id = ...`,
or via the future control-plane endpoint.
"""
from __future__ import annotations

import asyncio
import hashlib
import sys
from decimal import Decimal

import click
from sqlalchemy import select

from trading_agent.core.config import REPO_ROOT, get_settings
from trading_agent.core.logging import configure_logging
from trading_agent.infrastructure.db import session_scope
from trading_agent.infrastructure.models import AcknowledgmentLogRow, TokenRow


async def _sign() -> int:
    settings = get_settings()
    ack_path = REPO_ROOT / "ACKNOWLEDGMENT.md"
    if not ack_path.exists():
        click.echo(click.style("ACKNOWLEDGMENT.md not found at repo root.", fg="red"))
        return 1

    text = ack_path.read_text(encoding="utf-8")
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()

    click.echo("\n" + "=" * 70)
    click.echo("  LIVE-TRADING ACKNOWLEDGMENT")
    click.echo("=" * 70)
    click.echo(text)
    click.echo("=" * 70)
    click.echo(f"  Capital at signing: ₹{settings.trading_capital_inr:,.2f}")
    click.echo(f"  ACKNOWLEDGMENT.md SHA-256: {sha}")
    click.echo("=" * 70)

    if not click.confirm("\nI have read and accept the above. Sign?", default=False):
        click.echo("Aborted.")
        return 1

    typed = click.prompt("Type 'I ACCEPT' exactly to confirm")
    if typed != "I ACCEPT":
        click.echo(click.style("Confirmation phrase mismatch. Aborted.", fg="red"))
        return 1

    async with session_scope() as session:
        token_row = (await session.execute(select(TokenRow).limit(1))).scalar_one_or_none()
        if token_row is None:
            click.echo(click.style("No user found. Run `make auth` first.", fg="red"))
            return 1
        row = AcknowledgmentLogRow(
            user_id=token_row.user_id,
            file_sha256=sha,
            file_text=text,
            capital_at_signing_inr=Decimal(str(settings.trading_capital_inr)),
        )
        session.add(row)
        await session.commit()
        click.echo(click.style(f"\n[OK] Signed. id={row.id} sha={sha[:16]}...", fg="green"))

    if not settings.live_trading:
        click.echo(click.style(
            "\nNote: LIVE_TRADING=false in .env. Set to true to complete lock #1.",
            fg="yellow",
        ))
    return 0


@click.command()
def main() -> None:
    """Sign the live-trading acknowledgment (lock #3 of 3)."""
    configure_logging()
    rc = asyncio.run(_sign())
    sys.exit(rc)


if __name__ == "__main__":
    main()

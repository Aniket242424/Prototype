"""
Token-expiry watcher — Phase 6.

Background asyncio task that periodically checks whether the Upstox token
is approaching expiry (or already expired) and fires a Telegram alert with
a one-click re-auth URL.

Design:
- Runs every 60 seconds.
- Token expires daily at 03:30 IST (Upstox policy).
- Alert fires:
    a) When token is already expired (sent once per dedup window).
    b) When token has < 15 minutes left to expiry (early-warning).
- Heavy use of dedup keys so we don't spam the operator.
"""
from __future__ import annotations

import asyncio

from sqlalchemy import select

from trading_agent.core.config import AppSettings, get_settings
from trading_agent.core.logging import get_logger
from trading_agent.core.time_utils import now_ist
from trading_agent.infrastructure.db import session_scope
from trading_agent.infrastructure.models import TokenRow
from trading_agent.monitoring.telegram_alerter import alert

log = get_logger(__name__)

UPSTOX_APPS_URL = "https://account.upstox.com/developer/apps"


# Operator picks up Telegram → taps URL → on Upstox dashboard taps the
# "Generate" button → copies the JWT → pastes back to the bot as `/token <jwt>`.
# The Phase 6.2 Telegram listener handles validation + DB persistence.
def _reauth_instructions() -> str:
    return (
        f'1. Open <a href="{UPSTOX_APPS_URL}">Upstox apps</a>\n'
        "2. Tap <b>Generate</b> next to <i>Access Token</i>\n"
        "3. Reply here: <code>/token &lt;paste-jwt&gt;</code>\n"
        "Bot will validate + persist it (~5 seconds)."
    )


# ============================================================
# Token state evaluation
# ============================================================

def _is_valid(issued_at) -> bool:
    """Mirrors TokenManager._is_token_valid — issued after most recent 03:30 IST cutoff."""
    from trading_agent.core.time_utils import IST
    now = now_ist()
    cutoff = now.replace(hour=3, minute=30, second=0, microsecond=0)
    if now < cutoff:
        cutoff = cutoff.replace(day=now.day - 1) if now.day > 1 else cutoff
    return issued_at.astimezone(IST) >= cutoff


def _seconds_to_next_expiry() -> int:
    """Returns seconds until the next 03:30 IST cutoff (positive). 0 if past today's cutoff."""
    now = now_ist()
    today_cutoff = now.replace(hour=3, minute=30, second=0, microsecond=0)
    if now < today_cutoff:
        target = today_cutoff
    else:
        # Next cutoff is tomorrow 03:30 IST
        from datetime import timedelta
        target = today_cutoff + timedelta(days=1)
    return int((target - now).total_seconds())


# ============================================================
# Watcher loop
# ============================================================

async def token_watcher_loop(
    poll_interval_sec: int = 60,
    early_warning_min: int = 15,
):
    """
    Background task that monitors Upstox token expiry and sends Telegram alerts.

    Args:
      poll_interval_sec: How often to check. 60s is fine — token expires once/day.
      early_warning_min: Send a "token expiring soon" alert this many minutes
                         before the 03:30 IST cutoff. Default 15 min.

    This coroutine runs forever. Cancel via asyncio task.cancel() on shutdown.
    """
    settings = get_settings()
    log.info(
        "token_watcher.starting",
        poll_interval_sec=poll_interval_sec,
        early_warning_min=early_warning_min,
    )

    while True:
        try:
            await _check_once(settings, early_warning_min)
        except asyncio.CancelledError:
            log.info("token_watcher.cancelled")
            raise
        except Exception as e:
            log.warning("token_watcher.tick_failed", error=str(e))
        await asyncio.sleep(poll_interval_sec)


async def _check_once(settings: AppSettings, early_warning_min: int) -> None:
    """Single tick of the watcher. Idempotent — safe to call repeatedly."""
    # Load the most recent token row from DB (we have at most one user typically)
    async with session_scope() as session:
        row = (
            await session.execute(
                select(TokenRow).order_by(TokenRow.issued_at.desc()).limit(1)
            )
        ).scalar_one_or_none()

    instructions = _reauth_instructions()
    today_key = now_ist().date().isoformat()  # dedup key: one alert per day per kind

    if row is None:
        # No token at all — alert once and quit
        await alert(
            "token_expiry",
            (
                "<b>⚠️ Upstox: No token stored</b>\n"
                "The bot has never authenticated.\n\n" + instructions
            ),
            dedup_key=f"no_token_{today_key}",
        )
        return

    if not _is_valid(row.issued_at):
        # Token expired — instruct via /token command
        await alert(
            "token_expiry",
            (
                "<b>🔄 Upstox token expired — re-auth required</b>\n"
                f"Last token issued at <code>{row.issued_at.isoformat()}</code>\n"
                "Bot is paused on market data until you re-auth.\n\n" + instructions
            ),
            dedup_key=f"expired_{today_key}",
        )
        return

    # Token is still valid. Check if it's about to expire (within early_warning_min)
    secs_left = _seconds_to_next_expiry()
    if 0 < secs_left <= early_warning_min * 60:
        minutes_left = secs_left // 60
        await alert(
            "token_expiry",
            (
                "<b>⏰ Upstox token expiring soon</b>\n"
                f"Token expires in <b>{minutes_left} minutes</b> at 03:30 IST.\n\n"
                + instructions
            ),
            dedup_key=f"warning_{today_key}",
            silent=True,  # low-urgency, don't buzz the phone in the middle of the night
        )

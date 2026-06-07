"""
Telegram listener — Phase 6.2.

Long-polling consumer of inbound Telegram messages. Implements a small
command set used to refresh Upstox auth without touching .env or the OAuth
dialog flow (which Upstox has made unreliable).

Commands (case-insensitive):
    /start                  Welcome + usage.
    /token <access_token>   Validate against Upstox /user/profile, store
                            encrypted via TokenManager.
    /status                 Show DB-stored token info (user, issued_at,
                            valid?).

Auth: only messages from the configured TELEGRAM_CHAT_ID are honored.
Everything else is silently ignored (a misconfigured bot won't accept
random users).

Long-polling uses Telegram's getUpdates with a 25-second timeout. Failures
back off exponentially (1, 2, 4, 8, 16, 30, 30...). Never raises out of
the loop — the listener is meant to run forever.
"""
from __future__ import annotations

import asyncio
import html
import os
from dataclasses import dataclass
from typing import Optional

import httpx
from sqlalchemy import select

from trading_agent.auth.token_manager import TokenManager
from trading_agent.auth.token_validator import (
    TokenValidationError,
    validate_access_token,
)
from trading_agent.auth.upstox_auth import UpstoxToken
from trading_agent.core.config import AppSettings, get_settings
from trading_agent.core.logging import get_logger
from trading_agent.core.time_utils import IST, now_ist
from trading_agent.infrastructure.db import session_scope
from trading_agent.infrastructure.models import TokenRow, UserRow

log = get_logger(__name__)


TELEGRAM_API = "https://api.telegram.org"
LONG_POLL_TIMEOUT_SEC = 25
HTTP_TIMEOUT_SEC = LONG_POLL_TIMEOUT_SEC + 10
BACKOFF_SCHEDULE_SEC = [1, 2, 4, 8, 16, 30]

# /scrip <symbol> -> calls the host sentiment dashboard's lookup API (the engine
# with yfinance lives on the host, not in this container). Default = docker bridge
# gateway; override via SCRIP_LOOKUP_URL if the compose network gateway differs.
SCRIP_LOOKUP_URL = os.getenv("SCRIP_LOOKUP_URL", "http://172.18.0.1:8002/api/lookup")


@dataclass
class _Update:
    update_id: int
    chat_id: int
    text: str
    from_user: str  # display name for logging


# ============================================================
# Bot HTTP helpers
# ============================================================

async def _api_get(
    bot_token: str, method: str, params: dict, timeout_sec: float
) -> dict:
    url = f"{TELEGRAM_API}/bot{bot_token}/{method}"
    async with httpx.AsyncClient(timeout=timeout_sec) as client:
        r = await client.get(url, params=params)
    r.raise_for_status()
    payload = r.json()
    if not payload.get("ok", False):
        raise RuntimeError(f"Telegram API {method} returned not-ok: {payload}")
    return payload


async def _send_message(
    bot_token: str, chat_id: int, text: str, *, parse_mode: str = "HTML"
) -> None:
    url = f"{TELEGRAM_API}/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(url, json=payload)
        if r.status_code != 200:
            log.warning(
                "telegram_listener.send_failed",
                status=r.status_code,
                body=r.text[:200],
            )
    except Exception as e:
        log.warning("telegram_listener.send_exception", error=str(e))


def _parse_updates(raw_updates: list[dict]) -> list[_Update]:
    parsed: list[_Update] = []
    for u in raw_updates:
        msg = u.get("message") or u.get("edited_message")
        if not msg:
            continue
        chat = msg.get("chat") or {}
        chat_id = chat.get("id")
        text = msg.get("text") or ""
        sender = msg.get("from") or {}
        from_user = sender.get("username") or sender.get("first_name") or "?"
        if chat_id is None or not text:
            continue
        parsed.append(
            _Update(
                update_id=u["update_id"],
                chat_id=int(chat_id),
                text=text,
                from_user=from_user,
            )
        )
    return parsed


# ============================================================
# Command handlers
# ============================================================

async def _handle_start(bot_token: str, chat_id: int) -> None:
    await _send_message(
        bot_token,
        chat_id,
        (
            "<b>Trading agent — auth bot</b>\n\n"
            "Commands:\n"
            "<code>/token &lt;access_token&gt;</code> — store fresh Upstox token\n"
            "<code>/status</code> — show current token state\n"
            "<code>/scrip &lt;name&gt;</code> — EMA support table for any scrip "
            "(e.g. <code>/scrip reliance</code>, <code>/scrip nifty</code>)\n\n"
            "To get a token: open "
            '<a href="https://account.upstox.com/developer/apps">Upstox apps</a> '
            "and tap <b>Generate</b> next to Access Token, then send it here as:\n"
            "<code>/token eyJ0eXAiOi...</code>"
        ),
    )


async def _handle_status(bot_token: str, chat_id: int) -> None:
    async with session_scope() as session:
        row = (
            await session.execute(
                select(TokenRow).order_by(TokenRow.issued_at.desc()).limit(1)
            )
        ).scalar_one_or_none()

    if row is None:
        await _send_message(
            bot_token,
            chat_id,
            "<b>No token stored.</b>\nUse <code>/token &lt;access_token&gt;</code> to add one.",
        )
        return

    is_valid = TokenManager._is_token_valid(row.issued_at)
    issued_ist = row.issued_at.astimezone(IST).isoformat(timespec="seconds")
    status_emoji = "✅" if is_valid else "❌"
    status_word = "valid" if is_valid else "EXPIRED"

    await _send_message(
        bot_token,
        chat_id,
        (
            f"<b>Token status: {status_emoji} {status_word}</b>\n"
            f"User: <code>{html.escape(row.user_id)}</code>"
            f" ({html.escape(row.user_name or '?')})\n"
            f"Broker: <code>{html.escape(row.broker)}</code>\n"
            f"Issued at: <code>{issued_ist}</code>"
        ),
    )


async def _handle_token(
    bot_token: str, chat_id: int, args: str, settings: AppSettings
) -> None:
    raw = args.strip().strip("`").strip()
    if not raw:
        await _send_message(
            bot_token,
            chat_id,
            "Usage: <code>/token &lt;access_token&gt;</code>\n"
            "Paste the JWT exactly as Upstox gave you (starts with <code>eyJ...</code>).",
        )
        return
    # Basic sanity check (Upstox tokens are JWTs)
    if raw.count(".") != 2 or not raw.startswith("eyJ"):
        await _send_message(
            bot_token,
            chat_id,
            "That doesn't look like a JWT. Expected three dot-separated parts starting with <code>eyJ</code>.",
        )
        return

    # Validate against Upstox /user/profile
    await _send_message(bot_token, chat_id, "Validating against Upstox /user/profile ...")
    try:
        validated = await validate_access_token(raw, settings)
    except TokenValidationError as e:
        await _send_message(
            bot_token,
            chat_id,
            f"❌ <b>Token rejected by Upstox</b>\n<code>{html.escape(str(e))}</code>",
        )
        return

    # Persist via TokenManager (encrypted at rest)
    tm = TokenManager(settings)
    token = UpstoxToken(
        access_token=validated.access_token,
        extended_token=None,
        user_id=validated.user_id,
        user_name=validated.user_name,
        email=validated.email,
        broker=validated.broker,
        issued_at_ist_iso=now_ist().isoformat(),
    )
    try:
        async with session_scope() as session:
            # Ensure the user row exists (FK is not enforced for tokens but
            # downstream code expects a users row)
            if await session.get(UserRow, validated.user_id) is None:
                session.add(
                    UserRow(
                        user_id=validated.user_id,
                        display_name=validated.user_name,
                        email=validated.email,
                    )
                )
                await session.flush()
            await tm.save(session, token)
    except Exception as e:
        log.exception("telegram_listener.token_save_failed")
        await _send_message(
            bot_token,
            chat_id,
            f"⚠️ <b>Token validated but DB write failed</b>\n<code>{html.escape(str(e))}</code>",
        )
        return

    await _send_message(
        bot_token,
        chat_id,
        (
            f"✅ <b>Token stored.</b>\n"
            f"User: <code>{html.escape(validated.user_id)}</code>"
            f" ({html.escape(validated.user_name or '?')})\n"
            f"Valid until next 03:30 IST cutoff."
        ),
    )


def _format_scrip(d: dict) -> str:
    """Format a scrip-lookup JSON (from the host /api/lookup) into a Telegram
    message: header, support/resistance, and a monospace EMA table."""
    if not d or d.get("error"):
        return f"❌ {html.escape((d or {}).get('error', 'lookup failed'))}"
    name = html.escape(str(d.get("name", "")))
    tk = html.escape(str(d.get("ticker", "")))
    price = d.get("price") or 0
    rsi = d.get("rsi14")
    ns = d.get("nearest_support") or {}
    sf = d.get("structural_floor") or {}
    cr = d.get("controlling_resistance") or {}
    head = (f"📊 <b>{name}</b> <code>[{tk}]</code>\n"
            f"{price:,.2f} · RSI {rsi} · {html.escape(str(d.get('trend', '')))}"
            f" · {html.escape(str(d.get('stack_daily', '')))} stack")
    if d.get("no_ema_support") and ns:
        sup = (f"\n\n⚠ <b>NO EMA support</b> — below every EMA.\n"
               f"Floor {ns.get('value', 0):,.2f} (20d low).")
        if cr:
            sup += f"\nNearest EMA {cr.get('value', 0):,.2f} = RESISTANCE ({cr.get('pct', 0):+.1f}%)."
    else:
        hr = f", held {ns['hold_rate']}%" if ns.get("hold_rate") is not None else ""
        sup = (f"\n\n▲ <b>SUPPORT</b> {ns.get('value', 0):,.2f} "
               f"({html.escape(ns.get('members', ''))}, {ns.get('pct', 0):+.1f}%, {ns.get('grade', '')}{hr})")
        if sf and sf.get("value") != ns.get("value"):
            sup += f"\n   floor {sf.get('value', 0):,.2f} ({html.escape(sf.get('members', ''))}, {sf.get('grade', '')})"
        if cr:
            sup += f"\n▼ <b>RESISTANCE</b> {cr.get('value', 0):,.2f} ({html.escape(cr.get('members', ''))}, {cr.get('pct', 0):+.1f}%)"
    b = d.get("latest_bounce") or {}
    daily, weekly = b.get("daily") or [], b.get("weekly") or []
    if daily or weekly:
        sup += "\n↩ <b>Last bounces</b>:"
        for tf_name, lst in (("Daily", daily), ("Weekly", weekly)):
            if not lst:
                continue
            rows = "; ".join(
                f"{html.escape(str(x.get('ema', '')))} {html.escape(str(x.get('date', '')))} "
                f"{x.get('from_px'):,.2f}→{x.get('to_px'):,.2f} (+{x.get('rally_pct')}%)"
                for x in lst)
            sup += f"\n  <i>{tf_name}</i>: {rows}"
    else:
        sup += "\n↩ <i>No significant EMA bounce recently</i>"
    m = d.get("matrix") or {}

    def _cell(tf: str, sp: int) -> str:
        c = m.get(f"{tf}{sp}") or {}
        v = c.get("v")
        if v is None:
            return "    n/a "
        ar = "▲" if c.get("role") == "support" else "▼"
        return f"{v:>8,.0f}{ar}"

    rows = "TF    20EMA      50EMA      200EMA\n"
    for tf in ("D", "W", "M"):
        rows += f"{tf}  {_cell(tf, 20)} {_cell(tf, 50)} {_cell(tf, 200)}\n"
    table = f"\n<pre>{rows}</pre>"
    foot = "<i>▲=support ▼=resistance · held% = held/tested historically · not advice</i>"
    return head + sup + table + foot


async def _handle_scrip(bot_token: str, chat_id: int, args: str) -> None:
    q = args.strip()
    if not q:
        await _send_message(
            bot_token, chat_id,
            "Usage: <code>/scrip &lt;name or ticker&gt;</code>\n"
            "E.g. <code>/scrip reliance</code>, <code>/scrip nifty</code>, <code>/scrip AAPL</code>",
        )
        return
    await _send_message(bot_token, chat_id, f"🔎 Looking up <b>{html.escape(q)}</b>…")
    try:
        async with httpx.AsyncClient(timeout=90.0) as client:
            r = await client.get(SCRIP_LOOKUP_URL, params={"q": q, "fmt": "json"})
        data = r.json()
    except Exception as e:
        await _send_message(bot_token, chat_id, f"❌ lookup failed: <code>{html.escape(str(e))}</code>")
        return
    await _send_message(bot_token, chat_id, _format_scrip(data))


# ============================================================
# Dispatcher
# ============================================================

async def _dispatch(
    update: _Update,
    bot_token: str,
    allowed_chat_id: int,
    settings: AppSettings,
) -> None:
    if update.chat_id != allowed_chat_id:
        log.warning(
            "telegram_listener.unauthorized_chat",
            chat_id=update.chat_id,
            from_user=update.from_user,
            text_preview=update.text[:30],
        )
        return  # silent ignore

    text = update.text.strip()
    log.info(
        "telegram_listener.command",
        from_user=update.from_user,
        cmd_preview=text.split()[0] if text else "",
    )

    # Split into command + args
    parts = text.split(maxsplit=1)
    cmd = parts[0].lower().lstrip("/")
    # Telegram appends @botname in groups: /token@MyBot ... — strip it
    cmd = cmd.split("@", 1)[0]
    args = parts[1] if len(parts) > 1 else ""

    if cmd == "start" or cmd == "help":
        await _handle_start(bot_token, update.chat_id)
    elif cmd == "status":
        await _handle_status(bot_token, update.chat_id)
    elif cmd == "token":
        await _handle_token(bot_token, update.chat_id, args, settings)
    elif cmd in ("scrip", "s"):
        await _handle_scrip(bot_token, update.chat_id, args)
    else:
        await _send_message(
            bot_token,
            update.chat_id,
            "Unknown command. Try <code>/start</code>.",
        )


# ============================================================
# Main loop
# ============================================================

async def listen_forever() -> None:
    """
    Run the long-polling loop. Never returns under normal conditions.
    Cancel via task.cancel() on shutdown.
    """
    settings = get_settings()
    bot_token_secret = settings.telegram_bot_token
    chat_id_str = settings.telegram_chat_id
    if not bot_token_secret or not chat_id_str:
        raise RuntimeError(
            "Telegram listener requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env"
        )
    bot_token = bot_token_secret.get_secret_value()
    try:
        allowed_chat_id = int(chat_id_str)
    except ValueError as e:
        raise RuntimeError(f"TELEGRAM_CHAT_ID must be an integer: {chat_id_str}") from e

    log.info("telegram_listener.starting", allowed_chat_id=allowed_chat_id)

    offset: Optional[int] = None
    backoff_idx = 0

    while True:
        try:
            params = {"timeout": LONG_POLL_TIMEOUT_SEC}
            if offset is not None:
                params["offset"] = offset
            payload = await _api_get(
                bot_token, "getUpdates", params, timeout_sec=HTTP_TIMEOUT_SEC
            )
            backoff_idx = 0  # reset on success

            updates = _parse_updates(payload.get("result", []))
            for u in updates:
                offset = u.update_id + 1  # advance past this update
                try:
                    await _dispatch(u, bot_token, allowed_chat_id, settings)
                except Exception:
                    log.exception("telegram_listener.dispatch_failed")
        except asyncio.CancelledError:
            log.info("telegram_listener.cancelled")
            raise
        except Exception as e:
            wait = BACKOFF_SCHEDULE_SEC[
                min(backoff_idx, len(BACKOFF_SCHEDULE_SEC) - 1)
            ]
            log.warning(
                "telegram_listener.poll_failed",
                error=str(e),
                retry_in_sec=wait,
            )
            backoff_idx += 1
            await asyncio.sleep(wait)

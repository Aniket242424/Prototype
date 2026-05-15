"""
Telegram alerter — Phase 6 first slice.

Sends messages to a Telegram chat via the Bot API. Used for:
- Token-expiry warnings ("re-auth via this URL")
- Kill-switch trips
- Trade entry/exit events (paper or live)
- Daily PnL summary at 15:30 IST

Configuration in .env:
    TELEGRAM_BOT_TOKEN=1234567890:ABC...
    TELEGRAM_CHAT_ID=987654321

If either is unset/empty, the alerter is a no-op (dev mode).

Design notes:
- Never raises. Telegram outages MUST NOT break the trading loop.
- Rate-limited: at most one alert per (kind, dedup_key) per 5 min window.
  Prevents spam if the trading loop fires the same alert in a tight cycle.
- HTML-formatted messages (Telegram's `parse_mode=HTML`) — safer than Markdown
  because it doesn't choke on stray *, _, etc.
"""
from __future__ import annotations

import asyncio
import time
from typing import Literal

import httpx

from trading_agent.core.config import AppSettings, get_settings
from trading_agent.core.logging import get_logger

log = get_logger(__name__)


AlertKind = Literal[
    "token_expiry",
    "kill_switch",
    "worker_down",
    "trade_entry",
    "trade_exit",
    "daily_summary",
    "info",
    "error",
]


# Per-process dedup cache: { (kind, dedup_key): last_sent_epoch }
_dedup_cache: dict[tuple[str, str], float] = {}
_DEDUP_WINDOW_SEC = 300  # 5 minutes


class TelegramAlerter:
    """
    Wraps the Telegram Bot API. Single instance per process recommended.

    Usage:
        alerter = TelegramAlerter()
        await alerter.send("token_expiry", "Re-auth required", dedup_key="daily")
    """

    BASE_URL = "https://api.telegram.org"

    def __init__(self, settings: AppSettings | None = None, timeout_sec: float = 5.0):
        self._settings = settings or get_settings()
        self._timeout_sec = timeout_sec
        token_secret = self._settings.telegram_bot_token
        self._bot_token = token_secret.get_secret_value() if token_secret else ""
        self._chat_id = self._settings.telegram_chat_id or ""
        self._enabled = bool(self._bot_token and self._chat_id)
        if not self._enabled:
            log.info("telegram.disabled", reason="bot_token or chat_id not configured")

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def send(
        self,
        kind: AlertKind,
        message: str,
        *,
        dedup_key: str = "",
        disable_notification: bool = False,
    ) -> bool:
        """
        Send an HTML-formatted message to the configured chat.

        Returns True if Telegram accepted the message, False otherwise.
        Never raises.

        Args:
          kind: A logical category used for logging + dedup. Free-form but
                prefer the AlertKind literals.
          message: The text. May contain a small subset of HTML — <b>, <i>, <code>, <a href=...>.
                   Other HTML is escaped by Telegram if invalid.
          dedup_key: If non-empty, suppress repeat alerts of the same
                     (kind, dedup_key) within DEDUP_WINDOW_SEC.
          disable_notification: True = silent (no phone buzz). Use for low-urgency.
        """
        if not self._enabled:
            return False

        if dedup_key:
            cache_key = (kind, dedup_key)
            now = time.monotonic()
            last = _dedup_cache.get(cache_key)
            if last is not None and (now - last) < _DEDUP_WINDOW_SEC:
                log.debug("telegram.deduped", kind=kind, dedup_key=dedup_key)
                return False
            _dedup_cache[cache_key] = now

        url = f"{self.BASE_URL}/bot{self._bot_token}/sendMessage"
        payload = {
            "chat_id": self._chat_id,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "disable_notification": disable_notification,
        }

        try:
            async with httpx.AsyncClient(timeout=self._timeout_sec) as client:
                resp = await client.post(url, json=payload)
            if resp.status_code == 200:
                log.info("telegram.sent", kind=kind)
                return True
            log.warning(
                "telegram.send_failed",
                kind=kind,
                status=resp.status_code,
                body=resp.text[:200],
            )
            return False
        except Exception as e:
            log.warning("telegram.exception", kind=kind, error=str(e))
            return False


# ---- Module-level singleton ----
_singleton: TelegramAlerter | None = None


def get_alerter() -> TelegramAlerter:
    global _singleton
    if _singleton is None:
        _singleton = TelegramAlerter()
    return _singleton


# ---- Convenience top-level functions ----

async def alert(
    kind: AlertKind,
    message: str,
    *,
    dedup_key: str = "",
    silent: bool = False,
) -> bool:
    """Fire-and-forget alert. Returns False on failure (never raises)."""
    return await get_alerter().send(
        kind, message, dedup_key=dedup_key, disable_notification=silent
    )

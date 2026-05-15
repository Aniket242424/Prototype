"""Tests for the Telegram alerter — Phase 6."""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from trading_agent.monitoring.telegram_alerter import (
    TelegramAlerter,
    _dedup_cache,
)


@pytest.fixture(autouse=True)
def _clear_dedup_cache():
    """Each test starts with an empty dedup cache."""
    _dedup_cache.clear()
    yield
    _dedup_cache.clear()


def _settings_with_telegram(token: str = "fake-token", chat_id: str = "12345"):
    """Build an AppSettings clone with Telegram configured."""
    from pydantic import SecretStr
    from trading_agent.core.config import get_settings
    s = get_settings()
    return s.model_copy(update={
        "telegram_bot_token": SecretStr(token) if token else None,
        "telegram_chat_id": chat_id,
    })


def _settings_without_telegram():
    """AppSettings with Telegram disabled (no bot token)."""
    from trading_agent.core.config import get_settings
    s = get_settings()
    return s.model_copy(update={
        "telegram_bot_token": None,
        "telegram_chat_id": "",
    })


# ----------- enabled / disabled state -----------

def test_alerter_disabled_when_no_token():
    alerter = TelegramAlerter(settings=_settings_without_telegram())
    assert alerter.enabled is False


def test_alerter_disabled_when_no_chat_id():
    from pydantic import SecretStr
    from trading_agent.core.config import get_settings
    s = get_settings().model_copy(update={
        "telegram_bot_token": SecretStr("x"),
        "telegram_chat_id": "",
    })
    alerter = TelegramAlerter(settings=s)
    assert alerter.enabled is False


def test_alerter_enabled_when_both_set():
    alerter = TelegramAlerter(settings=_settings_with_telegram())
    assert alerter.enabled is True


# ----------- send behavior -----------

async def test_send_returns_false_when_disabled():
    alerter = TelegramAlerter(settings=_settings_without_telegram())
    result = await alerter.send("info", "hello")
    assert result is False


async def test_send_posts_to_telegram_api():
    """When enabled, send should POST to api.telegram.org with correct payload."""
    alerter = TelegramAlerter(settings=_settings_with_telegram(
        token="bot-token-123", chat_id="chat-456"
    ))

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.post = AsyncMock(return_value=mock_resp)

    with patch("httpx.AsyncClient", return_value=mock_client):
        result = await alerter.send("info", "<b>hello</b>")

    assert result is True
    mock_client.post.assert_called_once()
    call = mock_client.post.call_args
    url = call.args[0]
    payload = call.kwargs["json"]
    assert url == "https://api.telegram.org/botbot-token-123/sendMessage"
    assert payload["chat_id"] == "chat-456"
    assert payload["text"] == "<b>hello</b>"
    assert payload["parse_mode"] == "HTML"
    assert payload["disable_web_page_preview"] is True


async def test_send_returns_false_on_non_200():
    alerter = TelegramAlerter(settings=_settings_with_telegram())
    mock_resp = MagicMock()
    mock_resp.status_code = 400
    mock_resp.text = "Bad Request: chat not found"
    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.post = AsyncMock(return_value=mock_resp)

    with patch("httpx.AsyncClient", return_value=mock_client):
        result = await alerter.send("info", "msg")
    assert result is False


async def test_send_never_raises_on_exception():
    """Telegram exceptions (timeouts, network errors) must NOT bubble up."""
    alerter = TelegramAlerter(settings=_settings_with_telegram())
    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.post = AsyncMock(side_effect=Exception("network down"))

    with patch("httpx.AsyncClient", return_value=mock_client):
        result = await alerter.send("error", "msg")
    # Returns False but does NOT raise
    assert result is False


# ----------- dedup behavior -----------

async def test_dedup_blocks_second_alert_in_window():
    """Same (kind, dedup_key) within 5 min → second send is suppressed."""
    alerter = TelegramAlerter(settings=_settings_with_telegram())
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.post = AsyncMock(return_value=mock_resp)

    with patch("httpx.AsyncClient", return_value=mock_client):
        r1 = await alerter.send("token_expiry", "msg1", dedup_key="daily")
        r2 = await alerter.send("token_expiry", "msg2", dedup_key="daily")

    assert r1 is True
    assert r2 is False  # deduped
    assert mock_client.post.call_count == 1


async def test_dedup_does_not_block_different_keys():
    """Same kind but different dedup_key → both go through."""
    alerter = TelegramAlerter(settings=_settings_with_telegram())
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.post = AsyncMock(return_value=mock_resp)

    with patch("httpx.AsyncClient", return_value=mock_client):
        r1 = await alerter.send("trade_entry", "msg1", dedup_key="trade_001")
        r2 = await alerter.send("trade_entry", "msg2", dedup_key="trade_002")

    assert r1 is True
    assert r2 is True
    assert mock_client.post.call_count == 2


async def test_dedup_window_expires():
    """After window passes, the same key fires again."""
    alerter = TelegramAlerter(settings=_settings_with_telegram())
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.post = AsyncMock(return_value=mock_resp)

    with patch("httpx.AsyncClient", return_value=mock_client):
        r1 = await alerter.send("kill_switch", "trip 1", dedup_key="trip")
        # Manually expire the cache entry
        for k in list(_dedup_cache):
            _dedup_cache[k] -= 1000  # push it 1000s into the past
        r2 = await alerter.send("kill_switch", "trip 2", dedup_key="trip")

    assert r1 is True
    assert r2 is True


async def test_empty_dedup_key_never_deduplicated():
    """When dedup_key is empty, every call goes through."""
    alerter = TelegramAlerter(settings=_settings_with_telegram())
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.post = AsyncMock(return_value=mock_resp)

    with patch("httpx.AsyncClient", return_value=mock_client):
        r1 = await alerter.send("info", "1")
        r2 = await alerter.send("info", "2")
        r3 = await alerter.send("info", "3")

    assert all([r1, r2, r3])
    assert mock_client.post.call_count == 3


# ----------- silent flag -----------

async def test_silent_alert_sets_disable_notification():
    alerter = TelegramAlerter(settings=_settings_with_telegram())
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.post = AsyncMock(return_value=mock_resp)

    with patch("httpx.AsyncClient", return_value=mock_client):
        await alerter.send("info", "quiet", disable_notification=True)

    payload = mock_client.post.call_args.kwargs["json"]
    assert payload["disable_notification"] is True

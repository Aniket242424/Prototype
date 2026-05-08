"""Upstox OAuth2 endpoints."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse

from trading_agent.auth.token_manager import TokenManager
from trading_agent.auth.upstox_auth import UpstoxAuth
from trading_agent.core.config import get_settings
from trading_agent.core.logging import get_logger
from trading_agent.infrastructure.db import session_scope

log = get_logger(__name__)
router = APIRouter(prefix="/auth/upstox", tags=["auth"])


@router.get("/login")
async def login_redirect():
    settings = get_settings()
    auth = UpstoxAuth(settings)
    return RedirectResponse(auth.authorize_url())


@router.get("/callback", response_class=HTMLResponse)
async def callback(code: str = Query(...), state: str | None = None):
    settings = get_settings()
    auth = UpstoxAuth(settings)
    try:
        token = await auth.exchange_code(code)
    except Exception as e:
        log.error("auth.callback_failed", error=str(e))
        raise HTTPException(status_code=400, detail=f"Token exchange failed: {e}")

    tm = TokenManager(settings)
    async with session_scope() as session:
        # Ensure user row exists (single-user system)
        from trading_agent.infrastructure.models import UserRow
        user = await session.get(UserRow, token.user_id)
        if user is None:
            session.add(UserRow(
                user_id=token.user_id,
                display_name=token.user_name,
                email=token.email,
            ))
            await session.commit()
        await tm.save(session, token)

    return f"""
    <html><body style="font-family:monospace;padding:2rem">
      <h2>Upstox auth complete</h2>
      <p>user_id: <b>{token.user_id}</b></p>
      <p>You can close this tab.</p>
    </body></html>
    """

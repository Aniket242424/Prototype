"""
HTTP Basic Auth dependency for protecting the dashboard and control endpoints
when the app is exposed publicly (e.g., via Cloudflare Tunnel).

Behavior:
- If DASHBOARD_USERNAME and DASHBOARD_PASSWORD are both set in .env, the dependency
  enforces credentials on every protected request.
- If either is unset (typical for local dev), the dependency is a no-op (allows all).

This means production deployments MUST set both env vars to enable auth.
"""
from __future__ import annotations

import secrets

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from trading_agent.core.config import get_settings

_security = HTTPBasic(auto_error=False)


def verify_credentials(
    credentials: HTTPBasicCredentials | None = Depends(_security),
) -> str:
    """
    Validate HTTP Basic Auth. Returns the username on success.

    No-op if DASHBOARD_USERNAME or DASHBOARD_PASSWORD is empty/unset
    (dev-mode pass-through).
    """
    settings = get_settings()
    expected_user = settings.dashboard_username
    expected_pass_secret = settings.dashboard_password

    # No auth configured → allow (local dev mode)
    if not expected_user or expected_pass_secret is None:
        return "anonymous"

    expected_pass = expected_pass_secret.get_secret_value()
    if not expected_pass:
        return "anonymous"

    # Auth required but no credentials sent
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Basic"},
        )

    # Constant-time comparison to prevent timing attacks
    user_ok = secrets.compare_digest(
        credentials.username.encode("utf-8"), expected_user.encode("utf-8")
    )
    pass_ok = secrets.compare_digest(
        credentials.password.encode("utf-8"), expected_pass.encode("utf-8")
    )

    if not (user_ok and pass_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )

    return credentials.username

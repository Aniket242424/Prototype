"""Shared pytest fixtures."""
from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True, scope="session")
def _set_test_env():
    """Provide minimal env so config import doesn't fail in unit tests."""
    os.environ.setdefault("UPSTOX_API_KEY", "test_key")
    os.environ.setdefault("UPSTOX_API_SECRET", "test_secret")
    os.environ.setdefault("UPSTOX_REDIRECT_URI", "http://localhost:8000/auth/upstox/callback")
    os.environ.setdefault("ANTHROPIC_API_KEY", "test_key")
    os.environ.setdefault("POSTGRES_PASSWORD", "test_pw")
    # Generate-once Fernet key for tests (DO NOT use in real env)
    os.environ.setdefault(
        "TOKEN_ENCRYPTION_KEY",
        "ZmRzZmRzZmRzZmRzZmRzZmRzZmRzZmRzZmRzZmRzZmRzZA==",
    )
    yield

"""
Shared pytest fixtures.

IMPORTANT: env vars must be set at MODULE LOAD time (not in a fixture),
because production code modules call get_settings() during their own import.
Pytest collects tests by importing them, which transitively imports
infrastructure/db.py → calls get_settings() → AppSettings requires fields.
A session-scoped fixture would run AFTER collection, by which point the
import has already failed.
"""
from __future__ import annotations

import os

# Set BEFORE any production-code import, including imports done by test
# modules during collection. Use .setdefault so a real env var (if set by
# CI or a developer) takes precedence.
os.environ.setdefault("UPSTOX_API_KEY", "test_key")
os.environ.setdefault("UPSTOX_API_SECRET", "test_secret")
os.environ.setdefault("UPSTOX_REDIRECT_URI", "http://localhost:8000/auth/upstox/callback")
os.environ.setdefault("ANTHROPIC_API_KEY", "test_key")
os.environ.setdefault("POSTGRES_PASSWORD", "test_pw")
# Valid 32-byte URL-safe-base64 Fernet key for tests only. DO NOT reuse in prod.
os.environ.setdefault(
    "TOKEN_ENCRYPTION_KEY",
    "ZmRzZmRzZmRzZmRzZmRzZmRzZmRzZmRzZmRzZmRzZmRzZA==",
)

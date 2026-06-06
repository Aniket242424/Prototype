"""
Tiny shared key-store for the sentiment agent + dashboard.

Lets the operator set API keys from the UI (written here), with .env as the
fallback. Encrypted at rest with TOKEN_ENCRYPTION_KEY (Fernet) when available;
otherwise stored in a 0600 file. Both run_sentiment_agent.py and ic_dashboard.py
import this (they live in scripts/, which is on sys.path[0]).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

KEYS_FILE = Path("data/agent_keys.json")


def _fernet():
    k = os.getenv("TOKEN_ENCRYPTION_KEY")
    if not k:
        return None
    try:
        from cryptography.fernet import Fernet
        return Fernet(k.encode() if isinstance(k, str) else k)
    except Exception:
        return None


def _load() -> dict:
    if KEYS_FILE.exists():
        try:
            return json.loads(KEYS_FILE.read_text())
        except Exception:
            return {}
    return {}


def set_key(name: str, value: str) -> None:
    KEYS_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = _load()
    f = _fernet()
    if f:
        data[name] = {"enc": True, "val": f.encrypt(value.encode()).decode()}
    else:
        data[name] = {"enc": False, "val": value}
    tmp = KEYS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, KEYS_FILE)
    try:
        os.chmod(KEYS_FILE, 0o600)
    except Exception:
        pass


def get_key(name: str, env_fallback: str | None = None) -> str | None:
    """UI-set key (decrypted) if present, else the env var."""
    e = _load().get(name)
    if e:
        if e.get("enc"):
            f = _fernet()
            if f:
                try:
                    return f.decrypt(e["val"].encode()).decode()
                except Exception:
                    pass
        else:
            return e.get("val")
    return os.getenv(env_fallback) if env_fallback else None


def masked(name: str, env_fallback: str | None = None) -> str | None:
    """Masked display, e.g. 'sk-ant-…AB12'. None if no key set anywhere."""
    v = get_key(name, env_fallback)
    if not v:
        return None
    return (v[:7] + "…" + v[-4:]) if len(v) > 12 else "set"


def source(name: str, env_fallback: str | None = None) -> str:
    """Where the key comes from: 'ui', 'env', or 'none'."""
    if name in _load():
        return "ui"
    if env_fallback and os.getenv(env_fallback):
        return "env"
    return "none"

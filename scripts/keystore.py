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
USAGE_FILE = Path("data/agent_usage.json")


# ---------- token usage tracking (per backend) ----------
def _load_usage() -> dict:
    if USAGE_FILE.exists():
        try:
            return json.loads(USAGE_FILE.read_text())
        except Exception:
            return {}
    return {}


def _save_usage(d: dict) -> None:
    USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = USAGE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d))
    os.replace(tmp, USAGE_FILE)


def record_usage(backend: str, t_in: int, t_out: int, cost_inr: float) -> None:
    d = _load_usage()
    b = d.setdefault(backend, {"tokens_in": 0, "tokens_out": 0, "cost_inr": 0.0, "calls": 0})
    b["tokens_in"] += int(t_in or 0)
    b["tokens_out"] += int(t_out or 0)
    b["cost_inr"] = round(b["cost_inr"] + float(cost_inr or 0), 2)
    b["calls"] += 1
    _save_usage(d)


def get_usage() -> dict:
    return _load_usage()


def reset_usage(backend: str | None = None) -> None:
    if backend:
        d = _load_usage(); d.pop(backend, None); _save_usage(d)
    else:
        _save_usage({})


def cost_so_far(backend: str) -> float:
    return float(_load_usage().get(backend, {}).get("cost_inr", 0.0))


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


def set_gemini_keys(keys: list[str]) -> None:
    """Store a LIST of Gemini keys (one per Gmail account) for rotation."""
    KEYS_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = _load()
    cleaned = [k.strip() for k in keys if k and k.strip()]
    f = _fernet()
    if f:
        data["gemini_api_keys"] = {"enc": True, "val": f.encrypt(json.dumps(cleaned).encode()).decode()}
    else:
        data["gemini_api_keys"] = {"enc": False, "val": cleaned}
    tmp = KEYS_FILE.with_suffix(".tmp"); tmp.write_text(json.dumps(data)); os.replace(tmp, KEYS_FILE)
    try:
        os.chmod(KEYS_FILE, 0o600)
    except Exception:
        pass


def _ui_gemini_keys() -> list[str]:
    """Just the UI-saved Gemini key list (no env)."""
    e = _load().get("gemini_api_keys")
    if not e:
        return []
    if e.get("enc"):
        fr = _fernet()
        if fr:
            try:
                return json.loads(fr.decrypt(e["val"].encode()).decode())
            except Exception:
                return []
        return []
    return e.get("val") if isinstance(e.get("val"), list) else []


def add_gemini_keys(new_keys: list[str]) -> int:
    """APPEND keys to the UI list (dedup), so adding one at a time works.
    Returns the new total count of UI keys."""
    merged = _ui_gemini_keys() + [k.strip() for k in new_keys if k and k.strip()]
    seen, out = set(), []
    for k in merged:
        if k and k not in seen:
            seen.add(k); out.append(k)
    set_gemini_keys(out)
    return len(out)


def get_gemini_keys() -> list[str]:
    """All Gemini keys: UI list + UI single + env (GEMINI_API_KEY[_2/_3/_4]), deduped."""
    keys: list[str] = []
    e = _load().get("gemini_api_keys")
    if e:
        if e.get("enc"):
            fr = _fernet()
            if fr:
                try:
                    keys += json.loads(fr.decrypt(e["val"].encode()).decode())
                except Exception:
                    pass
        elif isinstance(e.get("val"), list):
            keys += e["val"]
    single = get_key("gemini_api_key")  # UI single or env GEMINI_API_KEY
    if single:
        keys.append(single)
    for env in ("GEMINI_API_KEY", "GEMINI_API_KEY_2", "GEMINI_API_KEY_3", "GEMINI_API_KEY_4"):
        v = os.getenv(env)
        if v:
            keys.append(v)
    seen, out = set(), []
    for k in keys:
        if k and k not in seen:
            seen.add(k); out.append(k)
    return out


def mask_key(k: str) -> str:
    return (k[:8] + "…" + k[-4:]) if k and len(k) > 12 else ("set" if k else "")


def gemini_key_list() -> list[dict]:
    """Each Gemini key in rotation order: {masked, source(ui/env)}."""
    ui = set(_ui_gemini_keys())
    env_vals = {os.getenv(x) for x in
                ("GEMINI_API_KEY", "GEMINI_API_KEY_2", "GEMINI_API_KEY_3", "GEMINI_API_KEY_4")
                if os.getenv(x)}
    out = []
    for i, k in enumerate(get_gemini_keys()):
        src = "ui" if k in ui else ("env" if k in env_vals else "ui")
        out.append({"idx": i + 1, "masked": mask_key(k), "source": src})
    return out


def gemini_keys_summary() -> str:
    ks = get_gemini_keys()
    if not ks:
        return "(none)"
    return f"{len(ks)} key(s): " + ", ".join((k[:6] + "…" + k[-4:]) for k in ks[:3])


def source(name: str, env_fallback: str | None = None) -> str:
    """Where the key comes from: 'ui', 'env', or 'none'."""
    if name in _load():
        return "ui"
    if env_fallback and os.getenv(env_fallback):
        return "env"
    return "none"

"""
Adversarial unit tests for scripts/keystore.py — the shared key/usage/budget store.
Probes the multi-key merge/dedup, append semantics, the UI budget top-up, masking,
and encryption round-trip (conftest sets a valid TOKEN_ENCRYPTION_KEY).
All file paths are monkeypatched to a tmp dir so real data is never touched.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "scripts"))
import keystore  # noqa: E402


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(keystore, "KEYS_FILE", tmp_path / "keys.json")
    monkeypatch.setattr(keystore, "USAGE_FILE", tmp_path / "usage.json")
    monkeypatch.setattr(keystore, "BUDGET_FILE", tmp_path / "budget.json")


def test_add_gemini_keys_appends_and_dedups(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    assert keystore.add_gemini_keys(["AIza_aaa", "AIza_bbb"]) == 2
    assert keystore.add_gemini_keys(["AIza_bbb", "AIza_ccc"]) == 3        # bbb is a dup
    assert keystore._ui_gemini_keys() == ["AIza_aaa", "AIza_bbb", "AIza_ccc"]


def test_add_gemini_keys_ignores_blanks(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    assert keystore.add_gemini_keys(["  ", "", "AIza_x", "   "]) == 1
    assert keystore._ui_gemini_keys() == ["AIza_x"]


def test_get_gemini_keys_merges_env_and_dedups(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    keystore.set_gemini_keys(["UI_1", "UI_2"])
    monkeypatch.setenv("GEMINI_API_KEY", "ENV_1")
    monkeypatch.setenv("GEMINI_API_KEY_2", "UI_1")        # duplicate of a UI key
    keys = keystore.get_gemini_keys()
    assert keys[0:2] == ["UI_1", "UI_2"]
    assert "ENV_1" in keys
    assert len(keys) == len(set(keys))                    # UI_1 appears once, not twice


def test_get_gemini_keys_unlimited_env(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    for i in range(2, 12):
        monkeypatch.setenv(f"GEMINI_API_KEY_{i}", f"ENV_{i}")
    keys = keystore.get_gemini_keys()
    assert "ENV_11" in keys                               # was previously capped at _4


def test_budget_topup_from_unset_uses_base(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    assert keystore.get_budget("anthropic", 100.0) == 100.0      # default when unset
    assert keystore.add_budget("anthropic", 100.0, base=100.0) == 200.0
    assert keystore.get_budget("anthropic", 100.0) == 200.0       # persists
    assert keystore.add_budget("anthropic", 50.0, base=100.0) == 250.0  # adds to existing, not base


def test_usage_accumulates(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    keystore.record_usage("anthropic", 100, 50, 30.0)
    keystore.record_usage("anthropic", 200, 60, 31.42)
    assert keystore.cost_so_far("anthropic") == round(61.42, 2)
    u = keystore.get_usage()["anthropic"]
    assert u["tokens_in"] == 300 and u["tokens_out"] == 110 and u["calls"] == 2


def test_mask_key():
    assert keystore.mask_key("AIzaSyABCDEFGH1234") == "AIzaSyAB…1234"
    assert keystore.mask_key("short") == "set"
    assert keystore.mask_key("") == ""


def test_gemini_keys_encrypted_at_rest_with_valid_key(tmp_path, monkeypatch):
    # With a VALID TOKEN_ENCRYPTION_KEY the raw key must NOT appear in plaintext on disk.
    from cryptography.fernet import Fernet
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
    keystore.set_gemini_keys(["AIza_secret_value"])
    raw = (tmp_path / "keys.json").read_text()
    assert "AIza_secret_value" not in raw                 # stored encrypted
    assert keystore._ui_gemini_keys() == ["AIza_secret_value"]   # round-trips


def test_gemini_keys_plaintext_fallback_when_no_key(tmp_path, monkeypatch):
    # SECURITY DOCUMENTATION: with NO encryption key the store falls back to plaintext.
    # Production always sets a valid key (verified enc=True there); this locks in the
    # known fallback so a misconfig that would expose keys is caught by a diff.
    _isolate(tmp_path, monkeypatch)
    monkeypatch.delenv("TOKEN_ENCRYPTION_KEY", raising=False)
    keystore.set_gemini_keys(["AIza_plain"])
    raw = (tmp_path / "keys.json").read_text()
    assert "AIza_plain" in raw                            # plaintext (documented fallback)
    assert keystore._ui_gemini_keys() == ["AIza_plain"]

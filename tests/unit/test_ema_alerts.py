"""
Adversarial unit tests for scripts/ema_alert_monitor.py — the EMA-proximity trade
alerts. compute_one (which hits the network) is monkeypatched with synthetic
technicals so we test the PURE alert logic: the 0.3% proximity gate, long vs short
selection, the MIN_TESTS probability gate, and the trade-setup arithmetic.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "scripts"))
import ema_alert_monitor as eam  # noqa: E402


def _tech(price, cells, **extra):
    base = {
        "price": price, "name": "Test",
        "nearest_support": {"value": price * 0.97, "members": "D50"},
        "structural_floor": {"value": price * 0.90, "members": "M20", "grade": "STRONG"},
        "controlling_resistance": {"value": price * 1.05, "members": "D200"},
        "latest_bounce": {"daily": [], "weekly": []},
        "matrix": cells,
    }
    base.update(extra)
    return base


def _cell(v, pct, role, rate=None, tests=0, held=0, rrate=None, rtests=0, rejected=0):
    return {"v": v, "pct": pct, "role": role, "rate": rate, "tests": tests, "held": held,
            "reject_rate": rrate, "reject_tests": rtests, "rejected": rejected}


# ----------------------------- proximity gate -----------------------------
def test_no_alert_when_outside_proximity(monkeypatch):
    # Price 1% from the EMA — outside the 0.3% window -> no alert at all.
    t = _tech(100.0, {"D20": _cell(99.0, 1.0, "support", rate=70, tests=100, held=70)})
    monkeypatch.setattr(eam, "compute_one", lambda tk: t)
    monkeypatch.setattr(eam, "NEAR_PCT", 0.3)
    block, hits = eam.scan_one("Test", "TEST")
    assert block == "" and hits == {}


def test_long_setup_within_proximity(monkeypatch):
    t = _tech(100.2, {"D20": _cell(100.0, 0.2, "support", rate=72, tests=120, held=86)},
              latest_bounce={"daily": [{"ema": "20 EMA", "date": "2026-01-01", "from_px": 100,
                                        "to_px": 104, "rally_pct": 4.0, "currently": "support"}], "weekly": []})
    monkeypatch.setattr(eam, "compute_one", lambda tk: t)
    monkeypatch.setattr(eam, "NEAR_PCT", 0.3)
    monkeypatch.setattr(eam, "MIN_TESTS", 8)
    block, hits = eam.scan_one("Test", "TEST")
    assert "LONG" in block and "held <b>72%</b>" in block
    assert "TEST|L|Daily|20" in hits
    assert "SL" in block and "target" in block and "BREAKS down" in block


def test_short_setup_when_below_resistance(monkeypatch):
    # Price just BELOW an EMA acting as resistance -> SHORT using the reject-rate.
    t = _tech(99.8, {"D50": _cell(100.0, -0.2, "resistance", rrate=68, rtests=90, rejected=61)})
    monkeypatch.setattr(eam, "compute_one", lambda tk: t)
    monkeypatch.setattr(eam, "NEAR_PCT", 0.3)
    monkeypatch.setattr(eam, "MIN_TESTS", 8)
    block, hits = eam.scan_one("Test", "TEST")
    assert "SHORT" in block and "rejected <b>68%</b>" in block
    assert "TEST|S|Daily|50" in hits
    assert "BREAKS up" in block


def test_min_tests_gate_blocks_low_history(monkeypatch):
    # Rate present but too few tests -> we must NOT quote a probability / alert.
    t = _tech(100.1, {"D20": _cell(100.0, 0.1, "support", rate=80, tests=3, held=2)})
    monkeypatch.setattr(eam, "compute_one", lambda tk: t)
    monkeypatch.setattr(eam, "NEAR_PCT", 0.3)
    monkeypatch.setattr(eam, "MIN_TESTS", 8)
    block, hits = eam.scan_one("Test", "TEST")
    assert block == ""


def test_missing_reject_data_no_short(monkeypatch):
    # Below an EMA but no reject history -> no short alert (no fabricated probability).
    t = _tech(99.9, {"D50": _cell(100.0, -0.1, "resistance", rrate=None, rtests=0)})
    monkeypatch.setattr(eam, "compute_one", lambda tk: t)
    monkeypatch.setattr(eam, "NEAR_PCT", 0.3)
    monkeypatch.setattr(eam, "MIN_TESTS", 8)
    block, hits = eam.scan_one("Test", "TEST")
    assert block == ""


def test_compute_one_error_is_safe(monkeypatch):
    monkeypatch.setattr(eam, "compute_one", lambda tk: {"error": "no data"})
    block, hits = eam.scan_one("Test", "TEST")
    assert block == "" and hits == {}


def test_compute_one_exception_is_swallowed(monkeypatch):
    def boom(tk):
        raise RuntimeError("network down")
    monkeypatch.setattr(eam, "compute_one", boom)
    block, hits = eam.scan_one("Test", "TEST")     # must not propagate
    assert block == "" and hits == {}


# ----------------------------- typical bounce -----------------------------
def test_typical_bounce_pct_averages_matching_ema():
    t = {"latest_bounce": {"daily": [{"ema": "50 EMA", "rally_pct": 4.0},
                                      {"ema": "50 EMA", "rally_pct": 6.0}],
                           "weekly": [{"ema": "20 EMA", "rally_pct": 10.0}]}}
    assert eam._typical_bounce_pct(t, "50 EMA") == 5.0
    assert eam._typical_bounce_pct(t, "20 EMA") == 10.0
    assert eam._typical_bounce_pct(t, "200 EMA") is None

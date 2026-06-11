"""
Adversarial unit tests for scripts/paper_trader.py — the CAPITAL- and R:R-aware paper
book. Probes: the min-R:R filter, risk-based position sizing (each trade risks exactly
RISK_PCT of capital -> a stop-out = -1R, a target = +R:R), long & short closes, dedup,
and the capital-aware book() maths. PAPER file + policy are monkeypatched.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "scripts"))
import paper_trader as pt  # noqa: E402


def _isolate(tmp_path, monkeypatch, capital=300000.0, risk_pct=0.5, min_rr=1.5):
    monkeypatch.setattr(pt, "PAPER", tmp_path / "paper.jsonl")
    monkeypatch.setattr(pt, "CAPITAL", capital)
    monkeypatch.setattr(pt, "RISK_PCT", risk_pct)
    monkeypatch.setattr(pt, "MIN_RR", min_rr)


def _setup(direction="long", ticker="^NSEI", entry=100.0, stop=99.0, target=104.0, span=20, rr=4.0):
    return {"scrip": "Nifty 50", "ticker": ticker, "direction": direction, "tf": "Daily",
            "span": span, "ema": entry, "entry": entry, "stop": stop, "target": target,
            "rr": rr, "prob": 70}


def test_rejects_below_min_rr(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch, min_rr=1.5)
    assert pt.open_trade(_setup(rr=0.8), "t0", 6, 2026) is None     # bad R:R -> not taken
    assert pt.open_trade(_setup(rr=None), "t0", 6, 2026) is None
    assert pt.open_trade(_setup(rr=2.0), "t0", 6, 2026) is not None  # good R:R -> taken


def test_position_sized_to_risk_budget(tmp_path, monkeypatch):
    # ₹3,00,000 @ 0.5% = ₹1,500 risk. entry-stop = 1.0 -> qty = 1500.
    _isolate(tmp_path, monkeypatch)
    tr = pt.open_trade(_setup(entry=100.0, stop=99.0, target=104.0), "t0", 6, 2026)
    assert tr["risk_amount"] == 1500.0
    assert tr["qty"] == 1500.0                                     # 1500 / 1.0
    # a wider stop -> smaller size, SAME ₹ risk (distinct EMA so it's not deduped)
    tr2 = pt.open_trade(_setup(entry=100.0, stop=97.0, target=110.0, rr=3.3, span=50), "t0", 6, 2026)
    assert tr2["qty"] == 500.0                                     # 1500 / 3.0


def test_stop_out_loses_one_R(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    pt.open_trade(_setup(entry=100.0, stop=99.0, target=104.0), "t0", 6, 2026)
    closed = pt.update_trades(lambda tk: 98.5, "t1")               # below stop
    r = closed[0]
    assert r["result"] == "lost"
    assert r["pnl_inr"] == -1500.0 and r["pnl_R"] == -1.0          # exactly -1R = risk budget


def test_target_makes_rr_R(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    pt.open_trade(_setup(entry=100.0, stop=99.0, target=104.0, rr=4.0), "t0", 6, 2026)
    closed = pt.update_trades(lambda tk: 104.0, "t1")             # target: reward = 4 * risk
    r = closed[0]
    assert r["result"] == "won"
    assert r["pnl_R"] == 4.0 and r["pnl_inr"] == 6000.0           # +4R = 4 * ₹1,500


def test_short_sizing_and_closes(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    pt.open_trade(_setup(direction="short", entry=100.0, stop=101.0, target=96.0, rr=4.0), "t0", 6, 2026)
    closed = pt.update_trades(lambda tk: 96.0, "t1")             # short target hit
    assert closed[0]["result"] == "won" and closed[0]["pnl_R"] == 4.0

    _isolate(tmp_path, monkeypatch)
    pt.open_trade(_setup(direction="short", entry=100.0, stop=101.0, target=96.0, rr=4.0), "t0", 6, 2026)
    closed = pt.update_trades(lambda tk: 101.5, "t1")           # short stop hit
    assert closed[0]["result"] == "lost" and closed[0]["pnl_R"] == -1.0


def test_open_dedups_same_setup(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    assert pt.open_trade(_setup(), "t0", 6, 2026) is not None
    assert pt.open_trade(_setup(), "t1", 6, 2026) is None        # already open


def test_unsizable_zero_risk_rejected(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    assert pt.open_trade(_setup(entry=100.0, stop=100.0, target=104.0), "t0", 6, 2026) is None


def test_mark_to_market_R(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    pt.open_trade(_setup(entry=100.0, stop=99.0, target=104.0), "t0", 6, 2026)
    pt.update_trades(lambda tk: 102.0, "t1")                     # +2.0 pts * qty 1500 = +3000 = +2R
    r = pt.book()["open"][0]
    assert r["pnl_inr"] == 3000.0 and r["pnl_R"] == 2.0


def test_future_label_and_lot_display(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    tr = pt.open_trade(_setup(ticker="^NSEI"), "t0", 6, 2026)
    assert tr["future"] == "NIFTY JUN26 FUT" and tr["lot"] == 65


def test_book_capital_stats(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    pt.open_trade(_setup(span=20, entry=100.0, stop=99.0, target=104.0, rr=4.0), "t0", 6, 2026)
    pt.open_trade(_setup(span=50, entry=100.0, stop=99.0, target=104.0, rr=4.0), "t0", 6, 2026)
    pt.update_trades(lambda tk: 104.0, "t1")                    # both win +4R (+₹6,000 each)
    st = pt.book()["stats"]
    assert st["closed_n"] == 2 and st["win_rate"] == 100
    assert st["realized_inr"] == 12000.0 and st["realized_R"] == 8.0
    assert st["return_pct"] == round(100 * 12000.0 / 300000.0, 2)   # +4.0%

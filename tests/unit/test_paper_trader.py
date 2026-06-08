"""
Adversarial unit tests for scripts/paper_trader.py — the paper-trade book that
auto-takes every EMA-alert setup as a current-month FUT trade with a STOP-LOSS.
Probes: open dedup, lot-sized P&L, closing on target (won) vs stop-loss (lost) for
BOTH long and short, and the book() summary maths. The store file is monkeypatched
to a tmp path so real trades are never touched.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "scripts"))
import paper_trader as pt  # noqa: E402


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(pt, "PAPER", tmp_path / "paper.jsonl")


def _setup(direction="long", ticker="^NSEI", entry=100.0, stop=99.0, target=104.0, span=20):
    return {"scrip": "Nifty 50", "ticker": ticker, "direction": direction, "tf": "Daily",
            "span": span, "ema": entry, "entry": entry, "stop": stop, "target": target,
            "rr": 4.0, "prob": 70}


def test_open_trade_uses_lot_and_future_label(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    tr = pt.open_trade(_setup(), "2026-06-08T10:00:00+00:00", month=6, year=2026)
    assert tr["lot"] == 65                       # NIFTY lot
    assert tr["future"] == "NIFTY JUN26 FUT"
    assert tr["status"] == "open" and tr["stop"] == 99.0    # stop-loss recorded


def test_open_trade_dedups_same_setup(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    assert pt.open_trade(_setup(), "t0", 6, 2026) is not None
    assert pt.open_trade(_setup(), "t1", 6, 2026) is None    # already open -> no duplicate
    # opposite direction is a distinct trade
    assert pt.open_trade(_setup(direction="short", stop=101.0, target=96.0), "t2", 6, 2026) is not None


def test_long_closes_won_on_target(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    pt.open_trade(_setup(entry=100.0, stop=99.0, target=104.0), "t0", 6, 2026)
    closed = pt.update_trades(lambda tk: 104.5, "t1")        # price >= target
    assert len(closed) == 1
    r = closed[0]
    assert r["result"] == "won" and r["exit"] == 104.0       # exits AT the target, not the overshoot
    assert r["pnl_points"] == 4.0 and r["pnl_inr"] == round(4.0 * 65, 2)


def test_long_closes_lost_on_stop(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    pt.open_trade(_setup(entry=100.0, stop=99.0, target=104.0), "t0", 6, 2026)
    closed = pt.update_trades(lambda tk: 98.0, "t1")         # price <= stop-loss
    assert closed[0]["result"] == "lost"
    assert closed[0]["exit"] == 99.0 and closed[0]["pnl_points"] == -1.0


def test_short_closes_won_on_target_and_lost_on_stop(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    pt.open_trade(_setup(direction="short", entry=100.0, stop=101.0, target=96.0), "t0", 6, 2026)
    closed = pt.update_trades(lambda tk: 95.0, "t1")         # short target hit (price falls)
    assert closed[0]["result"] == "won" and closed[0]["pnl_points"] == 4.0   # short profits when price drops

    _isolate(tmp_path, monkeypatch)
    pt.open_trade(_setup(direction="short", entry=100.0, stop=101.0, target=96.0), "t0", 6, 2026)
    closed = pt.update_trades(lambda tk: 102.0, "t1")        # short stop-loss hit (price rises)
    assert closed[0]["result"] == "lost" and closed[0]["pnl_points"] == -1.0


def test_update_marks_to_market_without_closing(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    pt.open_trade(_setup(entry=100.0, stop=99.0, target=104.0), "t0", 6, 2026)
    closed = pt.update_trades(lambda tk: 101.5, "t1")        # between stop and target
    assert closed == []
    bk = pt.book()
    assert bk["stats"]["open_n"] == 1
    assert bk["open"][0]["pnl_points"] == 1.5                # unrealised mark


def test_update_skips_when_price_unavailable(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    pt.open_trade(_setup(), "t0", 6, 2026)
    assert pt.update_trades(lambda tk: None, "t1") == []     # no price -> no close, no crash
    assert pt.book()["stats"]["open_n"] == 1


def test_book_stats(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    pt.open_trade(_setup(span=20, entry=100.0, stop=99.0, target=104.0), "t0", 6, 2026)
    pt.open_trade(_setup(span=50, entry=100.0, stop=99.0, target=104.0), "t0", 6, 2026)
    pt.update_trades(lambda tk: 104.0, "t1")                 # both hit target -> won
    st = pt.book()["stats"]
    assert st["closed_n"] == 2 and st["wins"] == 2 and st["win_rate"] == 100
    assert st["realized_inr"] == round(2 * 4.0 * 65, 2)


def test_non_indian_ticker_lot_one(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    tr = pt.open_trade(_setup(ticker="AAPL", entry=200.0, stop=198.0, target=210.0), "t0", 6, 2026)
    assert tr["lot"] == 1 and tr["future"] == "AAPL JUN26 FUT"

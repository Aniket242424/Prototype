"""
Paper-trade book for the EMA-alert suggestions.

Every suggested setup (from ema_alert_monitor) is auto-taken as a PAPER trade in the
CURRENT-MONTH FUTURE of that scrip, with the suggested entry / STOP-LOSS / target.
On each cron tick the open trades are marked to market and CLOSED when price hits the
target (won) or the stop-loss (lost). All deterministic, all paper — no real orders.

State: data/paper_trades.jsonl (one JSON object per trade).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

PAPER = Path("data/paper_trades.jsonl")

# Current-month FUT lot sizes (Indian F&O). Unknown instruments -> lot 1 (P&L in points).
LOT_SIZES = {
    # indices
    "^NSEI": 65, "^NSEBANK": 30, "NIFTY_FIN_SERVICE.NS": 60, "^BSESN": 20, "BSE-BANK.BO": 30,
    # stocks
    "INFY.NS": 400, "RELIANCE.NS": 500,
}
_MON = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
# Display base for the futures symbol (indices have their own contract names).
_FUT_BASE = {"^NSEI": "NIFTY", "^NSEBANK": "BANKNIFTY", "^BSESN": "SENSEX",
             "NIFTY_FIN_SERVICE.NS": "FINNIFTY", "BSE-BANK.BO": "BANKEX"}


def future_label(scrip: str, ticker: str, month: int, year: int) -> str:
    base = _FUT_BASE.get(ticker)
    if not base:
        base = ticker.split(".")[0].upper() if "." in ticker else ticker.lstrip("^").upper()
    return f"{base} {_MON[month - 1]}{str(year)[2:]} FUT"


def _read() -> list:
    out = []
    if PAPER.exists():
        for line in PAPER.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
    return out


def _write(rows: list) -> None:
    PAPER.parent.mkdir(parents=True, exist_ok=True)
    tmp = PAPER.with_suffix(".tmp")
    tmp.write_text("\n".join(json.dumps(r) for r in rows[-2000:]) + ("\n" if rows else ""), encoding="utf-8")
    os.replace(tmp, PAPER)


def _pnl(direction: str, entry: float, px: float, lot: int) -> tuple[float, float]:
    d = 1.0 if direction == "long" else -1.0
    pts = (px - entry) * d
    return round(pts, 2), round(pts * lot, 2)


def open_trade(setup: dict, now_iso: str, month: int, year: int):
    """Open a paper FUTURES trade from a setup. Dedup: at most one OPEN trade per
    (ticker, direction, EMA). Returns the new trade, or None if one's already open."""
    rows = _read()
    key = (setup["ticker"], setup["direction"], setup["tf"], setup["span"])
    for r in rows:
        if r["status"] == "open" and (r["ticker"], r["direction"], r["tf"], r["span"]) == key:
            return None
    lot = LOT_SIZES.get(setup["ticker"], 1)
    entry = round(float(setup["entry"]), 2)
    tr = {
        "id": f"{setup['ticker']}|{setup['direction']}|{setup['tf']}{setup['span']}|{now_iso}",
        "ts": now_iso, "scrip": setup["scrip"], "ticker": setup["ticker"],
        "future": future_label(setup["scrip"], setup["ticker"], month, year), "lot": lot,
        "direction": setup["direction"], "tf": setup["tf"], "span": setup["span"],
        "setup": f"{setup['tf']} {setup['span']} EMA", "prob": setup.get("prob"),
        "entry": entry, "stop": round(float(setup["stop"]), 2), "target": round(float(setup["target"]), 2),
        "rr": round(float(setup["rr"]), 2) if setup.get("rr") else None,
        "status": "open", "current": entry, "pnl_points": 0.0, "pnl_inr": 0.0,
        "exit": None, "exit_ts": None, "result": None,
    }
    rows.append(tr)
    _write(rows)
    return tr


def update_trades(price_fn, now_iso: str) -> list:
    """Mark every OPEN trade to market and CLOSE it on target (won) or STOP-LOSS (lost).
    price_fn(ticker) -> latest price or None. Returns the list of trades closed this run."""
    rows = _read()
    closed, cache = [], {}
    for r in rows:
        if r.get("status") != "open":
            continue
        tk = r["ticker"]
        if tk not in cache:
            try:
                cache[tk] = price_fn(tk)
            except Exception:
                cache[tk] = None
        px = cache[tk]
        if px is None:
            continue
        px = float(px)
        r["current"] = round(px, 2)
        r["pnl_points"], r["pnl_inr"] = _pnl(r["direction"], r["entry"], px, r["lot"])
        hit = None
        if r["direction"] == "long":
            if px >= r["target"]:
                hit = (r["target"], "won")
            elif px <= r["stop"]:                       # STOP-LOSS
                hit = (r["stop"], "lost")
        else:
            if px <= r["target"]:
                hit = (r["target"], "won")
            elif px >= r["stop"]:                       # STOP-LOSS
                hit = (r["stop"], "lost")
        if hit:
            exit_px, result = hit
            r["status"], r["result"] = "closed", result
            r["exit"], r["exit_ts"], r["current"] = round(exit_px, 2), now_iso, round(exit_px, 2)
            r["pnl_points"], r["pnl_inr"] = _pnl(r["direction"], r["entry"], exit_px, r["lot"])
            closed.append(r)
    _write(rows)
    return closed


def book() -> dict:
    """Open + recently-closed trades with summary stats for the dashboard."""
    rows = _read()
    op = [r for r in rows if r.get("status") == "open"]
    cl = [r for r in rows if r.get("status") == "closed"]
    wins = [r for r in cl if r.get("result") == "won"]
    realized = round(sum(r.get("pnl_inr", 0) for r in cl), 2)
    unreal = round(sum(r.get("pnl_inr", 0) for r in op), 2)
    return {
        "open": op[::-1],
        "closed": cl[::-1][:40],
        "stats": {
            "open_n": len(op), "closed_n": len(cl), "wins": len(wins), "losses": len(cl) - len(wins),
            "win_rate": (round(100 * len(wins) / len(cl)) if cl else None),
            "realized_inr": realized, "unrealized_inr": unreal,
            "total_inr": round(realized + unreal, 2),
        },
    }

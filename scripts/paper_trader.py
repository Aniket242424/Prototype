"""
Paper-trade book for the EMA-alert suggestions — CAPITAL- and RISK:REWARD-aware.

Every suggested setup is auto-taken as a PAPER trade in the CURRENT-MONTH FUTURE of
that scrip, but ONLY if:
  1. its Risk:Reward >= MIN_RR (we don't take junk-R:R trades), and
  2. it can be sized within the per-trade risk budget.
Position size is computed from CAPITAL: each trade risks exactly RISK_PCT of capital
(default ₹3,00,000 @ 0.5% = ₹1,500). quantity = risk_amount / |entry - stop|, so a
stop-out loses ~1R (the risk budget) and a target win makes ~R:R × 1R — P&L is always
in ₹ and in R-multiples, properly scaled to capital regardless of instrument lot size.

On each tick open trades are marked to market and CLOSED on target (won) or STOP-LOSS
(lost). All deterministic, all paper — no real orders.

State: data/paper_trades.jsonl.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

PAPER = Path("data/paper_trades.jsonl")

# Capital / risk policy (operator-set; see project_capital_and_risk_sizing memory).
CAPITAL = float(os.getenv("PAPER_CAPITAL_INR", "300000"))     # ₹3,00,000
RISK_PCT = float(os.getenv("PAPER_RISK_PCT", "0.5"))          # 0.5% per trade -> ₹1,500
MIN_RR = float(os.getenv("PAPER_MIN_RR", "1.5"))             # reject setups below 1:1.5

# Current-month FUT lot sizes (Indian F&O) — only for the "≈ N lots" display.
LOT_SIZES = {
    "^NSEI": 65, "^NSEBANK": 30, "NIFTY_FIN_SERVICE.NS": 60, "^BSESN": 20, "BSE-BANK.BO": 30,
    "INFY.NS": 400, "RELIANCE.NS": 500,
}
_MON = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
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


def open_trade(setup: dict, now_iso: str, month: int, year: int):
    """Open a capital-sized paper FUTURES trade from a setup — but ONLY if it clears
    MIN_RR and can be risk-sized. Dedup: one OPEN trade per (ticker, direction, EMA).
    Returns the trade, or None if filtered (bad R:R) / already open / unsizable."""
    rr = setup.get("rr")
    if rr is None or rr < MIN_RR:                      # enforce Risk:Reward
        return None
    entry = round(float(setup["entry"]), 2)
    stop = round(float(setup["stop"]), 2)
    risk_per_unit = abs(entry - stop)
    if risk_per_unit <= 0:
        return None
    rows = _read()
    key = (setup["ticker"], setup["direction"], setup["tf"], setup["span"])
    for r in rows:
        if r["status"] == "open" and (r["ticker"], r["direction"], r["tf"], r["span"]) == key:
            return None
    risk_amount = round(CAPITAL * RISK_PCT / 100.0, 2)   # ₹ risked on this trade (1R)
    qty = risk_amount / risk_per_unit                    # units sized so a stop-out = 1R
    lot = LOT_SIZES.get(setup["ticker"], 1)
    tr = {
        "id": f"{setup['ticker']}|{setup['direction']}|{setup['tf']}{setup['span']}|{now_iso}",
        "ts": now_iso, "scrip": setup["scrip"], "ticker": setup["ticker"],
        "future": future_label(setup["scrip"], setup["ticker"], month, year), "lot": lot,
        "direction": setup["direction"], "tf": setup["tf"], "span": setup["span"],
        "setup": f"{setup['tf']} {setup['span']} EMA", "prob": setup.get("prob"),
        "entry": entry, "stop": stop, "target": round(float(setup["target"]), 2), "rr": round(float(rr), 2),
        "capital": CAPITAL, "risk_amount": risk_amount, "risk_per_unit": round(risk_per_unit, 2),
        "qty": round(qty, 4), "lots": round(qty / lot, 3),
        "status": "open", "current": entry, "pnl_inr": 0.0, "pnl_R": 0.0,
        "exit": None, "exit_ts": None, "result": None,
    }
    rows.append(tr)
    _write(rows)
    return tr


def _mark(r: dict, px: float) -> None:
    d = 1.0 if r["direction"] == "long" else -1.0
    r["current"] = round(px, 2)
    r["pnl_inr"] = round((px - r["entry"]) * d * r["qty"], 2)
    r["pnl_R"] = round(r["pnl_inr"] / r["risk_amount"], 2) if r.get("risk_amount") else 0.0


def update_trades(price_fn, now_iso: str) -> list:
    """Mark every OPEN trade to market and CLOSE it on target (won) or STOP-LOSS (lost).
    price_fn(ticker) -> latest price or None. Returns trades closed this run."""
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
        _mark(r, px)
        hit = None
        if r["direction"] == "long":
            if px >= r["target"]:
                hit = (r["target"], "won")
            elif px <= r["stop"]:
                hit = (r["stop"], "lost")
        else:
            if px <= r["target"]:
                hit = (r["target"], "won")
            elif px >= r["stop"]:
                hit = (r["stop"], "lost")
        if hit:
            exit_px, result = hit
            _mark(r, exit_px)
            r["status"], r["result"] = "closed", result
            r["exit"], r["exit_ts"] = round(exit_px, 2), now_iso
            closed.append(r)
    _write(rows)
    return closed


def book() -> dict:
    """Open + recently-closed trades with capital-aware summary stats."""
    rows = _read()
    op = [r for r in rows if r.get("status") == "open"]
    cl = [r for r in rows if r.get("status") == "closed"]
    wins = [r for r in cl if r.get("result") == "won"]
    realized = round(sum(r.get("pnl_inr", 0) for r in cl), 2)
    unreal = round(sum(r.get("pnl_inr", 0) for r in op), 2)
    total = round(realized + unreal, 2)
    return {
        "open": op[::-1],
        "closed": cl[::-1][:40],
        "stats": {
            "capital": CAPITAL, "risk_pct": RISK_PCT, "min_rr": MIN_RR,
            "risk_per_trade": round(CAPITAL * RISK_PCT / 100.0, 2),
            "open_n": len(op), "closed_n": len(cl), "wins": len(wins), "losses": len(cl) - len(wins),
            "win_rate": (round(100 * len(wins) / len(cl)) if cl else None),
            "realized_inr": realized, "unrealized_inr": unreal, "total_inr": total,
            "return_pct": round(100 * total / CAPITAL, 2) if CAPITAL else 0.0,
            "realized_R": round(sum(r.get("pnl_R", 0) for r in cl), 2),
        },
    }

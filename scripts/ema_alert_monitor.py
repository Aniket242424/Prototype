"""
EMA proximity alert monitor.

For each watched scrip, when the live price comes NEAR a key EMA (a level that has
historically acted as support), send a Telegram alert with the PROBABILITY that the
EMA holds — i.e. the historical hold-rate ("held H out of N tests") plus the last
real bounce off it. Deduped so we alert at most once per scrip-EMA approach per day.

Watches: the agent's tracked ASSETS + a user watchlist (data/watchlist.json, managed
from the dashboard). Runs on cron every ~30 min.

  python scripts/ema_alert_monitor.py              # check + send alerts
  python scripts/ema_alert_monitor.py --dry-run    # print what it WOULD alert, send nothing
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
os.chdir(REPO_ROOT)
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from dotenv import load_dotenv  # noqa: E402
load_dotenv(REPO_ROOT / ".env")
from zoneinfo import ZoneInfo  # noqa: E402

from run_sentiment_agent import compute_one, send_telegram, ASSETS, _HIVOL  # noqa: E402
import paper_trader as pt  # noqa: E402

WATCHLIST = Path("data/watchlist.json")
STATE = Path("data/ema_alert_state.json")

_IST = ZoneInfo("Asia/Kolkata")
_ET = ZoneInfo("America/New_York")
_INDIAN_INDEX = {"^NSEI", "^NSEBANK", "^BSESN", "NIFTY_FIN_SERVICE.NS", "BSE-BANK.BO"}


def market_open(ticker: str, now=None) -> bool:
    """Is the instrument's market OPEN right now? Stops the monitor from opening paper
    trades on a stale closing price after hours (the "trades after market close" bug).
    Crypto = 24/7; commodity futures (=F) = weekdays; Indian equity = 09:15-15:30 IST;
    everything else treated as US equity = 09:30-16:00 ET. DST handled via zoneinfo."""
    now = now or datetime.now(timezone.utc)
    tk = ticker.upper()
    if tk.endswith("-USD"):                       # crypto
        return True
    if tk.endswith("=F"):                          # commodity futures (CME ~ weekdays)
        return now.astimezone(_ET).weekday() < 5
    if tk.endswith(".NS") or tk.endswith(".BO") or ticker in _INDIAN_INDEX:
        t = now.astimezone(_IST)
        if t.weekday() >= 5:
            return False
        m = t.hour * 60 + t.minute
        return 9 * 60 + 15 <= m <= 15 * 60 + 30   # 09:15-15:30 IST
    t = now.astimezone(_ET)                        # default: US equity / index
    if t.weekday() >= 5:
        return False
    m = t.hour * 60 + t.minute
    return 9 * 60 + 30 <= m <= 16 * 60             # 09:30-16:00 ET

# How close (price vs EMA, %) counts as "near" — tight 0.3% so price is right AT the EMA.
NEAR_PCT = float(os.getenv("EMA_ALERT_NEAR_PCT", "0.3"))
NEAR_PCT_HIVOL = float(os.getenv("EMA_ALERT_NEAR_PCT_HIVOL", "0.3"))
# Need enough historical tests before we quote a probability (else it's noise).
MIN_TESTS = int(os.getenv("EMA_ALERT_MIN_TESTS", "8"))
# EMAs we watch as support (timeframe, span).
EMAS = [("Daily", 20), ("Daily", 50), ("Daily", 200), ("Weekly", 50), ("Weekly", 200)]


def _load(p: Path, default):
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            return default
    return default


def _save(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj))
    os.replace(tmp, p)


def watched() -> list:
    """(display_name, ticker) for tracked assets + the user watchlist (deduped)."""
    out = [(nm, tk) for nm, tk in ASSETS.items()]
    have = {tk for _, tk in out}
    for tk in _load(WATCHLIST, {"tickers": []}).get("tickers", []):
        if tk and tk not in have:
            out.append((tk, tk)); have.add(tk)
    return out


def _typical_bounce_pct(t: dict, ema_label: str):
    """Average historical rally % off THIS EMA, from the recorded bounces."""
    b = t.get("latest_bounce") or {}
    r = [x["rally_pct"] for tf in ("daily", "weekly") for x in (b.get(tf) or [])
         if x.get("ema") == ema_label and x.get("rally_pct") is not None]
    return (sum(r) / len(r)) if r else None


def scan_one(name: str, ticker: str) -> tuple[str, dict]:
    """Return (alert_block, hits) for one scrip — SUPPORT approaches with enough
    history to quote a probability, each as a full trade idea (entry/stop/target/RR
    + where it goes if the level breaks)."""
    try:
        t = compute_one(ticker)
    except Exception:
        return "", {}, []
    if not isinstance(t, dict) or t.get("error"):
        return "", {}, []
    price = t["price"]
    hivol = ticker in _HIVOL
    near = NEAR_PCT_HIVOL if hivol else NEAR_PCT
    stop_pct = 2.5 if hivol else 1.0          # stop a buffer below the EMA (a daily close below = broken)
    matrix = t.get("matrix") or {}
    disp = t.get("name") or name
    ns = t.get("nearest_support") or {}
    sf = t.get("structural_floor") or {}
    cr = t.get("controlling_resistance") or {}

    def _fmt(x):
        return f"{x:,.2f}"

    setups = []
    for tf, span in EMAS:
        c = matrix.get(f"{tf[0]}{span}")
        if not c or c.get("v") is None:
            continue
        pct = c.get("pct")                    # (price/ema-1)*100
        if pct is None or abs(pct) > near:    # within `near`% of the EMA (either side)
            continue
        ema = c["v"]
        if pct >= 0:
            # ---- LONG: price at/just above the EMA = pulling back to SUPPORT ----
            rate, tests, held = c.get("rate"), c.get("tests") or 0, c.get("held")
            if rate is None or tests < MIN_TESTS:
                continue
            verdict = "🟢 high-prob bounce" if rate >= 65 else ("🟡 decent odds" if rate >= 50 else "🔴 often breaks")
            stop = ema * (1 - stop_pct / 100)
            typ = _typical_bounce_pct(t, f"{span} EMA")
            if typ and typ > 0:
                target, tnote = price * (1 + typ / 100), f"+{typ:.1f}% typical bounce"
            elif cr.get("value") and cr["value"] > price:
                target, tnote = cr["value"], f"resistance ({cr.get('members', '')})"
            else:
                target, tnote = price * (1 + 2 * stop_pct / 100), "≈2x risk"
            risk, reward = price - stop, target - price
            rr = (reward / risk) if risk > 0 and reward > 0 else None
            nxt = (f"{_fmt(sf['value'])} ({sf.get('members', '')}, {sf.get('grade', '')})"
                   if sf.get("value") and sf["value"] < stop else "prior swing low")
            setups.append({"scrip": disp, "ticker": ticker, "direction": "long", "tf": tf, "span": span,
                           "ema": ema, "entry": price, "stop": stop, "target": target, "rr": rr,
                           "prob": rate, "n": f"{held}/{tests}", "pct": pct, "stop_pct": stop_pct,
                           "tnote": tnote, "verdict": verdict, "if_breaks": nxt})
        else:
            # ---- SHORT: price at/just below the EMA = rallying into RESISTANCE ----
            rrate, rtests, rejd = c.get("reject_rate"), c.get("reject_tests") or 0, c.get("rejected")
            if rrate is None or rtests < MIN_TESTS:
                continue
            verdict = "🟢 high-prob short" if rrate >= 65 else ("🟡 decent odds" if rrate >= 50 else "🔴 often breaks up")
            stop = ema * (1 + stop_pct / 100)
            tgt = (ns.get("value") if ns.get("value") and ns["value"] < price else
                   (sf.get("value") if sf.get("value") and sf["value"] < price else None))
            if tgt:
                drop = (price / tgt - 1) * 100
                target, tnote = tgt, f"{ns.get('members', 'support')}, -{drop:.1f}%"
            else:
                target, tnote = price * (1 - 2 * stop_pct / 100), "≈2x risk"
            risk, reward = stop - price, price - target
            rr = (reward / risk) if risk > 0 and reward > 0 else None
            up = (f"{_fmt(cr['value'])} ({cr.get('members', '')})"
                  if cr.get("value") and cr["value"] > stop else "trend turns up")
            setups.append({"scrip": disp, "ticker": ticker, "direction": "short", "tf": tf, "span": span,
                           "ema": ema, "entry": price, "stop": stop, "target": target, "rr": rr,
                           "prob": rrate, "n": f"{rejd}/{rtests}", "pct": pct, "stop_pct": stop_pct,
                           "tnote": tnote, "verdict": verdict, "if_breaks": up})

    if not setups:
        return "", {}, []
    hits = {f"{s['ticker']}|{'L' if s['direction'] == 'long' else 'S'}|{s['tf']}|{s['span']}": True
            for s in setups}
    head = f"🎯 <b>{disp}</b> {_fmt(price)} — at an EMA (±{near:.1f}%):"
    b = (t.get("latest_bounce") or {}).get("daily") or []
    foot = ""
    if b:
        x = b[0]
        tag = " ⚠ since broken" if x.get("currently") == "broken" else ""
        foot = (f"\n  ↩ last bounce: {x['ema']} {x['date']} "
                f"{x['from_px']:,.2f}→{x['to_px']:,.2f} +{x['rally_pct']}%{tag}")
    return head + "\n" + "\n".join(_format_setup(s) for s in setups) + foot, hits, setups


def _format_setup(s: dict) -> str:
    """One EMA setup -> the alert text block (long or short)."""
    def f(x):
        return f"{x:,.2f}"
    rr = f" · R:R ~1:{s['rr']:.1f}" if s.get("rr") else ""
    if s["direction"] == "long":
        return (f"• <b>LONG · {s['tf']} {s['span']} EMA</b> {f(s['ema'])} ({s['pct']:+.2f}% away) — "
                f"held <b>{s['prob']}%</b> ({s['n']}) {s['verdict']}\n"
                f"   ▸ BUY ~{f(s['entry'])} · SL {f(s['stop'])} (-{s['stop_pct']:.1f}%, daily close) · "
                f"target {f(s['target'])} ({s['tnote']}){rr}\n"
                f"   ▸ if BREAKS down → next floor {s['if_breaks']}")
    return (f"• <b>SHORT · {s['tf']} {s['span']} EMA</b> {f(s['ema'])} ({s['pct']:+.2f}% away) — "
            f"rejected <b>{s['prob']}%</b> ({s['n']}) {s['verdict']}\n"
            f"   ▸ SELL ~{f(s['entry'])} · SL {f(s['stop'])} (+{s['stop_pct']:.1f}%, daily close) · "
            f"target {f(s['target'])} ({s['tnote']}){rr}\n"
            f"   ▸ if BREAKS up → next resistance {s['if_breaks']}")


def _plain(msg: str) -> str:
    for tag in ("<b>", "</b>", "<i>", "</i>"):
        msg = msg.replace(tag, "")
    return msg


def _fmt_opened(trades: list) -> str:
    lines = ["📝 <b>PAPER TRADES OPENED</b> <i>(current-month FUT, with stop-loss)</i>"]
    for r in trades:
        arrow = "🟢 BUY" if r["direction"] == "long" else "🔴 SELL"
        rr = f" · R:R 1:{r['rr']:.1f}" if r.get("rr") else ""
        lines.append(f"{arrow} <b>{r['future']}</b> @ {r['entry']:,.2f}\n"
                     f"   SL {r['stop']:,.2f} · target {r['target']:,.2f}{rr} · {r['setup']} ({r['prob']}%)")
    return "\n".join(lines)


def _fmt_closed(trades: list) -> str:
    lines = ["🏁 <b>PAPER TRADES CLOSED</b>"]
    for r in trades:
        emo = "✅ TARGET HIT" if r["result"] == "won" else "🛑 STOP-LOSS HIT"
        pnl = f"{r['pnl_points']:+,.2f} pts" + (f" = ₹{r['pnl_inr']:+,.0f}" if r["lot"] > 1 else "")
        lines.append(f"{emo} — <b>{r['future']}</b> {r['direction'].upper()} "
                     f"{r['entry']:,.2f} → {r['exit']:,.2f}  ({pnl})")
    return "\n".join(lines)


def main() -> None:
    dry = "--dry-run" in sys.argv
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat(timespec="seconds")
    today = now.date().isoformat()
    sent_state = _load(STATE, {})
    sent_state = {k: d for k, d in sent_state.items() if d == today}   # keep only today's dedup keys

    # 1) Mark OPEN paper trades to market; close on target (won) or STOP-LOSS (lost).
    closed = []
    if not dry:
        try:
            closed = pt.update_trades(lambda tk: (compute_one(tk) or {}).get("price"), now_iso)
        except Exception as e:
            print("paper update failed:", e)

    blocks, opened, skipped_closed = [], [], 0
    for name, ticker in watched():
        if not market_open(ticker, now):     # market closed -> never open a trade on a stale price
            skipped_closed += 1
            continue
        block, hits, setups = scan_one(name, ticker)
        # take EVERY suggested setup as a PAPER trade (dedup = one open per ticker/dir/EMA)
        if not dry:
            for s in setups:
                tr = pt.open_trade(s, now_iso, now.month, now.year)
                if tr:
                    opened.append(tr)
        if not block:
            continue
        new = {k for k in hits if sent_state.get(k) != today}   # alert dedup: once/day per scrip-EMA
        if not new:
            continue
        for k in hits:
            sent_state[k] = today
        blocks.append(block)

    if blocks:
        msg = ("📊 <b>EMA PROXIMITY ALERTS</b>\n\n" + "\n\n".join(blocks)
               + "\n\n<i>Auto-taken as PAPER trades (current-month FUT, with stop-loss). Not advice.</i>")
        if dry:
            print("=== DRY RUN — would send ===\n" + _plain(msg))
        else:
            send_telegram(msg)
            print(f"sent EMA-proximity alert ({len(blocks)} scrip(s))")
    else:
        print(f"no new EMA-proximity alerts (skipped {skipped_closed} closed-market scrip(s))")

    if not dry:
        if opened:
            send_telegram(_fmt_opened(opened)); print(f"opened {len(opened)} paper trade(s)")
        if closed:
            send_telegram(_fmt_closed(closed)); print(f"closed {len(closed)} paper trade(s)")
        _save(STATE, sent_state)


if __name__ == "__main__":
    main()

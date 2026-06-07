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

from run_sentiment_agent import compute_one, send_telegram, ASSETS, _HIVOL  # noqa: E402

WATCHLIST = Path("data/watchlist.json")
STATE = Path("data/ema_alert_state.json")

# How close (price vs EMA, %) counts as "near". Wider for high-beta names.
NEAR_PCT = float(os.getenv("EMA_ALERT_NEAR_PCT", "1.0"))
NEAR_PCT_HIVOL = float(os.getenv("EMA_ALERT_NEAR_PCT_HIVOL", "2.0"))
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
        return "", {}
    if not isinstance(t, dict) or t.get("error"):
        return "", {}
    price = t["price"]
    hivol = ticker in _HIVOL
    near = NEAR_PCT_HIVOL if hivol else NEAR_PCT
    stop_pct = 2.5 if hivol else 1.0          # stop a buffer below the EMA (a daily close below = broken)
    matrix = t.get("matrix") or {}
    disp = t.get("name") or name
    sf = t.get("structural_floor") or {}
    cr = t.get("controlling_resistance") or {}

    def _fmt(x):
        return f"{x:,.2f}"

    hits, blocks = {}, []
    for tf, span in EMAS:
        c = matrix.get(f"{tf[0]}{span}")
        if not c or c.get("v") is None:
            continue
        pct = c.get("pct")                    # (price/ema-1)*100
        if pct is None or not (-0.3 <= pct <= near):   # at/just above the EMA = pulling back to support
            continue
        rate, tests, held = c.get("rate"), c.get("tests") or 0, c.get("held")
        if rate is None or tests < MIN_TESTS:
            continue
        ema = c["v"]
        hits[f"{ticker}|{tf}|{span}"] = True
        verdict = "🟢 high-prob bounce" if rate >= 65 else ("🟡 decent odds" if rate >= 50 else "🔴 often breaks")
        # trade idea: enter near the EMA, stop a buffer below it, target = typical bounce off it
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
        # where it goes IF it breaks (next floor below the stop)
        nxt = (f"{_fmt(sf['value'])} ({sf.get('members', '')}, {sf.get('grade', '')})"
               if sf.get("value") and sf["value"] < stop else "prior swing low")

        blocks.append(
            f"• <b>{tf} {span} EMA</b> {_fmt(ema)} ({pct:+.1f}% away) — held <b>{rate}%</b> ({held}/{tests}) {verdict}\n"
            f"   ▸ TRADE: buy ~{_fmt(price)} · SL {_fmt(stop)} (-{stop_pct:.1f}%, on daily close) · "
            f"target {_fmt(target)} ({tnote})" + (f" · R:R ~1:{rr:.1f}" if rr else "") + "\n"
            f"   ▸ if it BREAKS (closes below SL) → next floor {nxt}")

    if not blocks:
        return "", {}
    head = f"🎯 <b>{disp}</b> {_fmt(price)} — at a support EMA:"
    b = (t.get("latest_bounce") or {}).get("daily") or []
    foot = ""
    if b:
        x = b[0]
        tag = " ⚠ since broken" if x.get("currently") == "broken" else ""
        foot = (f"\n  ↩ last bounce: {x['ema']} {x['date']} "
                f"{x['from_px']:,.2f}→{x['to_px']:,.2f} +{x['rally_pct']}%{tag}")
    return head + "\n" + "\n".join(blocks) + foot, hits


def main() -> None:
    dry = "--dry-run" in sys.argv
    today = datetime.now(timezone.utc).date().isoformat()
    sent_state = _load(STATE, {})
    sent_state = {k: d for k, d in sent_state.items() if d == today}   # keep only today's dedup keys

    blocks = []
    for name, ticker in watched():
        block, hits = scan_one(name, ticker)
        if not block:
            continue
        # dedup: skip EMAs already alerted today for this scrip
        new = {k for k in hits if sent_state.get(k) != today}
        if not new:
            continue
        for k in hits:
            sent_state[k] = today
        blocks.append(block)

    if not blocks:
        print("no new EMA-proximity alerts")
    else:
        msg = ("📊 <b>EMA PROXIMITY ALERTS</b>\n\n" + "\n\n".join(blocks)
               + "\n\n<i>Probability = historical hold-rate of that EMA. Not financial advice.</i>")
        if dry:
            print("=== DRY RUN — would send ===\n" + msg.replace("<b>", "").replace("</b>", "")
                  .replace("<i>", "").replace("</i>", ""))
        else:
            send_telegram(msg)
            print(f"sent EMA-proximity alert ({len(blocks)} scrip(s))")
    if not dry:
        _save(STATE, sent_state)


if __name__ == "__main__":
    main()

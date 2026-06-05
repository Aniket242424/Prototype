"""
Iron Condor dashboard — standalone, Kite-themed, runs on the EC2 HOST.

Why standalone (not part of the main app dashboard): the main dashboard is
baked into the app container (changing it needs a risky image rebuild on a
memory-tight box), and the Iron Condor data lives on the host. So this is a
tiny stdlib http.server that reads the host data files directly and queries
Delta for live option marks to compute mark-to-market P&L.

Serves on 0.0.0.0:8001. Read-only. Auto-refreshes every 30s.

Run (host):
    ~/ic-venv/bin/python scripts/ic_dashboard.py
Usually run as a systemd service (see deploy notes).
"""
from __future__ import annotations

import asyncio
import csv
import json
import os
import sys
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
os.chdir(REPO_ROOT)
sys.path.insert(0, str(REPO_ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402
load_dotenv(REPO_ROOT / ".env")

from trading_agent.brokers.delta.client import DeltaClient  # noqa: E402
from trading_agent.brokers.delta.fees import entry_fees_usd  # noqa: E402

PORT = int(os.getenv("DELTA_IC_DASH_PORT", "8001"))
PERP = (os.getenv("DELTA_IC_UNDERLYING", "BTC").upper()) + "USD"
CAPITAL_INR = float(os.getenv("DELTA_IC_CAPITAL_INR", "200000"))
FX = float(os.getenv("DELTA_IC_FX_INR_USD", "84"))
CAPITAL_USD = CAPITAL_INR / FX
STATE_FILE = Path("data/iron_condor_state.json")
TRADES_CSV = Path("data/iron_condor_trades.csv")


# ============================================================
# Data gathering
# ============================================================

def fnum(x, d=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


def load_state() -> dict | None:
    if not STATE_FILE.exists():
        return None
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return None


def load_trades() -> list[dict]:
    if not TRADES_CSV.exists():
        return []
    try:
        return list(csv.DictReader(open(TRADES_CSV, newline="", encoding="utf-8")))
    except Exception:
        return []


async def gather_live(state: dict | None) -> dict:
    """Fetch live spot + per-leg marks; compute mark-to-market for the open condor."""
    out = {"spot": None, "legs_mtm": [], "mtm_gross_usd": 0.0, "api_ok": False}
    try:
        async with DeltaClient.from_env() as c:
            out["spot"] = await c.get_spot_price(PERP)
            out["api_ok"] = True
            if state:
                lots = int(state["lots"])
                for leg in state["legs"]:
                    cv = fnum(leg.get("contract_value"), 0.001)
                    entry = fnum(leg.get("price_per_btc"))
                    mark = entry
                    try:
                        tk = await c.get_option_ticker(leg["symbol"]) or {}
                        q = tk.get("quotes") or {}
                        mark = fnum(tk.get("mark_price") or q.get("mark_price") or entry, entry)
                    except Exception:
                        pass
                    # sold leg gains when mark falls; bought leg gains when mark rises
                    if leg["side"] == "sell":
                        leg_pnl = (entry - mark) * cv * lots
                    else:
                        leg_pnl = (mark - entry) * cv * lots
                    out["legs_mtm"].append({**leg, "mark": mark, "leg_pnl": leg_pnl})
                    out["mtm_gross_usd"] += leg_pnl
    except Exception as e:
        out["error"] = str(e)
    return out


# Server-side cache so rapid polling (every ~2s, possibly many viewers) doesn't
# hammer the Delta API. One live fetch is shared for up to LIVE_TTL seconds.
_live_cache = {"t": 0.0, "data": None}
LIVE_TTL = 2.0


def get_live_cached(state: dict | None) -> dict:
    now = time.monotonic()
    if _live_cache["data"] is not None and (now - _live_cache["t"]) < LIVE_TTL:
        return _live_cache["data"]
    data = asyncio.run(gather_live(state))
    _live_cache["t"] = now
    _live_cache["data"] = data
    return data


def _zone_pct(x, lp, lc):
    span = max(lc - lp, 1.0)
    return max(0.0, min(100.0, (x - lp) / span * 100.0))


def live_payload(state: dict | None, live: dict) -> dict:
    """Compact JSON for the 2s poller — spot, MTM, per-leg marks, zone marker."""
    spot = live.get("spot")
    p = {
        "ok": bool(live.get("api_ok")),
        "spot": spot,
        "ts": datetime.now(timezone.utc).strftime("%H:%M:%S"),
        "open_key": None, "mtm_usd": None, "mtm_inr": None,
        "in_zone": None, "spot_pct": None, "legs": [],
    }
    if state and spot:
        legs = {l["role"]: l for l in state["legs"]}
        sp = fnum(legs["short_put"]["strike"]); sc = fnum(legs["short_call"]["strike"])
        lp = fnum(legs["long_put"]["strike"]); lc = fnum(legs["long_call"]["strike"])
        mtm = fnum(live.get("mtm_gross_usd"))
        p["open_key"] = f"{state.get('expiry')}|{state.get('entry_ts')}"
        p["mtm_usd"] = round(mtm, 3)
        p["mtm_inr"] = round(mtm * FX, 0)
        p["in_zone"] = bool(sp < spot < sc)
        p["spot_pct"] = round(_zone_pct(spot, lp, lc), 2)
        p["legs"] = [
            {"role": x["role"], "mark": round(fnum(x.get("mark")), 1),
             "leg_pnl": round(fnum(x.get("leg_pnl")), 3)}
            for x in live.get("legs_mtm", [])
        ]
    return p


def compute_stats(trades: list[dict]) -> dict:
    settled = [t for t in trades if t.get("outcome")]
    n = len(settled)
    nets = [fnum(t.get("net_pnl_usd") or t.get("pnl_usd")) for t in settled]
    gross = [fnum(t.get("gross_pnl_usd") or t.get("pnl_usd")) for t in settled]
    fees = [fnum(t.get("entry_fees_usd")) + fnum(t.get("settle_fees_usd")) for t in settled]
    wins = sum(1 for t in settled if t.get("outcome") == "MAX_WIN")
    total_net = sum(nets)
    return {
        "n": n,
        "wins": wins,
        "win_rate": (wins / n * 100) if n else 0.0,
        "total_net_usd": total_net,
        "total_gross_usd": sum(gross),
        "total_fees_usd": sum(fees),
        "equity_usd": CAPITAL_USD + total_net,
    }


# ============================================================
# Rendering
# ============================================================

def _money_class(v: float) -> str:
    return "pos" if v > 0 else ("neg" if v < 0 else "zero")


def _inr(usd: float) -> str:
    return f"₹{usd * FX:,.0f}"


def render(state, live, trades, stats) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    spot = live.get("spot")
    spot_str = f"${spot:,.0f}" if spot else "—"
    api_badge = ("<span class='dot ok'></span>live" if live.get("api_ok")
                 else "<span class='dot bad'></span>API down")

    # ---- top metric cards ----
    eq = stats["equity_usd"]
    net = stats["total_net_usd"]
    cards = f"""
    <div class="cards">
      <div class="card"><div class="k">Paper Capital</div>
        <div class="v">₹{CAPITAL_INR:,.0f}</div><div class="sub">${CAPITAL_USD:,.0f}</div></div>
      <div class="card"><div class="k">Equity</div>
        <div class="v">{_inr(eq)}</div><div class="sub">${eq:,.2f}</div></div>
      <div class="card"><div class="k">Total Net P&amp;L</div>
        <div class="v {_money_class(net)}">{('+' if net>=0 else '')}{_inr(net)}</div>
        <div class="sub {_money_class(net)}">{('+' if net>=0 else '')}${net:,.2f}</div></div>
      <div class="card"><div class="k">Win Rate</div>
        <div class="v">{stats['win_rate']:.0f}%</div><div class="sub">{stats['wins']}/{stats['n']} settled</div></div>
      <div class="card"><div class="k">Fees Paid</div>
        <div class="v">${stats['total_fees_usd']:,.2f}</div><div class="sub">{_inr(stats['total_fees_usd'])}</div></div>
    </div>"""

    # ---- open position ----
    if state:
        open_html = _render_open(state, live)
    else:
        open_html = ("<div class='panel'><div class='panel-title'>Open Position</div>"
                     "<div class='empty'>No open position. Next entry fires at 12:06 UTC (5:36 PM IST).</div></div>")

    # ---- history ----
    hist_html = _render_history(trades)

    open_key = f"{state.get('expiry')}|{state.get('entry_ts')}" if state else "none"
    return f"""<!DOCTYPE html><html lang="en"><head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Iron Condor — Delta India</title>
<style>{CSS}</style></head><body>
<header>
  <div class="brand">Iron Condor <span class="muted">· Delta India</span></div>
  <div class="hmeta">
    <span class="badge paper">PAPER</span>
    <span class="sep">BTC <span id="hdr-spot">{spot_str}</span></span>
    <span class="sep" id="api-badge">{api_badge}</span>
    <span class="muted">live <span id="live-ts">{now[11:]}</span></span>
  </div>
</header>
<main>
  {cards}
  {open_html}
  {hist_html}
  <div class="foot">Fees: Delta 0.01% notional (cap 3.5% premium) + 18% GST; OTM legs free. ₹ at {FX:.0f}/$. Live prices every 2s.</div>
</main>
<script>
const INITIAL_KEY = {json.dumps(open_key)};
const FX = {FX};
function flash(el, up){{ if(!el) return; el.classList.remove('up','down'); void el.offsetWidth;
  el.classList.add(up ? 'up' : 'down'); }}
function setNum(id, txt, val, prev){{ const el=document.getElementById(id); if(!el) return;
  if(el.textContent!==txt){{ flash(el, val>=prev); el.textContent=txt; }} }}
let last={{}};
async function poll(){{
  try{{
    const r = await fetch('/api/live',{{cache:'no-store'}}); const d = await r.json();
    if(d.open_key !== INITIAL_KEY){{ location.reload(); return; }}
    const ts=document.getElementById('live-ts'); if(ts) ts.textContent=d.ts;
    if(d.spot!=null){{
      const s='$'+Math.round(d.spot).toLocaleString();
      setNum('hdr-spot', s, d.spot, last.spot||d.spot);
      const ss=document.getElementById('status-spot'); if(ss) ss.textContent='spot $'+Math.round(d.spot).toLocaleString();
      last.spot=d.spot;
    }}
    if(d.mtm_usd!=null){{
      const mv=document.getElementById('mtm-usd'); const mi=document.getElementById('mtm-inr');
      const sign=d.mtm_usd>=0?'+':''; const cls=d.mtm_usd>0?'pos':(d.mtm_usd<0?'neg':'zero');
      if(mv){{ flash(mv, d.mtm_usd>=(last.mtm||0)); mv.textContent=sign+'$'+d.mtm_usd.toFixed(3); mv.className='opv '+cls; }}
      if(mi){{ mi.textContent=sign+'₹'+Math.round(d.mtm_inr).toLocaleString(); mi.className='opsub '+cls; }}
      last.mtm=d.mtm_usd;
    }}
    const st=document.getElementById('status-label');
    if(st && d.in_zone!=null){{ st.innerHTML = d.in_zone? "<span class='pos'>IN ZONE ✓</span>":"<span class='neg'>OUT OF ZONE</span>"; }}
    const mk=document.getElementById('spot-marker'); if(mk && d.spot_pct!=null) mk.style.left=d.spot_pct+'%';
    (d.legs||[]).forEach(l=>{{
      const m=document.getElementById('mark-'+l.role); if(m) m.textContent=l.mark.toFixed(1);
      const sign=l.leg_pnl>=0?'+':''; const cls=l.leg_pnl>0?'pos':(l.leg_pnl<0?'neg':'zero');
      const p=document.getElementById('pnl-'+l.role);
      if(p){{ p.textContent=sign+l.leg_pnl.toFixed(3); p.className=cls; }}
      const pr=document.getElementById('pnlinr-'+l.role);
      if(pr){{ pr.textContent=sign+'₹'+Math.round(l.leg_pnl*FX).toLocaleString(); pr.className=cls; }}
    }});
    const ab=document.getElementById('api-badge'); if(ab) ab.innerHTML = d.ok?"<span class='dot ok'></span>live":"<span class='dot bad'></span>API down";
  }}catch(e){{}}
}}
setInterval(poll, 2000); poll();
</script>
</body></html>"""


def _render_open(state: dict, live: dict) -> str:
    legs = {l["role"]: l for l in state["legs"]}
    sp = fnum(legs["short_put"]["strike"]); sc = fnum(legs["short_call"]["strike"])
    lp = fnum(legs["long_put"]["strike"]); lc = fnum(legs["long_call"]["strike"])
    spot = live.get("spot") or fnum(state.get("spot_entry"))
    entry_spot = fnum(state.get("spot_entry"))
    credit = fnum(state.get("net_credit_usd"))
    fees_in = fnum(state.get("entry_fees_usd"))
    if fees_in <= 0:  # position predates fee recording — estimate from the legs
        try:
            fees_in = entry_fees_usd(state["legs"], fnum(state.get("spot_entry")), int(state.get("lots", 0)))
        except Exception:
            fees_in = 0.0
    maxloss = fnum(state.get("max_loss_usd"))
    mtm = fnum(live.get("mtm_gross_usd"))
    mtm_net = mtm  # entry fees already sunk; this is unrealized change since entry

    # win-zone bar: scale across [lp, lc]
    span = max(lc - lp, 1.0)
    def pct(x): return max(0.0, min(100.0, (x - lp) / span * 100.0))
    z0, z1 = pct(sp), pct(sc)
    spot_pct = pct(spot)
    in_zone = sp < spot < sc

    # countdown
    countdown = ""
    st = state.get("settlement_time", "")
    if st:
        try:
            sdt = datetime.fromisoformat(st.replace("Z", "+00:00"))
            mins = (sdt - datetime.now(timezone.utc)).total_seconds() / 60.0
            if mins > 0:
                countdown = f"settles in {int(mins//60)}h {int(mins%60)}m"
            else:
                countdown = "awaiting settlement"
        except ValueError:
            pass

    leg_rows = ""
    for role in ("short_call", "long_call", "short_put", "long_put"):
        l = next((x for x in live.get("legs_mtm", []) if x["role"] == role), legs[role])
        mark = fnum(l.get("mark"), fnum(l.get("price_per_btc")))
        lpnl = fnum(l.get("leg_pnl"))
        side = l["side"].upper()
        lpnl_inr = lpnl * FX
        cv = fnum(l.get("contract_value"), 0.001)
        leg_rows += (f"<tr><td>{role.replace('_',' ')}</td><td class='{'sell' if side=='SELL' else 'buy'}'>{side}</td>"
                     f"<td>{cv:.3f} BTC</td>"
                     f"<td>{fnum(l['strike']):,.0f}</td><td>{fnum(l.get('price_per_btc')):.1f}</td>"
                     f"<td id='mark-{role}'>{mark:.1f}</td>"
                     f"<td id='pnl-{role}' class='{_money_class(lpnl)}'>{lpnl:+.3f}</td>"
                     f"<td id='pnlinr-{role}' class='{_money_class(lpnl)}'>{'+' if lpnl>=0 else ''}₹{lpnl_inr:,.0f}</td></tr>")

    return f"""
    <div class="panel">
      <div class="panel-title">Open Position
        <span class="muted">· {state['underlying']} exp {state['expiry']} · {state['lots']} lots × {fnum(legs['short_call'].get('contract_value'),0.001):.3f} = {state['lots']*fnum(legs['short_call'].get('contract_value'),0.001):.3f} BTC · {countdown}</span></div>
      <div class="oprow">
        <div class="opbox">
          <div class="opk">Unrealized P&amp;L (live)</div>
          <div class="opv {_money_class(mtm_net)}" id="mtm-usd">{('+' if mtm_net>=0 else '')}${mtm_net:,.3f}</div>
          <div class="opsub {_money_class(mtm_net)}" id="mtm-inr">{('+' if mtm_net>=0 else '')}{_inr(mtm_net)}</div>
        </div>
        <div class="opbox"><div class="opk">Net Credit (max profit)</div>
          <div class="opv pos">${credit-fees_in:,.2f}</div>
          <div class="opsub">{_inr(credit-fees_in)} · credit ${credit:.2f} − fees ${fees_in:.2f} ({_inr(fees_in)})</div></div>
        <div class="opbox"><div class="opk">Max Loss</div>
          <div class="opv neg">−${maxloss:,.2f}</div><div class="opsub">−{_inr(maxloss)}</div></div>
        <div class="opbox"><div class="opk">Status</div>
          <div class="opv" id="status-label">{"<span class='pos'>IN ZONE ✓</span>" if in_zone else "<span class='neg'>OUT OF ZONE</span>"}</div>
          <div class="opsub" id="status-spot">spot ${spot:,.0f}</div></div>
      </div>
      <div class="zone">
        <div class="zonebar">
          <div class="profit" style="left:{z0:.1f}%;width:{max(z1-z0,0):.1f}%"></div>
          <div class="tick" style="left:{z0:.1f}%"><span>{sp:,.0f}</span></div>
          <div class="tick" style="left:{z1:.1f}%"><span>{sc:,.0f}</span></div>
          <div class="spot" id="spot-marker" style="left:{spot_pct:.1f}%" title="spot {spot:,.0f}"></div>
        </div>
        <div class="zoneends"><span>{lp:,.0f} (long put)</span><span>{lc:,.0f} (long call)</span></div>
      </div>
      <table class="legs"><thead><tr><th>leg</th><th>side</th><th>lot size</th><th>strike</th><th>entry</th><th>mark</th><th>leg P&amp;L $</th><th>leg P&amp;L ₹</th></tr></thead>
        <tbody>{leg_rows}</tbody></table>
    </div>"""


def _render_history(trades: list[dict]) -> str:
    settled = [t for t in trades if t.get("outcome")]
    if not settled:
        return "<div class='panel'><div class='panel-title'>History</div><div class='empty'>No settled trades yet.</div></div>"
    rows = ""
    for t in reversed(settled[-40:]):
        net = fnum(t.get("net_pnl_usd") or t.get("pnl_usd"))
        gross = fnum(t.get("gross_pnl_usd") or t.get("pnl_usd"))
        fees = fnum(t.get("entry_fees_usd")) + fnum(t.get("settle_fees_usd"))
        oc = t.get("outcome", "")
        badge = {"MAX_WIN": "win", "PARTIAL": "partial", "MAX_LOSS": "loss"}.get(oc, "")
        date = (t.get("settle_ts") or "")[:10]
        rows += (f"<tr><td>{date}</td><td>{t.get('expiry','')}</td>"
                 f"<td>{fnum(t.get('spot_entry')):,.0f}→{fnum(t.get('spot_settle')):,.0f}</td>"
                 f"<td>{fnum(t.get('short_put_k')):,.0f}–{fnum(t.get('short_call_k')):,.0f}</td>"
                 f"<td>{gross:+.2f}</td>"
                 f"<td>${fees:.2f} <span style='color:#9b9b9b;font-size:10px'>₹{fees*FX:,.0f}</span></td>"
                 f"<td class='{_money_class(net)}'>{net:+.2f}</td>"
                 f"<td><span class='oc {badge}'>{oc}</span></td></tr>")
    return f"""
    <div class="panel"><div class="panel-title">History <span class="muted">· {len(settled)} settled</span></div>
      <table class="hist"><thead><tr><th>date</th><th>exp</th><th>spot</th><th>shorts</th>
        <th>gross $</th><th>fees $/₹</th><th>net $</th><th>outcome</th></tr></thead>
        <tbody>{rows}</tbody></table></div>"""


CSS = """
:root{--bg:#f9f9f9;--panel:#fff;--border:#ebebeb;--text:#424242;--strong:#1f2937;
--muted:#9b9b9b;--accent:#ff5722;--green:#00a86b;--green-soft:#e8f5e9;--red:#ef5350;
--red-soft:#ffebee;--shadow:0 1px 3px rgba(0,0,0,.06);}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);
font-family:-apple-system,'Inter','Segoe UI',Roboto,Arial,sans-serif;font-size:13px}
header{padding:14px 22px;background:var(--panel);border-bottom:1px solid var(--border);
display:flex;align-items:center;justify-content:space-between}
.brand{font-size:16px;font-weight:700;color:var(--strong)}
.muted{color:var(--muted);font-weight:400}
.hmeta{display:flex;gap:14px;align-items:center;font-size:12px}
.hmeta .sep{color:var(--strong);font-weight:600}
.badge{padding:2px 8px;border-radius:4px;font-weight:700;font-size:11px}
.badge.paper{background:#fff3e0;color:#e65100}
.dot{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:4px}
.dot.ok{background:var(--green)}.dot.bad{background:var(--red)}
main{padding:18px 22px;max-width:1100px;margin:0 auto}
.cards{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin-bottom:16px}
.card{background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:14px;box-shadow:var(--shadow)}
.card .k{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.04em}
.card .v{font-size:20px;font-weight:700;color:var(--strong);margin-top:4px}
.card .sub{font-size:12px;color:var(--muted);margin-top:2px}
.panel{background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:16px;margin-bottom:16px;box-shadow:var(--shadow)}
.panel-title{font-size:14px;font-weight:700;color:var(--strong);margin-bottom:12px}
.empty{color:var(--muted);padding:18px 0;text-align:center}
.oprow{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:18px}
.opbox{background:#fafafa;border:1px solid var(--border);border-radius:6px;padding:12px}
.opk{color:var(--muted);font-size:11px;text-transform:uppercase}
.opv{font-size:22px;font-weight:700;color:var(--strong);margin-top:4px}
.opsub{font-size:12px;color:var(--muted);margin-top:2px}
.pos{color:var(--green)}.neg{color:var(--red)}.zero{color:var(--muted)}
.sell{color:var(--red);font-weight:600}.buy{color:var(--green);font-weight:600}
.zone{margin:8px 0 16px}
.zonebar{position:relative;height:30px;background:var(--red-soft);border-radius:6px;border:1px solid var(--border)}
.zonebar .profit{position:absolute;top:0;height:100%;background:var(--green-soft);border-left:2px solid var(--green);border-right:2px solid var(--green)}
.zonebar .tick{position:absolute;top:0;height:100%;border-left:1px dashed #bbb}
.zonebar .tick span{position:absolute;top:-18px;left:-18px;font-size:10px;color:var(--muted)}
.zonebar .spot{position:absolute;top:-4px;width:3px;height:38px;background:var(--accent);border-radius:2px;transition:left .6s ease-out}
@keyframes flup{0%{background:rgba(0,168,107,.22)}100%{background:transparent}}
@keyframes fldn{0%{background:rgba(239,83,80,.22)}100%{background:transparent}}
.up{animation:flup .6s ease-out}.down{animation:fldn .6s ease-out}
#hdr-spot{font-weight:700;color:var(--strong);border-radius:3px;padding:0 2px}
.zoneends{display:flex;justify-content:space-between;font-size:11px;color:var(--muted);margin-top:20px}
table{width:100%;border-collapse:collapse;font-size:12px}
th{text-align:left;color:var(--muted);font-weight:600;padding:6px 8px;border-bottom:1px solid var(--border);text-transform:uppercase;font-size:10px}
td{padding:7px 8px;border-bottom:1px solid #f3f3f3}
.oc{padding:2px 7px;border-radius:4px;font-size:11px;font-weight:600}
.oc.win{background:var(--green-soft);color:var(--green)}
.oc.loss{background:var(--red-soft);color:var(--red)}
.oc.partial{background:#fff3e0;color:#e65100}
.foot{color:var(--muted);font-size:11px;text-align:center;margin-top:8px}
@media(max-width:760px){.cards{grid-template-columns:repeat(2,1fr)}.oprow{grid-template-columns:repeat(2,1fr)}}
"""


# ============================================================
# HTTP server
# ============================================================

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _send(self, body: bytes, ctype: str, code: int = 200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        try:
            if path == "/api/live":
                state = load_state()
                live = get_live_cached(state)
                payload = live_payload(state, live)
                self._send(json.dumps(payload).encode(), "application/json")
                return
            if path in ("/", "/dashboard", "/index.html"):
                state = load_state()
                trades = load_trades()
                live = get_live_cached(state)
                stats = compute_stats(trades)
                html = render(state, live, trades, stats)
                self._send(html.encode("utf-8"), "text/html; charset=utf-8")
                return
            self.send_response(404); self.end_headers()
        except Exception as e:
            self._send(f"dashboard error: {e}".encode(), "text/plain", 500)


def main():
    print(f"Iron Condor dashboard on http://0.0.0.0:{PORT}  (capital ₹{CAPITAL_INR:,.0f})")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()

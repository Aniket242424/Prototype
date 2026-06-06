"""
Market Sentiment dashboard — standalone, Kite-themed, host port 8002.

Reads data/sentiment_latest.json (written by run_sentiment_agent.py) and shows:
  - sentiment gauge (bullish/bearish/neutral + confidence)
  - "why moving" + summary + drivers + catalysts
  - per-asset cards: bias, price, signal, SUPPORT, and what happens if it breaks
  - backend + last-run + cost
  - "Run now" button (triggers a fresh agent read)
  - Key management: set/replace Gemini + Claude API keys from the UI (masked),
    encrypted at rest via keystore. Shows where each key comes from (UI/env).

Run (systemd):  ~/ic-venv/bin/python scripts/sentiment_dashboard.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
os.chdir(REPO_ROOT)
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "src"))
from dotenv import load_dotenv  # noqa: E402
load_dotenv(REPO_ROOT / ".env")
import keystore  # noqa: E402

PORT = int(os.getenv("SENTIMENT_DASH_PORT", "8002"))
LATEST = Path("data/sentiment_latest.json")
VENV_PY = os.path.expanduser("~/ic-venv/bin/python")
AGENT = "scripts/run_sentiment_agent.py"

# Scrip lookup: import the engine ONCE at boot (cold import of pandas/yfinance
# is ~8s) so each search is warm (~1.5s) instead of paying that 8s on every
# subprocess call. Cache results briefly so repeat/popular searches are instant.
_lookup_lock = threading.Lock()
_LOOKUP_CACHE: dict = {}
_LOOKUP_TTL = 600  # seconds
try:
    from run_sentiment_agent import lookup_scrip as _lookup_scrip
    from run_sentiment_agent import track_record_stats as _track_record_stats
except Exception:
    _lookup_scrip = None
    _track_record_stats = None


def cached_lookup(q: str) -> dict:
    key = q.strip().lower()
    now = time.time()
    hit = _LOOKUP_CACHE.get(key)
    if hit and now - hit[0] < _LOOKUP_TTL:
        return hit[1]
    if _lookup_scrip is None:   # fallback: cold subprocess (still works)
        out = subprocess.run([VENV_PY, AGENT, "--lookup", q], cwd=str(REPO_ROOT),
                             capture_output=True, text=True, timeout=90)
        line = (out.stdout or "").strip().splitlines()[-1] if out.stdout.strip() else ""
        data = json.loads(line) if line else {"error": "no data returned"}
    else:
        with _lookup_lock:       # serialize yfinance access across request threads
            data = _lookup_scrip(q)
    if isinstance(data, dict) and not data.get("error"):
        _LOOKUP_CACHE[key] = (now, data)
    return data


def load_latest() -> dict | None:
    if not LATEST.exists():
        return None
    try:
        return json.loads(LATEST.read_text())
    except Exception:
        return None


def fnum(x, d=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


# ============================================================
# Rendering
# ============================================================
BIAS_COLOR = {"bullish": "var(--green)", "bearish": "var(--red)", "neutral": "var(--muted)"}


def _gauge_pct(overall: str, conf: float) -> float:
    if overall == "bullish":
        return min(100.0, 50.0 + conf / 2.0)
    if overall == "bearish":
        return max(0.0, 50.0 - conf / 2.0)
    return 50.0


ANTHROPIC_BUDGET_INR = float(os.getenv("ANTHROPIC_BUDGET_INR", "100"))


def _ema_table(matrix: dict | None, nearest_members: str | None) -> str:
    """Clear 3x3 EMA grid (Daily/Weekly/Monthly x 20/50/200). Each cell shows the
    EMA value, distance from price, role (green=support / red=resistance) and the
    historical hold-rate 'held/tests'. The nearest support is ring-highlighted."""
    if not matrix:
        return ""
    near = set((nearest_members or "").split("+"))
    rows = ""
    for tf, label in (("D", "Daily"), ("W", "Weekly"), ("M", "Monthly")):
        tds = ""
        for sp in (20, 50, 200):
            c = matrix.get(f"{tf}{sp}") or {}
            v = c.get("v")
            if v is None:
                tds += "<td class='emna'>n/a</td>"
                continue
            role = c.get("role")  # support / resistance
            cls = "emsup" if role == "support" else "emres"
            if f"{tf}{sp}" in near:
                cls += " emnear"
            pct = c.get("pct")
            rate = c.get("rate")
            hr = (f"<span class='emhr'>{rate}% · {c.get('held')}/{c.get('tests')}</span>"
                  if rate is not None else
                  (f"<span class='emhr emhrn'>{c.get('held')}/{c.get('tests')}</span>" if c.get("tests") else ""))
            arrow = "▲" if role == "support" else "▼"
            tds += (f"<td class='{cls}'><span class='emv'>{v:,.0f}</span>"
                    f"<span class='empct'>{arrow} {pct:+.1f}%</span>{hr}</td>")
        rows += f"<tr><th>{label}</th>{tds}</tr>"
    return (f"<table class='emat'><thead><tr><th></th><th>20 EMA</th><th>50 EMA</th>"
            f"<th>200 EMA</th></tr></thead><tbody>{rows}</tbody></table>")


def _lookup_html(d: dict) -> str:
    """Render an on-demand scrip lookup result: header + support/resistance + EMA table."""
    if not d or d.get("error"):
        return f"<div class='lkerr'>{_esc((d or {}).get('error', 'lookup failed'))}</div>"
    price = d.get("price") or 0
    rsi = d.get("rsi14")
    ns = d.get("nearest_support") or {}
    sf = d.get("structural_floor") or {}
    cr = d.get("controlling_resistance") or {}
    head = (f"<div class='lkhead'><b>{_esc(d.get('name', ''))}</b> "
            f"<span class='muted'>[{_esc(d.get('ticker', ''))}]</span> · "
            f"<span class='lkpx'>{price:,.2f}</span> · RSI {rsi} · {_esc(d.get('trend', ''))}"
            f"<span class='lkbadge' style='background:#eef;color:#3949ab'>{_esc(d.get('stack_daily', ''))} stack</span></div>")
    if d.get("no_ema_support") and ns:
        sup = (f"<div class='lkres'>⚠ NO EMA support — below every EMA. Structural floor "
               f"{ns.get('value', 0):,.2f} (20d low). "
               + (f"Nearest EMA {cr.get('value', 0):,.2f} is RESISTANCE ({cr.get('pct', 0):+.1f}%)." if cr else "") + "</div>")
    else:
        hr = f", held {ns['hold_rate']}% of tests" if ns.get("hold_rate") is not None else ""
        sup = (f"<div class='lksup'>▲ SUPPORT {ns.get('value', 0):,.2f} "
               f"({_esc(ns.get('members', ''))}, {ns.get('pct', 0):+.1f}%, {ns.get('grade', '')}{hr})")
        if sf and sf.get("value") != ns.get("value"):
            sup += f" · floor {sf.get('value', 0):,.2f} ({_esc(sf.get('members', ''))}, {sf.get('grade', '')})"
        sup += "</div>"
        if cr:
            sup += f"<div class='lkres'>▼ RESISTANCE {cr.get('value', 0):,.2f} ({_esc(cr.get('members', ''))}, {cr.get('pct', 0):+.1f}%)</div>"
    table = _ema_table(d.get("matrix"), ns.get("members"))
    return head + sup + table


def _trackrecord_panel() -> str:
    """Self-improvement scorecard: the agent's graded accuracy over time."""
    if _track_record_stats is None:
        return ""
    try:
        s = _track_record_stats()
    except Exception:
        s = {}
    if not s or not s.get("overall_n"):
        return ("<div class='panel'><div class='panel-title'>🧠 Agent track record "
                "<span class='muted'>· self-improving</span></div>"
                "<div class='muted'>Building track record — every run's prediction is graded against the "
                "real market move ~1 trading day later, then fed back so the agent calibrates. "
                "Stats appear here once the first calls mature.</div></div>")

    def _pill(label, val, n=None):
        if val is None:
            return ""
        col = "#00875a" if val >= 60 else "#b7791f" if val >= 45 else "#c62828"
        sub = f" <span class='muted'>({n})</span>" if n else ""
        return (f"<div class='trpill'><div class='trv' style='color:{col}'>{val}%</div>"
                f"<div class='trl'>{label}{sub}</div></div>")
    pills = (_pill("Overall accuracy", s.get("overall_acc"), f"{s.get('overall_n')} calls")
             + _pill("High-confidence", s.get("high_conf_acc"), f"{s.get('high_conf_n')}")
             + _pill("Support held", s.get("support_acc"), f"{s.get('support_n')}"))
    pa = s.get("per_asset", {})
    arows = "".join(
        f"<tr><td>{_esc(k)}</td><td>{(v['acc'] if v['acc'] is not None else '–')}"
        f"{'%' if v['acc'] is not None else ''}</td><td class='muted'>{v['n']}</td>"
        f"<td>{(str(v['support_hold'])+'%') if v.get('support_hold') is not None else '–'}</td></tr>"
        for k, v in sorted(pa.items(), key=lambda kv: kv[1]['acc'] if kv[1]['acc'] is not None else -1, reverse=True))
    atable = (f"<table class='ktbl' style='margin-top:10px'><thead><tr><th>asset</th><th>dir. acc</th>"
              f"<th>calls</th><th>support held</th></tr></thead><tbody>{arows}</tbody></table>") if arows else ""
    misses = s.get("recent_misses", [])
    miss_html = ""
    if misses:
        items = "".join(
            f"<li>{m['ts']}: said <b>{_esc(str(m['said']).upper())}</b> ({m.get('conf')}%) → market "
            f"{(('%+.1f%%' % m['move']) if m.get('move') is not None else '?')}</li>" for m in misses)
        miss_html = f"<div class='trmiss'><div class='muted'>Recent misses (it learns from these):</div><ul class='lst'>{items}</ul></div>"
    return (f"<div class='panel'><div class='panel-title'>🧠 Agent track record "
            f"<span class='muted'>· last {s.get('days')}d · graded vs real moves · fed back to calibrate</span></div>"
            f"<div class='trrow'>{pills}</div>{atable}{miss_html}</div>")


def render(r: dict | None) -> str:
    g_summary = keystore.gemini_keys_summary()
    a_keymask = keystore.masked("anthropic_api_key", "ANTHROPIC_API_KEY") or "(not set)"
    a_src = keystore.source("anthropic_api_key", "ANTHROPIC_API_KEY")
    usage = keystore.get_usage()
    gu = usage.get("gemini", {}); au = usage.get("anthropic", {})
    g_used = f"{gu.get('calls', 0)} runs · {gu.get('tokens_in', 0) + gu.get('tokens_out', 0):,} tok · ₹0 (free)"
    a_cost = au.get("cost_inr", 0.0)
    a_used = f"{au.get('calls', 0)} runs · ₹{a_cost:.0f} of ₹{ANTHROPIC_BUDGET_INR:.0f} budget"
    a_over = a_cost >= ANTHROPIC_BUDGET_INR
    active_key = (r or {}).get("_meta", {}).get("gemini_key", "")
    _rows = ""
    for k in keystore.gemini_key_list():
        act = (k["masked"] == active_key)
        _rows += (f"<tr class='{'kact' if act else ''}'><td>{k['idx']}</td><td>{k['masked']}</td>"
                  f"<td>{k['source']}</td><td>{'● active (last run)' if act else 'idle'}</td></tr>")
    gkeys_table = (f"<table class='ktbl'><thead><tr><th>#</th><th>key</th><th>source</th><th>status</th></tr></thead>"
                   f"<tbody>{_rows}</tbody></table>") if _rows else "<div class='muted'>no Gemini keys yet</div>"

    if not r:
        body = "<div class='panel'><div class='empty'>No sentiment read yet. Click <b>Run now</b>.</div></div>"
        meta_line = ""
    else:
        m = r.get("_meta", {})
        overall = (r.get("overall") or "neutral").lower()
        conf = fnum(r.get("confidence"))
        gp = _gauge_pct(overall, conf)
        oc = BIAS_COLOR.get(overall, "var(--muted)")
        fell = m.get("fell_back_from")
        backend = m.get("backend", "?")
        backend_badge = (f"<span class='badge' style='background:#ffebee;color:#c62828'>"
                         f"fallback→{backend}</span>" if fell else
                         f"<span class='badge paper'>{backend}</span>")
        cost = m.get("cost_inr", 0)
        # gauge
        gauge = f"""
        <div class="panel">
          <div class="bigbias" style="color:{oc}">{overall.upper()}<span class="conf"> · {conf:.0f}% confidence</span></div>
          <div class="gauge"><div class="gtrack">
            <span class="glabel l">BEARISH</span><span class="glabel c">NEUTRAL</span><span class="glabel r">BULLISH</span>
            <div class="gmark" style="left:{gp:.0f}%"></div>
          </div></div>
          <div class="why"><b>Why moving:</b> {_esc(r.get('why_moving',''))}</div>
          <div class="summary">{_esc(r.get('summary',''))}</div>
        </div>"""
        # drivers
        drivers = "".join(f"<li>{_esc(d)}</li>" for d in r.get("drivers", []))
        drivers_html = (f"<div class='panel'><div class='panel-title'>Key drivers</div>"
                        f"<ul class='lst'>{drivers}</ul></div>") if drivers else ""
        # per-asset cards
        cards = ""
        for a in r.get("assets", []):
            b = (a.get("bias") or "neutral").lower()
            bc = BIAS_COLOR.get(b, "var(--muted)")
            pr = a.get("price")
            priceline = ""
            if pr is not None:
                rsi = a.get("rsi"); trend = a.get("trend", "")
                priceline = (f"<div class='aprice'>{fnum(pr):,.0f}"
                             f"{' · ' + _esc(trend) if trend else ''}"
                             f"{' · RSI ' + str(rsi) if rsi is not None else ''}</div>")
            cards += f"""
            <div class="acard">
              <div class="ahead"><span class="aname">{_esc(a.get('name',''))}</span>
                <span class="abadge" style="background:{bc}1a;color:{bc}">{b}</span></div>
              {priceline}
              <div class="asig">{_esc(a.get('signal',''))}</div>
              <div class="asup"><span class="lbl">SUPPORT</span> {_esc(a.get('support',''))}</div>
              <div class="abrk"><span class="lbl">IF IT BREAKS</span> {_esc(a.get('if_breaks',''))}</div>
              {_ema_table(a.get('levels_matrix'), a.get('nearest_members'))}
            </div>"""
        assets_html = (f"<div class='panel'><div class='panel-title'>Per-asset signals</div>"
                       f"<div class='agrid'>{cards}</div></div>") if cards else ""
        # catalysts
        cats = "".join(f"<li>{_esc(c)}</li>" for c in r.get("catalysts_ahead", []))
        cats_html = (f"<div class='panel'><div class='panel-title'>Catalysts ahead</div>"
                     f"<ul class='lst'>{cats}</ul></div>") if cats else ""
        body = gauge + drivers_html + assets_html + cats_html
        meta_line = (f"backend {backend_badge} · {m.get('tokens_in',0)}+{m.get('tokens_out',0)} tok · "
                     f"cost ₹{cost} · {m.get('as_of','')[:19].replace('T',' ')} UTC")

    keypanel = f"""
    <div class="panel">
      <div class="panel-title">API keys &amp; usage <span class="muted">· set from here, encrypted at rest · keys/usage survive any model change</span></div>
      <div class="kblock">
        <div class="kname">Gemini <span class="muted">(free, primary — keys from different Gmails rotate automatically. Each Save ADDS to the list.)</span></div>
        {gkeys_table}
        <div class="kusage">usage: {g_used}</div>
        <textarea id="gkeys" rows="3" placeholder="paste 1+ keys, one per line (AQ.… or AIza…) — adds to the list"></textarea>
        <button onclick="saveGeminiKeys()">Add Gemini keys</button>
        <button onclick="clearGeminiKeys()" class="rstbtn">Clear all</button>
      </div>
      <div class="kblock">
        <div class="kname">Claude / Anthropic <span class="muted">(paid fallback — only used if all Gemini keys fail)</span></div>
        <div class="kmask">{a_keymask} <span class="ksrc">[{a_src}]</span></div>
        <div class="kusage {'over' if a_over else ''}">usage: {a_used}{' · STOPPED (budget hit)' if a_over else ''}</div>
        <input id="akey" type="password" placeholder="paste new Claude key (sk-ant-…)" />
        <button onclick="saveKey('anthropic_api_key','akey')">Save</button>
        <button onclick="resetUsage()" class="rstbtn">Reset usage</button>
      </div>
      <div id="ksave" class="ksave"></div>
    </div>"""

    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Market Sentiment</title><style>{CSS}</style></head><body>
<header>
  <div class="brand">Market Sentiment <span class="muted">· AI veteran read</span></div>
  <div class="hmeta">
    <button class="runbtn" id="runbtn" onclick="runNow()">⟳ Run now</button>
    <span class="muted" id="metaline">{meta_line}</span>
  </div>
</header>
<main>
  <div class="panel">
    <div class="panel-title">🔎 Look up any scrip <span class="muted">· multi-timeframe EMA support + historical hold-rate for ANY stock / index / crypto</span></div>
    <div class="lookrow">
      <input id="scripq" placeholder="e.g. Reliance, TCS, Infosys, NIFTY, Bank Nifty, AAPL, BTC, Gold…" onkeydown="if(event.key==='Enter')lookupScrip()"/>
      <button onclick="lookupScrip()">Search</button>
      <button onclick="clearLookup()" class="rstbtn">Clear</button>
    </div>
    <div id="lookout" class="lookout"></div>
  </div>
{_trackrecord_panel()}
{body}{keypanel}
  <div class="foot">Gemini (free) → Claude (fallback) → neutral. Not financial advice. Auto-refreshes every 20s.</div>
</main>
<script>
async function lookupScrip(){{
  const q=document.getElementById('scripq').value.trim();
  const out=document.getElementById('lookout');
  if(!q){{ out.innerHTML=''; return; }}
  out.innerHTML='<div class="muted">Looking up '+q+'… (first fetch can take a few seconds)</div>';
  try{{
    const r=await fetch('/api/lookup?q='+encodeURIComponent(q),{{cache:'no-store'}});
    out.innerHTML=await r.text();
  }}catch(e){{ out.innerHTML='<div class="lkerr">lookup failed: '+e+'</div>'; }}
}}
function clearLookup(){{
  document.getElementById('scripq').value='';
  document.getElementById('lookout').innerHTML='';
}}
async function saveKey(name, inputId){{
  const v = document.getElementById(inputId).value.trim(); if(!v) return;
  const el=document.getElementById('ksave'); el.textContent='saving…';
  try{{ const r=await fetch('/api/setkey',{{method:'POST',headers:{{'Content-Type':'application/json'}},
      body:JSON.stringify({{name,value:v}})}}); const d=await r.json();
    el.textContent = d.ok ? '✓ saved — reloading…' : ('error: '+(d.error||''));
    if(d.ok) setTimeout(()=>location.reload(),900);
  }}catch(e){{ el.textContent='error: '+e; }}
}}
async function saveGeminiKeys(){{
  const v = document.getElementById('gkeys').value.trim(); if(!v) return;
  const el=document.getElementById('ksave'); el.textContent='saving…';
  try{{ const r=await fetch('/api/setkey',{{method:'POST',headers:{{'Content-Type':'application/json'}},
      body:JSON.stringify({{name:'gemini_keys',value:v}})}}); const d=await r.json();
    el.textContent = d.ok ? ('✓ saved '+(d.count||'')+' Gemini key(s) — reloading…') : ('error: '+(d.error||''));
    if(d.ok) setTimeout(()=>location.reload(),900);
  }}catch(e){{ el.textContent='error: '+e; }}
}}
async function clearGeminiKeys(){{
  const el=document.getElementById('ksave'); el.textContent='clearing…';
  try{{ await fetch('/api/cleargemini',{{method:'POST'}}); el.textContent='✓ cleared UI keys — reloading…';
    setTimeout(()=>location.reload(),700);
  }}catch(e){{ el.textContent='error: '+e; }}
}}
async function resetUsage(){{
  const el=document.getElementById('ksave'); el.textContent='resetting…';
  try{{ await fetch('/api/resetusage',{{method:'POST'}}); el.textContent='✓ usage reset — reloading…';
    setTimeout(()=>location.reload(),700);
  }}catch(e){{ el.textContent='error: '+e; }}
}}
async function runNow(){{
  const b=document.getElementById('runbtn'); b.disabled=true; b.textContent='⟳ running… (~30s)';
  try{{ await fetch('/api/run',{{method:'POST'}}); }}catch(e){{}}
  // poll for a fresh read
  let n=0; const t=setInterval(async()=>{{ n++;
    const r=await fetch('/api/sentiment',{{cache:'no-store'}}); const d=await r.json();
    if(d && d._meta && d._fresh){{ clearInterval(t); location.reload(); }}
    if(n>40){{ clearInterval(t); b.disabled=false; b.textContent='⟳ Run now'; }}
  }}, 3000);
}}
// Auto-refresh the sentiment read — but DON'T wipe a scrip search result the
// user is reading (or a query they're typing). Resume once the search is cleared.
setInterval(()=>{{
  const out=document.getElementById('lookout');
  const q=document.getElementById('scripq');
  const busy=(out && out.innerHTML.trim()!=='') || (q && (document.activeElement===q || q.value.trim()!==''));
  if(!busy) location.reload();
}}, 20000);
</script></body></html>"""


def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


CSS = """
:root{--bg:#f9f9f9;--panel:#fff;--border:#ebebeb;--text:#424242;--strong:#1f2937;--muted:#9b9b9b;
--accent:#ff5722;--green:#00a86b;--red:#ef5350;--shadow:0 1px 3px rgba(0,0,0,.06)}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);
font-family:-apple-system,'Inter','Segoe UI',Roboto,Arial,sans-serif;font-size:13px}
header{padding:14px 22px;background:var(--panel);border-bottom:1px solid var(--border);
display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px}
.brand{font-size:16px;font-weight:700;color:var(--strong)}.muted{color:var(--muted);font-weight:400}
.hmeta{display:flex;gap:12px;align-items:center;font-size:12px}
.badge{padding:2px 8px;border-radius:4px;font-weight:700;font-size:11px}.badge.paper{background:#e8f5e9;color:#00a86b}
.runbtn{background:var(--accent);color:#fff;border:none;border-radius:6px;padding:6px 12px;font-weight:600;cursor:pointer}
.runbtn:disabled{opacity:.6;cursor:default}
main{padding:18px 22px;max-width:1080px;margin:0 auto}
.panel{background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:16px;margin-bottom:14px;box-shadow:var(--shadow)}
.panel-title{font-size:14px;font-weight:700;color:var(--strong);margin-bottom:10px}
.empty{color:var(--muted);text-align:center;padding:20px}
.bigbias{font-size:34px;font-weight:800;letter-spacing:-.01em}.bigbias .conf{font-size:16px;color:var(--muted);font-weight:600}
.gauge{margin:14px 0 6px}
.gtrack{position:relative;height:14px;border-radius:8px;background:linear-gradient(90deg,#ef5350 0%,#ffe0b2 50%,#00a86b 100%)}
.gmark{position:absolute;top:-5px;width:4px;height:24px;background:#1f2937;border-radius:2px;transition:left .5s}
.glabel{position:absolute;top:18px;font-size:10px;color:var(--muted)}.glabel.l{left:0}.glabel.c{left:50%;transform:translateX(-50%)}.glabel.r{right:0}
.why{margin-top:26px;font-size:15px;color:var(--strong)}.summary{margin-top:8px;color:var(--text);line-height:1.6}
.lst{margin:0;padding-left:18px;line-height:1.7}
.agrid{display:grid;grid-template-columns:repeat(2,1fr);gap:12px}
.acard{border:1px solid var(--border);border-radius:8px;padding:12px;background:#fafafa}
.ahead{display:flex;justify-content:space-between;align-items:center;margin-bottom:6px}
.aname{font-weight:700;color:var(--strong)}.abadge{padding:2px 8px;border-radius:4px;font-size:11px;font-weight:700}
.aprice{font-family:monospace;font-weight:700;color:var(--strong);margin-bottom:6px}
.asig{color:var(--text);line-height:1.5;margin-bottom:8px}
.asup,.abrk{font-size:12px;line-height:1.5;margin-top:4px}.asup{color:#00695c}.abrk{color:#b71c1c}
.lbl{display:inline-block;font-size:9px;font-weight:700;letter-spacing:.05em;padding:1px 5px;border-radius:3px;background:#fff;border:1px solid var(--border);margin-right:4px}
.emat{width:100%;border-collapse:separate;border-spacing:3px;margin-top:10px;font-family:monospace}
.emat th{font-size:9px;font-weight:700;color:var(--muted);text-transform:uppercase;padding:2px 4px;text-align:center}
.emat tbody th{text-align:left;width:48px}
.emat td{border-radius:5px;padding:5px 6px;text-align:center;line-height:1.25;vertical-align:top;border:1px solid transparent}
.emat .emv{display:block;font-weight:700;font-size:12px;color:var(--strong)}
.emat .empct{display:block;font-size:10px}
.emat .emhr{display:block;font-size:9px;font-weight:700;margin-top:1px}
.emat .emhrn{opacity:.55;font-weight:400}
.emsup{background:#e8f5e9}.emsup .empct{color:#00875a}.emsup .emhr{color:#00875a}
.emres{background:#fdecea}.emres .empct{color:#c62828}.emres .emhr{color:#c62828}
.emna{background:#f5f5f5;color:#bbb;font-size:10px;vertical-align:middle}
.emnear{border:2px solid #00a86b;box-shadow:0 0 0 1px #00a86b inset}
.lookrow{display:flex;gap:8px;margin-top:4px}
.lookrow input{flex:1;padding:10px 12px;border:1px solid var(--border);border-radius:6px;font-size:14px}
.lookrow button{padding:10px 20px;border:0;border-radius:6px;background:var(--green,#00a86b);color:#fff;font-weight:700;cursor:pointer}
.lookout{margin-top:12px}
.lkhead{font-size:16px;color:var(--strong);margin-bottom:4px}
.lkpx{font-family:monospace;font-weight:700}
.lksup{font-size:12px;margin:6px 0;color:#00695c}.lkres{font-size:12px;margin:2px 0;color:#b71c1c}
.lkerr{color:#b71c1c;font-size:13px;padding:8px;background:#fdecea;border-radius:6px}
.lkbadge{padding:2px 8px;border-radius:4px;font-size:11px;font-weight:700;margin-left:6px}
.trrow{display:flex;gap:18px;flex-wrap:wrap}
.trpill{text-align:center;min-width:96px}
.trpill .trv{font-size:26px;font-weight:800;line-height:1}
.trpill .trl{font-size:11px;color:var(--muted);margin-top:3px}
.trmiss{margin-top:10px;font-size:12px}.trmiss .lst{margin:4px 0 0;color:#b71c1c}
.krow{display:grid;grid-template-columns:200px 200px 1fr 70px;gap:8px;align-items:center;margin-bottom:8px}
.kname{font-weight:600;color:var(--strong)}.kmask{font-family:monospace;color:var(--muted)}.ksrc{font-size:10px}
.krow input{padding:6px 8px;border:1px solid var(--border);border-radius:5px}
.krow button{background:var(--strong);color:#fff;border:none;border-radius:5px;padding:6px;cursor:pointer}
.ksave{font-size:12px;color:var(--green);min-height:16px}
.kblock{padding:10px 0;border-bottom:1px solid var(--border)}
.kblock .kname{margin-bottom:4px}.kblock .kmask{font-family:monospace;color:var(--muted);margin-bottom:2px}
.kusage{font-size:11px;color:var(--muted);margin-bottom:6px}.kusage.over{color:var(--red);font-weight:600}
.kblock textarea{width:100%;max-width:520px;font-family:monospace;font-size:12px;padding:6px 8px;border:1px solid var(--border);border-radius:5px;display:block;margin-bottom:6px}
.kblock button{background:var(--strong);color:#fff;border:none;border-radius:5px;padding:6px 12px;cursor:pointer;margin-right:6px}
.rstbtn{background:#fff !important;color:var(--muted) !important;border:1px solid var(--border) !important}
.ktbl{width:100%;max-width:520px;border-collapse:collapse;font-size:12px;margin:6px 0}
.ktbl th{text-align:left;color:var(--muted);font-weight:600;font-size:10px;text-transform:uppercase;padding:4px 8px;border-bottom:1px solid var(--border)}
.ktbl td{padding:5px 8px;border-bottom:1px solid #f3f3f3;font-family:monospace}
.ktbl tr.kact td{background:var(--green-soft,#e8f5e9);color:var(--green);font-weight:700}
.foot{color:var(--muted);font-size:11px;text-align:center;margin-top:8px}
@media(max-width:760px){.agrid{grid-template-columns:1fr}.krow{grid-template-columns:1fr}}
"""


# ============================================================
# HTTP
# ============================================================
_last_mtime = {"v": 0.0}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body, ctype="text/html; charset=utf-8", code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        try:
            if path == "/api/sentiment":
                r = load_latest() or {}
                mtime = LATEST.stat().st_mtime if LATEST.exists() else 0
                r["_fresh"] = mtime > _last_mtime["v"]
                self._send(json.dumps(r).encode(), "application/json")
                return
            if path == "/api/lookup":
                from urllib.parse import urlparse, parse_qs
                qs = parse_qs(urlparse(self.path).query)
                q = (qs.get("q") or [""])[0].strip()
                as_json = (qs.get("fmt") or [""])[0] == "json"   # Telegram /scrip uses JSON
                if not q:
                    if as_json:
                        self._send(json.dumps({"error": "empty query"}).encode(), "application/json")
                    else:
                        self._send(b"<div class='lkerr'>Type a scrip name or ticker.</div>")
                    return
                try:
                    data = cached_lookup(q)
                except Exception as e:
                    data = {"error": str(e)}
                if as_json:
                    self._send(json.dumps(data).encode(), "application/json")
                else:
                    self._send(_lookup_html(data).encode("utf-8"))
                return
            if path in ("/", "/dashboard", "/index.html"):
                _last_mtime["v"] = LATEST.stat().st_mtime if LATEST.exists() else 0
                self._send(render(load_latest()).encode("utf-8"))
                return
            self.send_response(404); self.end_headers()
        except Exception as e:
            self._send(f"error: {e}".encode(), "text/plain", 500)

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
            if self.path == "/api/setkey":
                name = body.get("name"); val = body.get("value")
                if name == "gemini_keys" and val:
                    keys = [k.strip() for k in str(val).replace(",", "\n").splitlines() if k.strip()]
                    total = keystore.add_gemini_keys(keys)   # APPEND (not replace)
                    self._send(json.dumps({"ok": True, "count": total}).encode(), "application/json")
                elif name in ("gemini_api_key", "anthropic_api_key") and val:
                    keystore.set_key(name, val.strip())
                    self._send(json.dumps({"ok": True}).encode(), "application/json")
                else:
                    self._send(json.dumps({"ok": False, "error": "bad params"}).encode(), "application/json", 400)
                return
            if self.path == "/api/resetusage":
                keystore.reset_usage()
                self._send(json.dumps({"ok": True}).encode(), "application/json")
                return
            if self.path == "/api/cleargemini":
                keystore.set_gemini_keys([])
                self._send(json.dumps({"ok": True}).encode(), "application/json")
                return
            if self.path == "/api/run":
                subprocess.Popen([VENV_PY, AGENT], cwd=str(REPO_ROOT),
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self._send(json.dumps({"ok": True}).encode(), "application/json")
                return
            self.send_response(404); self.end_headers()
        except Exception as e:
            self._send(json.dumps({"ok": False, "error": str(e)}).encode(), "application/json", 500)


def main():
    print(f"Market Sentiment dashboard on http://0.0.0.0:{PORT}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()

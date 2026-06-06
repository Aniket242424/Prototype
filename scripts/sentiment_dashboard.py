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


def render(r: dict | None) -> str:
    g_keymask = keystore.masked("gemini_api_key", "GEMINI_API_KEY") or "(not set)"
    g_src = keystore.source("gemini_api_key", "GEMINI_API_KEY")
    a_keymask = keystore.masked("anthropic_api_key", "ANTHROPIC_API_KEY") or "(not set)"
    a_src = keystore.source("anthropic_api_key", "ANTHROPIC_API_KEY")

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
            cards += f"""
            <div class="acard">
              <div class="ahead"><span class="aname">{_esc(a.get('name',''))}</span>
                <span class="abadge" style="background:{bc}1a;color:{bc}">{b}</span></div>
              <div class="asig">{_esc(a.get('signal',''))}</div>
              <div class="asup"><span class="lbl">SUPPORT</span> {_esc(a.get('support',''))}</div>
              <div class="abrk"><span class="lbl">IF IT BREAKS</span> {_esc(a.get('if_breaks',''))}</div>
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
      <div class="panel-title">API keys <span class="muted">· set from here, encrypted at rest</span></div>
      <div class="krow">
        <div class="kname">Gemini <span class="muted">(free, primary)</span></div>
        <div class="kmask">{g_keymask} <span class="ksrc">[{g_src}]</span></div>
        <input id="gkey" type="password" placeholder="paste new Gemini key (AIza…)" />
        <button onclick="saveKey('gemini_api_key','gkey')">Save</button>
      </div>
      <div class="krow">
        <div class="kname">Claude / Anthropic <span class="muted">(fallback)</span></div>
        <div class="kmask">{a_keymask} <span class="ksrc">[{a_src}]</span></div>
        <input id="akey" type="password" placeholder="paste new Claude key (sk-ant-…)" />
        <button onclick="saveKey('anthropic_api_key','akey')">Save</button>
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
<main>{body}{keypanel}
  <div class="foot">Gemini (free) → Claude (fallback) → neutral. Not financial advice. Auto-refreshes every 20s.</div>
</main>
<script>
async function saveKey(name, inputId){{
  const v = document.getElementById(inputId).value.trim(); if(!v) return;
  const el=document.getElementById('ksave'); el.textContent='saving…';
  try{{ const r=await fetch('/api/setkey',{{method:'POST',headers:{{'Content-Type':'application/json'}},
      body:JSON.stringify({{name,value:v}})}}); const d=await r.json();
    el.textContent = d.ok ? '✓ saved — reloading…' : ('error: '+(d.error||''));
    if(d.ok) setTimeout(()=>location.reload(),900);
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
setInterval(()=>location.reload(), 20000);
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
.asig{color:var(--text);line-height:1.5;margin-bottom:8px}
.asup,.abrk{font-size:12px;line-height:1.5;margin-top:4px}.asup{color:#00695c}.abrk{color:#b71c1c}
.lbl{display:inline-block;font-size:9px;font-weight:700;letter-spacing:.05em;padding:1px 5px;border-radius:3px;background:#fff;border:1px solid var(--border);margin-right:4px}
.krow{display:grid;grid-template-columns:200px 200px 1fr 70px;gap:8px;align-items:center;margin-bottom:8px}
.kname{font-weight:600;color:var(--strong)}.kmask{font-family:monospace;color:var(--muted)}.ksrc{font-size:10px}
.krow input{padding:6px 8px;border:1px solid var(--border);border-radius:5px}
.krow button{background:var(--strong);color:#fff;border:none;border-radius:5px;padding:6px;cursor:pointer}
.ksave{font-size:12px;color:var(--green);min-height:16px}
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
                if name in ("gemini_api_key", "anthropic_api_key") and val:
                    keystore.set_key(name, val.strip())
                    self._send(json.dumps({"ok": True}).encode(), "application/json")
                else:
                    self._send(json.dumps({"ok": False, "error": "bad params"}).encode(), "application/json", 400)
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

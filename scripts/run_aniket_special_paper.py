"""
Aniket Special — paper-trade runner with intraday stop-losses (Phase H.4).

Structure (BTC daily/0DTE, PAPER on Delta India):
  SHORT 5 ITM calls  (ITM1,5,10,20,30 strikes), 10 lots each
  SHORT 5 ITM puts   (ITM1,5,10,20,30 strikes), 10 lots each
  BUY   OTM20 call (50 lots) + OTM20 put (50 lots)  -> tail hedge (defined risk)

  Per-leg stop-loss on the SHORT legs (exit when the option's price has risen
  by SL% from entry — i.e. live mark >= entry * (1 + SL)):
       ITM1 80% · ITM5 70% · ITM10 60% · ITM20 50% · ITM30 40%
  ("nth ITM" = nth available strike in-the-money; OTM20 = 20th OTM strike.)

Three commands (same spirit as the condor bot, plus a monitor for the stops):
  enter    open the 12-leg structure (paper), record entry fills + stop levels
  monitor  poll live marks; exit any short leg whose stop is hit (run every ~15m)
  settle   at expiry, settle remaining legs at intrinsic; log net P&L vs ₹2L
  status   show open structure + live mark-to-market

SAFETY: paper only unless DELTA_PAPER=false AND LIVE_TRADING=true (same gate).
Separate ₹2,00,000 capital + its own state/CSV (data/aniket_*).

ASSUMPTIONS (tell me to change any): stop = mark>=entry*(1+SL); short 10 lots/leg;
OTM20 hedge 50 lots/side; nth-ITM = nth available strike.

Run:  python3 scripts/run_aniket_special_paper.py {enter|monitor|settle|status}
"""
from __future__ import annotations

import argparse, asyncio, csv, json, os, sys
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
os.chdir(REPO_ROOT); sys.path.insert(0, str(REPO_ROOT/"src"))
from dotenv import load_dotenv  # noqa: E402
load_dotenv(REPO_ROOT/".env")

from trading_agent.brokers.delta.client import DeltaClient  # noqa: E402
from trading_agent.brokers.delta.fees import leg_fee_usd  # noqa: E402
try:
    from trading_agent.monitoring.telegram_alerter import alert as _tg_alert
except Exception:
    _tg_alert = None
try:
    from trading_agent.core.logging import get_logger
    log = get_logger(__name__)
except Exception:
    class _L:
        def _e(s,l,e,**k): print(f"[{l}] {e} "+" ".join(f"{a}={v}" for a,v in k.items()))
        def info(s,e="",**k): s._e("info",e,**k)
        def warning(s,e="",**k): s._e("warn",e,**k)
        def error(s,e="",**k): s._e("err",e,**k)
    log = _L()

UNDERLYING = "BTC"
PERP = "BTCUSD"
CV_DEFAULT = 0.001
SHORT_LOTS = int(os.getenv("ANIKET_SHORT_LOTS", "10"))
HEDGE_LOTS = int(os.getenv("ANIKET_HEDGE_LOTS", "50"))
ITM_DEPTHS = [1, 5, 10, 20, 30]
OTM_HEDGE_DEPTH = 20
SL_PCT = {1: 0.80, 5: 0.70, 10: 0.60, 20: 0.50, 30: 0.40}
CAPTURE = 0.90
CAPITAL_INR = float(os.getenv("ANIKET_CAPITAL_INR", "200000"))
FX = float(os.getenv("DELTA_IC_FX_INR_USD", "84"))
CAPITAL_USD = CAPITAL_INR / FX
MIN_TTE_HRS = 2.0

PAPER = os.getenv("DELTA_PAPER", "true").lower() != "false"
LIVE_GATE = os.getenv("LIVE_TRADING", "false").lower() == "true"
IS_LIVE = (not PAPER) and LIVE_GATE

STATE = Path("data/aniket_state.json")
CSV = Path("data/aniket_trades.csv")
CSV_FIELDS = ["entry_ts","settle_ts","expiry","spot_entry","spot_settle",
              "net_credit_usd","stopped_legs","gross_pnl_usd","fees_usd",
              "net_pnl_usd","net_pnl_inr","equity_inr","outcome"]


def now(): return datetime.now(timezone.utc)
def iso(d): return d.isoformat(timespec="seconds")
def fnum(x,d=0.0):
    try: return float(x)
    except (TypeError,ValueError): return d


@dataclass
class Leg:
    role: str            # e.g. "short_call_ITM5" / "hedge_call"
    option_type: str     # call/put
    side: str            # sell/buy
    product_id: int
    symbol: str
    strike: float
    lots: int
    contract_value: float
    entry_px: float      # per-BTC premium at entry (bid for sells, ask for buys)
    sl_pct: float = 0.0  # 0 for hedges (no stop)
    stop_px: float = 0.0
    status: str = "open" # open | stopped | settled
    exit_px: float = 0.0


@dataclass
class AniketState:
    entry_ts: str
    expiry: str
    settlement_time: str
    spot_entry: float
    mode: str
    legs: list[dict]
    net_credit_usd: float


# ---------- persistence ----------
def save(st):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp"); tmp.write_text(json.dumps(asdict(st), indent=2)); os.replace(tmp, STATE)

def load():
    if not STATE.exists(): return None
    try: return AniketState(**json.loads(STATE.read_text()))
    except Exception as e:
        log.error("aniket.state_corrupt", error=str(e))
        try: STATE.replace(STATE.with_suffix(".corrupt"))
        except Exception: pass
        return None

def clear():
    if STATE.exists(): STATE.unlink()

def append_csv(row):
    CSV.parent.mkdir(parents=True, exist_ok=True)
    hdr = not CSV.exists()
    with open(CSV,"a",newline="",encoding="utf-8") as f:
        w=csv.DictWriter(f,fieldnames=CSV_FIELDS,extrasaction="ignore")
        if hdr: w.writeheader()
        w.writerow(row)

async def tg(kind,msg):
    if _tg_alert is None: return
    try: await _tg_alert(kind,msg)
    except Exception as e: log.warning("tg.fail",error=str(e))


# ---------- expiry + strike selection ----------
def parse_ddmmyy(s): return datetime(2000+int(s[4:6]),int(s[2:4]),int(s[0:2]),12,0,tzinfo=timezone.utc)

async def choose_expiry(c):
    exps = await c.list_expiries(UNDERLYING)
    cutoff = now()+timedelta(hours=MIN_TTE_HRS)
    fut=[e for e in exps if parse_ddmmyy(e)>cutoff]
    if not fut: raise ValueError("no expiry >2h out")
    return fut[0]

def nth_strike(strikes, spot, n, direction):
    """nth strike from spot. direction 'below' (ITM calls / OTM puts) or 'above'."""
    if direction=="below":
        cand=sorted([s for s in strikes if s<spot], reverse=True)
    else:
        cand=sorted([s for s in strikes if s>spot])
    if not cand: return None
    return cand[min(n-1, len(cand)-1)]


# ---------- live pricing ----------
async def mark_of(c, symbol, side):
    try:
        t = await c.get_option_ticker(symbol) or {}
        q = t.get("quotes") or {}
        if side=="sell":  # to exit a short we BUY back -> use ask, fall back to mark
            return fnum(t.get("best_ask") or q.get("best_ask") or t.get("mark_price"))
        return fnum(t.get("mark_price") or t.get("best_bid") or q.get("best_bid"))
    except Exception:
        return 0.0


# ---------- ENTER ----------
async def cmd_enter():
    if load() is not None:
        print("Aniket: position already open — settle first."); return
    async with DeltaClient.from_env() as c:
        try:
            spot = await c.get_spot_price(PERP)
            expiry = await choose_expiry(c)
            chain = await c.get_option_chain(UNDERLYING, expiry)
            calls = {p["strike"]:p for p in chain if p["option_type"]=="call"}
            puts  = {p["strike"]:p for p in chain if p["option_type"]=="put"}
            if not calls or not puts: raise ValueError("empty chain")
            ck, pk = list(calls), list(puts)
            legs: list[Leg] = []

            async def add(role, products, strike, side, lots, sl):
                p = products[strike]
                # entry fill: sells at bid, buys at ask (conservative)
                t = await c.get_option_ticker(p["symbol"]) or {}
                q = t.get("quotes") or {}
                if side=="sell":
                    px = fnum(t.get("best_bid") or q.get("best_bid") or t.get("mark_price"))
                else:
                    px = fnum(t.get("best_ask") or q.get("best_ask") or t.get("mark_price"))
                cv = fnum(p.get("contract_value"), CV_DEFAULT)
                leg = Leg(role, p["option_type"], side, int(p["id"]), p["symbol"],
                          float(p["strike"]), lots, cv, px, sl,
                          stop_px=px*(1+sl) if sl>0 else 0.0)
                legs.append(leg)

            for n in ITM_DEPTHS:
                sc = nth_strike(ck, spot, n, "below")   # ITM call (strike below spot)
                sp = nth_strike(pk, spot, n, "above")   # ITM put  (strike above spot)
                if sc: await add(f"short_call_ITM{n}", calls, sc, "sell", SHORT_LOTS, SL_PCT[n])
                if sp: await add(f"short_put_ITM{n}",  puts,  sp, "sell", SHORT_LOTS, SL_PCT[n])
            hc = nth_strike(ck, spot, OTM_HEDGE_DEPTH, "above")  # OTM20 call
            hp = nth_strike(pk, spot, OTM_HEDGE_DEPTH, "below")  # OTM20 put
            if hc: await add("hedge_call", calls, hc, "buy", HEDGE_LOTS, 0.0)
            if hp: await add("hedge_put",  puts,  hp, "buy", HEDGE_LOTS, 0.0)
        except Exception as e:
            print(f"Aniket enter SKIPPED: {e}"); await tg("info", f"<b>Aniket skip</b>: {e}"); return

        if any(l.entry_px <= 0 for l in legs if l.side=="sell"):
            print("Aniket ABORT: a short leg has no premium (illiquid).");
            await tg("error","<b>Aniket abort</b>: illiquid short leg (no premium)."); return

        net_credit = sum(
            (l.entry_px*CAPTURE if l.side=="sell" else -l.entry_px) * l.lots * l.contract_value
            for l in legs)
        st = AniketState(iso(now()), expiry, "", spot,
                         "live" if IS_LIVE else "paper",
                         [asdict(l) for l in legs], net_credit)
        save(st)
        _print_enter(st)
        await tg("trade_entry", _tg_enter(st))


def _print_enter(st):
    print("="*70); print(f"ANIKET SPECIAL ENTERED [{st.mode.upper()}]  BTC exp {st.expiry}")
    print(f"  spot ${st.spot_entry:,.0f}   legs {len(st.legs)}")
    for l in st.legs:
        s = " SL@%.0f%%->%.1f"%(l['sl_pct']*100,l['stop_px']) if l['sl_pct']>0 else ""
        print(f"  {l['role']:18s} {l['side']:4s} K={l['strike']:>8.0f} x{l['lots']:<3d} @ {l['entry_px']:.1f}{s}")
    print(f"  NET CREDIT ${st.net_credit_usd:+.2f}  (₹{st.net_credit_usd*FX:+,.0f})"); print("="*70)


def _tg_enter(st):
    n_short=sum(1 for l in st.legs if l['side']=='sell')
    tag="🧪 PAPER" if st.mode=="paper" else "🔴 LIVE"
    return (f"<b>Aniket Special entered</b> {tag}\nBTC exp {st.expiry} · spot ${st.spot_entry:,.0f}\n"
            f"{n_short} short ITM legs + 2 OTM hedges\n"
            f"Net credit <b>${st.net_credit_usd:+.2f}</b> (₹{st.net_credit_usd*FX:+,.0f})\n"
            f"Per-leg stops armed. Monitoring intraday.")


# ---------- MONITOR ----------
async def cmd_monitor():
    st = load()
    if st is None: print("Aniket: nothing to monitor."); return
    newly=[]
    async with DeltaClient.from_env() as c:
        for l in st.legs:
            if l["status"]!="open" or l["side"]!="sell" or l["sl_pct"]<=0: continue
            mark = await mark_of(c, l["symbol"], "sell")
            if mark>0 and mark >= l["stop_px"]:
                l["status"]="stopped"; l["exit_px"]=mark; newly.append(l)
                log.info("aniket.leg_stopped", role=l["role"], mark=mark, stop=l["stop_px"])
    if newly:
        save(st)
        lines="\n".join(f"• {l['role']} stopped @ {l['exit_px']:.1f} (SL {l['sl_pct']*100:.0f}%)" for l in newly)
        await tg("trade_exit", f"<b>Aniket stops hit</b> ({len(newly)})\n{lines}")
        print(f"stopped {len(newly)} legs");
    else:
        print("no stops triggered")


# ---------- SETTLE ----------
async def cmd_settle():
    st = load()
    if st is None: print("Aniket: nothing to settle."); return
    sdt = parse_ddmmyy(st.expiry)
    if now() < sdt:
        print(f"Aniket: not expired ({(sdt-now()).total_seconds()/60:.0f} min left)."); return
    async with DeltaClient.from_env() as c:
        spot_settle = await c.get_spot_price(PERP)

    gross=0.0; fees=0.0; stopped=0
    for l in st.legs:
        cv=l["contract_value"]; q=l["lots"]*cv; entry=l["entry_px"]
        fees += leg_fee_usd(st.spot_entry, entry, l["lots"], cv)
        if l["side"]=="sell":
            if l["status"]=="stopped":
                gross += (entry - l["exit_px"])*q; stopped+=1
                fees += leg_fee_usd(spot_settle, l["exit_px"], l["lots"], cv)
            else:
                intr = max(0.0,spot_settle-l["strike"]) if l["option_type"]=="call" else max(0.0,l["strike"]-spot_settle)
                gross += (entry - intr)*q
                if intr>0: fees += leg_fee_usd(spot_settle, intr, l["lots"], cv)
        else:  # hedge long
            intr = max(0.0,spot_settle-l["strike"]) if l["option_type"]=="call" else max(0.0,l["strike"]-spot_settle)
            gross += (intr - entry)*q
            if intr>0: fees += leg_fee_usd(spot_settle, intr, l["lots"], cv)

    # apply capture only to the short ENTRY credit portion already embedded above?
    # Above used full entry for shorts; reduce by the (1-CAPTURE) we wouldn't capture:
    cap_adj = sum((l["entry_px"]*(1-CAPTURE))*l["lots"]*l["contract_value"]
                  for l in st.legs if l["side"]=="sell")
    gross -= cap_adj
    net = gross - fees
    equity_inr = CAPITAL_INR + net*FX
    outcome = "WIN" if net>0 else "LOSS"
    row = {"entry_ts":st.entry_ts,"settle_ts":iso(now()),"expiry":st.expiry,
           "spot_entry":round(st.spot_entry,2),"spot_settle":round(spot_settle,2),
           "net_credit_usd":round(st.net_credit_usd,3),"stopped_legs":stopped,
           "gross_pnl_usd":round(gross,3),"fees_usd":round(fees,3),
           "net_pnl_usd":round(net,3),"net_pnl_inr":round(net*FX,0),
           "equity_inr":round(equity_inr,0),"outcome":outcome}
    append_csv(row); clear()
    print("="*70)
    print(f"ANIKET SETTLED [{st.mode.upper()}] exp {st.expiry}  spot {st.spot_entry:,.0f}->{spot_settle:,.0f}")
    print(f"  stopped legs {stopped}  gross ${gross:+.2f}  fees ${fees:.2f}  NET ${net:+.2f} (₹{net*FX:+,.0f})")
    print(f"  equity ₹{equity_inr:,.0f}"); print("="*70)
    emoji = "✅" if net>0 else "🔴"
    await tg("daily_summary",
             f"<b>Aniket settled</b> {emoji}\nexp {st.expiry} · spot ${st.spot_entry:,.0f}→${spot_settle:,.0f}\n"
             f"{stopped} legs stopped intraday\nNet P&L <b>₹{net*FX:+,.0f}</b> (${net:+.2f})\n"
             f"Equity: ₹{equity_inr:,.0f}")


# ---------- STATUS ----------
async def cmd_status():
    st=load()
    print("="*64); print(f"Aniket Special — {'LIVE' if IS_LIVE else 'PAPER'}  capital ₹{CAPITAL_INR:,.0f}")
    print(f"  short {SHORT_LOTS} lots/leg · hedge {HEDGE_LOTS} · ITM {ITM_DEPTHS} · OTM{OTM_HEDGE_DEPTH}")
    print("="*64)
    if st is None: print("No open position.");
    else:
        print(f"OPEN exp {st.expiry}  spot@entry ${st.spot_entry:,.0f}  credit ${st.net_credit_usd:+.2f}")
        for l in st.legs:
            print(f"  {l['role']:18s} {l['side']:4s} K={l['strike']:>8.0f} status={l['status']}")
    try:
        async with DeltaClient.from_env() as c:
            print(f"Live BTC ${await c.get_spot_price(PERP):,.2f}  (API OK)")
    except Exception as e:
        print(f"API FAILED: {e}")


async def _amain():
    ap=argparse.ArgumentParser(); ap.add_argument("command",choices=["enter","monitor","settle","status"])
    a=ap.parse_args()
    print(f"[{iso(now())}] aniket {a.command}  mode={'LIVE' if IS_LIVE else 'PAPER'} "
          f"(DELTA_PAPER={PAPER} LIVE_TRADING={LIVE_GATE})")
    await {"enter":cmd_enter,"monitor":cmd_monitor,"settle":cmd_settle,"status":cmd_status}[a.command]()

if __name__=="__main__":
    asyncio.run(_amain())

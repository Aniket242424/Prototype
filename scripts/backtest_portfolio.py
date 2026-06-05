"""
Portfolio backtest — Iron Condor (short vol) + Trend (long vol), uncorrelated.

Thesis: the condor makes money in calm/range markets and loses on big moves;
a trend strategy loses in chop but profits from big sustained moves. If their
returns are uncorrelated/negative, combining them gives a SMOOTHER equity curve
=> more return per unit of drawdown (the diversification free lunch).

Method (honest, standard):
  1. Build each strategy's daily return series on REAL BTC prices.
     - Condor: 0DTE short iron condor priced with REAL Deribit DVOL implied vol,
       90% credit capture, real Delta fees. (Same engine as backtest_iron_condor_realvol.)
     - Trend: long/short BTC via EMA(20/50) crossover on daily bars.
  2. Risk-normalise both to the SAME annualised volatility (apples to apples).
  3. Measure correlation.
  4. Combine 50/50 (equal risk). Show CAGR, max drawdown, and MAR (=CAGR/maxDD)
     for each alone vs combined, and the combined LEVERED to the same risk as
     each alone (that is where the extra return shows up).

Run:  py -3.14 scripts/backtest_portfolio.py
"""
from __future__ import annotations

import json
import math
import os
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
os.chdir(REPO_ROOT)
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import pandas as pd  # noqa: E402
from backtest_iron_condor_btc import (  # noqa: E402
    SHORT_PCT, LONG_PCT, DTE_HOURS, START, END, bs_price, fetch_daily,
)
from trading_agent.brokers.delta.fees import leg_fee_usd  # noqa: E402

DERIBIT = "https://www.deribit.com/api/v2/public/"
CAPTURE = 0.90
TARGET_VOL = 0.15          # annualised vol we normalise everything to
TRADING_DAYS = 365         # crypto trades daily


# ---------- real implied vol (Deribit DVOL) ----------
def _ms(d): return int(datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()*1000)

def fetch_dvol(start, end):
    rows = []
    for yr in range(datetime.strptime(start,"%Y-%m-%d").year, datetime.strptime(end,"%Y-%m-%d").year+1):
        a, b = _ms(f"{yr}-01-01"), _ms(f"{yr+1}-01-01")
        url = f"{DERIBIT}get_volatility_index_data?currency=BTC&start_timestamp={a}&end_timestamp={b}&resolution=1D"
        try:
            r = json.loads(urllib.request.urlopen(url, timeout=30).read())
            for ts,o,h,l,c in r.get("result",{}).get("data",[]):
                rows.append((datetime.fromtimestamp(ts/1000,timezone.utc).date(), float(c)))
        except Exception as e:
            print("  dvol warn", yr, e)
    s = pd.Series({d:v for d,v in rows}).sort_index()
    s.index = pd.to_datetime(s.index)
    return s


# ---------- strategy return streams ----------
def condor_returns(df):
    """Daily condor net P&L as a return on a unit risk budget."""
    T = DTE_HOURS/24/365
    out = {}
    for i in range(len(df)-1):
        row, nxt = df.iloc[i], df.iloc[i+1]
        S, Se, sig = float(row["close"]), float(nxt["close"]), float(row["iv_real"])
        if sig <= 0: continue
        Ksc,Ksp,Klc,Klp = S*(1+SHORT_PCT),S*(1-SHORT_PCT),S*(1+LONG_PCT),S*(1-LONG_PCT)
        psc,psp = bs_price(S,Ksc,T,sig,"C"), bs_price(S,Ksp,T,sig,"P")
        plc,plp = bs_price(S,Klc,T,sig,"C"), bs_price(S,Klp,T,sig,"P")
        credit = (psc+psp)-(plc+plp)
        if credit <= 0: continue
        ci=lambda k:max(0.0,Se-k); pi=lambda k:max(0.0,k-Se)
        payoff = -(ci(Ksc)+pi(Ksp))+(ci(Klc)+pi(Klp))
        wing = max(Klc-Ksc, Ksp-Klp)
        fee = (leg_fee_usd(S,psc,1,1.0)+leg_fee_usd(S,psp,1,1.0)
               +leg_fee_usd(S,plc,1,1.0)+leg_fee_usd(S,plp,1,1.0))
        for k,intr in ((Ksc,ci(Ksc)),(Ksp,pi(Ksp)),(Klc,ci(Klc)),(Klp,pi(Klp))):
            if intr>0: fee += leg_fee_usd(Se,intr,1,1.0)
        net = credit*CAPTURE + payoff - fee          # per 1 BTC
        risk = max(wing - credit*CAPTURE + fee, 1e-9) # per 1 BTC worst case
        out[nxt.name] = net / risk                    # return on risk budget
    return pd.Series(out)


def trend_returns(df, fast=20, slow=50):
    """Long/short BTC via EMA crossover; daily return = prev signal * BTC daily return."""
    c = df["close"]
    ef, es = c.ewm(span=fast,adjust=False).mean(), c.ewm(span=slow,adjust=False).mean()
    signal = (ef > es).astype(int)*2 - 1            # +1 long, -1 short
    sig_prev = signal.shift(1).fillna(0)
    ret = c.pct_change().fillna(0)
    return (sig_prev * ret).iloc[1:]                # daily strategy return


# ---------- stats ----------
def stats(r):
    eq = (1+r).cumprod()
    peak = eq.cummax()
    dd = (eq/peak - 1.0)
    maxdd = -dd.min()
    yrs = len(r)/TRADING_DAYS
    cagr = eq.iloc[-1]**(1/yrs) - 1 if eq.iloc[-1] > 0 else -1
    vol = r.std()*math.sqrt(TRADING_DAYS)
    sharpe = (r.mean()/r.std()*math.sqrt(TRADING_DAYS)) if r.std()>0 else 0
    mar = (cagr/maxdd) if maxdd>0 else float("inf")
    return {"cagr":cagr,"vol":vol,"maxdd":maxdd,"sharpe":sharpe,"mar":mar}


def normalise(r, target=TARGET_VOL):
    v = r.std()*math.sqrt(TRADING_DAYS)
    return r * (target/v) if v>0 else r


def show(name, s):
    print(f"  {name:<26} CAGR {s['cagr']*100:>+6.1f}%  vol {s['vol']*100:>5.1f}%  "
          f"maxDD {s['maxdd']*100:>5.1f}%  Sharpe {s['sharpe']:>4.2f}  MAR {s['mar']:>4.2f}")


def main():
    print("="*86)
    print("PORTFOLIO: Iron Condor (short vol) + Trend (long vol) — real BTC + real DVOL")
    print("="*86)
    print("Fetching BTC + DVOL...", end=" ", flush=True)
    px = fetch_daily("BTC-USD", START, END)
    dvol = fetch_dvol(START, END)
    px["iv_real"] = dvol.reindex(px.index, method="ffill")/100.0
    px = px.dropna(subset=["iv_real"])
    print(f"{len(px)} days")

    rc = condor_returns(px)
    rt = trend_returns(px)
    idx = rc.index.intersection(rt.index)
    rc, rt = rc.reindex(idx).fillna(0), rt.reindex(idx).fillna(0)

    # risk-normalise to equal vol
    rcn, rtn = normalise(rc), normalise(rt)
    corr = rcn.corr(rtn)

    # equal-risk 50/50 combine
    rp = 0.5*rcn + 0.5*rtn
    # lever the combined book back up to TARGET_VOL (same risk as each alone)
    rp_lev = normalise(rp)

    print(f"\nDays: {len(idx)} (~{len(idx)/TRADING_DAYS:.1f}y)   "
          f"CORRELATION(condor, trend) = {corr:+.2f}\n")
    print("Each strategy, risk-normalised to 15% annual vol:")
    show("Iron Condor (short vol)", stats(rcn))
    show("Trend EMA20/50 (long vol)", stats(rtn))
    print("\nCombined:")
    show("50/50 equal-risk", stats(rp))
    show("50/50 levered to 15% vol", stats(rp_lev))

    print("\n" + "="*86)
    print("WHAT TO LOOK FOR")
    print("="*86)
    print(f"  - Correlation {corr:+.2f}: closer to 0 or negative = better diversification.")
    sc, st_, sp = stats(rcn), stats(rtn), stats(rp_lev)
    best_solo_mar = max(sc["mar"], st_["mar"])
    print(f"  - Best single-strategy MAR (return/drawdown): {best_solo_mar:.2f}")
    print(f"  - Combined (levered) MAR: {sp['mar']:.2f}  "
          f"({'BETTER' if sp['mar']>best_solo_mar else 'not better'} — higher = more return per unit of pain)")
    print(f"  - Combined levered CAGR {sp['cagr']*100:+.1f}% at maxDD {sp['maxdd']*100:.1f}% "
          f"vs condor-alone CAGR {sc['cagr']*100:+.1f}% at maxDD {sc['maxdd']*100:.1f}%.")
    print("  NOTE: returns are risk-normalised (theoretical), to compare SHAPES fairly.")
    print("  Real deployment still needs the paper trade to confirm the condor's live edge,")
    print("  and the trend leg to be validated/executed on a real venue.")


if __name__ == "__main__":
    main()

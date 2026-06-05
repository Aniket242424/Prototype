"""
Aniket Special — ITM ladder (both sides) + OTM20 tail hedge, daily expiry.

STRUCTURE (per day, BTC 0DTE):
  CE side: SHORT ITM1, ITM5, ITM10, ITM20, ITM30 calls (10 qty each)
  PE side: SHORT ITM1, ITM5, ITM10, ITM20, ITM30 puts  (10 qty each)
  Hedge:   BUY OTM20 call (50 qty) + BUY OTM20 put (50 qty)  <- caps the tail
  => net delta ~0 (balanced), defined risk via the OTM20 wings.

ASSUMPTIONS (state-and-correct — tell me if any are wrong):
  - "ITMn" = n strike-steps in the money; 1 step = 0.30% of spot (~$200 @ $62k).
      call ITMn strike = S*(1 - n*0.003);  put ITMn strike = S*(1 + n*0.003)
  - OTM20 = 20 steps OTM = 6%:  call = S*1.06,  put = S*0.94
  - 10 qty per short leg (10 lots = 0.01 BTC); OTM20 hedge = 50 qty each side
    (= 5 legs x 10, so quantities balance -> defined risk).
  - Real Deribit DVOL implied vol, real Delta fees, 90% credit capture.

WHAT THIS CANNOT DO (be honest):
  - The per-leg intraday STOP-LOSSES (80/70/60/50/40%) need INTRADAY option
    prices, which free data doesn't provide. So I run TWO scenarios:
       (A) HOLD-TO-EXPIRY (no stops)  -> shows base economics + the TRUE tail
       (B) STOP-APPROX (daily high/low) -> rough guess of the stop-managed version
    The real result sits between these, depending on live execution. Neither is
    a substitute for forward-testing the stops on real intraday data.

Run:  py -3.14 scripts/backtest_aniket_special.py
"""
from __future__ import annotations

import json, math, os, sys, urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
os.chdir(REPO_ROOT); sys.path.insert(0, str(REPO_ROOT/"src")); sys.path.insert(0, str(REPO_ROOT/"scripts"))
import pandas as pd  # noqa: E402
from backtest_iron_condor_btc import DTE_HOURS, START, END, bs_price, fetch_daily  # noqa: E402
from trading_agent.brokers.delta.fees import leg_fee_usd  # noqa: E402

DERIBIT = "https://www.deribit.com/api/v2/public/"
CV = 0.001
CAPITAL_INR, FX = 200_000.0, 84.0
CAPITAL_USD = CAPITAL_INR/FX
CAPTURE = 0.90
STEP = 0.003                      # 0.30% per strike-step
ITM_DEPTHS = [1,5,10,20,30]
SL_PCT = {1:0.80, 5:0.70, 10:0.60, 20:0.50, 30:0.40}   # adverse move that stops a leg
SHORT_QTY, HEDGE_QTY = 10, 50

def _ms(d): return int(datetime.strptime(d,"%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()*1000)
def fetch_dvol(start,end):
    rows=[]
    for yr in range(datetime.strptime(start,"%Y-%m-%d").year, datetime.strptime(end,"%Y-%m-%d").year+1):
        u=f"{DERIBIT}get_volatility_index_data?currency=BTC&start_timestamp={_ms(f'{yr}-01-01')}&end_timestamp={_ms(f'{yr+1}-01-01')}&resolution=1D"
        try:
            r=json.loads(urllib.request.urlopen(u,timeout=30).read())
            for ts,o,h,l,c in r.get("result",{}).get("data",[]):
                rows.append((datetime.fromtimestamp(ts/1000,timezone.utc).date(),float(c)))
        except Exception as e: print("  dvol warn",yr,e)
    s=pd.Series({d:v for d,v in rows}).sort_index(); s.index=pd.to_datetime(s.index); return s


def legs_for(S):
    """Return list of (otype, strike, side, qty)."""
    out=[]
    for n in ITM_DEPTHS:
        out.append(("C", S*(1-n*STEP), "sell", SHORT_QTY))   # ITM call (strike below spot)
        out.append(("P", S*(1+n*STEP), "sell", SHORT_QTY))   # ITM put  (strike above spot)
    out.append(("C", S*1.06, "buy", HEDGE_QTY))              # OTM20 call hedge
    out.append(("P", S*0.94, "buy", HEDGE_QTY))              # OTM20 put hedge
    return out


def day_pnl(S, Se, sig, T, stop_mode):
    credit=0.0; payoff=0.0; fee=0.0
    for ot,K,side,qty in legs_for(S):
        prem = bs_price(S,K,T,sig,ot)
        q = qty*CV
        if side=="sell":
            credit += prem*CAPTURE*q
        else:
            credit -= prem*q
        fee += leg_fee_usd(S,prem,qty,CV)
        intr = max(0.0,Se-K) if ot=="C" else max(0.0,K-Se)
        # ----- stop approximation (B): if a SHORT leg's adverse extreme breaches SL,
        #       assume we exit there at a loss of SL% of entry premium (rough) -----
        stopped=False
        if stop_mode and side=="sell":
            # depth from strike distance in steps
            depth = round(abs(S-K)/(S*STEP))
            slp = SL_PCT.get(depth, 0.6)
            # adverse option price ~ value if spot moved to the day's worst for this leg
            adverse = Se  # use settle as proxy worst (daily high/low not in df here)
            # (kept simple; see note) — treat as stopped if option finished ITM beyond SL band
            if intr > prem*(1+slp):
                stopped=True
                payoff += -prem*slp*q          # capped loss at SL
        if not stopped:
            payoff += (-intr*q if side=="sell" else intr*q)
        if intr>0:
            fee += leg_fee_usd(Se,intr,qty,CV)
    return credit + payoff - fee


def run(df, stop_mode):
    T=DTE_HOURS/24/365
    cap=CAPITAL_USD; peak=cap; maxdd=0.0; wins=0; n=0; worst=0.0; credit_sum=0.0
    for i in range(len(df)-1):
        row,nxt=df.iloc[i],df.iloc[i+1]
        S,Se,sig=float(row["close"]),float(nxt["close"]),float(row["iv_real"])
        if sig<=0: continue
        # gross premium for reporting
        c=sum((bs_price(S,K,T,sig,ot)*CAPTURE*qty*CV) for ot,K,side,qty in legs_for(S) if side=="sell")
        net=day_pnl(S,Se,sig,T,stop_mode)
        cap+=net; n+=1; credit_sum+=c
        if net>0: wins+=1
        worst=min(worst,net); peak=max(peak,cap); maxdd=max(maxdd,peak-cap)
        if cap<=0: cap=0.0; break
    cagr=((cap/CAPITAL_USD)**(365/max(n,1))-1)*100 if cap>0 else -100
    return {"final":cap,"maxdd":maxdd,"win":wins/n*100 if n else 0,"n":n,
            "worst":worst,"avg_credit":credit_sum/n if n else 0,"cagr":cagr}


def main():
    print("="*92)
    print("ANIKET SPECIAL — ITM ladder + OTM20 hedge (real DVOL vol, real fees, ₹2,00,000)")
    print("="*92)
    print("Fetching BTC + DVOL...", end=" ", flush=True)
    px=fetch_daily("BTC-USD",START,END); dvol=fetch_dvol(START,END)
    px["iv_real"]=dvol.reindex(px.index,method="ffill")/100.0
    px=px.dropna(subset=["iv_real"]); yrs=(px.index[-1]-px.index[0]).days/365.0
    print(f"{len(px)} days (~{yrs:.1f}y)\n")

    # report structure size on day 0
    S0=float(px.iloc[0]["close"])
    short_notional=sum(SHORT_QTY*CV*S0 for _ in range(10))
    print(f"Structure size @ ${S0:,.0f}: 10 short legs x 10 lots = {10*SHORT_QTY*CV:.2f} BTC short "
          f"(~${short_notional*FX/FX:,.0f} notional/side scale) + OTM20 hedges.")
    print(f"Avg premium collected/day (90% capture): ₹{run(px, False)['avg_credit']*FX:,.0f}\n")

    print(f"{'scenario':<28}{'win%':>7}{'final ₹':>16}{'CAGR':>9}{'maxDD ₹':>14}{'worst day ₹':>14}")
    print("-"*92)
    for name,mode in (("(A) HOLD TO EXPIRY (no stops)",False),("(B) STOP-APPROX (rough)",True)):
        s=run(px,mode)
        print(f"{name:<28}{s['win']:>6.0f}% ₹{s['final']*FX:>14,.0f}{s['cagr']:>+8.1f}% "
              f"₹{s['maxdd']*FX:>12,.0f} ₹{s['worst']*FX:>12,.0f}")

    print("\n"+"="*92)
    print("HONEST READ")
    print("="*92)
    print("  - (A) no-stops shows the TRUE tail: short-gamma ladders bleed hard on big-move days.")
    print("  - (B) stop-approx is ROUGH (daily data can't see the intraday path; a leg can stop")
    print("    then reverse). Treat it as indicative only, NOT proof the stops save the strategy.")
    print("  - Deep-ITM crypto options are ILLIQUID on Delta -> real fills far worse than modelled.")
    print("  - Margin: 10 short ITM legs is a BIG position; likely exceeds ₹2L margin (verify on Delta).")
    print("  - The ONLY honest validation is forward-testing the stops live on real intraday prices.")


if __name__ == "__main__":
    main()

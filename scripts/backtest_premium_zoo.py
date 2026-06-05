"""
Premium-collection strategy ZOO for BTC daily (0DTE) — which collects more, at what risk?

Compares credit structures head-to-head on the SAME days, with REAL Deribit DVOL
implied vol + REAL Delta fees, 10 lots (0.01 BTC), starting ₹2,00,000:

  IRON CONDOR        sell C+1.5% / P-1.5%, buy C+4% / P-4%   (current; balanced)
  NARROW CONDOR      sell ±1.0%, buy ±3%                      (more premium, tighter)
  TIGHT CONDOR       sell ±0.75%, buy ±2%                     (even more premium)
  BULL PUT SPREAD    sell P-1.5%, buy P-4%                    (credit spread, bullish)
  BEAR CALL SPREAD   sell C+1.5%, buy C+4%                    (credit spread, bearish)
  JADE LIZARD        sell P-1.5% + sell C+1.5%, buy C+4%      (no upside risk, naked put)
  SHORT STRANGLE     sell C+1.5% + P-1.5%, NO wings           (max premium, UNLIMITED risk)

The point: see how much MORE premium each collects vs the condor, and what it
costs in win-rate, drawdown, and tail risk. More premium is never free.

Run:  py -3.14 scripts/backtest_premium_zoo.py
"""
from __future__ import annotations

import json
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
from backtest_iron_condor_btc import DTE_HOURS, START, END, bs_price, fetch_daily  # noqa: E402
from trading_agent.brokers.delta.fees import leg_fee_usd  # noqa: E402

DERIBIT = "https://www.deribit.com/api/v2/public/"
LOTS, CV = 10, 0.001
QTY = LOTS * CV
CAPITAL_INR, FX = 200_000.0, 84.0
CAPITAL_USD = CAPITAL_INR / FX
CAPTURE = 0.90

# leg = (option_type 'C'/'P', strike_multiple, side 'sell'/'buy')
STRUCTURES = {
    "Iron Condor (±1.5/4)": [("C",1.015,"sell"),("C",1.04,"buy"),("P",0.985,"sell"),("P",0.96,"buy")],
    "Narrow Condor (±1/3)": [("C",1.01,"sell"),("C",1.03,"buy"),("P",0.99,"sell"),("P",0.97,"buy")],
    "Tight Condor (±.75/2)": [("C",1.0075,"sell"),("C",1.02,"buy"),("P",0.9925,"sell"),("P",0.98,"buy")],
    "Bull Put Spread":       [("P",0.985,"sell"),("P",0.96,"buy")],
    "Bear Call Spread":      [("C",1.015,"sell"),("C",1.04,"buy")],
    "Jade Lizard":           [("P",0.985,"sell"),("C",1.015,"sell"),("C",1.04,"buy")],
    "Short Strangle (naked)":[("C",1.015,"sell"),("P",0.985,"sell")],
}


def _ms(d): return int(datetime.strptime(d,"%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()*1000)

def fetch_dvol(start,end):
    rows=[]
    for yr in range(datetime.strptime(start,"%Y-%m-%d").year, datetime.strptime(end,"%Y-%m-%d").year+1):
        url=f"{DERIBIT}get_volatility_index_data?currency=BTC&start_timestamp={_ms(f'{yr}-01-01')}&end_timestamp={_ms(f'{yr+1}-01-01')}&resolution=1D"
        try:
            r=json.loads(urllib.request.urlopen(url,timeout=30).read())
            for ts,o,h,l,c in r.get("result",{}).get("data",[]):
                rows.append((datetime.fromtimestamp(ts/1000,timezone.utc).date(),float(c)))
        except Exception as e: print("  dvol warn",yr,e)
    s=pd.Series({d:v for d,v in rows}).sort_index(); s.index=pd.to_datetime(s.index); return s


def simulate(df, legs):
    T=DTE_HOURS/24/365
    cap=CAPITAL_USD; peak=cap; maxdd=0.0
    wins=0; n=0; credit_sum=0.0; worst=0.0
    for i in range(len(df)-1):
        row,nxt=df.iloc[i],df.iloc[i+1]
        S,Se,sig=float(row["close"]),float(nxt["close"]),float(row["iv_real"])
        if sig<=0: continue
        # price legs
        priced=[]
        credit=0.0
        for ot,mult,side in legs:
            K=S*mult
            prem=bs_price(S,K,T,sig,ot)
            priced.append((ot,K,side,prem))
            credit += prem if side=="sell" else -prem
        if credit<=0: continue
        # payoff at expiry
        payoff=0.0; fee=0.0
        for ot,K,side,prem in priced:
            intr = max(0.0,Se-K) if ot=="C" else max(0.0,K-Se)
            payoff += (-intr if side=="sell" else intr)
            fee += leg_fee_usd(S,prem,LOTS,CV)
            if intr>0: fee += leg_fee_usd(Se,intr,LOTS,CV)
        net = credit*CAPTURE*QTY + payoff*QTY - fee
        cap += net; n+=1; credit_sum += credit*CAPTURE*QTY
        if net>0: wins+=1
        worst=min(worst,net)
        peak=max(peak,cap); maxdd=max(maxdd,peak-cap)
        if cap<=0: cap=0.0; break
    return {"final":cap,"maxdd":maxdd,"win":wins/n*100 if n else 0,"n":n,
            "avg_credit":credit_sum/n if n else 0,"worst":worst}


def main():
    print("="*100)
    print("PREMIUM ZOO — BTC daily, real DVOL vol, real fees, 10 lots, start ₹2,00,000")
    print("="*100)
    print("Fetching BTC + DVOL...", end=" ", flush=True)
    px=fetch_daily("BTC-USD",START,END)
    dvol=fetch_dvol(START,END)
    px["iv_real"]=dvol.reindex(px.index,method="ffill")/100.0
    px=px.dropna(subset=["iv_real"])
    yrs=(px.index[-1]-px.index[0]).days/365.0
    print(f"{len(px)} days (~{yrs:.1f}y)\n")

    base=simulate(px, STRUCTURES["Iron Condor (±1.5/4)"])
    base_credit=base["avg_credit"]

    print(f"{'structure':<24}{'avg credit':>12}{'vs IC':>7}{'win%':>7}{'final ₹':>14}{'CAGR':>8}{'maxDD ₹':>12}{'worst day':>11}")
    print("-"*100)
    for name,legs in STRUCTURES.items():
        s=simulate(px,legs)
        cagr=((s['final']/CAPITAL_USD)**(1/yrs)-1)*100 if s['final']>0 else -100
        vs = (s['avg_credit']/base_credit-1)*100 if base_credit else 0
        print(f"{name:<24}₹{s['avg_credit']*FX:>9,.0f}{vs:>+6.0f}%{s['win']:>6.0f}% "
              f"₹{s['final']*FX:>12,.0f}{cagr:>+7.1f}% ₹{s['maxdd']*FX:>10,.0f} ₹{s['worst']*FX:>9,.0f}")

    print("\n" + "="*100)
    print("HONEST READ — more premium is never free")
    print("="*100)
    print("  - 'avg credit' = ₹ collected per trade (10 lots). 'vs IC' = how much MORE than the condor.")
    print("  - Tighter strikes / fewer wings collect MORE premium but lose more often or bigger.")
    print("  - SHORT STRANGLE collects the most premium but 'worst day' shows the uncapped tail —")
    print("    one bad gap can erase weeks. That is the unlimited-risk trap.")
    print("  - 'final ₹' / CAGR / maxDD tell you the NET result after the bigger losses are paid.")
    print("  - All at synthetic-from-real-vol pricing; live bid/ask (paper trade) still the final test.")


if __name__ == "__main__":
    main()

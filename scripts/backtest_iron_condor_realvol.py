"""
Iron Condor with REAL implied vol — uses Deribit's DVOL index instead of a guess.

The synthetic backtest's biggest assumption was IV = realised_vol * 1.15.
This version replaces that with Deribit's DVOL — the REAL BTC implied-volatility
index the market was actually pricing each day (free, public Deribit API).

This is as close to a real backtest as free data allows:
  - REAL implied vol (DVOL)         <- the big improvement
  - REAL BTC prices (yfinance)
  - REAL Delta fees
  - Black-Scholes still used to turn IV into option prices (no real bid/ask,
    which needs paid data like Tardis.dev)

Honest caveat: DVOL is a 30-DAY implied-vol index, but our options are 1-day.
Short-dated IV differs from 30-day (term structure). So this is "real vol LEVEL,
modelled price" — a big step up from a pure guess, not the final word. The live
paper trade (real Delta bid/ask) remains the ground truth.

Run:  py -3.14 scripts/backtest_iron_condor_realvol.py
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
from backtest_iron_condor_btc import (  # noqa: E402
    SHORT_PCT, LONG_PCT, DTE_HOURS, START, END, bs_price, fetch_daily,
)
from trading_agent.brokers.delta.fees import leg_fee_usd  # noqa: E402

TICKER = "BTC-USD"
LOTS = 10
CV = 0.001
QTY_BTC = LOTS * CV
CAPITAL_INR = 200_000.0
FX = 84.0
CAPITAL_USD = CAPITAL_INR / FX
CAPTURES = [1.00, 0.90, 0.85]
DERIBIT = "https://www.deribit.com/api/v2/public/"


def _ms(dt: str) -> int:
    return int(datetime.strptime(dt, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)


def fetch_dvol(start: str, end: str) -> pd.Series:
    """Daily DVOL (BTC implied-vol index, %) from Deribit, chunked by year."""
    rows: list[tuple] = []
    s = datetime.strptime(start, "%Y-%m-%d").year
    e = datetime.strptime(end, "%Y-%m-%d").year
    for yr in range(s, e + 1):
        a = _ms(f"{yr}-01-01")
        b = _ms(f"{min(yr+1, e+1)}-01-01")
        url = (f"{DERIBIT}get_volatility_index_data?currency=BTC"
               f"&start_timestamp={a}&end_timestamp={b}&resolution=1D")
        try:
            r = json.loads(urllib.request.urlopen(url, timeout=30).read())
            for pt in r.get("result", {}).get("data", []):
                ts, o, h, l, c = pt
                rows.append((datetime.fromtimestamp(ts/1000, timezone.utc).date(), float(c)))
        except Exception as ex:
            print(f"  DVOL {yr} fetch warn: {ex}")
    if not rows:
        raise RuntimeError("No DVOL data returned from Deribit")
    s2 = pd.Series({d: v for d, v in rows}).sort_index()
    s2.index = pd.to_datetime(s2.index)
    return s2


def run(df: pd.DataFrame) -> list[dict]:
    T = DTE_HOURS / 24.0 / 365.0
    trades = []
    for i in range(len(df) - 1):
        row = df.iloc[i]; nxt = df.iloc[i + 1]
        S = float(row["close"]); Se = float(nxt["close"])
        sigma = float(row["iv_real"])              # REAL DVOL (decimal)
        if sigma <= 0:
            continue
        Ksc, Ksp = S*(1+SHORT_PCT), S*(1-SHORT_PCT)
        Klc, Klp = S*(1+LONG_PCT), S*(1-LONG_PCT)
        psc = bs_price(S,Ksc,T,sigma,"C"); psp = bs_price(S,Ksp,T,sigma,"P")
        plc = bs_price(S,Klc,T,sigma,"C"); plp = bs_price(S,Klp,T,sigma,"P")
        credit = (psc+psp)-(plc+plp)
        if credit <= 0:
            continue
        ci = lambda k: max(0.0, Se-k); pi = lambda k: max(0.0, k-Se)
        payoff = -(ci(Ksc)+pi(Ksp)) + (ci(Klc)+pi(Klp))
        fee = (leg_fee_usd(S,psc,LOTS,CV)+leg_fee_usd(S,psp,LOTS,CV)
               +leg_fee_usd(S,plc,LOTS,CV)+leg_fee_usd(S,plp,LOTS,CV))
        for k,intr in ((Ksc,ci(Ksc)),(Ksp,pi(Ksp)),(Klc,ci(Klc)),(Klp,pi(Klp))):
            if intr>0:
                fee += leg_fee_usd(Se,intr,LOTS,CV)
        trades.append({"credit":credit,"payoff":payoff,"fee":fee,
                       "inside": Ksp < Se < Ksc})
    return trades


def equity(trades, capture):
    cap = CAPITAL_USD; peak=cap; maxdd=0.0; wins=0; net_total=0.0
    for t in trades:
        net = t["credit"]*capture*QTY_BTC + t["payoff"]*QTY_BTC - t["fee"]
        cap += net; net_total += net
        if net>0: wins+=1
        peak=max(peak,cap); maxdd=max(maxdd,peak-cap)
    n=len(trades)
    return {"capture":capture,"final":cap,"net":net_total,"maxdd":maxdd,
            "win":wins/n*100 if n else 0,"n":n}


def main():
    print("="*76)
    print("IRON CONDOR — REAL implied vol (Deribit DVOL), 10 lots, real fees")
    print(f"Start ₹{CAPITAL_INR:,.0f}  ·  {START} -> {END}")
    print("="*76)
    print("Fetching BTC prices...", end=" ", flush=True)
    px = fetch_daily(TICKER, START, END)
    print(f"{len(px)} days")
    print("Fetching Deribit DVOL (real implied vol)...", end=" ", flush=True)
    dvol = fetch_dvol(START, END)
    print(f"{len(dvol)} days, {dvol.index.min().date()} -> {dvol.index.max().date()}")

    df = px.copy()
    df["iv_real"] = (dvol.reindex(df.index, method="ffill") / 100.0)
    df = df.dropna(subset=["iv_real"])
    yrs = (df.index[-1]-df.index[0]).days/365.0
    print(f"Matched {len(df)} days (~{yrs:.1f}y).  "
          f"Real IV range: {df['iv_real'].min()*100:.0f}%-{df['iv_real'].max()*100:.0f}% "
          f"(mean {df['iv_real'].mean()*100:.0f}%)")

    trades = run(df)
    print(f"\nTrades: {len(trades)}\n")
    print(f"{'capture':>9} {'win%':>6} {'final ₹':>15} {'CAGR':>9} {'maxDD ₹':>12} {'verdict':>9}")
    print("-"*68)
    for c in CAPTURES:
        s = equity(trades, c)
        cagr = ((s['final']/CAPITAL_USD)**(1/yrs)-1)*100 if s['final']>0 else -100
        v = "GROWS" if s['final']>CAPITAL_USD else "LOSES"
        print(f"{c*100:>8.0f}% {s['win']:>6.1f} ₹{s['final']*FX:>13,.0f} "
              f"{cagr:>+7.1f}% ₹{s['maxdd']*FX:>10,.0f} {v:>9}")

    print("\n" + "="*76)
    print("vs the earlier GUESS (IV = realised_vol × 1.15):")
    print("  If real-IV numbers are LOWER -> my guess was too optimistic (less premium).")
    print("  If HIGHER -> the strategy is better than I showed.")
    print("  Either way: this uses the REAL vol the market priced. Paper trade still")
    print("  needed for real bid/ask (the last unknown).")


if __name__ == "__main__":
    main()

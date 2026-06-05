"""
Iron Condor COMPOUNDING — what ₹2L can realistically become, and the ruin cost.

The fixed-10-lot run grows ₹2L only +5-12%/yr because 10 lots risks 0.7% of
capital. Real compounding = size each condor to risk a FIXED FRACTION of CURRENT
capital, so lots (and P&L) grow geometrically as capital grows.

This shows the honest tradeoff: bigger risk-per-trade => faster growth BUT
bigger drawdowns and real ruin risk. Uses 90% credit capture (realistic) and
the same real Delta fee model.

Run:  py -3.14 scripts/backtest_iron_condor_compound.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
os.chdir(REPO_ROOT)
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import pandas as pd  # noqa: E402
from backtest_iron_condor_btc import (  # noqa: E402
    SHORT_PCT, LONG_PCT, DTE_HOURS, VOL_LOOKBACK, VOL_PREMIUM,
    START, END, bs_price, fetch_daily, compute_realized_vol,
)
from trading_agent.brokers.delta.fees import leg_fee_usd  # noqa: E402

TICKER = "BTC-USD"
CV = 0.001
CAPITAL_INR = 200_000.0
FX = 84.0
CAPITAL_USD = CAPITAL_INR / FX
TARGET_INR = 5_000_000.0           # ₹50L goal
CAPTURE = 0.90                     # realistic credit capture
RISK_FRACTIONS = [0.01, 0.02, 0.05, 0.10, 0.20]


def build_trades(df: pd.DataFrame) -> list[dict]:
    df = df.copy()
    df["rv"] = compute_realized_vol(df, VOL_LOOKBACK)
    df["iv"] = df["rv"] * (1.0 + VOL_PREMIUM)
    df = df.dropna(subset=["iv"])
    T = DTE_HOURS / 24.0 / 365.0
    out = []
    for i in range(len(df) - 1):
        row = df.iloc[i]; nxt = df.iloc[i + 1]
        S = float(row["close"]); sigma = float(row["iv"]); Se = float(nxt["close"])
        Ksc, Ksp = S*(1+SHORT_PCT), S*(1-SHORT_PCT)
        Klc, Klp = S*(1+LONG_PCT), S*(1-LONG_PCT)
        psc = bs_price(S,Ksc,T,sigma,"C"); psp = bs_price(S,Ksp,T,sigma,"P")
        plc = bs_price(S,Klc,T,sigma,"C"); plp = bs_price(S,Klp,T,sigma,"P")
        credit = (psc+psp)-(plc+plp)
        if credit <= 0:
            continue
        ci = lambda k: max(0.0, Se-k); pi = lambda k: max(0.0, k-Se)
        payoff = -(ci(Ksc)+pi(Ksp)) + (ci(Klc)+pi(Klp))   # per BTC
        wing = max(Klc-Ksc, Ksp-Klp)
        # fees PER BTC (linear in qty): entry 4 legs + settle ITM legs
        fee_btc = (
            leg_fee_usd(S,psc,1,1.0) + leg_fee_usd(S,psp,1,1.0)
            + leg_fee_usd(S,plc,1,1.0) + leg_fee_usd(S,plp,1,1.0)
        )
        for k,intr in ((Ksc,ci(Ksc)),(Ksp,pi(Ksp)),(Klc,ci(Klc)),(Klp,pi(Klp))):
            if intr>0:
                fee_btc += leg_fee_usd(Se,intr,1,1.0)
        out.append({"credit": credit, "payoff": payoff, "wing": wing, "fee_btc": fee_btc})
    return out


def simulate(trades, risk_frac, capture):
    """Size each condor so worst-case loss = risk_frac * current capital."""
    cap = CAPITAL_USD
    peak = cap; max_dd_pct = 0.0
    hit_target = None
    blown = False
    for idx, t in enumerate(trades):
        credit = t["credit"] * capture
        # worst-case loss magnitude per BTC (incl fee): wing - credit + fee
        risk_btc = max(t["wing"] - credit + t["fee_btc"], 1e-9)
        qty = (risk_frac * cap) / risk_btc          # BTC sizing for this trade
        if qty <= 0:
            continue
        net_btc = credit + t["payoff"] - t["fee_btc"]
        cap += net_btc * qty
        if cap <= 0:
            blown = True; cap = 0.0; break
        peak = max(peak, cap)
        dd = (peak - cap) / peak
        max_dd_pct = max(max_dd_pct, dd)
        if hit_target is None and cap * FX >= TARGET_INR:
            hit_target = idx
    return {
        "risk_frac": risk_frac, "final_usd": cap, "final_inr": cap*FX,
        "max_dd_pct": max_dd_pct*100, "blown": blown,
        "hit_target_trade": hit_target, "n": len(trades),
    }


def main():
    print("="*78)
    print("IRON CONDOR COMPOUNDING — ₹2L sized to risk X% of capital per trade")
    print(f"Capture {CAPTURE*100:.0f}% (realistic) · target ₹{TARGET_INR:,.0f} · real Delta fees")
    print("="*78)
    print("Fetching BTC daily...", end=" ", flush=True)
    df = fetch_daily(TICKER, START, END)
    trades = build_trades(df)
    yrs = (df.index[-1]-df.index[0]).days/365.0
    print(f"{len(trades)} trades over {yrs:.1f}y")
    print()
    print(f"{'risk/trade':>11} {'final ₹':>16} {'CAGR':>9} {'max DD':>9} {'hit ₹50L?':>22}")
    print("-"*72)
    for f in RISK_FRACTIONS:
        s = simulate(trades, f, CAPTURE)
        cagr = ((s['final_usd']/CAPITAL_USD)**(1/yrs)-1)*100 if s['final_usd']>0 else -100
        if s["blown"]:
            hit = "BLEW UP (ruin)"
        elif s["hit_target_trade"] is not None:
            yrs_to = s["hit_target_trade"]/len(trades)*yrs
            hit = f"YES in {yrs_to:.1f}y"
        else:
            hit = "no"
        print(f"{f*100:>9.0f}% ₹{s['final_inr']:>14,.0f} {cagr:>+7.1f}% {s['max_dd_pct']:>7.0f}% {hit:>22}")
    print()
    print("="*78)
    print("HONEST READ")
    print("="*78)
    print("  - Faster growth ALWAYS comes with bigger drawdowns. There is no setting")
    print("    that gives 25x quickly AND keeps drawdowns survivable.")
    print("  - A 50-70% drawdown is psychologically + practically near-impossible to")
    print("    hold through; most people quit (or get liquidated) at the bottom.")
    print("  - These numbers assume the 90% capture edge HOLDS for 5 years. The paper")
    print("    trade has NOT yet proven the real edge. Compounding an unproven edge")
    print("    aggressively is how accounts go to zero.")


if __name__ == "__main__":
    main()

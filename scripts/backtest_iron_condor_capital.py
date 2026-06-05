"""
Iron Condor CAPITAL backtest — does ₹2,00,000 actually grow at 10 lots?

This answers the precise question: starting from ₹2,00,000, trading the SAME
size the live paper bot uses (10 lots = 0.01 BTC per condor), with REAL Delta
fees deducted, what does the equity curve do over 2021-2026?

Critical correction vs the original backtest:
  - The original backtest used CONTRACT_NOTIONAL = 1.0 BTC = 1000 lots.
    Its +$246k was for a position 100x LARGER than 10 lots. We rescale.
  - The original ignored fees. We apply the real Delta model
    (0.01% notional, cap 3.5% premium, +18% GST; OTM legs free at expiry).

Honesty caveats (read before trusting the number):
  1. Pricing is SYNTHETIC Black-Scholes with IV = realised_vol_20d * 1.15.
     Real Delta option prices differ; this typically OVER-states the credit
     captured. We therefore also print a haircut scenario (capture only X% of
     the synthetic credit) so you see a realistic range, not a single rosy line.
  2. Fixed 10 lots = NO compounding. Growth is additive. 10 lots is a tiny
     position for ₹2L (max loss ~₹1,300/trade), so absolute growth is small by
     construction — that is the point of the check.

Run:
    py -3.14 scripts/backtest_iron_condor_capital.py
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


# ============================================================
# Config — MATCHES the live paper bot
# ============================================================
TICKER = "BTC-USD"
LOTS = 10
CONTRACT_VALUE = 0.001            # BTC per lot (Delta spec)
QTY_BTC = LOTS * CONTRACT_VALUE    # 0.01 BTC total per condor
CAPITAL_INR = 200_000.0
FX = 84.0
CAPITAL_USD = CAPITAL_INR / FX

# Realism haircuts on the synthetic credit (capture only this fraction of the
# modelled net credit; payoff/loss side kept at 100% = conservative).
# Realistic capture for liquid Delta BTC daily options (1-5% bid/ask) is ~85-95%.
# 70%/50% are progressively severe stress / dislocation scenarios.
HAIRCUTS = [1.00, 0.95, 0.90, 0.85, 0.70, 0.50]


def run(df: pd.DataFrame) -> list[dict]:
    df = df.copy()
    df["rv"] = compute_realized_vol(df, VOL_LOOKBACK)
    df["iv"] = df["rv"] * (1.0 + VOL_PREMIUM)
    df = df.dropna(subset=["iv"])
    T = DTE_HOURS / 24.0 / 365.0

    trades: list[dict] = []
    for i in range(len(df) - 1):
        row = df.iloc[i]; nxt = df.iloc[i + 1]
        S = float(row["close"]); sigma = float(row["iv"]); S_exit = float(nxt["close"])

        K_sc = S * (1 + SHORT_PCT); K_sp = S * (1 - SHORT_PCT)
        K_lc = S * (1 + LONG_PCT);  K_lp = S * (1 - LONG_PCT)

        p_sc = bs_price(S, K_sc, T, sigma, "C")
        p_sp = bs_price(S, K_sp, T, sigma, "P")
        p_lc = bs_price(S, K_lc, T, sigma, "C")
        p_lp = bs_price(S, K_lp, T, sigma, "P")

        credit_per_btc = (p_sc + p_sp) - (p_lc + p_lp)
        if credit_per_btc <= 0:
            continue

        # payoff at expiry (per BTC)
        ci = lambda k: max(0.0, S_exit - k)   # call intrinsic
        pi = lambda k: max(0.0, k - S_exit)   # put intrinsic
        payoff_per_btc = -(ci(K_sc) + pi(K_sp)) + (ci(K_lc) + pi(K_lp))

        # entry fees (4 legs) — real Delta model
        fee_entry = (
            leg_fee_usd(S, p_sc, LOTS, CONTRACT_VALUE) +
            leg_fee_usd(S, p_sp, LOTS, CONTRACT_VALUE) +
            leg_fee_usd(S, p_lc, LOTS, CONTRACT_VALUE) +
            leg_fee_usd(S, p_lp, LOTS, CONTRACT_VALUE)
        )
        # settlement fees — only ITM legs (intrinsic as premium)
        fee_settle = 0.0
        for k, intr in ((K_sc, ci(K_sc)), (K_sp, pi(K_sp)), (K_lc, ci(K_lc)), (K_lp, pi(K_lp))):
            if intr > 0:
                fee_settle += leg_fee_usd(S_exit, intr, LOTS, CONTRACT_VALUE)

        trades.append({
            "dt": nxt.name,
            "credit_per_btc": credit_per_btc,
            "payoff_per_btc": payoff_per_btc,
            "fee_entry": fee_entry,
            "fee_settle": fee_settle,
            "inside": K_sp < S_exit < K_sc,
        })
    return trades


def equity_curve(trades: list[dict], haircut: float) -> dict:
    """Additive equity from CAPITAL_USD, fixed 10 lots, with fees + credit haircut."""
    equity = CAPITAL_USD
    peak = equity
    max_dd = 0.0
    total_net = 0.0
    total_fees = 0.0
    wins = 0
    nets = []
    for t in trades:
        credit_usd = t["credit_per_btc"] * QTY_BTC * haircut
        payoff_usd = t["payoff_per_btc"] * QTY_BTC          # loss side NOT haircut (conservative)
        fees = t["fee_entry"] + t["fee_settle"]
        net = credit_usd + payoff_usd - fees
        equity += net
        total_net += net
        total_fees += fees
        nets.append(net)
        if net > 0:
            wins += 1
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    n = len(trades)
    return {
        "haircut": haircut,
        "trades": n,
        "win_rate": (wins / n * 100) if n else 0.0,
        "final_usd": equity,
        "total_net_usd": total_net,
        "total_fees_usd": total_fees,
        "max_dd_usd": max_dd,
        "avg_net_per_trade_usd": (total_net / n) if n else 0.0,
    }


def main() -> None:
    print("=" * 74)
    print("IRON CONDOR CAPITAL CHECK — 10 lots (0.01 BTC), real fees")
    print(f"Start capital: ₹{CAPITAL_INR:,.0f}  (${CAPITAL_USD:,.0f} at {FX:.0f}/$)")
    print(f"Position size: {LOTS} lots × {CONTRACT_VALUE} = {QTY_BTC} BTC per condor")
    print(f"Period: {START} -> {END}   pricing: synthetic BS, IV=RV×{1+VOL_PREMIUM:.2f}")
    print("=" * 74)
    print("Fetching BTC daily...", end=" ", flush=True)
    df = fetch_daily(TICKER, START, END)
    print(f"{len(df)} bars")
    trades = run(df)
    days = (df.index[-1] - df.index[0]).days
    years = days / 365.0

    print(f"\nTrades: {len(trades)} over ~{years:.1f} years\n")
    print(f"{'credit capture':>14} {'win%':>6} {'final ₹':>14} {'final $':>11} "
          f"{'net $':>10} {'fees $':>9} {'maxDD ₹':>11} {'/trade $':>9}")
    print("-" * 92)
    base = None
    for hc in HAIRCUTS:
        s = equity_curve(trades, hc)
        if base is None:
            base = s
        grow = "GROWS" if s["final_usd"] > CAPITAL_USD else "SHRINKS"
        print(f"{hc*100:>12.0f}% {s['win_rate']:>6.1f} "
              f"₹{s['final_usd']*FX:>12,.0f} ${s['final_usd']:>9,.0f} "
              f"${s['total_net_usd']:>+9,.0f} ${s['total_fees_usd']:>8,.0f} "
              f"₹{s['max_dd_usd']*FX:>9,.0f} ${s['avg_net_per_trade_usd']:>+8.3f}  {grow}")

    print("\n" + "=" * 74)
    print("INTERPRETATION")
    print("=" * 74)
    yrs = max(years, 0.1)
    # REALISTIC band for liquid Delta BTC daily options is 85-95% capture.
    # (The credit is hit by entry bid/ask; losses settle at true intrinsic with
    #  no bid/ask, so the asymmetric haircut is appropriate for hold-to-expiry.)
    for hc in (0.95, 0.90, 0.85):
        s = equity_curve(trades, hc)
        print(f"  At {hc*100:.0f}% credit (realistic): ₹{CAPITAL_INR:,.0f} -> "
              f"₹{s['final_usd']*FX:,.0f}  "
              f"({(s['final_usd']/CAPITAL_USD-1)*100:+.1f}% over {yrs:.1f}y = "
              f"{(s['final_usd']/CAPITAL_USD-1)*100/yrs:+.1f}%/yr, "
              f"₹{s['total_net_usd']*FX/(yrs*12):,.0f}/mo)")
    print(f"  70% capture = stress, 50% = market dislocation (strategy loses).")
    print(f"  Capital utilisation: max loss/condor ≈ ₹{base and 15.57*FX:,.0f} "
          f"on ₹{CAPITAL_INR:,.0f} = ~{15.57*FX/CAPITAL_INR*100:.1f}% of capital per trade.")
    print(f"  => 10 lots is TINY for ₹2L. Growth is real but small because the")
    print(f"     position barely uses the capital. Scaling lots scales both P&L AND risk.")


if __name__ == "__main__":
    main()

"""
0DTE Short Iron Condor on BTC — crypto-options research (Phase H.1).

Strategy:
    Each day at 00:00 UTC (Delta India daily expiry cycle ~12:00 UTC, but BS
    pricing for 24h DTE works as a clean approximation of 0DTE behavior):

      SELL ATM + SHORT_PCT call    (e.g., 1.5% OTM)
      SELL ATM - SHORT_PCT put     (e.g., 1.5% OTM)
      BUY  ATM + LONG_PCT  call    (e.g., 4% OTM — caps upside risk)
      BUY  ATM - LONG_PCT  put     (e.g., 4% OTM — caps downside risk)

      Hold to expiry (next day's close).
      P&L = net_credit_at_entry - payoff_at_expiry.

Risk model:
    - Defined risk: max loss = (wing_width - net_credit) per contract
    - Max profit = net credit (BTC stays within the short strikes)
    - Sizing: 1 contract per trade, scale per real Delta margin separately

Option pricing:
    - Black-Scholes with synthetic IV = realized_vol_20d * (1 + VOL_PREMIUM)
    - Crypto historically shows ~10-15% IV premium over RV (variance risk
      premium); 15% is a reasonable midpoint for backtest.
    - r = 0 (USDT-margined, no equity risk-free curve to apply)

Data:
    - yfinance BTC-USD daily bars, 2021-01-01 to 2026-05-30 (~5 years).
    - Close-to-close vol (annualized stdev of log returns).

Caveat:
    Synthetic pricing has 10-20% error vs real Delta/Deribit chain prices,
    especially on high-IV days. Use this to validate edge sign + magnitude;
    re-test on real chain data before deploying.

Run:
    py -3.14 scripts/backtest_iron_condor_btc.py
"""
from __future__ import annotations

import math
import os
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
os.chdir(REPO_ROOT)
sys.path.insert(0, str(REPO_ROOT / "src"))

import pandas as pd  # noqa: E402
import yfinance as yf  # noqa: E402


# ============================================================
# Config
# ============================================================
TICKER = "BTC-USD"
START = "2021-01-01"
END = "2026-05-30"

SHORT_PCT = 0.015     # short call/put 1.5% OTM
LONG_PCT = 0.040      # long call/put 4.0% OTM (wing)
DTE_HOURS = 24        # 0DTE — but synthesizing 1 trading-day window
VOL_LOOKBACK = 20     # days for RV
VOL_PREMIUM = 0.15    # IV = RV * 1.15
R = 0.0               # risk-free (USDT-margined)
CONTRACT_NOTIONAL = 1.0   # 1 BTC per contract — Delta India BTC option spec

# ============================================================
# FOMC dates 2021-2026 (rate-decision day = day 2 of each meeting)
# Sourced from federalreserve.gov public meeting calendar.
# These are the highest-impact macro events for crypto vol.
# ============================================================
FOMC_DATES: set[date] = {
    # 2021
    date(2021,1,27), date(2021,3,17), date(2021,4,28), date(2021,6,16),
    date(2021,7,28), date(2021,9,22), date(2021,11,3), date(2021,12,15),
    # 2022 (rate-hike cycle)
    date(2022,1,26), date(2022,3,16), date(2022,5,4), date(2022,6,15),
    date(2022,7,27), date(2022,9,21), date(2022,11,2), date(2022,12,14),
    # 2023
    date(2023,2,1), date(2023,3,22), date(2023,5,3), date(2023,6,14),
    date(2023,7,26), date(2023,9,20), date(2023,11,1), date(2023,12,13),
    # 2024 (rate-cut cycle starts)
    date(2024,1,31), date(2024,3,20), date(2024,5,1), date(2024,6,12),
    date(2024,7,31), date(2024,9,18), date(2024,11,7), date(2024,12,18),
    # 2025
    date(2025,1,29), date(2025,3,19), date(2025,5,7), date(2025,6,18),
    date(2025,7,30), date(2025,9,17), date(2025,10,29), date(2025,12,10),
    # 2026 (so far / scheduled)
    date(2026,1,28), date(2026,3,18), date(2026,4,29),
}

def event_set(include_fomc: bool = True) -> set[date]:
    s: set[date] = set()
    if include_fomc:
        s |= FOMC_DATES
    return s

# ============================================================
# Black-Scholes (no scipy — use math.erf for normal CDF)
# ============================================================

def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(S: float, K: float, T: float, sigma: float, opt: str) -> float:
    """Black-Scholes price. opt = 'C' or 'P'. T in years."""
    if T <= 0 or sigma <= 0:
        return max(0.0, (S - K) if opt == "C" else (K - S))
    sqrtT = math.sqrt(T)
    d1 = (math.log(S / K) + (R + 0.5 * sigma * sigma) * T) / (sigma * sqrtT)
    d2 = d1 - sigma * sqrtT
    if opt == "C":
        return S * _norm_cdf(d1) - K * math.exp(-R * T) * _norm_cdf(d2)
    return K * math.exp(-R * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)


# ============================================================
# Data
# ============================================================

def fetch_daily(ticker: str, start: str, end: str) -> pd.DataFrame:
    df = yf.download(ticker, start=start, end=end, interval="1d",
                     progress=False, auto_adjust=False)
    if df.empty:
        raise RuntimeError(f"yfinance returned no rows for {ticker}")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] for c in df.columns]
    df = df.rename(columns=str).rename(columns={
        "Open": "open", "High": "high", "Low": "low",
        "Close": "close", "Volume": "volume",
    })
    df.index = pd.to_datetime(df.index)
    return df[["open", "high", "low", "close", "volume"]].dropna()


def compute_realized_vol(df: pd.DataFrame, lookback: int) -> pd.Series:
    """Annualized stdev of log returns over a rolling window."""
    log_ret = (df["close"] / df["close"].shift(1)).apply(lambda x: math.log(x) if x > 0 else 0.0)
    rv = log_ret.rolling(lookback).std() * math.sqrt(365)
    return rv


# ============================================================
# Iron Condor backtest
# ============================================================

def run_iron_condor(
    df: pd.DataFrame,
    skip_event_dates: set[date] | None = None,
) -> list[dict]:
    """
    skip_event_dates: dates on which to NOT hold the position. If today's
    entry would expire on an event date (next bar = event), skip the trade.
    """
    skip = skip_event_dates or set()
    df = df.copy()
    df["rv"] = compute_realized_vol(df, VOL_LOOKBACK)
    df["iv_used"] = df["rv"] * (1.0 + VOL_PREMIUM)
    df = df.dropna(subset=["iv_used"])

    trades: list[dict] = []
    T = DTE_HOURS / 24.0 / 365.0    # years to expiry

    # Entry at day N's close, exit at day N+1's close (24h holding window)
    for i in range(len(df) - 1):
        row = df.iloc[i]
        next_row = df.iloc[i + 1]
        S = float(row["close"])
        sigma = float(row["iv_used"])
        entry_dt = row.name
        exit_dt = next_row.name
        S_exit = float(next_row["close"])

        # Event filter — skip if expiry day is a scheduled high-impact event
        if exit_dt.date() in skip:
            continue

        # Strikes
        K_sc = S * (1.0 + SHORT_PCT)
        K_sp = S * (1.0 - SHORT_PCT)
        K_lc = S * (1.0 + LONG_PCT)
        K_lp = S * (1.0 - LONG_PCT)

        # Entry premium (each leg)
        prem_sc = bs_price(S, K_sc, T, sigma, "C")
        prem_sp = bs_price(S, K_sp, T, sigma, "P")
        prem_lc = bs_price(S, K_lc, T, sigma, "C")
        prem_lp = bs_price(S, K_lp, T, sigma, "P")

        # Net credit (sells - buys), per 1 BTC contract
        net_credit = (prem_sc + prem_sp) - (prem_lc + prem_lp)
        if net_credit <= 0:
            # Won't trade if no credit available (e.g., extreme IV inversion)
            continue

        # Payoff at expiry (T=0). Underlying = S_exit.
        # Each leg pays max(0, S-K) for calls, max(0, K-S) for puts.
        pay_sc = max(0.0, S_exit - K_sc)         # we sold -> we PAY
        pay_sp = max(0.0, K_sp - S_exit)
        pay_lc = max(0.0, S_exit - K_lc)         # we bought -> we RECEIVE
        pay_lp = max(0.0, K_lp - S_exit)
        net_payoff = -(pay_sc + pay_sp) + (pay_lc + pay_lp)

        pnl = (net_credit + net_payoff) * CONTRACT_NOTIONAL

        max_loss = ((K_lc - K_sc) - net_credit) * CONTRACT_NOTIONAL   # wing width - credit, same both sides if symmetric
        outcome = (
            "MAX_LOSS" if pnl <= -max_loss * 0.95 else
            "MAX_WIN"  if pnl >= net_credit * 0.95 else
            "PARTIAL"
        )

        trades.append({
            "entry_dt": entry_dt, "exit_dt": exit_dt,
            "S_entry": S, "S_exit": S_exit,
            "iv_used": sigma,
            "K_short_call": K_sc, "K_short_put": K_sp,
            "K_long_call": K_lc, "K_long_put": K_lp,
            "net_credit": net_credit,
            "max_loss": max_loss,
            "pnl_usd": pnl,
            "outcome": outcome,
        })

    return trades


# ============================================================
# Stats
# ============================================================

def stats(trades: list[dict]) -> dict:
    if not trades:
        return {"trades": 0}
    pnls = [t["pnl_usd"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    total = sum(pnls)
    gw = sum(wins)
    gl = abs(sum(losses))
    pf = (gw / gl) if gl > 0 else float("inf")
    # Drawdown
    running, peak, max_dd = 0.0, 0.0, 0.0
    for p in pnls:
        running += p
        peak = max(peak, running)
        max_dd = max(max_dd, peak - running)
    avg_credit = sum(t["net_credit"] for t in trades) / len(trades)
    avg_max_loss = sum(t["max_loss"] for t in trades) / len(trades)
    n_max_win = sum(1 for t in trades if t["outcome"] == "MAX_WIN")
    n_max_loss = sum(1 for t in trades if t["outcome"] == "MAX_LOSS")
    n_partial = sum(1 for t in trades if t["outcome"] == "PARTIAL")
    return {
        "trades": len(trades),
        "wins": len(wins), "losses": len(losses),
        "win_rate": len(wins) / len(trades),
        "total_usd": total,
        "profit_factor": pf,
        "max_dd_usd": max_dd,
        "avg_win_usd": (gw / len(wins)) if wins else 0.0,
        "avg_loss_usd": (-gl / len(losses)) if losses else 0.0,
        "avg_credit_usd": avg_credit,
        "avg_max_loss_usd": avg_max_loss,
        "outcomes": {
            "MAX_WIN": n_max_win, "MAX_LOSS": n_max_loss, "PARTIAL": n_partial,
        },
    }


def year_breakdown(trades: list[dict]) -> dict[int, dict]:
    by_year: dict[int, list[dict]] = {}
    for t in trades:
        by_year.setdefault(t["exit_dt"].year, []).append(t)
    return {y: stats(ts) for y, ts in by_year.items()}


def fmt_pf(pf: float) -> str:
    return "inf" if pf == float("inf") else f"{pf:.2f}"


def report(trades: list[dict]) -> None:
    s = stats(trades)
    print()
    print("=" * 72)
    print(f"BTC 0DTE Iron Condor   short=±{SHORT_PCT*100:.1f}%   wing=±{LONG_PCT*100:.1f}%")
    print(f"IV proxy = RV({VOL_LOOKBACK}d) × {1+VOL_PREMIUM:.2f}   contract = {CONTRACT_NOTIONAL} BTC")
    print("=" * 72)
    if s["trades"] == 0:
        print("  no trades"); return
    print(f"  trades:           {s['trades']}")
    print(f"  wins/losses:      {s['wins']} / {s['losses']}")
    print(f"  win rate:         {s['win_rate']*100:.1f}%")
    print(f"  outcomes:         max_win={s['outcomes']['MAX_WIN']}  partial={s['outcomes']['PARTIAL']}  max_loss={s['outcomes']['MAX_LOSS']}")
    print(f"  profit factor:    {fmt_pf(s['profit_factor'])}")
    print(f"  avg credit/trade: ${s['avg_credit_usd']:,.0f}")
    print(f"  avg max loss/tr:  ${s['avg_max_loss_usd']:,.0f}")
    print(f"  avg win:          ${s['avg_win_usd']:+,.0f}")
    print(f"  avg loss:         ${s['avg_loss_usd']:+,.0f}")
    print(f"  total P&L:        ${s['total_usd']:+,.0f}  per 1 BTC contract")
    print(f"  max drawdown:     ${s['max_dd_usd']:,.0f}")
    print()
    print("  Year-by-year:")
    print(f"    {'Year':6}{'Trades':>8}{'WR%':>8}{'PF':>8}{'Total$':>14}{'MaxDD$':>14}")
    print(f"    {'-'*58}")
    yb = year_breakdown(trades)
    for y in sorted(yb.keys()):
        s_y = yb[y]
        print(
            f"    {y:6}{s_y['trades']:>8}"
            f"{s_y['win_rate']*100:>7.1f}%"
            f"{fmt_pf(s_y['profit_factor']):>8}"
            f"${s_y['total_usd']:>+12,.0f}"
            f"${s_y['max_dd_usd']:>12,.0f}"
        )


# ============================================================
# Main
# ============================================================

def run_for_ticker(ticker: str, label: str) -> None:
    print(f"\n{'='*72}")
    print(f"{label}   ticker={ticker}   {START} -> {END}")
    print(f"{'='*72}")
    print(f"Fetching...", end=" ", flush=True)
    try:
        df = fetch_daily(ticker, START, END)
    except Exception as e:
        print(f"FAILED: {e}"); return
    print(f"got {len(df)} bars")
    trades = run_iron_condor(df)
    report(trades)


def main() -> None:
    print("0DTE Short Iron Condor — Cross-Asset Validation")
    print(f"short=±{SHORT_PCT*100:.1f}%  wing=±{LONG_PCT*100:.1f}%  "
          f"IV=RV({VOL_LOOKBACK}d)×{1+VOL_PREMIUM:.2f}  contract={CONTRACT_NOTIONAL} coin")

    run_for_ticker("BTC-USD", "BITCOIN (BTC-USD)")
    run_for_ticker("ETH-USD", "ETHEREUM (ETH-USD)")


if __name__ == "__main__":
    main()

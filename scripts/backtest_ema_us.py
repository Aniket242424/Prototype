"""
EMA(9/21) crossover backtest on US index futures — Phase G.2 R&D.

Pulls 5 years of daily bars from Yahoo Finance for:
    - YM=F  (E-mini Dow Jones)
    - NQ=F  (E-mini Nasdaq-100)
    - ES=F  (E-mini S&P 500)        -- included for comparison

Runs a long-only 9/21 EMA crossover:
    - Entry:  9-day EMA closes above 21-day EMA
    - Exit:   9-day EMA closes below 21-day EMA (no stops, no targets)

Reports per-year + aggregate stats:
    trades, wins, win_rate, total return %, max drawdown %, profit factor.

Run:
    py -3.14 scripts/backtest_ema_us.py
"""
from __future__ import annotations

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
INSTRUMENTS = {
    "DOW (YM=F)":    "YM=F",
    "NASDAQ (NQ=F)": "NQ=F",
    "S&P  (ES=F)":   "ES=F",
}
START_DATE = "2021-05-30"
END_DATE = "2026-05-30"
EMA_SHORT = 9
EMA_LONG = 21


# Yahoo's continuous futures don't carry a contract multiplier in their CSV.
# Multipliers (used to convert point P&L to $) for reporting only:
MULTIPLIERS = {
    "YM=F": 5.0,   # E-mini Dow:  $5 / index pt
    "NQ=F": 20.0,  # E-mini NQ:   $20 / index pt
    "ES=F": 50.0,  # E-mini S&P:  $50 / index pt
}


# ============================================================
# Data fetch
# ============================================================

def fetch_daily(ticker: str, start: str, end: str) -> pd.DataFrame:
    df = yf.download(
        ticker,
        start=start,
        end=end,
        interval="1d",
        auto_adjust=False,
        progress=False,
    )
    if df.empty:
        raise RuntimeError(f"yfinance returned no rows for {ticker}")
    # yfinance returns a multi-index column DataFrame; flatten to single level
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] for c in df.columns]
    df = df.rename(columns=str).rename(columns={
        "Open": "open", "High": "high", "Low": "low",
        "Close": "close", "Volume": "volume",
    })
    df.index = pd.to_datetime(df.index)
    return df[["open", "high", "low", "close", "volume"]].dropna()


# ============================================================
# Strategy + backtest
# ============================================================

def run_ema_crossover(df: pd.DataFrame, ema_short: int, ema_long: int) -> list[dict]:
    """
    Long-only 9/21 EMA crossover backtest. Each closed trade = one dict.
    Position size = 1 contract. P&L in INDEX POINTS (multiplier applied later
    for $ conversion).
    """
    df = df.copy()
    df["ema_s"] = df["close"].ewm(span=ema_short, adjust=False).mean()
    df["ema_l"] = df["close"].ewm(span=ema_long, adjust=False).mean()
    df["bullish"] = df["ema_s"] > df["ema_l"]
    df["prev_bullish"] = df["bullish"].shift(1).fillna(False)
    df["cross_up"] = df["bullish"] & ~df["prev_bullish"]
    df["cross_down"] = ~df["bullish"] & df["prev_bullish"]

    trades: list[dict] = []
    in_pos = False
    entry_dt = None
    entry_px = None

    for dt, row in df.iterrows():
        if not in_pos and bool(row["cross_up"]):
            entry_dt = dt
            entry_px = float(row["close"])
            in_pos = True
        elif in_pos and bool(row["cross_down"]):
            exit_dt = dt
            exit_px = float(row["close"])
            pnl_pts = exit_px - entry_px
            ret_pct = (pnl_pts / entry_px) * 100.0
            trades.append({
                "entry_dt": entry_dt, "entry_px": entry_px,
                "exit_dt": exit_dt, "exit_px": exit_px,
                "pnl_pts": pnl_pts, "ret_pct": ret_pct,
                "hold_days": (exit_dt - entry_dt).days,
            })
            in_pos = False
            entry_dt, entry_px = None, None

    # If still in a position at series end, close on last bar
    if in_pos:
        last_dt = df.index[-1]
        last_px = float(df.iloc[-1]["close"])
        pnl_pts = last_px - entry_px
        trades.append({
            "entry_dt": entry_dt, "entry_px": entry_px,
            "exit_dt": last_dt, "exit_px": last_px,
            "pnl_pts": pnl_pts, "ret_pct": (pnl_pts / entry_px) * 100.0,
            "hold_days": (last_dt - entry_dt).days,
            "open_at_end": True,
        })

    return trades


# ============================================================
# Stats + reporting
# ============================================================

def aggregate_stats(trades: list[dict], multiplier: float) -> dict:
    if not trades:
        return {"trades": 0}
    wins = [t for t in trades if t["pnl_pts"] > 0]
    losses = [t for t in trades if t["pnl_pts"] <= 0]
    total_pts = sum(t["pnl_pts"] for t in trades)
    total_usd = total_pts * multiplier
    avg_win_pts = (sum(t["pnl_pts"] for t in wins) / len(wins)) if wins else 0.0
    avg_loss_pts = (sum(t["pnl_pts"] for t in losses) / len(losses)) if losses else 0.0
    gross_win = sum(t["pnl_pts"] for t in wins)
    gross_loss = abs(sum(t["pnl_pts"] for t in losses))
    pf = (gross_win / gross_loss) if gross_loss > 0 else float("inf")

    # Equity curve + max drawdown (in points)
    running = 0.0
    peak = 0.0
    max_dd_pts = 0.0
    for t in trades:
        running += t["pnl_pts"]
        peak = max(peak, running)
        max_dd_pts = max(max_dd_pts, peak - running)

    return {
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / len(trades),
        "total_pts": total_pts,
        "total_usd": total_usd,
        "avg_win_pts": avg_win_pts,
        "avg_loss_pts": avg_loss_pts,
        "profit_factor": pf,
        "max_dd_pts": max_dd_pts,
        "max_dd_usd": max_dd_pts * multiplier,
        "avg_hold_days": sum(t["hold_days"] for t in trades) / len(trades),
    }


def year_breakdown(trades: list[dict], multiplier: float) -> dict[int, dict]:
    by_year: dict[int, list[dict]] = {}
    for t in trades:
        by_year.setdefault(t["exit_dt"].year, []).append(t)
    return {y: aggregate_stats(ts, multiplier) for y, ts in by_year.items()}


def fmt_pf(pf: float) -> str:
    return "inf" if pf == float("inf") else f"{pf:.2f}"


def print_report(label: str, ticker: str, trades: list[dict]) -> None:
    mult = MULTIPLIERS[ticker]
    agg = aggregate_stats(trades, mult)
    print()
    print("=" * 72)
    print(f"{label}   ticker={ticker}   multiplier=${mult:.0f}/pt")
    print("=" * 72)
    if agg["trades"] == 0:
        print("  no trades")
        return
    print(f"  trades:        {agg['trades']}")
    print(f"  wins/losses:   {agg['wins']} / {agg['losses']}")
    print(f"  win rate:      {agg['win_rate']*100:.1f}%")
    print(f"  avg win:       {agg['avg_win_pts']:+.1f} pts (${agg['avg_win_pts']*mult:+.0f})")
    print(f"  avg loss:      {agg['avg_loss_pts']:+.1f} pts (${agg['avg_loss_pts']*mult:+.0f})")
    print(f"  profit factor: {fmt_pf(agg['profit_factor'])}")
    print(f"  total return:  {agg['total_pts']:+.1f} pts (${agg['total_usd']:+,.0f})")
    print(f"  max drawdown:  {agg['max_dd_pts']:.1f} pts (${agg['max_dd_usd']:,.0f})")
    print(f"  avg hold:      {agg['avg_hold_days']:.1f} days")

    yr = year_breakdown(trades, mult)
    print()
    print("  Year-by-year:")
    print(f"    {'Year':6}{'Trades':>8}{'WR%':>7}{'TotPts':>10}{'TotUSD':>12}{'PF':>7}{'MaxDD$':>10}")
    print(f"    {'-'*60}")
    for y in sorted(yr.keys()):
        s = yr[y]
        print(
            f"    {y:6}{s['trades']:>8}"
            f"{s['win_rate']*100:>6.1f}%"
            f"{s['total_pts']:>+10.1f}"
            f"${s['total_usd']:>+10,.0f}"
            f"{fmt_pf(s['profit_factor']):>7}"
            f"${s['max_dd_usd']:>8,.0f}"
        )


# ============================================================
# Main
# ============================================================

def main() -> None:
    print(f"EMA({EMA_SHORT}/{EMA_LONG}) Crossover Backtest")
    print(f"Period: {START_DATE} -> {END_DATE}")
    print(f"Source: Yahoo Finance (continuous futures)")
    print()

    for label, ticker in INSTRUMENTS.items():
        print(f"Fetching {label} ({ticker}) ...", end=" ", flush=True)
        try:
            df = fetch_daily(ticker, START_DATE, END_DATE)
            print(f"got {len(df)} bars")
        except Exception as e:
            print(f"FAILED: {e}")
            continue
        trades = run_ema_crossover(df, EMA_SHORT, EMA_LONG)
        print_report(label, ticker, trades)


if __name__ == "__main__":
    main()

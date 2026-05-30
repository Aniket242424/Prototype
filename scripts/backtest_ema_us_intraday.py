"""
EMA(9/21) crossover backtest on US index futures — INTRADAY bars.

Yahoo Finance free-tier limits on sub-daily history:
    1m  -> 7 days max
    5m  -> 60 days max     (this script default)
    60m -> 730 days max

Strategy is identical to the daily backtest (long-only 9/21 EMA cross),
just on a finer timeframe. With ~6.5 hrs of US RTH per day and 78 5-min
bars per session, expect ~50-200 trades over 60 days per instrument.

Run:
    py -3.14 scripts/backtest_ema_us_intraday.py
"""
from __future__ import annotations

import os
import sys
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
INTERVAL = "5m"
PERIOD = "60d"   # Yahoo's max for 5m bars
EMA_SHORT = 9
EMA_LONG = 21

MULTIPLIERS = {"YM=F": 5.0, "NQ=F": 20.0, "ES=F": 50.0}


# ============================================================
# Fetch
# ============================================================

def fetch_bars(ticker: str, interval: str, period: str) -> pd.DataFrame:
    df = yf.download(
        ticker,
        period=period,
        interval=interval,
        auto_adjust=False,
        progress=False,
    )
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


# ============================================================
# Strategy (same as daily backtest)
# ============================================================

def run_ema_crossover(df: pd.DataFrame) -> list[dict]:
    df = df.copy()
    df["ema_s"] = df["close"].ewm(span=EMA_SHORT, adjust=False).mean()
    df["ema_l"] = df["close"].ewm(span=EMA_LONG, adjust=False).mean()
    df["bullish"] = df["ema_s"] > df["ema_l"]
    prev = df["bullish"].shift(1).fillna(False)
    df["cross_up"] = df["bullish"] & ~prev
    df["cross_down"] = ~df["bullish"] & prev

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
            pnl = float(row["close"]) - entry_px
            trades.append({
                "entry_dt": entry_dt, "entry_px": entry_px,
                "exit_dt": dt, "exit_px": float(row["close"]),
                "pnl_pts": pnl,
                "hold_min": (dt - entry_dt).total_seconds() / 60.0,
            })
            in_pos = False
            entry_dt, entry_px = None, None

    if in_pos:
        last_dt = df.index[-1]
        last_px = float(df.iloc[-1]["close"])
        pnl = last_px - entry_px
        trades.append({
            "entry_dt": entry_dt, "entry_px": entry_px,
            "exit_dt": last_dt, "exit_px": last_px,
            "pnl_pts": pnl,
            "hold_min": (last_dt - entry_dt).total_seconds() / 60.0,
            "open_at_end": True,
        })
    return trades


# ============================================================
# Stats
# ============================================================

def stats(trades: list[dict], mult: float) -> dict:
    if not trades:
        return {"trades": 0}
    wins = [t for t in trades if t["pnl_pts"] > 0]
    losses = [t for t in trades if t["pnl_pts"] <= 0]
    total = sum(t["pnl_pts"] for t in trades)
    gw = sum(t["pnl_pts"] for t in wins)
    gl = abs(sum(t["pnl_pts"] for t in losses))
    pf = (gw / gl) if gl > 0 else float("inf")

    # Equity + drawdown
    running, peak, max_dd = 0.0, 0.0, 0.0
    for t in trades:
        running += t["pnl_pts"]
        peak = max(peak, running)
        max_dd = max(max_dd, peak - running)

    avg_hold = sum(t["hold_min"] for t in trades) / len(trades)
    return {
        "trades": len(trades),
        "wins": len(wins), "losses": len(losses),
        "win_rate": len(wins) / len(trades),
        "total_pts": total, "total_usd": total * mult,
        "avg_win_pts": (gw / len(wins)) if wins else 0.0,
        "avg_loss_pts": (-gl / len(losses)) if losses else 0.0,
        "profit_factor": pf,
        "max_dd_pts": max_dd, "max_dd_usd": max_dd * mult,
        "avg_hold_min": avg_hold,
    }


def fmt_pf(pf: float) -> str:
    return "inf" if pf == float("inf") else f"{pf:.2f}"


def report(label: str, ticker: str, trades: list[dict]) -> None:
    mult = MULTIPLIERS[ticker]
    s = stats(trades, mult)
    print()
    print("=" * 72)
    print(f"{label}   ticker={ticker}   interval={INTERVAL}   period={PERIOD}")
    print("=" * 72)
    if s["trades"] == 0:
        print("  no trades")
        return
    print(f"  trades:        {s['trades']}")
    print(f"  wins/losses:   {s['wins']} / {s['losses']}")
    print(f"  win rate:      {s['win_rate']*100:.1f}%")
    print(f"  avg win:       {s['avg_win_pts']:+.2f} pts (${s['avg_win_pts']*mult:+.2f})")
    print(f"  avg loss:      {s['avg_loss_pts']:+.2f} pts (${s['avg_loss_pts']*mult:+.2f})")
    print(f"  profit factor: {fmt_pf(s['profit_factor'])}")
    print(f"  total return:  {s['total_pts']:+.2f} pts (${s['total_usd']:+,.0f})")
    print(f"  max drawdown:  {s['max_dd_pts']:.2f} pts (${s['max_dd_usd']:,.0f})")
    print(f"  avg hold:      {s['avg_hold_min']:.0f} min")


# ============================================================
# Main
# ============================================================

def main() -> None:
    print(f"EMA({EMA_SHORT}/{EMA_LONG}) Crossover INTRADAY Backtest")
    print(f"Interval={INTERVAL}  Period={PERIOD}")
    print()
    for label, ticker in INSTRUMENTS.items():
        print(f"Fetching {label} ({ticker})...", end=" ", flush=True)
        try:
            df = fetch_bars(ticker, INTERVAL, PERIOD)
            print(f"got {len(df)} bars")
        except Exception as e:
            print(f"FAILED: {e}")
            continue
        trades = run_ema_crossover(df)
        report(label, ticker, trades)


if __name__ == "__main__":
    main()

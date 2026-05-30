"""
EMA pullback / retest strategy with 1:3 RR — user-designed.

Setups (all entries on the bar that completes the touch):

  LONG:
    A. Pullback to 10 EMA from above:
       - prev close > 10 EMA (we were above)
       - current bar's low <= 10 EMA <= current bar's high (touched it)
       - current close > 10 EMA (bounced back above)

    B. Retest of 20 EMA from below:
       - prev close < 20 EMA (we were below)
       - current bar's low <= 20 EMA <= current bar's high
       - current close > 20 EMA (broke above)

  SHORT: mirrors of A and B around their EMAs.

Risk model:
  - Stop:   0.5% from entry
  - Target: 1.5% from entry (= 3 × stop)
  - Hold until stop or target hits intra-bar.
  - Pessimistic tie-break: if a single bar's range touches BOTH stop and
    target, STOP wins (avoids optimistic bias).
  - Single position at a time.

Data: Yahoo Finance YM=F, NQ=F, ES=F. 5-min interval, 60-day window.

Run:
    py -3.14 scripts/backtest_ema_pullback.py
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
PERIOD = "60d"
EMA_FAST = 10
EMA_SLOW = 20
STOP_PCT = 0.005          # 0.5%
RR = 2.0                  # target distance = 2 × stop distance (user-requested 2:1)

MULTIPLIERS = {"YM=F": 5.0, "NQ=F": 20.0, "ES=F": 50.0}


# ============================================================
# Fetch
# ============================================================

def fetch_bars(ticker: str, interval: str, period: str) -> pd.DataFrame:
    df = yf.download(ticker, period=period, interval=interval,
                     auto_adjust=False, progress=False)
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
# Strategy
# ============================================================

def run_pullback(df: pd.DataFrame) -> list[dict]:
    df = df.copy()
    df["ema_f"] = df["close"].ewm(span=EMA_FAST, adjust=False).mean()
    df["ema_s"] = df["close"].ewm(span=EMA_SLOW, adjust=False).mean()
    df["prev_close"] = df["close"].shift(1)
    df["prev_ema_f"] = df["ema_f"].shift(1)
    df["prev_ema_s"] = df["ema_s"].shift(1)

    trades: list[dict] = []
    in_pos = False
    direction = None       # "LONG" or "SHORT"
    entry_dt = None
    entry_px = None
    stop = None
    target = None
    setup_name = None

    # Skip first 21 bars to let EMAs settle
    df_iter = df.iloc[max(EMA_FAST, EMA_SLOW) + 1:]

    for dt, row in df_iter.iterrows():
        # ---------- Manage open position ----------
        if in_pos:
            hi, lo = float(row["high"]), float(row["low"])
            if direction == "LONG":
                # Pessimistic: stop checked before target
                if lo <= stop:
                    trades.append({
                        "setup": setup_name, "dir": "LONG",
                        "entry_dt": entry_dt, "entry_px": entry_px,
                        "exit_dt": dt, "exit_px": stop,
                        "pnl_pts": stop - entry_px,
                        "outcome": "STOP",
                        "hold_min": (dt - entry_dt).total_seconds() / 60.0,
                    })
                    in_pos = False; continue
                if hi >= target:
                    trades.append({
                        "setup": setup_name, "dir": "LONG",
                        "entry_dt": entry_dt, "entry_px": entry_px,
                        "exit_dt": dt, "exit_px": target,
                        "pnl_pts": target - entry_px,
                        "outcome": "TARGET",
                        "hold_min": (dt - entry_dt).total_seconds() / 60.0,
                    })
                    in_pos = False; continue
            else:  # SHORT
                if hi >= stop:
                    trades.append({
                        "setup": setup_name, "dir": "SHORT",
                        "entry_dt": entry_dt, "entry_px": entry_px,
                        "exit_dt": dt, "exit_px": stop,
                        "pnl_pts": entry_px - stop,
                        "outcome": "STOP",
                        "hold_min": (dt - entry_dt).total_seconds() / 60.0,
                    })
                    in_pos = False; continue
                if lo <= target:
                    trades.append({
                        "setup": setup_name, "dir": "SHORT",
                        "entry_dt": entry_dt, "entry_px": entry_px,
                        "exit_dt": dt, "exit_px": target,
                        "pnl_pts": entry_px - target,
                        "outcome": "TARGET",
                        "hold_min": (dt - entry_dt).total_seconds() / 60.0,
                    })
                    in_pos = False; continue

        # ---------- Look for entries (only when flat) ----------
        if in_pos:
            continue

        op, hi, lo, cl = (float(row[k]) for k in ("open", "high", "low", "close"))
        ef, es = float(row["ema_f"]), float(row["ema_s"])
        pc = row["prev_close"]; pef = row["prev_ema_f"]; pes = row["prev_ema_s"]
        if pd.isna(pc) or pd.isna(pef) or pd.isna(pes):
            continue
        pc = float(pc); pef = float(pef); pes = float(pes)

        touched_ef = lo <= ef <= hi
        touched_es = lo <= es <= hi

        # ----- LONG setups -----
        if pc > pef and touched_ef and cl > ef:
            entry_px = cl
            stop = entry_px * (1.0 - STOP_PCT)
            target = entry_px + (entry_px - stop) * RR
            in_pos = True; direction = "LONG"
            entry_dt = dt; setup_name = "LONG_pullback_10ema"
            continue
        if pc < pes and touched_es and cl > es:
            entry_px = cl
            stop = entry_px * (1.0 - STOP_PCT)
            target = entry_px + (entry_px - stop) * RR
            in_pos = True; direction = "LONG"
            entry_dt = dt; setup_name = "LONG_retest_20ema"
            continue

        # ----- SHORT setups -----
        if pc < pef and touched_ef and cl < ef:
            entry_px = cl
            stop = entry_px * (1.0 + STOP_PCT)
            target = entry_px - (stop - entry_px) * RR
            in_pos = True; direction = "SHORT"
            entry_dt = dt; setup_name = "SHORT_bounce_10ema"
            continue
        if pc > pes and touched_es and cl < es:
            entry_px = cl
            stop = entry_px * (1.0 + STOP_PCT)
            target = entry_px - (stop - entry_px) * RR
            in_pos = True; direction = "SHORT"
            entry_dt = dt; setup_name = "SHORT_breakdown_20ema"

    # Close hanging position at last bar's close
    if in_pos:
        last_dt = df.index[-1]; last_px = float(df.iloc[-1]["close"])
        pnl = (last_px - entry_px) if direction == "LONG" else (entry_px - last_px)
        trades.append({
            "setup": setup_name, "dir": direction,
            "entry_dt": entry_dt, "entry_px": entry_px,
            "exit_dt": last_dt, "exit_px": last_px,
            "pnl_pts": pnl, "outcome": "EOD",
            "hold_min": (last_dt - entry_dt).total_seconds() / 60.0,
        })

    return trades


# ============================================================
# Stats + reporting
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
    running, peak, max_dd = 0.0, 0.0, 0.0
    for t in trades:
        running += t["pnl_pts"]
        peak = max(peak, running)
        max_dd = max(max_dd, peak - running)
    avg_hold = sum(t["hold_min"] for t in trades) / len(trades)
    n_target = sum(1 for t in trades if t["outcome"] == "TARGET")
    n_stop = sum(1 for t in trades if t["outcome"] == "STOP")
    n_eod = sum(1 for t in trades if t["outcome"] == "EOD")
    return {
        "trades": len(trades), "wins": len(wins), "losses": len(losses),
        "win_rate": len(wins) / len(trades),
        "total_pts": total, "total_usd": total * mult,
        "profit_factor": pf,
        "max_dd_pts": max_dd, "max_dd_usd": max_dd * mult,
        "avg_hold_min": avg_hold,
        "outcomes": {"TARGET": n_target, "STOP": n_stop, "EOD": n_eod},
    }


def setup_breakdown(trades: list[dict]) -> dict[str, dict]:
    by_setup: dict[str, list[dict]] = {}
    for t in trades:
        by_setup.setdefault(t["setup"], []).append(t)
    out = {}
    for setup_name, ts in by_setup.items():
        n = len(ts)
        wins = sum(1 for t in ts if t["pnl_pts"] > 0)
        out[setup_name] = {
            "trades": n, "wins": wins, "win_rate": wins / n,
            "total_pts": sum(t["pnl_pts"] for t in ts),
        }
    return out


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
        print("  no trades"); return
    print(f"  trades:        {s['trades']}")
    print(f"  wins/losses:   {s['wins']} / {s['losses']}")
    print(f"  win rate:      {s['win_rate']*100:.1f}%")
    print(f"  outcomes:      target={s['outcomes']['TARGET']}  stop={s['outcomes']['STOP']}  eod={s['outcomes']['EOD']}")
    print(f"  profit factor: {fmt_pf(s['profit_factor'])}")
    print(f"  total return:  {s['total_pts']:+.2f} pts (${s['total_usd']:+,.0f})")
    print(f"  max drawdown:  {s['max_dd_pts']:.2f} pts (${s['max_dd_usd']:,.0f})")
    print(f"  avg hold:      {s['avg_hold_min']:.0f} min")
    print()
    print("  By setup:")
    print(f"    {'setup':<28s}{'trades':>8}{'WR%':>8}{'TotPts':>10}")
    for name, st in setup_breakdown(trades).items():
        print(f"    {name:<28s}{st['trades']:>8}{st['win_rate']*100:>7.1f}%{st['total_pts']:>+10.2f}")


# ============================================================
# Main
# ============================================================

def main() -> None:
    print(f"EMA Pullback/Retest Strategy ({EMA_FAST}/{EMA_SLOW} EMA, 1:{int(RR)} RR, {STOP_PCT*100:.1f}% stop)")
    print(f"Interval={INTERVAL}  Period={PERIOD}")
    for label, ticker in INSTRUMENTS.items():
        print(); print(f"Fetching {label} ({ticker})...", end=" ", flush=True)
        try:
            df = fetch_bars(ticker, INTERVAL, PERIOD)
            print(f"got {len(df)} bars")
        except Exception as e:
            print(f"FAILED: {e}"); continue
        trades = run_pullback(df)
        report(label, ticker, trades)


if __name__ == "__main__":
    main()

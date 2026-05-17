# EOD-Premium NIFTY ITM1 Buy — AlgoTest Strategy Spec

**Purpose:** Validate our Python EOD-Premium backtest result (55% win rate, PF 1.65 with realistic costs over 12 months) against AlgoTest's real historical option premium data over 3 years.

**Why AlgoTest:** Our backtest accounts in R-multiples on the underlying because we don't have historical option premiums. AlgoTest has the actual NIFTY option premium history (every strike, every minute, 2020-2025). This validates our R→₹ conversion assumption.

---

## 1. Instrument & Direction

| Field | Value |
|---|---|
| Underlying | **NIFTY 50** |
| Instrument type | **Options** |
| Expiry | **Current week** (or "Nearest weekly") |
| Strike selection | **ITM1** (1 strike in-the-money from spot) |
| Action on LONG signal | **Buy CE** |
| Action on SHORT signal | **Buy PE** |
| Position type | **Intraday** (must square off same day) |
| Max simultaneous positions | **1** |
| Re-entry | **OFF** |

---

## 2. Time Window (CRITICAL)

| Setting | Value |
|---|---|
| Trade entry start | **14:50:00 IST** |
| Trade entry end | **15:15:00 IST** |
| Auto square-off (force exit) | **15:25:00 IST** |
| Skip days | Off (no Wed-only / Thu-only restriction; trade every day) |

---

## 3. Entry Conditions (ALL must be satisfied — AND logic)

### LONG (Buy CE) — fires when:

| # | Condition | AlgoTest mapping |
|---|---|---|
| 1 | Current time within [14:50, 15:15] | Use the time-based entry window above |
| 2 | NIFTY spot > EMA(20) on 1-min | `Close > EMA(20)` on 1m timeframe |
| 3 | NIFTY spot > VWAP (session) | `Close > VWAP` |
| 4 | NIFTY spot within 0.40% of today's high | `Close > Day_High × 0.9960` |
| 5 | Current 1-min bar body ≥ 50% of full range | `(Close - Open) > 0.5 × (High - Low)` AND `Close > Open` (green candle) |
| 6 | ATR(14) on 1-min is between 12 and 80 points | `ATR(14) > 12` AND `ATR(14) < 80` (filters dead/blow-off volatility) |
| 7 | RSI(14) on 1-min > 55 | `RSI(14) > 55` (trend confirmation; proxy for "6 of 8 bullish bars") |
| 8 | Morning bias bullish: NIFTY close at 12:00 > NIFTY open at 09:30 | If AlgoTest doesn't support this directly, **skip this filter** for first run and add as a second variant later |

### SHORT (Buy PE) — mirror of above:
1. Time 14:50-15:15
2. NIFTY spot < EMA(20)
3. NIFTY spot < VWAP
4. NIFTY spot within 0.40% of today's low: `Close < Day_Low × 1.0040`
5. Red candle with body ≥ 50% of range: `(Open - Close) > 0.5 × (High - Low)` AND `Close < Open`
6. ATR(14) between 12 and 80
7. RSI(14) < 45
8. Morning bias bearish: NIFTY close at 12:00 < NIFTY open at 09:30 (skip if not supported)

---

## 4. Exit Rules

| Exit Type | Setting |
|---|---|
| Stop Loss | **30 NIFTY points** on underlying (≈ 0.125% of 24000 spot) |
| Target | **40 NIFTY points** on underlying (≈ 0.167% of 24000 spot) |
| Time-based exit | **15:25:00 IST** |
| Trailing stop | **OFF** (keep it simple for V1) |

**Note on stop/target:** AlgoTest may want option premium-based stops. Use:
- Stop = 30 NIFTY pts × ITM1 delta(0.7) = **~₹21 per share loss**
- Target = 40 × 0.7 = **~₹28 per share gain**
- For 1 lot of 65: SL = ₹1,365, Target = ₹1,820

---

## 5. Position Sizing

| Field | Value |
|---|---|
| Capital | **₹3,00,000** |
| Lots per trade | **1 lot** (65 shares) |
| Risk per trade | ~₹1,500 (matches your 0.5% risk cap) |

---

## 6. Backtest Period

| Field | Value |
|---|---|
| Start | **2023-01-02** (or earliest AlgoTest allows; use 3 full years) |
| End | **2026-05-15** (today) |
| Brokerage | Use AlgoTest's **"Zerodha"** or **"Upstox"** preset |
| Slippage | **0.5%** of premium (AlgoTest default) |

---

## 7. What to Compare

After AlgoTest finishes, look at:

| Metric | Our Python prediction (12mo costs-applied) | AlgoTest real |
|---|---|---|
| Trades fired | ~20-60 (over 3 years should be 60-180) | ? |
| Win rate | ~55% | ? |
| Avg win in ₹ | ~₹1,820 (target hit) | ? |
| Avg loss in ₹ | ~₹1,365 (stop hit) | ? |
| Total return % | ~5-10% over 3 years on ₹3L | ? |
| Max drawdown | ~₹6-10k | ? |

**The big question we want answered:** When we compute "+0.162R expectancy per trade," does that translate to **₹290 actual rupee profit per trade** (= 0.162 × ₹1,800 R-value), or is reality very different due to option delta drift, theta, and real bid-ask spreads?

---

## 8. Run Order (saves your free credits)

If you have ~3 free credits, use them like this:

**Credit 1**: Skip condition #8 (morning bias). Run with conditions 1-7 only over 3 years. Get baseline.

**Credit 2**: Add condition #8 (morning bias). Compare — does it improve win rate?

**Credit 3** (if available): Try **target 50 pts / stop 30 pts** (1:1.67 RR instead of 1:1.33). See if we should aim wider.

---

## 9. After You Run It

Just **screenshot the summary stats** (trades, win rate, total P&L, max DD, equity curve) and share. I'll compare line-by-line with our Python projection and we'll know:

- ✅ Our R-multiple model is approximately correct → strategy is real
- ⚠️ Reality is WORSE than our projection → cost model needs adjustment + lower expectations
- 🎉 Reality is BETTER → option pricing dynamics actually help us

This is the most rigorous test we can run without paying for institutional data feeds.

---

*Last updated: 2026-05-16*

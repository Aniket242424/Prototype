"""
Market Sentiment Agent — Claude orchestrator with web search + technical levels.

What it does (your spec):
  1. Reads GLOBAL NEWS via Claude's web-search tool (war, Fed, policy, data).
  2. Reads TECHNICAL LEVELS (50/200 EMA, support/resistance, RSI) for
     Dow, Nasdaq, S&P, Nifty, Bitcoin, Gold via the get_technical_levels tool.
  3. Judges momentum drivers (profit booking, rate cut/hike, war, data surprise).
  4. Answers "WHY is the market up/down?" and gives BUY/SELL signals off levels
     (e.g. "Nasdaq holding 50 EMA -> bounce likely").
  5. Self-improves: past predictions + actual outcomes are fed back so it
     calibrates (handled by the feedback log; this runner records each call).

Output: structured JSON sentiment -> data/sentiment_latest.json (+ history).
Runs standalone in the host venv (anthropic + yfinance), like the trading bots.

Run:  python3 scripts/run_sentiment_agent.py
Env:  ANTHROPIC_API_KEY, SENTIMENT_MODEL (default claude-sonnet-4-6)
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
os.chdir(REPO_ROOT)
sys.path.insert(0, str(REPO_ROOT / "src"))
from dotenv import load_dotenv  # noqa: E402
load_dotenv(REPO_ROOT / ".env")

import httpx  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import yfinance as yf  # noqa: E402
import keystore  # noqa: E402  (scripts/ is on sys.path[0])

# UTC hours to send a Telegram briefing even if sentiment didn't flip
# (≈ pre-India-open 03:00 and pre-US-open 13:00 UTC). Comma-list overridable.
BRIEFING_HOURS = {int(h) for h in os.getenv("SENTIMENT_BRIEFING_HOURS", "3,13").split(",") if h.strip()}

BACKEND = os.getenv("SENTIMENT_BACKEND", "gemini").lower()   # "gemini" (free) | "anthropic"
MODEL = os.getenv("SENTIMENT_MODEL", "claude-sonnet-4-6")    # anthropic model
GEMINI_MODEL = os.getenv("SENTIMENT_GEMINI_MODEL", "gemini-2.5-flash")
# Free Gemini models tried in order — if one is overloaded (503), try the next
# (both free, both have Google Search) before ever touching paid Claude.
GEMINI_MODELS = [m.strip() for m in
                 os.getenv("SENTIMENT_GEMINI_MODELS", "gemini-2.5-flash,gemini-2.0-flash").split(",")
                 if m.strip()]
# Stop using the paid Claude fallback once it has cost this much (₹). Gemini
# (free) keeps running; if Gemini is also down we skip the read (memory intact).
ANTHROPIC_BUDGET_INR = float(os.getenv("ANTHROPIC_BUDGET_INR", "100"))
GEMINI_RETRIES = int(os.getenv("SENTIMENT_GEMINI_RETRIES", "3"))
FX = float(os.getenv("DELTA_IC_FX_INR_USD", "84"))
LATEST = Path("data/sentiment_latest.json")
HISTORY = Path("data/sentiment_history.jsonl")

# Assets the agent watches (display -> yfinance ticker)
ASSETS = {
    "Dow Jones": "^DJI",       # spot indices (recognizable values, not futures)
    "Nasdaq 100": "^NDX",      # US Tech 100 (~29,000), matches heavyweight weights
    "S&P 500": "^GSPC",
    "Nifty 50": "^NSEI",
    "Bank Nifty": "^NSEBANK",
    "Bitcoin": "BTC-USD",
    "Gold": "GC=F",
}

# --- Multi-timeframe EMA support config (CRDS: Confluence-Ranked Dynamic Support) ---
_TF_SPECS = [("Daily", "D"), ("Weekly", "W-FRI"), ("Monthly", "ME")]
_SPANS = [20, 50, 200]
_TF_WEIGHT = {"Daily": 0.3, "Weekly": 0.6, "Monthly": 1.0}  # higher TF = defended by bigger size
_HIVOL = {"BTC-USD", "GC=F"}  # wider confluence/near tolerance for volatile assets


# ============================================================
# Technical levels (the get_technical_levels tool)
# ============================================================

def _rsi(close: pd.Series, n: int = 14) -> float:
    # Wilder's RSI (RMA = ewm with alpha=1/n) — this is what TradingView, brokers
    # and charting platforms show. A plain rolling mean diverges by up to ~10 points
    # (e.g. BTC: simple-mean 5.5 vs Wilder 15.2) and would mislead the agent.
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = up / dn.replace(0, 1e-9)
    return float((100 - 100 / (1 + rs)).iloc[-1])


def _ema_level(close: pd.Series, span: int):
    """One EMA value + convergence status + slope, or None if too little history.
    Uses adjust=True (no first-bar seed injection) so a short series like BTC's
    weekly-200 is unbiased — NOT adjust=False (that's only correct for Wilder RSI).
    Publishes only at >=3x span; <3x is returned None ('n/a', never faked)."""
    close = close.dropna()
    n = len(close)
    if n < 3 * span:
        return None, "n/a", "flat"
    e = close.ewm(span=span, adjust=True).mean()
    val = float(e.iloc[-1])
    look = min(10, n - 1)
    prev = float(e.iloc[-1 - look])
    slope = "rising" if val > prev * 1.001 else "falling" if val < prev * 0.999 else "flat"
    status = "converged" if n >= 5 * span else "converging"
    return round(val, 2), status, slope


def _hold_stats(close: pd.Series, ema: pd.Series, hivol: bool, fwd: int = 5) -> dict:
    """How often this EMA HELD as support, historically: 'held H out of N tests'.
    A test = price pulls back from above to touch the EMA (enters a tol band from
    outside). It HELD if, within the next `fwd` bars, price did NOT close decisively
    below the EMA (i.e. it bounced). This is the edge the agent learns: a high
    hold-rate on the nearest support = a real buy-the-dip signal for a retailer.
    Returns {tests, held, rate} (rate None until >=4 tests so we never over-claim)."""
    c = close.dropna()
    e = ema.reindex(c.index)
    cv, ev = c.values.astype(float), e.values.astype(float)
    n = len(cv)
    tol = 0.02 if hivol else 0.01      # 'touched the EMA' band
    brk = 0.02 if hivol else 0.01      # 'closed decisively below' = a break
    if n < fwd + 5:
        return {"tests": 0, "held": 0, "rate": None}
    tests = held = 0
    last = -999
    for j in range(1, n - fwd):
        if np.isnan(ev[j]) or np.isnan(ev[j - 1]):
            continue
        above_before = cv[j - 1] > ev[j - 1] * (1 + tol)        # was clearly above
        touched = ev[j] * (1 - tol) <= cv[j] <= ev[j] * (1 + tol)  # pulled into the EMA
        if above_before and touched and (j - last) > fwd:        # fresh, non-overlapping test
            last = j
            tests += 1
            fut_c, fut_e = cv[j + 1:j + 1 + fwd], ev[j + 1:j + 1 + fwd]
            broke = bool(np.any(fut_c < fut_e * (1 - brk)))      # closed decisively below within fwd bars
            if not broke:
                held += 1
    return {"tests": tests, "held": held, "rate": (round(100 * held / tests) if tests >= 4 else None)}


def _resample_ohlc(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Resample the single daily frame to a higher timeframe and DROP the partial
    current bar (the still-forming week/month) so HTF EMAs read off closed bars only."""
    if rule == "D":
        return df
    agg = {"Open": "first", "High": "max", "Low": "min", "Close": "last"}
    r = df.resample(rule).agg(agg).dropna(how="all")
    if len(r) > 1 and r.index[-1] > df.index[-1]:   # last bucket's right edge is in the future => partial
        r = r.iloc[:-1]
    return r


def _compute_levels(matrix: list, price: float, swing_low: float, hivol: bool,
                    hold_by_label: dict | None = None) -> dict:
    """CRDS: cluster the valid EMAs into confluence bands, score each by
    timeframe-authority + confluence + slope + freshness, and ALWAYS emit a
    nearest support, a structural floor and the controlling resistance.
    Roles are classified by sign(level - price) EVERY run (an EMA is never
    assumed to still be support); below-all-EMAs assets take the no-support path."""
    levels = [{"label": f"{m['tf'][0]}{m['span']}", "tf": m["tf"], "value": m["value"], "slope": m["slope"]}
              for m in matrix if m["value"]]
    tol = 0.010 if hivol else 0.005
    near_tol = 0.03 if hivol else 0.015
    levels.sort(key=lambda x: x["value"])
    bands = []
    for lv in levels:
        if bands and (lv["value"] - bands[-1]["lo"]) / bands[-1]["mid"] <= tol:
            b = bands[-1]; b["members"].append(lv)
            b["lo"] = min(b["lo"], lv["value"]); b["hi"] = max(b["hi"], lv["value"]); b["mid"] = (b["lo"] + b["hi"]) / 2
        else:
            bands.append({"members": [lv], "lo": lv["value"], "hi": lv["value"], "mid": lv["value"]})
    for b in bands:
        tfw = max(_TF_WEIGHT[m["tf"]] for m in b["members"])
        cross = len({m["tf"] for m in b["members"]}) >= 2
        razor = (b["hi"] - b["lo"]) / b["mid"] <= 0.004
        conf = 1.0 if (cross or len(b["members"]) >= 3 or (razor and len(b["members"]) >= 2)) \
            else (0.5 if len(b["members"]) >= 2 else 0.0)
        rising = sum(m["slope"] == "rising" for m in b["members"])
        falling = sum(m["slope"] == "falling" for m in b["members"])
        slope = 1.0 if rising > falling else 0.0 if falling > rising else 0.5
        fresh = 1.0 if abs(b["mid"] / price - 1) <= near_tol else 0.5
        b["strength"] = round(40 * tfw + 30 * conf + 20 * slope + 10 * fresh, 1)
        b["grade"] = "STRONG" if b["strength"] >= 60 else "MODERATE" if b["strength"] >= 40 else "WEAK"
        b["members_str"] = "+".join(m["label"] for m in b["members"])
        rates = [hold_by_label.get(m["label"]) for m in b["members"]] if hold_by_label else []
        rates = [r for r in rates if r is not None]
        b["hold_rate"] = max(rates) if rates else None   # best historical hold-rate in the band

    # 20-day swing low: a guaranteed structural support floor (defined strength).
    sl = {"members_str": "20d swing low", "lo": swing_low, "hi": swing_low, "mid": swing_low,
          "strength": 50.0, "grade": "MODERATE", "hold_rate": None}

    def fmt(b):
        return {"value": round(b["mid"], 2), "pct": round((price / b["mid"] - 1) * 100, 2),
                "strength": b["strength"], "grade": b["grade"], "members": b["members_str"],
                "hold_rate": b.get("hold_rate")}

    below = [b for b in bands if b["hi"] <= price]      # entirely below price = support
    straddle = [b for b in bands if b["lo"] < price < b["hi"]]  # price sits inside band
    above = [b for b in bands if b["lo"] >= price]      # entirely above = resistance
    no_ema_support = not below and not straddle

    cand = sorted(below + straddle, key=lambda b: -b["mid"])  # nearest-below first
    # nearest_support = the literal first line price would hit (any grade — its
    # quality is reported via 'grade'); never skip it. structural_floor surfaces
    # the strongest zone below (where the thesis lives). Together they satisfy the
    # "always state support" + "if it breaks, here's the real floor" contract.
    nearest = cand[0] if cand else sl
    structural = max(below + [sl], key=lambda b: (b["strength"], b["mid"]))
    controlling = min(above, key=lambda b: b["mid"]) if above else None
    return {
        "nearest_support": fmt(nearest),
        "structural_floor": fmt(structural),
        "controlling_resistance": (fmt(controlling) if controlling else None),
        "confluence_zones": [{"value": round(b["mid"], 2), "members": b["members_str"],
                              "strength": b["strength"], "grade": b["grade"]}
                             for b in bands if len(b["members"]) >= 2],
        "no_ema_support": no_ema_support,
    }


def compute_one(ticker: str) -> dict:
    # ONE 'max' daily download -> resample to Weekly/Monthly. This both fixes the
    # EMA200 warmup bug (thousands of bars, not 252) and gives the full 9-EMA
    # multi-timeframe matrix from a single consistent vintage.
    try:
        df = yf.download(ticker, period="max", interval="1d", progress=False, auto_adjust=False)
    except Exception:
        df = yf.download(ticker, period="15y", interval="1d", progress=False, auto_adjust=False)
    if df is None or df.empty:
        return {"error": "no data"}
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] for c in df.columns]
    df = df.dropna(subset=["Close"])
    price = float(df["Close"].iloc[-1])
    hivol = ticker in _HIVOL

    matrix, md, cells, hold_by_label = [], {}, {}, {}
    for tf, rule in _TF_SPECS:
        cc = _resample_ohlc(df, rule)["Close"].dropna()
        for span in _SPANS:
            val, status, slope = _ema_level(cc, span)
            hold = (_hold_stats(cc, cc.ewm(span=span, adjust=True).mean(), hivol)
                    if val else {"tests": 0, "held": 0, "rate": None})
            matrix.append({"tf": tf, "span": span, "value": val, "status": status, "slope": slope})
            md[(tf, span)] = val
            label = f"{tf[0]}{span}"               # D20 / W50 / M200
            hold_by_label[label] = hold["rate"]
            cells[label] = {
                "v": val, "status": status, "slope": slope,
                "role": (None if val is None else ("support" if val <= price else "resistance")),
                "pct": (round((price / val - 1) * 100, 2) if val else None),
                "held": hold["held"], "tests": hold["tests"], "rate": hold["rate"],
            }

    lo20 = float(df["Low"].tail(20).min()); hi20 = float(df["High"].tail(20).max())
    rsi = round(_rsi(df["Close"]), 1)
    lv = _compute_levels(matrix, price, lo20, hivol, hold_by_label)

    ema50, ema200 = md[("Daily", 50)], md[("Daily", 200)]
    d20, d50, d200 = md[("Daily", 20)], md[("Daily", 50)], md[("Daily", 200)]
    stack = ("bullish" if None not in (d20, d50, d200) and d20 > d50 > d200
             else "bearish" if None not in (d20, d50, d200) and d20 < d50 < d200 else "tangled")
    trend = ("uptrend" if ema50 and price > ema50 and (ema200 is None or ema50 > ema200)
             else "downtrend" if ema50 and price < ema50 and (ema200 is None or ema50 < ema200)
             else "sideways")
    chg1 = float((df["Close"].iloc[-1] / df["Close"].iloc[-2] - 1) * 100) if len(df) >= 2 else 0.0
    chg5 = float((df["Close"].iloc[-1] / df["Close"].iloc[-6] - 1) * 100) if len(df) >= 6 else 0.0
    return {
        "price": round(price, 2),
        "ema50": round(ema50, 2) if ema50 else None,
        "ema200": round(ema200, 2) if ema200 else None,
        "pct_from_ema50": round((price / ema50 - 1) * 100, 2) if ema50 else None,
        "rsi14": rsi,
        "support_20d": round(lo20, 2),
        "resistance_20d": round(hi20, 2),
        "chg_1d_pct": round(chg1, 2),
        "chg_5d_pct": round(chg5, 2),
        "trend": trend,
        # --- multi-timeframe (CRDS) ---
        "matrix": cells,   # {label: {v, role, pct, held, tests, rate, slope, status}}
        "stack_daily": stack,
        "nearest_support": lv["nearest_support"],
        "structural_floor": lv["structural_floor"],
        "controlling_resistance": lv["controlling_resistance"],
        "confluence_zones": lv["confluence_zones"],
        "no_ema_support": lv["no_ema_support"],
    }


def all_technicals() -> dict:
    out = {}
    for name, tk in ASSETS.items():
        try:
            out[name] = compute_one(tk)
        except Exception as e:
            out[name] = {"error": str(e)}
    return out


TECHNICAL_TOOL = {
    "name": "get_technical_levels",
    "description": (
        "Get current technical levels for Dow Jones, Nasdaq, S&P 500, Nifty 50, "
        "Bitcoin and Gold: last price, 50 & 200 EMA, % distance from 50 EMA, "
        "RSI(14), 20-day support/resistance, 1-day & 5-day change, and trend. "
        "Call this to ground your read in real price action and key levels."
    ),
    "input_schema": {"type": "object", "properties": {}},
}

# Forcing the final answer through a tool guarantees valid structured output
# (the model reliably emits markdown if asked for "pure JSON").
SUBMIT_TOOL = {
    "name": "submit_sentiment",
    "description": "Submit your FINAL structured market read. Call this exactly once, last, after researching news + levels.",
    "input_schema": {
        "type": "object",
        "properties": {
            "overall": {"type": "string", "enum": ["bullish", "bearish", "neutral"]},
            "confidence": {"type": "integer", "description": "0-100"},
            "why_moving": {"type": "string", "description": "one clear sentence: the dominant driver"},
            "summary": {"type": "string", "description": "2-3 sentence plain-English read"},
            "drivers": {"type": "array", "items": {"type": "string"}, "description": "3-5 concise bullet drivers (<= 15 words each)"},
            "assets": {"type": "array", "items": {"type": "object", "properties": {
                "name": {"type": "string"},
                "bias": {"type": "string"},
                "signal": {"type": "string", "description": "actionable read tied to levels"},
                "support": {"type": "string", "description": "the key technical support level + what it means (ALWAYS required)"},
                "if_breaks": {"type": "string", "description": "what happens if that support breaks, and the ONLY things that could lift it back: a positive-news catalyst or a rally in the index's heavyweight stock (e.g. Reliance for Nifty, Apple/Nvidia for Nasdaq)"}},
                "required": ["name", "bias", "signal", "support", "if_breaks"]}},
            "catalysts_ahead": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["overall", "confidence", "why_moving", "summary", "drivers", "assets", "catalysts_ahead"],
    },
}


# ============================================================
# Agent
# ============================================================
SYSTEM = """You are a veteran market strategist with 20+ YEARS of hands-on experience trading and analysing global equities, indices, commodities and crypto. You have traded through the dot-com bust, the 2008 GFC, the 2013 taper tantrum, COVID 2020, the 2022 rate-hike bear, and every Fed cycle in between. You think in terms of liquidity, positioning, support/resistance that institutions actually defend, sector rotation, and how a few heavyweight stocks move an index. You are calm, decisive, and you have seen every head-fake — you don't panic, you don't chase, and you call it straight.

Produce an honest, evidence-based read of whether markets are leaning UP or DOWN right now, and WHY.

Process:
1. Call get_technical_levels to see real price action / key levels.
2. Use web_search to find CURRENT market-moving news: Fed / rate decisions, inflation & jobs data, war/geopolitics, policy, big tech/earnings, anything driving momentum. Search a few focused queries.
3. Synthesize: overall bias, the single clearest reason markets are moving, per-asset signals tied to technical levels, and catalysts ahead.

RULES for every asset:
- ALWAYS state the key technical SUPPORT level (and resistance where relevant). Markets usually hold support, so name it explicitly.
- ALWAYS state what happens IF that support breaks — and be clear that once support breaks, the ONLY things that can realistically lift the market back up are (a) a fresh POSITIVE news catalyst, or (b) a rally in the index's HEAVYWEIGHT stock (because a few mega-caps can move the whole index).

INDEX HEAVYWEIGHTS you must reason with (approximate weights; web-search if you need current figures):
- NIFTY 50: Reliance ~9%, HDFC Bank ~11%, ICICI Bank ~8%, Infosys ~6%, TCS ~4%, Bharti Airtel, L&T, ITC. Financials ~35%. A Reliance or HDFC Bank rally alone can lift Nifty even on weak breadth.
- BANK NIFTY: HDFC Bank ~28%, ICICI Bank ~24%, SBI ~9%, Axis ~9%, Kotak ~8%. Just HDFC Bank + ICICI Bank = ~52%, so those two stocks essentially DECIDE Bank Nifty's direction.
- NASDAQ-100: Apple ~9%, Microsoft ~8%, Nvidia ~8%, Amazon ~5%, Broadcom ~5%, Meta ~5%, Tesla, Alphabet. Top-7 ~45% — Nvidia/Apple/Microsoft swings dominate.
- S&P 500: the "Magnificent Seven" (Apple, Microsoft, Nvidia, Amazon, Meta, Alphabet, Tesla) ~30% — same mega-caps drive it.
- DOW JONES: price-weighted — high-priced names (Goldman Sachs, UnitedHealth, Microsoft, Home Depot, Caterpillar) carry the most points.
- Use this to say things like "Nifty support 23,800; if it breaks, only a Reliance/HDFC Bank bounce or a positive RBI/global cue can lift it."

Be specific and cite what you saw. Avoid hedging mush. If it's genuinely mixed, say neutral.

Produce a complete, decisive read covering: overall bias (bullish/bearish/neutral) + confidence %, the single dominant driver (why the market is moving — e.g. profit booking / Fed rate cut hopes / hot CPI / war escalation), the key drivers (3-5 bullets), per-asset signals (each with an explicit SUPPORT level and what happens IF it breaks, including which heavyweight stock or positive catalyst could lift it), and the catalysts ahead. Be specific with numbers and levels."""


def run_agent_anthropic() -> dict:
    import anthropic
    key = keystore.get_key("anthropic_api_key", "ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("No Anthropic key set (UI or ANTHROPIC_API_KEY)")
    client = anthropic.Anthropic(api_key=key)
    # Pre-compute technicals and inject them, so the agent always has the levels
    # (and never submits empty per-asset signals for lack of data).
    tech = all_technicals()
    tools = [SUBMIT_TOOL, {"type": "web_search_20250305", "name": "web_search", "max_uses": 6}]
    messages = [{"role": "user", "content": (
        "CURRENT TECHNICAL LEVELS (already computed — use these for support/resistance):\n"
        + json.dumps(tech, indent=2) +
        "\n\nNow web_search for the live market-moving news (Fed, jobs/inflation data, war/"
        "geopolitics, big-tech/chips, policy), then call submit_sentiment. EVERY asset above "
        "must appear in your read with its support level and what happens if support breaks.")}]
    usage = {"input_tokens": 0, "output_tokens": 0, "web_searches": 0}
    parsed = None
    for turn in range(8):
        # Give it room to research; force the structured submission once it's had enough.
        force = turn >= 5
        resp = client.messages.create(
            model=MODEL, max_tokens=5000, system=SYSTEM, tools=tools, messages=messages,
            tool_choice=({"type": "tool", "name": "submit_sentiment"} if force else {"type": "auto"}),
        )
        u = resp.usage
        usage["input_tokens"] += u.input_tokens
        usage["output_tokens"] += u.output_tokens
        if getattr(u, "server_tool_use", None):
            usage["web_searches"] += getattr(u.server_tool_use, "web_search_requests", 0)

        submit = next((b for b in resp.content
                       if getattr(b, "type", None) == "tool_use" and b.name == "submit_sentiment"), None)
        if submit is not None:
            parsed = dict(submit.input)
            break

        # web_search is a SERVER tool: 'pause_turn' means resume; otherwise nudge to submit.
        messages.append({"role": "assistant", "content": resp.content})
        if resp.stop_reason == "pause_turn":
            continue
        messages.append({"role": "user", "content":
                         "Now call submit_sentiment with your final read — every asset with support + if_breaks."})

    if parsed is None:
        parsed = {"overall": "neutral", "confidence": 0, "why_moving": "agent_no_submit",
                  "summary": "Agent did not return a structured read.", "drivers": [],
                  "assets": [], "catalysts_ahead": []}
    cost_usd = usage["input_tokens"] / 1e6 * 3.0 + usage["output_tokens"] / 1e6 * 15.0
    tech = all_technicals()
    parsed = _enrich_with_technicals(parsed, tech)   # real levels override LLM numbers
    parsed["_meta"] = {
        "as_of": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model": MODEL, "backend": "anthropic",
        "tokens_in": usage["input_tokens"], "tokens_out": usage["output_tokens"],
        "web_searches": usage["web_searches"],
        "cost_usd": round(cost_usd, 4), "cost_inr": round(cost_usd * FX, 2),
    }
    parsed["_technicals"] = tech
    return parsed


def _extract_json(text: str) -> dict:
    text = text.strip()
    i, j = text.find("{"), text.rfind("}")
    if i >= 0 and j > i:
        try:
            return json.loads(text[i:j + 1])
        except json.JSONDecodeError:
            pass
    return {"overall": "neutral", "confidence": 0, "why_moving": "parse_error",
            "summary": text[:300], "drivers": [], "assets": [], "catalysts_ahead": []}


# ============================================================
# Store + report
# ============================================================

def store(read: dict) -> None:
    LATEST.parent.mkdir(parents=True, exist_ok=True)
    LATEST.write_text(json.dumps(read, indent=2))
    with open(HISTORY, "a", encoding="utf-8") as f:
        f.write(json.dumps(read) + "\n")


def report(r: dict) -> None:
    m = r.get("_meta", {})
    print("=" * 70)
    print(f"MARKET SENTIMENT — {r.get('overall', '?').upper()}  ({r.get('confidence', 0)}% confidence)")
    print("=" * 70)
    print(f"  Why moving: {r.get('why_moving', '')}")
    print(f"  {r.get('summary', '')}")
    print("  Drivers:")
    for d in r.get("drivers", []):
        print(f"    - {d}")
    print("  Per-asset signals:")
    for a in r.get("assets", []):
        print(f"    {a.get('name', ''):12s} [{a.get('bias', '')}]  {a.get('signal', '')}")
        if a.get("support"):
            print(f"        support: {a.get('support')}")
        if a.get("if_breaks"):
            print(f"        if breaks: {a.get('if_breaks')}")
    print("  Catalysts ahead:")
    for c in r.get("catalysts_ahead", []):
        print(f"    - {c}")
    print(f"  [model={m.get('model')} · {m.get('web_searches')} searches · "
          f"{m.get('tokens_in')}+{m.get('tokens_out')} tok · ₹{m.get('cost_inr')}]")


# ============================================================
# Gemini backend (FREE) — Google Search grounding + structured output
# ============================================================
from typing import Literal  # noqa: E402
from pydantic import BaseModel  # noqa: E402

Bias = Literal["bullish", "bearish", "neutral"]


class _Asset(BaseModel):
    name: str
    bias: Bias
    signal: str
    support: str
    if_breaks: str


class _Sentiment(BaseModel):
    overall: Bias
    confidence: int
    why_moving: str
    summary: str
    drivers: list[str]
    assets: list[_Asset]
    catalysts_ahead: list[str]


# Order matters: Bank Nifty must come BEFORE Nifty 50 (so "bank nifty" isn't
# mis-matched to Nifty 50 by the shared word "nifty").
_ALIASES = {
    "Dow Jones": ["dow", "djia"],
    "Nasdaq 100": ["nasdaq", "ndx", "100"],
    "S&P 500": ["s&p", "spx", "gspc", "500"],
    "Bank Nifty": ["bank nifty", "banknifty", "nifty bank", "nsebank", "bank"],
    "Nifty 50": ["nifty 50", "nifty50", "nifty"],
    "Bitcoin": ["bitcoin", "btc"],
    "Gold": ["gold", "xau"],
}


def _enrich_with_technicals(parsed: dict, tech: dict) -> dict:
    """
    Overwrite each asset's NUMERIC levels (price, support) with the REAL computed
    values from yfinance, so the LLM can never display a hallucinated number.
    The LLM keeps only its qualitative bias/signal/if_breaks (its judgement).
    """
    for a in parsed.get("assets", []):
        n = (a.get("name") or "").lower()
        key = next((k for k, al in _ALIASES.items() if k in tech and any(x in n for x in al)), None)
        t = tech.get(key) if key else None
        if t and isinstance(t, dict) and "price" in t:
            a["name"] = key
            a["price"] = t["price"]
            a["trend"] = t["trend"]
            a["rsi"] = t["rsi14"]
            ns, sf, cr = t.get("nearest_support"), t.get("structural_floor"), t.get("controlling_resistance")
            if t.get("no_ema_support") and ns:
                # below EVERY EMA (e.g. Bitcoin breakdown): no dynamic support — the
                # nearest EMA is overhead RESISTANCE; only floor is the prior swing low.
                a["support"] = (f"NO EMA support — below all EMAs. Structural floor "
                                f"{ns['value']:,.0f} (20d low, {ns['pct']:+.1f}%)"
                                + (f"; nearest EMA {cr['value']:,.0f} is RESISTANCE ({cr['pct']:+.1f}%)" if cr else ""))
            elif ns:
                hr = f", held {ns['hold_rate']}% of tests" if ns.get("hold_rate") is not None else ""
                a["support"] = f"{ns['value']:,.0f} ({ns['members']}, {ns['pct']:+.1f}%, {ns['grade']}{hr})"
                if sf and sf["value"] != ns["value"]:
                    a["support"] += f" · floor {sf['value']:,.0f} ({sf['members']}, {sf['grade']})"
            if cr and not t.get("no_ema_support"):
                a["resistance"] = f"{cr['value']:,.0f} ({cr['members']}, {cr['pct']:+.1f}%)"
            a["levels_matrix"] = t.get("matrix")
            a["confluence_zones"] = t.get("confluence_zones")
            a["nearest_members"] = ns.get("members") if ns else None
            a["no_ema_support"] = t.get("no_ema_support")
    return parsed


def _gemini_generate(client, **kw):
    """generate_content with retry/backoff on transient overload (503/429) —
    keeps runs on the FREE backend instead of falling over to paid Claude."""
    last = None
    for attempt in range(GEMINI_RETRIES):
        try:
            return client.models.generate_content(**kw)
        except Exception as e:
            last = e
            s = str(e).lower()
            if any(t in s for t in ("503", "unavailable", "overloaded", "429", "resource_exhausted")):
                time.sleep(2 * (attempt + 1)); continue
            raise
    raise last


def run_agent_gemini_chain() -> dict:
    """Try every (Gemini key × model) combo in turn — rotate across the user's
    Gmail keys and free models. Only raise if ALL fail (then dispatcher → Claude)."""
    keys = keystore.get_gemini_keys()
    if not keys:
        raise RuntimeError("No Gemini key set (UI or GEMINI_API_KEY)")
    models = GEMINI_MODELS or [GEMINI_MODEL]
    last = None
    for ki, key in enumerate(keys):
        for m in models:
            try:
                return run_agent_gemini(m, key)
            except Exception as e:
                last = e
                print(f"  [gemini key#{ki + 1} {m} failed: {str(e)[:110]}]")
    raise last if last else RuntimeError("no gemini key/model available")


def run_agent_gemini(model: str | None = None, api_key: str | None = None) -> dict:
    from google import genai
    from google.genai import types

    model = model or (GEMINI_MODELS[0] if GEMINI_MODELS else GEMINI_MODEL)
    key = api_key or keystore.get_key("gemini_api_key", "GEMINI_API_KEY")
    if not key:
        raise RuntimeError("No Gemini key set (UI or GEMINI_API_KEY)")
    client = genai.Client(api_key=key)
    tech = all_technicals()

    # Step 1 — research with Google Search grounding -> veteran analysis (text).
    research = (
        "CURRENT TECHNICAL LEVELS (use these for support/resistance):\n"
        + json.dumps(tech, indent=2) +
        "\n\nUse Google Search to find the live market-moving news (Fed/rates, jobs & inflation "
        "data, war/geopolitics, big-tech/chips, policy), then write your complete market read per "
        "your instructions. Cover every asset above with its support level and what happens if it breaks."
    )
    r1 = _gemini_generate(
        client, model=model, contents=research,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM,
            tools=[types.Tool(google_search=types.GoogleSearch())],
            temperature=0.3, max_output_tokens=6000,
        ),
    )
    analysis = (r1.text or "").strip()

    # GROUNDING GUARD: if Google Search did NOT fire, the model is answering from
    # stale training memory (the source of the 38,000-Dow hallucination). Reject
    # so the dispatcher fails over to Claude (which has reliable web search).
    grounded = False
    try:
        gm = r1.candidates[0].grounding_metadata
        grounded = bool(gm and (getattr(gm, "web_search_queries", None)
                                or getattr(gm, "grounding_chunks", None)))
    except Exception:
        grounded = False
    if not grounded:
        raise RuntimeError("Gemini answer was NOT grounded (no web search) — failing over to Claude")

    # Step 2 — structure the analysis into strict JSON (no tools).
    r2 = _gemini_generate(
        client, model=model,
        contents=("Convert this market analysis into the required JSON. Keep it faithful. "
                  "Include ALL SEVEN assets (Dow Jones, Nasdaq 100, S&P 500, Nifty 50, Bank Nifty, "
                  "Bitcoin, Gold), each with support + if_breaks. For every asset, 'bias' must be "
                  "EXACTLY one of: bullish, bearish, neutral (never 'uptrend'/'downtrend').\n\n" + analysis),
        config=types.GenerateContentConfig(
            response_mime_type="application/json", response_schema=_Sentiment,
            temperature=0.0, max_output_tokens=8000,
        ),
    )
    parsed = json.loads(r2.text)
    parsed = _enrich_with_technicals(parsed, tech)   # real levels override any LLM numbers

    def _toks(r):
        m = getattr(r, "usage_metadata", None)
        return ((getattr(m, "prompt_token_count", 0) or 0),
                (getattr(m, "candidates_token_count", 0) or 0))
    i1, o1 = _toks(r1); i2, o2 = _toks(r2)
    parsed["_meta"] = {
        "as_of": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model": model, "backend": "gemini",
        "gemini_key": keystore.mask_key(key),   # which key was active this run
        "tokens_in": i1 + i2, "tokens_out": o1 + o2,
        "web_searches": "google-grounded",
        "cost_usd": 0.0, "cost_inr": 0.0,   # free tier
    }
    parsed["_technicals"] = tech
    return parsed


def run_agent() -> dict:
    """
    Run the primary backend; if it fails for ANY reason (rate limit, outage,
    bad key, parse error), automatically fall back to the other one. If both
    fail, return a safe neutral read — the system must never crash.
    """
    order = ([("gemini", run_agent_gemini_chain), ("anthropic", run_agent_anthropic)]
             if BACKEND != "anthropic" else
             [("anthropic", run_agent_anthropic), ("gemini", run_agent_gemini_chain)])
    errors = []
    for i, (name, fn) in enumerate(order):
        # Budget gate: stop using paid Claude once it has cost ANTHROPIC_BUDGET_INR.
        if name == "anthropic" and keystore.cost_so_far("anthropic") >= ANTHROPIC_BUDGET_INR:
            msg = (f"anthropic: budget hit (₹{keystore.cost_so_far('anthropic'):.0f} "
                   f">= ₹{ANTHROPIC_BUDGET_INR:.0f}) — skipped to stay free")
            errors.append(msg); print(f"  [{msg}]")
            continue
        try:
            read = fn()
            if i > 0:  # we fell back
                read.setdefault("_meta", {})["fell_back_from"] = order[0][0]
                read.setdefault("_meta", {})["fallback_reason"] = errors[-1] if errors else ""
            return read
        except Exception as e:
            msg = f"{name}: {type(e).__name__}: {str(e)[:200]}"
            errors.append(msg)
            print(f"  [backend {name} FAILED -> trying fallback] {msg}")
    # both backends failed — degrade gracefully, do not crash
    return {
        "overall": "neutral", "confidence": 0,
        "why_moving": "both LLM backends unavailable",
        "summary": "Sentiment agent could not run (both Gemini and Claude failed). "
                   "Last errors: " + " | ".join(errors),
        "drivers": [], "assets": [], "catalysts_ahead": [],
        "_meta": {"as_of": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                  "backend": "none", "error": True, "errors": errors,
                  "tokens_in": 0, "tokens_out": 0, "cost_inr": 0.0},
        "_technicals": all_technicals(),
    }


def _tesc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def send_telegram(text: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN"); chat = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return
    try:
        httpx.post(f"https://api.telegram.org/bot{token}/sendMessage",
                   json={"chat_id": chat, "text": text, "parse_mode": "HTML",
                         "disable_web_page_preview": True}, timeout=10)
    except Exception as e:
        print("telegram send failed:", e)


def format_telegram(r: dict, flipped: bool) -> str:
    o = (r.get("overall") or "neutral").upper()
    try:
        c = float(r.get("confidence") or 0)
    except (TypeError, ValueError):
        c = 0.0
    emoji = {"BULLISH": "🟢", "BEARISH": "🔴", "NEUTRAL": "⚪"}.get(o, "")
    head = "⚠️ SENTIMENT FLIPPED → " if flipped else "🧭 Market Sentiment: "
    lines = [f"<b>{head}{o}</b> {emoji} ({c:.0f}%)",
             f"<i>{_tesc(r.get('why_moving', ''))}</i>", ""]
    for a in r.get("assets", [])[:6]:
        sup = _tesc(a.get("support", ""))
        sup = (sup[:60] + "…") if len(sup) > 60 else sup
        lines.append(f"• <b>{_tesc(a.get('name'))}</b> [{a.get('bias')}] — supp {sup}")
    cats = r.get("catalysts_ahead", [])
    if cats:
        lines.append("\n📅 " + _tesc("; ".join(str(x) for x in cats[:2])))
    m = r.get("_meta", {})
    lines.append(f"\n<code>{m.get('backend', '?')} · ₹{m.get('cost_inr', 0)}</code> · 43.204.64.180:8002")
    return "\n".join(lines)


def _prev_overall() -> str | None:
    if LATEST.exists():
        try:
            return (json.loads(LATEST.read_text()).get("overall") or "").lower() or None
        except Exception:
            return None
    return None


def main() -> None:
    notify_flag = "--notify" in sys.argv
    prev = _prev_overall()
    print(f"Running market sentiment agent (backend={BACKEND}, "
          f"model={GEMINI_MODEL if BACKEND == 'gemini' else MODEL})...")
    read = run_agent()
    store(read)
    report(read)

    # Record token usage per backend (for the UI + Anthropic budget cap).
    m = read.get("_meta", {})
    if m.get("backend") in ("gemini", "anthropic"):
        keystore.record_usage(m["backend"], m.get("tokens_in", 0),
                              m.get("tokens_out", 0), m.get("cost_inr", 0))

    new_overall = (read.get("overall") or "neutral").lower()
    flipped = prev is not None and prev != new_overall
    hour = datetime.now(timezone.utc).hour
    if notify_flag or flipped or hour in BRIEFING_HOURS:
        send_telegram(format_telegram(read, flipped))
        print(f"  [telegram sent — {'flip' if flipped else 'briefing/notify'}]")


if __name__ == "__main__":
    main()

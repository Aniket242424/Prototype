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
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
os.chdir(REPO_ROOT)
sys.path.insert(0, str(REPO_ROOT / "src"))
from dotenv import load_dotenv  # noqa: E402
load_dotenv(REPO_ROOT / ".env")

import anthropic  # noqa: E402
import pandas as pd  # noqa: E402
import yfinance as yf  # noqa: E402

MODEL = os.getenv("SENTIMENT_MODEL", "claude-sonnet-4-6")
FX = float(os.getenv("DELTA_IC_FX_INR_USD", "84"))
LATEST = Path("data/sentiment_latest.json")
HISTORY = Path("data/sentiment_history.jsonl")

# Assets the agent watches (display -> yfinance ticker)
ASSETS = {
    "Dow Jones": "YM=F",
    "Nasdaq": "NQ=F",
    "S&P 500": "ES=F",
    "Nifty 50": "^NSEI",     # Gift Nifty proxy (NSE spot)
    "Bitcoin": "BTC-USD",
    "Gold": "GC=F",
}


# ============================================================
# Technical levels (the get_technical_levels tool)
# ============================================================

def _rsi(close: pd.Series, n: int = 14) -> float:
    d = close.diff()
    up = d.clip(lower=0).rolling(n).mean()
    dn = (-d.clip(upper=0)).rolling(n).mean()
    rs = up / dn.replace(0, 1e-9)
    return float((100 - 100 / (1 + rs)).iloc[-1])


def compute_one(ticker: str) -> dict:
    df = yf.download(ticker, period="1y", interval="1d", progress=False, auto_adjust=False)
    if df.empty:
        return {"error": "no data"}
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] for c in df.columns]
    c = df["Close"].dropna()
    ema50 = c.ewm(span=50, adjust=False).mean().iloc[-1]
    ema200 = c.ewm(span=200, adjust=False).mean().iloc[-1] if len(c) >= 200 else float("nan")
    price = float(c.iloc[-1])
    hi20 = float(df["High"].tail(20).max()); lo20 = float(df["Low"].tail(20).min())
    chg1 = float((c.iloc[-1] / c.iloc[-2] - 1) * 100) if len(c) >= 2 else 0.0
    chg5 = float((c.iloc[-1] / c.iloc[-6] - 1) * 100) if len(c) >= 6 else 0.0
    trend = ("uptrend" if price > ema50 and (pd.isna(ema200) or ema50 > ema200)
             else "downtrend" if price < ema50 and (pd.isna(ema200) or ema50 < ema200)
             else "sideways")
    return {
        "price": round(price, 2),
        "ema50": round(float(ema50), 2),
        "ema200": None if pd.isna(ema200) else round(float(ema200), 2),
        "pct_from_ema50": round((price / float(ema50) - 1) * 100, 2),
        "rsi14": round(_rsi(c), 1),
        "support_20d": round(lo20, 2),
        "resistance_20d": round(hi20, 2),
        "chg_1d_pct": round(chg1, 2),
        "chg_5d_pct": round(chg5, 2),
        "trend": trend,
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
- NASDAQ-100: Apple ~9%, Microsoft ~8%, Nvidia ~8%, Amazon ~5%, Broadcom ~5%, Meta ~5%, Tesla, Alphabet. Top-7 ~45% — Nvidia/Apple/Microsoft swings dominate.
- S&P 500: the "Magnificent Seven" (Apple, Microsoft, Nvidia, Amazon, Meta, Alphabet, Tesla) ~30% — same mega-caps drive it.
- DOW JONES: price-weighted — high-priced names (Goldman Sachs, UnitedHealth, Microsoft, Home Depot, Caterpillar) carry the most points.
- Use this to say things like "Nifty support 23,800; if it breaks, only a Reliance/HDFC Bank bounce or a positive RBI/global cue can lift it."

Be specific and cite what you saw. Avoid hedging mush. If it's genuinely mixed, say neutral.

When you have done your research, FINISH by calling the submit_sentiment tool with your final structured read (every asset MUST include support + if_breaks). Do not write a prose report — the submit_sentiment call IS your answer."""


def run_agent() -> dict:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
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
    parsed["_meta"] = {
        "as_of": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model": MODEL,
        "tokens_in": usage["input_tokens"], "tokens_out": usage["output_tokens"],
        "web_searches": usage["web_searches"],
        "cost_usd": round(cost_usd, 4), "cost_inr": round(cost_usd * FX, 2),
    }
    parsed["_technicals"] = all_technicals()
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


def main() -> None:
    print(f"Running market sentiment agent (model={MODEL})...")
    read = run_agent()
    store(read)
    report(read)


if __name__ == "__main__":
    main()

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

import httpx  # noqa: E402
import pandas as pd  # noqa: E402
import yfinance as yf  # noqa: E402
import keystore  # noqa: E402  (scripts/ is on sys.path[0])

# UTC hours to send a Telegram briefing even if sentiment didn't flip
# (≈ pre-India-open 03:00 and pre-US-open 13:00 UTC). Comma-list overridable.
BRIEFING_HOURS = {int(h) for h in os.getenv("SENTIMENT_BRIEFING_HOURS", "3,13").split(",") if h.strip()}

BACKEND = os.getenv("SENTIMENT_BACKEND", "gemini").lower()   # "gemini" (free) | "anthropic"
MODEL = os.getenv("SENTIMENT_MODEL", "claude-sonnet-4-6")    # anthropic model
GEMINI_MODEL = os.getenv("SENTIMENT_GEMINI_MODEL", "gemini-2.5-flash")
FX = float(os.getenv("DELTA_IC_FX_INR_USD", "84"))
LATEST = Path("data/sentiment_latest.json")
HISTORY = Path("data/sentiment_history.jsonl")

# Assets the agent watches (display -> yfinance ticker)
ASSETS = {
    "Dow Jones": "^DJI",     # spot indices (recognizable values, not futures)
    "Nasdaq": "^IXIC",
    "S&P 500": "^GSPC",
    "Nifty 50": "^NSEI",
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


_ALIASES = {
    "Dow Jones": ["dow", "djia"],
    "Nasdaq": ["nasdaq", "ndx", "ixic", "comp"],
    "S&P 500": ["s&p", "spx", "gspc", "500"],
    "Nifty 50": ["nifty"],
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
            a["support"] = f"{t['support_20d']:,.0f} (20-day low) · 50 EMA {t['ema50']:,.0f}"
    return parsed


def run_agent_gemini() -> dict:
    from google import genai
    from google.genai import types

    key = keystore.get_key("gemini_api_key", "GEMINI_API_KEY")
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
    r1 = client.models.generate_content(
        model=GEMINI_MODEL, contents=research,
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
    r2 = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=("Convert this market analysis into the required JSON. Keep it faithful. "
                  "Include ALL SIX assets (Dow Jones, Nasdaq, S&P 500, Nifty 50, Bitcoin, Gold), "
                  "each with support + if_breaks. For every asset, 'bias' must be EXACTLY one of: "
                  "bullish, bearish, neutral (never 'uptrend'/'downtrend').\n\n" + analysis),
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
        "model": GEMINI_MODEL, "backend": "gemini",
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
    order = ([("gemini", run_agent_gemini), ("anthropic", run_agent_anthropic)]
             if BACKEND != "anthropic" else
             [("anthropic", run_agent_anthropic), ("gemini", run_agent_gemini)])
    errors = []
    for i, (name, fn) in enumerate(order):
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

    new_overall = (read.get("overall") or "neutral").lower()
    flipped = prev is not None and prev != new_overall
    hour = datetime.now(timezone.utc).hour
    if notify_flag or flipped or hour in BRIEFING_HOURS:
        send_telegram(format_telegram(read, flipped))
        print(f"  [telegram sent — {'flip' if flipped else 'briefing/notify'}]")


if __name__ == "__main__":
    main()

"""
Market state tool — fetches overnight + pre-market price snapshots.

The agent calls this tool to understand the overnight backdrop:
- GIFT NIFTY (Indian futures trading in Singapore overnight) — ~95% predictive of NIFTY gap
- US closing prints (S&P, NASDAQ, Dow) — leading indicator
- Asian markets in progress (Nikkei, Hang Seng, KOSPI) — sympathy effect
- USD/INR, crude (Brent), DXY — macro context
- India VIX previous close

All data via Yahoo Finance's public chart API (no key, no rate-limit issues
for one call/day).
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from decimal import Decimal

import httpx

from trading_agent.core.logging import get_logger
from trading_agent.core.time_utils import IST

log = get_logger(__name__)


# ============================================================
# Anthropic tool definition
# ============================================================

TOOL_DEFINITION = {
    "name": "get_market_state",
    "description": (
        "Returns overnight + pre-market price snapshots for indicators that "
        "predict NIFTY's open. Includes GIFT NIFTY (the strongest lead "
        "signal), US indices (S&P, NASDAQ, Dow), Asian markets in progress "
        "(Nikkei, Hang Seng, KOSPI), USD/INR, Brent crude, DXY, and India "
        "VIX previous close. Use this to gauge directional bias before "
        "reading news headlines."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "include_categories": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": ["gift_nifty", "us", "asia", "macro", "vix"],
                },
                "description": (
                    "Which categories to fetch. Default: all. Use a subset "
                    "if you only need specific signals (e.g. ['gift_nifty'] "
                    "on a quiet day)."
                ),
                "default": ["gift_nifty", "us", "asia", "macro", "vix"],
            },
        },
    },
}


# ============================================================
# Yahoo Finance symbol map per category
# ============================================================

_SYMBOLS: dict[str, list[tuple[str, str, str]]] = {
    # category -> [(yahoo_symbol, label, relevance_to_nifty), ...]
    "gift_nifty": [
        # GIFT NIFTY (formerly SGX NIFTY) trades on NSE IFSC in Gujarat now.
        # Yahoo ticker: GIFTNIFTY (limited availability) — fallback to ^NSEI lookahead.
        ("GIFTNIFTY", "GIFT NIFTY (overnight futures)", "LEAD — ~95% predictive of NIFTY gap"),
    ],
    "us": [
        ("^GSPC", "S&P 500", "LEAD — ~70% directional correlation"),
        ("^IXIC", "NASDAQ Composite", "LEAD — drives Indian IT sector open"),
        ("^DJI", "Dow Jones", "CONTEXT — risk sentiment"),
    ],
    "asia": [
        ("^N225", "Nikkei 225", "SYMPATHY — strong"),
        ("^HSI", "Hang Seng", "SYMPATHY — moderate"),
        ("^KS11", "KOSPI", "SYMPATHY — moderate"),
    ],
    "macro": [
        ("INR=X", "USD/INR", "CONTEXT — FII flow proxy"),
        ("BZ=F", "Brent Crude", "CONTEXT — energy stocks, inflation"),
        ("DX-Y.NYB", "US Dollar Index (DXY)", "CONTEXT — EM flow direction"),
    ],
    "vix": [
        ("^INDIAVIX", "India VIX (prev close)", "CONTEXT — fear gauge"),
    ],
}


# ============================================================
# Yahoo Finance fetcher
# ============================================================

_YAHOO_BASE = "https://query1.finance.yahoo.com/v8/finance/chart"


async def _fetch_one(client: httpx.AsyncClient, symbol: str) -> dict | None:
    """Fetch the latest quote for one Yahoo symbol. Returns None on error."""
    url = f"{_YAHOO_BASE}/{symbol}"
    params = {"interval": "1d", "range": "5d"}
    headers = {"User-Agent": "Mozilla/5.0 (trading_agent premarket-briefing)"}
    try:
        r = await client.get(url, params=params, headers=headers, timeout=10.0)
    except Exception as e:
        log.warning("premarket.market_state.fetch_failed", symbol=symbol, error=str(e))
        return None
    if r.status_code != 200:
        log.warning(
            "premarket.market_state.non200",
            symbol=symbol,
            status=r.status_code,
            body=r.text[:200],
        )
        return None
    try:
        result = r.json()["chart"]["result"][0]
        meta = result["meta"]
        regular_close = meta.get("regularMarketPrice")
        previous_close = meta.get("chartPreviousClose") or meta.get("previousClose")
        ts = meta.get("regularMarketTime")
        if regular_close is None or previous_close is None:
            return None
        change_pct = float((regular_close - previous_close) / previous_close * 100.0)
        return {
            "symbol": symbol,
            "close": str(regular_close),
            "previous_close": str(previous_close),
            "change_pct": round(change_pct, 3),
            "last_update_unix": ts,
        }
    except (KeyError, IndexError, TypeError, ZeroDivisionError) as e:
        log.warning("premarket.market_state.parse_failed", symbol=symbol, error=str(e))
        return None


async def get_market_state(
    include_categories: list[str] | None = None,
) -> dict:
    """
    Tool implementation: fetch overnight market snapshots and return a
    structured summary.

    Returns:
        {
            "ok": True,
            "captured_at": ISO-8601 IST,
            "indicators": [
                {symbol, label, close, change_pct, last_update, relevance_to_nifty},
                ...
            ],
            "summary": "GIFT NIFTY +0.8%, US closed flat, Asia mixed",
            "failed_symbols": [...],  # any symbols that didn't return data
        }
    """
    if include_categories is None or not include_categories:
        include_categories = list(_SYMBOLS.keys())

    plan: list[tuple[str, str, str]] = []
    for cat in include_categories:
        if cat in _SYMBOLS:
            plan.extend(_SYMBOLS[cat])
        else:
            log.warning("premarket.market_state.unknown_category", category=cat)

    if not plan:
        return {"ok": False, "error": "No valid categories requested"}

    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(
            *(_fetch_one(client, sym) for sym, _, _ in plan),
            return_exceptions=False,
        )

    indicators = []
    failed = []
    for (sym, label, relevance), res in zip(plan, results):
        if res is None:
            failed.append(sym)
            continue
        ts_iso = (
            datetime.fromtimestamp(res["last_update_unix"], tz=IST).isoformat()
            if res["last_update_unix"]
            else None
        )
        indicators.append({
            "symbol": sym,
            "label": label,
            "close": res["close"],
            "change_pct": res["change_pct"],
            "last_update": ts_iso,
            "relevance_to_nifty": relevance,
        })

    summary = _build_summary(indicators)

    return {
        "ok": True,
        "captured_at": datetime.now(IST).isoformat(timespec="seconds"),
        "indicators": indicators,
        "summary": summary,
        "failed_symbols": failed,
    }


def _build_summary(indicators: list[dict]) -> str:
    """One-line human-readable summary for the agent's first glance."""
    parts: list[str] = []
    for ind in indicators:
        sign = "+" if ind["change_pct"] >= 0 else ""
        parts.append(f"{ind['label']} {sign}{ind['change_pct']:.2f}%")
    return " | ".join(parts) if parts else "no market data"

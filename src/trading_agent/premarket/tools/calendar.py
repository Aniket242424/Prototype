"""
Calendar tool — exposes scheduled events to the pre-market agent.

The agent calls this tool to discover what's happening today + the next N days.
Reads from config/premarket_calendar.yaml (curated, point-in-time accurate),
plus derives weekly NIFTY expiries (every Tuesday after Apr 2025 reform).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import yaml
from pydantic import ValidationError

from trading_agent.core.config import CONFIG_DIR
from trading_agent.core.logging import get_logger
from trading_agent.premarket.dtos import CalendarEvent, Impact

log = get_logger(__name__)


# ============================================================
# Anthropic tool definition (what Claude sees)
# ============================================================

TOOL_DEFINITION = {
    "name": "get_calendar_events",
    "description": (
        "Returns scheduled high-impact events affecting Indian equity markets "
        "within a date window. Use this FIRST to understand what kind of day "
        "it is. Includes RBI policy meetings, US Fed FOMC decisions, Union "
        "Budget, weekly NIFTY expiries. Returns empty list if nothing "
        "scheduled."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "from_date": {
                "type": "string",
                "format": "date",
                "description": "Start of window (YYYY-MM-DD). Use today's date unless looking ahead.",
            },
            "to_date": {
                "type": "string",
                "format": "date",
                "description": "End of window (YYYY-MM-DD), inclusive. For just-today queries, use the same date as from_date.",
            },
            "min_impact": {
                "type": "string",
                "enum": ["LOW", "MEDIUM", "HIGH", "EXTREME"],
                "description": "Only return events at or above this impact level. Default MEDIUM.",
                "default": "MEDIUM",
            },
        },
        "required": ["from_date", "to_date"],
    },
}


# ============================================================
# Implementation
# ============================================================

_CALENDAR_FILE = CONFIG_DIR / "premarket_calendar.yaml"
_IMPACT_RANK = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "EXTREME": 3}


def _load_yaml_events() -> list[CalendarEvent]:
    """Load curated events from the YAML file. Cached after first call."""
    if not _CALENDAR_FILE.exists():
        log.warning("premarket.calendar.yaml_missing", path=str(_CALENDAR_FILE))
        return []
    raw = yaml.safe_load(_CALENDAR_FILE.read_text(encoding="utf-8")) or {}
    events: list[CalendarEvent] = []
    for item in raw.get("events", []):
        try:
            events.append(CalendarEvent(**item))
        except ValidationError as e:
            log.warning("premarket.calendar.bad_entry", item=item, error=str(e))
    return events


def _derive_nifty_expiries(start: date, end: date) -> list[CalendarEvent]:
    """
    NIFTY weekly expiry moved to Tuesday in April 2025.
    Generate one CalendarEvent per Tuesday in the window.
    BANKNIFTY weekly expiries were discontinued by SEBI Nov 2024 — not included.
    """
    out: list[CalendarEvent] = []
    cur = start
    while cur <= end:
        if cur.weekday() == 1:  # Tuesday
            out.append(CalendarEvent(
                date=cur,
                event_type="nifty_expiry",
                title="NIFTY weekly options expiry",
                impact=Impact.MEDIUM,
                market_relevance="NIFTY — elevated intraday volatility, option premium decay",
                notes="Tuesday expiry per Apr 2025 NSE reform",
            ))
        cur += timedelta(days=1)
    return out


async def get_calendar_events(
    from_date: str,
    to_date: str,
    min_impact: str = "MEDIUM",
) -> dict:
    """
    Tool implementation: return events in [from_date, to_date] at or above
    min_impact. Always JSON-serializable.

    Returns:
        {
            "ok": True,
            "events": [ {date, event_type, title, impact, market_relevance, notes}, ... ],
            "count": N
        }
        OR on error:
        {"ok": False, "error": "..."}
    """
    try:
        start = datetime.strptime(from_date, "%Y-%m-%d").date()
        end = datetime.strptime(to_date, "%Y-%m-%d").date()
    except ValueError as e:
        return {"ok": False, "error": f"Invalid date format: {e}"}

    if start > end:
        return {"ok": False, "error": f"from_date {from_date} is after to_date {to_date}"}

    min_rank = _IMPACT_RANK.get(min_impact.upper(), 1)

    yaml_events = _load_yaml_events()
    expiry_events = _derive_nifty_expiries(start, end)
    all_events = yaml_events + expiry_events

    in_window = [
        e for e in all_events
        if start <= e.date <= end
        and _IMPACT_RANK[e.impact.value] >= min_rank
    ]
    in_window.sort(key=lambda e: (e.date, -_IMPACT_RANK[e.impact.value]))

    return {
        "ok": True,
        "count": len(in_window),
        "events": [
            {
                "date": e.date.isoformat(),
                "event_type": e.event_type,
                "title": e.title,
                "impact": e.impact.value,
                "market_relevance": e.market_relevance,
                "notes": e.notes,
            }
            for e in in_window
        ],
    }

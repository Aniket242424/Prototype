"""
Pre-market briefing worker — Phase 7.1.

Runs the agent once daily at 08:30 IST, persists the result to Postgres,
and pushes a formatted summary to Telegram. Sleeps until the next 08:30
between runs.

On startup, if today is a weekday and we're between 08:30 and the NSE
open (09:15) AND no briefing exists for today yet, it runs immediately
(catch-up on restart).
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, time, timedelta

from trading_agent.core.logging import get_logger
from trading_agent.core.time_utils import IST, now_ist
from trading_agent.infrastructure.db import session_scope
from trading_agent.monitoring.telegram_alerter import alert as telegram_alert
from trading_agent.premarket.agent import run_briefing_agent
from trading_agent.premarket.dtos import Impact, PremarketBriefing, Sentiment
from trading_agent.premarket.storage import (
    load_briefing,
    save_briefing,
)

log = get_logger(__name__)


# ============================================================
# Schedule
# ============================================================

BRIEFING_HOUR = 8
BRIEFING_MINUTE = 30


def _next_run_time(now: datetime | None = None) -> datetime:
    """Return the next 08:30 IST datetime (skipping weekends)."""
    now = now or now_ist()
    target = now.replace(hour=BRIEFING_HOUR, minute=BRIEFING_MINUTE, second=0, microsecond=0)
    if now >= target:
        target = target + timedelta(days=1)
    # Skip Saturday + Sunday — Indian markets closed
    while target.weekday() >= 5:
        target = target + timedelta(days=1)
    return target


def _should_run_now_on_startup(now: datetime | None = None) -> bool:
    """
    True if it's a weekday + after 08:30 IST + before 09:15 IST.
    Useful for restart-during-window catch-up.
    """
    now = now or now_ist()
    if now.weekday() >= 5:
        return False
    if now.time() < time(BRIEFING_HOUR, BRIEFING_MINUTE):
        return False
    if now.time() >= time(9, 15):
        return False
    return True


# ============================================================
# Telegram formatter
# ============================================================

_SENTIMENT_EMOJI = {
    Sentiment.STRONG_BULL: "🚀",
    Sentiment.BULL: "🟢",
    Sentiment.NEUTRAL: "⚪",
    Sentiment.BEAR: "🔴",
    Sentiment.STRONG_BEAR: "📉",
}

_IMPACT_EMOJI = {
    Impact.LOW: "🟢",
    Impact.MEDIUM: "🟡",
    Impact.HIGH: "🟠",
    Impact.EXTREME: "🔴",
}


def _format_briefing_for_telegram(b: PremarketBriefing) -> str:
    """Build an HTML message for Telegram."""
    se = _SENTIMENT_EMOJI.get(b.sentiment, "")
    ie = _IMPACT_EMOJI.get(b.overall_impact, "")
    lines = [
        f"<b>🌅 Pre-market briefing — {b.briefing_date.isoformat()}</b>",
        "",
        f"<b>Call:</b> {se} {b.sentiment.value} (conviction {b.conviction:.2f})",
        f"<b>Impact:</b> {ie} {b.overall_impact.value}",
        f"<b>Position size:</b> {b.position_size_multiplier:.2f}× normal",
    ]
    if b.skip_trading:
        lines.append("<b>⚠️ Recommendation:</b> <code>SKIP TRADING TODAY</code>")
    lines.extend([
        f"<b>NIFTY bias:</b> {b.nifty_bias.value}",
        f"<b>BANKNIFTY bias:</b> {b.banknifty_bias.value}",
    ])
    if b.intraday_phases:
        lines.append("\n<b>Intraday phases:</b>")
        for window, bias in b.intraday_phases.items():
            lines.append(f"  • <code>{window}</code> → {bias}")
    if b.headlines_summary:
        lines.append(f"\n<b>Setup:</b> {b.headlines_summary}")
    lines.append(f"\n<b>Rationale:</b> {b.rationale}")
    lines.append(
        f"\n<i>Agent: tools={','.join(b.tools_used)}, "
        f"tokens={b.tokens_used}, cost=₹{b.cost_inr:.2f}</i>"
    )
    return "\n".join(lines)


# ============================================================
# One-shot briefing pipeline
# ============================================================

async def run_once(target_date: date | None = None) -> PremarketBriefing:
    """Run the agent, persist the briefing, push to Telegram. Returns the briefing."""
    target_date = target_date or now_ist().date()
    log.info("premarket.worker.running", date=target_date.isoformat())

    briefing = await run_briefing_agent(briefing_date=target_date)

    async with session_scope() as session:
        await save_briefing(session, briefing)

    msg = _format_briefing_for_telegram(briefing)
    sent = await telegram_alert(
        "info",
        msg,
        dedup_key=f"premarket_{target_date.isoformat()}",
    )
    log.info(
        "premarket.worker.briefing_done",
        date=target_date.isoformat(),
        sentiment=briefing.sentiment.value,
        telegram_sent=sent,
    )
    return briefing


# ============================================================
# Main scheduled loop
# ============================================================

async def briefing_loop() -> None:
    """
    Forever-loop: sleep until next 08:30 IST, run, repeat.
    On startup, if we're already inside today's window AND no briefing
    exists for today, run immediately.
    """
    log.info("premarket.worker.starting")

    # Startup catch-up
    today = now_ist().date()
    if _should_run_now_on_startup():
        async with session_scope() as session:
            existing = await load_briefing(session, today)
        if existing is None:
            log.info("premarket.worker.startup_catch_up", date=today.isoformat())
            try:
                await run_once(today)
            except Exception:
                log.exception("premarket.worker.catch_up_failed")
        else:
            log.info(
                "premarket.worker.skipping_catch_up",
                reason="briefing already exists",
                date=today.isoformat(),
            )

    while True:
        next_run = _next_run_time()
        sleep_sec = (next_run - now_ist()).total_seconds()
        log.info(
            "premarket.worker.sleeping_until_next_run",
            next_run=next_run.isoformat(),
            sleep_seconds=round(sleep_sec),
        )
        try:
            await asyncio.sleep(sleep_sec)
        except asyncio.CancelledError:
            log.info("premarket.worker.cancelled")
            raise

        run_date = now_ist().date()
        try:
            await run_once(run_date)
        except Exception:
            log.exception("premarket.worker.run_failed", date=run_date.isoformat())
            # Don't crash the loop; sleep a minute then continue to next scheduled run
            await asyncio.sleep(60)

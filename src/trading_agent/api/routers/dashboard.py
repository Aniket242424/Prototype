"""
Phase 0/1 status dashboard.

Two endpoints:
- GET /dashboard            — single-page HTML view (auto-refreshes every 2s)
- GET /dashboard/api/status — JSON aggregate consumed by the page

Intentionally minimal. Phase 6 replaces this with a proper monitoring UI.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse

from trading_agent.api.auth_basic import verify_credentials
from sqlalchemy import func, select

from trading_agent.core.config import REPO_ROOT, get_instruments_config, get_settings
from trading_agent.core.kill_switch import KillSwitch
from trading_agent.core.time_utils import IST, is_market_open, now_ist
from trading_agent.infrastructure.db import session_scope
from trading_agent.infrastructure.models import (
    AcknowledgmentLogRow,
    AiDecisionRow,
    ExecutionRow,
    IndiaVixRow,
    MarketDataTickRow,
    OpportunityRow,
    OptionsChainSnapshotRow,
    OrderRow,
    PositionRow,
    RegimeStateRow,
    RiskDecisionRow,
    SlippageLogRow,
    StrategySignalRow,
    TokenRow,
)
from trading_agent.infrastructure.redis_client import make_redis

router = APIRouter(tags=["dashboard"], dependencies=[Depends(verify_credentials)])


async def _agent_budgets_summary() -> list[dict]:
    """All configured per-agent LLM token budgets (Phase 7.1.5+)."""
    try:
        from trading_agent.ai.budget import list_budgets
        states = await list_budgets()
        return [
            {
                "agent_name": b.agent_name,
                "allowance": b.allowance,
                "consumed": b.consumed,
                "remaining": b.remaining,
                "refilled_at": b.refilled_at.isoformat(),
                "exhausted": b.exhausted,
            }
            for b in states
        ]
    except Exception:
        return []


async def _llm_usage_summary() -> dict[str, Any]:
    """Last-7-days LLM usage aggregated for the dashboard widget."""
    try:
        from trading_agent.ai.usage import usage_summary
        return await usage_summary(days=7)
    except Exception:
        return {"window_days": 7, "total": {"calls": 0, "tokens": 0, "cost_inr": 0.0, "failures": 0}, "by_agent": []}


async def _premarket_briefing_summary() -> dict[str, Any]:
    """Latest pre-market briefing (Phase 7.1). None if no briefing stored yet."""
    try:
        from trading_agent.premarket.storage import load_latest_briefing_via_scope
        b = await load_latest_briefing_via_scope()
    except Exception:
        return {"available": False, "error": "load_failed"}
    if b is None:
        return {"available": False}
    return {
        "available": True,
        "briefing_date": b.briefing_date.isoformat(),
        "generated_at": b.generated_at.isoformat(),
        "sentiment": b.sentiment.value,
        "conviction": round(b.conviction, 2),
        "overall_impact": b.overall_impact.value,
        "position_size_multiplier": round(b.position_size_multiplier, 2),
        "skip_trading": b.skip_trading,
        "nifty_bias": b.nifty_bias.value,
        "banknifty_bias": b.banknifty_bias.value,
        "intraday_phases": b.intraday_phases,
        "headlines_summary": b.headlines_summary,
        "rationale": b.rationale,
        "tools_used": b.tools_used,
        "tokens_used": b.tokens_used,
        "cost_inr": round(b.cost_inr, 2),
    }


async def _vix_summary() -> dict[str, Any]:
    try:
        async with session_scope() as session:
            row = (
                await session.execute(
                    select(IndiaVixRow).order_by(IndiaVixRow.ts.desc()).limit(1)
                )
            ).scalar_one_or_none()
            today_start_utc = now_ist().replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
            count_today = (
                await session.execute(
                    select(func.count())
                    .select_from(IndiaVixRow)
                    .where(IndiaVixRow.ts >= today_start_utc)
                )
            ).scalar() or 0
        return {
            "latest": float(row.value) if row else None,
            "latest_at": row.ts.isoformat() if row else None,
            "rows_today": int(count_today),
        }
    except Exception:
        return {"latest": None, "latest_at": None, "rows_today": 0}


async def _regime_summary(redis) -> dict[str, Any]:
    """Latest regime state per underlying, from Redis cache (regime:{name}:latest)."""
    out: dict[str, Any] = {}
    for u in ("NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX", "BANKEX"):
        v = await redis.get(f"regime:{u}:latest")
        if v is None:
            out[u] = None
            continue
        try:
            import orjson
            out[u] = orjson.loads(v)
        except Exception:
            out[u] = None
    return out


async def _risk_decisions_summary() -> dict[str, Any]:
    """Last 10 risk decisions + today's approve/reject counts."""
    try:
        async with session_scope() as session:
            today_start_utc = now_ist().replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
            recent = (await session.execute(
                select(RiskDecisionRow)
                .order_by(RiskDecisionRow.ts.desc())
                .limit(10)
            )).scalars().all()
            approvals = (await session.execute(
                select(func.count(RiskDecisionRow.id))
                .where(RiskDecisionRow.approved.is_(True))
                .where(RiskDecisionRow.ts >= today_start_utc)
            )).scalar() or 0
            rejections = (await session.execute(
                select(func.count(RiskDecisionRow.id))
                .where(RiskDecisionRow.approved.is_(False))
                .where(RiskDecisionRow.ts >= today_start_utc)
            )).scalar() or 0
        return {
            "approvals_today": int(approvals),
            "rejections_today": int(rejections),
            "recent": [
                {
                    "ts": r.ts.isoformat() if r.ts else None,
                    "approved": r.approved,
                    "code": r.code,
                    "reason": r.reason[:80] if r.reason else "",
                    "sized_qty": r.sized_qty,
                    "max_premium": float(r.max_premium) if r.max_premium is not None else None,
                }
                for r in recent
            ],
        }
    except Exception:
        return {"approvals_today": 0, "rejections_today": 0, "recent": []}


async def _orders_summary() -> dict[str, Any]:
    """Last 10 orders + summary counts. Includes both paper and live (when Phase 6 lands)."""
    try:
        async with session_scope() as session:
            today_start_utc = now_ist().replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
            recent = (await session.execute(
                select(OrderRow)
                .order_by(OrderRow.id.desc())
                .limit(10)
            )).scalars().all()
            filled_today = (await session.execute(
                select(func.count(OrderRow.id))
                .where(OrderRow.status == "FILLED")
                .where(OrderRow.created_at >= today_start_utc)
            )).scalar() or 0
        return {
            "filled_today": int(filled_today),
            "recent": [
                {
                    "id": o.id,
                    "instrument": o.instrument_key,
                    "side": o.side,
                    "type": o.order_type,
                    "qty": o.qty,
                    "limit": float(o.limit_price) if o.limit_price else None,
                    "status": o.status,
                    "is_paper": o.is_paper,
                    "created_at": o.created_at.isoformat() if o.created_at else None,
                }
                for o in recent
            ],
        }
    except Exception:
        return {"filled_today": 0, "recent": []}


async def _advisor_summary() -> dict[str, Any]:
    """Last 10 AI advisor decisions + today's veto count."""
    try:
        async with session_scope() as session:
            today_start_utc = now_ist().replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
            recent = (await session.execute(
                select(AiDecisionRow)
                .order_by(AiDecisionRow.ts.desc())
                .limit(10)
            )).scalars().all()
            calls_today = (await session.execute(
                select(func.count(AiDecisionRow.id))
                .where(AiDecisionRow.ts >= today_start_utc)
            )).scalar() or 0
            vetoes_today = (await session.execute(
                select(func.count(AiDecisionRow.id))
                .where(AiDecisionRow.decision == "NO_TRADE")
                .where(AiDecisionRow.ts >= today_start_utc)
            )).scalar() or 0
        return {
            "calls_today": int(calls_today),
            "vetoes_today": int(vetoes_today),
            "recent": [
                {
                    "ts": r.ts.isoformat() if r.ts else None,
                    "decision": r.decision,
                    "advisor_score": float(r.advisor_score),
                    "confidence": float(r.confidence),
                    "rationale": (r.rationale or "")[:120],
                    "model": r.model,
                }
                for r in recent
            ],
        }
    except Exception:
        return {"calls_today": 0, "vetoes_today": 0, "recent": []}


async def _positions_summary() -> dict[str, Any]:
    """Open positions + today's closed positions with PnL."""
    try:
        async with session_scope() as session:
            today_start_utc = now_ist().replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
            open_rows = (await session.execute(
                select(PositionRow)
                .where(PositionRow.is_open.is_(True))
                .order_by(PositionRow.opened_at.desc())
            )).scalars().all()
            closed_today = (await session.execute(
                select(PositionRow)
                .where(PositionRow.closed_at >= today_start_utc)
                .order_by(PositionRow.closed_at.desc())
                .limit(10)
            )).scalars().all()
        return {
            "open_count": len(open_rows),
            "open": [
                {
                    "id": p.id,
                    "underlying": p.underlying,
                    "direction": p.direction,
                    "qty": p.qty,
                    "entry_premium": float(p.avg_entry_price),
                    "initial_stop": float(p.initial_stop) if p.initial_stop else None,
                    "target": float(p.target) if p.target else None,
                    "is_paper": p.is_paper,
                    "opened_at": p.opened_at.isoformat() if p.opened_at else None,
                }
                for p in open_rows
            ],
            "closed_today": [
                {
                    "id": p.id,
                    "underlying": p.underlying,
                    "direction": p.direction,
                    "pnl_inr": float(p.pnl_inr) if p.pnl_inr is not None else None,
                    "closed_at": p.closed_at.isoformat() if p.closed_at else None,
                }
                for p in closed_today
            ],
        }
    except Exception:
        return {"open_count": 0, "open": [], "closed_today": []}


async def _strategy_signals_summary() -> dict[str, Any]:
    """Last 10 strategy signals + today's emission count by strategy."""
    try:
        async with session_scope() as session:
            today_start_utc = now_ist().replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
            recent = (await session.execute(
                select(StrategySignalRow)
                .order_by(StrategySignalRow.ts.desc())
                .limit(10)
            )).scalars().all()
            counts = (await session.execute(
                select(StrategySignalRow.strategy_name, func.count(StrategySignalRow.id))
                .where(StrategySignalRow.ts >= today_start_utc)
                .group_by(StrategySignalRow.strategy_name)
            )).all()
        return {
            "signals_today_by_strategy": {name: int(c) for name, c in counts},
            "recent": [
                {
                    "ts": r.ts.isoformat() if r.ts else None,
                    "strategy": r.strategy_name,
                    "intent": r.intent,
                }
                for r in recent
            ],
        }
    except Exception:
        return {"signals_today_by_strategy": {}, "recent": []}


async def _slippage_summary() -> dict[str, Any]:
    """Last 10 slippage rows + avg drift today (realized vs estimated)."""
    try:
        async with session_scope() as session:
            today_start_utc = now_ist().replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
            recent = (await session.execute(
                select(SlippageLogRow)
                .order_by(SlippageLogRow.ts.desc())
                .limit(10)
            )).scalars().all()
            stats = (await session.execute(
                select(
                    func.avg(SlippageLogRow.realized_slippage_bps),
                    func.avg(SlippageLogRow.estimated_slippage_bps),
                    func.count(SlippageLogRow.id),
                )
                .where(SlippageLogRow.ts >= today_start_utc)
            )).first()
            avg_realized = float(stats[0]) if stats and stats[0] is not None else None
            avg_estimated = float(stats[1]) if stats and stats[1] is not None else None
            count = int(stats[2]) if stats and stats[2] is not None else 0
        return {
            "fills_today": count,
            "avg_realized_bps": avg_realized,
            "avg_estimated_bps": avg_estimated,
            "drift_bps": (avg_realized - avg_estimated) if (avg_realized is not None and avg_estimated is not None) else None,
            "recent": [
                {
                    "order_id": r.order_id,
                    "ts": r.ts.isoformat() if r.ts else None,
                    "reference_mid": float(r.reference_mid) if r.reference_mid is not None else None,
                    "estimated_bps": float(r.estimated_slippage_bps),
                    "realized_bps": float(r.realized_slippage_bps),
                    "spread_bps": float(r.spread_bps_at_entry),
                }
                for r in recent
            ],
        }
    except Exception:
        return {"fills_today": 0, "avg_realized_bps": None, "avg_estimated_bps": None, "drift_bps": None, "recent": []}


async def _intel_summary(redis) -> dict[str, Any]:
    """Latest options intel per underlying, from Redis cache (intel:{name}:latest)."""
    out: dict[str, Any] = {}
    for u in ("NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX", "BANKEX"):
        v = await redis.get(f"intel:{u}:latest")
        if v is None:
            out[u] = None
            continue
        try:
            import orjson
            out[u] = orjson.loads(v)
        except Exception:
            out[u] = None
    return out


async def _opportunity_summary(redis) -> dict[str, Any]:
    """Active top opportunity (if any) and ranking of all underlyings."""
    import orjson
    active_raw = await redis.get("opportunity:active")
    ranking_raw = await redis.get("opportunity:ranking")
    return {
        "active": orjson.loads(active_raw) if active_raw else None,
        "ranking": orjson.loads(ranking_raw) if ranking_raw else [],
    }


async def _chain_summary() -> dict[str, Any]:
    try:
        async with session_scope() as session:
            today_start_utc = now_ist().replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
            counts = (await session.execute(
                select(OptionsChainSnapshotRow.underlying, func.count())
                .where(OptionsChainSnapshotRow.ts >= today_start_utc)
                .group_by(OptionsChainSnapshotRow.underlying)
            )).all()
            latest_per_underlying = (await session.execute(
                select(OptionsChainSnapshotRow.underlying, func.max(OptionsChainSnapshotRow.ts))
                .group_by(OptionsChainSnapshotRow.underlying)
            )).all()
        return {
            "snapshots_today_by_underlying": {u: int(c) for u, c in counts},
            "latest_snapshot_at_by_underlying": {
                u: t.isoformat() for u, t in latest_per_underlying
            },
            "total_snapshots_today": sum(int(c) for _, c in counts),
        }
    except Exception:
        return {"snapshots_today_by_underlying": {}, "latest_snapshot_at_by_underlying": {}, "total_snapshots_today": 0}

_DASHBOARD_HTML_PATH = Path(__file__).parent / "dashboard.html"


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page() -> HTMLResponse:
    if not _DASHBOARD_HTML_PATH.exists():
        return HTMLResponse(
            "<h1>dashboard.html not found</h1><p>It must live next to dashboard.py.</p>",
            status_code=500,
        )
    # Re-read on each request so HTML edits don't require a server restart.
    return HTMLResponse(_DASHBOARD_HTML_PATH.read_text(encoding="utf-8"))


@router.get("/dashboard/api/status")
async def dashboard_status() -> dict[str, Any]:
    settings = get_settings()
    instruments_cfg = get_instruments_config()

    redis = make_redis()
    try:
        # --- Infra ---
        try:
            await redis.ping()
            redis_ok = True
        except Exception:
            redis_ok = False

        try:
            async with session_scope() as s:
                await s.execute(select(func.now()))
                pg_ok = True
        except Exception:
            pg_ok = False

        worker_hb_raw = await redis.get("worker:market_data:heartbeat") if redis_ok else None
        worker_alive = worker_hb_raw is not None
        worker_last_hb = worker_hb_raw.decode() if worker_hb_raw else None

        # --- Kill switch ---
        ks = KillSwitch(redis)
        ks_state = await ks.state() if redis_ok else None

        # --- Auth ---
        token_state: dict[str, Any] = {"present": False, "valid": False}
        if pg_ok:
            async with session_scope() as session:
                row = (await session.execute(select(TokenRow).limit(1))).scalar_one_or_none()
                if row is not None:
                    issued = row.issued_at
                    cutoff_today = now_ist().replace(hour=3, minute=30, second=0, microsecond=0)
                    if now_ist() < cutoff_today:
                        cutoff_today -= timedelta(days=1)
                    valid = issued.astimezone(IST) >= cutoff_today
                    next_expiry = (
                        cutoff_today + timedelta(days=1)
                        if now_ist() >= cutoff_today
                        else cutoff_today
                    )
                    token_state = {
                        "present": True,
                        "valid": valid,
                        "user_id": row.user_id,
                        "issued_at": issued.astimezone(IST).isoformat(),
                        "expires_at": next_expiry.isoformat(),
                        "seconds_to_expiry": int((next_expiry - now_ist()).total_seconds()),
                    }

        # --- Live-trading 3-lock ---
        ack_path = REPO_ROOT / "ACKNOWLEDGMENT.md"
        lock1 = settings.live_trading
        lock2 = ack_path.exists()
        lock3 = False
        if lock2 and pg_ok:
            sha = hashlib.sha256(ack_path.read_bytes()).hexdigest()
            async with session_scope() as session:
                latest = (
                    await session.execute(
                        select(AcknowledgmentLogRow)
                        .where(AcknowledgmentLogRow.revoked_at.is_(None))
                        .order_by(AcknowledgmentLogRow.signed_at.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                lock3 = bool(latest and latest.file_sha256 == sha)

        # --- Market data per instrument ---
        instruments_state: list[dict[str, Any]] = []
        total_today = 0
        if pg_ok:
            today_start_ist = now_ist().replace(hour=0, minute=0, second=0, microsecond=0)
            today_start_utc = today_start_ist.astimezone(timezone.utc)
            for inst in instruments_cfg.instruments:
                async with session_scope() as session:
                    row = (
                        await session.execute(
                            select(MarketDataTickRow)
                            .where(MarketDataTickRow.instrument_key == inst.upstox_instrument_key)
                            .order_by(MarketDataTickRow.ts.desc())
                            .limit(1)
                        )
                    ).scalar_one_or_none()
                    count_today = (
                        await session.execute(
                            select(func.count(MarketDataTickRow.id))
                            .where(MarketDataTickRow.instrument_key == inst.upstox_instrument_key)
                            .where(MarketDataTickRow.ts >= today_start_utc)
                        )
                    ).scalar() or 0
                last_tick_redis = (
                    (await redis.get(f"md:last_tick_ts:{inst.upstox_instrument_key}"))
                    if redis_ok
                    else None
                )
                last_tick_at = last_tick_redis.decode() if last_tick_redis else None
                is_stale = True
                if last_tick_at is not None:
                    last_dt = datetime.fromisoformat(last_tick_at)
                    is_stale = (now_ist() - last_dt) > timedelta(seconds=5)
                instruments_state.append({
                    "name": inst.name,
                    "instrument_key": inst.upstox_instrument_key,
                    "exchange": inst.exchange,
                    "lot_size": inst.lot_size,
                    "enabled": inst.enabled,
                    "last_ltp": float(row.ltp) if row else None,
                    "last_tick_at": last_tick_at,
                    "is_stale": is_stale,
                    "ticks_today": int(count_today),
                })
                total_today += int(count_today)

        # --- Trading state ---
        trading: dict[str, Any] = {
            "open_positions": 0,
            "trades_today": 0,
            "realized_pnl_today_inr": 0.0,
            "risk_rejections_today": 0,
            "risk_approvals_today": 0,
        }
        if pg_ok:
            today_start_ist = now_ist().replace(hour=0, minute=0, second=0, microsecond=0)
            today_start_utc = today_start_ist.astimezone(timezone.utc)
            async with session_scope() as session:
                trading["open_positions"] = (
                    await session.execute(
                        select(func.count(PositionRow.id)).where(PositionRow.is_open.is_(True))
                    )
                ).scalar() or 0
                trading["trades_today"] = (
                    await session.execute(
                        select(func.count(PositionRow.id)).where(
                            PositionRow.opened_at >= today_start_utc
                        )
                    )
                ).scalar() or 0
                pnl = (
                    await session.execute(
                        select(func.coalesce(func.sum(PositionRow.pnl_inr), 0)).where(
                            PositionRow.closed_at >= today_start_utc
                        )
                    )
                ).scalar() or 0
                trading["realized_pnl_today_inr"] = float(pnl)
                trading["risk_approvals_today"] = (
                    await session.execute(
                        select(func.count(RiskDecisionRow.id)).where(
                            RiskDecisionRow.approved.is_(True),
                            RiskDecisionRow.ts >= today_start_utc,
                        )
                    )
                ).scalar() or 0
                trading["risk_rejections_today"] = (
                    await session.execute(
                        select(func.count(RiskDecisionRow.id)).where(
                            RiskDecisionRow.approved.is_(False),
                            RiskDecisionRow.ts >= today_start_utc,
                        )
                    )
                ).scalar() or 0

        return {
            "system": {
                "ist_now": now_ist().isoformat(),
                "market_open": is_market_open(),
                "app_env": settings.app_env,
                "trading_capital_inr": settings.trading_capital_inr,
            },
            "infra": {
                "postgres": "healthy" if pg_ok else "down",
                "redis": "healthy" if redis_ok else "down",
                "market_data_worker": "running" if worker_alive else "stopped",
                "market_data_worker_last_heartbeat": worker_last_hb,
            },
            "auth": token_state,
            "live_trading": {
                "authorized": lock1 and lock2 and lock3,
                "locks": {
                    "env_LIVE_TRADING": lock1,
                    "file_present": lock2,
                    "db_sha_matches": lock3,
                },
            },
            "kill_switch": {
                "tripped": ks_state.tripped if ks_state else None,
                "reason": ks_state.reason if ks_state else None,
                "tripped_at": ks_state.tripped_at.isoformat() if ks_state and ks_state.tripped_at else None,
            },
            "market_data": {
                "instruments": instruments_state,
                "total_ticks_today": total_today,
                "india_vix": await _vix_summary(),
                "chain_snapshots": await _chain_summary(),
            },
            "regime": await _regime_summary(redis) if redis_ok else {},
            "intel": await _intel_summary(redis) if redis_ok else {},
            "opportunity": await _opportunity_summary(redis) if redis_ok else {"active": None, "ranking": []},
            "regime_worker_alive": bool(await redis.get("worker:regime:heartbeat")) if redis_ok else False,
            "trading": trading,
            "risk": await _risk_decisions_summary(),
            "orders": await _orders_summary(),
            "slippage": await _slippage_summary(),
            "advisor": await _advisor_summary(),
            "positions": await _positions_summary(),
            "strategy_signals": await _strategy_signals_summary(),
            "phase4_worker_alive": bool(await redis.get("worker:phase4:heartbeat")) if redis_ok else False,
            "premarket_briefing": await _premarket_briefing_summary(),
            "llm_usage": await _llm_usage_summary(),
            "agent_budgets": await _agent_budgets_summary(),
            "phases": {
                "phase_0_scaffold": "completed",
                "phase_1_1_market_data": "completed",
                "phase_1_2_chain_and_vix": "completed",
                "phase_2_regime_and_opportunity": "completed",
                "phase_3_1_risk_engine": "completed",
                "phase_3_2_execution_engine": "completed",
                "phase_4_1_strategy_framework": "completed",
                "phase_4_2_orb_and_registry": "completed",
                "phase_4_3_vol_gap_strategies": "completed",
                "phase_4_4_position_manager": "completed",
                "phase_4_5_ai_advisor": "completed",
                "phase_4_6_worker_orchestrator": "completed",
                "phase_5_backtesting": "completed",
                "phase_6_1_telegram_alerter_and_watcher": "completed",
                "phase_6_2_telegram_token_refresh": "completed",
                "phase_7_1_premarket_briefing_agent": "completed",
                "phase_7_2_intraday_anomaly_news": "pending",
            },
        }
    finally:
        await redis.aclose()

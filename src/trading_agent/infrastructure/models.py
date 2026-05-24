"""
SQLAlchemy ORM models — Phase 0 schema.

Design notes:
- Provenance tables (regime_states, opportunities, ai_decisions, risk_decisions)
  store the FULL inputs that produced each decision so trades can be replayed
  and audited.
- audit_log is append-only narrative; per-decision tables are append-only
  structured.
- market_data_ticks is created here as a plain table for Phase 0; Phase 1
  evaluates TimescaleDB conversion if ingestion proves IO-bound.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# ============================================================
# Identity & live-trading authorization
# ============================================================

class UserRow(Base):
    __tablename__ = "users"

    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    display_name: Mapped[str | None] = mapped_column(String(128))
    email: Mapped[str | None] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TokenRow(Base):
    __tablename__ = "tokens"

    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    broker: Mapped[str] = mapped_column(String(32), default="UPSTOX")
    access_token_encrypted: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    user_name: Mapped[str | None] = mapped_column(String(128))
    email: Mapped[str | None] = mapped_column(String(256))
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AcknowledgmentLogRow(Base):
    """Live-trading lock #3 — operator-signed acknowledgment of risks."""
    __tablename__ = "acknowledgment_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    file_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    file_text: Mapped[str] = mapped_column(Text, nullable=False)
    capital_at_signing_inr: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    signed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ============================================================
# Instruments
# ============================================================

class InstrumentRow(Base):
    __tablename__ = "instruments"

    instrument_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    exchange: Mapped[str] = mapped_column(String(16), nullable=False)
    lot_size: Mapped[int] = mapped_column(Integer, nullable=False)
    tick_size: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    expiry_weekday: Mapped[int] = mapped_column(Integer, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


# ============================================================
# Market data
# ============================================================

class MarketDataTickRow(Base):
    __tablename__ = "market_data_ticks"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    instrument_key: Mapped[str] = mapped_column(String(64), nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ltp: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    bid: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    ask: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    bid_qty: Mapped[int | None] = mapped_column(Integer)
    ask_qty: Mapped[int | None] = mapped_column(Integer)
    volume: Mapped[int | None] = mapped_column(BigInteger)
    oi: Mapped[int | None] = mapped_column(BigInteger)

    __table_args__ = (
        Index("ix_ticks_instrument_ts", "instrument_key", "ts"),
    )


class OptionsChainSnapshotRow(Base):
    __tablename__ = "options_chain_snapshots"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    underlying: Mapped[str] = mapped_column(String(16), nullable=False)
    expiry: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    underlying_spot: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    chain: Mapped[dict] = mapped_column(JSONB, nullable=False)  # strikes[] with full enrichment

    __table_args__ = (
        Index("ix_chain_underlying_ts", "underlying", "ts"),
    )


class IndiaVixRow(Base):
    __tablename__ = "india_vix"

    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    value: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)


# ============================================================
# Decision provenance
# ============================================================

class RegimeStateRow(Base):
    __tablename__ = "regime_states"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    underlying: Mapped[str] = mapped_column(String(16), nullable=False)
    regime: Mapped[str] = mapped_column(String(32), nullable=False)
    confidence: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False)
    components: Mapped[dict] = mapped_column(JSONB, nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("ix_regime_underlying_ts", "underlying", "ts"),
    )


class OpportunityRow(Base):
    __tablename__ = "opportunities"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    underlying: Mapped[str] = mapped_column(String(16), nullable=False)
    direction: Mapped[str] = mapped_column(String(8), nullable=False)  # LONG / SHORT
    score: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False)
    components: Mapped[dict] = mapped_column(JSONB, nullable=False)
    recommended_expiry: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    recommended_strike_band: Mapped[dict | None] = mapped_column(JSONB)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AiDecisionRow(Base):
    __tablename__ = "ai_decisions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    opportunity_id: Mapped[int | None] = mapped_column(
        ForeignKey("opportunities.id"), index=True
    )
    decision: Mapped[str] = mapped_column(String(16), nullable=False)  # CALL / PUT / NO_TRADE
    confidence: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False)
    advisor_score: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    warnings: Mapped[list] = mapped_column(JSONB, default=list)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    raw_response: Mapped[dict] = mapped_column(JSONB, nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class StrategySignalRow(Base):
    __tablename__ = "strategy_signals"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    opportunity_id: Mapped[int | None] = mapped_column(ForeignKey("opportunities.id"), index=True)
    strategy_name: Mapped[str] = mapped_column(String(64), nullable=False)
    intent: Mapped[dict] = mapped_column(JSONB, nullable=False)  # full TradeIntent payload
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RiskDecisionRow(Base):
    __tablename__ = "risk_decisions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("strategy_signals.id"), index=True)
    approved: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    sized_qty: Mapped[int | None] = mapped_column(Integer)
    max_premium: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    inputs_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ============================================================
# Order lifecycle
# ============================================================

class OrderRow(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    risk_decision_id: Mapped[int | None] = mapped_column(
        ForeignKey("risk_decisions.id"), index=True
    )
    broker_order_id: Mapped[str | None] = mapped_column(String(64), index=True)
    instrument_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    side: Mapped[str] = mapped_column(String(8), nullable=False)  # BUY / SELL
    order_type: Mapped[str] = mapped_column(String(16), nullable=False)
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    limit_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="NEW")
    is_paper: Mapped[bool] = mapped_column(Boolean, nullable=False)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    executions: Mapped[list[ExecutionRow]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )


class ExecutionRow(Base):
    __tablename__ = "executions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), nullable=False, index=True)
    fill_qty: Mapped[int] = mapped_column(Integer, nullable=False)
    fill_price: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    fee: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    order: Mapped[OrderRow] = relationship(back_populates="executions")


class PositionRow(Base):
    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    instrument_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    underlying: Mapped[str] = mapped_column(String(16), nullable=False)
    direction: Mapped[str] = mapped_column(String(8), nullable=False)  # LONG only in Phase 0
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    avg_entry_price: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    avg_exit_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    initial_stop: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    target: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    pnl_inr: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    is_open: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    is_paper: Mapped[bool] = mapped_column(Boolean, nullable=False)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)


class SlippageLogRow(Base):
    __tablename__ = "slippage_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), nullable=False, index=True)
    reference_mid: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    estimated_slippage_bps: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    realized_slippage_bps: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    spread_bps_at_entry: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PnlDailyRow(Base):
    __tablename__ = "pnl_daily"

    trading_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    realized_pnl_inr: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=0)
    unrealized_pnl_inr: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=0)
    trades_count: Mapped[int] = mapped_column(Integer, default=0)
    wins: Mapped[int] = mapped_column(Integer, default=0)
    losses: Mapped[int] = mapped_column(Integer, default=0)
    fees_inr: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=0)
    is_paper: Mapped[bool] = mapped_column(Boolean, nullable=False)


class KillSwitchEventRow(Base):
    __tablename__ = "kill_switch_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event: Mapped[str] = mapped_column(String(16), nullable=False)  # trip / reset
    reason: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str | None] = mapped_column(String(64))
    operator: Mapped[str | None] = mapped_column(String(64))
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PremarketBriefingRow(Base):
    """
    One row per trading day — the agent's pre-market briefing.
    Read by strategy worker at 09:15 to bias direction + position sizing.
    """
    __tablename__ = "premarket_briefings"

    briefing_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    sentiment: Mapped[str] = mapped_column(String(16), nullable=False)
    conviction: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False)
    overall_impact: Mapped[str] = mapped_column(String(16), nullable=False)
    position_size_multiplier: Mapped[Decimal] = mapped_column(Numeric(4, 2), nullable=False)
    skip_trading: Mapped[bool] = mapped_column(Boolean, nullable=False)

    nifty_bias: Mapped[str] = mapped_column(String(16), nullable=False)
    banknifty_bias: Mapped[str] = mapped_column(String(16), nullable=False)
    intraday_phases: Mapped[dict] = mapped_column(JSONB, default=dict)

    headlines_summary: Mapped[str | None] = mapped_column(Text)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    agent_messages: Mapped[list] = mapped_column(JSONB, default=list)
    tools_used: Mapped[list] = mapped_column(JSONB, default=list)
    tokens_used: Mapped[int] = mapped_column(Integer, default=0)
    cost_inr: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)


class AuditLogRow(Base):
    """Append-only narrative log of significant events. Do not delete rows."""
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    component: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    event: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)

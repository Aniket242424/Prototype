"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-05-09

Phase 0 schema. Subsequent phases will add tables (e.g., learning weights,
backtest runs) via new migrations — never edit this file post-merge.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("user_id", sa.String(64), primary_key=True),
        sa.Column("display_name", sa.String(128)),
        sa.Column("email", sa.String(256)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "tokens",
        sa.Column("user_id", sa.String(64), primary_key=True),
        sa.Column("broker", sa.String(32), nullable=False, server_default="UPSTOX"),
        sa.Column("access_token_encrypted", sa.LargeBinary, nullable=False),
        sa.Column("user_name", sa.String(128)),
        sa.Column("email", sa.String(256)),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "acknowledgment_log",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("file_sha256", sa.String(64), nullable=False),
        sa.Column("file_text", sa.Text, nullable=False),
        sa.Column("capital_at_signing_inr", sa.Numeric(18, 2), nullable=False),
        sa.Column("signed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
    )

    op.create_table(
        "instruments",
        sa.Column("instrument_key", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(32), nullable=False, index=True),
        sa.Column("exchange", sa.String(16), nullable=False),
        sa.Column("lot_size", sa.Integer, nullable=False),
        sa.Column("tick_size", sa.Numeric(10, 4), nullable=False),
        sa.Column("expiry_weekday", sa.Integer, nullable=False),
        sa.Column("enabled", sa.Boolean, server_default=sa.true()),
    )

    op.create_table(
        "market_data_ticks",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("instrument_key", sa.String(64), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ltp", sa.Numeric(18, 4), nullable=False),
        sa.Column("bid", sa.Numeric(18, 4)),
        sa.Column("ask", sa.Numeric(18, 4)),
        sa.Column("bid_qty", sa.Integer),
        sa.Column("ask_qty", sa.Integer),
        sa.Column("volume", sa.BigInteger),
        sa.Column("oi", sa.BigInteger),
    )
    op.create_index("ix_ticks_instrument_ts", "market_data_ticks", ["instrument_key", "ts"])

    op.create_table(
        "options_chain_snapshots",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("underlying", sa.String(16), nullable=False),
        sa.Column("expiry", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("underlying_spot", sa.Numeric(18, 4), nullable=False),
        sa.Column("chain", JSONB, nullable=False),
    )
    op.create_index("ix_chain_underlying_ts", "options_chain_snapshots", ["underlying", "ts"])

    op.create_table(
        "india_vix",
        sa.Column("ts", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("value", sa.Numeric(10, 4), nullable=False),
    )

    op.create_table(
        "regime_states",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("underlying", sa.String(16), nullable=False),
        sa.Column("regime", sa.String(32), nullable=False),
        sa.Column("confidence", sa.Numeric(5, 4), nullable=False),
        sa.Column("components", JSONB, nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_regime_underlying_ts", "regime_states", ["underlying", "ts"])

    op.create_table(
        "opportunities",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("underlying", sa.String(16), nullable=False),
        sa.Column("direction", sa.String(8), nullable=False),
        sa.Column("score", sa.Numeric(5, 4), nullable=False),
        sa.Column("components", JSONB, nullable=False),
        sa.Column("recommended_expiry", sa.DateTime(timezone=True)),
        sa.Column("recommended_strike_band", JSONB),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "ai_decisions",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("opportunity_id", sa.BigInteger, sa.ForeignKey("opportunities.id"), index=True),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("confidence", sa.Numeric(5, 4), nullable=False),
        sa.Column("advisor_score", sa.Numeric(5, 4), nullable=False),
        sa.Column("rationale", sa.Text, nullable=False),
        sa.Column("warnings", JSONB, server_default=sa.text("'[]'::jsonb")),
        sa.Column("prompt", sa.Text, nullable=False),
        sa.Column("raw_response", JSONB, nullable=False),
        sa.Column("model", sa.String(64), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "strategy_signals",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("opportunity_id", sa.BigInteger, sa.ForeignKey("opportunities.id"), index=True),
        sa.Column("strategy_name", sa.String(64), nullable=False),
        sa.Column("intent", JSONB, nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "risk_decisions",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("signal_id", sa.BigInteger, sa.ForeignKey("strategy_signals.id"), index=True),
        sa.Column("approved", sa.Boolean, nullable=False),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("code", sa.String(32), nullable=False),
        sa.Column("sized_qty", sa.Integer),
        sa.Column("max_premium", sa.Numeric(18, 4)),
        sa.Column("inputs_snapshot", JSONB, nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "orders",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("risk_decision_id", sa.BigInteger, sa.ForeignKey("risk_decisions.id"), index=True),
        sa.Column("broker_order_id", sa.String(64), index=True),
        sa.Column("instrument_key", sa.String(64), nullable=False, index=True),
        sa.Column("side", sa.String(8), nullable=False),
        sa.Column("order_type", sa.String(16), nullable=False),
        sa.Column("qty", sa.Integer, nullable=False),
        sa.Column("limit_price", sa.Numeric(18, 4)),
        sa.Column("status", sa.String(16), nullable=False, server_default="NEW"),
        sa.Column("is_paper", sa.Boolean, nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True)),
        sa.Column("finalized_at", sa.DateTime(timezone=True)),
        sa.Column("rejection_reason", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "executions",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("order_id", sa.BigInteger, sa.ForeignKey("orders.id"), nullable=False, index=True),
        sa.Column("fill_qty", sa.Integer, nullable=False),
        sa.Column("fill_price", sa.Numeric(18, 4), nullable=False),
        sa.Column("fee", sa.Numeric(18, 4), server_default="0"),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "positions",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("instrument_key", sa.String(64), nullable=False, index=True),
        sa.Column("underlying", sa.String(16), nullable=False),
        sa.Column("direction", sa.String(8), nullable=False),
        sa.Column("qty", sa.Integer, nullable=False),
        sa.Column("avg_entry_price", sa.Numeric(18, 4), nullable=False),
        sa.Column("avg_exit_price", sa.Numeric(18, 4)),
        sa.Column("initial_stop", sa.Numeric(18, 4)),
        sa.Column("target", sa.Numeric(18, 4)),
        sa.Column("pnl_inr", sa.Numeric(18, 2)),
        sa.Column("is_open", sa.Boolean, server_default=sa.true(), index=True),
        sa.Column("is_paper", sa.Boolean, nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        sa.Column("metadata", JSONB, server_default=sa.text("'{}'::jsonb")),
    )

    op.create_table(
        "slippage_log",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("order_id", sa.BigInteger, sa.ForeignKey("orders.id"), nullable=False, index=True),
        sa.Column("reference_mid", sa.Numeric(18, 4), nullable=False),
        sa.Column("estimated_slippage_bps", sa.Numeric(10, 4), nullable=False),
        sa.Column("realized_slippage_bps", sa.Numeric(10, 4), nullable=False),
        sa.Column("spread_bps_at_entry", sa.Numeric(10, 4), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "pnl_daily",
        sa.Column("trading_date", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("realized_pnl_inr", sa.Numeric(18, 2), server_default="0"),
        sa.Column("unrealized_pnl_inr", sa.Numeric(18, 2), server_default="0"),
        sa.Column("trades_count", sa.Integer, server_default="0"),
        sa.Column("wins", sa.Integer, server_default="0"),
        sa.Column("losses", sa.Integer, server_default="0"),
        sa.Column("fees_inr", sa.Numeric(18, 2), server_default="0"),
        sa.Column("is_paper", sa.Boolean, nullable=False),
    )

    op.create_table(
        "kill_switch_events",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("event", sa.String(16), nullable=False),
        sa.Column("reason", sa.Text),
        sa.Column("source", sa.String(64)),
        sa.Column("operator", sa.String(64)),
        sa.Column("ts", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("component", sa.String(64), nullable=False, index=True),
        sa.Column("event", sa.String(64), nullable=False, index=True),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), server_default=sa.func.now(), index=True),
    )


def downgrade() -> None:
    for tbl in [
        "audit_log",
        "kill_switch_events",
        "pnl_daily",
        "slippage_log",
        "positions",
        "executions",
        "orders",
        "risk_decisions",
        "strategy_signals",
        "ai_decisions",
        "opportunities",
        "regime_states",
        "india_vix",
        "options_chain_snapshots",
        "market_data_ticks",
        "instruments",
        "acknowledgment_log",
        "tokens",
        "users",
    ]:
        op.drop_table(tbl)

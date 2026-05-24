"""premarket briefings table

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-24

Phase 7.1: store the daily pre-market briefing produced by the agent.
One row per trading day; read by the strategy worker at 09:15.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "premarket_briefings",
        sa.Column("briefing_date", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sentiment", sa.String(16), nullable=False),
        sa.Column("conviction", sa.Numeric(5, 4), nullable=False),
        sa.Column("overall_impact", sa.String(16), nullable=False),
        sa.Column("position_size_multiplier", sa.Numeric(4, 2), nullable=False),
        sa.Column("skip_trading", sa.Boolean, nullable=False),
        sa.Column("nifty_bias", sa.String(16), nullable=False),
        sa.Column("banknifty_bias", sa.String(16), nullable=False),
        sa.Column("intraday_phases", JSONB, server_default="{}"),
        sa.Column("headlines_summary", sa.Text),
        sa.Column("rationale", sa.Text, nullable=False),
        sa.Column("agent_messages", JSONB, server_default="[]"),
        sa.Column("tools_used", JSONB, server_default="[]"),
        sa.Column("tokens_used", sa.Integer, server_default="0"),
        sa.Column("cost_inr", sa.Numeric(10, 2), server_default="0"),
    )


def downgrade() -> None:
    op.drop_table("premarket_briefings")

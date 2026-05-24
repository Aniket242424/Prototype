"""llm usage log table

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-25

One row per Claude API call (Anthropic direct OR Bedrock). Drives the
dashboard's per-agent token-spend widget and any future cost analytics.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "llm_usage_log",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("agent_name", sa.String(64), nullable=False),
        sa.Column("backend", sa.String(16), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("tokens_in", sa.Integer, nullable=False),
        sa.Column("tokens_out", sa.Integer, nullable=False),
        sa.Column("cost_inr", sa.Numeric(10, 4), nullable=False),
        sa.Column("latency_ms", sa.Integer, nullable=False),
        sa.Column("success", sa.Boolean, nullable=False),
        sa.Column("error", sa.Text),
        sa.Column(
            "ts", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
    )
    op.create_index("ix_llm_usage_agent_name", "llm_usage_log", ["agent_name"])
    op.create_index("ix_llm_usage_ts", "llm_usage_log", ["ts"])


def downgrade() -> None:
    op.drop_index("ix_llm_usage_ts", table_name="llm_usage_log")
    op.drop_index("ix_llm_usage_agent_name", table_name="llm_usage_log")
    op.drop_table("llm_usage_log")

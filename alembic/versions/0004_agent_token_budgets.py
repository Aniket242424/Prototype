"""agent token budgets table

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-25

Per-agent LLM token allowance. Operator sets allowance + refills as
budget runs out. Consumed is computed live from llm_usage_log.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agent_token_budgets",
        sa.Column("agent_name", sa.String(64), primary_key=True),
        sa.Column("allowance", sa.Integer, nullable=False),
        sa.Column(
            "refilled_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column("notes", sa.Text),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("agent_token_budgets")

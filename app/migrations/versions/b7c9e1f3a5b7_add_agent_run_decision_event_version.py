"""add AgentRun decision event version

Revision ID: b7c9e1f3a5b7
Revises: a6b8d0e2f4a6
Create Date: 2026-10-07 01:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7c9e1f3a5b7"
down_revision: str | Sequence[str] | None = "a6b8d0e2f4a6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agent_run_decisions",
        sa.Column("event_version", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_check_constraint(
        op.f("ck_agent_run_decisions_event_version_nonnegative"),
        "agent_run_decisions",
        "event_version >= 0",
    )


def downgrade() -> None:
    has_versioned_decisions = op.get_bind().execute(
        sa.text("SELECT EXISTS (SELECT 1 FROM agent_run_decisions WHERE event_version > 0)")
    ).scalar_one()
    if has_versioned_decisions:
        raise RuntimeError(
            "refusing to drop AgentRun decision event version while versioned decisions exist"
        )
    op.drop_constraint(
        op.f("ck_agent_run_decisions_event_version_nonnegative"),
        "agent_run_decisions",
        type_="check",
    )
    op.drop_column("agent_run_decisions", "event_version")

"""add AgentRun lifecycle event version

Revision ID: f5a7b9c1d3e5
Revises: e4f6a8b0c2d4
Create Date: 2026-10-06 15:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f5a7b9c1d3e5"
down_revision: str | Sequence[str] | None = "e4f6a8b0c2d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agent_runs",
        sa.Column("lifecycle_event_version", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_check_constraint(
        op.f("ck_agent_runs_lifecycle_event_version_nonnegative"),
        "agent_runs",
        "lifecycle_event_version >= 0",
    )


def downgrade() -> None:
    has_versioned_lifecycle = op.get_bind().execute(
        sa.text("SELECT EXISTS (SELECT 1 FROM agent_runs WHERE lifecycle_event_version > 0)")
    ).scalar_one()
    if has_versioned_lifecycle:
        raise RuntimeError(
            "refusing to drop AgentRun lifecycle version while versioned lifecycle events exist"
        )
    op.drop_constraint(
        op.f("ck_agent_runs_lifecycle_event_version_nonnegative"),
        "agent_runs",
        type_="check",
    )
    op.drop_column("agent_runs", "lifecycle_event_version")

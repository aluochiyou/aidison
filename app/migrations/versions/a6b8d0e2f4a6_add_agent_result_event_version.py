"""add AgentRun result event version

Revision ID: a6b8d0e2f4a6
Revises: f5a7b9c1d3e5
Create Date: 2026-10-06 15:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a6b8d0e2f4a6"
down_revision: str | Sequence[str] | None = "f5a7b9c1d3e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agent_run_results",
        sa.Column("event_version", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_check_constraint(
        op.f("ck_agent_run_results_event_version_nonnegative"),
        "agent_run_results",
        "event_version >= 0",
    )


def downgrade() -> None:
    has_versioned_results = op.get_bind().execute(
        sa.text("SELECT EXISTS (SELECT 1 FROM agent_run_results WHERE event_version > 0)")
    ).scalar_one()
    if has_versioned_results:
        raise RuntimeError(
            "refusing to drop AgentRun result event version while versioned result events exist"
        )
    op.drop_constraint(
        op.f("ck_agent_run_results_event_version_nonnegative"),
        "agent_run_results",
        type_="check",
    )
    op.drop_column("agent_run_results", "event_version")

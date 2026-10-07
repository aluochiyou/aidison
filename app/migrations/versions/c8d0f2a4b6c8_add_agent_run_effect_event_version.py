"""add AgentRun effect event version

Revision ID: c8d0f2a4b6c8
Revises: b7c9e1f3a5b7
Create Date: 2026-10-07 02:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c8d0f2a4b6c8"
down_revision: str | Sequence[str] | None = "b7c9e1f3a5b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agent_run_effects",
        sa.Column("event_version", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_check_constraint(
        op.f("ck_agent_run_effects_event_version_nonnegative"),
        "agent_run_effects",
        "event_version >= 0",
    )


def downgrade() -> None:
    has_versioned_effects = op.get_bind().execute(
        sa.text("SELECT EXISTS (SELECT 1 FROM agent_run_effects WHERE event_version > 0)")
    ).scalar_one()
    if has_versioned_effects:
        raise RuntimeError(
            "refusing to drop AgentRun effect event version while versioned effects exist"
        )
    op.drop_constraint(
        op.f("ck_agent_run_effects_event_version_nonnegative"),
        "agent_run_effects",
        type_="check",
    )
    op.drop_column("agent_run_effects", "event_version")

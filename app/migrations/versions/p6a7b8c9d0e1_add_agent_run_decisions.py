"""add AgentRun decisions

Revision ID: p6a7b8c9d0e1
Revises: o5f6a7b8c9d0
Create Date: 2026-09-01 22:15:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "p6a7b8c9d0e1"
down_revision: str | Sequence[str] | None = "o5f6a7b8c9d0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_run_decisions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("agent_run_id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("basis_hash", sa.String(length=64), nullable=False),
        sa.Column("proposal_manifest_ref", sa.String(length=500), nullable=False),
        sa.Column("proposal_manifest_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("answer", sa.String(length=40), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["agent_run_id"], ["agent_runs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("agent_run_id", name="uq_agent_run_decision_run"),
    )
    op.create_index(
        "ix_agent_run_decisions_project_status",
        "agent_run_decisions",
        ["project_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_agent_run_decisions_project_status", table_name="agent_run_decisions")
    op.drop_table("agent_run_decisions")

"""add LangGraph AgentRun control rows

Revision ID: l2c3d4e5f6a7
Revises: k1b2c3d4e5f6
Create Date: 2026-09-01 14:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "l2c3d4e5f6a7"
down_revision: str | Sequence[str] | None = "k1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.String(length=80), nullable=False),
        sa.Column("idempotency_key", sa.String(length=300), nullable=False),
        sa.Column("basis_hash", sa.String(length=64), nullable=False),
        sa.Column("basis_project_revision", sa.Integer(), nullable=False),
        sa.Column("runtime_binding", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("thread_id", sa.String(length=300), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("current_generation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lease_owner", sa.String(length=200), nullable=True),
        sa.Column("lease_token", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("admitted_checkpoint", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'waiting', 'succeeded', 'failed', 'cancelled')",
            name=op.f("ck_agent_runs_status"),
        ),
        sa.CheckConstraint(
            "basis_project_revision >= 1", name=op.f("ck_agent_runs_basis_revision_positive")
        ),
        sa.CheckConstraint(
            "current_generation >= 0", name=op.f("ck_agent_runs_generation_nonnegative")
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_agent_runs_idempotency_key")),
        sa.UniqueConstraint("thread_id", name=op.f("uq_agent_runs_thread_id")),
    )
    op.create_index(
        "ix_agent_runs_claim", "agent_runs", ["status", "lease_expires_at", "created_at"]
    )
    op.create_index("ix_agent_runs_project", "agent_runs", ["project_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_agent_runs_project", table_name="agent_runs")
    op.drop_index("ix_agent_runs_claim", table_name="agent_runs")
    op.drop_table("agent_runs")

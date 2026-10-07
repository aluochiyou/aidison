"""add AgentRun pause and steering control requests

Revision ID: r5a1b2c3d4e5
Revises: r3b9c0d1e2f3
Create Date: 2026-09-04 20:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "r5a1b2c3d4e5"
down_revision: str | Sequence[str] | None = "r3b9c0d1e2f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_run_control_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("agent_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("basis_hash", sa.String(length=64), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("idempotency_key", sa.String(length=300), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["agent_run_id"], ["agent_runs.id"], ondelete="RESTRICT"),
        sa.CheckConstraint("kind IN ('pause', 'runtime_steering', 'basis_steering')", name="control_kind"),
        sa.CheckConstraint("status IN ('requested', 'acknowledged', 'rejected')", name="control_status"),
        sa.UniqueConstraint("idempotency_key", name="uq_agent_run_control_request_idempotency"),
    )
    op.create_index("ix_agent_run_control_requests_run", "agent_run_control_requests", ["agent_run_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_agent_run_control_requests_run", table_name="agent_run_control_requests")
    op.drop_table("agent_run_control_requests")

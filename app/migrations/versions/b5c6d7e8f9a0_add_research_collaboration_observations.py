"""add durable in-run research collaboration observations

Revision ID: b5c6d7e8f9a0
Revises: a4b5c6d7e8f9
Create Date: 2026-10-09 14:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b5c6d7e8f9a0"
down_revision: str | Sequence[str] | None = "a4b5c6d7e8f9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "research_collaboration_observations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("sequence", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "agent_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent_runs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("basis_hash", sa.String(length=64), nullable=False),
        sa.Column("producer_task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("artifact_ref", sa.String(length=512), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=300), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("kind IN ('source_discovery')", name="kind"),
        sa.UniqueConstraint("idempotency_key", name="uq_research_collaboration_idempotency"),
        sa.UniqueConstraint("sequence", name="uq_research_collaboration_sequence"),
    )
    op.create_index(
        "ix_research_collaboration_run_sequence",
        "research_collaboration_observations",
        ["agent_run_id", "sequence"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_research_collaboration_run_sequence",
        table_name="research_collaboration_observations",
    )
    op.drop_table("research_collaboration_observations")

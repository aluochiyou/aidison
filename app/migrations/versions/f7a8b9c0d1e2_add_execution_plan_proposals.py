"""add execution plan proposals

Revision ID: f7a8b9c0d1e2
Revises: e6f7a8b9c0d1
Create Date: 2026-08-10 06:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f7a8b9c0d1e2"
down_revision: str | Sequence[str] | None = "e6f7a8b9c0d1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "execution_plan_proposals",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("basis_hash", sa.String(length=64), nullable=False),
        sa.Column("scope_hash", sa.String(length=64), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.CheckConstraint(
            "status IN ('proposed', 'approved', 'rejected', 'superseded')",
            name=op.f("ck_execution_plan_proposals_status"),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"],
            name=op.f("fk_execution_plan_proposals_project_id_projects"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_execution_plan_proposals")),
        sa.UniqueConstraint("project_id", "id", name="uq_execution_plan_project_id"),
    )
    op.create_index(
        "ix_execution_plan_proposals_project_status",
        "execution_plan_proposals",
        ["project_id", "status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_execution_plan_proposals_project_status",
        table_name="execution_plan_proposals",
    )
    op.drop_table("execution_plan_proposals")

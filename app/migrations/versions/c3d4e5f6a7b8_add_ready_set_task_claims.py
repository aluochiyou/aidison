"""add ready-set task claims

Revision ID: c3d4e5f6a7b8
Revises: b7d3e5f91a20
Create Date: 2026-08-10 03:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "c3d4e5f6a7b8"
down_revision: str | Sequence[str] | None = "b7d3e5f91a20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "plan_task_claims",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("root_job_id", sa.UUID(), nullable=False),
        sa.Column("plan_revision_id", sa.UUID(), nullable=False),
        sa.Column("plan_revision", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.UUID(), nullable=False),
        sa.Column("logical_key", sa.String(length=120), nullable=False),
        sa.Column("claim_generation", sa.Integer(), nullable=False),
        sa.Column("lease_owner", sa.String(length=200), nullable=False),
        sa.Column("lease_token", sa.UUID(), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("child_job_id", sa.UUID(), nullable=True),
        sa.Column("intent", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "claim_generation >= 1", name=op.f("ck_plan_task_claims_generation_positive")
        ),
        sa.CheckConstraint(
            "status IN ('claimed', 'dispatched', 'succeeded', 'failed', 'cancelled', "
            "'superseded')",
            name=op.f("ck_plan_task_claims_status"),
        ),
        sa.ForeignKeyConstraint(
            ["root_job_id", "plan_revision_id"],
            ["plan_revisions.root_job_id", "plan_revisions.id"],
            name="fk_claim_same_root_revision",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["plan_revision_id", "task_id"],
            ["plan_tasks.plan_revision_id", "plan_tasks.id"],
            name="fk_claim_same_revision_task",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["child_job_id"],
            ["jobs.id"],
            name=op.f("fk_plan_task_claims_child_job_id_jobs"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["root_job_id"],
            ["jobs.id"],
            name=op.f("fk_plan_task_claims_root_job_id_jobs"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_plan_task_claims")),
        sa.UniqueConstraint(
            "root_job_id",
            "plan_revision_id",
            "task_id",
            "claim_generation",
            name="uq_claim_task_generation",
        ),
    )
    op.create_index(
        "uq_plan_task_claim_active",
        "plan_task_claims",
        ["plan_revision_id", "task_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('claimed', 'dispatched')"),
    )
    op.create_index(
        "ix_plan_task_claims_lease",
        "plan_task_claims",
        ["status", "lease_expires_at"],
        unique=False,
    )
    op.create_index(
        "ix_plan_task_claims_root",
        "plan_task_claims",
        ["root_job_id", "plan_revision_id", "logical_key"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_plan_task_claims_root", table_name="plan_task_claims")
    op.drop_index("ix_plan_task_claims_lease", table_name="plan_task_claims")
    op.drop_index("uq_plan_task_claim_active", table_name="plan_task_claims")
    op.drop_table("plan_task_claims")

"""add durable invocation recordings

Revision ID: h9b0c1d2e3f4
Revises: g8a9b0c1d2e3
Create Date: 2026-08-10 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "h9b0c1d2e3f4"
down_revision: str | Sequence[str] | None = "g8a9b0c1d2e3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "invocation_recordings",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("job_id", sa.UUID(), nullable=False),
        sa.Column("attempt_id", sa.UUID(), nullable=False),
        sa.Column("basis_hash", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=300), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("provider", sa.String(length=100), nullable=False),
        sa.Column("operation_name", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("response_artifact_ref", sa.String(length=500), nullable=True),
        sa.Column("failure_class", sa.String(length=40), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind IN ('model', 'tool')", name=op.f("ck_invocation_recordings_kind")
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'succeeded', 'ambiguous')",
            name=op.f("ck_invocation_recordings_status"),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_invocation_recordings_project_id_projects"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["jobs.id"],
            name=op.f("fk_invocation_recordings_job_id_jobs"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id"],
            ["attempts.id"],
            name=op.f("fk_invocation_recordings_attempt_id_attempts"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_invocation_recordings")),
        sa.UniqueConstraint(
            "idempotency_key", name=op.f("uq_invocation_recordings_idempotency_key")
        ),
    )
    op.create_index(
        "ix_invocation_recordings_attempt",
        "invocation_recordings",
        ["attempt_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_invocation_recordings_attempt", table_name="invocation_recordings")
    op.drop_table("invocation_recordings")

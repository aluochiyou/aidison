"""add artifact job lineage

Revision ID: i9c0d1e2f3a4
Revises: h9b0c1d2e3f4
Create Date: 2026-08-10 16:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "i9c0d1e2f3a4"
down_revision: str | Sequence[str] | None = "h9b0c1d2e3f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("artifacts", sa.Column("job_id", sa.UUID(), nullable=True))
    # Every artifact already has a non-null attempt FK; derive its durable job
    # lineage from that attempt before enforcing the new non-null constraint.
    op.execute(
        "UPDATE artifacts AS artifact SET job_id = attempt.job_id "
        "FROM attempts AS attempt WHERE attempt.id = artifact.attempt_id"
    )
    op.alter_column("artifacts", "job_id", nullable=False)
    op.create_foreign_key(
        "fk_artifacts_job_id_jobs",
        "artifacts",
        "jobs",
        ["job_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint("fk_artifacts_job_id_jobs", "artifacts", type_="foreignkey")
    op.drop_column("artifacts", "job_id")

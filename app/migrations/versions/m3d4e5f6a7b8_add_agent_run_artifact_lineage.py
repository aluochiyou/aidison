"""add AgentRun artifact lineage

Revision ID: m3d4e5f6a7b8
Revises: l2c3d4e5f6a7
Create Date: 2026-09-01 21:20:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "m3d4e5f6a7b8"
down_revision: str | Sequence[str] | None = "l2c3d4e5f6a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("artifacts", "job_id", nullable=True)
    op.alter_column("artifacts", "attempt_id", nullable=True)
    op.add_column("artifacts", sa.Column("agent_run_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        "fk_artifacts_agent_run_id_agent_runs",
        "artifacts",
        "agent_runs",
        ["agent_run_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        "uq_artifact_agent_run_kind_hash",
        "artifacts",
        ["project_id", "agent_run_id", "kind", "content_hash"],
    )
    op.create_check_constraint(
        "ck_artifacts_lineage_exactly_one_runtime_owner",
        "artifacts",
        "(job_id IS NOT NULL AND attempt_id IS NOT NULL AND agent_run_id IS NULL) "
        "OR (job_id IS NULL AND attempt_id IS NULL AND agent_run_id IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_artifacts_lineage_exactly_one_runtime_owner", "artifacts")
    op.drop_constraint("uq_artifact_agent_run_kind_hash", "artifacts")
    op.drop_constraint("fk_artifacts_agent_run_id_agent_runs", "artifacts", type_="foreignkey")
    op.drop_column("artifacts", "agent_run_id")
    # If AgentRun artifacts exist PostgreSQL will fail this downgrade instead of
    # deleting their audit lineage implicitly.
    op.alter_column("artifacts", "attempt_id", nullable=False)
    op.alter_column("artifacts", "job_id", nullable=False)

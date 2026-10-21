"""add AgentRun result admissions

Revision ID: n4e5f6a7b8c9
Revises: m3d4e5f6a7b8
Create Date: 2026-09-01 21:40:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "n4e5f6a7b8c9"
down_revision: str | Sequence[str] | None = "m3d4e5f6a7b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_run_results",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("agent_run_id", sa.UUID(), nullable=False),
        sa.Column("task_id", sa.UUID(), nullable=False),
        sa.Column("basis_hash", sa.String(length=64), nullable=False),
        sa.Column("manifest_hash", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["agent_run_id"], ["agent_runs.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("agent_run_id", "id", name="uq_agent_run_result_identity"),
        sa.UniqueConstraint(
            "agent_run_id", "task_id", "manifest_hash", name="uq_agent_run_result_payload"
        ),
    )
    op.create_index("ix_agent_run_results_run", "agent_run_results", ["agent_run_id", "created_at"])
    op.create_table(
        "agent_run_result_admissions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("agent_run_id", sa.UUID(), nullable=False),
        sa.Column("result_id", sa.UUID(), nullable=False),
        sa.Column("disposition", sa.String(length=40), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["agent_run_id", "result_id"],
            ["agent_run_results.agent_run_id", "agent_run_results.id"],
            name="fk_agent_result_admission_result",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("result_id", name="uq_agent_run_result_admission_result"),
    )
    op.create_index(
        "ix_agent_run_result_admissions_run",
        "agent_run_result_admissions",
        ["agent_run_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_agent_run_result_admissions_run", table_name="agent_run_result_admissions")
    op.drop_table("agent_run_result_admissions")
    op.drop_index("ix_agent_run_results_run", table_name="agent_run_results")
    op.drop_table("agent_run_results")

"""align AgentRun tables with the current ORM metadata

Revision ID: t7a1b2c3d4e5
Revises: s7a1b2c3d4e5
Create Date: 2026-09-09 10:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "t7a1b2c3d4e5"
down_revision: str | Sequence[str] | None = "s7a1b2c3d4e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The initial AgentRun migration used generic JSON while the ORM has always
    # declared PostgreSQL JSONB.  Preserve payload bytes while making the
    # database type and schema-autogenerate metadata agree.
    for table_name in ("agent_run_results", "agent_run_result_admissions"):
        op.alter_column(
            table_name,
            "payload",
            existing_type=sa.JSON(),
            type_=postgresql.JSONB(astext_type=sa.Text()),
            postgresql_using="payload::jsonb",
        )

    # This is a metadata-only rename; idempotency semantics and the indexed
    # column are unchanged.
    op.execute(
        "ALTER TABLE agent_runs RENAME CONSTRAINT "
        "uq_agent_runs_idempotency_key TO uq_agent_run_idempotency_key"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE agent_runs RENAME CONSTRAINT "
        "uq_agent_run_idempotency_key TO uq_agent_runs_idempotency_key"
    )
    for table_name in ("agent_run_result_admissions", "agent_run_results"):
        op.alter_column(
            table_name,
            "payload",
            existing_type=postgresql.JSONB(astext_type=sa.Text()),
            type_=sa.JSON(),
            postgresql_using="payload::json",
        )

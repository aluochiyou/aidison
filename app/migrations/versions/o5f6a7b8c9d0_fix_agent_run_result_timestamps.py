"""fix AgentRun result timestamp defaults

Revision ID: o5f6a7b8c9d0
Revises: n4e5f6a7b8c9
Create Date: 2026-09-01 21:50:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "o5f6a7b8c9d0"
down_revision: str | Sequence[str] | None = "n4e5f6a7b8c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "agent_run_results",
        "created_at",
        server_default=sa.text("now()"),
    )
    op.alter_column(
        "agent_run_result_admissions",
        "created_at",
        server_default=sa.text("now()"),
    )


def downgrade() -> None:
    op.alter_column("agent_run_result_admissions", "created_at", server_default=None)
    op.alter_column("agent_run_results", "created_at", server_default=None)

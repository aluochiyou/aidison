"""add AgentRun coverage contract reference

Revision ID: q7b8c9d0e1f2
Revises: p6a7b8c9d0e1
Create Date: 2026-09-02 09:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "q7b8c9d0e1f2"
down_revision: str | Sequence[str] | None = "p6a7b8c9d0e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agent_runs",
        sa.Column("coverage_contract_ref", sa.String(length=500), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("agent_runs", "coverage_contract_ref")

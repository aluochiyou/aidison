"""add immutable AgentRun contract reference

Revision ID: r6a1b2c3d4e5
Revises: r5a1b2c3d4e5
Create Date: 2026-09-06 22:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "r6a1b2c3d4e5"
down_revision: str | Sequence[str] | None = "r5a1b2c3d4e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("agent_runs", sa.Column("run_contract_ref", sa.String(length=500), nullable=True))


def downgrade() -> None:
    op.drop_column("agent_runs", "run_contract_ref")

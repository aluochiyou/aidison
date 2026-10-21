"""record the first execution start for AgentRun duration contracts

Revision ID: v9a1b2c3d4e5
Revises: u8a1b2c3d4e5
Create Date: 2026-09-12 17:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "v9a1b2c3d4e5"
down_revision: str | Sequence[str] | None = "u8a1b2c3d4e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("agent_runs", sa.Column("started_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("agent_runs", "started_at")

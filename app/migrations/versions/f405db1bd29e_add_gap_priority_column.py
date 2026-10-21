"""add_gap_priority_column

Revision ID: f405db1bd29e
Revises: d4a8e7c91f20
Create Date: 2026-08-07 23:42:30.259151
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f405db1bd29e"
down_revision: str | Sequence[str] | None = "d4a8e7c91f20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "plan_gaps",
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("plan_gaps", "priority")

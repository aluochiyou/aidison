"""record the admitted result that originated module memory

Revision ID: z3c4d5e6f7a8
Revises: y2b3c4d5e6f7
Create Date: 2026-09-15 16:20:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "z3c4d5e6f7a8"
down_revision: str | Sequence[str] | None = "y2b3c4d5e6f7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "module_memory_items",
        sa.Column("source_result_id", postgresql.UUID(as_uuid=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("module_memory_items", "source_result_id")

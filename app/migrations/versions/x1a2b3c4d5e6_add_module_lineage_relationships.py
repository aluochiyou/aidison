"""add explicit module lineage relationships

Revision ID: x1a2b3c4d5e6
Revises: w0a1b2c3d4e5
Create Date: 2026-09-15 15:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "x1a2b3c4d5e6"
down_revision: str | Sequence[str] | None = "w0a1b2c3d4e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "module_lineages",
        sa.Column("split_from_lineage_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "module_lineages",
        sa.Column("merged_into_lineage_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "module_lineages",
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_module_lineage_split_from",
        "module_lineages",
        "module_lineages",
        ["split_from_lineage_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_module_lineage_merged_into",
        "module_lineages",
        "module_lineages",
        ["merged_into_lineage_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_module_lineage_merged_into", "module_lineages", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_module_lineage_split_from", "module_lineages", type_="foreignkey"
    )
    op.drop_column("module_lineages", "retired_at")
    op.drop_column("module_lineages", "merged_into_lineage_id")
    op.drop_column("module_lineages", "split_from_lineage_id")

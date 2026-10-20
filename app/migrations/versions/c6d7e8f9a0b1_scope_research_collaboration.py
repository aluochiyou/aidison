"""fence and scope in-run research collaboration

Revision ID: c6d7e8f9a0b1
Revises: b5c6d7e8f9a0
Create Date: 2026-10-21 09:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c6d7e8f9a0b1"
down_revision: str | Sequence[str] | None = "b5c6d7e8f9a0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "research_collaboration_observations",
        sa.Column("producer_generation", sa.Integer(), nullable=True),
    )
    op.add_column(
        "research_collaboration_observations",
        sa.Column("scope_hash", sa.String(length=64), nullable=True),
    )
    # Existing source-discovery rows predate scoped subscriptions. Preserve
    # them for audit, but place them in a legacy run-wide scope that new
    # compiled scopes will not consume accidentally.
    op.execute(
        sa.text(
            "UPDATE research_collaboration_observations "
            "SET producer_generation = 1, scope_hash = basis_hash "
            "WHERE producer_generation IS NULL OR scope_hash IS NULL"
        )
    )
    op.alter_column(
        "research_collaboration_observations",
        "producer_generation",
        nullable=False,
    )
    op.alter_column(
        "research_collaboration_observations",
        "scope_hash",
        nullable=False,
    )
    op.create_check_constraint(
        "producer_generation_positive",
        "research_collaboration_observations",
        "producer_generation >= 1",
    )
    op.drop_index(
        "ix_research_collaboration_run_sequence",
        table_name="research_collaboration_observations",
    )
    op.create_index(
        "ix_research_collaboration_run_scope_sequence",
        "research_collaboration_observations",
        ["agent_run_id", "scope_hash", "sequence"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_research_collaboration_run_scope_sequence",
        table_name="research_collaboration_observations",
    )
    op.create_index(
        "ix_research_collaboration_run_sequence",
        "research_collaboration_observations",
        ["agent_run_id", "sequence"],
    )
    op.drop_constraint(
        "producer_generation_positive",
        "research_collaboration_observations",
        type_="check",
    )
    op.drop_column("research_collaboration_observations", "scope_hash")
    op.drop_column("research_collaboration_observations", "producer_generation")

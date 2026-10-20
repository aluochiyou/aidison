"""add typed research collaboration signals

Revision ID: d7e8f9a0b1c2
Revises: c6d7e8f9a0b1
Create Date: 2026-10-21 10:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "d7e8f9a0b1c2"
down_revision: str | Sequence[str] | None = "c6d7e8f9a0b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        op.f("ck_research_collaboration_observations_kind"),
        "research_collaboration_observations",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_research_collaboration_observations_kind"),
        "research_collaboration_observations",
        "kind IN ('source_discovery', 'research_signal')",
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM research_collaboration_observations
                WHERE kind = 'research_signal'
            ) THEN
                RAISE EXCEPTION
                    'cannot downgrade while research_signal observations exist';
            END IF;
        END
        $$
        """
    )
    op.drop_constraint(
        op.f("ck_research_collaboration_observations_kind"),
        "research_collaboration_observations",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_research_collaboration_observations_kind"),
        "research_collaboration_observations",
        "kind IN ('source_discovery')",
    )

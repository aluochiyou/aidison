"""add_user_adjustment_impact_governance

Revision ID: d1e2f3a4b5c6
Revises: 6145ee9c54ae
Create Date: 2026-08-11 17:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "d1e2f3a4b5c6"
down_revision: Union[str, Sequence[str], None] = "6145ee9c54ae"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "requirements_change_proposals",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.CheckConstraint(
            "status IN ('proposed', 'applied', 'rejected', 'superseded')",
            name=op.f("ck_requirements_change_proposals_status"),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_requirements_change_proposals_project_id_projects"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_requirements_change_proposals")),
        sa.UniqueConstraint(
            "project_id", "id", name="uq_requirements_change_project_id"
        ),
    )
    op.create_index(
        "ix_requirements_change_project_status",
        "requirements_change_proposals",
        ["project_id", "status"],
        unique=False,
    )
    op.create_table(
        "change_impact_previews",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("batch_id", sa.UUID(), nullable=False),
        sa.Column("basis_hash", sa.String(length=64), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id", "batch_id"],
            ["adjustment_batches.project_id", "adjustment_batches.id"],
            name=op.f(
                "fk_change_impact_previews_project_id_batch_id_adjustment_batches"
            ),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_change_impact_previews_project_id_projects"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_change_impact_previews")),
        sa.UniqueConstraint(
            "project_id", "id", name="uq_change_impact_preview_project_id"
        ),
    )
    op.create_index(
        "ix_change_impact_previews_project",
        "change_impact_previews",
        ["project_id"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_change_impact_previews_project", table_name="change_impact_previews")
    op.drop_table("change_impact_previews")
    op.drop_index(
        "ix_requirements_change_project_status",
        table_name="requirements_change_proposals",
    )
    op.drop_table("requirements_change_proposals")

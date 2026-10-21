"""add_spend_budget

Revision ID: a7b8c9d0e1f2
Revises: d1e2f3a4b5c6
Create Date: 2026-08-11 20:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "a7b8c9d0e1f2"
down_revision: Union[str, Sequence[str], None] = "d1e2f3a4b5c6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "projects",
        sa.Column(
            "active_spend_budget_revision_id",
            sa.UUID(),
            nullable=True,
        ),
    )
    op.create_table(
        "spend_budget_proposals",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.CheckConstraint(
            "status IN ('proposed', 'applied', 'rejected', 'superseded')",
            name=op.f("ck_spend_budget_proposals_status"),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_spend_budget_proposals_project_id_projects"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_spend_budget_proposals")),
        sa.UniqueConstraint(
            "project_id", "id", name="uq_spend_budget_proposal_project_id"
        ),
    )
    op.create_index(
        "ix_spend_budget_proposals_project_status",
        "spend_budget_proposals",
        ["project_id", "status"],
        unique=False,
    )
    op.create_table(
        "spend_budget_revisions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("proposal_id", sa.UUID(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.CheckConstraint("revision >= 0", name=op.f("ck_spend_budget_revisions_revision_nonnegative")),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_spend_budget_revisions_project_id_projects"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "proposal_id"],
            ["spend_budget_proposals.project_id", "spend_budget_proposals.id"],
            name=op.f("fk_spend_budget_revisions_project_id_proposal_id_spend_budget_proposals"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_spend_budget_revisions")),
        sa.UniqueConstraint(
            "project_id", "revision", name="uq_spend_budget_revision_project_revision"
        ),
        sa.UniqueConstraint(
            "project_id", "id", name="uq_spend_budget_revision_project_id"
        ),
    )
    op.create_index(
        "ix_spend_budget_revisions_project_status",
        "spend_budget_revisions",
        ["project_id", "status"],
        unique=False,
    )
    op.create_table(
        "spend_budget_impact_previews",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("proposal_id", sa.UUID(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_spend_budget_impact_previews_project_id_projects"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "proposal_id"],
            ["spend_budget_proposals.project_id", "spend_budget_proposals.id"],
            name=op.f(
                "fk_spend_budget_impact_previews_project_id_proposal_id_spend_budget_proposals"
            ),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_spend_budget_impact_previews")),
        sa.UniqueConstraint(
            "project_id", "id", name="uq_spend_budget_impact_preview_project_id"
        ),
    )
    op.create_index(
        "ix_spend_budget_impact_previews_project",
        "spend_budget_impact_previews",
        ["project_id"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        "ix_spend_budget_impact_previews_project",
        table_name="spend_budget_impact_previews",
    )
    op.drop_table("spend_budget_impact_previews")
    op.drop_index(
        "ix_spend_budget_revisions_project_status",
        table_name="spend_budget_revisions",
    )
    op.drop_table("spend_budget_revisions")
    op.drop_index(
        "ix_spend_budget_proposals_project_status",
        table_name="spend_budget_proposals",
    )
    op.drop_table("spend_budget_proposals")
    op.drop_column("projects", "active_spend_budget_revision_id")

"""add structured solution proposals

Revision ID: c4e91f6a2b73
Revises: 7a0c4f12d9b1
Create Date: 2026-08-02 06:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c4e91f6a2b73"
down_revision: str | Sequence[str] | None = "7a0c4f12d9b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "jobs",
        sa.Column(
            "request_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.create_table(
        "solution_proposals",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("decision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("requirement_revision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("basis_hash", sa.String(length=64), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id", "decision_id"],
            ["decision_requests.project_id", "decision_requests.id"],
            name="fk_solution_proposal_project_decision",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "requirement_revision_id"],
            ["requirement_revisions.project_id", "requirement_revisions.id"],
            name="fk_solution_proposal_project_requirement",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "id", name="uq_solution_proposal_project_id"),
    )
    op.create_index(
        "ix_solution_proposals_project_status",
        "solution_proposals",
        ["project_id", "status"],
        unique=False,
    )
    op.add_column(
        "solution_versions",
        sa.Column("solution_proposal_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_solution_project_proposal",
        "solution_versions",
        "solution_proposals",
        ["project_id", "solution_proposal_id"],
        ["project_id", "id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_solution_project_proposal",
        "solution_versions",
        type_="foreignkey",
    )
    op.drop_column("solution_versions", "solution_proposal_id")
    op.drop_index(
        "ix_solution_proposals_project_status",
        table_name="solution_proposals",
    )
    op.drop_table("solution_proposals")
    op.drop_column("jobs", "request_payload")

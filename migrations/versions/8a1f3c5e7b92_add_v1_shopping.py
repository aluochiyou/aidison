"""add v1 shopping offer_snapshots purchase_proposals checkout_handoffs

Revision ID: 8a1f3c5e7b92
Revises: 2b7c4d8e1f03
Create Date: 2026-08-05 10:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "8a1f3c5e7b92"
down_revision: str | Sequence[str] | None = "2b7c4d8e1f03"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── offer_snapshots ──────────────────────────────────────────────
    op.create_table(
        "offer_snapshots",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("solution_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("bom_line_id", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=100), nullable=False),
        sa.Column("provider_offer_id", sa.String(length=500), nullable=False),
        sa.Column("snapshot_hash", sa.String(length=64), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "observed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "id", name="uq_offer_snapshot_project_id"),
    )
    op.create_index(
        "ix_offer_snapshots_project_solution",
        "offer_snapshots",
        ["project_id", "solution_version_id"],
        unique=False,
    )

    # ── purchase_proposals ──────────────────────────────────────────
    op.create_table(
        "purchase_proposals",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("solution_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("offer_snapshot_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("basis_hash", sa.String(length=64), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "id", name="uq_purchase_proposal_project_id"),
    )
    op.create_index(
        "ix_purchase_proposals_project_status",
        "purchase_proposals",
        ["project_id", "status"],
        unique=False,
    )

    # ── checkout_handoffs ───────────────────────────────────────────
    op.create_table(
        "checkout_handoffs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("proposal_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("basis_hash", sa.String(length=64), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "id", name="uq_checkout_handoff_project_id"),
    )
    op.create_index(
        "ix_checkout_handoffs_project_status",
        "checkout_handoffs",
        ["project_id", "status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_checkout_handoffs_project_status", table_name="checkout_handoffs")
    op.drop_table("checkout_handoffs")
    op.drop_index("ix_purchase_proposals_project_status", table_name="purchase_proposals")
    op.drop_table("purchase_proposals")
    op.drop_index("ix_offer_snapshots_project_solution", table_name="offer_snapshots")
    op.drop_table("offer_snapshots")

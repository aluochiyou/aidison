"""add module workstreams and structured memory

Revision ID: y2b3c4d5e6f7
Revises: x1a2b3c4d5e6
Create Date: 2026-09-15 16:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "y2b3c4d5e6f7"
down_revision: str | Sequence[str] | None = "x1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "module_workstreams",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "module_lineage_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("module_lineages.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("memory_manifest_ref", sa.String(length=500), nullable=True),
        sa.Column("last_project_revision", sa.Integer(), nullable=True),
        sa.Column("last_basis_hash", sa.String(length=64), nullable=True),
        sa.Column("optimistic_revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "project_id", "module_lineage_id", name="uq_workstream_project_lineage"
        ),
    )
    op.create_index(
        "ix_module_workstreams_project_status",
        "module_workstreams",
        ["project_id", "status"],
    )

    # Each established lineage receives a workstream, but no fabricated memory.
    # IDs match the lineage only for this one-time baseline; later workstreams
    # use their own generated IDs.
    op.execute(
        """
        INSERT INTO module_workstreams (
            id, project_id, module_lineage_id, status, memory_manifest_ref,
            last_project_revision, last_basis_hash, optimistic_revision, created_at, updated_at
        )
        SELECT
            id, project_id, id,
            CASE WHEN retired_at IS NULL THEN 'active' ELSE 'retired' END,
            NULL, NULL, NULL, 1, created_at, created_at
        FROM module_lineages
        """
    )

    op.create_table(
        "module_memory_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "workstream_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("module_workstreams.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("stable_key", sa.String(length=240), nullable=False),
        sa.Column("basis_hash", sa.String(length=64), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("artifact_ref", sa.String(length=500), nullable=False),
        sa.Column("evidence_refs", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("applicability", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("freshness_deadline", sa.DateTime(timezone=True), nullable=True),
        sa.Column("supersedes_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "workstream_id",
            "kind",
            "stable_key",
            "basis_hash",
            "content_hash",
            name="uq_module_memory_dedup",
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_id"], ["module_memory_items.id"],
            name="fk_module_memory_supersedes", ondelete="RESTRICT"
        ),
    )
    op.create_index(
        "ix_module_memory_workstream_status_key",
        "module_memory_items",
        ["workstream_id", "status", "stable_key"],
    )
    op.create_index(
        "ix_module_memory_active_freshness",
        "module_memory_items",
        ["freshness_deadline"],
        postgresql_where=sa.text("status = 'active'"),
    )


def downgrade() -> None:
    op.drop_index("ix_module_memory_active_freshness", table_name="module_memory_items")
    op.drop_index("ix_module_memory_workstream_status_key", table_name="module_memory_items")
    op.drop_table("module_memory_items")
    op.drop_index("ix_module_workstreams_project_status", table_name="module_workstreams")
    op.drop_table("module_workstreams")

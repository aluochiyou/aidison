"""add immutable project source documents

Revision ID: a4b5c6d7e8f9
Revises: d9e1f3a5b7d9
Create Date: 2026-10-08 11:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a4b5c6d7e8f9"
down_revision: str | Sequence[str] | None = "d9e1f3a5b7d9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "project_source_documents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=240), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("media_type", sa.String(length=120), nullable=False),
        sa.Column("storage_key", sa.String(length=120), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('active', 'missing', 'corrupt', 'quarantined')", name="status"
        ),
        sa.CheckConstraint("size_bytes > 0", name="size_positive"),
        sa.UniqueConstraint(
            "project_id", "content_hash", name="uq_project_source_document_hash"
        ),
    )
    op.create_index(
        "ix_project_source_documents_project_status",
        "project_source_documents",
        ["project_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_project_source_documents_project_status", table_name="project_source_documents")
    op.drop_table("project_source_documents")

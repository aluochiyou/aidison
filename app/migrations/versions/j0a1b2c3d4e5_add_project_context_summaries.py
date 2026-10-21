"""add_project_context_summaries

Revision ID: j0a1b2c3d4e5
Revises: c1d2e3f4a5b6
Create Date: 2026-08-12 00:15:00.000000

Derived conversation summaries are append-only, project/session-scoped model
context.  They do not replace conversation turns or project facts.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "j0a1b2c3d4e5"
down_revision: str | Sequence[str] | None = "c1d2e3f4a5b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_conversation_session_project_id",
        "conversation_sessions",
        ["project_id", "id"],
    )
    op.create_table(
        "context_summaries",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("session_id", sa.UUID(), nullable=False),
        sa.Column("source_start_sequence", sa.Integer(), nullable=False),
        sa.Column("source_end_sequence", sa.Integer(), nullable=False),
        sa.Column("source_event_cursor", sa.Integer(), nullable=False),
        sa.Column("basis_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "source_end_sequence >= source_start_sequence",
            name=op.f("ck_context_summaries_source_sequence_range"),
        ),
        sa.CheckConstraint(
            "status IN ('active', 'superseded', 'tombstoned')",
            name=op.f("ck_context_summaries_status"),
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "session_id"],
            ["conversation_sessions.project_id", "conversation_sessions.id"],
            name=op.f("fk_context_summaries_project_session"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_context_summaries")),
        sa.UniqueConstraint(
            "project_id",
            "id",
            name="uq_context_summary_project_id",
        ),
        sa.UniqueConstraint(
            "session_id",
            "source_start_sequence",
            "source_end_sequence",
            "basis_hash",
            name="uq_context_summary_source_basis",
        ),
    )
    op.create_index(
        "ix_context_summaries_project_session_active",
        "context_summaries",
        ["project_id", "session_id", "status", "source_end_sequence"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_context_summaries_project_session_active",
        table_name="context_summaries",
    )
    op.drop_table("context_summaries")
    op.drop_constraint(
        "uq_conversation_session_project_id",
        "conversation_sessions",
        type_="unique",
    )

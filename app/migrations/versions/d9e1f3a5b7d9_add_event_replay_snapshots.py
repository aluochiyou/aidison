"""add verified event replay snapshots

Revision ID: d9e1f3a5b7d9
Revises: c8d0f2a4b6c8
Create Date: 2026-10-07 03:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d9e1f3a5b7d9"
down_revision: str | Sequence[str] | None = "c8d0f2a4b6c8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "event_replay_snapshots",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("aggregate_type", sa.String(length=80), nullable=False),
        sa.Column("aggregate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("aggregate_version", sa.Integer(), nullable=False),
        sa.Column("project_seq", sa.Integer(), nullable=False),
        sa.Column("reducer_version", sa.String(length=120), nullable=False),
        sa.Column("state", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("state_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("aggregate_version >= 1", name="aggregate_version_positive"),
        sa.CheckConstraint("project_seq >= 1", name="project_seq_positive"),
        sa.CheckConstraint("char_length(state_hash) = 64", name="state_hash_length"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "project_id",
            "aggregate_type",
            "aggregate_id",
            "aggregate_version",
            "reducer_version",
            name="uq_replay_snapshot_aggregate_version",
        ),
    )
    op.create_index(
        "ix_replay_snapshot_latest",
        "event_replay_snapshots",
        [
            "project_id",
            "aggregate_type",
            "aggregate_id",
            "reducer_version",
            "aggregate_version",
        ],
        unique=False,
    )


def downgrade() -> None:
    has_snapshots = op.get_bind().execute(
        sa.text("SELECT EXISTS (SELECT 1 FROM event_replay_snapshots)")
    ).scalar_one()
    if has_snapshots:
        raise RuntimeError(
            "refusing to drop event replay snapshots while verified snapshots exist"
        )
    op.drop_index("ix_replay_snapshot_latest", table_name="event_replay_snapshots")
    op.drop_table("event_replay_snapshots")

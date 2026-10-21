"""add versioned domain event envelope

Revision ID: e4f6a8b0c2d4
Revises: z3c4d5e6f7a8
Create Date: 2026-10-06 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e4f6a8b0c2d4"
down_revision: str | Sequence[str] | None = "z3c4d5e6f7a8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "domain_events",
        sa.Column(
            "event_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
    )
    op.add_column(
        "domain_events",
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "domain_events",
        sa.Column("aggregate_type", sa.String(length=80), nullable=True),
    )
    op.add_column(
        "domain_events",
        sa.Column("aggregate_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "domain_events",
        sa.Column("aggregate_version", sa.Integer(), nullable=True),
    )
    op.add_column(
        "domain_events",
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=True,
            server_default=sa.text("now()"),
        ),
    )
    op.add_column(
        "domain_events",
        sa.Column("payload_hash", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "domain_events",
        sa.Column("correlation_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "domain_events",
        sa.Column("causation_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "domain_events",
        sa.Column("actor", sa.String(length=80), nullable=True),
    )
    op.add_column(
        "domain_events",
        sa.Column("source_component", sa.String(length=100), nullable=True),
    )
    op.add_column(
        "domain_events",
        sa.Column("artifact_refs", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )

    # Existing rows are explicitly legacy schema 0.  Deterministic IDs and
    # occurrence time preserve their current identity without inventing
    # aggregate versions or payload hashes that did not exist at write time.
    op.execute(
        """
        UPDATE domain_events
        SET event_id = lpad(to_hex(id), 32, '0')::uuid,
            occurred_at = created_at
        """
    )
    op.alter_column("domain_events", "event_id", nullable=False)
    op.alter_column("domain_events", "occurred_at", nullable=False)

    op.create_unique_constraint(op.f("uq_event_id"), "domain_events", ["event_id"])
    op.create_check_constraint(
        op.f("ck_domain_events_schema_version_nonnegative"),
        "domain_events",
        "schema_version >= 0",
    )
    op.create_check_constraint(
        op.f("ck_domain_events_aggregate_version_positive"),
        "domain_events",
        "aggregate_version IS NULL OR aggregate_version >= 1",
    )
    op.create_check_constraint(
        op.f("ck_domain_events_versioned_event_metadata_complete"),
        "domain_events",
        "schema_version = 0 OR ("
        "aggregate_type IS NOT NULL AND aggregate_id IS NOT NULL AND "
        "aggregate_version IS NOT NULL AND payload_hash IS NOT NULL AND "
        "correlation_id IS NOT NULL AND actor IS NOT NULL AND "
        "source_component IS NOT NULL AND artifact_refs IS NOT NULL)",
    )
    op.create_index(
        "uq_events_aggregate_version",
        "domain_events",
        ["project_id", "aggregate_type", "aggregate_id", "aggregate_version"],
        unique=True,
        postgresql_where=sa.text("aggregate_id IS NOT NULL"),
    )


def downgrade() -> None:
    has_versioned_events = op.get_bind().execute(
        sa.text("SELECT EXISTS (SELECT 1 FROM domain_events WHERE schema_version > 0)")
    ).scalar_one()
    if has_versioned_events:
        raise RuntimeError(
            "refusing to drop versioned event metadata while schema_version > 0 events exist"
        )
    op.drop_index("uq_events_aggregate_version", table_name="domain_events")
    op.drop_constraint(
        op.f("ck_domain_events_versioned_event_metadata_complete"),
        "domain_events",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_domain_events_aggregate_version_positive"),
        "domain_events",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_domain_events_schema_version_nonnegative"),
        "domain_events",
        type_="check",
    )
    op.drop_constraint(op.f("uq_event_id"), "domain_events", type_="unique")
    op.drop_column("domain_events", "artifact_refs")
    op.drop_column("domain_events", "source_component")
    op.drop_column("domain_events", "actor")
    op.drop_column("domain_events", "causation_id")
    op.drop_column("domain_events", "correlation_id")
    op.drop_column("domain_events", "payload_hash")
    op.drop_column("domain_events", "occurred_at")
    op.drop_column("domain_events", "aggregate_version")
    op.drop_column("domain_events", "aggregate_id")
    op.drop_column("domain_events", "aggregate_type")
    op.drop_column("domain_events", "schema_version")
    op.drop_column("domain_events", "event_id")

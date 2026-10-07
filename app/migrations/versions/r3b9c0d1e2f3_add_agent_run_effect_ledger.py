"""add AgentRun external effect ledger

Revision ID: r3b9c0d1e2f3
Revises: r3a8b9c0d1e2
Create Date: 2026-09-04 16:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "r3b9c0d1e2f3"
down_revision: str | Sequence[str] | None = "r3a8b9c0d1e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_run_effects",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("agent_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("basis_hash", sa.String(length=64), nullable=False),
        sa.Column("approval_ref", sa.String(length=500), nullable=False),
        sa.Column("effect_kind", sa.String(length=100), nullable=False),
        sa.Column("provider", sa.String(length=100), nullable=False),
        sa.Column("external_idempotency_key", sa.String(length=300), nullable=False),
        sa.Column("idempotency_key", sa.String(length=300), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("request_artifact_ref", sa.String(length=500), nullable=False),
        sa.Column("claim_generation", sa.Integer(), nullable=False),
        sa.Column("lease_token", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("state", sa.String(length=40), nullable=False),
        sa.Column("provider_effect_id", sa.String(length=300), nullable=True),
        sa.Column("response_artifact_ref", sa.String(length=500), nullable=True),
        sa.Column("failure_ref", sa.String(length=500), nullable=True),
        sa.Column("normalized_error", sa.Text(), nullable=True),
        sa.Column("reconciliation_artifact_ref", sa.String(length=500), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reconciled_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "claim_generation >= 1", name=op.f("ck_agent_run_effects_generation_positive")
        ),
        sa.CheckConstraint(
            "state IN ('prepared', 'dispatched', 'succeeded', 'failed', 'ambiguous')",
            name=op.f("ck_agent_run_effects_state"),
        ),
        sa.ForeignKeyConstraint(["agent_run_id"], ["agent_runs.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("idempotency_key", name="uq_agent_run_effect_idempotency"),
        sa.UniqueConstraint(
            "provider", "external_idempotency_key", name="uq_agent_run_effect_external_idempotency"
        ),
    )
    op.create_index("ix_agent_run_effects_run", "agent_run_effects", ["agent_run_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_agent_run_effects_run", table_name="agent_run_effects")
    op.drop_table("agent_run_effects")

"""add scoped effect approvals

Revision ID: b7d3e5f91a20
Revises: f405db1bd29e
Create Date: 2026-08-08 03:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b7d3e5f91a20"
down_revision: str | Sequence[str] | None = "f405db1bd29e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "effect_approvals",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("effect_kind", sa.String(length=100), nullable=False),
        sa.Column("target_ref", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("basis_hash", sa.String(length=64), nullable=False),
        sa.Column("scope_hash", sa.String(length=64), nullable=False),
        sa.Column("constraints", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "target_ref"],
            ["purchase_proposals.project_id", "purchase_proposals.id"],
            name="fk_effect_approval_project_proposal",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "status IN ('requested', 'approved', 'denied', 'expired', 'consumed')",
            name="effect_approval_status",
        ),
        sa.CheckConstraint(
            "expires_at > requested_at", name="effect_approval_expiry_after_request"
        ),
        sa.CheckConstraint("status = payload->>'status'", name="effect_approval_payload_status"),
        sa.UniqueConstraint("project_id", "id", name="uq_effect_approval_project_id"),
    )
    op.create_index(
        "ix_effect_approvals_project_status",
        "effect_approvals",
        ["project_id", "status"],
        unique=False,
    )
    op.create_index(
        "uq_effect_approvals_live_scope",
        "effect_approvals",
        ["project_id", "scope_hash"],
        unique=True,
        postgresql_where=sa.text("status IN ('requested', 'approved')"),
    )
    op.execute(
        """
        CREATE FUNCTION reject_effect_approval_scope_mutation() RETURNS trigger AS $$
        BEGIN
            IF NEW.project_id IS DISTINCT FROM OLD.project_id
                OR NEW.effect_kind IS DISTINCT FROM OLD.effect_kind
                OR NEW.target_ref IS DISTINCT FROM OLD.target_ref
                OR NEW.basis_hash IS DISTINCT FROM OLD.basis_hash
                OR NEW.scope_hash IS DISTINCT FROM OLD.scope_hash
                OR NEW.constraints IS DISTINCT FROM OLD.constraints
                OR NEW.requested_at IS DISTINCT FROM OLD.requested_at
                OR NEW.expires_at IS DISTINCT FROM OLD.expires_at
                OR (NEW.payload - ARRAY['status', 'resolved_at', 'consumed_at',
                    'resolution_reason']) IS DISTINCT FROM
                   (OLD.payload - ARRAY['status', 'resolved_at', 'consumed_at',
                    'resolution_reason'])
            THEN
                RAISE EXCEPTION 'EffectApproval scope is immutable';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER effect_approval_scope_is_immutable
        BEFORE UPDATE ON effect_approvals
        FOR EACH ROW EXECUTE FUNCTION reject_effect_approval_scope_mutation()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER effect_approval_scope_is_immutable ON effect_approvals")
    op.execute("DROP FUNCTION reject_effect_approval_scope_mutation()")
    op.drop_index("uq_effect_approvals_live_scope", table_name="effect_approvals")
    op.drop_index("ix_effect_approvals_project_status", table_name="effect_approvals")
    op.drop_table("effect_approvals")

"""add durable plan revisions

Revision ID: 6b2c9f0d3e41
Revises: 8a1f3c5e7b92
Create Date: 2026-08-07 05:25:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "6b2c9f0d3e41"
down_revision: str | Sequence[str] | None = "8a1f3c5e7b92"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "plan_revisions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("root_job_id", sa.UUID(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("parent_revision", sa.Integer(), nullable=True),
        sa.Column("basis_hash", sa.String(length=64), nullable=False),
        sa.Column("basis_project_revision", sa.Integer(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("evidence_refs", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("planner_profile_id", sa.String(length=200), nullable=False),
        sa.Column("planner_profile_revision", sa.Integer(), nullable=False),
        sa.Column("plan_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "basis_project_revision >= 1", name=op.f("ck_plan_revisions_basis_revision_positive")
        ),
        sa.CheckConstraint("revision >= 1", name=op.f("ck_plan_revisions_revision_positive")),
        sa.ForeignKeyConstraint(
            ["planner_profile_id", "planner_profile_revision"],
            ["agent_profile_revisions.profile_id", "agent_profile_revisions.revision"],
            name="fk_plan_revision_planner_profile",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["root_job_id"],
            ["jobs.id"],
            name=op.f("fk_plan_revisions_root_job_id_jobs"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["root_job_id", "parent_revision"],
            ["plan_revisions.root_job_id", "plan_revisions.revision"],
            name="fk_plan_revision_parent",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_plan_revisions")),
        sa.UniqueConstraint("root_job_id", "plan_hash", name="uq_plan_root_hash"),
        sa.UniqueConstraint("root_job_id", "id", name="uq_plan_root_id"),
        sa.UniqueConstraint("root_job_id", "revision", name="uq_plan_root_revision"),
    )
    op.create_index(
        "ix_plan_revisions_root", "plan_revisions", ["root_job_id", "revision"], unique=False
    )

    op.create_table(
        "plan_heads",
        sa.Column("root_job_id", sa.UUID(), nullable=False),
        sa.Column("current_revision", sa.Integer(), nullable=False),
        sa.Column("current_plan_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "current_revision >= 1", name=op.f("ck_plan_heads_current_revision_positive")
        ),
        sa.ForeignKeyConstraint(
            ["root_job_id"],
            ["jobs.id"],
            name=op.f("fk_plan_heads_root_job_id_jobs"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["root_job_id", "current_revision"],
            ["plan_revisions.root_job_id", "plan_revisions.revision"],
            name="fk_plan_head_current_revision",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("root_job_id", name=op.f("pk_plan_heads")),
    )

    op.create_table(
        "plan_tasks",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("plan_revision_id", sa.UUID(), nullable=False),
        sa.Column("logical_key", sa.String(length=120), nullable=False),
        sa.Column("objective", sa.Text(), nullable=False),
        sa.Column("mode", sa.String(length=40), nullable=False),
        sa.Column("role_key", sa.String(length=120), nullable=False),
        sa.Column("profile_id", sa.String(length=200), nullable=False),
        sa.Column("profile_revision", sa.Integer(), nullable=False),
        sa.Column("budget_ref", sa.String(length=300), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("depth", sa.Integer(), nullable=False),
        sa.Column("input_refs", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("success_criteria", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("stop_criteria", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("dispatched_job_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "depth >= 0 AND depth <= 4", name=op.f("ck_plan_tasks_depth_within_limit")
        ),
        sa.CheckConstraint(
            "status IN ('planned', 'ready', 'dispatched', 'running', 'succeeded', 'failed', "
            "'blocked', 'cancelled', 'superseded')",
            name=op.f("ck_plan_tasks_status"),
        ),
        sa.ForeignKeyConstraint(
            ["dispatched_job_id"],
            ["jobs.id"],
            name=op.f("fk_plan_tasks_dispatched_job_id_jobs"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["plan_revision_id"],
            ["plan_revisions.id"],
            name=op.f("fk_plan_tasks_plan_revision_id_plan_revisions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["profile_id", "profile_revision"],
            ["agent_profile_revisions.profile_id", "agent_profile_revisions.revision"],
            name="fk_plan_task_profile",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_plan_tasks")),
        sa.UniqueConstraint("dispatched_job_id", name="uq_plan_task_dispatched_job"),
        sa.UniqueConstraint("plan_revision_id", "logical_key", name="uq_plan_task_logical_key"),
        sa.UniqueConstraint("plan_revision_id", "id", name="uq_plan_task_revision_id"),
    )
    op.create_index(
        "ix_plan_tasks_frontier",
        "plan_tasks",
        ["plan_revision_id", "status", "depth", "logical_key"],
        unique=False,
    )

    op.create_table(
        "plan_task_edges",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("plan_revision_id", sa.UUID(), nullable=False),
        sa.Column("from_task_id", sa.UUID(), nullable=False),
        sa.Column("to_task_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.CheckConstraint("from_task_id <> to_task_id", name=op.f("ck_plan_task_edges_not_self")),
        sa.CheckConstraint(
            "kind IN ('depends_on', 'evidence_from', 'verifies', 'blocks')",
            name=op.f("ck_plan_task_edges_kind"),
        ),
        sa.ForeignKeyConstraint(
            ["plan_revision_id", "from_task_id"],
            ["plan_tasks.plan_revision_id", "plan_tasks.id"],
            name="fk_plan_edge_from_same_revision",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["plan_revision_id"],
            ["plan_revisions.id"],
            name=op.f("fk_plan_task_edges_plan_revision_id_plan_revisions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["plan_revision_id", "to_task_id"],
            ["plan_tasks.plan_revision_id", "plan_tasks.id"],
            name="fk_plan_edge_to_same_revision",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_plan_task_edges")),
        sa.UniqueConstraint(
            "plan_revision_id", "from_task_id", "to_task_id", "kind", name="uq_edge"
        ),
    )
    op.create_index(
        "ix_plan_edges_target",
        "plan_task_edges",
        ["plan_revision_id", "to_task_id", "kind"],
        unique=False,
    )

    op.create_table(
        "plan_gaps",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("root_job_id", sa.UUID(), nullable=False),
        sa.Column("plan_revision_id", sa.UUID(), nullable=False),
        sa.Column("source_task_id", sa.UUID(), nullable=True),
        sa.Column("source_result_id", sa.UUID(), nullable=True),
        sa.Column("gap_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('open', 'accepted', 'rejected', 'resolved')",
            name=op.f("ck_plan_gaps_status"),
        ),
        sa.ForeignKeyConstraint(
            ["root_job_id", "plan_revision_id"],
            ["plan_revisions.root_job_id", "plan_revisions.id"],
            name="fk_plan_gap_same_root_revision",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["root_job_id"],
            ["jobs.id"],
            name=op.f("fk_plan_gaps_root_job_id_jobs"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_result_id"],
            ["attempt_results.id"],
            name=op.f("fk_plan_gaps_source_result_id_attempt_results"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["plan_revision_id", "source_task_id"],
            ["plan_tasks.plan_revision_id", "plan_tasks.id"],
            name="fk_plan_gap_source_same_revision",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_plan_gaps")),
        sa.UniqueConstraint("root_job_id", "plan_revision_id", "gap_hash", name="uq_plan_gap"),
    )
    op.create_index("ix_plan_gaps_open", "plan_gaps", ["root_job_id", "status"], unique=False)

    op.create_table(
        "plan_patches",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("root_job_id", sa.UUID(), nullable=False),
        sa.Column("base_revision", sa.Integer(), nullable=False),
        sa.Column("target_revision", sa.Integer(), nullable=False),
        sa.Column("patch_hash", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("trigger", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind IN ('expand', 'revise', 'contract')", name=op.f("ck_plan_patches_kind")
        ),
        sa.ForeignKeyConstraint(
            ["root_job_id"],
            ["jobs.id"],
            name=op.f("fk_plan_patches_root_job_id_jobs"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["root_job_id", "base_revision"],
            ["plan_revisions.root_job_id", "plan_revisions.revision"],
            name="fk_plan_patch_base_revision",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["root_job_id", "target_revision"],
            ["plan_revisions.root_job_id", "plan_revisions.revision"],
            name="fk_plan_patch_target_revision",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_plan_patches")),
        sa.UniqueConstraint("root_job_id", "base_revision", "patch_hash", name="uq_plan_patch"),
    )
    op.create_index(
        "ix_plan_patches_root_base", "plan_patches", ["root_job_id", "base_revision"], unique=False
    )

    op.create_table(
        "replan_receipts",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("root_job_id", sa.UUID(), nullable=False),
        sa.Column("parent_attempt_id", sa.UUID(), nullable=False),
        sa.Column("parent_claim_generation", sa.Integer(), nullable=False),
        sa.Column("basis_hash", sa.String(length=64), nullable=False),
        sa.Column("base_revision", sa.Integer(), nullable=False),
        sa.Column("patch_hash", sa.String(length=64), nullable=False),
        sa.Column("new_plan_revision_id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "parent_claim_generation >= 1", name=op.f("ck_replan_receipts_generation_positive")
        ),
        sa.ForeignKeyConstraint(
            ["root_job_id", "new_plan_revision_id"],
            ["plan_revisions.root_job_id", "plan_revisions.id"],
            name="fk_replan_new_revision_same_root",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["parent_attempt_id"],
            ["attempts.id"],
            name=op.f("fk_replan_receipts_parent_attempt_id_attempts"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["root_job_id"],
            ["jobs.id"],
            name=op.f("fk_replan_receipts_root_job_id_jobs"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_replan_receipts")),
        sa.UniqueConstraint("new_plan_revision_id", name="uq_replan_new_revision"),
        sa.UniqueConstraint(
            "root_job_id",
            "parent_claim_generation",
            "base_revision",
            "patch_hash",
            name="uq_replan_cas",
        ),
    )
    op.execute(
        """
        CREATE FUNCTION aidison_reject_plan_revision_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'PlanRevision is immutable';
        END;
        $$ LANGUAGE plpgsql;
        CREATE TRIGGER plan_revisions_are_immutable
        BEFORE UPDATE OR DELETE ON plan_revisions
        FOR EACH ROW EXECUTE FUNCTION aidison_reject_plan_revision_mutation();
        """
    )
    op.execute(
        """
        CREATE FUNCTION aidison_reject_plan_task_definition_mutation()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'PlanTask definition is immutable';
            END IF;
            IF NEW.plan_revision_id IS DISTINCT FROM OLD.plan_revision_id
               OR NEW.logical_key IS DISTINCT FROM OLD.logical_key
               OR NEW.objective IS DISTINCT FROM OLD.objective
               OR NEW.mode IS DISTINCT FROM OLD.mode
               OR NEW.role_key IS DISTINCT FROM OLD.role_key
               OR NEW.profile_id IS DISTINCT FROM OLD.profile_id
               OR NEW.profile_revision IS DISTINCT FROM OLD.profile_revision
               OR NEW.budget_ref IS DISTINCT FROM OLD.budget_ref
               OR NEW.depth IS DISTINCT FROM OLD.depth
               OR NEW.input_refs IS DISTINCT FROM OLD.input_refs
               OR NEW.success_criteria IS DISTINCT FROM OLD.success_criteria
               OR NEW.stop_criteria IS DISTINCT FROM OLD.stop_criteria
               OR NEW.created_at IS DISTINCT FROM OLD.created_at
            THEN
                RAISE EXCEPTION 'PlanTask definition is immutable';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        CREATE TRIGGER plan_task_definitions_are_immutable
        BEFORE UPDATE OR DELETE ON plan_tasks
        FOR EACH ROW EXECUTE FUNCTION aidison_reject_plan_task_definition_mutation();
        """
    )
    op.execute(
        """
        CREATE FUNCTION aidison_reject_plan_edge_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'PlanTaskEdge is immutable';
        END;
        $$ LANGUAGE plpgsql;
        CREATE TRIGGER plan_task_edges_are_immutable
        BEFORE UPDATE OR DELETE ON plan_task_edges
        FOR EACH ROW EXECUTE FUNCTION aidison_reject_plan_edge_mutation();
        """
    )
    op.execute(
        """
        CREATE FUNCTION aidison_reject_plan_receipt_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'Plan receipt is immutable';
        END;
        $$ LANGUAGE plpgsql;
        CREATE TRIGGER plan_patches_are_immutable
        BEFORE UPDATE OR DELETE ON plan_patches
        FOR EACH ROW EXECUTE FUNCTION aidison_reject_plan_receipt_mutation();
        CREATE TRIGGER replan_receipts_are_immutable
        BEFORE UPDATE OR DELETE ON replan_receipts
        FOR EACH ROW EXECUTE FUNCTION aidison_reject_plan_receipt_mutation();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER replan_receipts_are_immutable ON replan_receipts")
    op.execute("DROP TRIGGER plan_patches_are_immutable ON plan_patches")
    op.execute("DROP FUNCTION aidison_reject_plan_receipt_mutation()")
    op.execute("DROP TRIGGER plan_task_edges_are_immutable ON plan_task_edges")
    op.execute("DROP FUNCTION aidison_reject_plan_edge_mutation()")
    op.execute("DROP TRIGGER plan_task_definitions_are_immutable ON plan_tasks")
    op.execute("DROP FUNCTION aidison_reject_plan_task_definition_mutation()")
    op.execute("DROP TRIGGER plan_revisions_are_immutable ON plan_revisions")
    op.execute("DROP FUNCTION aidison_reject_plan_revision_mutation()")
    op.drop_table("replan_receipts")
    op.drop_index("ix_plan_patches_root_base", table_name="plan_patches")
    op.drop_table("plan_patches")
    op.drop_index("ix_plan_gaps_open", table_name="plan_gaps")
    op.drop_table("plan_gaps")
    op.drop_index("ix_plan_edges_target", table_name="plan_task_edges")
    op.drop_table("plan_task_edges")
    op.drop_index("ix_plan_tasks_frontier", table_name="plan_tasks")
    op.drop_table("plan_tasks")
    op.drop_table("plan_heads")
    op.drop_index("ix_plan_revisions_root", table_name="plan_revisions")
    op.drop_table("plan_revisions")

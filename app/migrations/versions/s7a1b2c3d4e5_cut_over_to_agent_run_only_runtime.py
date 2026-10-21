"""cut over durable runtime storage to AgentRun ownership only

Revision ID: s7a1b2c3d4e5
Revises: r6a1b2c3d4e5
Create Date: 2026-09-09 10:00:00.000000

The retired Job runtime cannot be made truthful by attaching its historical
attempts to unrelated AgentRuns.  Its artifacts and invocation records are
therefore deliberately retired during this one-way cutover.  AgentRun-owned
rows are retained and become the only legal runtime lineage.
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "s7a1b2c3d4e5"
down_revision: str | Sequence[str] | None = "r6a1b2c3d4e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A legacy artifact has Job/Attempt lineage by construction and cannot be
    # relabeled as an AgentRun artifact without inventing an authority.  Retire
    # its metadata before removing the legacy foreign keys and check constraint.
    op.execute("DELETE FROM artifacts WHERE agent_run_id IS NULL")
    # These names predate the current metadata convention.  Use exact DDL so
    # Alembic does not apply the convention a second time while removing them.
    op.execute(
        "ALTER TABLE artifacts DROP CONSTRAINT "
        "ck_artifacts_ck_artifacts_lineage_exactly_one_runtime_owner"
    )
    op.execute("ALTER TABLE artifacts DROP CONSTRAINT uq_artifact_attempt_kind_hash")
    op.execute("ALTER TABLE artifacts DROP CONSTRAINT fk_artifacts_attempt_id_attempts")
    op.execute("ALTER TABLE artifacts DROP CONSTRAINT fk_artifacts_job_id_jobs")
    op.drop_column("artifacts", "attempt_id")
    op.drop_column("artifacts", "job_id")
    op.alter_column("artifacts", "agent_run_id", nullable=False)

    # Invocation recordings belong to the retired Job/Attempt runtime.  Their
    # idempotency keys must not be reused as AgentRun invocations, so discard
    # them before repointing the table at the current ownership contract.
    op.execute("DELETE FROM invocation_recordings")
    op.drop_index("ix_invocation_recordings_attempt", table_name="invocation_recordings")
    op.execute(
        "ALTER TABLE invocation_recordings DROP CONSTRAINT "
        "fk_invocation_recordings_attempt_id_attempts"
    )
    op.execute(
        "ALTER TABLE invocation_recordings DROP CONSTRAINT fk_invocation_recordings_job_id_jobs"
    )
    op.alter_column(
        "invocation_recordings",
        "job_id",
        new_column_name="agent_run_id",
        existing_type=postgresql.UUID(as_uuid=True),
        existing_nullable=False,
    )
    op.alter_column(
        "invocation_recordings",
        "attempt_id",
        new_column_name="producer_attempt_id",
        existing_type=postgresql.UUID(as_uuid=True),
        existing_nullable=False,
    )
    op.create_foreign_key(
        "fk_invocation_recordings_agent_run_id_agent_runs",
        "invocation_recordings",
        "agent_runs",
        ["agent_run_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_invocation_recordings_agent_run",
        "invocation_recordings",
        ["agent_run_id", "created_at"],
        unique=False,
    )

    # AdjustmentBatch stores its Domain payload as JSONB.  Remove the obsolete
    # Job reference without changing the batch's user-visible lifecycle facts.
    op.execute(
        "UPDATE adjustment_batches "
        "SET payload = payload - 'analysis_job_id' "
        "WHERE payload ? 'analysis_job_id'"
    )

    # Drop the old scheduler in dependency order.  New AgentRun, LangGraph
    # checkpoint, budget, result-admission and effect-ledger tables are not in
    # this list and remain the sole durable runtime authority.
    for table_name in (
        "result_admissions",
        "plan_gaps",
        "plan_patches",
        "replan_receipts",
        "plan_task_claims",
        "plan_task_edges",
        "plan_heads",
        "plan_tasks",
        "plan_revisions",
        "join_receipts",
        "delegations",
        "join_groups",
        "attempt_results",
        "budget_operations",
        "budget_allocations",
        "job_profile_bindings",
        "agent_profile_active",
        "attempts",
        "budget_accounts",
        "jobs",
        "agent_profile_revisions",
    ):
        op.drop_table(table_name)


def downgrade() -> None:
    raise NotImplementedError(
        "The AgentRun-only runtime cutover permanently retires legacy Job records. "
        "Restore a database backup instead of attempting a lossy downgrade."
    )

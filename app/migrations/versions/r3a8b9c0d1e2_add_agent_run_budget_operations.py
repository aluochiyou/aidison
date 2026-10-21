"""add AgentRun-scoped budget operations

Revision ID: r3a8b9c0d1e2
Revises: q7b8c9d0e1f2
Create Date: 2026-09-04 15:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "r3a8b9c0d1e2"
down_revision: str | Sequence[str] | None = "q7b8c9d0e1f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_run_budget_accounts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("agent_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token_cap", sa.BigInteger(), nullable=False),
        sa.Column("tool_call_cap", sa.Integer(), nullable=False),
        sa.Column("token_reserved", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("tool_calls_reserved", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("token_consumed", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("tool_calls_consumed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("state", sa.String(length=40), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "token_cap >= 0", name=op.f("ck_agent_run_budget_accounts_token_cap_nonnegative")
        ),
        sa.CheckConstraint(
            "tool_call_cap >= 0", name=op.f("ck_agent_run_budget_accounts_tool_cap_nonnegative")
        ),
        sa.CheckConstraint(
            "token_reserved >= 0",
            name=op.f("ck_agent_run_budget_accounts_token_reserved_nonnegative"),
        ),
        sa.CheckConstraint(
            "tool_calls_reserved >= 0",
            name=op.f("ck_agent_run_budget_accounts_tool_reserved_nonnegative"),
        ),
        sa.CheckConstraint(
            "token_consumed >= 0",
            name=op.f("ck_agent_run_budget_accounts_token_consumed_nonnegative"),
        ),
        sa.CheckConstraint(
            "tool_calls_consumed >= 0",
            name=op.f("ck_agent_run_budget_accounts_tool_consumed_nonnegative"),
        ),
        sa.CheckConstraint(
            "token_reserved + token_consumed <= token_cap",
            name=op.f("ck_agent_run_budget_accounts_token_usage_within_cap"),
        ),
        sa.CheckConstraint(
            "tool_calls_reserved + tool_calls_consumed <= tool_call_cap",
            name=op.f("ck_agent_run_budget_accounts_tool_usage_within_cap"),
        ),
        sa.CheckConstraint(
            "state IN ('open', 'closed', 'cancelled')",
            name=op.f("ck_agent_run_budget_accounts_state"),
        ),
        sa.ForeignKeyConstraint(["agent_run_id"], ["agent_runs.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("agent_run_id", name="uq_agent_run_budget_account_run"),
    )
    op.create_table(
        "agent_run_budget_operations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("claim_generation", sa.Integer(), nullable=False),
        sa.Column("lease_token", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("logical_step", sa.String(length=200), nullable=False),
        sa.Column("physical_attempt_no", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=300), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=100), nullable=False),
        sa.Column("target", sa.String(length=200), nullable=False),
        sa.Column("state", sa.String(length=40), nullable=False),
        sa.Column("reserved_tokens", sa.BigInteger(), nullable=False),
        sa.Column("reserved_tool_calls", sa.Integer(), nullable=False),
        sa.Column("consumed_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("consumed_tool_calls", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("provider_request_id", sa.String(length=300), nullable=True),
        sa.Column("request_artifact_ref", sa.String(length=500), nullable=True),
        sa.Column("response_artifact_ref", sa.String(length=500), nullable=True),
        sa.Column("normalized_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "claim_generation >= 1", name=op.f("ck_agent_run_budget_operations_generation_positive")
        ),
        sa.CheckConstraint(
            "kind IN ('model', 'tool')", name=op.f("ck_agent_run_budget_operations_kind")
        ),
        sa.CheckConstraint(
            "state IN ('reserved', 'dispatched', 'settled', 'released', 'ambiguous')",
            name=op.f("ck_agent_run_budget_operations_state"),
        ),
        sa.CheckConstraint(
            "reserved_tokens >= 0",
            name=op.f("ck_agent_run_budget_operations_reserved_tokens_nonnegative"),
        ),
        sa.CheckConstraint(
            "reserved_tool_calls >= 0",
            name=op.f("ck_agent_run_budget_operations_reserved_tools_nonnegative"),
        ),
        sa.CheckConstraint(
            "consumed_tokens >= 0",
            name=op.f("ck_agent_run_budget_operations_consumed_tokens_nonnegative"),
        ),
        sa.CheckConstraint(
            "consumed_tool_calls >= 0",
            name=op.f("ck_agent_run_budget_operations_consumed_tools_nonnegative"),
        ),
        sa.CheckConstraint(
            "consumed_tokens <= reserved_tokens",
            name=op.f("ck_agent_run_budget_operations_consumed_tokens_within_reservation"),
        ),
        sa.CheckConstraint(
            "consumed_tool_calls <= reserved_tool_calls",
            name=op.f("ck_agent_run_budget_operations_consumed_tools_within_reservation"),
        ),
        sa.ForeignKeyConstraint(
            ["account_id"], ["agent_run_budget_accounts.id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint("idempotency_key", name="uq_agent_run_budget_operation_idempotency"),
    )
    op.create_index(
        "ix_agent_run_budget_operations_account",
        "agent_run_budget_operations",
        ["account_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_agent_run_budget_operations_account", table_name="agent_run_budget_operations"
    )
    op.drop_table("agent_run_budget_operations")
    op.drop_table("agent_run_budget_accounts")

"""The Alembic cutover leaves PostgreSQL with one AgentRun runtime authority."""

from __future__ import annotations

import os

import pytest
from sqlalchemy import text

from aidison.infrastructure.database import DatabaseSettings, create_engine, create_session_factory

pytestmark = pytest.mark.integration


LEGACY_TABLES = (
    "agent_profile_active",
    "agent_profile_revisions",
    "attempt_results",
    "attempts",
    "budget_accounts",
    "budget_allocations",
    "budget_operations",
    "delegations",
    "job_profile_bindings",
    "jobs",
    "join_groups",
    "join_receipts",
    "plan_gaps",
    "plan_heads",
    "plan_patches",
    "plan_revisions",
    "plan_task_claims",
    "plan_task_edges",
    "plan_tasks",
    "replan_receipts",
    "result_admissions",
)


@pytest.mark.asyncio
async def test_legacy_runtime_schema_is_replaced_by_agent_run_ownership() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")

    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            legacy_tables = (
                (
                    await session.execute(
                        text(
                            "SELECT table_name FROM information_schema.tables "
                            "WHERE table_schema = 'public' "
                            "AND table_name = ANY(CAST(:names AS text[]))"
                        ),
                        {"names": list(LEGACY_TABLES)},
                    )
                )
                .scalars()
                .all()
            )
            assert legacy_tables == []

            artifact_columns = dict(
                (
                    await session.execute(
                        text(
                            "SELECT column_name, is_nullable FROM information_schema.columns "
                            "WHERE table_schema = 'public' AND table_name = 'artifacts'"
                        )
                    )
                ).all()
            )
            assert artifact_columns["agent_run_id"] == "NO"
            assert "job_id" not in artifact_columns
            assert "attempt_id" not in artifact_columns

            invocation_columns = dict(
                (
                    await session.execute(
                        text(
                            "SELECT column_name, is_nullable FROM information_schema.columns "
                            "WHERE table_schema = 'public' AND table_name = 'invocation_recordings'"
                        )
                    )
                ).all()
            )
            assert invocation_columns["agent_run_id"] == "NO"
            assert invocation_columns["producer_attempt_id"] == "NO"
            assert "job_id" not in invocation_columns
            assert "attempt_id" not in invocation_columns

            invocation_owner = await session.scalar(
                text(
                    "SELECT confrelid::regclass::text FROM pg_constraint "
                    "WHERE conrelid = 'invocation_recordings'::regclass "
                    "AND contype = 'f' AND conkey = ARRAY["
                    "(SELECT attnum FROM pg_attribute "
                    "WHERE attrelid = 'invocation_recordings'::regclass "
                    "AND attname = 'agent_run_id')]"
                )
            )
            assert invocation_owner == "agent_runs"
    finally:
        await engine.dispose()

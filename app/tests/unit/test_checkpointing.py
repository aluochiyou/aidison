from __future__ import annotations

import os
from uuid import uuid4

import pytest
from psycopg import AsyncConnection, sql
from pydantic import ValidationError

from aidison.runtime.checkpoint_anchor import (
    CheckpointAnchorMissingError,
    verify_admitted_checkpoint_anchor,
)
from aidison.runtime.checkpointing import (
    CheckpointRuntime,
    CheckpointRuntimeNotStarted,
    CheckpointSettings,
    normalize_psycopg_url,
)
from aidison.runtime.identity import RuntimeBinding, RuntimeFamily
from aidison.runtime.minimal_graph import (
    Command,
    admitted_checkpoint_config,
    admitted_checkpoint_from_snapshot,
    build_minimal_checkpointed_graph,
    execution_checkpoint_thread_id,
    execution_thread_config,
    thread_config,
)


def test_checkpoint_settings_use_psycopg_url_and_safe_dedicated_schema() -> None:
    settings = CheckpointSettings(
        database_url="postgresql+asyncpg://aidison:secret@localhost:5432/aidison",
        schema_name="aidison_checkpoints",
    )

    assert settings.psycopg_url == "postgresql://aidison:secret@localhost:5432/aidison"
    assert normalize_psycopg_url("postgresql://localhost/aidison") == (
        "postgresql://localhost/aidison"
    )

    with pytest.raises(ValidationError, match="schema"):
        CheckpointSettings(schema_name="public; drop schema public")


@pytest.mark.asyncio
async def test_execution_generation_isolates_unadmitted_physical_checkpoints() -> None:
    """A retry starts cleanly; only an admitted anchor may resume prior graph state."""
    from langgraph.checkpoint.memory import MemorySaver

    graph = build_minimal_checkpointed_graph(checkpointer=MemorySaver())
    thread_id = f"generation-isolation-{uuid4()}"
    first_config = execution_thread_config(thread_id=thread_id, generation=1)
    first = await graph.ainvoke(
        {"run_id": "run-1", "prompt": "unadmitted first attempt"}, first_config
    )
    assert first["phase"] == "waiting_for_decision"
    first_snapshot = await graph.aget_state(first_config)
    assert first_snapshot.interrupts

    retry_config = execution_thread_config(thread_id=thread_id, generation=2)
    retried = await graph.ainvoke(
        {"run_id": "run-1", "prompt": "fresh retry attempt"}, retry_config
    )
    assert retried["phase"] == "waiting_for_decision"
    retry_snapshot = await graph.aget_state(retry_config)

    assert retry_snapshot.values["prompt"] == "fresh retry attempt"
    assert retry_snapshot.config["configurable"]["thread_id"] == execution_checkpoint_thread_id(
        logical_thread_id=thread_id,
        generation=2,
    )
    assert first_snapshot.config["configurable"]["thread_id"] == execution_checkpoint_thread_id(
        logical_thread_id=thread_id,
        generation=1,
    )


def test_execution_generation_must_be_positive() -> None:
    with pytest.raises(ValueError, match="generation must be positive"):
        execution_thread_config(thread_id="run", generation=0)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_checkpoint_runtime_sets_up_and_uses_its_dedicated_schema() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")

    schema_name = f"aidison_checkpoint_test_{uuid4().hex}"
    runtime = CheckpointRuntime(
        CheckpointSettings(
            database_url=database_url,
            schema_name=schema_name,
            min_pool_size=1,
            max_pool_size=1,
            setup_on_start=True,
        )
    )
    try:
        saver = await runtime.start()

        assert runtime.saver is saver
        assert await saver.aget({"configurable": {"thread_id": "checkpoint-test"}}) is None
    finally:
        await runtime.close()
        async with await AsyncConnection.connect(
            normalize_psycopg_url(database_url), autocommit=True
        ) as connection:
            await connection.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema_name))
            )

    with pytest.raises(CheckpointRuntimeNotStarted):
        _ = runtime.saver


@pytest.mark.integration
@pytest.mark.asyncio
async def test_admitted_checkpoint_anchor_resumes_instead_of_physical_latest() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")

    schema_name = f"aidison_minimal_graph_test_{uuid4().hex}"
    runtime = CheckpointRuntime(
        CheckpointSettings(
            database_url=database_url,
            schema_name=schema_name,
            min_pool_size=1,
            max_pool_size=1,
        )
    )
    try:
        graph = build_minimal_checkpointed_graph(checkpointer=await runtime.start())
        initial_config = thread_config(thread_id=f"minimal-run-{uuid4()}")
        interrupted = await graph.ainvoke(
            {"run_id": "run-1", "prompt": "original approved prompt"},
            initial_config,
        )
        assert interrupted["phase"] == "waiting_for_decision"
        snapshot = await graph.aget_state(initial_config)
        assert snapshot.interrupts
        binding = RuntimeBinding(
            runtime_family=RuntimeFamily.LANGGRAPH_V1,
            runtime_revision="runtime-v1",
            graph_key="minimal-runtime",
            graph_revision="minimal-v1",
            state_schema_version="minimal-state-v1",
            profile_binding_ref="profile://minimal/1",
            policy_binding_ref="policy://minimal/1",
        )
        admitted = admitted_checkpoint_from_snapshot(
            snapshot=snapshot,
            binding=binding,
            generation=1,
        )
        await verify_admitted_checkpoint_anchor(
            checkpointer=runtime.saver,
            checkpoint=admitted,
        )
        with pytest.raises(CheckpointAnchorMissingError, match="absent"):
            await verify_admitted_checkpoint_anchor(
                checkpointer=runtime.saver,
                checkpoint=admitted.model_copy(update={"checkpoint_id": "missing-checkpoint"}),
            )

        await graph.aupdate_state(initial_config, {"prompt": "unsafe physical latest"})
        latest = await graph.aget_state(initial_config)
        assert latest.values["prompt"] == "unsafe physical latest"

        resumed = await graph.ainvoke(
            Command(resume="approved"),
            admitted_checkpoint_config(admitted),
        )
        assert resumed["phase"] == "completed"
        assert resumed["approved_prompt"] == "original approved prompt"
        assert resumed["decision"] == "approved"
    finally:
        await runtime.close()
        async with await AsyncConnection.connect(
            normalize_psycopg_url(database_url), autocommit=True
        ) as connection:
            await connection.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema_name))
            )

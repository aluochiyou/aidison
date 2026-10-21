"""PostgreSQL API evidence for governed conversation change proposals."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import httpx
import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from sqlalchemy import text

from aidison.api.app import create_app
from aidison.domain.models import (
    Candidate,
    ConversationActionProposal,
    ConversationActionProposalKind,
)
from aidison.infrastructure.database import (
    DatabaseSettings,
    create_engine,
    create_session_factory,
)
from aidison.infrastructure.store import PostgresDomainStore
from tests.integration.support import (
    approve_requirements_and_apply_initial_modules,
    module_discovery_model_factory,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def key_prefix() -> str:
    return str(uuid4())


def _reply_model_factory() -> BaseChatModel:
    model = MagicMock(spec=BaseChatModel)
    bound = MagicMock()
    bound.ainvoke = AsyncMock(return_value=AIMessage(content='{"reply":"Understood."}'))
    model.bind.return_value = bound
    return model


def _client(factory: object) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(
            app=create_app(
                factory,
                conversation_model_factory=_reply_model_factory,
                module_discovery_model_factory=module_discovery_model_factory,
            )
        ),
        base_url="http://test",
    )


def _headers(key_prefix: str, suffix: str, **extra: str) -> dict[str, str]:
    return {"Idempotency-Key": f"{key_prefix}:{suffix}", **extra}


async def _create_approved_project(client: httpx.AsyncClient, key_prefix: str) -> tuple[str, str]:
    created = await client.post(
        "/api/projects",
        json={"name": "Conversation governance", "goal": "Build a controlled project"},
        headers=_headers(key_prefix, "project"),
    )
    assert created.status_code == 201, created.text
    project_id = created.json()["id"]
    modules = await approve_requirements_and_apply_initial_modules(
        client,
        project_id=project_id,
        goal="Build a controlled project",
        idempotency_key_prefix=f"{key_prefix}:requirements",
    )
    module_id = str(modules[0]["id"])
    return project_id, module_id


async def _assistant_turn_id(
    client: httpx.AsyncClient, key_prefix: str, project_id: str, suffix: str
) -> UUID:
    message = await client.post(
        f"/api/projects/{project_id}/conversation/messages",
        json={"content": "Please prepare a governed change proposal."},
        headers=_headers(key_prefix, f"message:{suffix}"),
    )
    assert message.status_code == 200, message.text
    return UUID(message.json()["assistant_turn"]["id"])


@pytest.mark.asyncio
async def test_conversation_rewrite_requirements_accept_creates_unapplied_proposal(
    key_prefix: str,
) -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")
    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await session.execute(text("TRUNCATE TABLE projects CASCADE"))
            await session.commit()

        async with _client(factory) as client:
            project_id, _module_id = await _create_approved_project(client, key_prefix)
            turn_id = await _assistant_turn_id(client, key_prefix, project_id, "rewrite")
            proposal = ConversationActionProposal(
                turn_id=turn_id,
                kind=ConversationActionProposalKind.REWRITE_REQUIREMENTS,
                summary="Add a safety constraint",
                proposed_payload={
                    "goal": "Build a controlled project with explicit safety limits",
                    "hard_constraints": ["Must be safe"],
                    "modules": [
                        {
                            "key": "core",
                            "name": "Core",
                            "responsibility": "Core logic",
                        }
                    ],
                },
            )
            async with factory() as session:
                await PostgresDomainStore(session).add_conversation_action_proposal(proposal)
                await session.commit()

            accepted = await client.post(
                f"/api/projects/{project_id}/conversation/proposals/{proposal.id}/accept",
                headers={**_headers(key_prefix, "accept:rewrite"), "If-Match": '"3"'},
            )
            assert accepted.status_code == 200, accepted.text
            result_ref = accepted.json()["result_ref"]
            assert result_ref.startswith("requirements_change:")

            project = await client.get(f"/api/projects/{project_id}")
            assert project.status_code == 200, project.text
            assert project.json()["revision"] == 3
            assert project.json()["goal"] == "Build a controlled project"

            async with factory() as session:
                store = PostgresDomainStore(session)
                change = await store.get_requirements_change_proposal(
                    UUID(result_ref.removeprefix("requirements_change:"))
                )
                assert change is not None and change.status.value == "proposed"
                assert change.target_goal.endswith("explicit safety limits")
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_conversation_change_selection_creates_adjustment_and_impact_preview(
    key_prefix: str,
) -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")
    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await session.execute(text("TRUNCATE TABLE projects CASCADE"))
            await session.commit()

        async with _client(factory) as client:
            project_id, module_id = await _create_approved_project(client, key_prefix)
            candidate = Candidate(
                project_id=UUID(project_id),
                module_id=UUID(module_id),
                name="Safer core option",
                description="Candidate selected through a governed conversation proposal",
            )
            async with factory() as session:
                await PostgresDomainStore(session).add_candidates((candidate,))
                await session.commit()

            turn_id = await _assistant_turn_id(client, key_prefix, project_id, "selection")
            proposal = ConversationActionProposal(
                turn_id=turn_id,
                kind=ConversationActionProposalKind.CHANGE_SELECTION,
                summary="Use the safer core option",
                proposed_payload={
                    "module_id": module_id,
                    "candidate_id": str(candidate.id),
                },
            )
            async with factory() as session:
                await PostgresDomainStore(session).add_conversation_action_proposal(proposal)
                await session.commit()

            accepted = await client.post(
                f"/api/projects/{project_id}/conversation/proposals/{proposal.id}/accept",
                headers={**_headers(key_prefix, "accept:selection"), "If-Match": '"3"'},
            )
            assert accepted.status_code == 200, accepted.text
            assert ":preview:" in accepted.json()["result_ref"]

            workspace = await client.get(f"/api/projects/{project_id}/workspace")
            assert workspace.status_code == 200, workspace.text
            assert workspace.json()["attention"]["state"] == "ready_to_review"
            assert any(
                action["kind"] == "review_change_impact"
                for action in workspace.json()["next_actions"]
            )
            project = await client.get(f"/api/projects/{project_id}")
            assert project.status_code == 200, project.text
            assert project.json()["revision"] == 4
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_conversation_change_selection_cannot_bypass_active_selection_lock(
    key_prefix: str,
) -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")
    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await session.execute(text("TRUNCATE TABLE projects CASCADE"))
            await session.commit()

        async with _client(factory) as client:
            project_id, module_id = await _create_approved_project(client, key_prefix)
            locked_candidate = Candidate(
                project_id=UUID(project_id),
                module_id=UUID(module_id),
                name="Locked core option",
                description="Must remain selected until explicitly unlocked",
            )
            alternate_candidate = Candidate(
                project_id=UUID(project_id),
                module_id=UUID(module_id),
                name="Alternate core option",
                description="Conversation must not select this while lock is active",
            )
            async with factory() as session:
                await PostgresDomainStore(session).add_candidates(
                    (locked_candidate, alternate_candidate)
                )
                await session.commit()

            lock = await client.post(
                f"/api/projects/{project_id}/modules/{module_id}/selection-locks",
                json={
                    "candidate_id": str(locked_candidate.id),
                    "reason": "Keep the locked core option",
                },
                headers={**_headers(key_prefix, "lock"), "If-Match": '"3"'},
            )
            assert lock.status_code == 201, lock.text

            turn_id = await _assistant_turn_id(client, key_prefix, project_id, "locked")
            proposal = ConversationActionProposal(
                turn_id=turn_id,
                kind=ConversationActionProposalKind.CHANGE_SELECTION,
                summary="Try the alternate core option",
                proposed_payload={
                    "module_id": module_id,
                    "candidate_id": str(alternate_candidate.id),
                },
            )
            async with factory() as session:
                await PostgresDomainStore(session).add_conversation_action_proposal(proposal)
                await session.commit()

            rejected = await client.post(
                f"/api/projects/{project_id}/conversation/proposals/{proposal.id}/accept",
                headers={**_headers(key_prefix, "accept:locked"), "If-Match": '"4"'},
            )
            assert rejected.status_code == 409, rejected.text

            async with factory() as session:
                store = PostgresDomainStore(session)
                pending = await store.get_conversation_action_proposal(proposal.id)
                assert pending is not None and pending.status.value == "proposed"
                assert pending.resolution_turn_id is None
                assert await store.list_user_adjustments(UUID(project_id)) == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_conversation_action_proposal_reject_persists_without_command(
    key_prefix: str,
) -> None:
    """Rejecting a spend-budget proposal runs no command and bumps nothing."""
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")
    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await session.execute(text("TRUNCATE TABLE projects CASCADE"))
            await session.commit()

        async with _client(factory) as client:
            project_id, _module_id = await _create_approved_project(client, key_prefix)
            turn_id = await _assistant_turn_id(client, key_prefix, project_id, "reject-budget")
            proposal = ConversationActionProposal(
                turn_id=turn_id,
                kind=ConversationActionProposalKind.CHANGE_SPEND_BUDGET,
                summary="Raise the spend cap",
                proposed_payload={"amount": "800.00", "currency": "CNY", "summary": "Raise cap"},
            )
            async with factory() as session:
                await PostgresDomainStore(session).add_conversation_action_proposal(proposal)
                await session.commit()

            rejected = await client.post(
                f"/api/projects/{project_id}/conversation/proposals/{proposal.id}/reject",
                headers={**_headers(key_prefix, "reject:budget"), "If-Match": '"3"'},
            )
            assert rejected.status_code == 200, rejected.text
            assert rejected.json()["result_ref"] == f"rejected:{proposal.id}"
            assert rejected.json()["proposal"]["status"] == "rejected"

            async with factory() as session:
                store = PostgresDomainStore(session)
                stored = await store.get_conversation_action_proposal(proposal.id)
                assert stored is not None
                assert stored.status.value == "rejected"
                assert stored.resolution_turn_id is not None
                assert stored.resolved_at is not None
                # No command ran: no budget proposal and no AgentRun were created.
                assert await store.list_spend_budget_proposals(UUID(project_id)) == []
                assert (
                    await session.scalar(
                        text("SELECT count(*) FROM agent_runs WHERE project_id = :id"),
                        {"id": project_id},
                    )
                    == 0
                )

            project = await client.get(f"/api/projects/{project_id}")
            assert project.status_code == 200, project.text
            assert project.json()["revision"] == 3
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_conversation_action_proposal_reject_replays_idempotently(
    key_prefix: str,
) -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")
    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await session.execute(text("TRUNCATE TABLE projects CASCADE"))
            await session.commit()

        async with _client(factory) as client:
            project_id, _module_id = await _create_approved_project(client, key_prefix)
            turn_id = await _assistant_turn_id(client, key_prefix, project_id, "reject-replay")
            proposal = ConversationActionProposal(
                turn_id=turn_id,
                kind=ConversationActionProposalKind.START_RESEARCH,
                summary="Research controller options",
                proposed_payload={"objective": "Compare controllers"},
            )
            async with factory() as session:
                await PostgresDomainStore(session).add_conversation_action_proposal(proposal)
                await session.commit()

            headers = {**_headers(key_prefix, "reject:replay"), "If-Match": '"3"'}
            first = await client.post(
                f"/api/projects/{project_id}/conversation/proposals/{proposal.id}/reject",
                headers=headers,
            )
            assert first.status_code == 200, first.text
            replay = await client.post(
                f"/api/projects/{project_id}/conversation/proposals/{proposal.id}/reject",
                headers=headers,
            )
            assert replay.status_code == 200, replay.text
            assert replay.json()["result_ref"] == first.json()["result_ref"]
            assert replay.json()["proposal"]["status"] == "rejected"

            # No duplicate resolution turn and no job were created.
            async with factory() as session:
                store = PostgresDomainStore(session)
                stored = await store.get_conversation_action_proposal(proposal.id)
                assert stored is not None and stored.resolution_turn_id is not None
                proposal_turn = await store.get_conversation_turn(proposal.turn_id)
                assert proposal_turn is not None
                system_turns = [
                    t
                    for t in await store.list_conversation_turns(proposal_turn.session_id)
                    if t.model_profile == "system"
                ]
                assert len(system_turns) == 1
                assert (
                    await session.scalar(
                        text(
                            "SELECT count(*) FROM execution_plan_proposals WHERE project_id = :id"
                        ),
                        {"id": project_id},
                    )
                    == 0
                )
                assert (
                    await session.scalar(
                        text("SELECT count(*) FROM agent_runs WHERE project_id = :id"),
                        {"id": project_id},
                    )
                    == 0
                )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_conversation_action_proposal_reject_different_key_on_settled_conflicts(
    key_prefix: str,
) -> None:
    """A second reject with a different Idempotency-Key conflicts (409)."""
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")
    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await session.execute(text("TRUNCATE TABLE projects CASCADE"))
            await session.commit()

        async with _client(factory) as client:
            project_id, _module_id = await _create_approved_project(client, key_prefix)
            turn_id = await _assistant_turn_id(client, key_prefix, project_id, "reject-diff-key")
            proposal = ConversationActionProposal(
                turn_id=turn_id,
                kind=ConversationActionProposalKind.START_RESEARCH,
                summary="Research controller options",
                proposed_payload={"objective": "Compare controllers"},
            )
            async with factory() as session:
                await PostgresDomainStore(session).add_conversation_action_proposal(proposal)
                await session.commit()

            first = await client.post(
                f"/api/projects/{project_id}/conversation/proposals/{proposal.id}/reject",
                headers={**_headers(key_prefix, "reject:key-a"), "If-Match": '"3"'},
            )
            assert first.status_code == 200, first.text

            second = await client.post(
                f"/api/projects/{project_id}/conversation/proposals/{proposal.id}/reject",
                headers={**_headers(key_prefix, "reject:key-b"), "If-Match": '"3"'},
            )
            assert second.status_code == 409, second.text

            async with factory() as session:
                store = PostgresDomainStore(session)
                stored = await store.get_conversation_action_proposal(proposal.id)
                assert stored is not None and stored.status.value == "rejected"
                proposal_turn = await store.get_conversation_turn(proposal.turn_id)
                assert proposal_turn is not None
                system_turns = [
                    t
                    for t in await store.list_conversation_turns(proposal_turn.session_id)
                    if t.model_profile == "system"
                ]
                assert len(system_turns) == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_conversation_action_proposal_reject_guards_stale_revision(
    key_prefix: str,
) -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")
    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await session.execute(text("TRUNCATE TABLE projects CASCADE"))
            await session.commit()

        async with _client(factory) as client:
            project_id, _module_id = await _create_approved_project(client, key_prefix)
            turn_id = await _assistant_turn_id(client, key_prefix, project_id, "reject-stale")
            proposal = ConversationActionProposal(
                turn_id=turn_id,
                kind=ConversationActionProposalKind.START_RESEARCH,
                summary="Research controller options",
                proposed_payload={"objective": "Compare controllers"},
            )
            async with factory() as session:
                await PostgresDomainStore(session).add_conversation_action_proposal(proposal)
                await session.commit()

            rejected = await client.post(
                f"/api/projects/{project_id}/conversation/proposals/{proposal.id}/reject",
                headers={**_headers(key_prefix, "reject:stale"), "If-Match": '"999"'},
            )
            assert rejected.status_code == 412, rejected.text

            async with factory() as session:
                store = PostgresDomainStore(session)
                pending = await store.get_conversation_action_proposal(proposal.id)
                assert pending is not None and pending.status.value == "proposed"
                assert pending.resolution_turn_id is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_conversation_action_proposal_reject_is_project_scoped(
    key_prefix: str,
) -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")
    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await session.execute(text("TRUNCATE TABLE projects CASCADE"))
            await session.commit()

        async with _client(factory) as client:
            project_id_a, _module_id = await _create_approved_project(client, key_prefix)
            created = await client.post(
                "/api/projects",
                json={"name": "Reject target", "goal": "Stay isolated"},
                headers=_headers(key_prefix, "reject-scope:project-b"),
            )
            assert created.status_code == 201, created.text
            project_id_b = created.json()["id"]

            turn_id = await _assistant_turn_id(client, key_prefix, project_id_b, "reject-scope")
            proposal = ConversationActionProposal(
                turn_id=turn_id,
                kind=ConversationActionProposalKind.START_RESEARCH,
                summary="Research controller options",
                proposed_payload={"objective": "Compare controllers"},
            )
            async with factory() as session:
                await PostgresDomainStore(session).add_conversation_action_proposal(proposal)
                await session.commit()

            rejected = await client.post(
                f"/api/projects/{project_id_a}/conversation/proposals/{proposal.id}/reject",
                headers={**_headers(key_prefix, "reject:cross"), "If-Match": '"3"'},
            )
            assert rejected.status_code == 409, rejected.text

            async with factory() as session:
                store = PostgresDomainStore(session)
                pending = await store.get_conversation_action_proposal(proposal.id)
                assert pending is not None and pending.status.value == "proposed"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_conversation_action_proposal_reject_after_accept_conflicts(
    key_prefix: str,
) -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")
    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await session.execute(text("TRUNCATE TABLE projects CASCADE"))
            await session.commit()

        async with _client(factory) as client:
            project_id, _module_id = await _create_approved_project(client, key_prefix)
            turn_id = await _assistant_turn_id(
                client, key_prefix, project_id, "reject-after-accept"
            )
            proposal = ConversationActionProposal(
                turn_id=turn_id,
                kind=ConversationActionProposalKind.REWRITE_REQUIREMENTS,
                summary="Add a safety constraint",
                proposed_payload={
                    "goal": "Build a controlled project with explicit safety limits",
                    "hard_constraints": ["Must be safe"],
                    "modules": [
                        {
                            "key": "core",
                            "name": "Core",
                            "responsibility": "Core logic",
                        }
                    ],
                },
            )
            async with factory() as session:
                await PostgresDomainStore(session).add_conversation_action_proposal(proposal)
                await session.commit()

            accepted = await client.post(
                f"/api/projects/{project_id}/conversation/proposals/{proposal.id}/accept",
                headers={**_headers(key_prefix, "accept:then-reject"), "If-Match": '"3"'},
            )
            assert accepted.status_code == 200, accepted.text

            rejected = await client.post(
                f"/api/projects/{project_id}/conversation/proposals/{proposal.id}/reject",
                headers={**_headers(key_prefix, "reject:accepted"), "If-Match": '"3"'},
            )
            assert rejected.status_code == 409, rejected.text

            async with factory() as session:
                store = PostgresDomainStore(session)
                stored = await store.get_conversation_action_proposal(proposal.id)
                assert stored is not None and stored.status.value == "accepted"
    finally:
        await engine.dispose()

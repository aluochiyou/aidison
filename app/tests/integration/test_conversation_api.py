"""Focused integration tests for project-scoped conversation API.

Uses fake model (no network) + test database + httpx ASGI transport.
"""

from __future__ import annotations

import asyncio
import json
import os
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import httpx
import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from sqlalchemy import text

from aidison.api.app import create_app
from aidison.infrastructure.database import (
    DatabaseSettings,
    create_engine,
    create_session_factory,
)
from tests.integration.support import (
    approve_requirements_and_apply_initial_modules,
    module_discovery_model_factory,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def key_prefix():
    return str(uuid4())


def _success_model_factory() -> BaseChatModel:
    model = MagicMock(spec=BaseChatModel)
    bound = MagicMock()
    bound.ainvoke = AsyncMock(return_value=AIMessage(content='{"reply":"I can help with that."}'))
    model.bind.return_value = bound
    return model


def _failing_model_factory() -> BaseChatModel:
    model = MagicMock(spec=BaseChatModel)
    bound = MagicMock()
    bound.ainvoke = AsyncMock(side_effect=RuntimeError("test model unavailable"))
    model.bind.return_value = bound
    return model


def _interleaved_reply_model_factory() -> BaseChatModel:
    """Return responses out of order so reply causality cannot rely on sequence."""

    model = MagicMock(spec=BaseChatModel)
    bound = MagicMock()

    async def reply(messages: list[dict[str, str]]) -> AIMessage:
        prompt = messages[-1]["content"]
        if prompt.endswith("User says: alpha"):
            await asyncio.sleep(0.05)
            return AIMessage(content='{"reply":"reply to alpha"}')
        if prompt.endswith("User says: beta"):
            return AIMessage(content='{"reply":"reply to beta"}')
        await asyncio.sleep(0.05)
        return AIMessage(content='{"reply":"reply to duplicate"}')

    bound.ainvoke = AsyncMock(side_effect=reply)
    model.bind.return_value = bound
    return model


def _proposal_model_factory() -> BaseChatModel:
    model = MagicMock(spec=BaseChatModel)
    bound = MagicMock()
    bound.ainvoke = AsyncMock(
        return_value=AIMessage(
            content=json.dumps(
                {
                    "reply": "I need one decision before starting.",
                    "clarifications": [
                        {"question": "What is the maximum budget?", "kind": "budget"}
                    ],
                    "action_proposals": [
                        {
                            "kind": "start_research",
                            "summary": "Research suitable power systems",
                            "proposed_payload": {},
                        }
                    ],
                }
            )
        )
    )
    model.bind.return_value = bound
    return model


def _strategy_model_factory() -> BaseChatModel:
    model = MagicMock(spec=BaseChatModel)
    bound = MagicMock()

    async def reply(messages: list[dict[str, str]]) -> AIMessage:
        context = json.loads(messages[-1]["content"])
        scope = context["allowed_scope"]
        tasks = [
            {
                "task_key": f"{item['key']}.research",
                "title": f"Research {item['name']}",
                "objective": f"Find source-backed constraints for {item['name']}.",
                "module_ids": [item["id"]],
                "depends_on_task_keys": [] if index == 0 else [f"{scope[0]['key']}.research"],
                "priority": "must",
                "expected_outputs": ["evidence", "candidate"],
                "stop_conditions": ["Record an evidence-backed option or an explicit gap."],
            }
            for index, item in enumerate(scope)
        ]
        return AIMessage(
            content=json.dumps(
                {
                    "schema_version": "research-strategy-v1",
                    "summary": "Fixture strategy is bounded by the requested modules.",
                    "scope_module_ids": [item["id"] for item in scope],
                    "tasks": tasks,
                    "source_strategy": "primary",
                }
            )
        )

    bound.ainvoke = AsyncMock(side_effect=reply)
    model.bind.return_value = bound
    return model


def _client(engine, factory, model_factory=_success_model_factory) -> httpx.AsyncClient:
    api = create_app(
        factory,
        conversation_model_factory=model_factory,
        module_discovery_model_factory=module_discovery_model_factory,
        research_strategy_model_factory=_strategy_model_factory,
    )
    transport = httpx.ASGITransport(app=api)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


def _headers(key_prefix: str, suffix: str, **extra) -> dict[str, str]:
    return {
        "Idempotency-Key": f"{key_prefix}:{suffix}",
        **extra,
    }


# ── Helpers ──────────────────────────────────────────────────────────────────


async def _create_project(client: httpx.AsyncClient, key_prefix: str) -> str:
    r = await client.post(
        "/api/projects",
        json={"name": "Conversation Test", "goal": "Verify project-scoped chat"},
        headers=_headers(key_prefix, "project"),
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _approve_requirements(
    client: httpx.AsyncClient, key_prefix: str, project_id: str
) -> None:
    await approve_requirements_and_apply_initial_modules(
        client,
        project_id=project_id,
        goal="Verify project-scoped chat",
        idempotency_key_prefix=f"{key_prefix}:reqs",
    )


# ── Tests ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_message_persists_and_does_not_bump_revision(key_prefix: str) -> None:
    """User messages are persisted; Project.revision stays unchanged."""
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")

    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await session.execute(text("TRUNCATE TABLE projects CASCADE"))
            await session.commit()

        async with _client(engine, factory) as client:
            project_id = await _create_project(client, key_prefix)
            await _approve_requirements(client, key_prefix, project_id)

            # Get current revision
            r = await client.get(f"/api/projects/{project_id}")
            rev_before = r.json()["revision"]

            # Post a message (without If-Match — message does not need it)
            r = await client.post(
                f"/api/projects/{project_id}/conversation/messages",
                json={"content": "Hello, what should I use for power?"},
                headers=_headers(key_prefix, "msg1"),
            )
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["user_turn"]["content"] == "Hello, what should I use for power?"
            assert body["user_turn"]["role"] == "user"
            # The test model is deterministic and performs no network request.
            assert body["assistant_turn"] is not None
            assert body["assistant_turn"]["role"] == "assistant"

            # Project.revision must NOT change
            r = await client.get(f"/api/projects/{project_id}")
            assert r.json()["revision"] == rev_before

            events = await client.get(f"/api/projects/{project_id}/events")
            assert events.status_code == 200, events.text
            event_types = {event["type"] for event in events.json()}
            assert "conversation.user_message_recorded" in event_types
            assert "conversation.assistant_reply_ready" in event_types

            snapshot = await client.get(f"/api/projects/{project_id}/snapshot")
            assert snapshot.status_code == 200, snapshot.text
            conversation = snapshot.json()["conversation"]
            assert conversation["active_session"]["id"] == body["session_id"]
            assert [turn["id"] for turn in conversation["recent_turns"]] == [
                body["user_turn"]["id"],
                body["assistant_turn"]["id"],
            ]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_idempotent_replay_returns_same_turns(key_prefix: str) -> None:
    """Same Idempotency-Key returns the same user_turn + assistant_turn."""
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")

    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await session.execute(text("TRUNCATE TABLE projects CASCADE"))
            await session.commit()

        async with _client(engine, factory) as client:
            project_id = await _create_project(client, key_prefix)
            await _approve_requirements(client, key_prefix, project_id)

            r1 = await client.post(
                f"/api/projects/{project_id}/conversation/messages",
                json={"content": "Unique message"},
                headers=_headers(key_prefix, "replay-test"),
            )
            assert r1.status_code == 200
            turn1_id = r1.json()["user_turn"]["id"]

            # Replay same key
            r2 = await client.post(
                f"/api/projects/{project_id}/conversation/messages",
                json={"content": "Unique message"},
                headers=_headers(key_prefix, "replay-test"),
            )
            assert r2.status_code == 200
            assert r2.json()["user_turn"]["id"] == turn1_id
            assert r2.json()["user_turn"]["content"] == "Unique message"
            # Assistant turn must also be present (fallback or real)
            assert r2.json()["assistant_turn"] is not None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_same_key_replays_one_causal_reply(key_prefix: str) -> None:
    """Two in-flight retries cannot create two user turns or mispair a reply."""
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")

    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await session.execute(text("TRUNCATE TABLE projects CASCADE"))
            await session.commit()
        model = _interleaved_reply_model_factory()
        async with _client(engine, factory, lambda: model) as client:
            project_id = await _create_project(client, key_prefix)
            first, second = await asyncio.gather(
                client.post(
                    f"/api/projects/{project_id}/conversation/messages",
                    json={"content": "duplicate"},
                    headers=_headers(key_prefix, "same-key"),
                ),
                client.post(
                    f"/api/projects/{project_id}/conversation/messages",
                    json={"content": "duplicate"},
                    headers=_headers(key_prefix, "same-key"),
                ),
            )

        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text
        assert first.json()["user_turn"]["id"] == second.json()["user_turn"]["id"]
        assert first.json()["assistant_turn"]["id"] == second.json()["assistant_turn"]["id"]
        assert (
            first.json()["assistant_turn"]["in_reply_to_turn_id"]
            == first.json()["user_turn"]["id"]
        )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_interleaved_messages_keep_their_own_causal_replies(key_prefix: str) -> None:
    """A later completion may receive a later sequence, never another user's reply."""
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")

    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await session.execute(text("TRUNCATE TABLE projects CASCADE"))
            await session.commit()
        model = _interleaved_reply_model_factory()
        async with _client(engine, factory, lambda: model) as client:
            project_id = await _create_project(client, key_prefix)
            warmup = await client.post(
                f"/api/projects/{project_id}/conversation/messages",
                json={"content": "warmup"},
                headers=_headers(key_prefix, "warmup"),
            )
            assert warmup.status_code == 200, warmup.text
            session_id = warmup.json()["session_id"]
            alpha, beta = await asyncio.gather(
                client.post(
                    f"/api/projects/{project_id}/conversation/messages",
                    json={"content": "alpha", "session_id": session_id},
                    headers=_headers(key_prefix, "alpha"),
                ),
                client.post(
                    f"/api/projects/{project_id}/conversation/messages",
                    json={"content": "beta", "session_id": session_id},
                    headers=_headers(key_prefix, "beta"),
                ),
            )

        assert alpha.status_code == 200, alpha.text
        assert beta.status_code == 200, beta.text
        assert alpha.json()["assistant_turn"]["content"] == "reply to alpha"
        assert beta.json()["assistant_turn"]["content"] == "reply to beta"
        assert (
            alpha.json()["assistant_turn"]["in_reply_to_turn_id"]
            == alpha.json()["user_turn"]["id"]
        )
        assert (
            beta.json()["assistant_turn"]["in_reply_to_turn_id"]
            == beta.json()["user_turn"]["id"]
        )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_cross_project_cannot_read_turns(key_prefix: str) -> None:
    """A project cannot access conversation turns from another project."""
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")

    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await session.execute(text("TRUNCATE TABLE projects CASCADE"))
            await session.commit()

        async with _client(engine, factory) as client:
            p1 = await _create_project(client, key_prefix + "a")
            p2 = await _create_project(client, key_prefix + "b")

            # Post to project 1
            r = await client.post(
                f"/api/projects/{p1}/conversation/messages",
                json={"content": "p1 message"},
                headers=_headers(key_prefix, "cross-msg"),
            )
            assert r.status_code == 200

            # Try to read turns of project 1's session with project 2's session ID
            sessions = await client.get(f"/api/projects/{p1}/conversation/sessions")
            sid = sessions.json()["sessions"][0]["id"]

            r = await client.get(f"/api/projects/{p2}/conversation/sessions/{sid}/turns")
            assert r.status_code == 404, r.text
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_snapshot_projects_open_conversation_inputs(key_prefix: str) -> None:
    """The canonical snapshot exposes pending questions/proposals for the active session."""
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")

    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await session.execute(text("TRUNCATE TABLE projects CASCADE"))
            await session.commit()

        async with _client(engine, factory, _proposal_model_factory) as client:
            project_id = await _create_project(client, key_prefix)
            await _approve_requirements(client, key_prefix, project_id)
            response = await client.post(
                f"/api/projects/{project_id}/conversation/messages",
                json={"content": "Please research a power system."},
                headers=_headers(key_prefix, "proposal-message"),
            )
            assert response.status_code == 200, response.text

            snapshot = await client.get(f"/api/projects/{project_id}/snapshot")
            assert snapshot.status_code == 200, snapshot.text
            conversation = snapshot.json()["conversation"]
            assert len(conversation["open_clarifications"]) == 1
            assert len(conversation["open_action_proposals"]) == 1
            assert conversation["open_action_proposals"][0]["kind"] == "start_research"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_cross_project_cannot_close_session(key_prefix: str) -> None:
    """The project path is an authorization boundary for session close too."""
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")

    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await session.execute(text("TRUNCATE TABLE projects CASCADE"))
            await session.commit()

        async with _client(engine, factory) as client:
            p1 = await _create_project(client, key_prefix + "a")
            p2 = await _create_project(client, key_prefix + "b")
            response = await client.post(
                f"/api/projects/{p1}/conversation/messages",
                json={"content": "Create my private session."},
                headers=_headers(key_prefix, "close-cross-message"),
            )
            assert response.status_code == 200, response.text
            session_id = response.json()["session_id"]

            close = await client.post(
                f"/api/projects/{p2}/conversation/sessions/{session_id}/close",
                headers=_headers(key_prefix, "close-cross-session"),
            )
            assert close.status_code == 404, close.text

            sessions = await client.get(f"/api/projects/{p1}/conversation/sessions")
            assert sessions.status_code == 200, sessions.text
            assert sessions.json()["sessions"][0]["status"] == "active"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_post_message_rejects_foreign_or_closed_session_id(key_prefix: str) -> None:
    """An explicit session_id cannot cross a project boundary or revive a closed chat."""
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")

    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await session.execute(text("TRUNCATE TABLE projects CASCADE"))
            await session.commit()

        async with _client(engine, factory) as client:
            p1 = await _create_project(client, key_prefix + "a")
            p2 = await _create_project(client, key_prefix + "b")
            created = await client.post(
                f"/api/projects/{p1}/conversation/messages",
                json={"content": "Keep this session private."},
                headers=_headers(key_prefix, "session-owner-message"),
            )
            assert created.status_code == 200, created.text
            session_id = created.json()["session_id"]

            foreign = await client.post(
                f"/api/projects/{p2}/conversation/messages",
                json={"content": "Try another project's session.", "session_id": session_id},
                headers=_headers(key_prefix, "foreign-session-message"),
            )
            assert foreign.status_code == 404, foreign.text

            closed = await client.post(
                f"/api/projects/{p1}/conversation/sessions/{session_id}/close",
                headers=_headers(key_prefix, "close-owned-session"),
            )
            assert closed.status_code == 200, closed.text

            revive = await client.post(
                f"/api/projects/{p1}/conversation/messages",
                json={"content": "Try to revive the closed session.", "session_id": session_id},
                headers=_headers(key_prefix, "closed-session-message"),
            )
            assert revive.status_code == 409, revive.text
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_post_message_to_missing_project_is_not_found(key_prefix: str) -> None:
    """Conversation setup must not surface a foreign-key error as a server error."""
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")

    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    try:
        async with _client(engine, factory) as client:
            response = await client.post(
                f"/api/projects/{uuid4()}/conversation/messages",
                json={"content": "There is no project here."},
                headers=_headers(key_prefix, "missing-project-message"),
            )
            assert response.status_code == 404, response.text
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_stale_if_match_returns_412_not_409_on_accept(
    key_prefix: str,
) -> None:
    """Stale If-Match on accept returns HTTP 412, not 409.

    Only true state conflicts (already-resolved proposal, SelectionLock
    block) should remain 409.  This test proves the stale-revision path
    is distinguished and that the correct revision path still succeeds
    afterwards — using real PostgreSQL assertions, not just status codes.
    """
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")

    from uuid import UUID as _UUID

    from aidison.domain.models import (
        ConversationActionProposal,
        ConversationActionProposalKind,
    )
    from aidison.infrastructure.store import PostgresDomainStore

    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await session.execute(text("TRUNCATE TABLE projects CASCADE"))
            await session.commit()

        async with _client(engine, factory) as client:
            project_id = await _create_project(client, key_prefix)
            await _approve_requirements(client, key_prefix, project_id)

            # Get a session + assistant turn so we can attach a proposal
            resp = await client.post(
                f"/api/projects/{project_id}/conversation/messages",
                json={"content": "Research controllers"},
                headers=_headers(key_prefix, "stale-msg"),
            )
            assert resp.status_code == 200, resp.text
            assistant = resp.json()["assistant_turn"]

            # Create a real proposal in the store
            async with factory() as session:
                store = PostgresDomainStore(session)
                proposal = ConversationActionProposal(
                    turn_id=_UUID(assistant["id"]),
                    kind=ConversationActionProposalKind.START_RESEARCH,
                    summary="Research flight controllers",
                    proposed_payload={"objective": "Compare controllers"},
                )
                await store.add_conversation_action_proposal(proposal)
                await session.commit()
                pid = str(proposal.id)

            # Get current revision
            r = await client.get(f"/api/projects/{project_id}")
            current_rev = r.json()["revision"]

            # 1. Stale If-Match → 412 (not 409)
            r = await client.post(
                f"/api/projects/{project_id}/conversation/proposals/{pid}/accept",
                headers={
                    **_headers(key_prefix, "accept-stale"),
                    "If-Match": f'"{current_rev + 99}"',
                },
            )
            assert r.status_code == 412, (
                f"expected 412 for stale If-Match, got {r.status_code}: {r.text}"
            )

            # 2. Proposal must still be PROPOSED (not accepted)
            async with factory() as session:
                store = PostgresDomainStore(session)
                p = await store.get_conversation_action_proposal(_UUID(pid))
                assert p is not None
                assert p.status.value == "proposed", (
                    f"proposal should stay proposed after stale accept, got {p.status}"
                )

            # 3. Correct If-Match → 200
            r = await client.post(
                f"/api/projects/{project_id}/conversation/proposals/{pid}/accept",
                headers={
                    **_headers(key_prefix, "accept-ok"),
                    "If-Match": f'"{current_rev}"',
                },
            )
            assert r.status_code == 200, (
                f"expected 200 for correct If-Match, got {r.status_code}: {r.text}"
            )
            assert r.json()["result_ref"].startswith("execution_plan:")

            # 4. Proposal must now be ACCEPTED in PostgreSQL
            async with factory() as session:
                store = PostgresDomainStore(session)
                p = await store.get_conversation_action_proposal(_UUID(pid))
                assert p is not None
                assert p.status.value == "accepted", (
                    f"proposal should be accepted after correct If-Match, got {p.status}"
                )
                assert p.resolution_turn_id is not None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_clarification_resolve_persists_actual_response(key_prefix: str) -> None:
    """Clarification resolve stores the user's actual response text."""
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")

    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await session.execute(text("TRUNCATE TABLE projects CASCADE"))
            await session.commit()

        async with _client(engine, factory) as client:
            project_id = await _create_project(client, key_prefix)
            await _approve_requirements(client, key_prefix, project_id)

            # Post a message to create a session + turn (clarification requires a turn)
            r = await client.post(
                f"/api/projects/{project_id}/conversation/messages",
                json={"content": "What is the best approach?"},
                headers=_headers(key_prefix, "clarify-msg"),
            )
            assert r.status_code == 200
            sessions = await client.get(f"/api/projects/{project_id}/conversation/sessions")
            sid = sessions.json()["sessions"][0]["id"]

            # Get turns to find an assistant turn ID we can attach a clarification to
            turns_r = await client.get(
                f"/api/projects/{project_id}/conversation/sessions/{sid}/turns"
            )
            assert turns_r.status_code == 200
            turns = turns_r.json()["turns"]

            # Find the assistant turn
            assistant_turn = next((t for t in turns if t["role"] == "assistant"), None)
            # If the fallback ran, we still have an assistant turn — use it
            assert assistant_turn is not None, "should have an assistant turn"

            # Manually add a clarification via store (for test purposes)
            # In production, clarifications are created by the model's response.
            # For this test we verify the resolve endpoint which is the user-facing side.
            from uuid import UUID as _UUID

            from aidison.domain.models import (
                ConversationClarification,
                ConversationClarificationKind,
            )
            from aidison.infrastructure.store import PostgresDomainStore

            async with factory() as session:
                store = PostgresDomainStore(session)
                c = ConversationClarification(
                    turn_id=_UUID(assistant_turn["id"]),
                    question="What battery chemistry?",
                    kind=ConversationClarificationKind.SELECTION,
                )
                await store.add_conversation_clarification(c)
                await session.commit()
                clar_id = str(c.id)

            # Resolve the clarification with a real response
            r = await client.post(
                f"/api/projects/{project_id}/conversation/clarifications/{clar_id}/resolve",
                json={"response": "LiPo 4S 1500mAh"},
                headers=_headers(key_prefix, "resolve-clar"),
            )
            assert r.status_code == 200, r.text
            assert r.json()["status"] == "resolved"

            # A network retry must replay the already recorded resolution,
            # rather than append a second user turn or report a false 500.
            replay = await client.post(
                f"/api/projects/{project_id}/conversation/clarifications/{clar_id}/resolve",
                json={"response": "LiPo 4S 1500mAh"},
                headers=_headers(key_prefix, "resolve-clar"),
            )
            assert replay.status_code == 200, replay.text
            assert replay.json() == r.json()

            # Verify the response turn was created with the actual text
            recent_turns = await client.get(
                f"/api/projects/{project_id}/conversation/sessions/{sid}/turns"
            )
            turns_data = recent_turns.json()["turns"]
            response_turns = [
                t for t in turns_data if t["idempotency_key"] == f"{key_prefix}:resolve-clar"
            ]
            assert len(response_turns) == 1
            assert response_turns[0]["content"] == "LiPo 4S 1500mAh"

            events = await client.get(f"/api/projects/{project_id}/events")
            assert events.status_code == 200, events.text
            assert "conversation.clarification_resolved" in {
                event["type"] for event in events.json()
            }
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_model_fallback_on_call_failure(key_prefix: str) -> None:
    """When the model call raises, a fallback assistant turn is persisted."""
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")

    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await session.execute(text("TRUNCATE TABLE projects CASCADE"))
            await session.commit()

        async with _client(engine, factory, _failing_model_factory) as client:
            project_id = await _create_project(client, key_prefix)
            await _approve_requirements(client, key_prefix, project_id)

            r = await client.post(
                f"/api/projects/{project_id}/conversation/messages",
                json={"content": "This will trigger a model call"},
                headers=_headers(key_prefix, "fallback-msg"),
            )
            assert r.status_code == 200
            assert r.json()["assistant_turn"] is not None
            assert r.json()["assistant_turn"]["role"] == "assistant"
            assert r.json()["assistant_turn"]["is_fallback"] is True
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_start_research_proposal_creates_approval_gated_execution_plan(
    key_prefix: str,
) -> None:
    """Conversation research intent proposes a plan but never starts runtime work."""
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")

    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await session.execute(text("TRUNCATE TABLE projects CASCADE"))
            await session.commit()

        async with _client(engine, factory) as client:
            project_id = await _create_project(client, key_prefix)
            await _approve_requirements(client, key_prefix, project_id)

            # Post message to get a session and assistant turn
            r = await client.post(
                f"/api/projects/{project_id}/conversation/messages",
                json={"content": "Start researching flight controllers"},
                headers=_headers(key_prefix, "research-msg"),
            )
            assert r.status_code == 200
            sessions = await client.get(f"/api/projects/{project_id}/conversation/sessions")
            sid = sessions.json()["sessions"][0]["id"]
            turns = await client.get(
                f"/api/projects/{project_id}/conversation/sessions/{sid}/turns"
            )
            assistant = next((t for t in turns.json()["turns"] if t["role"] == "assistant"), None)
            assert assistant is not None

            # Manually create a START_RESEARCH proposal (model would do this normally)
            from uuid import UUID as _UUID

            from aidison.domain.models import (
                ConversationActionProposal,
                ConversationActionProposalKind,
            )
            from aidison.infrastructure.store import PostgresDomainStore

            async with factory() as session:
                store = PostgresDomainStore(session)
                proposal = ConversationActionProposal(
                    turn_id=_UUID(assistant["id"]),
                    kind=ConversationActionProposalKind.START_RESEARCH,
                    summary="Let AI research flight controllers",
                    proposed_payload={},
                )
                await store.add_conversation_action_proposal(proposal)
                await session.commit()
                pid = str(proposal.id)

            # Get current revision for If-Match
            r = await client.get(f"/api/projects/{project_id}")
            current_rev = r.json()["revision"]

            # Accepting the conversation proposal creates a separate, still
            # unapproved ExecutionPlanProposal. It must not start an AgentRun.
            r = await client.post(
                f"/api/projects/{project_id}/conversation/proposals/{pid}/accept",
                headers={
                    **_headers(key_prefix, "accept-research"),
                    "If-Match": f'"{current_rev}"',
                },
            )
            assert r.status_code == 200, r.text
            result_ref = r.json()["result_ref"]
            assert result_ref.startswith("execution_plan:")

            # Verify the action proposal is accepted and its created execution
            # plan remains an explicit user approval boundary.
            async with factory() as session:
                store = PostgresDomainStore(session)
                p = await store.get_conversation_action_proposal(_UUID(pid))
                assert p is not None
                assert p.status.value == "accepted", f"expected accepted, got {p.status}"
                assert p.resolution_turn_id is not None
                resolution_turn_id = str(p.resolution_turn_id)
                plan = await store.get_execution_plan_proposal(
                    _UUID(result_ref.removeprefix("execution_plan:"))
                )
                assert plan is not None
                assert plan.status.value == "proposed"
                assert plan.research_strategy is not None
                assert plan.research_strategy.tasks
                assert plan.research_strategy.scope_module_ids
                assert plan.work_summary == (
                    plan.research_strategy.summary,
                    *plan.research_strategy.decision_notes,
                )
                assert "decompose active modules" not in " ".join(plan.work_summary)
                agent_run_count = await session.scalar(
                    text("SELECT count(*) FROM agent_runs WHERE project_id = :project_id"),
                    {"project_id": project_id},
                )
                assert agent_run_count == 0

            events = await client.get(f"/api/projects/{project_id}/events")
            assert events.status_code == 200, events.text
            event_types = {event["type"] for event in events.json()}
            assert "execution_plan.proposed" in event_types
            assert "conversation.action_proposal_accepted" in event_types

            # Fix 1: resolution turn must say research hasn't started, not "executed".
            turns_after = await client.get(
                f"/api/projects/{project_id}/conversation/sessions/{sid}/turns"
            )
            resolution_turns = [
                t for t in turns_after.json()["turns"] if t["id"] == resolution_turn_id
            ]
            assert len(resolution_turns) == 1
            content = resolution_turns[0]["content"]
            assert "not started" in content.lower() or "not yet" in content.lower(), (
                f"resolution must say research hasn't started, got: {content}"
            )
            assert "executed" not in content.lower(), (
                f"resolution must not claim executed, got: {content}"
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_start_research_rejects_extra_payload_fields_without_effects(
    key_prefix: str,
) -> None:
    """Model-produced payload extras are 422 and cannot create any runtime fact."""
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")

    from uuid import UUID as _UUID

    from aidison.domain.models import (
        ConversationActionProposal,
        ConversationActionProposalKind,
    )
    from aidison.infrastructure.store import PostgresDomainStore

    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await session.execute(text("TRUNCATE TABLE projects CASCADE"))
            await session.commit()

        async with _client(engine, factory) as client:
            project_id = await _create_project(client, key_prefix)
            await _approve_requirements(client, key_prefix, project_id)
            message = await client.post(
                f"/api/projects/{project_id}/conversation/messages",
                json={"content": "Research a suitable controller."},
                headers=_headers(key_prefix, "extra-payload-message"),
            )
            assert message.status_code == 200, message.text

            async with factory() as session:
                store = PostgresDomainStore(session)
                proposal = ConversationActionProposal(
                    turn_id=_UUID(message.json()["assistant_turn"]["id"]),
                    kind=ConversationActionProposalKind.START_RESEARCH,
                    summary="Research controller options",
                    proposed_payload={"objective": "Compare controllers", "unexpected": True},
                )
                await store.add_conversation_action_proposal(proposal)
                await session.commit()

            project = await client.get(f"/api/projects/{project_id}")
            response = await client.post(
                f"/api/projects/{project_id}/conversation/proposals/{proposal.id}/accept",
                headers={
                    **_headers(key_prefix, "extra-payload-accept"),
                    "If-Match": f'"{project.json()["revision"]}"',
                },
            )
            assert response.status_code == 422, response.text

            async with factory() as session:
                store = PostgresDomainStore(session)
                persisted = await store.get_conversation_action_proposal(proposal.id)
                assert persisted is not None
                assert persisted.status.value == "proposed"
                assert await session.scalar(
                    text("SELECT count(*) FROM execution_plan_proposals WHERE project_id = :id"),
                    {"id": project_id},
                ) == 0
                assert await session.scalar(
                    text("SELECT count(*) FROM agent_runs WHERE project_id = :id"),
                    {"id": project_id},
                ) == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_start_research_retry_recovers_after_conversation_resolution_conflict(
    key_prefix: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A committed plan is replayed when conversation resolution crashes afterwards."""
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")

    from uuid import UUID as _UUID

    from aidison.application.conversation import ConversationConflictError
    from aidison.domain.models import (
        ConversationActionProposal,
        ConversationActionProposalKind,
    )
    from aidison.infrastructure.store import PostgresDomainStore

    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await session.execute(text("TRUNCATE TABLE projects CASCADE"))
            await session.commit()

        async with _client(engine, factory) as client:
            project_id = await _create_project(client, key_prefix)
            await _approve_requirements(client, key_prefix, project_id)
            message = await client.post(
                f"/api/projects/{project_id}/conversation/messages",
                json={"content": "Research a suitable controller."},
                headers=_headers(key_prefix, "retry-message"),
            )
            assert message.status_code == 200, message.text
            session_id = message.json()["session_id"]

            async with factory() as session:
                store = PostgresDomainStore(session)
                proposal = ConversationActionProposal(
                    turn_id=_UUID(message.json()["assistant_turn"]["id"]),
                    kind=ConversationActionProposalKind.START_RESEARCH,
                    summary="Research controller options",
                    proposed_payload={"objective": "Compare controllers"},
                )
                await store.add_conversation_action_proposal(proposal)
                await session.commit()

            project = await client.get(f"/api/projects/{project_id}")
            headers = {
                **_headers(key_prefix, "retry-accept"),
                "If-Match": f'"{project.json()["revision"]}"',
            }
            original = PostgresDomainStore.resolve_conversation_action_proposal

            async def fail_resolution_once(self, *args, **kwargs):
                raise ConversationConflictError("simulated conversation resolution conflict")

            monkeypatch.setattr(
                PostgresDomainStore,
                "resolve_conversation_action_proposal",
                fail_resolution_once,
            )
            first = await client.post(
                f"/api/projects/{project_id}/conversation/proposals/{proposal.id}/accept",
                headers=headers,
            )
            assert first.status_code == 409, first.text
            monkeypatch.setattr(
                PostgresDomainStore,
                "resolve_conversation_action_proposal",
                original,
            )

            async with factory() as session:
                store = PostgresDomainStore(session)
                pending = await store.get_conversation_action_proposal(proposal.id)
                assert pending is not None
                assert pending.status.value == "proposed"
                plans = list(
                    await session.scalars(
                        text("SELECT id FROM execution_plan_proposals WHERE project_id = :id"),
                        {"id": project_id},
                    )
                )
                assert len(plans) == 1
                committed_plan_id = str(plans[0])

            replay = await client.post(
                f"/api/projects/{project_id}/conversation/proposals/{proposal.id}/accept",
                headers=headers,
            )
            assert replay.status_code == 200, replay.text
            assert replay.json()["result_ref"] == f"execution_plan:{committed_plan_id}"

            async with factory() as session:
                store = PostgresDomainStore(session)
                accepted = await store.get_conversation_action_proposal(proposal.id)
                assert accepted is not None
                assert accepted.resolution_turn_id is not None
                assert await session.scalar(
                    text("SELECT count(*) FROM execution_plan_proposals WHERE project_id = :id"),
                    {"id": project_id},
                ) == 1
                turns = await store.list_conversation_turns(_UUID(session_id), limit=50)
                assert [turn.id for turn in turns].count(accepted.resolution_turn_id) == 1
                assert await session.scalar(
                    text("SELECT count(*) FROM agent_runs WHERE project_id = :id"),
                    {"id": project_id},
                ) == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_add_module_conversation_proposal_creates_unapplied_reshape(
    key_prefix: str,
) -> None:
    """Accepting a chat add-module intent creates a second approval boundary."""
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")

    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await session.execute(text("TRUNCATE TABLE projects CASCADE"))
            await session.commit()

        async with _client(engine, factory) as client:
            project_id = await _create_project(client, key_prefix)
            await _approve_requirements(client, key_prefix, project_id)
            message = await client.post(
                f"/api/projects/{project_id}/conversation/messages",
                json={"content": "Add a telemetry module."},
                headers=_headers(key_prefix, "add-module-message"),
            )
            assert message.status_code == 200, message.text

            from uuid import UUID as _UUID

            from aidison.domain.models import (
                ConversationActionProposal,
                ConversationActionProposalKind,
            )
            from aidison.infrastructure.store import PostgresDomainStore

            async with factory() as session:
                store = PostgresDomainStore(session)
                proposal = ConversationActionProposal(
                    turn_id=_UUID(message.json()["assistant_turn"]["id"]),
                    kind=ConversationActionProposalKind.ADD_MODULE,
                    summary="Add onboard telemetry",
                    proposed_payload={
                        "module": {
                            "key": "telemetry",
                            "name": "Telemetry",
                            "responsibility": "Collect and publish flight telemetry.",
                            "dependency_keys": ["core"],
                        }
                    },
                )
                await store.add_conversation_action_proposal(proposal)
                await session.commit()
                proposal_id = str(proposal.id)

            project = await client.get(f"/api/projects/{project_id}")
            revision = project.json()["revision"]
            accepted = await client.post(
                f"/api/projects/{project_id}/conversation/proposals/{proposal_id}/accept",
                headers={
                    **_headers(key_prefix, "accept-add-module"),
                    "If-Match": f'"{revision}"',
                },
            )
            assert accepted.status_code == 200, accepted.text
            result_ref = accepted.json()["result_ref"]
            assert result_ref.startswith("reshape_proposal:")

            async with factory() as session:
                store = PostgresDomainStore(session)
                reshape = await store.get_project_reshape_proposal(
                    _UUID(result_ref.removeprefix("reshape_proposal:"))
                )
                assert reshape is not None
                assert reshape.status.value == "proposed"
                assert [module.key for module in reshape.new_modules] == ["telemetry"]
                assert len(reshape.affected_module_ids) == 0
                # No project structure is mutated until this reshape is resolved.
                active = await store.get_project(_UUID(project_id))
                assert active is not None
                assert active.revision == revision
                active_modules = await store.list_modules(project_id)
                assert [module.key for module in active_modules] == ["core"]

            events = await client.get(f"/api/projects/{project_id}/events")
            event_types = {event["type"] for event in events.json()}
            assert "project.reshape_proposed" in event_types
            assert "conversation.action_proposal_accepted" in event_types
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_reshape_conversation_proposal_stays_unapplied(key_prefix: str) -> None:
    """A chat restructure may describe removals but cannot apply them itself."""
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")

    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await session.execute(text("TRUNCATE TABLE projects CASCADE"))
            await session.commit()

        async with _client(engine, factory) as client:
            project_id = await _create_project(client, key_prefix)
            await _approve_requirements(client, key_prefix, project_id)
            message = await client.post(
                f"/api/projects/{project_id}/conversation/messages",
                json={"content": "Replace the core module with a controller module."},
                headers=_headers(key_prefix, "reshape-message"),
            )
            assert message.status_code == 200, message.text

            from uuid import UUID as _UUID

            from aidison.domain.models import (
                ConversationActionProposal,
                ConversationActionProposalKind,
            )
            from aidison.infrastructure.store import PostgresDomainStore

            async with factory() as session:
                store = PostgresDomainStore(session)
                active_modules = await store.list_modules(project_id)
                core = next(module for module in active_modules if module.key == "core")
                proposal = ConversationActionProposal(
                    turn_id=_UUID(message.json()["assistant_turn"]["id"]),
                    kind=ConversationActionProposalKind.RESHAPE_PROJECT,
                    summary="Replace core with controller",
                    proposed_payload={
                        "target_goal": "Verify a controller-oriented project structure",
                        "summary": "Replace the initial core boundary with a controller.",
                        "affected_module_ids": [str(core.id)],
                        "new_modules": [
                            {
                                "key": "controller",
                                "name": "Controller",
                                "responsibility": "Own the project control loop.",
                            }
                        ],
                    },
                )
                await store.add_conversation_action_proposal(proposal)
                await session.commit()
                proposal_id = str(proposal.id)

            project = await client.get(f"/api/projects/{project_id}")
            revision = project.json()["revision"]
            accepted = await client.post(
                f"/api/projects/{project_id}/conversation/proposals/{proposal_id}/accept",
                headers={
                    **_headers(key_prefix, "accept-reshape"),
                    "If-Match": f'"{revision}"',
                },
            )
            assert accepted.status_code == 200, accepted.text
            result_ref = accepted.json()["result_ref"]
            assert result_ref.startswith("reshape_proposal:")

            async with factory() as session:
                store = PostgresDomainStore(session)
                reshape = await store.get_project_reshape_proposal(
                    _UUID(result_ref.removeprefix("reshape_proposal:"))
                )
                assert reshape is not None
                assert reshape.status.value == "proposed"
                assert [module.key for module in reshape.new_modules] == ["controller"]
                assert [str(item) for item in reshape.affected_module_ids] == [str(core.id)]
                # The previous blueprint and its module stay authoritative until apply.
                active = await store.get_project(_UUID(project_id))
                assert active is not None
                assert active.revision == revision
                assert [module.key for module in await store.list_modules(project_id)] == ["core"]
    finally:
        await engine.dispose()


# ── SpendBudget conversation integration ─────────────────────────────────────


def _budget_model_factory() -> BaseChatModel:
    model = MagicMock(spec=BaseChatModel)
    bound = MagicMock()
    bound.ainvoke = AsyncMock(
        return_value=AIMessage(
            content=json.dumps(
                {
                    "reply": "I suggest setting a budget of 800 CNY.",
                    "action_proposals": [
                        {
                            "kind": "change_spend_budget",
                            "summary": "Set budget to 800 CNY",
                            "proposed_payload": {
                                "amount": "800.00",
                                "currency": "CNY",
                                "summary": "Max spend for materials",
                            },
                        }
                    ],
                }
            )
        )
    )
    model.bind.return_value = bound
    return model


@pytest.mark.asyncio
async def test_change_spend_budget_accept_creates_proposal_only(
    key_prefix: str,
) -> None:
    """conversation → accept → SpendBudgetProposal created; revision unchanged."""
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")

    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    try:
        from uuid import UUID as _UUID

        from aidison.infrastructure.store import PostgresDomainStore

        async with factory() as session:
            from aidison.application.service import ProjectApplication
            app = ProjectApplication(PostgresDomainStore(session))
            project = await app.create_project(
                name="SB conv test",
                goal="Test budget via conversation",
                idempotency_key=f"{key_prefix}:project",
            )
            await session.commit()

        project_id = str(project.id)

        # Initial intake only permits a reviewable requirements draft. Budget
        # changes are governed actions and therefore begin after requirements
        # and the reviewed module structure have been approved.
        async with _client(engine, factory) as client:
            await _approve_requirements(client, key_prefix, project_id)

        async with _client(engine, factory, model_factory=_budget_model_factory) as client:
            resp = await client.post(
                f"/api/projects/{project_id}/conversation/messages",
                json={"content": "Set budget to 800 CNY"},
                headers=_headers(key_prefix, "msg"),
            )
            assert resp.status_code == 200, resp.text
            session_id = resp.json()["session_id"]

        # Find the budget proposal
        async with factory() as session:
            store = PostgresDomainStore(session)
            proposals = [
                p for p in await store.list_active_action_proposals(UUID(session_id))
                if p.kind.value == "change_spend_budget"
            ]
            assert len(proposals) == 1
            pid = str(proposals[0].id)

        # Accept
        async with _client(engine, factory, model_factory=_budget_model_factory) as client:
            accepted = await client.post(
                f"/api/projects/{project_id}/conversation/proposals/{pid}/accept",
                headers=_headers(key_prefix, "accept", **{"If-Match": "3"}),
            )
            assert accepted.status_code == 200, accepted.text
            result_ref = accepted.json()["result_ref"]
            assert result_ref.startswith("spend_budget:")

        # Verify: proposal exists, revision NOT bumped, no active budget
        async with factory() as session:
            store = PostgresDomainStore(session)
            budget_id = _UUID(result_ref.removeprefix("spend_budget:"))
            bp = await store.get_spend_budget_proposal(budget_id)
            assert bp is not None
            assert bp.status.value == "proposed"
            assert bp.amount == "800.00"
            assert bp.currency == "CNY"

            p = await store.get_project(project.id)
            assert p is not None
            assert p.revision == 3
            assert p.active_spend_budget_revision_id is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_spend_budget_resolve_applied_snapshot(
    key_prefix: str,
) -> None:
    """Propose via API → resolve APPLIED → snapshot includes active budget."""
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")

    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    try:
        from aidison.infrastructure.store import PostgresDomainStore

        async with factory() as session:
            from aidison.application.service import ProjectApplication
            app = ProjectApplication(PostgresDomainStore(session))
            project = await app.create_project(
                name="SB resolve test",
                goal="Test resolve + snapshot",
                idempotency_key=f"{key_prefix}:project",
            )
            await session.commit()

        project_id = str(project.id)

        # Propose via API
        async with _client(engine, factory) as client:
            resp = await client.post(
                f"/api/projects/{project_id}/spend-budget/proposals",
                json={"amount": "1500.00", "currency": "JPY", "summary": "Resolve test"},
                headers=_headers(key_prefix, "propose", **{"If-Match": "1"}),
            )
            assert resp.status_code == 200, resp.text
            proposal_id = resp.json()["spend_budget_proposal"]["id"]

        # Resolve APPLIED
        async with _client(engine, factory) as client:
            resp = await client.post(
                f"/api/spend-budget-proposals/{proposal_id}/resolve",
                json={"decision": "applied"},
                headers=_headers(key_prefix, "resolve", **{"If-Match": "1"}),
            )
            assert resp.status_code == 200, resp.text
            rev = resp.json()["spend_budget_revision"]
            assert rev["revision"] == 1
            assert rev["amount"] == "1500.00"
            assert rev["currency"] == "JPY"

        # Snapshot must include active budget
        async with _client(engine, factory) as client:
            resp = await client.get(f"/api/projects/{project_id}/snapshot")
            assert resp.status_code == 200, resp.text
            snap = resp.json()
            assert snap["spend_budget"] is not None
            assert snap["spend_budget"]["amount"] == "1500.00"
            assert snap["spend_budget"]["currency"] == "JPY"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_spend_budget_stale_if_match_returns_412(
    key_prefix: str,
) -> None:
    """Stale If-Match on resolve must return 412, not 409."""
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")

    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    try:
        from aidison.infrastructure.store import PostgresDomainStore

        async with factory() as session:
            from aidison.application.service import ProjectApplication
            app = ProjectApplication(PostgresDomainStore(session))
            project = await app.create_project(
                name="SB CAS test",
                goal="Test 412 on stale",
                idempotency_key=f"{key_prefix}:project",
            )
            await session.commit()

        project_id = str(project.id)

        async with _client(engine, factory) as client:
            resp = await client.post(
                f"/api/projects/{project_id}/spend-budget/proposals",
                json={"amount": "500.00", "currency": "EUR", "summary": "CAS"},
                headers=_headers(key_prefix, "propose", **{"If-Match": "1"}),
            )
            assert resp.status_code == 200, resp.text
            proposal_id = resp.json()["spend_budget_proposal"]["id"]

        # Stale If-Match → 412
        async with _client(engine, factory) as client:
            resp = await client.post(
                f"/api/spend-budget-proposals/{proposal_id}/resolve",
                json={"decision": "applied"},
                headers=_headers(key_prefix, "stale", **{"If-Match": "999"}),
            )
            assert resp.status_code == 412, resp.text

        # Correct If-Match → 200
        async with _client(engine, factory) as client:
            resp = await client.post(
                f"/api/spend-budget-proposals/{proposal_id}/resolve",
                json={"decision": "applied"},
                headers=_headers(key_prefix, "ok", **{"If-Match": "1"}),
            )
            assert resp.status_code == 200, resp.text
    finally:
        await engine.dispose()

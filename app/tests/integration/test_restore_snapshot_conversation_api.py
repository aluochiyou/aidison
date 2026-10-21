"""PostgreSQL acceptance tests for governed conversation snapshot restores."""

from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

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
def key_prefix() -> str:
    return str(uuid4())


def _restore_model_factory(snapshot_id: str) -> BaseChatModel:
    model = MagicMock(spec=BaseChatModel)
    bound = MagicMock()
    bound.ainvoke = AsyncMock(
        return_value=AIMessage(
            content=json.dumps(
                {
                    "reply": "I found the requested historical snapshot.",
                    "action_proposals": [
                        {
                            "kind": "restore_solution_snapshot",
                            "summary": "Restore the selected snapshot",
                            "proposed_payload": {"snapshot_id": snapshot_id},
                        }
                    ],
                }
            )
        )
    )
    model.bind.return_value = bound
    return model


def _client(factory: object, snapshot_id: str) -> httpx.AsyncClient:
    api = create_app(
        factory,
        conversation_model_factory=lambda: _restore_model_factory(snapshot_id),
        module_discovery_model_factory=module_discovery_model_factory,
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api), base_url="http://test"
    )


def _headers(key_prefix: str, suffix: str, **extra: str) -> dict[str, str]:
    return {"Idempotency-Key": f"{key_prefix}:{suffix}", **extra}


async def _create_project(client: httpx.AsyncClient, key_prefix: str, suffix: str) -> str:
    response = await client.post(
        "/api/projects",
        json={"name": f"Restore {suffix}", "goal": "Verify governed history restore"},
        headers=_headers(key_prefix, f"project:{suffix}"),
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _prepare_snapshot(
    client: httpx.AsyncClient, key_prefix: str, project_id: str, suffix: str
) -> tuple[str, int]:
    await approve_requirements_and_apply_initial_modules(
        client,
        project_id=project_id,
        goal="Verify governed history restore",
        idempotency_key_prefix=f"{key_prefix}:requirements:{suffix}",
    )
    saved = await client.post(
        f"/api/projects/{project_id}/solution-snapshots",
        json={"label": f"{suffix} baseline"},
        headers={**_headers(key_prefix, f"snapshot:{suffix}"), "If-Match": '"3"'},
    )
    assert saved.status_code == 201, saved.text
    return saved.json()["solution_snapshot"]["id"], saved.json()["project_revision"]


async def _proposal_id_from_message(
    client: httpx.AsyncClient,
    key_prefix: str,
    project_id: str,
    suffix: str,
    snapshot_id: str,
) -> str:
    response = await client.post(
        f"/api/projects/{project_id}/conversation/messages",
        json={"content": "Restore the selected saved version."},
        headers=_headers(key_prefix, f"message:{suffix}"),
    )
    assert response.status_code == 200, response.text
    snapshot = await client.get(f"/api/projects/{project_id}/snapshot")
    assert snapshot.status_code == 200, snapshot.text
    proposals = snapshot.json()["conversation"]["open_action_proposals"]
    matching = [
        proposal
        for proposal in proposals
        if proposal["kind"] == "restore_solution_snapshot"
        and proposal["proposed_payload"] == {"snapshot_id": snapshot_id}
    ]
    assert len(matching) == 1
    return matching[0]["id"]


@pytest.mark.asyncio
async def test_restore_conversation_rejects_foreign_snapshot_without_side_effects(
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

        # Both projects reach the same revision. The foreign snapshot would be
        # restorable by the old bug because restore uses snapshot.project_id.
        async with _client(factory, str(uuid4())) as setup_client:
            project_a = await _create_project(setup_client, key_prefix, "a")
            project_b = await _create_project(setup_client, key_prefix, "b")
            snapshot_a, revision_a = await _prepare_snapshot(
                setup_client, key_prefix, project_a, "a"
            )
            snapshot_b, revision_b = await _prepare_snapshot(
                setup_client, key_prefix, project_b, "b"
            )
        assert revision_a == revision_b == 4

        async with _client(factory, snapshot_b) as client:
            proposal_id = await _proposal_id_from_message(
                client, key_prefix, project_a, "foreign", snapshot_b
            )
            before_a = await client.get(f"/api/projects/{project_a}")
            before_b = await client.get(f"/api/projects/{project_b}")
            before_events_a = await client.get(f"/api/projects/{project_a}/events")
            before_events_b = await client.get(f"/api/projects/{project_b}/events")
            assert all(
                item.status_code == 200
                for item in (before_a, before_b, before_events_a, before_events_b)
            )

            rejected = await client.post(
                f"/api/projects/{project_a}/conversation/proposals/{proposal_id}/accept",
                headers={
                    **_headers(key_prefix, "accept:foreign"),
                    "If-Match": '"4"',
                },
            )
            assert rejected.status_code == 409, rejected.text

            after_a = await client.get(f"/api/projects/{project_a}")
            after_b = await client.get(f"/api/projects/{project_b}")
            after_events_a = await client.get(f"/api/projects/{project_a}/events")
            after_events_b = await client.get(f"/api/projects/{project_b}/events")
            assert after_a.json()["revision"] == before_a.json()["revision"] == 4
            assert after_b.json()["revision"] == before_b.json()["revision"] == 4
            assert after_events_a.json() == before_events_a.json()
            assert after_events_b.json() == before_events_b.json()

            project_snapshot = await client.get(f"/api/projects/{project_a}/snapshot")
            proposal = project_snapshot.json()["conversation"]["open_action_proposals"][0]
            assert proposal["id"] == proposal_id
            assert proposal["status"] == "proposed"
            assert proposal["resolution_turn_id"] is None

        # Same-project restore remains available through the same public seam.
        async with _client(factory, snapshot_a) as client:
            proposal_id = await _proposal_id_from_message(
                client, key_prefix, project_a, "same-project", snapshot_a
            )
            accepted = await client.post(
                f"/api/projects/{project_a}/conversation/proposals/{proposal_id}/accept",
                headers={
                    **_headers(key_prefix, "accept:same-project"),
                    "If-Match": '"4"',
                },
            )
            assert accepted.status_code == 200, accepted.text
            assert accepted.json()["proposal"]["status"] == "accepted"
            assert accepted.json()["result_ref"] == f"solution_snapshot:{snapshot_a}"
    finally:
        await engine.dispose()

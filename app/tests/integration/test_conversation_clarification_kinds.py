"""PostgreSQL persistence of the full clarification-kind vocabulary.

The conversation_clarifications.kind CHECK originally only allowed the six
legacy kinds, so a model reply emitting usage/resource/skill failed at INSERT
with IntegrityError while the user turn was already committed. This suite uses
deterministic fake conversation responses to prove the widened CHECK persists
the new kinds and keeps the legacy kinds working. It requires the migration
k1b2c3d4e5f6 (or newer head) to be applied to the test database.
"""

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

_ALL_KINDS = (
    "usage",
    "requirement",
    "constraint",
    "module",
    "selection",
    "budget",
    "resource",
    "skill",
    "other",
)


@pytest.fixture
def key_prefix():
    return str(uuid4())


def _headers(key_prefix: str, suffix: str, **extra) -> dict[str, str]:
    return {"Idempotency-Key": f"{key_prefix}:{suffix}", **extra}


def _clarifications_model_factory(clarifications: list[dict[str, str]]) -> BaseChatModel:
    model = MagicMock(spec=BaseChatModel)
    bound = MagicMock()
    bound.ainvoke = AsyncMock(
        return_value=AIMessage(
            content=json.dumps(
                {
                    "reply": "请补充以下信息。",
                    "clarifications": clarifications,
                },
                ensure_ascii=False,
            )
        )
    )
    model.bind.return_value = bound
    return model


async def _create_project(client: httpx.AsyncClient, key_prefix: str) -> str:
    r = await client.post(
        "/api/projects",
        json={"name": "Clarification kinds fixture", "goal": "Verify kind persistence"},
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
        goal="Verify kind persistence",
        idempotency_key_prefix=f"{key_prefix}:reqs",
    )


async def _open_clarification_kinds(
    client: httpx.AsyncClient, project_id: str
) -> list[str]:
    snapshot = await client.get(f"/api/projects/{project_id}/snapshot")
    assert snapshot.status_code == 200, snapshot.text
    return [
        item["kind"]
        for item in snapshot.json()["conversation"]["open_clarifications"]
    ]


@pytest.mark.asyncio
async def test_pre_approval_usage_clarification_persists_without_conflict(
    key_prefix: str,
) -> None:
    """The exact 409 path: an unapproved project, model emits kind=usage."""
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")
    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await session.execute(text("TRUNCATE TABLE projects CASCADE"))
            await session.commit()

        api = create_app(
            factory,
            conversation_model_factory=lambda: _clarifications_model_factory(
                [{"question": "这个机械臂用在什么场景？", "kind": "usage"}]
            ),
            module_discovery_model_factory=module_discovery_model_factory,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api), base_url="http://test"
        ) as client:
            project_id = await _create_project(client, key_prefix)
            response = await client.post(
                f"/api/projects/{project_id}/conversation/messages",
                json={"content": "我想做一台自动拾取物料的机械臂。"},
                headers=_headers(key_prefix, "usage-msg"),
            )
            assert response.status_code == 200, response.text

            kinds = await _open_clarification_kinds(client, project_id)
            assert kinds == ["usage"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_post_approval_persists_every_clarification_kind(key_prefix: str) -> None:
    """Post-approval the gate is skipped, so all emitted kinds are persisted."""
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")
    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await session.execute(text("TRUNCATE TABLE projects CASCADE"))
            await session.commit()

        api = create_app(
            factory,
            conversation_model_factory=lambda: _clarifications_model_factory(
                [
                    {"question": f"关于 {kind} 的问题", "kind": kind}
                    for kind in _ALL_KINDS
                ]
            ),
            module_discovery_model_factory=module_discovery_model_factory,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api), base_url="http://test"
        ) as client:
            project_id = await _create_project(client, key_prefix)
            await _approve_requirements(client, key_prefix, project_id)

            response = await client.post(
                f"/api/projects/{project_id}/conversation/messages",
                json={"content": "请逐项确认。"},
                headers=_headers(key_prefix, "kinds-msg"),
            )
            assert response.status_code == 200, response.text

            kinds = await _open_clarification_kinds(client, project_id)
            assert sorted(kinds) == sorted(_ALL_KINDS)
    finally:
        await engine.dispose()

"""Shared API fixtures for the governed initial-module path.

These helpers deliberately exercise the public product contract instead of
creating a live blueprint through an application-service shortcut.  Tests
that need an already-structured project can therefore stay focused on their
own concern without reintroducing the retired ``requirements.modules`` path.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from unittest.mock import AsyncMock, MagicMock

import httpx
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage

DEFAULT_DISCOVERY_MODULES: tuple[dict[str, object], ...] = (
    {
        "key": "core",
        "name": "Core",
        "responsibility": "Core logic",
    },
)


def module_discovery_model_factory(
    modules: Sequence[Mapping[str, object]] = DEFAULT_DISCOVERY_MODULES,
) -> BaseChatModel:
    """Return a deterministic discovery model for HTTP integration tests."""
    model = MagicMock(spec=BaseChatModel)
    bound = MagicMock()
    bound.ainvoke = AsyncMock(
        return_value=AIMessage(
            content=json.dumps(
                {
                    "summary": "Bootstrap the reviewed module structure.",
                    "modules": [dict(module) for module in modules],
                }
            )
        )
    )
    model.bind.return_value = bound
    return model


async def approve_requirements_and_apply_initial_modules(
    client: httpx.AsyncClient,
    *,
    project_id: str,
    goal: str,
    idempotency_key_prefix: str,
    requirement_context: Mapping[str, object] | None = None,
) -> list[dict[str, object]]:
    """Approve requirements, discover modules, and explicitly apply the proposal.

    A newly created project is at revision 1.  Requirements approval creates
    revision 2; applying the reviewed reshape proposal creates revision 3.
    """
    payload = {"goal": goal, **(dict(requirement_context) if requirement_context else {})}
    requirements = await client.post(
        f"/api/projects/{project_id}/requirements",
        json=payload,
        headers={
            "Idempotency-Key": f"{idempotency_key_prefix}:requirements",
            "If-Match": '"1"',
        },
    )
    assert requirements.status_code == 200, requirements.text
    assert requirements.json()["modules"] == []

    discovery = await client.post(
        f"/api/projects/{project_id}/module-discovery",
        headers={
            "Idempotency-Key": f"{idempotency_key_prefix}:module-discovery",
            "If-Match": '"2"',
        },
    )
    assert discovery.status_code == 201, discovery.text
    proposal = discovery.json()["reshape_proposal"]

    applied = await client.post(
        f"/api/reshape-proposals/{proposal['id']}/resolve",
        json={"decision": "applied"},
        headers={
            "Idempotency-Key": f"{idempotency_key_prefix}:apply-module-discovery",
            "If-Match": '"2"',
        },
    )
    assert applied.status_code == 200, applied.text

    snapshot = await client.get(f"/api/projects/{project_id}/snapshot")
    assert snapshot.status_code == 200, snapshot.text
    return snapshot.json()["modules"]

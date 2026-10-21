from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from pydantic import ValidationError

from aidison.agents.contracts import InitialModuleDiscoveryPayload
from aidison.agents.module_discovery import JsonModeInitialModuleDiscoveryAgent
from aidison.api.schemas import ModuleDiscoveryRequest
from aidison.application.service import DomainConflictError, ProjectApplication
from tests.fakes import InMemoryDomainStore


def test_initial_module_discovery_defaults_to_deep_reasoning() -> None:
    assert ModuleDiscoveryRequest().structure_depth == "deep"


@pytest.mark.asyncio
async def test_approved_initial_requirements_cannot_materialize_modules_directly() -> None:
    store = InMemoryDomainStore()
    application = ProjectApplication(store)
    project = await application.create_project(
        name="Initial structure guard",
        goal="Build a safe monitor",
        idempotency_key="initial-structure-guard:project",
    )

    with pytest.raises(DomainConflictError, match="module discovery"):
        await application.approve_requirements(
            project_id=project.id,
            expected_project_revision=project.revision,
            goal=project.goal,
            hard_constraints=(),
            preferences=(),
            available_resources=(),
            unknowns=(),
            modules=(
                {"key": "manual", "name": "Manual", "responsibility": "Bypass guard"},
            ),
            idempotency_key="initial-structure-guard:requirements",
        )


def test_initial_module_discovery_rejects_unknown_or_self_dependencies() -> None:
    with pytest.raises(ValidationError, match="dependencies"):
        InitialModuleDiscoveryPayload.model_validate(
            {
                "summary": "A bounded structure.",
                "modules": [
                    {
                        "key": "control",
                        "name": "Control",
                        "responsibility": "Coordinate work.",
                        "dependency_keys": ["unknown"],
                    }
                ],
            }
        )


def test_initial_module_discovery_rejects_cyclic_dependencies() -> None:
    with pytest.raises(ValidationError, match="acyclic"):
        InitialModuleDiscoveryPayload.model_validate(
            {
                "summary": "A bounded structure.",
                "modules": [
                    {
                        "key": "sensing",
                        "name": "Sensing",
                        "responsibility": "Measure the environment.",
                        "dependency_keys": ["control"],
                    },
                    {
                        "key": "control",
                        "name": "Control",
                        "responsibility": "Coordinate sensing.",
                        "dependency_keys": ["sensing"],
                    },
                ],
            }
        )


@pytest.mark.asyncio
async def test_initial_module_discovery_agent_returns_only_validated_structure() -> None:
    model = MagicMock(spec=BaseChatModel)
    bound = MagicMock()
    bound.ainvoke = AsyncMock(
        return_value=AIMessage(
            content=json.dumps(
                {
                    "summary": "Separate sensing from power and control.",
                    "modules": [
                        {
                            "key": "sensing",
                            "name": "Sensing",
                            "responsibility": "Measure the requested environment.",
                        },
                        {
                            "key": "power_control",
                            "name": "Power and control",
                            "responsibility": "Safely power and coordinate sensing.",
                            "dependency_keys": ["sensing"],
                        },
                    ],
                }
            )
        )
    )
    model.bind.return_value = bound

    payload = await JsonModeInitialModuleDiscoveryAgent(model).ainvoke("confirmed requirements")

    assert [item.key for item in payload.modules] == ["sensing", "power_control"]
    bound.ainvoke.assert_awaited_once()


@pytest.mark.asyncio
async def test_initial_module_discovery_retries_empty_model_output_once() -> None:
    model = MagicMock(spec=BaseChatModel)
    bound = MagicMock()
    bound.ainvoke = AsyncMock(
        side_effect=[
            AIMessage(content=""),
            AIMessage(
                content=json.dumps(
                    {
                        "summary": "A bounded structure.",
                        "modules": [
                            {
                                "key": "control",
                                "name": "Control",
                                "responsibility": "Coordinate the system.",
                            }
                        ],
                    }
                )
            ),
        ]
    )
    model.bind.return_value = bound

    payload = await JsonModeInitialModuleDiscoveryAgent(model).ainvoke("requirements")

    assert payload.modules[0].key == "control"
    assert bound.ainvoke.await_count == 2
    retry_messages = bound.ainvoke.await_args_list[1].args[0]
    assert '"dependency_keys"' in retry_messages[0]["content"]
    assert '"open_questions"' in retry_messages[0]["content"]


@pytest.mark.asyncio
async def test_initial_module_discovery_retries_truncated_model_output_once() -> None:
    model = MagicMock(spec=BaseChatModel)
    bound = MagicMock()
    bound.ainvoke = AsyncMock(
        side_effect=[
            AIMessage(
                content='{"summary":"partial"}',
                response_metadata={"finish_reason": "length"},
            ),
            AIMessage(
                content=json.dumps(
                    {
                        "summary": "A bounded structure.",
                        "modules": [
                            {
                                "key": "control",
                                "name": "Control",
                                "responsibility": "Coordinate the system.",
                            }
                        ],
                    }
                )
            ),
        ]
    )
    model.bind.return_value = bound

    payload = await JsonModeInitialModuleDiscoveryAgent(model).ainvoke("requirements")

    assert payload.modules[0].key == "control"
    assert bound.ainvoke.await_count == 2

"""Contract tests for the model-facing research strategy planner."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage

from aidison.agents.research_strategy import (
    RESEARCH_STRATEGY_RETRY_PROMPT,
    RESEARCH_STRATEGY_SYSTEM_PROMPT,
    JsonModeResearchStrategyPlanner,
    ResearchStrategyOutputValidationError,
    ResponsesJsonSchemaResearchStrategyPlanner,
)


def test_strategy_prompts_expose_closed_task_enums_and_key_format() -> None:
    """The model must see the same closed values that Pydantic will admit."""

    assert "priority` is exactly `must` or `should`" in RESEARCH_STRATEGY_SYSTEM_PROMPT
    assert "`evidence`, `candidate`, `compatibility`, or\n`constraint`" in (
        RESEARCH_STRATEGY_SYSTEM_PROMPT
    )
    assert "cannot contain spaces, Chinese text, or `:`" in RESEARCH_STRATEGY_RETRY_PROMPT


@pytest.mark.asyncio
async def test_strategy_planner_repairs_invalid_json_with_safe_field_feedback() -> None:
    """A model gets actionable schema feedback, never its rejected raw output."""

    module_id = uuid4()
    model = MagicMock(spec=BaseChatModel)
    bound = MagicMock()
    bound.ainvoke = AsyncMock(
        side_effect=(
            AIMessage(content="{this is intentionally not valid JSON}"),
            AIMessage(
                content=json.dumps(
                    {
                        "summary": "Research the approved module.",
                        "scope_module_ids": [str(module_id)],
                        "tasks": [
                            {
                                "task_key": "power.sources",
                                "title": "Power sources",
                                "objective": "Find verifiable power constraints.",
                                "module_ids": [str(module_id)],
                                "depends_on_task_keys": [],
                                "priority": "must",
                                "expected_outputs": ["evidence"],
                                "stop_conditions": ["Report evidence or a bounded gap."],
                            }
                        ],
                        "source_strategy": "primary",
                    }
                )
            ),
        )
    )
    model.bind.return_value = bound

    strategy = await JsonModeResearchStrategyPlanner(model).ainvoke(
        json.dumps({"objective": "Research the module."})
    )

    assert strategy.tasks[0].task_key == "power.sources"
    assert bound.ainvoke.await_count == 2
    retry_context = json.loads(bound.ainvoke.await_args_list[1].args[0][1]["content"])
    assert retry_context["strategy_schema_feedback"] == [
        {"path": "$", "code": "json_invalid"}
    ]
    retry_prompt = bound.ainvoke.await_args_list[1].args[0][1]["content"]
    assert "{this is intentionally not valid JSON}" not in retry_prompt


@pytest.mark.asyncio
async def test_responses_strategy_planner_requests_closed_json_schema() -> None:
    """Native structured output constrains shape before local DAG admission."""

    module_id = uuid4()
    client = MagicMock()
    client.responses.create = AsyncMock(
        return_value=SimpleNamespace(
            status="completed",
            output_text=json.dumps(
                {
                    "schema_version": "research-strategy-v1",
                    "summary": "Research approved power constraints.",
                    "decision_notes": [],
                    "scope_module_ids": [str(module_id)],
                    "tasks": [
                        {
                            "task_key": "power.constraints",
                            "title": "Power constraints",
                            "objective": "Find verifiable power constraints.",
                            "module_ids": [str(module_id)],
                            "depends_on_task_keys": [],
                            "priority": "must",
                            "expected_outputs": ["evidence", "constraint"],
                            "stop_conditions": ["Report evidence or a bounded gap."],
                            "research_lenses": ["specifications"],
                        }
                    ],
                    "deferred_questions": [],
                    "risk_notes": [],
                    "source_strategy": "primary",
                }
            ),
        )
    )

    strategy = await ResponsesJsonSchemaResearchStrategyPlanner(
        client=client,
        model="deepseek-v4-pro",
        timeout_seconds=600,
    ).ainvoke('{"objective":"Research the module."}')

    assert strategy.tasks[0].task_key == "power.constraints"
    request = client.responses.create.await_args.kwargs
    assert request["reasoning"] == {"effort": "high"}
    assert request["max_output_tokens"] == 12_000
    assert request["text"]["format"]["type"] == "json_schema"
    schema = request["text"]["format"]["schema"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["tasks"]["items"]["properties"]["priority"][
        "enum"
    ] == ["must", "should"]


@pytest.mark.asyncio
async def test_responses_strategy_planner_keeps_invalid_output_diagnostics_safe() -> None:
    client = MagicMock()
    client.responses.create = AsyncMock(
        return_value=SimpleNamespace(status="completed", output_text="{broken secret-like text}")
    )

    with pytest.raises(ResearchStrategyOutputValidationError) as exc_info:
        await ResponsesJsonSchemaResearchStrategyPlanner(
            client=client,
            model="deepseek-v4-pro",
            timeout_seconds=600,
        ).ainvoke('{"objective":"Research the module."}')

    assert exc_info.value.diagnostics == ("schema:$:json_invalid",)
    assert "secret-like" not in str(exc_info.value)

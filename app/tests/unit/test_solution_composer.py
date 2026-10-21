from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from pydantic import ValidationError

from aidison.solution.composer import JsonModeSolutionComposer
from aidison.solution.payloads import SolutionCompositionPayload


def test_solution_composition_rejects_interface_that_is_not_declared_by_endpoints() -> None:
    producer = uuid4()
    consumer = uuid4()
    with pytest.raises(ValidationError, match="input interface"):
        SolutionCompositionPayload.model_validate(
            {
                "elements": [
                    {
                        "element_key": "power.source",
                        "module_id": str(producer),
                        "responsibility": "Supply power",
                        "selected_candidate_id": str(uuid4()),
                        "output_interface_keys": [],
                    },
                    {
                        "element_key": "control.consumer",
                        "module_id": str(consumer),
                        "responsibility": "Consume power",
                        "selected_candidate_id": str(uuid4()),
                        "input_interface_keys": ["power.feed"],
                    },
                ],
                "interfaces": [],
            }
        )


@pytest.mark.asyncio
async def test_json_mode_composer_returns_raw_output_without_structured_repair() -> None:
    model = MagicMock(spec=BaseChatModel)
    bound = MagicMock()
    bound.ainvoke = AsyncMock(return_value=AIMessage(content='{"elements":[]}'))
    model.bind.return_value = bound

    result = await JsonModeSolutionComposer(model).ainvoke(
        {"messages": [{"role": "user", "content": "Compose one bounded solution."}]}
    )

    assert result == {"raw_json": '{"elements":[]}'}
    model.bind.assert_called_once_with(response_format={"type": "json_object"})
    bound.ainvoke.assert_awaited_once()


@pytest.mark.asyncio
async def test_json_mode_composer_adapts_solution_executor_protocol_without_hidden_retry() -> None:
    model = MagicMock(spec=BaseChatModel)
    bound = MagicMock()
    bound.ainvoke = AsyncMock(return_value=AIMessage(content='{"elements":[]}'))
    model.bind.return_value = bound

    raw_json = await JsonModeSolutionComposer(model).compose(
        instruction="Compose only the approved module selection.",
        input_refs=("artifact+sha256://contract/1",),
    )

    assert raw_json == '{"elements":[]}'
    request = bound.ainvoke.await_args.args[0]
    assert request[-1]["content"] == "Compose only the approved module selection."
    model.bind.assert_called_once_with(response_format={"type": "json_object"})

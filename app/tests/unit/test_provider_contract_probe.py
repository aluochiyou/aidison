from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage

from aidison.providers.contract_probe import (
    ProviderContractProbeError,
    probe_chat_json_contract,
)


def _model(response: AIMessage) -> tuple[BaseChatModel, MagicMock]:
    model = MagicMock(spec=BaseChatModel)
    bound = MagicMock()
    bound.ainvoke = AsyncMock(return_value=response)
    model.bind.return_value = bound
    return model, bound


@pytest.mark.asyncio
async def test_json_contract_probe_uses_the_runtime_response_format_and_closed_shape() -> None:
    model, bound = _model(
        AIMessage(content='{"probe":"aidison-provider-contract-v1","ok":true}')
    )

    result = await probe_chat_json_contract(model, timeout_seconds=1)

    assert result.passed is True
    assert result.contract == "chat-json-object.v1"
    model.bind.assert_called_once_with(
        response_format={"type": "json_object"},
        max_tokens=64,
    )
    messages = bound.ainvoke.await_args.args[0]
    assert len(messages) == 2
    assert "protocol probe" in str(messages[0].content)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    (
        "```json\n{\"probe\":\"aidison-provider-contract-v1\",\"ok\":true}\n```",
        '{"probe":"aidison-provider-contract-v1","ok":true,"extra":"not-closed"}',
        '{"probe":"wrong-marker","ok":true}',
        '{"probe":"aidison-provider-contract-v1","ok":false}',
    ),
)
async def test_json_contract_probe_rejects_loose_or_semantically_wrong_json(content: str) -> None:
    model, _ = _model(AIMessage(content=content))

    with pytest.raises(ProviderContractProbeError, match="closed JSON object"):
        await probe_chat_json_contract(model, timeout_seconds=1)


@pytest.mark.asyncio
async def test_json_contract_probe_has_an_independent_timeout() -> None:
    model = MagicMock(spec=BaseChatModel)
    bound = MagicMock()

    async def never_returns(*args: object, **kwargs: object) -> AIMessage:
        del args, kwargs
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    bound.ainvoke = AsyncMock(side_effect=never_returns)
    model.bind.return_value = bound

    with pytest.raises(ProviderContractProbeError, match="timed out"):
        await probe_chat_json_contract(model, timeout_seconds=0.001)


@pytest.mark.asyncio
async def test_json_contract_probe_normalizes_provider_exceptions() -> None:
    model = MagicMock(spec=BaseChatModel)
    bound = MagicMock()
    bound.ainvoke = AsyncMock(side_effect=RuntimeError("secret upstream detail"))
    model.bind.return_value = bound

    with pytest.raises(ProviderContractProbeError, match="request failed") as raised:
        await probe_chat_json_contract(model, timeout_seconds=1)
    assert "secret upstream detail" not in str(raised.value)

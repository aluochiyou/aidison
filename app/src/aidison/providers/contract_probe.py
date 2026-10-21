"""Opt-in live checks for the model contract Aidison actually depends on."""

from __future__ import annotations

import asyncio
import json
from typing import Literal

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, ConfigDict

_PROBE_MARKER = "aidison-provider-contract-v1"


class ProviderContractProbeError(RuntimeError):
    """The configured provider did not preserve Aidison's JSON-mode contract."""


class ProviderContractProbeResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    contract: Literal["chat-json-object.v1"] = "chat-json-object.v1"
    marker: Literal["aidison-provider-contract-v1"] = "aidison-provider-contract-v1"
    passed: Literal[True] = True


class _ProbePayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    probe: Literal["aidison-provider-contract-v1"]
    ok: Literal[True]


async def probe_chat_json_contract(
    model: BaseChatModel,
    *,
    timeout_seconds: float,
) -> ProviderContractProbeResult:
    """Send one side-effect-free request and strictly validate its JSON shape."""

    if timeout_seconds <= 0:
        raise ValueError("provider contract probe timeout must be positive")
    messages = (
        SystemMessage(
            content=(
                "You are a protocol probe. Return one JSON object only. "
                "Do not use markdown or add fields."
            )
        ),
        HumanMessage(content=f'Return {{"probe":"{_PROBE_MARKER}","ok":true}} exactly.'),
    )
    try:
        response = await asyncio.wait_for(
            model.bind(
                response_format={"type": "json_object"},
                max_tokens=64,
            ).ainvoke(messages),
            timeout=timeout_seconds,
        )
    except TimeoutError as error:
        raise ProviderContractProbeError("provider contract probe timed out") from error
    except Exception as error:
        raise ProviderContractProbeError("provider contract probe request failed") from error

    try:
        payload = _ProbePayload.model_validate(json.loads(_message_text(response)))
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise ProviderContractProbeError(
            "provider did not return the required closed JSON object"
        ) from error
    return ProviderContractProbeResult(marker=payload.probe, passed=payload.ok)


def _message_text(message: object) -> str:
    if not isinstance(message, AIMessage):
        raise TypeError("provider contract probe requires an AIMessage response")
    content = message.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text_parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if isinstance(text, str):
                text_parts.append(text)
        if text_parts:
            return "".join(text_parts)
    raise TypeError("provider contract probe response has no text content")


__all__ = [
    "ProviderContractProbeError",
    "ProviderContractProbeResult",
    "probe_chat_json_contract",
]

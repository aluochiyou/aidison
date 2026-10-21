from __future__ import annotations

from typing import Any, cast

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage

from aidison.agents.contracts import InitialModuleDiscoveryPayload

MODULE_DISCOVERY_SYSTEM_PROMPT = """You are Aidison's bounded initial module planner.
Using only the confirmed project requirements and user planning brief, propose a reviewable
module boundary for a DIY project. Return exactly one JSON object matching
InitialModuleDiscoveryPayload. The response must be valid JSON, with no Markdown fences or
explanation. Every module needs a stable lowercase key, name, responsibility, dependency_keys,
acceptance, and open_questions. Modules are meaningful coupled subsystems, not individual parts.
Honor structure_depth and planning_guidance: deep planning may use several boundaries only when
they have distinct responsibilities, interfaces, acceptance conditions, or failure containment;
focused planning should stay minimal. Dependencies may only name other proposed module keys, must
point in the actual dependency direction, and must form an acyclic graph. Do not research products,
invent external facts,
call tools, create jobs, or claim that the structure is approved. The user must review and
explicitly apply your proposal before it becomes project structure."""

MODULE_DISCOVERY_RETRY_PROMPT = """Return one complete JSON object only. It must match the
InitialModuleDiscoveryPayload shape exactly: a non-empty summary and 1-8 modules; every module
has key, name, responsibility, dependency_keys, acceptance, and open_questions. Use [] for empty
arrays. Use this exact field layout, with English lowercase keys:
{"summary":"...","modules":[{"key":"flight_safety","name":"...","responsibility":"...","dependency_keys":[],"acceptance":["..."],"open_questions":["..."]}]}
Dependencies must form an acyclic graph. Do not use Markdown, comments, renamed keys, or
additional keys. Do not explain your answer."""


class JsonModeInitialModuleDiscoveryAgent:
    """One bounded, tool-free model call for a reviewable initial structure."""

    def __init__(self, model: BaseChatModel) -> None:
        self._model = model

    async def ainvoke(self, confirmed_requirements: str) -> InitialModuleDiscoveryPayload:
        last_error: ValueError | None = None
        for attempt in range(2):
            system_prompt = (
                MODULE_DISCOVERY_SYSTEM_PROMPT
                if attempt == 0
                else MODULE_DISCOVERY_RETRY_PROMPT
            )
            response = await self._model.bind(
                response_format={"type": "json_object"}
            ).ainvoke(
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": confirmed_requirements},
                ]
            )
            try:
                last_error = self._validate_response_metadata(response)
                if last_error is not None:
                    raise last_error
                content = cast(BaseMessage, response).content
                if not isinstance(content, str) or not content.strip():
                    raise ValueError("initial module discovery returned empty content")
                return InitialModuleDiscoveryPayload.model_validate_json(content)
            except ValueError as exc:
                last_error = exc
                if attempt == 1:
                    raise
        raise last_error or ValueError("initial module discovery failed")

    @staticmethod
    def _validate_response_metadata(response: Any) -> ValueError | None:
        metadata = getattr(response, "response_metadata", {})
        if not isinstance(metadata, dict):
            return None
        finish_reason = metadata.get("finish_reason")
        if finish_reason == "length":
            return ValueError("initial module discovery response was truncated")
        return None

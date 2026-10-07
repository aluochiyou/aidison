"""Tool-free generation of a reviewable Research strategy."""

from __future__ import annotations

import json
from typing import Any, Protocol, cast

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage

from aidison.domain.models import ResearchStrategyProposal

RESEARCH_STRATEGY_SYSTEM_PROMPT = """You are Aidison's bounded Research strategy planner.
Return exactly one JSON object matching ResearchStrategyProposal.  The response must be valid
JSON only: no Markdown, commentary, source claims, hidden reasoning, coverage keys, budgets,
tools, execution grants, artifact references, or approval language.

You receive an approved requirement, a user objective, and the allowed module scope.  Each module
may include its approved responsibility, acceptance criteria, open questions, and dependencies.
Use those module-local facts to choose materially distinct research objectives rather than creating
generic role-playing tasks. Build a small, reviewable task DAG. Every task must use only module
UUIDs listed in allowed_scope.
Use stable lowercase task_key values. A task_key must match
`^[a-z][a-z0-9_.-]{0,119}$`: use ASCII lowercase letters, digits, `.`, `_`, or `-` only;
never use spaces, Chinese characters, or a colon. Dependencies may only reference another task_key
in the same proposal and must be acyclic. Every task belongs to exactly one module because
evidence is
admitted per module; a complex module may have multiple tasks only when their objectives differ
materially. Honor research_depth and planning_guidance from the user context: deep research can
use a layered DAG for constraints, compatibility, trade-offs, and risks, while focused research
must remain minimal. Use `must` only for work necessary to answer the objective. Stop conditions
must say when the task has enough evidence or should report a gap; they must not request
unbounded search. For a deep plan, every allowed_scope module marked
`deep_decomposition_required=true` must have at least two tasks with materially distinct
objectives; split evidence/constraint establishment from candidate, trade-off, or compatibility
analysis when appropriate. Treat the approved usage, budget, skill, resources, hard constraints, and
unknowns as design inputs: explicitly reflect material trade-offs or deferred questions rather
than silently replacing them with generic module research. When budget, preferences, resources,
or usage context materially affect a choice, include at least one `candidate` output so the
reviewer can inspect the resulting trade-off and record the specific trade-off in
`decision_notes`. When unknowns exist, record them in
`deferred_questions` or `risk_notes`; do not silently discard them. When two selected modules
have an approved dependency, deep research must make the dependent module's compatibility task
depend on a task for that prerequisite module. Do not add an edge when the prerequisite module
was not selected. For deep tasks, add one to four concise `research_lenses` that describe
materially different investigation angles (for example: specifications, integration failure modes,
or alternatives); do not restate the task title or turn them into source claims. If user context
includes a non-null `source_strategy_preference`, set `source_strategy` to exactly that value;
it is a user-approved research constraint, not a suggestion. If it is null, choose the source
strategy that best fits the approved scope and make the choice visible for review. If user context
includes `strategy_review_feedback` or
`strategy_schema_feedback`, it is a server-generated correction request: keep the allowed scope
unchanged and revise the complete strategy so every listed issue is reflected in the complete
task DAG. The user will approve the strategy before any research starts.

Field values are closed enums: `priority` is exactly `must` or `should`; every
`expected_outputs` item is exactly one of `evidence`, `candidate`, `compatibility`, or
`constraint` (use one to four items, never translated labels or free text); `source_strategy` is
exactly `primary`, `independent`, `official`, or `mixed`. A valid minimal task fragment is
`{"task_key":"flight_safety.constraints","priority":"must","expected_outputs":["evidence","constraint"]}`.

If `strategy_generation_mode` is `global_skeleton`, return exactly one `must` task per allowed
module. That task is a concise module planning seed: its objective and lenses identify what a
later module-local planner should investigate. Do not decompose modules, add cross-module task
dependencies, or try to describe the final task DAG in this mode. If it is `module_detail`, the
allowed scope contains one module: expand its supplied `global_strategy_seed` into a complete
local task DAG. Do not refer to task keys outside the local module; the server adds approved
cross-module handoffs after validation."""

RESEARCH_STRATEGY_RETRY_PROMPT = """Return one complete JSON object only matching
ResearchStrategyProposal.  Include schema_version, summary, decision_notes, scope_module_ids,
tasks, deferred_questions, risk_notes, and source_strategy.  Every task needs task_key, title,
objective, module_ids, depends_on_task_keys, priority, expected_outputs, stop_conditions,
research_lenses. `task_key` uses only ASCII lowercase letters, digits, `.`, `_`, and `-`, begins
with a lowercase letter, and cannot contain spaces, Chinese text, or `:`. `priority` is exactly
`must` or `should`; `expected_outputs` is one to four exact values from `evidence`, `candidate`,
`compatibility`, `constraint`; `source_strategy` is exactly `primary`, `independent`, `official`,
or `mixed`. Use only provided module UUIDs, no unknown fields, no Markdown, and no explanation.
Apply every
strategy_review_feedback and strategy_schema_feedback issue by correcting the complete task DAG,
including dependency edges when an issue names a missing dependency handoff."""


class ResearchStrategyOutputValidationError(ValueError):
    """Safe, structural diagnostics for an untrusted strategy response.

    Raw model text stays out of exceptions, logs, and API responses.  The
    diagnostics only name a validation path and stable error code so callers
    can distinguish an output-contract failure from a later planning review.
    """

    def __init__(self, diagnostics: tuple[str, ...]) -> None:
        super().__init__("research strategy planner returned invalid output")
        self.diagnostics = diagnostics


class ResearchStrategyPlanner(Protocol):
    """A bounded strategy-output boundary, independent of the model SDK."""

    async def ainvoke(self, planning_context: str) -> ResearchStrategyProposal: ...


def research_strategy_output_json_schema() -> dict[str, object]:
    """Return the provider-facing structural contract for strategy output.

    This deliberately stays smaller than Pydantic's generated schema.  The
    provider enforces JSON shape and closed enums; Pydantic remains the local
    authority for UUID parsing, field bounds, task-key rules, and DAG checks.
    """

    string_list = {"type": "array", "items": {"type": "string"}}
    task_schema: dict[str, object] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "task_key": {"type": "string"},
            "title": {"type": "string"},
            "objective": {"type": "string"},
            "module_ids": {"type": "array", "items": {"type": "string"}},
            "depends_on_task_keys": string_list,
            "priority": {"type": "string", "enum": ["must", "should"]},
            "expected_outputs": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": ["evidence", "candidate", "compatibility", "constraint"],
                },
            },
            "stop_conditions": string_list,
            "research_lenses": string_list,
        },
        "required": [
            "task_key",
            "title",
            "objective",
            "module_ids",
            "depends_on_task_keys",
            "priority",
            "expected_outputs",
            "stop_conditions",
            "research_lenses",
        ],
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "schema_version": {
                "type": "string",
                "enum": ["research-strategy-v1"],
            },
            "summary": {"type": "string"},
            "decision_notes": string_list,
            "scope_module_ids": {"type": "array", "items": {"type": "string"}},
            "tasks": {"type": "array", "items": task_schema},
            "deferred_questions": string_list,
            "risk_notes": string_list,
            "source_strategy": {
                "type": "string",
                "enum": ["primary", "independent", "official", "mixed"],
            },
        },
        "required": [
            "schema_version",
            "summary",
            "decision_notes",
            "scope_module_ids",
            "tasks",
            "deferred_questions",
            "risk_notes",
            "source_strategy",
        ],
    }


class JsonModeResearchStrategyPlanner:
    """One bounded JSON-mode call; semantic authorization remains server-side."""

    def __init__(self, model: BaseChatModel) -> None:
        self._model = model

    async def ainvoke(self, planning_context: str) -> ResearchStrategyProposal:
        last_error: ValueError | None = None
        request_context = planning_context
        for attempt in range(2):
            response = await self._model.bind(response_format={"type": "json_object"}).ainvoke(
                [
                    {
                        "role": "system",
                        "content": (
                            RESEARCH_STRATEGY_SYSTEM_PROMPT
                            if attempt == 0
                            else RESEARCH_STRATEGY_RETRY_PROMPT
                        ),
                    },
                    {"role": "user", "content": request_context},
                ]
            )
            try:
                metadata_error = self._validate_response_metadata(response)
                if metadata_error is not None:
                    raise metadata_error
                content = cast(BaseMessage, response).content
                if not isinstance(content, str) or not content.strip():
                    raise ValueError("research strategy planner returned empty content")
                return ResearchStrategyProposal.model_validate_json(content)
            except ValueError as exc:
                last_error = exc
                if attempt == 1:
                    raise ResearchStrategyOutputValidationError(
                        _schema_diagnostics(exc)
                    ) from exc
                request_context = _with_schema_feedback(
                    planning_context=planning_context,
                    error=exc,
                )
        raise last_error or ValueError("research strategy planning failed")

    @staticmethod
    def _validate_response_metadata(response: Any) -> ValueError | None:
        metadata = getattr(response, "response_metadata", {})
        if isinstance(metadata, dict) and metadata.get("finish_reason") == "length":
            return ValueError("research strategy planner response was truncated")
        return None


class ResponsesJsonSchemaResearchStrategyPlanner:
    """Responses API planner with provider-enforced JSON Schema output.

    One invocation remains one bounded physical provider request.  Advanced
    semantic checks remain in the planning service, which may issue its own
    explicit review repair request with server-generated feedback.
    """

    def __init__(
        self,
        *,
        client: Any,
        model: str,
        timeout_seconds: float,
        # DeepSeek counts reasoning and visible JSON together.  A deep
        # hierarchical plan can legitimately need more than 4k before it
        # reaches the final schema-constrained message.
        max_output_tokens: int = 12_000,
    ) -> None:
        self._client = client
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._max_output_tokens = max_output_tokens

    async def ainvoke(self, planning_context: str) -> ResearchStrategyProposal:
        response = await self._client.responses.create(
            model=self._model,
            instructions=RESEARCH_STRATEGY_SYSTEM_PROMPT,
            input=planning_context,
            reasoning={"effort": "high"},
            max_output_tokens=self._max_output_tokens,
            text={
                "format": {
                    "type": "json_schema",
                    "name": "research_strategy_proposal",
                    "schema": research_strategy_output_json_schema(),
                }
            },
            store=False,
            timeout=self._timeout_seconds,
        )
        if getattr(response, "status", "completed") != "completed":
            raise ResearchStrategyOutputValidationError(("schema:$:response_incomplete",))
        content = getattr(response, "output_text", None)
        if not isinstance(content, str) or not content.strip():
            raise ResearchStrategyOutputValidationError(("schema:$:empty_response",))
        try:
            return ResearchStrategyProposal.model_validate_json(content)
        except ValueError as exc:
            raise ResearchStrategyOutputValidationError(_schema_diagnostics(exc)) from exc


def _with_schema_feedback(*, planning_context: str, error: ValueError) -> str:
    """Attach stable structural diagnostics to a repair request, not raw output."""

    try:
        context = json.loads(planning_context)
    except json.JSONDecodeError:
        return planning_context
    if not isinstance(context, dict):
        return planning_context
    return json.dumps(
        {**context, "strategy_schema_feedback": _schema_feedback(error)},
        ensure_ascii=False,
        sort_keys=True,
    )


def _schema_feedback(error: ValueError) -> list[dict[str, str]]:
    """Expose only validation locations and codes, never rejected model content."""

    errors = getattr(error, "errors", None)
    if not callable(errors):
        return [{"path": "$", "code": "invalid_response"}]
    try:
        details = errors()
    except Exception:
        return [{"path": "$", "code": "invalid_response"}]
    feedback: list[dict[str, str]] = []
    for detail in details[:16]:
        if not isinstance(detail, dict):
            continue
        location = detail.get("loc", ())
        path = "$"
        if isinstance(location, (tuple, list)):
            path += "".join(
                f"[{item}]" if isinstance(item, int) else f".{item}"
                for item in location
            )
        feedback.append(
            {
                "path": path,
                "code": str(detail.get("type") or "invalid_response"),
            }
        )
    return feedback or [{"path": "$", "code": "invalid_response"}]


def _schema_diagnostics(error: ValueError) -> tuple[str, ...]:
    """Encode validation feedback without preserving rejected model content."""

    return tuple(
        f"schema:{item['path']}:{item['code']}" for item in _schema_feedback(error)
    )


__all__ = [
    "JsonModeResearchStrategyPlanner",
    "ResearchStrategyPlanner",
    "ResearchStrategyOutputValidationError",
    "ResponsesJsonSchemaResearchStrategyPlanner",
    "research_strategy_output_json_schema",
]

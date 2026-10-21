from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage

from aidison.application.single_task_research import (
    SingleTaskResearchPayload,
    _research_result_status,
)
from aidison.providers.model_gateway import (
    ModelInvocationRequest,
    ModelTarget,
    ProviderFailure,
    ProviderFailureClass,
    RetryPolicy,
)
from aidison.research.langgraph_contracts import ResearchResultStatus, TaskEnvelope
from aidison.research.researcher import (
    GatewayJsonModeSingleTaskResearcher,
    JsonModeResearchProviderAdapter,
    JsonModeSingleTaskResearcher,
    _provider_usage_tokens,
    _research_messages,
)
from aidison.research.source_collection import CollectedResearchSource
from aidison.research.source_observations import SourceIdentity, SourceKind


def _task(*, coverage_keys: tuple[str, ...]) -> TaskEnvelope:
    return TaskEnvelope(
        run_id=uuid4(),
        task_key="research-status",
        basis_hash="a" * 64,
        plan_revision=1,
        capability="research",
        input_refs=(),
        dependency_task_ids=(),
        coverage_keys=coverage_keys,
        allowed_tool_ids=(),
        budget_ref="budget://test",
        idempotency_key="research-status-test",
    )


def test_research_result_status_is_partial_until_every_authorized_coverage_key_has_evidence(
) -> None:
    task = _task(coverage_keys=("power.current", "power.voltage"))

    assert _research_result_status(
        task=task,
        admitted_coverage_keys=(),
        unresolved_refs=(),
    ) is ResearchResultStatus.PARTIAL
    assert _research_result_status(
        task=task,
        admitted_coverage_keys=("power.current",),
        unresolved_refs=(),
    ) is ResearchResultStatus.PARTIAL
    assert _research_result_status(
        task=task,
        admitted_coverage_keys=("power.current", "power.voltage"),
        unresolved_refs=(),
    ) is ResearchResultStatus.SUCCEEDED
    assert _research_result_status(
        task=task,
        admitted_coverage_keys=("power.current", "power.voltage"),
        unresolved_refs=("artifact+sha256://" + "a" * 64 + "/" + str(uuid4()),),
    ) is ResearchResultStatus.PARTIAL


@pytest.mark.parametrize(
    "recommended_option,alternatives",
    (
        ("Flight controller A", ("Flight controller A",)),
        ("Flight Controller A", ("  flight   controller a  ",)),
        ("Flight controller A", ("Flight controller B", "FLIGHT CONTROLLER B")),
        (" ", ("Flight controller B",)),
    ),
)
def test_research_payload_rejects_duplicate_or_blank_user_choice(
    recommended_option: str,
    alternatives: tuple[str, ...],
) -> None:
    with pytest.raises(ValueError, match="distinct non-blank options"):
        SingleTaskResearchPayload(
            question="Compare flight controllers",
            summary="Source-backed comparison.",
            recommended_option=recommended_option,
            alternatives=alternatives,
        )


def test_research_payload_keeps_meaningfully_distinct_options() -> None:
    payload = SingleTaskResearchPayload(
        question="Compare flight controllers",
        summary="Source-backed comparison.",
        recommended_option="Flight controller A",
        alternatives=("Flight controller B",),
    )

    assert payload.recommended_option == "Flight controller A"


@pytest.mark.parametrize(
    ("message", "expected"),
    (
        (
            SimpleNamespace(
                usage_metadata={"input_tokens": 18, "output_tokens": 7},
                response_metadata={},
            ),
            25,
        ),
        (
            SimpleNamespace(
                response_metadata={"usage": {"prompt_tokens": 18, "completion_tokens": 7}},
                usage_metadata={},
            ),
            25,
        ),
        (SimpleNamespace(usage_metadata={"input_tokens": 18}, response_metadata={}), None),
    ),
)
def test_provider_usage_accepts_complete_provider_reported_components(
    message: object,
    expected: int | None,
) -> None:
    assert _provider_usage_tokens(message) == expected  # type: ignore[arg-type]


def test_research_payload_rejects_duplicate_evidence_assertion() -> None:
    evidence = {
        "coverage_key": "power.current",
        "source_key": "manufacturer-spec",
        "quote_text": "Continuous current: 35A.",
        "claim": "The controller supports 35A continuous current.",
        "subject_identity": "controller-a",
        "predicate": "continuous_current",
        "applicability": "nominal operation",
        "normalization_schema": "current-amperes-v1",
        "normalized_value": "35",
    }

    with pytest.raises(ValueError, match="evidence claims must not repeat"):
        SingleTaskResearchPayload(
            question="Compare controllers",
            summary="Source-backed comparison.",
            recommended_option="Controller A",
            alternatives=("Controller B",),
            evidence_claims=(evidence, evidence),
        )


@pytest.mark.asyncio
async def test_research_prompt_exposes_the_full_evidence_claim_contract() -> None:
    model = MagicMock(spec=BaseChatModel)
    bound = MagicMock()
    bound.ainvoke = AsyncMock(
        return_value=AIMessage(
            content=json.dumps(
                {
                    "question": "q",
                    "summary": "s",
                    "recommended_option": "o",
                    "alternatives": ["a"],
                    "evidence_claims": [],
                }
            )
        )
    )
    model.bind.return_value = bound
    source = CollectedResearchSource(
        key="source-a",
        source=SourceIdentity(
            kind=SourceKind.WEB,
            provider="test",
            canonical_locator="https://example.test/source",
        ),
        normalized_document="document",
        media_type="text/plain",
        representation="normalized-test-v1",
        parser_revision="test-v1",
        observed_at="2026-09-09T00:00:00Z",
        coverage_source_kinds=("evidence",),
    )

    await JsonModeSingleTaskResearcher(model).research(
        question="q",
        input_refs=(),
        evidence_context=(source,),
    )

    messages = bound.ainvoke.await_args.args[0]
    prompt = messages[0]["content"]
    for field in (
        "quote_text",
        "subject_identity",
        "predicate",
        "applicability",
        "normalization_schema",
        "normalized_value",
    ):
        assert f'"{field}"' in prompt
    assert 'field named "quote"' in prompt


def test_bounded_gateway_context_prefers_a_late_query_relevant_source_passage() -> None:
    source = CollectedResearchSource(
        key="source-a",
        source=SourceIdentity(
            kind=SourceKind.WEB,
            provider="test",
            canonical_locator="https://example.test/source",
        ),
        normalized_document=(
            "Company history and generic product marketing.\n\n"
            "The motor current limit is 30A and requires a 4S battery."
        ),
        media_type="text/plain",
        representation="normalized-test-v1",
        parser_revision="test-v1",
        observed_at="2026-09-09T00:00:00Z",
        coverage_source_kinds=("evidence",),
    )

    messages = _research_messages(
        system_prompt="policy",
        question="Compare motor current limits for a 4S battery.",
        input_refs=(),
        steering_instructions=(),
        evidence_context=(source,),
        max_source_characters=100,
    )

    assert "motor current limit" in messages[1]["content"]
    assert "Company history" not in messages[1]["content"]


def test_gateway_context_shrinks_source_excerpt_before_rejecting_a_valid_task() -> None:
    source = CollectedResearchSource(
        key="source-a",
        source=SourceIdentity(
            kind=SourceKind.WEB,
            provider="test",
            canonical_locator="https://example.test/source",
        ),
        normalized_document=(
            "Generic catalog material. " * 500
            + "The controller supports a 4S battery and a continuous current of 30A."
        ),
        media_type="text/plain",
        representation="normalized-test-v1",
        parser_revision="test-v1",
        observed_at="2026-09-09T00:00:00Z",
        coverage_source_kinds=("evidence",),
    )
    target = ModelTarget(
        provider="fixture",
        model="fixture-model",
        revision="v1",
        credential_pool_id="fixture-pool",
        quota_group="research",
        capabilities=("structured_output",),
    )
    researcher = GatewayJsonModeSingleTaskResearcher(
        gateway=MagicMock(),
        session_factory=MagicMock(),
        artifact_root=Path("/tmp/aidison-test-artifacts"),
        target=target,
        reserved_tokens_per_call=5_000,
        max_output_tokens=1_000,
    )
    task = _task(coverage_keys=("power.current",))

    assembly, output_tokens = researcher._assemble_budgeted_context(
        run=SimpleNamespace(basis_hash="a" * 64),
        task=task,
        question="Compare controller constraints.\n" + "requirement detail " * 100,
        input_refs=(),
        steering_instructions=(),
        evidence_context=(source,),
        reserved_tokens=5_000,
    )

    source_item = next(
        item for item in assembly.manifest.items if item.source_ref == "source://source-a"
    )
    assert source_item.transform == "excerpt"
    assert source_item.included_token_estimate < source_item.original_token_estimate
    assert output_tokens == 1_000


@pytest.mark.asyncio
async def test_gateway_adapter_enforces_deadline_before_hung_model_holds_lease(
    tmp_path,
) -> None:
    """A provider coroutine that never returns becomes a classified timeout."""
    model = MagicMock(spec=BaseChatModel)
    bound = MagicMock()

    async def never_returns(*_: object) -> object:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    bound.ainvoke = AsyncMock(side_effect=never_returns)
    model.bind.return_value = bound
    target = ModelTarget(
        provider="fixture",
        model="fixture-model",
        revision="v1",
        credential_pool_id="fixture-pool",
        quota_group="research",
        capabilities=("structured_output",),
    )
    request = ModelInvocationRequest(
        logical_invocation_id=uuid4(),
        run_id=uuid4(),
        project_id=uuid4(),
        task_id=uuid4(),
        basis_hash="a" * 64,
        prompt_ref="artifact://prompt",
        required_capabilities=("structured_output",),
        targets=(target,),
        retry_policy=RetryPolicy(max_attempts_per_target=1, base_backoff_seconds=0),
        deadline=datetime.now(UTC) + timedelta(milliseconds=100),
        provider_payload={
            "messages": [{"role": "user", "content": "bounded question"}],
            "max_output_tokens": 256,
        },
    )
    adapter = JsonModeResearchProviderAdapter(
        model=model,
        session_factory=MagicMock(),
        artifact_root=tmp_path,
    )

    with pytest.raises(ProviderFailure) as raised:
        await adapter.invoke(request=request, target=target)

    assert raised.value.failure_class is ProviderFailureClass.TIMEOUT_AFTER_DISPATCH

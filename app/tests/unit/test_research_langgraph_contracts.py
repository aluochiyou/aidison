from __future__ import annotations

from hashlib import sha256
from itertools import permutations
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from aidison.application.single_task_research import _evidence_sufficiency_instruction
from aidison.research.langgraph_contracts import (
    AdmissionDisposition,
    AdmissionRecord,
    AdmittedResultRef,
    ExecutionGrant,
    ResearchResultStatus,
    ResultEnvelope,
    TaskEnvelope,
    derive_reducer_collisions,
    merge_admitted_result_refs,
)
from aidison.research.strategy import compile_research_execution_policy


def _hash(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def _ref(*, result_id: UUID | None = None, value: str = "one") -> AdmittedResultRef:
    return AdmittedResultRef(
        result_id=result_id or uuid4(),
        manifest_hash=_hash(value),
        admitted_ref=f"artifact://admitted/{value}",
    )


def test_ref_reducer_is_order_independent_idempotent_and_collision_visible() -> None:
    first = _ref(value="first")
    second = _ref(value="second")
    competing = _ref(result_id=first.result_id, value="competing")
    expected = merge_admitted_result_refs((first, second), (competing,))

    for ordered in permutations((first, second, competing)):
        assert merge_admitted_result_refs((), ordered) == expected
    assert merge_admitted_result_refs(expected, expected) == expected
    assert merge_admitted_result_refs(
        merge_admitted_result_refs((first,), (second,)),
        (competing,),
    ) == expected

    collisions = derive_reducer_collisions(expected)
    assert len(collisions) == 1
    assert collisions[0].result_id == first.result_id
    assert {variant.manifest_hash for variant in collisions[0].variants} == {
        first.manifest_hash,
        competing.manifest_hash,
    }


def test_task_and_execution_authority_are_separate() -> None:
    task = TaskEnvelope(
        run_id=uuid4(),
        task_key="single-research",
        basis_hash=_hash("basis"),
        plan_revision=1,
        capability="research",
        input_refs=("artifact://input/1",),
        dependency_task_ids=(),
        coverage_keys=("research.answer",),
        allowed_tool_ids=(),
        budget_ref="budget://run/1",
        idempotency_key="task-1",
    )
    grant = ExecutionGrant(
        task_id=task.id,
        attempt_id=uuid4(),
        generation=1,
        lease_token=uuid4(),
        deadline_ref="deadline://run/1",
        idempotency_prefix="run-1/task-1",
    )

    assert grant.task_id == task.id
    assert "generation" not in TaskEnvelope.model_fields
    assert "lease_token" not in TaskEnvelope.model_fields


def test_deep_task_prompt_exposes_frozen_two_source_evidence_bar() -> None:
    task = TaskEnvelope(
        run_id=uuid4(),
        task_key="deep-research",
        basis_hash=_hash("basis"),
        plan_revision=1,
        capability="research",
        input_refs=(),
        dependency_task_ids=(),
        coverage_keys=("power.acceptance.01",),
        allowed_tool_ids=(),
        budget_ref="budget://run/1",
        idempotency_key="deep-task-1",
        collection_policy=compile_research_execution_policy(
            research_depth="deep"
        ).collection,
    )

    instruction = _evidence_sufficiency_instruction(task=task)

    assert "two distinct source_key" in instruction
    assert "never duplicate or invent" in instruction


def test_result_and_admission_do_not_conflate_producer_and_control_verdict() -> None:
    result = ResultEnvelope(
        run_id=uuid4(),
        task_id=uuid4(),
        basis_hash=_hash("basis"),
        producer_attempt_id=uuid4(),
        producer_generation=1,
        producer_profile_ref="profile://research/1",
        status=ResearchResultStatus.SUCCEEDED,
        artifact_ref="artifact://result/1",
        manifest_hash=_hash("result"),
        evidence_refs=(),
        coverage_observation_refs=(),
        unresolved_refs=(),
    )
    accepted = AdmissionRecord(
        run_id=result.run_id,
        result_id=result.id,
        result_manifest_hash=result.manifest_hash,
        disposition=AdmissionDisposition.ACCEPTED,
        reason_codes=("runtime_fenced", "schema_valid"),
        admitted_ref="admitted://result/1",
    )
    assert accepted.result_id == result.id

    with pytest.raises(ValidationError, match="admitted_ref"):
        AdmissionRecord(
            run_id=result.run_id,
            result_id=result.id,
            result_manifest_hash=result.manifest_hash,
            disposition=AdmissionDisposition.ACCEPTED,
            reason_codes=("runtime_fenced",),
        )

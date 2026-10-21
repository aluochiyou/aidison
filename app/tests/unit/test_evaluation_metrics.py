from __future__ import annotations

import asyncio
from hashlib import sha256
from uuid import uuid4

from aidison.evaluation.contracts import MetricStatus
from aidison.evaluation.metrics import (
    FieldRule,
    StructuredSchemaSpec,
    check_cost_latency_envelope,
    check_end_to_end_boundary,
    check_evidence_completeness,
    check_red_action_blocked,
    check_replay_determinism,
    check_result_admission,
    check_structured_contract,
)
from aidison.research.langgraph_contracts import (
    AdmissionDisposition,
    AdmissionRecord,
    ResearchResultStatus,
    ResultEnvelope,
)
from aidison.runtime.contracts import (
    BudgetOperationKind,
    InvocationRecording,
    InvocationRecordingStatus,
)

_BASIS = sha256(b"basis").hexdigest()


def _result(**changes: object) -> ResultEnvelope:
    payload: dict[str, object] = {
        "run_id": uuid4(),
        "task_id": uuid4(),
        "basis_hash": _BASIS,
        "producer_attempt_id": uuid4(),
        "producer_generation": 1,
        "producer_profile_ref": "profile://research/1",
        "status": ResearchResultStatus.SUCCEEDED,
        "artifact_ref": "artifact+sha256://result/one",
        "manifest_hash": sha256(b"manifest").hexdigest(),
        "evidence_refs": ("artifact+sha256://evidence/one",),
        "coverage_observation_refs": (),
        "unresolved_refs": (),
    }
    payload.update(changes)
    return ResultEnvelope.model_validate(payload)


def _recording(**changes: object) -> InvocationRecording:
    payload: dict[str, object] = {
        "project_id": uuid4(),
        "agent_run_id": uuid4(),
        "producer_attempt_id": uuid4(),
        "basis_hash": _BASIS,
        "idempotency_key": "job-1:github:search_code:stable",
        "request_hash": sha256(b"request").hexdigest(),
        "kind": BudgetOperationKind.TOOL,
        "provider": "github",
        "operation_name": "search_code",
        "status": InvocationRecordingStatus.SUCCEEDED,
        "response_artifact_ref": "artifact+sha256://" + "c" * 64 + "/" + str(uuid4()),
    }
    payload.update(changes)
    return InvocationRecording.model_validate(payload)


def test_structured_contract_passes_conforming_payload() -> None:
    schema = StructuredSchemaSpec(
        fields={
            "name": FieldRule(required=True, type="str"),
            "confidence": FieldRule(required=True, type="float"),
            "status": FieldRule(required=True, type="str", enum=("ok", "fail")),
        }
    )
    metric = check_structured_contract(
        payload={"name": "fc", "confidence": 0.8, "status": "ok"},
        schema=schema,
    )
    assert metric.status is MetricStatus.PASS


def test_structured_contract_fails_missing_required_and_wrong_type() -> None:
    schema = StructuredSchemaSpec(
        fields={
            "name": FieldRule(required=True, type="str"),
            "confidence": FieldRule(required=True, type="float"),
            "status": FieldRule(required=True, type="str", enum=("ok", "fail")),
        }
    )
    metric = check_structured_contract(
        payload={"confidence": "high", "status": "maybe"},
        schema=schema,
    )
    assert metric.status is MetricStatus.FAIL
    assert "missing required field: name" in metric.detail
    assert "expected float" in metric.detail
    assert "not in the allowed set" in metric.detail


def test_structured_contract_fails_pattern_violation() -> None:
    schema = StructuredSchemaSpec(
        fields={"task_key": FieldRule(required=True, type="str", pattern=r"^[a-z][a-z0-9_]{0,31}$")}
    )
    metric = check_structured_contract(payload={"task_key": "Bad Key!"}, schema=schema)
    assert metric.status is MetricStatus.FAIL
    assert "does not match pattern" in metric.detail


def test_evidence_completeness_passes_when_all_refs_resolve() -> None:
    metric = check_evidence_completeness(
        cited_refs=("ref/1", "ref/2"),
        available_refs=("ref/1", "ref/2", "ref/3"),
        min_evidence=1,
    )
    assert metric.status is MetricStatus.PASS


def test_evidence_completeness_fails_dangling_below_minimum_and_duplicate() -> None:
    metric = check_evidence_completeness(
        cited_refs=("ref/1", "ref/1", "ref/9"),
        available_refs=("ref/1",),
        min_evidence=4,
    )
    assert metric.status is MetricStatus.FAIL
    assert "dangling evidence refs" in metric.detail
    assert "duplicate evidence refs" in metric.detail
    assert "below minimum" in metric.detail


def test_result_admission_passes_for_admitted_result() -> None:
    result = _result()
    admission = AdmissionRecord(
        run_id=result.run_id,
        result_id=result.id,
        result_manifest_hash=result.manifest_hash,
        disposition=AdmissionDisposition.ACCEPTED,
        reason_codes=("runtime_fenced",),
        admitted_ref="admitted://result/one",
    )
    metric = check_result_admission(result=result, admission=admission)
    assert metric.status is MetricStatus.PASS
    assert metric.payload["admission_disposition"] == "accepted"


def test_result_admission_fails_for_mismatched_control_verdict() -> None:
    result = _result(status=ResearchResultStatus.FAILED, failure_ref="failure://timeout")
    admission = AdmissionRecord(
        run_id=result.run_id,
        result_id=result.id,
        result_manifest_hash=result.manifest_hash,
        disposition=AdmissionDisposition.ACCEPTED,
        reason_codes=("runtime_fenced",),
        admitted_ref="admitted://result/one",
    )
    metric = check_result_admission(result=result, admission=admission)
    assert metric.status is MetricStatus.FAIL
    assert "failed ResultEnvelope cannot be accepted" in metric.detail


def test_red_action_blocked_passes_when_gate_denies_ungranted_effect() -> None:
    metric = check_red_action_blocked(
        allowed_tool_classes=("web_search",),
        allowed_effects=("discovery",),
        attempted_tool_class="web_search",
        required_effects=("discovery", "write"),
    )
    assert metric.status is MetricStatus.PASS


def test_red_action_blocked_fails_when_gate_would_allow_ungranted_effect() -> None:
    metric = check_red_action_blocked(
        allowed_tool_classes=("web_search",),
        allowed_effects=("discovery", "write"),
        attempted_tool_class="web_search",
        required_effects=("discovery", "write"),
    )
    assert metric.status is MetricStatus.FAIL
    assert "NOT blocked" in metric.detail


def test_end_to_end_boundary_requires_all_steps_and_rejects_unauthorized_mutation() -> None:
    passing = check_end_to_end_boundary(
        required_steps=("requirements_approved", "decision_request_created"),
        completed_steps=("requirements_approved", "decision_request_created"),
        prohibited_steps=("canonical_project_mutated_without_user_decision",),
    )
    failing = check_end_to_end_boundary(
        required_steps=("requirements_approved", "decision_request_created"),
        completed_steps=(
            "requirements_approved",
            "canonical_project_mutated_without_user_decision",
        ),
        prohibited_steps=("canonical_project_mutated_without_user_decision",),
    )
    assert passing.status is MetricStatus.PASS
    assert failing.status is MetricStatus.FAIL
    assert "missing required" in failing.detail
    assert "prohibited" in failing.detail


def test_cost_latency_envelope_rejects_over_budget_and_negative_observations() -> None:
    passing = check_cost_latency_envelope(
        token_usage=20,
        token_budget=20,
        tool_calls=1,
        tool_call_budget=1,
        latency_seconds=1.0,
        latency_budget_seconds=1.0,
    )
    failing = check_cost_latency_envelope(
        token_usage=-1,
        token_budget=20,
        tool_calls=2,
        tool_call_budget=1,
        latency_seconds=1.1,
        latency_budget_seconds=1.0,
    )
    assert passing.status is MetricStatus.PASS
    assert failing.status is MetricStatus.FAIL
    assert "negative" in failing.detail
    assert "exceeds budget" in failing.detail


def test_replay_determinism_serves_recorded_artifact_without_external_call() -> None:
    recording = _recording()
    metric = asyncio.run(check_replay_determinism(recording=recording))
    assert metric.status is MetricStatus.PASS
    assert metric.payload["idempotency_key"] == recording.idempotency_key

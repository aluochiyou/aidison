from __future__ import annotations

import asyncio
from hashlib import sha256
from uuid import uuid4

from aidison.evaluation.contracts import MetricStatus
from aidison.evaluation.metrics import (
    FieldRule,
    StructuredSchemaSpec,
    check_evidence_completeness,
    check_red_action_blocked,
    check_replay_determinism,
    check_result_admission,
    check_structured_contract,
)
from aidison.runtime.contracts import (
    BudgetOperationKind,
    FailureClass,
    HandoffResultStatus,
    InvocationRecording,
    InvocationRecordingStatus,
    ResultEnvelope,
    ResultVerificationPolicy,
)

_BASIS = sha256(b"basis").hexdigest()


def _result(**changes: object) -> ResultEnvelope:
    payload: dict[str, object] = {
        "handoff_id": uuid4(),
        "basis_hash": _BASIS,
        "status": HandoffResultStatus.SUCCEEDED,
        "result_ref": "artifact+sha256://result/one",
        "evidence_refs": ("artifact+sha256://evidence/one",),
        "artifact_refs": ("artifact+sha256://proposal/one",),
        "confidence": 0.9,
        "schema_ref": "aidison://schemas/research-proposal/v2",
    }
    payload.update(changes)
    return ResultEnvelope.model_validate(payload)


def _recording(**changes: object) -> InvocationRecording:
    payload: dict[str, object] = {
        "project_id": uuid4(),
        "job_id": uuid4(),
        "attempt_id": uuid4(),
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
    policy = ResultVerificationPolicy(
        accepted_schema_refs=("aidison://schemas/research-proposal/v2",),
        min_evidence_refs=1,
        require_artifact_refs=True,
    )
    metric = check_result_admission(result=result, policy=policy)
    assert metric.status is MetricStatus.PASS
    assert metric.payload["admission_status"] == "admitted"


def test_result_admission_quarantines_rejected_result_coherently() -> None:
    result = _result(
        status=HandoffResultStatus.FAILED,
        confidence=0.0,
        failure_class=FailureClass.TIMEOUT,
    )
    metric = check_result_admission(result=result, policy=ResultVerificationPolicy())
    assert metric.status is MetricStatus.PASS
    assert metric.payload["admission_status"] == "quarantined"
    assert metric.payload["verification_status"] == "rejected"


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


def test_replay_determinism_serves_recorded_artifact_without_external_call() -> None:
    recording = _recording()
    metric = asyncio.run(check_replay_determinism(recording=recording))
    assert metric.status is MetricStatus.PASS
    assert metric.payload["idempotency_key"] == recording.idempotency_key

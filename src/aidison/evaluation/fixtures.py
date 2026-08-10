"""Deterministic, self-contained evaluation fixture registry.

Every fixture lives inline as JSON-safe inputs; running the suite never reads
the filesystem, a database, a model or the network.  The registry is the green
regression baseline — negative fixtures are built programmatically by tests.
"""

from __future__ import annotations

from aidison.evaluation.contracts import EvaluationCase

_BASIS_HASH = "a" * 64
_REQUEST_HASH = "b" * 64
_SHA256 = "c" * 64
_EVIDENCE_ONE = f"artifact+sha256://{_SHA256}/00000000-0000-0000-0000-000000000001"
_EVIDENCE_TWO = f"artifact+sha256://{_SHA256}/00000000-0000-0000-0000-000000000002"

FIXTURE_CASES: tuple[EvaluationCase, ...] = (
    EvaluationCase(
        key="structured-contract-complete",
        title="Structured output conforms to the declared contract",
        description=(
            "A typed research-proposal payload must declare every required field "
            "with the expected type, enum membership and lexical pattern."
        ),
        metric_id="structured_contract",
        inputs={
            "payload": {
                "task_key": "research/quadrotor/components",
                "objective": "Compare three flight controller options",
                "output_schema_ref": "aidison://schemas/research-proposal/v2",
                "confidence": 0.9,
                "status": "succeeded",
            },
            "schema": {
                "fields": {
                    "task_key": {
                        "required": True,
                        "type": "str",
                        "pattern": "^[a-z][a-z0-9/._-]{0,119}$",
                    },
                    "objective": {"required": True, "type": "str"},
                    "output_schema_ref": {"required": True, "type": "str"},
                    "confidence": {"required": True, "type": "float"},
                    "status": {
                        "required": True,
                        "type": "str",
                        "enum": ["succeeded", "failed", "cancelled"],
                    },
                }
            },
        },
    ),
    EvaluationCase(
        key="evidence-refs-resolved",
        title="Every cited evidence reference resolves",
        description=(
            "A result that cites two evidence artifacts must resolve them against "
            "the available provenance set without dangling or duplicate refs."
        ),
        metric_id="evidence_completeness",
        inputs={
            "cited_refs": [_EVIDENCE_ONE, _EVIDENCE_TWO],
            "available_refs": [_EVIDENCE_ONE, _EVIDENCE_TWO, "artifact+sha256://e" * 8],
            "min_evidence": 1,
        },
    ),
    EvaluationCase(
        key="result-admission-coherent",
        title="Runtime admission produces a coherent receipt for a valid result",
        description=(
            "A succeeded result that matches the policy schema with sufficient "
            "evidence must admit, and the ResultAdmission invariant must hold."
        ),
        metric_id="result_admission",
        inputs={
            "result": {
                "handoff_id": "00000000-0000-0000-0000-000000000010",
                "basis_hash": _BASIS_HASH,
                "status": "succeeded",
                "result_ref": f"artifact+sha256://{_SHA256}/00000000-0000-0000-0000-000000000011",
                "evidence_refs": [_EVIDENCE_ONE, _EVIDENCE_TWO],
                "artifact_refs": [f"artifact+sha256://{_SHA256}/00000000-0000-0000-0000-000000000012"],
                "confidence": 0.9,
                "schema_ref": "aidison://schemas/research-proposal/v2",
            },
            "policy": {
                "policy_id": "research-result-v1",
                "accepted_schema_refs": ["aidison://schemas/research-proposal/v2"],
                "min_evidence_refs": 1,
                "require_artifact_refs": True,
            },
        },
    ),
    EvaluationCase(
        key="replay-dispatch-once",
        title="Recorded invocation replays without a second external dispatch",
        description=(
            "A succeeded InvocationRecording bound to a job-stable idempotency key "
            "must be served from the ledger instead of invoking the callable again."
        ),
        metric_id="replay_determinism",
        inputs={
            "recording": {
                "project_id": "00000000-0000-0000-0000-000000000020",
                "job_id": "00000000-0000-0000-0000-000000000021",
                "attempt_id": "00000000-0000-0000-0000-000000000022",
                "basis_hash": _BASIS_HASH,
                "idempotency_key": "job-1:github:search_code:stable",
                "request_hash": _REQUEST_HASH,
                "kind": "tool",
                "provider": "github",
                "operation_name": "search_code",
                "status": "succeeded",
                "response_artifact_ref": (
                    f"artifact+sha256://{_SHA256}/00000000-0000-0000-0000-000000000023"
                ),
            }
        },
    ),
    EvaluationCase(
        key="red-action-blocked",
        title="Capability gate blocks an ungranted effect",
        description=(
            "A tool that is allowed as a class must still fail closed when the "
            "attempted effect (write) is outside the profile's granted effects."
        ),
        metric_id="red_action",
        inputs={
            "allowed_tool_classes": ["web_search"],
            "allowed_effects": ["discovery"],
            "attempted_tool_class": "web_search",
            "required_effects": ["discovery", "write"],
        },
    ),
)

FIXTURE_CASES_BY_KEY: dict[str, EvaluationCase] = {
    case.key: case for case in FIXTURE_CASES
}

"""Deterministic, self-contained evaluation fixture registry.

Every fixture lives inline as JSON-safe inputs; running the suite never reads
the filesystem, a database, a model or the network.  The registry is the green
regression baseline — negative fixtures are built programmatically by tests.
"""

from __future__ import annotations

from aidison.evaluation.contracts import (
    EvaluationCase,
    EvaluationLayer,
    FixtureKind,
    FrozenFixtureManifest,
)

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
        layer=EvaluationLayer.CONTRACT,
        fixture_kind=FixtureKind.GOLDEN,
        fixture_revision="r4.05",
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
        layer=EvaluationLayer.CAPABILITY,
        fixture_kind=FixtureKind.GOLDEN,
        fixture_revision="r4.05",
        inputs={
            "cited_refs": [_EVIDENCE_ONE, _EVIDENCE_TWO],
            "available_refs": [_EVIDENCE_ONE, _EVIDENCE_TWO, "artifact+sha256://e" * 8],
            "min_evidence": 1,
        },
    ),
    EvaluationCase(
        key="result-admission-coherent",
        title="Result and Control admission record bind coherently",
        description=(
            "A producer result and its separate Control-side admission record must "
            "bind the same AgentRun, result identity and immutable manifest."
        ),
        metric_id="result_admission",
        layer=EvaluationLayer.CONTRACT,
        fixture_kind=FixtureKind.GOLDEN,
        fixture_revision="r4.05",
        inputs={
            "result": {
                "id": "00000000-0000-0000-0000-000000000010",
                "run_id": "00000000-0000-0000-0000-000000000011",
                "task_id": "00000000-0000-0000-0000-000000000012",
                "basis_hash": _BASIS_HASH,
                "producer_attempt_id": "00000000-0000-0000-0000-000000000013",
                "producer_generation": 1,
                "producer_profile_ref": "profile://research/1",
                "status": "succeeded",
                "artifact_ref": f"artifact+sha256://{_SHA256}/00000000-0000-0000-0000-000000000014",
                "manifest_hash": _SHA256,
                "evidence_refs": [_EVIDENCE_ONE, _EVIDENCE_TWO],
                "coverage_observation_refs": [],
                "unresolved_refs": [],
            },
            "admission": {
                "run_id": "00000000-0000-0000-0000-000000000011",
                "result_id": "00000000-0000-0000-0000-000000000010",
                "result_manifest_hash": _SHA256,
                "disposition": "accepted",
                "reason_codes": ["runtime_fenced", "schema_valid", "artifact_present"],
                "admitted_ref": "admitted://agent-run-results/00000000-0000-0000-0000-000000000010",
            },
        },
    ),
    EvaluationCase(
        key="replay-dispatch-once",
        title="Recorded invocation replays without a second external dispatch",
        description=(
            "A succeeded InvocationRecording bound to an AgentRun-stable idempotency key "
            "must be served from the ledger instead of invoking the callable again."
        ),
        metric_id="replay_determinism",
        layer=EvaluationLayer.TRAJECTORY,
        fixture_kind=FixtureKind.TRACE_REVIEW,
        fixture_revision="r4.05",
        inputs={
            "recording": {
                "project_id": "00000000-0000-0000-0000-000000000020",
                "agent_run_id": "00000000-0000-0000-0000-000000000021",
                "producer_attempt_id": "00000000-0000-0000-0000-000000000022",
                "basis_hash": _BASIS_HASH,
                "idempotency_key": "agent-run-1:github:search_code:stable",
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
        layer=EvaluationLayer.ROBUSTNESS_SECURITY,
        fixture_kind=FixtureKind.FAULT,
        fixture_revision="r4.05",
        inputs={
            "allowed_tool_classes": ["web_search"],
            "allowed_effects": ["discovery"],
            "attempted_tool_class": "web_search",
            "required_effects": ["discovery", "write"],
        },
    ),
    EvaluationCase(
        key="project-decision-boundary",
        title="A complete scenario reaches a decision boundary without bypassing approval",
        description=(
            "The recorded end-to-end scenario must stop at a durable DecisionRequest; "
            "it may not mutate canonical project facts before a user decision."
        ),
        metric_id="end_to_end_boundary",
        layer=EvaluationLayer.END_TO_END,
        fixture_kind=FixtureKind.GOLDEN,
        fixture_revision="r4.05",
        inputs={
            "required_steps": [
                "requirements_approved",
                "execution_plan_approved",
                "run_started",
                "proposal_interrupted",
                "decision_request_created",
            ],
            "completed_steps": [
                "requirements_approved",
                "execution_plan_approved",
                "run_started",
                "proposal_interrupted",
                "decision_request_created",
            ],
            "prohibited_steps": ["canonical_project_mutated_without_user_decision"],
        },
    ),
    EvaluationCase(
        key="cost-latency-envelope",
        title="A bounded research attempt stays inside its cost and latency envelope",
        description=(
            "The recorded unit of work must remain within the explicit token, tool-call "
            "and latency budgets used to protect user cost and wait time."
        ),
        metric_id="cost_latency_envelope",
        layer=EvaluationLayer.COST_LATENCY,
        fixture_kind=FixtureKind.GOLDEN,
        fixture_revision="r4.05",
        inputs={
            "token_usage": 680,
            "token_budget": 800,
            "tool_calls": 2,
            "tool_call_budget": 3,
            "latency_seconds": 4.2,
            "latency_budget_seconds": 6.0,
        },
    ),
    EvaluationCase(
        key="research-quality-projection-deep",
        title="Deep research quality projection preserves coverage and source-diversity limits",
        description=(
            "A frozen deep-research contract is consolidated from admitted typed observations, "
            "then projected through the product-quality view. The offline score records MUST "
            "completion, source-ID and URL-origin minima, rejected-claim explanation, and "
            "compiled deep limits. It deliberately does not claim publisher independence."
        ),
        metric_id="research_quality_projection",
        layer=EvaluationLayer.CAPABILITY,
        fixture_kind=FixtureKind.GOLDEN,
        fixture_revision="r4.07",
        inputs={
            "coverage": {
                "basis_hash": _BASIS_HASH,
                "objective": "Validate power and control modules for a flight platform",
                "keys": [
                    {
                        "key": "control.acceptance.01",
                        "question": "Does the controller meet the safety interface constraint?",
                        "priority": "must",
                        "module_ids": ["control-module"],
                        "required_source_kinds": ["evidence"],
                        "min_distinct_sources": 2,
                        "min_distinct_origins": 2,
                    },
                    {
                        "key": "power.acceptance.01",
                        "question": "Does the power module meet the thrust reserve constraint?",
                        "priority": "must",
                        "module_ids": ["power-module"],
                        "required_source_kinds": ["evidence"],
                        "min_distinct_sources": 2,
                        "min_distinct_origins": 2,
                    },
                ],
            },
            "observations": [
                {
                    "result_id": "00000000-0000-0000-0000-000000000031",
                    "admitted_ref": "admitted://research-quality/control-one",
                    "basis_hash": _BASIS_HASH,
                    "coverage_key": "control.acceptance.01",
                    "status": "answered",
                    "source_kinds": ["evidence"],
                    "source_ids": ["source:controller-datasheet"],
                    "source_origins": ["web-origin:https://manufacturer.example"],
                    "evidence_refs": [_EVIDENCE_ONE],
                    "claim_key": {
                        "subject_identity": "module:control",
                        "predicate": "safety_interface",
                        "applicability": "deep-golden",
                        "normalization_schema": "research-eval-v1",
                    },
                    "value_hash": "d" * 64,
                },
                {
                    "result_id": "00000000-0000-0000-0000-000000000032",
                    "admitted_ref": "admitted://research-quality/control-two",
                    "basis_hash": _BASIS_HASH,
                    "coverage_key": "control.acceptance.01",
                    "status": "answered",
                    "source_kinds": ["evidence"],
                    "source_ids": ["source:controller-test-report"],
                    "source_origins": ["web-origin:https://lab.example"],
                    "evidence_refs": [_EVIDENCE_TWO],
                    "claim_key": {
                        "subject_identity": "module:control",
                        "predicate": "safety_interface",
                        "applicability": "deep-golden",
                        "normalization_schema": "research-eval-v1",
                    },
                    "value_hash": "d" * 64,
                },
                {
                    "result_id": "00000000-0000-0000-0000-000000000033",
                    "admitted_ref": "admitted://research-quality/power-one",
                    "basis_hash": _BASIS_HASH,
                    "coverage_key": "power.acceptance.01",
                    "status": "answered",
                    "source_kinds": ["evidence"],
                    "source_ids": ["source:motor-datasheet"],
                    "source_origins": ["web-origin:https://motor-maker.example"],
                    "evidence_refs": [_EVIDENCE_ONE],
                    "claim_key": {
                        "subject_identity": "module:power",
                        "predicate": "thrust_reserve",
                        "applicability": "deep-golden",
                        "normalization_schema": "research-eval-v1",
                    },
                    "value_hash": "e" * 64,
                },
                {
                    "result_id": "00000000-0000-0000-0000-000000000034",
                    "admitted_ref": "admitted://research-quality/power-two",
                    "basis_hash": _BASIS_HASH,
                    "coverage_key": "power.acceptance.01",
                    "status": "answered",
                    "source_kinds": ["evidence"],
                    "source_ids": ["source:bench-test"],
                    "source_origins": ["web-origin:https://bench-lab.example"],
                    "evidence_refs": [_EVIDENCE_TWO],
                    "claim_key": {
                        "subject_identity": "module:power",
                        "predicate": "thrust_reserve",
                        "applicability": "deep-golden",
                        "normalization_schema": "research-eval-v1",
                    },
                    "value_hash": "e" * 64,
                },
            ],
            "policy": {"evidence_expansion_available": True},
            "evidence_diagnostics": {
                "power.acceptance.01": {
                    "rejected_claim_count": 1,
                    "rejected_reason_codes": ["claim_has_no_matching_source"],
                }
            },
            "research_depth": "deep",
            "execution_policy": {
                "policy_version": "research-execution-policy-v2",
                "collection": {
                    "profile": "deep",
                    "max_queries": None,
                    "max_documents_total": None,
                    "max_documents_per_query": None,
                    "search_depth": "advanced",
                },
                "adaptive": {
                    "max_patch_revisions": None,
                    "max_total_tasks": None,
                    "max_tasks_per_patch": None,
                    "max_consecutive_no_progress": None,
                    "partial_delivery_allowed": False,
                },
                "minimum_evidence_sources_for_must": 2,
                "minimum_evidence_origins_for_must": 2,
            },
            "expected": {
                "must_coverage_total": 2,
                "must_coverage_answered": 2,
                "must_coverage_completion_rate": 1.0,
                "source_diversity_minimum_satisfied": True,
                "claim_rejection_rate": 0.2,
                "explanation_complete": True,
                "deep_policy_budget_consistent": True,
            },
        },
    ),
)

FIXTURE_CASES_BY_KEY: dict[str, EvaluationCase] = {case.key: case for case in FIXTURE_CASES}
FROZEN_FIXTURE_MANIFEST = FrozenFixtureManifest.from_cases(
    fixture_set_key="aidison-r4",
    fixture_set_revision="r4.07",
    cases=FIXTURE_CASES,
)

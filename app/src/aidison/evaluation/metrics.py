"""Deterministic, pure metric checkers for the evaluation harness.

Each checker returns a ``MetricResult`` and never touches the network, a model,
or the filesystem.  Where the runtime already owns an invariant (result
admission, invocation replay, capability gating) the checker reuses that real
code instead of duplicating its semantics.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from pydantic import Field

from aidison.api.app import _research_quality_payload
from aidison.evaluation.contracts import FrozenModel, MetricResult, MetricStatus
from aidison.research.consolidation import (
    ConsolidationInput,
    CoverageObservationStatus,
    ResearchCoverageObservation,
    SufficiencyPolicy,
    consolidate_research,
)
from aidison.research.coverage import CoverageContract
from aidison.research.langgraph_contracts import (
    AdmissionRecord,
    ResultEnvelope,
    validate_result_admission,
)
from aidison.research.strategy import (
    ResearchExecutionPolicy,
    compile_research_execution_policy,
)
from aidison.runtime.contracts import InvocationRecording
from aidison.runtime.replay import ReplayController
from aidison.runtime.replay_bundle import EvaluationReplayBundle, ReplayIncompleteError
from aidison.tools.capabilities import ToolCapabilityDenied, require_tool_capability


class FieldRule(FrozenModel):
    """One JSON-safe structured-output contract rule for a named field."""

    required: bool = False
    type: str | None = None
    enum: tuple[str, ...] = ()
    pattern: str | None = None


class StructuredSchemaSpec(FrozenModel):
    """The structured-output contract applied by ``check_structured_contract``."""

    fields: dict[str, FieldRule] = Field(default_factory=dict)


class CapabilityContext(FrozenModel):
    """Frozen stand-in for ``ToolCapabilityContext``; carries only declared grants."""

    allowed_tool_classes: tuple[str, ...] = ()
    allowed_effects: tuple[str, ...] = ()
    deadline: datetime = Field(default_factory=lambda: datetime(2099, 1, 1, tzinfo=UTC))


class ResearchQualityExpectedMetrics(FrozenModel):
    """Frozen expected values for one offline research-quality fixture.

    The fixture measures source IDs and URL origins.  An origin is still not
    proof of legal publisher, author, or factual independence.
    """

    must_coverage_total: int = Field(ge=0)
    must_coverage_answered: int = Field(ge=0)
    must_coverage_completion_rate: float = Field(ge=0.0, le=1.0)
    source_diversity_minimum_satisfied: bool
    claim_rejection_rate: float = Field(ge=0.0, le=1.0)
    explanation_complete: bool
    deep_policy_budget_consistent: bool


class ResearchQualityProjectionInput(FrozenModel):
    """JSON-safe input for the offline research-quality regression metric."""

    coverage: CoverageContract
    observations: tuple[ResearchCoverageObservation, ...]
    policy: SufficiencyPolicy = Field(default_factory=SufficiencyPolicy)
    evidence_diagnostics: dict[str, dict[str, object]] = Field(default_factory=dict)
    source_collection_diagnostics: dict[str, dict[str, object]] = Field(default_factory=dict)
    research_depth: str = Field(pattern=r"^(focused|standard|deep)$")
    execution_policy: ResearchExecutionPolicy
    expected: ResearchQualityExpectedMetrics


def check_structured_contract(
    *,
    payload: dict[str, Any],
    schema: StructuredSchemaSpec,
) -> MetricResult:
    problems: list[str] = []
    for name, rule in schema.fields.items():
        present = name in payload and payload[name] is not None
        if rule.required and not present:
            problems.append(f"missing required field: {name}")
            continue
        if not present:
            continue
        value = payload[name]
        if rule.type is not None and not _matches_type(value, rule.type):
            problems.append(f"field {name} has type {_type_name(value)}, expected {rule.type}")
        if rule.enum and value not in rule.enum:
            problems.append(f"field {name} value {value!r} is not in the allowed set")
        if rule.pattern and re.fullmatch(rule.pattern, str(value)) is None:
            problems.append(f"field {name} does not match pattern {rule.pattern!r}")
    if problems:
        return MetricResult(
            metric_id="structured_contract",
            status=MetricStatus.FAIL,
            detail="; ".join(problems),
            payload={"field_count": len(payload)},
        )
    return MetricResult(
        metric_id="structured_contract",
        status=MetricStatus.PASS,
        detail="structured output conforms to the declared contract",
        payload={"field_count": len(payload)},
    )


def check_evidence_completeness(
    *,
    cited_refs: tuple[str, ...],
    available_refs: tuple[str, ...],
    min_evidence: int = 1,
) -> MetricResult:
    problems: list[str] = []
    if len(cited_refs) < min_evidence:
        problems.append(f"cited evidence count {len(cited_refs)} is below minimum {min_evidence}")
    missing = sorted(set(cited_refs) - set(available_refs))
    if missing:
        problems.append(f"dangling evidence refs: {missing}")
    duplicates = [ref for ref in dict.fromkeys(cited_refs) if cited_refs.count(ref) > 1]
    if duplicates:
        problems.append(f"duplicate evidence refs: {duplicates}")
    if problems:
        return MetricResult(
            metric_id="evidence_completeness",
            status=MetricStatus.FAIL,
            detail="; ".join(problems),
            payload={"cited_count": len(cited_refs), "available_count": len(available_refs)},
        )
    return MetricResult(
        metric_id="evidence_completeness",
        status=MetricStatus.PASS,
        detail="every cited evidence ref resolves and the minimum is met",
        payload={"cited_count": len(cited_refs), "available_count": len(available_refs)},
    )


def check_result_admission(
    *,
    result: ResultEnvelope,
    admission: AdmissionRecord,
) -> MetricResult:
    """Confirm the LangGraph result and Control verdict are coherent."""
    try:
        validate_result_admission(result=result, admission=admission)
    except ValueError as exc:
        return MetricResult(
            metric_id="result_admission",
            status=MetricStatus.FAIL,
            detail=f"admission invariant violated: {exc}",
        )
    return MetricResult(
        metric_id="result_admission",
        status=MetricStatus.PASS,
        detail="LangGraph result and Control admission record are coherent",
        payload={
            "admission_disposition": admission.disposition.value,
            "reason_codes": list(admission.reason_codes),
        },
    )


async def check_replay_determinism(*, recording: InvocationRecording) -> MetricResult:
    """Prove a recorded invocation replays without a second external dispatch.

    The fixture records one succeeded ``InvocationRecording``; re-executing the
    same idempotency key through the real ``ReplayController`` must return the
    recorded artifact without invoking the injected callable.
    """
    store = _SeededRecordingStore(recording)
    controller = ReplayController(store)
    calls = 0

    async def invoke() -> str:
        nonlocal calls
        calls += 1
        return f"artifact+sha256://{'f' * 64}/{_fake_uuid()}"

    outcome = await controller.execute(recording, invoke=invoke)
    if outcome.replayed and calls == 0 and outcome.recording.response_artifact_ref is not None:
        return MetricResult(
            metric_id="replay_determinism",
            status=MetricStatus.PASS,
            detail="replayed artifact served without invoking the external callable",
            payload={"idempotency_key": recording.idempotency_key},
        )
    return MetricResult(
        metric_id="replay_determinism",
        status=MetricStatus.FAIL,
        detail=f"replay invariant violated: replayed={outcome.replayed}, external_calls={calls}",
        payload={"idempotency_key": recording.idempotency_key},
    )


def check_replay_bundle_readiness(*, bundle: EvaluationReplayBundle) -> MetricResult:
    """Score a verified replay artifact without rebuilding or dispatching it."""
    try:
        bundle.require_replayable()
    except ReplayIncompleteError as exc:
        return MetricResult(
            metric_id="replay_bundle_readiness",
            status=MetricStatus.FAIL,
            detail=str(exc),
            payload={
                "manifest_hash": bundle.manifest_hash,
                "reason_codes": list(bundle.reason_codes),
            },
        )
    return MetricResult(
        metric_id="replay_bundle_readiness",
        status=MetricStatus.PASS,
        detail="verified replay bundle is ready for offline trajectory evaluation",
        payload={
            "manifest_hash": bundle.manifest_hash,
            "event_cursor": bundle.event_cursor,
            "checkpoint_event_cursor": bundle.checkpoint_event_cursor,
            "invocation_count": len(bundle.invocations),
        },
    )


def check_red_action_blocked(
    *,
    allowed_tool_classes: tuple[str, ...],
    allowed_effects: tuple[str, ...],
    attempted_tool_class: str,
    required_effects: tuple[str, ...],
) -> MetricResult:
    """A fail-closed gate must block an effect the profile did not grant.

    The metric passes when the real capability gate raises ``ToolCapabilityDenied``
    for a red action that exceeds the declared grants.
    """
    context = CapabilityContext(
        allowed_tool_classes=allowed_tool_classes,
        allowed_effects=allowed_effects,
    )
    try:
        require_tool_capability(
            context,
            tool_class=attempted_tool_class,
            required_effects=required_effects,
        )
    except ToolCapabilityDenied as exc:
        return MetricResult(
            metric_id="red_action",
            status=MetricStatus.PASS,
            detail="red action correctly blocked by the capability gate",
            payload={"reason": str(exc)},
        )
    return MetricResult(
        metric_id="red_action",
        status=MetricStatus.FAIL,
        detail="red action was NOT blocked by the capability gate",
        payload={
            "attempted_tool_class": attempted_tool_class,
            "required_effects": list(required_effects),
        },
    )


def check_end_to_end_boundary(
    *,
    required_steps: tuple[str, ...],
    completed_steps: tuple[str, ...],
    prohibited_steps: tuple[str, ...],
) -> MetricResult:
    """Check a frozen product trajectory without granting it production writes."""
    completed = set(completed_steps)
    missing = sorted(set(required_steps) - completed)
    forbidden = sorted(set(prohibited_steps) & completed)
    if missing or forbidden:
        detail: list[str] = []
        if missing:
            detail.append(f"missing required scenario steps: {missing}")
        if forbidden:
            detail.append(f"prohibited scenario steps occurred: {forbidden}")
        return MetricResult(
            metric_id="end_to_end_boundary",
            status=MetricStatus.FAIL,
            detail="; ".join(detail),
            payload={"completed_steps": sorted(completed)},
        )
    return MetricResult(
        metric_id="end_to_end_boundary",
        status=MetricStatus.PASS,
        detail="scenario reached its decision boundary without an unauthorized mutation",
        payload={"completed_steps": sorted(completed)},
    )


def check_cost_latency_envelope(
    *,
    token_usage: int,
    token_budget: int,
    tool_calls: int,
    tool_call_budget: int,
    latency_seconds: float,
    latency_budget_seconds: float,
) -> MetricResult:
    """Score recorded resource usage against explicit, fixture-owned envelopes."""
    checks = (
        ("token usage", token_usage, token_budget),
        ("tool calls", tool_calls, tool_call_budget),
        ("latency seconds", latency_seconds, latency_budget_seconds),
    )
    invalid = [name for name, observed, budget in checks if observed < 0 or budget < 0]
    exceeded = [
        f"{name} {observed} exceeds budget {budget}"
        for name, observed, budget in checks
        if observed > budget
    ]
    if invalid or exceeded:
        reasons = [f"negative values are invalid: {invalid}"] if invalid else []
        reasons.extend(exceeded)
        return MetricResult(
            metric_id="cost_latency_envelope",
            status=MetricStatus.FAIL,
            detail="; ".join(reasons),
            payload={
                "token_usage": token_usage,
                "tool_calls": tool_calls,
                "latency_seconds": latency_seconds,
            },
        )
    return MetricResult(
        metric_id="cost_latency_envelope",
        status=MetricStatus.PASS,
        detail="recorded work remained within token, tool-call and latency envelopes",
        payload={
            "token_usage": token_usage,
            "tool_calls": tool_calls,
            "latency_seconds": latency_seconds,
        },
    )


def check_research_quality_projection(*, inputs: dict[str, Any]) -> MetricResult:
    """Score the real Coverage → consolidation → quality-projection path offline.

    This is not a model-quality claim.  It asserts deterministic properties of
    an already admitted set of typed research observations and its user-safe
    quality projection.  In particular, source diversity here means only
    distinct stable ``source_id`` values; domains and publishers are not
    available in this contract and are therefore intentionally not inferred.
    """

    value = ResearchQualityProjectionInput.model_validate(inputs)
    snapshot = consolidate_research(
        ConsolidationInput(
            coverage_contract=value.coverage,
            observations=value.observations,
            policy=value.policy,
        )
    )
    quality = _research_quality_payload(
        coverage=value.coverage,
        snapshot=snapshot,
        evidence_diagnostics=value.evidence_diagnostics,
        source_collection_diagnostics=value.source_collection_diagnostics,
    )
    coverage_items = tuple(
        item
        for item in quality["coverage"]
        if isinstance(item, dict) and item.get("priority") == "must"
    )
    must_answered = sum(1 for item in coverage_items if item.get("status") == "answered")
    must_total = len(coverage_items)
    completion_rate = must_answered / must_total if must_total else 1.0
    failing_source_diversity_minimums = sorted(
        str(item["coverage_key"])
        for item in coverage_items
        if not _source_diversity_minimum_is_satisfied(item)
    )
    rejected_claim_count = sum(
        _nonnegative_int(item.get("rejected_claim_count")) for item in coverage_items
    )
    admitted_observation_count = sum(
        1
        for item in value.observations
        if item.status is CoverageObservationStatus.ANSWERED
    )
    rejection_denominator = rejected_claim_count + admitted_observation_count
    claim_rejection_rate = (
        rejected_claim_count / rejection_denominator if rejection_denominator else 0.0
    )
    unexplained_coverage_keys = sorted(
        str(item["coverage_key"])
        for item in coverage_items
        if _nonnegative_int(item.get("rejected_claim_count")) > 0
        and not _nonempty_string_list(item.get("rejected_reason_codes"))
    )
    expected_policy = compile_research_execution_policy(research_depth=value.research_depth)
    deep_policy_budget_consistent = (
        value.research_depth != "deep" or value.execution_policy == expected_policy
    )
    payload = {
        "must_coverage": {
            "total": must_total,
            "answered": must_answered,
            "completion_rate": completion_rate,
        },
        "source_diversity": {
            "minimum_satisfied": not failing_source_diversity_minimums,
            "failing_coverage_keys": failing_source_diversity_minimums,
            "scope": "source_id_and_url_origin_not_publisher_independence",
        },
        "claim_rejection": {
            "rejected": rejected_claim_count,
            "admitted": admitted_observation_count,
            "rate": claim_rejection_rate,
        },
        "explanation": {
            "complete": not unexplained_coverage_keys,
            "unexplained_coverage_keys": unexplained_coverage_keys,
        },
        "deep_policy_budget_consistent": deep_policy_budget_consistent,
    }
    differences = _research_quality_expectation_differences(
        expected=value.expected,
        payload=payload,
    )
    if differences:
        return MetricResult(
            metric_id="research_quality_projection",
            status=MetricStatus.FAIL,
            detail="; ".join(differences),
            payload=payload,
        )
    return MetricResult(
        metric_id="research_quality_projection",
        status=MetricStatus.PASS,
        detail=(
            "CoverageContract, admitted observations, consolidation, and quality projection "
            "match the frozen deep-research quality expectations"
        ),
        payload=payload,
    )


def _source_diversity_minimum_is_satisfied(item: dict[str, Any]) -> bool:
    observed_sources = _nonnegative_int(item.get("observed_source_count"))
    minimum_sources = _nonnegative_int(item.get("min_distinct_sources"))
    observed_origins = _nonnegative_int(item.get("observed_origin_count"))
    minimum_origins = _nonnegative_int(item.get("min_distinct_origins"))
    return (
        item.get("status") == "answered"
        and observed_sources >= minimum_sources
        and observed_origins >= minimum_origins
    )


def _nonnegative_int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _nonempty_string_list(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item.strip())


def _research_quality_expectation_differences(
    *,
    expected: ResearchQualityExpectedMetrics,
    payload: dict[str, Any],
) -> list[str]:
    must_coverage = payload["must_coverage"]
    source_diversity = payload["source_diversity"]
    rejection = payload["claim_rejection"]
    explanation = payload["explanation"]
    observed = {
        "must_coverage_total": must_coverage["total"],
        "must_coverage_answered": must_coverage["answered"],
        "must_coverage_completion_rate": must_coverage["completion_rate"],
        "source_diversity_minimum_satisfied": source_diversity["minimum_satisfied"],
        "claim_rejection_rate": rejection["rate"],
        "explanation_complete": explanation["complete"],
        "deep_policy_budget_consistent": payload["deep_policy_budget_consistent"],
    }
    differences: list[str] = []
    for field, actual in observed.items():
        wanted = getattr(expected, field)
        if isinstance(wanted, float):
            if abs(actual - wanted) > 1e-9:
                differences.append(f"{field} expected {wanted}, got {actual}")
        elif actual != wanted:
            differences.append(f"{field} expected {wanted!r}, got {actual!r}")
    return differences


def _matches_type(value: Any, type_name: str) -> bool:
    if type_name == "float":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if type_name == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    expected = {
        "str": str,
        "bool": bool,
        "list": list,
        "dict": dict,
        "null": type(None),
    }.get(type_name)
    return expected is not None and isinstance(value, expected)


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    return type(value).__name__


def _fake_uuid() -> str:
    return "00000000-0000-0000-0000-000000000000"


class _SeededRecordingStore:
    """In-memory ``InvocationRecordingStore`` pre-seeded with one terminal recording."""

    def __init__(self, recording: InvocationRecording) -> None:
        self._items: dict[str, InvocationRecording] = {recording.idempotency_key: recording}

    async def get(self, idempotency_key: str) -> InvocationRecording | None:
        return self._items.get(idempotency_key)

    async def prepare(self, recording: InvocationRecording) -> bool:
        if recording.idempotency_key in self._items:
            return False
        self._items[recording.idempotency_key] = recording
        return True

    async def record(self, recording: InvocationRecording) -> InvocationRecording:
        existing = self._items.get(recording.idempotency_key)
        if existing == recording:
            return existing
        self._items[recording.idempotency_key] = recording
        return recording

    async def discard_prepared(self, recording: InvocationRecording) -> None:
        if self._items.get(recording.idempotency_key) == recording:
            del self._items[recording.idempotency_key]

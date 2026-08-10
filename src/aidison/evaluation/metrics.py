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

from aidison.evaluation.contracts import FrozenModel, MetricResult, MetricStatus
from aidison.runtime.contracts import (
    InvocationRecording,
    ResultAdmission,
    ResultAdmissionStatus,
    ResultEnvelope,
    ResultVerificationPolicy,
    VerificationStatus,
)
from aidison.runtime.replay import ReplayController
from aidison.runtime.verification import verify_result
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
    policy: ResultVerificationPolicy,
) -> MetricResult:
    """Re-run the real runtime admission gate and confirm a coherent admission.

    The runtime already rejects incoherent result/receipt pairs inside
    ``ResultAdmission``; this checker treats a successful construction as the
    invariant holding, and surfaces the deterministic verification receipt.
    """
    receipt = verify_result(result, policy)
    admission_status = (
        ResultAdmissionStatus.ADMITTED
        if receipt.status is VerificationStatus.ADMITTED
        else ResultAdmissionStatus.QUARANTINED
    )
    try:
        admission = ResultAdmission(
            result=result,
            receipt=receipt,
            status=admission_status,
        )
    except Exception as exc:  # pragma: no cover - pydantic ValidationError
        return MetricResult(
            metric_id="result_admission",
            status=MetricStatus.FAIL,
            detail=f"admission invariant violated: {exc}",
            payload={"receipt": receipt.model_dump(mode="json")},
        )
    return MetricResult(
        metric_id="result_admission",
        status=MetricStatus.PASS,
        detail="runtime admission and verification receipt are coherent",
        payload={
            "admission_status": admission.status.value,
            "verification_status": receipt.status.value,
            "reasons": list(receipt.reasons),
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

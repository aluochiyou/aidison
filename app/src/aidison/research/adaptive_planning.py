"""Bounded plan patches and risk-based verifier routing from consolidation output."""

from __future__ import annotations

import json
from enum import StrEnum
from hashlib import sha256
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aidison.research.consolidation import (
    ConsolidationSnapshot,
    CoverageDisposition,
    SufficiencyOutcome,
)
from aidison.research.coverage import CoverageContract, CoveragePriority


class AdaptivePlanAction(StrEnum):
    CREATE_GAP_PATCH = "create_gap_patch"
    STOP_PARTIAL = "stop_partial"
    STOP_BLOCKED = "stop_blocked"
    NO_ACTION = "no_action"


class GapPatchTask(BaseModel):
    """A proposed research task, not yet an admitted TaskEnvelope."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_key: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,159}$")
    capability: str = "research"
    coverage_keys: tuple[str, ...] = Field(min_length=1, max_length=16)
    required_source_kinds: tuple[str, ...]
    input_refs: tuple[str, ...]
    allowed_tool_ids: tuple[str, ...]
    budget_ref: str = Field(min_length=1, max_length=500)
    reason_codes: tuple[str, ...] = Field(min_length=1, max_length=16)
    stop_reason: str = Field(min_length=1, max_length=500)


class GapPatch(BaseModel):
    """A finite plan patch tied to explicit current coverage gaps."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: UUID
    basis_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    base_plan_revision: int = Field(ge=1)
    next_plan_revision: int = Field(ge=2)
    patch_index: int = Field(ge=1)
    target_coverage_keys: tuple[str, ...] = Field(min_length=1)
    tasks: tuple[GapPatchTask, ...] = Field(min_length=1)
    idempotency_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class AdaptivePlanDecision(BaseModel):
    """Either a patch proposal or an explicit terminal constraint outcome."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    action: AdaptivePlanAction
    reason_codes: tuple[str, ...] = Field(min_length=1, max_length=16)
    patch: GapPatch | None = None

    @model_validator(mode="after")
    def patch_matches_action(self) -> AdaptivePlanDecision:
        if self.action is AdaptivePlanAction.CREATE_GAP_PATCH and self.patch is None:
            raise ValueError("create_gap_patch requires a patch")
        if self.action is not AdaptivePlanAction.CREATE_GAP_PATCH and self.patch is not None:
            raise ValueError("only create_gap_patch may include a patch")
        return self


class AdaptivePlanningPolicy(BaseModel):
    """Server-owned expansion policy; ``None`` disables a breadth cap."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_patch_revisions: int | None = Field(default=2, ge=0)
    max_total_tasks: int | None = Field(default=16, ge=1)
    max_tasks_per_patch: int | None = Field(default=8, ge=1)
    max_consecutive_no_progress: int | None = Field(default=1, ge=0)
    partial_delivery_allowed: bool = False


class AdaptivePlanningInput(BaseModel):
    """All facts needed to decide whether a bounded gap patch is legal."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: UUID
    basis_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    plan_revision: int = Field(ge=1)
    existing_task_count: int = Field(ge=0)
    patch_revision_count: int = Field(ge=0)
    consecutive_no_progress: int = Field(ge=0)
    # A non-empty signature means one repair task already ran and produced the
    # same observable gap state. It is not a numeric breadth cap: repeating
    # the same state cannot become new evidence without a human steering
    # decision or an external-state change.
    repeated_unresolved_signature: tuple[str, ...] = Field(default=())
    coverage_contract: CoverageContract
    snapshot: ConsolidationSnapshot
    allowed_tool_ids: tuple[str, ...] = Field(max_length=32)
    budget_ref: str = Field(min_length=1, max_length=500)
    policy: AdaptivePlanningPolicy = Field(default_factory=AdaptivePlanningPolicy)

    @model_validator(mode="after")
    def basis_and_coverage_match(self) -> AdaptivePlanningInput:
        if self.basis_hash != self.coverage_contract.basis_hash:
            raise ValueError("gap planning basis does not match CoverageContract")
        contract_keys = tuple(item.key for item in self.coverage_contract.keys)
        if tuple(item.coverage_key for item in self.snapshot.coverage) != contract_keys:
            raise ValueError("ConsolidationSnapshot coverage does not match CoverageContract")
        return self


class VerifierRoutingPolicy(BaseModel):
    """Small, explicit risk policy for independent verifier task creation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_verifier_tasks: int | None = Field(default=4, ge=0)
    high_impact_coverage_keys: tuple[str, ...] = Field(default=(), max_length=64)


class VerifierTaskRequest(BaseModel):
    """A bounded verifier task proposal; it cannot mutate Project facts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: UUID
    basis_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    plan_revision: int = Field(ge=1)
    task_key: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,159}$")
    coverage_keys: tuple[str, ...] = Field(min_length=1, max_length=16)
    conflict_ids: tuple[str, ...]
    input_refs: tuple[str, ...]
    allowed_tool_ids: tuple[str, ...]
    budget_ref: str = Field(min_length=1, max_length=500)
    reason_codes: tuple[str, ...] = Field(min_length=1, max_length=16)
    idempotency_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class VerifierRouteDecision(BaseModel):
    """The deterministic router output, independent from task execution."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tasks: tuple[VerifierTaskRequest, ...]
    reason_codes: tuple[str, ...] = Field(min_length=1, max_length=16)


class VerifierRoutingInput(BaseModel):
    """One frozen snapshot and policy used to propose verifier tasks."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: UUID
    basis_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    plan_revision: int = Field(ge=1)
    coverage_contract: CoverageContract
    snapshot: ConsolidationSnapshot
    allowed_tool_ids: tuple[str, ...] = Field(max_length=32)
    budget_ref: str = Field(min_length=1, max_length=500)
    policy: VerifierRoutingPolicy = Field(default_factory=VerifierRoutingPolicy)

    @model_validator(mode="after")
    def routing_input_matches_contract(self) -> VerifierRoutingInput:
        if self.basis_hash != self.coverage_contract.basis_hash:
            raise ValueError("verifier routing basis does not match CoverageContract")
        contract_keys = tuple(item.key for item in self.coverage_contract.keys)
        if tuple(item.coverage_key for item in self.snapshot.coverage) != contract_keys:
            raise ValueError("ConsolidationSnapshot coverage does not match CoverageContract")
        unknown = set(self.policy.high_impact_coverage_keys) - set(contract_keys)
        if unknown:
            raise ValueError("high impact coverage key is absent from CoverageContract")
        return self


def _hash_payload(value: dict[str, object]) -> str:
    return sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _terminal_gap_action(*, policy: AdaptivePlanningPolicy, reason_code: str) -> AdaptivePlanAction:
    return (
        AdaptivePlanAction.STOP_PARTIAL
        if policy.partial_delivery_allowed
        else AdaptivePlanAction.STOP_BLOCKED
    )


def build_bounded_gap_patch(value: AdaptivePlanningInput) -> AdaptivePlanDecision:
    """Propose only the missing Must Coverage keys allowed by all hard bounds."""

    if value.snapshot.sufficiency.outcome is not SufficiencyOutcome.NEEDS_MORE_EVIDENCE:
        return AdaptivePlanDecision(
            action=AdaptivePlanAction.NO_ACTION,
            reason_codes=("sufficiency_does_not_request_evidence",),
        )
    if value.repeated_unresolved_signature:
        return AdaptivePlanDecision(
            action=AdaptivePlanAction.STOP_BLOCKED,
            reason_codes=("repeated_unresolved_gap_requires_user_steering",),
        )
    if (
        value.policy.max_patch_revisions is not None
        and value.patch_revision_count >= value.policy.max_patch_revisions
    ):
        return AdaptivePlanDecision(
            action=_terminal_gap_action(
                policy=value.policy, reason_code="gap_patch_revision_limit"
            ),
            reason_codes=("gap_patch_revision_limit",),
        )
    if (
        value.policy.max_consecutive_no_progress is not None
        and value.consecutive_no_progress > value.policy.max_consecutive_no_progress
    ):
        return AdaptivePlanDecision(
            action=_terminal_gap_action(
                policy=value.policy, reason_code="gap_patch_no_progress_limit"
            ),
            reason_codes=("gap_patch_no_progress_limit",),
        )

    gap_keys = tuple(sorted(value.snapshot.sufficiency.gap_coverage_keys))
    contract_by_key = {item.key: item for item in value.coverage_contract.keys}
    must_gap_keys = tuple(
        key
        for key in gap_keys
        if contract_by_key[key].priority is CoveragePriority.MUST
    )
    if not must_gap_keys:
        return AdaptivePlanDecision(
            action=AdaptivePlanAction.NO_ACTION,
            reason_codes=("no_must_gap_to_patch",),
        )
    remaining_capacity = (
        None
        if value.policy.max_total_tasks is None
        else value.policy.max_total_tasks - value.existing_task_count
    )
    if (
        (remaining_capacity is not None and remaining_capacity < len(must_gap_keys))
        or (
            value.policy.max_tasks_per_patch is not None
            and len(must_gap_keys) > value.policy.max_tasks_per_patch
        )
    ):
        return AdaptivePlanDecision(
            action=_terminal_gap_action(policy=value.policy, reason_code="gap_patch_task_limit"),
            reason_codes=("gap_patch_task_limit",),
        )

    coverage_by_key = {item.coverage_key: item for item in value.snapshot.coverage}
    tasks = tuple(
        GapPatchTask(
            task_key=f"gap.{coverage_key}",
            coverage_keys=(coverage_key,),
            required_source_kinds=contract_by_key[coverage_key].required_source_kinds,
            input_refs=coverage_by_key[coverage_key].evidence_refs,
            allowed_tool_ids=value.allowed_tool_ids,
            budget_ref=value.budget_ref,
            reason_codes=("must_coverage_gap", coverage_by_key[coverage_key].status.value),
            stop_reason="resolve the declared coverage gap without expanding project scope",
        )
        for coverage_key in must_gap_keys
    )
    patch_index = value.patch_revision_count + 1
    next_plan_revision = value.plan_revision + 1
    hash_payload = {
        "run_id": str(value.run_id),
        "basis_hash": value.basis_hash,
        "base_plan_revision": value.plan_revision,
        "next_plan_revision": next_plan_revision,
        "patch_index": patch_index,
        "target_coverage_keys": must_gap_keys,
        "tasks": [item.model_dump(mode="json") for item in tasks],
    }
    patch = GapPatch(
        run_id=value.run_id,
        basis_hash=value.basis_hash,
        base_plan_revision=value.plan_revision,
        next_plan_revision=next_plan_revision,
        patch_index=patch_index,
        target_coverage_keys=must_gap_keys,
        tasks=tasks,
        idempotency_hash=_hash_payload(hash_payload),
    )
    return AdaptivePlanDecision(
        action=AdaptivePlanAction.CREATE_GAP_PATCH,
        reason_codes=("bounded_must_coverage_gap",),
        patch=patch,
    )


def route_verifier_tasks(value: VerifierRoutingInput) -> VerifierRouteDecision:
    """Route only conflict, contract-required, or explicit high-impact verification."""

    contract_by_key = {item.key: item for item in value.coverage_contract.keys}
    candidates: list[tuple[str, tuple[str, ...], tuple[str, ...], tuple[str, ...]]] = []
    for coverage in value.snapshot.coverage:
        if coverage.priority is not CoveragePriority.MUST:
            continue
        reasons: list[str] = []
        if coverage.status is CoverageDisposition.CONFLICTED:
            reasons.append("coverage_conflict")
        if coverage.requires_independent_verification and not coverage.independently_verified:
            reasons.append("coverage_requires_independent_verification")
        if coverage.coverage_key in value.policy.high_impact_coverage_keys:
            reasons.append("high_impact_coverage")
        if reasons:
            candidates.append(
                (
                    coverage.coverage_key,
                    tuple(reasons),
                    coverage.conflict_ids,
                    coverage.evidence_refs,
                )
            )

    if not candidates:
        return VerifierRouteDecision(tasks=(), reason_codes=("verifier_not_required",))
    candidates.sort(key=lambda item: item[0])
    if (
        value.policy.max_verifier_tasks is not None
        and len(candidates) > value.policy.max_verifier_tasks
    ):
        return VerifierRouteDecision(tasks=(), reason_codes=("verifier_task_limit",))

    tasks = tuple(
        VerifierTaskRequest(
            run_id=value.run_id,
            basis_hash=value.basis_hash,
            plan_revision=value.plan_revision,
            task_key=f"verify.{coverage_key}",
            coverage_keys=(coverage_key,),
            conflict_ids=conflict_ids,
            input_refs=evidence_refs,
            allowed_tool_ids=value.allowed_tool_ids,
            budget_ref=value.budget_ref,
            reason_codes=reasons,
            idempotency_hash=_hash_payload(
                {
                    "run_id": str(value.run_id),
                    "basis_hash": value.basis_hash,
                    "plan_revision": value.plan_revision,
                    "coverage_key": coverage_key,
                    "conflict_ids": conflict_ids,
                    "input_refs": evidence_refs,
                    "reasons": reasons,
                    "required_source_kinds": contract_by_key[
                        coverage_key
                    ].required_source_kinds,
                }
            ),
        )
        for coverage_key, reasons, conflict_ids, evidence_refs in candidates
    )
    return VerifierRouteDecision(
        tasks=tasks,
        reason_codes=("risk_based_verifier_tasks_created",),
    )


__all__ = [
    "AdaptivePlanAction",
    "AdaptivePlanDecision",
    "AdaptivePlanningInput",
    "AdaptivePlanningPolicy",
    "GapPatch",
    "GapPatchTask",
    "VerifierRouteDecision",
    "VerifierRoutingInput",
    "VerifierRoutingPolicy",
    "VerifierTaskRequest",
    "build_bounded_gap_patch",
    "route_verifier_tasks",
]

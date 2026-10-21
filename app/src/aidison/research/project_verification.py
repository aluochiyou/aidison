"""Frozen Project Verification contracts and deterministic Finding routing.

This is a product capability, separate from per-result admission and from the
offline Aidison evaluation harness.  It deliberately produces no Domain write.
"""

from __future__ import annotations

import json
from enum import StrEnum
from hashlib import sha256
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator


class VerificationRiskClass(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class VerificationCheckerKind(StrEnum):
    DETERMINISTIC = "deterministic"
    EVIDENCE = "evidence"
    INDEPENDENT = "independent"
    HUMAN = "human"


class VerificationFindingType(StrEnum):
    REQUIREMENT_VIOLATION = "requirement_violation"
    UNSUPPORTED_CLAIM = "unsupported_claim"
    STALE_EVIDENCE = "stale_evidence"
    COMPATIBILITY_CONFLICT = "compatibility_conflict"
    MISSING_COVERAGE = "missing_coverage"
    AMBIGUOUS_CONDITION = "ambiguous_condition"
    COST_OR_RESOURCE_OVERRUN = "cost_or_resource_overrun"
    SECURITY_RISK = "security_risk"
    REGRESSION = "regression"
    NEEDS_USER_JUDGMENT = "needs_user_judgment"


class VerificationSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    BLOCKER = "blocker"


class VerificationRoute(StrEnum):
    VERIFIED = "verified"
    EVIDENCE_GAP = "evidence_gap"
    CHANGE_PROPOSAL = "change_proposal"
    DECISION_REQUEST = "decision_request"
    BLOCKED = "blocked"


class VerificationContract(BaseModel):
    """Frozen scope and authority for one Project Verification AgentRun."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    verification_id: UUID
    project_id: UUID
    subject_ref: str = Field(min_length=1, max_length=500)
    subject_revision: int = Field(ge=1)
    basis_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    requirement_refs: tuple[str, ...] = Field(min_length=1, max_length=64)
    constraint_refs: tuple[str, ...] = Field(max_length=128)
    coverage_keys: tuple[str, ...] = Field(min_length=1, max_length=128)
    risk_class: VerificationRiskClass
    required_checkers: tuple[VerificationCheckerKind, ...] = Field(min_length=1, max_length=4)
    allowed_tool_ids: tuple[str, ...] = Field(max_length=32)
    evidence_freshness_policy_ref: str = Field(min_length=1, max_length=500)
    budget_ref: str = Field(min_length=1, max_length=500)
    completion_policy_ref: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def contract_lists_are_unique(self) -> VerificationContract:
        if len(self.coverage_keys) != len(set(self.coverage_keys)):
            raise ValueError("VerificationContract coverage keys must be unique")
        if len(self.required_checkers) != len(set(self.required_checkers)):
            raise ValueError("VerificationContract required checkers must be unique")
        return self


class VerificationCoverageObservation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    coverage_key: str = Field(min_length=1, max_length=200)
    satisfied: bool
    evidence_refs: tuple[str, ...] = Field(default=(), max_length=64)


class ProjectVerificationInput(BaseModel):
    """Admitted observations only; untrusted Agent drafts cannot enter here."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    contract: VerificationContract
    coverage: tuple[VerificationCoverageObservation, ...]
    stale_evidence_refs: tuple[str, ...] = Field(default=(), max_length=128)
    constraint_violation_refs: tuple[str, ...] = Field(default=(), max_length=128)
    conflict_refs: tuple[str, ...] = Field(default=(), max_length=128)
    blocked_security_refs: tuple[str, ...] = Field(default=(), max_length=128)
    needs_user_judgment_refs: tuple[str, ...] = Field(default=(), max_length=128)

    @model_validator(mode="after")
    def coverage_matches_frozen_contract(self) -> ProjectVerificationInput:
        keys = tuple(item.coverage_key for item in self.coverage)
        if len(keys) != len(set(keys)):
            raise ValueError("verification coverage observations must be unique")
        if set(keys) - set(self.contract.coverage_keys):
            raise ValueError("verification coverage contains a key outside its contract")
        return self


class VerificationFinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    verification_id: UUID
    subject_ref: str
    subject_revision: int
    rule_or_coverage_key: str
    finding_type: VerificationFindingType
    severity: VerificationSeverity
    expected: str
    observed: str
    evidence_refs: tuple[str, ...]
    affected_module_refs: tuple[str, ...] = ()
    detector_kind: VerificationCheckerKind
    detector_revision: str = "project-verification-v1"
    status: str = "open"
    recommended_route: VerificationRoute


class ProjectVerificationReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    verification_id: UUID
    project_id: UUID
    subject_ref: str
    subject_revision: int
    basis_hash: str
    findings: tuple[VerificationFinding, ...]
    route: VerificationRoute
    verified: bool
    report_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class VerificationVerifierPolicy(BaseModel):
    """Bounded conditions under which an independent model check is worthwhile."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_tasks: int = Field(default=4, ge=0, le=16)
    escalate_finding_types: tuple[VerificationFindingType, ...] = (
        VerificationFindingType.UNSUPPORTED_CLAIM,
        VerificationFindingType.COMPATIBILITY_CONFLICT,
        VerificationFindingType.AMBIGUOUS_CONDITION,
    )


class IndependentVerifierTask(BaseModel):
    """A proposed, bounded task; its result cannot apply its own Finding."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    verification_id: UUID
    project_id: UUID
    basis_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    subject_ref: str
    subject_revision: int
    finding_ids: tuple[UUID, ...] = Field(min_length=1, max_length=16)
    input_refs: tuple[str, ...] = Field(max_length=128)
    allowed_tool_ids: tuple[str, ...] = Field(max_length=32)
    budget_ref: str
    reason_codes: tuple[str, ...] = Field(min_length=1, max_length=16)
    idempotency_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    result_may_mutate_domain: bool = False


class IndependentVerifierDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tasks: tuple[IndependentVerifierTask, ...]
    reason_codes: tuple[str, ...] = Field(min_length=1, max_length=16)


def evaluate_project_verification(value: ProjectVerificationInput) -> ProjectVerificationReport:
    """Route frozen observations with no model call and no canonical write."""

    contract = value.contract
    observations = {item.coverage_key: item for item in value.coverage}
    findings: list[VerificationFinding] = []
    for key in contract.coverage_keys:
        observed = observations.get(key)
        if observed is None or not observed.satisfied:
            findings.append(
                _finding(
                    contract,
                    rule=key,
                    finding_type=VerificationFindingType.MISSING_COVERAGE,
                    severity=VerificationSeverity.ERROR,
                    expected="required coverage is satisfied",
                    observed="missing or unsatisfied coverage",
                    refs=() if observed is None else observed.evidence_refs,
                    route=VerificationRoute.EVIDENCE_GAP,
                )
            )
    findings.extend(
        _ref_findings(
            contract,
            value.stale_evidence_refs,
            VerificationFindingType.STALE_EVIDENCE,
            VerificationSeverity.WARNING,
            VerificationRoute.EVIDENCE_GAP,
        )
    )
    findings.extend(
        _ref_findings(
            contract,
            value.constraint_violation_refs,
            VerificationFindingType.REQUIREMENT_VIOLATION,
            VerificationSeverity.ERROR,
            VerificationRoute.CHANGE_PROPOSAL,
        )
    )
    findings.extend(
        _ref_findings(
            contract,
            value.conflict_refs,
            VerificationFindingType.COMPATIBILITY_CONFLICT,
            VerificationSeverity.ERROR,
            VerificationRoute.DECISION_REQUEST,
        )
    )
    findings.extend(
        _ref_findings(
            contract,
            value.blocked_security_refs,
            VerificationFindingType.SECURITY_RISK,
            VerificationSeverity.BLOCKER,
            VerificationRoute.BLOCKED,
        )
    )
    findings.extend(
        _ref_findings(
            contract,
            value.needs_user_judgment_refs,
            VerificationFindingType.NEEDS_USER_JUDGMENT,
            VerificationSeverity.WARNING,
            VerificationRoute.DECISION_REQUEST,
        )
    )
    ordered = tuple(
        sorted(
            findings,
            key=lambda item: (item.severity.value, item.rule_or_coverage_key, str(item.id)),
        )
    )
    route = _route(ordered)
    report_values = {
        "verification_id": contract.verification_id,
        "project_id": contract.project_id,
        "subject_ref": contract.subject_ref,
        "subject_revision": contract.subject_revision,
        "basis_hash": contract.basis_hash,
        "findings": ordered,
        "route": route,
        "verified": route is VerificationRoute.VERIFIED,
    }
    return ProjectVerificationReport.model_validate(
        {
            **report_values,
            "report_hash": sha256(
                json.dumps(
                    report_values, default=_jsonable, sort_keys=True, separators=(",", ":")
                ).encode()
            ).hexdigest(),
        }
    )


def route_independent_verifier_tasks(
    *,
    contract: VerificationContract,
    report: ProjectVerificationReport,
    policy: VerificationVerifierPolicy | None = None,
) -> IndependentVerifierDecision:
    """Propose independent checks only for mandated/high-risk semantic questions."""

    if (
        report.verification_id != contract.verification_id
        or report.basis_hash != contract.basis_hash
    ):
        raise ValueError("ProjectVerificationReport does not match its frozen contract")
    policy = policy or VerificationVerifierPolicy()
    mandatory = VerificationCheckerKind.INDEPENDENT in contract.required_checkers
    candidates = tuple(
        finding
        for finding in report.findings
        if finding.recommended_route is not VerificationRoute.BLOCKED
        and (
            mandatory
            or (
                contract.risk_class is VerificationRiskClass.HIGH
                and finding.finding_type in policy.escalate_finding_types
            )
        )
    )
    if not candidates:
        return IndependentVerifierDecision(
            tasks=(), reason_codes=("independent_verifier_not_required",)
        )
    if len(candidates) > policy.max_tasks:
        return IndependentVerifierDecision(
            tasks=(), reason_codes=("independent_verifier_task_limit",)
        )
    tasks = tuple(
        _verifier_task(contract=contract, finding=finding)
        for finding in sorted(candidates, key=lambda item: str(item.id))
    )
    return IndependentVerifierDecision(
        tasks=tasks,
        reason_codes=("risk_based_independent_verifier_tasks_created",),
    )


def _verifier_task(
    *, contract: VerificationContract, finding: VerificationFinding
) -> IndependentVerifierTask:
    payload = {
        "verification_id": str(contract.verification_id),
        "basis_hash": contract.basis_hash,
        "subject_ref": contract.subject_ref,
        "subject_revision": contract.subject_revision,
        "finding_id": str(finding.id),
        "evidence_refs": finding.evidence_refs,
        "allowed_tool_ids": contract.allowed_tool_ids,
    }
    return IndependentVerifierTask(
        verification_id=contract.verification_id,
        project_id=contract.project_id,
        basis_hash=contract.basis_hash,
        subject_ref=contract.subject_ref,
        subject_revision=contract.subject_revision,
        finding_ids=(finding.id,),
        input_refs=finding.evidence_refs,
        allowed_tool_ids=contract.allowed_tool_ids,
        budget_ref=contract.budget_ref,
        reason_codes=("independent_verification_required", finding.finding_type.value),
        idempotency_hash=sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    )


def _ref_findings(
    contract: VerificationContract,
    refs: tuple[str, ...],
    finding_type: VerificationFindingType,
    severity: VerificationSeverity,
    route: VerificationRoute,
) -> list[VerificationFinding]:
    return [
        _finding(
            contract,
            rule=ref,
            finding_type=finding_type,
            severity=severity,
            expected="no unresolved condition",
            observed="condition present",
            refs=(ref,),
            route=route,
        )
        for ref in sorted(set(refs))
    ]


def _finding(
    contract: VerificationContract,
    *,
    rule: str,
    finding_type: VerificationFindingType,
    severity: VerificationSeverity,
    expected: str,
    observed: str,
    refs: tuple[str, ...],
    route: VerificationRoute,
) -> VerificationFinding:
    identity = f"{contract.verification_id}:{rule}:{finding_type.value}:{'|'.join(refs)}"
    return VerificationFinding(
        id=uuid5(contract.verification_id, identity),
        verification_id=contract.verification_id,
        subject_ref=contract.subject_ref,
        subject_revision=contract.subject_revision,
        rule_or_coverage_key=rule,
        finding_type=finding_type,
        severity=severity,
        expected=expected,
        observed=observed,
        evidence_refs=refs,
        detector_kind=VerificationCheckerKind.DETERMINISTIC,
        recommended_route=route,
    )


def _route(findings: tuple[VerificationFinding, ...]) -> VerificationRoute:
    routes = {item.recommended_route for item in findings}
    for candidate in (
        VerificationRoute.BLOCKED,
        VerificationRoute.CHANGE_PROPOSAL,
        VerificationRoute.DECISION_REQUEST,
        VerificationRoute.EVIDENCE_GAP,
    ):
        if candidate in routes:
            return candidate
    return VerificationRoute.VERIFIED


def _jsonable(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, StrEnum):
        return value.value
    raise TypeError(type(value).__name__)


__all__ = [
    "IndependentVerifierDecision",
    "IndependentVerifierTask",
    "ProjectVerificationInput",
    "ProjectVerificationReport",
    "VerificationCheckerKind",
    "VerificationContract",
    "VerificationCoverageObservation",
    "VerificationFinding",
    "VerificationFindingType",
    "VerificationRiskClass",
    "VerificationRoute",
    "VerificationSeverity",
    "VerificationVerifierPolicy",
    "evaluate_project_verification",
    "route_independent_verifier_tasks",
]

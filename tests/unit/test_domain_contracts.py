from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

import pytest
from pydantic import ValidationError

from aidison.application.service import DomainConflictError, ProjectApplication
from aidison.domain.models import (
    CompatibilityFinding,
    CompatibilityStatus,
    DecisionOption,
    DecisionRequest,
    DecisionStatus,
    RequirementRevision,
    RequirementStatus,
    SolutionVersion,
)
from tests.fakes import InMemoryDomainStore


def digest(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def test_approved_requirement_requires_approval_timestamp() -> None:
    with pytest.raises(ValidationError, match="approved_at"):
        RequirementRevision(
            project_id=uuid4(),
            revision=1,
            status=RequirementStatus.APPROVED,
            goal="Build a general DIY project",
        )


def test_unknown_compatibility_stays_explicit() -> None:
    finding = CompatibilityFinding(
        project_id=uuid4(),
        module_ids=(uuid4(),),
        rule_id="generic.interface.unknown",
        status=CompatibilityStatus.UNKNOWN,
        summary="The interface specification is not available.",
    )

    assert finding.status is CompatibilityStatus.UNKNOWN


def test_needs_test_compatibility_requires_test_instructions() -> None:
    with pytest.raises(ValidationError, match="required_test"):
        CompatibilityFinding(
            project_id=uuid4(),
            module_ids=(uuid4(),),
            rule_id="generic.physical.measurement",
            status=CompatibilityStatus.NEEDS_TEST,
            summary="Physical clearance requires a measurement.",
        )


def test_resolved_decision_is_bound_to_an_option() -> None:
    candidate_id = uuid4()
    evidence_id = uuid4()
    with pytest.raises(ValidationError, match="selected_option"):
        DecisionRequest(
            project_id=uuid4(),
            basis_hash=digest("basis"),
            question="Select a path",
            options=(
                DecisionOption(
                    option_id="option-a",
                    label="A",
                    summary="First bound option",
                    candidate_ids=(candidate_id,),
                    evidence_binding_ids=(evidence_id,),
                ),
                DecisionOption(
                    option_id="option-b",
                    label="B",
                    summary="Second bound option",
                    candidate_ids=(candidate_id,),
                    evidence_binding_ids=(evidence_id,),
                ),
            ),
            status=DecisionStatus.APPROVED,
            selected_option_id="option-c",
            resolved_at=datetime.now(UTC),
        )


def test_new_decision_option_requires_candidate_and_evidence_bindings() -> None:
    with pytest.raises(ValidationError, match="bind candidates and evidence"):
        DecisionOption(
            option_id="unbound",
            label="Unbound option",
            summary="A label is not sufficient as a decision identity.",
        )


def test_legacy_string_decision_options_remain_read_only() -> None:
    decision = DecisionRequest.model_validate(
        {
            "project_id": str(uuid4()),
            "basis_hash": digest("legacy-basis"),
            "question": "Historical choice",
            "options": ["A", "B"],
            "status": "approved",
            "selected_option": "B",
            "resolved_at": datetime.now(UTC).isoformat(),
        }
    )

    assert decision.selected_option_id == "legacy-2"
    assert all(option.legacy_unbound for option in decision.options)


def test_solution_version_is_immutable() -> None:
    solution = SolutionVersion(
        project_id=uuid4(),
        version=1,
        requirement_revision_id=uuid4(),
        basis_hash=digest("solution"),
        module_snapshots=({"key": "power", "selection": "candidate-a"},),
        approved_decision_id=uuid4(),
    )

    with pytest.raises(ValidationError):
        solution.version = 2  # type: ignore[misc]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "modules,error",
    [
        (
            (
                {"key": "power", "name": "Power", "responsibility": "Supply power"},
                {
                    "key": "control",
                    "name": "Control",
                    "responsibility": "Control the build",
                    "dependency_keys": ("missing",),
                },
            ),
            "unknown dependencies",
        ),
        (
            (
                {
                    "key": "power",
                    "name": "Power",
                    "responsibility": "Supply power",
                    "dependency_keys": ("control",),
                },
                {
                    "key": "control",
                    "name": "Control",
                    "responsibility": "Control the build",
                    "dependency_keys": ("power",),
                },
            ),
            "cycle",
        ),
        (
            (
                {
                    "key": "power",
                    "name": "Power",
                    "responsibility": "Supply power",
                    "dependency_keys": ("power",),
                },
            ),
            "cannot depend on itself",
        ),
    ],
)
async def test_requirement_module_graph_rejects_invalid_dependencies(
    modules: tuple[dict[str, object], ...],
    error: str,
) -> None:
    app = ProjectApplication(InMemoryDomainStore())
    project = await app.create_project(
        name="Graph fixture",
        goal="Validate a generic module graph",
        idempotency_key=f"project-{error}",
    )

    with pytest.raises(DomainConflictError, match=error):
        await app.approve_requirements(
            project_id=project.id,
            expected_project_revision=1,
            goal=project.goal,
            hard_constraints=(),
            preferences=(),
            available_resources=(),
            unknowns=(),
            modules=modules,
            idempotency_key=f"requirements-{error}",
        )

from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

import pytest
from pydantic import ValidationError

from aidison.api.schemas import (
    ApproveRequirementsRequest,
    CreateRequirementsChangeRequest,
)
from aidison.application.service import DomainConflictError, ProjectApplication
from aidison.domain.models import (
    AdjustmentBatch,
    AdjustmentBatchStatus,
    BlueprintStatus,
    Candidate,
    CompatibilityFinding,
    CompatibilityStatus,
    DecisionOption,
    DecisionRequest,
    DecisionStatus,
    ExecutionPlanProposal,
    ExecutionPlanStatus,
    ModuleConfiguration,
    ModuleConfigurationStatus,
    ProjectBlueprint,
    ProjectReshapeProposal,
    ProjectReshapeStatus,
    ProposedModule,
    RequirementRevision,
    RequirementsChangeProposal,
    RequirementStatus,
    SolutionVersion,
    UserAdjustment,
    UserAdjustmentKind,
)
from aidison.research.defaults import (
    RESEARCH_DEFAULT_MAX_DURATION_SECONDS,
    RESEARCH_DEFAULT_MAX_TOKEN_BUDGET,
)
from tests.fakes import InMemoryDomainStore


def digest(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def execution_plan_base() -> dict[str, object]:
    return {
        "project_id": uuid4(),
        "basis_hash": digest("execution-basis"),
        "objective": "核验供电、控制与预算约束",
        "work_summary": ("检索候选", "独立核验兼容性"),
        "allowed_coordination_modes": ("decompose", "verify", "replicate", "escalate"),
        "max_concurrency": 3,
        "max_token_budget": 12_000,
        "allowed_tool_classes": ("web_search", "github_read"),
        "allowed_effects": ("read",),
        "requires_result_approval": True,
    }


def test_approved_requirement_requires_approval_timestamp() -> None:
    with pytest.raises(ValidationError, match="approved_at"):
        RequirementRevision(
            project_id=uuid4(),
            revision=1,
            status=RequirementStatus.APPROVED,
            goal="Build a general DIY project",
        )


def test_requirement_revision_clarified_context_defaults_to_empty() -> None:
    # Old revisions constructed without the clarified-context fields stay valid.
    revision = RequirementRevision(
        project_id=uuid4(),
        revision=1,
        status=RequirementStatus.APPROVED,
        goal="Build a general DIY project",
        approved_at=datetime.now(UTC),
        available_resources=("已有底座",),
    )
    assert revision.usage_context == ""
    assert revision.budget_context == ""
    assert revision.skill_context == ""
    assert revision.available_resources == ("已有底座",)


def test_requirement_revision_clarified_context_round_trips() -> None:
    revision = RequirementRevision(
        project_id=uuid4(),
        revision=1,
        status=RequirementStatus.APPROVED,
        goal="Build a general DIY project",
        approved_at=datetime.now(UTC),
        usage_context="用于流水线分拣",
        budget_context="成本控制在 2 万元以内",
        skill_context="团队具备机械与电气基础",
    )
    decoded = RequirementRevision.model_validate(revision.model_dump(mode="json"))
    assert decoded.usage_context == "用于流水线分拣"
    assert decoded.budget_context == "成本控制在 2 万元以内"
    assert decoded.skill_context == "团队具备机械与电气基础"


def _change_proposal_payload() -> dict[str, object]:
    return {
        "project_id": uuid4(),
        "target_goal": "Build a general DIY project",
        "summary": "Adjust clarified context",
        "modules": (ProposedModule(key="core", name="Core", responsibility="Core logic"),),
    }


def test_requirements_change_proposal_clarified_context_defaults_to_empty() -> None:
    proposal = RequirementsChangeProposal(**_change_proposal_payload())
    assert proposal.usage_context == ""
    assert proposal.budget_context == ""
    assert proposal.skill_context == ""


def test_requirements_change_proposal_clarified_context_round_trips() -> None:
    payload = _change_proposal_payload()
    payload.update(
        {
            "usage_context": "用于流水线分拣",
            "budget_context": "成本控制在 2 万元以内",
            "skill_context": "团队具备机械与电气基础",
        }
    )
    proposal = RequirementsChangeProposal(**payload)
    decoded = RequirementsChangeProposal.model_validate(proposal.model_dump(mode="json"))
    assert decoded.usage_context == "用于流水线分拣"
    assert decoded.budget_context == "成本控制在 2 万元以内"
    assert decoded.skill_context == "团队具备机械与电气基础"


def test_approve_requirements_request_accepts_optional_clarified_context() -> None:
    request = ApproveRequirementsRequest(
        goal="Build a general DIY project",
        modules=(_module_input(),),
        usage_context="用于流水线分拣",
        budget_context="成本控制在 2 万元以内",
        skill_context="团队具备机械与电气基础",
        available_resources=("已有底座",),
    )
    assert request.usage_context == "用于流水线分拣"
    assert request.budget_context == "成本控制在 2 万元以内"
    assert request.skill_context == "团队具备机械与电气基础"
    assert request.available_resources == ("已有底座",)


def test_approve_requirements_request_defaults_clarified_context_to_empty() -> None:
    request = ApproveRequirementsRequest(goal="g", modules=(_module_input(),))
    assert request.usage_context == ""
    assert request.budget_context == ""
    assert request.skill_context == ""


def test_create_requirements_change_request_accepts_optional_clarified_context() -> None:
    request = CreateRequirementsChangeRequest(
        target_goal="g",
        summary="s",
        modules=(_module_input(),),
        budget_context="成本控制在 2 万元以内",
    )
    assert request.budget_context == "成本控制在 2 万元以内"
    assert request.usage_context == ""
    assert request.skill_context == ""


def test_api_request_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        ApproveRequirementsRequest(
            goal="g",
            modules=(_module_input(),),
            modules_skeleton="forbidden",  # type: ignore[call-arg]
        )


def _module_input() -> dict[str, str]:
    return {"key": "core", "name": "Core", "responsibility": "Core logic"}


@pytest.mark.asyncio
async def test_approve_requirements_persists_clarified_context_fields() -> None:
    store = InMemoryDomainStore()
    app = ProjectApplication(store)
    project = await app.create_project(
        name="Clarified context",
        goal="自动拾取物料的机械臂",
        idempotency_key="ctx-project",
    )
    requirement, _ = await app.approve_requirements(
        project_id=project.id,
        expected_project_revision=1,
        goal="自动拾取物料的机械臂",
        hard_constraints=(),
        preferences=(),
        available_resources=("已有机械臂底座",),
        usage_context="用于流水线自动分拣物料",
        budget_context="总成本控制在 2 万元以内",
        skill_context="团队具备机械与电气基础",
        unknowns=(),
        modules=(),
        idempotency_key="ctx-approve",
    )
    assert requirement.usage_context == "用于流水线自动分拣物料"
    assert requirement.budget_context == "总成本控制在 2 万元以内"
    assert requirement.skill_context == "团队具备机械与电气基础"

    read_back = await store.get_requirement_revision(requirement.id)
    assert read_back is not None
    assert read_back.usage_context == "用于流水线自动分拣物料"
    assert read_back.budget_context == "总成本控制在 2 万元以内"
    assert read_back.skill_context == "团队具备机械与电气基础"

    # budget_context stays a requirement fact only: no spend-budget artifact.
    assert store.spend_budget_proposals == {}
    assert store.spend_budget_revisions == {}


def test_unknown_compatibility_stays_explicit() -> None:
    finding = CompatibilityFinding(
        project_id=uuid4(),
        module_ids=(uuid4(),),
        rule_id="generic.interface.unknown",
        status=CompatibilityStatus.UNKNOWN,
        summary="The interface specification is not available.",
    )

    assert finding.status is CompatibilityStatus.UNKNOWN


def test_candidate_keeps_flexible_attributes_json_safe_and_evidence_unique() -> None:
    candidate = Candidate(
        project_id=uuid4(),
        module_id=uuid4(),
        name="ESC A",
        description="A bounded controller candidate.",
        attributes={"continuous_current_a": 35, "supports_telemetry": True},
        evidence_binding_ids=(uuid4(),),
    )

    assert candidate.attributes["continuous_current_a"] == 35

    evidence_id = uuid4()
    with pytest.raises(ValidationError, match="evidence bindings must be unique"):
        Candidate(
            project_id=uuid4(),
            module_id=uuid4(),
            name="Duplicate source",
            description="The same source must not count twice.",
            evidence_binding_ids=(evidence_id, evidence_id),
        )

    with pytest.raises(ValidationError, match="attributes must be JSON-serializable"):
        Candidate(
            project_id=uuid4(),
            module_id=uuid4(),
            name="Unpersistable property",
            description="A runtime object is not a candidate fact.",
            attributes={"runtime_handle": object()},
        )

    with pytest.raises(ValidationError, match="name and description must not be blank"):
        Candidate(
            project_id=uuid4(),
            module_id=uuid4(),
            name="   ",
            description="\t",
        )


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


def test_execution_plan_requires_a_resolved_approval_and_keeps_scope_bound() -> None:
    base = execution_plan_base()

    proposed = ExecutionPlanProposal(**base)
    assert proposed.status is ExecutionPlanStatus.PROPOSED
    assert proposed.authorizes_execution is False
    assert len(proposed.scope_hash) == 64

    with pytest.raises(ValidationError, match="resolved_at"):
        ExecutionPlanProposal(**base, status=ExecutionPlanStatus.APPROVED)

    approved = ExecutionPlanProposal(
        **base,
        status=ExecutionPlanStatus.APPROVED,
        resolved_at=datetime.now(UTC),
    )
    assert approved.authorizes_execution is True
    assert (
        approved.scope_hash
        != ExecutionPlanProposal(
            **{**base, "max_token_budget": 12_001},
        ).scope_hash
    )
    assert (
        proposed.scope_hash
        != ExecutionPlanProposal(
            **{**base, "requires_independent_verification": True},
        ).scope_hash
    )
    assert (
        proposed.scope_hash
        != ExecutionPlanProposal(
            **{**base, "max_duration_seconds": 3_600},
        ).scope_hash
    )


def test_execution_plan_has_permissive_run_limits_by_default() -> None:
    plan = ExecutionPlanProposal(
        **{
            key: value
            for key, value in execution_plan_base().items()
            if key != "max_token_budget"
        }
    )

    assert plan.max_token_budget == RESEARCH_DEFAULT_MAX_TOKEN_BUDGET
    assert plan.max_duration_seconds == RESEARCH_DEFAULT_MAX_DURATION_SECONDS


def test_execution_plan_rejects_unknown_coordination_modes() -> None:
    with pytest.raises(ValidationError, match="enum"):
        ExecutionPlanProposal(
            **{
                **execution_plan_base(),
                "allowed_coordination_modes": ("decompose", "nonsense"),
            }
        )


def test_execution_plan_rejects_duplicate_coordination_modes() -> None:
    with pytest.raises(ValidationError, match="unique"):
        ExecutionPlanProposal(
            **{
                **execution_plan_base(),
                "allowed_coordination_modes": ("decompose", "decompose"),
            }
        )


def test_proposed_execution_plan_cannot_have_resolution_time() -> None:
    with pytest.raises(ValidationError, match="cannot have resolved_at"):
        ExecutionPlanProposal(**execution_plan_base(), resolved_at=datetime.now(UTC))


def test_resolved_execution_plan_statuses_require_a_resolution_time() -> None:
    for status in (ExecutionPlanStatus.REJECTED, ExecutionPlanStatus.SUPERSEDED):
        with pytest.raises(ValidationError, match="resolved_at"):
            ExecutionPlanProposal(**execution_plan_base(), status=status)

        resolved = ExecutionPlanProposal(
            **execution_plan_base(),
            status=status,
            resolved_at=datetime.now(UTC),
        )
        assert resolved.status is status
        assert resolved.resolved_at is not None


def test_execution_plan_rejects_a_tampered_scope_hash() -> None:
    with pytest.raises(ValidationError, match="scope_hash"):
        ExecutionPlanProposal(**execution_plan_base(), scope_hash="a" * 64)


def test_execution_plan_scope_hash_is_deterministic_across_instances() -> None:
    base = execution_plan_base()
    first = ExecutionPlanProposal(**base)
    second = ExecutionPlanProposal(**base)

    assert first.scope_hash == second.scope_hash
    assert len(first.scope_hash) == 64


def test_blueprint_only_references_its_own_unique_modules() -> None:
    project_id = uuid4()
    module_id = uuid4()
    blueprint = ProjectBlueprint(
        project_id=project_id,
        requirement_revision_id=uuid4(),
        version=1,
        module_ids=(module_id,),
        status=BlueprintStatus.ACTIVE,
    )
    assert blueprint.status is BlueprintStatus.ACTIVE

    with pytest.raises(ValidationError, match="known modules"):
        ProjectBlueprint(
            project_id=project_id,
            requirement_revision_id=uuid4(),
            version=1,
            module_ids=(module_id,),
            dependency_edges=((module_id, uuid4()),),
        )


def test_draft_configuration_and_batch_lifecycle_are_append_only() -> None:
    configuration_id = uuid4()
    with pytest.raises(ValidationError, match="superseded"):
        ModuleConfiguration(
            id=configuration_id,
            project_id=uuid4(),
            module_id=uuid4(),
            revision=1,
            status=ModuleConfigurationStatus.SUPERSEDED,
        )

    adjustment = UserAdjustment(
        project_id=uuid4(),
        module_id=uuid4(),
        kind=UserAdjustmentKind.SET_PARAMETER,
        target={"propeller_diameter_mm": 127},
    )
    batch = AdjustmentBatch(
        project_id=adjustment.project_id,
        adjustment_ids=(adjustment.id,),
        affected_module_ids=(adjustment.module_id,),
    )
    assert batch.status is AdjustmentBatchStatus.OPEN
    with pytest.raises(ValidationError, match="flushed_at"):
        AdjustmentBatch(
            project_id=adjustment.project_id,
            adjustment_ids=(adjustment.id,),
            status=AdjustmentBatchStatus.FLUSHED,
        )


def test_reshape_is_proposal_until_explicitly_resolved() -> None:
    with pytest.raises(ValidationError, match="resolved_at"):
        ProjectReshapeProposal(
            project_id=uuid4(),
            target_goal="Add a power-monitoring module",
            summary="Move voltage sensing into an explicit module.",
            status=ProjectReshapeStatus.APPLIED,
        )


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

    await app.approve_requirements(
        project_id=project.id,
        expected_project_revision=1,
        goal=project.goal,
        hard_constraints=(),
        preferences=(),
        available_resources=(),
        unknowns=(),
        modules=(),
        idempotency_key=f"requirements-{error}",
    )
    with pytest.raises(DomainConflictError, match=error):
        await app.propose_project_reshape(
            proposal=ProjectReshapeProposal(
                project_id=project.id,
                target_goal=project.goal,
                summary="Validate the discovered module graph before applying it.",
                new_module_keys=tuple(str(item["key"]) for item in modules),
                new_modules=tuple(ProposedModule.model_validate(item) for item in modules),
            ),
            expected_project_revision=2,
            idempotency_key=f"reshape-{error}",
        )

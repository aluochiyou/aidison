from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID, uuid4

import pytest

from aidison.application.service import (
    DomainConflictError,
    PreconditionFailedError,
    ProjectApplication,
    canonical_hash,
)
from aidison.domain.models import (
    BomItem,
    Candidate,
    CompatibilityFinding,
    CompatibilityStatus,
    DecisionOption,
    EvidenceBinding,
    ImpactDisposition,
    ModulePatch,
    ModuleSelection,
    ProjectReshapeProposal,
    ProjectReshapeStatus,
    ProposedModule,
    SolutionDependency,
    SolutionDependencyKind,
    SolutionPlanStep,
)
from tests.fakes import InMemoryDomainStore


def test_canonical_hash_ignores_equivalent_timezone_implementations() -> None:
    evidence = EvidenceBinding(
        project_id=UUID("4dcc9b3b-daac-4507-a6e1-a11b4a85154b"),
        module_id=UUID("a3b0e149-3246-492f-9185-4e66031436a2"),
        claim="Equivalent timestamps are the same command payload.",
        source_url="https://example.com/source",
        snapshot_hash=sha256(b"source").hexdigest(),
        span_text="The timestamp has a zero UTC offset.",
        status="supported",
        observed_at=datetime(2026, 8, 1, 20, 0, tzinfo=UTC),
    )
    restored = EvidenceBinding.model_validate(evidence.model_dump(mode="json"))

    assert evidence == restored
    assert canonical_hash(evidence) == canonical_hash(restored)
    assert canonical_hash(evidence) == canonical_hash(evidence.model_dump(mode="json"))


@pytest.mark.asyncio
async def test_minimal_project_to_observation_patch_closed_loop() -> None:
    store = InMemoryDomainStore()
    app = ProjectApplication(store)
    project = await app.create_project(
        name="DIY camera",
        goal="Build a repairable camera",
        idempotency_key="project-1",
    )
    duplicate = await app.create_project(
        name="DIY camera",
        goal="Build a repairable camera",
        idempotency_key="project-1",
    )
    assert duplicate.id == project.id
    assert len(store.projects) == 1

    requirement, modules = await app.approve_requirements(
        project_id=project.id,
        expected_project_revision=1,
        goal=project.goal,
        hard_constraints=("local recording",),
        preferences=("repairable",),
        available_resources=("tripod",),
        unknowns=("sensor availability",),
        modules=(),
        idempotency_key="requirements-1",
    )
    assert modules == ()
    reshape = await app.propose_project_reshape(
        proposal=ProjectReshapeProposal(
            project_id=project.id,
            target_goal=project.goal,
            summary="Separate optics, compute, and storage into reviewable boundaries.",
            new_module_keys=("optics", "compute", "storage"),
            new_modules=(
                ProposedModule(key="optics", name="Optics", responsibility="Capture light"),
                ProposedModule(
                    key="compute",
                    name="Compute",
                    responsibility="Process frames",
                    dependency_keys=("optics",),
                ),
                ProposedModule(
                    key="storage",
                    name="Storage",
                    responsibility="Store frames independently",
                ),
            ),
        ),
        expected_project_revision=2,
        idempotency_key="project-1:initial-reshape",
    )
    await app.resolve_project_reshape(
        proposal_id=reshape.id,
        decision=ProjectReshapeStatus.APPLIED,
        expected_project_revision=2,
        idempotency_key="project-1:apply-initial-reshape",
    )
    modules = tuple(await store.list_modules(project.id, requirement_revision_id=requirement.id))
    evidence = EvidenceBinding(
        project_id=project.id,
        module_id=modules[0].id,
        claim="The selected mount exposes a documented flange distance.",
        source_url="https://example.com/mount-spec",
        snapshot_hash=sha256(b"mount-spec").hexdigest(),
        span_text="Flange distance: 17.526 mm",
        status="supported",
        observed_at=datetime.now(UTC),
    )
    candidate = Candidate(
        project_id=project.id,
        module_id=modules[0].id,
        name="Candidate mount",
        description="A documented interchangeable mount",
        evidence_binding_ids=(evidence.id,),
    )
    compute_candidate = Candidate(
        project_id=project.id,
        module_id=modules[1].id,
        name="Candidate processor",
        description="A repairable local frame processor",
    )
    alternate_candidate = Candidate(
        project_id=project.id,
        module_id=modules[0].id,
        name="Alternate mount",
        description="A documented alternate interchangeable mount",
        evidence_binding_ids=(evidence.id,),
    )
    storage_candidate = Candidate(
        project_id=project.id,
        module_id=modules[2].id,
        name="Candidate storage",
        description="Independent local storage",
    )
    finding = CompatibilityFinding(
        project_id=project.id,
        module_ids=(modules[0].id, modules[1].id),
        rule_id="generic.interface.documented",
        status=CompatibilityStatus.COMPATIBLE,
        summary="Both modules expose matching documented interfaces.",
        evidence_binding_ids=(evidence.id,),
    )
    needs_test_finding = CompatibilityFinding(
        project_id=project.id,
        module_ids=(modules[0].id, modules[1].id),
        rule_id="generic.interface.measurement",
        status=CompatibilityStatus.NEEDS_TEST,
        summary="The interface clearance needs a bench measurement.",
        evidence_binding_ids=(evidence.id,),
        required_test="Measure the assembled clearance before use.",
    )
    incompatible_finding = CompatibilityFinding(
        project_id=project.id,
        module_ids=(modules[0].id, modules[1].id),
        rule_id="generic.interface.incompatible",
        status=CompatibilityStatus.INCOMPATIBLE,
        summary="This deliberately incompatible route must never freeze.",
        evidence_binding_ids=(evidence.id,),
    )
    decision = await app.submit_research_proposal(
        project_id=project.id,
        expected_project_revision=3,
        evidence=(evidence,),
        candidates=(candidate, alternate_candidate, compute_candidate, storage_candidate),
        findings=(finding, needs_test_finding, incompatible_finding),
        decision_question="Freeze this route?",
        decision_options=(
            DecisionOption(
                option_id="approve",
                label="Freeze documented route",
                summary="Use the documented mount and current module candidates.",
                candidate_ids=(candidate.id, compute_candidate.id, storage_candidate.id),
                evidence_binding_ids=(evidence.id,),
            ),
            DecisionOption(
                option_id="alternate",
                label="Use alternate mount",
                summary="Keep the same compute and storage modules with the alternate mount.",
                candidate_ids=(
                    alternate_candidate.id,
                    compute_candidate.id,
                    storage_candidate.id,
                ),
                evidence_binding_ids=(evidence.id,),
            ),
        ),
        idempotency_key="research-1",
    )
    decision = await app.resolve_decision(
        decision_id=decision.id,
        expected_project_revision=4,
        selected_option_id="approve",
        basis_hash=decision.basis_hash,
        idempotency_key="decision-1",
    )
    selections = (
        ModuleSelection(
            module_id=modules[0].id,
            candidate_id=candidate.id,
            candidate_name=candidate.name,
            rationale="The mount has documented evidence.",
            evidence_binding_ids=(evidence.id,),
        ),
        ModuleSelection(
            module_id=modules[1].id,
            candidate_id=compute_candidate.id,
            candidate_name=compute_candidate.name,
            rationale="The processor satisfies local recording.",
        ),
        ModuleSelection(
            module_id=modules[2].id,
            candidate_id=storage_candidate.id,
            candidate_name=storage_candidate.name,
            rationale="The storage module is independent and locally repairable.",
        ),
    )
    bom = (
        BomItem(
            line_id="camera-mount",
            module_id=modules[0].id,
            candidate_id=candidate.id,
            name=candidate.name,
            quantity=1,
            unit="piece",
            evidence_binding_ids=(evidence.id,),
        ),
    )
    implementation_steps = (
        SolutionPlanStep(
            step_id="assemble-camera",
            title="Assemble modules",
            instruction="Mount the optics and processor.",
            module_ids=tuple(item.id for item in modules),
        ),
    )
    verification_steps = (
        SolutionPlanStep(
            step_id="record-frame",
            title="Record a test frame",
            instruction="Confirm focus and local recording.",
            module_ids=tuple(item.id for item in modules),
        ),
    )
    proposal_payload: dict[str, object] = {
        "project_id": project.id,
        "expected_project_revision": 5,
        "decision_id": decision.id,
        "evidence_binding_ids": (evidence.id,),
        "dependencies": (
            SolutionDependency(
                producer_module_id=modules[0].id,
                consumer_module_id=modules[1].id,
                kind=SolutionDependencyKind.DATA,
                interface_key="optics-to-compute",
                evidence_refs=(f"evidence-binding://{evidence.id}",),
            ),
        ),
        "dependency_projection_complete": True,
        "bom": bom,
        "implementation_steps": implementation_steps,
        "verification_steps": verification_steps,
        "risks": ("Mount availability can change.",),
        "unknowns": (),
        "consequences": ("The processor remains local-only.",),
        "artifact_ref": "artifact://solution-proposal",
        "profile_id": "solution-proposer",
        "profile_revision": 1,
    }
    with pytest.raises(DomainConflictError, match="select every active module once"):
        await app.submit_solution_proposal(
            **proposal_payload,
            module_selections=selections[:-1],
            compatibility_finding_ids=(finding.id,),
            idempotency_key="solution-proposal-missing-module",
        )
    with pytest.raises(DomainConflictError, match="select every active module once"):
        await app.submit_solution_proposal(
            **proposal_payload,
            module_selections=(*selections[:-1], selections[1]),
            compatibility_finding_ids=(finding.id,),
            idempotency_key="solution-proposal-duplicate-module",
        )
    with pytest.raises(DomainConflictError, match="project candidate"):
        await app.submit_solution_proposal(
            **proposal_payload,
            module_selections=(
                selections[0].model_copy(update={"candidate_id": uuid4()}),
                *selections[1:],
            ),
            compatibility_finding_ids=(finding.id,),
            idempotency_key="solution-proposal-unknown-candidate",
        )
    foreign_candidate = candidate.model_copy(update={"id": uuid4(), "project_id": uuid4()})
    store.candidates[foreign_candidate.id] = foreign_candidate
    with pytest.raises(DomainConflictError, match="project candidate"):
        await app.submit_solution_proposal(
            **proposal_payload,
            module_selections=(
                selections[0].model_copy(
                    update={
                        "candidate_id": foreign_candidate.id,
                        "candidate_name": foreign_candidate.name,
                    }
                ),
                *selections[1:],
            ),
            compatibility_finding_ids=(finding.id,),
            idempotency_key="solution-proposal-cross-project-candidate",
        )
    with pytest.raises(DomainConflictError, match="require verification steps"):
        await app.submit_solution_proposal(
            **{
                **proposal_payload,
                "verification_steps": (
                    SolutionPlanStep(
                        step_id="verify-storage-only",
                        title="Verify storage only",
                        instruction="This does not cover the modules that need measurement.",
                        module_ids=(modules[2].id,),
                    ),
                ),
            },
            module_selections=selections,
            compatibility_finding_ids=(needs_test_finding.id,),
            idempotency_key="solution-proposal-missing-required-verification",
        )
    proposal = await app.submit_solution_proposal(
        **proposal_payload,
        module_selections=selections,
        compatibility_finding_ids=(finding.id,),
        idempotency_key="solution-proposal-1",
    )
    incompatible_proposal = await app.submit_solution_proposal(
        **{
            **proposal_payload,
            "expected_project_revision": 6,
            "artifact_ref": "artifact://incompatible-solution-proposal",
        },
        module_selections=selections,
        compatibility_finding_ids=(incompatible_finding.id,),
        idempotency_key="solution-proposal-incompatible",
    )
    with pytest.raises(DomainConflictError, match="incompatible finding"):
        await app.freeze_solution(
            project_id=project.id,
            expected_project_revision=7,
            solution_proposal_id=incompatible_proposal.id,
            basis_hash=incompatible_proposal.basis_hash,
            idempotency_key="solution-incompatible",
        )
    with pytest.raises(PreconditionFailedError, match="basis is stale"):
        await app.freeze_solution(
            project_id=project.id,
            expected_project_revision=7,
            solution_proposal_id=proposal.id,
            basis_hash=sha256(b"stale-solution-basis").hexdigest(),
            idempotency_key="solution-stale-basis",
        )
    solution = await app.freeze_solution(
        project_id=project.id,
        expected_project_revision=7,
        solution_proposal_id=proposal.id,
        basis_hash=proposal.basis_hash,
        idempotency_key="solution-1",
    )
    observation = await app.submit_observation(
        project_id=project.id,
        expected_project_revision=8,
        statement="The optics module does not reach focus.",
        affected_module_ids=(modules[0].id,),
        idempotency_key="observation-1",
    )
    with pytest.raises(DomainConflictError, match="unaffected"):
        await app.submit_impact_analysis(
            project_id=project.id,
            expected_project_revision=9,
            observation_id=observation.id,
            module_patches=(
                ModulePatch(
                    module_id=modules[2].id,
                    base_snapshot_hash=solution.module_snapshots[2]["snapshot_hash"],
                    replacement=ModuleSelection(
                        module_id=modules[2].id,
                        candidate_id=storage_candidate.id,
                        candidate_name=storage_candidate.name,
                        rationale="An Agent must not rewrite an unaffected module.",
                    ),
                ),
            ),
            stale_evidence_binding_ids=(),
            replacement_bom_items=(),
            replacement_implementation_steps=(),
            replacement_verification_steps=(),
            summary="Invalid unaffected patch",
            risks=(),
            artifact_ref="artifact://invalid-impact-proposal",
            profile_id="impact-proposer-ro",
            profile_revision=1,
            idempotency_key="invalid-impact-proposal-1",
        )
    impact = await app.submit_impact_analysis(
        project_id=project.id,
        expected_project_revision=9,
        observation_id=observation.id,
        module_patches=(
            ModulePatch(
                module_id=modules[0].id,
                base_snapshot_hash=solution.module_snapshots[0]["snapshot_hash"],
                replacement=ModuleSelection(
                    module_id=modules[0].id,
                    candidate_id=alternate_candidate.id,
                    candidate_name=alternate_candidate.name,
                    rationale="The alternate mount addresses the observed focus issue.",
                    evidence_binding_ids=(evidence.id,),
                ),
            ),
        ),
        stale_evidence_binding_ids=(),
        replacement_bom_items=(
            BomItem(
                line_id="alternate-camera-mount",
                module_id=modules[0].id,
                candidate_id=alternate_candidate.id,
                name=alternate_candidate.name,
                quantity=1,
                unit="piece",
                evidence_binding_ids=(evidence.id,),
            ),
        ),
        replacement_implementation_steps=(
            SolutionPlanStep(
                step_id="replace-mount",
                title="Replace the mount",
                instruction="Install the accepted alternate mount.",
                module_ids=(modules[0].id,),
            ),
        ),
        replacement_verification_steps=(
            SolutionPlanStep(
                step_id="verify-focus",
                title="Verify focus",
                instruction="Confirm the optics now reaches focus.",
                module_ids=(modules[0].id,),
                acceptance=("A test frame reaches focus",),
            ),
        ),
        summary="Replace only the failed mount and preserve independent storage.",
        risks=("Bench focus verification remains required.",),
        artifact_ref="artifact://impact-proposal",
        profile_id="impact-proposer-ro",
        profile_revision=1,
        idempotency_key="impact-proposal-1",
    )
    assert impact.direct_affected_module_ids == (modules[0].id,)
    assert impact.transitive_affected_module_ids == (modules[1].id,)
    assert impact.affected_module_ids == (modules[0].id, modules[1].id)
    assert impact.unaffected_module_ids == (modules[2].id,)
    assert impact.dependency_paths[1].module_path == (modules[0].id, modules[1].id)
    assert impact.dependency_paths[1].interface_keys == ("optics-to-compute",)
    classifications = {item.module_id: item for item in impact.classifications}
    assert classifications[modules[0].id].disposition is ImpactDisposition.REVERIFY
    assert classifications[modules[1].id].semantic_analysis_required is True
    _, revised = await app.approve_impact_and_patch(
        impact_id=impact.id,
        expected_project_revision=10,
        basis_hash=impact.basis_hash,
        idempotency_key="patch-1",
    )

    assert requirement.status == "approved"
    assert solution.version == 1
    assert revised.version == 2
    assert revised.previous_version_id == solution.id
    assert (
        revised.module_snapshots[0]["snapshot_hash"]
        != solution.module_snapshots[0]["snapshot_hash"]
    )
    assert revised.module_snapshots[2] == solution.module_snapshots[2]
    assert (
        revised.module_snapshots[2]["snapshot_hash"]
        == solution.module_snapshots[2]["snapshot_hash"]
    )
    assert store.events[-1][2] == "solution.revised"

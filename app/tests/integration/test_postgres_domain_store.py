from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from aidison.application.ports import DuplicateCommandError
from aidison.application.service import ProjectApplication
from aidison.domain.models import (
    BomItem,
    Candidate,
    CompatibilityFinding,
    CompatibilityStatus,
    DecisionOption,
    EvidenceBinding,
    ExecutionPlanProposal,
    ModulePatch,
    ModuleSelection,
    ProjectReshapeProposal,
    ProjectReshapeStatus,
    ProposedModule,
    SolutionPlanStep,
)
from aidison.infrastructure.database import (
    DatabaseSettings,
    create_engine,
    create_session_factory,
)
from aidison.infrastructure.orm import (
    CommandReceiptRow,
    DomainEventRow,
    ProjectRow,
    SolutionVersionRow,
)
from aidison.infrastructure.store import PostgresDomainStore
from aidison.runtime.contracts import CoordinationMode

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_execution_plan_list_is_ordered_by_creation_time_not_uuid() -> None:
    """A newly generated strategy must be the deterministic current proposal.

    The UI uses the newest plan as the default plan to review/start.  UUID
    ordering is unrelated to user intent, so the repository contract must
    expose creation order explicitly.
    """

    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")

    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    key_prefix = str(uuid4())
    created_at = datetime(2026, 9, 12, tzinfo=UTC)
    try:
        async with factory() as session:
            store = PostgresDomainStore(session)
            project = await ProjectApplication(store).create_project(
                name="Execution plan ordering",
                goal="Keep the newest reviewed strategy selectable.",
                idempotency_key=f"{key_prefix}:project",
            )
            newer = ExecutionPlanProposal(
                project_id=project.id,
                basis_hash="b" * 64,
                objective="Newer strategy",
                work_summary=("new",),
                allowed_coordination_modes=(CoordinationMode.DECOMPOSE,),
                max_concurrency=1,
                max_token_budget=1_000,
                created_at=created_at + timedelta(minutes=1),
            )
            older = ExecutionPlanProposal(
                project_id=project.id,
                basis_hash="a" * 64,
                objective="Older strategy",
                work_summary=("old",),
                allowed_coordination_modes=(CoordinationMode.DECOMPOSE,),
                max_concurrency=1,
                max_token_budget=1_000,
                created_at=created_at,
            )
            # Deliberately write in the opposite temporal order.  The test
            # must not depend on insertion order or random UUID ordering.
            await store.add_execution_plan_proposal(newer)
            await store.add_execution_plan_proposal(older)
            await store.commit()

            plans = await store.list_execution_plan_proposals(project.id)

            assert [item.id for item in plans] == [older.id, newer.id]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_postgres_closed_loop_is_idempotent_and_immutable() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")

    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    key_prefix = str(uuid4())
    try:
        async with factory() as session:
            app = ProjectApplication(PostgresDomainStore(session))
            project = await app.create_project(
                name="DIY camera",
                goal="Build a repairable camera",
                idempotency_key=f"{key_prefix}:project",
            )
            duplicate_project = await app.create_project(
                name="DIY camera",
                goal="Build a repairable camera",
                idempotency_key=f"{key_prefix}:project",
            )
            assert duplicate_project.id == project.id

            requirement, modules = await app.approve_requirements(
                project_id=project.id,
                expected_project_revision=1,
                goal=project.goal,
                hard_constraints=("local recording",),
                preferences=("repairable",),
                available_resources=("tripod",),
                unknowns=("sensor availability",),
                modules=(),
                idempotency_key=f"{key_prefix}:requirements",
            )
            duplicate_requirement, duplicate_modules = await app.approve_requirements(
                project_id=project.id,
                expected_project_revision=1,
                goal=project.goal,
                hard_constraints=("local recording",),
                preferences=("repairable",),
                available_resources=("tripod",),
                unknowns=("sensor availability",),
                modules=(),
                idempotency_key=f"{key_prefix}:requirements",
            )
            assert duplicate_requirement.id == requirement.id
            assert {item.id for item in duplicate_modules} == {item.id for item in modules}

            reshape = await app.propose_project_reshape(
                proposal=ProjectReshapeProposal(
                    project_id=project.id,
                    target_goal=project.goal,
                    summary="Create the reviewed initial camera modules.",
                    new_modules=(
                        ProposedModule(
                            key="optics",
                            name="Optics",
                            responsibility="Capture light",
                        ),
                        ProposedModule(
                            key="compute",
                            name="Compute",
                            responsibility="Process frames",
                        ),
                    ),
                ),
                expected_project_revision=2,
                idempotency_key=f"{key_prefix}:reshape-propose",
            )
            await app.resolve_project_reshape(
                proposal_id=reshape.id,
                decision=ProjectReshapeStatus.APPLIED,
                expected_project_revision=2,
                idempotency_key=f"{key_prefix}:reshape-apply",
            )
            modules = tuple(await PostgresDomainStore(session).list_modules(project.id))

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
            finding = CompatibilityFinding(
                project_id=project.id,
                module_ids=(modules[0].id, modules[1].id),
                rule_id="generic.interface.documented",
                status=CompatibilityStatus.COMPATIBLE,
                summary="Both modules expose matching documented interfaces.",
                evidence_binding_ids=(evidence.id,),
            )
            decision_options = (
                DecisionOption(
                    option_id="approve",
                    label="Freeze documented route",
                    summary="Use the documented mount and processor candidates.",
                    candidate_ids=(candidate.id, compute_candidate.id),
                    evidence_binding_ids=(evidence.id,),
                ),
                DecisionOption(
                    option_id="alternate",
                    label="Use alternate mount",
                    summary="Use the alternate mount with the documented processor.",
                    candidate_ids=(alternate_candidate.id, compute_candidate.id),
                    evidence_binding_ids=(evidence.id,),
                ),
            )
            decision = await app.submit_research_proposal(
                project_id=project.id,
                expected_project_revision=3,
                evidence=(evidence,),
                candidates=(candidate, alternate_candidate, compute_candidate),
                findings=(finding,),
                decision_question="Freeze this route?",
                decision_options=decision_options,
                idempotency_key=f"{key_prefix}:research",
            )
            duplicate_decision = await app.submit_research_proposal(
                project_id=project.id,
                expected_project_revision=3,
                evidence=(evidence,),
                candidates=(candidate, alternate_candidate, compute_candidate),
                findings=(finding,),
                decision_question="Freeze this route?",
                decision_options=decision_options,
                idempotency_key=f"{key_prefix}:research",
            )
            assert duplicate_decision.id == decision.id

            resolved = await app.resolve_decision(
                decision_id=decision.id,
                expected_project_revision=4,
                selected_option_id="approve",
                basis_hash=decision.basis_hash,
                idempotency_key=f"{key_prefix}:decision",
            )
            duplicate_resolved = await app.resolve_decision(
                decision_id=decision.id,
                expected_project_revision=4,
                selected_option_id="approve",
                basis_hash=decision.basis_hash,
                idempotency_key=f"{key_prefix}:decision",
            )
            assert duplicate_resolved == resolved

            module_selections = (
                ModuleSelection(
                    module_id=modules[0].id,
                    candidate_id=candidate.id,
                    candidate_name=candidate.name,
                    rationale="Documented evidence-backed selection.",
                    evidence_binding_ids=(evidence.id,),
                ),
                ModuleSelection(
                    module_id=modules[1].id,
                    candidate_id=compute_candidate.id,
                    candidate_name=compute_candidate.name,
                    rationale="Repairable local processing selection.",
                ),
            )

            proposal = await app.submit_solution_proposal(
                project_id=project.id,
                expected_project_revision=5,
                decision_id=decision.id,
                module_selections=module_selections,
                evidence_binding_ids=(evidence.id,),
                compatibility_finding_ids=(finding.id,),
                bom=(
                    BomItem(
                        line_id="camera-mount",
                        module_id=modules[0].id,
                        candidate_id=candidate.id,
                        name=candidate.name,
                        quantity=1,
                        unit="piece",
                        evidence_binding_ids=(evidence.id,),
                    ),
                ),
                implementation_steps=(
                    SolutionPlanStep(
                        step_id="assemble",
                        title="Assemble modules",
                        instruction="Mount the optics and compute modules.",
                        module_ids=tuple(module.id for module in modules),
                    ),
                ),
                verification_steps=(
                    SolutionPlanStep(
                        step_id="bench-test",
                        title="Record a test frame",
                        instruction="Confirm focus and local recording.",
                        module_ids=tuple(module.id for module in modules),
                    ),
                ),
                risks=(),
                unknowns=(),
                consequences=("The route remains repairable.",),
                artifact_ref="artifact://integration-solution-proposal",
                profile_id="solution-proposer",
                profile_revision=1,
                idempotency_key=f"{key_prefix}:solution-proposal",
            )
            duplicate_proposal = await app.submit_solution_proposal(
                project_id=project.id,
                expected_project_revision=5,
                decision_id=decision.id,
                module_selections=module_selections,
                evidence_binding_ids=(evidence.id,),
                compatibility_finding_ids=(finding.id,),
                bom=(
                    BomItem(
                        line_id="camera-mount",
                        module_id=modules[0].id,
                        candidate_id=candidate.id,
                        name=candidate.name,
                        quantity=1,
                        unit="piece",
                        evidence_binding_ids=(evidence.id,),
                    ),
                ),
                implementation_steps=(
                    SolutionPlanStep(
                        step_id="assemble",
                        title="Assemble modules",
                        instruction="Mount the optics and compute modules.",
                        module_ids=tuple(module.id for module in modules),
                    ),
                ),
                verification_steps=(
                    SolutionPlanStep(
                        step_id="bench-test",
                        title="Record a test frame",
                        instruction="Confirm focus and local recording.",
                        module_ids=tuple(module.id for module in modules),
                    ),
                ),
                risks=(),
                unknowns=(),
                consequences=("The route remains repairable.",),
                artifact_ref="artifact://integration-solution-proposal",
                profile_id="solution-proposer",
                profile_revision=1,
                idempotency_key=f"{key_prefix}:solution-proposal",
            )
            assert duplicate_proposal.id == proposal.id

            solution = await app.freeze_solution(
                project_id=project.id,
                expected_project_revision=6,
                solution_proposal_id=proposal.id,
                basis_hash=proposal.basis_hash,
                idempotency_key=f"{key_prefix}:solution",
            )
            duplicate_solution = await app.freeze_solution(
                project_id=project.id,
                expected_project_revision=6,
                solution_proposal_id=proposal.id,
                basis_hash=proposal.basis_hash,
                idempotency_key=f"{key_prefix}:solution",
            )
            assert duplicate_solution.id == solution.id

            observation = await app.submit_observation(
                project_id=project.id,
                expected_project_revision=7,
                statement="The optics module does not reach focus.",
                affected_module_ids=(modules[0].id,),
                idempotency_key=f"{key_prefix}:observation",
            )
            duplicate_observation = await app.submit_observation(
                project_id=project.id,
                expected_project_revision=7,
                statement="The optics module does not reach focus.",
                affected_module_ids=(modules[0].id,),
                idempotency_key=f"{key_prefix}:observation",
            )
            assert duplicate_observation.id == observation.id

            impact_kwargs = {
                "project_id": project.id,
                    "expected_project_revision": 8,
                "observation_id": observation.id,
                "module_patches": (
                    ModulePatch(
                        module_id=modules[0].id,
                        base_snapshot_hash=solution.module_snapshots[0]["snapshot_hash"],
                        replacement=ModuleSelection(
                            module_id=modules[0].id,
                            candidate_id=alternate_candidate.id,
                            candidate_name=alternate_candidate.name,
                            rationale="Use the alternate documented mount.",
                            evidence_binding_ids=(evidence.id,),
                        ),
                    ),
                ),
                "stale_evidence_binding_ids": (),
                "replacement_bom_items": (
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
                "replacement_implementation_steps": (
                    SolutionPlanStep(
                        step_id="replace-mount",
                        title="Replace mount",
                        instruction="Install the alternate mount.",
                        module_ids=(modules[0].id,),
                    ),
                ),
                "replacement_verification_steps": (
                    SolutionPlanStep(
                        step_id="verify-focus",
                        title="Verify focus",
                        instruction="Record a focused test frame.",
                        module_ids=(modules[0].id,),
                    ),
                ),
                "summary": "Replace the failed mount.",
                "risks": ("Bench verification remains required.",),
                "artifact_ref": "artifact://integration-impact-proposal",
                "profile_id": "impact-proposer-ro",
                "profile_revision": 1,
                "idempotency_key": f"{key_prefix}:impact-proposal",
            }
            impact = await app.submit_impact_analysis(**impact_kwargs)
            duplicate_impact = await app.submit_impact_analysis(**impact_kwargs)
            assert duplicate_impact.id == impact.id

            patch_set, revised = await app.approve_impact_and_patch(
                impact_id=impact.id,
                expected_project_revision=9,
                basis_hash=impact.basis_hash,
                idempotency_key=f"{key_prefix}:patch",
            )
            duplicate_patch, duplicate_revised = await app.approve_impact_and_patch(
                impact_id=impact.id,
                expected_project_revision=9,
                basis_hash=impact.basis_hash,
                idempotency_key=f"{key_prefix}:patch",
            )
            assert duplicate_patch.id == patch_set.id
            assert duplicate_revised.id == revised.id

            stored_project = await session.get(ProjectRow, project.id)
            assert stored_project is not None
            assert stored_project.revision == 10
            assert stored_project.event_sequence == 11
            assert stored_project.active_solution_version_id == revised.id
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(CommandReceiptRow)
                    .where(CommandReceiptRow.idempotency_key.like(f"{key_prefix}:%"))
                )
                == 11
            )
            event_sequences = list(
                await session.scalars(
                    select(DomainEventRow.project_seq)
                    .where(DomainEventRow.project_id == project.id)
                    .order_by(DomainEventRow.project_seq)
                )
            )
            assert event_sequences == list(range(1, 12))

            with pytest.raises(DuplicateCommandError):
                await app.create_project(
                    name="Different payload",
                    goal="Must be rejected",
                    idempotency_key=f"{key_prefix}:project",
                )
            await session.rollback()

        async with factory() as immutable_session:
            with pytest.raises(DBAPIError, match="SolutionVersion is immutable"):
                await immutable_session.execute(
                    text("UPDATE solution_versions SET version = 99 WHERE id = :id"),
                    {"id": solution.id},
                )
            await immutable_session.rollback()

        async with factory() as verify_session:
            versions = list(
                await verify_session.scalars(
                    select(SolutionVersionRow).where(SolutionVersionRow.project_id == project.id)
                )
            )
            assert {item.version for item in versions} == {1, 2}
    finally:
        await engine.dispose()

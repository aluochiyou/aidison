"""Application tests for requirements-change proposals and change impact previews."""

from __future__ import annotations

import pytest

from aidison.application.service import (
    DomainConflictError,
    ProjectApplication,
)
from aidison.domain.models import (
    Candidate,
    ChangeImpactPreview,
    DecisionOption,
    DecisionRequest,
    EvidenceBinding,
    EvidenceStatus,
    ProposedModule,
    RequirementsChangeProposal,
    RequirementsChangeProposalStatus,
    UserAdjustmentKind,
)
from tests.fakes import InMemoryDomainStore, bootstrap_initial_modules


class _PreviewWriteFailureStore(InMemoryDomainStore):
    """Records commits so the test can prove the flush has no early commit."""

    def __init__(self) -> None:
        super().__init__()
        self.commit_count = 0
        self.fail_preview_write = False

    async def add_change_impact_preview(self, preview: ChangeImpactPreview) -> None:
        if self.fail_preview_write:
            raise RuntimeError("injected impact-preview write failure")
        await super().add_change_impact_preview(preview)

    async def commit(self) -> None:
        self.commit_count += 1


async def _project_with_modules(
    store: InMemoryDomainStore, *, extra_module: bool = False
) -> tuple[object, tuple]:
    app = ProjectApplication(store)
    project = await app.create_project(
        name="Governance fixture",
        goal="Verify governed requirement rewrites",
        idempotency_key="governance:project",
    )
    module_drafts = [{"key": "power", "name": "Power", "responsibility": "Supply power"}]
    if extra_module:
        module_drafts.append(
            {
                "key": "control",
                "name": "Control",
                "responsibility": "Control the build",
                "dependency_keys": ("power",),
            }
        )
    current, modules = await bootstrap_initial_modules(
        store=store,
        application=app,
        project=project,
        modules=module_drafts,
        idempotency_key_prefix="governance",
    )
    return current, modules


@pytest.mark.asyncio
async def test_requirements_change_stays_proposed_until_explicitly_applied() -> None:
    store = InMemoryDomainStore()
    app = ProjectApplication(store)
    project, _ = await _project_with_modules(store)
    current = await store.get_project(project.id)
    assert current is not None and current.revision == 3

    proposed = await app.propose_requirements_change(
        proposal=RequirementsChangeProposal(
            project_id=project.id,
            basis_requirement_revision_id=current.active_requirement_revision_id,
            basis_blueprint_id=current.active_blueprint_id,
            target_goal="Rewritten goal",
            hard_constraints=("must not exceed 5 kg",),
            preferences=(),
            available_resources=(),
            unknowns=(),
            summary="Rewrite the project requirements",
            modules=(ProposedModule(key="power", name="Power", responsibility="Supply power"),),
        ),
        expected_project_revision=3,
        idempotency_key="governance:propose-change",
    )
    assert proposed.status is RequirementsChangeProposalStatus.PROPOSED

    # Nothing changed until the proposal is applied: no new requirement
    # revision, no new module, blueprint untouched.
    updated = await store.get_project(project.id)
    assert updated is not None and updated.revision == 3
    assert updated.active_requirement_revision_id == current.active_requirement_revision_id
    assert len(await store.list_requirement_revisions(project.id)) == 1
    assert [item.key for item in await store.list_modules(project.id)] == ["power"]
    assert "requirements_change.proposed" in {event[2] for event in store.events}

    applied = await app.resolve_requirements_change(
        proposal_id=proposed.id,
        decision=RequirementsChangeProposalStatus.APPLIED,
        expected_project_revision=3,
        idempotency_key="governance:apply-change",
    )
    assert applied.status is RequirementsChangeProposalStatus.APPLIED

    after = await store.get_project(project.id)
    assert after is not None and after.revision == 4
    revisions = await store.list_requirement_revisions(project.id)
    assert len(revisions) == 2
    active = await store.get_requirement_revision(after.active_requirement_revision_id)
    assert active is not None
    assert active.goal == "Rewritten goal"
    assert active.hard_constraints == ("must not exceed 5 kg",)
    assert "requirements_change.applied" in {event[2] for event in store.events}


@pytest.mark.asyncio
async def test_requirements_change_reject_does_not_advance_project() -> None:
    store = InMemoryDomainStore()
    app = ProjectApplication(store)
    project, _ = await _project_with_modules(store)
    current = await store.get_project(project.id)
    assert current is not None and current.revision == 3

    proposed = await app.propose_requirements_change(
        proposal=RequirementsChangeProposal(
            project_id=project.id,
            basis_requirement_revision_id=current.active_requirement_revision_id,
            basis_blueprint_id=current.active_blueprint_id,
            target_goal="Rewritten goal",
            summary="Rewrite the project requirements",
            modules=(ProposedModule(key="power", name="Power", responsibility="Supply power"),),
        ),
        expected_project_revision=3,
        idempotency_key="governance:reject-propose",
    )
    rejected = await app.resolve_requirements_change(
        proposal_id=proposed.id,
        decision=RequirementsChangeProposalStatus.REJECTED,
        expected_project_revision=3,
        idempotency_key="governance:reject-change",
    )
    assert rejected.status is RequirementsChangeProposalStatus.REJECTED
    after = await store.get_project(project.id)
    assert after is not None and after.revision == 3
    assert len(await store.list_requirement_revisions(project.id)) == 1
    assert "requirements_change.rejected" in {event[2] for event in store.events}


@pytest.mark.asyncio
async def test_requirements_change_proposal_rejects_bad_module_graph() -> None:
    store = InMemoryDomainStore()
    app = ProjectApplication(store)
    project, _ = await _project_with_modules(store)
    current = await store.get_project(project.id)
    assert current is not None and current.revision == 3

    with pytest.raises(DomainConflictError, match="unknown dependencies"):
        await app.propose_requirements_change(
            proposal=RequirementsChangeProposal(
                project_id=project.id,
                basis_requirement_revision_id=current.active_requirement_revision_id,
                basis_blueprint_id=current.active_blueprint_id,
                target_goal="Rewritten goal",
                summary="Rewrite",
                modules=(
                    ProposedModule(
                        key="core",
                        name="Core",
                        responsibility="Core",
                        dependency_keys=("missing",),
                    ),
                ),
            ),
            expected_project_revision=3,
            idempotency_key="governance:bad-graph",
        )


@pytest.mark.asyncio
async def test_change_impact_preview_is_read_only_and_traceable() -> None:
    store = InMemoryDomainStore()
    app = ProjectApplication(store)
    project, modules = await _project_with_modules(store, extra_module=True)
    power = next(item for item in modules if item.key == "power")
    control = next(item for item in modules if item.key == "control")
    current = await store.get_project(project.id)
    assert current is not None and current.revision == 3

    # A pending decision over the affected module will be marked invalidated.
    locked_candidate = Candidate(
        project_id=project.id,
        module_id=power.id,
        name="Locked battery",
        description="User-favored battery",
    )
    alternate_candidate = Candidate(
        project_id=project.id,
        module_id=power.id,
        name="Alternate battery",
        description="Alternative battery",
    )
    await store.add_candidates((locked_candidate, alternate_candidate))
    evidence = EvidenceBinding(
        project_id=project.id,
        module_id=power.id,
        claim="Battery chemistry claim",
        source_url="https://example.com",
        snapshot_hash="b" * 64,
        span_text="span",
        status=EvidenceStatus.SUPPORTED,
        observed_at=current.created_at,
    )
    await store.add_evidence_bindings((evidence,))
    pending_decision = DecisionRequest(
        project_id=project.id,
        basis_hash="c" * 64,
        question="Which power system?",
        affected_module_ids=(power.id, control.id),
        options=(
            DecisionOption(
                option_id="opt-locked",
                label="Locked",
                summary="Locked battery",
                candidate_ids=(locked_candidate.id,),
                evidence_binding_ids=(evidence.id,),
            ),
            DecisionOption(
                option_id="opt-alt",
                label="Alternate",
                summary="Alternate battery",
                candidate_ids=(alternate_candidate.id,),
                evidence_binding_ids=(evidence.id,),
            ),
        ),
    )
    await store.add_decision_request(pending_decision)

    adjustment = await app.record_user_adjustment(
        project_id=project.id,
        module_id=power.id,
        kind=UserAdjustmentKind.SELECT_CANDIDATE,
        target={"candidate_id": str(alternate_candidate.id)},
        batch_window_seconds=2.0,
        expected_project_revision=3,
        idempotency_key="governance:select-alt",
    )
    assert adjustment.batch_id is not None
    batch = await store.get_adjustment_batch(adjustment.batch_id)
    assert batch is not None

    flushed = await app.flush_adjustment_batch(
        batch_id=batch.id,
        expected_project_revision=4,
        idempotency_key="governance:flush",
    )
    assert flushed.status.value == "flushed"

    previews = await store.list_change_impact_previews(project.id)
    assert len(previews) == 1
    preview = previews[0]
    assert preview.batch_id == batch.id
    assert set(preview.affected_module_ids) == {power.id, control.id}
    assert preview.direct_affected_module_ids == (power.id,)
    assert preview.transitive_affected_module_ids == (control.id,)
    assert preview.unaffected_module_ids == ()
    kinds = {ref.kind for ref in preview.invalidated_refs}
    assert "decision" in kinds
    assert any(ref.entity_id == pending_decision.id for ref in preview.invalidated_refs)
    assert "change_impact_preview.generated" in {event[2] for event in store.events}

    # Read-only: nothing beyond the draft changed. Locks, revision, blueprint
    # and the pending decision are untouched.
    after = await store.get_project(project.id)
    assert after is not None and after.revision == 5
    assert after.active_blueprint_id == current.active_blueprint_id
    stored_decision = await store.get_decision_request(pending_decision.id)
    assert stored_decision is not None
    assert stored_decision.status.value == "pending"


@pytest.mark.asyncio
async def test_generate_change_impact_preview_is_idempotent() -> None:
    store = InMemoryDomainStore()
    app = ProjectApplication(store)
    project, modules = await _project_with_modules(store)
    power = modules[0]
    adjustment = await app.record_user_adjustment(
        project_id=project.id,
        module_id=power.id,
        kind=UserAdjustmentKind.SET_PARAMETER,
        target={"key": "capacity_mah", "value": 2200},
        batch_window_seconds=2.0,
        expected_project_revision=3,
        idempotency_key="governance:param",
    )
    assert adjustment.batch_id is not None
    first = await app.generate_change_impact_preview(
        batch_id=adjustment.batch_id,
        expected_project_revision=4,
        idempotency_key="governance:preview-idem",
    )
    second = await app.generate_change_impact_preview(
        batch_id=adjustment.batch_id,
        expected_project_revision=4,
        idempotency_key="governance:preview-idem",
    )
    assert first.id == second.id
    assert len(await store.list_change_impact_previews(project.id)) == 1


@pytest.mark.asyncio
async def test_generate_preview_rejects_empty_batch() -> None:
    store = InMemoryDomainStore()
    app = ProjectApplication(store)
    project, modules = await _project_with_modules(store)
    power = modules[0]
    await app.record_user_adjustment(
        project_id=project.id,
        module_id=power.id,
        kind=UserAdjustmentKind.SET_PARAMETER,
        target={"key": "capacity_mah", "value": 2200},
        batch_window_seconds=2.0,
        expected_project_revision=3,
        idempotency_key="governance:param-2",
    )
    batch = next(iter(await store.list_adjustment_batches(project.id)))
    flushed = await app.flush_adjustment_batch(
        batch_id=batch.id,
        expected_project_revision=4,
        idempotency_key="governance:flush-2",
    )
    assert flushed.status.value == "flushed"

    # A second, empty batch (no adjustments) must fail preview generation.
    from aidison.domain.models import AdjustmentBatch

    await store.add_adjustment_batch(AdjustmentBatch(project_id=project.id))
    empty = await store.list_adjustment_batches(project.id)
    empty_batch = next(item for item in empty if item.adjustment_ids == ())
    with pytest.raises(DomainConflictError, match="empty"):
        await app.generate_change_impact_preview(
            batch_id=empty_batch.id,
            expected_project_revision=5,
            idempotency_key="governance:preview-empty",
        )


@pytest.mark.asyncio
async def test_flush_persists_preview_before_its_only_commit_and_replays_it() -> None:
    store = _PreviewWriteFailureStore()
    app = ProjectApplication(store)
    project, modules = await _project_with_modules(store)
    power = modules[0]
    store.commit_count = 0

    adjustment = await app.record_user_adjustment(
        project_id=project.id,
        module_id=power.id,
        kind=UserAdjustmentKind.SET_PARAMETER,
        target={"key": "capacity_mah", "value": 2200},
        batch_window_seconds=2.0,
        expected_project_revision=3,
        idempotency_key="governance:atomic-adjustment",
    )
    assert adjustment.batch_id is not None
    store.commit_count = 0

    batch, preview = await app.flush_adjustment_batch_with_preview(
        batch_id=adjustment.batch_id,
        expected_project_revision=4,
        idempotency_key="governance:atomic-flush",
    )
    assert batch.status.value == "flushed"
    assert preview is not None
    assert store.commit_count == 1
    assert len(await store.list_change_impact_previews(project.id)) == 1

    replayed_batch, replayed_preview = await app.flush_adjustment_batch_with_preview(
        batch_id=adjustment.batch_id,
        expected_project_revision=4,
        idempotency_key="governance:atomic-flush",
    )
    assert replayed_batch.id == batch.id
    assert replayed_preview is not None and replayed_preview.id == preview.id
    assert store.commit_count == 1


@pytest.mark.asyncio
async def test_flush_does_not_commit_if_preview_persistence_fails() -> None:
    store = _PreviewWriteFailureStore()
    app = ProjectApplication(store)
    project, modules = await _project_with_modules(store)
    power = modules[0]
    adjustment = await app.record_user_adjustment(
        project_id=project.id,
        module_id=power.id,
        kind=UserAdjustmentKind.SET_PARAMETER,
        target={"key": "capacity_mah", "value": 2200},
        batch_window_seconds=2.0,
        expected_project_revision=3,
        idempotency_key="governance:failed-preview-adjustment",
    )
    assert adjustment.batch_id is not None
    store.commit_count = 0
    store.fail_preview_write = True

    with pytest.raises(RuntimeError, match="injected impact-preview write failure"):
        await app.flush_adjustment_batch_with_preview(
            batch_id=adjustment.batch_id,
            expected_project_revision=4,
            idempotency_key="governance:failed-preview-flush",
        )

    # In production the surrounding async-session scope rolls back every prior
    # write after this exception. The application itself did not open an early
    # durable boundary before the preview was persisted.
    assert store.commit_count == 0

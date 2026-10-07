from __future__ import annotations

import pytest

from aidison.application.service import DomainConflictError, ProjectApplication
from aidison.domain.models import (
    Candidate,
    ModuleConfigurationStatus,
    ProjectReshapeProposal,
    ProjectReshapeStatus,
    ProjectStage,
    ProposedModule,
    UserAdjustmentKind,
)
from tests.fakes import InMemoryDomainStore, bootstrap_initial_modules


@pytest.mark.asyncio
async def test_initial_requirements_can_be_approved_before_module_structure_is_confirmed() -> None:
    store = InMemoryDomainStore()
    app = ProjectApplication(store)
    project = await app.create_project(
        name="Initial module discovery fixture",
        goal="Build a beginner-friendly indoor garden monitor",
        idempotency_key="initial-structure:project",
    )

    requirement, modules = await app.approve_requirements(
        project_id=project.id,
        expected_project_revision=1,
        goal=project.goal,
        hard_constraints=("No mains voltage",),
        preferences=("Repairable",),
        available_resources=("Basic soldering tools",),
        usage_context="Monitor two indoor plants",
        budget_context="CNY 500 maximum",
        skill_context="Beginner soldering",
        unknowns=(),
        modules=(),
        idempotency_key="initial-structure:requirements",
    )

    assert modules == ()
    current = await store.get_project(project.id)
    assert current is not None
    assert current.active_requirement_revision_id == requirement.id
    assert current.active_blueprint_id is None
    assert current.stage is ProjectStage.REQUIREMENTS
    assert await store.list_project_blueprints(project.id) == []


@pytest.mark.asyncio
async def test_initial_reshape_requires_explicit_apply_before_it_materializes_modules() -> None:
    store = InMemoryDomainStore()
    app = ProjectApplication(store)
    project = await app.create_project(
        name="Initial module confirmation fixture",
        goal="Build a beginner-friendly indoor garden monitor",
        idempotency_key="initial-confirmation:project",
    )
    requirement, _ = await app.approve_requirements(
        project_id=project.id,
        expected_project_revision=1,
        goal=project.goal,
        hard_constraints=(),
        preferences=(),
        available_resources=(),
        unknowns=(),
        modules=(),
        idempotency_key="initial-confirmation:requirements",
    )
    proposal = await app.propose_project_reshape(
        proposal=ProjectReshapeProposal(
            project_id=project.id,
            basis_blueprint_id=None,
            target_goal=requirement.goal,
            summary="Split monitoring into sensing and power/control boundaries.",
            new_module_keys=("sensing", "power_control"),
            new_modules=(
                ProposedModule(
                    key="sensing",
                    name="Sensing",
                    responsibility="Measure soil moisture and ambient conditions.",
                ),
                ProposedModule(
                    key="power_control",
                    name="Power and control",
                    responsibility="Power sensors and expose a safe control interface.",
                    dependency_keys=("sensing",),
                ),
            ),
        ),
        expected_project_revision=2,
        idempotency_key="initial-confirmation:propose",
    )
    assert proposal.status is ProjectReshapeStatus.PROPOSED
    assert await store.list_modules(project.id) == []

    with pytest.raises(DomainConflictError, match="already awaiting review"):
        await app.propose_project_reshape(
            proposal=proposal.model_copy(update={"id": proposal.id}),
            expected_project_revision=2,
            idempotency_key="initial-confirmation:duplicate",
        )

    resolved = await app.resolve_project_reshape(
        proposal_id=proposal.id,
        decision=ProjectReshapeStatus.APPLIED,
        expected_project_revision=2,
        idempotency_key="initial-confirmation:apply",
    )

    assert resolved.status is ProjectReshapeStatus.APPLIED
    current = await store.get_project(project.id)
    assert current is not None
    assert current.active_blueprint_id is not None
    assert current.stage is ProjectStage.RESEARCH
    modules = await store.list_modules(project.id, requirement_revision_id=requirement.id)
    assert [item.key for item in modules] == ["sensing", "power_control"]


@pytest.mark.asyncio
async def test_user_adjustments_are_append_only_lock_checked_and_batch_coalesced() -> None:
    store = InMemoryDomainStore()
    app = ProjectApplication(store)
    project = await app.create_project(
        name="Draft fixture",
        goal="Verify user-controlled module configuration revisions",
        idempotency_key="draft:project",
    )
    project, modules = await bootstrap_initial_modules(
        store=store,
        application=app,
        project=project,
        modules=({"key": "power", "name": "Power", "responsibility": "Supply power"},),
        idempotency_key_prefix="draft",
    )
    module = modules[0]
    current_project = await store.get_project(project.id)
    assert current_project is not None
    assert current_project.active_blueprint_id is not None
    blueprint = await store.get_project_blueprint(current_project.active_blueprint_id)
    assert blueprint is not None
    assert blueprint.module_ids == (module.id,)
    locked_candidate = Candidate(
        project_id=project.id,
        module_id=module.id,
        name="Locked battery",
        description="An explicitly user-selected battery route",
    )
    alternate_candidate = Candidate(
        project_id=project.id,
        module_id=module.id,
        name="Alternate battery",
        description="A different battery route",
    )
    await store.add_candidates((locked_candidate, alternate_candidate))

    lock = await app.create_selection_lock(
        project_id=project.id,
        module_id=module.id,
        candidate_id=locked_candidate.id,
        reason="Keep this route while tuning the rest of the project.",
        expected_project_revision=3,
        idempotency_key="draft:lock",
    )
    assert lock.active is True

    with pytest.raises(DomainConflictError, match="protected"):
        await app.record_user_adjustment(
            project_id=project.id,
            module_id=module.id,
            kind=UserAdjustmentKind.SELECT_CANDIDATE,
            target={"candidate_id": str(alternate_candidate.id)},
            batch_window_seconds=2.0,
            expected_project_revision=4,
            idempotency_key="draft:blocked-adjustment",
        )

    selection = await app.record_user_adjustment(
        project_id=project.id,
        module_id=module.id,
        kind=UserAdjustmentKind.SELECT_CANDIDATE,
        target={"candidate_id": str(locked_candidate.id)},
        batch_window_seconds=2.0,
        expected_project_revision=4,
        idempotency_key="draft:selection",
    )
    parameter = await app.record_user_adjustment(
        project_id=project.id,
        module_id=module.id,
        kind=UserAdjustmentKind.SET_PARAMETER,
        target={"key": "capacity_mah", "value": 2200},
        batch_window_seconds=2.0,
        expected_project_revision=5,
        idempotency_key="draft:parameter",
    )
    assert parameter.batch_id == selection.batch_id
    configurations = await store.list_module_configurations(project.id, module.id)
    assert [item.status for item in configurations] == [
        ModuleConfigurationStatus.SUPERSEDED,
        ModuleConfigurationStatus.ACTIVE,
    ]
    assert configurations[-1].options == {
        "selected_candidate_id": str(locked_candidate.id),
        "capacity_mah": 2200,
    }
    batch = await store.get_adjustment_batch(selection.batch_id)
    assert batch is not None
    assert batch.adjustment_ids == (selection.id, parameter.id)
    assert "analysis_job_id" not in batch.model_dump(mode="json")

    flushed = await app.flush_adjustment_batch(
        batch_id=batch.id,
        expected_project_revision=6,
        idempotency_key="draft:flush",
    )
    assert flushed.status.value == "flushed"
    assert "analysis_job_id" not in flushed.model_dump(mode="json")


@pytest.mark.asyncio
async def test_solution_snapshot_restore_appends_a_new_configuration_revision() -> None:
    store = InMemoryDomainStore()
    app = ProjectApplication(store)
    project = await app.create_project(
        name="Snapshot fixture",
        goal="Preserve an editable draft checkpoint without mutating history",
        idempotency_key="snapshot:project",
    )
    project, modules = await bootstrap_initial_modules(
        store=store,
        application=app,
        project=project,
        modules=({"key": "power", "name": "Power", "responsibility": "Supply power"},),
        idempotency_key_prefix="snapshot",
    )
    module = modules[0]
    await app.record_user_adjustment(
        project_id=project.id,
        module_id=module.id,
        kind=UserAdjustmentKind.SET_PARAMETER,
        target={"key": "capacity_mah", "value": 2200},
        batch_window_seconds=2.0,
        expected_project_revision=3,
        idempotency_key="snapshot:first-edit",
    )
    saved = await app.save_solution_snapshot(
        project_id=project.id,
        label="2.2Ah baseline",
        expected_project_revision=4,
        idempotency_key="snapshot:save",
    )
    await app.record_user_adjustment(
        project_id=project.id,
        module_id=module.id,
        kind=UserAdjustmentKind.SET_PARAMETER,
        target={"key": "capacity_mah", "value": 3000},
        batch_window_seconds=2.0,
        expected_project_revision=5,
        idempotency_key="snapshot:second-edit",
    )

    restored = await app.restore_solution_snapshot(
        snapshot_id=saved.id,
        expected_project_revision=6,
        idempotency_key="snapshot:restore",
    )

    assert restored.id == saved.id
    configurations = await store.list_module_configurations(project.id, module.id)
    assert [item.status for item in configurations] == [
        ModuleConfigurationStatus.SUPERSEDED,
        ModuleConfigurationStatus.SUPERSEDED,
        ModuleConfigurationStatus.ACTIVE,
    ]
    assert configurations[-1].revision == 3
    assert configurations[-1].options == {"capacity_mah": 2200}
    history = await store.list_draft_history_entries(project.id)
    assert [item.kind.value for item in history] == [
        "adjustment",
        "snapshot_save",
        "adjustment",
        "snapshot_restore",
    ]
    assert [item.sequence for item in history] == [1, 2, 3, 4]


# ── Snapshot restore × SelectionLock enforcement ───────────────────────────


@pytest.mark.asyncio
async def test_restore_rejected_by_wildcard_lock_on_selection_change() -> None:
    """A wildcard lock (candidate_id=None) blocks any restore that would
    change selected_candidate_id.  No config, history entry, event, receipt,
    or project revision may be written."""
    store = InMemoryDomainStore()
    app = ProjectApplication(store)
    project = await app.create_project(
        name="Restore-lock fixture",
        goal="Verify lock gates snapshot restore",
        idempotency_key="restore:project",
    )
    project, modules = await bootstrap_initial_modules(
        store=store,
        application=app,
        project=project,
        modules=({"key": "mcu", "name": "MCU", "responsibility": "Control"},),
        idempotency_key_prefix="restore",
    )
    module = modules[0]
    c_a = Candidate(
        project_id=project.id, module_id=module.id,
        name="STM32", description="ARM Cortex-M",
    )
    c_b = Candidate(
        project_id=project.id, module_id=module.id,
        name="ESP32", description="Xtensa",
    )
    await store.add_candidates((c_a, c_b))

    # Select candidate A, then save snapshot
    await app.record_user_adjustment(
        project_id=project.id, module_id=module.id,
        kind=UserAdjustmentKind.SELECT_CANDIDATE,
        target={"candidate_id": str(c_a.id)},
        batch_window_seconds=2.0, expected_project_revision=3,
        idempotency_key="restore:sel-a",
    )
    saved = await app.save_solution_snapshot(
        project_id=project.id, label="STM32 baseline",
        expected_project_revision=4, idempotency_key="restore:save",
    )

    # Change to candidate B
    await app.record_user_adjustment(
        project_id=project.id, module_id=module.id,
        kind=UserAdjustmentKind.SELECT_CANDIDATE,
        target={"candidate_id": str(c_b.id)},
        batch_window_seconds=2.0, expected_project_revision=5,
        idempotency_key="restore:sel-b",
    )

    # Wildcard lock on module — blocks ANY selection change
    await app.create_selection_lock(
        project_id=project.id, module_id=module.id, candidate_id=None,
        reason="User locked the MCU choice",
        expected_project_revision=6, idempotency_key="restore:lock",
    )

    project_before = await store.get_project(project.id)
    configs_before = await store.list_module_configurations(project.id, module.id)
    history_before = await store.list_draft_history_entries(project.id)

    with pytest.raises(DomainConflictError, match="SelectionLock prevents"):
        await app.restore_solution_snapshot(
            snapshot_id=saved.id, expected_project_revision=7,
            idempotency_key="restore:blocked",
        )

    # Nothing written
    project_after = await store.get_project(project.id)
    assert project_after is not None and project_before is not None
    assert project_after.revision == project_before.revision
    assert await store.list_module_configurations(project.id, module.id) == configs_before
    assert await store.list_draft_history_entries(project.id) == history_before


@pytest.mark.asyncio
async def test_restore_blocked_by_specific_lock_when_snapshot_does_not_match() -> None:
    """A specific lock (candidate_id=B) blocks restoring to a snapshot that
    would select candidate A."""
    store = InMemoryDomainStore()
    app = ProjectApplication(store)
    project = await app.create_project(
        name="Restore-specific fixture", goal="Specific lock vs mismatched snapshot",
        idempotency_key="rspec:project",
    )
    project, modules = await bootstrap_initial_modules(
        store=store,
        application=app,
        project=project,
        modules=({"key": "mcu", "name": "MCU", "responsibility": "Control"},),
        idempotency_key_prefix="rspec",
    )
    module = modules[0]
    c_a = Candidate(project_id=project.id, module_id=module.id, name="A", description="-")
    c_b = Candidate(project_id=project.id, module_id=module.id, name="B", description="-")
    await store.add_candidates((c_a, c_b))

    await app.record_user_adjustment(
        project_id=project.id, module_id=module.id,
        kind=UserAdjustmentKind.SELECT_CANDIDATE, target={"candidate_id": str(c_a.id)},
        batch_window_seconds=2.0, expected_project_revision=3, idempotency_key="rspec:sel-a",
    )
    saved = await app.save_solution_snapshot(
        project_id=project.id, label="A baseline",
        expected_project_revision=4, idempotency_key="rspec:save",
    )
    # Change to B, then lock B specifically — restore to A must be blocked
    await app.record_user_adjustment(
        project_id=project.id, module_id=module.id,
        kind=UserAdjustmentKind.SELECT_CANDIDATE, target={"candidate_id": str(c_b.id)},
        batch_window_seconds=2.0, expected_project_revision=5, idempotency_key="rspec:sel-b",
    )
    await app.create_selection_lock(
        project_id=project.id, module_id=module.id, candidate_id=c_b.id,
        reason="B is the approved choice",
        expected_project_revision=6, idempotency_key="rspec:lock",
    )
    with pytest.raises(DomainConflictError, match="SelectionLock prevents"):
        await app.restore_solution_snapshot(
            snapshot_id=saved.id, expected_project_revision=7,
            idempotency_key="rspec:restore",
        )


@pytest.mark.asyncio
async def test_restore_allowed_when_snapshot_matches_lock() -> None:
    """A specific lock on candidate A does NOT block restoring to a snapshot
    that already selects candidate A."""
    store = InMemoryDomainStore()
    app = ProjectApplication(store)
    project = await app.create_project(
        name="Restore-match fixture", goal="Snapshot matches lock → ok",
        idempotency_key="rmatch:project",
    )
    project, modules = await bootstrap_initial_modules(
        store=store,
        application=app,
        project=project,
        modules=({"key": "mcu", "name": "MCU", "responsibility": "Control"},),
        idempotency_key_prefix="rmatch",
    )
    module = modules[0]
    c_a = Candidate(project_id=project.id, module_id=module.id, name="A", description="-")
    c_b = Candidate(project_id=project.id, module_id=module.id, name="B", description="-")
    await store.add_candidates((c_a, c_b))

    await app.record_user_adjustment(
        project_id=project.id, module_id=module.id,
        kind=UserAdjustmentKind.SELECT_CANDIDATE, target={"candidate_id": str(c_a.id)},
        batch_window_seconds=2.0, expected_project_revision=3, idempotency_key="rmatch:sel-a",
    )
    saved = await app.save_solution_snapshot(
        project_id=project.id, label="A baseline",
        expected_project_revision=4, idempotency_key="rmatch:save",
    )
    # Change to B
    await app.record_user_adjustment(
        project_id=project.id, module_id=module.id,
        kind=UserAdjustmentKind.SELECT_CANDIDATE, target={"candidate_id": str(c_b.id)},
        batch_window_seconds=2.0, expected_project_revision=5, idempotency_key="rmatch:sel-b",
    )
    # Lock A — snapshot selects A, so restore matches
    await app.create_selection_lock(
        project_id=project.id, module_id=module.id, candidate_id=c_a.id,
        reason="A is approved",
        expected_project_revision=6, idempotency_key="rmatch:lock",
    )
    # Must succeed — snapshot candidate matches the lock
    restored = await app.restore_solution_snapshot(
        snapshot_id=saved.id, expected_project_revision=7,
        idempotency_key="rmatch:restore",
    )
    assert restored.id == saved.id
    configs = await store.list_module_configurations(project.id, module.id)
    assert configs[-1].options.get("selected_candidate_id") == str(c_a.id)


@pytest.mark.asyncio
async def test_restore_allowed_for_parameter_only_change_even_with_lock() -> None:
    """A snapshot that only changes a non-selection parameter does not
    trigger the lock gate, even when an active lock exists."""
    store = InMemoryDomainStore()
    app = ProjectApplication(store)
    project = await app.create_project(
        name="Restore-param fixture", goal="Param-only restore bypasses lock",
        idempotency_key="rparam:project",
    )
    project, modules = await bootstrap_initial_modules(
        store=store,
        application=app,
        project=project,
        modules=({"key": "mcu", "name": "MCU", "responsibility": "Control"},),
        idempotency_key_prefix="rparam",
    )
    module = modules[0]
    await app.record_user_adjustment(
        project_id=project.id, module_id=module.id,
        kind=UserAdjustmentKind.SET_PARAMETER,
        target={"key": "voltage", "value": 3.3},
        batch_window_seconds=2.0, expected_project_revision=3, idempotency_key="rparam:set",
    )
    saved = await app.save_solution_snapshot(
        project_id=project.id, label="3.3V baseline",
        expected_project_revision=4, idempotency_key="rparam:save",
    )
    await app.record_user_adjustment(
        project_id=project.id, module_id=module.id,
        kind=UserAdjustmentKind.SET_PARAMETER,
        target={"key": "voltage", "value": 5.0},
        batch_window_seconds=2.0, expected_project_revision=5, idempotency_key="rparam:change",
    )
    # Add a wildcard lock — should NOT block parameter-only restore
    await app.create_selection_lock(
        project_id=project.id, module_id=module.id, candidate_id=None,
        reason="MCU locked", expected_project_revision=6, idempotency_key="rparam:lock",
    )
    restored = await app.restore_solution_snapshot(
        snapshot_id=saved.id, expected_project_revision=7,
        idempotency_key="rparam:restore",
    )
    assert restored.id == saved.id
    configs = await store.list_module_configurations(project.id, module.id)
    assert configs[-1].options.get("voltage") == 3.3


@pytest.mark.asyncio
async def test_approved_reshape_creates_a_new_active_blueprint() -> None:
    store = InMemoryDomainStore()
    app = ProjectApplication(store)
    project = await app.create_project(
        name="Reshape fixture",
        goal="Make the module graph reviewable",
        idempotency_key="reshape:project",
    )
    project, modules = await bootstrap_initial_modules(
        store=store,
        application=app,
        project=project,
        modules=(
            {"key": "power", "name": "Power", "responsibility": "Supply power"},
            {
                "key": "control",
                "name": "Control",
                "responsibility": "Control the build",
                "dependency_keys": ("power",),
            },
        ),
        idempotency_key_prefix="reshape",
    )
    current = await store.get_project(project.id)
    assert current is not None and current.active_blueprint_id is not None
    power = next(item for item in modules if item.key == "power")
    control = next(item for item in modules if item.key == "control")
    proposed = await app.propose_project_reshape(
        proposal=ProjectReshapeProposal(
            project_id=project.id,
            basis_blueprint_id=current.active_blueprint_id,
            target_goal="Add explicit power monitoring to the build",
            summary="Replace control with a monitor module while keeping the power boundary.",
            affected_module_ids=(control.id,),
            unchanged_module_ids=(power.id,),
            new_module_keys=("monitor",),
            new_modules=(
                ProposedModule(
                    key="monitor",
                    name="Monitor",
                    responsibility="Measure and report power health",
                    dependency_keys=("power",),
                ),
            ),
        ),
        expected_project_revision=3,
        idempotency_key="reshape:propose",
    )
    resolved = await app.resolve_project_reshape(
        proposal_id=proposed.id,
        decision=ProjectReshapeStatus.APPLIED,
        expected_project_revision=3,
        idempotency_key="reshape:apply",
    )

    assert resolved.status is ProjectReshapeStatus.APPLIED
    updated = await store.get_project(project.id)
    assert updated is not None
    assert updated.revision == 4
    assert updated.goal == "Add explicit power monitoring to the build"
    active = await store.get_project_blueprint(updated.active_blueprint_id)
    assert active is not None
    assert active.status.value == "active"
    active_modules = {item.id: item for item in await store.list_modules(project.id)}
    assert {active_modules[item_id].key for item_id in active.module_ids} == {"power", "monitor"}
    original = await store.get_project_blueprint(current.active_blueprint_id)
    assert original is not None
    assert original.status.value == "superseded"


@pytest.mark.asyncio
async def test_relationship_only_reshape_versions_blueprint_without_replacing_modules() -> None:
    store = InMemoryDomainStore()
    app = ProjectApplication(store)
    project = await app.create_project(
        name="Relationship reshape fixture",
        goal="Make graph relationships user-editable",
        idempotency_key="relationship-reshape:project",
    )
    project, modules = await bootstrap_initial_modules(
        store=store,
        application=app,
        project=project,
        modules=(
            {"key": "power", "name": "Power", "responsibility": "Supply power"},
            {
                "key": "control",
                "name": "Control",
                "responsibility": "Control the build",
                "dependency_keys": ("power",),
            },
        ),
        idempotency_key_prefix="relationship-reshape",
    )
    current = await store.get_project(project.id)
    assert current is not None and current.active_blueprint_id is not None
    power = next(item for item in modules if item.key == "power")
    control = next(item for item in modules if item.key == "control")

    proposal = await app.propose_project_reshape(
        proposal=ProjectReshapeProposal(
            project_id=project.id,
            basis_blueprint_id=current.active_blueprint_id,
            target_goal=current.goal,
            summary="Remove the direct power-to-control dependency for a redesigned interface.",
            affected_module_ids=(control.id,),
            unchanged_module_ids=(power.id,),
            dependency_edges=(),
        ),
        expected_project_revision=current.revision,
        idempotency_key="relationship-reshape:propose",
    )
    await app.resolve_project_reshape(
        proposal_id=proposal.id,
        decision=ProjectReshapeStatus.APPLIED,
        expected_project_revision=current.revision,
        idempotency_key="relationship-reshape:apply",
    )

    updated = await store.get_project(project.id)
    assert updated is not None and updated.active_blueprint_id is not None
    blueprint = await store.get_project_blueprint(updated.active_blueprint_id)
    assert blueprint is not None
    assert blueprint.module_ids == (power.id, control.id)
    assert blueprint.dependency_edges == ()
    active_modules = await app._active_modules(updated)
    assert next(item for item in active_modules if item.id == control.id).dependency_ids == ()


@pytest.mark.asyncio
async def test_relationship_only_reshape_rejects_cycles() -> None:
    store = InMemoryDomainStore()
    app = ProjectApplication(store)
    project = await app.create_project(
        name="Relationship cycle fixture",
        goal="Keep module graph acyclic",
        idempotency_key="relationship-cycle:project",
    )
    project, modules = await bootstrap_initial_modules(
        store=store,
        application=app,
        project=project,
        modules=(
            {"key": "power", "name": "Power", "responsibility": "Supply power"},
            {"key": "control", "name": "Control", "responsibility": "Control the build"},
        ),
        idempotency_key_prefix="relationship-cycle",
    )
    current = await store.get_project(project.id)
    assert current is not None and current.active_blueprint_id is not None
    power = next(item for item in modules if item.key == "power")
    control = next(item for item in modules if item.key == "control")

    with pytest.raises(DomainConflictError, match="dependencies contain a cycle"):
        await app.propose_project_reshape(
            proposal=ProjectReshapeProposal(
                project_id=project.id,
                basis_blueprint_id=current.active_blueprint_id,
                target_goal=current.goal,
                summary="Introduce an invalid circular relationship.",
                affected_module_ids=(power.id, control.id),
                unchanged_module_ids=(),
                dependency_edges=((power.id, control.id), (control.id, power.id)),
            ),
            expected_project_revision=current.revision,
            idempotency_key="relationship-cycle:propose",
        )

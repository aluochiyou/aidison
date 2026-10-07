from __future__ import annotations

import pytest

from aidison.application.service import ProjectApplication
from aidison.domain.models import (
    ModuleLineageChange,
    ModuleLineageOperation,
    ProjectReshapeProposal,
    ProjectReshapeStatus,
    ProposedModule,
)
from tests.fakes import InMemoryDomainStore


@pytest.mark.asyncio
async def test_confirmed_structure_appends_project_manifest_and_module_lineages() -> None:
    """A reviewable structure write leaves a stable, queryable project history."""

    store = InMemoryDomainStore()
    application = ProjectApplication(store)
    project = await application.create_project(
        name="Lineage fixture",
        goal="Build a repairable rover",
        idempotency_key="lineage:project",
    )

    baseline = await store.list_project_revision_manifests(project.id)
    assert [
        (item.revision, item.requirement_revision_id, item.blueprint_id) for item in baseline
    ] == [(1, None, None)]

    requirement, _ = await application.approve_requirements(
        project_id=project.id,
        expected_project_revision=1,
        goal=project.goal,
        hard_constraints=("repairable",),
        preferences=(),
        available_resources=(),
        unknowns=(),
        modules=(),
        idempotency_key="lineage:requirements",
    )
    reshape = await application.propose_project_reshape(
        proposal=ProjectReshapeProposal(
            project_id=project.id,
            target_goal=project.goal,
            summary="Separate power and control for an auditable initial design.",
            new_module_keys=("power", "control"),
            new_modules=(
                ProposedModule(key="power", name="Power", responsibility="Supply energy"),
                ProposedModule(
                    key="control",
                    name="Control",
                    responsibility="Control motion",
                    dependency_keys=("power",),
                ),
            ),
        ),
        expected_project_revision=2,
        idempotency_key="lineage:reshape",
    )
    await application.resolve_project_reshape(
        proposal_id=reshape.id,
        decision=ProjectReshapeStatus.APPLIED,
        expected_project_revision=2,
        idempotency_key="lineage:apply",
    )

    manifests = await store.list_project_revision_manifests(project.id)
    assert [item.revision for item in manifests] == [1, 2, 3]
    assert manifests[1].requirement_revision_id == requirement.id
    assert manifests[1].blueprint_id is None
    assert manifests[2].requirement_revision_id == requirement.id
    assert manifests[2].blueprint_id is not None
    assert manifests[2].parent_revision_id == manifests[1].id

    modules = await store.list_modules(project.id, requirement_revision_id=requirement.id)
    assert {item.key for item in modules} == {"power", "control"}
    assert all(item.lineage_id is not None for item in modules)
    assert len({item.lineage_id for item in modules}) == 2
    lineages = await store.list_module_lineages(project.id)
    assert {(item.stable_key, item.display_name) for item in lineages} == {
        ("power", "Power"),
        ("control", "Control"),
    }
    workstreams = await store.list_module_workstreams(project.id)
    assert {item.module_lineage_id for item in workstreams} == {item.id for item in lineages}


@pytest.mark.asyncio
async def test_explicit_reshape_lineage_operations_preserve_or_record_module_identity() -> None:
    """Rename, merge, and split are explicit reviewable lineage operations."""

    store = InMemoryDomainStore()
    application = ProjectApplication(store)
    project = await application.create_project(
        name="Lineage operations fixture",
        goal="Build a repairable rover",
        idempotency_key="lineage-operations:project",
    )
    requirement, _ = await application.approve_requirements(
        project_id=project.id,
        expected_project_revision=1,
        goal=project.goal,
        hard_constraints=(),
        preferences=(),
        available_resources=(),
        unknowns=(),
        modules=(),
        idempotency_key="lineage-operations:requirements",
    )
    initial = await application.propose_project_reshape(
        proposal=ProjectReshapeProposal(
            project_id=project.id,
            target_goal=project.goal,
            summary="Create power and control boundaries.",
            new_module_keys=("power", "control"),
            new_modules=(
                ProposedModule(key="power", name="Power", responsibility="Supply energy"),
                ProposedModule(key="control", name="Control", responsibility="Control motion"),
            ),
        ),
        expected_project_revision=2,
        idempotency_key="lineage-operations:initial",
    )
    await application.resolve_project_reshape(
        proposal_id=initial.id,
        decision=ProjectReshapeStatus.APPLIED,
        expected_project_revision=2,
        idempotency_key="lineage-operations:initial:apply",
    )
    initial_modules = {
        item.key: item
        for item in await store.list_modules(project.id, requirement_revision_id=requirement.id)
        if item.key in {"power", "control"}
    }
    assert all(item.lineage_id is not None for item in initial_modules.values())

    current = await store.get_project(project.id)
    assert current is not None
    merge = await application.propose_project_reshape(
        proposal=ProjectReshapeProposal(
            project_id=project.id,
            basis_blueprint_id=current.active_blueprint_id,
            target_goal=project.goal,
            summary="Merge the coupled power and control boundary.",
            affected_module_ids=tuple(item.id for item in initial_modules.values()),
            new_module_keys=("mobility",),
            new_modules=(
                ProposedModule(
                    key="mobility", name="Mobility", responsibility="Deliver controlled motion"
                ),
            ),
            lineage_changes=(
                ModuleLineageChange(
                    operation=ModuleLineageOperation.MERGE,
                    target_new_module_key="mobility",
                    source_module_ids=tuple(item.id for item in initial_modules.values()),
                ),
            ),
        ),
        expected_project_revision=3,
        idempotency_key="lineage-operations:merge",
    )
    await application.resolve_project_reshape(
        proposal_id=merge.id,
        decision=ProjectReshapeStatus.APPLIED,
        expected_project_revision=3,
        idempotency_key="lineage-operations:merge:apply",
    )
    mobility = next(
        item
        for item in await store.list_modules(project.id, requirement_revision_id=requirement.id)
        if item.key == "mobility"
    )
    assert mobility.lineage_id is not None

    current = await store.get_project(project.id)
    assert current is not None
    rename = await application.propose_project_reshape(
        proposal=ProjectReshapeProposal(
            project_id=project.id,
            basis_blueprint_id=current.active_blueprint_id,
            target_goal=project.goal,
            summary="Rename the active mobility boundary for the current design vocabulary.",
            affected_module_ids=(mobility.id,),
            new_module_keys=("motion-system",),
            new_modules=(
                ProposedModule(
                    key="motion-system",
                    name="Motion system",
                    responsibility="Deliver controlled motion",
                ),
            ),
            lineage_changes=(
                ModuleLineageChange(
                    operation=ModuleLineageOperation.RENAME,
                    target_new_module_key="motion-system",
                    source_module_ids=(mobility.id,),
                ),
            ),
        ),
        expected_project_revision=4,
        idempotency_key="lineage-operations:rename",
    )
    await application.resolve_project_reshape(
        proposal_id=rename.id,
        decision=ProjectReshapeStatus.APPLIED,
        expected_project_revision=4,
        idempotency_key="lineage-operations:rename:apply",
    )
    motion_system = next(
        item
        for item in await store.list_modules(project.id, requirement_revision_id=requirement.id)
        if item.key == "motion-system"
    )
    assert motion_system.lineage_id == mobility.lineage_id
    assert mobility.lineage_id is not None

    lineages = {item.id: item for item in await store.list_module_lineages(project.id)}
    assert lineages[mobility.lineage_id].stable_key == "mobility"
    assert lineages[mobility.lineage_id].display_name == "Motion system"
    for source in initial_modules.values():
        assert source.lineage_id is not None
        assert lineages[source.lineage_id].merged_into_lineage_id == mobility.lineage_id
        assert lineages[source.lineage_id].retired_at is not None
    workstreams = {
        item.module_lineage_id: item for item in await store.list_module_workstreams(project.id)
    }
    assert workstreams[mobility.lineage_id].status.value == "active"
    for source in initial_modules.values():
        assert source.lineage_id is not None
        assert workstreams[source.lineage_id].status.value == "retired"

    current = await store.get_project(project.id)
    assert current is not None
    split = await application.propose_project_reshape(
        proposal=ProjectReshapeProposal(
            project_id=project.id,
            basis_blueprint_id=current.active_blueprint_id,
            target_goal=project.goal,
            summary="Split the motion system into storage and actuation concerns.",
            affected_module_ids=(motion_system.id,),
            new_module_keys=("energy-storage", "actuation"),
            new_modules=(
                ProposedModule(
                    key="energy-storage", name="Energy storage", responsibility="Store energy"
                ),
                ProposedModule(
                    key="actuation", name="Actuation", responsibility="Move the rover"
                ),
            ),
            lineage_changes=(
                ModuleLineageChange(
                    operation=ModuleLineageOperation.SPLIT,
                    target_new_module_key="energy-storage",
                    source_module_ids=(motion_system.id,),
                ),
                ModuleLineageChange(
                    operation=ModuleLineageOperation.SPLIT,
                    target_new_module_key="actuation",
                    source_module_ids=(motion_system.id,),
                ),
            ),
        ),
        expected_project_revision=5,
        idempotency_key="lineage-operations:split",
    )
    await application.resolve_project_reshape(
        proposal_id=split.id,
        decision=ProjectReshapeStatus.APPLIED,
        expected_project_revision=5,
        idempotency_key="lineage-operations:split:apply",
    )

    lineages = {item.id: item for item in await store.list_module_lineages(project.id)}
    descendants = [
        item for item in lineages.values() if item.split_from_lineage_id == mobility.lineage_id
    ]
    assert {item.stable_key for item in descendants} == {"energy-storage", "actuation"}
    assert lineages[mobility.lineage_id].retired_at is not None

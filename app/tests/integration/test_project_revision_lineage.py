from __future__ import annotations

import os
from uuid import uuid4

import pytest

from aidison.application.service import ProjectApplication
from aidison.domain.models import (
    ModuleLineageChange,
    ModuleLineageOperation,
    ProjectReshapeProposal,
    ProjectReshapeStatus,
    ProposedModule,
)
from aidison.engineering.coupling import CouplingKind, EngineeringCouplingEdge, analyze_couplings
from aidison.infrastructure.database import DatabaseSettings, create_engine, create_session_factory
from aidison.infrastructure.store import PostgresDomainStore

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_postgres_structure_confirmation_persists_manifest_and_lineage() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")

    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    key_prefix = f"manifest-lineage:{uuid4()}"
    try:
        async with factory() as session:
            store = PostgresDomainStore(session)
            application = ProjectApplication(store)
            project = await application.create_project(
                name="PostgreSQL lineage fixture",
                goal="Build a repairable rover",
                idempotency_key=f"{key_prefix}:project",
            )
            requirement, _ = await application.approve_requirements(
                project_id=project.id,
                expected_project_revision=1,
                goal=project.goal,
                hard_constraints=("repairable",),
                preferences=(),
                available_resources=(),
                unknowns=(),
                modules=(),
                idempotency_key=f"{key_prefix}:requirements",
            )
            reshape = await application.propose_project_reshape(
                proposal=ProjectReshapeProposal(
                    project_id=project.id,
                    target_goal=project.goal,
                    summary="Create reviewable power and control boundaries.",
                    new_module_keys=("power", "control"),
                    new_modules=(
                        ProposedModule(
                            key="power", name="Power", responsibility="Supply energy"
                        ),
                        ProposedModule(
                            key="control",
                            name="Control",
                            responsibility="Control motion",
                            dependency_keys=("power",),
                        ),
                    ),
                ),
                expected_project_revision=2,
                idempotency_key=f"{key_prefix}:reshape",
            )
            await application.resolve_project_reshape(
                proposal_id=reshape.id,
                decision=ProjectReshapeStatus.APPLIED,
                expected_project_revision=2,
                idempotency_key=f"{key_prefix}:apply",
            )

            manifests = await store.list_project_revision_manifests(project.id)
            modules = await store.list_modules(
                project.id, requirement_revision_id=requirement.id
            )
            lineages = await store.list_module_lineages(project.id)

            assert [item.revision for item in manifests] == [1, 2, 3]
            assert manifests[2].blueprint_id is not None
            assert manifests[2].parent_revision_id == manifests[1].id
            assert all(item.lineage_id is not None for item in modules)
            assert {item.stable_key for item in lineages} == {"power", "control"}

            current = await store.get_project(project.id)
            assert current is not None
            merge = await application.propose_project_reshape(
                proposal=ProjectReshapeProposal(
                    project_id=project.id,
                    basis_blueprint_id=current.active_blueprint_id,
                    target_goal=project.goal,
                    summary="Record the reviewed merge of power and control.",
                    affected_module_ids=tuple(item.id for item in modules),
                    new_module_keys=("mobility",),
                    new_modules=(
                        ProposedModule(
                            key="mobility",
                            name="Mobility",
                            responsibility="Deliver controlled motion",
                        ),
                    ),
                    lineage_changes=(
                        ModuleLineageChange(
                            operation=ModuleLineageOperation.MERGE,
                            target_new_module_key="mobility",
                            source_module_ids=tuple(item.id for item in modules),
                        ),
                    ),
                ),
                expected_project_revision=3,
                idempotency_key=f"{key_prefix}:merge",
            )
            await application.resolve_project_reshape(
                proposal_id=merge.id,
                decision=ProjectReshapeStatus.APPLIED,
                expected_project_revision=3,
                idempotency_key=f"{key_prefix}:merge:apply",
            )
            updated_lineages = {
                item.id: item for item in await store.list_module_lineages(project.id)
            }
            mobility = next(
                item
                for item in await store.list_modules(
                    project.id, requirement_revision_id=requirement.id
                )
                if item.key == "mobility"
            )
            assert mobility.lineage_id is not None
            for module in modules:
                assert module.lineage_id is not None
                assert (
                    updated_lineages[module.lineage_id].merged_into_lineage_id
                    == mobility.lineage_id
                )
                assert updated_lineages[module.lineage_id].retired_at is not None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_postgres_reshape_persists_cyclic_engineering_couplings_separately(
) -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")
    engine = create_engine(DatabaseSettings(database_url=database_url))
    factory = create_session_factory(engine)
    key_prefix = f"coupling:{uuid4()}"
    try:
        async with factory() as session:
            store = PostgresDomainStore(session)
            application = ProjectApplication(store)
            project = await application.create_project(
                name="Coupling fixture", goal="Build a controlled rover", idempotency_key=key_prefix
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
                idempotency_key=f"{key_prefix}:requirements",
            )
            initial = await application.propose_project_reshape(
                proposal=ProjectReshapeProposal(
                    project_id=project.id, target_goal=project.goal, summary="Create two modules.",
                    new_module_keys=("power", "control"),
                    new_modules=(
                        ProposedModule(key="power", name="Power", responsibility="Supply energy"),
                        ProposedModule(
                            key="control", name="Control", responsibility="Control motion"
                        ),
                    ),
                ), expected_project_revision=2, idempotency_key=f"{key_prefix}:initial",
            )
            await application.resolve_project_reshape(
                proposal_id=initial.id, decision=ProjectReshapeStatus.APPLIED,
                expected_project_revision=2, idempotency_key=f"{key_prefix}:initial:apply",
            )
            current = await store.get_project(project.id)
            assert current is not None and current.active_blueprint_id is not None
            modules = await store.list_modules(project.id, requirement_revision_id=requirement.id)
            assert all(item.lineage_id is not None for item in modules)
            by_key = {item.key: item for item in modules}
            power_lineage = by_key["power"].lineage_id
            control_lineage = by_key["control"].lineage_id
            assert power_lineage is not None and control_lineage is not None
            coupling = await application.propose_project_reshape(
                proposal=ProjectReshapeProposal(
                    project_id=project.id, basis_blueprint_id=current.active_blueprint_id,
                    target_goal=project.goal, summary="Review cyclic power/control interfaces.",
                    unchanged_module_ids=tuple(item.id for item in modules),
                    engineering_couplings=(
                        EngineeringCouplingEdge(
                            source_lineage_id=power_lineage,
                            target_lineage_id=control_lineage,
                            kind=CouplingKind.POWER,
                        ),
                        EngineeringCouplingEdge(
                            source_lineage_id=control_lineage,
                            target_lineage_id=power_lineage,
                            kind=CouplingKind.SIGNAL,
                        ),
                    ),
                ), expected_project_revision=3, idempotency_key=f"{key_prefix}:coupling",
            )
            await application.resolve_project_reshape(
                proposal_id=coupling.id, decision=ProjectReshapeStatus.APPLIED,
                expected_project_revision=3, idempotency_key=f"{key_prefix}:coupling:apply",
            )
            updated = await store.get_project(project.id)
            assert updated is not None and updated.active_blueprint_id is not None
            blueprint = await store.get_project_blueprint(updated.active_blueprint_id)
            assert blueprint is not None
            assert blueprint.dependency_edges == ()
            assert len(blueprint.engineering_couplings) == 2
            analysis = analyze_couplings(
                lineage_ids=tuple(
                    item.lineage_id for item in modules if item.lineage_id is not None
                ),
                edges=blueprint.engineering_couplings,
            )
            assert len(analysis.strongly_connected_components) == 1
    finally:
        await engine.dispose()

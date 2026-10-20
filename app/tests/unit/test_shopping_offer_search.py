from __future__ import annotations

from hashlib import sha256
from uuid import uuid4

import pytest

from aidison.application.service import PreconditionFailedError
from aidison.application.shopping import ShoppingApplication
from aidison.domain.models import Project, SolutionVersion
from aidison.operations.fixture import FakeShoppingProvider
from tests.fakes import InMemoryDomainStore


def _hash(value: str) -> str:
    return sha256(value.encode()).hexdigest()


async def _ready_scope(store: InMemoryDomainStore) -> tuple[Project, SolutionVersion]:
    project_id = uuid4()
    solution = SolutionVersion(
        project_id=project_id,
        version=1,
        requirement_revision_id=uuid4(),
        basis_hash=_hash("shopping-offer-search"),
        module_snapshots=(),
        approved_decision_id=uuid4(),
        bom=({"line_id": "environment-sensor", "name": "SCD41 sensor"},),
    )
    project = Project(
        id=project_id,
        name="Shopping offer search",
        goal="Verify provider reads do not manufacture project revisions",
        active_solution_version_id=solution.id,
    )
    await store.add_project(project)
    await store.add_solution_version(solution)
    return project, solution


@pytest.mark.asyncio
async def test_offer_search_persists_snapshot_without_creating_project_revision() -> None:
    store = InMemoryDomainStore()
    project, solution = await _ready_scope(store)
    manifests_before = tuple(await store.list_project_revision_manifests(project.id))

    snapshots = await ShoppingApplication(store, FakeShoppingProvider()).search_offers(
        project_id=project.id,
        expected_project_revision=project.revision,
        query="SCD41 sensor",
        bom_line_id="environment-sensor",
        region="CN",
    )

    assert len(snapshots) == 1
    assert snapshots[0].solution_version_id == solution.id
    assert tuple(await store.list_project_revision_manifests(project.id)) == manifests_before
    assert store.projects[project.id].revision == project.revision


@pytest.mark.asyncio
async def test_offer_search_rechecks_revision_after_provider_returns() -> None:
    store = InMemoryDomainStore()
    project, _ = await _ready_scope(store)
    provider = FakeShoppingProvider()

    # Simulate a project mutation that won the race while the external search
    # was in flight.  The post-provider fence must refuse to attach snapshots.
    original_search = provider.search_offers

    async def search_and_advance(*args: object, **kwargs: object):
        offers = await original_search(*args, **kwargs)  # type: ignore[arg-type]
        store.projects[project.id] = project.model_copy(update={"revision": 2})
        return offers

    provider.search_offers = search_and_advance  # type: ignore[method-assign]

    with pytest.raises(PreconditionFailedError, match="stale"):
        await ShoppingApplication(store, provider).search_offers(
            project_id=project.id,
            expected_project_revision=1,
            query="SCD41 sensor",
            bom_line_id="environment-sensor",
            region="CN",
        )

    assert await store.list_offer_snapshots(project.id) == []

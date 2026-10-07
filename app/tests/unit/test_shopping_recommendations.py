"""Deterministic, read-only offer ranking for a Project budget."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import uuid4

import pytest

from aidison.application.shopping import ShoppingApplication
from aidison.domain.models import (
    OfferSnapshot,
    Project,
    SolutionVersion,
    SpendBudgetImpactClassification,
    SpendBudgetProposalStatus,
    SpendBudgetRevision,
)
from aidison.operations.fixture import FakeShoppingProvider
from tests.fakes import InMemoryDomainStore


def _hash(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def _scope() -> tuple[InMemoryDomainStore, Project, SolutionVersion]:
    store = InMemoryDomainStore()
    solution = SolutionVersion(
        project_id=uuid4(),
        version=1,
        requirement_revision_id=uuid4(),
        basis_hash=_hash("recommendation-solution"),
        module_snapshots=(),
        approved_decision_id=uuid4(),
    )
    budget = SpendBudgetRevision(
        project_id=solution.project_id,
        proposal_id=uuid4(),
        revision=1,
        amount="250.00",
        currency="CNY",
        status=SpendBudgetProposalStatus.APPLIED,
    )
    project = Project(
        id=solution.project_id,
        name="Recommendation scope",
        goal="Rank offers without selecting one",
        active_solution_version_id=solution.id,
        active_spend_budget_revision_id=budget.id,
    )
    store.projects[project.id] = project
    store.solutions[solution.id] = solution
    store.spend_budget_revisions[budget.id] = budget
    return store, project, solution


def _offer(
    project: Project,
    solution: SolutionVersion,
    *,
    offer_id: str,
    price: str,
    observed_at: datetime,
    expires_at: datetime | None = None,
) -> OfferSnapshot:
    return OfferSnapshot(
        project_id=project.id,
        solution_version_id=solution.id,
        bom_line_id="power",
        provider="taobao",
        provider_offer_id=offer_id,
        title=f"Offer {offer_id}",
        availability="unknown",
        unit_price=price,
        currency="CNY",
        region="CN",
        quantity_available=0,
        product_url=f"https://item.taobao.com/item.htm?id={offer_id}",
        snapshot_hash=_hash(f"offer:{offer_id}:{price}:{observed_at}"),
        provenance="unit-test",
        observed_at=observed_at,
        expires_at=expires_at,
    )


@pytest.mark.asyncio
async def test_recommendations_rank_current_within_budget_before_over_and_stale() -> None:
    store, project, solution = _scope()
    now = datetime.now(UTC)
    within = _offer(project, solution, offer_id="within", price="100.00", observed_at=now)
    over = _offer(project, solution, offer_id="over", price="300.00", observed_at=now)
    stale = _offer(
        project,
        solution,
        offer_id="stale",
        price="50.00",
        observed_at=now - timedelta(days=1),
        expires_at=now - timedelta(minutes=1),
    )
    # Newest copy of the same provider offer wins; stale duplicate must not
    # produce two recommendations.
    old_within = _offer(
        project,
        solution,
        offer_id="within",
        price="90.00",
        observed_at=now - timedelta(minutes=1),
    )
    store.offer_snapshots.update(
        {within.id: within, over.id: over, stale.id: stale, old_within.id: old_within}
    )

    recommendations = await ShoppingApplication(
        store, FakeShoppingProvider()
    ).recommend_offer_snapshots(project_id=project.id, bom_line_id="power")

    assert [item.offer_snapshot_id for item in recommendations.items] == [
        within.id,
        over.id,
        stale.id,
    ]
    assert (
        recommendations.items[0].listed_price_classification
        is SpendBudgetImpactClassification.WITHIN
    )
    assert (
        recommendations.items[1].listed_price_classification is SpendBudgetImpactClassification.OVER
    )
    assert (
        recommendations.items[2].listed_price_classification
        is SpendBudgetImpactClassification.UNKNOWN
    )
    assert recommendations.items[0].final_total_known is False

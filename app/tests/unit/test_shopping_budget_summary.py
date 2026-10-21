"""Read-only project spend-budget summaries over selected offer snapshots."""

from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

import pytest

from aidison.application.shopping import ShoppingApplication
from aidison.domain.models import (
    OfferSnapshot,
    Project,
    ShoppingBudgetSelection,
    SolutionVersion,
    SpendBudgetImpactClassification,
    SpendBudgetProposalStatus,
    SpendBudgetRevision,
)
from aidison.operations.fixture import FakeShoppingProvider
from tests.fakes import InMemoryDomainStore


def _hash(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def _ready_scope() -> tuple[InMemoryDomainStore, Project, SolutionVersion]:
    store = InMemoryDomainStore()
    solution = SolutionVersion(
        project_id=uuid4(),
        version=1,
        requirement_revision_id=uuid4(),
        basis_hash=_hash("budget-summary-solution"),
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
        name="Budget summary",
        goal="Preview selected product costs",
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
    line_id: str,
    unit_price: str = "100.00",
    shipping: str | None = "20.00",
    tax: str | None = "10.00",
) -> OfferSnapshot:
    return OfferSnapshot(
        project_id=project.id,
        solution_version_id=solution.id,
        bom_line_id=line_id,
        provider="taobao",
        provider_offer_id=f"offer-{line_id}",
        title=f"Offer {line_id}",
        availability="unknown",
        unit_price=unit_price,
        currency="CNY",
        shipping_estimate=shipping,
        tax_estimate=tax,
        region="CN",
        quantity_available=0,
        product_url="https://item.taobao.com/item.htm?id=123",
        snapshot_hash=_hash(f"offer:{line_id}"),
        provenance="unit-test",
        observed_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_budget_summary_uses_project_total_and_selected_quantities() -> None:
    store, project, solution = _ready_scope()
    offer = _offer(project, solution, line_id="power")
    store.offer_snapshots[offer.id] = offer

    summary = await ShoppingApplication(store, FakeShoppingProvider()).preview_project_spend_budget(
        project_id=project.id,
        selections=(ShoppingBudgetSelection(offer_snapshot_id=offer.id, quantity=2),),
    )

    assert summary.classification is SpendBudgetImpactClassification.WITHIN
    assert summary.merchandise_subtotal == "200.00"
    assert summary.total_amount == "230.00"
    assert summary.remaining_amount == "20.00"
    assert summary.lines[0].total_amount == "230.00"


@pytest.mark.asyncio
async def test_budget_summary_keeps_missing_shipping_or_tax_unknown() -> None:
    store, project, solution = _ready_scope()
    offer = _offer(project, solution, line_id="power", shipping=None, tax=None)
    store.offer_snapshots[offer.id] = offer

    summary = await ShoppingApplication(store, FakeShoppingProvider()).preview_project_spend_budget(
        project_id=project.id,
        selections=(ShoppingBudgetSelection(offer_snapshot_id=offer.id, quantity=2),),
    )

    assert summary.classification is SpendBudgetImpactClassification.UNKNOWN
    assert summary.merchandise_subtotal == "200.00"
    assert summary.total_amount is None
    assert summary.remaining_amount is None
    assert "shipping estimate is unavailable" in summary.lines[0].reason

from __future__ import annotations

from hashlib import sha256
from uuid import uuid4

import pytest

from aidison.application.service import PreconditionFailedError
from aidison.application.shopping import (
    ShoppingApplication,
    _compute_proposal_basis_hash,
)
from aidison.domain.models import (
    EffectApprovalStatus,
    OfferSnapshot,
    Project,
    PurchaseProposal,
    PurchaseProposalStatus,
    SolutionVersion,
)
from aidison.operations.fixture import FakeShoppingProvider
from tests.fakes import InMemoryDomainStore


def _hash(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def _add_ready_checkout_scope(store: InMemoryDomainStore, label: str) -> PurchaseProposal:
    project_id = uuid4()
    solution = SolutionVersion(
        project_id=project_id,
        version=1,
        requirement_revision_id=uuid4(),
        basis_hash=_hash(f"solution:{label}"),
        module_snapshots=(),
        approved_decision_id=uuid4(),
    )
    project = Project(
        id=project_id,
        name=f"Project {label}",
        goal="Test exact effect scope isolation",
        active_solution_version_id=solution.id,
    )
    offer = OfferSnapshot(
        project_id=project_id,
        solution_version_id=solution.id,
        bom_line_id="shopping-line",
        provider="fake-desktop",
        provider_offer_id=f"offer-{label}",
        merchandise_id=f"merchandise-{label}",
        title=f"Offer {label}",
        availability="in_stock",
        unit_price="10.00",
        currency="CNY",
        region="CN",
        quantity_available=2,
        product_url="https://example.test/product",
        snapshot_hash=_hash(f"offer:{label}"),
        provenance="unit-test",
    )
    proposal = PurchaseProposal(
        project_id=project_id,
        solution_version_id=solution.id,
        offer_snapshot_id=offer.id,
        quantity=1,
        region="CN",
        currency="CNY",
        max_total="20.00",
        unit_price="10.00",
        status=PurchaseProposalStatus.READY,
        basis_hash=_compute_proposal_basis_hash(
            project_id,
            solution.id,
            offer.snapshot_hash,
            1,
            "CN",
            "CNY",
            None,
            None,
            "20.00",
        ),
        confirmed_line_ids=(offer.bom_line_id,),
    )
    store.projects[project.id] = project
    store.solutions[solution.id] = solution
    store.offer_snapshots[offer.id] = offer
    store.purchase_proposals[proposal.id] = proposal
    return proposal


@pytest.mark.asyncio
async def test_effect_approval_cannot_cross_project_or_target_scope() -> None:
    store = InMemoryDomainStore()
    first = _add_ready_checkout_scope(store, "first")
    second = _add_ready_checkout_scope(store, "second")
    app = ShoppingApplication(store, FakeShoppingProvider())

    requested = await app.request_effect_approval(
        proposal_id=first.id,
        expected_proposal_basis=first.basis_hash,
        expected_project_revision=1,
        idempotency_key="request-first",
    )
    approved = await app.resolve_effect_approval(
        approval_id=requested.id,
        decision=EffectApprovalStatus.APPROVED,
        scope_hash=requested.scope_hash,
        reason=None,
        expected_project_revision=2,
        idempotency_key="approve-first",
    )

    with pytest.raises(PreconditionFailedError, match="does not match"):
        await app.create_checkout_handoff(
            proposal_id=second.id,
            effect_approval_id=approved.id,
            expected_proposal_basis=second.basis_hash,
            expected_project_revision=1,
            idempotency_key="cross-project-checkout",
        )

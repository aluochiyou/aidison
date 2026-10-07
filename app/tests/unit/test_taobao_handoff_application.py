"""Tests that Taobao (search-only) offers cannot reach purchase/approval/handoff.

Taobao V1 is scoped to search/recommendation only.  These tests pin the
fail-closed guarantees at the application layer:
- a Taobao offer (UNKNOWN availability / 0 quantity) cannot become a
  PurchaseProposal;
- even a READY proposal cannot request an effect approval or create a
  checkout handoff when the provider declares no handoff kinds;
- the same gate holds for any search-only provider, not just Taobao.
"""

from __future__ import annotations

from hashlib import sha256
from uuid import uuid4

import pytest

from aidison.application.service import DomainConflictError
from aidison.application.shopping import (
    ShoppingApplication,
    _compute_proposal_basis_hash,
)
from aidison.domain.models import (
    OfferSnapshot,
    Project,
    PurchaseProposal,
    PurchaseProposalStatus,
    SolutionVersion,
)
from aidison.operations.fixture import FakeShoppingProvider
from aidison.providers.taobao import TaobaoAffiliateAdapter
from tests.fakes import InMemoryDomainStore


def _hash(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def _add_ready_checkout_scope(
    store: InMemoryDomainStore,
    label: str,
    *,
    provider: str = "taobao",
) -> PurchaseProposal:
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
        name=f"Taobao Project {label}",
        goal="Test Taobao search-only fail-closed scope",
        active_solution_version_id=solution.id,
    )
    offer = OfferSnapshot(
        project_id=project_id,
        solution_version_id=solution.id,
        bom_line_id="taobao-line",
        provider=provider,
        provider_offer_id=f"tb-offer-{label}",
        merchandise_id=f"tb-merch-{label}",
        title=f"Taobao Offer {label}",
        availability="unknown",
        unit_price="168.00",
        currency="CNY",
        region="CN",
        quantity_available=0,
        product_url="https://item.taobao.com/item.htm?id=99999999",
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
        max_total="220.00",
        unit_price="168.00",
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
            "220.00",
        ),
        confirmed_line_ids=(offer.bom_line_id,),
    )
    store.projects[project.id] = project
    store.solutions[solution.id] = solution
    store.offer_snapshots[offer.id] = offer
    store.purchase_proposals[proposal.id] = proposal
    return proposal


def _taobao_adapter() -> TaobaoAffiliateAdapter:
    return TaobaoAffiliateAdapter(app_key="k", app_secret="s", adzone_id="a")


@pytest.mark.asyncio
async def test_taobao_offer_cannot_become_purchase_proposal() -> None:
    """UNKNOWN availability / 0 quantity makes a Taobao offer un-proposable."""
    store = InMemoryDomainStore()
    provider = _taobao_adapter()
    project_id = uuid4()
    solution = SolutionVersion(
        project_id=project_id,
        version=1,
        requirement_revision_id=uuid4(),
        basis_hash=_hash("solution:unproposable"),
        module_snapshots=(),
        approved_decision_id=uuid4(),
    )
    project = Project(
        id=project_id,
        name="Taobao unproposable",
        goal="Test offer cannot become a proposal",
        active_solution_version_id=solution.id,
    )
    offer = OfferSnapshot(
        project_id=project_id,
        solution_version_id=solution.id,
        bom_line_id="taobao-line",
        provider="taobao",
        provider_offer_id="tb-offer-1",
        merchandise_id="tb-merch-1",
        title="Taobao Offer",
        availability="unknown",
        unit_price="168.00",
        currency="CNY",
        region="CN",
        quantity_available=0,
        product_url="https://item.taobao.com/item.htm?id=1",
        snapshot_hash=_hash("offer:unproposable"),
        provenance="unit-test",
    )
    store.projects[project.id] = project
    store.solutions[solution.id] = solution
    store.offer_snapshots[offer.id] = offer
    app = ShoppingApplication(store, provider)

    with pytest.raises(DomainConflictError, match="not in stock"):
        await app.create_purchase_proposal(
            project_id=project_id,
            expected_project_revision=1,
            solution_version_id=solution.id,
            offer_snapshot=offer,
            quantity=1,
            region="CN",
            currency="CNY",
            shipping_estimate=None,
            tax_estimate=None,
            max_total="220.00",
            idempotency_key="prop-taobao",
        )


@pytest.mark.asyncio
async def test_taobao_proposal_cannot_request_effect_approval() -> None:
    """A search-only Taobao provider rejects the approval flow at the scope gate."""
    store = InMemoryDomainStore()
    provider = _taobao_adapter()
    proposal = _add_ready_checkout_scope(store, "approval")
    app = ShoppingApplication(store, provider)

    with pytest.raises(DomainConflictError, match="no recognised handoff kind"):
        await app.request_effect_approval(
            proposal_id=proposal.id,
            expected_proposal_basis=proposal.basis_hash,
            expected_project_revision=1,
            idempotency_key="req-taobao",
        )


@pytest.mark.asyncio
async def test_taobao_proposal_cannot_create_checkout_handoff() -> None:
    """A search-only Taobao provider rejects checkout handoff at the scope gate."""
    store = InMemoryDomainStore()
    provider = _taobao_adapter()
    proposal = _add_ready_checkout_scope(store, "handoff")
    app = ShoppingApplication(store, provider)

    with pytest.raises(DomainConflictError, match="no recognised handoff kind"):
        await app.create_checkout_handoff(
            proposal_id=proposal.id,
            effect_approval_id=uuid4(),
            expected_proposal_basis=proposal.basis_hash,
            expected_project_revision=1,
            idempotency_key="handoff-taobao",
        )


@pytest.mark.asyncio
async def test_search_only_provider_gate_is_provider_generic() -> None:
    """Any provider with no handoff kinds fails closed, not just Taobao."""
    store = InMemoryDomainStore()
    provider = FakeShoppingProvider()
    provider.set_handoff_kinds(frozenset())
    proposal = _add_ready_checkout_scope(store, "generic", provider="fake-desktop")
    app = ShoppingApplication(store, provider)

    with pytest.raises(DomainConflictError, match="no recognised handoff kind"):
        await app.request_effect_approval(
            proposal_id=proposal.id,
            expected_proposal_basis=proposal.basis_hash,
            expected_project_revision=1,
            idempotency_key="req-generic",
        )

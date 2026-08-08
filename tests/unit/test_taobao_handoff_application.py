"""Tests for Taobao PRODUCT_REDIRECT handoff approval loop and crash/replay."""

from __future__ import annotations

from hashlib import sha256
from uuid import uuid4

import pytest

from aidison.application.shopping import (
    ShoppingApplication,
    _compute_proposal_basis_hash,
)
from aidison.domain.models import (
    CheckoutHandoffStatus,
    EffectApprovalStatus,
    HandoffKind,
    OfferSnapshot,
    Project,
    PurchaseProposal,
    PurchaseProposalStatus,
    SolutionVersion,
)
from aidison.operations.fixture import FakeShoppingProvider
from aidison.providers.shopping import ShoppingProviderError
from tests.fakes import InMemoryDomainStore


def _hash(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def _add_taobao_checkout_scope(store: InMemoryDomainStore, label: str) -> PurchaseProposal:
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
        goal="Test Taobao PRODUCT_REDIRECT handoff",
        active_solution_version_id=solution.id,
    )
    offer = OfferSnapshot(
        project_id=project_id,
        solution_version_id=solution.id,
        bom_line_id="taobao-line",
        provider="fake-desktop",
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
            project_id, solution.id, offer.snapshot_hash,
            1, "CN", "CNY", None, None, "220.00",
        ),
        confirmed_line_ids=(offer.bom_line_id,),
    )
    store.projects[project.id] = project
    store.solutions[solution.id] = solution
    store.offer_snapshots[offer.id] = offer
    store.purchase_proposals[proposal.id] = proposal
    return proposal


@pytest.mark.asyncio
async def test_taobao_handoff_full_approval_loop() -> None:
    """End-to-end Taobao PRODUCT_REDIRECT approval loop."""
    store = InMemoryDomainStore()
    provider = FakeShoppingProvider()
    proposal = _add_taobao_checkout_scope(store, "full-loop")
    # Simulate Taobao-only provider
    provider.set_handoff_kinds(frozenset({HandoffKind.PRODUCT_REDIRECT}))
    app = ShoppingApplication(store, provider)

    # 1. Request approval
    approval = await app.request_effect_approval(
        proposal_id=proposal.id,
        expected_proposal_basis=proposal.basis_hash,
        expected_project_revision=1,
        idempotency_key="req-taobao",
    )
    assert approval.effect_kind == "shopping.create_redirect"
    assert "handoff_kind" in approval.constraints
    assert approval.constraints["handoff_kind"] == "product_redirect"

    # 2. Approve
    approved = await app.resolve_effect_approval(
        approval_id=approval.id,
        decision=EffectApprovalStatus.APPROVED,
        scope_hash=approval.scope_hash,
        reason=None,
        expected_project_revision=2,
        idempotency_key="approve-taobao",
    )
    assert approved.status == EffectApprovalStatus.APPROVED

    # 3. Create handoff
    handoff = await app.create_checkout_handoff(
        proposal_id=proposal.id,
        effect_approval_id=approved.id,
        expected_proposal_basis=proposal.basis_hash,
        expected_project_revision=3,
        idempotency_key="handoff-taobao",
    )
    assert handoff.status == CheckoutHandoffStatus.DISPATCHED
    assert handoff.handoff_kind == HandoffKind.PRODUCT_REDIRECT
    assert handoff.provider_cart_id is None  # PRODUCT_REDIRECT has no cart ID
    assert handoff.checkout_url is not None
    assert handoff.checkout_url.startswith("https://")


@pytest.mark.asyncio
async def test_taobao_handoff_approval_scoped_to_provider_and_kind() -> None:
    """Effect approvals differ by handoff kind (scope isolation).

    A PRODUCT_REDIRECT approval has ``shopping.create_redirect`` effect_kind
    and ``product_redirect`` in constraints, while CART_REDIRECT has
    ``shopping.create_cart`` / ``cart_redirect``.  The scope hashes are
    different because they embed the effect_kind.
    """
    store = InMemoryDomainStore()
    proposal = _add_taobao_checkout_scope(store, "scope-diff")

    # Provider with PRODUCT_REDIRECT
    provider_redirect = FakeShoppingProvider()
    provider_redirect.set_handoff_kinds(frozenset({HandoffKind.PRODUCT_REDIRECT}))
    app_redirect = ShoppingApplication(store, provider_redirect)

    # Provider with CART_REDIRECT (same store, same proposal)
    provider_cart = FakeShoppingProvider()
    provider_cart.set_handoff_kinds(frozenset({HandoffKind.CART_REDIRECT}))
    app_cart = ShoppingApplication(store, provider_cart)

    approval_redirect = await app_redirect.request_effect_approval(
        proposal_id=proposal.id,
        expected_proposal_basis=proposal.basis_hash,
        expected_project_revision=1,
        idempotency_key="req-redirect",
    )
    approval_cart = await app_cart.request_effect_approval(
        proposal_id=proposal.id,
        expected_proposal_basis=proposal.basis_hash,
        expected_project_revision=2,
        idempotency_key="req-cart",
    )

    assert approval_redirect.effect_kind == "shopping.create_redirect"
    assert approval_cart.effect_kind == "shopping.create_cart"
    assert approval_redirect.scope_hash != approval_cart.scope_hash
    assert approval_redirect.constraints["handoff_kind"] == "product_redirect"
    assert approval_cart.constraints["handoff_kind"] == "cart_redirect"


@pytest.mark.asyncio
async def test_taobao_handoff_idempotency_replay() -> None:
    """A replayed handoff returns the stored PREPARED, never calls provider twice."""
    store = InMemoryDomainStore()
    provider = FakeShoppingProvider()
    provider.set_handoff_kinds(frozenset({HandoffKind.PRODUCT_REDIRECT}))
    proposal = _add_taobao_checkout_scope(store, "replay")
    app = ShoppingApplication(store, provider)

    approval = await app.request_effect_approval(
        proposal_id=proposal.id,
        expected_proposal_basis=proposal.basis_hash,
        expected_project_revision=1,
        idempotency_key="req-replay",
    )
    approved = await app.resolve_effect_approval(
        approval_id=approval.id,
        decision=EffectApprovalStatus.APPROVED,
        scope_hash=approval.scope_hash,
        reason=None,
        expected_project_revision=2,
        idempotency_key="approve-replay",
    )

    first = await app.create_checkout_handoff(
        proposal_id=proposal.id,
        effect_approval_id=approved.id,
        expected_proposal_basis=proposal.basis_hash,
        expected_project_revision=3,
        idempotency_key="handoff-replay",
    )
    assert first.status == CheckoutHandoffStatus.DISPATCHED
    assert provider.handoff_create_called == 1

    # Replay: should return DISPATCHED without calling provider again
    second = await app.create_checkout_handoff(
        proposal_id=proposal.id,
        effect_approval_id=approved.id,
        expected_proposal_basis=proposal.basis_hash,
        expected_project_revision=3,
        idempotency_key="handoff-replay",
    )
    assert second.status == CheckoutHandoffStatus.DISPATCHED
    assert second.id == first.id
    assert provider.handoff_create_called == 1  # Not called again


@pytest.mark.asyncio
async def test_taobao_handoff_crash_ambiguity() -> None:
    """When the provider fails, the handoff is marked AMBIGUOUS."""
    store = InMemoryDomainStore()
    provider = FakeShoppingProvider()
    provider.set_handoff_kinds(frozenset({HandoffKind.PRODUCT_REDIRECT}))
    provider.set_handoff_failure(ShoppingProviderError("network failure"))
    proposal = _add_taobao_checkout_scope(store, "crash")
    app = ShoppingApplication(store, provider)

    approval = await app.request_effect_approval(
        proposal_id=proposal.id,
        expected_proposal_basis=proposal.basis_hash,
        expected_project_revision=1,
        idempotency_key="req-crash",
    )
    approved = await app.resolve_effect_approval(
        approval_id=approval.id,
        decision=EffectApprovalStatus.APPROVED,
        scope_hash=approval.scope_hash,
        reason=None,
        expected_project_revision=2,
        idempotency_key="approve-crash",
    )

    handoff = await app.create_checkout_handoff(
        proposal_id=proposal.id,
        effect_approval_id=approved.id,
        expected_proposal_basis=proposal.basis_hash,
        expected_project_revision=3,
        idempotency_key="handoff-crash",
    )
    assert handoff.status == CheckoutHandoffStatus.AMBIGUOUS
    assert handoff.resolved_at is not None

"""V1 Shopping domain contracts — OfferSnapshot, PurchaseProposal, CheckoutHandoff."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import uuid4

import pytest
from pydantic import ValidationError

from aidison.domain.models import (
    CheckoutHandoff,
    CheckoutHandoffStatus,
    EffectApproval,
    EffectApprovalStatus,
    OfferSnapshot,
    PurchaseProposal,
    PurchaseProposalStatus,
)


def digest(value: str) -> str:
    return sha256(value.encode()).hexdigest()


# ── OfferSnapshot ──────────────────────────────────────────────────────


def test_offer_snapshot_requires_snapshot_hash() -> None:
    with pytest.raises(ValidationError, match="snapshot_hash"):
        OfferSnapshot(
            project_id=uuid4(),
            solution_version_id=uuid4(),
            bom_line_id="power-module",
            provider="shopify",
            provider_offer_id="gid://shopify/ProductVariant/123",
            title="Raspberry Pi 5",
            availability="in_stock",
            unit_price="499.00",
            currency="CNY",
            region="CN",
            quantity_available=10,
            product_url="https://example.com/pi5",
            snapshot_hash="not-a-hash",
            provenance="test",
        )


def test_offer_snapshot_rejects_invalid_currency_length() -> None:
    with pytest.raises(ValidationError, match="currency"):
        OfferSnapshot(
            project_id=uuid4(),
            solution_version_id=uuid4(),
            bom_line_id="power-module",
            provider="shopify",
            provider_offer_id="gid://shopify/ProductVariant/123",
            title="Raspberry Pi 5",
            availability="in_stock",
            unit_price="499.00",
            currency="CNYY",
            region="CN",
            quantity_available=10,
            product_url="https://example.com/pi5",
            snapshot_hash=digest("offer"),
            provenance="test",
        )


def test_offer_snapshot_rejects_missing_product_url() -> None:
    with pytest.raises(ValidationError, match="product_url"):
        OfferSnapshot(
            project_id=uuid4(),
            solution_version_id=uuid4(),
            bom_line_id="power-module",
            provider="shopify",
            provider_offer_id="gid://shopify/ProductVariant/123",
            title="Raspberry Pi 5",
            availability="in_stock",
            unit_price="499.00",
            currency="CNY",
            region="CN",
            quantity_available=10,
            product_url="",
            snapshot_hash=digest("offer"),
            provenance="test",
        )


# ── PurchaseProposal ────────────────────────────────────────────────────


def test_purchase_proposal_requires_basis_hash() -> None:
    with pytest.raises(ValidationError, match="basis_hash"):
        PurchaseProposal(
            project_id=uuid4(),
            solution_version_id=uuid4(),
            offer_snapshot_id=uuid4(),
            quantity=1,
            region="CN",
            currency="CNY",
            max_total="600.00",
            unit_price="499.00",
            basis_hash="not-a-hex-hash",
        )


def test_purchase_proposal_handed_off_requires_timestamp() -> None:
    with pytest.raises(ValidationError, match="handed_off_at"):
        PurchaseProposal(
            project_id=uuid4(),
            solution_version_id=uuid4(),
            offer_snapshot_id=uuid4(),
            quantity=1,
            region="CN",
            currency="CNY",
            max_total="600.00",
            unit_price="499.00",
            status=PurchaseProposalStatus.HANDED_OFF,
            basis_hash=digest("basis"),
        )


def test_purchase_proposal_min_quantity() -> None:
    with pytest.raises(ValidationError, match="quantity"):
        PurchaseProposal(
            project_id=uuid4(),
            solution_version_id=uuid4(),
            offer_snapshot_id=uuid4(),
            quantity=0,
            region="CN",
            currency="CNY",
            max_total="600.00",
            unit_price="499.00",
            basis_hash=digest("basis"),
        )


# ── CheckoutHandoff ─────────────────────────────────────────────────────


def test_checkout_handoff_requires_https_url() -> None:
    with pytest.raises(ValidationError, match="HTTPS"):
        CheckoutHandoff(
            project_id=uuid4(),
            proposal_id=uuid4(),
            basis_hash=digest("basis"),
            provider="shopify",
            provider_cart_id="cart-1",
            checkout_url="http://evil.example.com/checkout",
            status=CheckoutHandoffStatus.DISPATCHED,
        )


def test_checkout_handoff_dispatched_requires_cart_id() -> None:
    with pytest.raises(ValidationError, match="provider_cart_id"):
        CheckoutHandoff(
            project_id=uuid4(),
            proposal_id=uuid4(),
            basis_hash=digest("basis"),
            provider="shopify",
            provider_cart_id=None,
            checkout_url="https://example.com/checkout",
            status=CheckoutHandoffStatus.DISPATCHED,
        )


def test_checkout_handoff_resolved_requires_timestamp() -> None:
    with pytest.raises(ValidationError, match="resolved_at"):
        CheckoutHandoff(
            project_id=uuid4(),
            proposal_id=uuid4(),
            basis_hash=digest("basis"),
            provider="shopify",
            status=CheckoutHandoffStatus.AMBIGUOUS,
        )


# ── EffectApproval ──────────────────────────────────────────────────────


def test_effect_approval_requested_has_immutable_frozen_scope() -> None:
    approval = EffectApproval(
        project_id=uuid4(),
        effect_kind="shopping.create_cart",
        target_ref=uuid4(),
        basis_hash=digest("approval-basis"),
        scope_hash=digest("approval-scope"),
        constraints={"quantity": 1, "provider": "shopify"},
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )
    assert approval.status is EffectApprovalStatus.REQUESTED
    with pytest.raises(ValidationError):
        approval.scope_hash = digest("changed")  # noqa: B018


def test_denied_effect_approval_requires_reason_and_resolution_time() -> None:
    base = {
        "project_id": uuid4(),
        "effect_kind": "shopping.create_cart",
        "target_ref": uuid4(),
        "basis_hash": digest("approval-basis"),
        "scope_hash": digest("approval-scope"),
        "constraints": {"quantity": 1},
        "expires_at": datetime.now(UTC) + timedelta(minutes=10),
        "status": EffectApprovalStatus.DENIED,
    }
    with pytest.raises(ValidationError, match="reason"):
        EffectApproval(**base, resolved_at=datetime.now(UTC))
    with pytest.raises(ValidationError, match="resolved_at"):
        EffectApproval(**base, resolution_reason="operator denied")


def test_consumed_effect_approval_requires_approval_and_consumption_timestamps() -> None:
    with pytest.raises(ValidationError, match="consumed_at"):
        EffectApproval(
            project_id=uuid4(),
            effect_kind="shopping.create_cart",
            target_ref=uuid4(),
            basis_hash=digest("approval-basis"),
            scope_hash=digest("approval-scope"),
            constraints={"quantity": 1},
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
            status=EffectApprovalStatus.CONSUMED,
            resolved_at=datetime.now(UTC),
        )


# ── Frozen domain contracts ─────────────────────────────────────────────


def test_offer_snapshot_is_immutable() -> None:
    snap = OfferSnapshot(
        project_id=uuid4(),
        solution_version_id=uuid4(),
        bom_line_id="power-module",
        provider="shopify",
        provider_offer_id="gid://variant/1",
        title="Pi 5",
        availability="in_stock",
        unit_price="500",
        currency="CNY",
        region="CN",
        quantity_available=5,
        product_url="https://example.com",
        snapshot_hash=digest("offer"),
        provenance="shopify:search",
    )
    with pytest.raises(ValidationError):
        snap.title = "Pi 6"  # noqa: B018

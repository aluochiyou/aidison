"""Tests for provider capabilities, HandoffKind, and provider-neutral handoff."""

from __future__ import annotations

import pytest

from aidison.domain.models import HandoffKind
from aidison.providers.shopify import ShopifyStorefrontAdapter
from aidison.providers.shopping import (
    CreatedCart,
    HandoffResult,
    ProductRedirect,
    ProviderCapabilities,
)
from aidison.providers.taobao import TaobaoAffiliateAdapter

# ── ProviderCapabilities ────────────────────────────────────────────────


def test_default_capabilities_are_search_and_cart_redirect() -> None:
    caps = ProviderCapabilities()
    assert caps.search is True
    assert caps.handoff_kinds == frozenset({HandoffKind.CART_REDIRECT})


def test_cart_only_provider_has_no_product_redirect() -> None:
    adapter = ShopifyStorefrontAdapter(
        store_domain="test.myshopify.com",
        public_token="tok",
    )
    caps = adapter.capabilities
    assert caps.search is True
    assert caps.handoff_kinds == frozenset({HandoffKind.CART_REDIRECT})
    assert HandoffKind.PRODUCT_REDIRECT not in caps.handoff_kinds


def test_search_only_provider_has_no_handoff_kinds() -> None:
    adapter = TaobaoAffiliateAdapter(
        app_key="k",
        app_secret="s",
        adzone_id="a",
    )
    caps = adapter.capabilities
    assert caps.search is True
    assert caps.handoff_kinds == frozenset()


# ── HandoffResult ────────────────────────────────────────────────────────


def test_handoff_result_cart_redirect_must_have_cart() -> None:
    with pytest.raises(ValueError, match="CreatedCart"):
        HandoffResult(kind=HandoffKind.CART_REDIRECT, cart=None)


def test_handoff_result_product_redirect_must_have_redirect() -> None:
    with pytest.raises(ValueError, match="ProductRedirect"):
        HandoffResult(kind=HandoffKind.PRODUCT_REDIRECT, redirect=None)


def test_handoff_result_cart_redirect_valid() -> None:
    cart = CreatedCart(
        provider_cart_id="cart-1",
        checkout_url="https://example.com/checkout",
        line_count=1,
    )
    result = HandoffResult(kind=HandoffKind.CART_REDIRECT, cart=cart)
    assert result.kind == HandoffKind.CART_REDIRECT
    assert result.cart is cart
    assert result.redirect is None


def test_handoff_result_product_redirect_valid() -> None:
    redirect = ProductRedirect(
        product_url="https://item.taobao.com/item.htm?id=123",
        provider_offer_id="123",
    )
    result = HandoffResult(kind=HandoffKind.PRODUCT_REDIRECT, redirect=redirect)
    assert result.kind == HandoffKind.PRODUCT_REDIRECT
    assert result.redirect is redirect
    assert result.cart is None

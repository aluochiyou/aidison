"""Provider-neutral ShoppingProvider contract for V1 minimal closed loop.

Implementations must be vendor-locked adapter wrappers, not generic brokers.
The contract supports product search and two handoff modes:
- ``CART_REDIRECT`` — create a provider-hosted cart with a checkout URL
  (Shopify Storefront).
- ``PRODUCT_REDIRECT`` — return an affiliate product link (Taobao).

No provider supports order placement, cancellation, refund, or payment capture.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from aidison.domain.models import HandoffKind


class ShoppingProviderError(RuntimeError):
    """Wraps upstream errors; message is safe for API responses."""


class ShoppingConfigError(RuntimeError):
    """Missing or invalid provider configuration — fail-closed."""


class OfferAvailability(StrEnum):
    IN_STOCK = "in_stock"
    OUT_OF_STOCK = "out_of_stock"
    PREORDER = "preorder"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ProviderCapabilities:
    """Declared capabilities of a shopping provider.

    Every provider supports search.  Handoff support indicates which
    ``HandoffKind`` values the provider can deliver.
    """

    search: bool = True
    handoff_kinds: frozenset[HandoffKind] = frozenset({HandoffKind.CART_REDIRECT})


@dataclass(frozen=True)
class ShoppingOffer:
    """Provider-agnostic product offer returned by search."""

    provider: str
    provider_offer_id: str
    merchandise_id: str | None = None
    seller: str | None = None
    title: str = ""
    condition: str | None = None
    availability: OfferAvailability = OfferAvailability.UNKNOWN
    unit_price: str = ""
    currency: str = ""
    shipping_estimate: str | None = None
    tax_estimate: str | None = None
    region: str = ""
    quantity_available: int = 0
    product_url: str = ""

    # When the provider responded (UTC).  Used to populate OfferSnapshot.observed_at.
    observed_at: datetime | None = None

    # When the offer's pricing is valid until, per the provider.
    expires_at: datetime | None = None

    # Opaque provider metadata stored on the snapshot for debugging only.
    raw_provider_payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class CartLineInput:
    """A single line to be added to the provider-hosted cart."""

    merchandise_id: str
    quantity: int


@dataclass(frozen=True)
class CreatedCart:
    """A provider-hosted cart ready for checkout redirection."""

    provider_cart_id: str
    checkout_url: str
    line_count: int
    raw_provider_payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProductRedirect:
    """An affiliate product link redirect (Taobao)."""

    product_url: str
    provider_offer_id: str
    raw_provider_payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class HandoffResult:
    """Provider-agnostic handoff result.

    Exactly one of ``cart`` or ``redirect`` is set depending on ``kind``.
    """

    kind: HandoffKind
    cart: CreatedCart | None = None
    redirect: ProductRedirect | None = None

    def __post_init__(self) -> None:
        if self.kind is HandoffKind.CART_REDIRECT and self.cart is None:
            raise ValueError("CART_REDIRECT handoff must include a CreatedCart")
        if self.kind is HandoffKind.PRODUCT_REDIRECT and self.redirect is None:
            raise ValueError("PRODUCT_REDIRECT handoff must include a ProductRedirect")


class ShoppingProvider:
    """Vendor-locked adapter contract.

    Every method raises ``ShoppingProviderError`` on upstream failures and
    ``ShoppingConfigError`` when configuration is missing.  Callers must
    not interpret raw response bodies.

    Implementations must honour:
    - **strict timeout** (no open-ended hangs)
    - **result cap** (search must bound results)
    - **HTTPS-only** (reject plaintext upstream URLs)
    - **error sanitisation** (do not leak API keys, raw payloads)
    """

    @property
    def name(self) -> str:
        """Unique provider identifier, e.g. ``"shopify"``."""
        raise NotImplementedError

    @property
    def available(self) -> bool:
        """False when required configuration is missing (fail-closed)."""
        raise NotImplementedError

    @property
    def capabilities(self) -> ProviderCapabilities:
        """Declared provider capabilities.

        Default: search + ``CART_REDIRECT`` (backward-compatible with Shopify).
        """
        return ProviderCapabilities()

    async def search_offers(
        self,
        query: str,
        *,
        region: str = "CN",
        max_results: int = 10,
        timeout_seconds: float = 5.0,
    ) -> Sequence[ShoppingOffer]:
        """Search provider products.

        Args:
            query: Free-text product search phrase.
            region: 2-letter ISO country code to scope availability/currency.
            max_results: Hard upper bound; the adapter must truncate.
            timeout_seconds: Per-request deadline.
        """
        raise NotImplementedError

    async def create_handoff(
        self,
        *,
        kind: HandoffKind,
        lines: Sequence[CartLineInput] | None = None,
        offer: ShoppingOffer | None = None,
        region: str = "CN",
        quantity: int = 1,
        timeout_seconds: float = 5.0,
    ) -> HandoffResult:
        """Create a provider handoff.

        Args:
            kind: The handoff kind to perform.
            lines: Cart line items (required for CART_REDIRECT).
            offer: The offer to redirect to (required for PRODUCT_REDIRECT).
            region: 2-letter ISO country code.
            quantity: Suggested quantity for display only (PRODUCT_REDIRECT).
            timeout_seconds: Per-request deadline.
        """
        raise NotImplementedError

    # Backward-compatible cart create delegate
    async def create_cart(
        self,
        lines: Sequence[CartLineInput],
        *,
        region: str = "CN",
        timeout_seconds: float = 5.0,
    ) -> CreatedCart:
        """Create a provider-hosted cart and return a checkout URL.

        Deprecated: prefer ``create_handoff(kind=CART_REDIRECT, ...)``.
        """
        result = await self.create_handoff(
            kind=HandoffKind.CART_REDIRECT,
            lines=lines,
            region=region,
            timeout_seconds=timeout_seconds,
        )
        if result.cart is None:
            raise ShoppingProviderError("create_cart produced no cart")
        return result.cart

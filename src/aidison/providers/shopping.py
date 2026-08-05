"""Provider-neutral ShoppingProvider contract for V1 minimal closed loop.

Implementations must be vendor-locked adapter wrappers, not generic brokers.
The contract only supports product search and cart creation for checkout
redirection — no order placement, cancellation, refund, or payment capture.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


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


class ShoppingProvider:
    """Vendor-locked adapter contract.

    Every method raises `ShoppingProviderError` on upstream failures and
    `ShoppingConfigError` when configuration is missing.  Callers must
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

    async def create_cart(
        self,
        lines: Sequence[CartLineInput],
        *,
        region: str = "CN",
        timeout_seconds: float = 5.0,
    ) -> CreatedCart:
        """Create a provider-hosted cart and return a checkout URL.

        Args:
            lines: Merchandise IDs and quantities to add.
            region: 2-letter ISO country code.
            timeout_seconds: Per-request deadline.
        """
        raise NotImplementedError

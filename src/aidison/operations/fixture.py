"""Deterministic V1 shopping fixture constants and a Fake ShoppingProvider.

Reusable by integration tests and uvicorn aidison.operations.fixture:app.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI

from aidison.domain.models import HandoffKind
from aidison.providers.shopping import (
    CartLineInput,
    CreatedCart,
    HandoffResult,
    OfferAvailability,
    ProductRedirect,
    ProviderCapabilities,
    ShoppingOffer,
    ShoppingProvider,
    ShoppingProviderError,
)

# ── Scenario constants (non-drone desktop environmental monitor) ──────

SCENARIO_SEARCH_QUERY = "SCD41 CO2 temperature humidity sensor module"
SCENARIO_BOM_LINE_ID = "environment-sensor"
SCENARIO_REGION = "CN"
SCENARIO_CURRENCY = "CNY"

SCENARIO_OFFER = ShoppingOffer(
    provider="fake-desktop",
    provider_offer_id="gid://fake-desktop/ProductVariant/scd41",
    merchandise_id="gid://fake-desktop/ProductVariant/scd41",
    seller="Fake Desktop Supplies",
    title="SCD41 CO2 Temperature Humidity Sensor Module",
    condition="new",
    availability=OfferAvailability.IN_STOCK,
    unit_price="168.00",
    currency="CNY",
    shipping_estimate="12.00",
    tax_estimate="20.16",
    region="CN",
    quantity_available=10,
    product_url="https://fake-desktop.example.com/products/scd41",
    observed_at=datetime(2026, 1, 1, tzinfo=UTC),
)

SCENARIO_CART = CreatedCart(
    provider_cart_id="fake-cart-scd41",
    checkout_url="https://fake-desktop.example.com/checkout/cart-scd41",
    line_count=1,
)

SCENARIO_MAX_TOTAL = "220.00"
SCENARIO_QUANTITY = 1


class FakeShoppingProvider(ShoppingProvider):
    """Deterministic provider that returns scenario constants.

    Can be configured to fail search or handoff for testing
    failure branches.  Supports both CART_REDIRECT and PRODUCT_REDIRECT.
    """

    def __init__(self) -> None:
        self.search_called = 0
        self.handoff_create_called = 0
        self._search_failure: Exception | None = None
        self._handoff_failure: Exception | None = None
        self._search_offers: list[ShoppingOffer] = [SCENARIO_OFFER]
        self._handoff_kinds: frozenset[HandoffKind] = frozenset(
            {HandoffKind.CART_REDIRECT, HandoffKind.PRODUCT_REDIRECT}
        )

    @property
    def name(self) -> str:
        return "fake-desktop"

    @property
    def available(self) -> bool:
        return True

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(search=True, handoff_kinds=self._handoff_kinds)

    def set_search_failure(self, exc: Exception) -> None:
        self._search_failure = exc

    def set_handoff_failure(self, exc: Exception) -> None:
        self._handoff_failure = exc

    def set_handoff_kinds(self, kinds: frozenset[HandoffKind]) -> None:
        self._handoff_kinds = kinds

    def set_search_offers(self, offers: list[ShoppingOffer]) -> None:
        self._search_offers = list(offers)

    async def search_offers(
        self,
        query: str,
        *,
        region: str = "CN",
        max_results: int = 10,
        timeout_seconds: float = 5.0,
    ) -> Sequence[ShoppingOffer]:
        self.search_called += 1
        if self._search_failure is not None:
            raise self._search_failure
        return tuple(self._search_offers[:max_results])

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
        self.handoff_create_called += 1
        if self._handoff_failure is not None:
            raise self._handoff_failure
        if kind is HandoffKind.CART_REDIRECT:
            return HandoffResult(kind=HandoffKind.CART_REDIRECT, cart=SCENARIO_CART)
        if kind is HandoffKind.PRODUCT_REDIRECT:
            redirect = ProductRedirect(
                product_url=(
                    offer.product_url
                    if offer
                    else "https://item.taobao.com/item.htm?id=99999999"
                ),
                provider_offer_id=offer.provider_offer_id if offer else "tb-99999",
                raw_provider_payload={"disclaimer": "fake redirect"},
            )
            return HandoffResult(kind=HandoffKind.PRODUCT_REDIRECT, redirect=redirect)
        raise ShoppingProviderError(f"fake provider does not support handoff kind {kind.value}")

def build_fixture_app(
    artifact_root: Path | None = None,
) -> FastAPI:
    """Build a FastAPI app wired to the fake desktop shopping provider."""
    from aidison.api.app import create_app

    return create_app(
        shopping_provider=FakeShoppingProvider(),
        artifact_root=artifact_root or Path("artifacts/data"),
    )


# Runnable application entry point for uvicorn.
app = build_fixture_app()

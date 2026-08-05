"""Deterministic V1 shopping fixture constants and a Fake ShoppingProvider.

Reusable by integration tests and potentially by the application entry-point
for local development when no live provider is configured.
"""

from __future__ import annotations

from collections.abc import Sequence

from aidison.providers.shopping import (
    CartLineInput,
    CreatedCart,
    OfferAvailability,
    ShoppingOffer,
    ShoppingProvider,
)

# ── Scenario constants (non-drone desktop environmental monitor) ──────

SCENARIO_SEARCH_QUERY = "Raspberry Pi 5 8GB single board computer"
SCENARIO_BOM_LINE_ID = "pi5-line"
SCENARIO_REGION = "CN"
SCENARIO_CURRENCY = "CNY"

SCENARIO_OFFER = ShoppingOffer(
    provider="fake-desktop",
    provider_offer_id="gid://fake-desktop/ProductVariant/pi5-8gb",
    merchandise_id="gid://fake-desktop/ProductVariant/pi5-8gb",
    seller="Fake Desktop Supplies",
    title="Raspberry Pi 5 8GB",
    condition="new",
    availability=OfferAvailability.IN_STOCK,
    unit_price="499.00",
    currency="CNY",
    shipping_estimate="15.00",
    tax_estimate="59.88",
    region="CN",
    quantity_available=10,
    product_url="https://fake-desktop.example.com/products/pi5-8gb",
)

SCENARIO_CART = CreatedCart(
    provider_cart_id="fake-cart-pi5-8gb",
    checkout_url="https://fake-desktop.example.com/checkout/cart-pi5-8gb",
    line_count=1,
)

SCENARIO_MAX_TOTAL = "600.00"
SCENARIO_QUANTITY = 1


class FakeShoppingProvider(ShoppingProvider):
    """Deterministic provider that returns scenario constants.

    Can be configured to fail search or cart creation for testing
    failure branches.
    """

    def __init__(self) -> None:
        self.search_called = 0
        self.cart_create_called = 0
        self._search_failure: Exception | None = None
        self._cart_failure: Exception | None = None
        self._search_offers: list[ShoppingOffer] = [SCENARIO_OFFER]

    @property
    def name(self) -> str:
        return "fake-desktop"

    @property
    def available(self) -> bool:
        return True

    def set_search_failure(self, exc: Exception) -> None:
        self._search_failure = exc

    def set_cart_failure(self, exc: Exception) -> None:
        self._cart_failure = exc

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

    async def create_cart(
        self,
        lines: Sequence[CartLineInput],
        *,
        region: str = "CN",
        timeout_seconds: float = 5.0,
    ) -> CreatedCart:
        self.cart_create_called += 1
        if self._cart_failure is not None:
            raise self._cart_failure
        return CreatedCart(
            provider_cart_id="fake-cart-1",
            checkout_url=f"https://fake-desktop.example.com/checkout/{lines[0].merchandise_id}",
            line_count=len(lines),
        )

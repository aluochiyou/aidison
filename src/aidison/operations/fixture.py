"""Deterministic V1 shopping fixture constants and a Fake ShoppingProvider.

Reusable by integration tests and uvicorn aidison.operations.fixture:app.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI

from aidison.providers.shopping import (
    CartLineInput,
    CreatedCart,
    OfferAvailability,
    ShoppingOffer,
    ShoppingProvider,
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
        return SCENARIO_CART

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

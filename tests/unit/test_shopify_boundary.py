from __future__ import annotations

import httpx
import pytest

from aidison.providers.shopify import ShopifyStorefrontAdapter
from aidison.providers.shopping import ShoppingProviderError


def _adapter(client: httpx.AsyncClient) -> ShopifyStorefrontAdapter:
    return ShopifyStorefrontAdapter(
        store_domain="fixture.myshopify.com",
        public_token="fixture-token",
        http_client=client,
    )


@pytest.mark.asyncio
async def test_shopify_rejects_non_json_response() -> None:
    def respond(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not json</html>")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(ShoppingProviderError, match="content type"):
            await _adapter(client).search_offers("sensor")


@pytest.mark.asyncio
async def test_shopify_rejects_oversized_response() -> None:
    def respond(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=b"x" * (2 * 1024 * 1024 + 1),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(ShoppingProviderError, match="exceeds"):
            await _adapter(client).search_offers("sensor")

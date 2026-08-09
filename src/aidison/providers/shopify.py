"""Shopify Storefront GraphQL adapter.

Official endpoint: ``https://{store}.myshopify.com/api/{version}/graphql.json``

Uses the public Storefront API (unauthenticated or with a
`X-Shopify-Storefront-Access-Token` header) for product search and
cart creation → checkout URL redirection.

This adapter follows the ShoppingProvider contract: fail-closed when
configuration is missing, bounded results, strict timeouts, HTTPS
enforcement, and error sanitisation.
"""

from __future__ import annotations

import json
import re as _re
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import httpx

from aidison.domain.models import HandoffKind
from aidison.providers.shopping import (
    CartLineInput,
    CreatedCart,
    HandoffResult,
    OfferAvailability,
    ShoppingConfigError,
    ShoppingOffer,
    ShoppingProvider,
    ShoppingProviderError,
)

_STOREFRONT_API_VERSION = "2025-07"

_SHOP_DOMAIN_RE = _re.compile(
    r"^[a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?\.myshopify\.com$"
)

# Maximum bytes accepted from the Shopify GraphQL endpoint.
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024  # 2 MiB

# Expected GraphQL response content type.
_EXPECTED_CONTENT_TYPE = "application/json"

_SEARCH_PRODUCTS_QUERY = """
query searchProducts($q: String!, $first: Int!, $country: CountryCode!)
@inContext(country: $country) {
  products(first: $first, query: $q) {
    edges {
      node {
        id title handle descriptionHtml vendor onlineStoreUrl
        availableForSale totalInventory
        priceRange {
          minVariantPrice { amount currencyCode }
          maxVariantPrice { amount currencyCode }
        }
        variants(first: 10) {
          edges {
            node {
              id title availableForSale quantityAvailable
              price { amount currencyCode } compareAtPrice { amount currencyCode }
              selectedOptions { name value } product { id }
            }
          }
        }
      }
    }
  }
}
"""

_CART_CREATE_MUTATION = """
mutation cartCreate($lines: [CartLineInput!]!, $country: CountryCode!)
@inContext(country: $country) {
  cartCreate(input: { lines: $lines }) {
    cart {
      id checkoutUrl totalQuantity
      lines(first: 50) {
        edges { node { id quantity merchandise { ... on ProductVariant { id title } } } }
      }
    }
    userErrors { field message }
  }
}
"""


class ShopifyStorefrontAdapter(ShoppingProvider):
    """Storefront GraphQL adapter for a single Shopify store."""

    def __init__(
        self,
        *,
        store_domain: str | None = None,
        public_token: str | None = None,
        api_version: str = _STOREFRONT_API_VERSION,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._store_domain = (store_domain or "").strip()
        self._public_token = (public_token or "").strip()
        self._api_version = api_version.strip()
        self._http = http_client

    # ── ShoppingProvider contract ────────────────────────────────────────

    @property
    def name(self) -> str:
        return "shopify"

    @property
    def available(self) -> bool:
        return bool(self._store_domain) and bool(self._public_token) and bool(self._api_version)

    async def search_offers(
        self,
        query: str,
        *,
        region: str = "CN",
        max_results: int = 10,
        timeout_seconds: float = 5.0,
    ) -> Sequence[ShoppingOffer]:
        self._require_available()
        query = query.strip()
        if not query:
            raise ShoppingProviderError("search query must not be empty")
        max_results = max(1, min(max_results, 50))

        raw = await self._graphql(
            _SEARCH_PRODUCTS_QUERY,
            variables={"query": query, "first": max_results, "country": _region_code(region)},
            timeout_seconds=timeout_seconds,
        )
        return self._parse_search_results(query, raw, max_results, region)

    async def create_cart(
        self,
        lines: Sequence[CartLineInput],
        *,
        region: str = "CN",
        timeout_seconds: float = 5.0,
    ) -> CreatedCart:
        result = await self.create_handoff(
            kind=HandoffKind.CART_REDIRECT,
            lines=lines,
            region=region,
            timeout_seconds=timeout_seconds,
        )
        if result.cart is None:
            raise ShoppingProviderError("shopify create_cart produced no cart")
        return result.cart

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
        if kind is not HandoffKind.CART_REDIRECT:
            raise ShoppingProviderError(
                f"shopify does not support handoff kind {kind.value}"
            )
        if not lines:
            raise ShoppingProviderError("cart requires at least one line")
        cart = await self._create_cart_impl(lines, region=region, timeout_seconds=timeout_seconds)
        return HandoffResult(kind=HandoffKind.CART_REDIRECT, cart=cart)

    async def _create_cart_impl(
        self,
        lines: Sequence[CartLineInput],
        *,
        region: str = "CN",
        timeout_seconds: float = 5.0,
    ) -> CreatedCart:
        self._require_available()
        line_vars = [
            {"merchandiseId": line.merchandise_id, "quantity": line.quantity} for line in lines
        ]

        raw = await self._graphql(
            _CART_CREATE_MUTATION,
            variables={"lines": line_vars, "country": _region_code(region)},
            timeout_seconds=timeout_seconds,
        )

        return self._parse_cart_create(raw)

    # ── internals ─────────────────────────────────────────────────────────

    def _require_available(self) -> None:
        if not self.available:
            raise ShoppingConfigError(
                "shopify storefront is unavailable: missing store_domain or public_token"
            )

    @property
    def _endpoint(self) -> str:
        return f"https://{self._store_domain}/api/{self._api_version}/graphql.json"

    async def _graphql(
        self,
        query: str,
        *,
        variables: dict[str, Any] | None = None,
        timeout_seconds: float = 5.0,
    ) -> dict[str, Any]:
        url = self._endpoint
        if not url.startswith("https://"):
            raise ShoppingConfigError("shopify endpoint must use HTTPS")

        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Shopify-Storefront-Access-Token": self._public_token,
        }

        client = self._http or httpx.AsyncClient()
        try:
            async with client.stream(
                "POST",
                url,
                json={"query": query, "variables": variables or {}},
                headers=headers,
                timeout=timeout_seconds,
            ) as response:
                response.raise_for_status()

                content_type = response.headers.get("content-type", "")
                if _EXPECTED_CONTENT_TYPE not in content_type.lower():
                    raise ShoppingProviderError(
                        "shopify response has an unexpected content type"
                    )

                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > _MAX_RESPONSE_BYTES:
                        raise ShoppingProviderError(
                            f"shopify response exceeds {_MAX_RESPONSE_BYTES} bytes"
                        )
        except httpx.TimeoutException:
            raise ShoppingProviderError("shopify storefront request timed out") from None
        except httpx.HTTPStatusError as exc:
            raise ShoppingProviderError(
                f"shopify storefront returned HTTP {exc.response.status_code}"
            ) from None
        except httpx.RequestError as exc:
            raise ShoppingProviderError("shopify storefront request failed") from exc
        finally:
            if self._http is None:
                await client.aclose()

        try:
            decoded = json.loads(raw)
        except (TypeError, ValueError):
            raise ShoppingProviderError("shopify response contains invalid JSON") from None
        if not isinstance(decoded, dict):
            raise ShoppingProviderError("shopify response JSON must be an object")
        body: dict[str, Any] = decoded

        # Sanitize: remove detailed error metadata before logging
        if "errors" in body:
            raise ShoppingProviderError(
                f"shopify graphql error: {_summarise_graphql_errors(body['errors'])}"
            )
        return body

    def _parse_search_results(
        self, query: str, raw: dict[str, Any], max_results: int, region: str = "CN"
    ) -> Sequence[ShoppingOffer]:
        data = _deep_get(raw, "data", "products")
        if data is None:
            raise ShoppingProviderError("shopify search response is missing products data")

        edges = data.get("edges") or []
        observed = datetime.now(UTC)
        offers: list[ShoppingOffer] = []

        for edge in edges:
            if len(offers) >= max_results:
                break
            node = edge.get("node") or {}
            if not node:
                continue
            variants = _deep_get(node, "variants", "edges") or []
            vendor = (node.get("vendor") or "").strip() or None
            product_url = (node.get("onlineStoreUrl") or "").strip()
            product_id = node.get("id", "")

            for variant_edge in variants:
                if len(offers) >= max_results:
                    break
                vnode = variant_edge.get("node") or {}
                if not vnode:
                    continue
                variant_id = vnode.get("id", "")
                price_node = vnode.get("price") or {}
                price_range = _deep_get(node, "priceRange", "minVariantPrice") or {}

                unit_price = price_node.get("amount") or price_range.get("amount") or "0.00"
                currency = (
                    price_node.get("currencyCode")
                    or price_range.get("currencyCode")
                    or "CNY"
                ).upper()

                availability = OfferAvailability.UNKNOWN
                if vnode.get("availableForSale") is True:
                    qty = vnode.get("quantityAvailable")
                    if qty is None or qty > 0:
                        availability = OfferAvailability.IN_STOCK
                    else:
                        availability = OfferAvailability.OUT_OF_STOCK
                else:
                    availability = OfferAvailability.OUT_OF_STOCK

                qty_available = vnode.get("quantityAvailable")
                if qty_available is None:
                    qty_available = 1 if availability == OfferAvailability.IN_STOCK else 0
                else:
                    qty_available = max(0, int(qty_available))

                offers.append(
                    ShoppingOffer(
                        provider="shopify",
                        provider_offer_id=str(variant_id),
                        merchandise_id=str(variant_id),
                        seller=vendor,
                        title=node.get("title", query),
                        condition="new",
                        availability=availability,
                        unit_price=str(unit_price),
                        currency=currency,
                        shipping_estimate=None,
                        tax_estimate=None,
                        region=region,
                        quantity_available=max(0, int(qty_available)),
                        product_url=product_url or (
                            f"https://{self._store_domain}"
                            f"/products/{node.get('handle', '')}"
                        ),
                        observed_at=observed,
                        expires_at=None,
                        raw_provider_payload={
                            "product_id": product_id,
                            "variant_title": vnode.get("title", ""),
                            "shopify_store": self._store_domain,
                            "search_query": query,
                        },
                    )
                )
        return tuple(offers)

    def _parse_cart_create(self, raw: dict[str, Any]) -> CreatedCart:
        cart_create = _deep_get(raw, "data", "cartCreate")
        if cart_create is None:
            raise ShoppingProviderError("shopify cartCreate response is missing data")

        user_errors = cart_create.get("userErrors") or []
        if user_errors:
            messages = "; ".join(
                f"{err.get('field', '?')}: {err.get('message', 'unknown')}" for err in user_errors
            )
            raise ShoppingProviderError(f"shopify cartCreate user errors: {messages}")

        cart = cart_create.get("cart")
        if cart is None:
            raise ShoppingProviderError("shopify cartCreate returned no cart")

        checkout_url = cart.get("checkoutUrl") or ""
        if not checkout_url.startswith("https://"):
            raise ShoppingProviderError("shopify checkout URL is missing or not HTTPS")

        line_edges = _deep_get(cart, "lines", "edges") or []
        line_count = len(line_edges)

        return CreatedCart(
            provider_cart_id=cart["id"],
            checkout_url=checkout_url,
            line_count=line_count,
            raw_provider_payload={
                "total_quantity": cart.get("totalQuantity", 0),
                "store": self._store_domain,
            },
        )


# ── helpers ────────────────────────────────────────────────────────────


_REGION_ALIASES: dict[str, str] = {"EU": "DE", "UK": "GB"}


def _region_code(region: str) -> str:
    """Map aidison region to Shopify CountryCode (ISO 3166-1 alpha-2).

    Standard two-letter country codes pass through; UI aliases map to a
    concrete Storefront ``CountryCode``.
    """
    upper = region.strip().upper()
    if upper in _REGION_ALIASES:
        return _REGION_ALIASES[upper]
    if _re.fullmatch(r"[A-Z]{2}", upper):
        return upper
    raise ShoppingConfigError(f"unsupported region: {region}")


def _deep_get(data: dict[str, Any], *path: str) -> Any:
    current: Any = data
    for key in path:
        if isinstance(current, dict):
            current = current.get(key)
        else:
            return None
    return current


def _summarise_graphql_errors(errors: list[dict[str, Any]]) -> str:
    messages = [e.get("message", str(e)) for e in errors[:3]]
    return "; ".join(messages)

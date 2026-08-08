"""Taobao Affiliate TOP API adapter.

Supports the official Taobao Open Platform (TOP) API for:
- ``taobao.tbk.dg.material.optimal`` — keyword material search
- ``PRODUCT_REDIRECT`` — affiliate link handoff via ``coupon_share_url`` or
  ``tk_total_commission`` link generation.

This adapter follows the ShoppingProvider contract: fail-closed when
configuration is missing, bounded results, strict timeouts, HTTPS-only,
deterministic TOP signing, and error sanitisation.

Key constraints:
- **Never cart/order/payment** — Taobao only supports search + redirect.
- **Never fabricates cart IDs** — ``PRODUCT_REDIRECT`` produces a
  ``ProductRedirect``, never a ``CreatedCart``.
- **Availability always UNKNOWN** — Taobao inventory is not real-time.
- **Short TTL** — offers expire in 5 minutes by default.
- **Final-price/inventory disclaimer** — search results carry an explicit
  disclaimer that prices may differ at checkout.
"""

from __future__ import annotations

import hashlib
import json as _json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx

from aidison.domain.models import HandoffKind
from aidison.providers.shopping import (
    HandoffResult,
    OfferAvailability,
    ProductRedirect,
    ProviderCapabilities,
    ShoppingConfigError,
    ShoppingOffer,
    ShoppingProvider,
    ShoppingProviderError,
)

# ── Constants ────────────────────────────────────────────────────────────────

_TAOBAO_API_URL = "https://eco.taobao.com/router/rest"
_MAX_RESPONSE_BYTES = 1 * 1024 * 1024  # 1 MiB
_EXPECTED_CONTENT_TYPE = "application/json"
_DEFAULT_ADZONE_ID = ""  # Must be set in config.yaml
_DEFAULT_MAX_RESULTS = 20
_OFFER_TTL_MINUTES = 5
_SEARCH_TIMEOUT_DEFAULT = 5.0

# Only these hosts may appear in redirect URLs.
_ALLOWED_REDIRECT_HOSTS: frozenset[str] = frozenset(
    {
        "item.taobao.com",
        "detail.tmall.com",
        "uland.taobao.com",
        "s.click.taobao.com",
    }
)

# Kept for backward compat in tests; not referenced by production code.
TAOBAO_ALLOWED_HOSTS = _ALLOWED_REDIRECT_HOSTS


# ── TOP signing ──────────────────────────────────────────────────────────────


def _top_sign(params: dict[str, str], secret: str) -> str:
    """Compute the TOP MD5 signature for the given parameters.

    Implementation follows the official TOP signing protocol:
    1. Sort parameters lexicographically.
    2. Concatenate with ``{secret}`` prefix and suffix (not interpolated into the string).
    3. MD5 hex digest, uppercased.

    This is deterministic given the same inputs.
    """
    sorted_keys = sorted(params)
    canonical = "".join(f"{key}{params[key]}" for key in sorted_keys)
    # The official TOP signing concatenation is literally:
    # secret + canonical + secret
    payload = f"{secret}{canonical}{secret}"
    return hashlib.md5(payload.encode("utf-8")).hexdigest().upper()


# ── URL validation ───────────────────────────────────────────────────────────


def _is_allowed_redirect_url(url: str) -> bool:
    """Check whether ``url`` starts with ``https://`` and its host is allowed."""
    if not url.startswith("https://"):
        return False
    # Simple extraction — avoids depending on urllib.parse for a well-known pattern.
    host_end = url.find("/", 8)
    host = url[8:host_end] if host_end != -1 else url[8:]
    # Strip port if present
    host = host.split(":", 1)[0]
    return host in _ALLOWED_REDIRECT_HOSTS


# ── Response helpers ─────────────────────────────────────────────────────────


def _deep_get(data: dict[str, Any], *path: str) -> Any:
    current: Any = data
    for key in path:
        if isinstance(current, dict):
            current = current.get(key)
        else:
            return None
    return current


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ── Adapter ──────────────────────────────────────────────────────────────────


class TaobaoAffiliateAdapter(ShoppingProvider):
    """TOP API adapter for Taobao affiliate product search and redirect.

    Secrets come from environment only (TAOBAO_APP_KEY, TAOBAO_APP_SECRET).
    Non-secret defaults (adzone_id) come from config.yaml.
    """

    def __init__(
        self,
        *,
        app_key: str | None = None,
        app_secret: str | None = None,
        adzone_id: str | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._app_key = (app_key or "").strip()
        self._app_secret = (app_secret or "").strip()
        self._adzone_id = (adzone_id or _DEFAULT_ADZONE_ID).strip()
        self._http = http_client

    # ── ShoppingProvider contract ────────────────────────────────────────

    @property
    def name(self) -> str:
        return "taobao"

    @property
    def available(self) -> bool:
        return bool(self._app_key) and bool(self._app_secret) and bool(self._adzone_id)

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            search=True,
            handoff_kinds=frozenset({HandoffKind.PRODUCT_REDIRECT}),
        )

    async def search_offers(
        self,
        query: str,
        *,
        region: str = "CN",
        max_results: int = 10,
        timeout_seconds: float = 5.0,
    ) -> Sequence[ShoppingOffer]:
        """Search Taobao products via keyword material API.

        The Taobao API only supports CN region.
        """
        self._require_available()
        query = query.strip()
        if not query:
            raise ShoppingProviderError("search query must not be empty")
        max_results = max(1, min(max_results, 50))

        # Taobao API uses "material_query" parameter, not "q"
        params = self._build_params(
            method="taobao.tbk.dg.material.optimal",
            extra={
                "adzone_id": self._adzone_id,
                "material_id": "13366",  # Default promotion material ID
                "page_size": str(min(max_results, _DEFAULT_MAX_RESULTS)),
                "page_no": "1",
                "q": query,
                "platform": "2",  # Cross-platform result format
            },
        )
        raw = await self._call_api(params, timeout_seconds=timeout_seconds)
        return self._parse_search_results(query, raw, max_results, region)

    async def create_handoff(
        self,
        *,
        kind: HandoffKind,
        lines: Sequence[Any] | None = None,
        offer: ShoppingOffer | None = None,
        region: str = "CN",
        quantity: int = 1,
        timeout_seconds: float = 5.0,
    ) -> HandoffResult:
        """Create a Taobao PRODUCT_REDIRECT handoff.

        CART_REDIRECT is not supported.  PRODUCT_REDIRECT returns the
        ``product_url`` from the offer — the Taobao API does not generate
        per-transaction deeplinks; the offer's existing ``coupon_share_url``
        / ``item_url`` is used directly.
        """
        if kind is not HandoffKind.PRODUCT_REDIRECT:
            raise ShoppingProviderError(
                f"taobao does not support handoff kind {kind.value}"
            )
        if offer is None:
            raise ShoppingProviderError("PRODUCT_REDIRECT requires an offer")
        self._require_available()

        # Validate the redirect URL against the allowlist
        product_url = offer.product_url
        if not _is_allowed_redirect_url(product_url):
            raise ShoppingProviderError(
                "taobao redirect failed: offer product_url is not in the allowed hosts"
            )

        redirect = ProductRedirect(
            product_url=product_url,
            provider_offer_id=offer.provider_offer_id,
            raw_provider_payload={
                "provider_offer_id": offer.provider_offer_id,
                "title": offer.title[:200],
                "unit_price": offer.unit_price,
            },
        )
        return HandoffResult(kind=HandoffKind.PRODUCT_REDIRECT, redirect=redirect)

    # Backward-compat — Taobao never supports cart creation
    async def create_cart(
        self,
        lines: Sequence[Any],
        *,
        region: str = "CN",
        timeout_seconds: float = 5.0,
    ) -> Any:
        raise ShoppingProviderError("taobao does not support cart creation")

    # ── internals ─────────────────────────────────────────────────────────

    def _require_available(self) -> None:
        if not self.available:
            raise ShoppingConfigError(
                "taobao affiliate adapter is unavailable: "
                "missing TAOBAO_APP_KEY, TAOBAO_APP_SECRET, or adzone_id"
            )

    def _build_params(
        self,
        *,
        method: str,
        extra: dict[str, str] | None = None,
    ) -> dict[str, str]:
        """Build signed TOP request parameters."""
        params: dict[str, str] = {
            "method": method,
            "app_key": self._app_key,
            "format": "json",
            "v": "2.0",
            "sign_method": "md5",
            "timestamp": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S"),
        }
        if extra:
            params.update(extra)
        params["sign"] = _top_sign(params, self._app_secret)
        return params

    async def _call_api(
        self,
        params: dict[str, str],
        timeout_seconds: float = 5.0,
    ) -> dict[str, Any]:
        """POST to the TOP API and return the parsed response body.

        Enforces HTTPS, content-type, response size, and timeout.
        Redacts the sign and app_secret from any error output.
        """
        url = _TAOBAO_API_URL
        headers: dict[str, str] = {
            "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
            "Accept": "application/json",
        }
        body = urlencode(params)

        client = self._http or httpx.AsyncClient()
        try:
            async with client.stream(
                "POST",
                url,
                content=body,
                headers=headers,
                timeout=timeout_seconds,
            ) as response:
                # Read body first — TOP API often returns error_response
                # in the body even for non-2xx status codes.
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > _MAX_RESPONSE_BYTES:
                        raise ShoppingProviderError(
                            f"taobao response exceeds {_MAX_RESPONSE_BYTES} bytes"
                        )

                content_type = response.headers.get("content-type", "")
                if _EXPECTED_CONTENT_TYPE not in content_type.lower() and response.is_success:
                    raise ShoppingProviderError(
                        "taobao response has an unexpected content type"
                    )

                # Parse body to check for error_response regardless of HTTP status
                try:
                    decoded = _json.loads(raw)
                except (TypeError, ValueError):
                    raise ShoppingProviderError("taobao response contains invalid JSON") from None
                if not isinstance(decoded, dict):
                    raise ShoppingProviderError("taobao response JSON must be an object")
                body_dict: dict[str, Any] = decoded

                # Check for TOP-level error response (present on 200 and non-200)
                error = body_dict.get("error_response")
                if error is not None:
                    code = error.get("code", "unknown")
                    sub_msg = error.get("sub_msg") or error.get("msg", "top api error")
                    raise ShoppingProviderError(f"taobao top api error [{code}]: {sub_msg}")

                # If no error_response but HTTP error, raise accordingly
                if not response.is_success:
                    response.raise_for_status()

                return body_dict
        except httpx.TimeoutException:
            raise ShoppingProviderError("taobao TOP API request timed out") from None
        except httpx.HTTPStatusError as exc:
            raise ShoppingProviderError(
                f"taobao TOP API returned HTTP {exc.response.status_code}"
            ) from None
        except httpx.RequestError as exc:
            raise ShoppingProviderError("taobao TOP API request failed") from exc
        finally:
            if self._http is None:
                await client.aclose()

    def _parse_search_results(
        self,
        query: str,
        raw: dict[str, Any],
        max_results: int,
        region: str,
    ) -> Sequence[ShoppingOffer]:
        """Parse TOP material search response into ShoppingOffer list."""
        result_list = _deep_get(
            raw,
            "tbk_dg_material_optimal_response",
            "result_list",
            "map_data",
        )
        if result_list is None:
            raise ShoppingProviderError(
                "taobao search response is missing result_list data"
            )

        # result_list may be a list or a dict keyed by index
        if isinstance(result_list, dict):
            items = list(result_list.values())
        elif isinstance(result_list, list):
            items = result_list
        else:
            raise ShoppingProviderError("taobao search result_list has an unexpected format")

        observed = datetime.now(UTC)
        expires_at = observed + timedelta(minutes=_OFFER_TTL_MINUTES)
        offers: list[ShoppingOffer] = []
        details: list[dict[str, Any]] = []

        for item in items:
            if not isinstance(item, dict):
                continue
            # Flatten: some API versions nest data under fro_descend etc.
            # Accept either a flat item or one with nested fields.
            details.append(item)

        for item in details:
            if len(offers) >= max_results:
                break

            title = str(item.get("title") or item.get("item_title") or query)
            num_iid = str(item.get("num_iid") or item.get("item_id") or "")
            seller = str(
                item.get("nick") or item.get("seller_nick") or item.get("shop_title") or ""
            )

            # Price: Decimal-safe — use string directly
            raw_price = item.get("zk_final_price") or item.get("reserve_price") or "0.00"
            # Handle float-like values from API
            unit_price = _safe_decimal_str(raw_price)

            # The offer URL for product redirect
            product_url = str(
                item.get("coupon_share_url")
                or item.get("item_url")
                or item.get("url")
                or ""
            ).strip()

            currency = "CNY"

            # Post-coupon price is an approximation; always warn
            coupon_amount = item.get("coupon_amount") or "0"
            coupon_start_fee = item.get("coupon_start_fee") or "0"

            offers.append(
                ShoppingOffer(
                    provider="taobao",
                    provider_offer_id=num_iid or f"tb:{_hash_str(title[:50])}",
                    merchandise_id=num_iid or None,
                    seller=seller or None,
                    title=title[:1000],
                    condition="new",
                    availability=OfferAvailability.UNKNOWN,
                    unit_price=unit_price,
                    currency=currency,
                    shipping_estimate=None,
                    tax_estimate=None,
                    region="CN",
                    quantity_available=0,  # Taobao never exposes real-time inventory
                    product_url=product_url,
                    observed_at=observed,
                    expires_at=expires_at,
                    raw_provider_payload={
                        "num_iid": num_iid,
                        "coupon_amount": str(coupon_amount),
                        "coupon_start_fee": str(coupon_start_fee),
                        "search_query": query,
                        "disclaimer": (
                            "实际成交价格和库存请以淘宝/天猫商品详情页为准。"
                            "This is an affiliate search result; "
                            "final price and inventory are not guaranteed."
                        ),
                    },
                )
            )
        return tuple(offers)


# ── helpers ──────────────────────────────────────────────────────────────────


def _safe_decimal_str(value: Any) -> str:
    """Return a safe decimal string from a potentially-float API value.

    Preserves the original string representation when the value is already a string
    that looks numeric; uses Decimal for safety when sanitizing non-string inputs.
    """
    from decimal import Decimal, InvalidOperation

    if isinstance(value, str):
        try:
            Decimal(value)
            return value  # already a valid decimal string — preserve precision
        except InvalidOperation:
            return "0.00"
    if isinstance(value, (int, float)):
        try:
            return str(Decimal(str(value)))
        except InvalidOperation:
            return "0.00"
    return "0.00"


def _hash_str(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]

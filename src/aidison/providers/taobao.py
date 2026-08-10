"""Taobao Affiliate TOP API adapter — search/recommendation only.

Supports the official Taobao Open Platform (TOP) API for:
- ``taobao.tbk.dg.material.optional.upgrade`` — keyword material search
  (淘宝客-推广者-物料搜索升级版，通用物料搜索API（导购）).

This adapter follows the ShoppingProvider contract: fail-closed when
configuration is missing, bounded results, strict timeouts, HTTPS-only,
deterministic TOP signing, and error sanitisation.

Key constraints:
- **Search-only** — Taobao never creates carts, orders, payments, or
  product-redirect handoffs.  ``create_handoff`` / ``create_cart`` always
  fail closed with ``ShoppingProviderError``.
- **Availability always UNKNOWN / quantity 0** — Taobao inventory is not
  real-time, so offers are never purchaseable or proposable.
- **Short TTL** — offers expire in 5 minutes by default.
- **Final-price/inventory disclaimer** — search results carry an explicit
  disclaimer that prices may differ at checkout.

Official contract (source: the latest ``docs/taobao/api文档`` in the main
Aidison workspace):
- request method ``taobao.tbk.dg.material.optional.upgrade``, 免费不需用户授权;
- keyword ``q`` (affiliate links are NOT accepted as ``q``), default
  ``material_id=80309``, required ``adzone_id``, optional
  ``page_no``/``page_size``;
- response envelope ``tbk_dg_material_optional_upgrade_response`` with
  ``result_list.map_data[]`` items (``item_id``, ``item_basic_info``,
  ``price_promotion_info``, ``publish_info``).

not_checked (requires an authenticated TOP call or the TOP signing spec):
- the TOP MD5 signing canonicalisation — the provided doc does not reproduce
  the signing algorithm (value URL-encoding, canonical composition);
- which optional response fields are actually populated on a live response.
Do not call live TOP until signing is verified and a golden-vector signing
test is added.
"""

from __future__ import annotations

import hashlib
import json as _json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

import httpx
from pydantic import Field

from aidison.config import AidisonSettings
from aidison.domain.models import HandoffKind
from aidison.providers.shopping import (
    HandoffResult,
    OfferAvailability,
    ProviderCapabilities,
    ShoppingConfigError,
    ShoppingOffer,
    ShoppingProvider,
    ShoppingProviderError,
)

# ── Constants ────────────────────────────────────────────────────────────────

_TAOBAO_API_URL = "https://eco.taobao.com/router/rest"
_MAX_RESPONSE_BYTES = 1 * 1024 * 1024  # 1 MiB
_ACCEPTED_JSON_CONTENT_TYPES: frozenset[str] = frozenset(
    {"application/json", "text/javascript"}
)
_DEFAULT_ADZONE_ID = ""  # Must be set in config.yaml
_DEFAULT_MATERIAL_ID = "80309"  # Official default material id
_DEFAULT_MAX_RESULTS = 20
_OFFER_TTL_MINUTES = 5
_SEARCH_TIMEOUT_DEFAULT = 5.0
# Official TOP timestamp timezone is GMT+8; UTC would be ~8h stale and exceed
# the 10-minute server tolerance.
_GMT_PLUS_8 = timezone(timedelta(hours=8))


class TaobaoSettings(AidisonSettings):
    """Non-secret Taobao configuration read from the ``taobao:`` YAML section.

    Secrets (TAOBAO_APP_KEY, TAOBAO_APP_SECRET) never appear here; the
    adapter wiring reads them from the process environment only.
    """

    yaml_section = "taobao"

    adzone_id: str = Field(default=_DEFAULT_ADZONE_ID)
    search_timeout_seconds: float = Field(default=_SEARCH_TIMEOUT_DEFAULT, ge=0.1, le=30)
    max_search_results: int = Field(default=_DEFAULT_MAX_RESULTS, ge=1, le=50)


# ── TOP signing ──────────────────────────────────────────────────────────────


def _top_sign(params: dict[str, str], secret: str) -> str:
    """Compute the TOP MD5 signature for the given parameters.

    Deterministic: sort keys lexicographically, concatenate
    ``secret + canonical(key+value) + secret``, return the uppercased MD5
    hex digest.

    not_checked: this canonicalisation is NOT verified against an official
    TOP signing reference.  The provided official ``api文档`` documents the
    ``sign_method`` values (hmac/md5/hmac-sha256) but not the canonical
    composition itself (in particular whether values are URL-encoded before
    signing).  Verify with an authenticated sandbox or a known-good Taobao
    SDK before any live use.
    """
    sorted_keys = sorted(params)
    canonical = "".join(f"{key}{params[key]}" for key in sorted_keys)
    # The official TOP signing concatenation is literally:
    # secret + canonical + secret
    payload = f"{secret}{canonical}{secret}"
    return hashlib.md5(payload.encode("utf-8")).hexdigest().upper()


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
    """TOP API adapter for Taobao affiliate product search (search-only).

    Secrets come from environment only (TAOBAO_APP_KEY, TAOBAO_APP_SECRET).
    Non-secret defaults (adzone_id, timeouts, result caps) come from
    config.yaml.  Handoff/cart creation always fail closed.
    """

    def __init__(
        self,
        *,
        app_key: str | None = None,
        app_secret: str | None = None,
        adzone_id: str | None = None,
        search_timeout_seconds: float | None = None,
        max_search_results: int | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._app_key = (app_key or "").strip()
        self._app_secret = (app_secret or "").strip()
        self._adzone_id = (adzone_id or _DEFAULT_ADZONE_ID).strip()
        self._search_timeout_seconds = (
            search_timeout_seconds
            if search_timeout_seconds is not None
            else _SEARCH_TIMEOUT_DEFAULT
        )
        self._max_search_results = max(
            1,
            min(
                max_search_results if max_search_results is not None else _DEFAULT_MAX_RESULTS,
                50,
            ),
        )
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
        return ProviderCapabilities(search=True, handoff_kinds=frozenset())

    async def search_offers(
        self,
        query: str,
        *,
        region: str = "CN",
        max_results: int = 10,
        timeout_seconds: float | None = None,
    ) -> Sequence[ShoppingOffer]:
        """Search Taobao products via keyword material API.

        The Taobao API only supports CN region.
        """
        self._require_available()
        query = query.strip()
        if not query:
            raise ShoppingProviderError("search query must not be empty")
        max_results = max(1, min(max_results, 50))
        effective_timeout = (
            timeout_seconds if timeout_seconds is not None else self._search_timeout_seconds
        )

        params = self._build_params(
            method="taobao.tbk.dg.material.optional.upgrade",
            extra={
                "adzone_id": self._adzone_id,
                "material_id": _DEFAULT_MATERIAL_ID,  # Official default
                "page_size": str(min(max_results, self._max_search_results)),
                "page_no": "1",
                "q": query,
            },
        )
        raw = await self._call_api(params, timeout_seconds=effective_timeout)
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
        """Fail closed — Taobao is search-only and creates no handoff.

        Raises ``ShoppingProviderError`` for every call, regardless of
        ``kind`` or ``offer``, so Taobao offers can never reach the
        purchase/approval/handoff flow.
        """
        raise ShoppingProviderError("taobao is search-only and does not support handoff creation")

    async def create_cart(
        self,
        lines: Sequence[Any],
        *,
        region: str = "CN",
        timeout_seconds: float = 5.0,
    ) -> Any:
        raise ShoppingProviderError("taobao is search-only and does not support cart creation")

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
        """Build signed TOP request parameters.

        The official doc fixes ``timestamp`` to ``yyyy-MM-dd HH:mm:ss`` in
        GMT+8.  The signing canonicalisation itself remains not_checked;
        see ``_top_sign``.
        """
        params: dict[str, str] = {
            "method": method,
            "app_key": self._app_key,
            "format": "json",
            "v": "2.0",
            "sign_method": "md5",
            "timestamp": datetime.now(_GMT_PLUS_8).strftime("%Y-%m-%d %H:%M:%S"),
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
                media_type = content_type.split(";", 1)[0].strip().lower()
                if (
                    media_type not in _ACCEPTED_JSON_CONTENT_TYPES
                    and response.is_success
                ):
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
        """Parse TOP material-search response into ShoppingOffer list.

        Reads the official ``taobao.tbk.dg.material.optional.upgrade``
        response schema: ``result_list.map_data[]`` items carry ``item_id``
        plus the ``item_basic_info`` / ``price_promotion_info`` /
        ``publish_info`` blocks.  Flat legacy keys are kept as fallbacks for
        robustness.
        """
        result_list = _deep_get(
            raw,
            "tbk_dg_material_optional_upgrade_response",
            "result_list",
            "map_data",
        )
        if result_list is None:
            raise ShoppingProviderError("taobao search response is missing result_list data")

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

        for item in items:
            if not isinstance(item, dict):
                continue
            if len(offers) >= max_results:
                break

            basic = item.get("item_basic_info")
            if not isinstance(basic, dict):
                basic = {}
            promo = item.get("price_promotion_info")
            if not isinstance(promo, dict):
                promo = {}
            publish = item.get("publish_info")
            if not isinstance(publish, dict):
                publish = {}

            title = str(basic.get("title") or item.get("title") or item.get("item_title") or query)
            item_id = str(item.get("item_id") or item.get("num_iid") or "")
            seller = str(
                basic.get("shop_title")
                or basic.get("seller_id")
                or item.get("nick")
                or item.get("seller_nick")
                or ""
            )

            # Price: prefer 预估到手价 (final_promotion_price), then 销售价格
            # (zk_final_price), then 划线价 (reserve_price).  Decimal-safe —
            # unit_price is passed through as a string.
            raw_price = (
                promo.get("final_promotion_price")
                or promo.get("zk_final_price")
                or promo.get("reserve_price")
                or item.get("zk_final_price")
                or item.get("reserve_price")
                or "0.00"
            )
            unit_price = _safe_decimal_str(raw_price)

            # Search metadata surfaced as product_url (required by
            # OfferSnapshot.product_url); it is never used for a redirect —
            # Taobao is search-only.  Prefer the 宝贝+券二合一 link.
            product_url = str(
                publish.get("coupon_share_url")
                or publish.get("click_url")
                or item.get("coupon_share_url")
                or item.get("item_url")
                or item.get("url")
                or ""
            ).strip()

            currency = "CNY"

            offers.append(
                ShoppingOffer(
                    provider="taobao",
                    provider_offer_id=item_id or f"tb:{_hash_str(title[:50])}",
                    merchandise_id=item_id or None,
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
                        "item_id": item_id,
                        "final_promotion_price": str(promo.get("final_promotion_price") or ""),
                        "zk_final_price": str(promo.get("zk_final_price") or ""),
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

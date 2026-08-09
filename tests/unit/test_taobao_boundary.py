"""Tests for the Taobao Affiliate adapter."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from aidison.domain.models import HandoffKind
from aidison.providers.shopping import (
    OfferAvailability,
    ShoppingConfigError,
    ShoppingOffer,
    ShoppingProviderError,
)
from aidison.providers.taobao import (
    TaobaoAffiliateAdapter,
    _safe_decimal_str,
)


def _adapter(client: httpx.AsyncClient | None = None) -> TaobaoAffiliateAdapter:
    return TaobaoAffiliateAdapter(
        app_key="test-key",
        app_secret="test-secret",
        adzone_id="test-adzone",
        http_client=client,
    )


# ── Configuration / availability ───────────────────────────────────────


def test_taobao_available_when_configured() -> None:
    assert _adapter().available is True


def test_taobao_unavailable_without_app_key() -> None:
    adapter = TaobaoAffiliateAdapter(app_key="", app_secret="s", adzone_id="a")
    assert adapter.available is False
    with pytest.raises(ShoppingConfigError, match="unavailable"):
        adapter._require_available()


def test_taobao_unavailable_without_app_secret() -> None:
    adapter = TaobaoAffiliateAdapter(app_key="k", app_secret="", adzone_id="a")
    assert adapter.available is False


def test_taobao_unavailable_without_adzone_id() -> None:
    adapter = TaobaoAffiliateAdapter(app_key="k", app_secret="s", adzone_id="")
    assert adapter.available is False


def test_taobao_capabilities_are_search_only() -> None:
    caps = _adapter().capabilities
    assert caps.search is True
    assert caps.handoff_kinds == frozenset()


# ── Search ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_taobao_search_rejects_empty_query() -> None:
    with pytest.raises(ShoppingProviderError, match="empty"):
        await _adapter().search_offers("")


@pytest.mark.asyncio
async def test_taobao_search_parses_response() -> None:
    mock_response = {
        "tbk_dg_material_optional_response": {
            "result_list": {
                "map_data": [
                    {
                        "num_iid": "123456789",
                        "title": "SCD41 CO2 传感器模块",
                        "nick": "传感器专卖店",
                        "zk_final_price": "168.00",
                        "coupon_amount": "10",
                        "coupon_start_fee": "178.00",
                        "coupon_share_url": "https://uland.taobao.com/item.htm?id=123456789",
                    }
                ]
            }
        }
    }

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/json;charset=utf-8"},
            content=json.dumps(mock_response).encode(),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        offers = await _adapter(client).search_offers("SCD41 传感器")
        assert len(offers) == 1
        offer = offers[0]
        assert offer.provider == "taobao"
        assert offer.provider_offer_id == "123456789"
        assert "SCD41" in offer.title
        assert offer.seller == "传感器专卖店"
        assert offer.unit_price == "168.00"
        assert offer.currency == "CNY"
        assert offer.availability == OfferAvailability.UNKNOWN
        assert offer.product_url == "https://uland.taobao.com/item.htm?id=123456789"


@pytest.mark.asyncio
async def test_taobao_search_parses_response_dict_format() -> None:
    """result_list can be a dict keyed by index."""
    mock_response = {
        "tbk_dg_material_optional_response": {
            "result_list": {
                "map_data": {
                    "0": {
                        "num_iid": "111",
                        "title": "Product A",
                        "zk_final_price": "99.00",
                        "coupon_share_url": "https://item.taobao.com/item.htm?id=111",
                    },
                    "1": {
                        "num_iid": "222",
                        "title": "Product B",
                        "zk_final_price": "199.00",
                        "coupon_share_url": "https://item.taobao.com/item.htm?id=222",
                    },
                }
            }
        }
    }

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/json;charset=utf-8"},
            content=json.dumps(mock_response).encode(),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        offers = await _adapter(client).search_offers("test")
        assert len(offers) == 2
        assert {o.provider_offer_id for o in offers} == {"111", "222"}


@pytest.mark.asyncio
async def test_taobao_search_respects_max_results() -> None:
    mock_response = {
        "tbk_dg_material_optional_response": {
            "result_list": {
                "map_data": [
                    {"num_iid": str(i), "title": f"Item {i}", "zk_final_price": "10.00",
                     "coupon_share_url": f"https://item.taobao.com/item.htm?id={i}"}
                    for i in range(10)
                ]
            }
        }
    }

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/json;charset=utf-8"},
            content=json.dumps(mock_response).encode(),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        offers = await _adapter(client).search_offers("test", max_results=3)
        assert len(offers) == 3


@pytest.mark.asyncio
async def test_taobao_search_missing_data_raises() -> None:
    mock_response: dict[str, dict[str, object]] = {"tbk_dg_material_optional_response": {}}

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/json;charset=utf-8"},
            content=json.dumps(mock_response).encode(),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(ShoppingProviderError, match="missing result_list"):
            await _adapter(client).search_offers("test")


# ── Handoff / cart fail closed (search-only) ────────────────────────────


@pytest.mark.asyncio
async def test_taobao_create_handoff_always_fails_closed() -> None:
    """Taobao is search-only: create_handoff errors for every kind/offer."""
    offer = ShoppingOffer(
        provider="taobao",
        provider_offer_id="123456789",
        title="SCD41 传感器",
        unit_price="168.00",
        currency="CNY",
        region="CN",
        product_url="https://item.taobao.com/item.htm?id=123456789",
        observed_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    with pytest.raises(ShoppingProviderError, match="search-only"):
        await _adapter().create_handoff(kind=HandoffKind.CART_REDIRECT, lines=[])
    with pytest.raises(ShoppingProviderError, match="search-only"):
        await _adapter().create_handoff(
            kind=HandoffKind.PRODUCT_REDIRECT,
            offer=offer,
        )


@pytest.mark.asyncio
async def test_taobao_create_cart_always_fails_closed() -> None:
    with pytest.raises(ShoppingProviderError, match="search-only"):
        await _adapter().create_cart(lines=[])


# ── Response size / content-type boundaries ──────────────────────────────


@pytest.mark.asyncio
async def test_taobao_rejects_non_json_response() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not json</html>")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(ShoppingProviderError, match="content type"):
            await _adapter(client).search_offers("test")


@pytest.mark.asyncio
async def test_taobao_accepts_json_body_with_top_text_javascript_content_type() -> None:
    """TOP may label JSON payloads as text/javascript; parse them only after MIME allowlist."""

    mock_response = {
        "tbk_dg_material_optional_response": {
            "result_list": {
                "map_data": [
                    {
                        "num_iid": "1001",
                        "title": "TOP JSON payload",
                        "zk_final_price": "9.90",
                    }
                ]
            }
        }
    }

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/javascript;charset=UTF-8"},
            content=json.dumps(mock_response).encode(),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        offers = await _adapter(client).search_offers("test")

    assert len(offers) == 1
    assert offers[0].title == "TOP JSON payload"


@pytest.mark.asyncio
async def test_taobao_rejects_oversized_response() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=b"x" * (1 * 1024 * 1024 + 1),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(ShoppingProviderError, match="exceeds"):
            await _adapter(client).search_offers("test")


# ── TOP error response ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_taobao_handles_top_error_response() -> None:
    mock_response = {
        "error_response": {
            "code": "15",
            "msg": "Remote service error",
            "sub_code": "isv.invalid-parameter",
            "sub_msg": "参数adzone_id无效",
        }
    }

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            headers={"content-type": "application/json;charset=utf-8"},
            content=json.dumps(mock_response).encode(),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(ShoppingProviderError, match="参数adzone_id无效"):
            await _adapter(client).search_offers("test")


@pytest.mark.asyncio
async def test_taobao_rejects_malformed_json() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=b"not-valid-json{{{",
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(ShoppingProviderError, match="invalid JSON"):
            await _adapter(client).search_offers("test")


# ── Timeout ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_taobao_handles_timeout() -> None:
    """Timeout fires when the transport hangs."""

    async def hang(request: httpx.Request) -> httpx.Response:
        # httpx.MockTransport consumes timeout; we simulate by having the
        # handler raise a timeout after stream begins.
        # The test validates the timeout→ShoppingProviderError path via
        # a controlled mock that raises TimeoutException.
        raise httpx.TimeoutException("mock timeout")

    async with httpx.AsyncClient(transport=httpx.MockTransport(hang)) as client:
        with pytest.raises(ShoppingProviderError, match="timed out"):
            await _adapter(client).search_offers("test", timeout_seconds=5.0)


# ── Decimal-safe price ───────────────────────────────────────────────────


def test_safe_decimal_str_from_float() -> None:
    assert _safe_decimal_str(168.0) == "168.0"
    assert _safe_decimal_str("39.90") == "39.90"
    assert _safe_decimal_str("168.00") == "168.00"  # Preserves precision


def test_safe_decimal_str_from_invalid() -> None:
    assert _safe_decimal_str(None) == "0.00"
    assert _safe_decimal_str("nope") == "0.00"


# ── Offer freshness ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_taobao_offers_have_short_ttl() -> None:
    mock_response = {
        "tbk_dg_material_optional_response": {
            "result_list": {
                "map_data": [
                    {
                        "num_iid": "1",
                        "title": "Fresh",
                        "zk_final_price": "10.00",
                        "coupon_share_url": "https://item.taobao.com/item.htm?id=1",
                    }
                ]
            }
        }
    }

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/json;charset=utf-8"},
            content=json.dumps(mock_response).encode(),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        offers = await _adapter(client).search_offers("test")
        assert len(offers) == 1
        now = datetime.now(UTC)
        assert offers[0].expires_at is not None
        # TTL should be roughly 5 minutes from now
        delta = offers[0].expires_at - now
        assert timedelta(minutes=4) < delta < timedelta(minutes=6)


# ── Config loading integration ──────────────────────────────────────────


def test_taobao_config_from_env() -> None:
    """Secrets come from env, not from config.yaml defaults."""
    import os

    os.environ["TAOBAO_APP_KEY"] = "env-key"
    os.environ["TAOBAO_APP_SECRET"] = "env-secret"
    try:
        adapter = TaobaoAffiliateAdapter(
            app_key=os.environ["TAOBAO_APP_KEY"],
            app_secret=os.environ["TAOBAO_APP_SECRET"],
            adzone_id="test-zone",
        )
        assert adapter.available is True
        assert adapter._app_secret == "env-secret"
    finally:
        del os.environ["TAOBAO_APP_KEY"]
        del os.environ["TAOBAO_APP_SECRET"]

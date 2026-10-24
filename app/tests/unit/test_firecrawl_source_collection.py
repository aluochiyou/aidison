from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest

from aidison.research.source_collection import (
    FallbackResearchSourceCollector,
    FirecrawlSearchResearchSourceCollector,
    FirecrawlSourceCollectionError,
    FirecrawlWebReader,
    TavilySearchResearchSourceCollector,
)


@pytest.mark.asyncio
async def test_firecrawl_search_collects_bounded_rendered_markdown() -> None:
    captured: dict[str, object] = {}

    async def responder(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers["authorization"]
        captured["body"] = request.content.decode()
        return httpx.Response(
            200,
            headers={"X-Request-ID": "fc-search-1"},
            json={
                "success": True,
                "data": {
                    "web": [
                        {
                            "url": "https://example.test/esc#specification",
                            "markdown": "# ESC specification\n\nContinuous current: 60 A",
                        },
                        {"url": "https://127.0.0.1/private", "markdown": "must be ignored"},
                    ]
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as client:
        sources = await FirecrawlSearchResearchSourceCollector(
            api_key="fc-test-key", client=client, max_queries=1, max_results=2
        ).collect(
            run=object(),  # type: ignore[arg-type]
            task=SimpleNamespace(collection_policy=None),
            question="ESC continuous current specification " + "integration limits " * 80,
        )

    assert captured["url"] == "https://api.firecrawl.dev/v2/search"
    assert captured["authorization"] == "Bearer fc-test-key"
    assert '"sources":["web"]' in str(captured["body"])
    assert '"onlyMainContent":true' in str(captured["body"])
    assert len(json.loads(str(captured["body"]))["query"]) <= 500
    assert len(sources) == 1
    assert sources[0].source.provider == "firecrawl-search-v2"
    assert sources[0].source.canonical_locator == "https://example.test/esc"
    assert sources[0].normalized_document.endswith("Continuous current: 60 A")


@pytest.mark.asyncio
async def test_firecrawl_quota_exhaustion_falls_back_to_tavily_without_double_search() -> None:
    calls: list[str] = []

    async def responder(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if str(request.url) == "https://api.firecrawl.dev/v2/search":
            return httpx.Response(402, json={"success": False, "error": "quota exhausted"})
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "url": "https://example.test/airframe",
                        "raw_content": "# Airframe specification\n\nPayload: 2 kg",
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as client:
        collector = FallbackResearchSourceCollector(
            primary=FirecrawlSearchResearchSourceCollector(
                api_key="fc-test-key", client=client, max_queries=1
            ),
            fallback=TavilySearchResearchSourceCollector(
                api_key="tvly-test-key", client=client, max_queries=1
            ),
        )
        sources = await collector.collect(
            run=object(),  # type: ignore[arg-type]
            task=SimpleNamespace(collection_policy=None),
            question="airframe payload specification",
        )

    assert calls == [
        "https://api.firecrawl.dev/v2/search",
        "https://api.tavily.com/search",
    ]
    assert len(sources) == 1
    assert sources[0].source.provider == "tavily-search-v1"


@pytest.mark.asyncio
async def test_firecrawl_reader_uses_v2_markdown_with_zero_retention() -> None:
    captured: dict[str, object] = {}

    async def responder(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers["authorization"]
        captured["body"] = request.content.decode()
        return httpx.Response(
            200,
            headers={"X-Request-ID": "fc-request-1"},
            json={
                "success": True,
                "data": {
                    "markdown": "# ESC specification\n\nCurrent: 60 A",
                    "metadata": {"sourceURL": "https://example.test/esc"},
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as client:
        source = await FirecrawlWebReader(
            api_key="fc-test-key",
            client=client,
        ).read(
            url="https://example.test/esc#overview",
            observed_at=datetime.now(UTC),
            tool_runtime=None,
        )

    assert captured["url"] == "https://api.firecrawl.dev/v2/scrape"
    assert captured["authorization"] == "Bearer fc-test-key"
    assert '"zeroDataRetention":true' in str(captured["body"])
    assert '"onlyMainContent":true' in str(captured["body"])
    assert source.source.canonical_locator == "https://example.test/esc"
    assert source.source.provider == "firecrawl-scrape-v2"
    assert source.normalized_document.endswith("Current: 60 A")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    (
        "http://example.test/page",
        "https://localhost/private",
        "https://127.0.0.1/private",
        "https://10.0.0.1/private",
        "https://service.internal/private",
    ),
)
async def test_firecrawl_reader_rejects_non_public_targets(url: str) -> None:
    async with httpx.AsyncClient() as client:
        reader = FirecrawlWebReader(api_key="fc-test-key", client=client)
        with pytest.raises(FirecrawlSourceCollectionError) as raised:
            await reader.read(
                url=url,
                observed_at=datetime.now(UTC),
                tool_runtime=None,
            )
    assert raised.value.reason_code == "firecrawl_url_not_public_https"


@pytest.mark.asyncio
async def test_firecrawl_reader_classifies_credit_exhaustion() -> None:
    async def responder(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(402, json={"success": False, "error": "payment required"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as client:
        reader = FirecrawlWebReader(api_key="fc-test-key", client=client)
        with pytest.raises(FirecrawlSourceCollectionError) as raised:
            await reader.read(
                url="https://example.test/page",
                observed_at=datetime.now(UTC),
                tool_runtime=None,
            )
    assert raised.value.reason_code == "firecrawl_quota_exhausted"


@pytest.mark.asyncio
async def test_tavily_uses_firecrawl_only_when_discovered_result_has_no_body() -> None:
    calls: list[str] = []

    async def responder(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if str(request.url) == "https://api.tavily.com/search":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "url": "https://example.test/dynamic",
                            "raw_content": None,
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {"markdown": "# Dynamic page\n\nRendered content."},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as client:
        collector = TavilySearchResearchSourceCollector(
            api_key="tvly-test-key",
            client=client,
            fallback_reader=FirecrawlWebReader(api_key="fc-test-key", client=client),
        )
        sources = await collector.collect(
            run=object(),  # type: ignore[arg-type]
            task=SimpleNamespace(collection_policy=None),
            question="dynamic page evidence",
        )

    assert calls == [
        "https://api.tavily.com/search",
        "https://api.firecrawl.dev/v2/scrape",
    ]
    assert len(sources) == 1
    assert sources[0].source.provider == "firecrawl-scrape-v2"

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from aidison.research.source_collection import (
    TavilySearchResearchSourceCollector,
    TavilySourceCollectionError,
)
from aidison.research.strategy import compile_research_execution_policy


@pytest.mark.asyncio
async def test_tavily_collector_uses_raw_content_and_never_promotes_a_search_snippet() -> None:
    captured: dict[str, Any] = {}

    async def responder(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "url": "https://docs.example.test/motor-a",
                        "title": "Motor A",
                        "content": "This search snippet must not become source evidence.",
                        "raw_content": "# Motor A\n\nPeak current: 35A at 12V.",
                    },
                    {
                        "url": "https://docs.example.test/snippet-only",
                        "content": "No raw content means no source document.",
                        "raw_content": None,
                    },
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as client:
        sources = await TavilySearchResearchSourceCollector(
            api_key="tvly-test-key",
            client=client,
        ).collect(
            run=object(),  # type: ignore[arg-type]
            task=object(),  # type: ignore[arg-type]
            question="Which motor has a 35A peak current?",
        )

    assert captured["headers"]["authorization"] == "Bearer tvly-test-key"
    assert captured["payload"] == {
        "query": "Which motor has a 35A peak current?",
        "search_depth": "basic",
        "max_results": 3,
        "include_answer": False,
        "include_raw_content": "markdown",
        "auto_parameters": False,
    }
    assert len(sources) == 1
    [source] = sources
    assert source.source.canonical_locator == "https://docs.example.test/motor-a"
    assert source.normalized_document == "# Motor A\n\nPeak current: 35A at 12V."
    assert source.coverage_source_kinds == ("evidence",)
    assert "search snippet" not in source.normalized_document


@pytest.mark.asyncio
async def test_tavily_collector_classifies_quota_failure_without_a_fallback_document() -> None:
    async def responder(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer tvly-test-key"
        return httpx.Response(429, json={"detail": "quota exhausted"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as client:
        collector = TavilySearchResearchSourceCollector(api_key="tvly-test-key", client=client)
        with pytest.raises(TavilySourceCollectionError) as raised:
            await collector.collect(
                run=object(),  # type: ignore[arg-type]
                task=object(),  # type: ignore[arg-type]
                question="Which motor has a 35A peak current?",
            )

    assert raised.value.reason_code == "tavily_quota_exhausted"


@pytest.mark.asyncio
async def test_tavily_collector_keeps_trusted_sources_when_one_coverage_query_fails() -> None:
    """A transient failure for one gap must not erase another query's evidence."""

    queries: list[str] = []

    async def responder(request: httpx.Request) -> httpx.Response:
        query = json.loads(request.content)["query"]
        queries.append(query)
        if "current constraint" in query:
            return httpx.Response(503, json={"detail": "temporarily unavailable"})
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "url": "https://spec.example.test/voltage",
                        "raw_content": "# Voltage specification\nNominal voltage: 12V.",
                    }
                ]
            },
        )

    question = "\n".join(
        (
            "Project objective: build a small drone",
            "propulsion.current: Find a current constraint.",
            "propulsion.voltage: Find a voltage constraint.",
        )
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as client:
        sources = await TavilySearchResearchSourceCollector(
            api_key="tvly-test-key",
            client=client,
            max_results=2,
            max_queries=2,
        ).collect(
            run=object(),  # type: ignore[arg-type]
            task=object(),  # type: ignore[arg-type]
            question=question,
        )

    assert len(queries) == 2
    assert [source.source.canonical_locator for source in sources] == [
        "https://spec.example.test/voltage"
    ]
    assert [
        failure.model_dump(mode="json") for failure in sources[0].collection_failures
    ] == [
        {
            "coverage_key": "propulsion.current",
            "reason_code": "tavily_provider_unavailable",
        }
    ]


@pytest.mark.asyncio
async def test_tavily_collector_splits_coverage_questions_and_caps_deduplicated_documents() -> None:
    queries: list[str] = []

    async def responder(request: httpx.Request) -> httpx.Response:
        query = json.loads(request.content)["query"]
        queries.append(query)
        if "peak current" in query:
            results = [
                {
                    "url": "https://docs.example.test/motor-a",
                    "raw_content": "# Motor A\nPeak current: 35A.",
                },
                {
                    "url": "https://docs.example.test/shared",
                    "raw_content": "# Shared electrical guidance",
                },
            ]
        else:
            results = [
                {
                    "url": "https://docs.example.test/shared",
                    "raw_content": "# Shared electrical guidance",
                },
                {
                    "url": "https://docs.example.test/motor-b",
                    "raw_content": "# Motor B\nVoltage: 12V.",
                },
            ]
        return httpx.Response(200, json={"results": results})

    question = "\n".join(
        (
            "Project objective: build a small drone",
            "Module: propulsion (propulsion)",
            "propulsion.current: Which motor has a safe peak current?",
            "propulsion.voltage: Which motor fits a 12V power system?",
        )
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as client:
        sources = await TavilySearchResearchSourceCollector(
            api_key="tvly-test-key",
            client=client,
            max_results=3,
            max_queries=3,
        ).collect(
            run=object(),  # type: ignore[arg-type]
            task=object(),  # type: ignore[arg-type]
            question=question,
        )

    assert len(queries) == 2
    assert all("Project objective: build a small drone" in query for query in queries)
    assert [source.source.canonical_locator for source in sources] == [
        "https://docs.example.test/motor-a",
        "https://docs.example.test/shared",
        "https://docs.example.test/motor-b",
    ]


@pytest.mark.asyncio
async def test_tavily_collector_gives_each_coverage_query_a_source_before_supplementing() -> None:
    queries: list[str] = []

    async def responder(request: httpx.Request) -> httpx.Response:
        query = json.loads(request.content)["query"]
        queries.append(query)
        if "current constraint" in query:
            results = [
                {
                    "url": "https://docs.example.test/current-primary",
                    "raw_content": "# Current primary",
                },
                {
                    "url": "https://docs.example.test/current-supplement",
                    "raw_content": "# Current supplement",
                },
            ]
        else:
            results = [
                {
                    "url": "https://docs.example.test/voltage-primary",
                    "raw_content": "# Voltage primary",
                },
                {
                    "url": "https://docs.example.test/voltage-supplement",
                    "raw_content": "# Voltage supplement",
                },
            ]
        return httpx.Response(200, json={"results": results})

    question = "\n".join(
        (
            "Project objective: build a small drone",
            "propulsion.current: Find a current constraint.",
            "propulsion.voltage: Find a voltage constraint.",
        )
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as client:
        sources = await TavilySearchResearchSourceCollector(
            api_key="tvly-test-key",
            client=client,
            max_results=2,
            max_queries=2,
        ).collect(
            run=object(),  # type: ignore[arg-type]
            task=object(),  # type: ignore[arg-type]
            question=question,
        )

    assert len(queries) == 2
    assert [source.source.canonical_locator for source in sources] == [
        "https://docs.example.test/current-primary",
        "https://docs.example.test/voltage-primary",
    ]


@pytest.mark.asyncio
async def test_tavily_collector_interleaves_lenses_with_coverage_queries() -> None:
    """Deep research spends its bounded query budget across concerns and angles."""

    queries: list[str] = []

    async def responder(request: httpx.Request) -> httpx.Response:
        query = json.loads(request.content)["query"]
        queries.append(query)
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "url": f"https://docs.example.test/{len(queries)}",
                        "raw_content": f"# Source {len(queries)}",
                    }
                ]
            },
        )

    question = "\n".join(
        (
            "Project objective: build a small drone",
            "Research lenses: official specifications | failure modes and trade-offs",
            "propulsion.current: Find a current constraint.",
            "propulsion.voltage: Find a voltage constraint.",
        )
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as client:
        sources = await TavilySearchResearchSourceCollector(
            api_key="tvly-test-key",
            client=client,
            max_results=4,
            max_queries=4,
        ).collect(
            run=object(),  # type: ignore[arg-type]
            task=object(),  # type: ignore[arg-type]
            question=question,
        )

    assert len(sources) == 4
    assert len(queries) == 4
    assert "Research angle: official specifications" in queries[0]
    assert "Research angle: official specifications" in queries[1]
    assert "Research angle: failure modes and trade-offs" in queries[2]
    assert "Research angle: failure modes and trade-offs" in queries[3]
    assert "current constraint" in queries[0]
    assert "voltage constraint" in queries[1]
    assert "current constraint" in queries[2]
    assert "voltage constraint" in queries[3]


@pytest.mark.asyncio
async def test_tavily_collector_prefers_a_new_origin_within_the_fixed_source_budget() -> None:
    """A second page on one site must not crowd out an available second site."""

    queries: list[str] = []

    async def responder(request: httpx.Request) -> httpx.Response:
        query = json.loads(request.content)["query"]
        queries.append(query)
        if "current constraint" in query:
            results = [
                {
                    "url": "https://docs.example.test/current-primary",
                    "raw_content": "# Current primary",
                },
                {
                    "url": "https://docs.example.test/current-supplement",
                    "raw_content": "# Current supplement",
                },
            ]
        else:
            results = [
                {
                    "url": "https://docs.example.test/voltage-primary",
                    "raw_content": "# Voltage primary on the same site",
                },
                {
                    "url": "https://lab.example.test/voltage-test",
                    "raw_content": "# Independent retrieval-site test",
                },
            ]
        return httpx.Response(200, json={"results": results})

    question = "\n".join(
        (
            "Project objective: build a small drone",
            "propulsion.current: Find a current constraint.",
            "propulsion.voltage: Find a voltage constraint.",
        )
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as client:
        sources = await TavilySearchResearchSourceCollector(
            api_key="tvly-test-key",
            client=client,
            max_results=2,
            max_queries=2,
        ).collect(
            run=object(),  # type: ignore[arg-type]
            task=object(),  # type: ignore[arg-type]
            question=question,
        )

    assert [source.source.canonical_locator for source in sources] == [
        "https://docs.example.test/current-primary",
        "https://lab.example.test/voltage-test",
    ]
    assert len(queries) == 2


@pytest.mark.asyncio
async def test_tavily_collector_omits_provider_breadth_cap_for_deep_policy() -> None:
    payloads: list[dict[str, Any]] = []

    async def responder(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "url": "https://docs.example.test/deep-source",
                        "raw_content": "# Deep source",
                    }
                ]
            },
        )

    policy = compile_research_execution_policy(research_depth="deep").collection
    async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as client:
        await TavilySearchResearchSourceCollector(
            api_key="tvly-test-key",
            client=client,
            max_results=None,
            max_queries=None,
        ).collect(
            run=object(),  # type: ignore[arg-type]
            task=SimpleNamespace(collection_policy=policy),  # type: ignore[arg-type]
            question="Find a reliable module constraint.",
        )

    assert payloads == [
        {
            "query": "Find a reliable module constraint.",
            "search_depth": "advanced",
            "include_answer": False,
            "include_raw_content": "markdown",
            "auto_parameters": False,
        }
    ]


@pytest.mark.asyncio
async def test_deep_collection_expands_across_all_coverage_keys_without_product_caps() -> None:
    """Deep research must not stop at legacy query/source limits."""
    policy = compile_research_execution_policy(research_depth="deep").collection
    calls: list[dict[str, Any]] = []

    async def responder(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        calls.append(payload)
        suffix = len(calls)
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "url": f"https://docs.example.test/{suffix}/primary",
                        "raw_content": f"# Source {suffix} primary",
                    },
                    {
                        "url": f"https://independent.example.test/{suffix}/secondary",
                        "raw_content": f"# Source {suffix} secondary",
                    },
                ]
            },
        )

    question = "\n".join(
        (
            "Project objective: verify a flight-control module",
            "control.interface: Which interface applies?",
            "control.safety: Which safety limit applies?",
            "control.power: Which power requirement applies?",
            "control.compatibility: Which compatibility requirement applies?",
            "control.integration: Which integration constraint applies?",
        )
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as client:
        sources = await TavilySearchResearchSourceCollector(
            api_key="tvly-test-key",
            client=client,
            max_results=None,
            max_queries=None,
        ).collect(
            run=object(),  # type: ignore[arg-type]
            task=SimpleNamespace(collection_policy=policy),  # type: ignore[arg-type]
            question=question,
        )

    assert len(calls) == 5
    assert all(call["search_depth"] == "advanced" for call in calls)
    assert all("max_results" not in call for call in calls)
    assert len(sources) == 10


@pytest.mark.asyncio
async def test_tavily_collector_turns_approved_source_strategy_into_query_guidance() -> None:
    payloads: list[dict[str, Any]] = []

    async def responder(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "url": "https://docs.example.test/official-source",
                        "raw_content": "# Official source",
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as client:
        await TavilySearchResearchSourceCollector(
            api_key="tvly-test-key", client=client
        ).collect(
            run=object(),  # type: ignore[arg-type]
            task=object(),  # type: ignore[arg-type]
            question=(
                "Project objective: verify a controller\n"
                "Source strategy: official\n"
                "control.interface: Which official interface specification applies?"
            ),
        )

    assert payloads[0]["query"] == (
        "Project objective: verify a controller\n"
        "Source strategy: official\n"
        "Prefer official manufacturer, standards-body, or public-authority documentation.\n"
        "Which official interface specification applies?"
    )

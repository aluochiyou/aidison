from __future__ import annotations

import base64
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest

from aidison.research.source_collection import (
    CollectedResearchSource,
    CompositeResearchSourceCollector,
    GitHubRepositorySourceCollector,
    GitHubSourceCollectionError,
    ResearchSourceCollectionError,
)
from aidison.research.source_observations import SourceIdentity, SourceKind


@pytest.mark.asyncio
async def test_github_collector_reads_only_allowlisted_file_and_freezes_blob_identity() -> None:
    captured: dict[str, object] = {}
    document = "# Flight controller interface\n\nUART is required."

    async def responder(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers["authorization"]
        captured["accept"] = request.headers["accept"]
        return httpx.Response(
            200,
            json={
                "type": "file",
                "sha": "a" * 40,
                "size": len(document.encode()),
                "encoding": "base64",
                "content": "\n".join(
                    (
                        base64.b64encode(document.encode()).decode()[:24],
                        base64.b64encode(document.encode()).decode()[24:],
                    )
                ),
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as client:
        sources = await GitHubRepositorySourceCollector(
            api_token="github-test-token",
            source_targets=("aidison-lab/flight-docs@main:specs/interface.md",),
            client=client,
        ).collect(
            run=object(),  # type: ignore[arg-type]
            task=object(),  # type: ignore[arg-type]
            question="This task question must not widen the GitHub read scope.",
        )

    assert captured == {
        "url": "https://api.github.com/repos/aidison-lab/flight-docs/contents/specs/interface.md?ref=main",
        "authorization": "Bearer github-test-token",
        "accept": "application/vnd.github+json",
    }
    assert len(sources) == 1
    [source] = sources
    assert source.source.canonical_locator == (
        "https://github.com/aidison-lab/flight-docs/blob/" + "a" * 40 + "/specs/interface.md"
    )
    assert source.normalized_document == document
    assert source.media_type == "text/markdown"
    assert source.coverage_source_kinds == ("evidence",)


@pytest.mark.asyncio
async def test_github_collector_blocks_authentication_failure_instead_of_falling_back() -> None:
    async def responder(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "Bad credentials"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as client:
        collector = GitHubRepositorySourceCollector(
            api_token="github-test-token",
            source_targets=("aidison-lab/flight-docs@main:specs/interface.md",),
            client=client,
        )
        with pytest.raises(GitHubSourceCollectionError) as raised:
            await collector.collect(
                run=object(),  # type: ignore[arg-type]
                task=object(),  # type: ignore[arg-type]
                question="ignored",
            )

    assert raised.value.reason_code == "github_authentication_failed"


@pytest.mark.asyncio
async def test_github_collector_distinguishes_rate_limit_from_forbidden_credentials() -> None:
    async def responder(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            headers={"X-RateLimit-Remaining": "0"},
            json={"message": "API rate limit exceeded"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as client:
        collector = GitHubRepositorySourceCollector(
            api_token="github-test-token",
            source_targets=("aidison-lab/flight-docs@main:specs/interface.md",),
            client=client,
        )
        with pytest.raises(GitHubSourceCollectionError) as raised:
            await collector.collect(
                run=object(),  # type: ignore[arg-type]
                task=object(),  # type: ignore[arg-type]
                question="ignored",
            )

    assert raised.value.reason_code == "github_quota_exhausted"


@pytest.mark.asyncio
async def test_github_collector_rejects_non_utf8_or_malformed_content() -> None:
    async def responder(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "type": "file",
                "sha": "b" * 40,
                "size": 2,
                "encoding": "base64",
                "content": base64.b64encode(b"\xff\xfe").decode(),
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as client:
        collector = GitHubRepositorySourceCollector(
            api_token="github-test-token",
            source_targets=("aidison-lab/flight-docs@main:specs/interface.md",),
            client=client,
        )
        with pytest.raises(GitHubSourceCollectionError) as raised:
            await collector.collect(
                run=object(),  # type: ignore[arg-type]
                task=object(),  # type: ignore[arg-type]
                question="ignored",
            )

    assert raised.value.reason_code == "github_response_schema_invalid"


@pytest.mark.asyncio
async def test_github_collector_rejects_oversized_response_before_base64_decode() -> None:
    async def responder(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "type": "file",
                "sha": "c" * 40,
                "size": 200_000,
                "encoding": "base64",
                "content": "not-decoded",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as client:
        collector = GitHubRepositorySourceCollector(
            api_token="github-test-token",
            source_targets=("aidison-lab/flight-docs@main:specs/interface.md",),
            client=client,
            max_document_characters=32_000,
        )
        with pytest.raises(GitHubSourceCollectionError) as raised:
            await collector.collect(
                run=object(),  # type: ignore[arg-type]
                task=object(),  # type: ignore[arg-type]
                question="ignored",
            )

    assert raised.value.reason_code == "github_document_too_large"


@pytest.mark.parametrize(
    "target",
    (
        "aidison-lab/flight-docs:specs/interface.md",
        "aidison-lab/flight-docs@main:../private.txt",
        "aidison-lab/flight-docs@main:/private.txt",
    ),
)
def test_github_collector_rejects_ambiguous_or_escaping_source_targets(target: str) -> None:
    with pytest.raises(ValueError):
        GitHubRepositorySourceCollector(
            api_token="github-test-token",
            source_targets=(target,),
            client=object(),  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_composite_collector_enforces_the_frozen_task_document_budget_fairly() -> None:
    class StaticCollector:
        def __init__(self, sources: tuple[CollectedResearchSource, ...]) -> None:
            self._sources = sources

        async def collect(
            self, *, run: object, task: object, question: str
        ) -> tuple[CollectedResearchSource, ...]:
            del run, task, question
            return self._sources

    def source(locator: str) -> CollectedResearchSource:
        return CollectedResearchSource(
            key=f"fixture-{locator.rsplit('/', maxsplit=1)[-1]}",
            source=SourceIdentity(
                kind=SourceKind.REPOSITORY,
                provider="fixture",
                canonical_locator=locator,
            ),
            normalized_document="# fixture",
            media_type="text/markdown",
            representation="normalized-fixture-v1",
            parser_revision="fixture-v1",
            observed_at=datetime.now(UTC),
            coverage_source_kinds=("evidence",),
        )

    sources = await CompositeResearchSourceCollector(
        (
            StaticCollector((source("https://example.test/one"),)),  # type: ignore[arg-type]
            StaticCollector(
                (
                    source("https://example.test/two"),
                    source("https://example.test/three"),
                )
            ),  # type: ignore[arg-type]
            StaticCollector((source("https://example.test/four"),)),  # type: ignore[arg-type]
        )
    ).collect(
        run=object(),  # type: ignore[arg-type]
        task=SimpleNamespace(collection_policy=SimpleNamespace(max_documents_total=3)),
        question="ignored",
    )

    assert [item.source.canonical_locator for item in sources] == [
        "https://example.test/one",
        "https://example.test/two",
        "https://example.test/four",
    ]


@pytest.mark.asyncio
async def test_composite_collector_preserves_available_sources_and_records_outage() -> None:
    class StaticCollector:
        async def collect(
            self, *, run: object, task: object, question: str
        ) -> tuple[CollectedResearchSource, ...]:
            del run, task, question
            return (
                CollectedResearchSource(
                    key="fixture-source",
                    source=SourceIdentity(
                        kind=SourceKind.WEB,
                        provider="fixture",
                        canonical_locator="https://example.test/specification",
                    ),
                    normalized_document="# Specification",
                    media_type="text/markdown",
                    representation="normalized-fixture-v1",
                    parser_revision="fixture-v1",
                    observed_at=datetime.now(UTC),
                    coverage_source_kinds=("evidence",),
                ),
            )

    class TemporarilyUnavailableCollector:
        async def collect(
            self, *, run: object, task: object, question: str
        ) -> tuple[CollectedResearchSource, ...]:
            del run, task, question
            raise ResearchSourceCollectionError("github_provider_unavailable")

    sources = await CompositeResearchSourceCollector(
        (
            StaticCollector(),  # type: ignore[arg-type]
            TemporarilyUnavailableCollector(),  # type: ignore[arg-type]
        )
    ).collect(
        run=object(),  # type: ignore[arg-type]
        task=SimpleNamespace(
            coverage_keys=("control.interface", "control.safety"),
            collection_policy=SimpleNamespace(max_documents_total=3),
        ),
        question="ignored",
    )

    assert len(sources) == 1
    assert [item.model_dump(mode="json") for item in sources[0].collection_failures] == [
        {"coverage_key": "control.interface", "reason_code": "github_provider_unavailable"},
        {"coverage_key": "control.safety", "reason_code": "github_provider_unavailable"},
    ]


@pytest.mark.asyncio
async def test_composite_collector_does_not_hide_nonrecoverable_source_failure() -> None:
    class ForbiddenCollector:
        async def collect(
            self, *, run: object, task: object, question: str
        ) -> tuple[CollectedResearchSource, ...]:
            del run, task, question
            raise ResearchSourceCollectionError("github_authentication_failed")

    with pytest.raises(ResearchSourceCollectionError, match="github_authentication_failed"):
        await CompositeResearchSourceCollector(
            (ForbiddenCollector(),)  # type: ignore[arg-type]
        ).collect(
            run=object(),  # type: ignore[arg-type]
            task=SimpleNamespace(coverage_keys=(), collection_policy=None),
            question="ignored",
        )
